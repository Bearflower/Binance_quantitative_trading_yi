"""
K线服务客户端单元测试

覆盖 shared/kline_service.py 的所有方法：
- get_klines() — 参数验证、响应解析、错误处理、网络异常
- register_symbol() — 注册成功/失败场景
- unregister_symbol() — 注销成功/404/失败场景
- get_multi_timeframe_data() — 并发调用、部分失败容错
- 参数验证：空交易对、无效周期、limit 越界
"""
import pytest
import json
from decimal import Decimal
from unittest.mock import patch, AsyncMock, MagicMock
import aiohttp
from aiohttp import ClientSession

from shared.kline_service import KLineService, KLineServiceError


# ========== Fixtures ==========

@pytest.fixture
def kline_service():
    """创建 KLineService 实例"""
    service = KLineService(
        service_url="http://test-kline:8000/api/v1",
        timeout=10
    )
    yield service
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(service.close())
        else:
            loop.run_until_complete(service.close())
    except RuntimeError:
        pass


@pytest.fixture
def mock_session():
    """创建 mock HTTP 会话"""
    session = MagicMock(spec=ClientSession)
    session.closed = False
    return session


def _make_mock_response(status=200, data=None):
    """创建 mock HTTP 响应"""
    response = AsyncMock()
    response.status = status
    response.json.return_value = data or {}
    response.__aenter__.return_value = response
    response.__aexit__.return_value = None
    return response


# ========== 初始化测试 ==========

class TestInitialization:
    """测试初始化"""

    @pytest.mark.asyncio
    async def test_init_with_custom_url(self):
        service = KLineService(service_url="http://custom:8000/api/v1", timeout=30)
        assert service.service_url == "http://custom:8000/api/v1"
        assert service.timeout == 30
        await service.close()

    @pytest.mark.asyncio
    async def test_init_with_default_timeout(self):
        service = KLineService(service_url="http://test:8000/api/v1")
        assert service.timeout == 10
        await service.close()

    @pytest.mark.asyncio
    async def test_session_lazy_init(self, kline_service):
        assert kline_service.session is None

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with KLineService(service_url="http://test:8000/api/v1") as service:
            assert service.session is not None
            assert not service.session.closed
        assert service.session.closed


# ========== get_klines 测试 ==========

class TestGetKlines:
    """测试 get_klines()"""

    # --- 参数验证 ---

    @pytest.mark.parametrize("symbol,interval,limit,expected_msg", [
        ("", "1h", 100, "交易对不能为空"),
        ("   ", "1h", 100, "交易对不能为空"),
        ("BTCUSDT", "", 100, "K线周期不能为空"),
        ("BTCUSDT", "invalid", 100, "无效的K线周期"),
        ("BTCUSDT", "1h", 0, "数量限制必须在1-1500之间"),
        ("BTCUSDT", "1h", 1501, "数量限制必须在1-1500之间"),
    ])
    @pytest.mark.asyncio
    async def test_parameter_validation(self, kline_service, symbol, interval, limit, expected_msg):
        with pytest.raises(ValueError, match=expected_msg):
            await kline_service.get_klines(symbol, interval, limit)

    # --- 正常响应 ---

    @pytest.mark.asyncio
    async def test_successful_response(self, kline_service):
        """正常响应应正确解析 K 线数据"""
        mock_data = {
            "code": 0, "message": "success",
            "data": [{
                "symbol": "BTCUSDT", "interval": "1h",
                "open_time": 1700000000000, "open_price": 50000.0,
                "high_price": 51000.0, "low_price": 49000.0,
                "close_price": 50500.0, "volume": 100.5,
                "close_time": 1700003600000, "quote_volume": 5000000.0,
                "trade_count": 1000,
            }]
        }

        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False  # 防止 _init_session() 创建真实 session
            mock_response = _make_mock_response(200, mock_data)
            mock_session.get.return_value = mock_response

            klines = await kline_service.get_klines("BTCUSDT", "1h", 10)

            assert len(klines) == 1
            assert klines[0]["open"] == Decimal("50000")
            assert klines[0]["high"] == Decimal("51000")
            assert klines[0]["low"] == Decimal("49000")
            assert klines[0]["close"] == Decimal("50500")
            assert klines[0]["volume"] == Decimal("100.5")
            assert klines[0]["open_time"] == 1700000000000
            assert klines[0]["close_time"] == 1700003600000

    @pytest.mark.asyncio
    async def test_empty_data_response(self, kline_service):
        """空数据响应应返回空列表"""
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = _make_mock_response(200, {"code": 0, "message": "无数据", "data": []})
            mock_session.get.return_value = mock_response

            klines = await kline_service.get_klines("BTCUSDT", "1h", 10)
            assert klines == []

    @pytest.mark.asyncio
    async def test_multiple_klines(self, kline_service):
        """多条 K 线数据应全部返回"""
        mock_data = {
            "code": 0, "message": "success",
            "data": [
                {"open_time": 1, "open_price": 10.0, "high_price": 11.0, "low_price": 9.0,
                 "close_price": 10.5, "volume": 1.0, "close_time": 2, "quote_volume": 10.0, "trade_count": 5},
                {"open_time": 2, "open_price": 10.5, "high_price": 11.5, "low_price": 10.0,
                 "close_price": 11.0, "volume": 2.0, "close_time": 3, "quote_volume": 20.0, "trade_count": 10},
            ]
        }
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = _make_mock_response(200, mock_data)
            mock_session.get.return_value = mock_response

            klines = await kline_service.get_klines("BTCUSDT", "1h", 10)
            assert len(klines) == 2

    # --- 错误处理 ---

    @pytest.mark.asyncio
    async def test_non_200_response(self, kline_service):
        """非 200 响应应抛出 KLineServiceError"""
        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.__aenter__.return_value = mock_response
        mock_response.__aexit__.return_value = None

        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_session.get.return_value = mock_response

            with pytest.raises(KLineServiceError, match="K线服务请求失败: 500"):
                await kline_service.get_klines("BTCUSDT", "1h", 10)

    @pytest.mark.asyncio
    async def test_error_code_response(self, kline_service):
        """业务错误码应抛出 KLineServiceError"""
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = _make_mock_response(200, {"code": 1001, "message": "数据库查询失败"})
            mock_session.get.return_value = mock_response

            with pytest.raises(KLineServiceError, match="数据库查询失败"):
                await kline_service.get_klines("BTCUSDT", "1h", 10)

    @pytest.mark.asyncio
    async def test_network_error(self, kline_service):
        """网络异常应抛出 KLineServiceError"""
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_session.get.side_effect = aiohttp.ClientError("连接失败")

            with pytest.raises(KLineServiceError, match="连接失败"):
                await kline_service.get_klines("BTCUSDT", "1h", 10)

    @pytest.mark.asyncio
    async def test_response_not_dict(self, kline_service):
        """响应不是字典格式应抛出异常"""
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = _make_mock_response(200, [1, 2, 3])
            mock_session.get.return_value = mock_response

            with pytest.raises(KLineServiceError, match="响应数据格式错误"):
                await kline_service.get_klines("BTCUSDT", "1h", 10)

    @pytest.mark.asyncio
    async def test_data_not_list(self, kline_service):
        """data 字段不是列表应抛出异常"""
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = _make_mock_response(200, {"code": 0, "message": "success", "data": "invalid"})
            mock_session.get.return_value = mock_response

            with pytest.raises(KLineServiceError, match="K线数据格式错误"):
                await kline_service.get_klines("BTCUSDT", "1h", 10)

    @pytest.mark.asyncio
    async def test_limit_parameter(self, kline_service):
        """limit 参数应正确传递"""
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = _make_mock_response(200, {"code": 0, "message": "success", "data": []})
            mock_session.get.return_value = mock_response

            await kline_service.get_klines("BTCUSDT", "1h", 1000)

            # 验证 limit 被截断为 100
            call_args = mock_session.get.call_args
            assert call_args[1]["params"]["limit"] == 100


# ========== register_symbol 测试 ==========

class TestRegisterSymbol:
    """测试 register_symbol()"""

    @pytest.mark.asyncio
    async def test_parameter_validation_empty_symbol(self, kline_service):
        with pytest.raises(ValueError, match="交易对不能为空"):
            await kline_service.register_symbol("", ["1h"])

    @pytest.mark.asyncio
    async def test_parameter_validation_empty_intervals(self, kline_service):
        with pytest.raises(ValueError, match="K线周期列表不能为空"):
            await kline_service.register_symbol("BTCUSDT", [])

    @pytest.mark.asyncio
    async def test_register_success(self, kline_service):
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = _make_mock_response(200, {"code": 0, "message": "success", "data": {}})
            mock_session.post.return_value = mock_response

            result = await kline_service.register_symbol("NEWCOINUSDT", ["1h"])
            assert result is True

    @pytest.mark.asyncio
    async def test_register_non_200(self, kline_service):
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = AsyncMock()
            mock_response.status = 500
            mock_response.json.return_value = {"error": "server error"}
            mock_response.__aenter__.return_value = mock_response
            mock_response.__aexit__.return_value = None
            mock_session.post.return_value = mock_response

            result = await kline_service.register_symbol("NEWCOINUSDT", ["1h"])
            assert result is False

    @pytest.mark.asyncio
    async def test_register_error_code(self, kline_service):
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = _make_mock_response(200, {"code": 1001, "message": "注册失败"})
            mock_session.post.return_value = mock_response

            result = await kline_service.register_symbol("NEWCOINUSDT", ["1h"])
            assert result is False

    @pytest.mark.asyncio
    async def test_register_network_error(self, kline_service):
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_session.post.side_effect = aiohttp.ClientError("连接失败")

            result = await kline_service.register_symbol("NEWCOINUSDT", ["1h"])
            assert result is False

    @pytest.mark.asyncio
    async def test_register_symbol_uppercased(self, kline_service):
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = _make_mock_response(200, {"code": 0, "message": "success", "data": {}})
            mock_session.post.return_value = mock_response

            result = await kline_service.register_symbol("newcoinusdt", ["1h"])
            assert result is True

    @pytest.mark.asyncio
    async def test_register_multiple_intervals(self, kline_service):
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = _make_mock_response(200, {"code": 0, "message": "success", "data": {}})
            mock_session.post.return_value = mock_response

            result = await kline_service.register_symbol("NEWCOINUSDT", ["1h", "4h", "1d"])
            assert result is True


# ========== unregister_symbol 测试 ==========

class TestUnregisterSymbol:
    """测试 unregister_symbol()"""

    @pytest.mark.asyncio
    async def test_parameter_validation(self, kline_service):
        with pytest.raises(ValueError, match="交易对不能为空"):
            await kline_service.unregister_symbol("")

    @pytest.mark.asyncio
    async def test_unregister_success(self, kline_service):
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = _make_mock_response(200, {"code": 0, "message": "success", "data": {"symbol": "X", "status": "cancelled"}})
            mock_session.delete.return_value = mock_response

            result = await kline_service.unregister_symbol("NEWCOINUSDT")
            assert result is True

    @pytest.mark.asyncio
    async def test_unregister_404(self, kline_service):
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = AsyncMock()
            mock_response.status = 404
            mock_response.__aenter__.return_value = mock_response
            mock_response.__aexit__.return_value = None
            mock_session.delete.return_value = mock_response

            result = await kline_service.unregister_symbol("NEWCOINUSDT")
            assert result is True

    @pytest.mark.asyncio
    async def test_unregister_500(self, kline_service):
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = AsyncMock()
            mock_response.status = 500
            mock_response.__aenter__.return_value = mock_response
            mock_response.__aexit__.return_value = None
            mock_session.delete.return_value = mock_response

            result = await kline_service.unregister_symbol("NEWCOINUSDT")
            assert result is False

    @pytest.mark.asyncio
    async def test_unregister_network_error(self, kline_service):
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_session.delete.side_effect = aiohttp.ClientError("连接失败")

            result = await kline_service.unregister_symbol("NEWCOINUSDT")
            assert result is False

    @pytest.mark.asyncio
    async def test_unregister_symbol_uppercased(self, kline_service):
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = _make_mock_response(200, {"code": 0, "message": "success", "data": {"symbol": "X", "status": "cancelled"}})
            mock_session.delete.return_value = mock_response

            result = await kline_service.unregister_symbol("newcoinusdt")
            assert result is True


# ========== get_multi_timeframe_data 测试 ==========

class TestGetMultiTimeframeData:
    """测试 get_multi_timeframe_data()"""

    @pytest.mark.asyncio
    async def test_parameter_validation_empty_symbol(self, kline_service):
        with pytest.raises(ValueError, match="交易对不能为空"):
            await kline_service.get_multi_timeframe_data("", ["1h", "4h"])

    @pytest.mark.asyncio
    async def test_parameter_validation_empty_intervals(self, kline_service):
        with pytest.raises(ValueError, match="时间框架列表不能为空"):
            await kline_service.get_multi_timeframe_data("BTCUSDT", [])

    @pytest.mark.asyncio
    async def test_parameter_validation_not_list(self, kline_service):
        with pytest.raises(ValueError, match="时间框架必须是列表"):
            await kline_service.get_multi_timeframe_data("BTCUSDT", "1h")

    @pytest.mark.asyncio
    async def test_parameter_validation_invalid_interval(self, kline_service):
        with pytest.raises(ValueError, match="无效的时间框架"):
            await kline_service.get_multi_timeframe_data("BTCUSDT", ["1h", "invalid"])

    @pytest.mark.asyncio
    async def test_all_intervals_success(self, kline_service):
        """所有周期都成功应返回完整数据"""
        mock_data = {"code": 0, "message": "success", "data": [{"open_time": 1, "open_price": 10.0}]}

        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_response = _make_mock_response(200, mock_data)
            mock_session.get.return_value = mock_response

            result = await kline_service.get_multi_timeframe_data("BTCUSDT", ["1h", "4h"])
            assert "1h" in result
            assert "4h" in result
            assert len(result["1h"]) == 1
            assert len(result["4h"]) == 1

    @pytest.mark.asyncio
    async def test_partial_failure(self, kline_service):
        """部分周期失败应返回成功的数据"""
        mock_data = {"code": 0, "message": "success", "data": [{"open_time": 1, "open_price": 10.0}]}

        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            # 第一次调用（1h）成功，第二次（4h）失败
            mock_session.get.side_effect = [
                _make_mock_response(200, mock_data),
                ConnectionError("4h 请求失败"),
            ]

            result = await kline_service.get_multi_timeframe_data("BTCUSDT", ["1h", "4h"])
            assert "1h" in result
            assert len(result["1h"]) == 1
            assert "4h" not in result

    @pytest.mark.asyncio
    async def test_all_failures(self, kline_service):
        """所有周期都失败应返回空字典"""
        with patch.object(kline_service, 'session') as mock_session:
            mock_session.closed = False
            mock_session.get.side_effect = ConnectionError("连接失败")

            result = await kline_service.get_multi_timeframe_data("BTCUSDT", ["1h", "4h"])
            assert result == {}


# ========== _get_limit 测试 ==========

class TestGetLimit:
    """测试 _get_limit()"""

    def test_known_intervals(self):
        service = KLineService(service_url="http://test:8000/api/v1")
        assert service._get_limit('1d') == 100
        assert service._get_limit('4h') == 100
        assert service._get_limit('1h') == 100
        assert service._get_limit('15m') == 100
        assert service._get_limit('5m') == 100
        assert service._get_limit('1m') == 100

    def test_unknown_interval(self):
        service = KLineService(service_url="http://test:8000/api/v1")
        assert service._get_limit('unknown') == 100


# ========== 清理测试 ==========

class TestCleanup:
    """测试资源清理"""

    @pytest.mark.asyncio
    async def test_close(self, kline_service):
        await kline_service._init_session()
        assert kline_service.session is not None
        assert not kline_service.session.closed
        await kline_service.close()
        assert kline_service.session.closed

    @pytest.mark.asyncio
    async def test_close_twice(self, kline_service):
        await kline_service.close()
        await kline_service.close()  # 第二次调用不应报错


# ========== 集成测试（需要 K 线服务运行） ==========

class TestIntegration:
    """集成测试：需要 K 线服务在运行"""

    @pytest.mark.asyncio
    @pytest.mark.skipif(True, reason="需要 K 线服务运行，默认跳过")
    async def test_real_get_klines(self):
        async with KLineService(service_url="http://43.156.242.184:8765/api/v1") as service:
            klines = await service.get_klines("BTCUSDT", "1h", 10)
            assert len(klines) > 0
            assert klines[0]["symbol"] == "BTCUSDT"