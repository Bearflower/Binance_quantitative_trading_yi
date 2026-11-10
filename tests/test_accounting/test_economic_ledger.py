from datetime import datetime, timezone
from decimal import Decimal

import pytest
from unittest.mock import AsyncMock

from ai_tuner.accounting.economic_ledger import build_economic_report, coverage_gaps
from ai_tuner.accounting.report_reader import (
    read_monthly_economic_report, read_hrs_summary_provenance,
)
from scripts.report_economic_ledger import month_window_ms


def _fill(trade_id=11, order_id=101, pnl="5", fee="0.2"):
    return {"account_id": "main", "symbol": "ABCUSDT", "trade_id": trade_id,
            "order_id": order_id, "realized_pnl": Decimal(pnl),
            "commission": Decimal(fee), "commission_asset": "USDT"}


def _income(tran_id, kind, amount, trade_id=None, asset="USDT"):
    return {"account_id": "main", "tran_id": tran_id, "symbol": "ABCUSDT",
            "income_type": kind, "income": Decimal(amount), "trade_id": trade_id,
            "asset": asset, "event_time_utc": datetime(2026, 10, 1, tzinfo=timezone.utc)}


def _owner(strategy="hrs", account_id="main"):
    return {"account_id": account_id, "symbol": "ABCUSDT",
            "order_id": "101", "strategy": strategy}


def test_exact_trade_and_order_attribution_keeps_funding_unassigned():
    report = build_economic_report(
        [_fill()],
        [_income(1, "REALIZED_PNL", "5", 11),
         _income(2, "COMMISSION", "-0.2", 11),
         _income(3, "FUNDING_FEE", "-0.1")],
        [_owner()],
    )
    assert report["account_usdt_total"] == "4.7"
    assert report["by_strategy"]["hrs"]["net_usdt"] == "4.8"
    assert report["unattributed_usdt_total"] == "-0.1"
    assert report["unattributed"][0]["reason"] == "funding_owner_unproven"


def test_shared_order_or_mismatched_amount_fails_closed():
    report = build_economic_report(
        [_fill()], [_income(1, "REALIZED_PNL", "5", 11)],
        [_owner(), _owner("new_coin")],
    )
    assert report["by_strategy"] == {}
    assert report["unattributed"][0]["reason"] == "order_owner_ambiguous"

    report = build_economic_report(
        [_fill()], [_income(1, "REALIZED_PNL", "6", 11)],
        [_owner()],
    )
    assert report["unattributed"][0]["reason"] == "fill_income_amount_mismatch"


def test_conflicting_duplicate_income_is_rejected():
    with pytest.raises(ValueError, match="tran_id"):
        build_economic_report([], [_income(1, "FUNDING_FEE", "1"),
                                   _income(1, "FUNDING_FEE", "2")], [])


def test_same_tran_id_across_income_types_is_not_collapsed():
    report = build_economic_report(
        [_fill()],
        [_income(1, "REALIZED_PNL", "5", 11),
         _income(1, "COMMISSION", "-0.2", 11)],
        [_owner()],
    )
    assert report["income_count"] == 2
    assert report["account_usdt_total"] == "4.8"
    assert report["by_strategy"]["hrs"]["net_usdt"] == "4.8"


def test_reviewed_override_matches_income_type_as_well_as_tran_id():
    pnl = _income(1, "REALIZED_PNL", "5", 11)
    fee = _income(1, "COMMISSION", "-0.2", 11)
    override = {
        "account_id": "main", "tran_id": 1, "income_type": "COMMISSION",
        "symbol": "ABCUSDT", "income": Decimal("-0.2"), "asset": "USDT",
        "event_time_utc": fee["event_time_utc"], "strategy": "hrs",
        "reviewed_by": "operator", "evidence": {"order_id": 101},
    }
    report = build_economic_report([_fill()], [pnl, fee], [], [override])
    assert report["income_count"] == 2
    assert report["by_strategy"]["hrs"]["commission_usdt"] == "-0.2"
    assert report["unattributed"][0]["income_type"] == "REALIZED_PNL"


def test_owner_requires_matching_account_id():
    report = build_economic_report(
        [_fill()], [_income(1, "REALIZED_PNL", "5", 11)],
        [_owner(account_id="other")],
    )
    assert report["by_strategy"] == {}
    assert report["unattributed"][0]["reason"] == "order_owner_missing"
    report = build_economic_report(
        [_fill()], [_income(1, "REALIZED_PNL", "5", 11)],
        [_owner(account_id=None)],
    )
    assert report["unattributed"][0]["reason"] == "order_owner_missing"


def test_reviewed_funding_override_requires_exact_event_identity():
    income = _income(3, "FUNDING_FEE", "-0.1")
    override = {
        "account_id": "main", "tran_id": 3, "strategy": "hrs",
        "symbol": "ABCUSDT", "income_type": "FUNDING_FEE",
        "income": Decimal("-0.1"), "asset": "USDT",
        "event_time_utc": income["event_time_utc"],
        "reviewed_by": "operator", "evidence": {"position_snapshot_id": 42},
    }
    report = build_economic_report([], [income], [], [override])
    assert report["by_strategy"]["hrs"]["funding_usdt"] == "-0.1"
    assert report["unattributed"] == []
    override["income"] = Decimal("-0.2")
    report = build_economic_report([], [income], [], [override])
    assert report["by_strategy"] == {}
    assert report["unattributed"][0]["reason"] == "override_identity_mismatch"
    override["income"] = None
    report = build_economic_report([], [income], [], [override])
    assert report["unattributed"][0]["reason"] == "override_identity_mismatch"


def test_derived_funding_matches_identity_and_manual_conflict_fails_closed():
    income = _income(3, "FUNDING_FEE", "-0.1")
    derived = {**income, "strategy": "hrs", "method": "position_replay",
               "evidence": {"baseline": "flat-1"}, "algorithm_version": "v1"}
    report = build_economic_report([], [income], [], derived_attributions=[derived])
    assert report["by_strategy"]["hrs"]["funding_usdt"] == "-0.1"
    assert report["unattributed"] == []
    bad = {**derived, "income": Decimal("-0.2")}
    report = build_economic_report([], [income], [], derived_attributions=[bad])
    assert report["unattributed"][0]["reason"] == "derived_identity_mismatch"
    manual = {**income, "strategy": "btc_eth", "reviewed_by": "operator",
              "evidence": {"workbook": "reviewed"}}
    report = build_economic_report([], [income], [], [manual], [derived])
    assert report["by_strategy"]["btc_eth"]["funding_usdt"] == "-0.1"
    assert report["attribution_conflicts"][0]["tran_id"] == "3"
    assert report["attribution_conflicts"][0]["manual_strategy"] == "btc_eth"
    assert report["attribution_conflicts"][0]["derived_strategy"] == "hrs"


def test_direct_order_owner_conflict_with_derived_is_visible():
    income = _income(1, "REALIZED_PNL", "5", 11)
    derived = {**income, "strategy": "btc_eth", "method": "exact_order",
               "evidence": {"trade_id": "11"}, "algorithm_version": 1}
    report = build_economic_report([_fill()], [income], [_owner()],
                                   derived_attributions=[derived])
    assert report["attribution_conflicts"][0]["reason"] == "direct_owner_conflict"


def test_non_usdt_income_is_separate_from_usdt_bridge():
    report = build_economic_report(
        [_fill()], [_income(1, "COMMISSION", "-0.2", 11, "BNB")],
        [_owner()],
    )
    assert report["account_usdt_total"] == "0"
    assert report["other_asset_totals"] == {"BNB": "-0.2"}
    assert report["unattributed"][0]["reason"] == "non_usdt_asset"


def test_missing_fill_amount_is_unattributed_not_report_failure():
    fill = _fill()
    fill["realized_pnl"] = None
    report = build_economic_report(
        [fill], [_income(1, "REALIZED_PNL", "5", 11)],
        [_owner()],
    )
    assert report["unattributed"][0]["reason"] == "fill_amount_missing"
    assert report["account_usdt_total"] == report["unattributed_usdt_total"]


def test_coverage_requires_contiguous_complete_windows():
    cursors = [
        {"source": "um/income", "account_id": "main", "income_type": kind,
         "symbol": None, "window_start_ms": 0, "covered_until_ms": 100,
         "page_complete": True, "request_status": "ok"}
        for kind in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")
    ]
    cursors.append({"source": "userTrades", "account_id": "main", "income_type": None,
                    "symbol": "ABCUSDT", "window_start_ms": 0, "covered_until_ms": 100,
                    "page_complete": True, "request_status": "ok"})
    assert coverage_gaps(cursors, "main", 0, 100, {"ABCUSDT"}) == []
    cursors[-1]["page_complete"] = False
    assert coverage_gaps(cursors, "main", 0, 100, {"ABCUSDT"}) == ["fills:ABCUSDT"]


@pytest.mark.asyncio
async def test_reader_reports_missing_cursor_without_writing_database():
    db = type("DB", (), {})()
    db.fetch_all = AsyncMock(side_effect=[
        [_income(1, "REALIZED_PNL", "5", 11)],
        [_fill()],
        [_owner()],
        [],
    ])
    report = await read_monthly_economic_report(db, "main", 0, 100)
    assert report["by_strategy"]["hrs"]["net_usdt"] == "5"
    assert report["id_match_and_coverage_complete"] is False
    assert "income:COMMISSION" in report["coverage_gaps"]
    assert "fills:ABCUSDT" in report["coverage_gaps"]
    assert len(db.fetch_all.await_args_list) == 4
    owner_sql, owner_account, *_ = db.fetch_all.await_args_list[2].args
    assert "account_id = $1" in owner_sql
    assert owner_account == "main"


@pytest.mark.asyncio
async def test_reader_rejects_fill_without_expected_income_even_with_complete_cursors():
    db = type("DB", (), {})()
    fill = _fill()
    fill["trade_time_utc"] = datetime.fromtimestamp(0.05, timezone.utc)
    cursors = [
        {"source": "um/income", "account_id": "main", "income_type": kind,
         "symbol": None, "window_start_ms": 0, "covered_until_ms": 100,
         "page_complete": True, "request_status": "ok"}
        for kind in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE")
    ]
    cursors.append({"source": "userTrades", "account_id": "main", "income_type": None,
                    "symbol": "ABCUSDT", "window_start_ms": 0, "covered_until_ms": 100,
                    "page_complete": True, "request_status": "ok"})
    db.fetch_all = AsyncMock(side_effect=[[], [fill], [_owner()], cursors])
    report = await read_monthly_economic_report(db, "main", 0, 100)
    assert report["coverage_gaps"] == []
    assert report["missing_income_fills"] == [
        {"symbol": "ABCUSDT", "trade_id": "11", "income_type": "COMMISSION"},
        {"symbol": "ABCUSDT", "trade_id": "11", "income_type": "REALIZED_PNL"},
    ]
    assert report["id_match_and_coverage_complete"] is False


@pytest.mark.asyncio
async def test_reader_loads_reviewed_overrides_only_when_explicitly_enabled():
    db = type("DB", (), {})()
    income = _income(3, "FUNDING_FEE", "-0.1")
    override = {
        "account_id": "main", "tran_id": 3, "strategy": "hrs",
        "symbol": "ABCUSDT", "income_type": "FUNDING_FEE",
        "income": Decimal("-0.1"), "asset": "USDT",
        "event_time_utc": income["event_time_utc"],
        "reviewed_by": "operator", "evidence": {"position_snapshot_id": 42},
    }
    db.fetch_all = AsyncMock(side_effect=[[income], [], [], [], [override]])
    report = await read_monthly_economic_report(
        db, "main", 0, 100, include_reviewed_overrides=True,
    )
    assert report["by_strategy"]["hrs"]["funding_usdt"] == "-0.1"
    assert len(db.fetch_all.await_args_list) == 5
    assert "event_time_utc" in db.fetch_all.await_args_list[0].args[0].split("FROM")[0]


@pytest.mark.asyncio
async def test_reader_derived_conflict_blocks_complete_report():
    db = type("DB", (), {})()
    income = _income(3, "FUNDING_FEE", "-0.1")
    manual = {**income, "strategy": "hrs", "reviewed_by": "operator", "evidence": {"id": 1}}
    derived = {**income, "strategy": "btc_eth", "method": "position_replay",
               "algorithm_version": "v1", "evidence": {"id": 2}}
    db.fetch_all = AsyncMock(side_effect=[[income], [], [], [], [manual], [derived]])
    report = await read_monthly_economic_report(
        db, "main", 0, 100, include_reviewed_overrides=True,
        include_derived_attribution=True,
    )
    assert report["attribution_conflicts"]
    assert report["strategy_attribution_complete"] is False
    assert report["id_match_and_coverage_complete"] is False


def test_cli_month_window_is_beijing_calendar_month():
    start, end = month_window_ms("2026-10")
    from datetime import datetime, timezone
    assert datetime.fromtimestamp(start / 1000, timezone.utc).isoformat() == "2026-09-30T16:00:00+00:00"
    assert datetime.fromtimestamp(end / 1000, timezone.utc).isoformat() == "2026-10-31T16:00:00+00:00"
    with pytest.raises(ValueError):
        month_window_ms("2026-13")


@pytest.mark.asyncio
async def test_hrs_summary_provenance_is_account_scoped_and_keeps_legacy_unknown():
    db = type("DB", (), {})()
    db.fetch_all = AsyncMock(return_value=[
        {"pnl_source": "exchange_order_matched", "record_count": 2,
         "gross_pnl": Decimal("3")},
        {"pnl_source": None, "record_count": 1,
         "gross_pnl": Decimal("-1")},
    ])
    report = await read_hrs_summary_provenance(db, "main", *month_window_ms("2026-10"))
    assert report["sources"]["exchange_order_matched"] == {
        "record_count": 2, "gross_pnl_usdt": "3",
    }
    assert report["sources"]["legacy_unknown"]["record_count"] == 1
    sql, account_id, start, end = db.fetch_all.await_args.args
    assert "account_id = $1" in sql
    assert "order_type = 'PNL_SUMMARY'" in sql
    assert account_id == "main"
    assert start.tzinfo is None and start.month == 10 and start.day == 1
    assert end.tzinfo is None and end.month == 11 and end.day == 1
