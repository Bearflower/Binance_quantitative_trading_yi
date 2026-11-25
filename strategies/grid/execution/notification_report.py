"""Exact PM fill and net-position presentation for the order-grid notifier."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from shared.notification_time import beijing_time


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
    phase_epoch_id: int
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
    return ReportTotals(epoch_id,
                        phase_realized + phase_funding + position.unrealized_pnl,
                        cumulative_realized + cumulative_funding + position.unrealized_pnl,
                        phase_realized, cumulative_realized,
                        phase_funding, cumulative_funding)


def _money(value: Decimal) -> str:
    return f"{value:+.4f}"


def render_report(fact: TradeFact, position: PositionSnapshot,
                  totals: ReportTotals, *, role=None, recorded_ms=None, epoch_status=None, pm=True) -> str:
    mode = "PM" if pm else "经典合约"
    purpose = {"ENTRY": "网格委托", "REVERSE": "网格配对", "EXIT": "网格退出",
               "STOP": "止损保护"}.get(role, "委托")
    side = "买入" if fact.side == "BUY" else "卖出"
    lines = [f"【ETHUSDT 实盘网格】{purpose}成交 · 第 {fact.epoch_id} 轮",
             f"{side} {fact.quantity:f} ETH @ {fact.price:f} USDT"]
    if recorded_ms:
        lines.append(f"成交记录时间（北京时间） {beijing_time(recorded_ms)}")
    lines += [f"推送时间（北京时间） {beijing_time()}", "",
              f"本笔扣费后 {_money(fact.realized_pnl - fact.commission)} USDT",
              f"毛盈亏 {_money(fact.realized_pnl)} ｜ 本笔手续费 -{fact.commission:.4f} USDT", ""]
    if position.amount:
        direction = "多" if position.amount > 0 else "空"
        lines += [f"当前净仓: {direction} {abs(position.amount):f} ETH（{mode} 快照）",
                  f"持仓均价 {position.entry_price:f} ｜ 标记价 {position.mark_price:f} USDT",
                  f"当前浮盈亏 {_money(position.unrealized_pnl)} USDT"]
    else:
        lines.append(f"当前净仓: 空仓（{mode} 快照）")
    status = {"CLOSED": "已结束", "ACTIVE": "未结束"}.get(epoch_status, "")
    lines += ["", f"最近网格阶段  #{totals.phase_epoch_id} {status}".rstrip(),
              f"当前网格阶段盈亏: {_money(totals.phase)} USDT",
              f"执行器累计盈亏: {_money(totals.cumulative)} USDT",
              "阶段和累计均已扣手续费，计入资金费与当前浮盈亏。",
              f"成交 ID  {fact.trade_id}"]
    return "\n".join(lines)
