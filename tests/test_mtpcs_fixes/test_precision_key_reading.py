"""两版 MTPCS 的交易所精度键名与移动止损零数量回归测试。

验证真实过滤器键 stepSize/tickSize 经统一助手传入保护单和移动止损计算。
测试使用假 client，不触碰交易所或数据库。
"""
import importlib
import asyncio
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
    created_loop = None
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        # Python 3.9 在前序 asyncio 用例清理 loop 后，策略构造中的 Lock 仍需 loop。
        created_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(created_loop)
    s = strategy_ctx.strategy_cls(
        config=strategy_ctx.config,
        binance_client=AsyncMock(),
        kline_service=AsyncMock(),
        notification_client=AsyncMock(),
        db_manager=None,
    )
    s.binance.cancel_algo_order = AsyncMock()
    s.notification.send = AsyncMock()
    yield s
    if created_loop is not None:
        asyncio.set_event_loop(None)
        created_loop.close()


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

    async def test_api_failure_blocks_precision_then_recovers(self, strategy):
        """取数失败不得返回猜测精度；下次可重查交易所真实过滤器。"""
        s = strategy
        real = {
            'quantityPrecision': 2,
            'pricePrecision': 4,
            'stepSize': '0.01',
            'tickSize': Decimal('0.0001'),
        }
        s.binance.get_symbol_info = AsyncMock(side_effect=[RuntimeError('timeout'), real])

        with pytest.raises(RuntimeError, match='timeout'):
            await s._get_symbol_precision('BNBUSDT')
        assert 'BNBUSDT' not in s.symbol_precision

        assert await s._get_symbol_precision('BNBUSDT') == real
        assert await s._get_symbol_precision('BNBUSDT') == real
        assert s.binance.get_symbol_info.await_count == 2

    async def test_precision_params_do_not_guess_after_api_failure(self, strategy):
        strategy.binance.get_symbol_info = AsyncMock(side_effect=RuntimeError('timeout'))
        with pytest.raises(RuntimeError, match='timeout'):
            await strategy._get_precision_params('BNBUSDT')

    async def test_close_keeps_existing_orders_when_precision_unavailable(self, strategy, position_cls):
        position = make_position(position_cls)
        strategy._sync_position_with_exchange = AsyncMock(return_value={'closed': False})
        strategy._cancel_symbol_conditional_orders = AsyncMock()
        strategy.binance.get_symbol_info = AsyncMock(side_effect=RuntimeError('timeout'))

        assert await strategy._close_position('BNBUSDT', position, Decimal('0.1'), 'TIME_STOP') is False
        strategy._cancel_symbol_conditional_orders.assert_not_awaited()

    async def test_rebuild_keeps_existing_orders_when_precision_unavailable(self, strategy, position_cls):
        position = make_position(position_cls)
        position.stop_loss_order_id = 123
        strategy._cancel_single_order_with_retry = AsyncMock(return_value=True)
        strategy.binance.get_symbol_info = AsyncMock(side_effect=RuntimeError('timeout'))

        with pytest.raises(RuntimeError, match='timeout'):
            await strategy._cancel_orders_for_rebuild('BNBUSDT', position)
        strategy._cancel_single_order_with_retry.assert_not_awaited()

    async def test_trailing_update_keeps_old_order_when_precision_unavailable(self, strategy, position_cls):
        position = make_position(position_cls)
        position.trailing_stop_order_id = 456
        strategy.binance.get_symbol_info = AsyncMock(side_effect=RuntimeError('timeout'))
        strategy.binance.cancel_algo_order = AsyncMock()

        with pytest.raises(RuntimeError, match='timeout'):
            await strategy._sync_trailing_stop_order('BNBUSDT', position, Decimal('59000'))
        strategy.binance.cancel_algo_order.assert_not_awaited()
        assert position.trailing_stop_order_id == 456

    async def test_trailing_trigger_keeps_old_order_when_precision_unavailable(self, strategy, position_cls):
        position = make_position(position_cls)
        position.trailing_stop_order_id = 456
        strategy._cancel_trailing_order = AsyncMock()
        strategy.binance.get_symbol_info = AsyncMock(side_effect=RuntimeError('timeout'))

        with pytest.raises(RuntimeError, match='timeout'):
            await strategy._handle_trailing_trigger('BNBUSDT', position, Decimal('59000'), Decimal('58000'))
        strategy._cancel_trailing_order.assert_not_awaited()

    async def test_extreme_stop_keeps_old_order_when_precision_unavailable(self, strategy, position_cls):
        position = make_position(position_cls)
        position.stop_loss_order_id = 789
        strategy.binance.get_symbol_info = AsyncMock(side_effect=RuntimeError('timeout'))
        strategy.binance.cancel_algo_order = AsyncMock()
        strategy.binance.place_conditional_order = AsyncMock()

        with pytest.raises(RuntimeError, match='timeout'):
            await strategy._tighten_extreme_stop('BNBUSDT', position, Decimal('59000'))
        strategy.binance.cancel_algo_order.assert_not_awaited()
        strategy.binance.place_conditional_order.assert_not_awaited()


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
