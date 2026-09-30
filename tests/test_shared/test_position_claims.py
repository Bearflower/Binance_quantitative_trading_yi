"""
R07 跨策略开仓预占互斥 · 单元测试（T09 claim_position_atomic + T10 占用生命周期）

覆盖 R07-AC1..AC6：
  AC1 并发占位 -> 至多一个 claimed=True
  AC2 A 占位阻塞 B；A 释放后 B 可再占
  AC3 TTL 过期后可再占（清理/内部过期释放）
  AC4 下单在锁外（断言锁持有期未发生下单调用）
  AC6 冲突走「已被持有」返回 claimed=False，不抛异常

用假连接/假 pool 模拟部分唯一索引与 advisory lock（禁止连生产库）。
"""
import asyncio
import zlib

import pytest
from unittest.mock import MagicMock

from shared.position_ownership import (
    try_claim_symbol,
    release_claim,
    cleanup_expired_claims,
)

_A = "MTPCS策略"
_B = "MTPCS激进策略"
_COMP = {_A: [_B], _B: [_A]}  # 各自的对家名单
_SYM = "SOLUSDT"


def _lock_key(symbol):
    return zlib.crc32(symbol.encode()) & 0xFFFFFFFF


async def _claim(db, name, symbol=_SYM, **kw):
    return await try_claim_symbol(
        db, symbol, name, competing_record_names=_COMP[name], **kw
    )


# ============================================================
# T09：claim_position_atomic 原子占位
# ============================================================
class TestClaimPositionAtomic:
    async def test_returns_expected_keys(self, claims_db):
        res = await claims_db.claim_position_atomic(_lock_key(_SYM), _SYM, _A, "intent-1", 30)
        assert set(res.keys()) == {"claimed", "owner", "claim_id"}
        assert res["claimed"] is True and res["owner"] == _A and res["claim_id"] == 1

    async def test_same_strategy_idempotent_reuse(self, claims_db):
        """同策略重复占位 -> 幂等复用，返回既有 claim_id，不新增行"""
        first = await claims_db.claim_position_atomic(_lock_key(_SYM), _SYM, _A, "i1", 30)
        again = await claims_db.claim_position_atomic(_lock_key(_SYM), _SYM, _A, "i2", 30)
        assert again["claimed"] is True
        assert again["claim_id"] == first["claim_id"]

    async def test_competitor_blocked(self, claims_db):
        """对家占位 -> claimed=False 且返回占用方，不抛异常（AC6）"""
        await claims_db.claim_position_atomic(_lock_key(_SYM), _SYM, _A, "i1", 30)
        res = await claims_db.claim_position_atomic(
            _lock_key(_SYM), _SYM, _B, "i2", 30, competing_names=[_A]
        )
        assert res["claimed"] is False and res["owner"] == _A

    async def test_empty_competing_blocks_any_other(self, claims_db):
        """competing_names 为空 -> 与任意其他策略互斥"""
        await claims_db.claim_position_atomic(_lock_key(_SYM), _SYM, _A, "i1", 30)
        res = await claims_db.claim_position_atomic(_lock_key(_SYM), _SYM, "网格交易策略", "i2", 30)
        assert res["claimed"] is False and res["owner"] == _A

    async def test_non_competitor_falls_through_to_index_conflict(self, claims_db):
        """非对家已持有 -> 尝试插入被部分唯一索引拒绝 -> claimed=False（暴露冲突方）"""
        await claims_db.claim_position_atomic(_lock_key(_SYM), _SYM, "网格交易策略", "i1", 30)
        res = await claims_db.claim_position_atomic(
            _lock_key(_SYM), _SYM, _A, "i2", 30, competing_names=[_B]
        )
        assert res["claimed"] is False and res["owner"] == "网格交易策略"

    async def test_lock_timeout_configurable(self, claims_db, claims_store):
        """lock_timeout_seconds 落到事务级 set_config（禁止硬编码）"""
        await claims_db.claim_position_atomic(
            _lock_key(_SYM), _SYM, _A, "i1", 30, lock_timeout_seconds=7
        )
        assert claims_store.set_config_values == ["7000ms"]

    async def test_ttl_release_expired_before_insert(self, claims_db, claims_store):
        """先释放过期有效占用，再允许重新占位（索引不区分 expires_at）"""
        await claims_db.claim_position_atomic(_lock_key(_SYM), _SYM, _A, "i1", 0)
        res = await claims_db.claim_position_atomic(
            _lock_key(_SYM), _SYM, _B, "i2", 30, competing_names=[_A]
        )
        assert res["claimed"] is True and res["owner"] == _B
        assert claims_store.rows[0]["claim_state"] == "RELEASED"


# ============================================================
# T10：try_claim_symbol / release_claim / cleanup_expired_claims
# ============================================================
class TestTryClaimSymbol:
    async def test_concurrent_only_one_claimed(self, claims_db):
        """AC1：两策略并发占位 -> 至多一个 claimed=True"""
        r1, r2 = await asyncio.gather(_claim(claims_db, _A), _claim(claims_db, _B))
        assert [r1["claimed"], r2["claimed"]].count(True) == 1

    async def test_block_then_release_then_reclaim(self, claims_db):
        """AC2：A 占位阻塞 B；A 释放后 B 可再占"""
        ra = await _claim(claims_db, _A)
        rb = await _claim(claims_db, _B)
        assert ra["claimed"] is True and rb["claimed"] is False and rb["owner"] == _A
        assert await release_claim(claims_db, _SYM, _A) is True
        rb2 = await _claim(claims_db, _B)
        assert rb2["claimed"] is True

    async def test_ttl_expired_reclaim(self, claims_db):
        """AC3：TTL 过期后可再占（内部过期释放，不永久阻塞）"""
        assert (await _claim(claims_db, _A, ttl_minutes=0))["claimed"] is True
        assert (await _claim(claims_db, _B))["claimed"] is True

    async def test_cleanup_expired_claims_marks_released_and_keeps_rows(self, claims_db, claims_store):
        """AC3/清理：过期占用置 RELEASED（保留行不删），返回清理条数"""
        await _claim(claims_db, _A, ttl_minutes=0)
        count = await cleanup_expired_claims(claims_db)
        assert count == 1
        assert claims_store.rows[0]["claim_state"] == "RELEASED"
        assert len(claims_store.rows) == 1  # 保留行，便于审计

    async def test_cleanup_no_expired_returns_zero(self, claims_db):
        await _claim(claims_db, _A, ttl_minutes=30)
        assert await cleanup_expired_claims(claims_db) == 0

    async def test_release_only_own_claim(self, claims_db):
        """释放只针对本策略，不误放对家"""
        await _claim(claims_db, _A)
        assert await release_claim(claims_db, _SYM, _B) is False
        assert await release_claim(claims_db, _SYM, _A) is True

    async def test_write_failure_conservative_not_blocked_open(self, claims_db, claims_store):
        """AC6/降级：占用写入异常 -> claimed=False（保守不双开），不抛异常"""
        claims_store.raise_on_claim = True
        res = await _claim(claims_db, _A)
        assert res["claimed"] is False and res["owner"] is None

    async def test_enabled_false_degrades_to_trade_records(self, claims_db, claims_store):
        """开关关闭 -> 回到既有 trade_records 互斥，不写占用行"""
        claims_store.owner_by_symbol[_SYM] = _B
        res = await _claim(claims_db, _A, enabled=False)
        assert res["claimed"] is False
        assert claims_store.rows == []  # 未启用占用表
        # 无权威归属 -> 放行
        claims_store.owner_by_symbol.clear()
        res2 = await _claim(claims_db, _A, enabled=False)
        assert res2["claimed"] is True

    async def test_authority_conflict_releases_claim(self, claims_db, claims_store):
        """占位成功但存在对家权威归属 -> 释放占用并拒绝（不覆盖既有持仓）"""
        claims_store.owner_by_symbol[_SYM] = _B
        res = await _claim(claims_db, _A)
        assert res["claimed"] is False
        assert claims_store.rows[0]["claim_state"] == "RELEASED"

    async def test_own_authority_not_blocked(self, claims_db, claims_store):
        """归属为本策略（加仓场景）-> 放行"""
        claims_store.owner_by_symbol[_SYM] = _A
        assert (await _claim(claims_db, _A))["claimed"] is True

    async def test_non_competitor_holder_does_not_block(self, claims_db, claims_store):
        """非对家持有占用 -> 不构成互斥（与 competing 语义一致），放行"""
        await claims_db.claim_position_atomic(_lock_key(_SYM), _SYM, "网格交易策略", "i1", 30)
        res = await _claim(claims_db, _A)
        assert res["claimed"] is True


class TestAc4OrderOutsideLock:
    async def test_order_never_called_while_lock_held(self, claims_db, claims_store):
        """AC4：下单必须在锁外执行（锁持有期间不得发生下单调用）"""
        order_spy = MagicMock()
        events = []

        def _on_enter():
            events.append("lock_enter")
            order_spy.assert_not_called()  # 锁内不得下单

        def _on_exit():
            events.append("lock_exit")

        claims_store.on_enter = _on_enter
        claims_store.on_exit = _on_exit

        claim = await _claim(claims_db, _A)
        assert claim["claimed"] is True
        order_spy("place_order")  # 释放锁之后才下单
        order_spy.assert_called_once()
        assert events[-1] == "lock_exit"
        assert events.count("lock_enter") == events.count("lock_exit")