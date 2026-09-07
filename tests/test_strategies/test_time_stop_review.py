"""
时间止损复核逻辑测试

覆盖：
- _time_stop_review 的豁免规则（距第一目标较近→继续持有）
- _time_stop_review 的评分判定（空头逻辑成立→持有；被破坏→止损）
- 数据异常/缺失时的 bias_hold 决策
- 各维度评分函数（趋势/反转形态/量能/情绪）
"""
import sys
import os
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from strategies.new_coin.executor import TradingExecutor


def create_mock_binance_api():
    """创建 Mock BinanceClient"""
    mock = MagicMock()
    mock._request = AsyncMock()
    mock.place_order = AsyncMock()
    mock.place_conditional_order = AsyncMock()
    return mock


def create_mock_db():
    """创建 Mock DatabaseManager"""
    mock = MagicMock()
    mock.execute = AsyncMock()
    return mock


def create_mock_notification():
    """创建 Mock NotificationClient"""
    mock = MagicMock()
    mock.send = AsyncMock()
    return mock


def create_base_config() -> dict:
    """创建基础配置（含时间止损复核）"""
    return {
        'trading': {
            'leverage': 2,
            'single_position_margin': 50,
            'stop_loss_percent': 0.05,
            'take_profit_percent': 0.10,
            'batch_take_profit': {
                'enabled': True,
                'target1_atr_multiplier': 1.5,
                'target1_close_percent': 0.30,
                'target2_atr_multiplier': 3.5,
                'target2_close_percent': 0.40,
                'trailing_stop_atr_multiplier': 1.5,
            },
            'time_stop': {
                'enabled': True,
                'max_holding_hours': 72,
            },
            'time_stop_review': {
                'enabled': True,
                'hold_threshold': 5.0,
                'exempt_progress': 0.7,
                'bias_hold': True,
                'weights': {
                    'trend': 0.40,
                    'price_action': 0.30,
                    'volume': 0.20,
                    'sentiment': 0.10,
                },
                'trend': {'drop_reference': 0.10},
                'reversal': {
                    'lookback': 5,
                    'rebound_ratio': 0.01,
                    'breakout_ratio': 0.01,
                },
                'volume': {
                    'lookback': 5,
                    'volume_surge': 1.5,
                },
                'sentiment': {'funding_positive': 0.0001},
            },
            'emergency_stop': {
                'enabled': True,
                'check_minutes': 15,
                'trigger_percent': 0.015,
            },
            'atr_stop': {'multiplier': 2.5},
        }
    }


def _deep_merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def create_executor(
    config_override: dict = None,
    mock_binance=None,
    mock_db=None,
    mock_notification=None,
    mock_kline=None,
) -> TradingExecutor:
    """创建 TradingExecutor 实例（所有外部服务使用 mock）"""
    config = create_base_config()
    if config_override:
        _deep_merge(config, config_override)

    mock_binance = mock_binance or create_mock_binance_api()
    mock_db = mock_db or create_mock_db()
    mock_notification = mock_notification or create_mock_notification()
    mock_kline = mock_kline or MagicMock()

    executor = TradingExecutor(
        binance_api=mock_binance,
        db=mock_db,
        notification=mock_notification,
        config=config,
        kline_service=mock_kline,
    )
    return executor


def make_klines(close_seq, high_seq=None, low_seq=None, open_seq=None, volume=100.0):
    """根据收盘价序列构造K线列表"""
    klines = []
    n = len(close_seq)
    for i in range(n):
        close = close_seq[i]
        prev = close_seq[i - 1] if i > 0 else close
        klines.append({
            'open': open_seq[i] if open_seq else prev,
            'high': high_seq[i] if high_seq else max(prev, close),
            'low': low_seq[i] if low_seq else min(prev, close),
            'close': close,
            'volume': volume,
            'open_time': i,
        })
    return klines


class TestTimeStopReview:
    """_time_stop_review() 测试"""

    @pytest.mark.asyncio
    async def test_exempt_when_near_target1(self):
        """距第一目标较近时豁免时间止损（继续持有）"""
        mock_binance = create_mock_binance_api()
        # entry=100, atr=2, target1=100-1.5*2=97；progress=(100-97.5)/3=0.83≥0.7 → 豁免
        mock_binance._request.return_value = {'price': '97.5'}
        mock_kline = MagicMock()
        mock_kline.get_klines = AsyncMock(return_value=make_klines([100]*30))

        executor = create_executor(
            mock_binance=mock_binance,
            mock_kline=mock_kline,
        )
        tracking = {'entry_price': 100.0, 'atr': 2.0}

        should_stop, reason = await executor._time_stop_review("TESTUSDT", tracking)

        assert should_stop is False
        assert "豁免" in reason

    @pytest.mark.asyncio
    async def test_hold_when_downtrend_holds(self):
        """空头逻辑仍成立（持续阴跌、无反转）→ 继续持有"""
        mock_binance = create_mock_binance_api()
        mock_binance._request.return_value = {'price': '99.0'}
        # 持续下跌的K线
        close_seq = [100, 99.8, 99.6, 99.4, 99.2]
        mock_kline = MagicMock()
        mock_kline.get_klines = AsyncMock(return_value=make_klines(close_seq * 6))

        executor = create_executor(
            mock_binance=mock_binance,
            mock_kline=mock_kline,
        )
        tracking = {'entry_price': 100.0, 'atr': 2.0}

        should_stop, _ = await executor._time_stop_review("TESTUSDT", tracking)

        # 现价99 < 开仓100，且无反弹，评分应高 → 持有
        assert should_stop is False

    @pytest.mark.asyncio
    async def test_stop_when_reversal(self):
        """空头逻辑被破坏（放量上攻、反弹破位）→ 止损"""
        mock_binance = create_mock_binance_api()
        mock_binance._request.return_value = {'price': '101.5'}
        # 前期下跌后强烈反弹至101.5（高于近期低点）
        close_seq = [100, 99, 98.5, 99.5, 101.0]
        mock_kline = MagicMock()
        mock_kline.get_klines = AsyncMock(return_value=make_klines(close_seq * 6, volume=200.0))

        executor = create_executor(
            mock_binance=mock_binance,
            mock_kline=mock_kline,
        )
        tracking = {'entry_price': 100.0, 'atr': 2.0}

        should_stop, reason = await executor._time_stop_review("TESTUSDT", tracking)

        assert should_stop is True
        assert "止损" in reason

    @pytest.mark.asyncio
    async def test_bias_hold_when_kline_missing(self):
        """K线缺失时按 bias_hold 偏向继续持有"""
        mock_binance = create_mock_binance_api()
        mock_binance._request.return_value = {'price': '99.0'}
        mock_kline = MagicMock()
        mock_kline.get_klines = AsyncMock(return_value=[])

        executor = create_executor(
            mock_binance=mock_binance,
            mock_kline=mock_kline,
        )
        tracking = {'entry_price': 100.0, 'atr': 2.0}

        should_stop, reason = await executor._time_stop_review("TESTUSDT", tracking)

        assert should_stop is False
        assert "K线" in reason

    @pytest.mark.asyncio
    async def test_bias_hold_false_on_missing_data(self):
        """bias_hold=False 时，K线缺失则止损（偏保守）"""
        mock_binance = create_mock_binance_api()
        mock_binance._request.return_value = {'price': '99.0'}
        mock_kline = MagicMock()
        mock_kline.get_klines = AsyncMock(return_value=[])

        executor = create_executor(
            config_override={'trading': {'time_stop_review': {'bias_hold': False}}},
            mock_binance=mock_binance,
            mock_kline=mock_kline,
        )
        tracking = {'entry_price': 100.0, 'atr': 2.0}

        should_stop, _ = await executor._time_stop_review("TESTUSDT", tracking)

        assert should_stop is True

    @pytest.mark.asyncio
    async def test_bias_hold_when_price_abnormal(self):
        """当前价格异常（<=0）时按 bias_hold 偏向继续持有"""
        mock_binance = create_mock_binance_api()
        mock_binance._request.return_value = {'price': '0'}

        executor = create_executor(mock_binance=mock_binance)
        tracking = {'entry_price': 100.0, 'atr': 2.0}

        should_stop, reason = await executor._time_stop_review("TESTUSDT", tracking)

        assert should_stop is False
        assert "价格异常" in reason


class TestReviewScoring:
    """复核评分函数测试"""

    def _executor(self):
        return create_executor()

    def test_volume_no_surge_high_score(self):
        """未放量上攻 → 量能分高（空头健康）8.0"""
        ex = self._executor()
        klines = make_klines([100, 99.8, 99.6, 99.4, 99.2, 99.0, 98.8, 98.6, 98.4, 98.2], volume=100.0)
        assert ex._score_volume(klines) == 8.0

    def test_volume_surge_up_low_score(self):
        """放量上攻 → 量能分低（空头受威胁）2.0"""
        ex = self._executor()
        # 前半段基量100，后半段放量且阳线占比高
        baseline = make_klines([100, 99.8, 99.6, 99.4, 99.2], volume=100.0)
        recent = make_klines([99.2, 99.5, 100.0, 100.5, 101.0], volume=200.0)
        klines = baseline + recent
        assert ex._score_volume(klines) == 2.0

    def test_price_action_reversal_low_score(self):
        """强反弹破位 → 反转形态分低（削弱空头）"""
        ex = self._executor()
        klines = make_klines([100, 99, 98.5, 99.5, 101.5])
        score = ex._score_price_action(klines, current_price=101.5)
        assert score < 5.0

    def test_price_action_no_reversal_high_score(self):
        """无反转 → 反转形态分高（空头成立）"""
        ex = self._executor()
        klines = make_klines([100, 99.8, 99.6, 99.4, 99.2])
        score = ex._score_price_action(klines, current_price=99.2)
        assert score >= 5.0

    def test_sentiment_neutral(self):
        """情绪维度当前恒为中性的 5.0"""
        ex = self._executor()
        assert ex._score_sentiment() == 5.0

    def test_compute_review_score_range(self):
        """综合评分应在 0~10 区间内"""
        ex = self._executor()
        klines = make_klines([100, 99, 98.5, 99.5, 101.5] * 6)
        score = ex._compute_review_score(100.0, 101.5, 2.0, klines)
        assert 0.0 <= score <= 10.0