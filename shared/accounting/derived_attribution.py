"""Derive a candidate owner without changing exchange income or fill facts."""

from __future__ import annotations

from decimal import Decimal

from shared.accounting.position_replay import owner_at


def _decimal(value):
    if value is None:
        return None
    result = Decimal(str(value))
    return result if result.is_finite() else None


def derive_income(income, fills_by_trade_id, order_evidence, replay):
    """Return (insert-only derived row, None) or (None, typed rejection)."""
    asset = str(income.get("asset") or "").upper()
    if asset != "USDT":
        return None, "non_usdt_asset"
    amount = _decimal(income.get("income"))
    if amount is None:
        return None, "invalid_income_amount"
    kind = str(income.get("income_type") or "")
    account_id, symbol = str(income["account_id"]), str(income["symbol"])
    event_time = income["event_time_utc"]
    if kind == "FUNDING_FEE":
        if replay is None:
            return None, "position_unverified"
        strategy, reason = owner_at(replay, event_time)
        if strategy is None:
            return None, reason
        method = "verified_position_interval"
        proof = {"baseline_at": replay.flat_at.isoformat(),
                 "coverage_end": replay.coverage_end.isoformat(),
                 "final_qty": str(replay.final_qty),
                 "fill_times": sorted(stamp.isoformat() for stamp in replay.fill_times)}
    elif kind in {"REALIZED_PNL", "COMMISSION"}:
        trade_id = income.get("trade_id")
        if trade_id is None:
            return None, "missing_trade_id"
        fill = fills_by_trade_id.get(str(trade_id)) or fills_by_trade_id.get(trade_id)
        if not fill or str(fill.get("account_id")) != account_id or str(fill.get("symbol")) != symbol:
            return None, "fill_not_found"
        order_id = fill.get("order_id")
        owner = order_evidence.get(str(order_id)) if order_id is not None else None
        if not owner or str(owner.get("account_id")) != account_id or str(owner.get("symbol")) != symbol or str(owner.get("order_id")) != str(order_id) or not owner.get("strategy"):
            return None, "order_owner_missing"
        expected = _decimal(fill.get("realized_pnl") if kind == "REALIZED_PNL" else fill.get("commission"))
        if expected is None:
            return None, "fill_amount_missing"
        if kind == "COMMISSION":
            if fill.get("commission_asset") != "USDT":
                return None, "fill_commission_non_usdt"
            expected = -expected
        if amount != expected:
            return None, "fill_income_amount_mismatch"
        strategy, method = str(owner["strategy"]), "exact_order"
        proof = {"trade_id": str(trade_id), "order_id": str(order_id),
                 "order_evidence": owner.get("evidence")}
    else:
        return None, "unsupported_income_type"
    row = {field: income[field] for field in
           ("account_id", "income_type", "tran_id", "symbol", "income", "asset", "event_time_utc")}
    row.update(strategy=strategy, method=method, evidence=proof, algorithm_version=1)
    return row, None
