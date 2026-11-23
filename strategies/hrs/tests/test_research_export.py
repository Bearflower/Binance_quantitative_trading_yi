import json
import os
from datetime import timedelta
import pytest

from strategies.hrs.research_export import export_quotes, timestamp
from strategies.hrs.research_liquidity import open_store, iso


def test_quote_export_keeps_exit_after_window_but_honors_as_of(tmp_path, capsys):
    path = tmp_path / "quotes.sqlite"
    start = timestamp("2026-11-23T00:00:00+08:00")
    end = start + timedelta(hours=1)
    seen = start + timedelta(hours=6, seconds=1)
    with open_store(path) as db:
        for key, signal, received in [("valid", start, seen), ("future", start, seen + timedelta(seconds=1)),
                                      ("prior", start - timedelta(seconds=1), seen)]:
            payload = json.dumps({"signal_time": iso(signal), "recorded_at": iso(received)})
            db.execute("INSERT INTO receipts VALUES (?,?,?,?,?)",
                       (key, "exit_6h", iso(signal + timedelta(hours=6)), "ok", payload))
    export_quotes(path, start, end, seen)
    assert json.loads(capsys.readouterr().out)["evidence_id"] == "valid"
    with pytest.raises(ValueError):
        timestamp("2026-11-23")


@pytest.mark.skipif(not os.environ.get("HRS_TEST_POSTGRES_DSN"), reason="isolated PostgreSQL required")
@pytest.mark.asyncio
async def test_analysis_export_excludes_late_recorded_evidence(monkeypatch, capsys):
    import asyncpg
    from unittest.mock import AsyncMock
    from strategies.hrs.research_export import export_analysis
    dsn = os.environ["HRS_TEST_POSTGRES_DSN"]
    conn = await asyncpg.connect(dsn)
    start = timestamp("2026-11-23T01:00:00+00:00")
    end = start + timedelta(seconds=30)
    fixture_transaction = conn.transaction()
    await fixture_transaction.start()
    try:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS hrs")
        await conn.execute("""CREATE TABLE IF NOT EXISTS hrs.hrs_decision_evidence
              (evidence_id text, run_id text, kind text, symbol text, event_time timestamptz,
               available_at timestamptz, recorded_at timestamptz, payload_sha256 text, payload jsonb)""")
        for key, recorded in [("on_time", start), ("late", end + timedelta(seconds=1))]:
            await conn.execute("""INSERT INTO hrs.hrs_decision_evidence
                     (evidence_id,run_id,kind,symbol,event_time,available_at,recorded_at,payload_sha256,payload)
                     VALUES ($1,'export_test','lv_rm_analysis','TESTUSDT',$2,$2,$3,'test','{"long_should_entry":true}'::jsonb)""",
                     key,start,recorded)
        class Source:
            cursor = conn.cursor
            close = AsyncMock()

            def transaction(self, **options):
                assert options["readonly"]
                return conn.transaction()

        monkeypatch.setenv("DATABASE_URL", dsn)
        monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=Source()))
        await export_analysis(start, end, end)
        assert json.loads(capsys.readouterr().out)["evidence_id"] == "on_time"
    finally:
        await fixture_transaction.rollback()
        await conn.close()
