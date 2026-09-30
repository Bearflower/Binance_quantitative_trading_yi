"""测试 shared/order_fill_waiter.py 的统一订单终态等待助手（R06）

覆盖：
- R06-AC1：CANCELED + executedQty=0.4 → 部分成交(0.4)
- R06-AC2：超时撤单后重读终态 0.4 → 部分成交
- R06-AC3：超时撤单后重读终态 0 → 未成交
- R06-AC4：撤单抛 -2011 且实际已成交 → 查单确认成交量
- R06-AC5：FILLED 路径行为不变
- R06-F2：EXPIRED/REJECTED 且 executedQty>0 → 部分成交
- R06-F6：首查可见延迟来自配置（不硬编码）

全部使用假交易所客户端，不访问真实交易所。
"""

from decimal import Decimal

import pytest

from shared import api_retry_config
from shared.api_retry_config import reset_cache
from shared.binance_api import BinanceAPIError
from shared.order_fill_waiter import (
    STATUS_CANCELED_UNFILLED,
    STATUS_EXPIRED_UNFILLED,
    STATUS_FILLED,
    STATUS_PARTIAL,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    _resolve_visibility_delay,
    read_order_final_state,
    wait_order_final_state,
)


@pytest.fixture(autouse=True)
def fast_config(monkeypatch):
    """首查可见延迟与轮询间隔归零，避免测试变慢"""
    monkeypatch.setenv("ORDER_FILL_PM_ORDER_VISIBILITY_DELAY_SECONDS", "0")
    monkeypatch.setenv("ORDER_FILL_CHECK_INTERVAL_SECONDS", "0.001")
    reset_cache()
    yield
    reset_cache()


def _order(status, executed="0", orig="1", order_id=1, avg="0"):
    """构造交易所订单对象"""
    return {
        "orderId": order_id,
        "clientOrderId": f"c{order_id}",
        "status": status,
        "executedQty": executed,
        "origQty": orig,
        "avgPrice": avg,
    }


class FakeWaiterClient:
    """假交易所客户端：按脚本返回订单，支持撤单竞态/可见延迟模拟"""

    def __init__(self, open_sequence, final_order=None, cancel_error=None):
        self._open = list(open_sequence)  # dict 或 Exception；最后一项重复返回
        self.final_order = final_order
        self.cancel_error = cancel_error
        self.cancelled = False
        self.cancel_calls = 0
        self.read_calls = 0

    def _next_open(self):
        item = self._open[0]
        if len(self._open) > 1:
            self._open.pop(0)
        return item

    async def get_order(self, symbol, order_id):
        self.read_calls += 1
        if self.cancelled:
            return self.final_order
        item = self._next_open()
        if isinstance(item, Exception):
            raise item
        return item

    async def get_order_by_client_id(self, symbol, client_order_id):
        return await self.get_order(symbol, None)

    async def cancel_order(self, symbol, order_id=None, client_order_id=None):
        self.cancel_calls += 1
        self.cancelled = True  # 竞态：即使撤单抛错，订单也已进入终态
        if self.cancel_error is not None:
            raise self.cancel_error
        return {"status": "CANCELED"}


class TestTerminalStateParsing:
    """R06-F2/AC1/AC5：终态与部分成交识别"""

    @pytest.mark.asyncio
    async def test_canceled_with_partial_fill(self):
        """AC1：CANCELED + 0.4 → PARTIAL"""
        client = FakeWaiterClient([_order("CANCELED", executed="0.4", orig="1")])
        result = await wait_order_final_state(
            client, "BTCUSDT", order_id=1, timeout_seconds=1, check_interval=0.001,
            visibility_delay=0,
        )
        assert result.status == STATUS_PARTIAL
        assert result.executed_qty == Decimal("0.4")
        assert result.has_fill is True
        assert result.is_filled is False
        assert client.cancel_calls == 0  # 终态已到，不撤单

    @pytest.mark.asyncio
    async def test_filled_unchanged(self):
        """AC5：FILLED 路径行为不变"""
        client = FakeWaiterClient([_order("FILLED", executed="1", orig="1", avg="100.5")])
        result = await wait_order_final_state(
            client, "BTCUSDT", order_id=1, timeout_seconds=1, check_interval=0.001,
            visibility_delay=0,
        )
        assert result.status == STATUS_FILLED
        assert result.is_filled is True
        assert result.remaining_qty == Decimal("0")
        assert result.avg_price == Decimal("100.5")

    @pytest.mark.asyncio
    async def test_rejected_and_expired_without_fill(self):
        rejected = FakeWaiterClient([_order("REJECTED", executed="0", orig="1")])
        r1 = await wait_order_final_state(
            rejected, "BTCUSDT", order_id=1, timeout_seconds=1,
            check_interval=0.001, visibility_delay=0,
        )
        assert r1.status == STATUS_REJECTED
        assert r1.has_fill is False

        expired = FakeWaiterClient([_order("EXPIRED", executed="0", orig="1")])
        r2 = await wait_order_final_state(
            expired, "BTCUSDT", order_id=1, timeout_seconds=1,
            check_interval=0.001, visibility_delay=0,
        )
        assert r2.status == STATUS_EXPIRED_UNFILLED
        assert r2.has_fill is False

    @pytest.mark.asyncio
    async def test_rejected_with_partial_fill(self):
        """R06-F2：REJECTED 但 executedQty>0 仍按部分成交处理"""
        client = FakeWaiterClient([_order("REJECTED", executed="0.3", orig="1")])
        result = await wait_order_final_state(
            client, "BTCUSDT", order_id=1, timeout_seconds=1,
            check_interval=0.001, visibility_delay=0,
        )
        assert result.status == STATUS_PARTIAL
        assert result.executed_qty == Decimal("0.3")


class TestTimeoutAndCancelRace:
    """R06-AC2/AC3/AC4/F3/F4：超时撤单后读最终量、撤单竞态"""

    @pytest.mark.asyncio
    async def test_timeout_cancel_then_partial(self):
        """AC2：超时撤单后重读得到 executedQty=0.4 → 部分成交"""
        client = FakeWaiterClient(
            [_order("NEW", executed="0", orig="1")],
            final_order=_order("CANCELED", executed="0.4", orig="1"),
        )
        result = await wait_order_final_state(
            client, "BTCUSDT", order_id=1, timeout_seconds=0.05,
            check_interval=0.001, visibility_delay=0,
        )
        assert result.status == STATUS_PARTIAL
        assert result.executed_qty == Decimal("0.4")
        assert client.cancel_calls == 1

    @pytest.mark.asyncio
    async def test_timeout_cancel_then_unfilled(self):
        """AC3：超时撤单后重读得到 executedQty=0 → 未成交"""
        client = FakeWaiterClient(
            [_order("NEW", executed="0", orig="1")],
            final_order=_order("CANCELED", executed="0", orig="1"),
        )
        result = await wait_order_final_state(
            client, "BTCUSDT", order_id=1, timeout_seconds=0.05,
            check_interval=0.001, visibility_delay=0,
        )
        assert result.status == STATUS_CANCELED_UNFILLED
        assert result.executed_qty == Decimal("0")
        assert result.has_fill is False
        assert client.cancel_calls == 1

    @pytest.mark.asyncio
    async def test_cancel_race_minus_2011_treated_as_filled(self):
        """AC4：撤单抛 -2011（已成交/不存在）→ 查单确认成交量，不死判未成交"""
        client = FakeWaiterClient(
            [_order("NEW", executed="0", orig="1")],
            final_order=_order("FILLED", executed="1", orig="1"),
            cancel_error=BinanceAPIError(-2011, "Unknown order sent."),
        )
        result = await wait_order_final_state(
            client, "BTCUSDT", order_id=1, timeout_seconds=0.05,
            check_interval=0.001, visibility_delay=0,
        )
        assert result.status == STATUS_FILLED
        assert result.executed_qty == Decimal("1")
        assert client.cancel_calls == 1

    @pytest.mark.asyncio
    async def test_cancel_other_error_propagates(self):
        """非竞态撤单错误应上抛，不得静默吞掉"""
        client = FakeWaiterClient(
            [_order("NEW", executed="0", orig="1")],
            final_order=_order("CANCELED", executed="0", orig="1"),
            cancel_error=BinanceAPIError(-1003, "Too many requests."),
        )
        with pytest.raises(BinanceAPIError):
            await wait_order_final_state(
                client, "BTCUSDT", order_id=1, timeout_seconds=0.05,
                check_interval=0.001, visibility_delay=0,
            )


class TestVisibilityDelay:
    """R06-F6：首查 -2013 可见延迟在循环内消化重试"""

    @pytest.mark.asyncio
    async def test_minus_2013_then_visible(self):
        client = FakeWaiterClient([
            BinanceAPIError(-2013, "Order does not exist."),
            _order("FILLED", executed="1", orig="1"),
        ])
        result = await wait_order_final_state(
            client, "BTCUSDT", order_id=1, timeout_seconds=1,
            check_interval=0.001, visibility_delay=0,
        )
        assert result.status == STATUS_FILLED


class TestReadFinalState:
    """R06-F3/F4：仅读终态"""

    @pytest.mark.asyncio
    async def test_read_final_state_success(self):
        client = FakeWaiterClient([_order("CANCELED", executed="0.4", orig="1")])
        result = await read_order_final_state(
            client, "BTCUSDT", order_id=1, read_retries=2, retry_interval=0.001
        )
        assert result.status == STATUS_PARTIAL
        assert result.executed_qty == Decimal("0.4")

    @pytest.mark.asyncio
    async def test_read_final_state_unavailable_returns_unknown(self):
        """用尽重试仍不可见 → 保守返回 UNKNOWN（携带原始标识供审计）"""
        client = FakeWaiterClient([BinanceAPIError(-2013, "not visible")])
        result = await read_order_final_state(
            client, "BTCUSDT", order_id=7, read_retries=2, retry_interval=0.001
        )
        assert result.status == STATUS_UNKNOWN
        assert result.order_id == 7
        assert result.has_fill is False


class TestValidationAndConfig:
    @pytest.mark.asyncio
    async def test_requires_order_identifier(self):
        client = FakeWaiterClient([_order("FILLED")])
        with pytest.raises(ValueError):
            await wait_order_final_state(client, "BTCUSDT", timeout_seconds=1)

    def test_visibility_delay_from_config(self, monkeypatch):
        """延迟值取自配置（默认 0.5），可被环境变量覆盖，不得硬编码"""
        reset_cache()
        assert _resolve_visibility_delay(None) == pytest.approx(
            api_retry_config.get_order_fill()["pm_order_visibility_delay_seconds"]
        )
        monkeypatch.setenv("ORDER_FILL_PM_ORDER_VISIBILITY_DELAY_SECONDS", "0.123")
        reset_cache()
        try:
            assert _resolve_visibility_delay(None) == pytest.approx(0.123)
        finally:
            reset_cache()