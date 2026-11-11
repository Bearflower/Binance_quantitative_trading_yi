"""A restarted new-coin lot needs a durable exchange entry identity."""
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from strategies.new_coin.executor import TradingExecutor
from tests.test_strategies.test_new_coin_recovery_evidence import setup_recovery


@pytest.mark.asyncio
async def test_short_position_persists_exchange_entry_order_identity():
    executor = TradingExecutor.__new__(TradingExecutor)
    executor.db = AsyncMock()

    await executor._insert_short_position(
        symbol="PFEUSDT", quantity=Decimal("3.28"),
        entry_price=Decimal("30.41271340"), entry_order_id=141970,
    )

    args = executor.db.execute.await_args.args
    assert "metadata" in args[0]
    assert args[2] == "PFEUSDT_141970"
    assert json.loads(args[-1]) == {"entry_order_id": 141970, "schema": 1}


@pytest.mark.asyncio
async def test_short_position_rejects_missing_exchange_entry_identity():
    executor = TradingExecutor.__new__(TradingExecutor)
    executor.db = AsyncMock()

    await executor._insert_short_position(
        symbol="PFEUSDT", quantity=Decimal("3.28"),
        entry_price=Decimal("30.41271340"), entry_order_id=None,
    )
    executor.db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_tp1_identity_is_bound_to_the_same_open_entry():
    executor = TradingExecutor.__new__(TradingExecutor)
    executor.db = AsyncMock()
    executor.db.execute.return_value = "UPDATE 1"
    executor.position_tracking = {"PFEUSDT": {"entry_order_id": 141970}}

    assert await executor._persist_tp1_identity("PFEUSDT", 2000001554798281) is True
    query, position_id, algo_id, entry_id = executor.db.execute.await_args.args
    assert "metadata" in query and "status='open'" in query
    assert position_id == "PFEUSDT_141970"
    assert algo_id == 2000001554798281
    assert entry_id == "141970"


@pytest.mark.asyncio
async def test_tp1_identity_does_not_bind_to_ambiguous_or_closed_row():
    executor = TradingExecutor.__new__(TradingExecutor)
    executor.db = AsyncMock()
    executor.db.execute.return_value = "UPDATE 0"
    executor.position_tracking = {"PFEUSDT": {"entry_order_id": 141970}}

    assert await executor._persist_tp1_identity("PFEUSDT", 2000001554798281) is False
    executor.db.execute.return_value = "UPDATE 2"
    assert await executor._persist_tp1_identity("PFEUSDT", 2000001554798281) is False


@pytest.mark.asyncio
async def test_recovery_restores_only_exact_persisted_exchange_ids_as_candidates():
    strategy, executor, rebuilt = setup_recovery(quantity=100)
    row = executor.db.fetch_all.return_value[0]
    row["position_id"] = "PFEUSDT_141970"
    row["metadata"] = {"schema": 1, "entry_order_id": 141970,
                       "tp1_algo_id": 2000001554798281}

    await strategy._sync_baseline_to_tracking(executor, rebuilt)

    tracking = executor.position_tracking["PFEUSDT"]
    assert tracking["recovery_entry_order_id"] == 141970
    assert tracking["recovery_tp1_algo_id"] == 2000001554798281
    assert tracking["time_exit_status"] == "review_required"


@pytest.mark.asyncio
async def test_recovery_rejects_mismatched_identity_without_unlocking():
    strategy, executor, rebuilt = setup_recovery(quantity=100)
    row = executor.db.fetch_all.return_value[0]
    row["position_id"] = "PFEUSDT_wrong"
    row["metadata"] = {"schema": 1, "entry_order_id": 141970,
                       "tp1_algo_id": 2000001554798281}

    await strategy._sync_baseline_to_tracking(executor, rebuilt)

    tracking = executor.position_tracking["PFEUSDT"]
    assert tracking.get("recovery_entry_order_id") is None
    assert tracking["time_exit_status"] == "unknown"


@pytest.mark.asyncio
async def test_recovery_parses_jsonb_text_metadata_without_unlocking():
    strategy, executor, rebuilt = setup_recovery(quantity=100)
    row = executor.db.fetch_all.return_value[0]
    row["position_id"] = "PFEUSDT_141970"
    row["metadata"] = json.dumps({"schema": 1, "entry_order_id": 141970,
                                  "tp1_algo_id": 2000001554798281})

    await strategy._sync_baseline_to_tracking(executor, rebuilt)

    tracking = executor.position_tracking["PFEUSDT"]
    assert tracking["recovery_entry_order_id"] == 141970
    assert tracking["time_exit_status"] == "review_required"
