"""P0-A / P0-C 覆盖率补齐用例（补充既有 P0-A 验收测试未覆盖的分支/异常/边界）。

覆盖补充点（均为既有源码的真实分支，不改动被测源码）：
- 对账开关关闭时的回退路径 `_sync_memory_only`（设计好的降级：reconcile.enabled=false）；
- `_reconcile_positions_with_exchange`/`_guard_protection_orders` 在 executor 缺失时直接返回；
- 交易所响应非 list、元素非 dict、positionAmt 非数值等异常结构的 fail-closed 处理；
- 关闭前回查 `_reconfirm_symbol_absent` 的非 list 响应与 ValueError 容错；
- DB 查询异常 → `_load_open_positions_from_db` 返回空字典；缺 symbol 行被跳过；
- 守卫权威集合为空、权威集合不可用时**不推进时间戳**（fail-closed 可观测）；
- 告警降频命中（`_notify_protection_issue` / `_notify_atr_unavailable`）与告警发送失败的容错；
- 边界：`zombie_confirm_cycles=0`、配置缺省时的默认值、`_calculate_atr` 显式 period 与通用异常。

全部使用替身，不连接真实数据库与交易所。
"""
import os
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

from test_take_profit_fill_detect import create_executor  # noqa: E402
from strategies.new_coin.strategy import NewCoinStrategy  # noqa: E402


# 复用既有 P0-A 测试文件的构造工具，避免重复实现（DRY）
from test_new_coin_reconcile_guard_p0a import (  # noqa: E402
    _base_config,
    _build_strategy,
    _closed_symbols,
    _mock_executor,
    _setup_guard,
)


# ============================================================================
# 对账：开关回退 / executor 缺失 / 异常响应结构
# ============================================================================

class TestReconcileFallbacks:

    @pytest.mark.asyncio
    async def test_reconcile_disabled_falls_back_to_memory_only(self):
        """reconcile.enabled=False → 走旧行为：仅清内存脏数据，不查 DB open。"""
        strategy = _build_strategy({'trading': {'reconcile': {'enabled': False}}})
        strategy.positions = {'BBBUSDT': {'entry_price': 5.0}}
        strategy.binance_client._request = AsyncMock(return_value=[])

        await strategy._reconcile_positions_with_exchange()

        assert _closed_symbols(strategy) == ['BBBUSDT']
        assert strategy.positions == {}
        # 回退路径不应查询 DB 自有币种集合
        strategy.trading_executor.get_open_short_symbols.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_memory_only_returns_when_exchange_unavailable(self):
        """回退路径中交易所不可用（None）→ 直接返回，不清理不持久化。"""
        strategy = _build_strategy({'trading': {'reconcile': {'enabled': False}}})
        strategy.positions = {'BBBUSDT': {'entry_price': 5.0}}
        strategy.binance_client._request = AsyncMock(side_effect=Exception("网络错误"))

        await strategy._reconcile_positions_with_exchange()

        strategy._handle_position_closed.assert_not_awaited()
        strategy._save_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_memory_only_no_stale_does_not_persist(self):
        """回退路径中无脏数据 → 不调用 _save_state。"""
        strategy = _build_strategy({'trading': {'reconcile': {'enabled': False}}})
        strategy.positions = {}
        strategy.binance_client._request = AsyncMock(return_value=[])

        await strategy._reconcile_positions_with_exchange()

        strategy._save_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reconfirm_skips_non_dict_item(self):
        """回查列表含非 dict 元素 → 跳过该元素，继续判定其余行。"""
        strategy = _build_strategy()
        strategy.binance_client._request = AsyncMock(return_value=[
            None,
            {'symbol': 'AAAUSDT', 'positionAmt': '-1'},
        ])

        assert await strategy._reconfirm_symbol_absent('AAAUSDT') is False

    @pytest.mark.asyncio
    async def test_reconcile_returns_early_without_executor(self):
        """executor 缺失 → 对账直接返回，不抛异常。"""
        strategy = _build_strategy()
        strategy.trading_executor = None

        await strategy._reconcile_positions_with_exchange()  # 不应抛异常

    @pytest.mark.asyncio
    async def test_exchange_response_not_list_is_fail_closed(self):
        """交易所响应非 list → _fetch_exchange_short_symbols 返回 None（fail-closed）。"""
        strategy = _build_strategy()
        strategy.binance_client._request = AsyncMock(return_value={'unexpected': 'dict'})

        assert await strategy._fetch_exchange_short_symbols() is None

    @pytest.mark.asyncio
    async def test_exchange_malformed_items_are_skipped(self):
        """元素非 dict / positionAmt 非数值 → 跳过，不污染空头集合。"""
        strategy = _build_strategy()
        strategy.binance_client._request = AsyncMock(return_value=[
            None,
            {'symbol': 'BADUSDT', 'positionAmt': 'abc'},
            {'symbol': 'OKUSDT', 'positionAmt': '-1'},
        ])

        assert await strategy._fetch_exchange_short_symbols() == {'OKUSDT'}

    @pytest.mark.asyncio
    async def test_reconfirm_non_list_response_does_not_close(self):
        """僵尸回查响应非 list → 返回 None（不关闭）。"""
        strategy = _build_strategy()
        strategy.binance_client._request = AsyncMock(return_value={'unexpected': 'dict'})

        assert await strategy._reconfirm_symbol_absent('AAAUSDT') is None

    @pytest.mark.asyncio
    async def test_reconfirm_malformed_amt_treated_as_absent(self):
        """回查中 positionAmt 非数值 → 容错跳过该行，判定为无仓（True）。"""
        strategy = _build_strategy()
        strategy.binance_client._request = AsyncMock(return_value=[
            {'symbol': 'AAAUSDT', 'positionAmt': 'not-a-number'},
        ])

        assert await strategy._reconfirm_symbol_absent('AAAUSDT') is True

    @pytest.mark.asyncio
    async def test_closed_zombie_not_closed_again_next_round(self):
        """P0-A-AC9：僵尸被关闭后（DB 不再返回），下一轮不再重复关闭/计数。"""
        strategy = _build_strategy({'trading': {'reconcile': {'zombie_confirm_cycles': 1}}})
        strategy.positions = {}
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value={'AAAUSDT'})
        strategy.binance_client._request = AsyncMock(return_value=[])

        await strategy._reconcile_positions_with_exchange()          # 第一轮：关闭
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value=set())
        await strategy._reconcile_positions_with_exchange()          # 第二轮：已非 open

        strategy._handle_position_closed.assert_awaited_once()
        assert strategy._zombie_miss_counts == {}

    @pytest.mark.asyncio
    async def test_zombie_confirm_cycles_zero_closes_immediately(self):
        """边界：zombie_confirm_cycles=0 → 首轮即确认关闭（1 >= 0）。"""
        strategy = _build_strategy({'trading': {'reconcile': {'zombie_confirm_cycles': 0}}})
        strategy.positions = {}
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value={'AAAUSDT'})
        strategy.binance_client._request = AsyncMock(return_value=[])

        await strategy._reconcile_positions_with_exchange()

        strategy._handle_position_closed.assert_awaited_once()
        assert _closed_symbols(strategy) == ['AAAUSDT']


class TestLoadOpenPositionsBoundaries:

    @pytest.mark.asyncio
    async def test_db_error_returns_empty_dict(self):
        """DB 查询异常 → 返回空字典（不抛异常，调用方按无入场价处理）。"""
        strategy = _build_strategy()
        strategy.db.fetch_all = AsyncMock(side_effect=Exception("DB 不可用"))

        assert await strategy._load_open_positions_from_db() == {}

    @pytest.mark.asyncio
    async def test_rows_without_symbol_are_skipped(self):
        """缺 symbol 的行被跳过，合法行正常解析；opened_at 为空 → entry_time=None。"""
        strategy = _build_strategy()
        strategy.db.fetch_all = AsyncMock(return_value=[
            {'entry_price': 1.0, 'opened_at': None},                # 缺 symbol，跳过
            {'symbol': 'AAAUSDT', 'entry_price': 2.5, 'opened_at': None},
        ])

        result = await strategy._load_open_positions_from_db()

        assert set(result.keys()) == {'AAAUSDT'}
        assert result['AAAUSDT']['entry_price'] == 2.5
        assert result['AAAUSDT']['entry_time'] is None


class TestConfigDefaults:

    def test_reconcile_and_guard_defaults_when_config_absent(self):
        """配置缺省时使用文档默认值：enabled=True / N=3 / guard_interval=300。"""
        cfg = _base_config()
        cfg['trading'].pop('reconcile', None)
        cfg['trading']['replenish'].pop('guard_interval_seconds', None)
        strategy = NewCoinStrategy(cfg)

        assert strategy.reconcile_enabled is True
        assert strategy.zombie_confirm_cycles == 3
        assert strategy.guard_interval_seconds == 300.0


# ============================================================================
# 守卫：executor 缺失 / 权威集合为空 / 不可用时不推进时间戳
# ============================================================================

class TestGuardBoundaries:

    @pytest.mark.asyncio
    async def test_guard_returns_early_without_executor(self):
        """executor 缺失 → 守卫直接返回，不抛异常。"""
        strategy = _build_strategy()
        strategy.trading_executor = None

        await strategy._guard_protection_orders()  # 不应抛异常

    @pytest.mark.asyncio
    async def test_guard_empty_authoritative_skips_work(self):
        """权威集合为空 → 推进时间戳但不核验任何标的。"""
        strategy = _build_strategy()
        _setup_guard(strategy, [], [])  # DB open / 交易所空头均为空

        await strategy._guard_protection_orders()

        strategy.trading_executor.find_missing_protection.assert_not_awaited()
        assert strategy._guard_last_run_at > 0.0

    @pytest.mark.asyncio
    async def test_guard_unavailable_does_not_advance_timestamp(self):
        """权威集合不可用（取数失败）→ 不推进时间戳，下一轮立即重试。"""
        strategy = _build_strategy()
        strategy.trading_executor.get_open_short_symbols = AsyncMock(return_value={'AAAUSDT'})
        strategy.binance_client._request = AsyncMock(side_effect=Exception("网络错误"))

        await strategy._guard_protection_orders()

        assert strategy._guard_last_run_at == 0.0
        strategy.trading_executor.replenish_conditional_orders.assert_not_awaited()


# ============================================================================
# 告警：降频命中 / 发送失败容错（策略层与 executor 层）
# ============================================================================

class TestNotifyResilience:

    @pytest.mark.asyncio
    async def test_protection_issue_throttled_does_not_send(self):
        """降频命中（should_notify=False）→ 不发送飞书。"""
        strategy = _build_strategy()
        strategy.trading_executor.should_notify = MagicMock(return_value=False)

        await strategy._notify_protection_issue('AAAUSDT', ['止损单'], 2)

        strategy.notification_client.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_protection_issue_send_failure_is_swallowed(self):
        """告警发送异常 → 被吞掉，不影响主流程。"""
        strategy = _build_strategy()
        strategy.notification_client.send = AsyncMock(side_effect=Exception("飞书不可用"))

        await strategy._notify_protection_issue('AAAUSDT', ['止损单'], 1)  # 不应抛异常

    @pytest.mark.asyncio
    async def test_protection_issue_message_contains_symbol_type_attempt(self):
        """P0-C-AC4：告警文案须含 symbol、缺失类型、累计重试次数。"""
        strategy = _build_strategy()

        await strategy._notify_protection_issue('AAAUSDT', ['止损单', '止盈单'], 2)

        message = strategy.notification_client.send.await_args.kwargs['message']
        assert 'AAAUSDT' in message
        assert '止损单' in message and '止盈单' in message
        assert '累计重试次数: 2' in message


    @pytest.mark.asyncio
    async def test_atr_unavailable_throttled_does_not_send(self):
        """ATR 告警降频命中 → 不发送。"""
        executor = create_executor()
        executor.should_notify = MagicMock(return_value=False)

        await executor._notify_atr_unavailable('AAAUSDT')

        executor.notification.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_atr_unavailable_send_failure_is_swallowed(self):
        """ATR 告警发送异常 → 被吞掉，不影响主流程。"""
        executor = create_executor()
        executor.notification.send = AsyncMock(side_effect=Exception("飞书不可用"))

        await executor._notify_atr_unavailable('AAAUSDT')  # 不应抛异常

    @pytest.mark.asyncio
    async def test_calculate_atr_with_explicit_period_and_generic_error(self):
        """显式传入 period（跳过配置读取）+ 通用异常 → 返回 0 且不告警。"""
        executor = create_executor()
        executor.kline_service.get_klines = AsyncMock(side_effect=ValueError("解析失败"))

        assert await executor._calculate_atr('AAAUSDT', period=14) == Decimal('0')
        executor.notification.send.assert_not_awaited()
