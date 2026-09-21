#!/usr/bin/env python3
"""[本地] 测算每币 180 天 cash-and-carry 净年化 + 分段（牛/崩/震荡）

方法：做多现货 + 做空等额永续（方向中性）：
    - 收益主要来自做空永续收取的 funding（现货无 funding）
    - 价格腿：现货永续对冲后，价格走势基本抵消；基差收敛影响计入
    - 消耗现金流：现货全额占用资金（资金成本按年化 x% 算）；
      rose 永续仅需保证金（按名义 e.g. 5%），相对小
    - 手续费：开平各一次，现货 taker 0.1%，永续 taker 0.05%

年化净收益（占现货本金口径）≈
    Σ(daily funding)/天 ×365 − 资金成本年化% − 双边手续费摊到年

分段：
    用 BTC 现货 180 天收盘，按滚动 30 日收益率阈值划分牛/崩/震荡区间，
    分别统计各段内 funding 合成年化。

输出：
    <OUTPUT_DIR>/summary.csv         每币一行：净年化/分段/funding均值/手续费等
    控制台打印核心汇总
"""
import csv
import glob
import json
import math
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# ==================== 配置区 ====================
# 数据目录：默认 2 年全量（data_2y），可用第 2 个 argv 覆盖（如传入 data 用 180 天）
_DATA_ARG = os.path.join("..", "data") if len(sys.argv) > 1 and sys.argv[1] in ("data",) else os.path.join("..", "data_2y")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), _DATA_ARG)
SYM_DIR = os.path.join(DATA_DIR, "sym")
MANIFEST = os.path.join(DATA_DIR, "symbols.json")

# 资金成本（现货占用）年化百分数
CAPITAL_COST_YIELD = 0.03          # 3% —— 保守取机会成本（等权基准）
# 手续费
SPOT_FEE = 0.001                   # 现货 taker 0.1%（开+平 各一次）
PERP_FEE = 0.0005                  # 永续 taker 0.05%
# 分段：BTC 滚动 30 日收益率阈值
REGIME_DAYS = 30
BULL_THRESH = 0.10                 # > +10% 牛
CRASH_THRESH = -0.10               # < -10% 崩
FUNDING_PERIODS_PER_DAY = 3        # 每 8h 一次


def parse_ts(s: str) -> datetime:
    """解析 UTC 字符串到 datetime（本地时区无关）"""
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def load_funding(symbol: str) -> List[float]:
    """读取 funding 费率序列（按时间升序）"""
    path = os.path.join(SYM_DIR, symbol, "funding.csv")
    rates = []
    if not os.path.exists(path):
        return rates
    with open(path, encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)  # 跳过表头
        for row in r:
            if len(row) >= 2:
                try:
                    rates.append(float(row[1]))
                except (TypeError, ValueError):
                    continue
    return rates


def load_close(symbol: str, kind: str) -> List[Tuple[datetime, float]]:
    """读取 K线 close 序列（kind: spot / perp），返回 (时间, 收盘价) 升序"""
    path = os.path.join(SYM_DIR, symbol, f"{kind}_1h.csv")
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) >= 5:
                try:
                    out.append((parse_ts(row[0]), float(row[4])))
                except (TypeError, ValueError):
                    continue
    return out


def funding_yield_net(symbol: str,
                      capital_cost: float = CAPITAL_COST_YIELD) -> Tuple[float, float, int]:
    """估算该币 180 天 cash-and-carry 净年化收益率（占现货本金口径，小数）

    返回: (净年化小数, 总 funding 收益小数, 持仓天数)
    """
    rates = load_funding(symbol)
    if len(rates) < 100:   # 数据不足
        return 0.0, 0.0, 0

    days = len(rates) / FUNDING_PERIODS_PER_DAY   # 约 180
    # funding 累计：做空永续收 funding，费率直接线性叠加（对冲把头寸固定在名义1）
    funding_total = sum(rates)                      # 180天累计 funding（小数）
    # 摊到年
    funding_annual = funding_total / days * 365.0

    # 资金成本：现货全额占用，年化 capital_cost
    cost_annual = capital_cost

    # 手续费：开平双边一次，摊到 180 天再年化
    roundtrip_fee = SPOT_FEE + PERP_FEE             # 0.0015 一次完成
    fee_annual = roundtrip_fee / days * 365.0

    net_annual = funding_annual + cost_annual + fee_annual  # cost/fee 都是负贡献
    # 扣除资金成本与手续费（它们为负）：funding_annual − cost − fee
    net_annual = funding_annual - cost_annual - fee_annual
    return net_annual, funding_total, int(days)


def btc_regime_map(symbol: str, start: datetime, end: datetime) -> Dict[str, int]:
    """基于 BTC 现货 30 日滚动收益，为区间打 regime 标记（小时级）

    返回 {date(日期str): regime}, regime ∈ 牛/崩/震荡，且对给定日有效（仅当 BTC 有数据）
    """
    btc_closes = load_close("BTCUSDT", "spot")
    btc_closes = [c for c in btc_closes if start <= c[0] <= end]
    if len(btc_closes) < REGIME_DAYS * 24:
        return {}
    # 构建按小时 date key 的 dict date->close
    by_time = {c[0]: c[1] for c in btc_closes}
    times = sorted(by_time.keys())

    regime_by_date: Dict[str, str] = {}
    # 以每日 00:00 为锚点标记当天
    day_anchors: Dict[str, float] = {}
    for t in times:
        day = t.strftime("%Y-%m-%d")
        if day not in day_anchors:
            day_anchors[day] = by_time[t]

    # 用小时序列算 30 日滚动收益（对照 REGIME_DAYS*24 根之前）
    for i in range(REGIME_DAYS * 24, len(times)):
        t = times[i]
        past = times[i - REGIME_DAYS * 24]
        ret30 = by_time[t] / by_time[past] - 1.0
        day = t.strftime("%Y-%m-%d")
        if ret30 >= BULL_THRESH:
            regime_by_date[day] = "牛"
        elif ret30 <= CRASH_THRESH:
            regime_by_date[day] = "崩"
        else:
            regime_by_date[day] = "震荡"
    return regime_by_date


def segment_funding(symbol: str, regime_map: Dict[str, str]) -> Dict[str, float]:
    """按 regime 分段统计 funding 合成的年化（乘 365 口径）"""
    path = os.path.join(SYM_DIR, symbol, "funding.csv")
    seg = {"牛": [], "崩": [], "震荡": []}
    if not os.path.exists(path):
        return {k: math.nan for k in seg}
    with open(path, encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)
        for row in r:
            if len(row) < 2:
                continue
            try:
                t = parse_ts(row[0])
                rate = float(row[1])
            except (TypeError, ValueError):
                continue
            day = t.strftime("%Y-%m-%d")
            reg = regime_map.get(day)
            if reg and reg in seg:
                seg[reg].append(rate)
    out = {}
    for k, v in seg.items():
        if v:
            daily = sum(v) / (len(v) / FUNDING_PERIODS_PER_DAY)
            out[k] = daily * 365.0
        else:
            out[k] = math.nan
    return out


def main() -> int:
    if not os.path.exists(MANIFEST):
        print(f"❌ 未找到 {MANIFEST}")
        return 1
    with open(MANIFEST, encoding="utf-8") as f:
        universe = json.load(f)

    # BTC 全局 regime 只需要算一次（用其自身时间范围）
    btc_closes = load_close("BTCUSDT", "spot")
    if btc_closes:
        start = btc_closes[0][0]
        end = btc_closes[-1][0]
    else:
        start = end = datetime.now(timezone.utc)
    regime_map = btc_regime_map("BTCUSDT", start, end)

    rows = []
    seg_tot = {"牛": 0.0, "崩": 0.0, "震荡": 0.0}
    seg_n = {"牛": 0, "崩": 0, "震荡": 0}
    bull_pos = crash_pos = range_pos = 0

    for item in universe:
        sym = item["symbol"]
        net_annual, funding_total, days = funding_yield_net(sym)
        if days == 0:
            continue
        segs = segment_funding(sym, regime_map)
        for k in seg_tot:
            if not math.isnan(segs[k]):
                seg_tot[k] += segs[k]
                seg_n[k] += 1
        bl = segs.get("牛", math.nan)
        cr = segs.get("崩", math.nan)
        rn = segs.get("震荡", math.nan)
        if not math.isnan(bl) and bl > 0:
            bull_pos += 1
        if not math.isnan(cr) and cr > 0:
            crash_pos += 1
        if not math.isnan(rn) and rn > 0:
            range_pos += 1
        rows.append({
            "symbol": sym,
            "days": days,
            "net_annual": net_annual,
            "funding_total": funding_total,
            "regime_bull_annual": bl,
            "regime_crash_annual": cr,
            "regime_range_annual": rn,
        })

    # 排序输出
    rows.sort(key=lambda x: x["net_annual"], reverse=True)

    out_csv = os.path.join(DATA_DIR, "summary.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        w.writeheader()
        w.writerows(rows)

    print("=" * 100)
    print(f"L0 cash-and-carry: 每币 180 天净年化（做多现货+做空永续，吃funding，扣资金成本3%+双边手续费0.15%）")
    print(f"有效币数: {len(rows)}   数据天数: BTC spot {len(btc_closes)} 根")
    print(f"参考: 主流5币 funding 年化约 ~9%; 山寨 30%-100%+ 是'肉'所在")
    print("=" * 100)
    print(f"{'币':<14}{'天数':>6}{'净年化%':>10}{'funding总%':>12}{'牛%':>8}{'崩%':>8}{'震荡%':>8}")
    print("-" * 100)
    for r in rows[:20]:
        b = f"{r['regime_bull_annual']*100:+.1f}" if not math.isnan(r['regime_bull_annual']) else "  n/a"
        c = f"{r['regime_crash_annual']*100:+.1f}" if not math.isnan(r['regime_crash_annual']) else "  n/a"
        rg = f"{r['regime_range_annual']*100:+.1f}" if not math.isnan(r['regime_range_annual']) else "  n/a"
        print(f"{r['symbol']:<14}{r['days']:>6}{r['net_annual']*100:>10.2f}"
              f"{r['funding_total']*100:>12.2f}{b:>8}{c:>8}{rg:>8}")
    print("-" * 100)
    # 末5
    for r in rows[-5:]:
        b = f"{r['regime_bull_annual']*100:+.1f}" if not math.isnan(r['regime_bull_annual']) else "  n/a"
        c = f"{r['regime_crash_annual']*100:+.1f}" if not math.isnan(r['regime_crash_annual']) else "  n/a"
        rg = f"{r['regime_range_annual']*100:+.1f}" if not math.isnan(r['regime_range_annual']) else "  n/a"
        print(f"{r['symbol']:<14}{r['days']:>6}{r['net_annual']*100:>10.2f}"
              f"{r['funding_total']*100:>12.2f}{b:>8}{c:>8}{rg:>8}")

    # 汇总
    def avg(dc, n):
        return dc / n if n else math.nan

    print("=" * 100)
    print("分段 funding 年化均值（占名义本金）:")
    for k in seg_tot:
        print(f"  {k}: 均值 {avg(seg_tot[k], seg_n[k])*100:+.2f}%  (样本 {seg_n[k]} 币)")
    print()
    print(f"分段正 funding 币占比: 牛 {bull_pos}/{seg_n['牛']}  崩 {crash_pos}/{seg_n['崩']}  震荡 {range_pos}/{seg_n['震荡']}")
    all_net = [r["net_annual"] for r in rows]
    pos_n = sum(1 for x in all_net if x > 0)
    print(f"净年化>0 币数: {pos_n}/{len(all_net)}  中位数净年化: {sorted(all_net)[len(all_net)//2]*100:+.2f}%")
    print(f"结果已存: {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())