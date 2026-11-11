from ai_tuner.allocation.shadow_allocation import preview_monthly_allocation
from shared.accounting.published_report import REPORT_VERSION


def inputs():
    return dict(
        report={"account_id": "default", "start_ms": 1, "end_ms": 2,
                "requested_end_ms": 2, "window_key": "monthly:1",
                "refresh_error": None,
                "publication_status": "published", "account_coverage_complete": True,
                "strategy_attribution_complete": True, "report_id": "r1",
                "report_version": REPORT_VERSION,
                "by_strategy": {"hrs": {"net_usdt": "-2"},
                                "btc_eth_aggressive": {"net_usdt": "1"}}},
        capital={"hrs": {"average_capital_usdt": "20", "coverage_complete": True,
                         "account_id": "default", "start_ms": 1, "end_ms": 2,
                         "source_id": "c1"},
                 "btc_eth_aggressive": {"average_capital_usdt": "30",
                                        "coverage_complete": True, "source_id": "c2",
                                        "account_id": "default", "start_ms": 1,
                                        "end_ms": 2}},
        activity={"hrs": {"owned_entry_fills": 2, "coverage_complete": True,
                          "source_id": "a1", "account_id": "default",
                          "start_ms": 1, "end_ms": 2},
                  "btc_eth_aggressive": {"owned_entry_fills": 1,
                                         "coverage_complete": True, "source_id": "a2",
                                         "account_id": "default", "start_ms": 1,
                                         "end_ms": 2}},
        account_id="default", start_ms=1, end_ms=2,
        strategy_ids=("hrs", "btc_eth_aggressive"), total_capital="100",
        reserve_ratio="0.2", rank_ratios=["0.4", "0.4"],
        effective_month="2026-12",
    )


def test_preview_never_writes_or_activates_allocation():
    result = preview_monthly_allocation(**inputs())
    assert result["status"] == "PREVIEW"
    assert result["mode"] == "shadow_only"
    assert result["allocation"]["entries"][0]["strategy_id"] == "btc_eth_aggressive"
    assert result["allocation"]["entries"][1]["strategy_id"] == "hrs"


def test_missing_capital_or_activity_blocks_all_rankings():
    data = inputs()
    del data["capital"]["hrs"]
    data["activity"]["btc_eth_aggressive"]["coverage_complete"] = False
    result = preview_monthly_allocation(**data)
    assert result["status"] == "BLOCKED_DATA"
    assert result["allocation"] is None
    assert "hrs:capital_unverified" in result["reasons"]
    assert "btc_eth_aggressive:activity_unverified" in result["reasons"]


def test_incomplete_income_or_excess_ratios_block():
    data = inputs()
    data["report"]["strategy_attribution_complete"] = False
    data["rank_ratios"] = ["0.6", "0.4"]
    result = preview_monthly_allocation(**data)
    assert result["allocation"] is None
    assert "income_report_unverified" in result["reasons"]
    assert "allocation_ratios_invalid" in result["reasons"]


def test_malformed_or_nonfinite_inputs_block_without_exception():
    data = inputs()
    data["report"]["by_strategy"] = []
    data["capital"]["hrs"]["average_capital_usdt"] = "1e10000"
    data["rank_ratios"] = None
    result = preview_monthly_allocation(**data)
    assert result["status"] == "BLOCKED_DATA"
    assert result["allocation"] is None
