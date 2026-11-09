"""Resolve prewritten STOP intents against exchange clientAlgoId after restart."""

from decimal import Decimal, InvalidOperation

from shared.accounting.stop_binding import record_stop_binding
from shared.accounting.stop_intent_journal import StopJournalError


async def reconcile_stop_intents(journal, db, client, account_id, strategy, *, limit=5):
    if not journal or not db or not client or not account_id or not strategy:
        raise StopJournalError("STOP recovery dependency missing")
    intents = journal.pending()
    if any(row.get("account_id") != account_id or row.get("strategy") != strategy
           for row in intents):
        raise StopJournalError("STOP journal account or strategy mismatch")
    resolved = 0
    # Legacy unknown episodes remain pending for manual review, but cannot
    # starve later recoverable intents from the bounded exchange lookup batch.
    recoverable = [row for row in intents if row.get("episode_entry_order_id") is not None]
    for row in recoverable[-limit:]:
        try:
            algo = await client.get_algo_order(client_algo_id=row["client_algo_id"])
        except Exception:
            # An unknown write outcome cannot be treated as an absent order.
            continue
        if not isinstance(algo, dict):
            continue
        try:
            algo_id = int(algo["algoId"])
            actual_qty = Decimal(str(algo["quantity"]))
            intended_qty = Decimal(str(row["stop_quantity"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if (algo_id <= 0 or not actual_qty.is_finite() or actual_qty <= 0
                or not intended_qty.is_finite() or actual_qty > intended_qty
                or algo.get("clientAlgoId") != row["client_algo_id"]
                or algo.get("symbol") != row["symbol"]
                or algo.get("side") != row["stop_side"]
                or algo.get("orderType") not in ("STOP", "STOP_MARKET")
                or algo.get("reduceOnly") not in (True, "true")):
            continue
        if await record_stop_binding(
            db, account_id=account_id, strategy=strategy,
            symbol=row["symbol"],
            episode_entry_order_id=row["episode_entry_order_id"],
            algo_id=algo_id, stop_side=row["stop_side"],
            stop_quantity=actual_qty,
        ):
            journal.resolve(row["client_algo_id"], algo_id)
            resolved += 1
    return resolved
