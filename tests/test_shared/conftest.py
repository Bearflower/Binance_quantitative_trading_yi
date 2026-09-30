"""
R07 占用表测试支撑（供 test_position_claims / test_position_ownership_regression 共用）

用假连接/假连接池模拟 trading.position_claims 的「部分唯一索引」与 advisory lock
串行化语义，**禁止连接生产数据库**。真实 DatabaseManager 仅注入假 pool，SQL 常量与
原子占位/释放/清理逻辑均走被测真实代码。
"""
import asyncio
import time
from contextlib import asynccontextmanager

import pytest

from shared.database import DatabaseManager

# 有效占用状态（与 DDL 的部分唯一索引口径一致）
_ACTIVE_STATES = ("PENDING", "ACTIVE")


class _FakeStore:
    """内存态占用表 + trade_records 权威归属，模拟部分唯一索引与锁串行化"""

    def __init__(self):
        self.rows = []               # 占用行：dict(id, symbol, strategy, claim_state, expires_at, ...)
        self.next_id = 1
        self.owner_by_symbol = {}    # trade_records 权威归属：symbol -> strategy
        self.raise_on_claim = False  # 置 True 时模拟占用写入异常（DB 不可用）
        self.set_config_values = []  # 记录 set_config（lock_timeout）入参，供断言
        self.lock = asyncio.Lock()   # 模拟 pg_advisory_xact_lock 串行化
        self.on_enter = None         # 事务进入钩子（AC4：锁持有期断言）
        self.on_exit = None          # 事务退出钩子

    def active(self, symbol, unexpired=False):
        """取 symbol 的有效占用行（id 最大者）；unexpired 时仅取未过期"""
        now = time.time()
        cand = [
            r for r in self.rows
            if r["symbol"] == symbol and r["claim_state"] in _ACTIVE_STATES
            and (not unexpired or r["expires_at"] > now)
        ]
        return max(cand, key=lambda r: r["id"]) if cand else None


class _FakeTxn:
    """事务上下文：以 asyncio.Lock 串行化，进入/退出触发钩子"""

    def __init__(self, store):
        self._store = store

    async def __aenter__(self):
        await self._store.lock.acquire()
        if self._store.on_enter:
            self._store.on_enter()
        return self

    async def __aexit__(self, *exc):
        if self._store.on_exit:
            self._store.on_exit()
        self._store.lock.release()
        return False


class _FakeConn:
    """假 asyncpg 连接：按 SQL 特征分发，模拟 DDL 语义（唯一索引/TTL/释放）"""

    def __init__(self, store):
        self._s = store

    def transaction(self):
        return _FakeTxn(self._s)

    async def execute(self, sql, *args):
        if self._s.raise_on_claim:
            raise RuntimeError("db down")
        if "set_config" in sql:
            self._s.set_config_values.append(args[0])
            return "SET"
        if "pg_advisory_xact_lock" in sql:
            return "SELECT 1"
        if sql.startswith("UPDATE") and "reason = $3" in sql:
            return self._release(sql, args[0], args[1])
        if sql.startswith("UPDATE") and "reason = 'expired'" in sql and "symbol = $1" in sql:
            return self._release_expired(args[0])
        if sql.startswith("UPDATE") and "expires_at <= NOW()" in sql:
            return self._release_expired(None)
        raise AssertionError(f"未预期的 execute SQL: {sql}")

    async def fetchrow(self, sql, *args):
        if "trade_records" in sql:
            owner = self._s.owner_by_symbol.get(args[0])
            return {"strategy": owner} if owner else None
        if "position_claims" in sql:
            unexpired = "expires_at > NOW()" in sql
            row = self._s.active(args[0], unexpired=unexpired)
            if not row:
                return None
            return {"strategy": row["strategy"], "id": row["id"]}
        raise AssertionError(f"未预期的 fetchrow SQL: {sql}")

    async def fetchval(self, sql, *args):
        # INSERT ... ON CONFLICT DO NOTHING RETURNING id：模拟部分唯一索引
        symbol, strategy, intent, ttl_minutes = args
        if self._s.active(symbol) is not None:
            return None  # 索引冲突 → DO NOTHING
        row = {
            "id": self._s.next_id,
            "symbol": symbol,
            "strategy": strategy,
            "trade_intent_id": intent,
            "claim_state": "PENDING",
            "expires_at": time.time() + int(ttl_minutes) * 60,
            "reason": None,
        }
        self._s.next_id += 1
        self._s.rows.append(row)
        return row["id"]

    def _release(self, sql, symbol, strategy):
        n = 0
        for r in self._s.rows:
            if (r["symbol"] == symbol and r["strategy"] == strategy
                    and r["claim_state"] in _ACTIVE_STATES):
                r["claim_state"] = "RELEASED"
                n += 1
        return f"UPDATE {n}"

    def _release_expired(self, symbol):
        now = time.time()
        n = 0
        for r in self._s.rows:
            if (r["claim_state"] in _ACTIVE_STATES and r["expires_at"] <= now
                    and (symbol is None or r["symbol"] == symbol)):
                r["claim_state"] = "RELEASED"
                r["reason"] = "expired"
                n += 1
        return f"UPDATE {n}"


class _FakePool:
    """假连接池：acquire 返回同一假连接（单连接即可满足串行语义）"""

    def __init__(self, store):
        self._conn = _FakeConn(store)

    @asynccontextmanager
    async def acquire(self):
        yield self._conn

    async def close(self):
        return None


def build_claims_db(store):
    """构造注入假 pool 的真实 DatabaseManager（支持占用表能力）"""
    db = DatabaseManager.__new__(DatabaseManager)
    db.pool = _FakePool(store)
    return db


@pytest.fixture
def claims_store():
    """内存占用表 store"""
    return _FakeStore()


@pytest.fixture
def claims_db(claims_store):
    """绑定假 pool 的真实 DatabaseManager"""
    return build_claims_db(claims_store)