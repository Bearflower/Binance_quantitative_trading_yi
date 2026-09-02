"""
测试 MTPCS 策略「震荡反转风控三机制」（v6.26）

覆盖范围：
1. _safe_last 统一兜底读取
2. 机制①：方向一致性对齐（_check_direction_alignment）
3. 机制②：乖离率 / 过热保护（_evaluate_overheat / _check_overheat_ban / _apply_overheat_downgrade）
4. 机制③：量价确认 + 波动突变（_check_volume_confirm / _check_volatility_regime）
5. 三条件投票（_bb_touch_votes / _rsi_extreme_votes / _reversal_pattern_votes / _vote_ranging_direction）
6. 震荡入场编排（_check_ranging_entry）
7. 市场状态辅助函数（_pct_change 等 / get_market_state / get_market_state_simple）
8. 指标计算新增字段（calculate_all 的 EMA21 / ATR_long）

对应验收标准：AC-1.1~1.4、AC-2.1~2.7、AC-3.1~3.5、AC-4.1~4.4、AC-5.1~5.4，
以及 PRD 6.1 / 7.2 兜底矩阵（NaN / 缺失 / 空 / 极值 / 异常路径）。
"""
import copy
from datetime import datetime, timedelta
from typing import Dict, Optional

import numpy as np
import pandas as pd
import pytest

from strategies.btc_eth.strategy import BTCEthStrategy, _safe_last
from strategies.btc_eth.market_state import (
    MarketState,
    _pct_change,
    _signed_daily_slope,
    _signed_price_change,
)
from strategies.btc_eth.market_state import get_market_state, get_market_state_simple
from shared.indicators import TechnicalIndicators


# ============================================================
# 测试夹具：配置、指标、K线、策略实例
# ============================================================

def make_ranging_config() -> Dict:
    """构造与 config.yaml 结构一致的震荡策略配置（含三机制完整字段）。"""
    return {
        'enabled': True,
        'entry_conditions': {
            'bb_touch': True,
            'bb_touch_threshold': 0.05,
            'rsi_extreme': True,
            'rsi_oversold': 20,
            'rsi_overbought': 80,
            'reversal_pattern': True,
        },
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
        'volume_confirm': {
            'enabled': True,
            'shrink_ratio': 1.0,
        },
        'volatility_regime': {
            'enabled': True,
            'atr_long_period': 50,
            'spike_ratio': 2.0,
            'pause_bars': 12,
            'pause_interval_hours': 4,
        },
    }


def make_indicators(
        ema21_1d: float = 100.0,
        ema55_1d: float = 100.0,
        rsi_4h: float = 50.0,
        atr_4h: float = 2.0,
        atr_long_4h: float = 2.0,
        bb_upper: float = 110.0,
        bb_middle: float = 100.0,
        bb_lower: float = 90.0,
        volume_ma: float = 1000.0,
) -> Dict:
    """构造多时间框架指标字典，默认处于「不触发任何机制」的中性状态。"""
    return {
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
            'BB_Upper': pd.Series([bb_upper]),
            'BB_Middle': pd.Series([bb_middle]),
            'BB_Lower': pd.Series([bb_lower]),
            'Volume_MA': pd.Series([volume_ma]),
        },
    }


def make_klines(
        close_1d: float = 100.0,
        close_4h: float = 100.0,
        volume: float = 500.0,
        prev_open: float = 98.0,
        prev_close: float = 99.0,
        curr_open: float = 98.0,
) -> Dict:
    """构造 K 线字典，默认两根 4h K线（无反转形态）+ 两根 1d K线。"""
    return {
        '4h': [
            {'open': prev_open, 'high': 101.0, 'low': 97.0, 'close': prev_close, 'volume': 800.0},
            {'open': curr_open, 'high': 102.0, 'low': 97.0, 'close': close_4h, 'volume': volume},
        ],
        '1d': [
            {'open': 99.0, 'close': 98.0},
            {'open': 98.0, 'close': close_1d},
        ],
    }


def make_strategy(ranging_config: Optional[Dict] = None) -> BTCEthStrategy:
    """构造绕过 __init__ 的策略实例，手动注入被测方法所需属性。"""
    if ranging_config is None:
        ranging_config = make_ranging_config()
    strategy = object.__new__(BTCEthStrategy)
    strategy.risk_config = {'ranging_strategy': copy.deepcopy(ranging_config)}
    strategy.symbol_extreme_pause_until = {}
    return strategy


# ============================================================
# 1. _safe_last 统一兜底读取
# ============================================================

class TestSafeLast:
    """测试 _safe_last 对缺失 / 空 / NaN / None 的统一兜底。"""

    def test_returns_latest_value(self):
        indicators = {'1d': {'EMA21': pd.Series([1.0, 2.0, 3.0])}}
        assert _safe_last(indicators, '1d', 'EMA21') == 3.0

    def test_missing_timeframe_returns_none(self):
        assert _safe_last({}, '1d', 'EMA21') is None

    def test_timeframe_not_dict_returns_none(self):
        assert _safe_last({'1d': pd.Series([1.0])}, '1d', 'EMA21') is None

    def test_missing_field_returns_none(self):
        assert _safe_last({'1d': {}}, '1d', 'EMA21') is None

    def test_empty_series_returns_none(self):
        indicators = {'1d': {'EMA21': pd.Series([], dtype=float)}}
        assert _safe_last(indicators, '1d', 'EMA21') is None

    def test_nan_value_returns_none(self):
        indicators = {'1d': {'EMA21': pd.Series([np.nan])}}
        assert _safe_last(indicators, '1d', 'EMA21') is None

    def test_none_value_returns_none(self):
        indicators = {'1d': {'EMA21': pd.Series([None])}}
        assert _safe_last(indicators, '1d', 'EMA21') is None


# ============================================================
# 2. 机制①：方向一致性对齐（AC-1）
# ============================================================

class TestDirectionAlignment:
    """测试方向一致性机制（AC-1.1 ~ AC-1.4）。"""

    @staticmethod
    def _check(direction, indicators, ranging_config=None):
        if ranging_config is None:
            ranging_config = make_ranging_config()
        return BTCEthStrategy._check_direction_alignment(direction, indicators, ranging_config)

    def test_bullish_alignment_blocks_short(self):
        """AC-1.1：多头排列禁止做空。"""
        indicators = make_indicators(ema21_1d=110.0, ema55_1d=100.0)
        ok, reason = self._check('SHORT', indicators)
        assert ok is False
        assert '多头排列禁止做空' in reason

    def test_bearish_alignment_blocks_long(self):
        """AC-1.2：空头排列禁止做多。"""
        indicators = make_indicators(ema21_1d=100.0, ema55_1d=110.0)
        ok, reason = self._check('LONG', indicators)
        assert ok is False
        assert '空头排列禁止做多' in reason

    def test_bullish_alignment_allows_long(self):
        """AC-1.3：多头排列 + LONG 放行。"""
        indicators = make_indicators(ema21_1d=110.0, ema55_1d=100.0)
        ok, _ = self._check('LONG', indicators)
        assert ok is True

    def test_bearish_alignment_allows_short(self):
        """AC-1.3：空头排列 + SHORT 放行。"""
        indicators = make_indicators(ema21_1d=100.0, ema55_1d=110.0)
        ok, _ = self._check('SHORT', indicators)
        assert ok is True

    def test_missing_ma_rejects_by_default(self):
        """AC-1.4：均线缺失，fallback_on_missing=false 时保守拒绝。"""
        indicators = make_indicators()
        del indicators['1d']['EMA55']
        ok, reason = self._check('LONG', indicators)
        assert ok is False
        assert '均线数据缺失' in reason

    def test_nan_ma_rejects(self):
        """AC-1.4：均线 NaN 时保守拒绝。"""
        indicators = make_indicators()
        indicators['1d']['EMA21'] = pd.Series([np.nan])
        ok, _ = self._check('LONG', indicators)
        assert ok is False

    def test_missing_ma_fallback_allows(self):
        """AC-1.4：fallback_on_missing=true 时缺失放行。"""
        cfg = make_ranging_config()
        cfg['direction_alignment']['fallback_on_missing'] = True
        indicators = make_indicators()
        del indicators['1d']['EMA55']
        ok, _ = self._check('LONG', indicators, cfg)
        assert ok is True

    def test_stick_rejects(self):
        """均线粘合（差值比例 <= stick_threshold_pct）保守拒绝。"""
        indicators = make_indicators(ema21_1d=100.1, ema55_1d=100.0)
        ok, reason = self._check('LONG', indicators)
        assert ok is False
        assert '粘合' in reason

    def test_slow_ma_zero_treated_as_missing(self):
        """slow_ma == 0 走缺失分支，保守拒绝。"""
        indicators = make_indicators(ema21_1d=110.0, ema55_1d=0.0)
        ok, reason = self._check('LONG', indicators)
        assert ok is False
        assert '均线数据缺失' in reason

    def test_disabled_allows_any_direction(self):
        """AC-5.4：enabled=false 时退化为放行。"""
        cfg = make_ranging_config()
        cfg['direction_alignment']['enabled'] = False
        indicators = make_indicators(ema21_1d=110.0, ema55_1d=100.0)
        ok, _ = self._check('SHORT', indicators, cfg)
        assert ok is True

    def test_stick_threshold_from_config(self):
        """AC-1.5 / AC-5.1：粘合阈值来自配置，无硬编码。"""
        cfg = make_ranging_config()
        cfg['direction_alignment']['stick_threshold_pct'] = 0.001
        # 差值 0.1% > 0.001%，不再视为粘合，转为多头排列，LONG 放行
        indicators = make_indicators(ema21_1d=100.1, ema55_1d=100.0)
        ok, _ = self._check('LONG', indicators, cfg)
        assert ok is True

    def test_field_names_from_config(self):
        """AC-1.5：均线字段名来自配置（fast_ma/slow_ma），非硬编码。"""
        cfg = make_ranging_config()
        cfg['direction_alignment']['fast_ma'] = 'MA21'
        # indicators 中只有 EMA21，无 MA21 → fast 缺失 → 拒绝
        indicators = make_indicators(ema21_1d=110.0, ema55_1d=100.0)
        ok, reason = self._check('LONG', indicators, cfg)
        assert ok is False
        assert '均线数据缺失' in reason


# ============================================================
# 3. 机制②：过热评估（AC-2）
# ============================================================

class TestEvaluateOverheat:
    """测试过热档位评估（AC-2.1 ~ AC-2.7）。"""

    @staticmethod
    def _evaluate(direction, indicators=None, klines=None, overheat_cfg=None):
        if indicators is None:
            indicators = make_indicators()
        if klines is None:
            klines = make_klines()
        if overheat_cfg is None:
            overheat_cfg = make_ranging_config()['overheat_protection']
        return BTCEthStrategy._evaluate_overheat(direction, indicators, klines, overheat_cfg)

    def test_normal_returns_ok(self):
        assert self._evaluate('LONG') == 'ok'

    def test_empty_cfg_returns_ok(self):
        assert self._evaluate('LONG', overheat_cfg={}) == 'ok'

    # ---- AC-2.1 乖离率超禁开阈值 ----

    def test_bias_fast_over_ban_long(self):
        """AC-2.1：做多，快线乖离率 >= 6% → ban。"""
        klines = make_klines(close_1d=107.0)
        assert self._evaluate('LONG', klines=klines) == 'ban'

    def test_bias_slow_over_ban_long(self):
        """AC-2.1：做多，慢线乖离率 >= 9% 而快线未达标 → ban。"""
        indicators = make_indicators(ema21_1d=104.0, ema55_1d=100.0)
        klines = make_klines(close_1d=109.0)  # bias_fast=4.8%, bias_slow=9.0%
        assert self._evaluate('LONG', indicators=indicators, klines=klines) == 'ban'

    def test_bias_over_ban_short(self):
        """AC-2.1：做空，向下乖离率 <= -6% → ban。"""
        klines = make_klines(close_1d=93.0)
        assert self._evaluate('SHORT', klines=klines) == 'ban'

    # ---- AC-2.2 乖离率进入降级区间 ----

    def test_bias_downgrade_long(self):
        """AC-2.2：做多，快线乖离率 4% <= bias < 6% → downgrade。"""
        klines = make_klines(close_1d=104.5)
        assert self._evaluate('LONG', klines=klines) == 'downgrade'

    def test_bias_downgrade_short(self):
        """AC-2.2：做空，向下乖离率超过降级阈值但未达禁开 → downgrade。"""
        klines = make_klines(close_1d=95.5)
        assert self._evaluate('SHORT', klines=klines) == 'downgrade'

    # ---- AC-2.3 / AC-2.4 RSI 极值与预警 ----

    def test_rsi_extreme_ban_long(self):
        """AC-2.3：做多 RSI > 85 → ban。"""
        indicators = make_indicators(rsi_4h=90.0)
        assert self._evaluate('LONG', indicators=indicators) == 'ban'

    def test_rsi_extreme_ban_short(self):
        """AC-2.3：做空 RSI < 15 → ban。"""
        indicators = make_indicators(rsi_4h=10.0)
        assert self._evaluate('SHORT', indicators=indicators) == 'ban'

    def test_rsi_warn_downgrade_long(self):
        """AC-2.4：做多 75 < RSI <= 85 → downgrade。"""
        indicators = make_indicators(rsi_4h=80.0)
        assert self._evaluate('LONG', indicators=indicators) == 'downgrade'

    def test_rsi_warn_downgrade_short(self):
        """AC-2.4：做空 15 <= RSI < 25 → downgrade。"""
        indicators = make_indicators(rsi_4h=20.0)
        assert self._evaluate('SHORT', indicators=indicators) == 'downgrade'

    def test_rsi_boundary_85_is_downgrade_not_ban(self):
        """RSI 边界：85 恰好触发预警降级，而非禁开。"""
        indicators = make_indicators(rsi_4h=85.0)
        assert self._evaluate('LONG', indicators=indicators) == 'downgrade'

    def test_rsi_boundary_75_is_ok(self):
        """RSI 边界：75 不触发预警。"""
        indicators = make_indicators(rsi_4h=75.0)
        assert self._evaluate('LONG', indicators=indicators) == 'ok'

    def test_rsi_boundary_15_is_downgrade_not_ban(self):
        """RSI 边界：做空 15 恰好触发预警降级。"""
        indicators = make_indicators(rsi_4h=15.0)
        assert self._evaluate('SHORT', indicators=indicators) == 'downgrade'

    def test_rsi_boundary_25_is_ok(self):
        """RSI 边界：做空 25 不触发预警。"""
        indicators = make_indicators(rsi_4h=25.0)
        assert self._evaluate('SHORT', indicators=indicators) == 'ok'

    # ---- AC-2.5 方向敏感性（无 abs） ----

    def test_long_ignores_downward_bias(self):
        """AC-2.5：做多只看向上过热，向下乖离不触发。"""
        klines = make_klines(close_1d=50.0)  # 向下偏离 50%
        assert self._evaluate('LONG', klines=klines) == 'ok'

    def test_short_ignores_upward_bias(self):
        """AC-2.5：做空只看向下过热，向上乖离不触发。"""
        klines = make_klines(close_1d=150.0)  # 向上偏离 50%
        assert self._evaluate('SHORT', klines=klines) == 'ok'

    # ---- AC-2.6 downgrade_enabled=false ----

    def test_downgrade_enabled_false_turns_downgrade_into_ban(self):
        """AC-2.6：downgrade_enabled=false 时预警区间也禁开。"""
        overheat_cfg = make_ranging_config()['overheat_protection']
        overheat_cfg['downgrade_enabled'] = False
        klines = make_klines(close_1d=104.5)  # 原本 downgrade
        assert self._evaluate('LONG', klines=klines, overheat_cfg=overheat_cfg) == 'ban'

    # ---- AC-2.7 阈值来自配置 ----

    def test_bias_ban_threshold_from_config(self):
        """AC-2.7 / AC-5.1：禁开阈值来自配置。"""
        overheat_cfg = make_ranging_config()['overheat_protection']
        overheat_cfg['bias_fast_ban_pct'] = 3.0
        klines = make_klines(close_1d=104.0)  # bias=4%，默认 6% 不 ban，但配置 3% 则 ban
        assert self._evaluate('LONG', klines=klines, overheat_cfg=overheat_cfg) == 'ban'

    def test_rsi_extreme_threshold_from_config(self):
        """AC-2.7 / AC-5.1：RSI 极值阈值来自配置。"""
        overheat_cfg = make_ranging_config()['overheat_protection']
        overheat_cfg['rsi_extreme_long'] = 70
        indicators = make_indicators(rsi_4h=80.0)
        assert self._evaluate('LONG', indicators=indicators, overheat_cfg=overheat_cfg) == 'ban'

    # ---- 边界：数据缺失 ----

    def test_missing_1d_klines_returns_ok(self):
        klines = {'4h': make_klines()['4h']}
        assert self._evaluate('LONG', klines=klines) == 'ok'

    def test_empty_1d_klines_returns_ok(self):
        klines = make_klines()
        klines['1d'] = []
        assert self._evaluate('LONG', klines=klines) == 'ok'

    def test_1d_without_close_returns_ok(self):
        klines = make_klines()
        klines['1d'] = [{'open': 99.0}, {'open': 98.0}]
        assert self._evaluate('LONG', klines=klines) == 'ok'

    def test_nan_close_1d_returns_ok(self):
        klines = make_klines(close_1d=np.nan)
        assert self._evaluate('LONG', klines=klines) == 'ok'

    def test_missing_indicators_returns_ok(self):
        indicators = {'1d': {}, '4h': {}}
        assert self._evaluate('LONG', indicators=indicators) == 'ok'


# ============================================================
# 4. 机制②：过热禁开（AC-2.1）
# ============================================================

class TestCheckOverheatBan:
    """测试重度过热禁开入口。"""

    def test_ban_returns_false(self):
        """AC-2.1：极值区禁开。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        klines = make_klines(close_1d=107.0)
        ok, reason = strategy._check_overheat_ban('LONG', make_indicators(), klines, rcfg)
        assert ok is False
        assert '过热禁开' in reason

    def test_normal_returns_true(self):
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        ok, _ = strategy._check_overheat_ban('LONG', make_indicators(), make_klines(), rcfg)
        assert ok is True

    def test_disabled_returns_true(self):
        """AC-5.4：enabled=false 时放行。"""
        rcfg = make_ranging_config()
        rcfg['overheat_protection']['enabled'] = False
        strategy = make_strategy(rcfg)
        ok, _ = strategy._check_overheat_ban('LONG', make_indicators(), make_klines(close_1d=107.0), rcfg)
        assert ok is True

    def test_downgrade_not_banned(self):
        """轻度过热（downgrade）不应被禁开拦截，只降级。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        klines = make_klines(close_1d=104.5)
        ok, _ = strategy._check_overheat_ban('LONG', make_indicators(), klines, rcfg)
        assert ok is True


# ============================================================
# 5. 机制②：过热降级（AC-2.2）
# ============================================================

class TestApplyOverheatDowngrade:
    """测试轻度过热降级阶梯。"""

    def test_grade_s_downgrade_to_a(self):
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        klines = make_klines(close_1d=104.5)
        assert strategy._apply_overheat_downgrade('S', 'LONG', make_indicators(), klines, rcfg) == 'A'

    def test_grade_ladder_full_descending(self):
        """S→A→B→C，C 再降返回 None。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        klines = make_klines(close_1d=104.5)
        indicators = make_indicators()
        assert strategy._apply_overheat_downgrade('A', 'LONG', indicators, klines, rcfg) == 'B'
        assert strategy._apply_overheat_downgrade('B', 'LONG', indicators, klines, rcfg) == 'C'
        assert strategy._apply_overheat_downgrade('C', 'LONG', indicators, klines, rcfg) is None

    def test_no_downgrade_when_ok(self):
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        assert strategy._apply_overheat_downgrade('S', 'LONG', make_indicators(), make_klines(), rcfg) == 'S'

    def test_no_downgrade_when_ban(self):
        """重度过热（ban）走禁开路径，此处不降级。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        klines = make_klines(close_1d=107.0)
        assert strategy._apply_overheat_downgrade('S', 'LONG', make_indicators(), klines, rcfg) == 'S'

    def test_unknown_grade_returns_none(self):
        """不在降级阶梯中的 grade，降级时返回 None。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        klines = make_klines(close_1d=104.5)
        assert strategy._apply_overheat_downgrade('X', 'LONG', make_indicators(), klines, rcfg) is None

    def test_disabled_returns_original_grade(self):
        """AC-5.4：enabled=false 时不降级。"""
        rcfg = make_ranging_config()
        rcfg['overheat_protection']['enabled'] = False
        strategy = make_strategy(rcfg)
        klines = make_klines(close_1d=104.5)
        assert strategy._apply_overheat_downgrade('S', 'LONG', make_indicators(), klines, rcfg) == 'S'

    def test_downgrade_enabled_false_returns_original_grade(self):
        """AC-2.6：downgrade_enabled=false 时预警转禁开，此处不降级。"""
        rcfg = make_ranging_config()
        rcfg['overheat_protection']['downgrade_enabled'] = False
        strategy = make_strategy(rcfg)
        klines = make_klines(close_1d=104.5)
        assert strategy._apply_overheat_downgrade('S', 'LONG', make_indicators(), klines, rcfg) == 'S'


# ============================================================
# 6. 机制③：量价确认（AC-3）
# ============================================================

class TestVolumeConfirm:
    """测试量价确认（AC-3.1 ~ AC-3.5）。"""

    @staticmethod
    def _check(direction, indicators=None, klines=None, ranging_config=None):
        if indicators is None:
            indicators = make_indicators()
        if klines is None:
            klines = make_klines()
        if ranging_config is None:
            ranging_config = make_ranging_config()
        return BTCEthStrategy._check_volume_confirm(direction, indicators, klines, ranging_config)

    def test_short_not_broken_middle_rejects(self):
        """AC-3.1：做空未跌破中轨 → 拒绝。"""
        klines = make_klines(close_4h=105.0)  # 105 >= 100
        ok, reason = self._check('SHORT', klines=klines)
        assert ok is False
        assert '跌破' in reason

    def test_short_not_shrink_rejects(self):
        """AC-3.2：做空已跌破中轨但未缩量 → 拒绝。"""
        klines = make_klines(close_4h=95.0, volume=2000.0)  # volume >= 1000
        ok, reason = self._check('SHORT', klines=klines)
        assert ok is False
        assert '缩量' in reason

    def test_short_pass(self):
        """AC-3.3：同时满足跌破中轨 + 缩量 → 通过。"""
        klines = make_klines(close_4h=95.0, volume=500.0)
        ok, _ = self._check('SHORT', klines=klines)
        assert ok is True

    def test_long_pass(self):
        """AC-3.4：做多对称，站上中轨 + 缩量 → 通过。"""
        klines = make_klines(close_4h=105.0, volume=500.0)
        ok, _ = self._check('LONG', klines=klines)
        assert ok is True

    def test_long_not_above_middle_rejects(self):
        """AC-3.4：做多未站上中轨 → 拒绝。"""
        klines = make_klines(close_4h=95.0, volume=500.0)
        ok, reason = self._check('LONG', klines=klines)
        assert ok is False
        assert '站上' in reason

    def test_long_not_shrink_rejects(self):
        """AC-3.4：做多已站上中轨但未缩量 → 拒绝。"""
        klines = make_klines(close_4h=105.0, volume=2000.0)
        ok, reason = self._check('LONG', klines=klines)
        assert ok is False
        assert '缩量' in reason

    # ---- AC-3.5 数据缺失保守拒绝 ----

    def test_missing_volume_column_rejects(self):
        klines = {'4h': [{'close': 95.0}]}
        ok, reason = self._check('SHORT', klines=klines)
        assert ok is False
        assert '量价数据缺失' in reason

    def test_missing_close_column_rejects(self):
        klines = {'4h': [{'volume': 500.0}]}
        ok, reason = self._check('SHORT', klines=klines)
        assert ok is False
        assert '量价数据缺失' in reason

    def test_missing_bb_middle_rejects(self):
        indicators = make_indicators()
        del indicators['4h']['BB_Middle']
        ok, reason = self._check('SHORT', indicators=indicators)
        assert ok is False
        assert '量价数据缺失' in reason

    def test_missing_volume_ma_rejects(self):
        indicators = make_indicators()
        del indicators['4h']['Volume_MA']
        ok, reason = self._check('SHORT', indicators=indicators)
        assert ok is False
        assert '量价数据缺失' in reason

    def test_nan_volume_rejects(self):
        klines = make_klines()
        klines['4h'][-1]['volume'] = None
        ok, reason = self._check('SHORT', klines=klines)
        assert ok is False
        assert '量价数据缺失' in reason

    def test_nan_close_rejects(self):
        klines = make_klines()
        klines['4h'][-1]['close'] = None
        ok, reason = self._check('SHORT', klines=klines)
        assert ok is False
        assert '量价数据缺失' in reason

    # ---- 配置化 / 开关 ----

    def test_shrink_ratio_from_config(self):
        """AC-5.1：shrink_ratio 来自配置。"""
        cfg = make_ranging_config()
        cfg['volume_confirm']['shrink_ratio'] = 0.5
        klines = make_klines(close_4h=95.0, volume=600.0)  # 600 < 1000 但 600 >= 500
        ok, reason = self._check('SHORT', klines=klines, ranging_config=cfg)
        assert ok is False
        assert '缩量' in reason

    def test_disabled_returns_true(self):
        """AC-5.4：enabled=false 时放行。"""
        cfg = make_ranging_config()
        cfg['volume_confirm']['enabled'] = False
        klines = make_klines(close_4h=105.0, volume=2000.0)  # 本应拒绝
        ok, _ = self._check('SHORT', klines=klines, ranging_config=cfg)
        assert ok is True


# ============================================================
# 7. 机制③：波动突变 / 极端期（AC-4）
# ============================================================

class TestVolatilityRegime:
    """测试波动突变 / 极端期暂停（AC-4.1 ~ AC-4.4）。"""

    def test_spike_marks_extreme_and_rejects(self):
        """AC-4.1：ATR 短长比超阈值时标记极端期并拒绝。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        indicators = make_indicators(atr_4h=10.0, atr_long_4h=4.0)  # ratio=2.5 > 2.0
        ok, reason = strategy._check_volatility_regime('BTCUSDT', indicators, rcfg)
        assert ok is False
        assert '极端期' in reason
        assert 'BTCUSDT' in strategy.symbol_extreme_pause_until

    def test_pause_window_rejects(self):
        """AC-4.2：暂停窗口内拒绝。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        strategy.symbol_extreme_pause_until['BTCUSDT'] = datetime.now() + timedelta(hours=1)
        indicators = make_indicators()  # 正常比值
        ok, reason = strategy._check_volatility_regime('BTCUSDT', indicators, rcfg)
        assert ok is False
        assert '极端期' in reason

    def test_pause_window_ended_restores(self):
        """AC-4.3：暂停窗口结束后恢复。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        strategy.symbol_extreme_pause_until['BTCUSDT'] = datetime.now() - timedelta(hours=1)
        indicators = make_indicators()
        ok, _ = strategy._check_volatility_regime('BTCUSDT', indicators, rcfg)
        assert ok is True

    def test_state_persists_across_calls(self):
        """AC-4.4：状态跨调用保持，第二次仍拒绝。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        indicators = make_indicators(atr_4h=10.0, atr_long_4h=4.0)
        ok1, _ = strategy._check_volatility_regime('BTCUSDT', indicators, rcfg)
        assert ok1 is False
        ok2, reason = strategy._check_volatility_regime('BTCUSDT', indicators, rcfg)
        assert ok2 is False
        assert '极端期' in reason

    def test_missing_atr_skips_detection(self):
        """数据缺失：无 ATR 时跳过检测，不抛异常。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        indicators = make_indicators()
        del indicators['4h']['ATR']
        ok, _ = strategy._check_volatility_regime('BTCUSDT', indicators, rcfg)
        assert ok is True

    def test_missing_atr_long_skips_detection(self):
        """数据缺失：无 ATR_long 时跳过检测，不抛异常。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        indicators = make_indicators()
        del indicators['4h']['ATR_long']
        ok, _ = strategy._check_volatility_regime('BTCUSDT', indicators, rcfg)
        assert ok is True

    def test_atr_long_zero_skips_detection(self):
        """ATR_long == 0 时跳过检测（避免除零）。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        indicators = make_indicators(atr_4h=10.0, atr_long_4h=0.0)
        ok, _ = strategy._check_volatility_regime('BTCUSDT', indicators, rcfg)
        assert ok is True

    def test_disabled_returns_true(self):
        """AC-5.4：enabled=false 时放行。"""
        rcfg = make_ranging_config()
        rcfg['volatility_regime']['enabled'] = False
        strategy = make_strategy(rcfg)
        indicators = make_indicators(atr_4h=10.0, atr_long_4h=4.0)
        ok, _ = strategy._check_volatility_regime('BTCUSDT', indicators, rcfg)
        assert ok is True

    def test_spike_ratio_from_config(self):
        """AC-5.1：spike_ratio 来自配置。"""
        rcfg = make_ranging_config()
        rcfg['volatility_regime']['spike_ratio'] = 3.0
        strategy = make_strategy(rcfg)
        indicators = make_indicators(atr_4h=10.0, atr_long_4h=4.0)  # ratio=2.5 < 3.0
        ok, _ = strategy._check_volatility_regime('BTCUSDT', indicators, rcfg)
        assert ok is True


# ============================================================
# 8. 三条件投票（_bb_touch_votes / _rsi_extreme_votes / _reversal_pattern_votes / _vote_ranging_direction）
# ============================================================

class TestBBTouchVotes:
    """测试 BB 触轨投票。"""

    def test_disabled_returns_no_votes(self):
        ec = make_ranging_config()['entry_conditions'].copy()
        ec['bb_touch'] = False
        df = pd.DataFrame({'close': [95.0]})
        result = BTCEthStrategy._bb_touch_votes(df, make_indicators(), ec)
        assert result == (0, 0, [], 1.0, 1.0)

    def test_touch_lower_gives_long_vote(self):
        df = pd.DataFrame({'close': [91.0]})
        indicators = make_indicators(bb_upper=110.0, bb_lower=90.0)
        ld, sd, conds, _, _ = BTCEthStrategy._bb_touch_votes(
            df, indicators, make_ranging_config()['entry_conditions'])
        assert ld == 1
        assert sd == 0
        assert any('下轨' in c for c in conds)

    def test_touch_upper_gives_short_vote(self):
        df = pd.DataFrame({'close': [109.0]})
        indicators = make_indicators(bb_upper=110.0, bb_lower=90.0)
        ld, sd, conds, _, _ = BTCEthStrategy._bb_touch_votes(
            df, indicators, make_ranging_config()['entry_conditions'])
        assert ld == 0
        assert sd == 1
        assert any('上轨' in c for c in conds)

    def test_no_touch_returns_no_votes(self):
        df = pd.DataFrame({'close': [100.0]})
        indicators = make_indicators()
        ld, sd, conds, _, _ = BTCEthStrategy._bb_touch_votes(
            df, indicators, make_ranging_config()['entry_conditions'])
        assert ld == 0
        assert sd == 0
        assert conds == []

    def test_missing_bb_returns_no_votes(self):
        df = pd.DataFrame({'close': [95.0]})
        indicators = make_indicators()
        del indicators['4h']['BB_Upper']
        ld, sd, conds, _, _ = BTCEthStrategy._bb_touch_votes(
            df, indicators, make_ranging_config()['entry_conditions'])
        assert (ld, sd, conds) == (0, 0, [])

    def test_empty_df_returns_no_votes(self):
        df = pd.DataFrame()
        ld, sd, conds, _, _ = BTCEthStrategy._bb_touch_votes(
            df, make_indicators(), make_ranging_config()['entry_conditions'])
        assert (ld, sd, conds) == (0, 0, [])


class TestRSIExtremeVotes:
    """测试 RSI 极端投票。"""

    def test_disabled_returns_no_votes(self):
        ec = make_ranging_config()['entry_conditions'].copy()
        ec['rsi_extreme'] = False
        assert BTCEthStrategy._rsi_extreme_votes(make_indicators(rsi_4h=10.0), ec) == (0, 0, [], 0.0)

    def test_oversold_gives_long_vote(self):
        ld, sd, conds, rsi = BTCEthStrategy._rsi_extreme_votes(
            make_indicators(rsi_4h=10.0), make_ranging_config()['entry_conditions'])
        assert ld == 1
        assert sd == 0
        assert rsi == 10.0

    def test_overbought_gives_short_vote(self):
        ld, sd, conds, rsi = BTCEthStrategy._rsi_extreme_votes(
            make_indicators(rsi_4h=90.0), make_ranging_config()['entry_conditions'])
        assert ld == 0
        assert sd == 1
        assert any('超买' in c for c in conds)

    def test_neutral_returns_no_votes(self):
        ld, sd, conds, _ = BTCEthStrategy._rsi_extreme_votes(
            make_indicators(rsi_4h=50.0), make_ranging_config()['entry_conditions'])
        assert (ld, sd, conds) == (0, 0, [])

    def test_missing_rsi_returns_no_votes(self):
        indicators = make_indicators()
        del indicators['4h']['RSI']
        ld, sd, conds, rsi = BTCEthStrategy._rsi_extreme_votes(
            indicators, make_ranging_config()['entry_conditions'])
        assert (ld, sd, conds, rsi) == (0, 0, [], 0.0)


class TestReversalPatternVotes:
    """测试反转 K 线形态投票。"""

    def test_disabled_returns_no_votes(self):
        ec = make_ranging_config()['entry_conditions'].copy()
        ec['reversal_pattern'] = False
        assert BTCEthStrategy._reversal_pattern_votes(pd.DataFrame({'open': [1.0], 'close': [2.0]}), ec) == (0, 0, [])

    def test_bullish_engulfing_gives_long_vote(self):
        df = pd.DataFrame({
            'open': [100.0, 85.0],
            'close': [90.0, 105.0],
        })
        ld, sd, conds = BTCEthStrategy._reversal_pattern_votes(
            df, make_ranging_config()['entry_conditions'])
        assert ld == 1
        assert sd == 0
        assert '看涨吞没' in conds

    def test_bearish_engulfing_gives_short_vote(self):
        df = pd.DataFrame({
            'open': [90.0, 105.0],
            'close': [100.0, 85.0],
        })
        ld, sd, conds = BTCEthStrategy._reversal_pattern_votes(
            df, make_ranging_config()['entry_conditions'])
        assert ld == 0
        assert sd == 1
        assert '看跌吞没' in conds

    def test_no_pattern_returns_no_votes(self):
        df = pd.DataFrame({
            'open': [100.0, 101.0],
            'close': [101.0, 102.0],
        })
        ld, sd, conds = BTCEthStrategy._reversal_pattern_votes(
            df, make_ranging_config()['entry_conditions'])
        assert (ld, sd, conds) == (0, 0, [])

    def test_short_df_returns_no_votes(self):
        df = pd.DataFrame({'open': [100.0], 'close': [101.0]})
        assert BTCEthStrategy._reversal_pattern_votes(df, make_ranging_config()['entry_conditions']) == (0, 0, [])


class TestVoteRangingDirection:
    """测试三条件投票得出候选方向。"""

    def test_long_wins(self):
        strategy = make_strategy()
        indicators = make_indicators(rsi_4h=10.0)  # 超卖 → 多1
        klines = make_klines(prev_open=100.0, prev_close=90.0, curr_open=85.0, close_4h=105.0)  # 看涨吞没 → 多1
        direction, reason, conditions = strategy._vote_ranging_direction(
            indicators, klines, make_ranging_config()['entry_conditions'])
        assert direction == 'LONG'
        assert reason == ''
        assert len(conditions) >= 2

    def test_short_wins(self):
        strategy = make_strategy()
        indicators = make_indicators(rsi_4h=90.0)  # 超买 → 空1
        klines = make_klines(prev_open=90.0, prev_close=100.0, curr_open=105.0, close_4h=85.0)  # 看跌吞没 → 空1
        direction, _, _ = strategy._vote_ranging_direction(
            indicators, klines, make_ranging_config()['entry_conditions'])
        assert direction == 'SHORT'

    def test_no_votes_returns_none(self):
        strategy = make_strategy()
        indicators = make_indicators(rsi_4h=50.0)
        klines = make_klines()  # 无反转形态
        direction, reason, _ = strategy._vote_ranging_direction(
            indicators, klines, make_ranging_config()['entry_conditions'])
        assert direction is None
        assert '无震荡入场条件' in reason

    def test_tie_returns_none(self):
        strategy = make_strategy()
        # 放宽布林带，避免 close=85 触下轨产生多余的多票，保证 RSI 超卖(多1) 与看跌吞没(空1) 平票
        indicators = make_indicators(rsi_4h=10.0, bb_upper=120.0, bb_lower=80.0)
        klines = make_klines(prev_open=90.0, prev_close=100.0, curr_open=105.0, close_4h=85.0)  # 看跌吞没 → 空1
        direction, reason, _ = strategy._vote_ranging_direction(
            indicators, klines, make_ranging_config()['entry_conditions'])
        assert direction is None
        assert '方向不一致' in reason


# ============================================================
# 9. 震荡入场编排（_check_ranging_entry）
# ============================================================

class TestCheckRangingEntry:
    """测试震荡入场四机制编排（顺序、短路、异常兜底）。"""

    def test_full_long_pass(self):
        """端到端：LONG 场景通过全部机制 → (True, LONG)。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        indicators = make_indicators(ema21_1d=101.0, ema55_1d=100.0, rsi_4h=10.0)
        klines = make_klines(
            close_1d=100.0,
            prev_open=100.0, prev_close=90.0, curr_open=85.0, close_4h=105.0, volume=500.0,
        )
        ok, direction = strategy._check_ranging_entry('BTCUSDT', indicators, klines)
        assert ok is True
        assert direction == 'LONG'

    def test_bullish_alignment_blocks_short_entry(self):
        """AC-1.1 集成：多头排列做空被 _check_ranging_entry 拒绝。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        indicators = make_indicators(ema21_1d=110.0, ema55_1d=100.0, rsi_4h=90.0)
        klines = make_klines(prev_open=90.0, prev_close=100.0, curr_open=105.0, close_4h=85.0)
        ok, reason = strategy._check_ranging_entry('BTCUSDT', indicators, klines)
        assert ok is False
        assert '多头排列禁止做空' in reason

    def test_disabled_strategy_rejects(self):
        rcfg = make_ranging_config()
        rcfg['enabled'] = False
        strategy = make_strategy(rcfg)
        ok, reason = strategy._check_ranging_entry('BTCUSDT', make_indicators(), make_klines())
        assert ok is False
        assert '震荡市策略未启用' in reason

    def test_missing_4h_data_rejects(self):
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        ok, reason = strategy._check_ranging_entry('BTCUSDT', {'1d': {}}, {'1d': []})
        assert ok is False
        assert '4h数据缺失' in reason

    def test_no_vote_rejects(self):
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        ok, reason = strategy._check_ranging_entry(
            'BTCUSDT', make_indicators(rsi_4h=50.0), make_klines())
        assert ok is False
        assert '无震荡入场条件' in reason

    def test_malformed_indicator_returns_exception_reason(self):
        """异常兜底：指标字段不是 Series 时被捕获，返回异常原因而非抛出。"""
        rcfg = make_ranging_config()
        strategy = make_strategy(rcfg)
        indicators = make_indicators(rsi_4h=10.0)
        indicators['4h']['RSI'] = 'not_a_series'  # 触发 _safe_last 的 iloc 异常
        ok, reason = strategy._check_ranging_entry('BTCUSDT', indicators, make_klines())
        assert ok is False
        assert '震荡入场检查异常' in reason


# ============================================================
# 10. 市场状态辅助函数（带符号百分比）
# ============================================================

class TestPctChange:
    """测试 _pct_change 带符号百分比与无效数据兜底。"""

    def test_positive_change(self):
        assert _pct_change(110.0, 100.0) == pytest.approx(10.0)

    def test_negative_change(self):
        assert _pct_change(90.0, 100.0) == pytest.approx(-10.0)

    def test_current_none(self):
        assert _pct_change(None, 100.0) is None

    def test_previous_none(self):
        assert _pct_change(110.0, None) is None

    def test_nan_inputs(self):
        assert _pct_change(np.nan, 100.0) is None
        assert _pct_change(110.0, np.nan) is None

    def test_non_positive_previous(self):
        assert _pct_change(110.0, 0.0) is None
        assert _pct_change(110.0, -5.0) is None


class TestSignedWrappers:
    """测试薄封装函数（_signed_price_change / _signed_daily_slope）。"""

    def test_signed_price_change_preserves_sign(self):
        assert _signed_price_change(110.0, 100.0) == pytest.approx(10.0)
        assert _signed_price_change(90.0, 100.0) == pytest.approx(-10.0)

    def test_signed_price_change_invalid(self):
        assert _signed_price_change(None, 100.0) is None

    def test_signed_daily_slope_preserves_sign(self):
        assert _signed_daily_slope(101.0, 100.0) == pytest.approx(1.0)
        assert _signed_daily_slope(99.0, 100.0) == pytest.approx(-1.0)

    def test_signed_daily_slope_invalid(self):
        assert _signed_daily_slope(None, 100.0) is None


# ============================================================
# 11. get_market_state / get_market_state_simple
# ============================================================

def make_strong_trend_inputs():
    """构造满足 5 个强趋势条件的输入。"""
    indicators_4h = {
        'ADX': pd.Series([30.0] * 9 + [40.0]),
        'BB_Upper': pd.Series([116.0] * 10),
        'BB_Middle': pd.Series([100.0] * 10),
        'BB_Lower': pd.Series([84.0] * 10),
        'MA21': pd.Series([99.0] * 10),
    }
    close_prices = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 110.0])
    indicators_1d = {'MA21': pd.Series([100.0, 100.2])}
    return indicators_4h, close_prices, indicators_1d


class TestGetMarketState:
    """测试生产版市场状态判定。"""

    def test_strong_trend(self):
        indicators_4h, close_prices, indicators_1d = make_strong_trend_inputs()
        state, desc = get_market_state(indicators_4h, close_prices, indicators_1d)
        assert state == MarketState.STRONG_TREND

    def test_adx_missing_ranging(self):
        indicators_4h, close_prices, indicators_1d = make_strong_trend_inputs()
        del indicators_4h['ADX']
        state, _ = get_market_state(indicators_4h, close_prices, indicators_1d)
        assert state == MarketState.RANGING

    def test_adx_below_threshold_ranging(self):
        indicators_4h, close_prices, indicators_1d = make_strong_trend_inputs()
        indicators_4h['ADX'] = pd.Series([30.0] * 10)
        state, _ = get_market_state(indicators_4h, close_prices, indicators_1d)
        assert state == MarketState.RANGING

    def test_bb_missing_ranging(self):
        indicators_4h, close_prices, indicators_1d = make_strong_trend_inputs()
        del indicators_4h['BB_Upper']
        state, _ = get_market_state(indicators_4h, close_prices, indicators_1d)
        assert state == MarketState.RANGING

    def test_bb_width_too_narrow_ranging(self):
        indicators_4h, close_prices, indicators_1d = make_strong_trend_inputs()
        indicators_4h['BB_Upper'] = pd.Series([103.5] * 10)
        indicators_4h['BB_Lower'] = pd.Series([96.5] * 10)  # width=7% 恰好 <= 阈值
        state, _ = get_market_state(indicators_4h, close_prices, indicators_1d)
        assert state == MarketState.RANGING

    def test_price_change_too_small_ranging(self):
        indicators_4h, close_prices, indicators_1d = make_strong_trend_inputs()
        close_prices = pd.Series([100.0] * 10)  # 变化 0%
        state, _ = get_market_state(indicators_4h, close_prices, indicators_1d)
        assert state == MarketState.RANGING

    def test_daily_slope_too_small_ranging(self):
        indicators_4h, close_prices, indicators_1d = make_strong_trend_inputs()
        indicators_1d = {'MA21': pd.Series([100.0, 100.0])}  # 斜率 0
        state, _ = get_market_state(indicators_4h, close_prices, indicators_1d)
        assert state == MarketState.RANGING

    def test_consecutive_fail_ranging(self):
        indicators_4h, close_prices, indicators_1d = make_strong_trend_inputs()
        indicators_4h['MA21'] = pd.Series([105.0, 105.0, 105.0, 105.0, 105.0,
                                           105.0, 105.0, 100.0, 111.0, 109.0])
        state, _ = get_market_state(indicators_4h, close_prices, indicators_1d)
        assert state == MarketState.RANGING


class TestGetMarketStateSimple:
    """测试回测版市场状态判定（AC-5.2 逻辑一致）。"""

    def _make_strong_df(self):
        return pd.DataFrame({
            'ADX': [30.0] * 9 + [40.0],
            'BB_Upper': [116.0] * 10,
            'BB_Middle': [100.0] * 10,
            'BB_Lower': [84.0] * 10,
            'MA21': [99.0] * 10,
            'close': [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 110.0],
        })

    def test_strong_trend(self):
        df_4h = self._make_strong_df()
        df_1d = pd.DataFrame({'MA21': [100.0, 100.2]})
        assert get_market_state_simple(df_4h, df_1d) == MarketState.STRONG_TREND

    def test_adx_below_ranging(self):
        df_4h = self._make_strong_df()
        df_4h['ADX'] = [30.0] * 10
        df_1d = pd.DataFrame({'MA21': [100.0, 100.2]})
        assert get_market_state_simple(df_4h, df_1d) == MarketState.RANGING

    def test_bb_narrow_ranging(self):
        df_4h = self._make_strong_df()
        df_4h['BB_Upper'] = [103.5] * 10
        df_4h['BB_Lower'] = [96.5] * 10
        df_1d = pd.DataFrame({'MA21': [100.0, 100.2]})
        assert get_market_state_simple(df_4h, df_1d) == MarketState.RANGING

    def test_missing_1d_ranging(self):
        df_4h = self._make_strong_df()
        assert get_market_state_simple(df_4h, None) == MarketState.RANGING


# ============================================================
# 12. calculate_all 新增字段（EMA21 / ATR_long）
# ============================================================

def make_ohlcv_data(n: int = 70) -> pd.DataFrame:
    """构造足够长度的 OHLCV 数据（覆盖 EMA55 / ATR_long(50) 的 warmup 期）。"""
    rng = np.random.default_rng(42)
    close = 100.0 + np.cumsum(rng.normal(0.0, 1.0, n))
    df = pd.DataFrame({
        'open': close,
        'high': close + 1.0,
        'low': close - 1.0,
        'close': close,
        'volume': rng.uniform(100.0, 200.0, n),
    })
    return df


class TestCalculateAllNewFields:
    """测试 calculate_all 新增 EMA21 / ATR_long 字段。"""

    def test_ema21_computed(self):
        result = TechnicalIndicators.calculate_all(make_ohlcv_data())
        assert 'EMA21' in result
        assert 'EMA55' in result
        assert not pd.isna(result['EMA21'].iloc[-1])
        assert not pd.isna(result['EMA55'].iloc[-1])

    def test_atr_long_computed_with_config_period(self):
        data = make_ohlcv_data()
        result = TechnicalIndicators.calculate_all(data, atr_long_period=50)
        assert 'ATR_long' in result
        assert 'ATR' in result
        assert len(result['ATR_long']) == len(data)
        assert not pd.isna(result['ATR_long'].iloc[-1])

    def test_atr_long_default_period(self):
        result = TechnicalIndicators.calculate_all(make_ohlcv_data())
        assert 'ATR_long' in result
        assert not pd.isna(result['ATR_long'].iloc[-1])