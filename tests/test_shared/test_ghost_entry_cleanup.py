"""幽灵入场记录互斥修复专项测试（方案 9.6）

覆盖验收标准：
  AC1  new_coin 归属判定：record_name="new_coin" 后自家 NEW 记录不再判为"其他策略"
  AC2  CANCELED/EXPIRED/CLOSED 终态记录不参与互斥归属（SQL + 行为）
  AC3  撤单同步标记：
        cancel 响应 CANCELED+executedQty=0 → mark_entry_canceled 被调用
        cancel 响应 FILLED → 不调用
        cancel 响应 executedQty>0 → 不调用 + warning
        cancel 抛 -2011 → get_order 复查后决定（CANCELED/EXPIRED→标记，FILLED/失败→不标记）
  AC5  清理脚本 classify_entry 纯函数四分类
  AC9  release_claim(reason='position_closed') 标记 CLOSED：
        成功路径 + db 抛异常不影响主流程
  D2   release_claim 扩 reason 落终态：open_failed / open_exception / micro_zeroed
       → 标 CANCELED（终态，被互斥 SQL 排除）；position_closed → CLOSED（不回归）
  D3   终态标记命中 0 行 → warning（可观测）

全部 fake/mock，禁止真实网络。
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from shared.binance_api import BinanceClient, BinanceAPIError  # noqa: E402
from shared.position_ownership import (  # noqa: E402
    _MARK_ENTRY_CANCELED_SQL,
    _MARK_ENTRY_CLOSED_SQL,
    _open_entry_sql,
    release_claim,
)
from shared.trade_logger import TradeLogger  # noqa: E402
from scripts.fix_ghost_entry_records import build_candidate_query, classify_entry  # noqa: E402


# ============================================================
# AC2：终态排除 SQL + 行为
# ============================================================
class TestAC2TerminalStatusesExcluded:
    """_open_entry_sql 两分支都必须排除 CANCELED/EXPIRED/CLOSED 终态"""

    def test_both_sql_versions_exclude_terminal(self):
        for sql in (_open_entry_sql(True), _open_entry_sql(False)):
            assert "CANCELED" in sql
            assert "EXPIRED" in sql
            assert "CLOSED" in sql
            assert "status NOT IN" in sql

    @pytest.mark.asyncio
    async def test_canceled_record_not_blocking_mutex(self):
        """AC2 行为：DB 只有 CANCELED 终态 NEW 记录 → is_symbol_owned_by_other 不拦截"""
        from shared.position_ownership import is_symbol_owned_by_other

        # advisory lock 返回 CANCELED 行（但 SQL 已排除，所以 advisory_fetcher 不返回）
        db = MagicMock()
        db.fetch_one_advisory_lock = AsyncMock(return_value=None)  # 无归属 → 放行
        db_manager = MagicMock()
        db_manager.fetch_one = AsyncMock(return_value=None)
        type(db_manager).supports_position_claims = MagicMock(return_value=False)

        blocked = await is_symbol_owned_by_other(
            db_manager, "SNPSUSDT", "new_coin", competing_record_names=["btc_eth"]
        )
        assert blocked is False


# ============================================================
# TradeLogger.mark_entry_canceled 方法测试
# ============================================================
class TestMarkEntryCanceled:
    """TradeLogger.mark_entry_canceled：仅 NEW→CANCELED 降级，幂等安全"""

    @pytest.mark.asyncio
    async def test_new_record_marked_canceled(self):
        """有 NEW 记录命中 → 返回 True，执行 UPDATE"""
        db = MagicMock()
        db.execute = AsyncMock(return_value="UPDATE 1")
        tl = TradeLogger(db, "new_coin")
        ok = await tl.mark_entry_canceled("12345", "BTCUSDT")
        assert ok is True
        db.execute.assert_awaited_once()
        args = db.execute.call_args[0]
        assert "status = 'CANCELED'" in args[0]
        assert "status = 'NEW'" in args[0]
        assert args[1:] == ("12345", "BTCUSDT")

    async def test_already_canceled_noop(self):
        """已是 CANCELED → WHERE 条件不命中 → 返回 False，不抛异常"""
        db = MagicMock()
        db.execute = AsyncMock(return_value="UPDATE 0")
        tl = TradeLogger(db, "new_coin")
        ok = await tl.mark_entry_canceled("12345", "BTCUSDT")
        assert ok is False

    async def test_missing_params_return_false(self):
        """order_id/symbol 缺失 → 返回 False，不调 db"""
        db = MagicMock()
        db.execute = AsyncMock()
        tl = TradeLogger(db, "new_coin")
        assert await tl.mark_entry_canceled("", "BTCUSDT") is False
        assert await tl.mark_entry_canceled("12345", "") is False
        db.execute.assert_not_awaited()

    async def test_db_exception_swallowed(self):
        """db.execute 抛异常 → 返回 False，不向上传播"""
        db = MagicMock()
        db.execute = AsyncMock(side_effect=Exception("db down"))
        tl = TradeLogger(db, "new_coin")
        ok = await tl.mark_entry_canceled("12345", "BTCUSDT")
        assert ok is False


# ============================================================
# AC3：cancel_order 撤单后按判据调用 mark_entry_canceled
# ============================================================
class TestAC3CancelOrderMarksEntry:
    """cancel_order 在撤单成功后按 status + executedQty 判据标记 CANCELED"""

    @pytest.fixture
    def fake_trade_logger(self):
        """模拟 TradeLogger：只验证 mark_entry_canceled 调用，不真落库"""
        tl = MagicMock()
        tl.mark_entry_canceled = AsyncMock(return_value=True)
        return tl

    @pytest.fixture
    def client(self, fake_trade_logger):
        """构造 BinanceClient（不真连交易所，直接 mock _request）"""
        c = BinanceClient(
            api_key="test_api_key_12345",
            api_secret="test_api_secret_67890",
            testnet=True,
        )
        c.trade_logger = fake_trade_logger
        return c

    async def test_canceled_qty0_marks_entry(self, client, fake_trade_logger):
        """AC3 正向：响应 status='CANCELED' AND executedQty==0 → 标记"""
        client._request = AsyncMock(return_value={
            "status": "CANCELED", "executedQty": "0", "orderId": 12345,
        })
        await client.cancel_order("BTCUSDT", order_id="12345")
        fake_trade_logger.mark_entry_canceled.assert_awaited_once_with("12345", "BTCUSDT")

    async def test_filled_does_not_mark(self, client, fake_trade_logger):
        """AC3 反例：响应 status='FILLED'（撤销瞬间成交）→ 不标记"""
        client._request = AsyncMock(return_value={
            "status": "FILLED", "executedQty": "1.0", "orderId": 12345,
        })
        await client.cancel_order("BTCUSDT", order_id="12345")
        fake_trade_logger.mark_entry_canceled.assert_not_awaited()

    async def test_partial_fill_qty_gt0_does_not_mark(self, client, fake_trade_logger):
        """AC3 反例：响应 executedQty>0（部分成交后撤）→ 不标记"""
        client._request = AsyncMock(return_value={
            "status": "CANCELED", "executedQty": "0.3", "orderId": 12345,
        })
        await client.cancel_order("BTCUSDT", order_id="12345")
        fake_trade_logger.mark_entry_canceled.assert_not_awaited()

    async def test_no_trade_logger_skips_gracefully(self, client):
        """trade_logger=None 时不调任何标记方法，cancel_order 正常返回"""
        client.trade_logger = None
        client._request = AsyncMock(return_value={
            "status": "CANCELED", "executedQty": "0",
        })
        result = await client.cancel_order("BTCUSDT", order_id="12345")
        assert result["status"] == "CANCELED"

    # ---------- -2011 分支 ----------

    async def test_minus_2011_get_order_canceled_marks(self, client, fake_trade_logger):
        """cancel -2011 → get_order 显示 CANCELED → 标记"""
        client._request = AsyncMock(side_effect=BinanceAPIError(-2011, "Order does not exist"))
        client.get_order = AsyncMock(return_value={"status": "CANCELED", "orderId": 12345})
        with pytest.raises(BinanceAPIError) as exc_info:
            await client.cancel_order("BTCUSDT", order_id="12345")
        assert exc_info.value.code == -2011  # 异常仍向上传播
        fake_trade_logger.mark_entry_canceled.assert_awaited_once_with("12345", "BTCUSDT")

    async def test_minus_2011_get_order_filled_no_mark(self, client, fake_trade_logger):
        """cancel -2011 → get_order 显示 FILLED → 不标记"""
        client._request = AsyncMock(side_effect=BinanceAPIError(-2011, "Order does not exist"))
        client.get_order = AsyncMock(return_value={"status": "FILLED", "orderId": 12345})
        with pytest.raises(BinanceAPIError) as exc_info:
            await client.cancel_order("BTCUSDT", order_id="12345")
        assert exc_info.value.code == -2011
        fake_trade_logger.mark_entry_canceled.assert_not_awaited()

    async def test_minus_2011_get_order_exception_no_mark(self, client, fake_trade_logger):
        """cancel -2011 → get_order 抛异常 → 不标记（保守）"""
        client._request = AsyncMock(side_effect=BinanceAPIError(-2011, "Order does not exist"))
        client.get_order = AsyncMock(side_effect=Exception("db down"))
        with pytest.raises(BinanceAPIError) as exc_info:
            await client.cancel_order("BTCUSDT", order_id="12345")
        assert exc_info.value.code == -2011
        fake_trade_logger.mark_entry_canceled.assert_not_awaited()

    # ---------- cancel_order 边界分支补齐 ----------

    async def test_client_order_id_empty_raises_value(self, client):
        """补充：client_order_id 纯空白 → ValueError（绕过 falsy 检查命中 4th 校验）"""
        with pytest.raises(ValueError, match="客户端订单ID不能为空"):
            await client.cancel_order("BTCUSDT", client_order_id="  ")

    async def test_order_id_whitespace_raises_value(self, client):
        """补充：order_id 纯空白 → ValueError（覆盖 3rd 校验分支）"""
        with pytest.raises(ValueError, match="订单ID不能为空"):
            await client.cancel_order("BTCUSDT", order_id="  ")

    async def test_cancel_by_client_order_id_still_marks(self, client, fake_trade_logger):
        """补充：走 client_order_id 分支时，标记逻辑仍正常（order_id=None 跳过标记调用）"""
        client._request = AsyncMock(return_value={
            "status": "CANCELED", "executedQty": "0", "orderId": 99999,
        })
        await client.cancel_order("BTCUSDT", client_order_id="co12345")
        # client_order_id 路径下 order_id=None → mark_entry_by_cancel_response 内 order_id 为 None → 直接 return
        fake_trade_logger.mark_entry_canceled.assert_not_awaited()

    # ---------- _mark_entry_by_cancel_response 边界 ----------

    async def test_unknown_status_no_mark_just_warn(self, client, fake_trade_logger):
        """补充：撤单响应 status='REJECTED' + executedQty=0 → 不标记，仅告警"""
        client._request = AsyncMock(return_value={
            "status": "REJECTED", "executedQty": "0", "orderId": 12345,
        })
        await client.cancel_order("BTCUSDT", order_id="12345")
        fake_trade_logger.mark_entry_canceled.assert_not_awaited()

    async def test_mark_response_helper_exception_swallowed(self, client):
        """补充：mark_entry_by_cancel_response 内部抛异常 → cancel_order 正常返回"""
        fake_tl = MagicMock()
        fake_tl.mark_entry_canceled = AsyncMock(side_effect=Exception("db down"))
        client.trade_logger = fake_tl
        client._request = AsyncMock(return_value={
            "status": "CANCELED", "executedQty": "0", "orderId": 12345,
        })
        result = await client.cancel_order("BTCUSDT", order_id="12345")
        assert result["status"] == "CANCELED"  # cancel_order 仍正常返回

    # ---------- _mark_entry_after_cancel_missing 边界 ----------

    async def test_minus_2011_no_trade_logger_returns_early(self, client, fake_trade_logger):
        """补充：无 trade_logger 时 -2011 分支直接 return，不抛异常"""
        client.trade_logger = None
        client._request = AsyncMock(side_effect=BinanceAPIError(-2011, "Order does not exist"))
        with pytest.raises(BinanceAPIError) as exc_info:
            await client.cancel_order("BTCUSDT", order_id="12345")
        assert exc_info.value.code == -2011

    async def test_minus_2011_order_id_non_integer_no_mark(self, client, fake_trade_logger):
        """补充：order_id 非整数 → int() 抛 ValueError → 不标记"""
        client._request = AsyncMock(side_effect=BinanceAPIError(-2011, "Order does not exist"))
        # cancel_order 内部会 str(order_id)，但 get_order 需要 int → 这里用非数字字符串
        # 但 cancel_order 里做了 str(order_id)，然后传给 helper 又 int(order_id_str)
        # 让 helper 的 int() 抛 ValueError
        import unittest.mock as um
        original_get_order = client.get_order
        client._request = AsyncMock(side_effect=BinanceAPIError(-2011, "Order does not exist"))
        with pytest.raises(BinanceAPIError) as exc_info:
            await client.cancel_order("BTCUSDT", order_id="abc")  # str("abc") → int("abc") 抛 ValueError
        assert exc_info.value.code == -2011
        fake_trade_logger.mark_entry_canceled.assert_not_awaited()

    async def test_minus_2011_get_order_none_no_mark(self, client, fake_trade_logger):
        """补充：get_order 返回 None（订单不存在于交易所）→ 不标记"""
        client._request = AsyncMock(side_effect=BinanceAPIError(-2011, "Order does not exist"))
        client.get_order = AsyncMock(return_value=None)
        with pytest.raises(BinanceAPIError) as exc_info:
            await client.cancel_order("BTCUSDT", order_id="12345")
        assert exc_info.value.code == -2011
        fake_trade_logger.mark_entry_canceled.assert_not_awaited()

    async def test_minus_2011_unknown_status_no_mark(self, client, fake_trade_logger):
        """补充：get_order 返回未知 status → 不标记"""
        client._request = AsyncMock(side_effect=BinanceAPIError(-2011, "Order does not exist"))
        client.get_order = AsyncMock(return_value={"status": "REJECTED"})
        with pytest.raises(BinanceAPIError) as exc_info:
            await client.cancel_order("BTCUSDT", order_id="12345")
        assert exc_info.value.code == -2011
        fake_trade_logger.mark_entry_canceled.assert_not_awaited()

    # ============================================================
    # F5：资金风险通道边界防护（-2026-10-14 加固）
    # ============================================================

    async def test_missing_executed_qty_key_no_mark(self, client, fake_trade_logger):
        """
        F5：撤单响应 status='CANCELED' 但缺 executedQty 键 → 保守不标记。
        若无此分支，float(result.get('executedQty', 0)) 把缺失键当 0，
        部分成交记录会被误标 CANCELED → 其真实持仓退出互斥 → 跨策略双开（资金风险）。
        """
        client._request = AsyncMock(return_value={
            "status": "CANCELED", "orderId": 12345,
            # 故意缺 executedQty 键
        })
        await client.cancel_order("BTCUSDT", order_id="12345")
        fake_trade_logger.mark_entry_canceled.assert_not_awaited()

    async def test_missing_qty_key_filled_still_no_mark(self, client, fake_trade_logger):
        """F5 补充：status='FILLED' 也缺 executedQty → 仍保守不标记（走 F5 分支）"""
        client._request = AsyncMock(return_value={
            "status": "FILLED", "orderId": 12345,
            # 缺 executedQty
        })
        await client.cancel_order("BTCUSDT", order_id="12345")
        fake_trade_logger.mark_entry_canceled.assert_not_awaited()


# ============================================================
# AC5：classify_entry 纯函数四分类
# ============================================================
class TestAC5ClassifyEntry:
    """清理脚本 classify_entry 四分类判定（纯函数，100% 覆盖）"""

    def test_binxed_canceled(self):
        assert classify_entry("CANCELED", None, None) == "CANCELED"

    def test_binxed_expired(self):
        assert classify_entry("EXPIRED", None, None) == "CANCELED"

    def test_binxed_minus_2013(self):
        """-2013 (No such order) → 等同于不存在"""
        assert classify_entry(None, -2013, None) == "CANCELED"

    def test_filled_position_zero(self):
        assert classify_entry("FILLED", None, 0.0) == "CLOSED"

    def test_filled_position_near_zero(self):
        assert classify_entry("FILLED", None, 1e-15) == "CLOSED"

    def test_filled_position_nonzero(self):
        assert classify_entry("FILLED", None, 1.5) == "KEPT"

    def test_filled_position_negative(self):
        """空头持仓 positionAmt 为负，abs 判断"""
        assert classify_entry("FILLED", None, -0.5) == "KEPT"

    def test_new_live(self):
        assert classify_entry("NEW", None, None) == "LIVE"

    def test_partially_filled_live(self):
        assert classify_entry("PARTIALLY_FILLED", None, None) == "LIVE"

    def test_skip_on_generic_error(self):
        """非 -2013 的其他错误码 → SKIP"""
        assert classify_entry(None, -1000, None) == "SKIP"

    def test_skip_on_no_order_no_error(self):
        """order_status=None 且 error_code=None（不该出现）→ SKIP"""
        assert classify_entry(None, None, None) == "SKIP"

    def test_filled_position_unknown(self):
        """FILLED 但持仓查询失败 → SKIP"""
        assert classify_entry("FILLED", None, None) == "SKIP"

    def test_unknown_status_skip(self):
        assert classify_entry("REJECTED", None, None) == "SKIP"


# ============================================================
# AC9：release_claim position_closed 标记 CLOSED
# ============================================================
class TestAC9ReleaseClaimPositionClosed:
    """release_claim(reason='position_closed') → 入场记录 CLOSED + 占用释放"""

    @pytest.fixture
    def claims_db(self):
        """模拟 supports_position_claims=True 的 DatabaseManager"""
        db = MagicMock()
        db.execute = AsyncMock(return_value="UPDATE 1")
        type(db).supports_position_claims = MagicMock(return_value=True)
        return db

    @pytest.fixture
    def no_claims_db(self):
        """模拟不支持 claims 的 DatabaseManager（supports_position_claims=False）"""
        db = MagicMock()
        db.execute = AsyncMock(return_value="UPDATE 1")
        type(db).supports_position_claims = MagicMock(return_value=False)
        return db

    async def test_position_closed_marks_entry_closed(self, claims_db):
        """reason='position_closed' → 先执行 _MARK_ENTRY_CLOSED_SQL 再释放占用"""
        ok = await release_claim(claims_db, "BTCUSDT", "new_coin", reason="position_closed")
        assert ok is True
        # execute 被调用两次：第一次 _MARK_ENTRY_CLOSED_SQL，第二次 _CLAIM_RELEASE_SQL
        assert claims_db.execute.await_count == 2
        call_args_list = [c[0][0] for c in claims_db.execute.call_args_list]
        assert _MARK_ENTRY_CLOSED_SQL in call_args_list

    async def test_open_failed_marks_entry_canceled(self, claims_db):
        """D2：reason='open_failed' → 入场记录置 CANCELED（终态），不置 CLOSED"""
        ok = await release_claim(claims_db, "BTCUSDT", "new_coin", reason="open_failed")
        assert ok is True
        # execute 两次：第一次 CANCELED 标记，第二次 _CLAIM_RELEASE_SQL
        assert claims_db.execute.await_count == 2
        called_sqls = [c[0][0] for c in claims_db.execute.call_args_list]
        assert _MARK_ENTRY_CANCELED_SQL in called_sqls
        assert _MARK_ENTRY_CLOSED_SQL not in called_sqls

    @pytest.mark.parametrize("reason", ["open_exception", "micro_zeroed"])
    async def test_unfilled_reasons_mark_canceled(self, claims_db, reason):
        """D2：open_exception / micro_zeroed → 同样置 CANCELED（未成仓/被撤语义）"""
        ok = await release_claim(claims_db, "BTCUSDT", "new_coin", reason=reason)
        assert ok is True
        called_sqls = [c[0][0] for c in claims_db.execute.call_args_list]
        assert _MARK_ENTRY_CANCELED_SQL in called_sqls
        assert _MARK_ENTRY_CLOSED_SQL not in called_sqls

    async def test_unmapped_reason_no_terminal_marking(self, claims_db):
        """D2 反例：reason='released'（未命中映射）→ 不做终态标记，只释放占用"""
        ok = await release_claim(claims_db, "BTCUSDT", "new_coin", reason="released")
        assert ok is True
        called_sqls = [c[0][0] for c in claims_db.execute.call_args_list]
        assert _MARK_ENTRY_CANCELED_SQL not in called_sqls
        assert _MARK_ENTRY_CLOSED_SQL not in called_sqls

    async def test_terminal_mark_zero_rows_warns(self, claims_db):
        """D3：终态标记命中 0 行 → warning（原静默），占用释放继续成功"""
        claims_db.execute = AsyncMock(side_effect=["UPDATE 0", "UPDATE 1"])
        with patch("shared.position_ownership.logger") as fake_logger:
            ok = await release_claim(
                claims_db, "BTCUSDT", "new_coin", reason="position_closed"
            )
        assert ok is True
        assert fake_logger.warning.called
        assert "未匹配到可标记终态的入场记录" in str(fake_logger.warning.call_args)

    async def test_mark_entry_failure_does_not_block_release(self, claims_db):
        """CLOSED 标记 SQL 抛异常 → 仅告警，占用释放继续正常执行"""
        call_count = {"n": 0}

        async def _execute(sql, *args):
            call_count["n"] += 1
            if call_count["n"] == 1 and "trade_records" in sql:
                raise Exception("trade_records 临时不可达")
            return "UPDATE 1"

        claims_db.execute = AsyncMock(side_effect=_execute)
        ok = await release_claim(claims_db, "BTCUSDT", "new_coin", reason="position_closed")
        assert ok is True  # 占用释放仍成功
        # execute 第二次调用（_CLAIM_RELEASE_SQL）仍成功
        assert call_count["n"] == 2

    async def test_no_claims_capability_still_marks_entry(self, no_claims_db):
        """
        不支持 claims 时（supports_position_claims=False）仍执行 CLOSED 标记，
        然后占用释放路径早退（db.execute 仍会被调用）。
        关键：CLOSED 标记不依赖 claims 能力。
        """
        ok = await release_claim(no_claims_db, "BTCUSDT", "new_coin", reason="position_closed")
        assert ok is True
        # _MARK_ENTRY_CLOSED_SQL 仍被执行（不依赖 claims）
        called_sqls = [c[0][0] for c in no_claims_db.execute.call_args_list]
        assert _MARK_ENTRY_CLOSED_SQL in called_sqls

    async def test_db_manager_none_skips_everything(self):
        """db_manager=None → 直接返回 True，不执行任何 SQL"""
        ok = await release_claim(None, "BTCUSDT", "new_coin", reason="position_closed")
        assert ok is True

    async def test_release_failure_still_returns_false(self, claims_db):
        """占用释放 SQL 抛异常 → 返回 False，不影响其前的 CLOSED 标记"""
        call_count = {"n": 0}

        async def _execute(sql, *args):
            call_count["n"] += 1
            if call_count["n"] == 2:  # 第二次调用是 _CLAIM_RELEASE_SQL
                raise Exception("claim table down")
            return "UPDATE 1"

        claims_db.execute = AsyncMock(side_effect=_execute)
        ok = await release_claim(claims_db, "BTCUSDT", "new_coin", reason="position_closed")
        assert ok is False  # 占用释放失败返回 False
        # 但 CLOSED 标记仍已执行
        assert call_count["n"] == 2


# ============================================================
# F4：classify_entry 将 -2011 同归不存在类
# ============================================================
class TestF4Minus2011EquivalentTo2013:
    """-2011（订单不存在/已消失）→ 与 -2013 同等归入 CANCELED 类"""

    def test_minus_2011_returns_canceled(self):
        assert classify_entry(None, -2011, None) == "CANCELED"

    def test_minus_2013_still_works(self):
        """原有 -2013 用例不变（回归）"""
        assert classify_entry(None, -2013, None) == "CANCELED"


# ============================================================
# F1：AC2 完整互斥行为 + SQL 断言
# ============================================================
class TestF1AC2MutexBehavior:
    """is_symbol_owned_by_other 完整行为链：对家 True / 终态 False + SQL NOT IN 断言"""

    @pytest.mark.asyncio
    async def test_other_strategy_new_record_blocks_mutex(self):
        """
        对家（btc_eth）有入场记录 → is_symbol_owned_by_other 返回 True。
        注意：MagicMock 上 getattr('fetch_one_advisory_lock') 会返回 MagicMock
        而非 None，导致走 advisory_fetcher 路径但不是 AsyncMock。用 patch
        直接替换 _resolve_owner_with_lock 返回值更稳定。
        """
        from shared.position_ownership import is_symbol_owned_by_other

        with patch("shared.position_ownership._resolve_owner_with_lock",
                   new=AsyncMock(return_value="btc_eth")):
            db_manager = MagicMock()
            type(db_manager).supports_position_claims = MagicMock(return_value=False)
            blocked = await is_symbol_owned_by_other(
                db_manager, "SNPSUSDT", "new_coin",
                competing_record_names=["btc_eth", "hrs"],
            )
        assert blocked is True

    @pytest.mark.asyncio
    async def test_no_other_strategy_allows(self):
        """对家无记录 → 返回 False（放行）"""
        from shared.position_ownership import is_symbol_owned_by_other

        with patch("shared.position_ownership._resolve_owner_with_lock",
                   new=AsyncMock(return_value=None)):
            db_manager = MagicMock()
            type(db_manager).supports_position_claims = MagicMock(return_value=False)
            blocked = await is_symbol_owned_by_other(
                db_manager, "SNPSUSDT", "new_coin",
                competing_record_names=["btc_eth"],
            )
        assert blocked is False

    def test_sql_contains_terminal_exclusion(self):
        """F1 附加：_open_entry_sql 两分支都必须带 NOT IN 终态排除"""
        for sql in (_open_entry_sql(True), _open_entry_sql(False)):
            assert "NOT IN" in sql
            assert "'CANCELED'" in sql
            assert "'EXPIRED'" in sql
            assert "'CLOSED'" in sql


# ============================================================
# F2：run_audit 主逻辑 mock 单测
# ============================================================
class TestF2RunAuditMock:
    """清理脚本主流程 dry-run / execute / 分类 / 限速 / 异常跳过"""

    @pytest.fixture
    def fake_row(self):
        """构造最小 asyncpg.Record 兼容 stub"""
        row = MagicMock()
        row.__getitem__ = lambda self, k: {
            "id": 42, "symbol": "SNPSUSDT", "strategy": "new_coin",
            "order_id": "123456", "order_type": "LIMIT",
            "status": "NEW", "realized_pnl": None, "executed_at": None,
        }[k]
        row.id = 42
        row.symbol = "SNPSUSDT"
        row.strategy = "new_coin"
        row.order_id = "123456"
        return row

    @pytest.fixture
    def fake_db(self, fake_row):
        """模拟 asyncpg.Connection"""
        db = MagicMock()
        db.fetch = AsyncMock(return_value=[fake_row])
        db.execute = AsyncMock(return_value="UPDATE 1")
        return db

    @pytest.fixture
    def fake_client(self):
        """模拟 BinanceClient：get_order 返回 CANCELED，get_position 返回 0 持仓"""
        c = MagicMock()
        c.get_order = AsyncMock(return_value={"status": "CANCELED"})
        c.get_position = AsyncMock(return_value=[{"symbol": "SNPSUSDT", "positionAmt": "0"}])
        return c

    @pytest.mark.asyncio
    async def test_dry_run_does_not_execute_any_update(self, fake_db, fake_client):
        """dry-run（默认）→ db.execute 不应被调用（只读 + 输出）"""
        from scripts.fix_ghost_entry_records import run_audit
        await run_audit(fake_db, fake_client, execute=False, min_interval=0.0)
        # audit_entry 里的限速 sleep 在 fake_client 下会跳过 → 但 db.fetch 被调用
        # 关键断言：fake_db.execute 不被调用
        fake_db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_execute_only_writes_canceled_class(self, fake_db, fake_client, fake_row):
        """--execute：只有 CANCELED/CLOSED 类落库，KEPT/LIVE/SKIP 不动"""
        from scripts.fix_ghost_entry_records import run_audit
        await run_audit(fake_db, fake_client, execute=True, min_interval=0.0)
        # CANCELED 类：get_order 返回 CANCELED → classify_entry → "CANCELED" → 落库
        # execute 应被调用至少一次（_MARK_CANCELED_SQL）
        assert fake_db.execute.await_count >= 1

    @pytest.mark.asyncio
    async def test_live_class_not_written(self, fake_db, fake_client):
        """LIVE 类（交易所仍 NEW）→ 不落库"""
        from scripts.fix_ghost_entry_records import run_audit
        fake_client.get_order = AsyncMock(return_value={"status": "NEW"})  # 活单
        await run_audit(fake_db, fake_client, execute=True, min_interval=0.0)
        fake_db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kept_filled_nonzero_not_written(self, fake_db, fake_client):
        """KEPT 类（FILLED + 持仓非 0）→ 不落库"""
        from scripts.fix_ghost_entry_records import run_audit
        fake_client.get_order = AsyncMock(return_value={"status": "FILLED"})
        fake_client.get_position = AsyncMock(return_value=[
            {"symbol": "SNPSUSDT", "positionAmt": "10"}  # 非 0
        ])
        await run_audit(fake_db, fake_client, execute=True, min_interval=0.0)
        fake_db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_row_exception_skips_continues(self, fake_db, fake_client):
        """单条 audit_entry 抛异常 → run_audit 继续处理其他行不中断"""
        from scripts.fix_ghost_entry_records import run_audit
        fake_client.get_order = AsyncMock(side_effect=Exception("交易所挂了"))
        # SKIP 类 → 不落库，但脚本不崩
        await run_audit(fake_db, fake_client, execute=True, min_interval=0.0)
        fake_db.execute.assert_not_awaited()  # SKIP 类不落库


# ============================================================
# F3：config.yaml ownership 加载 + _is_symbol_occupied stub
# ============================================================
class TestF3NewCoinConfigAndOccupancy:
    """F3：load_ownership_config 断言 + _is_symbol_occupied stub"""

    def test_load_ownership_config_my_record_name_is_new_coin(self):
        """F3(a)：真实读 strategies/new_coin/config.yaml → my_record_name=='new_coin'"""
        import yaml
        from shared.position_ownership import load_ownership_config

        config_path = os.path.join(
            PROJECT_ROOT, "strategies", "new_coin", "config.yaml"
        )
        with open(config_path) as f:
            config = yaml.safe_load(f)
        ownership = load_ownership_config(config)
        # load_ownership_config 只返回 {'my_record_name', 'competing_record_names'}
        assert ownership["my_record_name"] == "new_coin"
        assert isinstance(ownership["competing_record_names"], list)

    @pytest.mark.asyncio
    async def test_is_symbol_occupied_with_short_positions(self):
        """
        F3(b)：executor._is_symbol_occupied 命中 short_positions 来源 → 返回 True。
        通过 patch _collect_occupancy_sources 返回 sources 列表，
        避免真实 DB 查询（参照 executor.py:743-780 结构）。
        """
        from strategies.new_coin.executor import TradingExecutor
        # 验证类和方法存在（结构约束）
        assert hasattr(TradingExecutor, '_is_symbol_occupied')
        assert hasattr(TradingExecutor, '_collect_occupancy_sources')
        # 直接 mock _collect_occupancy_sources 返回含 'short_positions' 的 sources 列表
        with patch.object(
            TradingExecutor, '_collect_occupancy_sources',
            new=AsyncMock(return_value=(["short_positions"], 0.0, True)),
        ), patch.object(
            TradingExecutor, '_notify_duplicate_symbol',
            new=AsyncMock(return_value=None),
        ), patch.object(
            TradingExecutor, '__init__', lambda self, *a, **kw: None,
        ):
            inst = TradingExecutor()
            result = await inst._is_symbol_occupied("SNPSUSDT")
            assert result is True

    @pytest.mark.asyncio
    async def test_is_symbol_occupied_no_sources_returns_false(self):
        """F3(b) 反例：无任何 sources → _is_symbol_occupied 返回 False"""
        from strategies.new_coin.executor import TradingExecutor
        with patch.object(
            TradingExecutor, '_collect_occupancy_sources',
            new=AsyncMock(return_value=([], None, True)),  # exchange 可用，无 sources
        ), patch.object(
            TradingExecutor, '_notify_duplicate_symbol',
            new=AsyncMock(return_value=None),
        ), patch.object(
            TradingExecutor, '__init__', lambda self, *a, **kw: None,
        ):
            inst = TradingExecutor()
            result = await inst._is_symbol_occupied("SNPSUSDT")
            assert result is False


# ============================================================
# T2：build_candidate_query 过滤参数化（--symbol / --strategy）
# ============================================================
class TestT2BuildCandidateQuery:
    """分币种/策略受限清理：过滤值一律参数化，禁止字符串拼接"""

    def test_no_filter_returns_base_sql_and_no_params(self):
        sql, params = build_candidate_query()
        assert params == []
        assert "symbol = ANY(" not in sql
        assert "ORDER BY executed_at ASC, id ASC" in sql
        assert "LIMIT $" not in sql

    def test_symbol_filter_is_parameterized(self):
        sql, params = build_candidate_query(symbols=["BNBUSDT"])
        assert params == [["BNBUSDT"]]
        assert "AND symbol = ANY($1)" in sql
        # 过滤值不得出现在 SQL 文本中（参数化，防注入/防范围失控）
        assert "BNBUSDT" not in sql

    def test_symbol_strategy_limit_param_order(self):
        sql, params = build_candidate_query(
            symbols=["BNBUSDT", "SOLUSDT"],
            strategies=["btc_eth_aggressive"],
            max_rows=50,
        )
        assert params == [["BNBUSDT", "SOLUSDT"], ["btc_eth_aggressive"], 50]
        assert "AND symbol = ANY($1)" in sql
        assert "AND strategy = ANY($2)" in sql
        assert sql.rstrip().endswith("LIMIT $3")

    def test_empty_iterable_treated_as_no_filter(self):
        sql, params = build_candidate_query(symbols=[], strategies=None)
        assert params == []
        assert "ANY(" not in sql


# ============================================================
# T2b：audit_entry 持仓判定（查询成功但无该币种行 → 归零，不误判 SKIP）
# ============================================================
class TestT2bAuditEntryPositionResolution:
    """FILLED 记录的持仓判定：区分「查询成功但无持仓行」与「查询异常」"""

    @pytest.mark.asyncio
    async def test_filled_no_position_row_is_closed(self):
        """账户无任何持仓行（合法返回）→ position_amt=0 → CLOSED"""
        from scripts.fix_ghost_entry_records import audit_entry

        client = MagicMock()
        client.get_order = AsyncMock(return_value={"status": "FILLED"})
        client.get_position = AsyncMock(return_value=[])
        result = await audit_entry(
            client, {"symbol": "BNBUSDT", "order_id": "96733987860"}, min_interval=0.0
        )
        assert result["category"] == "CLOSED"
        assert result["position_amt"] == 0.0

    @pytest.mark.asyncio
    async def test_filled_other_symbol_rows_is_closed(self):
        """返回列表只有其他币种行 → 本币种视为归零 → CLOSED"""
        from scripts.fix_ghost_entry_records import audit_entry

        client = MagicMock()
        client.get_order = AsyncMock(return_value={"status": "FILLED"})
        client.get_position = AsyncMock(
            return_value=[{"symbol": "BTCUSDT", "positionAmt": "1"}]
        )
        result = await audit_entry(
            client, {"symbol": "BNBUSDT", "order_id": "1"}, min_interval=0.0
        )
        assert result["category"] == "CLOSED"

    @pytest.mark.asyncio
    async def test_filled_nonzero_position_kept(self):
        """本币种持仓非 0（SOL 真仓）→ KEPT（保留，互斥继续生效）"""
        from scripts.fix_ghost_entry_records import audit_entry

        client = MagicMock()
        client.get_order = AsyncMock(return_value={"status": "FILLED"})
        client.get_position = AsyncMock(
            return_value=[{"symbol": "SOLUSDT", "positionAmt": "0.52"}]
        )
        result = await audit_entry(
            client, {"symbol": "SOLUSDT", "order_id": "249454901842"}, min_interval=0.0
        )
        assert result["category"] == "KEPT"
        assert result["position_amt"] == 0.52

    @pytest.mark.asyncio
    async def test_filled_position_query_error_skips(self):
        """持仓查询异常 → 保守 SKIP（不得误判为归零）"""
        from scripts.fix_ghost_entry_records import audit_entry

        client = MagicMock()
        client.get_order = AsyncMock(return_value={"status": "FILLED"})
        client.get_position = AsyncMock(side_effect=Exception("positionRisk down"))
        result = await audit_entry(
            client, {"symbol": "BNBUSDT", "order_id": "1"}, min_interval=0.0
        )
        assert result["category"] == "SKIP"
        assert result["position_amt"] is None


# ============================================================
# AC4：release_claim_if_no_position 守卫（真仓不误标终态）
# ============================================================
class TestAC4ReleaseClaimGuard:
    """未成仓释放的三个调用点都经此守卫：已登记持仓的 symbol 一律不释放"""

    @pytest.mark.asyncio
    async def test_position_present_skips_release(self):
        from shared.protection_retry import release_claim_if_no_position

        fn = AsyncMock()
        await release_claim_if_no_position(
            {"BTCUSDT": object()}, MagicMock(), "BTCUSDT", "new_coin",
            "open_failed", fn,
        )
        fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_position_absent_releases_with_reason(self):
        from shared.protection_retry import release_claim_if_no_position

        fn = AsyncMock()
        await release_claim_if_no_position(
            {}, MagicMock(), "BTCUSDT", "new_coin", "open_failed", fn,
        )
        fn.assert_awaited_once()
        assert fn.call_args.kwargs["reason"] == "open_failed"


# ============================================================
# 2026-10-26 修复：realized_pnl IS NULL + resolve_conflict_owner
# ============================================================

class TestBNBFixAC1SQLContainsRealizedPnl:
    """AC1：终态标记 SQL 必须包含 realized_pnl IS NULL 条件，
    与 _open_entry_sql(False) 判定口径对齐。"""

    def test_mark_entry_terminal_sql_both_statuses_contain_realized_pnl(self):
        from shared.position_ownership import _mark_entry_terminal_sql
        closed = _mark_entry_terminal_sql('CLOSED')
        canceled = _mark_entry_terminal_sql('CANCELED')
        for sql in (closed, canceled):
            assert "realized_pnl IS NULL" in sql

    def test_module_level_sql_constants_contain_realized_pnl(self):
        """模块级常量 _MARK_ENTRY_*_SQL 也必须含 realized_pnl IS NULL"""
        assert "realized_pnl IS NULL" in _MARK_ENTRY_CLOSED_SQL
        assert "realized_pnl IS NULL" in _MARK_ENTRY_CANCELED_SQL

    def test_full_close_marks_all_nonterminal_entries_for_same_owner(self):
        """同币多次开仓已归零时，旧 NEW 也必须退出互斥判定。"""
        from shared.position_ownership import _mark_entry_terminal_sql
        sql = _mark_entry_terminal_sql('CLOSED')
        assert "WHERE symbol = $1 AND strategy = $2" in sql
        assert "realized_pnl IS NULL" in sql
        assert "status NOT IN" in sql
        assert "LIMIT 1" not in sql


class TestBNBFixAC2ReleaseClaimUsesRealizedPnlFilter:
    """AC2：release_claim(reason='position_closed') 的 CLOSED 标记 SQL
    必须同时含 realized_pnl IS NULL 和 status='CLOSED'，参数正确；
    并对 UPDATE 0 / UPDATE 1 分别做告警行为断言（伪 DB 行为）。"""

    @pytest.mark.asyncio
    async def test_release_claim_closed_sql_and_params(self):
        from shared.position_ownership import release_claim

        db = MagicMock()
        db.execute = AsyncMock(return_value="UPDATE 1")
        # 关键：不要在 type(db) 上设置 supports_position_claims；
        # getattr(type(db), 'supports_position_claims', False) 会回退默认 False，
        # 使 _supports_claims 返回 False，release_claim 在终态标记后早退。
        await release_claim(db, "BNBUSDT", "btc_eth", reason="position_closed")
        # 只调了一次 execute（claims 不支持，走早退）；即 _MARK_ENTRY_CLOSED_SQL
        db.execute.assert_awaited_once()
        sql, symbol_arg, strategy_arg = db.execute.call_args.args
        assert "realized_pnl IS NULL" in sql
        assert "status = 'CLOSED'" in sql
        assert symbol_arg == "BNBUSDT"
        assert strategy_arg == "btc_eth"

    @pytest.mark.asyncio
    async def test_release_claim_update_one_does_not_warn(self):
        """UPDATE 1 命中入场记录 → 正常路径，不触发 0 行告警"""
        from shared.position_ownership import release_claim

        db = MagicMock()
        db.execute = AsyncMock(return_value="UPDATE 1")
        with patch("shared.position_ownership.logger.warning") as fake_warn:
            await release_claim(db, "BNBUSDT", "btc_eth", reason="position_closed")
        # 不应出现"未匹配到可标记终态的入场记录"告警
        for call in fake_warn.call_args_list:
            assert "未匹配到可标记终态的入场记录" not in str(call)

    @pytest.mark.asyncio
    async def test_release_claim_update_zero_triggers_warning(self):
        """UPDATE 0 未命中 → 必须触发 warning（伪 DB 行为断言）"""
        from shared.position_ownership import release_claim

        db = MagicMock()
        db.execute = AsyncMock(return_value="UPDATE 0")
        with patch("shared.position_ownership.logger.warning") as fake_warn:
            await release_claim(db, "BNBUSDT", "btc_eth", reason="position_closed")
        assert any(
            "未匹配到可标记终态的入场记录" in str(call)
            for call in fake_warn.call_args_list
        )


class TestBNBFixAC3ResolveConflictOwner:
    """AC3：resolve_conflict_owner + _acquire_entry_slot 冲突分支 owner 解析"""

    # ---------- resolve_conflict_owner 单元 ----------

    @pytest.mark.asyncio
    async def test_authority_hit_returns_authority_owner(self):
        from shared.position_ownership import resolve_conflict_owner

        db = MagicMock()
        with patch(
            "shared.position_ownership._resolve_owner_with_lock",
            new=AsyncMock(return_value="btc_eth"),
        ), patch(
            "shared.position_ownership._resolve_claim_owner",
            new=AsyncMock(return_value=None),
        ) as fake_claim:
            owner = await resolve_conflict_owner(
                db, "BNBUSDT", "btc_eth_aggressive",
                competing_record_names=["btc_eth"],
            )
        assert owner == "btc_eth"
        # authority 命中短路，claim 不应被查询
        fake_claim.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_authority_miss_claim_hit_returns_claim_owner(self):
        from shared.position_ownership import resolve_conflict_owner

        db = MagicMock()
        with patch(
            "shared.position_ownership._resolve_owner_with_lock",
            new=AsyncMock(return_value=None),
        ), patch(
            "shared.position_ownership._resolve_claim_owner",
            new=AsyncMock(return_value="new_coin"),
        ):
            owner = await resolve_conflict_owner(
                db, "SNPSUSDT", "btc_eth",
                competing_record_names=["new_coin"],
            )
        assert owner == "new_coin"

    @pytest.mark.asyncio
    async def test_enabled_false_skips_claim_query(self):
        """enabled=False → 不调 _resolve_claim_owner（与 is_symbol_owned_by_other 对齐）"""
        from shared.position_ownership import resolve_conflict_owner

        db = MagicMock()
        with patch(
            "shared.position_ownership._resolve_owner_with_lock",
            new=AsyncMock(return_value=None),
        ), patch(
            "shared.position_ownership._resolve_claim_owner",
            new=AsyncMock(),
        ) as fake_claim:
            owner = await resolve_conflict_owner(
                db, "SOLUSDT", "btc_eth",
                competing_record_names=["new_coin"],
                enabled=False,
            )
        assert owner is None
        fake_claim.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolve_owner_with_lock_exception_returns_none_no_raise(self):
        """_resolve_owner_with_lock 抛异常 → 返回 None 且**不向上抛异常**"""
        from shared.position_ownership import resolve_conflict_owner

        db = MagicMock()
        with patch(
            "shared.position_ownership._resolve_owner_with_lock",
            new=AsyncMock(side_effect=Exception("advisory lock 炸了")),
        ):
            owner = await resolve_conflict_owner(
                db, "BNBUSDT", "btc_eth_aggressive",
                competing_record_names=["btc_eth"],
            )
        assert owner is None  # 不抛异常

    # ---------- 一致性对拍：is_symbol_owned_by_other ⇔ resolve_conflict_owner ----------

    @pytest.mark.asyncio
    async def test_consistency_authority_hit(self):
        """一致组 1：权威归属命中 → is_symbol_owned_by_other=True ⇔ owner != None"""
        from shared.position_ownership import (
            is_symbol_owned_by_other, resolve_conflict_owner,
        )

        db = MagicMock()
        with patch(
            "shared.position_ownership._resolve_owner_with_lock",
            new=AsyncMock(return_value="btc_eth"),
        ), patch(
            "shared.position_ownership._resolve_claim_owner",
            new=AsyncMock(return_value=None),
        ):
            blocked = await is_symbol_owned_by_other(
                db, "BNBUSDT", "btc_eth_aggressive",
                competing_record_names=["btc_eth"],
            )
            owner = await resolve_conflict_owner(
                db, "BNBUSDT", "btc_eth_aggressive",
                competing_record_names=["btc_eth"],
            )
        assert blocked is True
        assert owner is not None
        assert owner == "btc_eth"

    @pytest.mark.asyncio
    async def test_consistency_claim_hit(self):
        """一致组 2：权威无命中 + claim 命中"""
        from shared.position_ownership import (
            is_symbol_owned_by_other, resolve_conflict_owner,
        )

        db = MagicMock()
        with patch(
            "shared.position_ownership._resolve_owner_with_lock",
            new=AsyncMock(return_value=None),
        ), patch(
            "shared.position_ownership._resolve_claim_owner",
            new=AsyncMock(return_value="new_coin"),
        ):
            blocked = await is_symbol_owned_by_other(
                db, "SNPSUSDT", "btc_eth",
                competing_record_names=["new_coin"],
            )
            owner = await resolve_conflict_owner(
                db, "SNPSUSDT", "btc_eth",
                competing_record_names=["new_coin"],
            )
        assert blocked is True
        assert owner == "new_coin"

    @pytest.mark.asyncio
    async def test_consistency_no_conflict(self):
        """一致组 3：全无 → 两者都不拦截"""
        from shared.position_ownership import (
            is_symbol_owned_by_other, resolve_conflict_owner,
        )

        db = MagicMock()
        with patch(
            "shared.position_ownership._resolve_owner_with_lock",
            new=AsyncMock(return_value=None),
        ), patch(
            "shared.position_ownership._resolve_claim_owner",
            new=AsyncMock(return_value=None),
        ):
            blocked = await is_symbol_owned_by_other(
                db, "ETHUSDT", "btc_eth",
                competing_record_names=["new_coin"],
            )
            owner = await resolve_conflict_owner(
                db, "ETHUSDT", "btc_eth",
                competing_record_names=["new_coin"],
            )
        assert blocked is False
        assert owner is None

    # ---------- _acquire_entry_slot 冲突分支透传 owner ----------

    @pytest.mark.asyncio
    async def test_acquire_entry_slot_passes_real_owner(self):
        """归属互斥命中时，_handle_claim_conflict 第二个参数是解析出的真实 owner"""
        from shared.protection_retry import _acquire_entry_slot

        strategy = MagicMock()
        strategy.my_record_name = "btc_eth_aggressive"
        strategy._competing_record_names = ["btc_eth"]
        strategy._ownership_enabled = True
        strategy._claim_ttl_minutes = 30
        strategy._lock_timeout_seconds = 5
        strategy.db_manager = MagicMock()
        strategy._handle_claim_conflict = AsyncMock()

        is_owned = AsyncMock(return_value=True)
        try_claim = AsyncMock()

        async def resolve_owner(*args, **kwargs):
            return "btc_eth"

        result = await _acquire_entry_slot(
            strategy, "BNBUSDT",
            is_owned_fn=is_owned, try_claim_fn=try_claim,
            resolve_owner_fn=resolve_owner,
        )
        assert result is False
        strategy._handle_claim_conflict.assert_awaited_once_with("BNBUSDT", "btc_eth")
        try_claim.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_acquire_entry_slot_resolve_owner_exception_degrades(self):
        """resolve_owner_fn 抛异常 → _handle_claim_conflict 收到 None，
        _acquire_entry_slot 返回 False 且**不向上抛异常**"""
        from shared.protection_retry import _acquire_entry_slot

        strategy = MagicMock()
        strategy.my_record_name = "btc_eth_aggressive"
        strategy._competing_record_names = ["btc_eth"]
        strategy._ownership_enabled = True
        strategy._claim_ttl_minutes = 30
        strategy._lock_timeout_seconds = 5
        strategy.db_manager = MagicMock()
        strategy._handle_claim_conflict = AsyncMock()

        is_owned = AsyncMock(return_value=True)
        try_claim = AsyncMock()

        async def boom(*args, **kwargs):
            raise Exception("resolve_owner_fn 炸了")

        with patch("shared.protection_retry.logger.warning") as fake_warn:
            result = await _acquire_entry_slot(
                strategy, "BNBUSDT",
                is_owned_fn=is_owned, try_claim_fn=try_claim,
                resolve_owner_fn=boom,
            )
        assert result is False  # 不抛异常
        strategy._handle_claim_conflict.assert_awaited_once_with("BNBUSDT", None)
        # 必须触发降级 warning
        assert any(
            "冲突 owner 解析异常" in str(call)
            for call in fake_warn.call_args_list
        )

    @pytest.mark.asyncio
    async def test_acquire_entry_slot_claim_conflict_owner_none_fallback(self):
        """claim 分支 owner=None（保守情形）→ 触发 resolve_owner_fn fallback"""
        from shared.protection_retry import _acquire_entry_slot

        strategy = MagicMock()
        strategy.my_record_name = "btc_eth_aggressive"
        strategy._competing_record_names = ["btc_eth"]
        strategy._ownership_enabled = True
        strategy._claim_ttl_minutes = 30
        strategy._lock_timeout_seconds = 5
        strategy.db_manager = MagicMock()
        strategy._handle_claim_conflict = AsyncMock()

        is_owned = AsyncMock(return_value=False)
        # 保守情形：claimed=False 且 owner=None（try_claim_symbol 可能的返回）
        try_claim = AsyncMock(return_value={"claimed": False, "owner": None, "claim_id": None})

        async def resolve_owner(*args, **kwargs):
            return "btc_eth"

        result = await _acquire_entry_slot(
            strategy, "BNBUSDT",
            is_owned_fn=is_owned, try_claim_fn=try_claim,
            resolve_owner_fn=resolve_owner,
        )
        assert result is False
        strategy._handle_claim_conflict.assert_awaited_once_with("BNBUSDT", "btc_eth")


# ============================================================
# 待修 3 补充：终态标记 SQL 语义镜像 + 两选一场景回归钉住
# ============================================================

def _simulate_mark_entry_terminal_select(
    rows, symbol: str, strategy: str, *, include_realized_pnl_filter: bool,
):
    """SQL 语义镜像（纯函数）：模拟 `_mark_entry_terminal_sql` 内层 SELECT 行为，
    用于钉住「realized_pnl IS NULL 是修复点」。

    逐条过滤：symbol 相等 → strategy 相等 → order_type ∈ ('LIMIT','MARKET')
    → realized_pnl is None（可选，include_realized_pnl_filter=False 时跳过）
    → status ∉ ('CANCELED','EXPIRED','CLOSED') → 按 executed_at DESC, id DESC 取第一条。

    Args:
        rows: 假记录列表（每条为 dict，含 id/symbol/strategy/order_type/status/realized_pnl/executed_at）
        symbol: 匹配 symbol
        strategy: 匹配 strategy
        include_realized_pnl_filter: 是否模拟 SQL 中的 `realized_pnl IS NULL` 条件
            True → 真实修复后 SQL；False → 修复前（平仓单会被误选中）

    Returns:
        被选中的记录 dict；无命中返回 None
    """
    entry_types = ('LIMIT', 'MARKET')
    terminal = {'CANCELED', 'EXPIRED', 'CLOSED'}
    filtered = [
        r for r in rows
        if r['symbol'] == symbol
        and r['strategy'] == strategy
        and r['order_type'] in entry_types
        and (not include_realized_pnl_filter or r.get('realized_pnl') is None)
        and r['status'] not in terminal
    ]
    if not filtered:
        return None
    filtered.sort(key=lambda r: (r.get('executed_at') or '', r.get('id') or 0), reverse=True)
    return filtered[0]


class TestBNBFixMarkEntryTerminalSemantics:
    """待修 3：SQL 文本位置断言 + 两选一场景行为钉住（核心回归）"""

    # ---------- 3a：SQL 文本位置断言 ----------

    def test_mark_entry_terminal_sql_realized_pnl_filter_after_order_type(self):
        from shared.position_ownership import _mark_entry_terminal_sql
        sql = _mark_entry_terminal_sql('CLOSED')
        idx_order_type = sql.index('order_type IN')
        idx_realized = sql.index('realized_pnl IS NULL')
        assert idx_order_type < idx_realized
        assert 'ORDER BY executed_at DESC, id DESC LIMIT 1' not in sql

    def test_mark_entry_terminal_sql_preserves_limit_one_and_order_by(self):
        from shared.position_ownership import _mark_entry_terminal_sql
        sql = _mark_entry_terminal_sql('CANCELED')
        assert 'ORDER BY executed_at DESC, id DESC' in sql
        assert 'LIMIT 1' in sql

    # ---------- 3b：两选一场景行为钉住 ----------

    def test_real_fix_selects_entry_not_exit(self):
        """生产数据复刻：BNBUSDT 入场单(id=4292) vs 平仓单(id=4315)，
        修复后必须选中入场单 4292（平仓单因 realized_pnl 非空被排除）"""
        rows = [
            {
                'id': 4315, 'symbol': 'BNBUSDT', 'strategy': 'btc_eth',
                'order_type': 'LIMIT', 'status': 'FILLED',
                'realized_pnl': 0.4344, 'executed_at': '2026-10-23 20:05:00.945',
            },
            {
                'id': 4292, 'symbol': 'BNBUSDT', 'strategy': 'btc_eth',
                'order_type': 'LIMIT', 'status': 'NEW',
                'realized_pnl': None, 'executed_at': '2026-10-23 06:05:03',
            },
        ]
        selected = _simulate_mark_entry_terminal_select(
            rows, 'BNBUSDT', 'btc_eth', include_realized_pnl_filter=True,
        )
        assert selected is not None
        assert selected['id'] == 4292

    def test_without_realized_pnl_filter_would_select_exit(self):
        """同上数据，**去掉** realized_pnl IS NULL 条件（修复前 SQL）时会选中 4315，
        证实该条件正是修复点（ORDER BY executed_at DESC 让更晚的平仓单排在前面）"""
        rows = [
            {
                'id': 4315, 'symbol': 'BNBUSDT', 'strategy': 'btc_eth',
                'order_type': 'LIMIT', 'status': 'FILLED',
                'realized_pnl': 0.4344, 'executed_at': '2026-10-23 20:05:00.945',
            },
            {
                'id': 4292, 'symbol': 'BNBUSDT', 'strategy': 'btc_eth',
                'order_type': 'LIMIT', 'status': 'NEW',
                'realized_pnl': None, 'executed_at': '2026-10-23 06:05:03',
            },
        ]
        selected = _simulate_mark_entry_terminal_select(
            rows, 'BNBUSDT', 'btc_eth', include_realized_pnl_filter=False,
        )
        assert selected is not None
        assert selected['id'] == 4315  # 坏的选择！证明 realized_pnl IS NULL 是修复点
