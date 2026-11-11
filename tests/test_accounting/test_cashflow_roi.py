from shared.accounting.cashflow_roi import evaluate_account_roi
from dashboard.backend.models.schemas import ReturnsData


def evidence():
    return dict(
        start_equity_usd="100", end_equity_usd="165",
        cashflows=[{"source": "deposit", "event_id": "d1",
                    "exchange_time_ms": 1000, "boundary": "external_in",
                    "value_usd": "50", "valuation_id": "v1",
                    "pre_equity_usd": "110", "post_equity_usd": "160"}],
        income_net_usd="15", unrealized_change_usd="0",
        bridge_tolerance_usd="0.01", coverage_complete=True,
        start_watermark="w1", end_watermark="w2",
    )


def test_complete_cashflow_and_equity_bridge_produces_time_weighted_return():
    result = evaluate_account_roi(**evidence())
    assert result["status"] == "VERIFIED"
    assert result["yield"] == "0.134375"
    assert result["bridge_residual_usd"] == "0"


def test_missing_event_time_valuation_stays_unknown():
    data = evidence()
    data["cashflows"][0]["pre_equity_usd"] = None
    result = evaluate_account_roi(**data)
    assert result["yield"] is None
    assert "event_time_equity_unverified" in result["unavailable_reasons"]


def test_missing_coverage_or_duplicate_event_stays_unknown():
    data = evidence()
    data["coverage_complete"] = False
    data["cashflows"].append(dict(data["cashflows"][0]))
    result = evaluate_account_roi(**data)
    assert result["yield"] is None
    assert "cashflow_coverage_unverified" in result["unavailable_reasons"]
    assert "cashflow_identity_unverified" in result["unavailable_reasons"]


def test_equity_bridge_mismatch_does_not_publish_roi():
    data = evidence()
    data["income_net_usd"] = "5"
    result = evaluate_account_roi(**data)
    assert result["yield"] is None
    assert "account_equity_bridge_mismatch" in result["unavailable_reasons"]


def test_dashboard_keeps_unadjusted_change_separate_from_unknown_roi():
    data = ReturnsData(period="daily", equity="100", snapshot_date="2026-11-11",
                       **{"yield": -5.0})
    output = data.model_dump(by_alias=True)
    assert output["yield"] == -5.0
    assert output["cashflow_adjusted"] is False
    assert output["investment_return_yield"] is None
    assert output["investment_return_unavailable"] is True


def test_malformed_cashflow_identity_returns_unknown():
    data = evidence()
    data["cashflows"][0]["source"] = ["deposit"]
    result = evaluate_account_roi(**data)
    assert result["yield"] is None
    assert "cashflow_identity_unverified" in result["unavailable_reasons"]
