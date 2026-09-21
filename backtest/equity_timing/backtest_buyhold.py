# -*- coding: utf-8 -*-
"""
"等权持有 + 择时择空仓" vs "纯等权躺平" 回测
=================================================
研究问题：在 112 币等权组合上叠加"系统性风险择时开关"，
         能否在不大幅牺牲收益的前提下，显著压缩最大回撤？

信号设计（择时择空仓 = 全市场风险开关，基于 BTC 趋势）
  - T 日持仓掩码 = (BTC 收盘价 T-1  >  BTC 的 SMA(X))？1 : 0
  - 信号用 T-1 收盘决定 T 日，避免未来函数
  - 掩码从 1→0（清仓）或 0→1（建仓）翻转日计入换手成本

运行（本地，严禁部署/服务器回测）：
  python3 backtest/equity_timing/backtest_buyhold.py
  可选：--sma 50,100,200   --cost 0.001
"""
import argparse
import glob
import os

import numpy as np
import pandas as pd

# ----------------------------- 顶部常量 -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORT_PATH = os.path.join(BASE_DIR, "report_buyhold.md")
SIGNAL_SYMBOL = "BTCUSDT"   # 择时风险开关用币（市场代理）
TRADING_DAYS = 365      # 日线年化交易日


def load_panel():
    """读取全部日线 close + quote_volume，构造 (时间×币) 面板。"""
    close, vol = {}, {}
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "**", "*_1d.csv"), recursive=True)):
        sym = os.path.basename(path).replace("_1d.csv", "").upper()
        df = pd.read_csv(path, parse_dates=["open_time"],
                         usecols=["open_time", "close_price", "quote_volume"])
        close[sym] = df.set_index("open_time")["close_price"]
        vol[sym] = df.set_index("open_time")["quote_volume"]
    return (pd.DataFrame(close).sort_index(), pd.DataFrame(vol).sort_index())


def select_universe(vol_panel, topk):
    """按全期平均成交额选前 topk 活跃币；topk<=0 表示全池。"""
    if topk <= 0:
        return list(vol_panel.columns)
    avg = vol_panel.mean(skipna=True).sort_values(ascending=False)
    return list(avg.index[:topk])


def daily_returns(panel):
    """等权 112 币组合日收益 = 各币当日收益率的逐日截面均值（动态上市数）。"""
    ret = panel.pct_change()
    # 上市首日 pct_change 为 NaN，忽略；否则会被 0 拉低
    combo = ret.mean(axis=1, skipna=True)
    combo = combo.replace([np.inf, -np.inf], np.nan).dropna()
    return combo


def timing_mask(btc_close, sma):
    """T 日持仓掩码，用 T-1 的 BTC 价 vs SMA(X)。掩码含换手翻转点。"""
    sma_series = btc_close.rolling(sma).mean()
    sig = btc_close > sma_series
    mask = sig.shift(1).fillna(False)   # T 日用 T-1 信号，防未来函数
    # 翻转点：mask == mask.shift(1) 为 False 说明翻转
    flip = (mask != mask.shift(1)).fillna(False)
    return mask, flip


def graded_weight(btc_close, sma, bands):
    """分级降仓：按 T-1 的 BTC/SMA 偏离度给连续仓位权重（非 0/1 开关）。

    bands: 按阈值降序的 (阈值, 权重) 列表；ratio 落在首个满足的档则取该权重，
    低于最小阈值时仓位为 0。
    """
    sma_series = btc_close.rolling(sma).mean()
    ratio = btc_close / sma_series
    bands = sorted(bands, reverse=True)   # 阈值降序
    conds = [ratio >= thr for thr, _ in bands]
    choices = [w for _, w in bands]
    weight = pd.Series(np.select(conds, choices, default=0.0), index=ratio.index)
    return weight.shift(1).fillna(0.0)   # T-1 信号决定 T 日仓位


def nav_from_weight(combo, weight, args):
    """由仓位权重序列生成净值及指标。换手成本 = 每日权重变化幅度 × 单边费率。"""
    delta = weight.diff().abs().fillna(weight.iloc[0])
    cost = delta * args.cost
    nav = (1 + combo * weight - cost).cumprod()
    m = metrics(nav, cost)
    m["cost_tot"] = float(cost.sum())
    return m


def vol_target_weight(combo, ann_target, window, max_lev):
    """波动率目标仓位：w = (年化目标σ/√365) / 最近 window 日滚动σ，限幅。

    用 T-1 及之前的滚动 std 预测下一期波动（防未来函数）。
    """
    roll_std = combo.rolling(window).std()
    # 未满 window 的预热期按当前可得 std 估算，避免 NaN 全空仓
    roll_std = roll_std.bfill()
    daily_target = ann_target / np.sqrt(TRADING_DAYS)
    w_raw = daily_target / roll_std
    weight = w_raw.clip(upper=max_lev).fillna(0.0)
    return weight.shift(1).fillna(0.0)   # T-1 波动决定 T 日仓位


def metrics(nav, flip_cost):
    """基于净值序列算指标。nav 为已含换手成本的每日净值。"""
    ret = nav.pct_change().dropna()
    if ret.empty or nav.iloc[0] <= 0:
        return {"total_ret": 0.0, "ann_ret": 0.0, "ann_vol": 0.0,
                "sharpe": 0.0, "max_dd": 0.0, "calmar": 0.0, "flips": 0}
    years = len(nav) / TRADING_DAYS
    total_ret = nav.iloc[-1] / nav.iloc[0] - 1.0
    ann_ret = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1.0
    ann_vol = ret.std() * np.sqrt(TRADING_DAYS)
    sharpe = 0.0 if ann_vol == 0 else ann_ret / ann_vol
    drawdown = nav / nav.cummax() - 1.0
    max_dd = abs(drawdown.min())
    calmar = 0.0 if max_dd == 0 else ann_ret / max_dd
    return {"total_ret": total_ret, "ann_ret": ann_ret, "ann_vol": ann_vol,
            "sharpe": sharpe, "max_dd": max_dd, "calmar": calmar,
            "flips": int(flip_cost.sum())}


def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sma", default="50,100,200",
                    help="BTC 择时均线窗口，逗号分隔，多窗口对比")
    ap.add_argument("--cost", type=float, default=0.001,
                    help="翻转一次的全持仓换手成本（单边费率×来回）")
    ap.add_argument("--topk", type=int, default=30,
                    help="按平均成交额选前 N 活跃币作等权篮子；0=全池")
    ap.add_argument("--ann_targets", default="0.2,0.3,0.4",
                    help="vol-target 年化目标波动率，逗号分隔")
    ap.add_argument("--windows", default="20,30,60",
                    help="vol-target 滚动波动窗长，逗号分隔")
    ap.add_argument("--max_lev", type=float, default=1.5,
                    help="vol-target 最大杠杆上限")
    args = ap.parse_args()
    smas = [int(x) for x in args.sma.split(",")]
    args.ann_targets = [float(x) for x in args.ann_targets.split(",")]
    args.windows = [int(x) for x in args.windows.split(",")]

    close_panel, vol_panel = load_panel()
    symbols = select_universe(vol_panel, args.topk)
    panel = close_panel[symbols]
    combo = daily_returns(panel)
    btc_close = close_panel[SIGNAL_SYMBOL].reindex(combo.index).dropna()

    lines = []
    lines.append("# 等权持有 + 择时择空仓 vs 纯等权躺平（112币日线）回测报告")
    lines.append("")
    lines.append(f"- 回测窗口：{combo.index[0]:%Y-%m-%d} ~ {combo.index[-1]:%Y-%m-%d}"
                 f"（{len(combo)} 交易日）")
    lines.append(f"- 等权篮子：{len(panel.columns)} 个币（按平均成交额前"
                 f"{('全池' if args.topk<=0 else args.topk)}）")
    lines.append(f"- 择时开关：BTC 收盘 vs SMA(X)，T-1 信号决定 T 日持仓，"
                 f"翻转换手成本 {args.cost*100:.2f}%/次")

    # 基准：纯等权躺平（无择时、无换手）
    base_nav = (1 + combo).cumprod()
    base = metrics(base_nav, pd.Series(0.0, index=combo.index))
    base_m = metrics(base_nav, pd.Series(0.0, index=combo.index))
    base_m["flips"] = 0

    lines.append("")
    lines.append("## 纯等权躺平基准（无择时）")
    lines.append(_fmt(base_m))

    # 择时版：多窗口对比
    lines.append("")
    lines.append("## 择时择空仓 多窗口对比（BTC 均线开关）")
    lines.append("| SMA | 年化% | 波动% | 夏普 | 最大回撤% | Calmar | 翻转次数 | 换手损耗% |")
    lines.append("|-----|-------|-------|------|-----------|--------|---------|----------|")
    for sma in smas:
        mask, flip = timing_mask(btc_close, sma)
        timed = combo * mask
        cost = flip * args.cost   # 翻转日扣换手成本
        nav = (1 + timed - cost).cumprod()
        m = metrics(nav, flip)
        cost_tot = float(flip.sum() * args.cost)
        lines.append(f"| {sma:3d} | {m['ann_ret']*100:5.2f} | {m['ann_vol']*100:5.2f} | "
                     f"{m['sharpe']:4.2f} | {m['max_dd']*100:6.2f} | {m['calmar']:4.2f} | "
                     f"{m['flips']:3d} | {cost_tot*100:6.2f} |")

    # 分级降仓：多窗口对比
    lines.append("")
    lines.append("## 分级降仓风控（BTC/SMA 偏离度分三档：满仓→半仓→空仓）")
    lines.append("| SMA | 年化% | 波动% | 夏普 | 最大回撤% | Calmar | 换手损耗% |")
    lines.append("|-----|-------|-------|------|-----------|--------|----------|")
    bands = [(1.03, 1.0), (0.98, 0.55), (0.95, 0.25)]   # 偏离越高仓位越高
    for sma in smas:
        weight = graded_weight(btc_close, sma, bands)
        m = nav_from_weight(combo, weight, args)
        lines.append(f"| {sma:3d} | {m['ann_ret']*100:5.2f} | {m['ann_vol']*100:5.2f} | "
                     f"{m['sharpe']:4.2f} | {m['max_dd']*100:6.2f} | {m['calmar']:4.2f} | "
                     f"{m['cost_tot']*100:6.2f} |")

    # 波动率目标仓位：目标σ × 窗长 敏感性对比
    lines.append("")
    lines.append("## 波动率目标仓位（vol-target，与方向无关的风控）")
    lines.append("| 目标σ年化 | 窗长 | 年化% | 波动% | 夏普 | 最大回撤% | Calmar | 换手损耗% |")
    lines.append("|-----------|------|-------|-------|------|-----------|--------|----------|")
    for ann_target in args.ann_targets:
        for window in args.windows:
            weight = vol_target_weight(combo, ann_target, window, args.max_lev)
            m = nav_from_weight(combo, weight, args)
            lines.append(f"| {ann_target*100:4.0f}% | {window:3d} | {m['ann_ret']*100:5.2f} | "
                         f"{m['ann_vol']*100:5.2f} | {m['sharpe']:4.2f} | "
                         f"{m['max_dd']*100:6.2f} | {m['calmar']:4.2f} | "
                         f"{m['cost_tot']*100:6.2f} |")

    lines.append("")
    lines.append("## 基准指标明细")
    lines.append(_fmt(base_m))

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))
    print(f"\n报告已写入 {REPORT_PATH}")


def _fmt(m):
    return (f"- 累计收益 {m['total_ret']*100:7.2f}% | 年化 {m['ann_ret']*100:6.2f}% | "
            f"波动 {m['ann_vol']*100:5.2f}% | 夏普 {m['sharpe']:4.2f} | "
            f"最大回撤 {m['max_dd']*100:6.2f}% | Calmar {m['calmar']:4.2f}")


if __name__ == "__main__":
    run()