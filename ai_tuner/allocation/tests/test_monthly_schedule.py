"""月度调度须在自然月结束后执行，并允许短期失败重试。"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ai_tuner.main import StratTuneAI

CST = timezone(timedelta(hours=8))


@pytest.mark.parametrize("day,expected_calls", [(31, 0), (1, 1), (2, 1), (3, 1), (4, 0)])
def test_scheduled_allocation_only_after_month_close(day, expected_calls):
    month = 10 if day == 31 else 11
    frozen = datetime(2026, month, day, 9, 30, tzinfo=CST)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

    monthly_job = SimpleNamespace(run_monthly_allocation=AsyncMock())
    app = SimpleNamespace(monthly_job=monthly_job)
    with patch("ai_tuner.main.datetime", FrozenDateTime):
        asyncio.run(StratTuneAI._scheduled_allocation(app))

    assert monthly_job.run_monthly_allocation.await_count == expected_calls
