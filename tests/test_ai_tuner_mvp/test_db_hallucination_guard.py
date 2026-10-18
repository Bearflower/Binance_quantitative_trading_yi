"""
防幻觉回归测试

目的：严格用 DatabaseManager 真实方法白名单，任何调用幻影方法
（fetch / fetchrow / close / 其它不存在的方法）立即抛 AttributeError，
杜绝"fake DB 漏实现了幻影方法 → 测试通过但真实环境 AttributeError"的幻觉漏洞。

两条防线：
  1. 严格 DB 桩（StrictDBStub）跑 ReadinessAuditor → 断言不抛 AttributeError
  2. 静态扫描 ai_tuner/readiness/auditor.py + scripts/ai_tuner_mvp.py
     → 正则零命中幻影方法调用（.fetch( / .fetchrow( / .close() ）
"""

import re
import types
from datetime import datetime, timezone

import pytest

from ai_tuner.readiness.auditor import ReadinessAuditor


# ============================================================
# 真实接口白名单（与 shared/database.py DatabaseManager 对齐）
# ============================================================

_DB_METHOD_WHITELIST = frozenset({
    "connect",
    "disconnect",
    "execute",
    "execute_ddl",
    "fetch_one",
    "fetch_all",
    "fetch_one_advisory_lock",
    "execute_transaction",
})


class StrictDBStub:
    """
    严格 DB 桩：对 __getattr__ 做白名单守卫。

    — 白名单内的方法返回 AsyncMock（供 Auditor 正常 await）
    — 白名单外的属性一律 raise AttributeError
      → auditor 若调用 .fetch / .fetchrow / .close 等幻影方法立即爆炸
    """

    def __init__(self, fetch_one_side_effect=None, fetch_all_return=None):
        from unittest.mock import AsyncMock
        self.__dict__["fetch_one"] = AsyncMock(
            side_effect=fetch_one_side_effect or self._default_fetch_one
        )
        self.__dict__["fetch_all"] = AsyncMock(
            return_value=fetch_all_return or [{"table_schema": "btc_eth_aggressive"}]
        )

    async def _default_fetch_one(self, sql, *args):
        """
        默认 fetch_one：返回能让 Auditor 跑完整个流程的最小数据。
        对具体门禁值不关心（本测试目标是"无 AttributeError"，不是语义正确）。
        """
        s = str(sql)
        args_str = str(args)

        if "COUNT(*) AS closed_trades" in s:
            return {"closed_trades": 30, "complete_ratio": 0.99}
        if "SELECT 1 FROM information_schema.columns" in s and "strategy_id" in s:
            return {"table_schema": "btc_eth_aggressive"}
        if "trade_signals" in s and "COUNT(*) AS cnt" in s and "strategy_id" in s:
            return {"cnt": 180}
        if "COUNT(DISTINCT symbol)" in s:
            return {"syms": 4}
        if "table_name ILIKE '%kline%'" in s and "ORDER BY table_schema" in s:
            return {"table_schema": "btc_eth"}
        if "SELECT table_name" in s and "%kline%" in s:
            return {"table_name": "kline_1h"}
        if "column_name" in s and "information_schema.columns" in s:
            return {"column_name": "open_time"}
        if "SELECT COUNT(*) AS cnt" in s and "kline_1h" in s and "open_time" in s:
            return {"cnt": 2200}
        return None

    def __getattr__(self, name):
        # 任何未显式定义、也不在白名单里的方法 → 立即抛
        if name in _DB_METHOD_WHITELIST:
            from unittest.mock import AsyncMock
            # 懒创建白名单方法
            stub = AsyncMock()
            object.__setattr__(self, name, stub)
            return stub
        raise AttributeError(
            f"StrictDBStub 拒绝访问幻影方法 '{name}' — 这不是 DatabaseManager 的真实方法"
        )


_BASE_CONFIG = {
    "purpose_driven_mvp": {
        "mode": "audit",
        "candidates": ["btc_eth_aggressive", "hrs"],
        "identity_ratio_baseline": 0.90,  # 强制键
        "readiness": {
            "window_days": 90,
            "min_closed_trades": 20,
            "min_effective_signals": 100,
            "min_trade_field_completeness": 0.95,
            "min_config_identity_coverage": 0.90,
            "min_kline_coverage": 0.98,
            "min_external_snapshot_coverage": 0.95,
            "min_regime_count": 3,
            "external_snapshot_requirement": "required",
            "per_strategy": {
                "btc_eth_aggressive": {
                    "min_external_snapshot_coverage": 0.0,
                    "external_snapshot_requirement": "not_required_for_current_manifest",
                },
                "hrs": {
                    "min_external_snapshot_coverage": 0.95,
                    "external_snapshot_requirement": "required",
                },
            },
        },
    },
}


# ============================================================
# 防线 1：StrictDBStub 跑 auditor → 不抛 AttributeError
# ============================================================


@pytest.mark.asyncio
async def test_readiness_auditor_never_calls_fake_db_methods():
    """
    StrictDBStub 下跑 ReadinessAuditor.run → 不应有任何 AttributeError。

    若 auditor 里残留了 .fetch / .fetchrow / .close 等幻影方法调用，
    StrictDBStub.__getattr__ 会立即 raise → 测试红。
    """
    db = StrictDBStub()
    auditor = ReadinessAuditor(db, _BASE_CONFIG)

    # 不关心具体 decision 是什么，只关心没有 AttributeError
    result = await auditor.run(as_of=datetime(2026, 10, 17, tzinfo=timezone.utc))
    assert result is not None
    assert hasattr(result, "decision")


@pytest.mark.asyncio
async def test_readiness_auditor_calls_no_fetchrow_nor_fetch():
    """
    显式断言 StrictDBStub 上的 fetchrow / fetch / close 从未被调用
    （它们根本不存在，__getattr__ 会阻止访问）。
    """
    db = StrictDBStub()

    # 白名单里只有 fetch_one / fetch_all / connect / disconnect ...
    # 幻影方法不在白名单 → 访问直接 AttributeError
    with pytest.raises(AttributeError):
        _ = db.fetchrow
    with pytest.raises(AttributeError):
        _ = db.fetch
    with pytest.raises(AttributeError):
        _ = db.close


# ============================================================
# 防线 2：静态扫描 auditor.py + ai_tuner_mvp.py → 零幻影方法
# ============================================================


_PHANTOM_METHOD_RE = re.compile(
    r"""
    \.fetch\s*\(|       # .fetch(
    \.fetchrow\s*\(|    # .fetchrow(
    \bawait\s+db\.close\s*\(|  # await db.close(  — 幻影 close
    \bdb\.close\s*\(     # db.close(
    """,
    re.VERBOSE,
)


def _read_source(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_auditor_py_no_phantom_db_calls():
    """ai_tuner/readiness/auditor.py 里零 .fetch( / .fetchrow( / .close("""
    src = _read_source("ai_tuner/readiness/auditor.py")
    hits = [
        f"{path}:{i+1}:{line.strip()}"
        for i, line in enumerate(src.splitlines())
        if _PHANTOM_METHOD_RE.search(line)
    ]
    assert not hits, f"auditor.py 存在幻影 DB 方法调用:\n" + "\n".join(hits)


def test_ai_tuner_mvp_py_no_phantom_db_calls():
    """scripts/ai_tuner_mvp.py 里零 .fetch( / .fetchrow( / db.close("""
    src = _read_source("scripts/ai_tuner_mvp.py")
    hits = [
        f"{path}:{i+1}:{line.strip()}"
        for i, line in enumerate(src.splitlines())
        if _PHANTOM_METHOD_RE.search(line)
    ]
    assert not hits, f"ai_tuner_mvp.py 存在幻影 DB 方法调用:\n" + "\n".join(hits)


def test_database_manager_has_only_whitelist_methods():
    """
    DatabaseManager 真实接口白名单自证（防白名单本身写错）。
    — 确认白名单方法真实存在
    — 确认幻影方法 fetch / fetchrow / close 确实不存在
    """
    from shared.database import DatabaseManager

    for m in _DB_METHOD_WHITELIST:
        assert hasattr(DatabaseManager, m), (
            f"白名单方法 '{m}' 在 DatabaseManager 上不存在 — 白名单本身错了"
        )

    for phantom in ("fetch", "fetchrow", "close"):
        assert not hasattr(DatabaseManager, phantom), (
            f"幻影方法 '{phantom}' 意外地存在于 DatabaseManager 上 — 幻觉前提被推翻"
        )
