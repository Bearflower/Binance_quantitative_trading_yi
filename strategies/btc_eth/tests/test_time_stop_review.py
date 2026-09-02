"""
测试 MTPCS 策略「时间平仓复核制」（v6.27）

覆盖范围：
1. _check_time_stop 分支编排：
   - 守卫（tp1_hit / 无持仓 / 复核已完成）
   - 未到期
   - review_enabled=false 原逻辑回归（无条件到点平）
   - 复核期耗尽兜底（无条件平）
   - 复核数据获取失败保守平仓
   - 复核判定「该持有」继续持有
   - 复核判定「不该持有」平仓
2. _should_keep_position 三机制编排：
   - 数据可用性预检（任一关键指标缺失 → 保守不该持有）
   - 三机制全放行 → 该持有
   - 方向一致性拒绝 / 重度过热拒绝 / 波动极端拒绝
   - 编排层异常兜底
3. _do_time_stop_close 平仓统一出口：
   - 成功置 time_stop_review_done（set_tp1_hit=False 不置 tp1_hit）
   - set_tp1_hit=True 时置 tp1_hit（review_enabled=false 原逻辑）
   - 失败不置标记（幂等重试）

对应验收标准：AC-1~AC-18（v6.27 需求文档第八章）。
"""
import asyncio
import copy
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Optional

import pandas as pd
import pytest

from strategies.btc_eth.strategy import BTCEthStrategy, PositionState, _safe_last


# ============================================================
# 测试夹具：配置、指标、K线、策略实例、持仓
# ============================================================

def make_ranging_config() -> Dict:
    """构造与 config.yaml 结构一致的震荡策略配置（含三机制完整字段）。"""
    return {
        'enabled': True,
        'direction_alignment': {
            'enabled': True,
            'fast_ma': 'EMA21',
            'slow_ma': 'EMA55',
            'fallback_on_missing': False,
            'stick_threshold_pct': 0.3,
        },
        'overheat_protection': {
            'enabled': True,
            'downgrade_enabled': True,
            'bias_fast_downgrade_pct': 4.0,
            'bias_fast_ban_pct': 6.0,
            'bias_slow_downgrade_pct': 6.0,
            'bias_slow_ban_pct': 9.0,
            'rsi_extreme_long': 85,
            'rsi_extreme_short': 15,
            'rsi_warn_long': 75,
            'rsi_warn_short': 25,
        },
        'volume_confirm': {'enabled': True, 'shrink_ratio': 1.0},
        'volatility_regime': {
            'enabled': True,
            'atr_long_period': 50,
            'spike_ratio': 2.0,
            'pause_bars': 12,
            'pause_interval_hours': 4,
        },
    }


def make_time_stop_config(
    review_enabled: bool = True,
    max_holding_hours: int = 96,
    close_ratio: float = 0.5,
    max_review_hours: int = 24,
) -> Dict:
    """构造单等级 time_stop 配置。"""
    return {
        'max_holding_hours': max_holding_hours,
        'close_ratio': close_ratio,
        'review_enabled': review_enabled,
        'max_review_hours': max_review_hours,
    }


def make_indicators(
    ema21_1d: float = 110.0,
    ema55_1d: float = 100.0,
    rsi_4h: float = 50.0,
    atr_4h: float = 2.0,
    atr_long_4h: float = 2.0,
    drop_field: Optional[str] = None,
) -> Dict:
    """构造多时间框架指标字典。

    默认「多头排列 + 中性过热 + 中性波动」，适配 direction=LONG 的「该持有」场景。
    drop_field 用于模拟数据缺失（如 ('4h','RSI')）。
    """
    indicators = {
        '1d': {
            'EMA21': pd.Series([ema21_1d]),
            'EMA55': pd.Series([ema55_1d]),
        },
        '4h': {
            'EMA21': pd.Series([100.0]),
            'EMA55': pd.Series([100.0]),
            'RSI': pd.Series([rsi_4h]),
            'ATR': pd.Series([atr_4h]),
            'ATR_long': pd.Series([atr_long_4h]),
        },
    }
    if drop_field is not None:
        tf, field = drop_field
        indicators[tf].pop(field, None)
    return indicators


def make_klines(close_1d: float = 100.0) -> Dict:
    """构造最小 K 线字典（供 _should_keep_position 的 klines 非空校验）。"""
    return {
        '4h': [
            {'open': 98.0, 'high': 101.0, 'low': 97.0, 'close': 99.0, 'volume': 800.0},
            {'open': 99.0, 'high': 102.0, 'low': 97.0, 'close': close_1d, 'volume': 500.0},
        ],
        '1d': [
            {'open': 99.0, 'close': 98.0},
            {'open': 98.0, 'close': close_1d},
        ],
    }


def make_strategy(
    review_enabled: bool = True,
    close_returns: bool = True,
) -> BTCEthStrategy:
    """构造绕过 __init__ 的策略实例，注入被测方法所需属性与 mock。"""
    strategy = object.__new__(BTCEthStrategy)
    strategy.risk_config = {
        'ranging_strategy': make_ranging_config(),
        'signal_levels': {
            'S': {'time_stop': make_time_stop_config(review_enabled=review_enabled)},
        },
    }
    strategy.symbol_extreme_pause_until = {}
    strategy.timeframes = ['1h', '4h', '1d']
    install_fake_close(strategy, returns=close_returns)
    return strategy


def install_fake_close(strategy: BTCEthStrategy, returns: bool = True) -> BTCEthStrategy:
    """注入 _close_position 的 async mock，记录调用。"""
    calls = []

    async def fake_close(**kwargs):
        calls.append(kwargs)
        return returns

    strategy._close_position = fake_close
    strategy.close_calls = calls
    return strategy


def install_fake_market_data(
    strategy: BTCEthStrategy,
    indicators=None,
    klines=None,
) -> BTCEthStrategy:
    """注入 _get_review_market_data 的 async mock（返回指定数据或 (None,None)）。"""
    async def fake_get_data(symbol):
        return indicators, klines
    strategy._get_review_market_data = fake_get_data
    return strategy


def install_fake_keep(strategy: BTCEthStrategy, keep: bool, reason: str = "") -> BTCEthStrategy:
    """注入 _should_keep_position 的 async mock。"""
    async def fake_keep(symbol, position, indicators, klines):
        return keep, reason
    strategy._should_keep_position = fake_keep
    return strategy


def make_position(
    grade: str = 'S',
    direction: str = 'LONG',
    hours_ago: float = 0,
    qty: float = 1.0,
    tp1_hit: bool = False,
    review_done: bool = False,
) -> PositionState:
    """构造持仓状态。"""
    pos = PositionState()
    pos.grade = grade
    pos.direction = direction
    pos.entry_time = datetime.now() - timedelta(hours=hours_ago)
    pos.entry_price = Decimal('100')
    pos.initial_quantity = Decimal(str(qty))
    pos.current_quantity = Decimal(str(qty))
    pos.tp1_hit = tp1_hit
    pos.time_stop_review_done = review_done
    return pos


# ============================================================
# 1. _check_time_stop 分支编排
# ============================================================

class TestCheckTimeStopGuard:
    """守卫：tp1_hit / 无持仓 / 复核已完成 → 不检查不平仓。"""

    def test_tp1_hit_returns(self):
        strategy = make_strategy()
        pos = make_position(hours_ago=97, tp1_hit=True)
        asyncio.run(strategy._check_time_stop('BTCUSDT', pos))
        assert strategy.close_calls == []

    def test_no_quantity_returns(self):
        strategy = make_strategy()
        pos = make_position(hours_ago=97, qty=0)
        asyncio.run(strategy._check_time_stop('BTCUSDT', pos))
        assert strategy.close_calls == []

    def test_review_done_returns(self):
        strategy = make_strategy()
        pos = make_position(hours_ago=97, review_done=True)
        asyncio.run(strategy._check_time_stop('BTCUSDT', pos))
        assert strategy.close_calls == []


class TestCheckTimeStopNotDue:
    """未到期 → 返回，不平仓。"""

    def test_below_max_holding(self):
        strategy = make_strategy()
        pos = make_position(hours_ago=10)
        asyncio.run(strategy._check_time_stop('BTCUSDT', pos))
        assert strategy.close_calls == []


class TestCheckTimeStopLegacy:
    """review_enabled=false：完全恢复原逻辑（无条件到点平，置 tp1_hit）。"""

    def test_legacy_close_on_due(self):
        strategy = make_strategy(review_enabled=False)
        pos = make_position(hours_ago=97)
        asyncio.run(strategy._check_time_stop('BTCUSDT', pos))
        assert len(strategy.close_calls) == 1
        call = strategy.close_calls[0]
        assert call['close_reason'] == 'TIME_STOP'
        assert call['close_quantity'] == Decimal('0.5')  # close_ratio=0.5
        assert pos.tp1_hit is True  # 原逻辑置位
        assert pos.time_stop_review_done is True


class TestCheckTimeStopReviewExhausted:
    """复核期耗尽 → 无条件平 TIME_STOP（最高优先级，不置 tp1_hit）。"""

    def test_review_exhausted_force_close(self):
        strategy = make_strategy(review_enabled=True)
        pos = make_position(hours_ago=121)  # 96 + 24 = 120，超兜底线
        asyncio.run(strategy._check_time_stop('BTCUSDT', pos))
        assert len(strategy.close_calls) == 1
        call = strategy.close_calls[0]
        assert call['close_reason'] == 'TIME_STOP'
        assert pos.tp1_hit is False  # 复核路径不置 tp1_hit（避免误激活 also_on_tp1）
        assert pos.time_stop_review_done is True


class TestCheckTimeStopReviewDataMissing:
    """复核数据获取失败 → 保守平 TIME_STOP_REVIEW。"""

    def test_data_missing_conservative_close(self):
        strategy = make_strategy(review_enabled=True)
        install_fake_market_data(strategy, indicators=None, klines=None)
        pos = make_position(hours_ago=97)
        asyncio.run(strategy._check_time_stop('BTCUSDT', pos))
        assert len(strategy.close_calls) == 1
        assert strategy.close_calls[0]['close_reason'] == 'TIME_STOP_REVIEW'
        assert pos.time_stop_review_done is True


class TestCheckTimeStopReviewKeep:
    """复核判定「该持有」→ 继续持有，不平仓。"""

    def test_keep_position(self):
        strategy = make_strategy(review_enabled=True)
        install_fake_market_data(
            strategy, indicators=make_indicators(), klines=make_klines())
        install_fake_keep(strategy, keep=True)
        pos = make_position(hours_ago=97)
        asyncio.run(strategy._check_time_stop('BTCUSDT', pos))
        assert strategy.close_calls == []
        assert pos.time_stop_review_done is False  # 未完成，下周期继续复核


class TestCheckTimeStopReviewClose:
    """复核判定「不该持有」→ 平 TIME_STOP_REVIEW。"""

    def test_not_keep_close(self):
        strategy = make_strategy(review_enabled=True)
        install_fake_market_data(
            strategy, indicators=make_indicators(), klines=make_klines())
        install_fake_keep(strategy, keep=False, reason="方向一致性拒绝：多头排列禁止做空")
        pos = make_position(hours_ago=97)
        asyncio.run(strategy._check_time_stop('BTCUSDT', pos))
        assert len(strategy.close_calls) == 1
        call = strategy.close_calls[0]
        assert call['close_reason'] == 'TIME_STOP_REVIEW'
        assert call['close_quantity'] == Decimal('0.5')
        assert pos.time_stop_review_done is True


# ============================================================
# 2. _should_keep_position 三机制编排
# ============================================================

class TestShouldKeepPositionDataPrecheck:
    """数据可用性预检：任一关键指标缺失 → 保守不该持有。"""

    @pytest.mark.parametrize("drop_field", [
        ('1d', 'EMA21'), ('1d', 'EMA55'),
        ('4h', 'RSI'), ('4h', 'ATR'), ('4h', 'ATR_long'),
    ])
    def test_missing_field_rejects(self, drop_field):
        strategy = make_strategy()
        pos = make_position(direction='LONG')
        indicators = make_indicators(drop_field=drop_field)
        keep, reason = asyncio.run(strategy._should_keep_position(
            'BTCUSDT', pos, indicators, make_klines()))
        assert keep is False
        assert '复核数据缺失' in reason

    def test_missing_klines_rejects(self):
        strategy = make_strategy()
        pos = make_position(direction='LONG')
        keep, reason = asyncio.run(strategy._should_keep_position(
            'BTCUSDT', pos, make_indicators(), {'1d': [], '4h': []}))
        assert keep is False
        assert 'K线数据为空' in reason


class TestShouldKeepPositionAllPass:
    """三机制全放行 → 该持有。"""

    def test_long_keep(self):
        strategy = make_strategy()
        pos = make_position(direction='LONG')
        keep, reason = asyncio.run(strategy._should_keep_position(
            'BTCUSDT', pos, make_indicators(), make_klines()))
        assert keep is True
        assert reason == ""


class TestShouldKeepPositionDirectionReject:
    """方向一致性：多头排列禁止做空 → 该平仓。"""

    def test_short_rejected_in_bullish(self):
        strategy = make_strategy()
        pos = make_position(direction='SHORT')
        indicators = make_indicators(ema21_1d=110.0, ema55_1d=100.0)
        keep, reason = asyncio.run(strategy._should_keep_position(
            'BTCUSDT', pos, indicators, make_klines()))
        assert keep is False
        assert '方向一致性' in reason


class TestShouldKeepPositionOverheatReject:
    """重度过热：多头持仓 RSI 极值 → 该平仓。"""

    def test_overheat_long_rejected(self):
        strategy = make_strategy()
        pos = make_position(direction='LONG')
        indicators = make_indicators(rsi_4h=90.0)  # > rsi_extreme_long=85
        keep, reason = asyncio.run(strategy._should_keep_position(
            'BTCUSDT', pos, indicators, make_klines()))
        assert keep is False
        assert '过热' in reason or '极值' in reason or 'RSI' in reason


class TestShouldKeepPositionVolatilityReject:
    """波动极端：ATR/ATR_long 突变 → 该平仓（并写入 symbol_extreme_pause_until）。"""

    def test_volatility_spike_rejected(self):
        strategy = make_strategy()
        pos = make_position(direction='LONG')
        indicators = make_indicators(atr_4h=10.0, atr_long_4h=2.0)  # ratio=5 > 2
        keep, reason = asyncio.run(strategy._should_keep_position(
            'BTCUSDT', pos, indicators, make_klines()))
        assert keep is False
        assert '极端期' in reason or '波动' in reason
        assert 'BTCUSDT' in strategy.symbol_extreme_pause_until  # 预期副作用


class TestShouldKeepPositionExceptionFallback:
    """编排层异常 → 保守不该持有，不中断主循环。"""

    def test_exception_conservative(self, monkeypatch):
        strategy = make_strategy()

        async def boom(symbol, position, indicators, klines):
            raise RuntimeError("模拟异常")
        monkeypatch.setattr(strategy, '_check_direction_alignment', boom)

        pos = make_position(direction='LONG')
        keep, reason = asyncio.run(strategy._should_keep_position(
            'BTCUSDT', pos, make_indicators(), make_klines()))
        assert keep is False
        assert '复核异常' in reason


# ============================================================
# 3. _do_time_stop_close 平仓统一出口
# ============================================================

class TestDoTimeStopClose:
    """平仓统一出口：成功置标记 / 失败不置标记。"""

    def test_success_sets_review_done_not_tp1(self):
        strategy = make_strategy(close_returns=True)
        pos = make_position(qty=2.0)
        asyncio.run(strategy._do_time_stop_close(
            'BTCUSDT', pos, Decimal('0.5'), reason='TIME_STOP_REVIEW'))
        assert len(strategy.close_calls) == 1
        assert strategy.close_calls[0]['close_quantity'] == Decimal('1.0')
        assert pos.time_stop_review_done is True
        assert pos.tp1_hit is False

    def test_success_with_set_tp1_hit(self):
        strategy = make_strategy(close_returns=True)
        pos = make_position(qty=2.0)
        asyncio.run(strategy._do_time_stop_close(
            'BTCUSDT', pos, Decimal('0.5'), reason='TIME_STOP', set_tp1_hit=True))
        assert pos.time_stop_review_done is True
        assert pos.tp1_hit is True

    def test_failure_keeps_state(self):
        strategy = make_strategy(close_returns=False)
        pos = make_position(qty=2.0)
        asyncio.run(strategy._do_time_stop_close(
            'BTCUSDT', pos, Decimal('0.5'), reason='TIME_STOP_REVIEW'))
        assert pos.time_stop_review_done is False
        assert pos.tp1_hit is False


# ============================================================
# 4. 向后兼容：配置缺失时 review_enabled 默认 False
# ============================================================

class TestConfigBackwardCompatibility:
    """配置缺失 review_enabled 时，默认走原逻辑（行为不变）。"""

    def test_missing_review_config_legacy_behavior(self):
        strategy = object.__new__(BTCEthStrategy)
        strategy.risk_config = {
            'ranging_strategy': make_ranging_config(),
            'signal_levels': {
                'S': {'time_stop': {'max_holding_hours': 96, 'close_ratio': 0.5}},
            },
        }
        strategy.symbol_extreme_pause_until = {}
        install_fake_close(strategy, returns=True)
        pos = make_position(hours_ago=97)
        asyncio.run(strategy._check_time_stop('BTCUSDT', pos))
        assert len(strategy.close_calls) == 1
        assert strategy.close_calls[0]['close_reason'] == 'TIME_STOP'
        assert pos.tp1_hit is True  # 原逻辑
