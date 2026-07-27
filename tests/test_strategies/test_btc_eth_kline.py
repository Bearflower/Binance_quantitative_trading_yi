"""
测试 BTC/ETH 策略中与K线服务相关的核心逻辑

测试覆盖：
1. 策略初始化时K线服务验证
2. 获取多时间框架K线数据
3. K线数据不足处理
4. 错误恢复场景
5. 指标计算（基于K线数据）
"""
import sys
import os
import pytest
import pandas as pd
import numpy as np
from decimal import Decimal
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

# 确保项目根目录在 path 中
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

import yaml
from strategies.btc_eth.strategy import BTCEthStrategy, PositionState
from shared.kline_service import KLineService
from shared.indicators import TechnicalIndicators


# ============================================================================
# 辅助函数：生成测试用K线数据
# ============================================================================

def generate_klines(count: int, base_price: float = 50000.0,
                    interval_minutes: int = 60, volatility: float = 0.02) -> list:
    """
    生成模拟K线数据

    Args:
        count: K线数量
        base_price: 起始价格
        interval_minutes: K线间隔（分钟）
        volatility: 波动率

    Returns:
        K线数据列表，格式与 kline_service.get_klines 返回一致
    """
    import random
    random.seed(42)
    np.random.seed(42)

    klines = []
    start_time = int(datetime(2026, 1, 1).timestamp() * 1000)
    price = base_price

    for i in range(count):
        open_time = start_time + i * interval_minutes * 60 * 1000
        close_time = open_time + (interval_minutes - 1) * 60 * 1000

        # 生成随机价格
        change = price * volatility * np.random.randn()
        open_price = price
        close_price = price + change

        high_price = max(open_price, close_price) * (1 + abs(np.random.randn() * volatility * 0.3))
        low_price = min(open_price, close_price) * (1 - abs(np.random.randn() * volatility * 0.3))
        volume = base_price * 0.1 * (1 + 0.5 * np.random.randn())

        kline = {
            'open_time': open_time,
            'open': Decimal(str(round(open_price, 2))),
            'high': Decimal(str(round(high_price, 2))),
            'low': Decimal(str(round(low_price, 2))),
            'close': Decimal(str(round(close_price, 2))),
            'volume': Decimal(str(round(abs(volume), 4))),
            'close_time': close_time,
            'quote_volume': Decimal(str(round(abs(volume * close_price), 2))),
            'trades': int(abs(np.random.randn() * 10000) + 100),
        }
        klines.append(kline)
        price = close_price

    return klines


def generate_uptrend_klines(count: int, base_price: float = 50000.0,
                            interval_minutes: int = 60) -> list:
    """生成稳定上升趋势的K线数据（用于通过趋势过滤器的测试）"""
    np.random.seed(42)
    klines = []
    start_time = int(datetime(2026, 1, 1).timestamp() * 1000)
    price = base_price

    for i in range(count):
        open_time = start_time + i * interval_minutes * 60 * 1000
        close_time = open_time + (interval_minutes - 1) * 60 * 1000

        # 稳定上升趋势 + 小幅随机波动
        trend = price * 0.001  # 每根K线涨0.1%
        noise = price * 0.005 * np.random.randn()
        open_price = price
        close_price = price + trend + noise

        high_price = max(open_price, close_price) * 1.005
        low_price = min(open_price, close_price) * 0.995
        volume = base_price * 0.1 * (1 + 0.3 * np.random.randn())

        kline = {
            'open_time': open_time,
            'open': Decimal(str(round(open_price, 2))),
            'high': Decimal(str(round(high_price, 2))),
            'low': Decimal(str(round(low_price, 2))),
            'close': Decimal(str(round(close_price, 2))),
            'volume': Decimal(str(round(abs(volume), 4))),
            'close_time': close_time,
            'quote_volume': Decimal(str(round(abs(volume * close_price), 2))),
            'trades': int(abs(np.random.randn() * 10000) + 100),
        }
        klines.append(kline)
        price = float(close_price)

    return klines


def load_config() -> dict:
    """加载策略配置文件"""
    config_path = os.path.join(
        PROJECT_ROOT, "strategies", "btc_eth", "config.yaml"
    )
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def config():
    """加载真实配置"""
    return load_config()


@pytest.fixture
def mock_binance():
    """创建模拟币安客户端"""
    client = AsyncMock()
    client.get_ticker = AsyncMock(return_value={
        'lastPrice': '50000.00',
        'priceChangePercent': '1.5'
    })
    client.get_ticker_price = AsyncMock(return_value=Decimal('50000.00'))
    client.get_funding_rate = AsyncMock(return_value=0.0001)
    return client


@pytest.fixture
def mock_kline_service():
    """创建模拟K线服务"""
    service = AsyncMock(spec=KLineService)
    return service


@pytest.fixture
def mock_notification():
    """创建模拟通知服务"""
    client = AsyncMock()
    return client


@pytest.fixture
def strategy(config, mock_binance, mock_kline_service, mock_notification):
    """创建策略实例（未初始化）"""
    return BTCEthStrategy(
        config=config,
        binance_client=mock_binance,
        kline_service=mock_kline_service,
        notification_client=mock_notification,
        db_manager=None
    )


# ============================================================================
# 测试类 1：策略初始化时K线服务验证
# ============================================================================

class TestStrategyInitialization:
    """测试策略初始化时K线服务的正确设置"""

    def test_kline_service_set_on_init(self, config, mock_binance, mock_kline_service, mock_notification):
        """测试：正确创建BTCEthStrategy并设置kline_service"""
        s = BTCEthStrategy(
            config=config,
            binance_client=mock_binance,
            kline_service=mock_kline_service,
            notification_client=mock_notification,
            db_manager=None
        )
        assert s.kline_service is mock_kline_service
        assert s.binance is mock_binance
        assert s.notification is mock_notification
        assert s.db_manager is None
        assert s.symbols == config['strategy']['symbols']
        assert s.timeframes == config['strategy']['timeframes']

    @pytest.mark.asyncio
    async def test_kline_service_none_is_accepted(self, config, mock_binance, mock_notification):
        """测试：kline_service为None时策略可以创建，但analyze会捕获异常并返回错误结果"""
        s = BTCEthStrategy(
            config=config,
            binance_client=mock_binance,
            kline_service=None,  # type: ignore
            notification_client=mock_notification,
            db_manager=None
        )
        # 策略可以创建，但 kline_service 为 None
        assert s.kline_service is None
        # analyze 有顶层 try-except 捕获异常，返回结果而非抛出
        result = await s.analyze("BTCUSDT")
        assert result is not None
        assert "执行异常" in result.get('reason', '')


# ============================================================================
# 测试类 2：获取多时间框架K线数据
# ============================================================================

class TestGetMultiTimeframeData:
    """测试多时间框架K线数据获取"""

    @pytest.fixture
    def strategy_with_mocks(self, strategy, mock_binance):
        """配置策略的mock，使其能通过前置检查到达K线获取逻辑"""
        # Mock频率控制：允许交易
        strategy.frequency_controller.can_trade = MagicMock(return_value=(True, ""))
        # Mock经济日历：允许交易
        strategy._check_economic_calendar = MagicMock(return_value=(True, ""))
        # 清空持仓
        strategy.positions = {}
        # Mock binance价格获取
        strategy.binance = mock_binance
        return strategy

    @pytest.mark.asyncio
    async def test_get_multi_timeframe_data_normal(self, strategy_with_mocks, mock_kline_service):
        """测试：get_multi_timeframe_data() 返回正常数据"""
        strategy = strategy_with_mocks
        symbol = "BTCUSDT"

        # 生成正常K线数据
        klines_1h = generate_uptrend_klines(100, base_price=50000)
        klines_4h = generate_uptrend_klines(100, base_price=50000, interval_minutes=240)
        klines_1d = generate_uptrend_klines(100, base_price=50000, interval_minutes=1440)

        mock_kline_service.get_multi_timeframe_data = AsyncMock(return_value={
            '1h': klines_1h,
            '4h': klines_4h,
            '1d': klines_1d,
        })
        strategy.kline_service = mock_kline_service

        # 执行分析
        result = await strategy.analyze(symbol)

        # 验证调用了 get_multi_timeframe_data
        mock_kline_service.get_multi_timeframe_data.assert_called_once_with(
            symbol=symbol,
            intervals=strategy.timeframes
        )

        # 应该继续处理，不会返回"K线数据获取失败"
        assert result is not None
        assert result['reason'] != "K线数据获取失败"

    @pytest.mark.asyncio
    async def test_partial_period_failure(self, strategy_with_mocks, mock_kline_service):
        """测试：某个周期数据获取失败（返回空），策略应能继续处理其他周期"""
        strategy = strategy_with_mocks
        symbol = "BTCUSDT"

        klines_1h = generate_uptrend_klines(100, base_price=50000)
        # 4h 数据为空
        klines_4h = []
        klines_1d = generate_uptrend_klines(100, base_price=50000, interval_minutes=1440)

        mock_kline_service.get_multi_timeframe_data = AsyncMock(return_value={
            '1h': klines_1h,
            '4h': klines_4h,
            '1d': klines_1d,
        })
        strategy.kline_service = mock_kline_service

        result = await strategy.analyze(symbol)

        # 数据完整性检查失败，应返回特定周期数据不完整
        assert result is not None
        assert "4h" in result.get('reason', '') or "K线数据不完整" in result.get('reason', '')

    @pytest.mark.asyncio
    async def test_all_periods_failure(self, strategy_with_mocks, mock_kline_service):
        """测试：所有周期数据获取失败，策略应能正确处理并跳过"""
        strategy = strategy_with_mocks
        symbol = "BTCUSDT"

        # 所有周期数据为空
        mock_kline_service.get_multi_timeframe_data = AsyncMock(return_value={})
        strategy.kline_service = mock_kline_service

        result = await strategy.analyze(symbol)

        # 应该返回空数据错误
        assert result is not None
        assert result['reason'] == "K线数据获取失败" or "K线数据" in result['reason']

    @pytest.mark.asyncio
    async def test_none_klines_result(self, strategy_with_mocks, mock_kline_service):
        """测试：get_multi_timeframe_data 返回 None"""
        strategy = strategy_with_mocks
        symbol = "BTCUSDT"

        mock_kline_service.get_multi_timeframe_data = AsyncMock(return_value=None)
        strategy.kline_service = mock_kline_service

        result = await strategy.analyze(symbol)

        assert result is not None
        assert result['reason'] == "K线数据获取失败" or "K线数据" in result['reason']


# ============================================================================
# 测试类 3：K线数据不足处理
# ============================================================================

class TestKlineDataInsufficient:
    """测试K线数据不足时的处理"""

    @pytest.fixture
    def strategy_with_mocks(self, strategy, mock_binance):
        strategy.frequency_controller.can_trade = MagicMock(return_value=(True, ""))
        strategy._check_economic_calendar = MagicMock(return_value=(True, ""))
        strategy.positions = {}
        strategy.binance = mock_binance
        return strategy

    @pytest.mark.asyncio
    async def test_insufficient_data_skips_analysis(self, strategy_with_mocks, mock_kline_service):
        """测试：K线数据不足（少于最低要求）时，策略应跳过分析"""
        strategy = strategy_with_mocks
        symbol = "BTCUSDT"

        # 仅提供5根K线（远少于指标计算所需的最低数量）
        few_klines = generate_uptrend_klines(5, base_price=50000)
        mock_kline_service.get_multi_timeframe_data = AsyncMock(return_value={
            '1h': few_klines,
            '4h': few_klines,
            '1d': few_klines,
        })
        strategy.kline_service = mock_kline_service

        result = await strategy.analyze(symbol)

        # 数据太少，指标计算会得到NaN，各种过滤器应该会失败
        assert result is not None
        # 应该返回某个失败原因，而不是正常信号
        assert 'direction' not in result or result.get('grade') != 'S'

    @pytest.mark.asyncio
    async def test_boundary_minimum_klines(self, strategy_with_mocks, mock_kline_service):
        """测试：边界情况，刚好达到最低要求（60根K线，满足MA55计算）"""
        strategy = strategy_with_mocks
        symbol = "BTCUSDT"

        # 提供60根K线（刚好满足MA55计算）
        boundary_klines = generate_uptrend_klines(60, base_price=50000)
        mock_kline_service.get_multi_timeframe_data = AsyncMock(return_value={
            '1h': boundary_klines,
            '4h': boundary_klines,
            '1d': boundary_klines,
        })
        strategy.kline_service = mock_kline_service

        result = await strategy.analyze(symbol)

        # 60根K线可以计算指标（MA55, EMA55等都可计算）
        assert result is not None
        # 虽然可能因为其他条件不通过而返回失败，但不应该因为指标计算崩溃
        assert '执行异常' not in result.get('reason', '')

    @pytest.mark.asyncio
    async def test_insufficient_1d_data(self, strategy_with_mocks, mock_kline_service):
        """测试：日线数据不足，但小时线数据充足"""
        strategy = strategy_with_mocks
        symbol = "BTCUSDT"

        klines_1h = generate_uptrend_klines(100, base_price=50000)
        klines_4h = generate_uptrend_klines(100, base_price=50000, interval_minutes=240)
        # 日线只有3根
        klines_1d = generate_uptrend_klines(3, base_price=50000, interval_minutes=1440)

        mock_kline_service.get_multi_timeframe_data = AsyncMock(return_value={
            '1h': klines_1h,
            '4h': klines_4h,
            '1d': klines_1d,
        })
        strategy.kline_service = mock_kline_service

        result = await strategy.analyze(symbol)

        # 应该能正常处理，不会崩溃
        assert result is not None
        assert '执行异常' not in result.get('reason', '')


# ============================================================================
# 测试类 4：错误恢复场景
# ============================================================================

class TestErrorRecovery:
    """测试K线服务错误恢复场景"""

    @pytest.fixture
    def strategy_with_mocks(self, strategy, mock_binance):
        strategy.frequency_controller.can_trade = MagicMock(return_value=(True, ""))
        strategy._check_economic_calendar = MagicMock(return_value=(True, ""))
        strategy.positions = {}
        strategy.binance = mock_binance
        return strategy

    @pytest.mark.asyncio
    async def test_kline_service_temporary_unavailable(self, strategy_with_mocks, mock_kline_service):
        """测试：K线服务临时不可用（返回空数据），策略应跳过当前分析周期"""
        strategy = strategy_with_mocks
        symbol = "BTCUSDT"

        # 模拟服务不可用，返回空数据
        mock_kline_service.get_multi_timeframe_data = AsyncMock(return_value={})
        strategy.kline_service = mock_kline_service

        result = await strategy.analyze(symbol)

        assert result is not None
        assert result['reason'] == "K线数据获取失败" or "K线数据" in result['reason']
        assert 'direction' not in result  # 不应生成交易信号

    @pytest.mark.asyncio
    async def test_recovery_after_retry(self, strategy_with_mocks, mock_kline_service):
        """测试：K线服务重试后恢复，数据正常获取"""
        strategy = strategy_with_mocks
        symbol = "BTCUSDT"

        # 生成正常数据
        klines_1h = generate_uptrend_klines(100, base_price=50000)
        klines_4h = generate_uptrend_klines(100, base_price=50000, interval_minutes=240)
        klines_1d = generate_uptrend_klines(100, base_price=50000, interval_minutes=1440)

        # 第一次调用失败，第二次调用成功（模拟重试恢复）
        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {}
            return {
                '1h': klines_1h,
                '4h': klines_4h,
                '1d': klines_1d,
            }

        mock_kline_service.get_multi_timeframe_data = AsyncMock(side_effect=side_effect)
        strategy.kline_service = mock_kline_service

        # 第一次调用：失败
        result1 = await strategy.analyze(symbol)
        assert result1 is not None
        assert "K线数据" in result1.get('reason', '')

        # 第二次调用：恢复
        result2 = await strategy.analyze(symbol)
        assert result2 is not None
        assert result2.get('reason', '') != "K线数据获取失败"

    @pytest.mark.asyncio
    async def test_retry_still_fails(self, strategy_with_mocks, mock_kline_service):
        """测试：K线服务重试后仍失败，策略应跳过当前分析周期"""
        strategy = strategy_with_mocks
        symbol = "BTCUSDT"

        # 持续返回空数据
        mock_kline_service.get_multi_timeframe_data = AsyncMock(return_value={})
        strategy.kline_service = mock_kline_service

        # 多次调用都应跳过
        for _ in range(3):
            result = await strategy.analyze(symbol)
            assert result is not None
            assert result['reason'] == "K线数据获取失败" or "K线数据" in result['reason']

    @pytest.mark.asyncio
    async def test_exception_in_get_multi_timeframe(self, strategy_with_mocks, mock_kline_service):
        """测试：get_multi_timeframe_data 抛出异常，策略应捕获并返回错误"""
        strategy = strategy_with_mocks
        symbol = "BTCUSDT"

        mock_kline_service.get_multi_timeframe_data = AsyncMock(
            side_effect=Exception("K线服务连接超时")
        )
        strategy.kline_service = mock_kline_service

        result = await strategy.analyze(symbol)

        # analyze 方法有外层 try-except，应捕获异常并返回分析结果
        assert result is not None
        assert 'reason' in result
        # 异常被捕获，返回"执行异常"消息
        assert "执行异常" in result['reason'] or "K线数据" in result['reason']


# ============================================================================
# 测试类 5：指标计算（基于K线数据）
# ============================================================================

class TestIndicatorCalculation:
    """测试基于K线数据的技术指标计算"""

    def test_indicators_with_normal_data(self):
        """测试：正常K线数据可以正确计算技术指标（ADX, RSI, EMA等）"""
        klines = generate_klines(100, base_price=50000)
        df = pd.DataFrame(klines)

        # 转换为数值类型（与策略中的处理一致）
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # 计算所有指标
        indicators = TechnicalIndicators.calculate_all(df)

        # 验证关键指标存在且有效
        assert 'ADX' in indicators
        assert 'RSI' in indicators
        assert 'ATR' in indicators
        assert 'MACD' in indicators
        assert 'MACD_Signal' in indicators
        assert 'MACD_Hist' in indicators
        assert 'MA7' in indicators
        assert 'MA21' in indicators
        assert 'MA55' in indicators
        assert 'EMA12' in indicators
        assert 'EMA26' in indicators
        assert 'EMA55' in indicators
        assert 'BB_Upper' in indicators
        assert 'BB_Middle' in indicators
        assert 'BB_Lower' in indicators
        assert 'Volume_MA' in indicators

        # 验证最后一个值不为NaN
        for name, series in indicators.items():
            last_val = series.iloc[-1]
            assert pd.notna(last_val), f"{name} 的最后一个值为 NaN"

        # 验证ADX在合理范围
        adx = indicators['ADX'].iloc[-1]
        assert 0 <= adx <= 100, f"ADX值 {adx} 不在0-100范围内"

        # 验证RSI在合理范围
        rsi = indicators['RSI'].iloc[-1]
        assert 0 <= rsi <= 100, f"RSI值 {rsi} 不在0-100范围内"

        # 验证ATR为正
        atr = indicators['ATR'].iloc[-1]
        assert atr > 0, f"ATR值 {atr} 应为正数"

    def test_indicators_with_price_zero(self):
        """测试：价格为零时指标计算不应崩溃"""
        klines = generate_klines(100, base_price=50000)
        # 将所有close价格设为0
        for k in klines:
            k['close'] = Decimal('0')
            k['high'] = Decimal('0')
            k['low'] = Decimal('0')
            k['open'] = Decimal('0')

        df = pd.DataFrame(klines)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # 不应崩溃
        indicators = TechnicalIndicators.calculate_all(df)

        # 指标应存在（可能为NaN或0）
        assert 'RSI' in indicators
        assert 'ADX' in indicators
        assert 'ATR' in indicators

    def test_indicators_with_nan_values(self):
        """测试：数据包含空值时指标计算不应崩溃"""
        klines = generate_klines(100, base_price=50000)
        df = pd.DataFrame(klines)

        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # 在中间插入NaN
        df.loc[30:35, 'close'] = np.nan
        df.loc[30:35, 'high'] = np.nan
        df.loc[30:35, 'low'] = np.nan

        # 不应崩溃
        indicators = TechnicalIndicators.calculate_all(df)

        # 指标应存在，最后的值可能为NaN但不应导致异常
        assert 'RSI' in indicators
        assert 'ADX' in indicators
        assert 'ATR' in indicators

    def test_indicators_with_constant_price(self):
        """测试：价格完全不变时指标计算"""
        klines = generate_klines(100, base_price=50000)
        for k in klines:
            k['open'] = Decimal('50000')
            k['high'] = Decimal('50000')
            k['low'] = Decimal('50000')
            k['close'] = Decimal('50000')
            k['volume'] = Decimal('100')

        df = pd.DataFrame(klines)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        indicators = TechnicalIndicators.calculate_all(df)

        # 价格不变时，ATR应为0
        atr = indicators['ATR'].iloc[-1]
        assert atr == 0 or pd.isna(atr), f"恒定价格下ATR应为0或NaN，实际为 {atr}"

        # RSI应为50（无价格变化）
        rsi = indicators['RSI'].iloc[-1]
        assert rsi == 50 or pd.isna(rsi), f"恒定价格下RSI应为50，实际为 {rsi}"

    def test_indicators_with_extreme_values(self):
        """测试：极端价格值时指标计算不应崩溃"""
        klines = generate_klines(100, base_price=1e-8)  # 极低价格
        df = pd.DataFrame(klines)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # 不应崩溃
        indicators = TechnicalIndicators.calculate_all(df)
        assert 'RSI' in indicators
        assert 'ADX' in indicators
        assert 'ATR' in indicators

    def test_indicators_with_insufficient_data(self):
        """测试：数据量不足时指标计算"""
        klines = generate_klines(5, base_price=50000)  # 仅5根K线
        df = pd.DataFrame(klines)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # 不应崩溃
        indicators = TechnicalIndicators.calculate_all(df)

        # 大部分指标应为NaN（数据不足）
        assert 'RSI' in indicators
        rsi = indicators['RSI'].iloc[-1]
        assert pd.isna(rsi), f"数据不足时RSI应为NaN，实际为 {rsi}"

        assert 'ADX' in indicators
        adx = indicators['ADX'].iloc[-1]
        assert pd.isna(adx), f"数据不足时ADX应为NaN，实际为 {adx}"

    def test_indicators_correctly_calculate_ema(self):
        """测试：EMA指标计算正确性"""
        # 创建简单递增数据
        data = {
            'open': list(range(100, 200)),
            'high': list(range(102, 202)),
            'low': list(range(98, 198)),
            'close': list(range(101, 201)),
            'volume': [1000] * 100,
        }
        df = pd.DataFrame(data)

        indicators = TechnicalIndicators.calculate_all(df)

        # EMA12应比SMA12更接近近期价格（EMA赋予近期更高权重）
        ema12 = indicators['EMA12'].iloc[-1]
        # 手动计算SMA12
        sma12 = df['close'].iloc[-12:].mean()
        # 在上升趋势中，EMA12 > SMA12（EMA对近期价格更敏感）
        assert ema12 > sma12, f"上升趋势中EMA12({ema12})应大于SMA12({sma12})"

        # EMA55应比SMA55更接近近期价格
        ema55 = indicators['EMA55'].iloc[-1]
        sma55 = df['close'].iloc[-55:].mean()
        # 在上升趋势中，EMA55 > SMA55
        assert ema55 > sma55, f"上升趋势中EMA55({ema55})应大于SMA55({sma55})"

    def test_indicators_dataframe_types(self):
        """测试：TechnicalIndicators.calculate_all 接受DataFrame并返回正确类型"""
        klines = generate_klines(100, base_price=50000)
        df = pd.DataFrame(klines)

        # 使用Decimal类型（与策略中的实际数据类型一致）
        indicators = TechnicalIndicators.calculate_all(df)

        # 返回字典
        assert isinstance(indicators, dict)

        # 所有值都是pd.Series
        for name, series in indicators.items():
            assert isinstance(series, pd.Series), f"{name} 不是pd.Series类型"

        # 所有Series长度与输入DataFrame相同
        for name, series in indicators.items():
            assert len(series) == len(df), f"{name} 长度 {len(series)} 不等于输入长度 {len(df)}"


# ============================================================================
# 测试类 6：K线数据格式兼容性
# ============================================================================

class TestKlineDataFormat:
    """测试K线数据格式兼容性"""

    @pytest.fixture
    def strategy_with_mocks(self, strategy, mock_binance):
        strategy.frequency_controller.can_trade = MagicMock(return_value=(True, ""))
        strategy._check_economic_calendar = MagicMock(return_value=(True, ""))
        strategy.positions = {}
        strategy.binance = mock_binance
        return strategy

    @pytest.mark.asyncio
    async def test_decimal_price_values(self, strategy_with_mocks, mock_kline_service):
        """测试：Decimal类型的价格值能被正确处理"""
        strategy = strategy_with_mocks
        symbol = "BTCUSDT"

        # 生成包含Decimal值的K线数据
        klines = []
        for i in range(100):
            klines.append({
                'open_time': int(datetime(2026, 1, 1).timestamp() * 1000) + i * 3600000,
                'open': Decimal(str(50000 + i * 10)),
                'high': Decimal(str(50100 + i * 10)),
                'low': Decimal(str(49900 + i * 10)),
                'close': Decimal(str(50050 + i * 10)),
                'volume': Decimal('1000.5'),
                'close_time': int(datetime(2026, 1, 1).timestamp() * 1000) + (i + 1) * 3600000 - 1,
                'quote_volume': Decimal('50000000'),
                'trades': 10000,
            })

        mock_kline_service.get_multi_timeframe_data = AsyncMock(return_value={
            '1h': klines,
            '4h': klines,
            '1d': klines,
        })
        strategy.kline_service = mock_kline_service

        # 不应崩溃
        result = await strategy.analyze(symbol)
        assert result is not None
        # 即使不通过，也不应因格式问题崩溃
        assert '执行异常' not in result.get('reason', '')

    @pytest.mark.asyncio
    async def test_float_price_values(self, strategy_with_mocks, mock_kline_service):
        """测试：float类型的价格值能被正确处理"""
        strategy = strategy_with_mocks
        symbol = "BTCUSDT"

        # 生成包含float值的K线数据（与Decimal混合）
        klines = []
        for i in range(100):
            klines.append({
                'open_time': int(datetime(2026, 1, 1).timestamp() * 1000) + i * 3600000,
                'open': float(50000 + i * 10),
                'high': float(50100 + i * 10),
                'low': float(49900 + i * 10),
                'close': float(50050 + i * 10),
                'volume': float(1000.5),
                'close_time': int(datetime(2026, 1, 1).timestamp() * 1000) + (i + 1) * 3600000 - 1,
                'quote_volume': float(50000000),
                'trades': 10000,
            })

        mock_kline_service.get_multi_timeframe_data = AsyncMock(return_value={
            '1h': klines,
            '4h': klines,
            '1d': klines,
        })
        strategy.kline_service = mock_kline_service

        # 不应崩溃
        result = await strategy.analyze(symbol)
        assert result is not None
        assert '执行异常' not in result.get('reason', '')


# ============================================================================
# 测试类 7：动态利润保护（移动止损）
# ============================================================================

class TestDynamicTrailingStop:
    """测试动态利润保护机制"""

    @pytest.fixture
    def position_long(self):
        """创建做多持仓"""
        pos = PositionState()
        pos.entry_price = Decimal('60000')
        pos.direction = 'LONG'
        pos.current_quantity = Decimal('0.1')
        pos.atr = Decimal('600')  # 1% ATR
        pos.highest_price = Decimal('60000')
        pos.tp1_hit = False
        pos.trailing_activated = False
        pos.trailing_stop_price = None
        return pos

    @pytest.fixture
    def position_short(self):
        """创建做空持仓"""
        pos = PositionState()
        pos.entry_price = Decimal('60000')
        pos.direction = 'SHORT'
        pos.current_quantity = Decimal('1.0')
        pos.atr = Decimal('600')  # 1% ATR
        pos.lowest_price = Decimal('60000')
        pos.tp1_hit = False
        pos.trailing_activated = False
        pos.trailing_stop_price = None
        return pos

    @pytest.fixture
    def strategy_with_config(self, strategy):
        """配置动态利润保护参数"""
        # 注入动态利润保护配置
        strategy.risk_config['dynamic_trailing'] = {
            'enabled': True,
            'activation': {'min_profit_pct': 1.5, 'also_on_tp1': True},
            'regression_tiers': [
                {'profit_ceiling': 1.5, 'retrace_ratio': 0.0},
                {'profit_ceiling': 4.0, 'retrace_ratio': 0.5},
                {'profit_ceiling': 8.0, 'retrace_ratio': 0.35},
                {'profit_ceiling': 999.0, 'retrace_ratio': 0.25}
            ],
            'volatility_adjustment': {'enabled': False}
        }
        strategy.risk_config['stop_loss_atr_multiplier'] = 1.5

        # 模拟 vol_adj 返回 1.0（不启用波动率调节）
        strategy._get_volatility_adjustment = AsyncMock(return_value=1.0)
        return strategy

    @pytest.mark.asyncio
    async def test_not_activated_low_profit(self, strategy_with_config, position_long):
        """TC-01: 浮盈0%，未激活"""
        strategy = strategy_with_config
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_long, Decimal('60000'))
        assert result is None
        assert position_long.trailing_activated is False

    @pytest.mark.asyncio
    async def test_not_activated_1pct(self, strategy_with_config, position_long):
        """TC-02: 浮盈1.2%，未激活"""
        strategy = strategy_with_config
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_long, Decimal('60720'))
        assert result is None
        assert position_long.trailing_activated is False

    @pytest.mark.asyncio
    async def test_activated_breakeven(self, strategy_with_config, position_long):
        """TC-03: 最高价浮盈1.5%，基于峰值计算止损"""
        strategy = strategy_with_config
        position_long.highest_price = Decimal('60900')  # 模拟价格曾到过60900
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_long, Decimal('60900'))
        assert result is not None
        assert position_long.trailing_activated is True
        # 参考价=highest_price=60900，浮盈=1.5%
        # 1.5% < 4.0%，进入1.5%~4%阶梯，回撤0.5
        # 浮盈金额=900，回撤=900*0.5=450，止损价=60900-450=60450
        assert result == Decimal('60450')

    @pytest.mark.asyncio
    async def test_tier_2pct_profit(self, strategy_with_config, position_long):
        """TC-04: 最高价浮盈2%，基于峰值计算止损"""
        strategy = strategy_with_config
        position_long.highest_price = Decimal('61200')  # 模拟价格曾到过61200
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_long, Decimal('61200'))
        assert result is not None
        assert position_long.trailing_activated is True
        # 参考价=61200，浮盈金额=1200，回撤=1200*0.5*1.0=600
        # 止损价=61200-600=60600
        assert result == Decimal('60600')

    @pytest.mark.asyncio
    async def test_tier_5pct_profit(self, strategy_with_config, position_long):
        """TC-05: 最高价浮盈5%，基于峰值计算止损"""
        strategy = strategy_with_config
        position_long.highest_price = Decimal('63000')  # 模拟价格曾到过63000
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_long, Decimal('63000'))
        assert result is not None
        # 参考价=63000，浮盈金额=3000，回撤=3000*0.35*1.0=1050
        # 止损价=63000-1050=61950
        assert result == Decimal('61950')

    @pytest.mark.asyncio
    async def test_tier_10pct_profit(self, strategy_with_config, position_long):
        """TC-06: 最高价浮盈10%，基于峰值计算止损"""
        strategy = strategy_with_config
        position_long.highest_price = Decimal('66000')  # 模拟价格曾到过66000
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_long, Decimal('66000'))
        assert result is not None
        # 参考价=66000，浮盈金额=6000，回撤=6000*0.25*1.0=1500
        # 止损价=66000-1500=64500
        assert result == Decimal('64500')

    @pytest.mark.asyncio
    async def test_activated_by_tp1(self, strategy_with_config, position_long):
        """TC-07: TP1触发但浮盈<1.5%，应激活"""
        strategy = strategy_with_config
        position_long.tp1_hit = True
        position_long.highest_price = Decimal('60300')  # 模拟价格曾到过60300
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_long, Decimal('60300'))
        assert result is not None
        assert position_long.trailing_activated is True

    @pytest.mark.asyncio
    async def test_short_direction(self, strategy_with_config, position_short):
        """TC-08: 做空方向，基于最低价计算浮盈"""
        strategy = strategy_with_config
        position_short.lowest_price = Decimal('58200')  # 模拟价格曾到过58200
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_short, Decimal('58200'))
        assert result is not None
        assert position_short.trailing_activated is True
        # 参考价=58200，浮盈金额=1800，回撤=1800*0.5*1.0=900
        # 止损价=58200+900=59100
        assert result == Decimal('59100')

    @pytest.mark.asyncio
    async def test_hard_stop_below_trailing(self, strategy_with_config, position_long):
        """TC-09: 硬止损价低于动态保护价（做多时硬止损作为兜底）"""
        strategy = strategy_with_config
        position_long.highest_price = Decimal('61200')  # 模拟价格曾到过61200
        # 硬止损价 = 60000 - 600 * 1.5 = 60000 - 900 = 59100
        # 动态保护价（2%浮盈，基于峰值）= 61200 - 1200*0.5 = 60600
        # 最终 = max(60600, 59100) = 60600（动态保护价更紧，硬止损兜底）
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_long, Decimal('61200'))
        assert result == Decimal('60600')
        # 硬止损价低于动态保护价，说明动态保护更紧
        hard_stop = Decimal('60000') - Decimal('600') * Decimal('1.5')
        assert result > hard_stop

    @pytest.mark.asyncio
    async def test_one_way_movement_long(self, strategy_with_config, position_long):
        """TC-10: 单向移动保护（做多止损价不降低）"""
        strategy = strategy_with_config
        position_long.highest_price = Decimal('61200')  # 模拟价格曾到过61200
        # 基于峰值61200，设置止损价60600
        await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_long, Decimal('61200'))
        assert position_long.trailing_stop_price == Decimal('60600')
        # 价格回落到60600（刚好触及止损），应触发
        triggered = Decimal('60600') <= position_long.trailing_stop_price
        assert triggered is True
        # 价格再回落到60300，但峰值仍为61200，止损价不应降低
        await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_long, Decimal('60300'))
        assert position_long.trailing_stop_price == Decimal('60600')

    @pytest.mark.asyncio
    async def test_short_stop_moves_down(self, strategy_with_config, position_short):
        """TC-10b: 单向移动保护（做空止损价不提高）"""
        strategy = strategy_with_config
        position_short.lowest_price = Decimal('58200')  # 模拟价格曾到过58200
        # 基于最低价58200，设置止损价59100
        await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_short, Decimal('58200'))
        # 价格反弹到58800，但最低价仍为58200，止损价不应提高
        await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_short, Decimal('58800'))
        assert position_short.trailing_stop_price == Decimal('59100')

    @pytest.mark.asyncio
    async def test_activated_remains_active(self, strategy_with_config, position_long):
        """TC-11: 激活后浮盈回落，保持激活状态"""
        strategy = strategy_with_config
        position_long.highest_price = Decimal('61200')  # 模拟价格曾到过61200
        # 先激活（浮盈2%）
        await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_long, Decimal('61200'))
        assert position_long.trailing_activated is True
        # 浮盈回落到0.5%，仍应保持激活
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_long, Decimal('60300'))
        assert result is not None  # 不应返回None

    @pytest.mark.asyncio
    async def test_check_dynamic_trailing_triggered(self, strategy_with_config, position_long):
        """TC-12: _check_dynamic_trailing 触发平仓"""
        strategy = strategy_with_config
        position_long.trailing_activated = True
        position_long.trailing_stop_price = Decimal('60600')
        # 价格跌破止损价
        with patch.object(strategy, '_close_position', AsyncMock()) as mock_close:
            await strategy._check_dynamic_trailing("BTCUSDT", position_long, Decimal('60500'))
            mock_close.assert_called_once()
            args, kwargs = mock_close.call_args
            assert kwargs['close_reason'] == 'TRAILING_STOP'

    @pytest.mark.asyncio
    async def test_check_dynamic_trailing_not_triggered(self, strategy_with_config, position_long):
        """TC-13: 价格未跌破止损价，不触发"""
        strategy = strategy_with_config
        position_long.trailing_activated = True
        position_long.trailing_stop_price = Decimal('60600')
        with patch.object(strategy, '_close_position', AsyncMock()) as mock_close:
            await strategy._check_dynamic_trailing("BTCUSDT", position_long, Decimal('60800'))
            mock_close.assert_not_called()

    @pytest.mark.asyncio
    async def test_volatility_adjustment_enabled(self, strategy_with_config, position_long):
        """TC-14: 波动率调节因子启用"""
        strategy = strategy_with_config
        position_long.highest_price = Decimal('61200')  # 模拟价格曾到过61200
        strategy.risk_config['dynamic_trailing']['volatility_adjustment']['enabled'] = True
        strategy._get_volatility_adjustment = AsyncMock(return_value=1.5)
        # 浮盈2%，vol_adj=1.5，回撤=1200*0.5*1.5=900
        # 止损价=61200-900=60300
        result = await strategy._calculate_dynamic_trailing_stop("BTCUSDT", position_long, Decimal('61200'))
        assert result == Decimal('60300')

    @pytest.mark.asyncio
    async def test_peak_retracement_protection(self, strategy_with_config, position_long):
        """TC-15: 核心场景 - 价格从峰值回落，止损基于峰值计算
        
        典型场景：BTC 开仓 60,000，涨到 66,000（峰值），回落到 64,000
        基于峰值计算：止损价 = 66,000 × (1 - 10%×25%) = 64,350
        当前价 64,000 < 64,350 → 触发保护
        若用当前价计算：止损价 = 64,000 - 4,000×35% = 62,600 → 不触发
        """
        strategy = strategy_with_config
        # 模拟：开仓 60,000，峰值 66,000，当前 64,000
        position_long.highest_price = Decimal('66000')  # 峰值
        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('64000')
        )
        assert result is not None
        assert position_long.trailing_activated is True
        # 基于峰值：浮盈=10% → >8%阶梯，回撤0.25
        # 浮盈金额=6000，回撤=6000*0.25=1500
        # 止损价=66000-1500=64500
        assert result == Decimal('64500')
        # 当前价64000低于止损价64500，应触发
        assert Decimal('64000') <= result


if __name__ == '__main__':
    pytest.main([__file__, '-v'])