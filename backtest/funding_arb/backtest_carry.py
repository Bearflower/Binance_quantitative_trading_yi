# -*- coding: utf-8 -*-
"""
B 形态资金费率对冲套利（市场中性 carry）回测脚本 —— v2（多币池 × 多模式 对比版）

策略逻辑：
  在给定币池内，每个资金结算时刻判定——做空年化 funding 最高的币（高腿）、
  做多年化 funding 最低的币（低腿），赚取两腿净 funding 利差（carry），
  并通过对冲抵消大部分价格方向风险。MVP 只吃 carry，不赌配对价差回归。

v2 变更：
  - 支持币池 5（BTC/ETH/BNB/SOL/XRP）与 10（+DOGE/ADA/LINK/AVAX/LTC）。
  - 价格 4h 数据已本地补齐 180 天 10 币（backtest/funding_arb/data/prices/）。
  - 支持多种运行模式（券池×参数），用于衡量"参数方向"对收益的决定性影响。

运行（本地，严禁部署/服务器回测）：
  python3 backtest/funding_arb/backtest_carry.py
  可选：--pool 5|10|all   --mode mvp|longmaker|all
"""
import argparse
import os

import numpy as np
import pandas as pd

# ----------------------------- 顶部常量（可调参数） -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FUND_DIR = os.path.join(BASE_DIR, "data")
PX_DIR = os.path.join(BASE_DIR, "data", "prices")
REPORT_PATH = os.path.join(BASE_DIR, "report_carry.md")

# 结算间隔与年化乘数
SETTLE_HOURS = 8
ANNUALIZE = (24 // SETTLE_HOURS) * 365  # = 1095

# 币池
POOLS = {
    "5": ["BTC", "ETH", "BNB", "SOL", "XRP"],
    "10": ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX", "LTC"],
}

# 运行模式：不同 持仓上限 / 利差回落退出比 / 单腿手续费 ×4次
MODES = {
    "mvp": {"hold": 5, "exit": 0.5, "fee": 0.0005},   # 短持高频 + taker，原 MVP
    "longmaker": {"hold": 30, "exit": 0.2, "fee": 0.0002},  # 长持低换手 + maker
}

# 入场净利差阈值（年化，十进制）敏感性档位
ENTRY_THRESHOLDS = [0.04, 0.06, 0.08, 0.10, 0.12]

# 保护止损：累计净收益相对入场后峰值回撤
MAX_DRAWDOWN = 0.02


# ----------------------------- 数据加载 -----------------------------
def _read_csv(sym, subdir, name):
    """读取单币 CSV 并返回带 datetime 索引、数值化列名的 DataFrame。"""
    path = os.path.join(subdir, f"{sym}USDT_{name}.csv")
    return pd.read_csv(path)


def load_funding(sym):
    """读取单币 funding 历史：funding_time_utc, funding_rate。"""
    df = _read_csv(sym, FUND_DIR, "funding")
    df["t"] = pd.to_datetime(df["funding_time_utc"])
    return df[["t"]].assign(rate=df["funding_rate"])


def load_price(sym):
    """读取单币 4h 价格：open_time(ms), close_price。"""
    df = _read_csv(sym, PX_DIR, "4h")
    df["t"] = pd.to_datetime(df["open_time"], unit="ms")
    return df[["t"]].assign(close=df["close_price"])


def _to_ts(df):
    """t 转纳秒整数列 tk（用于 merge_asof）。"""
    d = df.copy()
    d["tk"] = d["t"].astype("int64")
    return d


def build_wide_funding(symbols):
    """合并多币 funding 为宽表：index=t，每币一列单期费率。"""
    frames = []
    for sym in symbols:
        df = load_funding(sym).set_index("t").rename(columns={"rate": f"{sym}_rate"})
        frames.append(df)
    wide = pd.concat(frames, axis=1).sort_index()
    wide.index = pd.DatetimeIndex(wide.index)
    return wide


def build_price_lookup(symbols):
    """按 BTC 时间轴 asof 合并多币 close，供结算时刻回填。"""
    frames = [_to_ts(load_price(s))[["tk", "close"]].rename(columns={"close": f"px_{s}"})
              for s in symbols]
    skeleton = _to_ts(load_funding("BTC"))[["t", "tk"]]
    for f in frames:
        skeleton = pd.merge_asof(skeleton, f, on="tk", direction="backward")
    skeleton["t"] = pd.to_datetime(skeleton["t"])
    return skeleton.set_index("t")


def _price_window(symbols):
    """价格 4h 数据公共时间窗口 [(lo, hi)]，用于裁剪回测区间。"""
    lo = hi = None
    for sym in symbols:
        df = load_price(sym)
        cand = (df["t"].iloc[0], df["t"].iloc[-1])
        lo = cand[0] if lo is None else max(lo, cand[0])
        hi = cand[1] if hi is None else min(hi, cand[1])
    return lo, hi


def build_basis(wide, symbols):
    """funding 宽表与价格合并，并按价格真实窗口裁剪（防 merge_asof 越界回填）。"""
    basis = wide.join(build_price_lookup(symbols), how="inner").dropna()
    basis.index = pd.DatetimeIndex(basis.index)
    lo, hi = _price_window(symbols)
    return basis.loc[lo:hi].copy()


# ----------------------------- 回测核心 -----------------------------
def annualized(rate):
    """单期费率 → 年化费率（8h 结算乘数=1095）。"""
    return rate * ANNUALIZE


def pick_legs(row, symbols):
    """返回 (高腿, 低腿, 净年化利差)。"""
    rates = {s: row[f"{s}_annual"] for s in symbols}
    high = max(rates, key=rates.get)
    low = min(rates, key=rates.get)
    return high, low, rates[high] - rates[low]


def run_threshold(basis, symbols, threshold, hold, exit_f, fee):
    """跑完某 (阈值, 模式) 的完整模拟，返回 (统计, 交易明细)。"""
    trades, pos, cum_net = [], {}, []
    for t, row in basis.iterrows():
        if pos:
            _advance(pos, row, basis, symbols)
            cur_spread = max(row[f"{pos['high']}_annual"] - row[f"{pos['low']}_annual"], 0.0)
            if _should_exit(pos, cur_spread, threshold, hold, exit_f):
                trades.append(_close(pos, t, fee))
                cum_net.append(trades[-1]["net_ret"])
                pos = {}
        if not pos:
            high, low, spread = pick_legs(row, symbols)
            if spread > threshold:
                pos = _open(row, high, low)
    return _aggregate(trades, cum_net), trades


def _open(row, high, low):
    """开仓：首期 carry 立即纳入（当期 funding 已确定）。"""
    pos = {
        "high": high, "low": low,
        "entry_high_px": row[f"px_{high}"], "entry_low_px": row[f"px_{low}"],
        "entry_spread": row[f"{high}_annual"] - row[f"{low}_annual"],
        "cycles": 1, "carry": row[f"{high}_rate"] - row[f"{low}_rate"],
        "price_pnl": 0.0, "peak_net": row[f"{high}_rate"] - row[f"{low}_rate"],
        "entry_time": row.name,
    }
    pos["peak_net"] = max(pos["peak_net"], pos["carry"] + pos["price_pnl"])
    return pos


def _advance(pos, row, basis, symbols):
    """持仓推进一期：累加 carry、更新价格 PnL 与峰值。"""
    pos["cycles"] += 1
    pos["carry"] += row[f"{pos['high']}_rate"] - row[f"{pos['low']}_rate"]
    ret_h = row[f"px_{pos['high']}"] / pos["entry_high_px"] - 1.0
    ret_l = row[f"px_{pos['low']}"] / pos["entry_low_px"] - 1.0
    pos["price_pnl"] = -ret_h + ret_l
    pos["peak_net"] = max(pos["peak_net"], pos["carry"] + pos["price_pnl"])


def _should_exit(pos, cur_spread, threshold, hold, exit_f):
    """平仓判定：周期上限 / 利差回落 / 保护止损。"""
    if pos["cycles"] >= hold:
        return True
    if cur_spread < pos["entry_spread"] * exit_f:
        return True
    if pos["peak_net"] - (pos["carry"] + pos["price_pnl"]) > MAX_DRAWDOWN:
        return True
    return False


def _close(pos, t, fee):
    """平仓：扣两腿各开+平共 4 次手续费，生成交易记录。"""
    net = pos["carry"] + pos["price_pnl"] - 4 * fee
    return {
        "entry_time": pos["entry_time"], "exit_time": t,
        "high": pos["high"], "low": pos["low"],
        "entry_spread": pos["entry_spread"], "cycles": pos["cycles"],
        "carry": pos["carry"], "price_pnl": pos["price_pnl"], "net_ret": net,
    }


def _aggregate(trades, cum_net):
    """汇总单档统计。"""
    n = len(trades)
    total_cycles = sum(t["cycles"] for t in trades)
    carry = sum(t["carry"] for t in trades)
    px = sum(t["price_pnl"] for t in trades)
    net = sum(t["net_ret"] for t in trades)
    ann = net * (ANNUALIZE / total_cycles) if total_cycles else 0.0
    wins = sum(1 for t in trades if t["net_ret"] > 0)
    return {
        "triggers": n, "cycles": total_cycles, "carry": carry, "px_pnl": px,
        "net": net, "ann_net": ann, "win_rate": wins / n if n else 0.0,
        "max_dd": _max_drawdown(cum_net),
    }


def _max_drawdown(cum_net):
    """累计净收益序列最大回撤。"""
    if not cum_net:
        return 0.0
    s = pd.Series(cum_net)
    return float(abs((s - s.cummax()).min()))


# ----------------------------- 输出 -----------------------------
def fmt_row(st, thr):
    """格式化一行统计。"""
    return (
        f"| {thr*100:.0f}% | {st['triggers']:3d} | {st['cycles']:4d} | "
        f"{st['carry']*100:6.2f} | {st['px_pnl']*100:7.2f} | {st['net']*100:8.2f} | "
        f"{st['ann_net']*100:7.2f} | {st['max_dd']*100:6.2f} | {st['win_rate']*100:4.0f}% |"
    )


def header():
    return "| 阈值 | 触发 | 周期 | carry% | 价格PnL% | 净收益% | 年化% | 回撤% | 胜率 |"


def run(pool_key, mode_key):
    """运行一组 (币池, 模式)，返回 basis 与敏感性行。"""
    symbols = POOLS[pool_key]
    hold, exit_f, fee = MODES[mode_key]["hold"], MODES[mode_key]["exit"], MODES[mode_key]["fee"]
    wide = build_wide_funding(symbols)
    basis = build_basis(wide, symbols)
    for s in symbols:
        basis[f"{s}_annual"] = annualized(basis[f"{s}_rate"])
    rows = []
    for thr in ENTRY_THRESHOLDS:
        st, _ = run_threshold(basis, symbols, thr, hold, exit_f, fee)
        st["threshold"] = thr
        rows.append(st)
    return basis, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", choices=["5", "10", "all"], default="all")
    ap.add_argument("--mode", choices=["mvp", "longmaker", "all"], default="all")
    args = ap.parse_args()

    pools = ["5", "10"] if args.pool == "all" else [args.pool]
    modes = ["mvp", "longmaker"] if args.mode == "all" else [args.mode]

    out = []
    for pk in pools:
        for mk in modes:
            basis, rows = run(pk, mk)
            out.append((pk, mk, basis, rows))
            print(f"\n===== 币池 {pk} | 模式 {mk} =====")
            print(f"回测起止 {basis.index[0]:%Y-%m-%d %H:%M} ~ {basis.index[-1]:%Y-%m-%d %H:%M}，"
                  f"结算周期 {len(basis)}")
            print(header())
            print("|------|----|----|------|---------|--------|-------|------|-----|")
            for r in rows:
                print(fmt_row(r, r["threshold"]))

    lines = []
    lines.append("# 资金费率对冲套利 B 形态回测报告（v2：多币池 × 多模式）")
    for pk, mk, basis, rows in out:
        lines.append("")
        lines.append(f"## 币池 {pk} | 模式 {mk}")
        lines.append(f"- 结算间隔：8h，年化乘数 {ANNUALIZE}")
        lines.append(f"- 回测起止 {basis.index[0]:%Y-%m-%d %H:%M} ~ {basis.index[-1]:%Y-%m-%d %H:%M}"
                     f"，结算周期 {len(basis)}")
        lines.append(header())
        lines.append("|------|----|----|------|---------|--------|-------|------|-----|")
        for r in rows:
            lines.append(fmt_row(r, r["threshold"]))
    lines.append("")
    lines.append("## 综合结论")
    lines.append("见终端输出的对比与下方人工判断（由调度者补充）。")
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n报告已写入 {REPORT_PATH}")


if __name__ == "__main__":
    main()