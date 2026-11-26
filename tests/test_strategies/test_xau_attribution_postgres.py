"""Committed XAU attribution survives writer exit and a fresh-process export.

Opt in with XAU_ATTRIBUTION_TEST_DSN (loopback database ending ``_test``) and
XAU_ATTRIBUTION_TEST_DB_ACK=disposable. Never point this at a production tunnel.
The database must not already contain research; only the schema created by this
test is dropped. No application env file or production credentials are loaded.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import yaml

from strategies.btc_eth.shadow.collector import ShadowCollector
from strategies.btc_eth.shadow.export import export_run
from strategies.btc_eth.shadow.repository import ShadowRepository


ROOT = Path(__file__).resolve().parents[2]
DSN_ENV = "XAU_ATTRIBUTION_TEST_DSN"
ACK_ENV = "XAU_ATTRIBUTION_TEST_DB_ACK"
PROFILES = ("xau_original", "xau_aggressive", "xau_calibrated")
CASES = (("trend", False), ("mean_reversion", False), ("trend", True), ("unknown", None))
START = datetime(2026, 11, 25, tzinfo=timezone.utc)


def _safe_dsn():
    dsn = os.environ.get(DSN_ENV, "")
    parsed = urlparse(dsn)
    if (
        os.environ.get(ACK_ENV) != "disposable"
        or parsed.scheme not in ("postgres", "postgresql")
        or parsed.hostname not in ("127.0.0.1", "localhost")
        or parsed.query or parsed.fragment
        or not re.fullmatch(r"/[a-zA-Z0-9_]+_test", parsed.path)
    ):
        raise ValueError("requires acknowledged disposable loopback PostgreSQL *_test database")
    return dsn


class _Database:
    """Actual asyncpg transport for the DatabaseManager interface used by repo."""

    def __init__(self, connection):
        self.connection = connection

    async def execute(self, query, *args):
        return await self.connection.execute(query, *args)

    async def fetch_one(self, query, *args):
        row = await self.connection.fetchrow(query, *args)
        return dict(row) if row is not None else None

    async def fetch_all(self, query, *args):
        return [dict(row) for row in await self.connection.fetch(query, *args)]


class _NoExchangeAccess:
    async def get_account_info(self):
        raise AssertionError("persistence test must never call an exchange")


def _events(run_id, repo):
    configs = {
        profile: yaml.safe_load(
            (ROOT / "strategies/btc_eth/shadow/config" / f"{profile}.yaml").read_text()
        ) for profile in PROFILES
    }
    events = []
    for profile in PROFILES:
        collector = ShadowCollector(
            run_id=run_id, instance_id=configs[profile]["instance"]["id"],
            symbol="XAUUSDT", profiles=[profile], profile_configs={profile: configs[profile]},
            repository=repo, kline_service=None, binance_client=_NoExchangeAccess(),
            git_commit="attribution-persistence-test",
        )
        for index, (intent, fallback) in enumerate(CASES):
            signal = {
                "direction": "LONG", "grade": "A", "score": 88.5,
                "entry_price": Decimal("4000"), "initial_stop_loss": Decimal("3980"),
                "tp1_price": Decimal("4020"), "tp2_price": Decimal("4040"),
                "atr": Decimal("10"), "price_change_1h": 0.01,
                "market_state": "RANGING",
            }
            if intent != "unknown":
                signal.update(entry_intent=intent, entry_route_fallback=fallback)
            original_signal = deepcopy(signal)
            scheduled = START + timedelta(hours=index)
            event = collector._make_event_from_signal(
                profile_id=profile, scheduled_at=scheduled,
                evaluated_at=scheduled + timedelta(seconds=1),
                signal_components=signal, close_times={"1h": 1795564800000},
                atr_filter_snapshot={"history_count": 40, "effective_atr_pct": 0.25},
            )
            assert signal == original_signal
            events.append(event)
        scheduled = START + timedelta(hours=4)
        events.append(collector._make_event(profile_id=profile, scheduled_at=scheduled,
            evaluated_at=scheduled + timedelta(seconds=1), decision="no_signal"))
    return events


async def _child(mode, run_id, output_dir):
    connection = await asyncpg.connect(_safe_dsn())
    try:
        db = _Database(connection)
        repo = ShadowRepository(db)
        if mode == "write":
            events = _events(run_id, repo)
            assert await repo.upsert_shadow_run(
                run_id=run_id, instance_id="xau-attribution-test", symbol="XAUUSDT",
                started_at=START, initial_virtual_equity_usdt=300,
                git_commit="attribution-persistence-test", git_sha="a" * 40,
                config_hashes={event["profile_id"]: event["config_hash"] for event in events},
            )
            assert await repo.insert_decision_events_batch(events) == 15
            assert await repo.insert_decision_events_batch(events) == 0
            # Every INSERT above commits normally, outside any rollback transaction.
        else:
            assert mode == "export"
            result = await export_run(db=db, run_id=run_id, output_dir=output_dir)
            assert result["events_exported"] == 15
    finally:
        await connection.close()


@pytest.mark.skipif(
    not os.getenv(DSN_ENV) or os.getenv(ACK_ENV) != "disposable",
    reason="dedicated disposable local PostgreSQL not explicitly configured",
)
def test_xau_jsonb_attribution_survives_writer_exit_and_export(tmp_path):
    dsn = _safe_dsn()
    run_id = f"xau-attribution-{uuid4()}"

    async def create_schema():
        connection = await asyncpg.connect(dsn)
        try:
            async with connection.transaction():
                # Fail closed on a pre-existing schema; roll back incomplete setup.
                await connection.execute("CREATE SCHEMA research")
                ddl = (ROOT / "database/postgres/init-scripts/12-research-schema.sql").read_text()
                for table in ("shadow_runs", "shadow_decision_events"):
                    statement = re.search(
                        rf"CREATE TABLE IF NOT EXISTS research\.{table} \(.*?\n\);",
                        ddl, re.DOTALL,
                    )
                    assert statement is not None
                    await connection.execute(statement.group())
        finally:
            await connection.close()

    async def read_committed_rows():
        connection = await asyncpg.connect(dsn)
        try:
            rows = await ShadowRepository(_Database(connection)).find_events_by_run(run_id=run_id)
            assert len(rows) == 15
            for row in rows:
                index = int((row["scheduled_at"] - START).total_seconds() // 3600)
                if index == 4:
                    assert row["decision"] == "no_signal"
                    assert json.loads(row["feature_snapshot"]) == {"entry_attribution": {
                        "schema_version": 1, "status": "no_candidate", "intent": None,
                        "producer_git_commit": "attribution-persistence-test",
                    }}
                    continue
                intent, fallback = CASES[index]
                snapshot = json.loads(row["feature_snapshot"])
                assert snapshot == {
                    "entry_attribution": {"schema_version": 1,
                        "status": "unknown" if intent == "unknown" else "recorded",
                        "intent": intent, "producer_git_commit": "attribution-persistence-test"},
                    "entry_intent": intent, "entry_route_fallback": fallback,
                    "market_state": "RANGING", "atr": 10.0, "price_change_1h": 0.01,
                    "atr_filter": {"history_count": 40, "effective_atr_pct": 0.25},
                }
            return rows
        finally:
            await connection.close()

    async def cleanup():
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute("DROP SCHEMA research CASCADE")
        finally:
            await connection.close()

    asyncio.run(create_schema())
    try:
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        for mode in ("write", "export"):
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), mode, run_id, str(tmp_path)],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            # The writer has exited before any independent reader starts.
            if mode == "write":
                rows = asyncio.run(read_committed_rows())

        exported = [json.loads(line) for line in (tmp_path / "decision_events.jsonl").read_text().splitlines()]
        manifest = json.loads((tmp_path / "run_manifest.json").read_text())
        assert len(exported) == 15
        assert manifest["run_id"] == run_id
        assert manifest["initial_virtual_equity_usdt"] == 300
        assert manifest["git_sha"] == "a" * 40
        for event, row in zip(exported, rows):
            for field in ("run_id", "profile_id", "symbol", "decision", "direction", "grade", "config_hash", "git_commit"):
                assert event[field] == row[field]
            for field in ("scheduled_at", "evaluated_at"):
                assert event[field] == row[field].isoformat()
            for field in ("feature_snapshot", "source_kline_close_times"):
                assert event[field] == json.loads(row[field])
            if event["decision"] == "candidate":
                assert event["reference_price"] == event["intended_entry_price"] == 4000
                assert event["score"] == 88.5
            else:
                assert event["reference_price"] is event["intended_entry_price"] is None
            assert event["config_hash"] == manifest["config_hashes"][event["profile_id"]]
    finally:
        asyncio.run(cleanup())


if __name__ == "__main__":
    asyncio.run(_child(*sys.argv[1:]))
