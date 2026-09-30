"""测试 shared/binance_api.py 的读/写分级重试与幂等标识（R02）

覆盖：
- R02-AC1：写请求超时 → 按 clientOrderId 核对到已受理 → 交易所侧只接受 1 个订单
- R02-AC2：写请求超时后先发「按 clientOrderId 查单」，不再调用下单端点
- R02-AC3：读请求在瞬时错误下仍按配置次数重试（行为不回归）
- R02-AC4：同一交易意图复用相同 newClientOrderId；不同意图 ID 不同
- R02-AC5：现有调用方接口向后兼容
- R02-F5：条件单写接口的分级/幂等策略一致（PM 不传 newClientOrderId）
- D4：api_retry.enabled=false 时回退既有「按读重试」语义

所有测试使用假 HTTP：直接替换 client._request_once / client._request，不访问真实交易所。
"""

import asyncio
from decimal import Decimal

import aiohttp
import pytest

from shared import api_retry_config
from shared.api_retry_config import get_api_retry, reset_cache
from shared.binance_api import BinanceAPIError, BinanceClient, UnknownOrderResultError


@pytest.fixture(autouse=True)
def fast_config(monkeypatch):
    """加速重试：读延迟/退避归零、核对间隔归零；测试后清缓存避免污染其他用例"""
    monkeypatch.setenv("API_RETRY_READ_DELAY_SECONDS", "0")
    monkeypatch.setenv("API_RETRY_READ_BACKOFF", "1")
    monkeypatch.setenv("API_RETRY_ORDER_VERIFY_INTERVAL_SECONDS", "0")
    reset_cache()
    yield
    reset_cache()


def _make_client(**kwargs) -> BinanceClient:
    """构建测试客户端（testnet，不发起真实网络请求）"""
    return BinanceClient(
        api_key="test_api_key_123456",
        api_secret="test_api_secret_123456",
        testnet=True,
        **kwargs,
    )


class TestReadRetry:
    """R02-AC3：读路径重试行为不回归"""

    @pytest.mark.asyncio
    async def test_read_retries_on_transient_error(self):
        client = _make_client()
        calls = {"n": 0}

        async def fake_once(method, endpoint, params=None, signed=True):
            calls["n"] += 1
            if calls["n"] < 3:
                raise aiohttp.ClientError("瞬时网络错误")
            return {"ok": True}

        client._request_once = fake_once
        result = await client._request("GET", "/fapi/v2/positionRisk", None, True)
        assert result == {"ok": True}
        assert calls["n"] == 3  # 配置 max_retries=3 生效
        await client.close()

    @pytest.mark.asyncio
    async def test_idempotent_post_routes_to_read_path(self):
        """幂等写（如 set_leverage）走读路径，保留重试语义"""
        client = _make_client()
        calls = {"n": 0}

        async def fake_once(method, endpoint, params=None, signed=True):
            calls["n"] += 1
            if calls["n"] == 1:
                raise aiohttp.ClientError("瞬时网络错误")
            return {"ok": True}

        client._request_once = fake_once
        result = await client._request("POST", "/fapi/v1/leverage", {}, True, idempotent=True)
        assert result == {"ok": True}
        assert calls["n"] == 2
        await client.close()


class TestWriteGradeRetry:
    """R02-AC1/AC2/F3：非幂等写请求不盲重发，先核对再决策"""

    @pytest.mark.asyncio
    async def test_unknown_result_verified_returns_single_order(self):
        """AC1/AC2：超时后核对到订单，只调用下单端点 1 次"""
        client = _make_client()
        calls = {"n": 0}
        verify_calls = {"n": 0}

        async def fake_once(method, endpoint, params=None, signed=True):
            calls["n"] += 1
            raise asyncio.TimeoutError("下单超时")

        found = {"orderId": 999, "status": "NEW", "executedQty": "0", "origQty": "1"}

        async def fake_by_cid(symbol, client_order_id):
            verify_calls["n"] += 1
            return found

        client._request_once = fake_once
        client.get_order_by_client_id = fake_by_cid

        result = await client._request(
            "POST", "/papi/v1/um/order", {"symbol": "BTCUSDT"}, True, idempotency_key="sq1"
        )
        assert result is found
        assert calls["n"] == 1  # 下单端点只调用一次，未盲重发
        assert verify_calls["n"] >= 1  # 确实发起了按 clientOrderId 查单
        await client.close()

    @pytest.mark.asyncio
    async def test_unknown_result_unverified_raises(self):
        """F3：核对不到 → 抛 UnknownOrderResultError（保守，不重复提交）"""
        client = _make_client()

        async def fake_once(method, endpoint, params=None, signed=True):
            raise asyncio.TimeoutError("下单超时")

        async def fake_by_cid(symbol, client_order_id):
            return None

        client._request_once = fake_once
        client.get_order_by_client_id = fake_by_cid

        with pytest.raises(UnknownOrderResultError):
            await client._request(
                "POST", "/papi/v1/um/order", {"symbol": "BTCUSDT"}, True, idempotency_key="sq1"
            )
        await client.close()

    @pytest.mark.asyncio
    async def test_disabled_falls_back_to_read_retry(self):
        """D4：api_retry.enabled=false → POST 走读路径重试（回退既有语义）"""
        import os

        os.environ["API_RETRY_ENABLED"] = "false"
        reset_cache()
        try:
            client = _make_client()
            calls = {"n": 0}

            async def fake_once(method, endpoint, params=None, signed=True):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise asyncio.TimeoutError("瞬时超时")
                return {"ok": True}

            client._request_once = fake_once
            result = await client._request(
                "POST", "/papi/v1/um/order", {"symbol": "BTCUSDT"}, True, idempotency_key="sq1"
            )
            assert result == {"ok": True}
            assert calls["n"] == 2  # 回退为“全 method 重试”
            await client.close()
        finally:
            os.environ.pop("API_RETRY_ENABLED", None)
            reset_cache()


class TestPlaceOrderIdempotency:
    """R02-AC4/AC5/F2：newClientOrderId 稳定复用与向后兼容"""

    @pytest.mark.asyncio
    async def test_same_intent_reuses_id_different_intent_new_id(self):
        client = _make_client()

        async def fake_symbol_info(symbol):
            return {"stepSize": "0.001", "tickSize": Decimal("0.01")}

        captured = []

        async def fake_request(method, endpoint, params=None, signed=True, **kwargs):
            captured.append(dict(params))
            return {"orderId": len(captured)}

        client.get_symbol_info = fake_symbol_info
        client._request = fake_request

        # 同一交易意图两次提交 → 相同 ID
        await client.place_order("BTCUSDT", "BUY", Decimal("0.001"), client_order_id="sq-fixed-1")
        await client.place_order("BTCUSDT", "BUY", Decimal("0.001"), client_order_id="sq-fixed-1")
        assert captured[0]["newClientOrderId"] == "sq-fixed-1"
        assert captured[1]["newClientOrderId"] == "sq-fixed-1"

        # 不传 ID（不同意图）→ 自动生成且互不相同
        await client.place_order("BTCUSDT", "BUY", Decimal("0.001"))
        await client.place_order("BTCUSDT", "BUY", Decimal("0.001"))
        assert captured[2]["newClientOrderId"] != captured[3]["newClientOrderId"]
        await client.close()

    @pytest.mark.asyncio
    async def test_kwarg_new_client_order_id_backward_compatible(self):
        """AC5：既有调用方通过 kwargs 传 newClientOrderId 仍可用"""
        client = _make_client()

        async def fake_symbol_info(symbol):
            return {"stepSize": "0.001", "tickSize": Decimal("0.01")}

        captured = []

        async def fake_request(method, endpoint, params=None, signed=True, **kwargs):
            captured.append(dict(params))
            return {"orderId": 1}

        client.get_symbol_info = fake_symbol_info
        client._request = fake_request

        # 不传任何幂等参数（旧调用方）也能运行，且自动带上 newClientOrderId
        result = await client.place_order("BTCUSDT", "BUY", Decimal("0.001"))
        assert result["orderId"] == 1
        assert captured[0].get("newClientOrderId")

        # 通过 kwargs 传 newClientOrderId（兼容旧写法）
        await client.place_order("ETHUSDT", "BUY", Decimal("0.01"), newClientOrderId="legacy-1")
        assert captured[1]["newClientOrderId"] == "legacy-1"
        await client.close()

    def test_generate_client_order_id_prefix_and_length(self):
        client = _make_client()
        cid = client._generate_client_order_id()
        assert cid.startswith(get_api_retry()["client_order_id_prefix"])
        assert len(cid) <= 36


class TestConditionalOrderIdempotency:
    """R02-F5：条件单写接口分级一致；PM 不传 newClientOrderId（避免 -1106）"""

    @pytest.mark.asyncio
    async def test_pm_conditional_omits_client_order_id(self):
        client = _make_client(use_unified_account=True)

        async def fake_symbol_info(symbol):
            return {"stepSize": "0.001", "tickSize": Decimal("0.01")}

        async def fake_ticker(symbol):
            return {"lastPrice": "100"}

        captured = []

        async def fake_request(method, endpoint, params=None, signed=True, **kwargs):
            captured.append(dict(params))
            return {"algoId": 1}

        client.get_symbol_info = fake_symbol_info
        client.get_ticker = fake_ticker
        client._request = fake_request

        await client.place_conditional_order(
            "BTCUSDT", "SELL", Decimal("90"), Decimal("0.001"), order_type="STOP_MARKET"
        )
        assert "newClientOrderId" not in captured[0]
        await client.close()

    @pytest.mark.asyncio
    async def test_non_pm_conditional_includes_client_order_id(self):
        client = _make_client(use_unified_account=False)

        async def fake_symbol_info(symbol):
            return {"stepSize": "0.001", "tickSize": Decimal("0.01")}

        async def fake_ticker(symbol):
            return {"lastPrice": "100"}

        captured = []

        async def fake_request(method, endpoint, params=None, signed=True, **kwargs):
            captured.append(dict(params))
            return {"orderId": 1}

        client.get_symbol_info = fake_symbol_info
        client.get_ticker = fake_ticker
        client._request = fake_request

        await client.place_conditional_order(
            "BTCUSDT", "SELL", Decimal("90"), Decimal("0.001"), order_type="STOP_MARKET"
        )
        assert captured[0].get("newClientOrderId")
        await client.close()


class TestGetOrderByClientId:
    """R02-F3：按 clientOrderId 查单，订单不存在返回 None，其他错误抛出"""

    @pytest.mark.asyncio
    async def test_not_found_returns_none(self):
        client = _make_client()

        async def fake_request(method, endpoint, params=None, signed=True, **kwargs):
            raise BinanceAPIError(-2013, "Order does not exist.")

        client._request = fake_request
        assert await client.get_order_by_client_id("BTCUSDT", "cid-1") is None
        await client.close()

    @pytest.mark.asyncio
    async def test_other_error_raises(self):
        client = _make_client()

        async def fake_request(method, endpoint, params=None, signed=True, **kwargs):
            raise BinanceAPIError(-1022, "Signature not valid.")

        client._request = fake_request
        with pytest.raises(BinanceAPIError):
            await client.get_order_by_client_id("BTCUSDT", "cid-1")
        await client.close()


class TestConfigSource:
    """配置项来自 shared/api_retry_config.yaml，不得硬编码"""

    def test_read_defaults_present(self):
        cfg = api_retry_config.get_api_retry()
        assert cfg["read"]["max_retries"] >= 0
        assert cfg["read"]["backoff"] >= 1
        assert cfg["client_order_id_prefix"]

    def test_env_override_applies(self, monkeypatch):
        monkeypatch.setenv("API_RETRY_READ_MAX_RETRIES", "7")
        reset_cache()
        try:
            assert api_retry_config.get_api_retry()["read"]["max_retries"] == 7
        finally:
            reset_cache()