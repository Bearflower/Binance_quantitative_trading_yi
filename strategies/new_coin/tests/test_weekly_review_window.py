"""周报使用上一完整北京自然周，与 trade_records 的 naive 北京时间一致。"""

from datetime import datetime as RealDateTime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from strategies.new_coin.strategy import NewCoinStrategy


@pytest.mark.asyncio
async def test_weekly_review_queries_previous_beijing_week_with_upper_bound():
    strategy = NewCoinStrategy.__new__(NewCoinStrategy)
    strategy.my_record_name = "new_coin"
    strategy.db = type("DB", (), {})()
    strategy.db.fetch_all = AsyncMock(return_value=[{"realized_pnl": 1.0}])
    strategy.notification_client = type("Notifications", (), {})()
    strategy.notification_client.send = AsyncMock()

    class FixedDateTime(RealDateTime):
        @classmethod
        def now(cls, tz=None):
            return RealDateTime(2026, 11, 2, 0, 5, tzinfo=timezone.utc).astimezone(tz)

    with patch("strategies.new_coin.strategy.datetime", FixedDateTime):
        await strategy._weekly_review()

    query, name, start, end = strategy.db.fetch_all.await_args.args
    assert name == "new_coin"
    assert "executed_at >= $2" in query
    assert "executed_at < $3" in query
    assert start == RealDateTime(2026, 10, 26, 0, 0)
    assert end == RealDateTime(2026, 11, 2, 0, 0)
    assert start.tzinfo is end.tzinfo is None
