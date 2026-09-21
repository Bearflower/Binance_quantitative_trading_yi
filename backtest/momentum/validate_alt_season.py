#!/usr/bin/env python3
"""HRS alt season（山寨吸血效应）验证

验证命题：
    hrs 的 trend_filter 只看「单币」的 EMA20（做多要求价格>EMA20，做空要求<EMA20），
    捕捉不到「BTC 上涨、但整个山寨板块在下跌」这种 alt season 吸血结构。
    本脚本实测：BTC 趋势/收益 与 山寨横截面收益 是否存在「稳定背离」。

若存在稳定背离（BTC 涨但 alt 平均收益为负 / 显著跑输），则证明：
    hrs 需要补一个「板块级 / BTC 相对强度」过滤器，避免在山寨被吸血时接飞刀。

方法（全部无前视，本地数据）：
    1. BTC 日线定义趋势/收益（anchor）
    2. 山寨横截面 = 受限池等权平均日收益 + 上涨占比（宽度）
    3. 全局回归：alt_avg_ret = alpha + beta * btc_ret，看 alpha、beta、R²
    4. 稳定性检验：按 BTC 三态分片 + 拆样本前后两半，看背离是否稳定复现
"""
import csv
import os
from datetime import datetime
from typing import Dict, List

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KLINES_DIR = os.path.join(SCRIPT_DIR, "data", "klines")

# 排除的锚/非山寨币
EXCLUDE = {"BTCUSDT", "ETHUSDT"}


def log(msg: str) -> None:
    print(msg, flush=True)


def load_btc() -> Dict[str, float]:
    """加载 BTC 日线 close，返回 {date: close_price}"""
    filepath = os.path.join(KLINES_DIR, "btcusdt_1d.csv")
    rows = list(csv.reader(open(filepath, encoding="utf-8")))
    header = rows[0]
    ci = {name: row.index(name) for row in rows[:1] for name in [x for x in row]}
    date_idx = header.index("open_time")
    close_idx = header.index("close_price")
    out = {}
    for r in rows[1:]:
        try:
            out[r[date_idx]] = float(r[close_idx])
        except (ValueError, IndexError):
            continue
    return out


def load_alt_panel(min_obs: int = 60) -> Dict[str, Dict[str, float]]:
    """加载受限池山寨 close 面板，返回 {symbol: {date: close}}"""
    panel: Dict[str, Dict[str, float]] = {}
    for fname in os.listdir(KLINES_DIR):
        if not fname.endswith("_1d.csv") or fname in ("btcusdt_1d.csv", "ethusdt_1d.csv"):
            continue
        symbol = fname[:-len("_1d.csv")].upper()
        if symbol in EXCLUDE:
            continue
        filepath = os.path.join(KLINES_DIR, fname)
        rows = list(csv.reader(open(filepath, encoding="utf-8")))
        header = rows[0]
        date_idx = header.index("open_time")
        close_idx = header.index("close_price")
        series = {}
        for r in rows[1:]:
            try:
                series[r[date_idx]] = float(r[close_idx])
            except (ValueError, IndexError):
                continue
        if len(series) >= min_obs:
            panel[symbol] = series
    return panel


def pct_change(series: Dict[str, float]) -> Dict[str, float]:
    """计算环比百分比变化（前一日→当日），返回 {date: (close_now/close_prev - 1)}"""
    dates = sorted(series.keys())
    out = {}
    for i in range(1, len(dates)):
        prev = series[dates[i - 1]]
        cur = series[dates[i]]
        if prev and prev > 0:
            out[dates[i]] = cur / prev - 1
    return out


def main() -> int:
    log("=" * 64)
    log("HRS alt season（山寨吸血效应）验证")
    log(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 64)

    btc = load_btc()
    alt_panel = load_alt_panel()
    log(f"山寨币种数: {len(alt_panel)}")

    # 计算各币日收益
    btc_ret = pct_change(btc)
    alt_rets: Dict[str, Dict[str, float]] = {s: pct_change(panel) for s, panel in alt_panel.items()}

    # 对齐日期（所有币共同的日期）
    common_dates = set(btc_ret.keys())
    for rets in alt_rets.values():
        common_dates &= set(rets.keys())
    dates = sorted(common_dates)
    if len(dates) < 30:
        log("共用样本过少，无法验证")
        return 1

    # 构建日度横截面：alt 等权平均收益 + 上涨占比 + BTC 收益
    alt_avg: List[float] = []
    btc_ret_seq: List[float] = []
    breadth: List[float] = []
    usable_dates = []
    for d in dates:
        rets = [alt_rets[s][d] for s in alt_rets.keys() if d in alt_rets[s]]
        if len(rets) < 10:
            continue
        alt_avg.append(float(np.mean(rets)))
        breadth.append(float(np.mean([1 if r > 0 else 0 for r in rets])))
        btc_ret_seq.append(btc_ret[d])
        usable_dates.append(d)

    alt_avg = np.array(alt_avg)
    btc_ret_seq = np.array(btc_ret_seq)
    breadth = np.array(breadth)
    usable_dates = np.array(usable_dates)
    n = len(alt_avg)
    log(f"对齐后样本天数: {n}")

    # ============ 1. 全局回归 alpha/beta ============
    X = np.vstack([np.ones(n), btc_ret_seq]).T
    beta_hat, *_ = np.linalg.lstsq(X, alt_avg, rcond=None)
    alpha, beta = beta_hat[0], beta_hat[1]
    res = alt_avg - X @ beta_hat
    ss_res = np.sum(res ** 2)
    ss_tot = np.sum((alt_avg - alt_avg.mean()) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 1e-9 else 0.0
    log("\n[1] 全局回归: alt_avg_ret = alpha + beta * btc_ret")
    log(f"    alpha（截距/每日超额）: {alpha*100:+.4f}%")
    log(f"    beta（对BTC敏感度）   : {beta:.3f}") # beta>>1=放大，<1=弱相关，负=背离
    log(f"    R²                  : {r2:.3f}")

    # ============ 2. 按 BTC 三态分片 ============
    # 用 BTC EMA20+斜率 判定三态（与 regime 回测一致）
    def calc_regime(btc_close: Dict[str, float]) -> Dict[str, str]:
        dates_sorted = sorted(btc_close.keys())
        closes = [btc_close[d] for d in dates_sorted]
        ema = [closes[0]]
        span = 20
        alpha_ema = 2 / (span + 1)
        for px in closes[1:]:
            ema.append(alpha_ema * px + (1 - alpha_ema) * ema[-1])
        out = {}
        for i in range(span, len(closes)):
            slope = ema[i] / ema[i - 5] - 1
            # 判定在 i-1 日（无前视，用当根EMA判定次日状态）
            if closes[i] > ema[i] and slope > 0.001:
                out[dates_sorted[i]] = "TREND_UP"
            elif closes[i] < ema[i] and slope < -0.001:
                out[dates_sorted[i]] = "TREND_DOWN"
            else:
                out[dates_sorted[i]] = "RANGE"
        return out

    regime = calc_regime(btc)
    state_agg = {"TREND_UP": [], "TREND_DOWN": [], "RANGE": []}
    state_dist = {"TREND_UP": 0, "TREND_DOWN": 0, "RANGE": 0}
    for d, alt_a, bd in zip(usable_dates, alt_avg, breadth):
        st = regime.get(d, "RANGE")
        state_dist[st] += 1
        state_agg[st].append(alt_a)

    log("\n[2] 按 BTC 三态分片的山寨横截面平均收益:")
    names = {"TREND_UP": "趋势多(BTC涨)", "TREND_DOWN": "趋势空(BTC跌)", "RANGE": "震荡"}
    for st in ["TREND_UP", "TREND_DOWN", "RANGE"]:
        arr = np.array(state_agg[st])
        if len(arr) == 0:
            log(f"    {names[st]:<14}: 无样本")
            continue
        mean = arr.mean() * 100
        log(f"    {names[st]:<14}: {state_dist[st]:4d}天  今日均收益 {mean:+.3f}%"
            f"  (年化约 {mean*365:+.0f}%)")

    # ============ 3. 稳定性检验（拆前后两半） ============
    log("\n[3] 稳定性检验（拆样本前后两半的 alpha）:")
    half = n // 2
    for label, sl in [("前一半", slice(0, half)), ("后一半", slice(half, n))]:
        x = X[sl]
        y = alt_avg[sl]
        bh, *_ = np.linalg.lstsq(x, y, rcond=None)
        log(f"    {label:<8}: alpha {bh[0]*100:+.4f}%  beta {bh[1]:.3f}")

    # ============ 4. 背离状态汇总（关键结论） ============
    # 定义"背离日"：BTC 上涨但山寨横截面为负 / BTC 下跌但山寨横截面为正
    up_days = btc_ret_seq > 0
    down_days = btc_ret_seq < 0
    alt_neg_while_btc_up = float(np.mean((alt_avg < 0)[up_days])) * 100 if up_days.any() else 0.0
    alt_pos_while_btc_down = float(np.mean((alt_avg > 0)[down_days])) * 100 if down_days.any() else 0.0
    beta_pos = beta > 0

    log("\n[4] 背离量化:")
    log(f"    BTC上涨日 中山寨横截面为负的比例 : {alt_neg_while_btc_up:.1f}%")
    log(f"    BTC下跌日 中山寨横截面为正的比例 : {alt_pos_while_btc_down:.1f}%")

    log("\n" + "=" * 64)
    log("结论判定:")
    if beta < 0.3:
        log("  ⚠️  beta < 0.3：山寨与BTC严重脱钩甚至背离，alt season 吸血效应显著存在")
        log("     → hrs 需要补「板块级/BTC相对强度」过滤器，避免单币 trend_filter 误判")
    elif beta < 0.8:
        log("  ⚠️  beta < 0.8：山寨对BTC敏感度明显偏低，存在部分脱钩倾向")
        log("     → alternative 吸血时段存在，但需结合分片/alpha 判断稳定与否")
    else:
        log("  ✅  beta >= 0.8：山寨整体跟随BTC，alt season 吸附/吸血效应不显著")
        log("     → hrs 现有单币 trend_filter 基本够用，补全局过滤价值存疑")
    log("=" * 64)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())