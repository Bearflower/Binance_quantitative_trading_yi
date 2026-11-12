"""Exact PM fill and net-position presentation for the order-grid notifier."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class TradeFact:
    epoch_id: int
    order_id: str
    trade_id: str
    side: str
    quantity: Decimal
    price: Decimal
    realized_pnl: Decimal
    commission: Decimal
    commission_asset: str


@dataclass(frozen=True)
class PositionSnapshot:
    amount: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class ReportTotals:
    phase: Decimal
    cumulative: Decimal
    phase_realized: Decimal
    cumulative_realized: Decimal
    phase_funding: Decimal
    cumulative_funding: Decimal


def summarize(facts: Iterable[TradeFact], epoch_id: int,
              position: PositionSnapshot, phase_funding: Decimal,
              cumulative_funding: Decimal) -> ReportTotals:
    facts = tuple(facts)
    if epoch_id <= 0 or not all(value.is_finite() for value in (
            position.amount, position.entry_price, position.mark_price,
            position.unrealized_pnl, phase_funding, cumulative_funding)):
        raise ValueError("invalid report evidence")
    for fact in facts:
        if (fact.commission_asset != "USDT" or fact.commission < 0
                or not all(value.is_finite() for value in (
                    fact.quantity, fact.price, fact.realized_pnl, fact.commission))):
            raise ValueError("invalid commission asset or fill evidence")
    phase_realized = sum((fact.realized_pnl - fact.commission
                          for fact in facts if fact.epoch_id == epoch_id), Decimal(0))
    cumulative_realized = sum((fact.realized_pnl - fact.commission
                               for fact in facts), Decimal(0))
    return ReportTotals(phase_realized + phase_funding + position.unrealized_pnl,
                        cumulative_realized + cumulative_funding + position.unrealized_pnl,
                        phase_realized, cumulative_realized,
                        phase_funding, cumulative_funding)


def _money(value: Decimal) -> str:
    return f"{value:+.4f}"


def render_report(fact: TradeFact, position: PositionSnapshot,
                  totals: ReportTotals) -> str:
    direction = "多" if position.amount > 0 else "空" if position.amount < 0 else "空仓"
    amount = f"{direction} {abs(position.amount):f} ETH" if position.amount else "空仓 (0 ETH)"
    action = "买入" if fact.side == "BUY" else "卖出"
    return (
        "📋 【ETHUSDT 自动网格】实际成交与持仓\n"
        f"网格阶段: #{fact.epoch_id}\n"
        f"成交: {action} {fact.quantity:f} ETH @ {fact.price:f} USDT\n"
        f"交易所成交 ID: {fact.trade_id}\n"
        f"本笔已实现盈亏: {_money(fact.realized_pnl)} USDT\n"
        f"本笔手续费: -{fact.commission:.4f} USDT\n"
        f"当前净仓: {amount}\n"
        f"持仓均价 / 标记价: {position.entry_price:f} / {position.mark_price:f} USDT\n"
        f"当前未实现盈亏: {_money(position.unrealized_pnl)} USDT\n"
        f"当前网格阶段盈亏: {_money(totals.phase)} USDT\n"
        f"执行器累计盈亏: {_money(totals.cumulative)} USDT\n"
        "口径: 本执行器成交已实现盈亏 - 手续费 + ETHUSDT 资金费 + 当前净仓未实现盈亏；"
        "持仓为发送时 PM 快照。"
    )
