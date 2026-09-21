"""
MTPCS 策略分批止盈与动态移动止损修复后的单元测试

针对生产 bug（开仓时交易所挂全仓 TP1 止盈条件单，价格触及 TP1 一次性全平，
导致分批止盈与移动止损失效）修复后的核心逻辑验证：

A. _sync_trailing_stop_order：移动止损尾仓数量 + 硬止损全仓单保留（改动5）
B. _check_partial_take_profit：防双重平仓的 TP 级别推断（改动4）
C. _calculate_dynamic_trailing_stop：硬止损 multiplier 按等级读取（改动6）
"""
import sys
import os
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

# 确保项目根目录在 path 中
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

import yaml
from strategies.btc_eth.strategy import BTCEthStrategy, PositionState


def load_config() -> dict:
    """加载策略配置文件"""
    config_path = os.path.join(PROJECT_ROOT, "strategies", "btc_eth", "config.yaml")
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def create_base_strategy(config, mock_binance, mock_kline_service, mock_notification):
    """创建基础策略实例"""
    return BTCEthStrategy(
        config=config,
        binance_client=mock_binance,
        kline_service=mock_kline_service,
        notification_client=mock_notification,
        db_manager=None
    )


def setup_hard_stop_config(strategy, grade, multiplier):
    """注入某信号等级的硬止损 multiplier 与动态利润保护配置

    改动6：硬止损 multiplier 现在从 signal_levels[grade]['stop_loss_atr_multiplier']
    读取（不再写死 1.5），测试按 grade 注入对应值以精确断言等级读取。

    Args:
        strategy: 策略实例
        grade: 信号等级（S/A/B/C）
        multiplier: 该等级的硬止损 ATR 倍数
    """
    strategy.risk_config.setdefault('signal_levels', {})
    strategy.risk_config['signal_levels'].setdefault(grade, {})
    strategy.risk_config['signal_levels'][grade]['stop_loss_atr_multiplier'] = multiplier
    strategy.risk_config['signal_levels'][grade]['dynamic_trailing'] = {
        'enabled': True,
        'activation': {'min_profit_pct': 1.5, 'also_on_tp1': True},
        'regression_tiers': [
            {'profit_ceiling': 1.5, 'retrace_ratio': 0.0},
            {'profit_ceiling': 4.0, 'retrace_ratio': 0.5},
            {'profit_ceiling': 8.0, 'retrace_ratio': 0.35},
            {'profit_ceiling': 999.0, 'retrace_ratio': 0.25}
        ],
        'volatility_adjustment': {'enabled': False}
    }
    strategy._get_volatility_adjustment = AsyncMock(return_value=1.0)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def config():
    return load_config()


@pytest.fixture
def mock_binance():
    return AsyncMock()


@pytest.fixture
def mock_kline_service():
    return AsyncMock()


@pytest.fixture
def mock_notification():
    return AsyncMock()


@pytest.fixture
def strategy(config, mock_binance, mock_kline_service, mock_notification):
    return create_base_strategy(config, mock_binance, mock_kline_service, mock_notification)


# ============================================================================
# A. _sync_trailing_stop_order：尾仓数量 + 硬止损保留
# ============================================================================

class TestSyncTrailingStopOrder:
    """改动5：移动止损单数量改为尾仓 + 硬止损全仓单保留不取消"""

    @pytest.fixture
    def strategy_with_mocks(self, strategy):
        """配置策略并模拟交易所 API"""
        strategy.risk_config['stop_limit_order'] = {'offset_pct': 0.002}
        strategy.binance.cancel_algo_order = AsyncMock(return_value={})
        strategy.binance.place_conditional_order = AsyncMock(return_value={
            'algoId': 12345,
            'orderId': 12345
        })
        strategy._get_symbol_precision = AsyncMock(return_value={
            'tick_size': '0.01',
            'step_size': '0.001'
        })
        strategy._adjust_price_precision = MagicMock(side_effect=lambda x, _: x)
        strategy._adjust_quantity_precision = MagicMock(side_effect=lambda x, _: x)
        return strategy

    @pytest.fixture
    def position_long(self):
        """做多持仓（初始数量 0.1，等级 A）"""
        pos = PositionState()
        pos.entry_price = Decimal('60000')
        pos.direction = 'LONG'
        pos.initial_quantity = Decimal('0.1')
        pos.current_quantity = Decimal('0.1')
        pos.atr = Decimal('600')
        pos.grade = 'A'
        pos.trailing_stop_order_id = None
        return pos

    @pytest.mark.asyncio
    async def test_first_activation_keeps_hard_stop_uses_tail_quantity(
        self, strategy_with_mocks, position_long
    ):
        """A1：首次激活，硬止损保留，移动止损数量 == initial_quantity * 30%"""
        strategy = strategy_with_mocks
        position_long.trailing_stop_order_id = None
        position_long.stop_loss_order_id = 99999  # 硬止损全仓单

        await strategy._sync_trailing_stop_order(
            "BTCUSDT", position_long, Decimal('60600')
        )

        # 硬止损全仓单保留：首次激活无旧移动止损单可取消，且不触碰硬止损单
        strategy.binance.cancel_algo_order.assert_not_called()
        assert position_long.stop_loss_order_id == 99999

        # 创建的是尾仓移动止损条件单（STOP），数量 = 0.1 * 0.30 = 0.03
        strategy.binance.place_conditional_order.assert_called_once()
        args, kwargs = strategy.binance.place_conditional_order.call_args
        assert kwargs['order_type'] == 'STOP'
        assert kwargs['side'] == 'SELL'
        assert kwargs['reduce_only'] is True
        assert kwargs['quantity'] == Decimal('0.03')

        # 记录新移动止损订单 ID
        assert position_long.trailing_stop_order_id == 12345

    @pytest.mark.asyncio
    async def test_existing_trailing_order_cancelled_then_recreated(
        self, strategy_with_mocks, position_long
    ):
        """A2：已有旧移动止损单，先取消旧单再创建新单，数量仍为尾仓 30%"""
        strategy = strategy_with_mocks
        position_long.trailing_stop_order_id = 11111  # 旧移动止损单
        position_long.stop_loss_order_id = 99999  # 硬止损全仓单

        await strategy._sync_trailing_stop_order(
            "BTCUSDT", position_long, Decimal('61200')
        )

        # 仅取消旧移动止损单（11111），不针对硬止损单（99999）
        strategy.binance.cancel_algo_order.assert_called_once_with("BTCUSDT", 11111)
        assert position_long.stop_loss_order_id == 99999

        # 创建新移动止损单，数量仍为尾仓 30%
        strategy.binance.place_conditional_order.assert_called_once()
        args, kwargs = strategy.binance.place_conditional_order.call_args
        assert kwargs['quantity'] == Decimal('0.03')
        assert position_long.trailing_stop_order_id == 12345


# ============================================================================
# B. _check_partial_take_profit：防双重平仓推断
# ============================================================================

class TestCheckPartialTakeProfit:
    """改动4：先同步交易所仓位，再按 current_quantity 缺口推断已触发的 TP 级别"""

    @pytest.fixture
    def position_long(self):
        """做多持仓（初始数量 0.1，等级 A）"""
        pos = PositionState()
        pos.entry_price = Decimal('60000')
        pos.direction = 'LONG'
        pos.initial_quantity = Decimal('0.1')
        pos.current_quantity = Decimal('0.1')
        pos.atr = Decimal('600')
        pos.grade = 'A'
        pos.tp1_hit = False
        pos.tp2_hit = False
        pos.trailing_activated = False
        return pos

    @pytest.mark.asyncio
    async def test_exchange_already_closed_marks_both_tp(self, strategy, position_long):
        """B1：交易所已平仓（closed=True）→ tp1_hit=tp2_hit=True，且不再平仓"""
        with patch.object(
            strategy, '_sync_position_with_exchange',
            AsyncMock(return_value={'closed': True})
        ), patch.object(strategy, '_close_position', AsyncMock()) as mock_close:
            await strategy._check_partial_take_profit(
                "BTCUSDT", position_long, Decimal('50000')
            )

        assert position_long.tp1_hit is True
        assert position_long.tp2_hit is True
        mock_close.assert_not_called()

    @pytest.mark.asyncio
    async def test_infer_both_tp_hit_when_quantity_at_tail(self, strategy, position_long):
        """B2：current 已到 30% 尾仓 → tp1_hit=tp2_hit=True，且不再计算 TP 价格"""
        position_long.current_quantity = Decimal('0.03')  # = initial * 30%

        with patch.object(
            strategy, '_sync_position_with_exchange',
            AsyncMock(return_value={'closed': False})
        ), patch.object(strategy, '_close_position', AsyncMock()) as mock_close, \
             patch.object(strategy, '_calculate_tp_price', MagicMock()) as mock_tp:
            await strategy._check_partial_take_profit(
                "BTCUSDT", position_long, Decimal('50000')
            )

        assert position_long.tp1_hit is True
        assert position_long.tp2_hit is True
        mock_close.assert_not_called()
        mock_tp.assert_not_called()  # 推断命中后不再进入 TP 价格分支

    @pytest.mark.asyncio
    async def test_infer_only_tp1_hit_when_quantity_at_70_percent(
        self, strategy, position_long
    ):
        """B3：current 到 70%（TP1 已平 30%）→ tp1_hit=True, tp2_hit=False"""
        position_long.current_quantity = Decimal('0.07')  # = initial * (1 - 30%)

        # TP2 价格 mock 得极高，做多不触发，仅验证推断分支，不触发后续平仓
        with patch.object(
            strategy, '_sync_position_with_exchange',
            AsyncMock(return_value={'closed': False})
        ), patch.object(strategy, '_close_position', AsyncMock()) as mock_close, \
             patch.object(
                 strategy, '_calculate_tp_price',
                 MagicMock(return_value=Decimal('999999'))
             ):
            await strategy._check_partial_take_profit(
                "BTCUSDT", position_long, Decimal('50000')
            )

        assert position_long.tp1_hit is True
        assert position_long.tp2_hit is False
        mock_close.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_inference_when_full_quantity(self, strategy, position_long):
        """B4：current 仍为全仓 → 推断不误标记（tp1_hit/tp2_hit 保持 False）"""
        position_long.current_quantity = Decimal('0.1')  # = initial（全仓）

        # TP1 价格 mock 得极高，做多不触发，最终保持未触发状态
        with patch.object(
            strategy, '_sync_position_with_exchange',
            AsyncMock(return_value={'closed': False})
        ), patch.object(strategy, '_close_position', AsyncMock()) as mock_close, \
             patch.object(
                 strategy, '_calculate_tp_price',
                 MagicMock(return_value=Decimal('999999'))
             ):
            await strategy._check_partial_take_profit(
                "BTCUSDT", position_long, Decimal('50000')
            )

        assert position_long.tp1_hit is False
        assert position_long.tp2_hit is False
        mock_close.assert_not_called()


# ============================================================================
# C. _calculate_dynamic_trailing_stop：硬止损 multiplier 等级读取
# ============================================================================

class TestDynamicTrailingHardStopMultiplier:
    """改动6：硬止损 multiplier 从 signal_levels[grade]['stop_loss_atr_multiplier'] 读取"""

    @pytest.fixture
    def position_long(self):
        """做多持仓（负 ATR：让硬止损成为 max 的胜者，从而隔离并精确断言 multiplier）"""
        pos = PositionState()
        pos.entry_price = Decimal('60000')
        pos.direction = 'LONG'
        pos.initial_quantity = Decimal('0.1')
        pos.current_quantity = Decimal('0.1')
        pos.atr = Decimal('-600')  # 负 ATR：做多硬止损=entry-(-600)*mult，位于入场价之上
        pos.highest_price = None
        pos.grade = 'A'
        pos.tp1_hit = False
        pos.trailing_activated = True
        pos.trailing_stop_price = None
        return pos

    @pytest.mark.asyncio
    async def test_grade_a_uses_correct_multiplier(self, strategy, position_long):
        """C1：grade=A → 硬止损 = entry - atr * 1.8"""
        setup_hard_stop_config(strategy, 'A', 1.8)
        position_long.grade = 'A'

        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('60000')
        )

        # 做多：60000 - (-600) * 1.8 = 61080
        assert result == Decimal('61080')

    @pytest.mark.asyncio
    async def test_grade_b_uses_correct_multiplier(self, strategy, position_long):
        """C2：grade=B → 硬止损 = entry - atr * 1.3"""
        setup_hard_stop_config(strategy, 'B', 1.3)
        position_long.grade = 'B'

        result = await strategy._calculate_dynamic_trailing_stop(
            "BTCUSDT", position_long, Decimal('60000')
        )

        # 做多：60000 - (-600) * 1.3 = 60780
        assert result == Decimal('60780')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])