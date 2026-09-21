"""
MTPCS v6.28 加仓统一托管 + 移动止损尾仓修复功能测试

覆盖两版本（原版 btc_eth + 激进版 btc_eth_aggressive，pytest 参数化），10 组核心分支：

  1. execute_signal 顶层加仓分派（5 分支）
  2. _open_new_position 新开仓成功/失败路径
  3. _place_tp_order / _place_stop_loss_order 公共下单函数
  4. _add_position 加仓五步顺序与回滚
  5. _merge_position 加权均价合并
  6. _rebuild_condition_orders 4 类条件单重建
  7. _handle_trailing_trigger 平尾仓（核心修复：平仓量 = initial×remaining_ratio）
  8. _sync_position_with_exchange 加仓分支与各状态同步
  9. _retry_rebuild_pending 重建待收敛机制
 10. 异常路径

测试只 mock 外部依赖（binance/kline/notification/db），不触碰真实交易所与数据库。
"""
import importlib
import os
import sys
from decimal import Decimal
from unittest.mock import AsyncMock

# 确保项目根目录在 path 中
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

import pytest
import yaml

# 两版本参数化：模块路径 + 配置文件相对路径（元组作为单个值，配合 ids 显示用例名）
STRATEGY_MODULES = [
    ('strategies.btc_eth.strategy', 'strategies/btc_eth/config.yaml'),
    ('strategies.btc_eth_aggressive.strategy',
     'strategies/btc_eth_aggressive/config.yaml'),
]


def make_signal(**overrides) -> dict:
    """构造标准测试信号（各字段可通过关键字覆盖）"""
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
    """构造测试持仓状态（默认 LONG 0.1@60000、A 级，可覆盖）"""
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


# ============================================================================
# Fixtures（两版本参数化）
# ============================================================================

@pytest.fixture(params=STRATEGY_MODULES, ids=['btc_eth', 'btc_eth_aggressive'])
def strategy_builder(request):
    """参数化加载两版本策略模块与配置，返回 (策略类, PositionState类, 配置)"""
    module_name, config_relpath = request.param
    module = importlib.import_module(module_name)
    config_path = os.path.join(PROJECT_ROOT, config_relpath)
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    return module.BTCEthStrategy, module.PositionState, config


@pytest.fixture
def strategy(strategy_builder):
    """构造策略实例并注入测试 mock（不触碰真实交易所/数据库）"""
    strategy_cls, _, config = strategy_builder
    s = strategy_cls(
        config=config,
        binance_client=AsyncMock(),
        kline_service=AsyncMock(),
        notification_client=AsyncMock(),
        db_manager=None,
    )
    # 预置精度缓存，避免真实查询交易所
    s.symbol_precision['BTCUSDT'] = {
        'stepSize': '0.001',
        'tickSize': Decimal('0.01'),
        'quantityPrecision': 3,
        'pricePrecision': 2,
    }
    # 屏蔽与本次测试无关的外部依赖
    s.binance.place_order = AsyncMock(return_value={'orderId': 100, 'status': 'FILLED'})
    s._check_entry_limits = AsyncMock(return_value=True)
    s._check_total_margin_ratio = AsyncMock(return_value=True)
    s._wait_for_order_fill = AsyncMock(return_value={'orderId': 100, 'status': 'FILLED'})
    s.frequency_controller.record_trade = AsyncMock()
    s._send_signal_error_notification = AsyncMock()
    return s


@pytest.fixture
def position_cls(strategy_builder):
    """返回当前参数化版本的 PositionState 类"""
    return strategy_builder[1]


# ============================================================================
# 第 1 组：execute_signal 顶层加仓分派（5 分支）
# ============================================================================

class TestExecuteSignalDispatch:
    """execute_signal 顶层加仓分派（v6.28 五分支）"""

    async def test_no_position_opens_new(self, strategy, position_cls):
        """分支①：无持仓 → 走新开仓路径"""
        s = strategy
        s._open_new_position = AsyncMock(return_value=True)
        result = await s.execute_signal(make_signal())
        assert result is True
        s._open_new_position.assert_awaited_once()

    async def test_flat_position_opens_new(self, strategy, position_cls):
        """分支①：持仓已清仓（current<=0）→ 走新开仓路径"""
        s = strategy
        pos = make_position(position_cls, current_quantity=Decimal('0'))
        s.positions['BTCUSDT'] = pos
        s._open_new_position = AsyncMock(return_value=True)
        result = await s.execute_signal(make_signal())
        assert result is True
        s._open_new_position.assert_awaited_once()

    async def test_reverse_direction_rejects(self, strategy, position_cls):
        """分支②：反向信号拒绝加仓"""
        s = strategy
        pos = make_position(position_cls, direction='LONG')
        s.positions['BTCUSDT'] = pos
        s._add_position = AsyncMock(return_value=True)
        result = await s.execute_signal(make_signal(direction='SHORT'))
        assert result is False
        s._add_position.assert_not_awaited()

    async def test_tp1_hit_rejects(self, strategy, position_cls):
        """分支③：已触发 TP1 → 拒绝加仓"""
        s = strategy
        pos = make_position(position_cls, tp1_hit=True)
        s.positions['BTCUSDT'] = pos
        s._add_position = AsyncMock(return_value=True)
        result = await s.execute_signal(make_signal())
        assert result is False
        s._add_position.assert_not_awaited()

    async def test_tp2_hit_rejects(self, strategy, position_cls):
        """分支③：已触发 TP2 → 拒绝加仓"""
        s = strategy
        pos = make_position(position_cls, tp2_hit=True)
        s.positions['BTCUSDT'] = pos
        s._add_position = AsyncMock(return_value=True)
        result = await s.execute_signal(make_signal())
        assert result is False
        s._add_position.assert_not_awaited()

    async def test_unprofitable_rejects(self, strategy, position_cls):
        """分支④：持仓浮亏 → 拒绝加仓"""
        s = strategy
        s.positions['BTCUSDT'] = make_position(position_cls)
        s._is_position_profitable = AsyncMock(return_value=False)
        s._add_position = AsyncMock(return_value=True)
        result = await s.execute_signal(make_signal())
        assert result is False
        s._add_position.assert_not_awaited()

    async def test_profitable_adds_position(self, strategy, position_cls):
        """分支⑤：浮盈同向 → 加仓统一托管"""
        s = strategy
        s.positions['BTCUSDT'] = make_position(position_cls)
        s._is_position_profitable = AsyncMock(return_value=True)
        s._add_position = AsyncMock(return_value=True)
        result = await s.execute_signal(make_signal())
        assert result is True
        s._add_position.assert_awaited_once()


# ============================================================================
# 第 2 组：_open_new_position 新开仓成功/失败路径
# ============================================================================

class TestOpenNewPosition:
    """_open_new_position 新开仓主流程成功/失败路径"""

    async def test_success_builds_position(self, strategy, position_cls):
        """成功：记录频率 → 开仓 → 保护单 → 构建持仓状态"""
        s = strategy
        s._place_entry_order = AsyncMock(return_value={'orderId': 100, 'status': 'FILLED'})
        s._place_entry_protection_orders = AsyncMock(
            return_value=({'stop': 201, 'tp1': 202, 'tp2': 203}, True))
        result = await s._open_new_position(make_signal())
        assert result is True
        s.frequency_controller.record_trade.assert_awaited_once()
        pos = s.positions['BTCUSDT']
        assert pos.entry_price == Decimal('60000')
        assert pos.initial_quantity == Decimal('0.1')
        assert pos.entry_order_id == 100
        assert pos.stop_loss_order_id == 201
        assert pos.tp1_order_id == 202
        assert pos.tp2_order_id == 203
        assert pos.grade == 'A'

    async def test_entry_order_none_fails(self, strategy, position_cls):
        """失败：入场下单返回 None → 返回 False"""
        s = strategy
        s._place_entry_order = AsyncMock(return_value=None)
        result = await s._open_new_position(make_signal())
        assert result is False
        assert 'BTCUSDT' not in s.positions

    async def test_protection_orders_fail_aborts(self, strategy, position_cls):
        """失败：保护单任一失败 → 终止开仓"""
        s = strategy
        s._place_entry_order = AsyncMock(return_value={'orderId': 100, 'status': 'FILLED'})
        s._place_entry_protection_orders = AsyncMock(return_value=(None, False))
        result = await s._open_new_position(make_signal())
        assert result is False
        assert 'BTCUSDT' not in s.positions

    async def test_exception_sends_error_and_fails(self, strategy, position_cls):
        """异常：入场下单抛异常 → 发错误通知并返回 False"""
        s = strategy
        s._place_entry_order = AsyncMock(side_effect=RuntimeError('下单失败'))
        result = await s._open_new_position(make_signal())
        assert result is False
        s._send_signal_error_notification.assert_awaited_once()
        assert 'BTCUSDT' not in s.positions

    async def test_place_entry_order_success(self, strategy, position_cls):
        """_place_entry_order：设置杠杆 + 检查通过 + 限价单成交"""
        s = strategy
        result = await s._place_entry_order('BTCUSDT', make_signal())
        assert result == {'orderId': 100, 'status': 'FILLED'}
        s.binance.set_leverage.assert_awaited_once_with('BTCUSDT', 5)
        s._check_entry_limits.assert_awaited_once()

    async def test_place_entry_order_leverage_fail(self, strategy, position_cls):
        """_place_entry_order：设置杠杆失败 → 返回 None"""
        s = strategy
        s.binance.set_leverage = AsyncMock(side_effect=RuntimeError('杠杆设置失败'))
        result = await s._place_entry_order('BTCUSDT', make_signal())
        assert result is None

    async def test_place_entry_order_limits_reject(self, strategy, position_cls):
        """_place_entry_order：开仓前检查未通过 → 返回 None"""
        s = strategy
        s._check_entry_limits = AsyncMock(return_value=False)
        result = await s._place_entry_order('BTCUSDT', make_signal())
        assert result is None


# ============================================================================
# 第 3 组：_place_tp_order / _place_stop_loss_order 公共下单函数
# ============================================================================

class TestPlaceTPAndStopLossOrders:
    """_place_tp_order / _place_stop_loss_order 公共下单函数（v6.28 消除重复）"""

    async def test_place_tp_order_success(self, strategy, position_cls):
        """TP 止盈单成功：走 TAKE_PROFIT 条件单"""
        s = strategy
        s.binance.place_conditional_order = AsyncMock(return_value={'algoId': 3001})
        result = await s._place_tp_order(
            'BTCUSDT', 'SELL', Decimal('64000'), Decimal('63904'),
            Decimal('0.03'), 'TP1', 'btc_eth')
        assert result == 3001
        s.binance.place_conditional_order.assert_awaited_once_with(
            symbol='BTCUSDT', side='SELL', stop_price=Decimal('64000'),
            price=Decimal('63904'), quantity=Decimal('0.03'),
            order_type='TAKE_PROFIT', reduce_only=True)

    async def test_place_tp_order_failure(self, strategy, position_cls):
        """TP 止盈单失败：条件单异常 → 返回 None"""
        s = strategy
        s.binance.place_conditional_order = AsyncMock(
            side_effect=RuntimeError('下单失败'))
        result = await s._place_tp_order(
            'BTCUSDT', 'SELL', Decimal('64000'), Decimal('63904'),
            Decimal('0.03'), 'TP1', 'btc_eth')
        assert result is None

    async def test_place_stop_loss_success(self, strategy, position_cls):
        """硬止损单成功：触发价 = entry - ATR×multiplier（按等级配置），走 STOP 条件单"""
        s = strategy
        s.binance.place_conditional_order = AsyncMock(return_value={'algoId': 3002})
        pos = make_position(position_cls)
        # 从配置动态读取 A 级 multiplier，兼容两版本差异（btc_eth=1.5、aggressive=1.8）
        grade_risk = s._get_grade_risk('A')
        mult = Decimal(str(grade_risk['stop_loss_atr_multiplier']))
        expected_stop = Decimal('60000') - Decimal('1000') * mult
        expected_limit = s._adjust_price_precision(
            s._apply_limit_offset(expected_stop, Decimal('0.002'), 'LONG'), Decimal('0.01'))
        result = await s._place_stop_loss_order(
            'BTCUSDT', pos, Decimal('0.1'), Decimal('0.01'), 'SELL', Decimal('0.002'))
        assert result == 3002
        s.binance.place_conditional_order.assert_awaited_once_with(
            symbol='BTCUSDT', side='SELL', stop_price=expected_stop,
            price=expected_limit, quantity=Decimal('0.1'),
            order_type='STOP', reduce_only=True)


# ============================================================================
# 第 4 组：_add_position 加仓五步顺序与回滚
# ============================================================================

class TestAddPosition:
    """_add_position 加仓五步：开仓→同步→取消旧单→合并→重建（v6.28）"""

    async def test_success_five_steps_in_order(self, strategy, position_cls):
        """加仓五步全部成功 → True，且顺序正确、加权均价生效"""
        s = strategy
        pos = make_position(position_cls)
        signal = make_signal(entry_price=Decimal('61000'), atr=Decimal('1200'), grade='S')
        order_log = []

        async def fake_place_entry(symbol, sig):
            order_log.append('place_entry')
            return {'orderId': 100, 'status': 'FILLED'}

        async def fake_sync(symbol, position, close_reason):
            order_log.append('sync')
            return {'closed': False, 'partially_closed': False,
                    'actual_quantity': Decimal('0.2')}

        async def fake_cancel(symbol, position):
            order_log.append('cancel')
            return True

        async def fake_rebuild(symbol, position):
            order_log.append('rebuild')
            return True

        s._place_entry_order = AsyncMock(side_effect=fake_place_entry)
        s._sync_position_with_exchange = AsyncMock(side_effect=fake_sync)
        s._cancel_orders_for_rebuild = AsyncMock(side_effect=fake_cancel)
        s._rebuild_condition_orders = AsyncMock(side_effect=fake_rebuild)

        result = await s._add_position('BTCUSDT', pos, signal)
        assert result is True
        assert order_log == ['place_entry', 'sync', 'cancel', 'rebuild']
        # 加权均价：(60000×0.1 + 61000×0.1) / 0.2 = 60500
        assert pos.entry_price == Decimal('60500')
        assert pos.current_quantity == Decimal('0.2')
        assert pos.initial_quantity == Decimal('0.2')
        assert pos.atr == Decimal('1200')
        assert pos.grade == 'S'
        assert pos.rebuild_pending is False

    async def test_sync_closed_rolls_back(self, strategy, position_cls):
        """同步发现交易所已平仓 → 回滚加仓，不取消旧单不合并"""
        s = strategy
        pos = make_position(position_cls)
        s._place_entry_order = AsyncMock(return_value={'orderId': 100, 'status': 'FILLED'})
        s._sync_position_with_exchange = AsyncMock(
            return_value={'closed': True, 'partially_closed': False,
                          'actual_quantity': Decimal('0')})
        s._cancel_orders_for_rebuild = AsyncMock(return_value=True)
        result = await s._add_position('BTCUSDT', pos, make_signal())
        assert result is False
        s._cancel_orders_for_rebuild.assert_not_awaited()
        assert pos.rebuild_pending is False

    async def test_sync_none_rolls_back(self, strategy, position_cls):
        """同步异常（actual_quantity=None）→ 回滚加仓"""
        s = strategy
        pos = make_position(position_cls)
        s._place_entry_order = AsyncMock(return_value={'orderId': 100, 'status': 'FILLED'})
        s._sync_position_with_exchange = AsyncMock(
            return_value={'closed': False, 'partially_closed': False,
                          'actual_quantity': None})
        result = await s._add_position('BTCUSDT', pos, make_signal())
        assert result is False

    async def test_cancel_fail_marks_pending(self, strategy, position_cls):
        """取消旧条件单失败 → 标记 rebuild_pending 并返回 False"""
        s = strategy
        pos = make_position(position_cls)
        s._place_entry_order = AsyncMock(return_value={'orderId': 100, 'status': 'FILLED'})
        s._sync_position_with_exchange = AsyncMock(
            return_value={'closed': False, 'partially_closed': False,
                          'actual_quantity': Decimal('0.2')})
        s._cancel_orders_for_rebuild = AsyncMock(return_value=False)
        result = await s._add_position('BTCUSDT', pos, make_signal())
        assert result is False
        assert pos.rebuild_pending is True

    async def test_rebuild_fail_marks_pending(self, strategy, position_cls):
        """重建条件单失败 → 标记 rebuild_pending 并返回 False"""
        s = strategy
        pos = make_position(position_cls)
        s._place_entry_order = AsyncMock(return_value={'orderId': 100, 'status': 'FILLED'})
        s._sync_position_with_exchange = AsyncMock(
            return_value={'closed': False, 'partially_closed': False,
                          'actual_quantity': Decimal('0.2')})
        s._cancel_orders_for_rebuild = AsyncMock(return_value=True)
        s._rebuild_condition_orders = AsyncMock(return_value=False)
        result = await s._add_position('BTCUSDT', pos, make_signal())
        assert result is False
        assert pos.rebuild_pending is True


# ============================================================================
# 第 5 组：_merge_position 加权均价合并
# ============================================================================

class TestMergePosition:
    """_merge_position 加仓合并（加权均价，原地修改，v6.28）"""

    async def test_weighted_average_price(self, strategy, position_cls):
        """加权均价：(60000×0.1 + 61000×0.1) / 0.2 = 60500"""
        pos = make_position(position_cls)
        signal = make_signal(entry_price=Decimal('61000'))
        strategy._merge_position(pos, signal, {'orderId': 999}, Decimal('0.2'))
        assert pos.entry_price == Decimal('60500')

    async def test_no_added_keeps_entry(self, strategy, position_cls):
        """无实际增量（added<=0）→ 入场价保持不变"""
        pos = make_position(position_cls)
        signal = make_signal(entry_price=Decimal('61000'))
        strategy._merge_position(pos, signal, {'orderId': 999}, Decimal('0.1'))
        assert pos.entry_price == Decimal('60000')

    async def test_old_zero_uses_new_entry(self, strategy, position_cls):
        """旧持仓为 0 且出现增量 → 入场价取新信号价"""
        pos = make_position(position_cls, initial_quantity=Decimal('0'),
                            current_quantity=Decimal('0'))
        signal = make_signal(entry_price=Decimal('61000'))
        strategy._merge_position(pos, signal, {'orderId': 999}, Decimal('0.1'))
        assert pos.entry_price == Decimal('61000')

    async def test_updates_quantities_and_metadata(self, strategy, position_cls):
        """更新 initial/current 数量、ATR、等级、入场订单ID"""
        pos = make_position(position_cls)
        signal = make_signal(entry_price=Decimal('61000'), atr=Decimal('1200'), grade='S')
        strategy._merge_position(pos, signal, {'orderId': 999}, Decimal('0.2'))
        assert pos.initial_quantity == Decimal('0.2')
        assert pos.current_quantity == Decimal('0.2')
        assert pos.atr == Decimal('1200')
        assert pos.grade == 'S'
        assert pos.entry_order_id == 999


# ============================================================================
# 第 6 组：_rebuild_condition_orders 4 类条件单重建
# ============================================================================

class TestRebuildConditionOrders:
    """_rebuild_condition_orders 重建 4 类条件单（硬止损/TP1/TP2/移动止损，v6.28）"""

    async def test_success_rebuilds_all_types(self, strategy, position_cls):
        """成功：硬止损 + TP1 + TP2 重建，移动止损未激活跳过"""
        s = strategy
        s.binance.place_conditional_order = AsyncMock(side_effect=[
            {'algoId': 101}, {'algoId': 102}, {'algoId': 103}])
        pos = make_position(position_cls)
        result = await s._rebuild_condition_orders('BTCUSDT', pos)
        assert result is True
        assert pos.stop_loss_order_id == 101
        assert pos.tp1_order_id == 102
        assert pos.tp2_order_id == 103
        assert s.binance.place_conditional_order.await_count == 3

    async def test_success_with_trailing_activated(self, strategy, position_cls):
        """成功：移动止损已激活 → 第 4 类条件单（尾仓 initial×remaining_ratio）"""
        s = strategy
        s.binance.place_conditional_order = AsyncMock(side_effect=[
            {'algoId': 101}, {'algoId': 102}, {'algoId': 103}, {'algoId': 104}])
        pos = make_position(position_cls, trailing_activated=True,
                            trailing_stop_price=Decimal('62000'))
        result = await s._rebuild_condition_orders('BTCUSDT', pos)
        assert result is True
        assert pos.trailing_stop_order_id == 104
        assert s.binance.place_conditional_order.await_count == 4

    async def test_stop_loss_fail_returns_false(self, strategy, position_cls):
        """硬止损下单失败 → 重建失败"""
        s = strategy
        s.binance.place_conditional_order = AsyncMock(side_effect=[None])
        pos = make_position(position_cls)
        result = await s._rebuild_condition_orders('BTCUSDT', pos)
        assert result is False

    async def test_tp1_fail_returns_false(self, strategy, position_cls):
        """TP1 下单失败 → 重建失败"""
        s = strategy
        s.binance.place_conditional_order = AsyncMock(side_effect=[
            {'algoId': 101}, None])
        pos = make_position(position_cls)
        result = await s._rebuild_condition_orders('BTCUSDT', pos)
        assert result is False

    async def test_trailing_fail_returns_false(self, strategy, position_cls):
        """移动止损下单失败 → 重建失败"""
        s = strategy
        s.binance.place_conditional_order = AsyncMock(side_effect=[
            {'algoId': 101}, {'algoId': 102}, {'algoId': 103}, None])
        pos = make_position(position_cls, trailing_activated=True,
                            trailing_stop_price=Decimal('62000'))
        result = await s._rebuild_condition_orders('BTCUSDT', pos)
        assert result is False


# ============================================================================
# 第 7 组：_handle_trailing_trigger 平尾仓（核心修复）
# ============================================================================

class TestHandleTrailingTrigger:
    """_handle_trailing_trigger 峰值回落保护：平尾仓量 = initial×remaining_ratio"""

    async def test_close_uses_initial_times_ratio(self, strategy, position_cls):
        """核心修复：平仓量为 initial×remaining_ratio（0.1×0.3=0.03），而非全平剩余"""
        s = strategy
        pos = make_position(position_cls, current_quantity=Decimal('0.07'))
        s._cancel_trailing_order = AsyncMock()
        s._close_position = AsyncMock(return_value=True)
        s._rebuild_remaining_protection = AsyncMock()
        await s._handle_trailing_trigger('BTCUSDT', pos, Decimal('60000'), Decimal('59500'))
        s._cancel_trailing_order.assert_awaited_once_with('BTCUSDT', pos)
        s._close_position.assert_awaited_once_with(
            symbol='BTCUSDT', position=pos, close_quantity=Decimal('0.03'),
            close_reason='TRAILING_STOP', current_price=Decimal('60000'))
        s._rebuild_remaining_protection.assert_awaited_once_with('BTCUSDT', pos)

    async def test_close_capped_by_current(self, strategy, position_cls):
        """限幅：尾仓量超过当前剩余持仓 → 按当前剩余量平仓，防止超卖"""
        s = strategy
        pos = make_position(position_cls, current_quantity=Decimal('0.02'))
        s._cancel_trailing_order = AsyncMock()
        s._close_position = AsyncMock(return_value=True)
        s._rebuild_remaining_protection = AsyncMock()
        await s._handle_trailing_trigger('BTCUSDT', pos, Decimal('60000'), Decimal('59500'))
        s._close_position.assert_awaited_once_with(
            symbol='BTCUSDT', position=pos, close_quantity=Decimal('0.02'),
            close_reason='TRAILING_STOP', current_price=Decimal('60000'))

    async def test_zero_precision_falls_back_to_current(self, strategy, position_cls):
        """兜底：尾仓量精度调整后为 0 → 平全部剩余"""
        s = strategy
        s.symbol_precision['BTCUSDT']['stepSize'] = '0.1'
        pos = make_position(position_cls, current_quantity=Decimal('0.07'))
        s._cancel_trailing_order = AsyncMock()
        s._close_position = AsyncMock(return_value=True)
        s._rebuild_remaining_protection = AsyncMock()
        await s._handle_trailing_trigger('BTCUSDT', pos, Decimal('60000'), Decimal('59500'))
        s._close_position.assert_awaited_once_with(
            symbol='BTCUSDT', position=pos, close_quantity=Decimal('0.07'),
            close_reason='TRAILING_STOP', current_price=Decimal('60000'))


# ============================================================================
# 第 8 组：_sync_position_with_exchange 加仓分支与各状态同步
# ============================================================================

class TestSyncPositionWithExchange:
    """_sync_position_with_exchange 加仓分支与各状态同步（v6.28）"""

    async def test_add_position_branch(self, strategy, position_cls):
        """加仓分支：持仓量增加 → 仅更新 current_quantity，保留 initial 供加权均价"""
        s = strategy
        pos = make_position(position_cls)
        s.binance.get_position = AsyncMock(
            return_value=[{'symbol': 'BTCUSDT', 'positionAmt': '0.15'}])
        result = await s._sync_position_with_exchange('BTCUSDT', pos, 'ADD_POSITION')
        assert result == {'closed': False, 'partially_closed': False,
                          'actual_quantity': Decimal('0.15')}
        assert pos.current_quantity == Decimal('0.15')
        assert pos.initial_quantity == Decimal('0.1')
        assert pos.direction == 'LONG'

    async def test_no_change_normal(self, strategy, position_cls):
        """交易所持仓量不变 → 正常状态"""
        s = strategy
        pos = make_position(position_cls)
        s.binance.get_position = AsyncMock(
            return_value=[{'symbol': 'BTCUSDT', 'positionAmt': '0.1'}])
        result = await s._sync_position_with_exchange('BTCUSDT', pos, '')
        assert result == {'closed': False, 'partially_closed': False,
                          'actual_quantity': Decimal('0.1')}

    async def test_empty_list_marks_closed(self, strategy, position_cls):
        """空列表（PM 账户已平仓）→ closed=True 且本地置零"""
        s = strategy
        pos = make_position(position_cls)
        s.binance.get_position = AsyncMock(return_value=[])
        result = await s._sync_position_with_exchange('BTCUSDT', pos, '')
        assert result['closed'] is True
        assert pos.current_quantity == Decimal('0')
        assert pos.direction == 'FLAT'

    async def test_pos_amt_zero_marks_closed(self, strategy, position_cls):
        """显式 posAmt≈0 → closed=True"""
        s = strategy
        pos = make_position(position_cls)
        s.binance.get_position = AsyncMock(
            return_value=[{'symbol': 'BTCUSDT', 'positionAmt': '0.0000001'}])
        result = await s._sync_position_with_exchange('BTCUSDT', pos, '')
        assert result['closed'] is True
        assert pos.current_quantity == Decimal('0')

    async def test_partial_close_detected(self, strategy, position_cls):
        """持仓量减少 → partially_closed=True 并同步数量"""
        s = strategy
        pos = make_position(position_cls)
        s.binance.get_position = AsyncMock(
            return_value=[{'symbol': 'BTCUSDT', 'positionAmt': '0.06'}])
        result = await s._sync_position_with_exchange('BTCUSDT', pos, '')
        assert result['partially_closed'] is True
        assert result['actual_quantity'] == Decimal('0.06')
        assert pos.current_quantity == Decimal('0.06')

    async def test_exception_returns_none_actual(self, strategy, position_cls):
        """查询异常 → actual_quantity=None，继续使用本地数据"""
        s = strategy
        pos = make_position(position_cls)
        s.binance.get_position = AsyncMock(side_effect=RuntimeError('网络异常'))
        result = await s._sync_position_with_exchange('BTCUSDT', pos, '')
        assert result == {'closed': False, 'partially_closed': False,
                          'actual_quantity': None}
        assert pos.current_quantity == Decimal('0.1')


# ============================================================================
# 第 9 组：_retry_rebuild_pending 重建待收敛机制
# ============================================================================

class TestRetryRebuildPending:
    """_retry_rebuild_pending 加仓重建收敛机制（v6.28）"""

    async def test_no_pending_skipped(self, strategy, position_cls):
        """无重建待收敛持仓 → 不处理"""
        s = strategy
        s.positions['BTCUSDT'] = make_position(position_cls)
        s._cancel_orders_for_rebuild = AsyncMock(return_value=True)
        s._rebuild_condition_orders = AsyncMock(return_value=True)
        await s._retry_rebuild_pending()
        s._cancel_orders_for_rebuild.assert_not_awaited()
        s._rebuild_condition_orders.assert_not_awaited()

    async def test_flat_clears_flag(self, strategy, position_cls):
        """已平仓持仓 → 直接清除标记，不重建"""
        s = strategy
        pos = make_position(position_cls, current_quantity=Decimal('0'),
                            rebuild_pending=True)
        s.positions['BTCUSDT'] = pos
        s._cancel_orders_for_rebuild = AsyncMock(return_value=True)
        await s._retry_rebuild_pending()
        assert pos.rebuild_pending is False
        s._cancel_orders_for_rebuild.assert_not_awaited()

    async def test_cancel_fail_keeps_flag(self, strategy, position_cls):
        """取消旧单失败 → 保留标记，下轮重试"""
        s = strategy
        pos = make_position(position_cls, rebuild_pending=True)
        s.positions['BTCUSDT'] = pos
        s._cancel_orders_for_rebuild = AsyncMock(return_value=False)
        s._rebuild_condition_orders = AsyncMock(return_value=True)
        await s._retry_rebuild_pending()
        assert pos.rebuild_pending is True
        s._rebuild_condition_orders.assert_not_awaited()

    async def test_rebuild_fail_keeps_flag(self, strategy, position_cls):
        """重建失败 → 保留标记，下轮重试"""
        s = strategy
        pos = make_position(position_cls, rebuild_pending=True)
        s.positions['BTCUSDT'] = pos
        s._cancel_orders_for_rebuild = AsyncMock(return_value=True)
        s._rebuild_condition_orders = AsyncMock(return_value=False)
        await s._retry_rebuild_pending()
        assert pos.rebuild_pending is True

    async def test_success_clears_flag(self, strategy, position_cls):
        """取消+重建均成功 → 清除标记"""
        s = strategy
        pos = make_position(position_cls, rebuild_pending=True)
        s.positions['BTCUSDT'] = pos
        s._cancel_orders_for_rebuild = AsyncMock(return_value=True)
        s._rebuild_condition_orders = AsyncMock(return_value=True)
        await s._retry_rebuild_pending()
        assert pos.rebuild_pending is False
        s._cancel_orders_for_rebuild.assert_awaited_once_with('BTCUSDT', pos)
        s._rebuild_condition_orders.assert_awaited_once_with('BTCUSDT', pos)


# ============================================================================
# 第 10 组：异常路径
# ============================================================================

class TestExceptionPaths:
    """v6.28 关键异常路径：全部降级为失败返回，不静默失败"""

    async def test_add_position_exception(self, strategy, position_cls):
        """加仓主流程内部异常 → 返回 False"""
        s = strategy
        pos = make_position(position_cls)
        s._place_entry_order = AsyncMock(side_effect=RuntimeError('内部异常'))
        result = await s._add_position('BTCUSDT', pos, make_signal())
        assert result is False

    async def test_rebuild_condition_orders_exception(self, strategy, position_cls):
        """重建条件单读取参数异常 → 返回 False"""
        s = strategy
        pos = make_position(position_cls)
        s._load_order_rebuild_params = AsyncMock(side_effect=RuntimeError('参数读取异常'))
        result = await s._rebuild_condition_orders('BTCUSDT', pos)
        assert result is False

    async def test_place_conditional_order_exception(self, strategy, position_cls):
        """条件单下单抛异常 → 返回 None（调用方终止/跳过）"""
        s = strategy
        s.binance.place_conditional_order = AsyncMock(side_effect=RuntimeError('下单异常'))
        result = await s._place_conditional_order_and_record(
            'BTCUSDT', 'SELL', 'STOP', Decimal('58500'), Decimal('58383.00'),
            Decimal('0.1'), 'STOP_LOSS', 'btc_eth')
        assert result is None

    async def test_get_precision_params_exception(self, strategy, position_cls):
        """精度查询异常 → 返回默认精度，不中断重建"""
        s = strategy
        s._get_symbol_precision = AsyncMock(side_effect=RuntimeError('精度查询异常'))
        step_size, tick_size = await s._get_precision_params('BTCUSDT')
        assert step_size == '0.001'
        assert tick_size == Decimal('0.01')
