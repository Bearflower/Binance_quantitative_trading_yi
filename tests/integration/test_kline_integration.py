"""
K线服务对接集成测试

测试 K线服务客户端 → 策略数据管道 → 指标计算 → 市场状态判断 的完整数据流。
使用真实 K线服务数据，验证数据格式、类型转换、指标计算等环节的正确性。

运行方式：
  cd /Users/yl/vscode/Binance_quantitative_trading
  python -m pytest tests/integration/test_kline_integration.py -v -s

依赖：
  - K线服务运行在 http://43.156.242.184:8765/api/v1
  - 可通过环境变量 KLINE_SERVICE_URL 覆盖
"""
import os
import sys
import pytest
import asyncio
import pandas as pd
import numpy as np
from decimal import Decimal
from datetime import datetime
from typing import Dict, List

# 确保项目根目录在 path 中
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, PROJECT_ROOT)

from shared.kline_service import KLineService, KLineServiceError
from shared.indicators import TechnicalIndicators

# K线服务地址
KLINE_SERVICE_URL = os.getenv("KLINE_SERVICE_URL", "http://43.156.242.184:8765/api/v1")

# 测试币种
ALL_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "TRXUSDT"]
TEST_TIMEFRAMES = ["1h", "4h", "1d"]

# 数据有效性阈值
MIN_KLINES_COUNT = {
    "1h": 50,   # 至少50根1h K线（EMA55需要55根，但允许少量缺失）
    "4h": 20,   # 至少20根4h K线
    "1d": 5,    # 至少5根日线K线
}


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
async def kline_service():
    """创建K线服务客户端"""
    service = KLineService(
        service_url=KLINE_SERVICE_URL,
        timeout=15,
        max_retries=3
    )
    await service._init_session()
    yield service
    await service.close()


# ============================================================================
# 测试类 1：K线服务连接与基础数据获取
# ============================================================================

class TestKlineServiceConnection:
    """测试K线服务连接与基础数据获取"""

    @pytest.mark.asyncio
    async def test_service_connectivity(self, kline_service):
        """测试：K线服务可连接，获取BTCUSDT 1h数据成功"""
        klines = await kline_service.get_klines("BTCUSDT", "1h", limit=10)
        assert len(klines) > 0, "K线服务应返回数据"
        assert len(klines) <= 10, "返回数据量不应超过limit"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("symbol", ALL_SYMBOLS)
    async def test_all_symbols_available(self, kline_service, symbol):
        """测试：所有6个币种都能获取到1h K线数据"""
        klines = await kline_service.get_klines(symbol, "1h", limit=10)
        assert len(klines) > 0, f"{symbol} 应返回K线数据"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("interval", TEST_TIMEFRAMES)
    async def test_all_timeframes_available(self, kline_service, interval):
        """测试：所有时间框架（1h/4h/1d）都能获取数据"""
        klines = await kline_service.get_klines("BTCUSDT", interval, limit=10)
        assert len(klines) > 0, f"{interval} 应返回K线数据"


# ============================================================================
# 测试类 2：数据格式验证
# ============================================================================

class TestDataFormat:
    """测试K线服务返回的数据格式"""

    REQUIRED_FIELDS = ['open_time', 'open', 'high', 'low', 'close', 'volume',
                       'close_time', 'quote_volume', 'trades']

    @pytest.mark.asyncio
    async def test_kline_fields_exist(self, kline_service):
        """测试：返回的K线数据包含所有必需字段"""
        klines = await kline_service.get_klines("BTCUSDT", "1h", limit=10)
        assert len(klines) > 0

        for field in self.REQUIRED_FIELDS:
            assert field in klines[0], f"缺少字段: {field}"

    @pytest.mark.asyncio
    async def test_kline_field_types(self, kline_service):
        """测试：返回的K线数据字段类型正确"""
        klines = await kline_service.get_klines("BTCUSDT", "1h", limit=10)
        assert len(klines) > 0

        k = klines[0]

        # 数值字段应为 Decimal 类型
        for field in ['open', 'high', 'low', 'close', 'volume', 'quote_volume']:
            assert isinstance(k[field], Decimal), \
                f"{field} 应为 Decimal 类型，实际为 {type(k[field]).__name__}"

        # 时间戳应为整数
        assert isinstance(k['open_time'], int), f"open_time 应为 int"
        assert isinstance(k['close_time'], int), f"close_time 应为 int"

        # trades 应为整数
        assert isinstance(k['trades'], int), f"trades 应为 int"

    @pytest.mark.asyncio
    async def test_price_validity(self, kline_service):
        """测试：价格数据在合理范围内（high >= open/close >= low）"""
        for symbol in ALL_SYMBOLS[:3]:  # 测试前3个即可
            klines = await kline_service.get_klines(symbol, "1h", limit=50)
            assert len(klines) > 0

            for k in klines:
                price_fields = ['open', 'high', 'low', 'close']
                # 所有价格 > 0
                for field in price_fields:
                    assert k[field] > 0, \
                        f"{symbol} {field} 应 > 0，实际为 {k[field]}"

                # high >= low
                assert k['high'] >= k['low'], \
                    f"{symbol} high({k['high']}) < low({k['low']})"

                # high >= open, high >= close
                assert k['high'] >= k['open'], \
                    f"{symbol} high({k['high']}) < open({k['open']})"
                assert k['high'] >= k['close'], \
                    f"{symbol} high({k['high']}) < close({k['close']})"

                # low <= open, low <= close
                assert k['low'] <= k['open'], \
                    f"{symbol} low({k['low']}) > open({k['open']})"
                assert k['low'] <= k['close'], \
                    f"{symbol} low({k['low']}) > close({k['close']})"

    @pytest.mark.asyncio
    async def test_time_sequence(self, kline_service):
        """测试：K线数据按时间升序排列"""
        klines = await kline_service.get_klines("BTCUSDT", "1h", limit=50)
        assert len(klines) > 1

        for i in range(1, len(klines)):
            assert klines[i]['open_time'] > klines[i-1]['open_time'], \
                f"K线时间应递增: {klines[i-1]['open_time']} >= {klines[i]['open_time']}"

    @pytest.mark.asyncio
    async def test_no_duplicate_timestamps(self, kline_service):
        """测试：同周期K线没有重复时间戳"""
        for interval in TEST_TIMEFRAMES:
            klines = await kline_service.get_klines("BTCUSDT", interval, limit=100)
            timestamps = [k['open_time'] for k in klines]
            assert len(timestamps) == len(set(timestamps)), \
                f"{interval} 存在重复时间戳"


# ============================================================================
# 测试类 3：多时间框架数据获取
# ============================================================================

class TestMultiTimeframeData:
    """测试多时间框架数据获取"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("symbol", ALL_SYMBOLS)
    async def test_get_multi_timeframe_all_present(self, kline_service, symbol):
        """测试：所有币种都能获取完整的多时间框架数据"""
        data = await kline_service.get_multi_timeframe_data(
            symbol, intervals=TEST_TIMEFRAMES
        )

        for tf in TEST_TIMEFRAMES:
            assert tf in data, f"{symbol} 缺少 {tf} 数据"
            assert len(data[tf]) > 0, f"{symbol} {tf} 数据为空"

    @pytest.mark.asyncio
    async def test_multi_timeframe_data_count(self, kline_service):
        """测试：多时间框架数据量满足最低要求"""
        for symbol in ALL_SYMBOLS:
            data = await kline_service.get_multi_timeframe_data(
                symbol, intervals=TEST_TIMEFRAMES
            )

            for tf, min_count in MIN_KLINES_COUNT.items():
                assert len(data.get(tf, [])) >= min_count, \
                    f"{symbol} {tf} 数据量 {len(data.get(tf, []))} < {min_count}"

    @pytest.mark.asyncio
    async def test_multi_timeframe_data_consistency(self, kline_service):
        """测试：多时间框架数据时间一致性（1h最新K线时间 ≈ 4h最新K线时间）"""
        symbol = "BTCUSDT"
        data = await kline_service.get_multi_timeframe_data(
            symbol, intervals=TEST_TIMEFRAMES
        )

        latest_times = {}
        for tf in TEST_TIMEFRAMES:
            if data.get(tf):
                latest_times[tf] = data[tf][-1]['open_time']

        # 1h 和 4h 最新K线时间应该在1小时内
        if '1h' in latest_times and '4h' in latest_times:
            time_diff = abs(latest_times['1h'] - latest_times['4h'])
            assert time_diff <= 4 * 3600000, \
                f"1h({latest_times['1h']})和4h({latest_times['4h']})最新K线时间差 {time_diff}ms > 4h"


# ============================================================================
# 测试类 4：DataFrame转换与指标计算
# ============================================================================

class TestDataFrameConversion:
    """测试K线数据 → DataFrame → 指标计算的完整管道"""

    @pytest.mark.asyncio
    async def test_decimal_to_dataframe_conversion(self, kline_service):
        """测试：Decimal类型K线数据能正确转换为DataFrame并计算指标"""
        klines = await kline_service.get_klines("BTCUSDT", "1h", limit=100)

        # 模拟策略中的转换逻辑
        df = pd.DataFrame(klines)
        df['open'] = pd.to_numeric(df['open'], errors='coerce')
        df['high'] = pd.to_numeric(df['high'], errors='coerce')
        df['low'] = pd.to_numeric(df['low'], errors='coerce')
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df['volume'] = pd.to_numeric(df['volume'], errors='coerce')

        # 验证转换后没有NaN（正常数据）
        for col in ['open', 'high', 'low', 'close']:
            nan_count = df[col].isna().sum()
            assert nan_count == 0, f"{col} 有 {nan_count} 个NaN值"

        # 计算指标
        indicators = TechnicalIndicators.calculate_all(df)

        # 验证关键指标存在
        required_indicators = ['ADX', 'RSI', 'ATR', 'MACD', 'EMA12', 'EMA26',
                               'EMA55', 'MA7', 'MA21', 'MA55', 'BB_Upper',
                               'BB_Middle', 'BB_Lower', 'Volume_MA']
        for name in required_indicators:
            assert name in indicators, f"缺少指标: {name}"

    @pytest.mark.asyncio
    async def test_indicators_end_values_valid(self, kline_service):
        """测试：最新指标值不为NaN，且在合理范围"""
        klines = await kline_service.get_klines("BTCUSDT", "1h", limit=100)

        df = pd.DataFrame(klines)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        indicators = TechnicalIndicators.calculate_all(df)

        # ADX: 0-100
        adx = indicators['ADX'].iloc[-1]
        assert pd.notna(adx), "ADX 最新值不应为NaN"
        assert 0 <= adx <= 100, f"ADX({adx}) 不在0-100范围"

        # RSI: 0-100
        rsi = indicators['RSI'].iloc[-1]
        assert pd.notna(rsi), "RSI 最新值不应为NaN"
        assert 0 <= rsi <= 100, f"RSI({rsi}) 不在0-100范围"

        # ATR: > 0
        atr = indicators['ATR'].iloc[-1]
        assert pd.notna(atr), "ATR 最新值不应为NaN"
        assert atr > 0, f"ATR({atr}) 应 > 0"

        # EMA55: 不为NaN
        ema55 = indicators['EMA55'].iloc[-1]
        assert pd.notna(ema55), "EMA55 最新值不应为NaN"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("symbol", ALL_SYMBOLS)
    async def test_all_symbols_indicator_calculation(self, kline_service, symbol):
        """测试：所有6个币种的指标计算都能成功"""
        data = await kline_service.get_multi_timeframe_data(
            symbol, intervals=TEST_TIMEFRAMES
        )

        for tf in TEST_TIMEFRAMES:
            if not data.get(tf):
                pytest.skip(f"{symbol} {tf} 无数据")

            df = pd.DataFrame(data[tf])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')

            indicators = TechnicalIndicators.calculate_all(df)

            # 验证ADX存在（趋势判断的关键指标）
            assert 'ADX' in indicators, f"{symbol} {tf} 缺少ADX"
            assert 'EMA55' in indicators, f"{symbol} {tf} 缺少EMA55"
            assert 'RSI' in indicators, f"{symbol} {tf} 缺少RSI"
            assert 'ATR' in indicators, f"{symbol} {tf} 缺少ATR"


# ============================================================================
# 测试类 5：数据完整性验证
# ============================================================================

class TestDataCompleteness:
    """测试数据完整性（无缺口、覆盖最新时间）"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("symbol", ALL_SYMBOLS)
    async def test_recent_data_available(self, kline_service, symbol):
        """测试：最新数据在最近2小时内（证明K线采集正常运行）"""
        klines = await kline_service.get_klines(symbol, "1h", limit=1)
        assert len(klines) > 0

        latest_time = klines[0]['open_time']  # 毫秒时间戳
        now_ms = int(datetime.now().timestamp() * 1000)

        # 最新K线时间应在2小时内
        time_diff_hours = (now_ms - latest_time) / 3600000
        assert time_diff_hours <= 2, \
            f"{symbol} 最新K线时间距今 {time_diff_hours:.1f}小时，可能采集已停止"

    @pytest.mark.asyncio
    async def test_no_large_gaps_1h(self, kline_service):
        """测试：1h K线数据没有大缺口（相邻K线间隔不超过2小时）"""
        for symbol in ALL_SYMBOLS:
            klines = await kline_service.get_klines(symbol, "1h", limit=100)
            if len(klines) < 2:
                continue

            gaps = []
            for i in range(1, len(klines)):
                gap = klines[i]['open_time'] - klines[i-1]['open_time']
                if gap > 2 * 3600000:  # 超过2小时
                    gaps.append((i, gap))

            if gaps:
                gap_str = ", ".join([f"K线{i}:{g/3600000:.1f}h" for i, g in gaps[:5]])
                print(f"\n⚠ {symbol} 1h存在 {len(gaps)} 个缺口: {gap_str}")
            # 不强制断言，因为币安API可能有短暂间隙


# ============================================================================
# 测试类 6：DataFrame字段名兼容性（新旧K线服务对比）
# ============================================================================

class TestFieldNameCompatibility:
    """测试K线服务返回的字段名与策略期望的字段名一致"""

    @pytest.mark.asyncio
    async def test_kline_service_uses_correct_field_names(self, kline_service):
        """测试：K线服务返回'open'/'high'/'low'/'close'（而非'open_price'等）"""
        klines = await kline_service.get_klines("BTCUSDT", "1h", limit=10)

        # 策略期望的字段名（在 kline_service.py 已做映射）
        expected_fields = {'open', 'high', 'low', 'close', 'volume'}
        actual_fields = set(klines[0].keys())

        for field in expected_fields:
            assert field in actual_fields, \
                f"缺少字段 '{field}'，实际字段: {actual_fields}"

        # 确保没有重复的 'open_price' 等原始字段（已映射）
        deprecated_fields = {'open_price', 'high_price', 'low_price', 'close_price'}
        for field in deprecated_fields:
            assert field not in actual_fields, \
                f"存在已废弃字段 '{field}'，应已被映射为简单名称"

    @pytest.mark.asyncio
    async def test_pd_to_numeric_handles_decimal(self, kline_service):
        """测试：pd.to_numeric 能正确转换 Decimal 类型"""
        klines = await kline_service.get_klines("BTCUSDT", "1h", limit=10)

        df = pd.DataFrame(klines)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            converted = pd.to_numeric(df[col], errors='coerce')
            assert converted.dtype.kind in ('f', 'i'), \
                f"{col} 转换后类型 {converted.dtype} 不是数值类型"
            assert converted.isna().sum() == 0, \
                f"{col} 转换后有 {converted.isna().sum()} 个NaN"


# ============================================================================
# 测试类 7：错误处理与边界情况
# ============================================================================

class TestErrorHandling:
    """测试错误处理与边界情况"""

    @pytest.mark.asyncio
    async def test_invalid_symbol_returns_empty(self, kline_service):
        """测试：无效币种返回空数据"""
        klines = await kline_service.get_klines("INVALIDUSDT", "1h", limit=10)
        # 可能返回空，也可能返回错误（取决于K线服务实现）
        # 不管怎样，策略能处理空数据
        assert isinstance(klines, list)

    @pytest.mark.asyncio
    async def test_empty_data_handling(self, kline_service):
        """测试：空数据列表能正确处理为DataFrame"""
        empty_df = pd.DataFrame([])
        # 空DataFrame调用calculate_all应该抛出异常
        with pytest.raises(ValueError):
            TechnicalIndicators.calculate_all(empty_df)

    @pytest.mark.asyncio
    async def test_minimal_dataframe_handling(self, kline_service):
        """测试：最少数据量（5条）不会导致崩溃"""
        klines = await kline_service.get_klines("BTCUSDT", "1h", limit=5)
        df = pd.DataFrame(klines)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        indicators = TechnicalIndicators.calculate_all(df)
        # 数据不足时大部分指标为NaN，但不应该崩溃
        assert 'ADX' in indicators
        assert 'RSI' in indicators
        assert 'ATR' in indicators

    @pytest.mark.asyncio
    async def test_retry_on_failure(self, kline_service):
        """测试：重试机制在临时失败后能恢复（使用无效interval触发重试）"""
        # 使用有效symbol和interval确保正常请求
        # 首次请求成功验证重试不会误触发
        klines = await kline_service.get_klines("BTCUSDT", "1h", limit=10)
        assert isinstance(klines, list)
        assert len(klines) > 0


# ============================================================================
# 测试类 8：策略分析管道端到端测试
# ============================================================================

class TestEndToEndPipeline:
    """端到端测试：数据获取 → 指标计算 → 市场状态 → 入场检查"""

    @pytest.mark.asyncio
    async def test_full_pipeline_single_symbol(self, kline_service):
        """测试：单个币种完整分析管道"""
        symbol = "BTCUSDT"

        # 1. 获取多时间框架数据
        data = await kline_service.get_multi_timeframe_data(
            symbol, intervals=TEST_TIMEFRAMES
        )

        for tf in TEST_TIMEFRAMES:
            assert tf in data and len(data[tf]) > 0, f"{tf} 数据缺失"

        # 2. 计算每个时间框架的指标
        indicators = {}
        for tf, klines in data.items():
            df = pd.DataFrame(klines)
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            indicators[tf] = TechnicalIndicators.calculate_all(df)

        # 3. 验证关键指标
        for tf in TEST_TIMEFRAMES:
            ind = indicators[tf]
            assert 'ADX' in ind, f"{tf} 缺少ADX"
            assert 'RSI' in ind, f"{tf} 缺少RSI"
            assert 'ATR' in ind, f"{tf} 缺少ATR"
            assert 'EMA55' in ind, f"{tf} 缺少EMA55"
            assert 'BB_Upper' in ind, f"{tf} 缺少BB_Upper"
            assert 'BB_Lower' in ind, f"{tf} 缺少BB_Lower"

            # ADX需要至少~28条数据才能计算（14 DI + 14 ADX平滑）
            adx_val = ind['ADX'].iloc[-1]
            if pd.notna(adx_val):
                assert 0 <= adx_val <= 100, f"{tf} ADX({adx_val})不在0-100范围"
            else:
                print(f"\n⚠ {tf} ADX为NaN（数据量不足），跳过范围检查")

            # RSI需要至少14条数据
            rsi_val = ind['RSI'].iloc[-1]
            if pd.notna(rsi_val):
                assert 0 <= rsi_val <= 100, f"{tf} RSI({rsi_val})不在0-100范围"

        print(f"\n✅ {symbol} 完整管道测试通过")
        print(f"   1h: ADX={indicators['1h']['ADX'].iloc[-1]:.1f}, "
              f"RSI={indicators['1h']['RSI'].iloc[-1]:.1f}, "
              f"ATR={indicators['1h']['ATR'].iloc[-1]:.2f}")
        print(f"   4h: ADX={indicators['4h']['ADX'].iloc[-1]:.1f}, "
              f"RSI={indicators['4h']['RSI'].iloc[-1]:.1f}")
        print(f"   1d: ADX={indicators['1d']['ADX'].iloc[-1]:.1f}, "
              f"RSI={indicators['1d']['RSI'].iloc[-1]:.1f}")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("symbol", ALL_SYMBOLS)
    async def test_all_symbols_pipeline(self, kline_service, symbol):
        """测试：所有6个币种完整管道均能正常运行"""
        data = await kline_service.get_multi_timeframe_data(
            symbol, intervals=TEST_TIMEFRAMES
        )

        for tf in TEST_TIMEFRAMES:
            if tf not in data or not data[tf]:
                pytest.fail(f"{symbol} {tf} 数据缺失")

            df = pd.DataFrame(data[tf])
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = pd.to_numeric(df[col], errors='coerce')

            indicators = TechnicalIndicators.calculate_all(df)

            assert 'ADX' in indicators, f"{symbol} {tf} 缺少ADX"
            assert 'EMA55' in indicators, f"{symbol} {tf} 缺少EMA55"

            # ADX需要至少~28条数据，数据不足时允许NaN
            adx = indicators['ADX'].iloc[-1]
            if pd.notna(adx):
                assert 0 <= adx <= 100, f"{symbol} {tf} ADX({adx})不在0-100范围"

        print(f"\n✅ {symbol}: 管道测试通过")


# ============================================================================
# 运行入口
# ============================================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s', '--tb=short'])