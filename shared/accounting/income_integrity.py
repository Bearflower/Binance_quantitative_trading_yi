"""Account-scoped income integrity latch and lock shared by writers/readers."""
import asyncio
from contextlib import asynccontextmanager

_LOCK = 'SELECT pg_advisory_lock(hashtextextended($1, 0))'
_UNLOCK = 'SELECT pg_advisory_unlock(hashtextextended($1, 0))'
_BLOCKED = "SELECT count(*) FROM trading.income_ingestion_attempt WHERE account_id=$1 AND state IN ('pending','conflicted')"


class IncomeIntegrityBlocked(RuntimeError):
    """A durable ingestion attempt remains unresolved; no fresh report is valid."""


@asynccontextmanager
async def income_account_lock(connection, account_id):
    """Session lock must precede any repeatable-read snapshot; use one connection.

    Unlock even if cancellation interrupts acquisition. Pool reset is the final
    backstop if the connection itself dies; its durable pending latch remains.
    """
    key = 'income-integrity-v1:' + account_id
    try:
        await connection.execute(_LOCK, key)
        yield
    finally:
        cleanup = asyncio.create_task(connection.execute(_UNLOCK, key))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


async def require_income_integrity(connection, account_id):
    if await connection.fetchval(_BLOCKED, account_id):
        raise IncomeIntegrityBlocked('unresolved_income_ingestion')
