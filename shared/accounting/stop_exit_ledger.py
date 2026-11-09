"""Immutable, transactional storage for a fully verified closed STOP episode."""

from decimal import Decimal, InvalidOperation


class StopExitConflict(ValueError):
    """An exchange order or trade ID was already assigned to another episode."""


def _integer(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 and str(number) == str(value) else None


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _normalize(event):
    if not isinstance(event, dict) or event.get("proof_kind") != "closed_stop_episode_v1":
        return None
    identity = (
        event.get("account_id"), event.get("strategy"), event.get("symbol"),
        _integer(event.get("episode_entry_order_id")), _integer(event.get("algo_id")),
        _integer(event.get("order_id")), event.get("side"),
    )
    account, strategy, symbol, entry_id, algo_id, order_id, side = identity
    quantity = _decimal(event.get("quantity"))
    realized = _decimal(event.get("realized_pnl"))
    commission = _decimal(event.get("commission"))
    net = _decimal(event.get("net_pnl"))
    anchor = _integer(event.get("entry_first_fill_time_ms"))
    stop_time = _integer(event.get("last_fill_time_ms"))
    raw_ids = event.get("trade_ids")
    if (not account or not strategy or not symbol or entry_id is None or algo_id is None
            or order_id is None or entry_id == order_id or side not in ("BUY", "SELL")
            or quantity is None or quantity <= 0 or realized is None
            or commission is None or commission < 0 or net != realized - commission
            or anchor is None or stop_time is None or stop_time <= anchor
            or event.get("commission_asset") != "USDT"
            or not isinstance(raw_ids, list) or not raw_ids):
        return None
    trade_ids = [_integer(value) for value in raw_ids]
    if None in trade_ids or len(set(trade_ids)) != len(trade_ids):
        return None
    return identity + (quantity, realized, commission, net, anchor, stop_time, trade_ids)


async def persist_verified_stop_exit(db, event):
    """Return True on first insert or exact replay; roll back conflicting trade links."""
    values = _normalize(event)
    if values is None or db is None:
        return False
    (account, strategy, symbol, entry_id, algo_id, order_id, side,
     quantity, realized, commission, net, anchor, stop_time, trade_ids) = values
    if getattr(db, "pool", None) is None:
        await db.connect()
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO trading.aggressive_stop_exits "
                "(account_id,strategy,symbol,episode_entry_order_id,algo_id,order_id,"
                "stop_side,quantity,realized_pnl,commission,net_pnl,"
                "entry_first_fill_time_ms,last_fill_time_ms) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) "
                "ON CONFLICT DO NOTHING",
                account, strategy, symbol, entry_id, algo_id, order_id, side,
                quantity, realized, commission, net, anchor, stop_time,
            )
            row = await conn.fetchrow(
                "SELECT strategy,episode_entry_order_id,order_id,stop_side,quantity,"
                "realized_pnl,commission,net_pnl,entry_first_fill_time_ms,last_fill_time_ms "
                "FROM trading.aggressive_stop_exits "
                "WHERE account_id=$1 AND symbol=$2 AND algo_id=$3",
                account, symbol, algo_id,
            )
            if row is None or (
                row["strategy"], row["episode_entry_order_id"], row["order_id"],
                row["stop_side"], _decimal(row["quantity"]),
                _decimal(row["realized_pnl"]), _decimal(row["commission"]),
                _decimal(row["net_pnl"]), row["entry_first_fill_time_ms"],
                row["last_fill_time_ms"],
            ) != (strategy, entry_id, order_id, side, quantity, realized,
                  commission, net, anchor, stop_time):
                raise StopExitConflict("STOP exit identity conflicts with persisted event")
            for trade_id in trade_ids:
                await conn.execute(
                    "INSERT INTO trading.aggressive_stop_exit_trades "
                    "(account_id,symbol,trade_id,algo_id) VALUES ($1,$2,$3,$4) "
                    "ON CONFLICT DO NOTHING",
                    account, symbol, trade_id, algo_id,
                )
                owner = await conn.fetchval(
                    "SELECT algo_id FROM trading.aggressive_stop_exit_trades "
                    "WHERE account_id=$1 AND symbol=$2 AND trade_id=$3",
                    account, symbol, trade_id,
                )
                if owner != algo_id:
                    raise StopExitConflict("trade ID assigned to another STOP exit")
            stored = await conn.fetch(
                "SELECT trade_id FROM trading.aggressive_stop_exit_trades "
                "WHERE account_id=$1 AND symbol=$2 AND algo_id=$3",
                account, symbol, algo_id,
            )
            if {row["trade_id"] for row in stored} != set(trade_ids):
                raise StopExitConflict("STOP exit trade set differs from persisted event")
    return True


async def latest_verified_stop_time_ms(db, account_id, strategy, symbol):
    """Read only persisted closed STOP episodes for the exact account/strategy."""
    if not db or not account_id or not strategy or not symbol:
        return None
    row = await db.fetch_one(
        "SELECT MAX(last_fill_time_ms) AS last_fill_time_ms "
        "FROM trading.aggressive_stop_exits "
        "WHERE account_id=$1 AND strategy=$2 AND symbol=$3",
        account_id, strategy, symbol,
    )
    return _integer(row.get("last_fill_time_ms")) if row else None
