"""统计套利 · 配对交易 — 第二步：价差 z-score 回归回测（净值曲线版）

对第一步筛选出的稳健协整对，用价差 z-score 均值回归策略做回测：
    - 入场：|z| > ENTRY_Z（价差偏离均值过大）
    - 出场：|z| < EXIT_Z（价差回归到均值区间）
    - 断裂：|z| 继续走阔超 STOP_Z → 认亏平仓（统计为断裂事件）
    - 双腿做多低估/做空高估，费用按实际换手扣入净值

与 V1（点位累计）的关键区别：
    1. 收益按「净值曲线」口径：每对独立账户，等资金仓位，日度盯市，
       PnL 按双腿市值比例与 beta 对冲，逐日累加为组合净值。
    2. 组合层只输出真实年化 / 夏普 / 最大回撤 / 盈亏占比。
elimination新币过滤（min_history）+ 共同锚去重必要说明。

严格本地执行，严禁在服务器运行回测。
"""
import os
import sys
from datetime import datetime
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pairs_engine import (  # noqa: E402
    MIN_HISTORY,
    coint_stats,
    dedup_anchors,
    filter_min_history,
    is_robust,
    load_panel,
)

# ---------- 回测参数（成本贴近实盘永续） ----------
# 手续费：taker 0.05%，滑点 0.05% /腿/边；单笔双边 4 次经手
TAKER_FEE = 0.0005
SLIPPAGE = 0.0005
COST_PER_LEG = TAKER_FEE + SLIPPAGE   # 单腿单边成本
HALF_ROUND_COST = COST_PER_LEG * 2    # 开仓(两腿) 或 平仓(两腿)

ENTRY_Z = 2.0      # 入场偏离
EXIT_Z = 0.3       # 出场回归阈值
STOP_Z = 4.0       # 断裂/止损阈值

LOOKBACK = 60      # 滚动 z 估计窗口（交易日）
# 每对分配的名义资金单位（等权净值基准）
PAIR_NOTIONAL = 1.0


def log(msg: str) -> None:
    print(msg, flush=True)


def _rolling_z(spread: np.ndarray, lookback: int) -> np.ndarray:
    """无前视滚动 z-score（只用过去 lookback 天）"""
    span = min(lookback, max(10, len(spread) // 2))
    s = pd.Series(spread)
    mu = s.rolling(span, min_periods=10).mean()
    sd = s.rolling(span, min_periods=10).std(ddof=0)
    z = (s - mu) / sd.replace(0, np.nan)
    return z.fillna(0.0).to_numpy()


def _pair_equity(
    log_a: np.ndarray,
    log_b: np.ndarray,
    beta: float,
    entry_z: float = ENTRY_Z,
    exit_z: float = EXIT_Z,
    stop_z: float = STOP_Z,
) -> Tuple[np.ndarray, Dict[str, float]]:
    """回测单个配对，返回逐日净值曲线与统计量

    净值口径：
        - 账户初始资金 = PAIR_NOTIONAL
        - 开仓做多价差(买B卖A)：B 多头名义 = P，A 空头名义 = beta*P（对冲）
        - 每日盯市收益 = P * r_B - beta*P * r_A（r 为当日对数收益）
        - 开/平仓各扣 HALF_ROUND_COST 比例的手续费+滑点

    Returns:
        (equity_curve, stats)
    """
    spread = log_b - beta * log_a
    z = _rolling_z(spread, LOOKBACK)
    n = len(spread)
    ret_a = np.zeros_like(log_a)  # 对数日收益
    ret_b = np.zeros_like(log_b)
    ret_a[1:] = log_a[1:] - log_a[:-1]
    ret_b[1:] = log_b[1:] - log_b[:-1]

    P = PAIR_NOTIONAL
    position = 0      # 0 空仓；+1 做多价差(买B卖A)；-1 做空价差(卖B买A)
    entry_day = -1
    equity = np.ones(n)
    cost_acc = 0.0
    gross_acc = 0.0
    n_trades = 0
    n_breaks = 0
    wins = 0
    closed: List[float] = []

    for i in range(1, n):
        z_i = z[i]
        # 入场
        if position == 0 and abs(z_i) >= entry_z:
            if 0 < i < n:
                # 用 i-1 的 z 确认刚穿越（避免同根 K 反复进出）
                z_prev = z[i - 1]
                if abs(z_prev) < entry_z:
                    position = 1 if z_i < 0 else -1
                    entry_day = i
                    n_trades += 1
                    cost_acc += HALF_ROUND_COST
                    # 开场当天无浮动收益（当根不计）
        # 持仓浮盈（从下一根起）
        if position != 0 and i > entry_day:
            delta = ret_b[i] - beta * ret_a[i]
            pnl_day = position * P * delta
            equity[i] = equity[i - 1] * (1 + pnl_day / P)
            gross_acc += pnl_day
        elif position == 0:
            equity[i] = equity[i - 1]

        # 出场判定
        if position != 0 and i > entry_day:
            broke = (z_i <= -stop_z) if position == 1 else (z_i >= stop_z)
            reverted = abs(z_i) <= exit_z
            if broke or reverted:
                # 平仓扣两腿成本（按当前账户净值比例）
                fee = equity[i] * HALF_ROUND_COST
                equity[i] -= fee
                cost_acc += HALF_ROUND_COST
                cur_ret = equity[i] / equity[entry_day] - 1
                closed.append(cur_ret)
                if cur_ret > 0:
                    wins += 1
                if broke:
                    n_breaks += 1
                position = 0

    stats = {
        "n_trades": n_trades,
        "win_rate": wins / n_trades if n_trades else 0.0,
        "cost": cost_acc,
        "n_breaks": n_breaks,
        "closed_trades": len(closed),
    }
    return equity, stats


def _perf(equity: np.ndarray) -> Dict[str, float]:
    """从净值曲线计算年化/夏普/最大回撤"""
    if len(equity) < 2 or equity[-1] <= 0:
        return {"ann": 0.0, "sharpe": 0.0, "maxdd": 0.0, "net": 0.0}
    rets = np.diff(equity) / equity[:-1]
    days = len(equity)
    net = equity[-1] - 1.0
    ann = (equity[-1]) ** (365.0 / days) - 1.0 if equity[-1] > 0 else -1.0
    sharpe = 0.0
    if rets.std(ddof=0) > 0:
        sharpe = (rets.mean() / rets.std(ddof=0)) * np.sqrt(365.0)
    peak = np.maximum.accumulate(equity)
    dd = equity / peak - 1.0
    maxdd = float(dd.min())
    return {"ann": ann, "sharpe": float(sharpe), "maxdd": maxdd, "net": net}


def main() -> int:
    log("=" * 72)
    log("统计套利 · 配对交易 第二步：价差 z-score 回归回测（净值曲线版 v2）")
    log(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"成本: taker {TAKER_FEE*100:.2f}% + 滑点 {SLIPPAGE*100:.2f}% /腿/边")
    log(f"阈值: 入场 |z|>{ENTRY_Z} 出场 |z|<{EXIT_Z} 断裂止损 |z|>{STOP_Z}")
    log("=" * 72)

    panel = load_panel()
    log_panel = np.log(panel)
    symbols = list(panel.columns)
    # 每币有效样本数（用于新币过滤）
    obs_count = panel.notna().sum().to_dict()
    log(f"参与回测币种: {len(symbols)} | 时间 {panel.index[0].date()} ~ {panel.index[-1].date()}")

    from itertools import combinations
    cand: List[Tuple[str, str, Dict[str, float]]] = []
    for a, b in combinations(symbols, 2):
        mask = panel[[a, b]].notna().all(axis=1)
        if mask.sum() < 90:
            continue
        stats = coint_stats(log_panel[a][mask].values, log_panel[b][mask].values)
        if is_robust(stats):
            cand.append((a, b, stats))
    log(f"稳健协整候选对: {len(cand)}")

    cand = filter_min_history(cand, obs_count)
    log(f"剔除上市不足 {MIN_HISTORY} 天后: {len(cand)} 对")

    deduped = dedup_anchors(cand)
    log(f"共同锚去重后: {len(deduped)} 对\n")

    if not deduped:
        log("❌ 无可用候选对 → 方向终结")
        return 1

    # ---- 逐对回测 ----
    # 逐对返回 (对, 逐日收益数组, 对齐到全面板统一日期轴)
    per_pair = []
    for a, b, stats in deduped:
        mask = panel[[a, b]].notna().all(axis=1)
        la = log_panel[a][mask].values
        lb = log_panel[b][mask].values
        if len(la) < 90:
            continue
        eq, st = _pair_equity(la, lb, stats["beta"])
        p = _perf(eq)
        per_pair.append((a, b, p, st, eq))

    # 组合级：先把所有对的净值曲线对齐到统一日期索引（全面板时间轴），
    # 再在每日取"当日有数据且在场"的对做等权平均收益。
    long_panel = pd.DataFrame(index=panel.index)
    for a, b, _, _, eq in per_pair:
        mask = panel[[a, b]].notna().all(axis=1)
        idx = panel.index[mask]
        long_panel[f"{a}|{b}"] = pd.Series(eq, index=idx)
    # 每日组合收益：当日所有在场(有净值)对的均值（跳过当日缺失的券）
    combo_rets = long_panel.pct_change().mean(axis=1, skipna=True).fillna(0.0)
    combo_eq = (1 + combo_rets).cumprod()
    combo_perf = _perf(combo_eq.to_numpy())

    per_pair.sort(key=lambda x: -x[2]["ann"])
    log(f"{'A':<12}{'B':<12}{'年化':>8}{'夏普':>6}{'回撤':>9}{'交易':>5}{'胜率':>6}{'断裂':>5}")
    for a, b, pp, st, _eq in per_pair:
        log(f"{a:<12}{b:<12}{pp['ann']*100:>7.1f}%{pp['sharpe']:>6.2f}{pp['maxdd']*100:>8.1f}%"
            f"{st['n_trades']:>5}{st['win_rate']*100:>5.0f}%{st['n_breaks']:>5}")

    profitable = sum(1 for _, _, pp, _, _ in per_pair if pp["net"] > 0)
    total_breaks = sum(st["n_breaks"] for _, _, _, st, _ in per_pair)
    total_trades = sum(st["n_trades"] for _, _, _, st, _ in per_pair)

    log("\n" + "=" * 72)
    log("组合级结果（等权所有去重后对，净值曲线口径）:")
    log(f"  对 数: {len(per_pair)}  盈利对: {profitable} ({profitable/len(per_pair)*100:.1f}%)")
    log(f"  总交易: {total_trades}  断裂事件: {total_breaks}")
    log(f"  组合年化: {combo_perf['ann']*100:.2f}%")
    log(f"  组合夏普: {combo_perf['sharpe']:.2f}")
    log(f"  组合最大回撤: {combo_perf['maxdd']*100:.2f}%")
    log(f"  组合累计净收益: {combo_perf['net']*100:.2f}%")
    log("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())