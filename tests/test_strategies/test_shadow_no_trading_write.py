"""影子不写 trading.* 守卫测试（spec §13.1 / 架构方案 §4.4）

两层断言：
  1. 静态：repository.py 全部 SQL 字符串硬性 research. 前缀，不含 trading.
  2. 动态：mock db.execute，逐行断言 INSERT/UPDATE 均指向 research.* schema

覆盖所有 research schema 写入路径（shadow_runs / shadow_decision_events）。
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from strategies.btc_eth.shadow.repository import (
    ShadowRepository,
    get_all_repository_sql,
)


# ---------------------------------------------------------------------------
# 静态核对：所有 SQL 必须以 research. 前缀表名，禁止 trading.
# ---------------------------------------------------------------------------
class TestRepositorySqlStatic:
    """repository.py 全部 SQL 字符串静态检查"""

    def test_all_sql_have_research_prefix(self):
        """所有 SQL 必须引用 research.* 表"""
        all_sql = get_all_repository_sql()
        assert len(all_sql) >= 5, f"repository.py 应至少有 5 条 SQL，实际 {len(all_sql)}"
        for sql in all_sql:
            assert "research.shadow_runs" in sql or "research.shadow_decision_events" in sql, (
                f"SQL 未引用 research.* 表：{sql[:80]}..."
            )

    def test_no_trading_schema_in_any_sql(self):
        """禁止任何 SQL 字符串出现 trading. 的 INSERT/UPDATE/DELETE"""
        forbidden_patterns = [
            "trading.",
        ]
        all_sql = get_all_repository_sql()
        for sql in all_sql:
            sql_upper = sql.upper()
            # 检查 INSERT/UPDATE/DELETE 是否指向 trading schema
            if any(kw in sql_upper for kw in ("INSERT", "UPDATE", "DELETE")):
                for forbidden in forbidden_patterns:
                    assert forbidden not in sql_upper, (
                        f"SQL 含禁止的 {forbidden} schema 引用：{sql[:120]}..."
                    )


# ---------------------------------------------------------------------------
# 动态核对：mock DatabaseManager.execute，断言无 trading.* 写入
# ---------------------------------------------------------------------------
class TestRepositoryNoTradingWrite:
    """repository 动态写入路径不碰 trading.*"""

    @pytest.fixture
    def mock_db(self):
        """返回一个 execute/fetch_one 均为 AsyncMock 的 db"""
        db = AsyncMock()
        db.execute = AsyncMock(return_value="OK")
        db.fetch_one = AsyncMock(return_value=None)
        db.fetch_all = AsyncMock(return_value=[])
        return db

    def _assert_no_trading_write(self, mock_db):
        """遍历所有 execute 调用，断言 SQL 不含 trading. 写入"""
        for call in mock_db.execute.await_args_list:
            args, _kwargs = call
            sql = args[0] if args else ""
            # 必须以 research. 引用表
            assert "research." in sql, f"execute SQL 未引用 research. schema：{sql[:120]}..."
            # 禁止 trading. 出现
            assert "trading." not in sql.lower(), (
                f"execute SQL 含禁止的 trading. schema 引用：{sql[:120]}..."
            )

    @pytest.mark.asyncio
    async def test_upsert_shadow_run_no_trading(self, mock_db):
        repo = ShadowRepository(mock_db)
        ok = await repo.upsert_shadow_run(
            run_id="test-run",
            instance_id="mtcps_xau_shadow",
            symbol="XAUUSDT",
            started_at=None,
            initial_virtual_equity_usdt=300,
        )
        assert ok is True
        self._assert_no_trading_write(mock_db)

    @pytest.mark.asyncio
    async def test_close_shadow_run_no_trading(self, mock_db):
        repo = ShadowRepository(mock_db)
        ok = await repo.close_shadow_run(run_id="test-run", status="closed")
        assert ok is True
        self._assert_no_trading_write(mock_db)

    @pytest.mark.asyncio
    async def test_insert_decision_event_no_trading(self, mock_db):
        repo = ShadowRepository(mock_db)
        event = {
            "run_id": "test-run",
            "profile_id": "xau_original",
            "symbol": "XAUUSDT",
            "scheduled_at": None,
            "evaluated_at": None,
            "decision": "candidate",
            "direction": "LONG",
            "grade": "S",
            "score": 90.5,
            "reference_price": 1234.5,
            "intended_order_type": "LIMIT",
            "intended_entry_price": 1234.5,
            "stop_loss_rule": {"kind": "stop_loss", "atr_multiplier": 2.5},
            "tp1_rule": {"kind": "tp1", "atr_multiplier": 6.0},
            "tp2_rule": {"kind": "tp2", "atr_multiplier": 9.0},
            "trailing_rule": {"enabled": True},
            "time_stop_rule": {"max_holding_hours": 96},
            "feature_snapshot": {"market_state": "STRONG_TREND", "atr": 5.0},
            "reason_codes": [],
            "data_quality_reason": None,
            "config_hash": "abc123",
            "git_commit": "def456",
            "source_kline_close_times": {"1h": 1234567890000, "4h": 1234560000000},
        }
        ok = await repo.insert_decision_event(event)
        assert ok is True
        self._assert_no_trading_write(mock_db)

    @pytest.mark.asyncio
    async def test_write_failure_degrades_silently(self, mock_db):
        """写 DB 异常被捕获降级，不向上抛"""
        mock_db.execute.side_effect = RuntimeError("连接断了")
        repo = ShadowRepository(mock_db)
        # upsert_shadow_run 异常 → 返回 False，不抛
        ok = await repo.upsert_shadow_run(
            run_id="test-run",
            instance_id="mtcps_xau_shadow",
            symbol="XAUUSDT",
            started_at=None,
            initial_virtual_equity_usdt=300,
        )
        assert ok is False

        # insert_decision_event 异常 → 返回 False，不抛
        event = {
            "run_id": "test-run", "profile_id": "xau_original", "symbol": "XAUUSDT",
            "scheduled_at": None, "evaluated_at": None, "decision": "candidate",
            "direction": "LONG", "grade": "S", "score": 90.5,
            "config_hash": "abc", "source_kline_close_times": {},
        }
        ok = await repo.insert_decision_event(event)
        assert ok is False


# ---------------------------------------------------------------------------
# 禁止 trading.* schema 级验证：repository 不 import trading.* 相关模块
# ---------------------------------------------------------------------------
class TestRepositoryNoTradingImport:
    """repository.py 不执行任何 trading.* schema 写入"""

    def test_all_executable_sql_no_trading_schema(self):
        """get_all_repository_sql() 每条 SQL 含 research. 表名且不含 trading. 引用"""
        all_sql = get_all_repository_sql()
        for sql in all_sql:
            assert "research.shadow_" in sql, (
                f"SQL 未引用 research.shadow_ 表：{sql[:120]}..."
            )
            assert "trading." not in sql, (
                f"SQL 含 trading. schema 引用：{sql[:120]}..."
            )


# ---------------------------------------------------------------------------
# 读路径异常：DB 故障必须向上抛（阻断项 2 —— 读契约 v2）
# ---------------------------------------------------------------------------
class TestRepositoryReadFailureRaises:
    """读查询 DB 故障必须向上抛出，绝不能静默降级返回空数据"""

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value="OK")
        db.fetch_one = AsyncMock(return_value=None)
        return db

    @pytest.mark.asyncio
    async def test_find_active_run_raises_on_db_failure(self, mock_db):
        """读路径 DB 异常必须向上抛出（不返回 None）"""
        mock_db.fetch_one.side_effect = RuntimeError("连接断了")
        repo = ShadowRepository(mock_db)
        with pytest.raises(RuntimeError, match="连接断了"):
            await repo.find_active_run(instance_id="mtcps_xau_shadow")

    @pytest.mark.asyncio
    async def test_find_run_by_id_raises_on_db_failure(self, mock_db):
        """按 ID 查询 DB 异常必须向上抛出（不返回 None）"""
        mock_db.fetch_one.side_effect = RuntimeError("连接断了")
        repo = ShadowRepository(mock_db)
        with pytest.raises(RuntimeError, match="连接断了"):
            await repo.find_run_by_id(run_id="r-1")

    @pytest.mark.asyncio
    async def test_find_events_by_run_raises_on_db_failure(self, mock_db):
        """导出用查询 DB 异常必须向上抛出（不返回空 []）"""
        mock_db.fetch_all.side_effect = RuntimeError("连接断了")
        repo = ShadowRepository(mock_db)
        with pytest.raises(RuntimeError, match="连接断了"):
            await repo.find_events_by_run(run_id="r-1")


# ---------------------------------------------------------------------------
# 序列化辅助：_json_or_none 契约（dict/list → JSON 字符串；标量 → str；None → None）
# ---------------------------------------------------------------------------
class TestJsonOrNone:
    def test_none_stays_none(self):
        from strategies.btc_eth.shadow.repository import _json_or_none
        assert _json_or_none(None) is None

    def test_dict_list_serialize_to_json(self):
        import json as _json
        from strategies.btc_eth.shadow.repository import _json_or_none
        d = _json_or_none({"b": 1, "a": "x"})
        assert _json.loads(d) == {"b": 1, "a": "x"}
        # sort_keys=True → 'a' 排在 'b' 前
        assert d.index('"a"') < d.index('"b"')
        lst = _json_or_none([1, 2])
        assert _json.loads(lst) == [1, 2]

    def test_scalar_coerced_to_str(self):
        from strategies.btc_eth.shadow.repository import _json_or_none
        assert _json_or_none(123) == "123"
        assert _json_or_none(1.5) == "1.5"
