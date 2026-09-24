#!/usr/bin/env python3
"""
组合级空头熔断 - 指数口径对比回测（本地执行）

对比口径：
  1. 全池(5币+HRS 62标的)等权，无 cap
  2. 全池等权，单币涨幅 cap ±10%
  3. 分池平权：MTPCS(5币) 与 HRS(62币) 各自独立指数
  4. 分池：MTPCS 平权 + HRS cap±10% 平权

指数口径：index_hour = B ⟺ mean(池内每标的 ret[B-1h])，ret[T]=close[T]/close[T-1h]-1
熔断评估：MTPCS原版/激进版读 T 指数，HRS 读 T-1 指数；仅统计指数有效的仓位。
"""
import glob
from pathlib import Path

import pandas as pd

DATA = Path(__file__).parent / "data"
FIXED = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]
SHORT_STRATEGIES = {"MTPCS策略", "MTPCS激进策略", "HRS策略"}
MIN_SYMBOLS = 3


def load_closes() -> dict:
    """加载全部标的收盘价序列（键=symbol，如 BTCUSDT）"""
    closes = {}
    for f in sorted(glob.glob(str(DATA / "klines/*_1h.csv"))):
        sym = Path(f).stem.replace("_1h", "").upper()
        df = pd.read_csv(f, parse_dates=["open_time"])
        closes[sym] = (
            df.set_index("open_time")["close_price"].astype(float).sort_index()
        )
        closes[sym] = closes[sym][~closes[sym].index.duplicated(keep="last")]
    dfh = pd.read_csv(
        DATA / "hrs_pool_klines.csv", header=None,
        names=["symbol", "open_time", "close_price"], parse_dates=["open_time"])
    for s, g in dfh.groupby("symbol"):
        closes[s] = g.set_index("open_time")["close_price"].astype(float).sort_index()
        closes[s] = closes[s][~closes[s].index.duplicated(keep="last")]
    return closes


def build_idx(closes: dict, syms: list, cap=None) -> pd.Series:
    """构建等权指数（index_hour 口径）"""
    ret_frames = []
    for s in syms:
        ret = closes[s] / closes[s].shift(1) - 1.0
        if cap is not None:
            ret = ret.clip(-cap, cap)
        ret.name = s  # 必须设置列名，避免 concat 时同名去重
        ret_frames.append(ret)
    frame = pd.concat(ret_frames, axis=1)
    shifted = frame.shift(periods=1, freq="1h")
    idx = shifted.mean(axis=1, skipna=True)
    idx[shifted.notna().sum(axis=1) < MIN_SYMBOLS] = pd.NA
    return idx.sort_index()


def load_positions() -> list:
    """按 (strategy, symbol) 时间线重建空单仓位"""
    tr = pd.read_csv(DATA / "trade_records_all.csv", parse_dates=["executed_at"])
    tr = tr[tr["strategy"].isin(SHORT_STRATEGIES)].sort_values(
        ["strategy", "symbol", "executed_at"])
    positions = []
    for (strat, sym), group in tr.groupby(["strategy", "symbol"]):
        cur, acc = None, 0.0
        for row in group.itertuples():
            is_entry = (
                row.side == "SELL" and row.order_type in ("LIMIT", "MARKET")
                and row.status == "NEW")
            if is_entry:
                if cur is not None:
                    positions.append(
                        {"strategy": strat, "symbol": sym,
                         "open_time": cur, "pnl": acc})
                cur, acc = row.executed_at, 0.0
            elif row.side == "BUY" and pd.notna(row.realized_pnl):
                acc += float(row.realized_pnl)
        if cur is not None:
            positions.append(
                {"strategy": strat, "symbol": sym,
                 "open_time": cur, "pnl": acc})
    return positions


def eh(strat: str, t: pd.Timestamp) -> pd.Timestamp:
    """策略开仓时实际读取的 index_hour"""
    h = t.floor("h")
    return h - pd.Timedelta(hours=1) if strat == "HRS策略" else h


def triggered_hours(idx: pd.Series, trigger: float, release: float) -> set:
    """滞回状态机，返回被触发的小时集合"""
    state, out = False, set()
    for hour, value in idx.items():
        if pd.isna(value):
            continue
        if state:
            if value < release:
                state = False
        elif value > trigger:
            state = True
        if state:
            out.add(hour)
    return out


def scan(idx_map: dict, positions: list, trigger: float, release: float) -> None:
    """按策略读各自指数，统计拦截/未拦盈亏"""
    trig = {s: triggered_hours(idx_map[s].dropna(), trigger, release)
            for s in SHORT_STRATEGIES}
    ev, inter, ni = [], [], []
    for p in positions:
        h = eh(p["strategy"], p["open_time"])
        if h not in idx_map[p["strategy"]].dropna().index:
            continue
        ev.append(p)
        (inter if h in trig[p["strategy"]] else ni).append(p)
    si, sn = sum(p["pnl"] for p in inter), sum(p["pnl"] for p in ni)
    print("  trigger=%.0f%% release=%.0f%%: 可评估=%d 拦截=%d(盈亏%+.2f) 未拦=%d(盈亏%+.2f)" % (
        trigger * 100, release * 100, len(ev), len(inter), si, len(ni), sn))
    for s in ["MTPCS策略", "MTPCS激进策略", "HRS策略"]:
        i2 = [p for p in inter if p["strategy"] == s]
        n2 = [p for p in ni if p["strategy"] == s]
        print("      %-10s 拦截=%d(盈亏%+7.2f) 未拦=%d(盈亏%+7.2f)" % (
            s, len(i2), sum(p["pnl"] for p in i2), len(n2),
            sum(p["pnl"] for p in n2)))


def dist(idx: pd.Series, label: str) -> None:
    vals = idx.dropna()
    print("  %s: max=%5.2f%%  中位=%5.2f%%  >2%%:%3dh  >3%%:%2dh  >4%%:%2dh" % (
        label, vals.max() * 100, vals.median() * 100,
        int((vals > 0.02).sum()), int((vals > 0.03).sum()),
        int((vals > 0.04).sum())))


def main() -> None:
    closes = load_closes()
    hrs_syms = [s for s in closes if s not in FIXED]
    positions = load_positions()
    print("空单仓位: %d 笔（MTPCS=%d 激进=%d HRS=%d）" % (
        len(positions),
        sum(1 for p in positions if p["strategy"] == "MTPCS策略"),
        sum(1 for p in positions if p["strategy"] == "MTPCS激进策略"),
        sum(1 for p in positions if p["strategy"] == "HRS策略")))

    variants = {
        "全池67等权 无cap": {s: build_idx(closes, FIXED + hrs_syms) for s in SHORT_STRATEGIES},
        "全池67等权 cap±10%": {s: build_idx(closes, FIXED + hrs_syms, 0.10) for s in SHORT_STRATEGIES},
        "分池平权(MTPCS5/HRS62) 无cap": {"MTPCS策略": build_idx(closes, FIXED),
                                           "MTPCS激进策略": build_idx(closes, FIXED),
                                           "HRS策略": build_idx(closes, hrs_syms)},
        "分池平权(MTPCS5/HRS62) HRS cap±10%": {"MTPCS策略": build_idx(closes, FIXED),
                                                 "MTPCS激进策略": build_idx(closes, FIXED),
                                                 "HRS策略": build_idx(closes, hrs_syms, 0.10)},
    }

    for label, idx_map in variants.items():
        print("\n===== %s =====" % label)
        dist(idx_map["MTPCS策略"], "MTPCS指数")
        dist(idx_map["HRS策略"], "HRS指数")
        for trig, rel in [(0.02, 0.01), (0.03, 0.015), (0.03, 0.02)]:
            scan(idx_map, positions, trig, rel)


if __name__ == "__main__":
    main()
