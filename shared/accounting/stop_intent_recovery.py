"""Resolve prewritten STOP intents against exchange clientAlgoId after restart."""

from decimal import Decimal, InvalidOperation

from shared.accounting.stop_binding import record_stop_binding
from shared.accounting.stop_intent_journal import StopJournalError


async def reconcile_stop_intents(journal, db, client, account_id, strategy, *, limit=5,
                                 on_resolved=None, on_prepost=None):
    if not journal or not db or not client or not account_id or not strategy:
        raise StopJournalError("STOP recovery dependency missing")
    intents = journal.pending()
    if any(row.get("account_id") != account_id or row.get("strategy") != strategy
           for row in intents):
        raise StopJournalError("STOP journal account or strategy mismatch")
    resolved = 0
    # A v2 intent with no POST stage is provably unsent even for legacy lots.
    # Post-started legacy orders still require manual provenance review.
    recoverable = [row for row in intents if not row.get("post_started", True)
                   or row.get("episode_entry_order_id") is not None]
    for row in recoverable[-limit:]:
        if not row.get("post_started", True):
            old_id = row.get("previous_stop_algo_id")
            old_algo = None
            if old_id is not None:
                try:
                    old_algo = await client.get_algo_order(old_id)
                except Exception:
                    continue
                if (not isinstance(old_algo, dict)
                        or old_algo.get("symbol") != row["symbol"]
                        or str(old_algo.get("algoId")) != str(old_id)
                        or old_algo.get("actualOrderId")
                        or old_algo.get("algoStatus") not in
                        ("NEW", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED")):
                    continue
            if on_prepost is not None:
                await on_prepost(row, old_algo)
            journal.reject(row["client_algo_id"], 0)
            resolved += 1
            continue
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
            if on_resolved is not None:
                await on_resolved(row, algo)
            journal.resolve(row["client_algo_id"], algo_id)
            resolved += 1
    return resolved
