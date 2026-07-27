"""
测试：下架币种缓存机制
验证 _delisted_symbols 能正确缓存4108错误并避免重复请求
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '.'))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from strategies.new_coin.detector import ListingDetector
from shared.binance_api import BinanceClient
from shared.database import DatabaseManager


@pytest.fixture
def detector():
    """创建检测器实例（mock依赖）"""
    binance_api = MagicMock(spec=BinanceClient)
    db = MagicMock(spec=DatabaseManager)
    config = {
        'detector': {
            'exclude_patterns': ['_'],
            'oi_rank_limit': 10,
            'oi_cache_ttl_seconds': 300,
            'max_listing_hours': 48
        }
    }
    d = ListingDetector(binance_api=binance_api, db=db, config=config)
    d._new_coin_records_loaded = True
    return d


@pytest.mark.asyncio
async def test_delisted_symbol_first_call_caches_and_logs(detector):
    """首次遇到4108错误：记录缓存 + 打印info日志（非warning）"""
    # mock _request 抛出4108错误
    detector.binance_api._request = AsyncMock(
        side_effect=Exception("[-4108] Symbol is on delivering")
    )

    with patch('strategies.new_coin.detector.logger.info') as mock_info, \
         patch('strategies.new_coin.detector.logger.warning') as mock_warning:
        result = await detector._fetch_oi_for_symbol("BTCU")

        # 返回0.0
        assert result == 0.0
        # 加入缓存
        assert "BTCU" in detector._delisted_symbols
        # 打印info日志（含"币种已下架"）
        mock_info.assert_called_once()
        assert "币种已下架" in mock_info.call_args[0][0]
        # 不打印warning
        mock_warning.assert_not_called()


@pytest.mark.asyncio
async def test_delisted_symbol_second_call_skips_api(detector):
    """已缓存的下架币种：跳过API请求，直接返回"""
    detector._delisted_symbols.add("BTCU")
    detector.binance_api._request = AsyncMock()  # 不应被调用

    with patch('strategies.new_coin.detector.logger.info') as mock_info, \
         patch('strategies.new_coin.detector.logger.warning') as mock_warning:
        result = await detector._fetch_oi_for_symbol("BTCU")

        assert result == 0.0
        # API 未被调用
        detector.binance_api._request.assert_not_called()
        # 无日志
        mock_info.assert_not_called()
        mock_warning.assert_not_called()


@pytest.mark.asyncio
async def test_other_error_still_logs_warning(detector):
    """非4108错误：不走缓存，打印warning日志"""
    detector.binance_api._request = AsyncMock(
        side_effect=Exception("网络超时")
    )

    with patch('strategies.new_coin.detector.logger.info') as mock_info, \
         patch('strategies.new_coin.detector.logger.warning') as mock_warning:
        result = await detector._fetch_oi_for_symbol("BTCU")

        assert result == 0.0
        # 不加入下架缓存（非4108错误）
        assert "BTCU" not in detector._delisted_symbols
        # 不打印info
        mock_info.assert_not_called()
        # 打印warning（含"获取OI失败"）
        mock_warning.assert_called_once()
        assert "获取OI失败" in mock_warning.call_args[0][0]


@pytest.mark.asyncio
async def test_get_recent_coins_oi_filters_delisted(detector):
    """get_recent_coins_oi 过滤已下架币种"""
    # 设置_new_coin_records（使用当前时间-1小时，确保在时间窗口内）
    from datetime import datetime, timedelta, timezone
    recent_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    detector._new_coin_records = {
        "BTCU": {"detected_at": recent_time, "first_oi": 1000000},
        "ETHU": {"detected_at": recent_time, "first_oi": 2000000},
        "VALIDUSDT": {"detected_at": recent_time, "first_oi": 3000000},
    }
    # BTCU已下架，ETHU正常
    detector._delisted_symbols.add("BTCU")
    # VALIDUSDT正常返回
    detector.binance_api._request = AsyncMock(
        side_effect=lambda method, endpoint, **kw: (
            {"openInterest": "5000000"} if kw.get('params', {}).get('symbol') == "VALIDUSDT"
            else {"openInterest": "3000000"} if kw.get('params', {}).get('symbol') == "ETHU"
            else {"openInterest": "0"}
        )
    )

    with patch('strategies.new_coin.detector.logger.info') as mock_info:
        oi_list = await detector.get_recent_coins_oi(limit=10)

        # 只有2个非下架币种被请求
        assert len(oi_list) == 2
        # BTCU 被过滤，不生成API调用
        api_calls = [c[0][1] for c in detector.binance_api._request.call_args_list]
        assert all("BTCU" not in str(call) for call in api_calls)


@pytest.mark.asyncio
async def test_successful_oi_fetch_not_cached_as_delisted(detector):
    """正常获取OI的币种不会被加入下架列表"""
    detector.binance_api._request = AsyncMock(
        return_value={"openInterest": "5000000"}
    )

    result = await detector._fetch_oi_for_symbol("VALIDUSDT")

    assert result == 5000000.0
    assert "VALIDUSDT" not in detector._delisted_symbols


if __name__ == '__main__':
    pytest.main([__file__, '-v'])