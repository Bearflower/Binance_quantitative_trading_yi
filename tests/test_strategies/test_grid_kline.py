"""
网格交易策略K线服务核心逻辑单元测试

覆盖 strategies/grid/ 中与K线服务相关的核心逻辑：
- MarketStateDetector 初始化与数据获取
- 多时间框架K线数据获取与容错
- 基于K线数据的市场状态识别
- K线数据错误处理
- GridSignalBot 中的K线使用
- 基于K线数据的指标计算
"""
import random
from decimal import Decimal
from typing import Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.kline_service import KLineService
from strategies.grid.market_state import (
    MarketState,
    MarketAnalysis,
    MarketStateDetector,
)
from strategies.grid.signal_bot import GridSignalBot


# ========== 测试数据 ==========

MOCK_SYMBOL = "BTCUSDT"


def _generate_trend_klines(
    count: int = 100,
    start_price: float = 50000.0,
    trend: float = 0.0,
    volatility: float = 100.0,
) -> List[Dict]:
    """
    生成模拟K线数据

    Args:
        count: K线数量
        start_price: 起始价格
        trend: 趋势方向（正值=上涨，负值=下跌）
        volatility: 波动率

    Returns:
        K线数据列表（价格字段使用float类型，与pandas兼容）
    """
    random.seed(42)

    klines = []
    price = start_price
    for i in range(count):
        open_price = price
        change = trend + random.uniform(-volatility, volatility)
        close_price = open_price + change
        high = max(open_price, close_price) + random.uniform(
            0, abs(volatility) * 0.5
        )
        low = min(open_price, close_price) - random.uniform(
            0, abs(volatility) * 0.5
        )
        klines.append(
            {
                "open_time": 1700000000000 + i * 3600000,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close_price,
                "volume": random.uniform(50, 200),
                "close_time": 1700000000000 + (i + 1) * 3600000,
                "quote_volume": open_price * random.uniform(50, 200),
                "trade_count": random.randint(500, 2000),
            }
        )
        price = close_price
    return klines


# 震荡市场数据：无趋势，低波动 → 低ADX
MOCK_KLINES_OSCILLATION = _generate_trend_klines(
    count=100, start_price=50000, trend=0, volatility=10
)

# 弱趋势数据：轻微趋势
MOCK_KLINES_WEAK_TREND = _generate_trend_klines(
    count=100, start_price=50000, trend=50, volatility=200
)

# 强趋势数据：明显趋势
MOCK_KLINES_STRONG_TREND = _generate_trend_klines(
    count=100, start_price=50000, trend=100, volatility=150
)

# 数据不足（少于30根）
MOCK_KLINES_INSUFFICIENT = _generate_trend_klines(
    count=20, start_price=50000, trend=50, volatility=200
)


def _make_tf_data(
    klines_15m: List[Dict],
    klines_1h: List[Dict],
    klines_4h: List[Dict],
) -> Dict[str, List[Dict]]:
    """构建多时间框架数据字典"""
    return {"15m": klines_15m, "1h": klines_1h, "4h": klines_4h}


# ========== Fixtures ==========


@pytest.fixture
def mock_kline_service():
    """创建模拟K线服务"""
    return AsyncMock(spec=KLineService)


@pytest.fixture
def detector(mock_kline_service):
    """创建 MarketStateDetector 实例"""
    return MarketStateDetector(kline_service=mock_kline_service)


# ========== 1. MarketStateDetector 初始化测试 ==========


class TestMarketStateDetectorInit:
    """测试MarketStateDetector初始化"""

    def test_init_with_kline_service(self, mock_kline_service):
        """正确创建MarketStateDetector并设置kline_service"""
        det = MarketStateDetector(kline_service=mock_kline_service)
        assert det.kline_service is mock_kline_service
        assert det.adx_period == 10
        assert det.ema_fast_period == 20
        assert det.ema_slow_period == 50
        assert det.atr_period == 14
        assert det.adx_extreme_strong == 40

    def test_init_without_kline_service(self):
        """kline_service为None时抛出ValueError"""
        with pytest.raises(ValueError, match="K线服务不能为空"):
            MarketStateDetector(kline_service=None)


# ========== 2. 获取多时间框架K线数据测试 ==========


class TestGetMultiTimeframeData:
    """测试获取多时间框架K线数据"""

    @pytest.mark.asyncio
    async def test_normal_get_multi_timeframe_data(
        self, mock_kline_service, detector
    ):
        """正常获取15m, 1h, 4h数据"""
        klines = _generate_trend_klines(100, 50000, 50, 100)
        tf_data = _make_tf_data(klines, klines, klines)
        mock_kline_service.get_multi_timeframe_data.return_value = tf_data

        result = await detector._get_multi_timeframe_data(MOCK_SYMBOL)

        assert "15m" in result
        assert "1h" in result
        assert "4h" in result
        assert len(result["15m"]) == 100
        assert len(result["1h"]) == 100
        assert len(result["4h"]) == 100
        mock_kline_service.get_multi_timeframe_data.assert_called_once_with(
            symbol=MOCK_SYMBOL, intervals=["15m", "1h", "4h"]
        )

    @pytest.mark.asyncio
    async def test_partial_interval_empty(
        self, mock_kline_service, detector
    ):
        """某个周期数据为空，detector应抛出ValueError"""
        tf_data = _make_tf_data(
            _generate_trend_klines(100, 50000, 50, 100),
            [],  # 1h数据为空
            _generate_trend_klines(100, 50000, 50, 100),
        )
        mock_kline_service.get_multi_timeframe_data.return_value = tf_data

        with pytest.raises(ValueError, match="缺少.*1h.*时间框架数据"):
            await detector._get_multi_timeframe_data(MOCK_SYMBOL)

    @pytest.mark.asyncio
    async def test_all_intervals_empty(
        self, mock_kline_service, detector
    ):
        """所有周期都为空，detector应抛出ValueError"""
        mock_kline_service.get_multi_timeframe_data.return_value = {}

        with pytest.raises(ValueError, match="缺少.*15m.*时间框架数据"):
            await detector._get_multi_timeframe_data(MOCK_SYMBOL)


# ========== 3. 市场状态识别测试 ==========


class TestMarketStateDetection:
    """测试基于K线数据的市场状态识别"""

    @pytest.mark.asyncio
    async def test_detect_market_state_returns_analysis(
        self, mock_kline_service, detector
    ):
        """detect_market_state 正确返回 MarketAnalysis 实例"""
        klines = MOCK_KLINES_OSCILLATION
        tf_data = _make_tf_data(klines, klines, klines)
        mock_kline_service.get_multi_timeframe_data.return_value = tf_data

        analysis = await detector.detect_market_state(MOCK_SYMBOL)

        assert isinstance(analysis, MarketAnalysis)
        assert isinstance(analysis.state, MarketState)
        assert isinstance(analysis.trend_strength, Decimal)
        assert 0 <= analysis.trend_strength <= Decimal("0.5")
        assert isinstance(analysis.adx_1h, Decimal)
        assert isinstance(analysis.adx_4h, Decimal)
        assert isinstance(analysis.adx_15m, Decimal)
        assert isinstance(analysis.atr_smooth, Decimal)
        assert isinstance(analysis.confidence, Decimal)
        assert isinstance(analysis.current_price, Decimal)

    @pytest.mark.asyncio
    async def test_oscillation_detected(
        self, mock_kline_service, detector
    ):
        """低波动无趋势数据应识别为OSCILLATION或WEAK_TREND"""
        klines = _generate_trend_klines(
            count=100, start_price=50000, trend=0, volatility=5
        )
        tf_data = _make_tf_data(klines, klines, klines)
        mock_kline_service.get_multi_timeframe_data.return_value = tf_data

        analysis = await detector.detect_market_state(MOCK_SYMBOL)

        # 低ADX应判定为震荡或弱趋势
        assert analysis.state in [
            MarketState.OSCILLATION,
            MarketState.WEAK_TREND,
        ]

    @pytest.mark.asyncio
    async def test_insufficient_data_raises_value_error(
        self, mock_kline_service, detector
    ):
        """K线数据不足时（少于30根），detect_market_state 抛出ValueError"""
        klines = MOCK_KLINES_INSUFFICIENT
        tf_data = _make_tf_data(klines, klines, klines)
        mock_kline_service.get_multi_timeframe_data.return_value = tf_data

        with pytest.raises(ValueError, match="K线数据不足"):
            await detector.detect_market_state(MOCK_SYMBOL)

    @pytest.mark.asyncio
    async def test_empty_symbol_raises_value_error(
        self, mock_kline_service, detector
    ):
        """空交易对抛出ValueError"""
        with pytest.raises(ValueError, match="交易对不能为空"):
            await detector.detect_market_state("")

        with pytest.raises(ValueError, match="交易对不能为空"):
            await detector.detect_market_state("   ")

    @pytest.mark.asyncio
    async def test_prices_are_consistent(
        self, mock_kline_service, detector
    ):
        """检测结果中的价格与K线数据一致"""
        klines = MOCK_KLINES_WEAK_TREND
        tf_data = _make_tf_data(klines, klines, klines)
        mock_kline_service.get_multi_timeframe_data.return_value = tf_data

        analysis = await detector.detect_market_state(MOCK_SYMBOL)

        # current_price 应等于K线最后一根收盘价
        expected_price = Decimal(str(klines[-1]["close"]))
        assert analysis.current_price == expected_price


# ========== 4. K线数据错误处理测试 ==========


class TestKlineErrorHandling:
    """测试K线数据错误处理"""

    @pytest.mark.asyncio
    async def test_service_exception_in_get_multi_timeframe(
        self, mock_kline_service, detector
    ):
        """K线服务返回异常时，_get_multi_timeframe_data 抛出异常"""
        mock_kline_service.get_multi_timeframe_data.side_effect = Exception(
            "服务连接失败"
        )

        with pytest.raises(Exception, match="服务连接失败"):
            await detector._get_multi_timeframe_data(MOCK_SYMBOL)

    @pytest.mark.asyncio
    async def test_service_exception_in_detect_market_state(
        self, mock_kline_service, detector
    ):
        """detect_market_state 中K线服务异常，异常应传播"""
        mock_kline_service.get_multi_timeframe_data.side_effect = Exception(
            "K线服务异常"
        )

        with pytest.raises(Exception, match="K线服务异常"):
            await detector.detect_market_state(MOCK_SYMBOL)

    @pytest.mark.asyncio
    async def test_partial_kline_data_missing_15m(
        self, mock_kline_service, detector
    ):
        """15m数据缺失，抛出ValueError"""
        tf_data = {
            "1h": _generate_trend_klines(100),
            "4h": _generate_trend_klines(100),
        }
        mock_kline_service.get_multi_timeframe_data.return_value = tf_data

        with pytest.raises(ValueError, match="缺少.*15m"):
            await detector._get_multi_timeframe_data(MOCK_SYMBOL)

    @pytest.mark.asyncio
    async def test_partial_kline_data_missing_4h(
        self, mock_kline_service, detector
    ):
        """4h数据缺失，抛出ValueError"""
        tf_data = _make_tf_data(
            _generate_trend_klines(100),
            _generate_trend_klines(100),
            [],
        )
        # 4h 为空列表，应被识别为缺失
        mock_kline_service.get_multi_timeframe_data.return_value = tf_data

        with pytest.raises(ValueError, match="缺少.*4h"):
            await detector._get_multi_timeframe_data(MOCK_SYMBOL)

    @pytest.mark.asyncio
    async def test_boundary_breakthrough_empty_klines(
        self, mock_kline_service, detector
    ):
        """check_boundary_breakthrough 中K线为空时返回 (False, None)"""
        mock_kline_service.get_klines.return_value = []

        result = await detector.check_boundary_breakthrough(
            MOCK_SYMBOL,
            upper_boundary=Decimal("60000"),
            lower_boundary=Decimal("40000"),
        )

        assert result == (False, None)

    @pytest.mark.asyncio
    async def test_boundary_breakthrough_service_exception(
        self, mock_kline_service, detector
    ):
        """check_boundary_breakthrough 中K线服务异常时返回 (False, None)"""
        mock_kline_service.get_klines.side_effect = Exception("K线服务异常")

        result = await detector.check_boundary_breakthrough(
            MOCK_SYMBOL,
            upper_boundary=Decimal("60000"),
            lower_boundary=Decimal("40000"),
        )

        assert result == (False, None)


# ========== 5. GridSignalBot 中的K线使用测试 ==========


class TestGridSignalBotKline:
    """测试GridSignalBot中的K线使用"""

    @pytest.fixture
    def mock_config(self):
        """创建模拟配置"""
        return {
            "symbols": ["BTCUSDT"],
            "trading": {"leverage": 10, "margin": 500},
            "market": {
                "adx_extreme_strong": 40,
                "adx_extreme_strong_4h": 30,
                "adx_normal_strong": 30,
                "adx_normal_strong_4h": 25,
                "weak_trend_adx_lower": 25,
                "weak_trend_adx_upper": 30,
                "volatility_ratio_threshold": 1.2,
                "volatility_consecutive_count": 2,
                "volatility_recovery_ratio": 1.2,
                "recovery_adx_strong_1h": 30,
                "recovery_adx_strong_4h": 30,
                "recovery_adx_weak_1h": 25,
                "recovery_adx_weak_4h": 25,
                "trend_strength_divisor": 30,
                "atr_history_size": 5,
                "ema_fast": 20,
                "ema_slow": 50,
                "atr_period": 14,
                "emergency_adx_threshold": 55,
                "trend_acceleration_threshold": 8,
                "adx_history_size": 3,
                "adx_period": 10,
                "price_emergency_1h": 0.03,
                "price_emergency_15m": 0.015,
                "adx_early_warning_15m": 50,
                "price_early_warning_1h": 0.01,
                "atr_baseline_period": 30,
                "confidence": {
                    "emergency_extreme_trend": 0.99,
                    "trend_accelerating": 0.9,
                    "extreme_strong_trend": 0.95,
                    "volatility_abnormal": 0.85,
                    "normal_strong_trend": 0.8,
                    "weak_trend": 0.7,
                    "oscillation": 0.5,
                    "price_emergency": 1.0,
                    "early_warning_15m": 0.92,
                    "trend_confirmed_1h": 0.95,
                },
            },
            "grid": {"min_grid_count": 5},
            "signal_bot": {
                "check_interval_minutes": 60,
                "push_cooldown_hours_alert": 1,
                "push_cooldown_hours_normal": 6,
                "push_cooldown_hours_tradable": 2,
                "conservative_grid_reduce": 10,
                "trigger_thresholds": {"profit_rate_low": 0.012},
            },
        }

    @pytest.fixture
    def mock_binance_client(self):
        return AsyncMock()

    @pytest.fixture
    def mock_notification_client(self):
        return AsyncMock()

    @pytest.fixture
    def mock_grid_calculator(self):
        calc = MagicMock()
        calc.calculate_baseline_atr.return_value = Decimal("1000")
        calc.calculate_dynamic_grid_params.return_value = MagicMock(
            grid_count=10,
            profit_rate=Decimal("0.05"),
            lower_boundary=Decimal("45000"),
            upper_boundary=Decimal("55000"),
            grid_spacing=Decimal("100"),
            grid_mode=MagicMock(value="NEUTRAL"),
            stop_loss_low=Decimal("40000"),
            stop_loss_high=Decimal("60000"),
            stop_move_up_price=None,
            stop_move_down_price=None,
        )
        calc.validate_profit_rate.return_value = (True, None)
        calc.validate_position_size.return_value = (True, "", None)
        return calc

    @pytest.mark.asyncio
    async def test_calculate_grid_params_uses_klines(
        self,
        mock_kline_service,
        mock_binance_client,
        mock_notification_client,
        mock_grid_calculator,
        mock_config,
    ):
        """获取历史K线数据用于计算基准ATR"""
        # 准备模拟K线数据
        klines_1d = _generate_trend_klines(
            count=100, start_price=50000, trend=50, volatility=200
        )
        mock_kline_service.get_klines.return_value = klines_1d

        bot = GridSignalBot(
            binance_client=mock_binance_client,
            kline_service=mock_kline_service,
            notification_client=mock_notification_client,
            grid_calculator=mock_grid_calculator,
            config=mock_config,
        )

        # 准备市场分析结果
        analysis = MarketAnalysis(
            state=MarketState.OSCILLATION,
            trend_strength=Decimal("0.1"),
            adx_1h=Decimal("20"),
            adx_4h=Decimal("18"),
            ema20_1h=Decimal("50000"),
            ema50_1h=Decimal("49000"),
            current_price=Decimal("50000"),
            atr_smooth=Decimal("500"),
            confidence=Decimal("0.5"),
        )

        # 直接测试 _calculate_grid_params
        params = await bot._calculate_grid_params(MOCK_SYMBOL, analysis)

        # 验证 get_klines 被正确调用（1日K线，100条）
        mock_kline_service.get_klines.assert_called_once_with(
            symbol=MOCK_SYMBOL, interval="1d", limit=100
        )
        # 验证网格参数被计算
        assert params is not None
        assert params.grid_count == 10

    @pytest.mark.asyncio
    async def test_kline_failure_during_grid_params(
        self,
        mock_kline_service,
        mock_binance_client,
        mock_notification_client,
        mock_grid_calculator,
        mock_config,
    ):
        """K线数据获取失败，异常应向上传播"""
        mock_kline_service.get_klines.side_effect = Exception("K线服务异常")

        bot = GridSignalBot(
            binance_client=mock_binance_client,
            kline_service=mock_kline_service,
            notification_client=mock_notification_client,
            grid_calculator=mock_grid_calculator,
            config=mock_config,
        )

        analysis = MarketAnalysis(
            state=MarketState.OSCILLATION,
            trend_strength=Decimal("0.1"),
            adx_1h=Decimal("20"),
            adx_4h=Decimal("18"),
            ema20_1h=Decimal("50000"),
            ema50_1h=Decimal("49000"),
            current_price=Decimal("50000"),
            atr_smooth=Decimal("500"),
            confidence=Decimal("0.5"),
        )

        with pytest.raises(Exception, match="K线服务异常"):
            await bot._calculate_grid_params(MOCK_SYMBOL, analysis)


# ========== 6. 指标计算测试 ==========


class TestIndicatorCalculation:
    """测试基于K线数据的指标计算"""

    def test_calculate_adx_ema_atr(self, detector):
        """使用K线数据正确计算ADX、EMA、ATR等指标"""
        klines = _generate_trend_klines(
            count=100, start_price=50000, trend=100, volatility=150
        )

        result = detector._calculate_indicators(klines)

        assert "adx" in result
        assert "ema_fast" in result
        assert "ema_slow" in result
        assert "atr" in result
        assert "current_price" in result

        assert isinstance(result["adx"], Decimal)
        assert isinstance(result["ema_fast"], Decimal)
        assert isinstance(result["atr"], Decimal)
        assert result["current_price"] > Decimal("0")

    def test_insufficient_data_raises_error(self, detector):
        """数据不足（少于30根）时抛出ValueError"""
        klines = _generate_trend_klines(
            count=20, start_price=50000, trend=50, volatility=200
        )

        with pytest.raises(ValueError, match="K线数据不足，至少需要30根"):
            detector._calculate_indicators(klines)

    def test_empty_data_raises_error(self, detector):
        """空数据时抛出ValueError"""
        with pytest.raises(ValueError, match="K线数据不足"):
            detector._calculate_indicators([])

    def test_calculate_smooth_atr(self, detector):
        """使用K线数据计算平滑ATR"""
        klines = _generate_trend_klines(
            count=100, start_price=50000, trend=50, volatility=200
        )

        atr_smooth = detector._calculate_smooth_atr(klines)

        assert isinstance(atr_smooth, Decimal)
        assert atr_smooth >= Decimal("0")

    def test_smooth_atr_insufficient_data(self, detector):
        """数据不足时平滑ATR返回默认值0"""
        klines = _generate_trend_klines(
            count=20, start_price=50000, trend=50, volatility=200
        )

        atr_smooth = detector._calculate_smooth_atr(klines)

        assert atr_smooth == Decimal("0")

    def test_smooth_atr_empty_data(self, detector):
        """空数据时平滑ATR返回默认值0"""
        atr_smooth = detector._calculate_smooth_atr([])

        assert atr_smooth == Decimal("0")

    def test_calculate_price_change(self, detector):
        """使用K线数据正确计算价格变动率"""
        klines = [
            {
                "open_time": 1700000000000,
                "open": 50000.0,
                "high": 51000.0,
                "low": 49000.0,
                "close": 50000.0,
                "volume": 100.0,
                "close_time": 1700003600000,
                "quote_volume": 5000000.0,
                "trade_count": 1000,
            },
            {
                "open_time": 1700003600000,
                "open": 50000.0,
                "high": 51500.0,
                "low": 49500.0,
                "close": 51000.0,
                "volume": 120.0,
                "close_time": 1700007200000,
                "quote_volume": 6000000.0,
                "trade_count": 1200,
            },
        ]

        price_change = detector._calculate_price_change(klines)

        assert isinstance(price_change, Decimal)
        # (51000 - 50000) / 50000 = 0.02
        assert price_change == Decimal("0.02")

    def test_price_change_insufficient_data(self, detector):
        """只有1根K线时价格变动率返回0"""
        klines = [
            {
                "open_time": 1700000000000,
                "open": 50000.0,
                "high": 51000.0,
                "low": 49000.0,
                "close": 50500.0,
                "volume": 100.0,
                "close_time": 1700003600000,
                "quote_volume": 5000000.0,
                "trade_count": 1000,
            },
        ]

        price_change = detector._calculate_price_change(klines)

        assert price_change == Decimal("0")

    def test_price_change_empty_data(self, detector):
        """空数据时价格变动率返回0"""
        assert detector._calculate_price_change([]) == Decimal("0")

    def test_price_change_zero_prev_close(self, detector):
        """前一根K线收盘价为0时价格变动率返回0"""
        klines = [
            {
                "open_time": 1700000000000,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "close": 0.0,
                "volume": 100.0,
                "close_time": 1700003600000,
                "quote_volume": 5000000.0,
                "trade_count": 1000,
            },
            {
                "open_time": 1700003600000,
                "open": 50000.0,
                "high": 51500.0,
                "low": 49500.0,
                "close": 51000.0,
                "volume": 120.0,
                "close_time": 1700007200000,
                "quote_volume": 6000000.0,
                "trade_count": 1200,
            },
        ]

        price_change = detector._calculate_price_change(klines)

        assert price_change == Decimal("0")

    def test_calculate_indicators_with_adx_period(self, detector):
        """使用自定义ADX周期计算指标"""
        klines = _generate_trend_klines(
            count=100, start_price=50000, trend=100, volatility=150
        )

        # 使用默认ADX周期（10）
        result_default = detector._calculate_indicators(klines)
        # 使用自定义ADX周期（14）
        result_custom = detector._calculate_indicators(
            klines, adx_period=14
        )

        assert isinstance(result_default["adx"], Decimal)
        assert isinstance(result_custom["adx"], Decimal)

    def test_calculate_indicators_with_tail_ema(self, detector):
        """使用tail截取方式计算EMA（4h专用）"""
        klines = _generate_trend_klines(
            count=100, start_price=50000, trend=100, volatility=150
        )

        result = detector._calculate_indicators(klines, use_tail_ema=True)

        assert isinstance(result["ema_fast"], Decimal)
        assert isinstance(result["ema_slow"], Decimal)
        assert result["ema_fast"] > Decimal("0")
        assert result["ema_slow"] > Decimal("0")

    def test_missing_columns_raises_error(self, detector):
        """K线数据缺少必需列时抛出ValueError"""
        klines = [
            {
                "open_time": 1700000000000,
                "open": 50000.0,
                "close": 50500.0,
            }
        ] * 50  # 放大到50条

        with pytest.raises(ValueError, match="缺少必需的列"):
            detector._calculate_indicators(klines)


# ========== 7. 边界突破检测测试 ==========


class TestBoundaryBreakthrough:
    """测试边界突破检测"""

    @pytest.mark.asyncio
    async def test_check_boundary_breakthrough_up(
        self, mock_kline_service, detector
    ):
        """价格突破上边界返回 ('UP', True)"""
        klines = [
            {
                "open_time": 1700000000000,
                "open": 50000.0,
                "high": 65000.0,
                "low": 49000.0,
                "close": 62000.0,
                "volume": 100.0,
                "close_time": 1700003600000,
                "quote_volume": 5000000.0,
                "trade_count": 1000,
            }
        ]
        mock_kline_service.get_klines.return_value = klines

        breached, direction = await detector.check_boundary_breakthrough(
            MOCK_SYMBOL,
            upper_boundary=Decimal("60000"),
            lower_boundary=Decimal("40000"),
        )

        assert breached is True
        assert direction == "UP"

    @pytest.mark.asyncio
    async def test_check_boundary_breakthrough_down(
        self, mock_kline_service, detector
    ):
        """价格突破下边界返回 ('DOWN', True)"""
        klines = [
            {
                "open_time": 1700000000000,
                "open": 50000.0,
                "high": 51000.0,
                "low": 35000.0,
                "close": 38000.0,
                "volume": 100.0,
                "close_time": 1700003600000,
                "quote_volume": 5000000.0,
                "trade_count": 1000,
            }
        ]
        mock_kline_service.get_klines.return_value = klines

        breached, direction = await detector.check_boundary_breakthrough(
            MOCK_SYMBOL,
            upper_boundary=Decimal("60000"),
            lower_boundary=Decimal("40000"),
        )

        assert breached is True
        assert direction == "DOWN"

    @pytest.mark.asyncio
    async def test_check_boundary_no_breakthrough(
        self, mock_kline_service, detector
    ):
        """价格在边界内返回 (False, None)"""
        klines = [
            {
                "open_time": 1700000000000,
                "open": 50000.0,
                "high": 51000.0,
                "low": 49000.0,
                "close": 50500.0,
                "volume": 100.0,
                "close_time": 1700003600000,
                "quote_volume": 5000000.0,
                "trade_count": 1000,
            }
        ]
        mock_kline_service.get_klines.return_value = klines

        breached, direction = await detector.check_boundary_breakthrough(
            MOCK_SYMBOL,
            upper_boundary=Decimal("60000"),
            lower_boundary=Decimal("40000"),
        )

        assert breached is False
        assert direction is None