"""Bounded audit of prospective STOP bindings before new entries are considered."""

from shared.accounting.stop_episode_evidence import fetch_verified_closed_stop_episode
from shared.accounting.stop_exit_ledger import persist_verified_stop_exit


async def audit_due_stop_bindings(db, client, account_id, strategy, *, limit=5):
    """Process a small due batch; unresolved evidence remains an entry block."""
    if not db or not client or not account_id or not strategy or not 1 <= limit <= 20:
        raise ValueError("STOP audit identity or limit invalid")
    rows = await db.fetch_all(
        "SELECT account_id,strategy,symbol,episode_entry_order_id,algo_id,"
        "stop_side,stop_quantity,last_observed_status,terminal_observations "
        "FROM trading.aggressive_stop_bindings "
        "WHERE account_id=$1 AND strategy=$2 AND audit_state='pending' "
        "AND next_audit_at<=now() ORDER BY bound_at DESC LIMIT $3",
        account_id, strategy, limit,
    )
    results = {"verified": 0, "no_fill": 0, "review": 0, "pending": 0}
    for raw in rows:
        binding = dict(raw)
        symbol = binding["symbol"]
        algo_id = binding["algo_id"]
        status = "pending"
        observations = int(binding.get("terminal_observations") or 0)
        observed = binding.get("last_observed_status")
        error = None
        try:
            proof = await fetch_verified_closed_stop_episode(client, binding)
            if proof is not None:
                if await persist_verified_stop_exit(db, proof):
                    status = "verified"
                else:
                    error = "closed episode rejected by exit ledger"
            else:
                algo = await client.get_algo_order(algo_id)
                if not isinstance(algo, dict) or algo.get("symbol") != symbol or int(algo.get("algoId", 0)) != algo_id:
                    error = "algo identity unavailable"
                else:
                    observed_now = algo.get("algoStatus")
                    actual = algo.get("actualOrderId")
                    if observed_now in ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED") and not actual:
                        observations = observations + 1 if observed == observed_now else 1
                        observed = observed_now
                        if observations >= 2:
                            status = "no_fill"
                    elif observed_now in ("TRIGGERED", "FINISHED") or actual:
                        observations = observations + 1 if observed == observed_now else 1
                        observed = observed_now
                        if observations >= 2:
                            status = "review"
                            error = "triggered STOP lacks complete closed-episode proof"
                    else:
                        observed = observed_now
                        observations = 0
        except Exception as exc:
            error = str(exc)[:300]
        await db.execute(
            "UPDATE trading.aggressive_stop_bindings SET audit_state=$4, "
            "last_audit_at=now(), next_audit_at=now()+interval '5 minutes', "
            "last_observed_status=$5, terminal_observations=$6, last_audit_error=$7 "
            "WHERE account_id=$1 AND symbol=$2 AND algo_id=$3 AND strategy=$8 "
            "AND audit_state='pending'",
            account_id, symbol, algo_id, status, observed, observations, error, strategy,
        )
        results[status] += 1
    return results


async def unresolved_stop_binding_exists(db, account_id, strategy, symbol):
    """Any pending or review STOP for a flat symbol blocks a fresh entry."""
    if not db or not account_id or not strategy or not symbol:
        raise ValueError("STOP gate identity missing")
    row = await db.fetch_one(
        "SELECT EXISTS(SELECT 1 FROM trading.aggressive_stop_bindings "
        "WHERE account_id=$1 AND strategy=$2 AND symbol=$3 "
        "AND audit_state IN ('pending','review')) AS unresolved",
        account_id, strategy, symbol,
    )
    if row is None or not isinstance(row.get("unresolved"), bool):
        raise ValueError("STOP gate read unavailable")
    return row["unresolved"]
