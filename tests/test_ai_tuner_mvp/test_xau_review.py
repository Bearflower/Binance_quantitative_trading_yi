from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest
from tests.test_ai_tuner_mvp.test_xau_shadow_patrol import FakeDB
from ai_tuner.monitor.xau_shadow_patrol import XAUShadowPatrol, OBSERVATION_END


@pytest.mark.asyncio
async def test_observation_queries_are_bounded_and_runs_not_truncated():
    now = datetime(2026, 12, 25, tzinfo=timezone.utc)
    db = FakeDB(now)
    result = await XAUShadowPatrol(db, AsyncMock(), now=lambda: now).run_once()
    runs = [q for q in db.queries if "shadow_runs" in q]
    assert all("LIMIT 30" not in q for q in runs)
    gap_args = [args for q, args in db.calls if "generate_series" in q][0]
    assert gap_args[-1] < OBSERVATION_END
    assert "运行身份未核验" in result["message"]
