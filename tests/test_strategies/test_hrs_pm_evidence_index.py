"""Real PostgreSQL regression for the unchanged archived PM lookup."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from strategies.hrs.equity_service import HrsEquityService
from strategies.hrs.equity_evidence import canonical, EvidenceBlocked

DSN = os.environ.get("HRS_INDEX_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="dedicated disposable PostgreSQL required")
MIGRATION = Path("database/postgres/migrations/2026-11-25-hrs-pm-evidence-index.sql")


@pytest.mark.asyncio
async def test_lookup_uses_bounded_index_preserves_cutoff_segment_and_digest():
    c = await asyncpg.connect(DSN)
    try:
        # This suite owns its dedicated database, never a shared production DSN.
        assert await c.fetchval("SELECT current_database()") == "hrs_index_test"
        await c.execute("DROP SCHEMA IF EXISTS trading CASCADE; CREATE SCHEMA trading")
        await c.execute(Path("database/postgres/migrations/2026-11-17-hrs-equity-service.sql").read_text())
        await c.execute("""INSERT INTO trading.hrs_equity_segment
            VALUES ('test','seg',now(),100,'hash','{}',now())""")
        boundary = datetime(2026,11,25,tzinfo=timezone.utc)
        # Many rejected JSON rows reproduce the old full-table predicate scan.
        await c.execute("""INSERT INTO trading.hrs_equity_evidence
            SELECT 'test',md5(n::text),'seg',$1::timestamptz-interval '1 hour',
                $1::timestamptz-interval '1 hour',jsonb_build_object('padding',repeat(md5(n::text),100))
            FROM generate_series(1,12000) n""", boundary)
        doc = dict(account_id="test",result=dict(ledger_version=1),
                   source_collection=dict(complete=True,gaps=[]))
        encoded, digest = canonical(doc)
        async def insert(doc_text, sha, segment, as_of, available):
            await c.execute("INSERT INTO trading.hrs_equity_evidence VALUES('test',$1,$2,$3,$4,$5::jsonb)",
                            sha,segment,as_of,available,doc_text)
        await insert(encoded,digest,"seg",boundary-timedelta(minutes=3),boundary-timedelta(minutes=2))
        await insert(encoded,"future-availability","seg",boundary-timedelta(minutes=1),boundary)
        await insert(encoded,"future-valuation","seg",boundary,boundary)
        await insert(encoded,"wrong-segment","other",boundary-timedelta(seconds=30),boundary-timedelta(seconds=30))
        captured = []
        async def fetch_one(sql,*args):
            captured.append((sql,args))
            return await c.fetchrow(sql,*args)
        service = object.__new__(HrsEquityService)
        service.account = "test"
        service.db = SimpleNamespace(fetch_one=fetch_one)
        cutover = int(boundary.timestamp()*1000)
        before = await service._last_pm_evidence(cutover)
        assert before == (digest,doc)
        if MIGRATION.exists():
            # psql autocommit: concurrent DDL is never wrapped in a transaction.
            applied = subprocess.run(["psql", DSN, "-X", "-v", "ON_ERROR_STOP=1",
                                      "-f", str(MIGRATION)], capture_output=True, text=True)
            assert applied.returncode == 0, applied.stderr
        await c.execute("ANALYZE trading.hrs_equity_evidence")
        assert await service._last_pm_evidence(cutover) == before
        sql,args = captured[-1]
        plan = json.loads(await c.fetchval("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) "+sql,*args))[0]
        assert "hrs_equity_pm_lookup_idx" in json.dumps(plan), plan
        assert plan["Plan"]["Shared Hit Blocks"] + plan["Plan"]["Shared Read Blocks"] < 100, plan
        assert await c.fetchval("""SELECT indisvalid AND indisready FROM pg_index
            WHERE indexrelid='trading.hrs_equity_pm_lookup_idx'::regclass""")
        # Even indexed evidence still passes the existing integrity check.
        await c.execute("UPDATE trading.hrs_equity_evidence SET payload=payload||'{\"tampered\":true}'::jsonb WHERE evidence_sha256=$1",digest)
        with pytest.raises(EvidenceBlocked,match="archived_pm_owner_evidence_changed"):
            await service._last_pm_evidence(cutover)
        # IF NOT EXISTS must never accept a pre-existing wrong access path.
        await c.execute("DROP INDEX trading.hrs_equity_pm_lookup_idx")
        await c.execute("CREATE INDEX hrs_equity_pm_lookup_idx ON trading.hrs_equity_evidence(account_id)")
        rejected = subprocess.run(["psql", DSN, "-X", "-v", "ON_ERROR_STOP=1",
                                   "-f", str(MIGRATION)], capture_output=True, text=True)
        assert rejected.returncode != 0, "migration silently accepted wrong index"
    finally:
        await c.close()
