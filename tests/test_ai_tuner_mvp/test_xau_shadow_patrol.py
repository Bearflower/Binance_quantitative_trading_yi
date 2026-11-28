from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from ai_tuner.monitor.xau_shadow_patrol import XAUShadowPatrol, daily_patrol_trigger


UTC = timezone.utc


class FakeDB:
    def __init__(self, now):
        self.now = now
        self.queries = []
        self.calls = []

    async def fetch_all(self, query, *args):
        self.queries.append(query)
        if "shadow_decision_events" in query:
            return [
                dict(profile_id=profile, run_id="run-1", scheduled_at=self.now.replace(minute=0, second=0, microsecond=0),
                     data_quality_reason=None, decision="no_signal", feature_snapshot=json.dumps({"atr_filter": {"history_count": 100, "final_threshold": 0.01}}))
                for profile in ("xau_calibrated", "xau_original", "xau_aggressive")
            ]
        if "shadow_runs" in query:
            return [dict(run_id="run-1", instance_id="mtcps_xau_calibrated_shadow", status="active", started_at=self.now - timedelta(days=1), ended_at=None)]
        return []

    async def fetch_one(self, query, *args):
        self.queries.append(query)
        self.calls.append((query, args))
        if "generate_series" in query:
            return {"n": 6, "recent_gaps": 0}
        if "registered_symbols" in query:
            return dict(expires_at=(self.now + timedelta(days=6)).replace(tzinfo=None), status="active", intervals=["1h", "4h", "1d"])
        if "kline_xauusdt_" in query:
            return {"n": 100, "latest_open_time": self.now}
        return {"n": 0}


@pytest.mark.asyncio
async def test_patrol_reads_only_and_sends_full_report():
    now = datetime(2026, 11, 13, 6, 35, tzinfo=UTC)
    db = FakeDB(now)
    notifier = AsyncMock()
    notifier.send.return_value = True
    patrol = XAUShadowPatrol(db, notifier, now=lambda: now)

    result = await patrol.run_once()

    assert result["ok"] is False
    assert "运行身份未核验" in result["message"]
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


def test_daily_patrol_fires_at_0733_beijing_only():
    cst = ZoneInfo("Asia/Shanghai")
    trigger = daily_patrol_trigger()
    before = datetime(2026, 11, 13, 7, 32, tzinfo=cst)
    after = datetime(2026, 11, 13, 7, 34, tzinfo=cst)
    assert trigger.get_next_fire_time(None, before) == datetime(2026, 11, 13, 7, 33, tzinfo=cst)
    assert trigger.get_next_fire_time(None, after) == datetime(2026, 11, 14, 7, 33, tzinfo=cst)


@pytest.mark.asyncio
async def test_daily_patrol_checks_current_due_hour():
    now = datetime(2026, 11, 13, 23, 45, tzinfo=UTC)
    db = FakeDB(now)
    notifier = AsyncMock()
    notifier.send.return_value = True
    await XAUShadowPatrol(db, notifier, now=lambda: now).run_once()
    gap_call = next(call for call in db.calls if "generate_series" in call[0])
    assert gap_call[1][1] == now.replace(minute=0)

@pytest.mark.asyncio
@pytest.mark.parametrize("recent", [0, 1])
async def test_reports_historical_gaps_without_repeated_new_alerts(recent, monkeypatch):
    from ai_tuner.monitor import xau_shadow_patrol as patrol
    monkeypatch.setattr(patrol, "digest_outbox", None)
    monkeypatch.setattr(patrol, "runtime_issues", lambda now: [])
    now = datetime(2026, 11, 28, 1, 10, tzinfo=UTC)
    db = FakeDB(now)
    fetch_one = db.fetch_one

    async def facts(query, *args):
        if "generate_series" in query:
            assert "FILTER (WHERE slot.scheduled_at >= $3)" in query
            assert args[1] == datetime(2026, 11, 28, 0, tzinfo=UTC)
            assert args[2] == args[1] - timedelta(hours=23)
            return {"n": 6 + recent, "recent_gaps": recent}
        return await fetch_one(query, *args)

    db.fetch_one = facts
    result = await XAUShadowPatrol(db, AsyncMock(), now=lambda: now).run_once()
    assert f"累计 {6 + recent}，近24小时 {recent}" in result["message"]
    assert ("近24小时存在校准版小时缺口" in result["message"]) == bool(recent)
    assert result["ok"] == (recent == 0)
