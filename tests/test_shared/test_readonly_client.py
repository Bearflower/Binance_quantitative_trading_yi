"""只读 Binance 客户端安全边界测试

§2 实施：断言 6 个写方法均抛 ReadOnlyViolationError 且 0 次 HTTP，
只读方法正常放行；底层 _request 白名单对任何写端点在发起 HTTP 前拦截。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from shared.readonly_client import ReadOnlyBinanceClient, ReadOnlyViolationError


# ---------------------------------------------------------------------------
# 辅助：构建一个带 mock session 的 ReadOnlyBinanceClient 实例
# ---------------------------------------------------------------------------
def _make_client(**kwargs):
    """构造一个最小可用的 ReadOnlyBinanceClient（不连币安）"""
    from shared.binance_api import BinanceClient

    # 用 patch 避免真实 HTTP 初始化
    with patch.object(BinanceClient, '_init_session', new=AsyncMock()):
        client = ReadOnlyBinanceClient(
            api_key="fake",
            api_secret="fake",
            instance_id="test-shadow",
            **kwargs,
        )
    # 替换 session 为 MagicMock，以便断言未被调用
    client.session = MagicMock()
    client.session.request = AsyncMock()
    client.use_unified_account = False
    return client


# ---------------------------------------------------------------------------
# 6 个写方法：全部抛 ReadOnlyViolationError，且 session.request 0 次
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("method_name,args,kwargs", [
    ("place_order",       ("BTCUSDT", "BUY"),           {"order_type": "MARKET", "quantity": None}),
    ("place_conditional_order", ("BTCUSDT", "BUY"),     {"stop_price": 9999.0}),
    ("cancel_order",      ("BTCUSDT",),                 {"order_id": "123"}),
    ("cancel_algo_order", ("BTCUSDT", 1),               {}),
    ("cancel_all_algo_orders", ("BTCUSDT",),             {}),
    ("set_leverage",      ("BTCUSDT", 3),               {}),
])
async def test_write_methods_raise_and_zero_http(method_name, args, kwargs):
    """§2.4 安全边界：写方法抛异常且 0 次 HTTP"""
    client = _make_client()
    client.instance_id = "mtcps_xau_shadow"

    method = getattr(client, method_name)
    with pytest.raises(ReadOnlyViolationError) as exc_info:
        await method(*args, **kwargs)

    err = exc_info.value
    assert "mtcps_xau_shadow" in err.instance_id or "mtcps_xau_shadow" in str(err)
    # 关键：session.request 不应被调用（任何形式都不行）
    assert client.session.request.await_count == 0, (
        f"{method_name} 不应发起 HTTP 请求，但 session.request 被调用了 "
        f"{client.session.request.await_count} 次"
    )


# ---------------------------------------------------------------------------
# 只读方法放行：底层白名单对 GET 不拦截
# （test_request_underlayer_allows_get 已完整验证）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_read_method_pass_through_get():
    """只读方法底层 _request GET 放行"""
    client = _make_client()
    fake = {"symbols": ["BTCUSDT"]}
    with patch.object(
        type(client).__bases__[0], '_request',
        new_callable=AsyncMock, return_value=fake
    ):
        result = await client._request("GET", "/fapi/v1/exchangeInfo", {}, signed=False)
    assert result == fake


# ---------------------------------------------------------------------------
# _request 底层收口：任何 POST/PUT/DELETE 端点在 HTTP 发起前被拦截
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_request_underlayer_blocks_post():
    """底层 _request 白名单收口：POST 端点抛异常"""
    client = _make_client()
    with pytest.raises(ReadOnlyViolationError) as exc_info:
        await client._request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"}, signed=True)
    err = exc_info.value
    assert err.method == "POST" or "POST" in str(err)


@pytest.mark.asyncio
async def test_request_underlayer_allows_get():
    """底层 _request 白名单放行：GET 端点正常通过"""
    client = _make_client()
    fake = {"symbols": []}
    with patch.object(
        type(client).__bases__[0], '_request',
        new_callable=AsyncMock, return_value=fake
    ):
        result = await client._request("GET", "/fapi/v1/exchangeInfo", {}, signed=False)
    assert result == fake


# ---------------------------------------------------------------------------
# _request 底层收口：PUT / DELETE 一律拦截（哪怕端点不带写关键词）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_request_underlayer_blocks_put():
    """PUT 方法（如改保证金模式）在 HTTP 发起前拦截，0 次 HTTP"""
    client = _make_client()
    with pytest.raises(ReadOnlyViolationError):
        await client._request("PUT", "/fapi/v1/marginType", {"symbol": "BTCUSDT"}, signed=True)
    assert client.session.request.await_count == 0


@pytest.mark.asyncio
async def test_request_underlayer_blocks_delete():
    """DELETE 方法（撤单）在 HTTP 发起前拦截，0 次 HTTP"""
    client = _make_client()
    with pytest.raises(ReadOnlyViolationError):
        await client._request("DELETE", "/fapi/v1/order", {"symbol": "BTCUSDT", "orderId": 1}, signed=True)
    assert client.session.request.await_count == 0


# ---------------------------------------------------------------------------
# 写端点关键词显式覆盖：/leverage、/margin、/algo/order（POST 路径）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [
    "/fapi/v1/leverage",
    "/papi/v1/um/leverage",
    "/fapi/v1/marginType",
    "/papi/v1/um/algo/order",
    "/fapi/v1/algo/order",
])
async def test_request_blocks_write_endpoint_keywords(endpoint):
    """含 /leverage、/margin、/algo/order 关键词的写端点一律拦截"""
    client = _make_client()
    with pytest.raises(ReadOnlyViolationError) as exc_info:
        await client._request("POST", endpoint, {"symbol": "BTCUSDT"}, signed=True)
    assert client.session.request.await_count == 0
    assert endpoint in exc_info.value.endpoint


@pytest.mark.asyncio
async def test_get_order_query_endpoint_should_pass_through():
    """只读查询 GET /fapi/v1/order 应放行（当前被误拦）"""
    client = _make_client()
    fake = {"orderId": 1, "status": "FILLED"}
    with patch.object(
        type(client).__bases__[0], '_request',
        new_callable=AsyncMock, return_value=fake
    ):
        result = await client._request(
            "GET", "/fapi/v1/order",
            {"symbol": "BTCUSDT", "orderId": 1}, signed=True,
        )
    assert result == fake
