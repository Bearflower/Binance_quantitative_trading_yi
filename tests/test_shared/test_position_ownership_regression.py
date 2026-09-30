"""
R07 非回归测试：占用表接入后，既有归属/互斥语义不变（AC5）

重点：
  - is_symbol_owned_by_other 双重判定（占用表 OR trade_records）接入正确
  - enabled=False 时回到纯 trade_records 判定（灰度回退）
  - 既有 mock DatabaseManager（无占用表能力）行为与改造前一致
  - ownership 配置读取新增键的默认值/覆盖

既有 45 项用例见 tests/test_position_ownership.py（本文件不重复，仅做 R07 差异化回归）。
"""
import zlib

import pytest
from unittest.mock import AsyncMock, MagicMock

from shared.position_ownership import (
    is_symbol_owned_by_other,
    load_ownership_config,
    _supports_claims,
)

_A = "MTPCS策略"
_B = "MTPCS激进策略"
_SYM = "SOLUSDT"


# ============================================================
# 1. 双重判定：占用表 OR trade_records
# ============================================================
class TestDualOwnershipCheck:
    async def test_authority_owner_blocks(self, claims_db, claims_store):
        """trade_records 权威归属为对家 -> 拦截（未回归）"""
        claims_store.owner_by_symbol[_SYM] = _B
        assert await is_symbol_owned_by_other(claims_db, _SYM, _A, [_B]) is True

    async def test_claim_owner_blocks_without_authority(self, claims_db):
        """仅有占用表预占（无 trade_records 记录）-> 也拦截（R07 新增互斥）"""
        await claims_db.claim_position_atomic(zlib.crc32(_SYM.encode()) & 0xFFFFFFFF, _SYM, _B, "i", 30)
        assert await is_symbol_owned_by_other(claims_db, _SYM, _A, [_B]) is True

    async def test_own_claim_not_blocked(self, claims_db):
        """占用为本策略 -> 放行（加仓/自身重复占位）"""
        await claims_db.claim_position_atomic(zlib.crc32(_SYM.encode()) & 0xFFFFFFFF, _SYM, _A, "i", 30)
        assert await is_symbol_owned_by_other(claims_db, _SYM, _A, [_B]) is False

    async def test_non_competitor_claim_not_blocked(self, claims_db):
        """占用方非对家 -> 不拦截（competing 精确互斥）"""
        await claims_db.claim_position_atomic(
            zlib.crc32(_SYM.encode()) & 0xFFFFFFFF, _SYM, "网格交易策略", "i", 30
        )
        assert await is_symbol_owned_by_other(claims_db, _SYM, _A, [_B]) is False

    async def test_no_owner_not_blocked(self, claims_db):
        """无任何归属/占用 -> 放行"""
        assert await is_symbol_owned_by_other(claims_db, _SYM, _A, [_B]) is False


# ============================================================
# 2. enabled=False 灰度回退：忽略占用表
# ============================================================
class TestEnabledGate:
    async def test_disabled_ignores_claim(self, claims_db):
        """关闭开关 -> 忽略占用表预占（回到既有 trade_records 判定）"""
        await claims_db.claim_position_atomic(zlib.crc32(_SYM.encode()) & 0xFFFFFFFF, _SYM, _B, "i", 30)
        assert await is_symbol_owned_by_other(claims_db, _SYM, _A, [_B], enabled=False) is False

    async def test_disabled_still_checks_authority(self, claims_db, claims_store):
        """关闭开关 -> 仍做 trade_records 权威判定"""
        claims_store.owner_by_symbol[_SYM] = _B
        assert await is_symbol_owned_by_other(claims_db, _SYM, _A, [_B], enabled=False) is True


# ============================================================
# 3. 既有 mock DatabaseManager（无占用表能力）行为不变
# ============================================================
class TestLegacyMockUnchanged:
    def test_mock_lacks_claim_capability(self):
        """能力探测：普通 mock 不具备占用表能力 -> 只走 trade_records"""
        assert _supports_claims(MagicMock()) is False

    async def test_mock_owner_intercepts(self):
        db = MagicMock()
        db.fetch_one_advisory_lock = AsyncMock(return_value={"strategy": _B})
        db.fetch_one = AsyncMock(return_value=None)
        assert await is_symbol_owned_by_other(db, _SYM, _A) is True
        # 无占用表能力 -> 只查 trade_records 一次（不误触占用查询）
        db.fetch_one.assert_not_awaited()

    async def test_mock_db_error_not_blocked(self):
        """既有降级：DB 异常 -> 返回 False（不回归）"""
        db = MagicMock()
        db.fetch_one_advisory_lock = AsyncMock(side_effect=Exception("db down"))
        db.fetch_one = AsyncMock(return_value=None)
        assert await is_symbol_owned_by_other(db, _SYM, _A) is False


# ============================================================
# 4. ownership 配置新增键默认值/覆盖
# ============================================================
class TestOwnershipConfigClaimKeys:
    def test_defaults(self):
        out = load_ownership_config({"strategy": {"record_name": _A}})
        assert out["enabled"] is True
        assert out["claim_ttl_minutes"] == 30
        assert out["claim_cleanup_interval_minutes"] == 10
        assert out["lock_timeout_seconds"] == 5

    def test_overrides(self):
        cfg = {
            "strategy": {"record_name": _A},
            "ownership": {
                "enabled": "false",
                "claim_ttl_minutes": 15,
                "claim_cleanup_interval_minutes": 3,
                "lock_timeout_seconds": 2.5,
            },
        }
        out = load_ownership_config(cfg)
        assert out["enabled"] is False
        assert out["claim_ttl_minutes"] == 15
        assert out["claim_cleanup_interval_minutes"] == 3
        assert out["lock_timeout_seconds"] == 2.5

    def test_invalid_values_fall_back(self):
        """非法配置值 -> 取默认值，不抛异常"""
        cfg = {"strategy": {"record_name": _A}, "ownership": {"claim_ttl_minutes": "abc"}}
        out = load_ownership_config(cfg)
        assert out["claim_ttl_minutes"] == 30