from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from strategies.hrs.risk_ledger import (
    CashFlow, IncomeEvent, LedgerNotReady, LedgerObservation, PositionMark,
    initialize_ledger, reconcile_ledger,
)

T = datetime(2026, 11, 1, tzinfo=timezone.utc)


def observation(at=T, **kwargs):
    return LedgerObservation(
        as_of=at, segment_id="hrs-1", incomes=(), cash_flows=(), positions=(),
        income_complete_through=at, inventory_verified_at=at,
        income_complete=True, positions_complete=True, **kwargs,
    )


def seed():
    return initialize_ledger("hrs-1", D("1000"), observation())


def test_signed_attributed_income_fees_funding_and_short_mark():
    at = T + timedelta(minutes=1)
    obs = replace(observation(at), incomes=(
        IncomeEvent("pnl", "realized_pnl", D("20"), at, "hrs-1", True),
        IncomeEvent("fee", "fee", D("-2"), at, "hrs-1", True),
        IncomeEvent("fund", "funding", D("-1"), at, "hrs-1", True),
    ), positions=(PositionMark("p1", "hrs-1", D("-2"), D("100"), D("105"), at, True),))
    result = reconcile_ledger(seed(), obs)
    assert result.equity == D("1007")
    assert result.nav == D("1.007")


def test_reallocation_changes_units_without_return_or_high_watermark():
    state = seed()
    at = T + timedelta(minutes=1)
    result = reconcile_ledger(state, replace(observation(at), cash_flows=(
        CashFlow("allocation", D("1000"), at, "hrs-1"),
    )))
    assert result.equity == D("2000")
    assert result.units == D("2000")
    assert result.nav == D("1")
    at2 = at + timedelta(minutes=1)
    withdrawn = reconcile_ledger(result, replace(observation(at2), cash_flows=(
        CashFlow("withdrawal", D("-1500"), at2, "hrs-1"),
    )))
    assert withdrawn.units == D("500")
    assert withdrawn.nav == D("1")


def test_duplicate_income_is_idempotent_but_conflict_and_late_income_fail_closed():
    at = T + timedelta(minutes=1)
    event = IncomeEvent("pnl", "realized_pnl", D("-30"), at, "hrs-1", True)
    state = reconcile_ledger(seed(), replace(observation(at), incomes=(event, event)))
    assert state.nav == D(".97")
    later = observation(at + timedelta(minutes=1))
    assert reconcile_ledger(state, replace(later, incomes=(event,))).nav == D(".97")
    with pytest.raises(LedgerNotReady, match="conflicting_income"):
        reconcile_ledger(state, replace(later, incomes=(replace(event, amount=D("1")),)))
    with pytest.raises(LedgerNotReady, match="late_income_requires_reconciliation"):
        reconcile_ledger(state, replace(later, incomes=(replace(event, event_id="late"),)))


@pytest.mark.parametrize("field,value,reason", [
    ("income_complete", False, "income_incomplete"),
    ("positions_complete", False, "position_inventory_incomplete"),
    ("income_complete_through", T - timedelta(seconds=1), "income_watermark"),
    ("inventory_verified_at", T - timedelta(minutes=3), "stale_position_inventory"),
    ("segment_id", "other", "segment_mismatch"),
])
def test_missing_stale_or_conflicting_evidence_is_not_zero(field, value, reason):
    with pytest.raises(LedgerNotReady, match=reason):
        reconcile_ledger(seed(), replace(observation(T + timedelta(seconds=1)), **{field: value}))


@pytest.mark.parametrize("amount", [D("NaN"), D("Infinity"), float("nan"), None])
def test_nonfinite_or_nondecimal_cash_cannot_initialize(amount):
    with pytest.raises(LedgerNotReady):
        initialize_ledger("hrs-1", amount, observation())


def test_unverified_mark_and_misattributed_income_rejected():
    at = T + timedelta(seconds=1)
    with pytest.raises(LedgerNotReady, match="unverified_position"):
        reconcile_ledger(seed(), replace(observation(at), positions=(
            PositionMark("p", "hrs-1", D("1"), D("1"), D("1"), at, False),)))
    with pytest.raises(LedgerNotReady, match="income_attribution"):
        reconcile_ledger(seed(), replace(observation(at), incomes=(
            IncomeEvent("x", "funding", D("1"), at, "other", True),)))


def test_mid_interval_cashflow_requires_verified_contemporaneous_nav():
    at = T + timedelta(minutes=2)
    flow = CashFlow("allocation", D("100"), T + timedelta(minutes=1), "hrs-1")
    with pytest.raises(LedgerNotReady, match="cashflow_valuation_missing"):
        reconcile_ledger(seed(), replace(observation(at), cash_flows=(flow,)))
    result = reconcile_ledger(seed(), replace(observation(at), cash_flows=(
        replace(flow, nav_before=D("1"), valuation_evidence="checkpoint-1"),)))
    assert result.nav == D("1")


def test_income_and_deposit_at_same_instant_does_not_dilute_loss():
    at = T + timedelta(minutes=1)
    result = reconcile_ledger(seed(), replace(observation(at), incomes=(
        IncomeEvent("loss", "realized_pnl", D("-60"), at, "hrs-1", True),
    ), cash_flows=(CashFlow("deposit", D("940"), at, "hrs-1"),)))
    assert result.nav == D(".94")
    assert result.units == D("2000")
    assert result.equity == D("1880")


def test_duplicate_cashflow_is_idempotent_and_conflicting_repeat_rejected():
    at = T + timedelta(minutes=1)
    flow = CashFlow("deposit", D("100"), at, "hrs-1")
    state = reconcile_ledger(seed(), replace(observation(at), cash_flows=(flow, flow)))
    repeated = reconcile_ledger(state, replace(observation(at + timedelta(minutes=1)), cash_flows=(flow,)))
    assert repeated.cash_allocation == D("1100")
    assert repeated.nav == D("1")
    with pytest.raises(LedgerNotReady, match="conflicting_cashflow"):
        reconcile_ledger(state, replace(observation(at + timedelta(minutes=1)), cash_flows=(replace(flow, amount=D("200")),)))


@pytest.mark.parametrize("bad_mark,reason", [
    (PositionMark("p", "hrs-1", D("1"), D("1"), D("1"), T - timedelta(minutes=3), True), "stale_position_mark"),
    (PositionMark("p", "hrs-1", D("1"), D("1"), D("1"), T + timedelta(minutes=1), True), "stale_position_mark"),
    (PositionMark("p", "hrs-1", D("1"), D("1"), D("NaN"), T, True), "invalid_mark_price"),
])
def test_bad_mark_cannot_create_zero_unrealized_pnl(bad_mark, reason):
    with pytest.raises(LedgerNotReady, match=reason):
        initialize_ledger("hrs-1", D("1000"), replace(observation(), positions=(bad_mark,)))


def test_zero_equity_full_withdrawal_and_exhausted_capital_cannot_reset_nav():
    at = T + timedelta(minutes=1)
    with pytest.raises(LedgerNotReady, match="nonpositive_equity"):
        reconcile_ledger(seed(), replace(observation(at), cash_flows=(CashFlow("withdraw", D("-1000"), at, "hrs-1"),)))
    with pytest.raises(LedgerNotReady, match="nonpositive_equity"):
        reconcile_ledger(seed(), replace(observation(at), incomes=(IncomeEvent("loss", "realized_pnl", D("-1000"), at, "hrs-1", True),)))
