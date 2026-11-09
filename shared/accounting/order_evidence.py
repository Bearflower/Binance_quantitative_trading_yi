"""Resolve exact exchange order ownership without symbol or time guessing."""

from __future__ import annotations


def _same(value, expected):
    return value is not None and str(value) == str(expected)


def resolve_order_owner(account_id, symbol, order_id, trade_records, algo_links,
                        condition_orders):
    """Return ``(strategy, evidence)`` or ``(None, rejection_reason)``.

    Every source must carry the exact account, symbol and exchange order identity.
    This excludes historical null-account records from automatic attribution.
    """
    if not account_id or not symbol or not order_id:
        return None, "order_identity_missing"

    direct = [row for row in trade_records
              if _same(row.get("account_id"), account_id)
              and _same(row.get("symbol"), symbol)
              and _same(row.get("order_id"), order_id)
              and row.get("strategy")]
    direct_owners = {str(row["strategy"]) for row in direct}
    if len(direct_owners) > 1:
        return None, "order_owner_ambiguous"

    links = [row for row in algo_links
             if _same(row.get("account_id"), account_id)
             and _same(row.get("symbol"), symbol)
             and _same(row.get("actual_order_id"), order_id)]
    algo_ids = {str(row["algo_id"]) for row in links if row.get("algo_id") is not None}
    if len(algo_ids) > 1:
        return None, "algo_link_ambiguous"

    algo_owner = None
    algo_evidence = None
    if algo_ids:
        algo_id = next(iter(algo_ids))
        conditions = [row for row in condition_orders
                      if _same(row.get("account_id"), account_id)
                      and _same(row.get("symbol"), symbol)
                      and _same(row.get("algo_id"), algo_id)
                      and row.get("strategy_name")]
        condition_owners = {str(row["strategy_name"]) for row in conditions}
        if not condition_owners:
            return None, "condition_owner_missing"
        if len(condition_owners) > 1:
            return None, "condition_owner_ambiguous"
        algo_owner = next(iter(condition_owners))
        digests = {str(row.get("payload_sha256")) for row in links}
        if len(digests) != 1 or None in digests or "None" in digests:
            return None, "algo_payload_ambiguous"
        algo_evidence = {"source_kind": "pm_algo_link", "algo_id": algo_id,
                         "payload_sha256": next(iter(digests)),
                         "actual_order_id": str(order_id)}

    direct_owner = next(iter(direct_owners)) if direct_owners else None
    if direct_owner and algo_owner and direct_owner != algo_owner:
        return None, "order_owner_conflict"
    if algo_owner:
        if direct:
            algo_evidence["trade_record_ids"] = sorted(str(row["id"]) for row in direct)
        return algo_owner, algo_evidence
    if direct_owner:
        return direct_owner, {"source_kind": "trade_record",
                              "source_ids": sorted(str(row["id"]) for row in direct),
                              "order_id": str(order_id)}
    return None, "order_owner_missing"
