"""
费用/资金费/滑点口径模块。

全部使用 Decimal；不连 Binance，只读本地快照的历史 funding rate 表。
"""

from __future__ import annotations

from decimal import Decimal
from typing import List, Tuple, Optional
from datetime import datetime

# 复用项目内统一盈亏计算（禁止重复实现）
from shared.trade_logger import TradeLogger


def taker_fee_notional(notional: Decimal, bps: Decimal) -> Decimal:
    """计算单边 taker 手续费。返回负值（支出）。

    Args:
        notional: 名义价值（USDT）
        bps: 手续费 bps（Decimal，如 5 表示 0.05%）
    """
    fee = notional * bps / Decimal("10000")
    return -fee


def total_roundtrip_fee(notional_entry: Decimal, notional_exit: Decimal, bps: Decimal) -> Decimal:
    """双边 taker 手续费合计（入场 + 出场）。"""
    entry = taker_fee_notional(notional_entry, bps)
    exit_ = taker_fee_notional(notional_exit, bps)
    return entry + exit_


def slippage_cost(notional: Decimal, bps: Decimal, direction: str) -> Decimal:
    """计算单边滑点成本。LONG 方向多付 bps，SHORT 方向少收 bps；两者对 PnL 的影响同为负。
    返回负值。
    """
    # direction 保留以匹配上游调用签名（total_roundtrip_slippage 传入 LONG/SHORT），
    # 但计算本身与方向无关（滑点成本同为负）。
    cost = notional * bps / Decimal("10000")
    return -cost


def total_roundtrip_slippage(notional_entry: Decimal, notional_exit: Decimal, bps: Decimal) -> Decimal:
    """双边滑点合计。"""
    return slippage_cost(notional_entry, bps, "LONG") + slippage_cost(notional_exit, bps, "SHORT")


def calculate_pnl(
    direction: str,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
) -> Decimal:
    """统一盈亏计算——直接复用 shared.trade_logger.TradeLogger.calculate_pnl。
    回放器严禁重复实现，这里只是薄封装（便于测试时 monkey-patch）。
    """
    return TradeLogger.calculate_pnl(direction, entry_price, exit_price, quantity)


# ---------------- 资金费 ----------------

def sum_funding_fee(
    quantity: Decimal,
    entry_price: Decimal,
    exit_price: Decimal,      # 接口兼容：已用 entry_price 近似 mark_price，exit_price 未参与计算
    direction: str,
    funding_events: List[Tuple[datetime, Decimal]],
) -> Decimal:
    """计算持仓跨越的所有资金费结算时点的合计资金费（USDT）。

    Args:
        quantity: 持仓数量（币）
        entry_price: 入场价
        exit_price: 出场价
        direction: "LONG" 或 "SHORT"
        funding_events: [(funding_time, funding_rate), ...] 排好序，位于 [entry_time, exit_time] 区间内。
                        funding_rate 为 Decimal，如 0.0001 表示 0.01%。
                        仅用 funding_rate，不用时间戳（已由调用方过滤好区间）。

    Returns:
        资金费合计（USDT）。正 = 收取，负 = 支付。
        结算公式：每 8 小时一次，USDT 结算 = quantity * mark_price * funding_rate。
        回放器简化假设：mark_price ≈ 本次资金费结算时点附近的 K 线均价，
        但为了不引入额外快照依赖，我们用 entry_price 作为 mark_price 近似，
        然后用 (quantity * entry_price) * funding_rate 作为每笔资金费。
    """
    total = Decimal("0")
    for _, rate in funding_events:
        notional = quantity * entry_price
        # LONG 方向 funding_rate>0 → 多付 → 负；SHORT 方向 funding_rate>0 → 空收 → 正。
        if direction == "LONG":
            contribution = -notional * rate
        elif direction == "SHORT":
            contribution = notional * rate
        else:
            raise ValueError(f"不支持 direction: {direction}")
        total += contribution
    return total
