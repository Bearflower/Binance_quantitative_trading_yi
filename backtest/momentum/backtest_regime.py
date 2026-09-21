#!/usr/bin/env python3
"""市场行情状态机（三态 regime）价值验证回测

验证命题：
    一个「全局三态 regime 过滤器」（趋势多/趋势空/震荡，BTC 主导）
    作为择时开关，能否改善一个横截面策略的风险调整后表现。

为什么用横截面动量做测试腿：
    上轮动量回测已证明「横截面动量选币」自身不对独立策略成立，
    但它本质是「选币引擎」——恰好适合用来隔离测量「择时过滤器」的增量。
    若 regime 过滤器能让同一选币引擎的夏普/回撤显著改善，
    就证明「市场状态机作为全策略上游过滤层」有价值。

三态判定（BTC 日线，无前视）：
    - TREND_UP   : BTC close > EMA20 且 EMA20 斜率 > +阈值
    - TREND_DOWN : BTC close < EMA20 且 EMA20 斜率 < -阈值
    - RANGE      : 其余

对比方案（均用同池、同调仓周期、同交易成本）：
    A. 无过滤      : 总是做多 Top N 动量
    B. 仅趋势多    : 仅 TREND_UP 做多，其余空仓（现金）
    C. 三态方向    : TREND_UP 做多 / TREND_DOWN 做空 / RANGE 空仓

输出：各方案 年化收益、夏普、最大回撤、卡玛比率、持仓占比、市场状态分布。
"""
import csv
import os
from datetime import datetime
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

# ==================== 回测配置区 ====================
CONFIG = {
    "rebalance_days": 7,          # 调仓周期（天）
    "top_n": 10,                  # 多头/空头持股数
    "momentum_window": 30,        # 动量窗口（天）
    "fee_per_side": 0.0005,       # 手续费 0.05%/边
    "slippage_per_side": 0.0005,  # 滑点 0.05%/边
    # 三态 regime 判定参数（BTC 日线）
    "regime_ema_period": 20,      # BTC EMA 周期
    "regime_slope_lookback": 5,   # EMA 斜率回看期数
    "regime_slope_threshold": 0.001,  # EMA 斜率阈值（5日百分比，0.1%）
}

# 路径配置
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
KLINES_DIR = os.path.join(DATA_DIR, "klines")

RANGE_STATE = "RANGE"
TREND_UP_STATE = "TREND_UP"
TREND_DOWN_STATE = "TREND_DOWN"


# ==================== 数据加载 ====================

def load_close_panel(min_rows: int) -> pd.DataFrame:
    """加载受限池日线收盘价面板（date × symbol）"""
    closes: Dict[str, pd.Series] = {}
    for fname in os.listdir(KLINES_DIR):
        if not fname.endswith("_1d.csv"):
            continue
        symbol = fname[:-len("_1d.csv")].upper()
        # 跳过锚数据（BTC/ETH 单独用于 regime，不进受限池）
        if symbol in ("BTCUSDT", "ETHUSDT"):
            continue
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
        raise FileNotFoundError("无受限池日线数据")
    panel = pd.DataFrame(closes)
    panel = panel[~panel.index.duplicated(keep="last")].sort_index()
    return panel.dropna(how="all")


def load_btc_close() -> pd.Series:
    """加载 BTC 日线收盘价（regime 锚）"""
    filepath = os.path.join(KLINES_DIR, "btcusdt_1d.csv")
    rows = list(csv.reader(open(filepath, encoding="utf-8")))
    header = rows[0]
    col_map = {name: idx for idx, name in enumerate(header)}
    times, prices = [], []
    for r in rows[1:]:
        try:
            ts = pd.to_datetime(r[col_map["open_time"]])
            px = float(r[col_map["close_price"]])
        except (ValueError, IndexError):
            continue
        times.append(ts)
        prices.append(px)
    return pd.Series(prices, index=pd.DatetimeIndex(times)).sort_index()


# ==================== Regime 判定 ====================

def calc_regime_series(btc_close: pd.Series, cfg: Dict) -> pd.Series:
    """计算三态 regime 时间序列（无前视）

    Args:
        btc_close: BTC 收盘价序列
        cfg: 配置字典

    Returns:
        Series，index 为日期，值为 TREND_UP / TREND_DOWN / RANGE
    """
    ema_period = cfg["regime_ema_period"]
    slope_lookback = cfg["regime_slope_lookback"]
    slope_threshold = cfg["regime_slope_threshold"]

    ema = btc_close.ewm(span=ema_period, adjust=False).mean()
    # EMA 斜率：当期 EMA / lookback 期前 EMA - 1
    slope = ema / ema.shift(slope_lookback) - 1

    states = {}
    for d in ema.index:
        e = ema.get(d, np.nan)
        s = slope.get(d, np.nan)
        c = btc_close.get(d, np.nan)
        if not np.isfinite(e) or not np.isfinite(s) or not np.isfinite(c):
            states[d] = RANGE_STATE
            continue
        if c > e and s > slope_threshold:
            states[d] = TREND_UP_STATE
        elif c < e and s < -slope_threshold:
            states[d] = TREND_DOWN_STATE
        else:
            states[d] = RANGE_STATE
    return pd.Series(states).sort_index()


# ==================== 指标计算 ====================

def calc_metrics(equity: pd.Series) -> Dict[str, float]:
    """计算绩效指标"""
    rets = equity / equity.shift(1) - 1
    rets = rets.dropna()
    if rets.empty:
        return {"total": 0.0, "cagr": 0.0, "max_dd": 0.0, "sharpe": 0.0,
                "calmar": 0.0, "years": 0.0}
    days = (equity.index[-1] - equity.index[0]).days
    years = max(days / 365.0, 1e-9)
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (1 + total) ** (1 / years) - 1 if (1 + total) > 0 else float("nan")
    roll_max = equity.cummax()
    drawdown = equity / roll_max - 1
    max_dd = drawdown.min()
    vol = rets.std() * np.sqrt(365)
    sharpe = (rets.mean() * 365) / vol if vol > 1e-9 else 0.0
    calmar = cagr / abs(max_dd) if max_dd < -1e-9 else 0.0
    return {"total": total, "cagr": cagr, "max_dd": max_dd, "sharpe": sharpe,
            "calmar": calmar, "years": years}


# ==================== 回测主逻辑 ====================

def run_rotation_with_regime(
    closes: pd.DataFrame,
    regime: pd.Series,
    mode: str,
    cfg: Dict,
) -> Tuple[pd.Series, float]:
    """带 regime 过滤的横截面动量轮动

    Args:
        closes: date × symbol 收盘价面板
        regime: 三态 regime 序列
        mode: "unfiltered" / "long_only" / "directional"
        cfg: 配置字典

    Returns:
        (净值序列, 持仓时间占比)
    """
    window = cfg["momentum_window"]
    n_top = cfg["top_n"]
    rebal = cfg["rebalance_days"]
    fee = cfg["fee_per_side"]
    slip = cfg["slippage_per_side"]

    mom = closes / closes.shift(window + 1) - 1
    hold_ret = closes / closes.shift(rebal) - 1

    dates = closes.index
    start_idx = window + 1
    rebal_dates = list(dates[start_idx::rebal])
    rebal_dates = [d for d in rebal_dates if d in mom.index and d in hold_ret.index]

    equity = [1.0]
    prev_vec = pd.Series(dtype=float)
    holding_days = 0
    total_days = 0

    for idx in range(1, len(rebal_dates)):
        cur = rebal_dates[idx]
        prev = rebal_dates[idx - 1]
        # 本持有期 regime（用持有期起点 prev 判定的状态，无前视）
        state = regime.get(prev, RANGE_STATE) if prev in regime.index else RANGE_STATE

        mom_snapshot = mom.loc[prev].dropna()
        if len(mom_snapshot) < n_top:
            equity.append(equity[-1])
            prev_vec = pd.Series(dtype=float)
            total_days += 1
            continue

        ranked = mom_snapshot.sort_values(ascending=False)
        long_syms = list(ranked.index[:n_top])
        short_syms = list(ranked.index[-n_top:])

        # 依据 mode + 状态决定持仓
        new_weights: Dict[str, float] = {}
        if mode == "unfiltered":
            # 总是做多 Top N
            w = 1.0 / n_top
            new_weights = {s: w for s in long_syms}
            holding_days += 1
        elif mode == "long_only":
            # 仅 TREND_UP 做多，其余空仓
            if state == TREND_UP_STATE:
                w = 1.0 / n_top
                new_weights = {s: w for s in long_syms}
                holding_days += 1
        elif mode == "directional":
            # TREND_UP 做多 / TREND_DOWN 做空 / RANGE 空仓
            w = 1.0 / n_top
            if state == TREND_UP_STATE:
                new_weights = {s: w for s in long_syms}
                holding_days += 1
            elif state == TREND_DOWN_STATE:
                new_weights = {s: -w for s in short_syms}
                holding_days += 1
        else:
            raise ValueError(f"未知 mode: {mode}")

        new_vec = pd.Series(new_weights)
        turnover = 0.5 * (new_vec.sub(prev_vec, fill_value=0.0)).abs().sum()

        if new_vec.empty:
            port_ret = 0.0
            cost = 0.0
        else:
            leg_ret = hold_ret.loc[cur, list(new_vec.index)]
            port_ret = float((leg_ret * new_vec).sum())
            cost = turnover * (fee + slip) * 2
        equity.append(equity[-1] * (1 + port_ret - cost))
        prev_vec = new_vec
        total_days += 1

    holding_ratio = holding_days / total_days if total_days else 0.0
    return pd.Series(equity, index=pd.DatetimeIndex(rebal_dates[:len(equity)])), holding_ratio


def print_metrics(name: str, equity: pd.Series, holding_ratio: float) -> None:
    """打印单方案指标"""
    m = calc_metrics(equity)
    print(f"  {name:<12} | 年化 {m['cagr']*100:+7.1f}% | 夏普 {m['sharpe']:5.2f} | "
          f"回撤 {m['max_dd']*100:6.1f}% | 卡玛 {m['calmar']:5.2f} | "
          f"持仓 {holding_ratio*100:4.0f}%")


def main() -> int:
    """主流程"""
    print("=" * 62)
    print("市场行情状态机（三态 regime）价值验证回测")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"配置: 动量{CONFIG['momentum_window']}d | Top{CONFIG['top_n']} | "
          f"调仓{CONFIG['rebalance_days']}d | EMA{CONFIG['regime_ema_period']}")
    print("=" * 62)

    closes = load_close_panel(90)
    btc_close = load_btc_close()
    print(f"受限池币种数: {len(closes.columns)}")
    print(f"时间范围: {closes.index[0].date()} ~ {closes.index[-1].date()}")

    regime = calc_regime_series(btc_close, CONFIG)

    # 市场状态分布
    state_counts = regime.value_counts()
    total_states = len(regime)
    print("\n市场状态分布（BTC 主导三态）:")
    state_names = {TREND_UP_STATE: "趋势多", TREND_DOWN_STATE: "趋势空", RANGE_STATE: "震荡"}
    for st in [TREND_UP_STATE, TREND_DOWN_STATE, RANGE_STATE]:
        cnt = state_counts.get(st, 0)
        print(f"  {state_names[st]:<6} : {cnt:4d} 天 ({cnt/total_states*100:4.1f}%)")

    print("\n===== 对比结果（同池横截面动量，30d 动量窗口）=====")
    results = [
        ("无过滤", "unfiltered"),
        ("仅趋势多", "long_only"),
        ("三态方向", "directional"),
    ]
    for name, mode in results:
        eq, hold = run_rotation_with_regime(closes, regime, mode, CONFIG)
        print_metrics(name, eq, hold)

    print("\n===== 结论判定 =====")
    print("若「仅趋势多」或「三态方向」相比「无过滤」在夏普/卡玛上显著提升，")
    print("同时最大回撤显著收窄 → 证明 regime 过滤器（择时开关）有价值。")
    print("反之，若过滤器未能改善风险调整后收益，则说明单纯三态择时不够，")
    print("需叠加方向偏好/仓位乘数或更多态（如 PANIC 恐慌态）。")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())