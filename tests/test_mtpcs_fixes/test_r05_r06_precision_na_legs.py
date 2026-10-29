"""精度修复 + N/A 保护腿 回归测试（MTPCS 原版 btc_eth + 激进版 btc_eth_aggressive）

本文件只覆盖「精度统一走 _get_precision_params + TP/尾仓数量取整为 0 的 N/A 腿语义」
修复项，且**两策略参数化**（同一断言对两版本各跑一遍）：

  精度修复
    - _get_precision_params 从驼峰键字典（stepSize/tickSize）取真实值
    - _get_protection_precision 返回顺序为 (tick_size, step_size)，防顺序写反
    - _sync_trailing_stop_order（移动止损同步）：尾仓数量经步长取整为 0 时早退不打单
    - _sync_trailing_stop_order 正常场景：按真实步长取整后下单（initial=1、step=0.01
      → 下单数量 0.3）

  N/A 保护腿（数量取整为 0 视为不可挂）
    - _rebuild_condition_orders：TP 数量为 0 → 标记 N/A + 跳过该腿 + 整体仍返回 True
    - protection_complete / missing_protection_legs：N/A 集合内的 TP 腿视为就位、不计缺失
    - place_missing_tp_level（shared）：数量为 0 → 返回 None + 首次告警一次、后续静默
    - merge_position（shared）：加仓合并后 N/A 集合清空（总量变大后可能重新可挂）

测试只使用假 client / mock，绝不触碰真实网络、交易所与数据库。
"""
import importlib
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# 确保项目根目录在 path 中
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

import pytest
import yaml

from shared import protection_retry as protection_retry_module

# 两版本参数化：模块路径 + 配置文件相对路径
STRATEGY_MODULES = [
    ('strategies.btc_eth.strategy', 'strategies/btc_eth/config.yaml'),
    ('strategies.btc_eth_aggressive.strategy',
     'strategies/btc_eth_aggressive/config.yaml'),
]


# ============================================================================
# 测试辅助构造器
# ============================================================================

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


def mock_grade_risk(strategy, remaining_ratio=0.3):
    """打桩 _get_grade_risk，仅提供移动止损数量计算所需的 remaining_ratio"""
    strategy._get_grade_risk = MagicMock(return_value={
        'partial_take_profit': {'remaining_ratio': remaining_ratio},
    })


def mock_rebuild_params(strategy, step_size='0.01', tp1_ratio=0.3, tp2_ratio=0.4):
    """打桩 _load_order_rebuild_params（_rebuild_condition_orders 的参数来源）"""
    strategy._load_order_rebuild_params = AsyncMock(return_value=(
        step_size, Decimal('0.01'), 'SELL',
        {'tp1_close_ratio': tp1_ratio, 'tp2_close_ratio': tp2_ratio,
         'remaining_ratio': 0.3},
        Decimal('0.002'), Decimal('0.0015')))


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
def position_cls(strategy_ctx):
    """当前参数化版本的 PositionState 类"""
    return strategy_ctx.position_cls


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
    s.binance.cancel_algo_order = AsyncMock()
    s.notification.send = AsyncMock()
    return s


# ============================================================================
# 场景 1/2：精度参数读取（驼峰键 + 返回顺序）
# ============================================================================

class TestPrecisionParams:
    """_get_precision_params / _get_protection_precision 精度修复回归"""

    async def test_get_precision_params_reads_camel_case_keys(self, strategy):
        """场景1：_get_precision_params 从驼峰键字典取真实值（mock _get_symbol_precision）"""
        s = strategy
        s._get_symbol_precision = AsyncMock(return_value={
            'stepSize': '0.01',
            'tickSize': Decimal('0.0001'),
        })
        step_size, tick_size = await s._get_precision_params('BTCUSDT')
        assert step_size == '0.01'
        assert tick_size == Decimal('0.0001')
        s._get_symbol_precision.assert_awaited_once_with('BTCUSDT')

    async def test_get_protection_precision_returns_tick_then_step(self, strategy):
        """场景2：_get_protection_precision 返回顺序为 (tick, step)（防顺序写反）"""
        s = strategy
        # step 与 tick 取不同值，若顺序写反则断言必然失败
        s._get_precision_params = AsyncMock(
            return_value=('0.001', Decimal('0.0001')))
        tick_size, step_size = await s._get_protection_precision('BTCUSDT')
        assert tick_size == Decimal('0.0001')
        assert step_size == Decimal('0.001')


# ============================================================================
# 场景 3/4：移动止损同步（尾仓数量取整为 0 早退 / 正常按步长取整）
# ============================================================================

class TestSyncTrailingStopOrder:
    """_sync_trailing_stop_order 尾仓数量精度修复回归"""

    async def test_zero_close_quantity_skips_order(self, strategy, position_cls):
        """场景3：initial=0.03、remaining_ratio=0.3、step=0.01 → 0.009 取整为 0 →
        不得调用 _place_conditional_order_and_record，且不抛异常"""
        s = strategy
        pos = make_position(position_cls,
                            initial_quantity=Decimal('0.03'),
                            current_quantity=Decimal('0.03'))
        s._get_precision_params = AsyncMock(return_value=('0.01', Decimal('0.01')))
        mock_grade_risk(s, remaining_ratio=0.3)
        s._place_conditional_order_and_record = AsyncMock()

        await s._sync_trailing_stop_order('BTCUSDT', pos, Decimal('50000'))

        s._place_conditional_order_and_record.assert_not_awaited()
        assert pos.trailing_stop_order_id is None

    async def test_normal_close_quantity_rounded_by_step(self, strategy, position_cls):
        """场景4：initial=1、remaining_ratio=0.3、step=0.01 → 下单数量为 0.3
        （验证精度修复后按真实步长取整，而非旧的下划线键默认值）"""
        s = strategy
        pos = make_position(position_cls,
                            initial_quantity=Decimal('1'),
                            current_quantity=Decimal('1'))
        s._get_precision_params = AsyncMock(return_value=('0.01', Decimal('0.01')))
        mock_grade_risk(s, remaining_ratio=0.3)
        s._place_conditional_order_and_record = AsyncMock(return_value=777)

        await s._sync_trailing_stop_order('BTCUSDT', pos, Decimal('50000'))

        s._place_conditional_order_and_record.assert_awaited_once()
        # 第 6 个位置参数为 close_quantity
        close_qty = s._place_conditional_order_and_record.await_args.args[5]
        assert close_qty == Decimal('0.3')
        assert pos.trailing_stop_order_id == 777


# ============================================================================
# 场景 5：加仓重建 TP 腿数量取整为 0 → 标记 N/A + 跳过 + 仍返回 True
# ============================================================================

class TestRebuildConditionOrdersTpNaLegs:
    """_rebuild_condition_orders TP 数量为 0 时的 N/A 腿语义"""

    async def test_tp1_zero_leg_marked_na_but_rebuild_succeeds(
        self, strategy, position_cls
    ):
        """场景5a：TP1 数量取整为 0 → 标记 N/A、不调用该腿下单、整体仍返回 True；
        TP2 数量正常（0.012→0.01）照常下单"""
        s = strategy
        pos = make_position(position_cls,
                            initial_quantity=Decimal('0.03'),
                            current_quantity=Decimal('0.03'))
        mock_rebuild_params(s, step_size='0.01')
        s._rebuild_stop_loss = AsyncMock(return_value=True)
        s._rebuild_trailing_stop = AsyncMock(return_value=True)
        s._place_tp_order = AsyncMock(return_value=900)

        ok = await s._rebuild_condition_orders('BTCUSDT', pos)

        assert ok is True
        assert pos.tp1_order_id is None
        assert 'TP1' in pos.not_placeable_legs
        # TP2 正常：0.03 × 0.4 = 0.012 → 步长 0.01 取整为 0.01
        assert pos.tp2_order_id == 900
        s._place_tp_order.assert_awaited_once()
        assert s._place_tp_order.await_args.args[4] == Decimal('0.01')

    async def test_both_tp_zero_legs_marked_na_no_order(
        self, strategy, position_cls
    ):
        """场景5b（边界）：两腿数量均取整为 0（initial=0.003）→ 两腿均标记 N/A、
        _place_tp_order 完全不被调用、整体仍返回 True（不被 TP 缺失阻断）"""
        s = strategy
        pos = make_position(position_cls,
                            initial_quantity=Decimal('0.003'),
                            current_quantity=Decimal('0.003'))
        mock_rebuild_params(s, step_size='0.01')
        s._rebuild_stop_loss = AsyncMock(return_value=True)
        s._rebuild_trailing_stop = AsyncMock(return_value=True)
        s._place_tp_order = AsyncMock(return_value=901)

        ok = await s._rebuild_condition_orders('BTCUSDT', pos)

        assert ok is True
        assert pos.tp1_order_id is None
        assert pos.tp2_order_id is None
        assert pos.not_placeable_legs == {'TP1', 'TP2'}
        s._place_tp_order.assert_not_awaited()

    async def test_rebuild_remaining_protection_tp2_zero_marks_na(
        self, strategy, position_cls
    ):
        """场景5c：平尾仓后补挂剩余保护时 TP2 数量取整为 0 → 标记 N/A、不下单；
        硬止损按剩余持仓正常补挂"""
        s = strategy
        pos = make_position(position_cls,
                            initial_quantity=Decimal('0.02'),
                            current_quantity=Decimal('0.01'),
                            tp1_hit=True)
        mock_rebuild_params(s, step_size='0.01')
        s._place_stop_loss_order = AsyncMock(return_value=800)
        s._place_tp_order = AsyncMock(return_value=902)

        await s._rebuild_remaining_protection('BTCUSDT', pos)

        # TP2：0.02 × 0.4 = 0.008 → 步长 0.01 取整为 0 → 标记 N/A 且不发单
        assert 'TP2' in pos.not_placeable_legs
        assert pos.tp2_order_id is None
        s._place_tp_order.assert_not_awaited()
        # 硬止损仍全量保护：剩余 0.01 正常补挂
        assert pos.stop_loss_order_id == 800


def test_restart_single_unlabelled_tp_must_not_be_assumed_tp1(strategy):
    """重启回归闸：只有一张未标等级的 TP 单时，不得凭列表位置归属 TP1。"""
    has_stop, has_tp1, has_tp2 = strategy._check_existing_protection(
        'BTCUSDT', None,
        {'BTCUSDT': {'STOP_LOSS': [800], 'TAKE_PROFIT': [900]}},
    )
    assert has_stop
    assert not has_tp1 and not has_tp2


def test_restart_labelled_tp2_is_not_assumed_tp1(strategy):
    has_stop, has_tp1, has_tp2 = strategy._check_existing_protection(
        'BTCUSDT', None,
        {'BTCUSDT': {'STOP_LOSS': [800], 'TAKE_PROFIT': [
            {'algo_id': 900, 'tp_level': 2},
        ]}},
    )
    assert has_stop and not has_tp1 and has_tp2


@pytest.mark.asyncio
async def test_tp2_order_records_level(strategy, strategy_ctx, monkeypatch):
    recorder = AsyncMock()
    monkeypatch.setattr(strategy_ctx.module, 'record_condition_order', recorder)
    strategy.db_manager = object()
    strategy.binance.place_conditional_order = AsyncMock(return_value={'algoId': 900})
    order_id = await strategy._place_tp_order(
        'BTCUSDT', 'SELL', Decimal('110'), Decimal('109'), Decimal('0.01'),
        'TP2', strategy.strategy_name,
    )
    assert order_id == 900
    assert recorder.await_args.kwargs['tp_level'] == 2


@pytest.mark.asyncio
async def test_restart_loader_keeps_tp_level(strategy, strategy_ctx, monkeypatch):
    strategy.db_manager = object()
    loader = AsyncMock(return_value=[
        {'symbol': 'BTCUSDT', 'order_type': 'TAKE_PROFIT', 'algo_id': 900, 'tp_level': 2},
    ])
    monkeypatch.setattr(strategy_ctx.module, 'get_open_orders', loader)
    orders = await strategy._load_existing_open_orders()
    assert orders['BTCUSDT']['TAKE_PROFIT'] == [{'algo_id': 900, 'tp_level': 2}]


@pytest.mark.asyncio
async def test_restart_only_tp2_marks_tp1_na_without_duplicate(strategy, strategy_ctx, monkeypatch):
    s = strategy
    symbol = 'BTCUSDT'
    original_entry_time = datetime.now() - timedelta(hours=8)
    s.db_manager = AsyncMock()
    s.db_manager.fetch_one.return_value = {'state_data': {'positions': {symbol: {
        'entry_time': original_entry_time.isoformat(), 'direction': 'LONG',
        'tp2_order_id': 900,
    }}}}
    async def owned(*args, **kwargs):
        return s.my_record_name
    monkeypatch.setattr(strategy_ctx.module, 'resolve_position_owner', owned)
    s._get_current_price = AsyncMock(return_value=Decimal('91'))
    s._calc_protection_atr = AsyncMock(return_value=Decimal('1'))
    s._get_protection_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.01')))
    s._calc_protection_prices = MagicMock(return_value={
        'tp1_quantity': Decimal('0'), 'tp2_quantity': Decimal('0.01'),
    })
    s._place_protection_stop = AsyncMock()
    s._place_tp_protection_order = AsyncMock()
    await s._ensure_symbol_protection(
        {'symbol': symbol, 'positionAmt': '0.03', 'entryPrice': '90'}, {symbol},
        {symbol: {'STOP_LOSS': [800], 'TAKE_PROFIT': [{'algo_id': 900, 'tp_level': 2}]}},
        Decimal('0.002'), Decimal('0.0015'), Decimal('1.5'),
    )
    pos = s.positions[symbol]
    assert pos.stop_loss_order_id == 800
    assert pos.entry_price == Decimal('90')
    assert pos.entry_time == original_entry_time
    assert pos.tp2_order_id == 900
    assert 'TP1' in pos.not_placeable_legs
    assert not pos.protection_pending
    s._place_tp_protection_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_only_tp2_without_matching_snapshot_does_not_reset_clock(
    strategy, strategy_ctx, monkeypatch,
):
    s = strategy
    symbol = 'BTCUSDT'
    s.db_manager = AsyncMock()
    s.db_manager.fetch_one.return_value = {'state_data': {'positions': {symbol: {
        'entry_time': (datetime.now() - timedelta(hours=8)).isoformat(),
        'direction': 'LONG', 'tp2_order_id': 901,
    }}}}
    monkeypatch.setattr(strategy_ctx.module, 'resolve_position_owner',
                        AsyncMock(return_value=s.my_record_name))
    s._get_current_price = AsyncMock(return_value=Decimal('91'))
    s._calc_protection_atr = AsyncMock(return_value=Decimal('1'))
    s._get_protection_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.01')))
    s._calc_protection_prices = MagicMock(return_value={
        'tp1_quantity': Decimal('0'), 'tp2_quantity': Decimal('0.01'),
    })
    s._place_tp_protection_order = AsyncMock()

    await s._ensure_symbol_protection(
        {'symbol': symbol, 'positionAmt': '0.03', 'entryPrice': '90'}, {symbol},
        {symbol: {'STOP_LOSS': [800], 'TAKE_PROFIT': [{'algo_id': 900, 'tp_level': 2}]}},
        Decimal('0.002'), Decimal('0.0015'), Decimal('1.5'),
    )

    assert symbol not in s.positions
    s._place_tp_protection_order.assert_not_awaited()
    s.notification.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('has_tp1', [False, True])
async def test_missing_stop_keeps_retry_even_without_matching_snapshot(
    strategy, strategy_ctx, monkeypatch, has_tp1,
):
    s = strategy
    symbol = 'BTCUSDT'
    s.db_manager = AsyncMock()
    s.db_manager.fetch_one.return_value = None
    monkeypatch.setattr(strategy_ctx.module, 'resolve_position_owner',
                        AsyncMock(return_value=s.my_record_name))
    s._get_current_price = AsyncMock(return_value=Decimal('91'))
    s._calc_protection_atr = AsyncMock(return_value=Decimal('1'))
    s._get_protection_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.01')))
    s._calc_protection_prices = MagicMock(return_value={
        'tp1_quantity': Decimal('0'), 'tp2_quantity': Decimal('0.01'),
    })
    s._place_protection_stop = AsyncMock(return_value=None)
    s._place_tp_protection_order = AsyncMock()
    tp_orders = [{'algo_id': 900, 'tp_level': 2}]
    if has_tp1:
        tp_orders.insert(0, {'algo_id': 899, 'tp_level': 1})

    await s._ensure_symbol_protection(
        {'symbol': symbol, 'positionAmt': '0.03', 'entryPrice': '90'}, {symbol},
        {symbol: {'TAKE_PROFIT': tp_orders}},
        Decimal('0.002'), Decimal('0.0015'), Decimal('1.5'),
    )

    s._place_protection_stop.assert_awaited_once()
    if has_tp1:
        assert symbol in s.positions  # 两张 TP 均在挂，不属于仅 TP2 的新闸
        assert s.positions[symbol].protection_pending
    else:
        assert symbol not in s.positions  # 无可信时间，不接管到软件平仓流程
        assert symbol in s._untracked_stop_retry_symbols
        assert symbol in s._unverified_entry_time_symbols
        s.notification.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('pending_stop', [False, True])
async def test_unverified_position_rechecked_without_adopting_position(strategy, pending_stop):
    s = strategy
    s._unverified_entry_time_symbols.add('BTCUSDT')
    if pending_stop:
        s._untracked_stop_retry_symbols.add('BTCUSDT')
    s._retry_pending_cancellations = AsyncMock()
    s._cleanup_residual_orders = AsyncMock()
    s._retry_rebuild_pending = AsyncMock()
    s._retry_protection_pending = AsyncMock()
    s._ensure_position_protection = AsyncMock()
    s._maybe_cleanup_expired_claims = AsyncMock()

    await s.update_positions()

    assert 'BTCUSDT' not in s.positions
    s._ensure_position_protection.assert_awaited_once()


def test_unverified_entry_time_preserves_previous_state_snapshot(strategy):
    assert strategy.state_snapshot_safe_to_write
    strategy._unverified_entry_time_symbols.add('BTCUSDT')
    assert not strategy.state_snapshot_safe_to_write


@pytest.mark.asyncio
async def test_closed_untracked_position_clears_stop_retry_and_snapshot_hold(strategy):
    strategy._untracked_stop_retry_symbols.add('BTCUSDT')
    strategy._unverified_entry_time_symbols.add('BTCUSDT')
    strategy.binance.get_position = AsyncMock(return_value=[])

    await strategy._ensure_position_protection()

    assert not strategy._untracked_stop_retry_symbols
    assert strategy.state_snapshot_safe_to_write


@pytest.mark.asyncio
@pytest.mark.parametrize('has_stop', [False, True])
async def test_unverified_position_does_not_adopt_when_tp2_disappears(
    strategy, strategy_ctx, monkeypatch, has_stop,
):
    s = strategy
    symbol = 'BTCUSDT'
    s._unverified_entry_time_symbols.add(symbol)
    monkeypatch.setattr(strategy_ctx.module, 'resolve_position_owner',
                        AsyncMock(return_value=s.my_record_name))
    s._get_current_price = AsyncMock(return_value=Decimal('91'))
    s._calc_protection_atr = AsyncMock(return_value=Decimal('1'))
    s._get_protection_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.01')))
    s._calc_protection_prices = MagicMock(return_value={
        'tp1_quantity': Decimal('0'), 'tp2_quantity': Decimal('0.01'),
    })
    s._place_protection_stop = AsyncMock(return_value=None)
    s._place_tp_protection_order = AsyncMock()
    orders = {'STOP_LOSS': [800]} if has_stop else {}

    await s._ensure_symbol_protection(
        {'symbol': symbol, 'positionAmt': '0.03', 'entryPrice': '90'}, {symbol},
        {symbol: orders}, Decimal('0.002'), Decimal('0.0015'), Decimal('1.5'),
    )

    assert symbol not in s.positions
    assert symbol in s._unverified_entry_time_symbols
    assert (symbol in s._untracked_stop_retry_symbols) is not has_stop
    s._place_tp_protection_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_tp1_na_does_not_immediately_software_close_on_rebuilt_atr(
    strategy, strategy_ctx, monkeypatch,
):
    s = strategy
    symbol = 'BTCUSDT'
    async def owned(*args, **kwargs):
        return s.my_record_name
    monkeypatch.setattr(strategy_ctx.module, 'resolve_position_owner', owned)
    s._get_current_price = AsyncMock(return_value=Decimal('100'))
    s._calc_protection_atr = AsyncMock(return_value=Decimal('1'))
    s._get_protection_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.01')))
    s._calc_protection_prices = MagicMock(return_value={
        'tp1_quantity': Decimal('0'), 'tp2_quantity': Decimal('0.01'),
    })
    s._calculate_tp_price = MagicMock(return_value=Decimal('95'))
    s._place_tp_protection_order = AsyncMock()
    await s._ensure_symbol_protection(
        {'symbol': symbol, 'positionAmt': '0.03', 'entryPrice': '90'}, {symbol},
        {symbol: {'STOP_LOSS': [800], 'TAKE_PROFIT': [{'algo_id': 900, 'tp_level': 2}]}},
        Decimal('0.002'), Decimal('0.0015'), Decimal('1.5'),
    )
    assert symbol not in s.positions
    s._place_tp_protection_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_legacy_unknown_tp_never_places_second_tp(strategy, strategy_ctx, monkeypatch):
    s = strategy
    symbol = 'BTCUSDT'
    async def owned(*args, **kwargs):
        return s.my_record_name
    monkeypatch.setattr(strategy_ctx.module, 'resolve_position_owner', owned)
    s._get_current_price = AsyncMock(return_value=Decimal('100'))
    s._calc_protection_atr = AsyncMock(return_value=Decimal('1'))
    s._get_protection_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.01')))
    s._calc_protection_prices = MagicMock(return_value={
        'tp1_quantity': Decimal('0'), 'tp2_quantity': Decimal('0.01'),
    })
    s._place_protection_stop = AsyncMock()
    s._place_tp_protection_order = AsyncMock()
    await s._ensure_symbol_protection(
        {'symbol': symbol, 'positionAmt': '0.03'}, {symbol},
        {symbol: {'STOP_LOSS': [800], 'TAKE_PROFIT': [900]}},
        Decimal('0.002'), Decimal('0.0015'), Decimal('1.5'),
    )
    s._place_tp_protection_order.assert_not_awaited()
    assert symbol not in s.positions


@pytest.mark.asyncio
async def test_legacy_unknown_tp_without_stop_alerts_if_stop_rebuild_fails(
    strategy, strategy_ctx, monkeypatch,
):
    s = strategy
    symbol = 'BTCUSDT'
    async def owned(*args, **kwargs):
        return s.my_record_name
    monkeypatch.setattr(strategy_ctx.module, 'resolve_position_owner', owned)
    s._get_current_price = AsyncMock(return_value=Decimal('100'))
    s._calc_protection_atr = AsyncMock(return_value=Decimal('1'))
    s._get_protection_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.01')))
    s._calc_protection_prices = MagicMock(return_value={})
    s._place_protection_stop = AsyncMock(return_value=None)
    s._place_tp_protection_order = AsyncMock()
    await s._ensure_symbol_protection(
        {'symbol': symbol, 'positionAmt': '0.03'}, {symbol},
        {symbol: {'TAKE_PROFIT': [900]}},
        Decimal('0.002'), Decimal('0.0015'), Decimal('1.5'),
    )
    s._place_protection_stop.assert_awaited_once()
    s._place_tp_protection_order.assert_not_awaited()
    s.notification.send.assert_awaited_once()
    assert symbol not in s.positions


# ============================================================================
# 场景 6：保护完整性纯逻辑（N/A 腿视为就位 / 不计缺失）
# ============================================================================

class TestProtectionCompleteNaLegs:
    """shared.protection_retry 的 protection_complete / missing_protection_legs"""

    def make_leg_position(self, position_cls, npl):
        """stop 已就位、TP1/TP2 均缺单，N/A 集合由参数给定"""
        return make_position(
            position_cls,
            stop_loss_order_id=1,
            tp1_order_id=None,
            tp2_order_id=None,
            not_placeable_legs=set(npl),
        )

    def test_complete_when_tps_in_na_set(self, position_cls):
        """场景6a：stop 有 ID、tp1/tp2 均在 N/A 集合 → 视为就位（True）、缺失列表为空"""
        pos = self.make_leg_position(position_cls, npl={'TP1', 'TP2'})
        assert protection_retry_module.protection_complete(pos) is True
        assert protection_retry_module.missing_protection_legs(pos) == []

    def test_missing_when_na_set_empty(self, position_cls):
        """场景6b（边界）：N/A 集合为空、TP 单缺失 → 不完整且缺失列表含 TP1/TP2"""
        pos = self.make_leg_position(position_cls, npl=set())
        assert protection_retry_module.protection_complete(pos) is False
        assert protection_retry_module.missing_protection_legs(pos) == ['TP1', 'TP2']

    def test_missing_partial_na_only_reports_other_leg(self, position_cls):
        """场景6c（边界）：N/A 集合仅含 TP1 → 仍不完整，缺失列表只报 TP2"""
        pos = self.make_leg_position(position_cls, npl={'TP1'})
        assert protection_retry_module.protection_complete(pos) is False
        assert protection_retry_module.missing_protection_legs(pos) == ['TP2']


# ============================================================================
# 场景 7：补挂单腿数量为 0 → 返回 None + 首次告警一次、后续静默
# ============================================================================

class TestPlaceMissingTpLevelZeroQty:
    """shared.protection_retry.place_missing_tp_level 零数量 N/A 语义"""

    async def test_first_warn_then_silent_and_added_to_na(
        self, strategy, position_cls, monkeypatch
    ):
        """场景7：qty 取整为 0 → 返回 None、集合新增 TP{level}；
        连续调用两次时第二次不再输出 warning（警告次数 == 1）"""
        s = strategy
        s._calculate_tp_price = MagicMock(return_value=Decimal('61000'))
        s._place_tp_order = AsyncMock()
        # 用假 logger 替换模块级 structlog logger（structlog 走 stdout，caplog 捕获不到）
        fake_logger = MagicMock()
        monkeypatch.setattr(protection_retry_module, 'logger', fake_logger)

        pos = make_position(position_cls,
                            initial_quantity=Decimal('0.03'),
                            current_quantity=Decimal('0.03'))
        assert 'TP1' not in pos.not_placeable_legs

        # 首次调用：告警一次 + 加入 N/A 集合
        first = await protection_retry_module.place_missing_tp_level(
            s, 'BTCUSDT', pos, 1, '0.01', Decimal('0.01'), 'SELL',
            {'tp1_close_ratio': 0.3}, Decimal('0.0015'))
        assert first is None
        assert 'TP1' in pos.not_placeable_legs
        assert fake_logger.warning.call_count == 1
        s._place_tp_order.assert_not_awaited()

        # 第二次调用：静默（不再告警），集合保持
        second = await protection_retry_module.place_missing_tp_level(
            s, 'BTCUSDT', pos, 1, '0.01', Decimal('0.01'), 'SELL',
            {'tp1_close_ratio': 0.3}, Decimal('0.0015'))
        assert second is None
        assert fake_logger.warning.call_count == 1
        s._place_tp_order.assert_not_awaited()


# ============================================================================
# 场景 8：加仓合并后 N/A 集合清空
# ============================================================================

class TestMergePositionClearsNaLegs:
    """shared.protection_retry.merge_position 末尾清空 N/A 集合"""

    def test_merge_position_clears_not_placeable_legs(self, position_cls):
        """场景8（边界）：合并前 N/A 含 TP1 → 调用后被清空（总量变大可能重新可挂）"""
        pos = make_position(position_cls,
                            initial_quantity=Decimal('0.1'),
                            current_quantity=Decimal('0.1'),
                            not_placeable_legs={'TP1'})
        assert pos.not_placeable_legs == {'TP1'}
        signal = {'entry_price': Decimal('61000'), 'atr': Decimal('1000'),
                  'grade': 'A'}
        entry = SimpleNamespace(order_id=100, avg_price=Decimal('60500'))

        protection_retry_module.merge_position(pos, signal, entry, Decimal('0.4'))

        assert pos.not_placeable_legs == set()
        # 佐证合并真实执行（防空断言）：总量更新为交易所实际总量
        assert pos.initial_quantity == Decimal('0.4')
        assert pos.current_quantity == Decimal('0.4')
