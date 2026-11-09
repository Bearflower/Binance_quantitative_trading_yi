"""Run only against a disposable local stop_tests PostgreSQL database."""

import os
from uuid import uuid4

import pytest

from shared.accounting.stop_binding import record_stop_binding
from shared.accounting.stop_exit_ledger import StopExitConflict, persist_verified_stop_exit


DSN = os.environ.get("STOP_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="Set STOP_TEST_DSN to local stop_tests DB")


class DB:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, query, *args):
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch_one(self, query, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)


def event(account, algo=20, order=30, trades=None, strategy="btc_eth_aggressive"):
    return {
        "proof_kind": "closed_stop_episode_v1", "account_id": account,
        "strategy": strategy, "symbol": "ETHUSDT",
        "episode_entry_order_id": 10, "algo_id": algo, "order_id": order,
        "side": "SELL", "quantity": "0.7", "realized_pnl": "-2",
        "commission": "0.1", "net_pnl": "-2.1", "commission_asset": "USDT",
        "entry_first_fill_time_ms": 1000, "last_fill_time_ms": 4000,
        "trade_ids": trades or [3, 4],
    }


@pytest.mark.asyncio
async def test_real_pg_fk_unique_trade_and_transaction_rollback():
    import asyncpg

    assert DSN.startswith("postgresql://") and "127.0.0.1:55432/stop_tests" in DSN
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    account = "test-" + uuid4().hex
    db = DB(pool)
    try:
        for algo in (20, 21):
            assert await record_stop_binding(
                db, account_id=account, strategy="btc_eth_aggressive",
                symbol="ETHUSDT", episode_entry_order_id=10,
                algo_id=algo, stop_side="SELL", stop_quantity="0.7",
            )
        assert await persist_verified_stop_exit(db, event(account))
        assert await persist_verified_stop_exit(db, event(account))

        with pytest.raises(StopExitConflict, match="trade ID"):
            await persist_verified_stop_exit(
                db, event(account, algo=21, order=31, trades=[3, 5]),
            )
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM trading.aggressive_stop_exits WHERE account_id=$1",
                account,
            ) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM trading.aggressive_stop_exit_trades WHERE account_id=$1",
                account,
            ) == 2

        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await persist_verified_stop_exit(
                db, event(account, algo=21, order=31, trades=[6, 7], strategy="other"),
            )
    finally:
        await pool.close()
