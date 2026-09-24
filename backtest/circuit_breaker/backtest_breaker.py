#!/usr/bin/env python3
"""
组合级单边行情空头熔断 - 阈值回测（本地执行，禁止在服务器运行）

数据来源（本地 data/ 目录，从服务器 PGSQL 导出，均为 UTC）：
  - klines/{symbol}_1h.csv     : 固定 5 币（BTC/ETH/BNB/SOL/XRP）1h K线
  - hrs_pool_klines.csv        : HRS 活跃池（62 标的）1h K线
  - hrs_active_pool.txt        : HRS 活跃池标的列表
  - trade_records_all.csv      : 三策略全部交易记录

指数口径（与需求文档一致）：
  index_hour = B ⟺ mean(池内每标的 ret[B-1h])，ret[T] = close[T]/close[T-1h] - 1
  - 变体 A：仅固定 5 币（最小指数）
  - 变体 B：固定 5 币 + HRS 池合并等权（生产口径；HRS 池历史用当前池近似）

空单仓位重建：
  SHORT 开仓 = SELL + LIMIT/MARKET + NEW；平仓盈亏 = BUY 行 realized_pnl 累加，
  边界 = 同 (strategy,symbol) 的下一次开仓。

拦截评估：MTPCS 原版/激进版读 T 指数，HRS 读 T-1 指数；滞回状态机扫描
trigger∈[2%,6%] × release∈[1%,3%]。

用法：python3 backtest_breaker.py [variant]
  variant: A=仅固定5币, B=固定5币+HRS池(默认)
"""
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

DATA = Path(__file__).parent / "data"
FIXED_POOL = ["btcusdt", "ethusdt", "bnbusdt", "solusdt", "xrpusdt"]
SHORT_STRATEGIES = {"MTPCS策略", "MTPCS激进策略", "HRS策略"}
ENTRY_ORDER_TYPES = {"LIMIT", "MARKET"}
MIN_SYMBOLS = 3
HOUR = pd.Timedelta(hours=1)
INCIDENT_DAY = pd.Timestamp("2026-09-21")  # 复盘事故日


def load_fixed_klines(symbol: str) -> pd.Series:
    """读取固定币 K线收盘价序列（index=open_time, value=close）"""
    df = pd.read_csv(DATA / f"klines/{symbol}_1h.csv", parse_dates=["open_time"])
    closes = df.set_index("open_time")["close_price"].astype(float).sort_index()
    return closes[~closes.index.duplicated(keep="last")]


def load_hrs_pool_klines() -> dict:
    """读取 HRS 池 K线（symbol -> close series）"""
    df = pd.read_csv(DATA / "hrs_pool_klines.csv", parse_dates=["open_time"],
                     header=None, names=["symbol", "open_time", "close_price"])
    out = {}
    for symbol, g in df.groupby("symbol"):
        g = g.set_index("open_time")["close_price"].astype(float).sort_index()
        out[symbol] = g[~g.index.duplicated(keep="last")]
    return out


def get_pool(variant: str) -> dict:
    """返回池内各标的的收盘价序列 dict"""
    closes = {s: load_fixed_klines(s) for s in FIXED_POOL}
    if variant == "B":
        hrs = load_hrs_pool_klines()
        closes.update(hrs)  # 键为 HRS 原始 symbol（如 SAMSUNGUSDT）
    return closes


def build_equal_weight_index(closes: dict) -> pd.Series:
    """构建等权指数序列（index_hour 口径）"""
    ret_frames = []
    for symbol, series in closes.items():
        ret = series / series.shift(1) - 1.0
        ret.name = symbol
        ret_frames.append(ret)
    frame = pd.concat(ret_frames, axis=1)
    shifted = frame.shift(periods=1, freq="1h")
    idx = shifted.mean(axis=1, skipna=True)
    idx[shifted.notna().sum(axis=1) < MIN_SYMBOLS] = pd.NA
    return idx.sort_index()


def load_trades() -> pd.DataFrame:
    """读取三策略交易记录"""
    df = pd.read_csv(DATA / "trade_records_all.csv", parse_dates=["executed_at"])
    return df[df["strategy"].isin(SHORT_STRATEGIES)]


def reconstruct_short_positions(df: pd.DataFrame) -> list:
    """按 (strategy, symbol) 时间线重建空单仓位"""
    df = df.sort_values(["strategy", "symbol", "executed_at"])
    positions = []
    for (strategy, symbol), group in df.groupby(["strategy", "symbol"]):
        current_open, acc_pnl = None, 0.0
        for row in group.itertuples():
            is_entry = (
                row.side == "SELL"
                and row.order_type in ENTRY_ORDER_TYPES
                and row.status == "NEW"
            )
            if is_entry:
                if current_open is not None:
                    positions.append(
                        {"strategy": strategy, "symbol": symbol,
                         "open_time": current_open, "total_pnl": acc_pnl})
                current_open, acc_pnl = row.executed_at, 0.0
            elif row.side == "BUY" and pd.notna(row.realized_pnl):
                acc_pnl += float(row.realized_pnl)
        if current_open is not None:
            positions.append(
                {"strategy": strategy, "symbol": symbol,
                 "open_time": current_open, "total_pnl": acc_pnl})
    return positions


def effective_index_hour(strategy: str, open_time: pd.Timestamp) -> pd.Timestamp:
    """策略开仓时实际读取的 index_hour（MTPCS 读 T，HRS 整点执行读 T-1）"""
    hour = open_time.floor("h")
    if strategy == "HRS策略":
        return hour - HOUR
    return hour


def build_forward_returns(closes: dict) -> dict:
    """池等权前向收益（触发小时之后的池平均涨幅）"""
    base = pd.concat(closes, axis=1).mean(axis=1)
    fwd1 = base / base.shift(1) - 1.0
    fwd4 = base / base.shift(4) - 1.0
    return {"fwd_1h": fwd1.shift(periods=1, freq="1h"),
            "fwd_4h": fwd4.shift(periods=1, freq="1h")}


def simulate(trigger: float, release: float, idx: pd.Series,
             positions: list, forward: dict) -> dict:
    """滞回状态机重放 + 空单拦截评估"""
    state, triggered_hours = False, set()
    for hour, value in idx.items():
        if pd.isna(value):
            continue
        if state:
            if value < release:
                state = False
        elif value > trigger:
            state = True
        if state:
            triggered_hours.add(hour)

    evaluated, intercepted, not_intercepted = [], [], []
    for pos in positions:
        eh = effective_index_hour(pos["strategy"], pos["open_time"])
        if eh not in idx.index or pd.isna(idx.loc[eh]):
            continue
        evaluated.append(pos)
        (intercepted if eh in triggered_hours else not_intercepted).append(pos["total_pnl"])

    def stats(pnls):
        n = len(pnls)
        return {"count": n, "sum": sum(pnls), "win": sum(1 for p in pnls if p > 0),
                "avg": sum(pnls) / n if n else 0.0}

    is_, ns = stats(intercepted), stats(not_intercepted)
    valid = {h for h in triggered_hours
             if h in forward["fwd_1h"].index and pd.notna(forward["fwd_1h"].loc[h])}
    allh = [h for h in idx.index if pd.notna(idx.loc[h])
            and h in forward["fwd_1h"].index and pd.notna(forward["fwd_1h"].loc[h])]

    def avg(series, hours):
        vals = [series.loc[h] for h in hours if h in series.index and pd.notna(series.loc[h])]
        return sum(vals) / len(vals) if vals else float("nan")

    return {
        "trigger": trigger, "release": release,
        "triggered_hours": len(triggered_hours), "total_hours": len(allh),
        "intercepted": is_, "not_intercepted": ns,
        "fwd1_trig": avg(forward["fwd_1h"], valid),
        "fwd4_trig": avg(forward["fwd_4h"], valid),
        "fwd1_nontrig": avg(forward["fwd_1h"], set(allh) - triggered_hours),
        "fwd4_nontrig": avg(forward["fwd_4h"], set(allh) - triggered_hours),
    }


def summarize_index(idx: pd.Series, label: str) -> None:
    vals = idx.dropna()
    print(f"\n=== {label}：等权指数分布（样本小时数=%d） ===" % len(vals))
    for t in [0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.06]:
        print("  涨幅 > %.1f%% : %4d 小时 (%.2f%%)" % (
            t * 100, int((vals > t).sum()), 100 * (vals > t).mean()))
    print("  mean=%.4f  median=%.4f  min=%.4f  max=%.4f" % (
        vals.mean(), vals.median(), vals.min(), vals.max()))


def incident_analysis(idx: pd.Series, positions: list) -> None:
    """复盘 09-21 事故日：该日空单开仓时点指数值"""
    print("\n=== 09-21 事故日空单开仓与当日指数（触发时点） ===")
    day_pos = [p for p in positions if p["open_time"].normalize() == INCIDENT_DAY]
    day_pos.sort(key=lambda p: p["open_time"])
    for p in day_pos:
        eh = effective_index_hour(p["strategy"], p["open_time"])
        val = idx.loc[eh] if eh in idx.index else float("nan")
        print("  %-12s %-14s 开仓 %s  指数=%.3f%%  已实现盈亏=%+7.2f" % (
            p["strategy"], p["symbol"], p["open_time"].strftime("%m-%d %H:%M"),
            val * 100, p["total_pnl"]))
    print(f"  事故日共 {len(day_pos)} 笔空单开仓")


def main() -> None:
    variant = (sys.argv[1].upper() if len(sys.argv) > 1 else "B")
    if variant not in ("A", "B"):
        print("参数错误：variant 仅支持 A(固定5币) / B(固定5币+HRS池)")
        sys.exit(1)

    print("加载池内 K线并构建等权指数（变体 %s）..." % variant)
    closes = get_pool(variant)
    idx = build_equal_weight_index(closes)
    forward = build_forward_returns(closes)
    trades = load_trades()
    positions = reconstruct_short_positions(trades)

    print("\n=== 空单仓位重建结果 ===")
    for s, c in Counter(p["strategy"] for p in positions).items():
        print(f"  {s}: {c} 笔空单")
    ev = [p for p in positions
          if effective_index_hour(p["strategy"], p["open_time"]) in idx.dropna().index]
    ev_pnl = sum(p["total_pnl"] for p in ev)
    print(f"  可评估仓位: {len(ev)} / {len(positions)}，已实现盈亏合计 {ev_pnl:.2f} USDT")
    print(f"  指数时间范围: {idx.dropna().index.min()} ~ {idx.dropna().index.max()}")

    summarize_index(idx, f"变体 {variant}")

    incident_analysis(idx, positions)

    print("\n=== 阈值扫描结果（变体 %s） ===" % variant)
    print("trigger | release | 触发小时 | 拦截空单 | 拦截单合计盈亏 | 未拦空单合计盈亏 | 触发后1h池涨 | 触发后4h池涨 | 未触发1h池涨")
    results = []
    for trigger in [0.02, 0.03, 0.04, 0.05, 0.06]:
        for release in [0.01, 0.015, 0.02, 0.025, 0.03]:
            if release >= trigger:
                continue
            r = simulate(trigger, release, idx, positions, forward)
            results.append(r)
            nh = r["total_hours"] or 1
            print("  %5.1f%% | %5.1f%% | %5.1f%% (%4dh) | %3d | %+10.2f | %+10.2f | %+.3f%% | %+.3f%% | %+.3f%%" % (
                trigger * 100, release * 100, 100 * r["triggered_hours"] / nh,
                r["triggered_hours"], r["intercepted"]["count"], r["intercepted"]["sum"],
                r["not_intercepted"]["sum"], r["fwd1_trig"] * 100, r["fwd4_trig"] * 100,
                r["fwd1_nontrig"] * 100))

    # 推荐组合的分策略明细
    for trigger, release in [(0.02, 0.01), (0.02, 0.015), (0.03, 0.02), (0.04, 0.02)]:
        r = simulate(trigger, release, idx, positions, forward)
        print(f"\n--- 推荐组合 trigger={trigger:.0%} release={release:.0%} 分策略明细 ---")
        for strat in ["MTPCS策略", "MTPCS激进策略", "HRS策略"]:
            sub = [p for p in positions
                   if p["strategy"] == strat
                   and effective_index_hour(strat, p["open_time"]) in idx.dropna().index]
            state, trig = False, set()
            for hour, value in idx.items():
                if pd.isna(value):
                    continue
                if state:
                    if value < release:
                        state = False
                elif value > trigger:
                    state = True
                if state:
                    trig.add(hour)
            inter = [p for p in sub if effective_index_hour(strat, p["open_time"]) in trig]
            ni = [p for p in sub if effective_index_hour(strat, p["open_time"]) not in trig]
            si, sn = sum(p["total_pnl"] for p in inter), sum(p["total_pnl"] for p in ni)
            print("  %-10s 可评估=%2d 拦截=%2d (盈亏%+7.2f)  未拦=%2d (盈亏%+7.2f)" % (
                strat, len(sub), len(inter), si, len(ni), sn))

    with open(DATA / f"backtest_result_{variant}.csv", "w", newline="") as f:
        import csv
        w = csv.writer(f)
        w.writerow(["trigger", "release", "triggered_hours", "total_hours",
                    "intercepted_count", "intercepted_pnl_sum", "not_intercepted_pnl_sum",
                    "fwd1_trig", "fwd4_trig"])
        for r in results:
            w.writerow([r["trigger"], r["release"], r["triggered_hours"], r["total_hours"],
                        r["intercepted"]["count"], round(r["intercepted"]["sum"], 4),
                        round(r["not_intercepted"]["sum"], 4),
                        round(r["fwd1_trig"], 6), round(r["fwd4_trig"], 6)])
    print("\n结果已保存: %s" % (DATA / f"backtest_result_{variant}.csv"))


if __name__ == "__main__":
    main()
