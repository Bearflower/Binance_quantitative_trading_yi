"""测试 shared/reduce_only_close.py 的统一减仓平仓助手（R03 + R04-F3）

覆盖：
- R03-AC1：position=-1，首笔 BUY 成交 0.4 后撤单 → 次笔提交 0.6；最终 position=0；成功
- R03-AC2：剩余量经精度截断为 0 且仍有持仓 → 返回失败且不提交任何反向单
- R03-AC3：所有平仓提交均带减仓约束（reduceOnly）
- R03-AC5：以交易所真实持仓为准，不产生反向仓
- R03-AC6：-2022 被拒 → 先撤同向条件单后重试成功
- D1：平仓单强制 reduceOnly
- D3：微仓减仓清零；清零失败告警

全部使用假交易所客户端，不访问真实交易所。
"""

from decimal import Decimal

import pytest

from shared import api_retry_config
from shared.api_retry_config import reset_cache
from shared.binance_api import BinanceAPIError, UnknownOrderResultError
from shared.reduce_only_close import (
    CLOSE_FAILED,
    CLOSE_FILLED,
    CLOSE_PARTIAL,
    close_remaining,
)


@pytest.fixture(autouse=True)
def fast_config(monkeypatch):
    """等待助手首查延迟/轮询归零，避免测试变慢"""
    monkeypatch.setenv("ORDER_FILL_PM_ORDER_VISIBILITY_DELAY_SECONDS", "0")
    monkeypatch.setenv("ORDER_FILL_CHECK_INTERVAL_SECONDS", "0.001")
    reset_cache()
    yield
    reset_cache()


class FakeExchange:
    """假交易所：维护带符号持仓，按脚本对每笔下单施加成交量效果

    Args:
        position: 初始带符号持仓（正=多头，负=空头）
        fills: 每笔下单的脚本，元素形如 {"executed": "0.4", "status": "CANCELED"}
        step: 数量步长
        reject_reduce_only_times: 前 N 次下单抛 -2022（模拟 ReduceOnly 被拒）
    """

    def __init__(self, position, fills, step="0.1", reject_reduce_only_times=0):
        self.position = Decimal(str(position))
        self.fills = list(fills)
        self.step = step
        self.orders = {}
        self.submissions = []
        self.cancel_all_calls = 0
        self.cancel_order_calls = 0
        self.raise_unknown = False
        self._seq = 0
        self._reject_left = reject_reduce_only_times

    async def get_symbol_info(self, symbol):
        return {"stepSize": self.step, "tickSize": Decimal("0.01")}

    async def get_position(self, symbol=None):
        return [{"symbol": symbol or "BTCUSDT", "positionAmt": str(self.position)}]

    async def place_order(self, symbol, side, quantity=None, price=None,
                          order_type="MARKET", **kwargs):
        if self.raise_unknown:
            raise UnknownOrderResultError(
                "POST", "/papi/v1/um/order", symbol=symbol, client_order_id="x"
            )
        if self._reject_left > 0:
            self._reject_left -= 1
            raise BinanceAPIError(-2022, "ReduceOnly Order is rejected.")

        reduce_only = bool(kwargs.get("reduce_only"))
        script = self.fills[self._seq] if self._seq < len(self.fills) else {
            "executed": quantity, "status": "FILLED"
        }
        self._seq += 1
        order_id = 1000 + self._seq
        executed = Decimal(str(script.get("executed", quantity)))
        # 减仓单真实语义：交易所按持仓上限截断成交量，天然防反向
        if reduce_only:
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
            "side": side,
            "qty": Decimal(str(quantity)),
            "order_type": order_type,
            "price": price,
            "reduce_only": reduce_only,
        })
        return order

    async def get_order(self, symbol, order_id):
        return self.orders[order_id]

    async def get_order_by_client_id(self, symbol, client_order_id):
        for order in self.orders.values():
            if order["clientOrderId"] == client_order_id:
                return order
        return None

    async def cancel_order(self, symbol, order_id=None, client_order_id=None):
        self.cancel_order_calls += 1
        return {"status": "CANCELED"}

    async def cancel_all_algo_orders(self, symbol):
        self.cancel_all_calls += 1
        return {"code": 200}


def _close(client, target, **kwargs):
    """调用 close_remaining，默认使用极短间隔加速测试"""
    kwargs.setdefault("retry_interval", 0)
    kwargs.setdefault("position_confirm_interval", 0)
    kwargs.setdefault("timeout_seconds", 1)
    return close_remaining(client, "BTCUSDT", Decimal(str(target)), **kwargs)


class TestRemainingQuantityRecalc:
    """R03-AC1/AC3/F1/F2：每次提交前重算剩余量、撤单后累加成交量"""

    @pytest.mark.asyncio
    async def test_partial_then_recalc(self):
        """AC1：首笔成交 0.4 后次笔提交 0.6，最终归零且成功"""
        client = FakeExchange(
            position=-1,
            fills=[
                {"executed": "0.4", "status": "CANCELED"},
                {"executed": "0.6", "status": "FILLED"},
            ],
        )
        outcome = await _close(client, 1)
        assert outcome.success is True
        assert outcome.status == CLOSE_FILLED
        assert outcome.closed_qty == Decimal("1")
        assert client.position == Decimal("0")
        # 次笔提交量必须为 0.6（而非初始 1），AC1 核心断言
        assert client.submissions[0]["qty"] == Decimal("1")
        assert client.submissions[1]["qty"] == Decimal("0.6")

    @pytest.mark.asyncio
    async def test_all_submissions_reduce_only(self):
        """AC3：所有平仓提交均带 reduceOnly"""
        client = FakeExchange(
            position=-1,
            fills=[{"executed": "0.5", "status": "CANCELED"},
                   {"executed": "0.5", "status": "FILLED"}],
        )
        await _close(client, 1)
        assert client.submissions
        assert all(s["reduce_only"] is True for s in client.submissions)


class TestMicroPositionAndNoReverse:
    """R03-AC2/AC5/D3：精度截断、微仓清零、防反向"""

    @pytest.mark.asyncio
    async def test_micro_truncated_returns_failure_without_order(self):
        """AC2：剩余量截断为 0 且禁用清零 → 失败且不提交任何单"""
        client = FakeExchange(position=-1, fills=[])
        outcome = await _close(client, 0.05, zero_micro_position=False)
        assert outcome.success is False
        assert outcome.status == CLOSE_FAILED
        assert client.submissions == []

    @pytest.mark.asyncio
    async def test_micro_position_cleared(self):
        """D3：微仓减仓清零（带 reduceOnly，交易所按持仓截断，不反向）"""
        client = FakeExchange(
            position=-0.05,
            fills=[{"executed": "0.05", "status": "FILLED"}],
            step="0.1",
        )
        outcome = await _close(client, 0.05)
        assert outcome.success is True
        assert client.position == Decimal("0")
        assert client.submissions[0]["reduce_only"] is True

    @pytest.mark.asyncio
    async def test_no_reverse_when_target_exceeds_position(self):
        """AC5：目标量大于真实持仓 → 以真实持仓为准，不产生反向仓"""
        client = FakeExchange(position=-0.3, fills=[{"executed": "0.3", "status": "FILLED"}])
        outcome = await _close(client, 1)
        assert outcome.success is True
        assert client.submissions[0]["qty"] == Decimal("0.3")
        assert client.position == Decimal("0")


class TestReduceOnlyRejection:
    """R03-AC6/D1：-2022 被拒 → 先撤同向条件单后重试成功"""

    @pytest.mark.asyncio
    async def test_minus_2022_cancel_conditional_and_retry(self):
        client = FakeExchange(
            position=-1,
            fills=[{"executed": "1", "status": "FILLED"}],
            reject_reduce_only_times=1,
        )
        outcome = await _close(client, 1)
        assert outcome.success is True
        assert client.cancel_all_calls == 1  # 已撤该币种条件单以解除冲突
        assert len(client.submissions) == 1  # 被拒的一笔不计入提交记录


class TestEdgeCases:
    """边界与降级路径"""

    @pytest.mark.asyncio
    async def test_no_position_is_noop_success(self):
        client = FakeExchange(position=0, fills=[])
        outcome = await _close(client, 1)
        assert outcome.success is True
        assert outcome.status == CLOSE_FILLED
        assert client.submissions == []

    @pytest.mark.asyncio
    async def test_unknown_order_result_no_resubmit(self):
        """结果未知 → 不重发，交由对账决定；最终未成交则失败"""
        client = FakeExchange(position=-1, fills=[])
        client.raise_unknown = True
        outcome = await _close(client, 1, max_retries=0)
        assert outcome.success is False
        assert client.submissions == []

    @pytest.mark.asyncio
    async def test_partial_outcome_when_stuck(self):
        """反复部分成交仍无法归零 → 返回部分成交且 success=False"""
        client = FakeExchange(
            position=-1,
            fills=[
                {"executed": "0.5", "status": "CANCELED"},
                {"executed": "0", "status": "CANCELED"},
            ],
        )
        outcome = await _close(client, 1, max_retries=1)
        assert outcome.success is False
        assert outcome.status == CLOSE_PARTIAL
        assert outcome.closed_qty > 0

    @pytest.mark.asyncio
    async def test_validation_empty_symbol(self):
        client = FakeExchange(position=-1, fills=[])
        with pytest.raises(ValueError):
            await close_remaining(client, "", Decimal("1"))

    @pytest.mark.asyncio
    async def test_validation_non_positive_target(self):
        client = FakeExchange(position=-1, fills=[])
        with pytest.raises(ValueError):
            await close_remaining(client, "BTCUSDT", Decimal("0"))