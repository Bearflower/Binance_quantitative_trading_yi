# -*- coding: utf-8 -*-
"""波动率目标仓位（Vol-Target）回测 · 合约可交易币池口径
=============================================================
研究问题：原现货 Top30 口径结论（vol-target 30%/30：年化 +21.2%、回撤 29.1%、
        夏普 0.66）在「合约可交易币池 + point-in-time 选币 + 计入资金费率」口径下
        还剩多少？

口径修正（相对原现货回测的三处关键差异）：
  1. 币池 = 合约可交易币（357 个 perp），而非现货长尾 Top30
  2. point-in-time：按 onboard_date 过滤未上市 + 滚动 30 日成交额排序选前 N、
     季度再平衡（消除未来函数 + 生存偏差）
  3. 计入资金费率：w>1 的杠杆部分按每日资金费率扣成本（多头费率>0 为成本）
     注：w<=1 视为现货敞口（不扣资金费），此为本口径下对 vol-target 最有利的假设，
         真实满份额 perp 多头需全额扣费，实际扣费只会更高（更保守）。

运行（本地，严禁服务器回测）：
  python3 backtest/equity_timing/backtest_vol_target_perp.py

可选参数：
  --topn 30              每期选前 N 币
  --targets 0.20,0.30    年化目标波动率（逗号分隔）
  --window 30            滚动波动窗长（交易日）
  --max_lev 1.5          最大杠杆/仓位上限
  --cost 0.0005          单边换手费率（0.05%）
  --deadband 0.05        换手死区：|Δw| 低于该值不交易
  --rebal 90             季度再平衡间隔（自然日）
"""
import argparse
import json
import os

import numpy as np
import pandas as pd

# ----------------------------- 顶部常量 -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_2Y = os.path.join(BASE_DIR, os.pardir, "cash_carry", "data_2y")
SYM_DIR = os.path.join(DATA_2Y, "sym")
META_PATH = os.path.join(DATA_2Y, "symbols.json")
REPORT_PATH = os.path.join(BASE_DIR, "report_vol_target_perp.md")
TRADING_DAYS = 365          # 加密 7×24，年化用 365
ROLL_TURNOVER = 30          # point-in-time 成交额滚动窗长


# ----------------------------- 数据加载 -----------------------------
def load_panels():
    """读取 357 币 perp 1h → 日线 close / 日成交额(quote) / 日资金费率。"""
    meta = {}
    for it in json.load(open(META_PATH, encoding="utf-8")):
        meta[it["symbol"]] = pd.to_datetime(it["onboard_date"], unit="ms")

    closes, turnovers, fundings = {}, {}, {}
    for sym, onboard in meta.items():
        pf = os.path.join(SYM_DIR, sym, "perp_1h.csv")
        ff = os.path.join(SYM_DIR, sym, "funding.csv")
        if not os.path.exists(pf):
            continue
        px = pd.read_csv(pf, parse_dates=["open_time"],
                         usecols=["open_time", "close", "volume"])
        px = px.set_index("open_time").sort_index()
        closes[sym] = px["close"].resample("1D").last()
        turnovers[sym] = (px["close"] * px["volume"]).resample("1D").sum()
        if os.path.exists(ff):
            f = pd.read_csv(ff, parse_dates=["time"], usecols=["time", "rate"])
            f = f.set_index("time").sort_index()
            fundings[sym] = f["rate"].resample("1D").sum()

    close = pd.DataFrame(closes).sort_index()
    turnover = pd.DataFrame(turnovers).sort_index()
    funding = pd.DataFrame(fundings).sort_index()
    # 去掉最后一个不完整交易日（数据止于当日凌晨，24h 不全）
    close, turnover, funding = close.iloc[:-1], turnover.iloc[:-1], funding.iloc[:-1]
    meta = {s: t for s, t in meta.items() if s in close.columns}
    return close, turnover, funding, meta


# ----------------------------- point-in-time 选币 -----------------------------
def build_membership(turnover, meta, topn, rebal_days):
    """每个再平衡日用「滚动 30 日成交额」选前 topn，季度再平衡，构造逐日成员矩阵。"""
    dates = turnover.index
    cols = list(turnover.columns)
    # 滚动 30 日成交额，shift(1) 只用再平衡日之前的数据（防未来函数）
    roll = turnover.rolling(ROLL_TURNOVER, min_periods=ROLL_TURNOVER).mean().shift(1)
    # 上市过滤：onboard <= 当日才可入选
    eligible = pd.DataFrame(
        {s: dates >= meta[s] for s in cols}, index=dates)

    # 季度再平衡日（自然日，snap 到最近过去交易日）
    cal = pd.date_range(dates[0] + pd.Timedelta(days=ROLL_TURNOVER),
                        dates[-1], freq=pd.Timedelta(days=rebal_days))
    rebal_idx = []
    for d in cal:
        snap = dates[dates <= d]
        if len(snap):
            rebal_idx.append(snap[-1])
    rebal_idx = sorted(set(rebal_idx))

    universe = {}
    for d in rebal_idx:
        cand = (eligible.loc[d] & roll.loc[d].notna())
        top = roll.loc[d][cand].sort_values(ascending=False).index[:topn]
        universe[d] = list(top)

    membership = pd.DataFrame(False, index=dates, columns=cols)
    for i, d in enumerate(rebal_idx):
        end = rebal_idx[i + 1] if i + 1 < len(rebal_idx) else dates[-1] + pd.Timedelta(days=1)
        seg = dates[(dates >= d) & (dates < end)]
        membership.loc[seg, universe[d]] = True
    return membership, rebal_idx, universe


def rebal_churn(membership, rebal_idx):
    """估算季度再平衡时的成分换手（换出+换入占比 / N）。"""
    churns = []
    for i in range(1, len(rebal_idx)):
        prev = set(membership.columns[membership.loc[rebal_idx[i - 1]].values])
        cur = set(membership.columns[membership.loc[rebal_idx[i]].values])
        removed = prev - cur
        churns.append((rebal_idx[i], len(removed) / max(len(cur), 1)))
    return churns


# ----------------------------- 组合构造 -----------------------------
def combo_returns(close, membership):
    """等权组合日收益 = 当日成分币收益率的截面均值。"""
    ret = close.pct_change().replace([np.inf, -np.inf], np.nan)
    return ret.where(membership).mean(axis=1, skipna=True)


def combo_funding(funding, membership):
    """等权组合日资金费率 = 当日成分币资金费率截面均值（多头 rate>0 为成本）。"""
    return funding.where(membership).mean(axis=1, skipna=True).fillna(0.0)


def vol_target_weight(combo, ann_target, window, max_lev, deadband):
    """波动率目标仓位：w = (年化目标σ/√365) / 最近 window 日滚动σ，限幅 + 死区。"""
    roll_std = combo.rolling(window).std().bfill()
    daily_target = ann_target / np.sqrt(TRADING_DAYS)
    w_raw = (daily_target / roll_std).clip(upper=max_lev).fillna(0.0)
    weight = w_raw.shift(1).fillna(0.0)   # T-1 波动决定 T 日仓位（防未来函数）

    if deadband and deadband > 0:
        applied = [weight.iloc[0]]
        for i in range(1, len(weight)):
            cur = applied[-1]
            raw = weight.iloc[i]
            applied.append(raw if abs(raw - cur) >= deadband else cur)
        weight = pd.Series(applied, index=weight.index)
    return weight


def build_nav(combo, weight, cost_side, fund, membership, rebal_idx):
    """由仓位权重 + 换手成本 + 资金费率 + 调仓成本生成净值。"""
    dw = weight.diff().abs().fillna(weight.iloc[0])
    cost_vt = dw * cost_side                                             # 波动率调仓成本
    lev = (weight - 1.0).clip(lower=0.0)
    cost_fund = (lev * fund).reindex(combo.index).fillna(0.0)            # 杠杆部分资金费
    cost_rebal = pd.Series(0.0, index=combo.index)
    for d, churn in rebal_churn(membership, rebal_idx):
        if d in cost_rebal.index:
            cost_rebal.loc[d] = churn * weight.reindex(cost_rebal.index).loc[d] * cost_side
    total_cost = cost_vt + cost_fund + cost_rebal
    nav = (1 + combo * weight - total_cost).cumprod()
    return nav, cost_vt, cost_fund, cost_rebal


def metrics(nav):
    """基于净值序列计算年化/波动/夏普/回撤/Calmar。"""
    nav = nav.dropna()
    if len(nav) < 2 or nav.iloc[0] <= 0:
        return {"ann_ret": 0.0, "ann_vol": 0.0, "sharpe": 0.0,
                "max_dd": 0.0, "calmar": 0.0}
    ret = nav.pct_change().dropna()
    years = len(nav) / TRADING_DAYS
    ann_ret = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1.0
    ann_vol = ret.std() * np.sqrt(TRADING_DAYS)
    sharpe = 0.0 if ann_vol == 0 else ann_ret / ann_vol
    drawdown = nav / nav.cummax() - 1.0
    max_dd = abs(drawdown.min())
    calmar = 0.0 if max_dd == 0 else ann_ret / max_dd
    return {"ann_ret": ann_ret, "ann_vol": ann_vol, "sharpe": sharpe,
            "max_dd": max_dd, "calmar": calmar}


# ----------------------------- 主流程 -----------------------------
def run():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topn", type=int, default=30)
    ap.add_argument("--targets", default="0.20,0.30")
    ap.add_argument("--window", type=int, default=30)
    ap.add_argument("--max_lev", type=float, default=1.5)
    ap.add_argument("--cost", type=float, default=0.0005)
    ap.add_argument("--deadband", type=float, default=0.05)
    ap.add_argument("--rebal", type=int, default=90)
    args = ap.parse_args()
    args.targets = [float(x) for x in args.targets.split(",")]

    close, turnover, funding, meta = load_panels()
    membership, rebal_idx, universe = build_membership(turnover, meta, args.topn, args.rebal)
    combo = combo_returns(close, membership)
    fund = combo_funding(funding, membership).reindex(combo.index).fillna(0.0)
    valid = combo.dropna()

    lines = []
    lines.append("# 波动率目标仓位回测报告（合约可交易币池口径）")
    lines.append("")
    lines.append(f"- 回测窗口：{valid.index[0]:%Y-%m-%d} ~ {valid.index[-1]:%Y-%m-%d}"
                 f"（{len(valid)} 个交易日）")
    lines.append(f"- 币池：{len(meta)} 个合约币，point-in-time 按滚动 30 日成交额选前 "
                 f"{args.topn}，每 {args.rebal} 天再平衡（共 {len(rebal_idx)} 期）")
    lines.append(f"- 等权组合；vol-target 窗长 {args.window}，最大仓 {args.max_lev}x，"
                 f"死区 {args.deadband*100:.0f}%，单边换手 {args.cost*100:.2f}%")
    lines.append("- 资金费率：仅 w>1 杠杆部分按每日资金费率成本化（w<=1 视为现货不扣费）")

    # 基准：等权躺平（同一币池，不套 vol-target）
    base_nav = (1 + combo).cumprod()
    base = metrics(base_nav)

    lines.append("")
    lines.append("## 基准：等权躺平（合约币池 point-in-time 选币）")
    lines.append(_fmt(base))

    lines.append("")
    lines.append("## Vol-Target 多目标σ对比")
    lines.append("| 目标σ | 年化% | 波动% | 夏普 | 最大回撤% | Calmar | "
                 "调仓损耗% | 资金费% |")
    lines.append("|-------|-------|-------|------|-----------|--------|----------|--------|")
    navs = {}
    for tgt in sorted(args.targets):
        w = vol_target_weight(combo, tgt, args.window, args.max_lev, args.deadband)
        nav, cvt, cfun, crb = build_nav(combo, w, args.cost, fund, membership, rebal_idx)
        navs[tgt] = (nav, w, cvt, cfun)
        m = metrics(nav)
        lines.append(f"| {tgt*100:4.0f}% | {m['ann_ret']*100:5.2f} | {m['ann_vol']*100:5.2f} | "
                     f"{m['sharpe']:4.2f} | {m['max_dd']*100:6.2f} | {m['calmar']:4.2f} | "
                     f"{float(cvt.sum())*100:6.2f} | {float(cfun.sum())*100:6.2f} |")

    # 资金费率明细（等权组合全期日均费率）
    lines.append("")
    lines.append("## 资金费率明细")
    lines.append(f"- 等权组合日均资金费率（多头）: {fund.mean()*100:.5f}%/日 "
                 f"→ 年化约 {fund.mean()*365*100:.2f}%")
    lines.append(f"- 全期资金费率累计（若全额多头持有而非仅杠杆部分）: "
                 f"{fund.sum()*100:.2f}%")
    lines.append("- 说明：上表「资金费%」仅为 w>1 杠杆部分累计，口径对 vol-target 最有利。")

    # 分子区间（半年度稳健性）
    lines.append("")
    lines.append("## 半年度子区间稳健性（躺平 vs Vol-Target 30%）")
    lines.append("| 区间 | 躺平年化% | 躺平回撤% | VT30 年化% | VT30 回撤% | VT30 夏普 |")
    lines.append("|------|-----------|-----------|------------|-----------|-----------|")
    bound = pd.date_range(valid.index[0], valid.index[-1], freq="6MS")
    tgt30 = 0.30
    for i in range(len(bound) - 1):
        s, e = bound[i], bound[i + 1]
        seg = valid[(valid.index >= s) & (valid.index < e)]
        if len(seg) < 40:
            continue
        b = metrics(base_nav.reindex(seg.index))
        nav30, *_ = navs.get(tgt30, build_nav(combo, vol_target_weight(
            combo, tgt30, args.window, args.max_lev, args.deadband), args.cost,
            fund, membership, rebal_idx))
        v = metrics(nav30.reindex(seg.index))
        lines.append(f"| {seg.index[0]:%Y-%m}~{seg.index[-1]:%Y-%m} | "
                     f"{b['ann_ret']*100:5.2f} | {b['max_dd']*100:5.2f} | "
                     f"{v['ann_ret']*100:5.2f} | {v['max_dd']*100:5.2f} | {v['sharpe']:4.2f} |")

    # TopN 敏感性（固定 σ=30%）
    lines.append("")
    lines.append("## TopN 敏感性（σ=30%，窗长 30）")
    lines.append("| TopN | 年化% | 波动% | 夏普 | 最大回撤% | Calmar |")
    lines.append("|------|-------|-------|------|-----------|--------|")
    for n in [20, 30, 50]:
        mem, _ridx, _u = build_membership(turnover, meta, n, args.rebal)
        c = combo_returns(close, mem)
        w = vol_target_weight(c, 0.30, args.window, args.max_lev, args.deadband)
        f = combo_funding(funding, mem).reindex(c.index).fillna(0.0)
        nav, *_ = build_nav(c, w, args.cost, f, mem, _ridx)
        m = metrics(nav)
        lines.append(f"| {n:4d} | {m['ann_ret']*100:5.2f} | {m['ann_vol']*100:5.2f} | "
                     f"{m['sharpe']:4.2f} | {m['max_dd']*100:6.2f} | {m['calmar']:4.2f} |")

    lines.append("")
    lines.append("## 与原现货口径结论对照")
    lines.append("| 口径 | 年化% | 最大回撤% | 夏普 |")
    lines.append("|------|-------|-----------|------|")
    lines.append("| 原现货 Top30 躺平 | +18.0 | 66.0 | 0.23 |")
    lines.append("| 原现货 Top30 VT 30/30 | +21.2 | 29.1 | 0.66 |")
    lines.append(f"| 合约口径 躺平 | {base['ann_ret']*100:+.1f} | {base['max_dd']*100:.1f} | {base['sharpe']:.2f} |")
    nav30 = navs[0.30][0]
    m30 = metrics(nav30)
    lines.append(f"| 合约口径 VT 30/30 | {m30['ann_ret']*100:+.1f} | {m30['max_dd']*100:.1f} | {m30['sharpe']:.2f} |")

    out = "\n".join(lines)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(out)
    print(out)
    print(f"\n报告已写入 {REPORT_PATH}")


def _fmt(m):
    return (f"- 年化 {m['ann_ret']*100:+.2f}% | 波动 {m['ann_vol']*100:5.2f}% | "
            f"夏普 {m['sharpe']:4.2f} | 最大回撤 {m['max_dd']*100:6.2f}% | "
            f"Calmar {m['calmar']:4.2f}")


if __name__ == "__main__":
    run()