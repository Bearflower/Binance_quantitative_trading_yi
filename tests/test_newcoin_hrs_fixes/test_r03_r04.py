"""R03（new_coin 平仓）+ R04（hrs 平仓守卫）修复验证单测。

覆盖验收标准：
- R03-AC1：提交前重读真实持仓、按剩余待平量重算（次笔 = target - 已平量）
- R03-AC2：剩余量经精度截断为 0 且仍有持仓 → 不提交反向单，返回失败
- R03-AC3：限价 / 市价兜底提交均带 reduceOnly
- R03-AC4：撤单抛 -2011 → 读单累加 executedQty，不再按全量补单
- R03-AC5：以交易所真实持仓为准，target 超量被截断，不产生反向仓
- R03-AC6：-2022 被拒 → 先撤同向条件单再重试成功
- R04-AC1/AC2：失败 / 仅受理 → 不 _writeback_pnl、不 cancel_all_orders、不 remove_position
- R04-AC3：完全成交 → 原流程（回写盈亏 + 撤保护单 + 删持仓）
- R04-AC4：部分成交 → 保留仓位 + 按剩余量重建保护 + 告警
- R04-AC5：多轮失败幂等（不产生超额单），告警可观测

另含 T13 / T16（new_coin 侧）接线烟测：入场等待委托统一助手、开仓占用冲突跳过。

全部使用假交易所客户端，**禁止真实网络**。
"""

import os
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from shared.api_retry_config import reset_cache  # noqa: E402
from shared.binance_api import BinanceAPIError  # noqa: E402
from shared.reduce_only_close import (  # noqa: E402
    CLOSE_ACCEPTED,
    CLOSE_FAILED,
    CLOSE_FILLED,
    CLOSE_PARTIAL,
    CloseOutcome,
)
from strategies.hrs.executor import TradingExecutor as HrsExecutor  # noqa: E402
from strategies.hrs.strategy import HRSStrategy  # noqa: E402
from strategies.new_coin.executor import TradingExecutor as NewCoinExecutor  # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def fast_order_fill(monkeypatch):
    """订单终态等待的首查延迟 / 轮询归零，避免测试变慢。"""
    monkeypatch.setenv("ORDER_FILL_PM_ORDER_VISIBILITY_DELAY_SECONDS", "0")
    monkeypatch.setenv("ORDER_FILL_CHECK_INTERVAL_SECONDS", "0.001")
    reset_cache()
    yield
    reset_cache()


class FakeExchange:
    """假交易所：维护带符号持仓，按脚本对每笔下单施加成交量效果。

    Args:
        position: 初始带符号持仓（正=多头，负=空头）
        step: 数量步长
        fills: 每笔下单脚本列表，元素形如 {"executed": "0.4", "status": "CANCELED"}
        ticker: get_ticker 返回的最新价
        bids: 订单簿买盘（None 表示用默认买一价）
        reject_reduce_only_times: 前 N 次下单抛 -2022（模拟 ReduceOnly 被拒）
        cancel_order_error: cancel_order 抛出的异常（模拟 -2011 竞态）
        place_error: place_order 抛出的异常（用于失败分支）
        hide_terminal_until_cancel: True 时撤单前订单恒为 NEW（模拟等待超时后读终态）
    """

    def __init__(self, position, *, step="0.1", fills=None, ticker="100", bids=None,
                 reject_reduce_only_times=0, cancel_order_error=None,
                 place_error=None, hide_terminal_until_cancel=False):
        self.position = Decimal(str(position))
        self.step = Decimal(str(step))
        self.fills = list(fills or [])
        self.ticker = ticker
        self.bids = bids if bids is not None else [["99.9", "1"]]
        self.orders = {}
        self.submissions = []
        self.cancel_all_calls = 0
        self.cancel_order_calls = 0
        self._seq = 0
        self._reject_left = reject_reduce_only_times
        self._cancel_order_error = cancel_order_error
        self._place_error = place_error
        self._hide_terminal = hide_terminal_until_cancel
        self._cancel_called = False

    async def get_symbol_info(self, symbol):
        return {"stepSize": str(self.step), "tickSize": "0.01"}

    async def get_position(self, symbol=None):
        return [{"symbol": symbol or "BTCUSDT", "positionAmt": str(self.position)}]

    async def get_orderbook(self, symbol, limit=5):
        return {"bids": [list(b) for b in self.bids]}

    async def get_ticker(self, symbol):
        return {"lastPrice": self.ticker}

    async def place_order(self, symbol, side, quantity=None, price=None,
                          order_type="MARKET", **kwargs):
        if self._place_error is not None:
            raise self._place_error
        if self._reject_left > 0:
            self._reject_left -= 1
            raise BinanceAPIError(-2022, "ReduceOnly Order is rejected.")
        reduce_only = bool(kwargs.get("reduce_only"))
        script = self.fills[self._seq] if self._seq < len(self.fills) else {
            "executed": quantity, "status": "FILLED"}
        self._seq += 1
        order_id = 1000 + self._seq
        executed = Decimal(str(script.get("executed", quantity)))
        if reduce_only:
            # 减仓单真实语义：交易所按持仓上限截断成交量，天然防反向
            executed = min(executed, abs(self.position))
        self.position += -executed if side == "SELL" else executed
        order = {
            "orderId": order_id,
            "clientOrderId": kwargs.get("client_order_id") or f"sq{order_id}",
            "status": script.get("status", "FILLED"),
            "executedQty": str(executed),
            "origQty": str(quantity),
            "avgPrice": "100",
            "symbol": symbol,
        }
        self.orders[order_id] = order
        self.submissions.append({
            "side": side, "qty": Decimal(str(quantity)),
            "order_type": order_type, "price": price, "reduce_only": reduce_only,
        })
        return order

    async def get_order(self, symbol, order_id):
        order = dict(self.orders[order_id])
        if self._hide_terminal and not self._cancel_called:
            order["status"] = "NEW"
            order["executedQty"] = "0"
        return order

    async def get_order_by_client_id(self, symbol, client_order_id):
        for order in self.orders.values():
            if order["clientOrderId"] == client_order_id:
                return order
        return None

    async def cancel_order(self, symbol, order_id=None, client_order_id=None):
        self.cancel_order_calls += 1
        self._cancel_called = True
        if self._cancel_order_error is not None:
            raise self._cancel_order_error
        return {"status": "CANCELED"}

    async def cancel_all_algo_orders(self, symbol):
        self.cancel_all_calls += 1
        return {"code": 200}


def _close_cfg(**over):
    """构造 new_coin close_position 配置（默认极短间隔加速测试）。"""
    cfg = {
        "max_retries": 3,
        "retry_interval": 0,
        "poll_interval": 0.001,
        "timeout": 0.05,
        "reduce_only": True,
        "sync_before_reduce_only": True,
        "position_confirm_retries": 2,
        "position_confirm_interval": 0,
    }
    cfg.update(over)
    return cfg


def _newcoin_executor(client, close_cfg=None):
    """构造 new_coin TradingExecutor（绕过 __init__，仅装配平仓路径所需依赖）。"""
    ex = NewCoinExecutor.__new__(NewCoinExecutor)
    ex.binance_api = client
    ex.db = MagicMock()
    ex.notification = MagicMock()
    ex.notification.send = AsyncMock()
    ex.config = {
        "strategy": {"name": "new_coin", "record_name": "新币做空策略"},
        "notification": {"project": "new_coin"},
        "trading": {"close_position": close_cfg or _close_cfg()},
    }
    ex._ownership = {
        "my_record_name": "新币做空策略", "competing_record_names": [],
        "enabled": True, "claim_ttl_minutes": 30,
        "claim_cleanup_interval_minutes": 10, "lock_timeout_seconds": 5,
    }
    ex._last_claim_cleanup_at = 0.0
    ex._get_symbol_precision = AsyncMock(return_value=(Decimal("0.1"), Decimal("0.001")))
    ex._update_short_position_closed = AsyncMock()
    return ex


# ============================================================
# R03：new_coin _close_position
# ============================================================
class TestNewCoinClosePosition:
    """R03-AC1..AC6：new_coin 平仓复用统一减仓助手的行为收缩。"""

    async def test_ac1_recalc_remaining_before_resubmit(self):
        """AC1：首笔成交 0.4 后，次笔必须按剩余量 0.6 提交（而非初始 1.0）"""
        client = FakeExchange(position=-1, step="0.001", fills=[
            {"executed": "0.4", "status": "CANCELED"},
            {"executed": "0.6", "status": "FILLED"},
        ])
        ex = _newcoin_executor(client)

        ok = await ex._close_position("BTCUSDT", Decimal("1.0"), "止盈")

        assert ok is True
        assert client.position == Decimal("0")
        assert client.submissions[0]["qty"] == Decimal("1")
        assert client.submissions[1]["qty"] == Decimal("0.6")

    async def test_ac2_truncated_micro_no_reverse_returns_failure(self):
        """AC2：剩余量截断为 0 且仍有持仓、微仓清零失败 → 不提交反向单且返回失败"""
        client = FakeExchange(position=-0.05, step="0.1",
                              bids=[], ticker="0",
                              place_error=BinanceAPIError(-1001, "下单被拒"))
        ex = _newcoin_executor(client)

        ok = await ex._close_position("BTCUSDT", Decimal("1.0"), "止损")

        assert ok is False
        # 绝不允许出现反向（SELL 平空）单
        assert client.submissions == []
        ex.notification.send.assert_awaited()

    async def test_ac3_all_submissions_reduce_only(self):
        """AC3：限价 + 市价兜底提交全部带 reduceOnly"""
        client = FakeExchange(position=-1, step="0.001", fills=[
            {"executed": "0.4", "status": "CANCELED"},
            {"executed": "0.6", "status": "FILLED"},
        ])
        ex = _newcoin_executor(client)

        assert await ex._close_position("BTCUSDT", Decimal("1.0"), "止盈") is True
        assert client.submissions
        assert all(s["reduce_only"] is True for s in client.submissions)

    async def test_ac4_cancel_not_found_accumulates_executed_qty(self):
        """AC4：等待超时撤单抛 -2011 → 读终态累加已成交 0.4，次笔仅补 0.6"""
        client = FakeExchange(position=-1, step="0.001",
                              hide_terminal_until_cancel=True,
                              cancel_order_error=BinanceAPIError(-2011, "Order does not exist"),
                              fills=[
                                  {"executed": "0.4", "status": "CANCELED"},
                                  {"executed": "0.6", "status": "FILLED"},
                              ])
        ex = _newcoin_executor(client)

        ok = await ex._close_position("BTCUSDT", Decimal("1.0"), "止盈")

        assert ok is True
        assert client.cancel_order_calls >= 1
        assert client.submissions[1]["qty"] == Decimal("0.6")
        assert client.position == Decimal("0")

    async def test_ac5_target_capped_no_reverse(self):
        """AC5：target 超真实持仓 → 以真实持仓为准，不产生反向仓"""
        client = FakeExchange(position=-0.3, step="0.001",
                              fills=[{"executed": "0.3", "status": "FILLED"}])
        ex = _newcoin_executor(client)

        ok = await ex._close_position("BTCUSDT", Decimal("1.5"), "止盈")

        assert ok is True
        assert client.submissions[0]["qty"] == Decimal("0.3")
        assert client.position == Decimal("0")

    async def test_ac6_minus_2022_cancel_conditional_then_retry(self):
        """AC6：-2022 被拒 → 先撤同向条件单再重试成功"""
        client = FakeExchange(position=-1, step="0.001", reject_reduce_only_times=1,
                              fills=[{"executed": "1", "status": "FILLED"}])
        ex = _newcoin_executor(client)

        assert await ex._close_position("BTCUSDT", Decimal("1.0"), "止盈") is True
        assert client.cancel_all_calls == 1
        assert client.position == Decimal("0")
        assert len(client.submissions) == 1  # 被拒的一笔不计入提交记录

    async def test_market_fallback_submits_only_remaining(self):
        """限价阶段仅成交 0.3 → 市价兜底只提交剩余 0.7"""
        client = FakeExchange(position=-1, step="0.001", fills=[
            {"executed": "0.3", "status": "CANCELED"},
            {"executed": "0", "status": "CANCELED"},
            {"executed": "0", "status": "CANCELED"},
            {"executed": "0", "status": "CANCELED"},
            {"executed": "0.7", "status": "FILLED"},
        ])
        ex = _newcoin_executor(client)

        assert await ex._close_position("BTCUSDT", Decimal("1.0"), "止盈") is True
        last = client.submissions[-1]
        assert last["order_type"] == "MARKET"
        assert last["qty"] == Decimal("0.7")
        assert client.position == Decimal("0")

    async def test_no_position_is_idempotent_success(self):
        """幂等边界：已无空头持仓 → 视为已平（True）且标记 short_positions 关闭"""
        client = FakeExchange(position=0, fills=[])
        ex = _newcoin_executor(client)

        assert await ex._close_position("BTCUSDT", Decimal("1.0"), "止盈") is True
        assert client.submissions == []
        ex._update_short_position_closed.assert_awaited()

    async def test_stuck_partial_returns_failure_and_alerts(self):
        """多次仍无法归零 → 返回失败并告警（保留仓位待下轮）"""
        client = FakeExchange(position=-1, step="0.1", fills=[
            {"executed": "0.5", "status": "CANCELED"},
            {"executed": "0", "status": "CANCELED"},
            {"executed": "0", "status": "CANCELED"},
            {"executed": "0", "status": "CANCELED"},
            {"executed": "0", "status": "CANCELED"},
            {"executed": "0", "status": "CANCELED"},
            {"executed": "0", "status": "CANCELED"},
            {"executed": "0", "status": "CANCELED"},
        ])
        ex = _newcoin_executor(client)

        assert await ex._close_position("BTCUSDT", Decimal("1.0"), "止损") is False
        ex.notification.send.assert_awaited()


# ============================================================
# R04：hrs close_position 结构化结果
# ============================================================
def _hrs_executor(client, cfg=None):
    """构造 hrs TradingExecutor（绕过 __init__，仅装配平仓路径依赖）。"""
    ex = HrsExecutor.__new__(HrsExecutor)
    ex.binance_api = client
    ex.order_type_close = "LIMIT"
    ex.config = cfg or {"trading": {
        "retry_count": 1, "retry_interval": 0,
        "close_order": {
            "wait_timeout_seconds": 0.05, "poll_interval_seconds": 0.001,
            "reduce_only": True, "sync_before_reduce_only": True,
            "keep_protection_on_partial": True,
        },
    }}
    return ex


class TestHrsClosePosition:
    """R04：close_position 返回结构化 CloseOutcome，不再把 NEW 回执当成功。"""

    async def test_filled_returns_success_outcome(self):
        client = FakeExchange(position=-1, step="0.001",
                              fills=[{"executed": "1", "status": "FILLED"}])
        ex = _hrs_executor(client)

        outcome = await ex.close_position("BTCUSDT", "short", 1.0, reason="时间止损")

        assert outcome.success is True
        assert outcome.status == CLOSE_FILLED
        assert client.position == Decimal("0")
        assert client.submissions[0]["side"] == "BUY"

    async def test_no_position_returns_filled_zero(self):
        client = FakeExchange(position=0, fills=[])
        ex = _hrs_executor(client)

        outcome = await ex.close_position("BTCUSDT", "short", 1.0, reason="时间止损")

        assert outcome.success is True
        assert outcome.status == CLOSE_FILLED
        assert outcome.closed_qty == Decimal("0")

    async def test_unfilled_returns_non_success(self):
        """等待超时且未成交（撤单后读终态 executedQty=0）→ 非成功结构化结果"""
        client = FakeExchange(position=-1, step="0.1",
                              hide_terminal_until_cancel=True,
                              fills=[{"executed": "0", "status": "CANCELED"},
                                     {"executed": "0", "status": "CANCELED"}])
        ex = _hrs_executor(client)

        outcome = await ex.close_position("BTCUSDT", "short", 1.0, reason="移动止盈")

        assert outcome.success is False
        assert outcome.status in (CLOSE_FAILED, CLOSE_ACCEPTED)
        assert client.position == Decimal("-1")

    async def test_exception_returns_failed_outcome(self):
        client = FakeExchange(position=-1, fills=[])
        client.get_position = AsyncMock(side_effect=RuntimeError("查询失败"))
        ex = _hrs_executor(client)

        outcome = await ex.close_position("BTCUSDT", "short", 1.0, reason="时间止损")

        assert isinstance(outcome, CloseOutcome)
        assert outcome.success is False
        assert outcome.status == CLOSE_FAILED


# ============================================================
# R04：hrs strategy 守卫（_finalize_close_if_filled）
# ============================================================
def _hrs_guard(notify=True, keep_protection=True):
    """构造仅装配清理守卫所需依赖的 HRSStrategy（绕过 __init__）。"""
    guard = HRSStrategy.__new__(HRSStrategy)
    guard.config = {"trading": {"close_order": {
        "keep_protection_on_partial": keep_protection}}}
    guard._writeback_pnl = AsyncMock()
    guard.position_manager = MagicMock()
    guard.position_manager.cancel_all_orders = AsyncMock()
    guard.position_manager.remove_position = MagicMock()
    guard._replenish_single_position = AsyncMock()
    guard.notification_client = MagicMock()
    guard.notification_client.send = AsyncMock()
    guard._should_notify = MagicMock(return_value=notify)
    return guard


async def _call_guard(guard, result):
    """以统一入参调用守卫，返回是否完成清理。"""
    return await HRSStrategy._finalize_close_if_filled(
        guard, symbol="BTCUSDT", direction="short", result=result,
        entry_price=100.0, exit_price=110.0, quantity=1.0, close_reason="时间止损",
    )


class TestHrsCloseGuard:
    """R04-AC1..AC5：依据结构化结果决定是否清理、保留仓位、重建保护。"""

    async def test_ac3_filled_cleans_up(self):
        """AC3：完全成交 → 回写盈亏 + 撤保护单 + 删持仓"""
        guard = _hrs_guard()
        result = CloseOutcome(CLOSE_FILLED, Decimal("1"), Decimal("1"), True, "ok",
                              raw={"orderId": 1})

        assert await _call_guard(guard, result) is True
        guard._writeback_pnl.assert_awaited_once()
        guard.position_manager.cancel_all_orders.assert_called_once_with("BTCUSDT")
        guard.position_manager.remove_position.assert_called_once_with("BTCUSDT")

    async def test_ac1_ac2_failed_keeps_position(self):
        """AC1/AC2：失败 → 不清理、不撤单、不删仓，告警可观测"""
        guard = _hrs_guard()
        result = CloseOutcome(CLOSE_FAILED, Decimal("0"), Decimal("1"), False, "未成交")

        assert await _call_guard(guard, result) is False
        guard._writeback_pnl.assert_not_awaited()
        guard.position_manager.cancel_all_orders.assert_not_called()
        guard.position_manager.remove_position.assert_not_called()
        guard.notification_client.send.assert_awaited()

    async def test_ac1_ac2_accepted_keeps_position(self):
        """AC2：仅受理（ACCEPTED）等同失败处理，保留仓位与保护单"""
        guard = _hrs_guard()
        result = CloseOutcome(CLOSE_ACCEPTED, Decimal("0"), Decimal("1"), False, "已受理未成交")

        assert await _call_guard(guard, result) is False
        guard._writeback_pnl.assert_not_awaited()
        guard.position_manager.remove_position.assert_not_called()
        guard._replenish_single_position.assert_not_awaited()

    async def test_ac4_partial_replenishes_protection(self):
        """AC4：部分成交 → 不删仓、按剩余量重建保护、告警"""
        guard = _hrs_guard()
        result = CloseOutcome(CLOSE_PARTIAL, Decimal("0.4"), Decimal("1"), False, "部分成交")

        assert await _call_guard(guard, result) is False
        guard.position_manager.remove_position.assert_not_called()
        guard._replenish_single_position.assert_awaited_once_with("BTCUSDT")
        guard.notification_client.send.assert_awaited()

    async def test_ac4_partial_keep_protection_disabled_no_replenish(self):
        """AC4：keep_protection_on_partial=False → 保留旧单，不重建"""
        guard = _hrs_guard(keep_protection=False)
        result = CloseOutcome(CLOSE_PARTIAL, Decimal("0.4"), Decimal("1"), False, "部分成交")

        assert await _call_guard(guard, result) is False
        guard._replenish_single_position.assert_not_awaited()

    async def test_ac5_repeated_failures_idempotent(self):
        """AC5：多轮失败幂等 → 不清理、不产生超额单，每轮均告警"""
        guard = _hrs_guard()
        result = CloseOutcome(CLOSE_FAILED, Decimal("0"), Decimal("1"), False, "未成交")

        assert await _call_guard(guard, result) is False
        assert await _call_guard(guard, result) is False
        guard.position_manager.cancel_all_orders.assert_not_called()
        guard.position_manager.remove_position.assert_not_called()
        assert guard.notification_client.send.await_count == 2

    async def test_alert_suppressed_when_disabled(self):
        """告警开关关闭 → 不发送（但保留仓位语义不变）"""
        guard = _hrs_guard(notify=False)
        result = CloseOutcome(CLOSE_FAILED, Decimal("0"), Decimal("1"), False, "未成交")

        assert await _call_guard(guard, result) is False
        guard.notification_client.send.assert_not_awaited()


# ============================================================
# T13 / T16（new_coin 侧）接线烟测
# ============================================================
class TestNewCoinWiring:
    """T13 入场等待委托统一助手；T16 开仓占用冲突跳过。"""

    async def test_t13_entry_wait_delegates_to_shared(self):
        """T13：_wait_for_order_fill 返回结构化 OrderFillResult"""
        client = FakeExchange(position=-1, step="0.001",
                              fills=[{"executed": "1", "status": "FILLED"}])
        ex = _newcoin_executor(client)
        order = await client.place_order("BTCUSDT", "SELL", quantity=Decimal("1"),
                                         order_type="MARKET", reduce_only=False)

        fill = await ex._wait_for_order_fill(
            "BTCUSDT", order["orderId"], timeout_seconds=1,
            client_order_id=order["clientOrderId"],
        )

        assert fill.is_filled is True
        assert fill.executed_qty == Decimal("1")

    async def test_t16_claim_conflict_skips_open(self):
        """T16：占用冲突 → 跳过开仓并返回明确原因，不中断主循环"""
        ex = _newcoin_executor(FakeExchange(position=0))
        ex.baseline_ready = True
        ex._leverage_valid = True
        ex._get_account_balance = AsyncMock(return_value=Decimal("100"))
        ex._is_symbol_occupied = AsyncMock(return_value=False)
        ex._claim_symbol = AsyncMock(
            return_value={"claimed": False, "owner": "MTPCS策略", "claim_id": None})
        ex._notify_claim_conflict = AsyncMock()
        ex._release_claim_quietly = AsyncMock()

        result = await ex.execute_short("BTCUSDT", {"total_score": 8.0}, 100.0)

        assert result == (None, "该币种已被其他策略持有，跳过开仓")
        ex._notify_claim_conflict.assert_awaited_once_with("BTCUSDT", "MTPCS策略")
        assert ex.binance_api.submissions == []  # 冲突路径不产生任何下单