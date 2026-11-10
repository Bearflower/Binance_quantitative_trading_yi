"""Optional isolated PostgreSQL contract for the insert-only writer."""

import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from scripts.reconcile_income_attribution import apply_plan


@pytest.mark.asyncio
async def test_writer_is_idempotent_and_preserves_exchange_fact():
    dsn = os.environ.get("ATTRIBUTION_TEST_DSN")
    if not dsn:
        pytest.skip("ATTRIBUTION_TEST_DSN not set")
    import asyncpg

    connection = await asyncpg.connect(dsn)
    event_time = datetime(2026, 11, 9, tzinfo=timezone.utc)
    try:
        await connection.execute("""CREATE TABLE IF NOT EXISTS trading.execution_fact_income (
            account_id text NOT NULL, income_type text NOT NULL, tran_id bigint NOT NULL,
            symbol text NOT NULL, income numeric NOT NULL, asset text NOT NULL,
            event_time_utc timestamptz NOT NULL,
            PRIMARY KEY(account_id, income_type, tran_id))""")
        await connection.execute("""CREATE TABLE IF NOT EXISTS trading.reviewed_income_attribution (
            account_id text NOT NULL, income_type text NOT NULL, tran_id bigint NOT NULL,
            strategy text NOT NULL, symbol text NOT NULL, income numeric NOT NULL,
            asset text NOT NULL, event_time_utc timestamptz NOT NULL,
            PRIMARY KEY(account_id, income_type, tran_id))""")
        await connection.execute("""INSERT INTO trading.execution_fact_income
            VALUES ('writer-test','FUNDING_FEE',992,'ABCUSDT',-0.1,'USDT',$1)
            ON CONFLICT DO NOTHING""", event_time)
        row = {"account_id": "writer-test", "income_type": "FUNDING_FEE",
               "tran_id": 992, "strategy": "hrs", "symbol": "ABCUSDT",
               "income": Decimal("-0.1"), "asset": "USDT",
               "event_time_utc": event_time, "method": "verified_position_interval",
               "evidence": {"baseline_at": "2026-11-08T00:00:00+00:00"},
               "algorithm_version": 1}
        plan = {"conflicts": [], "order_rows": [], "derived_rows": [row]}
        await apply_plan(connection, plan, coverage_complete=True)
        await apply_plan(connection, plan, coverage_complete=True)
        assert await connection.fetchval("""SELECT count(*) FROM trading.derived_income_attribution
            WHERE account_id='writer-test' AND income_type='FUNDING_FEE' AND tran_id=992""") == 1
        assert await connection.fetchval("""SELECT income FROM trading.execution_fact_income
            WHERE account_id='writer-test' AND income_type='FUNDING_FEE' AND tran_id=992""") == Decimal("-0.1")
    finally:
        await connection.close()
