"""HRS monthly PnL must follow income event time, not lifecycle summary time."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from ai_tuner.allocation.hrs_monthly_income import (
    HRSIncomeAttributionError,
    HRSMonthlyIncomeCollector,
)
from ai_tuner.allocation.pnl_collector import PnLCollector


BJ = timezone(timedelta(hours=8))


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _income(tran_id, amount, dt):
    return {
        "tranId": tran_id, "symbol": "ABCUSDT", "incomeType": "REALIZED_PNL",
        "income": str(amount), "time": _ms(dt), "tradeId": str(tran_id),
    }


def _trade(row_id, strategy, pnl, dt, order_type="LIMIT"):
    return {
        "id": row_id, "strategy": strategy, "symbol": "ABCUSDT",
        "side": "BUY", "order_type": order_type,
        "executed_at": dt.replace(tzinfo=None), "realized_pnl": Decimal(str(pnl)),
    }


def _collector(incomes, trades, *, page_limit=1000, overrides=None, prior_symbols=None):
    client = type("Client", (), {})()

    async def get_income_history(*, start_time, end_time, income_type, limit):
        return [r for r in incomes if start_time <= r["time"] < end_time]

    client.get_income_history = get_income_history
    db = type("DB", (), {})()
    db.fetch_all = AsyncMock(side_effect=[
        trades, [{"symbol": s} for s in (prior_symbols or [])], overrides or [],
    ])
    return HRSMonthlyIncomeCollector(db, client, page_limit=page_limit)


@pytest.mark.asyncio
async def test_cross_month_summary_does_not_carry_prior_month_pnl():
    sep = datetime(2026, 9, 30, 23, 50, tzinfo=BJ)
    oct_ = datetime(2026, 10, 2, 10, 0, tzinfo=BJ)
    trades = [
        _trade(1, "hrs", 5, sep),
        _trade(2, "hrs", 3, oct_),
        _trade(3, "hrs", 8, oct_, "PNL_SUMMARY"),
    ]
    income = [_income(10, 5, sep), _income(11, 3, oct_)]
    collector = _collector(income, trades)

    result = await collector.collect(
        datetime(2026, 10, 1, tzinfo=BJ), datetime(2026, 11, 1, tzinfo=BJ)
    )

    assert result == Decimal("3")


@pytest.mark.asyncio
async def test_unmatched_income_on_hrs_symbol_stops_allocation():
    oct_ = datetime(2026, 10, 2, 10, 0, tzinfo=BJ)
    collector = _collector([_income(11, 3, oct_)], [_trade(2, "hrs", 4, oct_)])

    with pytest.raises(HRSIncomeAttributionError, match="未能归属"):
        await collector.collect(datetime(2026, 10, 1, tzinfo=BJ), datetime(2026, 11, 1, tzinfo=BJ))


@pytest.mark.asyncio
async def test_shared_symbol_stops_allocation():
    oct_ = datetime(2026, 10, 2, 10, 0, tzinfo=BJ)
    collector = _collector(
        [_income(11, 3, oct_)],
        [_trade(2, "hrs", 3, oct_), _trade(3, "new_coin", 3, oct_)],
    )

    with pytest.raises(HRSIncomeAttributionError, match="共享"):
        await collector.collect(datetime(2026, 10, 1, tzinfo=BJ), datetime(2026, 11, 1, tzinfo=BJ))


@pytest.mark.asyncio
async def test_full_api_page_is_incomplete_without_proof_of_last_page():
    oct_ = datetime(2026, 10, 2, 10, 0, tzinfo=BJ)
    collector = _collector(
        [_income(11, 3, oct_), _income(12, 2, oct_)],
        [_trade(2, "hrs", 3, oct_)], page_limit=2,
    )

    with pytest.raises(HRSIncomeAttributionError, match="满页"):
        await collector.collect(datetime(2026, 10, 1, tzinfo=BJ), datetime(2026, 11, 1, tzinfo=BJ))


@pytest.mark.asyncio
async def test_monthly_allocator_cannot_fall_back_to_summary_when_income_unavailable():
    db = type("DB", (), {})()
    db.fetch_one = AsyncMock(return_value={"total_pnl": Decimal("8")})
    collector = PnLCollector(
        db, [{"strategy_id": "hrs"}],
        binance_client=None, strict_hrs_income=True,
    )

    with pytest.raises(HRSIncomeAttributionError, match="Binance 客户端"):
        await collector.collect_all_realized_pnl(
            datetime(2026, 10, 1, tzinfo=BJ), datetime(2026, 11, 1, tzinfo=BJ)
        )
    db.fetch_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_audited_trade_id_override_can_resolve_missing_summary_detail():
    oct_ = datetime(2026, 10, 22, 10, 0, tzinfo=BJ)
    event = _income(10466172947012, Decimal("0.795"), oct_)
    collector = _collector(
        [event], [_trade(1, "hrs", Decimal("2.9302"), oct_ - timedelta(days=7))],
        overrides=[{
            "tran_id": 10466172947012, "strategy": "hrs", "symbol": "ABCUSDT",
            "income": Decimal("0.795"), "event_time_ms": event["time"],
        }],
    )

    result = await collector.collect(
        datetime(2026, 10, 1, tzinfo=BJ), datetime(2026, 11, 1, tzinfo=BJ)
    )
    assert result == Decimal("0.795")


@pytest.mark.asyncio
async def test_override_with_wrong_amount_is_rejected():
    oct_ = datetime(2026, 10, 22, 10, 0, tzinfo=BJ)
    event = _income(10466172947012, Decimal("0.795"), oct_)
    collector = _collector(
        [event], [_trade(1, "hrs", Decimal("2.9302"), oct_ - timedelta(days=7))],
        overrides=[{
            "tran_id": 10466172947012, "strategy": "hrs", "symbol": "ABCUSDT",
            "income": Decimal("1.000"), "event_time_ms": event["time"],
        }],
    )

    with pytest.raises(HRSIncomeAttributionError, match="未能归属"):
        await collector.collect(datetime(2026, 10, 1, tzinfo=BJ), datetime(2026, 11, 1, tzinfo=BJ))


@pytest.mark.asyncio
async def test_prior_hrs_symbol_taken_by_other_strategy_stops_allocation():
    oct_ = datetime(2026, 10, 22, 10, 0, tzinfo=BJ)
    collector = _collector(
        [_income(12, 3, oct_)], [_trade(2, "new_coin", 3, oct_)],
        prior_symbols=["ABCUSDT"],
    )

    with pytest.raises(HRSIncomeAttributionError, match="历史 HRS 币种"):
        await collector.collect(datetime(2026, 10, 1, tzinfo=BJ), datetime(2026, 11, 1, tzinfo=BJ))


@pytest.mark.asyncio
async def test_empty_income_with_monthly_hrs_pnl_stops_allocation():
    oct_ = datetime(2026, 10, 22, 10, 0, tzinfo=BJ)
    collector = _collector([], [_trade(2, "hrs", 3, oct_)])

    with pytest.raises(HRSIncomeAttributionError, match="income 为空"):
        await collector.collect(datetime(2026, 10, 1, tzinfo=BJ), datetime(2026, 11, 1, tzinfo=BJ))
