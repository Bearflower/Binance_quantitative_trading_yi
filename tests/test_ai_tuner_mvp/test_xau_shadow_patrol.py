from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

import pytest

from ai_tuner.monitor.xau_shadow_patrol import XAUShadowPatrol


UTC = timezone.utc


class FakeDB:
    def __init__(self, now):
        self.now = now
        self.queries = []

    async def fetch_all(self, query, *args):
        self.queries.append(query)
        if "shadow_decision_events" in query:
            return [
                dict(profile_id=profile, run_id="run-1", scheduled_at=self.now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1),
                     data_quality_reason=None, decision="no_signal", feature_snapshot=json.dumps({"atr_filter": {"history_count": 100, "final_threshold": 0.01}}))
                for profile in ("xau_calibrated", "xau_original", "xau_aggressive")
            ]
        if "shadow_runs" in query:
            return [dict(run_id="run-1", instance_id="mtcps_xau_calibrated_shadow", status="active", started_at=self.now - timedelta(days=1), ended_at=None)]
        return []

    async def fetch_one(self, query, *args):
        self.queries.append(query)
        if "registered_symbols" in query:
            return dict(expires_at=(self.now + timedelta(days=6)).replace(tzinfo=None), status="active", intervals=["1h", "4h", "1d"])
        if "kline_xauusdt_" in query:
            return {"n": 100}
        return {"n": 0}


@pytest.mark.asyncio
async def test_patrol_reads_only_and_sends_full_report():
    now = datetime(2026, 11, 13, 6, 35, tzinfo=UTC)
    db = FakeDB(now)
    notifier = AsyncMock()
    notifier.send.return_value = True
    patrol = XAUShadowPatrol(db, notifier, now=lambda: now)

    result = await patrol.run_once()

    assert result["ok"] is True
    assert result["sent"] is True
    assert "xau_calibrated" in result["message"]
    assert "续期" in result["message"]
    assert "100" in result["message"]
    notifier.send.assert_awaited_once()
    assert notifier.send.call_args.kwargs["project"] == "xau_monitor"
    assert all(q.lstrip().upper().startswith("SELECT") for q in db.queries)


@pytest.mark.asyncio
async def test_missing_event_or_quality_degradation_is_reported():
    now = datetime(2026, 11, 13, 6, 35, tzinfo=UTC)
    db = FakeDB(now)
    original = db.fetch_all

    async def degraded(query, *args):
        rows = await original(query, *args)
        if "shadow_decision_events" in query:
            rows = [r for r in rows if r["profile_id"] != "xau_calibrated"]
            rows[0]["data_quality_reason"] = "missing_4h"
        return rows

    db.fetch_all = degraded
    notifier = AsyncMock()
    notifier.send.return_value = True
    result = await XAUShadowPatrol(db, notifier, now=lambda: now).run_once()
    assert result["ok"] is False
    assert "缺失" in result["message"]
    assert "missing_4h" in result["message"]
