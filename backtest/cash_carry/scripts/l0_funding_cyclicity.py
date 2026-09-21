#!/usr/bin/env python3
"""[本地] 2 年 funding 周期性分析——判断 L0 是否有时机可抓

核心问题：过去 730 天，每币的 funding 净费率（做空永续收取）在多少时间段
超过成本阈值（约 +8%/年 覆盖资金成本3%+手续费0.15%+滑点），从而 cash-and-carry
有可捕捉的"窗口"。如果高费率窗口占比很低或几乎不出现，L0 无可抓之肉。

对每个币输出：
    - funding_annual_mean:    2 年 funding 合成年化均值（全期）
    - funding_annual_max:     任一月 funding 合成年化的峰值
    - months_above_threshold: 月级 funding 年化 > 阈值 的月数
    - above_ratio:            高费率月占总数据月比例
    - top_month:              最高月的 funding 年化
    - bottom_month:           最低月的 funding 年化
    - skew:                   月分布正偏（牛期 funding 高）是否稳定

全部按"做空永续收取 funding"口径（funding>0 收钱）。
"""
import csv
import glob
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone

# 数据目录：默认用 2 年全量（data_2y），可用 argv 覆盖
_DATA_ARG = os.path.join("..", "data_2y") if len(sys.argv) > 1 else os.path.join("..", "data")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), _DATA_ARG)
SYM_DIR = os.path.join(DATA_DIR, "sym")
MANIFEST = os.path.join(DATA_DIR, "symbols.json")

# 成本阈值（年化）：资金成本 + 手续费，做空现货的现金占用按机会成本 3%
COST_THRESHOLD_YIELD = 0.0315     # 3% 资金成本 + 0.15% 双边手续费
FUNDING_PERIODS_PER_DAY = 3       # 每 8h 一次


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def load_funding_full(symbol: str):
    """读取完整 funding (time, rate) 序列"""
    path = os.path.join(SYM_DIR, symbol, "funding.csv")
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) >= 2:
                try:
                    out.append((parse_ts(row[0]), float(row[1])))
                except (TypeError, ValueError):
                    continue
    return out


def monthly_funding_annual(symbol):
    """按自然月聚合 funding，折算年化。返回 {YYYY-MM: annual_yield}"""
    rows = load_funding_full(symbol)
    monthly = {}
    for t, rate in rows:
        key = t.strftime("%Y-%m")
        monthly.setdefault(key, []).append(rate)
    out = {}
    for k, v in monthly.items():
        daily = sum(v) / (len(v) / FUNDING_PERIODS_PER_DAY)   # 本月日均 funding
        out[k] = daily * 365.0                                 # 折算年化
    return out


def analyze(symbol, cost: float = COST_THRESHOLD_YIELD):
    m = monthly_funding_annual(symbol)
    if not m:
        return None
    all_method = []
    months = []
    for k, v in sorted(m.items()):
        months.append(k)
        all_method.append(v)
    total_days_months = len(months)
    annual_mean = sum(all_method) / len(all_method)
    annual_max = max(all_method)
    annual_min = min(all_method)
    above = [v for v in all_method if v >= cost]
    above_months = [k for k, v in sorted(m.items()) if v >= cost]
    above_ratio = len(above) / len(all_method) if all_method else 0.0
    above_str = ",".join(above_months) if above_months else ""
    # 全年funding累计（复利近似：按日累加）
    rows = load_funding_full(symbol)
    total_funding = sum(rate for _, rate in rows) if rows else 0.0
    return {
        "symbol": symbol,
        "n_months": total_days_months,
        "annual_mean": annual_mean,
        "annual_max": annual_max,
        "annual_min": annual_min,
        "funding_total_2y": total_funding,
        "above_ratio": above_ratio,
        "above_n_months": len(above_months),
        "above_months": above_str,
    }


def main():
    if not os.path.exists(MANIFEST):
        print(f"❌ 未找到 {MANIFEST}")
        return 1
    with open(MANIFEST, encoding="utf-8") as f:
        universe = json.load(f)

    results = []
    for item in universe:
        sym = item["symbol"]
        r = analyze(sym)
        if r:
            results.append(r)

    results.sort(key=lambda x: x["annual_mean"], reverse=True)

    out_csv = os.path.join(DATA_DIR, "funding_cyclicity.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "symbol", "n_months", "annual_mean", "annual_max", "annual_min",
            "funding_total_2y", "above_ratio", "above_n_months", "above_months",
        ])
        w.writeheader()
        w.writerows(results)

    print("=" * 100)
    print("L0 funding 周期性：每币 730 天月级 funding 年化（做空永续收取）")
    print(f"有效币数: {len(results)}    成本阈值: {COST_THRESHOLD_YIELD*100:.1f}%/年（3%资金+0.15%手续费）")
    print("=" * 100)
    print(f"{'币':<14}{'月数':>5}{'均值%':>9}{'峰值%':>9}{'谷值%':>9}{'funding2y%':>12}{'高月占比':>9}{'高月数':>6}")
    print("-" * 100)
    for r in results[:25]:
        am = f"{r['annual_mean']*100:+.1f}" if not math.isnan(r['annual_mean']) else "  n/a"
        mx = f"{r['annual_max']*100:+.1f}" if not math.isnan(r['annual_max']) else "  n/a"
        mn = f"{r['annual_min']*100:+.1f}" if not math.isnan(r['annual_min']) else "  n/a"
        print(f"{r['symbol']:<14}{r['n_months']:>5}{am:>9}{mx:>9}{mn:>9}"
              f"{r['funding_total_2y']*100:>12.2f}{r['above_ratio']*100:>9.1f}{r['above_n_months']:>6}")

    # 全局统计
    means = [r["annual_mean"] for r in results]
    maxs = [r["annual_max"] for r in results]
    ratios = [r["above_ratio"] for r in results]
    med_m = sorted(means)[len(means)//2]
    med_max = sorted(maxs)[len(maxs)//2]
    med_ratio = sorted(ratios)[len(ratios)//2]
    n_have_window = sum(1 for r in results if r["annual_max"] >= COST_THRESHOLD_YIELD)
    n_above_50pct = sum(1 for r in results if r["above_ratio"] >= 0.5)

    print("-" * 100)
    print("全局统计:")
    print(f"  均值/中位数净 funding 年化: {statistics.mean(means)*100:+.2f}% / {med_m*100:+.2f}%")
    print(f"  月峰值 funding 年化 中位数: {med_max*100:+.1f}%")
    print(f"  高费率月占比 中位数: {med_ratio*100:.1f}%")
    print(f"  出现过可捕捉窗口(月峰值≥{COST_THRESHOLD_YIELD*100:.1f}%)的币: {n_have_window}/{len(results)}")
    print(f"  高费率月占比≥50%的币: {n_above_50pct}/{len(results)}")
    print()
    print(f"结果已存: {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())