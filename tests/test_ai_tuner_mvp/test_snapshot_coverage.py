from datetime import datetime, timezone

import pytest

from ai_tuner.readiness.auditor import ReadinessAuditor


START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 10, 2, tzinfo=timezone.utc)


class SnapshotDB:
    def __init__(self, row):
        self.row = row

    async def fetch_one(self, query, *args):
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
