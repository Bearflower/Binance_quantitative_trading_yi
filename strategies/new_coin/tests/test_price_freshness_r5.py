"""R5 [P2] 新币价格新鲜度 —— 回归用例

覆盖验收断言 1-4（对应 requirements_R1-R6_2026-10-16.md 第 219-224 行）：

  1. 慢返回     → 请求耗时 60s、max_age=5s → 拒绝
  2. 执行前超时 → 取价到下单前耗时超 max_age → 重取 + 重判
  3. 重取突破   → 非影子拒绝 / 影子放行留痕
  4. 未知源时间 → source_time_ms=None → 用 request_span 代理，不静默放行

复现脚本原型：docs/reviews/strategy_reaudit_2026-10-16/reproduce_findings.py
  slow_ticker_accepted → 本文件 test_slow_ticker_rejected 反转后
  （旧代码恒接受 60s 请求；修复后拒绝）。

全部用 mock/合成行情，不连交易所/DB。
"""

from __future__ import annotations

import os
import sys
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from strategies.new_coin.strategy import (
    NewCoinStrategy,
    FreshQuote,
    _PRICE_FRESH_DEFAULT_SECONDS,
)
from strategies.new_coin.tests.conftest import make_minimal_executor


# ============================================================
# 辅助构造
# ============================================================

def _make_minimal_strategy(config: Dict[str, Any]) -> NewCoinStrategy:
    """绕过 __init__，只注入 execute_signal 所需属性（沿用 test_rebound_entry_contract）。"""
    s = object.__new__(NewCoinStrategy)
    s.config = config
    s.pause_until = None
    s.consecutive_losses = 0
    s.traded_symbols = []
    s.blacklist = []
    s.daily_trade_count = 0
    s.last_trade_date = None
    s.positions = {}
    s._check_pause_status = AsyncMock(return_value=False)
    s.binance_client = MagicMock()
    s.binance_client.get_ticker_price = AsyncMock()
    s.trading_executor = MagicMock()
    s.trading_executor.execute_short = AsyncMock()
    s._save_state = AsyncMock(return_value=None)
    s._shadow_rebound_rejects: Dict[str, Any] = {}
    return s


@pytest.fixture
def r5_config() -> Dict[str, Any]:
    return {
        'strategy': {'name': 'new_coin'},
        'scoring': {
            'rebound_check': {
                'enabled': True,
                'lookback_klines': 5,
                'max_price_age_seconds': 5,  # 统一用 5s 做新鲜度阈值
                'shadow': {'enabled': False},
            },
        },
        'trading': {'daily_trade_limit': 2},
    }


def _signal(*, symbol='TESTUSDT', breakout_high=100.0) -> Dict[str, Any]:
    return {
        'symbol': symbol,
        'action': 'SHORT',
        'score_result': {'total_score': 8.5},
        'breakout_reference': {
            'breakout_reference_high': breakout_high,
            'reference_window_end': 0,
        },
        'market_data': {'current_price': 0},
    }


def _fresh_quote(
    *,
    price: Decimal = Decimal('100'),
    source_time_ms: int | None = None,
    request_started_ms: int = 1000,
    request_finished_ms: int = 1000,
) -> FreshQuote:
    """手工构造 FreshQuote（默认 request_span=0、源时间未知）。"""
    return FreshQuote(
        price=price,
        source_time_ms=source_time_ms,
        request_started_ms=request_started_ms,
        request_finished_ms=request_finished_ms,
        source_time_kind=(
            'server_time' if source_time_ms is not None else 'request_span'
        ),
    )


# ============================================================
# 验收断言 1：慢返回必须拒绝（反转复现脚本 slow_ticker_accepted）
# ============================================================

class TestSlowTickerReject:
    """R5-1：请求耗时 > max_age_seconds 必须拒绝。"""

    async def test_slow_ticker_rejected(self, r5_config):
        """请求耗时 60s、max_age=5s → 拒绝（修复前恒接受，龄值恒为 0）。"""
        strategy = _make_minimal_strategy(r5_config)

        # 用 time.time patch 推进时钟 60 秒——ticker 返回后 time.time() 比 started 大 60
        clock = [1000]  # started_ms ≈ 1000; finished_ms ≈ 1060
        async def slow_fetch(_symbol):
            clock[0] += 60  # ticker "内部"推进 60s
            return Decimal('100')

        strategy.binance_client.get_ticker_price = slow_fetch

        async def _patched_now():
            return clock[0]

        with patch(
            'strategies.new_coin.strategy.time.time',
            side_effect=lambda: clock[0],
        ):
            quote, reason = await strategy._fetch_fresh_ticker('TESTUSDT', max_age_seconds=5)

        assert quote is None, "慢请求应被拒绝——quote 必须为 None"
        assert reason is not None
        assert '超龄' in reason, f"拒绝原因应含'超龄'——实际: {reason}"
        assert '60.0' in reason, f"拒绝原因应含请求耗时 60.0s——实际: {reason}"

    async def test_normal_fast_ticker_accepted(self, r5_config):
        """请求耗时 0.1s、max_age=5s → 正常接受（对照组）。"""
        strategy = _make_minimal_strategy(r5_config)

        clock = [1000]
        async def fast_fetch(_symbol):
            clock[0] += 0  # 不推进
            return Decimal('98.5')

        strategy.binance_client.get_ticker_price = fast_fetch

        with patch(
            'strategies.new_coin.strategy.time.time',
            side_effect=lambda: clock[0],
        ):
            quote, reason = await strategy._fetch_fresh_ticker('TESTUSDT', max_age_seconds=5)

        assert quote is not None, "正常请求应被接受"
        assert reason is None
        assert quote.request_span_ms == 0  # 请求耗时 ≈ 0
        assert quote.age_ms(clock[0] * 1000) == 0


# ============================================================
# 验收断言 2：执行准备超时必须调 entry_guard 重校
# ============================================================

class TestExecutionTimeoutEntryGuard:
    """R5-2：executor 下单前 FreshQuote 超龄时必须调 entry_guard。"""

    async def test_entry_guard_passed_to_executor(self, r5_config):
        """execute_signal 调 executor.execute_short 时应传 quote + entry_guard。"""
        strategy = _make_minimal_strategy(r5_config)
        strategy.binance_client.get_ticker_price = AsyncMock(return_value=Decimal('99.0'))
        strategy.trading_executor.execute_short = AsyncMock(
            return_value=({'orderId': 1}, '')
        )

        ok, reason = await strategy.execute_signal(_signal(breakout_high=100.0))
        assert ok is True

        # executor 应被调且收到 quote + entry_guard
        kwargs = strategy.trading_executor.execute_short.call_args.kwargs
        assert 'quote' in kwargs, "execute_short 应收到 quote 参数"
        assert kwargs['quote'] is not None
        assert 'entry_guard' in kwargs, "execute_short 应收到 entry_guard 参数"
        assert callable(kwargs['entry_guard']), "entry_guard 必须是可调用对象"

    async def test_entry_guard_not_set_for_legacy_signal(self, r5_config):
        """legacy signal（无 breakout_reference）→ 不传 FreshQuote/entry_guard。"""
        strategy = _make_minimal_strategy(r5_config)
        strategy.binance_client.get_ticker_price = AsyncMock(return_value=Decimal('99.0'))
        strategy.trading_executor.execute_short = AsyncMock(
            return_value=({'orderId': 1}, '')
        )

        legacy_signal = {
            'symbol': 'TESTUSDT',
            'action': 'SHORT',
            'score_result': {'total_score': 8.5},
            'market_data': {'current_price': 99.0},
            # 无 breakout_reference
        }
        ok, reason = await strategy.execute_signal(legacy_signal)
        assert ok is True

        kwargs = strategy.trading_executor.execute_short.call_args.kwargs
        assert kwargs.get('quote') is None, "legacy 路径不该有 FreshQuote"
        assert kwargs.get('entry_guard') is None, "legacy 路径不该有 entry_guard"

    async def test_entry_guard_rejection_propagated(self, r5_config):
        """entry_guard 返回 (None, reason) → executor 释放占位、返回拒绝原因。

        此用例验证 entry_guard 闭包内部逻辑（重取后反弹突破 + 影子关闭 → 拒绝）
        被 execute_signal 正确传递——无需真 executor，直接测 execute_signal 完整链路。
        """
        r5_config['scoring']['rebound_check']['shadow']['enabled'] = False
        strategy = _make_minimal_strategy(r5_config)
        # 初始价 OK（99 < 100），但 ticker 会先取到 99，
        # 然后 executor 重取时（entry_guard）返回 100.1（> 100）
        fetch_count = {'n': 0}
        async def two_fetch(_symbol):
            fetch_count['n'] += 1
            if fetch_count['n'] == 1:
                return Decimal('99.0')  # 首次取价 OK
            return Decimal('100.1')      # 重取反弹突破
        strategy.binance_client.get_ticker_price = two_fetch
        # executor 被调时若收到 entry_guard 且 quote 超龄，应返回重取拒绝
        # 这里我们 mock executor 直接拒绝（模拟 executor 内部重校拒绝路径）
        async def _fake_execute_short(**kw):
            # 真实 executor 会调 entry_guard；简化为：若 entry_guard 存在则调用之
            if kw.get('entry_guard'):
                nq, nr = await kw['entry_guard']()
                if nq is None:
                    return None, nr or "重取反弹突破"
            return {'orderId': 1}, ''
        strategy.trading_executor.execute_short = AsyncMock(side_effect=_fake_execute_short)

        ok, reason = await strategy.execute_signal(_signal(breakout_high=100.0))
        assert ok is False
        assert '反弹' in reason or '突破' in reason or '参照' in reason
        assert fetch_count['n'] == 2, "应调用 ticker 两次（首次 + 重取）"


# ============================================================
# 验收断言 3：重取后突破参照 —— 非影子拒绝 / 影子放行留痕
# ============================================================

class TestRefetchBreakoutD3:
    """R5-3：重取后突破固定参照高点，按 D3 口径处理。"""

    async def test_breakout_non_shadow_rejects(self, r5_config):
        """非影子 + 重取突破 → 拒绝。"""
        r5_config['scoring']['rebound_check']['shadow']['enabled'] = False
        strategy = _make_minimal_strategy(r5_config)
        fetch_count = {'n': 0}
        async def two_fetch(_symbol):
            fetch_count['n'] += 1
            if fetch_count['n'] == 1:
                return Decimal('99.0')
            return Decimal('100.1')  # > ref_high=100，突破
        strategy.binance_client.get_ticker_price = two_fetch

        async def _fake_execute_short(**kw):
            if kw.get('entry_guard'):
                nq, nr = await kw['entry_guard']()
                if nq is None:
                    return None, nr or "重取反弹突破"
            return {'orderId': 1}, ''
        strategy.trading_executor.execute_short = AsyncMock(side_effect=_fake_execute_short)

        ok, reason = await strategy.execute_signal(_signal(breakout_high=100.0))
        assert ok is False
        assert fetch_count['n'] == 2

    async def test_breakout_shadow_passes_with_trail(self, r5_config):
        """影子开启 + 重取突破 → 放行但留痕（_shadow_rebound_rejects 有记录）。"""
        r5_config['scoring']['rebound_check']['shadow']['enabled'] = True
        strategy = _make_minimal_strategy(r5_config)
        fetch_count = {'n': 0}
        async def two_fetch(_symbol):
            fetch_count['n'] += 1
            if fetch_count['n'] == 1:
                return Decimal('99.0')
            return Decimal('100.1')
        strategy.binance_client.get_ticker_price = two_fetch

        async def _fake_execute_short(**kw):
            if kw.get('entry_guard'):
                nq, nr = await kw['entry_guard']()
                if nq is None:
                    return None, nr or "重取反弹突破"
                # 影子放行 → 继续下单
            return {'orderId': 1}, ''
        strategy.trading_executor.execute_short = AsyncMock(side_effect=_fake_execute_short)

        ok, reason = await strategy.execute_signal(_signal(breakout_high=100.0))
        assert ok is True, "影子模式 + 反弹突破 → 放行留痕"
        # 影子留痕被 strategy 层 _fetch_fresh_price_and_check_rebound 和
        # entry_guard 都可能记录——至少有一条
        assert len(strategy._shadow_rebound_rejects) >= 1


# ============================================================
# 验收断言 4：未知源时间必须用代理口径，不静默放行
# ============================================================

class TestUnknownSourceTime:
    """R5-4：源时间不可得时，用 request_span 代理，明确标注，不静默放行。"""

    def test_unknown_source_time_uses_request_span(self):
        """FreshQuote.source_time_ms=None → age_ms 按代理口径：now - request_started_ms。

        R5 阻断修复：此前 age_ms 在代理路径固定返回 request_span_ms 常量，
        与传入 now_ms 无关 → executor 下单前重校时，执行准备耗时再长也不会超龄，
        entry_guard 永不触发。修复后 age_ms(now) 反映真实的「自请求发起以来」总时长。
        """
        q = _fresh_quote(
            price=Decimal('100'),
            source_time_ms=None,  # 源时间未知
            request_started_ms=1000,
            request_finished_ms=15000,  # 请求耗时 14s
        )
        assert q.source_time_kind == 'request_span'
        # now_ms=100000 时，age = 100000 - 1000 = 99000ms（涵盖请求耗时 14s + 等待 85s）
        assert q.age_ms(100000) == 99000
        # now_ms=request_finished_ms 时，age ≈ request_span（请求结束时刻判定超龄的行为不变）
        assert q.age_ms(15000) == 14000  # 15000 - 1000 = 14000 = request_span_ms
        assert q.request_span_ms == 14000

    def test_known_source_time_uses_real_source(self):
        """已知源时间 → age_ms 用 now - source_time_ms。"""
        q = _fresh_quote(
            price=Decimal('100'),
            source_time_ms=10000,  # 源时间 10000ms
            request_started_ms=1000,
            request_finished_ms=1200,
        )
        assert q.source_time_kind == 'server_time'
        # age_ms(now_ms=20000) → 20000 - 10000 = 10000ms
        assert q.age_ms(20000) == 10000

    async def test_unknown_source_time_not_silently_passed(self, r5_config):
        """未知源时间 + 请求耗时 > max_age → 拒绝（不静默放行）。"""
        strategy = _make_minimal_strategy(r5_config)
        r5_config['scoring']['rebound_check']['max_price_age_seconds'] = 5

        clock = [1000]
        async def fetch_unknown_source(_symbol):
            clock[0] += 30  # 请求耗时 30s
            return Decimal('100')

        strategy.binance_client.get_ticker_price = fetch_unknown_source

        with patch(
            'strategies.new_coin.strategy.time.time',
            side_effect=lambda: clock[0],
        ):
            quote, reason = await strategy._fetch_fresh_ticker('TESTUSDT', max_age_seconds=5)

        assert quote is None, "未知源时间 + 超慢请求 → 拒绝"
        # 日志应含 request_span 代理口径（从 debug 日志断言不了，但 reject_reason 包含请求耗时）
        assert '30.0' in reason or '30' in reason


# ============================================================
# 阻断修复专项：代理路径下执行准备耗时累积 → age_ms 必须反映
# ============================================================

class TestExecutorEntryGuardTrigger:
    """R5 阻断修复验证：代理路径下执行准备耗时累积超龄时，entry_guard 必须被触发。

    根因：修复前 FreshQuote.age_ms 在 source_time_ms=None 时固定返回
    request_span_ms 常量，与 executor 传入的 now_ms 无关 → 执行准备
    耗时再长也不会超龄 → entry_guard 永不触发。
    """

    async def test_executor_triggers_entry_guard_when_prep_exceeds_max_age(self):
        """代理路径（source_time_ms=None）+ 执行准备推进 clock 超 max_age → 必须调 entry_guard。"""
        cfg = {
            'scoring': {'rebound_check': {'max_price_age_seconds': 1, 'shadow': {'enabled': False}}},
        }
        exe = make_minimal_executor(
            cfg, leverage=10, market_order_score_threshold=10,
            budget_position_size=Decimal('100'), budget_reject=None,
            format_quantity_return=Decimal('10'),
            place_short_order_return={'orderId': 99},
        )

        # 构造代理路径 FreshQuote：请求很快（span=0.05s），但 executor 下单前
        # now_ms - request_started_ms > 1000ms → age_ms 应该超龄
        quote = FreshQuote(
            price=Decimal('99.0'),
            source_time_ms=None,
            request_started_ms=1000,
            request_finished_ms=1050,  # 请求仅 50ms
            source_time_kind='request_span',
        )

        # 推进 clock 让 age_ms 超 max_age_seconds=1000ms
        # executor 内部会用 int(time.time()*1000) 取 now_ms
        # 让 now_ms = 1000 + 1500 = 2500 → age = 1500ms > 1000ms
        clock = [2.5]  # seconds，time.time() 返回秒
        async def _place_short_order_should_not_be_called(*a, **kw):
            raise AssertionError("_place_short_order 不应被调——entry_guard 拒绝后应返回")
        exe._place_short_order = _place_short_order_should_not_be_called

        # entry_guard 被调 → 返回 (None, reason) 模拟重取后反弹突破（非影子关闭 → 拒绝）
        guard_called = {'n': 0}
        async def guard():
            guard_called['n'] += 1
            return None, "执行前反弹突破参照高点"

        with patch('strategies.new_coin.executor.time.time', side_effect=lambda: clock[0]):
            # execute_short 会多次调 time.time；让后续调用也返回推进后的值
            result = await exe.execute_short(
                symbol='TESTUSDT',
                score_result={'total_score': 8.5},
                current_price=99.0,
                quote=quote,
                entry_guard=guard,
            )

        order, reason = result
        assert order is None, "entry_guard 拒绝后不应下单"
        assert '反弹' in reason or '突破' in reason or '参照' in reason, f"拒绝原因应含反弹关键词: {reason}"
        assert guard_called['n'] >= 1, "entry_guard 必须至少被调 1 次——阻断修复核心断言"
        # 占位应被释放
        assert exe._release_claim_quietly.await_count >= 1

    async def test_executor_skips_entry_guard_when_quote_fresh(self):
        """代理路径 + 执行准备耗时 < max_age → 不触发 entry_guard（核心断言）。

        后续下单流程 mock 完整度不在此测试覆盖范围内——R5 阻断修复的核心是
        「age_ms 在代理路径下反映真实时间推进」，此断言已由核心用例覆盖。
        """
        cfg = {
            'scoring': {'rebound_check': {'max_price_age_seconds': 30, 'shadow': {'enabled': False}}},
        }
        exe = make_minimal_executor(
            cfg, leverage=10, market_order_score_threshold=10,
            budget_position_size=Decimal('100'), budget_reject=None,
            format_quantity_return=Decimal('10'),
            place_short_order_return={'orderId': 99},
        )

        quote = FreshQuote(
            price=Decimal('99.0'),
            source_time_ms=None,
            request_started_ms=1000,
            request_finished_ms=1050,
            source_time_kind='request_span',
        )
        # now_ms 推进很小（age = 1060 - 1000 = 60ms << 30000ms）→ 不超龄
        clock = [1.06]

        guard_called = {'n': 0}
        async def guard():
            guard_called['n'] += 1
            return None, "不应被调"

        with patch('strategies.new_coin.executor.time.time', side_effect=lambda: clock[0]):
            await exe.execute_short(
                symbol='TESTUSDT',
                score_result={'total_score': 8.5},
                current_price=99.0,
                quote=quote,
                entry_guard=guard,
            )

        # R5 阻断修复核心断言：新鲜报价 + 短执行准备 → entry_guard 不应被调
        assert guard_called['n'] == 0, (
            f"新鲜报价不应触发 entry_guard（age_ms 在代理路径应反映真实时间推进，"
            f"age=60ms << max=30000ms，判定不超龄）"
        )
