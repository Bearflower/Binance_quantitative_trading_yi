"""Real PostgreSQL tests, opt in only against an explicitly supplied test DSN."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import os
from pathlib import Path
import uuid

import pytest
import pytest_asyncio

from strategies.hrs.loss_gate import GateHistory, evaluate_loss_gate
from strategies.hrs.loss_gate_repository import LossGateRepository, RepositoryConflict
from strategies.hrs.risk_ledger import IncomeEvent, LedgerObservation, initialize_ledger, reconcile_ledger

T = datetime(2026, 10, 1, 16, tzinfo=timezone.utc)
DSN = os.environ.get("HRS_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="HRS_TEST_POSTGRES_DSN required: isolated PostgreSQL only")


def observation(at=T):
    return LedgerObservation(at, "hrs-1", (), (), (), at, at, True, True)


def seed():
    ledger = initialize_ledger("hrs-1", D("1000"), observation())
    gate = evaluate_loss_gate(None, ledger.checkpoint,
                             GateHistory((ledger.checkpoint,), T, T, True),
                             allow_inception_segment=True).state
    return ledger, gate


@pytest_asyncio.fixture
async def store():
    import asyncpg
    from shared.database import DatabaseManager
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=5)
    migration = Path("database/postgres/migrations/2026-11-17-hrs-risk-ledger-loss-gates.sql").read_text()
    async with pool.acquire() as connection:
        await connection.execute("CREATE SCHEMA IF NOT EXISTS trading")
        await connection.execute(migration)
        await connection.execute(migration)  # migration must be idempotent
    db = DatabaseManager("unused", 1, "unused", "unused", "")
    db.pool = pool
    account = "test-" + uuid.uuid4().hex
    try:
        yield LossGateRepository(db), account, pool
    finally:
        async with pool.acquire() as connection:
            for table in ("hrs_loss_gate_reset_audit", "hrs_risk_ledger_events", "hrs_risk_equity_checkpoints", "hrs_loss_gate_state"):
                await connection.execute(f"DELETE FROM trading.{table} WHERE account_id=$1", account)
        await pool.close()


@pytest.mark.asyncio
async def test_atomic_checkpoint_restart_idempotency_and_conflicting_retry(store):
    repo, account, pool = store
    ledger, gate = seed()
    first = await repo.commit(account, ledger, gate, expected_version=None, checkpoint_id="opening")
    assert first.version == 1
    restored = await LossGateRepository(repo.db).load(account, "hrs-1")
    assert restored.ledger == ledger
    assert restored.gate == gate
    duplicate = await repo.commit(account, ledger, gate, expected_version=None, checkpoint_id="opening")
    assert duplicate.version == 1
    with pytest.raises(RepositoryConflict, match="checkpoint_conflict"):
        await repo.commit(account, replace(ledger, cash_allocation=D("2000")), gate,
                          expected_version=1, checkpoint_id="opening")
    async with pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM trading.hrs_risk_equity_checkpoints WHERE account_id=$1", account) == 1


@pytest.mark.asyncio
async def test_concurrent_compare_and_swap_has_exactly_one_winner(store):
    repo, account, pool = store
    ledger, gate = seed()
    await repo.commit(account, ledger, gate, expected_version=None, checkpoint_id="opening")
    at = T + timedelta(minutes=1)
    event = IncomeEvent("income-1", "realized_pnl", D("-30"), at, "hrs-1", True)
    updated = reconcile_ledger(ledger, replace(observation(at), incomes=(event,)))
    new_gate = evaluate_loss_gate(gate, updated.checkpoint,
                                 GateHistory((ledger.checkpoint, updated.checkpoint), T, at, True),
                                 allow_inception_segment=True).state
    results = await asyncio.gather(*(
        repo.commit(account, updated, new_gate, expected_version=1, checkpoint_id=f"race-{i}")
        for i in range(2)
    ), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, RepositoryConflict) for item in results) == 1
    loaded = await repo.load(account, "hrs-1")
    assert loaded.version == 2
    assert loaded.gate.daily_latched
    assert loaded.ledger.income_total == D("-30")
    async with pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM trading.hrs_risk_ledger_events WHERE account_id=$1", account) == 1


@pytest.mark.asyncio
async def test_locked_scope_serializes_gate_check_and_reservation(store):
    repo, account, pool = store
    ledger, gate = seed()
    await repo.commit(account, ledger, gate, expected_version=None, checkpoint_id="opening")
    entered = asyncio.Event()
    release = asyncio.Event()
    second_entered = asyncio.Event()

    async def first():
        async with repo.locked(account, "hrs-1") as session:
            assert (await session.load()).version == 1
            entered.set()
            await release.wait()

    async def second():
        await entered.wait()
        async with repo.locked(account, "hrs-1") as session:
            second_entered.set()
            assert session.connection is not None

    task1 = asyncio.create_task(first())
    task2 = asyncio.create_task(second())
    await entered.wait()
    await asyncio.sleep(.05)
    assert not second_entered.is_set()
    release.set()
    await asyncio.gather(task1, task2)
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_reset_audit_persists_and_stale_reset_or_segment_switch_rejected(store):
    repo, account, pool = store
    ledger, gate = seed()
    gate = replace(gate, rolling_latched=True, rolling_latch_scope="inception_segment")
    await repo.commit(account, ledger, gate, expected_version=None, checkpoint_id="opening")
    reset = await repo.reset_rolling(account, "hrs-1", expected_version=1,
                                    reason="reviewed", evidence="case-42", at=T)
    assert reset.version == 2
    assert not reset.gate.rolling_latched
    with pytest.raises(RepositoryConflict, match="version_conflict"):
        await repo.reset_rolling(account, "hrs-1", expected_version=1,
                                reason="again", evidence="case-42", at=T)
    with pytest.raises(RepositoryConflict, match="segment_conflict"):
        await repo.load(account, "different-segment")
    async with pool.acquire() as connection:
        row = await connection.fetchrow("SELECT reason,evidence FROM trading.hrs_loss_gate_reset_audit WHERE account_id=$1", account)
        assert dict(row) == {"reason": "reviewed", "evidence": "case-42"}


@pytest.mark.asyncio
async def test_income_event_cannot_be_removed_or_rewritten_and_failure_rolls_back(store):
    repo, account, pool = store
    ledger, gate = seed()
    await repo.commit(account, ledger, gate, expected_version=None, checkpoint_id="opening")
    at = T + timedelta(minutes=1)
    event = IncomeEvent("income-1", "realized_pnl", D("1"), at, "hrs-1", True)
    updated = reconcile_ledger(ledger, replace(observation(at), incomes=(event,)))
    await repo.commit(account, updated, gate, expected_version=1, checkpoint_id="income")
    for invalid in (replace(updated, as_of=at + timedelta(minutes=1), incomes=(), income_total=D("0")), replace(updated, as_of=at + timedelta(minutes=1), incomes=(replace(event, amount=D("2")),), income_total=D("2"))):
        with pytest.raises(RepositoryConflict, match="event_history_conflict"):
            await repo.commit(account, invalid, gate, expected_version=2, checkpoint_id="invalid")
    assert (await repo.load(account, "hrs-1")).version == 2
    async with pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM trading.hrs_risk_equity_checkpoints WHERE account_id=$1", account) == 2


@pytest.mark.asyncio
async def test_repository_rejects_implicit_daily_reset_and_invalid_nav(store):
    repo, account, pool = store
    ledger, gate = seed()
    gate = replace(gate, daily_latched=True)
    await repo.commit(account, ledger, gate, expected_version=None, checkpoint_id="opening")
    updated = reconcile_ledger(ledger, observation(T + timedelta(minutes=1)))
    with pytest.raises(RepositoryConflict, match="daily_reset_requires_valid_new_day"):
        await repo.commit(account, updated, replace(gate, daily_latched=False), expected_version=1, checkpoint_id="bypass")
    with pytest.raises(RepositoryConflict, match="invalid_ledger_checkpoint"):
        await repo.commit(account, replace(updated, nav=D("2")), gate, expected_version=1, checkpoint_id="broken")


@pytest.mark.asyncio
async def test_failed_commit_savepoint_cannot_leave_partial_events_if_caught(store):
    repo, account, pool = store
    ledger, gate = seed()
    await repo.commit(account, ledger, gate, expected_version=None, checkpoint_id="opening")
    at = T + timedelta(minutes=1)
    event = IncomeEvent("event", "realized_pnl", D("1"), at, "hrs-1", True)
    updated = reconcile_ledger(ledger, replace(observation(at), incomes=(event,)))
    # Invalid checkpoint uniqueness after an event write must rollback the write,
    # even when the caller handles the error inside a longer reservation scope.
    async with repo.locked(account, "hrs-1") as session:
        original = session.connection
        # Exercise actual DB rollback by adding a rejecting temporary constraint.
        await original.execute("ALTER TABLE trading.hrs_risk_equity_checkpoints ADD CONSTRAINT hrs_test_reject CHECK (checkpoint_id <> 'reject-me')")
        try:
            with pytest.raises(Exception):
                await session.commit(updated, gate, expected_version=1, checkpoint_id="reject-me")
            assert await original.fetchval("SELECT count(*) FROM trading.hrs_risk_ledger_events WHERE account_id=$1", account) == 0
        finally:
            await original.execute("ALTER TABLE trading.hrs_risk_equity_checkpoints DROP CONSTRAINT hrs_test_reject")
    assert (await repo.load(account, "hrs-1")).version == 1


@pytest.mark.asyncio
async def test_restart_duplicate_income_does_not_change_equity_or_insert_again(store):
    repo, account, pool = store
    ledger, gate = seed()
    await repo.commit(account, ledger, gate, expected_version=None, checkpoint_id="opening")
    at = T + timedelta(minutes=1)
    event = IncomeEvent("event", "realized_pnl", D("-30"), at, "hrs-1", True)
    updated = reconcile_ledger(ledger, replace(observation(at), incomes=(event,)))
    new_gate = evaluate_loss_gate(gate, updated.checkpoint,
                                 GateHistory((ledger.checkpoint, updated.checkpoint), T, at, True),
                                 allow_inception_segment=True).state
    await repo.commit(account, updated, new_gate, expected_version=1, checkpoint_id="loss")
    restarted = await LossGateRepository(repo.db).load(account, "hrs-1")
    later = at + timedelta(minutes=1)
    duplicate = reconcile_ledger(restarted.ledger, replace(observation(later), incomes=(event,)))
    result = await repo.commit(account, duplicate, restarted.gate, expected_version=2, checkpoint_id="duplicate-income")
    assert result.ledger.equity == D("970")
    assert result.gate.daily_latched
    async with pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM trading.hrs_risk_ledger_events WHERE account_id=$1", account) == 1
    checkpoints = await repo.checkpoints(account, "hrs-1", since=at)
    assert [sample.as_of for sample in checkpoints] == [at, later]
