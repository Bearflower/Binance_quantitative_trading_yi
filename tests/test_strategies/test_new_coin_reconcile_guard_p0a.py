"""P0-A / P0-C 修复验收测试：new_coin 僵尸对账 + 持续保护守卫 + 告警降频 + ATR 分类。

对应验收标准（方案文档 §P0-A / §P0-C）：
- P0-A-AC1 权威集合口径：真实敞口 = DB open ∩ 交易所空头；
- P0-A-AC2 僵尸：DB open 但交易所无仓，须连续 N 周期确认 + 关闭前回查，异常一律不关；
- P0-A-AC3 守卫：真实敞口缺保护单 → 补挂（补挂前清完成标记以允许自愈）；
- P0-A-AC4 守卫：无缺口不动、失败不置完成且告警；
- P0-A 边界：他策略持仓不动、真实持仓不误关、内存脏数据清理、并发重入拦截、首查异常 fail-closed；
- P0-C-AC6 告警降频：同一 key 窗口内只放行一次；
- P0-C ATR：K 线服务拒绝（KLineServiceError）→ 0 + 降频告警；200 空 → 0 不告警。

全部使用替身，不连接真实数据库与交易所。
"""
import os
import sys
import time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

# 复用同目录既有工厂，避免重复实现（DRY）
from test_take_profit_fill_detect import create_executor  # noqa: E402
from shared.kline_service import KLineServiceError  # noqa: E402
from strategies.new_coin.strategy import NewCoinStrategy  # noqa: E402


# ============================================================================
# 构造工具
# ============================================================================

def _deep_merge(base: dict, override: dict) -> None:
    """深度合并字典（就地修改 base）。"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _base_config(overrides: dict = None) -> dict:
    """构造 new_coin 策略测试配置（含对账/补全/告警配置）。"""
    config = {
        'strategy': {'name': 'new_coin'},
        'detector': {'check_interval': 300},
        'kline': {'interval': '15m', 'atr_period': 14},
        'trading': {
            'reconcile': {'enabled': True, 'zombie_confirm_cycles': 3},
            'replenish': {
                'guard_interval_seconds': 0,
                'require_take_profit': True,
                'alert_throttle_seconds': 3600,
            },
        },
        'notification': {'project': 'new_coin'},
    }
    if overrides:
        _deep_merge(config, overrides)
    return config


def _mock_executor(own_open=None) -> MagicMock:
    """构造 trading_executor 替身（各异步方法均可 await）。"""
    ex = MagicMock()
    ex.get_open_short_symbols = AsyncMock(
        return_value=set() if own_open is None else own_open
    )
    ex.find_missing_protection = AsyncMock(return_value=[])
    ex.reset_replenish_flag = MagicMock()
    ex.replenish_conditional_orders = AsyncMock(return_value=True)
    ex.should_notify = MagicMock(return_value=True)
    ex.alert_throttle_seconds = 3600
    return ex


def _build_strategy(overrides: dict = None) -> NewCoinStrategy:
    """构造带替身依赖的 new_coin 策略实例（跳过重量级 initialize）。"""
    strategy = NewCoinStrategy(_base_config(overrides))
    strategy.db = MagicMock()
    strategy.db.fetch_all = AsyncMock(return_value=[])
    strategy.binance_client = MagicMock()
    strategy.binance_client._request = AsyncMock(return_value=[])
    strategy.notification_client = MagicMock()
    strategy.notification_client.send = AsyncMock()
    strategy.trading_executor = _mock_executor()
    strategy._save_state = AsyncMock()
    strategy._handle_position_closed = AsyncMock()
    return strategy


def _closed_symbols(strategy) -> list:
    """取出 _handle_position_closed 实际被调用的 symbol 列表。"""
    return [c.args[0] for c in strategy._handle_position_closed.await_args_list]


# ============================================================================
# P0-A：僵尸对账（连续 N 周期 + 关闭前回查）
# ============================================================================

class TestZombieReconcile:

    @pytest.mark.asyncio
    async def test_zombie_closed_only_after_confirm_cycles(self):
        """连续 N 周期确认后第 N 次才关闭；前 N-1 次不关。"""
        strategy = _build_strategy({'trading': {'reconcile': {'zombie_confirm_cycles': 3}}})
        strategy.positions = {}
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value={'AAAUSDT'})
        strategy.binance_client._request = AsyncMock(return_value=[])  # 两查均无空头

        for _ in range(2):
            await strategy._reconcile_positions_with_exchange()
        strategy._handle_position_closed.assert_not_awaited()

        await strategy._reconcile_positions_with_exchange()
        strategy._handle_position_closed.assert_awaited_once()
        assert _closed_symbols(strategy) == ['AAAUSDT']
        assert 'AAAUSDT' not in strategy._zombie_miss_counts

    @pytest.mark.asyncio
    async def test_first_query_failure_is_fail_closed(self):
        """交易所持仓接口异常 → 整轮跳过、不推进计数、不关闭。"""
        strategy = _build_strategy()
        strategy.positions = {}
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value={'AAAUSDT'})
        strategy.binance_client._request = AsyncMock(side_effect=Exception("网络错误"))

        await strategy._reconcile_positions_with_exchange()

        strategy._handle_position_closed.assert_not_awaited()
        assert strategy._zombie_miss_counts == {}

    @pytest.mark.asyncio
    async def test_db_open_unavailable_is_fail_closed(self):
        """DB 自有币种集合不可用（None）→ 整轮跳过。"""
        strategy = _build_strategy()
        strategy.positions = {}
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value=None)
        strategy.binance_client._request = AsyncMock(return_value=[{'symbol': 'AAAUSDT', 'positionAmt': '-1'}])

        await strategy._reconcile_positions_with_exchange()

        strategy._handle_position_closed.assert_not_awaited()
        assert strategy._zombie_miss_counts == {}

    @pytest.mark.asyncio
    async def test_reconfirm_failure_does_not_close(self):
        """关闭前回查失败 → 本轮不关闭（fail-closed），计数保留。"""
        strategy = _build_strategy({'trading': {'reconcile': {'zombie_confirm_cycles': 1}}})
        strategy.positions = {}
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value={'AAAUSDT'})
        strategy.binance_client._request = AsyncMock(side_effect=[[], RuntimeError("回查失败")])

        await strategy._reconcile_positions_with_exchange()

        strategy._handle_position_closed.assert_not_awaited()
        assert strategy._zombie_miss_counts.get('AAAUSDT') == 1

    @pytest.mark.asyncio
    async def test_reconfirm_finds_position_clears_count(self):
        """回查发现有仓 → 视为真实敞口，计数清零且不关闭。"""
        strategy = _build_strategy({'trading': {'reconcile': {'zombie_confirm_cycles': 1}}})
        strategy.positions = {}
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value={'AAAUSDT'})
        strategy.binance_client._request = AsyncMock(
            side_effect=[[], [{'symbol': 'AAAUSDT', 'positionAmt': '-1'}]]
        )

        await strategy._reconcile_positions_with_exchange()

        strategy._handle_position_closed.assert_not_awaited()
        assert 'AAAUSDT' not in strategy._zombie_miss_counts

    @pytest.mark.asyncio
    async def test_real_position_not_closed(self):
        """DB open ∩ 交易所空头 → 真实持仓，既不在内存脏数据清理也不做僵尸关闭。"""
        strategy = _build_strategy()
        strategy.positions = {'AAAUSDT': {'entry_price': 10.0}}
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value={'AAAUSDT'})
        strategy.binance_client._request = AsyncMock(return_value=[{'symbol': 'AAAUSDT', 'positionAmt': '-2'}])

        await strategy._reconcile_positions_with_exchange()

        strategy._handle_position_closed.assert_not_awaited()
        assert 'AAAUSDT' in strategy.positions

    @pytest.mark.asyncio
    async def test_other_strategy_positions_untouched(self):
        """他策略持仓（交易所空头 − DB open）一律不动，仅关闭本策略僵尸。"""
        strategy = _build_strategy({'trading': {'reconcile': {'zombie_confirm_cycles': 1}}})
        strategy.positions = {}
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value={'AAAUSDT'})
        strategy.binance_client._request = AsyncMock(return_value=[{'symbol': 'OTHERUSDT', 'positionAmt': '-1'}])

        await strategy._reconcile_positions_with_exchange()

        assert _closed_symbols(strategy) == ['AAAUSDT']

    @pytest.mark.asyncio
    async def test_stale_memory_position_cleared_and_persisted(self):
        """内存脏数据（内存有、交易所无）→ 关闭并持久化。"""
        strategy = _build_strategy()
        strategy.positions = {'BBBUSDT': {'entry_price': 5.0}}
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value=set())
        strategy.binance_client._request = AsyncMock(return_value=[])

        await strategy._reconcile_positions_with_exchange()

        strategy._handle_position_closed.assert_awaited_once()
        assert _closed_symbols(strategy) == ['BBBUSDT']
        assert strategy.positions == {}
        strategy._save_state.assert_awaited()

    def test_zombie_confirm_cycles_zero_clamped_to_one(self):
        """配置误填 0 → 下限兜底为 1，避免首周期即关闭、绕过连续确认。"""
        strategy = _build_strategy({'trading': {'reconcile': {'zombie_confirm_cycles': 0}}})
        assert strategy.zombie_confirm_cycles == 1

    def test_zombie_confirm_cycles_negative_clamped_to_one(self):
        """配置误填负值 → 下限兜底为 1，避免连续确认机制被绕过。"""
        strategy = _build_strategy({'trading': {'reconcile': {'zombie_confirm_cycles': -5}}})
        assert strategy.zombie_confirm_cycles == 1


# ============================================================================
# P0-A：持续保护守卫
# ============================================================================

def _setup_guard(strategy, symbols, db_rows):
    """为守卫用例装配权威集合所需的替身返回值。"""
    strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value=set(symbols))
    strategy.binance_client._request = AsyncMock(
        return_value=[{'symbol': s, 'positionAmt': '-1'} for s in symbols]
    )
    strategy.db.fetch_all = AsyncMock(return_value=db_rows)


class TestProtectionGuard:

    @pytest.mark.asyncio
    async def test_gap_triggers_replenish_with_entry_price(self):
        """真实敞口缺保护单 → 清完成标记并按 DB 入场价增量补挂（strict 核验 + 传 missing）。"""
        strategy = _build_strategy()
        _setup_guard(strategy, ['AAAUSDT'], [{'symbol': 'AAAUSDT', 'entry_price': 100.0, 'opened_at': None}])
        strategy.trading_executor.find_missing_protection = AsyncMock(return_value=['止损单'])

        await strategy._guard_protection_orders()

        # P0-D：核验用 strict=True（不可判定时返回 None），补挂时把权威 missing 传入
        strategy.trading_executor.find_missing_protection.assert_awaited_once_with(
            'AAAUSDT', strict=True
        )
        strategy.trading_executor.reset_replenish_flag.assert_called_once_with('AAAUSDT')
        strategy.trading_executor.replenish_conditional_orders.assert_awaited_once()
        args = strategy.trading_executor.replenish_conditional_orders.await_args.args
        assert args[0] == 'AAAUSDT'
        assert args[1] == Decimal('100.0')
        assert strategy.trading_executor.replenish_conditional_orders.await_args.kwargs[
            'missing'
        ] == ['止损单']

    @pytest.mark.asyncio
    async def test_guard_uncertain_when_db_error_no_place(self):
        """P0-D-AC7：核验不可判定（None）→ 零补挂、零清标记、告警含「不可判定」、累计重试。"""
        strategy = _build_strategy()
        _setup_guard(strategy, ['AAAUSDT'], [{'symbol': 'AAAUSDT', 'entry_price': 100.0, 'opened_at': None}])
        strategy.trading_executor.find_missing_protection = AsyncMock(return_value=None)

        await strategy._guard_protection_orders()

        strategy.trading_executor.replenish_conditional_orders.assert_not_awaited()
        strategy.trading_executor.reset_replenish_flag.assert_not_called()
        assert strategy._protection_gap_attempts['AAAUSDT'] == 1
        strategy.notification_client.send.assert_awaited()
        message = strategy.notification_client.send.await_args.kwargs['message']
        assert '不可判定' in message

    @pytest.mark.asyncio
    async def test_gap_closed_then_reopen_self_heal(self):
        """P0-D-AC12：缺口闭合 → 下轮不补且清计数；随后再丢 → 重新补。"""
        strategy = _build_strategy()  # guard_interval_seconds=0，不节流
        _setup_guard(strategy, ['AAAUSDT'], [{'symbol': 'AAAUSDT', 'entry_price': 100.0, 'opened_at': None}])
        strategy.trading_executor.find_missing_protection = AsyncMock(return_value=['止盈单'])

        await strategy._guard_protection_orders()   # 第一轮：缺 TP → 补
        assert strategy.trading_executor.replenish_conditional_orders.await_count == 1
        assert strategy._protection_gap_attempts == {}

        strategy.trading_executor.find_missing_protection = AsyncMock(return_value=[])
        await strategy._guard_protection_orders()   # 第二轮：无缺口 → 不补、清计数
        assert strategy.trading_executor.replenish_conditional_orders.await_count == 1
        assert strategy._protection_gap_attempts == {}

        strategy.trading_executor.find_missing_protection = AsyncMock(return_value=['止盈单'])
        await strategy._guard_protection_orders()   # 第三轮：TP 再丢 → 重新补
        assert strategy.trading_executor.replenish_conditional_orders.await_count == 2

    @pytest.mark.asyncio
    async def test_no_gap_does_nothing(self):
        """无缺口 → 不补挂、不告警、清空重试计数。"""
        strategy = _build_strategy()
        _setup_guard(strategy, ['AAAUSDT'], [{'symbol': 'AAAUSDT', 'entry_price': 100.0, 'opened_at': None}])
        strategy.trading_executor.find_missing_protection = AsyncMock(return_value=[])

        await strategy._guard_protection_orders()

        strategy.trading_executor.replenish_conditional_orders.assert_not_awaited()
        strategy.notification_client.send.assert_not_awaited()
        assert strategy._protection_gap_attempts == {}

    @pytest.mark.asyncio
    async def test_replenish_failure_records_attempt_and_alerts(self):
        """补挂失败 → 累计重试次数 + 告警，不置完成标记。"""
        strategy = _build_strategy()
        _setup_guard(strategy, ['AAAUSDT'], [{'symbol': 'AAAUSDT', 'entry_price': 100.0, 'opened_at': None}])
        strategy.trading_executor.find_missing_protection = AsyncMock(return_value=['止损单'])
        strategy.trading_executor.replenish_conditional_orders = AsyncMock(return_value=False)

        await strategy._guard_protection_orders()

        assert strategy._protection_gap_attempts['AAAUSDT'] == 1
        strategy.notification_client.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_invalid_entry_price_alerts_without_replenish(self):
        """入场价无效 → 不补挂、直接告警（无法计算阈值）。"""
        strategy = _build_strategy()
        _setup_guard(strategy, ['AAAUSDT'], [{'symbol': 'AAAUSDT', 'entry_price': 0, 'opened_at': None}])
        strategy.trading_executor.find_missing_protection = AsyncMock(return_value=['止损单'])

        await strategy._guard_protection_orders()

        strategy.trading_executor.replenish_conditional_orders.assert_not_awaited()
        strategy.notification_client.send.assert_awaited()
        assert strategy._protection_gap_attempts == {}

    @pytest.mark.asyncio
    async def test_inflight_symbol_is_skipped(self):
        """已在途（_guard_inflight）的 symbol 本轮跳过，防并发重复补挂。"""
        strategy = _build_strategy()
        _setup_guard(strategy, ['AAAUSDT'], [{'symbol': 'AAAUSDT', 'entry_price': 100.0, 'opened_at': None}])
        strategy._guard_inflight = {'AAAUSDT'}

        await strategy._guard_protection_orders()

        strategy.trading_executor.find_missing_protection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_guard_throttled_within_interval(self):
        """未到配置周期 → 整轮跳过，不核验保护单。"""
        strategy = _build_strategy({'trading': {'replenish': {'guard_interval_seconds': 300}}})
        _setup_guard(strategy, ['AAAUSDT'], [{'symbol': 'AAAUSDT', 'entry_price': 100.0, 'opened_at': None}])
        strategy._guard_last_run_at = time.monotonic()

        await strategy._guard_protection_orders()

        strategy.trading_executor.find_missing_protection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_guard_fail_closed_when_exchange_unavailable(self):
        """交易所持仓不可用 → 守卫整轮跳过（fail-closed）。"""
        strategy = _build_strategy()
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value={'AAAUSDT'})
        strategy.binance_client._request = AsyncMock(side_effect=Exception("网络错误"))

        await strategy._guard_protection_orders()

        strategy.trading_executor.replenish_conditional_orders.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_guard_isolates_per_symbol_exception(self):
        """单标的核验异常不影响其余标的（逐标的隔离）。"""
        strategy = _build_strategy()
        _setup_guard(
            strategy, ['AAAUSDT', 'BBBUSDT'],
            [
                {'symbol': 'AAAUSDT', 'entry_price': 100.0, 'opened_at': None},
                {'symbol': 'BBBUSDT', 'entry_price': 50.0, 'opened_at': None},
            ],
        )

        async def _flaky(symbol, *args, **kwargs):
            if symbol == 'AAAUSDT':
                raise RuntimeError("DB 抖动")
            return ['止损单']

        strategy.trading_executor.find_missing_protection = AsyncMock(side_effect=_flaky)

        await strategy._guard_protection_orders()

        strategy.trading_executor.replenish_conditional_orders.assert_awaited_once()
        assert strategy.trading_executor.replenish_conditional_orders.await_args.args[0] == 'BBBUSDT'


# ============================================================================
# P0-A/P0-C：executor 层 —— 缺口核验 / 降频 / ATR 分类
# ============================================================================

class TestExecutorProtectionHelpers:

    @pytest.mark.asyncio
    async def test_find_missing_protection_reports_take_profit_gap(self):
        """仅有止损单 → 缺止盈单。"""
        executor = create_executor()
        executor.db.fetch_all = AsyncMock(return_value=[{'order_type': 'STOP_LOSS'}])
        assert await executor.find_missing_protection('AAAUSDT') == ['止盈单']

    @pytest.mark.asyncio
    async def test_find_missing_protection_no_gap(self):
        """止损 + 止盈均在 → 无缺口。"""
        executor = create_executor()
        executor.db.fetch_all = AsyncMock(
            return_value=[{'order_type': 'STOP_LOSS'}, {'order_type': 'TAKE_PROFIT'}]
        )
        assert await executor.find_missing_protection('AAAUSDT') == []

    @pytest.mark.asyncio
    async def test_find_missing_protection_query_error_returns_all(self):
        """查询异常 → 保守返回全部应存在类型（fail-closed）。"""
        executor = create_executor()
        executor.db.fetch_all = AsyncMock(side_effect=Exception("DB 不可用"))
        assert await executor.find_missing_protection('AAAUSDT') == ['止损单', '止盈单']

    @pytest.mark.asyncio
    async def test_find_missing_protection_require_tp_false(self):
        """require_take_profit=False → 不要求止盈单。"""
        executor = create_executor()
        executor.db.fetch_all = AsyncMock(return_value=[])
        assert await executor.find_missing_protection('AAAUSDT', require_take_profit=False) == ['止损单']

    def test_reset_replenish_flag_discards(self):
        """reset_replenish_flag 清除完成标记，允许后续自愈。"""
        executor = create_executor()
        executor._replenished_symbols.add('AAAUSDT')
        executor.reset_replenish_flag('AAAUSDT')
        assert 'AAAUSDT' not in executor._replenished_symbols

    def test_should_notify_throttles_within_window(self):
        """同一 key 窗口内只放行一次；不同 key 相互独立。"""
        executor = create_executor()
        assert executor.should_notify(('k', 'AAAUSDT'), 60) is True
        assert executor.should_notify(('k', 'AAAUSDT'), 60) is False
        assert executor.should_notify(('k', 'BBBUSDT'), 60) is True

    def test_should_notify_zero_window_always_true(self):
        """window_seconds<=0 → 不降频，每次都放行。"""
        executor = create_executor()
        assert executor.should_notify(('k', 'AAAUSDT'), 0) is True
        assert executor.should_notify(('k', 'AAAUSDT'), 0) is True


class TestAtrClassification:

    @pytest.mark.asyncio
    async def test_kline_service_rejected_returns_zero_and_alerts(self):
        """K 线服务拒绝（KLineServiceError）→ ATR=0 + ERROR 告警。"""
        executor = create_executor()
        executor.kline_service.get_klines = AsyncMock(
            side_effect=KLineServiceError("K线数据暂不可用", status_code=503)
        )

        assert await executor._calculate_atr('AAAUSDT') == Decimal('0')
        executor.notification.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_insufficient_data_returns_zero_without_alert(self):
        """200 空（数据不足）→ ATR=0 但不告警（与「服务拒绝」区分）。"""
        executor = create_executor()
        executor.kline_service.get_klines = AsyncMock(return_value=[])

        assert await executor._calculate_atr('AAAUSDT') == Decimal('0')
        executor.notification.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_repeated_alerts_are_throttled(self):
        """同一 symbol 的 ATR 不可用告警在窗口内只发一次。"""
        executor = create_executor()
        executor.kline_service.get_klines = AsyncMock(
            side_effect=KLineServiceError("K线数据暂不可用", status_code=503)
        )

        await executor._calculate_atr('AAAUSDT')
        await executor._calculate_atr('AAAUSDT')

        assert executor.notification.send.await_count == 1
