from datetime import datetime, timezone

import pytest

from ai_tuner.readiness.auditor import ReadinessAuditor


START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 2, tzinfo=timezone.utc)


class SnapshotDB:
    def __init__(self, row):
        self.row = row

    async def fetch_one(self, query, *args):
        self.query = query
        assert "ai_tuner_universe_snapshots" in query
        assert "ai_tuner_market_snapshots" in query
        return self.row


def auditor_for(db):
    auditor = ReadinessAuditor.__new__(ReadinessAuditor)
    auditor.db = db
    return auditor


@pytest.mark.asyncio
async def test_snapshot_coverage_uses_complete_symbol_hours_only():
    result = await auditor_for(SnapshotDB({
        "expected": 10, "complete": 9,
        "evidence_start": START, "sources": ["binance", "coingecko"],
        "has_window_start_universe": True, "first_scan_at": START,
    }))._query_external_snapshot("hrs", START, END)
    assert result.expected == 10
    assert result.complete == 9
    assert result.coverage == 0.9


@pytest.mark.asyncio
async def test_missing_universe_history_cannot_pass_snapshot_gate():
    result = await auditor_for(SnapshotDB({
        "expected": 0, "complete": 0, "evidence_start": None, "sources": [],
    }))._query_external_snapshot("hrs", START, END)
    assert result.coverage == 0.0
    assert "historical_universe_missing" in result.gaps


@pytest.mark.asyncio
async def test_universe_membership_expires_at_next_global_scan():
    db = SnapshotDB({
        "expected": 1, "complete": 0, "evidence_start": START, "sources": [],
        "has_window_start_universe": True, "first_scan_at": START,
    })
    await auditor_for(db)._query_external_snapshot("hrs", START, END)
    assert "ai_tuner_universe_scan_runs" in db.query
    assert "JOIN scan_runs" in db.query
    assert "created_at <= $3" in db.query
    assert "m.collected_at <= $3" in db.query
    assert "m.source_observed_at <= $3" in db.query


@pytest.mark.asyncio
async def test_late_universe_history_is_fail_closed():
    late = START.replace(hour=12)
    result = await auditor_for(SnapshotDB({
        "expected": 12, "complete": 12, "evidence_start": late,
        "sources": ["binance", "coingecko"],
        "has_window_start_universe": False, "first_scan_at": late,
    }))._query_external_snapshot("hrs", START, END)
    assert result.coverage == 0.0
    assert result.expected == 12
    assert result.complete == 12
    assert any("historical_universe_missing_before=" in gap for gap in result.gaps)
