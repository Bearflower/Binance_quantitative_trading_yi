"""Durable candidate binding from a newly placed STOP to a known entry episode.

The record is prospective provenance. It does not prove a STOP execution or
authorize a cooldown without a separately verified order and full fill chain.
"""

from decimal import Decimal, InvalidOperation


def _positive_integer(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 and str(number) == str(value) else None


async def record_stop_binding(
    db, *, account_id, strategy, symbol, episode_entry_order_id,
    algo_id, stop_side, stop_quantity,
):
    """Insert once and confirm the stored identity; conflicting evidence fails closed."""
    entry_id = _positive_integer(episode_entry_order_id)
    stop_id = _positive_integer(algo_id)
    try:
        quantity = Decimal(str(stop_quantity))
    except (InvalidOperation, TypeError, ValueError):
        quantity = None
    if (not db or not account_id or not strategy or not symbol or entry_id is None
            or stop_id is None or stop_side not in ("BUY", "SELL")
            or quantity is None or not quantity.is_finite() or quantity <= 0):
        return False
    await db.execute(
        "INSERT INTO trading.aggressive_stop_bindings "
        "(account_id, strategy, symbol, episode_entry_order_id, algo_id, stop_side, stop_quantity) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING",
        account_id, strategy, symbol, entry_id, stop_id, stop_side, quantity,
    )
    row = await db.fetch_one(
        "SELECT strategy, episode_entry_order_id, stop_side, stop_quantity "
        "FROM trading.aggressive_stop_bindings "
        "WHERE account_id=$1 AND symbol=$2 AND algo_id=$3",
        account_id, symbol, stop_id,
    )
    return bool(row and row["strategy"] == strategy
                and int(row["episode_entry_order_id"]) == entry_id
                and row["stop_side"] == stop_side
                and Decimal(str(row["stop_quantity"])) == quantity)
