"""Read-only, fail-closed proof for an untouched recovered short position.

The exchange position is account-wide. A same-symbol trade after this entry can
belong to another strategy, so only an exact, unreduced entry can be verified
without an immutable strategy trade-to-lot ledger.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def _positive(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('invalid amount') from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError('invalid amount')
    return amount


def _id(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError('invalid id')
    try:
        result = int(value)
    except (ValueError, TypeError) as exc:
        raise ValueError('invalid id') from exc
    if result <= 0 or str(result) != str(value):
        raise ValueError('invalid id')
    return result


async def verify_unreduced_short(
    exchange: Any, *, symbol: str, entry_order_id: int, tp1_algo_id: int,
    opened_at: datetime, anchor: Decimal, entry_price: Decimal,
    remaining: Decimal, now: datetime,
) -> bool:
    """Authorize only a recent, uninterrupted lot with exchange-side protection.

    A False result is an ordinary evidence gap. No exception grants eligibility.
    The 6-day horizon stays inside PM UM's 7-day trade query window.
    """
    try:
        if (not isinstance(symbol, str) or not symbol.endswith('USDT')
                or type(entry_order_id) is not int or type(tp1_algo_id) is not int
                or entry_order_id <= 0 or tp1_algo_id <= 0):
            return False
        if (opened_at.tzinfo is None or now.tzinfo is None
                or opened_at > now or now - opened_at > timedelta(days=6)):
            return False
        anchor = _positive(anchor)
        entry_price = _positive(entry_price)
        if _positive(remaining) != anchor:
            return False
        positions = await exchange.get_position(symbol)
        if not isinstance(positions, list):
            return False
        matching = [position for position in positions
                    if isinstance(position, dict) and position.get('symbol') == symbol
                    and Decimal(str(position.get('positionAmt', '0'))) != 0]
        if len(matching) != 1:
            return False
        position = matching[0]
        if (Decimal(str(position.get('positionAmt'))) != -anchor
                or abs(_positive(position.get('entryPrice')) - entry_price)
                   > entry_price * Decimal('0.000001')):
            return False
        mark = _positive(position.get('markPrice'))
        order = await exchange.get_order(symbol, entry_order_id)
        if (not isinstance(order, dict) or order.get('symbol') != symbol
                or _id(order.get('orderId')) != entry_order_id
                or order.get('side') != 'SELL' or order.get('status') != 'FILLED'
                or _positive(order.get('executedQty')) != anchor
                or abs(_positive(order.get('avgPrice')) - entry_price)
                   > entry_price * Decimal('0.000001')):
            return False
        entry_fills = await exchange.get_user_trades(
            symbol, order_id=str(entry_order_id), limit=1000)
        if (not isinstance(entry_fills, list) or not entry_fills
                or len(entry_fills) >= 1000):
            return False
        entry_ids: set[int] = set()
        entry_qty = Decimal(0)
        first_ms: int | None = None
        for fill in entry_fills:
            if (not isinstance(fill, dict) or fill.get('symbol') != symbol
                    or _id(fill.get('orderId')) != entry_order_id
                    or fill.get('side') != 'SELL'):
                return False
            fill_id = _id(fill.get('id'))
            if fill_id in entry_ids:
                return False
            entry_ids.add(fill_id)
            entry_qty += _positive(fill.get('qty'))
            trade_ms = _id(fill.get('time'))
            first_ms = trade_ms if first_ms is None else min(first_ms, trade_ms)
        if entry_qty != anchor or first_ms is None:
            return False
        opened_ms = int(opened_at.astimezone(timezone.utc).timestamp() * 1000)
        now_ms = int(now.astimezone(timezone.utc).timestamp() * 1000)
        if first_ms < opened_ms - 3_600_000 or first_ms > now_ms:
            return False
        all_fills = await exchange.get_user_trades(
            symbol, start_time=first_ms, end_time=now_ms, limit=1000)
        if (not isinstance(all_fills, list) or len(all_fills) >= 1000
                or len(all_fills) != len(entry_fills)):
            return False
        if {_id(fill.get('id')) for fill in all_fills} != entry_ids:
            return False
        algo = await exchange.get_algo_order(tp1_algo_id)
        if (not isinstance(algo, dict) or _id(algo.get('algoId')) != tp1_algo_id
                or algo.get('symbol') != symbol or algo.get('side') != 'BUY'
                or algo.get('orderType') != 'TAKE_PROFIT'
                or algo.get('algoStatus') != 'NEW'
                or algo.get('reduceOnly') is not True
                or _positive(algo.get('quantity')) >= anchor
                or algo.get('actualOrderId') not in (None, '', '0', 0)):
            return False
        ordinary = await exchange.get_open_orders(symbol)
        if not isinstance(ordinary, list) or ordinary:
            return False
        algos = await exchange.get_open_algo_orders(symbol)
        if not isinstance(algos, list):
            return False
        tp1_seen = False
        stop_covered = Decimal(0)
        stop_all = False
        seen: set[int] = set()
        for active in algos:
            if not isinstance(active, dict) or active.get('symbol') != symbol:
                return False
            active_id = _id(active.get('algoId'))
            if active_id in seen:
                return False
            seen.add(active_id)
            if active_id == tp1_algo_id:
                tp1_seen = (active.get('algoStatus') == 'NEW'
                            and active.get('side') == 'BUY'
                            and active.get('orderType') == 'TAKE_PROFIT'
                            and active.get('reduceOnly') is True)
            if (active.get('side') == 'BUY'
                    and active.get('orderType') in ('STOP', 'STOP_MARKET')
                    and active.get('algoStatus') == 'NEW'
                    and _positive(active.get('triggerPrice')) > mark):
                if active.get('closePosition') is True:
                    stop_all = True
                elif active.get('reduceOnly') is True:
                    stop_covered += _positive(active.get('quantity'))
        return tp1_seen and (stop_all or stop_covered >= remaining)
    except Exception:
        return False
