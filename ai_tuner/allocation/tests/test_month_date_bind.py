"""Month keys work with both production VARCHAR and new DATE schemas."""

from ai_tuner.allocation.daily_refresher import DailyAllocationRefresher
from ai_tuner.allocation.monthly_job import MonthlyAllocationJob
from ai_tuner.allocation.config_updater import AllocationConfigUpdater
from shared.capital_manager import CapitalManager
from math import inf, nan
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_month_queries_support_text_and_date_columns():
    assert "left(month::text, 7) = $1" in DailyAllocationRefresher._FETCH_ACTIVE_QUERY
    assert "left(month::text, 7) = $3" in DailyAllocationRefresher._UPDATE_QUERY
    assert "left(month::text, 7) = $1" in MonthlyAllocationJob._IDEMPOTENCY_CHECK_QUERY


def test_capital_manager_month_query_supports_text_and_date_columns():
    import inspect

    source = inspect.getsource(CapitalManager._fetch_allocation_from_db)
    assert "left(month::text, 7) = $1" in source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data_type", "expected"),
    [("character varying", "2026-11"), ("date", date(2026, 11, 1))],
)
async def test_month_insert_binds_native_column_type(data_type, expected):
    db = SimpleNamespace(
        fetch_one=AsyncMock(return_value={"data_type": data_type}),
        execute=AsyncMock(return_value="INSERT 0 1"),
    )
    result = SimpleNamespace(
        month="2026-11", total_capital=500, entries=[], is_first_month=False
    )
    assert await AllocationConfigUpdater()._save_to_db(result, db)
    assert db.execute.await_args.args[1] == expected


def test_daily_refresh_rejects_nonfinite_allocation_ratio():
    refresher = object.__new__(DailyAllocationRefresher)

    assert refresher._recalculate_entries(
        [{"strategy_id": "hrs", "allocated_ratio": nan, "allocated_amount": 100}],
        1000,
    ) is None


def test_daily_refresh_rejects_nonfinite_capital():
    refresher = object.__new__(DailyAllocationRefresher)

    assert refresher._recalculate_entries(
        [{"strategy_id": "hrs", "allocated_ratio": 0.2, "allocated_amount": 100}],
        inf,
    ) is None
