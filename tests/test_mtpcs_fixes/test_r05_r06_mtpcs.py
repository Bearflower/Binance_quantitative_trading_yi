"""
R05 / R06 / R07 修复批次单测（MTPCS 原版 btc_eth + 激进版 btc_eth_aggressive）

本文件只覆盖本次开发的三个修复项，且**两策略参数化**（同一断言对两版本各跑一遍）：

  R05（保护单失败丢失已成交开仓管理状态）
    - AC1 入场成交、止损单失败 → 持仓已登记、返回「持仓已建立、保护待补」
    - AC2 入场成交、TP1 失败但止损成功 → stop_loss_order_id 有值、protection_pending=True
    - AC3 补挂成功 → protection_pending=False、保护单 ID 齐备
    - AC4 补挂达配置上限仍失败 → 按 on_exhausted 告警、不自动减仓（D2）、保持 pending
    - AC5 两策略同构（由参数化覆盖）
    - F1 登记不得晚于保护单创建；F3 失败不丢态 + 保留已成功 ID
    - 进程重启启动恢复能识别 protection_pending

  R06（入场超时撤单忽略部分成交 / 撤单竞态）
    - AC1 订单 CANCELED、executedQty=0.4 → 结构化为「部分成交」并按 0.4 建仓挂保护
    - AC2 超时撤单后重读 executedQty=0.4 → 部分成交
    - AC3 超时撤单后重读 executedQty=0 → 未成交（与修复前一致，放弃开仓）
    - AC4 撤单抛 -2011 且实际已成交 → 查单确认成交量并按成交处理
    - AC5 FILLED 路径不回归
    - AC6 两策略同构（由参数化覆盖）

  R07 开仓预占互斥（T16 接入 / D3 微仓清零）
    - 冲突 → 跳过开仓 + 告警，不抛异常
    - 开仓失败 → 释放占用；平仓归零 → 释放占用；微仓清零 → 释放占用
    - 占位不因保护失败被误释放
    - 定时清理过期占用且按 claim_cleanup_interval_minutes 节流
    - D3：微仓清零成功/失败（告警）分支

测试只使用假交易所 / 假 client，绝不触碰真实网络、交易所与数据库。
"""
import importlib
import os
import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# 确保项目根目录在 path 中
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

import pytest
import yaml

from shared.order_fill_waiter import OrderFillResult
from shared.api_retry_config import reset_cache

# 两版本参数化：模块路径 + 配置文件相对路径
STRATEGY_MODULES = [
    ('strategies.btc_eth.strategy', 'strategies/btc_eth/config.yaml'),
    ('strategies.btc_eth_aggressive.strategy',
     'strategies/btc_eth_aggressive/config.yaml'),
]


# ============================================================================
# 测试辅助构造器
# ============================================================================

def make_signal(**overrides) -> dict:
    """构造标准测试信号（字段可覆盖）"""
    signal = {
        'symbol': 'BTCUSDT',
        'direction': 'LONG',
        'grade': 'A',
        'score': 80,
        'timestamp': 1700000000,
        'quantity': Decimal('0.1'),
        'entry_price': Decimal('60000'),
        'leverage': 5,
        'atr': Decimal('1000'),
        'initial_stop_loss': Decimal('58500'),
        'tp1_price': Decimal('64000'),
        'tp2_price': Decimal('66000'),
    }
    signal.update(overrides)
    return signal


def make_position(position_cls, **overrides):
    """构造测试持仓状态（默认 LONG 0.1@60000、A 级）"""
    pos = position_cls()
    pos.direction = 'LONG'
    pos.entry_price = Decimal('60000')
    pos.initial_quantity = Decimal('0.1')
    pos.current_quantity = Decimal('0.1')
    pos.atr = Decimal('1000')
    pos.grade = 'A'
    for key, value in overrides.items():
        setattr(pos, key, value)
    return pos


def make_fill(order_id=100, executed=Decimal('0.1'), orig=None, status='FILLED',
              avg_price=Decimal('0')):
    """构造入场订单终态结果（R06 OrderFillResult），替代旧 dict 桩"""
    executed = Decimal(str(executed))
    orig = executed if orig is None else Decimal(str(orig))
    remaining = orig - executed
    if remaining < 0:
        remaining = Decimal('0')
    return OrderFillResult(
        status=status,
        executed_qty=executed,
        orig_qty=orig,
        remaining_qty=remaining,
        avg_price=Decimal(str(avg_price)),
        order_id=order_id,
        client_order_id=None,
        raw={},
    )


class FakeExchange:
    """假交易所客户端（仅实现 R06 等待助手所需方法，绝不联网）

    行为：在未调用 cancel_order 前返回 ``before`` 快照，调用后返回 ``after`` 快照；
    ``cancel_error`` 可模拟撤单竞态（-2011/-2013）。
    """

    def __init__(self, before, after=None):
        self._before = before
        self._after = after if after is not None else before
        self.cancel_calls = []
        self.place_calls = []
        self.cancel_error = None

    async def get_order(self, symbol, order_id):
        return self._after if self.cancel_calls else self._before

    async def get_order_by_client_id(self, symbol, client_order_id):
        return await self.get_order(symbol, client_order_id)

    async def cancel_order(self, symbol, order_id=None, client_order_id=None):
        # 先记录再抛错：模拟「撤单竞态时订单其实已成交」
        self.cancel_calls.append(order_id)
        if self.cancel_error is not None:
            raise self.cancel_error

    async def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return {'orderId': 100, 'status': 'NEW'}


def order_snapshot(status, executed, orig, avg='60000'):
    """构造交易所订单快照（币安原始字段）"""
    return {
        'orderId': 100,
        'status': status,
        'executedQty': executed,
        'origQty': orig,
        'avgPrice': avg,
    }


# ============================================================================
# Fixtures（两版本参数化）
# ============================================================================

@pytest.fixture(params=STRATEGY_MODULES, ids=['btc_eth', 'btc_eth_aggressive'])
def strategy_ctx(request):
    """参数化加载两版本策略模块与配置（不触碰真实交易所）"""
    module_name, config_relpath = request.param
    module = importlib.import_module(module_name)
    config_path = os.path.join(PROJECT_ROOT, config_relpath)
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    return SimpleNamespace(
        module=module,
        strategy_cls=module.BTCEthStrategy,
        position_cls=module.PositionState,
        config=config,
    )


@pytest.fixture
def strategy_module(strategy_ctx):
    """当前参数化版本的策略模块（用于 monkeypatch 模块级共享函数）"""
    return strategy_ctx.module


@pytest.fixture
def position_cls(strategy_ctx):
    """当前参数化版本的 PositionState 类"""
    return strategy_ctx.position_cls


@pytest.fixture
def error_cls(strategy_ctx):
    """当前参数化版本的 BinanceAPIError 类（保证与策略内判定同类）"""
    return strategy_ctx.module.BinanceAPIError


@pytest.fixture
def strategy(strategy_ctx):
    """构造策略实例并注入测试 mock（binance/kline/notification/db 全 mock）"""
    s = strategy_ctx.strategy_cls(
        config=strategy_ctx.config,
        binance_client=AsyncMock(),
        kline_service=AsyncMock(),
        notification_client=AsyncMock(),
        db_manager=None,
    )
    s.symbol_precision['BTCUSDT'] = {
        'stepSize': '0.001',
        'tickSize': Decimal('0.01'),
        'quantityPrecision': 3,
        'pricePrecision': 2,
    }
    s.binance.set_leverage = AsyncMock()
    s._check_entry_limits = AsyncMock(return_value=True)
    s._check_total_margin_ratio = AsyncMock(return_value=True)
    s.frequency_controller.record_trade = AsyncMock()
    s._send_signal_error_notification = AsyncMock()
    s.notification.send = AsyncMock()
    return s


@pytest.fixture(autouse=True)
def _fast_order_fill(monkeypatch):
    """将 order_fill 的可见延迟/轮询间隔压到 0，使 R06 测试秒级完成（值走环境变量）"""
    monkeypatch.setenv('ORDER_FILL_PM_ORDER_VISIBILITY_DELAY_SECONDS', '0')
    monkeypatch.setenv('ORDER_FILL_CHECK_INTERVAL_SECONDS', '0')
    reset_cache()
    yield
    reset_cache()


# ============================================================================
# R05：保护单失败不丢仓位（保护完整性独立状态 + 补挂收敛）
# ============================================================================

class TestR05ProtectionState:
    """R05 保护单失败后仍保留已成交开仓的管理状态"""

    async def test_f1_registration_precedes_protection(self, strategy, position_cls):
        """R05-F1：登记持仓不得晚于保护单创建（挂保护时持仓已在 self.positions）"""
        s = strategy
        seen = {}

        async def prot(symbol, signal, qty):
            seen['registered'] = symbol in s.positions
            seen['qty'] = s.positions[symbol].initial_quantity if symbol in s.positions else None
            return {'stop': 201, 'tp1': 202, 'tp2': 203}

        s._place_entry_order = AsyncMock(return_value=make_fill())
        s._place_entry_protection_orders = AsyncMock(side_effect=prot)
        result = await s._open_new_position(make_signal())
        assert result is True
        assert seen['registered'] is True
        assert seen['qty'] == Decimal('0.1')
        assert s.positions['BTCUSDT'].protection_pending is False

    async def test_ac1_stop_loss_failure_keeps_position(self, strategy, position_cls):
        """R05-AC1：入场成交、止损单失败 → 返回成功、持仓非空且含真实成交量"""
        s = strategy
        s._place_entry_order = AsyncMock(return_value=make_fill(executed=Decimal('0.1')))
        s._place_entry_protection_orders = AsyncMock(
            return_value={'stop': None, 'tp1': 202, 'tp2': 203})
        s._replenish_protection_round = AsyncMock(return_value=False)
        result = await s._open_new_position(make_signal())
        assert result is True
        assert 'BTCUSDT' in s.positions
        pos = s.positions['BTCUSDT']
        assert pos.initial_quantity == Decimal('0.1')
        assert pos.current_quantity == Decimal('0.1')
        assert pos.protection_pending is True
        assert pos.stop_loss_order_id is None
        # 已成功的保护单 ID 被保留（R05-F3）
        assert pos.tp1_order_id == 202
        assert pos.tp2_order_id == 203
        # 进入补挂流程
        s._replenish_protection_round.assert_awaited_once()

    async def test_ac2_tp1_failure_stop_ok(self, strategy, position_cls):
        """R05-AC2：入场成交、TP1 失败但止损成功 → stop 有值、protection_pending=True"""
        s = strategy
        s._place_entry_order = AsyncMock(return_value=make_fill())
        s._place_entry_protection_orders = AsyncMock(
            return_value={'stop': 201, 'tp1': None, 'tp2': 203})
        s._replenish_protection_round = AsyncMock(return_value=False)
        result = await s._open_new_position(make_signal())
        assert result is True
        pos = s.positions['BTCUSDT']
        assert pos.stop_loss_order_id == 201
        assert pos.tp1_order_id is None
        assert pos.protection_pending is True
        s._replenish_protection_round.assert_awaited_once()

    async def test_ac3_replenish_success_clears_pending(self, strategy, position_cls):
        """R05-AC3：补挂成功 → protection_pending=False、保护单 ID 齐备、计数清零"""
        s = strategy
        pos = make_position(position_cls)
        pos.protection_pending = True
        pos.stop_loss_order_id = None
        pos.tp1_order_id = None
        pos.tp2_order_id = None
        s._load_order_rebuild_params = AsyncMock(return_value=(
            '0.001', Decimal('0.01'), 'SELL',
            {'tp1_close_ratio': 0.3, 'tp2_close_ratio': 0.4},
            Decimal('0.002'), Decimal('0.0015')))
        s._place_stop_loss_order = AsyncMock(return_value=301)
        s._place_missing_tp_level = AsyncMock(side_effect=[302, 303])
        s._notify_protection_incomplete = AsyncMock()
        ok = await s._replenish_protection_round('BTCUSDT', pos, notify_first=True)
        assert ok is True
        assert pos.protection_pending is False
        assert pos.stop_loss_order_id == 301
        assert pos.tp1_order_id == 302
        assert pos.tp2_order_id == 303
        assert pos.protection_retry_count == 0

    async def test_ac4_retry_exhausted_alerts_no_close(self, strategy, position_cls):
        """R05-AC4：补挂达上限 → 告警（on_exhausted=alert），不自动减仓（D2），保持 pending"""
        s = strategy
        pos = make_position(position_cls)
        pos.protection_pending = True
        pos.stop_loss_order_id = None
        pos.tp1_order_id = None
        pos.tp2_order_id = None
        s.positions['BTCUSDT'] = pos
        s._replenish_missing_protection = AsyncMock(return_value=False)
        s._notify_protection_incomplete = AsyncMock()
        s._notify_protection_exhausted = AsyncMock()
        s._close_position = AsyncMock()
        max_retries = s._protection_max_retries
        assert max_retries > 0
        for _ in range(max_retries):
            await s._retry_protection_pending()
        assert pos.protection_retry_count >= max_retries
        s._notify_protection_exhausted.assert_awaited_once()
        s._close_position.assert_not_awaited()
        assert pos.protection_pending is True
        assert 'BTCUSDT' in s.positions
        # 达上限后不再尝试补挂（保持 pending 供重启兜底）
        s._replenish_missing_protection.reset_mock()
        await s._retry_protection_pending()
        s._replenish_missing_protection.assert_not_awaited()

    async def test_f3_protection_failure_not_release_claim(
        self, strategy, position_cls, strategy_module, monkeypatch
    ):
        """R05-F3 + R07：保护失败已建仓时不得误释放占用"""
        s = strategy
        release = AsyncMock(return_value=True)
        monkeypatch.setattr(strategy_module, 'release_claim', release)
        s._place_entry_order = AsyncMock(return_value=make_fill())
        s._place_entry_protection_orders = AsyncMock(
            return_value={'stop': None, 'tp1': None, 'tp2': None})
        s._replenish_protection_round = AsyncMock(return_value=False)
        result = await s._open_new_position(make_signal())
        assert result is True
        assert 'BTCUSDT' in s.positions
        release.assert_not_awaited()

    async def test_startup_recovery_marks_pending(
        self, strategy, position_cls, strategy_module, monkeypatch
    ):
        """R05（重启兜底）：启动恢复补挂后仍不完整 → 标记 protection_pending"""
        s = strategy

        async def fake_owner(db_manager, symbol, **kwargs):
            return s.my_record_name

        monkeypatch.setattr(strategy_module, 'resolve_position_owner', fake_owner)
        s._check_existing_protection = MagicMock(return_value=(False, False, False))
        s._get_current_price = AsyncMock(return_value=Decimal('60000'))
        s._calc_protection_atr = AsyncMock(return_value=Decimal('1000'))
        s._get_protection_precision = AsyncMock(
            return_value=(Decimal('0.01'), Decimal('0.001')))
        s._calc_protection_prices = MagicMock(return_value={})
        s._place_protection_stop = AsyncMock(return_value=None)
        s._place_missing_tp_orders = AsyncMock()
        await s._ensure_symbol_protection(
            {'symbol': 'BTCUSDT', 'positionAmt': '0.1'},
            {'BTCUSDT'}, {}, Decimal('0.002'), Decimal('0.0015'), Decimal('1.5'))
        assert 'BTCUSDT' in s.positions
        assert s.positions['BTCUSDT'].protection_pending is True


# ============================================================================
# R06：入场超时撤单识别部分成交 / 撤单竞态（统一走 wait_order_final_state）
# ============================================================================

class TestR06OrderFill:
    """R06 结构化等待终态，识别部分成交与撤单竞态"""

    async def test_ac1_wait_returns_partial(self, strategy, position_cls):
        """R06-AC1：订单 CANCELED、executedQty=0.4 → 结构化返回「部分成交 0.4」"""
        s = strategy
        canceled = order_snapshot('CANCELED', '0.4', '1')
        s.binance = FakeExchange(before=canceled)
        result = await s._wait_for_order_fill('BTCUSDT', 100, timeout_seconds=5)
        assert result is not None
        assert result.has_fill is True
        assert result.is_filled is False
        assert result.executed_qty == Decimal('0.4')

    async def test_ac1_partial_builds_position_at_executed_qty(self, strategy, position_cls):
        """R06-AC1/F5：部分成交 → 按实际成交量（0.4）建仓并挂保护（而非返回 None）"""
        s = strategy
        s._place_entry_order = AsyncMock(return_value=make_fill(
            order_id=100, executed=Decimal('0.4'), orig=Decimal('1'), status='CANCELED'))
        captured = {}

        async def prot(symbol, signal, qty):
            captured['qty'] = qty
            return {'stop': 201, 'tp1': 202, 'tp2': 203}

        s._place_entry_protection_orders = AsyncMock(side_effect=prot)
        result = await s._open_new_position(make_signal())
        assert result is True
        assert s.positions['BTCUSDT'].initial_quantity == Decimal('0.4')
        assert s.positions['BTCUSDT'].current_quantity == Decimal('0.4')
        # 保护单数量按实际成交量（0.4）计算
        assert captured['qty'] == Decimal('0.4')

    async def test_ac2_timeout_cancel_then_partial(self, strategy, position_cls):
        """R06-AC2：超时撤单后重读 executedQty=0.4 → 按部分成交处理"""
        s = strategy
        fake = FakeExchange(
            before=order_snapshot('NEW', '0', '1'),
            after=order_snapshot('CANCELED', '0.4', '1'))
        s.binance = fake
        result = await s._wait_for_order_fill('BTCUSDT', 100, timeout_seconds=0.05)
        assert result.executed_qty == Decimal('0.4')
        assert result.has_fill is True
        assert fake.cancel_calls == [100]

    async def test_ac3_timeout_cancel_then_unfilled(self, strategy, position_cls):
        """R06-AC3：超时撤单后重读 executedQty=0 → 未成交"""
        s = strategy
        s.binance = FakeExchange(
            before=order_snapshot('NEW', '0', '1'),
            after=order_snapshot('CANCELED', '0', '1'))
        result = await s._wait_for_order_fill('BTCUSDT', 100, timeout_seconds=0.05)
        assert result.has_fill is False
        assert result.executed_qty == Decimal('0')

    async def test_ac3_place_and_wait_returns_none_when_unfilled(self, strategy, position_cls):
        """R06-AC3：无成交时入场方法返回 None（与修复前一致，放弃开仓）"""
        s = strategy
        # 缩短入场等待超时（避免使用配置默认 300s），让超时撤单路径快速触发
        s.risk_config.setdefault('position_sizing', {})['entry_order_timeout_seconds'] = 0.05
        s.binance = FakeExchange(
            before=order_snapshot('NEW', '0', '1'),
            after=order_snapshot('CANCELED', '0', '1'))
        result = await s._place_and_wait_entry_order('BTCUSDT', make_signal())
        assert result is None

    async def test_ac4_cancel_race_2011_treated_as_filled(
        self, strategy, position_cls, error_cls
    ):
        """R06-AC4：撤单抛 -2011（已成交竞态）→ 查单确认成交量并按成交处理"""
        s = strategy
        fake = FakeExchange(
            before=order_snapshot('NEW', '0', '1'),
            after=order_snapshot('FILLED', '1', '1'))
        fake.cancel_error = error_cls(-2011, 'Unknown order sent.')
        s.binance = fake
        result = await s._wait_for_order_fill('BTCUSDT', 100, timeout_seconds=0.05)
        assert result.is_filled is True
        assert result.executed_qty == Decimal('1')

    async def test_ac5_filled_path_unchanged(self, strategy, position_cls):
        """R06-AC5：FILLED 路径行为不变（不回归）"""
        s = strategy
        s.binance = FakeExchange(before=order_snapshot('FILLED', '0.1', '0.1'))
        result = await s._wait_for_order_fill('BTCUSDT', 100, timeout_seconds=5)
        assert result.is_filled is True
        assert result.executed_qty == Decimal('0.1')


# ============================================================================
# R07 / T16：开仓预占互斥与占用生命周期（含 D3 微仓清零）
# ============================================================================

class TestR07OwnershipClaims:
    """R07 占用互斥接入 + D3 微仓清零"""

    async def test_t16_claim_conflict_skips_open_with_alert(
        self, strategy, position_cls, strategy_module, monkeypatch
    ):
        """R07-AC1/AC6：占位冲突 → 跳过开仓 + 告警，不抛异常中断主循环"""
        s = strategy

        async def fake_claim(*args, **kwargs):
            return {'claimed': False, 'owner': 'MTPCS对家策略', 'claim_id': None}

        monkeypatch.setattr(strategy_module, 'try_claim_symbol', fake_claim)
        result = await s._open_new_position(make_signal())
        assert result is False
        assert 'BTCUSDT' not in s.positions
        s.frequency_controller.record_trade.assert_not_awaited()
        s.notification.send.assert_awaited()

    async def test_t16_open_failure_releases_claim(
        self, strategy, position_cls, strategy_module, monkeypatch
    ):
        """R07-F4：开仓失败（入场返回 None）→ 释放占用（reason=open_failed）"""
        s = strategy
        release = AsyncMock(return_value=True)
        monkeypatch.setattr(strategy_module, 'release_claim', release)
        s._place_entry_order = AsyncMock(return_value=None)
        result = await s._open_new_position(make_signal())
        assert result is False
        release.assert_awaited_once()
        assert release.await_args.kwargs.get('reason') == 'open_failed'

    async def test_t16_position_closed_releases_claim(
        self, strategy, position_cls, strategy_module, monkeypatch
    ):
        """R07-F4：平仓归零 → 删除持仓记录并释放占用（reason=position_closed）"""
        s = strategy
        release = AsyncMock(return_value=True)
        monkeypatch.setattr(strategy_module, 'release_claim', release)
        s.positions['BTCUSDT'] = make_position(position_cls, current_quantity=Decimal('0'))
        s._cleanup_position_orders = AsyncMock()
        await s._cleanup_residual_orders()
        assert 'BTCUSDT' not in s.positions
        release.assert_awaited_once()
        assert release.await_args.kwargs.get('reason') == 'position_closed'

    async def test_t16_micro_entry_zeroes_and_releases(
        self, strategy, position_cls, strategy_module, monkeypatch
    ):
        """R06/R07-D3：成交微仓经精度截断为 0 → 清零 + 释放占用 + 不建仓"""
        s = strategy
        release = AsyncMock(return_value=True)
        monkeypatch.setattr(strategy_module, 'release_claim', release)
        s._place_entry_order = AsyncMock(return_value=make_fill(
            order_id=100, executed=Decimal('0.0004'), orig=Decimal('0.0004')))
        s._zero_micro_entry = AsyncMock()
        result = await s._open_new_position(make_signal())
        assert result is False
        assert 'BTCUSDT' not in s.positions
        s._zero_micro_entry.assert_awaited_once()
        release.assert_awaited_once()
        assert release.await_args.kwargs.get('reason') == 'micro_zeroed'

    async def test_t16_cleanup_throttled(
        self, strategy, position_cls, strategy_module, monkeypatch
    ):
        """R07-F7：过期占用清理按 claim_cleanup_interval_minutes 节流（连续调用仅触发一次）"""
        s = strategy
        cleanup = AsyncMock(return_value=2)
        monkeypatch.setattr(strategy_module, 'cleanup_expired_claims', cleanup)
        s._ownership_enabled = True
        s._last_claim_cleanup_time = None
        await s._maybe_cleanup_expired_claims()
        await s._maybe_cleanup_expired_claims()
        cleanup.assert_awaited_once()

    async def test_d3_zero_micro_calls_close_remaining(
        self, strategy, position_cls, strategy_module, monkeypatch
    ):
        """D3：微仓清零走 close_remaining（成功分支）"""
        s = strategy
        close_mock = AsyncMock(return_value=SimpleNamespace(
            success=True, closed_qty=Decimal('0.0004'), reason=None))
        monkeypatch.setattr(strategy_module, 'close_remaining', close_mock)
        entry = make_fill(order_id=100, executed=Decimal('0.0004'), orig=Decimal('0.0004'))
        qty = await s._resolve_entry_quantity('BTCUSDT', entry)
        assert qty is None
        close_mock.assert_awaited_once()

    async def test_d3_zero_micro_failure_alerts(
        self, strategy, position_cls, strategy_module, monkeypatch
    ):
        """D3：微仓清零失败 → 告警（不抛异常，不中断主循环）"""
        s = strategy
        monkeypatch.setattr(strategy_module, 'close_remaining', AsyncMock(
            return_value=SimpleNamespace(success=False, closed_qty=Decimal('0'),
                                         reason='reduce_only_rejected')))
        s._notify_warning = AsyncMock()
        entry = make_fill(order_id=100, executed=Decimal('0.0004'), orig=Decimal('0.0004'))
        qty = await s._resolve_entry_quantity('BTCUSDT', entry)
        assert qty is None
        s._notify_warning.assert_awaited_once()