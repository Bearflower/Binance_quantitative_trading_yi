"""The STOP exit ledger rejects conflicting replays and trade reuse."""

import pytest

from shared.accounting.stop_exit_ledger import (
    StopExitConflict, latest_verified_stop_time_ms, persist_verified_stop_exit,
)


def event():
    return {
        "proof_kind": "closed_stop_episode_v1", "account_id": "default",
        "strategy": "btc_eth_aggressive", "symbol": "ETHUSDT",
        "episode_entry_order_id": 10, "algo_id": 20, "order_id": 30,
        "side": "SELL", "quantity": "0.7", "realized_pnl": "-2",
        "commission": "0.1", "net_pnl": "-2.1", "commission_asset": "USDT",
        "entry_first_fill_time_ms": 1000, "last_fill_time_ms": 4000,
        "trade_ids": [3, 4],
    }


class Context:
    def __init__(self, value):
        self.value = value
        self.exit_error = None

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, error_type, error, traceback):
        self.exit_error = error


class Connection:
    def __init__(self, trade_owner=20):
        self.trade_owner = trade_owner
        self.writes = []
        self.transaction_context = Context(None)

    def transaction(self):
        return self.transaction_context

    async def execute(self, query, *args):
        self.writes.append((query, args))

    async def fetchrow(self, query, *args):
        return {
            "strategy": "btc_eth_aggressive", "episode_entry_order_id": 10,
            "order_id": 30, "stop_side": "SELL", "quantity": "0.7",
            "realized_pnl": "-2", "commission": "0.1", "net_pnl": "-2.1",
            "entry_first_fill_time_ms": 1000, "last_fill_time_ms": 4000,
        }

    async def fetchval(self, query, *args):
        return self.trade_owner

    async def fetch(self, query, *args):
        return [{"trade_id": 3}, {"trade_id": 4}]


class DB:
    def __init__(self, conn):
        self.conn = conn
        self.pool = self
        self.acquire_context = Context(conn)

    def acquire(self):
        return self.acquire_context


@pytest.mark.asyncio
async def test_exact_replay_and_unique_trades_are_accepted():
    conn = Connection()
    db = DB(conn)
    assert await persist_verified_stop_exit(db, event())
    assert await persist_verified_stop_exit(db, event())
    assert len(conn.writes) == 6
    assert conn.transaction_context.exit_error is None


@pytest.mark.asyncio
async def test_conflicting_trade_owner_rolls_back():
    conn = Connection(trade_owner=99)
    with pytest.raises(StopExitConflict, match="trade ID"):
        await persist_verified_stop_exit(DB(conn), event())
    assert isinstance(conn.transaction_context.exit_error, StopExitConflict)


@pytest.mark.asyncio
async def test_unproven_or_inconsistent_event_never_writes():
    conn = Connection()
    db = DB(conn)
    invalid = event() | {"proof_kind": "order_only"}
    assert not await persist_verified_stop_exit(db, invalid)
    invalid = event() | {"net_pnl": "1"}
    assert not await persist_verified_stop_exit(db, invalid)
    assert conn.writes == []


@pytest.mark.asyncio
async def test_cooldown_query_is_scoped_to_account_strategy_symbol():
    class ReadDB:
        async def fetch_one(self, query, *args):
            assert args == ("default", "btc_eth_aggressive", "ETHUSDT")
            assert "strategy=$2" in query
            return {"last_fill_time_ms": 4000}

    assert await latest_verified_stop_time_ms(
        ReadDB(), "default", "btc_eth_aggressive", "ETHUSDT",
    ) == 4000
