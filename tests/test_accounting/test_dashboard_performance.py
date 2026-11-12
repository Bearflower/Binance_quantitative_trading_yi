from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from shared.accounting.dashboard_performance import realized_income_metrics, verified_lifecycle_stats


def _event(day, amount, owner="hrs"):
    return {"asset": "USDT", "amount": str(amount), "strategy": owner,
            "event_time_utc": datetime(2026, 11, day, 4, tzinfo=timezone.utc).isoformat()}


def _report(events, *, complete=True, attributed=True, end_ms=1793721600000):
    return {"start_ms": 1793462400000, "end_ms": end_ms,
            "publication_status": "published", "account_coverage_complete": complete,
            "strategy_attribution_complete": attributed, "events": events}


def test_realized_metrics_use_exchange_income_and_opening_capital():
    report = _report([_event(1, "10"), _event(2, "-20"), _event(3, "5")])
    capital = {date(2026, 11, day): Decimal("100") for day in (1, 2, 3)}
    result = realized_income_metrics(report, capital, {"hrs": capital}, ["hrs"])
    assert result["account"]["sample_count"] == 3
    assert result["account"]["max_drawdown"] == pytest.approx(0.2 / 1.1)
    assert result["account"]["sharpe"] is not None
    assert result["strategies"]["hrs"]["max_drawdown"] == result["account"]["max_drawdown"]


def test_missing_capital_and_incomplete_ledger_do_not_invent_metrics():
    capital = {date(2026, 11, 1): Decimal("100")}
    result = realized_income_metrics(_report([_event(1, "10")]), capital, {"hrs": capital}, ["hrs"])
    assert result["account"]["max_drawdown"] is None
    assert result["account"]["reason"] == "opening_capital_missing"
    result = realized_income_metrics(_report([_event(1, "10")], complete=False), capital,
                                     {"hrs": capital}, ["hrs"])
    assert result["account"]["reason"] == "income_coverage_incomplete"


def test_unattributed_income_blocks_strategy_metrics_only():
    capital = {date(2026, 11, day): Decimal("100") for day in (1, 2, 3)}
    result = realized_income_metrics(_report([_event(1, "10")], attributed=False),
                                     capital, {"hrs": capital}, ["hrs"])
    assert result["account"]["max_drawdown"] is not None
    assert result["strategies"]["hrs"]["reason"] == "strategy_attribution_incomplete"


def test_constant_returns_have_drawdown_but_undefined_sharpe():
    capital = {date(2026, 11, day): Decimal("100") for day in (1, 2, 3)}
    result = realized_income_metrics(_report([]), capital, {"hrs": capital}, ["hrs"])
    assert result["account"]["max_drawdown"] == 0
    assert result["account"]["sharpe"] is None
    assert result["account"]["reason"] == "zero_return_variance"


def test_verified_lifecycles_count_positions_not_partial_closing_fills():
    base = datetime(2026, 11, 9, 23, 35, tzinfo=timezone.utc)
    baselines = [{"account_id": "default", "symbol": symbol, "flat_at": base,
                  "exchange_qty": Decimal(0)} for symbol in ("ETHUSDT", "SOLUSDT")]
    fills = []
    owners = []
    incomes = []
    for symbol, rows in (
        ("ETHUSDT", [(1, "BUY", "0.012", "0", "0.005"),
                      (2, "SELL", "0.012", "-0.52716", "0.005")]),
        ("SOLUSDT", [(3, "BUY", "0.3", "0", "0.005"),
                      (4, "SELL", "0.08", "-0.1824", "0.001"),
                      (5, "SELL", "0.22", "-0.5016", "0.004")]),
    ):
        for tid, side, qty, gross, fee in rows:
            stamp = datetime(2026, 11, 10, 3 + tid, tzinfo=timezone.utc)
            order_id = 4 if tid == 5 else tid
            fills.append({"account_id": "default", "symbol": symbol, "trade_id": tid,
                          "order_id": order_id, "trade_time_utc": stamp, "side": side,
                          "qty": qty, "realized_pnl": gross, "commission": fee,
                          "commission_asset": "USDT"})
            owners.append({"account_id": "default", "symbol": symbol,
                           "order_id": order_id, "strategy": "btc_eth_aggressive"})
            incomes.append({"symbol": symbol, "trade_id": tid, "tran_id": tid * 10 + 1,
                            "income_type": "COMMISSION", "income": str(-Decimal(fee))})
            if Decimal(gross):
                incomes.append({"symbol": symbol, "trade_id": tid, "tran_id": tid * 10 + 2,
                                "income_type": "REALIZED_PNL", "income": gross})
    report_events = [{"asset": "USDT", "symbol": row["symbol"],
                      "tran_id": row["tran_id"], "income_type": row["income_type"],
                      "amount": row["income"], "strategy": "btc_eth_aggressive"}
                     for row in incomes]
    end_ms = int(datetime(2026, 11, 12, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
    result = verified_lifecycle_stats(_report(report_events, end_ms=end_ms), baselines, fills, owners, incomes,
                                      {"ETHUSDT", "SOLUSDT"}, ["btc_eth_aggressive", "hrs"])
    assert result["account"]["closed_count"] == 2
    assert result["account"]["order_count"] == 4
    assert result["account"]["fill_count"] == 5
    assert result["account"]["win_count"] == 0
    assert result["account"]["win_rate"] == 0
    assert result["account"]["sample_partial"] is True
    assert result["strategies"]["btc_eth_aggressive"]["closed_count"] == 2
    assert result["strategies"]["hrs"]["reason"] == "no_verified_closures"
    report_events[0]["strategy"] = "new_coin"
    conflicted = verified_lifecycle_stats(_report(report_events, end_ms=end_ms),
                                          baselines, fills, owners, incomes,
                                          {"ETHUSDT", "SOLUSDT"}, ["btc_eth_aggressive"])
    assert conflicted["account"]["closed_count"] == 1


def test_lifecycle_rejects_income_mismatch_and_missing_fill_coverage():
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    baselines = [{"account_id": "default", "symbol": "ETHUSDT", "flat_at": base,
                  "exchange_qty": Decimal(0)}]
    fills = [{"account_id": "default", "symbol": "ETHUSDT", "trade_id": tid,
              "order_id": tid, "trade_time_utc": datetime(2026, 11, 2, tid,
                                                          tzinfo=timezone.utc),
              "side": side, "qty": "1", "realized_pnl": gross,
              "commission": "0.01", "commission_asset": "USDT"}
             for tid, side, gross in ((1, "BUY", "0"), (2, "SELL", "2"))]
    owners = [{"account_id": "default", "symbol": "ETHUSDT", "order_id": tid,
               "strategy": "hrs"} for tid in (1, 2)]
    wrong_income = [{"symbol": "ETHUSDT", "trade_id": tid,
                     "income_type": "COMMISSION", "income": "-0.02"}
                    for tid in (1, 2)]
    wrong_income.append({"symbol": "ETHUSDT", "trade_id": 2,
                         "income_type": "REALIZED_PNL", "income": "2"})
    assert verified_lifecycle_stats(_report([]), baselines, fills, owners, wrong_income,
                                    {"ETHUSDT"}, ["hrs"])["account"]["reason"] == "no_verified_closures"
    assert verified_lifecycle_stats(_report([]), baselines, fills, owners, wrong_income,
                                    set(), ["hrs"])["account"]["reason"] == "fill_coverage_incomplete"
