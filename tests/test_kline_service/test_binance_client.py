"""
测试币安 API 客户端 BinanceClient
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime


class MockResponse:
    """模拟 aiohttp 响应，支持 async with 上下文"""

    def __init__(self, status=200, json_data=None, text_data=""):
        self.status = status
        self._json_data = json_data or {}
        self._text_data = text_data

    async def json(self):
        return self._json_data

    async def text(self):
        return self._text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class CallTracker:
    """包装函数并跟踪调用次数"""

    def __init__(self, func):
        self._func = func
        self.call_count = 0
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        self.calls.append((args, kwargs))
        return self._func(*args, **kwargs)


def make_request_mock(responses):
    """
    创建模拟的 session.request 方法。

    由于 async with 需要一个 async context manager 而不是 coroutine，
    这里直接返回 MockResponse 实例（它是 async context manager），
    而不是 AsyncMock（调用后返回 coroutine，无法被 async with 直接使用）。

    Args:
        responses: 单个 MockResponse 或 MockResponse 列表

    Returns:
        可用于 session.request 的 CallTracker 包装函数
    """
    if not isinstance(responses, list):
        responses = [responses]

    iterator = iter(responses)

    def request_func(method, url, **kwargs):
        try:
            return next(iterator)
        except StopIteration:
            return responses[-1]

    return CallTracker(request_func)


class TestBinanceClient:
    """测试 BinanceClient"""

    @pytest.fixture
    def binance_client(self):
        """创建 BinanceClient 实例，手动重置内部状态"""
        from services.kline_service.core.binance_client import BinanceClient
        client = BinanceClient(base_url="https://fapi.binance.com")
        # 使用 MagicMock 作为 session，close 方法设为 AsyncMock 以支持 await
        client.session = MagicMock()
        client.session.close = AsyncMock()
        client.request_count = 0
        client.last_request_time = 0
        return client

    # ==================== 连接管理 ====================

    @pytest.mark.asyncio
    async def test_connect_create_session(self, binance_client):
        """测试 connect 创建新的 HTTP 会话"""
        # 重置 session 为 None，模拟初次连接
        binance_client.session = None
        with patch("services.kline_service.core.binance_client.aiohttp") as mock_aiohttp:
            mock_session = AsyncMock()
            mock_aiohttp.ClientSession.return_value = mock_session

            await binance_client.connect()

            assert binance_client.session is mock_session
            mock_aiohttp.ClientSession.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_already_connected(self, binance_client):
        """测试 connect 在已连接时不再创建新会话"""
        original_session = binance_client.session
        with patch("services.kline_service.core.binance_client.aiohttp") as mock_aiohttp:
            await binance_client.connect()

            # 不创建新会话
            mock_aiohttp.ClientSession.assert_not_called()
            assert binance_client.session is original_session

    @pytest.mark.asyncio
    async def test_disconnect(self, binance_client):
        """测试 disconnect 关闭会话"""
        await binance_client.disconnect()
        assert binance_client.session is None

    @pytest.mark.asyncio
    async def test_disconnect_no_session(self, binance_client):
        """测试 disconnect 在无会话时不会报错"""
        binance_client.session = None
        await binance_client.disconnect()  # 不应抛出异常

    # ==================== _request ====================

    @pytest.mark.asyncio
    async def test_request_200_success(self, binance_client):
        """测试 _request 200 成功"""
        resp = MockResponse(status=200, json_data={"key": "value"})
        binance_client.session.request = make_request_mock(resp)

        result = await binance_client._request("GET", "/fapi/v1/test")

        assert result == {"key": "value"}
        assert binance_client.session.request.call_count == 1

    @pytest.mark.asyncio
    async def test_request_429_rate_limit(self, binance_client):
        """测试 _request 429 频率限制后自动重试"""
        # 第一次请求 429，第二次 200
        resp_429 = MockResponse(status=429, text_data="rate limit")
        resp_200 = MockResponse(status=200, json_data={"success": True})

        binance_client.session.request = make_request_mock([resp_429, resp_200])

        result = await binance_client._request("GET", "/fapi/v1/test", retry=3)

        assert result == {"success": True}
        assert binance_client.session.request.call_count == 2

    @pytest.mark.asyncio
    async def test_request_400_client_error(self, binance_client):
        """测试 _request 400 客户端错误，返回 None"""
        resp = MockResponse(status=400, json_data={})
        binance_client.session.request = make_request_mock(resp)

        result = await binance_client._request("GET", "/fapi/v1/test")

        assert result is None

    @pytest.mark.asyncio
    async def test_request_500_server_error(self, binance_client):
        """测试 _request 500 服务器错误，返回 None"""
        resp = MockResponse(status=500, json_data={})
        binance_client.session.request = make_request_mock(resp)

        result = await binance_client._request("GET", "/fapi/v1/test")

        assert result is None

    @pytest.mark.asyncio
    async def test_request_timeout(self, binance_client):
        """测试 _request 网络超时后重试"""
        def raise_timeout(*args, **kwargs):
            raise TimeoutError("timeout")
        binance_client.session.request = raise_timeout

        result = await binance_client._request("GET", "/fapi/v1/test", retry=2)

        assert result is None
        # 每次重试都会调用一次，retry=2 表示最多重试 2 次
        # 并且每次重试前尝试 1 次，共 2 次调用

    @pytest.mark.asyncio
    async def test_request_retry_exhausted(self, binance_client):
        """测试 _request 重试耗尽后返回 None"""
        resp_429 = MockResponse(status=429, text_data="rate limit")
        binance_client.session.request = make_request_mock([resp_429, resp_429, resp_429])

        result = await binance_client._request("GET", "/fapi/v1/test", retry=3)

        assert result is None
        assert binance_client.session.request.call_count == 3

    # ==================== get_klines ====================

    @pytest.mark.asyncio
    async def test_get_klines_success(self, binance_client, mock_kline_data):
        """测试 get_klines 正常返回"""
        with patch.object(binance_client, "_request") as mock_request:
            mock_request.return_value = mock_kline_data

            result = await binance_client.get_klines("BTCUSDT", "15m")

            assert result == mock_kline_data
            mock_request.assert_called_once_with(
                "GET", "/fapi/v1/klines",
                {"symbol": "BTCUSDT", "interval": "15m", "limit": 500}
            )

    @pytest.mark.asyncio
    async def test_get_klines_with_params(self, binance_client):
        """测试 get_klines 参数透传"""
        with patch.object(binance_client, "_request") as mock_request:
            mock_request.return_value = []

            await binance_client.get_klines(
                "BTCUSDT", "1h", start_time=1000, end_time=2000, limit=100
            )

            mock_request.assert_called_once_with(
                "GET", "/fapi/v1/klines",
                {
                    "symbol": "BTCUSDT",
                    "interval": "1h",
                    "limit": 100,
                    "startTime": 1000,
                    "endTime": 2000,
                }
            )

    @pytest.mark.asyncio
    async def test_get_klines_api_error(self, binance_client):
        """测试 get_klines API 异常时返回 None"""
        with patch.object(binance_client, "_request") as mock_request:
            mock_request.return_value = None

            result = await binance_client.get_klines("BTCUSDT", "15m")

            assert result is None

    # ==================== get_all_symbols ====================

    @pytest.mark.asyncio
    async def test_get_all_symbols_success(self, binance_client):
        """测试 get_all_symbols 正常返回"""
        mock_exchange_info = {
            "symbols": [
                {"symbol": "BTCUSDT", "status": "TRADING"},
                {"symbol": "ETHUSDT", "status": "TRADING"},
                {"symbol": "OLDUSDT", "status": "BREAK"},
            ]
        }
        with patch.object(binance_client, "_request") as mock_request:
            mock_request.return_value = mock_exchange_info

            result = await binance_client.get_all_symbols()

            assert result == ["BTCUSDT", "ETHUSDT"]
            mock_request.assert_called_once_with("GET", "/fapi/v1/exchangeInfo")

    @pytest.mark.asyncio
    async def test_get_all_symbols_api_error(self, binance_client):
        """测试 get_all_symbols API 异常时返回空列表"""
        with patch.object(binance_client, "_request") as mock_request:
            mock_request.return_value = None

            result = await binance_client.get_all_symbols()

            assert result == []

    # ==================== get_symbol_info ====================

    @pytest.mark.asyncio
    async def test_get_symbol_info_success(self, binance_client):
        """测试 get_symbol_info 正常返回"""
        mock_exchange_info = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                    ],
                }
            ]
        }
        with patch.object(binance_client, "_request") as mock_request:
            mock_request.return_value = mock_exchange_info

            result = await binance_client.get_symbol_info("BTCUSDT")

            assert result is not None
            assert result["symbol"] == "BTCUSDT"
            assert result["base_asset"] == "BTC"
            assert result["quote_asset"] == "USDT"
            assert result["status"] == "TRADING"

    @pytest.mark.asyncio
    async def test_get_symbol_info_not_found(self, binance_client):
        """测试 get_symbol_info 找不到交易对时返回 None"""
        mock_exchange_info = {"symbols": []}
        with patch.object(binance_client, "_request") as mock_request:
            mock_request.return_value = mock_exchange_info

            result = await binance_client.get_symbol_info("NONEXISTENT")

            assert result is None

    @pytest.mark.asyncio
    async def test_get_symbol_info_api_error(self, binance_client):
        """测试 get_symbol_info API 异常时返回 None"""
        with patch.object(binance_client, "_request") as mock_request:
            mock_request.return_value = None

            result = await binance_client.get_symbol_info("BTCUSDT")

            assert result is None

    # ==================== get_server_time ====================

    @pytest.mark.asyncio
    async def test_get_server_time_success(self, binance_client):
        """测试 get_server_time 正常返回"""
        with patch.object(binance_client, "_request") as mock_request:
            mock_request.return_value = {"serverTime": 1700000000000}

            result = await binance_client.get_server_time()

            assert result == 1700000000000

    @pytest.mark.asyncio
    async def test_get_server_time_error(self, binance_client):
        """测试 get_server_time 异常时返回 None"""
        with patch.object(binance_client, "_request") as mock_request:
            mock_request.return_value = None

            result = await binance_client.get_server_time()

            assert result is None

    # ==================== 请求计数 ====================

    @pytest.mark.asyncio
    async def test_request_count_increment(self, binance_client):
        """测试请求计数递增"""
        resp = MockResponse(status=200, json_data={})
        binance_client.session.request = make_request_mock(resp)

        initial_count = binance_client.request_count
        await binance_client._request("GET", "/fapi/v1/test")
        assert binance_client.request_count == initial_count + 1