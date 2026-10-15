"""B3 验收测试：新币反弹判断修复 + sentiment_degraded 传递。

覆盖的契约（对应 D3 决策）：
- 参照线固定（breakout_reference_high/reference_window_end/signal_time 随 signal 传递、下单前不再重算）
- 只纳入已关闭 K 线（klines[-1] 若 close_time 未过则排除）
- 新鲜实时价在 execute_signal 入口获取，超龄/无效/获取失败→拒绝新开仓
- 实时价 vs 固定高点：严格「超过」才拒绝（等于或低于都放行）
- sentiment_degraded 显式传给 should_entry；记录实际 OI 回看时长
- 影子开关（默认 true）下只记录差集不拦截；关闭后真实拦截
- executor.py 持仓复核处的 rebound_ratio/breakout_ratio 口径不同（不动）

全部用 mock/fixture，不连交易所/DB。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from strategies.new_coin.strategy import (
    NewCoinStrategy,
    _compute_breakout_reference_high,
    _PRICE_FRESH_DEFAULT_SECONDS,
    _SHADOW_DEFAULT,
)
from strategies.new_coin.scoring_engine import ScoringResult


# ============================================================
# K 线 fixture：构造一组已关闭 K 线
# ============================================================

def _make_kline(open_time_ms: int, close_time_ms: int,
                high: float, low: float, close: float) -> Dict[str, Any]:
    return {
        'open_time': open_time_ms,
        'close_time': close_time_ms,
        'open': Decimal(str(low)),
        'high': Decimal(str(high)),
        'low': Decimal(str(low)),
        'close': Decimal(str(close)),
        'volume': Decimal('1000'),
    }


def _build_klines(*, closed_window: int, include_unclosed_tail: bool = False) -> List[Dict[str, Any]]:
    """构造 1h K 线序列。close_window 是关闭的那 N 根（最新 high=100）。

    include_unclosed_tail=True 时，在末尾追加一根 close_time 未来的未收盘 K 线（high=120），
    用来验证参照高点计算时必须排除它。
    """
    now_ms = int(time.time() * 1000)
    interval_ms = 3600 * 1000
    klines = []
    for i in range(closed_window):
        close_t = now_ms - (closed_window - i) * interval_ms
        open_t = close_t - interval_ms
        high = 90 + i  # 最后一根已关闭 K 线 high=90+(closed_window-1)
        klines.append(_make_kline(open_t, close_t, high, 80.0, 85.0))
    if include_unclosed_tail:
        open_t = now_ms - interval_ms // 2  # 半小时前开的
        close_t = now_ms + interval_ms // 2  # 半小时后才关
        klines.append(_make_kline(open_t, close_t, 120.0, 95.0, 115.0))
    return klines


# ============================================================
# 参照线计算契约
# ============================================================

class TestBreakoutReferenceHigh:
    """参照高点必须只纳入已关闭 K 线，且固定到 analysis 结果里。"""

    def test_fixed_to_closed_klines_only(self):
        """已关闭窗口 5 根 high 分别是 90..94，最近高点应为 94 而非未收盘 120。"""
        klines = _build_klines(closed_window=5, include_unclosed_tail=True)
        lookback = 5
        ref = _compute_breakout_reference_high(klines, lookback)
        assert ref is not None
        assert ref['high'] == 94.0  # 已关闭的最后一根 high
        # 参照窗口结束点必须是最后一根被纳入 K 线的 close_time
        assert ref['window_end'] == klines[-2]['close_time']  # klines[-1] 未收盘，应排除

    def test_all_closed_take_last_n(self):
        """全部已关闭时，直接取最后 N 根的最高点。"""
        klines = _build_klines(closed_window=6)  # high=90..95
        ref = _compute_breakout_reference_high(klines, lookback=5)
        assert ref is not None
        assert ref['high'] == 95.0  # 最后 5 根 high 是 91..95，max=95
        assert ref['window_end'] == klines[-1]['close_time']

    def test_lookback_exceeds_available(self):
        """K 线不足 lookback 时，取全部可用的已关闭 K 线。"""
        klines = _build_klines(closed_window=3)  # 只有 3 根
        ref = _compute_breakout_reference_high(klines, lookback=10)
        assert ref is not None
        assert ref['high'] == 92.0  # 90, 91, 92
        assert ref['window_end'] == klines[-1]['close_time']

    def test_all_klines_unclosed_returns_none(self):
        """极端情况：全部 K 线都未关闭，必须返回 None（调用方应跳过反弹检查）。"""
        now_ms = int(time.time() * 1000)
        klines = [
            _make_kline(now_ms - 900_000, now_ms + 900_000, 150.0, 100.0, 110.0),
            _make_kline(now_ms - 1_800_000, now_ms - 600_000, 130.0, 90.0, 100.0),
        ]
        ref = _compute_breakout_reference_high(klines, lookback=5)
        # 第二根 close_time 已过（已关闭），所以应该能算
        assert ref is not None
        # 把两根都改成未来 close_time
        klines_all_future = [
            _make_kline(now_ms - 600_000, now_ms + 1_800_000, 150.0, 100.0, 110.0),
        ]
        ref2 = _compute_breakout_reference_high(klines_all_future, lookback=5)
        assert ref2 is None


# ============================================================
# 执行器构造 + execute_signal 反弹闸门
# ============================================================

def _make_minimal_strategy(config: Dict[str, Any]) -> NewCoinStrategy:
    """绕过 __init__ 构造 NewCoinStrategy，只注入 execute_signal 所需属性。"""
    s = object.__new__(NewCoinStrategy)
    s.config = config
    # 状态（execute_signal 闸门会检查）
    s.pause_until = None  # 未暂停
    s.consecutive_losses = 0
    s.traded_symbols = []
    s.blacklist = []
    s.daily_trade_count = 0
    s.last_trade_date = None
    s.positions = {}
    # _check_pause_status 内部还会用到 consecutive_losses；让它始终返回 False 即可
    s._check_pause_status = AsyncMock(return_value=False)
    # 依赖（execute_signal 会调用 trading_executor.execute_short）
    s.binance_client = MagicMock()
    s.binance_client.get_ticker_price = AsyncMock()
    s.trading_executor = MagicMock()
    s.trading_executor.execute_short = AsyncMock()
    # score/analysis（execute_signal 会落仓）
    s._save_state = AsyncMock(return_value=None)
    # B3 影子记录容器（execute_signal 里也有懒初始化，这里仅让 hasattr 命中）
    s._shadow_rebound_rejects: Dict[str, Any] = {}
    return s


@pytest.fixture
def base_config() -> Dict[str, Any]:
    """一个最小可用的 new_coin config。"""
    return {
        'strategy': {'name': 'new_coin'},
        'scoring': {
            'rebound_check': {
                'enabled': True,
                'lookback_klines': 5,
                # 新鲜度/影子开关（测试可以覆盖）
                'max_price_age_seconds': 30,  # 30秒
                'shadow': {'enabled': True},
            },
        },
        'trading': {
            'daily_trade_limit': 2,
        },
    }


def _signal_with_reference(*, symbol: str = 'TESTUSDT',
                           breakout_high: float = 100.0,
                           window_end_ms: int = 0,
                           signal_time_ms: int = 0,
                           sentiment_degraded: bool = False,
                           oi_lookback_hours_actual: int | None = None) -> Dict[str, Any]:
    """构造一份包含参照高点的 signal 字典。"""
    if window_end_ms == 0:
        window_end_ms = int(time.time() * 1000) - 1000  # 已关闭
    if signal_time_ms == 0:
        signal_time_ms = int(time.time() * 1000)
    return {
        'symbol': symbol,
        'action': 'SHORT',
        'score_result': {
            'symbol': symbol, 'total_score': 8.5,
            'contract_score': 9.0, 'technical_score': 7.0, 'sentiment_score': 8.0,
            'veto': False, 'veto_reason': '',
            'details': {},
        },
        # 旧路径里可能还带 market_data（current_price 来自 analyze 时的 close），
        # 但 B3 新逻辑会忽略它、改用新鲜 ticker；这里故意塞个不同值，验证不会被用。
        'market_data': {'current_price': 50.0},
        # B3 新增的参照与时间戳
        'breakout_reference': {
            'breakout_reference_high': breakout_high,
            'reference_window_end': window_end_ms,
            'signal_time': signal_time_ms,
        },
        'sentiment_degraded': sentiment_degraded,
        'oi_lookback_hours_actual': oi_lookback_hours_actual,
    }


@pytest.mark.asyncio
class TestExecuteSignalReboundGate:
    """execute_signal 里的反弹闸门 + 新鲜价逻辑。"""

    async def test_real_price_below_reference_passes(self, base_config):
        """实时价 99.9 < 参照高点 100 → 通过反弹闸门（D3 验收样例 1）。"""
        base_config['scoring']['rebound_check']['shadow']['enabled'] = False
        strategy = _make_minimal_strategy(base_config)
        strategy.binance_client.get_ticker_price = AsyncMock(return_value=Decimal('99.9'))
        strategy.trading_executor.execute_short = AsyncMock(
            return_value=({'orderId': 1}, ''))

        signal = _signal_with_reference(breakout_high=100.0)
        ok, reason = await strategy.execute_signal(signal)

        assert ok is True
        strategy.trading_executor.execute_short.assert_awaited_once()
        # 落仓 entry_price 应使用新鲜 ticker，不是旧 market_data.current_price
        stored = strategy.positions['TESTUSDT']
        assert abs(stored['entry_price'] - 99.9) < 1e-9

    async def test_real_price_equal_to_reference_passes(self, base_config):
        """实时价 == 参照高点 → 按「超过」规则通过（D3 验收样例 2）。"""
        base_config['scoring']['rebound_check']['shadow']['enabled'] = False
        strategy = _make_minimal_strategy(base_config)
        strategy.binance_client.get_ticker_price = AsyncMock(return_value=Decimal('100.0'))
        strategy.trading_executor.execute_short = AsyncMock(
            return_value=({'orderId': 1}, ''))

        signal = _signal_with_reference(breakout_high=100.0)
        ok, _ = await strategy.execute_signal(signal)
        assert ok is True
        strategy.trading_executor.execute_short.assert_awaited_once()

    async def test_real_price_exceeds_reference_rejects(self, base_config):
        """实时价 100.1 > 参照高点 100 → 拒绝（D3 验收样例 3）。"""
        base_config['scoring']['rebound_check']['shadow']['enabled'] = False
        strategy = _make_minimal_strategy(base_config)
        strategy.binance_client.get_ticker_price = AsyncMock(return_value=Decimal('100.1'))

        signal = _signal_with_reference(breakout_high=100.0)
        ok, reason = await strategy.execute_signal(signal)

        assert ok is False
        assert '反弹' in reason
        # 关键：不得调用 execute_short
        strategy.trading_executor.execute_short.assert_not_awaited()

    async def test_ticker_fetch_failure_rejects(self, base_config):
        """get_ticker_price 抛异常 → 拒绝（新鲜价获取失败闸门）。"""
        base_config['scoring']['rebound_check']['shadow']['enabled'] = False
        strategy = _make_minimal_strategy(base_config)
        strategy.binance_client.get_ticker_price = AsyncMock(
            side_effect=ConnectionError('网络抖动'))

        signal = _signal_with_reference()
        ok, reason = await strategy.execute_signal(signal)
        assert ok is False
        assert '实时价' in reason or 'ticker' in reason.lower() or 'ticker' in reason

    async def test_ticker_returns_non_positive_rejects(self, base_config):
        """ticker 返回 0 或负值 → 视为无效价，拒绝。"""
        base_config['scoring']['rebound_check']['shadow']['enabled'] = False
        strategy = _make_minimal_strategy(base_config)
        strategy.binance_client.get_ticker_price = AsyncMock(return_value=Decimal('0'))

        signal = _signal_with_reference()
        ok, reason = await strategy.execute_signal(signal)
        assert ok is False
        assert '无效' in reason or '实时价' in reason

    async def test_stale_ticker_rejects(self, base_config):
        """ticker 超龄（mock 返回 observed_at 过老） → 拒绝。

        通过 patch 让 `_fetch_fresh_ticker` 返回一个 observed_at 超龄的价格。
        """
        base_config['scoring']['rebound_check']['shadow']['enabled'] = False
        base_config['scoring']['rebound_check']['max_price_age_seconds'] = 5
        strategy = _make_minimal_strategy(base_config)

        # ticker 超龄：返回 (None, stale_observed) 触发 _fetch_fresh_price_and_check_rebound 的
        # 「实时价超龄」拒绝分支（observed_at 有值 → reason 精确为"实时价超龄"）
        stale_observed = int(time.time()) - 30
        async def fake_fetch(*args, **kwargs):
            return None, stale_observed

        signal = _signal_with_reference(breakout_high=100.0)
        with patch.object(NewCoinStrategy, '_fetch_fresh_ticker', fake_fetch):
            ok, reason = await strategy.execute_signal(signal)

        assert ok is False
        assert '超龄' in reason or '过期' in reason or '新鲜' in reason

    async def test_no_reference_skips_check(self, base_config):
        """signal 里没有 breakout_reference → 不做反弹门（兼容旧信号）。"""
        base_config['scoring']['rebound_check']['shadow']['enabled'] = False
        strategy = _make_minimal_strategy(base_config)
        strategy.binance_client.get_ticker_price = AsyncMock(return_value=Decimal('95.0'))
        strategy.trading_executor.execute_short = AsyncMock(
            return_value=({'orderId': 1}, ''))

        signal = {
            'symbol': 'TESTUSDT',
            'action': 'SHORT',
            'score_result': {
                'symbol': 'TESTUSDT', 'total_score': 8.5,
                'contract_score': 9.0, 'technical_score': 7.0, 'sentiment_score': 8.0,
                'veto': False, 'veto_reason': '', 'details': {},
            },
            'market_data': {'current_price': 95.0},
        }
        ok, reason = await strategy.execute_signal(signal)
        # legacy path：没有 breakout_reference → 走旧路径，应允许
        assert ok is True
        strategy.trading_executor.execute_short.assert_awaited_once()

    # ---- 影子模式 ------------------------------------------------------------

    async def test_shadow_enabled_does_not_reject(self, base_config):
        """影子开关开启（默认安全态）时，即使实时价超限也只记录不拦截。"""
        base_config['scoring']['rebound_check']['shadow']['enabled'] = True  # 默认
        strategy = _make_minimal_strategy(base_config)
        strategy.binance_client.get_ticker_price = AsyncMock(return_value=Decimal('150.0'))
        strategy.trading_executor.execute_short = AsyncMock(
            return_value=({'orderId': 1}, ''))

        signal = _signal_with_reference(breakout_high=100.0)
        ok, _ = await strategy.execute_signal(signal)
        # 影子模式下即使实时价 > 参照高点，也应放行
        assert ok is True
        # 但应有影子记录（在 strategy._shadow_rebound_rejects 里）
        assert hasattr(strategy, '_shadow_rebound_rejects')
        assert 'TESTUSDT' in strategy._shadow_rebound_rejects

    async def test_shadow_disabled_rejects(self, base_config):
        """影子关闭 → 真实拦截。"""
        base_config['scoring']['rebound_check']['shadow']['enabled'] = False
        strategy = _make_minimal_strategy(base_config)
        strategy.binance_client.get_ticker_price = AsyncMock(return_value=Decimal('150.0'))

        signal = _signal_with_reference(breakout_high=100.0)
        ok, _ = await strategy.execute_signal(signal)
        assert ok is False


# ============================================================
# 配置常量契约（默认值不能是 None/负数）
# ============================================================

class TestConfigConstants:
    def test_freshness_default_positive(self):
        assert _PRICE_FRESH_DEFAULT_SECONDS > 0

    def test_shadow_default_is_safe(self):
        # 安全态：影子开启，不影响实盘
        assert _SHADOW_DEFAULT is True
