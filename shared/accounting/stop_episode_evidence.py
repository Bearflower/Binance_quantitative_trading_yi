"""Verify a STOP closed the same position episode as its bound first entry.

This is read-only proof. A caller must persist it idempotently before using it
for a cooldown. Missing pages, a flat boundary, or an intervening reopen fail
closed.
"""

from decimal import Decimal, InvalidOperation

from shared.accounting.stop_exit_evidence import fetch_verified_stop_execution
from shared.position_entry_recovery import fetch_lot_fills


def _positive_id(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 and str(number) == str(value) else None


def _positive_qty(value):
    try:
        qty = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return qty if qty.is_finite() and qty > 0 else None


def verified_closed_stop_episode(binding, stop, entry_order, entry_fills, lot_fills):
    """Return a closed-episode event only when all order/fill identities agree."""
    if (not isinstance(binding, dict) or not isinstance(stop, dict)
            or not isinstance(entry_order, dict) or not isinstance(entry_fills, list)
            or not isinstance(lot_fills, list) or not entry_fills or not lot_fills):
        return None
    account = binding.get("account_id")
    strategy = binding.get("strategy")
    symbol = binding.get("symbol")
    entry_id = _positive_id(binding.get("episode_entry_order_id"))
    algo_id = _positive_id(binding.get("algo_id"))
    order_id = _positive_id(stop.get("order_id"))
    stop_qty = _positive_qty(stop.get("quantity"))
    bound_qty = _positive_qty(binding.get("stop_quantity"))
    stop_time = _positive_id(stop.get("last_fill_time_ms"))
    side = binding.get("stop_side")
    if (not account or not strategy or not symbol or entry_id is None
            or algo_id is None or order_id is None or entry_id == order_id
            or stop_qty is None or bound_qty is None or stop_qty > bound_qty
            or stop_time is None or side not in ("BUY", "SELL")
            or stop.get("account_id") != account or stop.get("symbol") != symbol
            or _positive_id(stop.get("algo_id")) != algo_id or stop.get("side") != side
            or stop.get("commission_asset") != "USDT"
            or _positive_id(entry_order.get("orderId")) != entry_id
            or entry_order.get("symbol") != symbol
            or entry_order.get("side") == side
            or entry_order.get("side") not in ("BUY", "SELL")):
        return None
    entry_qty = _positive_qty(entry_order.get("executedQty"))
    if entry_qty is None:
        return None
    entry_trade_ids = set()
    entry_sum = Decimal(0)
    anchor = None
    for fill in entry_fills:
        if not isinstance(fill, dict):
            return None
        trade_id = _positive_id(fill.get("id"))
        time_ms = _positive_id(fill.get("time"))
        qty = _positive_qty(fill.get("qty"))
        if (trade_id is None or trade_id in entry_trade_ids or time_ms is None
                or qty is None or _positive_id(fill.get("orderId")) != entry_id
                or fill.get("symbol") != symbol or fill.get("side") != entry_order["side"]):
            return None
        entry_trade_ids.add(trade_id)
        entry_sum += qty
        anchor = min(anchor, time_ms) if anchor is not None else time_ms
    if entry_sum != entry_qty or anchor is None or anchor >= stop_time:
        return None
    raw_stop_ids = stop.get("trade_ids")
    if not isinstance(raw_stop_ids, list) or not raw_stop_ids:
        return None
    expected_stop_ids = {_positive_id(x) for x in raw_stop_ids}
    if None in expected_stop_ids or len(expected_stop_ids) != len(raw_stop_ids):
        return None
    try:
        rows = sorted(lot_fills, key=lambda row: (int(row["time"]), int(row["id"])))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if (_positive_id(rows[0].get("orderId")) != entry_id
            or _positive_id(rows[0].get("time")) != anchor
            or _positive_id(rows[-1].get("orderId")) != order_id
            or _positive_id(rows[-1].get("time")) != stop_time):
        return None
    net = Decimal(0)
    seen = set()
    seen_entry = set()
    seen_stop = set()
    stop_sum = Decimal(0)
    direction = Decimal(1) if entry_order["side"] == "BUY" else Decimal(-1)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return None
        trade_id = _positive_id(row.get("id"))
        time_ms = _positive_id(row.get("time"))
        qty = _positive_qty(row.get("qty"))
        row_side = row.get("side")
        row_order = _positive_id(row.get("orderId"))
        if (trade_id is None or trade_id in seen or time_ms is None
                or time_ms < anchor or time_ms > stop_time or qty is None
                or row.get("symbol") != symbol or row_side not in ("BUY", "SELL")
                or row_order is None):
            return None
        seen.add(trade_id)
        if row_order == entry_id:
            seen_entry.add(trade_id)
        if row_order == order_id:
            seen_stop.add(trade_id)
            stop_sum += qty
            if row_side != side:
                return None
        net += qty if row_side == "BUY" else -qty
        if index < len(rows) - 1 and net * direction <= 0:
            return None
    if (net != 0 or seen_entry != entry_trade_ids
            or seen_stop != expected_stop_ids or stop_sum != stop_qty):
        return None
    try:
        realized = Decimal(str(stop["realized_pnl"]))
        commission = Decimal(str(stop["commission"]))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None
    if not realized.is_finite() or not commission.is_finite() or commission < 0:
        return None
    return {
        **stop, "strategy": strategy,
        "proof_kind": "closed_stop_episode_v1",
        "episode_entry_order_id": entry_id,
        "entry_first_fill_time_ms": anchor,
        "net_pnl": str(realized - commission),
    }


async def fetch_verified_closed_stop_episode(client, binding):
    """Fetch all source evidence; reject incomplete order or history pages."""
    if not isinstance(binding, dict):
        return None
    account = binding.get("account_id")
    symbol = binding.get("symbol")
    algo_id = _positive_id(binding.get("algo_id"))
    entry_id = _positive_id(binding.get("episode_entry_order_id"))
    if not account or not symbol or algo_id is None or entry_id is None:
        return None
    stop = await fetch_verified_stop_execution(client, account, symbol, algo_id)
    if stop is None:
        return None
    entry_order = await client.get_order(symbol, entry_id)
    entry_fills = await client.get_user_trades(symbol, order_id=str(entry_id), limit=1000)
    if not isinstance(entry_fills, list) or len(entry_fills) >= 1000 or not entry_fills:
        return None
    try:
        anchor = min(_positive_id(fill.get("time")) or 0 for fill in entry_fills)
    except AttributeError:
        return None
    if anchor <= 0 or anchor >= stop["last_fill_time_ms"]:
        return None
    lot_fills = await fetch_lot_fills(
        client, symbol, anchor, now_ms=stop["last_fill_time_ms"],
    )
    return verified_closed_stop_episode(binding, stop, entry_order, entry_fills, lot_fills)
