#!/usr/bin/env python3
"""[本地] L0 择机型回测原型——阈值 gate + 精选池，验证真实可捕收益

策略（对齐讨论稿 L0 定义）:
    做多现货 + 做空等额永续（方向中性，吃永续正 funding）
    开仓 gate: 当前月 funding 净费率 ≥ 成本阈值（默认 3.1%/年）
    离场 gate: 月 funding 净费率 < 阈值 → 平仓空仓
    精选池: 全期净年化 > 0 的币（用 funding_cyclicity 结果），或传 --all

方法（防幻觉）:
    - 按月粒度合成净值（资金费率月级折算年化再 /12 得月收益）
    - 持仓期叠加手续费（每次开仓扣 0.15% 双边，/12 摊到月）
    - 组合 = 当月所有在场币等权平均月收益
    - 输出年度/总收益、在场月占比、真实可捕年化

用法:
    python3 l0_selective_backtest.py [--threshold 0.0315] [--poolsize N]
"""
import csv
import glob
import os
import sys
import statistics
from collections import defaultdict

DATA_DIR = os.path.join("..", "data_2y")
SYM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATA_DIR, "sym")

# 默认参数
THRESHOLD = 0.0315          # 成本阈值年化（3%资金+0.15%手续费）
FEE_PERP_MONTH = 0.0015 / 12  # 双边手续费0.15%，摊到持仓月
WINDOW_MONTHS = 12          # 默认精选池筛选用全期

def parse_args():
    args = {"threshold": THRESHOLD, "poolsize": None, "all": False}
    if "--threshold" in sys.argv:
        args["threshold"] = float(sys.argv[sys.argv.index("--threshold")+1])
    if "--poolsize" in sys.argv:
        args["poolsize"] = int(sys.argv[sys.argv.index("--poolsize")+1])
    if "--all" in sys.argv:
        args["all"] = True
    return args

def load_monthly_funding(sym):
    """返回 {YYYY-MM: 月funding粮油折算年化}"""
    mm = defaultdict(list)
    path = os.path.join(SYM_DIR, sym, "funding.csv")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        r = csv.reader(f); next(r, None)
        for row in r:
            if len(row) >= 2:
                try:
                    mm[row[0][:7]].append(float(row[1]))
                except (TypeError, ValueError):
                    continue
    return {k: sum(v)/len(v)*3*365 for k, v in mm.items()}

def build_selected_pool():
    """从 funding_cyclicity 结果选精选池: 全期净年化为正
    返回 sym 列表（按年化降序）"""
    cyc = os.path.join(os.path.dirname(SYM_DIR), "funding_cyclicity.csv")
    pool = []
    if os.path.exists(cyc):
        with open(cyc, encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                try:
                    if float(row["annual_mean"]) > THRESHOLD:
                        pool.append(row["symbol"])
                except (TypeError, ValueError):
                    continue
    return pool

def backtest_one(sym, threshold):
    """单币择机回测: 返回 (月度收益序列: [(YYYY-MM, monthly_rate, in_market)], 在场月数, 总月数)"""
    funding = load_monthly_funding(sym)
    if not funding:
        return [], 0, 0
    months = sorted(funding.keys())
    series = []
    in_market_months = 0
    for m in months:
        ann = funding[m]
        if ann >= threshold:
            # 在场: 吃 funding - 手续费
            monthly = ann / 12.0 - FEE_PERP_MONTH
            in_market_months += 1
        else:
            monthly = 0.0
        series.append((m, monthly, ann >= threshold))
    return series, in_market_months, len(months)

def main():
    args = parse_args()
    pool = build_selected_pool()
    if args["all"]:
        pool = sorted(d for d in os.listdir(SYM_DIR) if os.path.isdir(os.path.join(SYM_DIR, d)))
    if args["poolsize"]:
        pool = pool[:args["poolsize"]]

    if not pool:
        print("❌ 精选池为空")
        return 1

    print("=" * 90)
    print(f"L0 择机型回测原型: 阈值gate={args['threshold']*100:.1f}%  精选池={len(pool)}个币"
          f"  ({'全池' if args['all'] else 'annual_mean>阈值'})")
    print("=" * 90)

    # 收集所有币的月度序列
    all_series = {}
    total_market = 0
    total_n = 0
    for sym in pool:
        series, in_m, n = backtest_one(sym, args["threshold"])
        all_series[sym] = series
        total_market += in_m
        total_n += n

    # 汇总: 按月等权组合
    month_agg = defaultdict(list)   # YYYY-MM -> [monthly rates]
    for sym, series in all_series.items():
        for m, rate, _ in series:
            month_agg[m].append(rate)

    all_months = sorted(month_agg.keys())
    # 累积净值
    equity = 1.0
    yearly = defaultdict(list)     # YYYY -> 该年各月收益
    for m in all_months:
        rates = month_agg[m]
        avg = sum(rates) / len(rates)
        equity *= (1 + avg)
        y = m[:4]
        yearly[y].append(avg)

    print(f"组合月度在场占比(称平均): {total_market / max(total_n,1)*100:.1f}%")
    print(f"有效币数: {len(all_series)}   覆盖月份: {all_months[0]} ~ {all_months[-1]} ({len(all_months)}个月)")
    print("-" * 90)
    for y in sorted(yearly):
        y_ret = 1.0
        for r in yearly[y]:
            y_ret *= (1 + r)
        y_ann = (y_ret ** (12.0 / max(len(yearly[y]),1))) - 1
        print(f"  {y}: 该年复合净值 {y_ret:.3f}   折合年化 {y_ann*100:+.1f}%")
    total_ann = (equity ** (12.0 / max(len(all_months),1))) - 1
    print("-" * 90)
    print(f"总净值({all_months[0]}~{all_months[-1]}): {equity:.3f}   折合年化 {total_ann*100:+.1f}%")
    print(f"看重点: 在全部月份等权含空仓币的情况下, 真实年化")

    # 前15个币独立的年化(在场月)
    print("\n单币在场期年化 Top15 (仅在场月收益年化):")
    indiv = []
    for sym, series in all_series.items():
        in_rates = [rate for _, rate, im in series if im]
        if in_rates:
            ann = sum(in_rates) / len(in_rates) * 12
            indiv.append((sym, ann, len(in_rates)))
    indiv.sort(key=lambda x: x[1], reverse=True)
    for sym, ann, n in indiv[:15]:
        print(f"  {sym:<14} 在场期年化 {ann*100:+6.1f}%   在场{n}月")
    return 0

if __name__ == "__main__":
    sys.exit(main())