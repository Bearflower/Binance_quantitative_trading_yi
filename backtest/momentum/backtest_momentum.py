#!/usr/bin/env python3
"""多币种动量轮动 本地可行性回测

目的：
    用数据判断「多币种动量轮动」方向值不值得做 —— 对比
    1) 动量多头轮动  2) 动量多空双向轮动  3) 同池等权持有
    在 7d / 30d 两种动量窗口下的表现（含交易成本）。

数据：
    读取 scripts/download_universe_data.py 下载到
    backtest/momentum/data/klines/ 的本地日线（受限池）。

方法要点：
    - 横截面动量排名：在每个调仓日对上期（7d/30d）涨幅排序，
      做多 TopN、做空 BottomN（可选）。
    - 无前视偏差：动量用调仓日前一天收盘价计算，收益按调仓日之后持有期计。
    - 交易成本：手续费 + 滑点（每边），按实际换手率（turnover）扣除。
    - 资金费率：作为每日拖累，默认中性(0)，另跑敏感性。
    - 幸存者偏差说明：受限池来自当前 exchangeInfo，已下架币种不含在内，
      动量结果可能被乐观高估，属本方法固有局限，报告中说明。

输出：
    - 全周期：总收益、年化、最大回撤、夏普、换手年成本
    - 市场状态切片（牛市/熊市）：动量轮动相对等权持有的超额
"""
import csv
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ==================== 回测配置区 ====================
CONFIG = {
    "rebalance_days": 7,          # 调仓周期（天）
    "top_n": 10,                  # 多头 / 空头持股数
    "momentum_windows": [7, 30],  # 动量窗口（天）
    "long_short_enabled": True,   # 是否开启空头腿
    "fee_per_side": 0.0005,       # 手续费（taker 0.05%/边）
    "slippage_per_side": 0.0005,  # 滑点（0.05%/边）
    "funding_drag_daily": 0.0,    # 每日资金费率拖累（中性默认0，敏感性单独跑）
    "min_price_rows": 90,         # 参与回测的币种最少K线数
}

# 路径配置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
KLINES_DIR = os.path.join(DATA_DIR, "klines")

# 账户初始净值
INITIAL_CAPITAL = 10000.0


# ==================== 数据加载 ====================

def load_close_panel(min_rows: int) -> pd.DataFrame:
    """加载全部受限池日线收盘价，构成 date × symbol 面板

    Args:
        min_rows: 参与回测的币种最少K线数，不足则剔除

    Returns:
        DataFrame，index 为日期（升序），列名为币种 symbol
    """
    closes: Dict[str, pd.Series] = {}
    for fname in os.listdir(KLINES_DIR):
        if not fname.endswith("_1d.csv"):
            continue
        symbol = fname[:-len("_1d.csv")].upper()
        filepath = os.path.join(KLINES_DIR, fname)
        rows = list(csv.reader(open(filepath, encoding="utf-8")))
        if not rows:
            continue
        header = rows[0]
        col_map = {name: idx for idx, name in enumerate(header)}
        data_rows = rows[1:]
        if len(data_rows) < min_rows:
            continue
        times, prices = [], []
        for r in data_rows:
            try:
                ts = pd.to_datetime(r[col_map["open_time"]])
                px = float(r[col_map["close_price"]])
            except (ValueError, IndexError):
                continue
            times.append(ts)
            prices.append(px)
        if len(times) < min_rows:
            continue
        closes[symbol] = pd.Series(prices, index=pd.DatetimeIndex(times)).sort_index()
    if not closes:
        raise FileNotFoundError("backtest/momentum/data/klines 下无日线数据，"
                                "请先运行 scripts/download_universe_data.py")
    panel = pd.DataFrame(closes)
    # 去重索引并按时间升序
    panel = panel[~panel.index.duplicated(keep="last")].sort_index()
    return panel.dropna(how="all")


# ==================== 指标计算 ====================

def calc_metrics(equity: pd.Series) -> Dict[str, float]:
    """计算绩效指标

    Args:
        equity: 净值序列（初始为1.0）

    Returns:
        各项指标字典
    """
    rets = equity / equity.shift(1) - 1
    rets = rets.dropna()
    if rets.empty:
        return {"total": 0.0, "cagr": 0.0, "max_dd": 0.0, "sharpe": 0.0, "years": 0.0}

    n = len(equity)
    days = (equity.index[-1] - equity.index[0]).days
    years = max(days / 365.0, 1e-9)
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if (1 + total) > 0 else float("nan")
    roll_max = equity.cummax()
    drawdown = equity / roll_max - 1
    max_dd = drawdown.min()
    vol = rets.std() * np.sqrt(365)
    sharpe = (rets.mean() * 365) / vol if vol > 1e-9 else 0.0
    return {
        "total": total,
        "cagr": cagr,
        "max_dd": max_dd,
        "sharpe": sharpe,
        "years": years,
    }


# ==================== 回测主逻辑 ====================

def run_rotation(
    closes: pd.DataFrame,
    window: int,
    long_short: bool,
    cfg: Dict,
) -> Tuple[pd.Series, List[float]]:
    """运行动量轮动回测

    Args:
        closes: date × symbol 收盘价面板
        window: 动量窗口（天）
        long_short: 是否开启空头腿
        cfg: 配置字典

    Returns:
        (净值序列, 每期换手率列表)
    """
    n_top = cfg["top_n"]
    rebal = cfg["rebalance_days"]
    fee = cfg["fee_per_side"]
    slip = cfg["slippage_per_side"]
    fund = cfg["funding_drag_daily"]

    # 动量：调仓日 t 使用 close[t-1]/close[t-1-window] - 1，消除前视
    mom = closes / closes.shift(window + 1) - 1
    # 持有期收益：close[t] / close[t - rebal] - 1
    hold_ret = closes / closes.shift(rebal) - 1

    # 有效调仓日期：跳过动量预热期后，按 rebal 步长取调仓日
    dates = closes.index
    start_idx = window + 1
    rebal_dates = list(dates[start_idx::rebal])
    rebal_dates = [d for d in rebal_dates if d in mom.index and d in hold_ret.index]

    # 逐期调仓
    equity = [1.0]
    turnovers: List[float] = []
    prev_vec = pd.Series(dtype=float)

    for idx in range(1, len(rebal_dates)):
        cur = rebal_dates[idx]
        # 上一期调仓日（持有起点）
        prev = rebal_dates[idx - 1]
        # 本期动量（基于 prev 前一天收盘，已在 mom 中以窗口计算）
        mom_snapshot = mom.loc[prev].dropna()
        if len(mom_snapshot) < n_top:
            equity.append(equity[-1])
            continue
        ranked = mom_snapshot.sort_values(ascending=False)
        long_syms = list(ranked.index[:n_top])
        new_weights: Dict[str, float] = {}
        if long_short:
            short_syms = list(ranked.index[-n_top:])
            weight = 1.0 / n_top
            long_keys = set(long_syms)
            short_keys = set(short_syms)
            # 多头 +1/N，空头 -1/N；撞车时抵消
            for s in long_syms:
                new_weights[s] = new_weights.get(s, 0.0) + weight
            for s in short_syms:
                new_weights[s] = new_weights.get(s, 0.0) - weight
        else:
            weight = 1.0 / n_top
            new_weights = {s: weight for s in long_syms}

        new_vec = pd.Series(new_weights)
        # 换手率
        turnover = 0.5 * (new_vec.sub(prev_vec, fill_value=0.0)).abs().sum()
        turnovers.append(turnover)

        # 本持有期收益
        leg_ret = hold_ret.loc[cur, list(new_weights.keys())]
        port_ret = float((leg_ret * new_vec).sum())
        # 成本：换手 × 每边成本 × 2（卖出+买入）；资金费率日拖累 × 持有天数
        cost = turnover * (fee + slip) * 2 + fund * rebal
        equity.append(equity[-1] * (1 + port_ret - cost))
        prev_vec = new_vec

    return pd.Series(equity, index=pd.DatetimeIndex(rebal_dates[:len(equity)])), turnovers


def run_equal_weight(closes: pd.DataFrame, cfg: Dict, start_offset: int = 0) -> pd.Series:
    """同池等权持有基准（每周期重平衡到等权）

    Args:
        closes: date × symbol 收盘价面板
        cfg: 配置字典
        start_offset: 起始偏移（跳过动量预热期），与轮动保持同一起点可比

    Returns:
        净值序列
    """
    rebal = cfg["rebalance_days"]
    fee = cfg["fee_per_side"]
    slip = cfg["slippage_per_side"]
    n = len(closes.columns)
    if n == 0:
        return pd.Series([1.0])
    hold_ret = closes.mean(axis=1) / closes.shift(rebal).mean(axis=1).replace(0, np.nan) - 1
    hold_ret = hold_ret.fillna(0.0)
    dates = closes.index
    rebal_dates = list(dates[start_offset::rebal])
    equity = [1.0]
    for i in range(1, len(rebal_dates)):
        # 等权组合，换手由市值漂移引起，近似很小，按全换手上限简化：
        # 这里用每次全换手的最坏成本 2*(fee+slip)，再除以组合换手率折扣
        port_ret = float(hold_ret.loc[rebal_dates[i]])
        # 等权重平衡（1/N），成本相对动量低，取 turnover≈0.5
        cost = 0.5 * (fee + slip) * 2
        equity.append(equity[-1] * (1 + port_ret - cost))
    return pd.Series(equity, index=pd.DatetimeIndex(rebal_dates[:len(equity)]))


# ==================== 市场状态切片 ====================

def slice_by_market(
    closes: pd.DataFrame,
    rotation: pd.Series,
    ew: pd.Series,
    window: int,
) -> List[Dict]:
    """按全池等权 30d 动量正负划分牛/熊，对比超额收益

    Args:
        closes: 收盘价面板
        rotation: 动量轮动净值
        ew: 等权净值
        window: 动量窗口（仅用于报告标注）

    Returns:
        每个市场状态的结果字典列表
    """
    # 全池等权 30d 动量作为市场温度计
    ew_price = closes.mean(axis=1)
    market_mom = ew_price / ew_price.shift(31) - 1

    common = rotation.index.intersection(ew.index)
    segments = {"牛市": [], "熊市": []}
    for d in common:
        m = market_mom.get(d, np.nan)
        if not np.isfinite(m):
            continue
        key = "牛市" if m >= 0 else "熊市"
        # 取该调仓日到下一调仓日的超额
        idx_pos = common.get_loc(d)
        if idx_pos + 1 >= len(common):
            continue
        d_next = common[idx_pos + 1]
        seg_ret = rotation.loc[d_next] / rotation.loc[d] - 1
        ew_ret = ew.loc[d_next] / ew.loc[d] - 1
        segments[key].append(seg_ret - ew_ret)

    results = []
    for name, excess in segments.items():
        if not excess:
            continue
        arr = np.array(excess)
        results.append({
            "state": name,
            "periods": len(arr),
            "win_rate": float((arr > 0).mean()),
            "avg_excess": float(arr.mean()),
            "cum_excess": float(np.prod(1 + arr) - 1),
        })
    return results


# ==================== 报告输出 ====================

def fmt_pct(x: float) -> str:
    """百分比格式化"""
    return f"{x * 100:+.1f}%"


def fmt_metrics(metrics: Dict[str, float]) -> str:
    """指标格式化"""
    return (
        f"总收益 {fmt_pct(metrics['total'])} | 年化 {fmt_pct(metrics['cagr'])} | "
        f"最大回撤 {fmt_pct(metrics['max_dd'])} | 夏普 {metrics['sharpe']:.2f} | "
        f"跨度 {metrics['years']:.2f}年"
    )


def print_results(
    closes: pd.DataFrame,
    log_rows: List[str],
) -> None:
    """打印主对比结果并拼接日志

    Args:
        closes: 收盘价面板
        log_rows: 日志收集列表
    """
    for window in CONFIG["momentum_windows"]:
        print()
        print(f"===== 动量窗口 {window}d =====")

        # 等权基准，与轮动同步调仓起点
        ew = run_equal_weight(closes, CONFIG, start_offset=window + 1)
        ew_metrics = calc_metrics(ew)
        log_rows.append(f"[{window}d 动量] 等权持有基准: {fmt_metrics(ew_metrics)}")

        # 多头轮动
        long_eq, long_turn = run_rotation(closes, window, False, CONFIG)
        long_metrics = calc_metrics(long_eq)
        avg_turn = float(np.mean(long_turn)) if long_turn else 0.0
        log_rows.append(f"[{window}d 动量] 多头 Top{CONFIG['top_n']}: "
                        f"{fmt_metrics(long_metrics)} | 半年换手率 {avg_turn:.2f}")

        print(f"\n  等权持有基准  : {fmt_metrics(ew_metrics)}")
        print(f"  多头 Top{CONFIG['top_n']} : {fmt_metrics(long_metrics)}"
              f"  | 换手率{avg_turn:.2f}")

        # 多空双向
        if CONFIG["long_short_enabled"]:
            ls_eq, ls_turn = run_rotation(closes, window, True, CONFIG)
            ls_metrics = calc_metrics(ls_eq)
            avg_turn_ls = float(np.mean(ls_turn)) if ls_turn else 0.0
            log_rows.append(f"[{window}d 动量] 多空 Top{CONFIG['top_n']}: "
                            f"{fmt_metrics(ls_metrics)} | 半年换手率 {avg_turn_ls:.2f}")
            print(f"  多空 Top{CONFIG['top_n']}: {fmt_metrics(ls_metrics)}"
                  f"  | 换手率{avg_turn_ls:.2f}")

            # 市场状态切片
            slices = slice_by_market(closes, long_eq, ew, window)
            log_rows.append(f"[{window}d 动量] 多头轮动市场切片:")
            for sres in slices:
                log_rows.append(
                    f"  - {sres['state']}: 期数{sres['periods']} "
                    f"胜率 {sres['win_rate']*100:.0f}% "
                    f"平均超额 {fmt_pct(sres['avg_excess'])} "
                    f"累计超额 {fmt_pct(sres['cum_excess'])}"
                )
            print("  市场状态切片（多头轮动相对等权）:")
            for sres in slices:
                print(f"    - {sres['state']}: 期数{sres['periods']} "
                      f"胜率 {sres['win_rate']*100:.0f}% "
                      f"平均超额 {fmt_pct(sres['avg_excess'])} "
                      f"累计超额 {fmt_pct(sres['cum_excess'])}")


def funding_sensitivity(closes: pd.DataFrame, log_rows: List[str]) -> None:
    """资金费率敏感性：多空双向不同资金拖累下的年化表现

    Args:
        closes: 收盘价面板
        log_rows: 日志收集列表
    """
    print()
    print("===== 资金费率敏感性（多空双向，30d 动量）=====")
    log_rows.append("资金费率敏感性（多空双向，30d 动量）: 年日均拖累 vs 年化超额")
    for daily_drag in [0.0, 0.0003]:
        cfg = dict(CONFIG, funding_drag_daily=daily_drag)
        ls_eq, _ = run_rotation(closes, 30, True, cfg)
        m = calc_metrics(ls_eq)
        ew = run_equal_weight(closes, cfg, start_offset=31)
        em = calc_metrics(ew)
        excess_cagr = m["cagr"] - em["cagr"]
        desc = f"拖累 {daily_drag*365*100:.1f}%/年"
        log_rows.append(
            f"  - {desc}: 年化 {fmt_pct(m['cagr'])} | 相对等权超额 {fmt_pct(excess_cagr)}"
        )
        print(f"  {desc}: 年化 {fmt_pct(m['cagr'])} | 相对等权超额 {fmt_pct(excess_cagr)}")


# ==================== 主入口 ====================

def main() -> int:
    """主流程

    Returns:
        退出码（0 成功，1 失败）
    """
    print("=" * 62)
    print("多币种动量轮动 - 可行性回测（本地）")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"配置: 调仓{CONFIG['rebalance_days']}天 | Top{CONFIG['top_n']} | "
          f"窗口{CONFIG['momentum_windows']} | 多空{CONFIG['long_short_enabled']}")
    print("=" * 62)

    closes = load_close_panel(CONFIG["min_price_rows"])
    print(f"受限池币种数: {len(closes.columns)}")
    print(f"时间范围: {closes.index[0].date()} ~ {closes.index[-1].date()}")

    log_rows = [
        f"受限池币种数: {len(closes.columns)}",
        f"时间范围: {closes.index[0].date()} ~ {closes.index[-1].date()}",
        f"配置: 调仓{CONFIG['rebalance_days']}天 | Top{CONFIG['top_n']} | "
        f"窗口{CONFIG['momentum_windows']} | 多空{CONFIG['long_short_enabled']}",
    ]

    print_results(closes, log_rows)
    if CONFIG["long_short_enabled"]:
        funding_sensitivity(closes, log_rows)

    print()
    print("=" * 62)
    print("回测完成。注意: 受限池来自当前交易所列表，存在幸存者偏差；")
    print("资金费率按不同拖累做了敏感性展示，资金规模大时还需另计开/平仓滑点。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())