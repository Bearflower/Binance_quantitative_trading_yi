"""
新币做空策略止盈成交检测 - executor 方法测试

测试目标：
1. detect_take_profit_fills() — 通过对比持仓数量变化检测止盈单成交
2. update_target_status() — 更新目标达成状态
3. clear_position_tracking() — 幂等清理持仓跟踪与上次跟踪数量
4. _get_exchange_position_qty() — 获取交易所实际做空持仓数量
"""
import sys
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

from strategies.new_coin.executor import TradingExecutor


# ============================================================================
# 辅助函数
# ============================================================================

def create_base_config() -> dict:
    """创建基础配置（含止盈成交检测配置）"""
    return {
        'trading': {
            'leverage': 2,
            'max_positions': 3,
            'single_position_margin': 50,
            'stop_loss_percent': 0.05,
            'take_profit_percent': 0.10,
            'limit_order_slippage': 0.001,
            'batch_take_profit': {
                'enabled': True,
                'target1_atr_multiplier': 1.5,
                'target1_close_percent': 0.30,
                'target2_atr_multiplier': 3.5,
                'target2_close_percent': 0.40,
                'trailing_stop_atr_multiplier': 1.5,
            },
            'position_detection': {
                'enabled': True,
                'qty_tolerance_ratio': 0.01,
                'qty_tolerance_absolute': 0.0001,
                'zero_qty_threshold': 0.0001,
            },
            'atr_stop': {
                'multiplier': 2.5,
            },
            'dynamic_trailing': {
                'enabled': True,
                'activation': {
                    'min_profit_pct': 1.5,
                    'also_on_tp1': False,
                    'also_on_tp2': True,
                },
                'regression_tiers': [
                    {'profit_ceiling': 1.5, 'retrace_ratio': 0.0},
                    {'profit_ceiling': 4.0, 'retrace_ratio': 0.5},
                    {'profit_ceiling': 8.0, 'retrace_ratio': 0.35},
                    {'profit_ceiling': 999.0, 'retrace_ratio': 0.25},
                ],
                'volatility_adjustment': {
                    'enabled': False,
                },
                'stop_limit_order': {
                    'offset_pct': 0.002,
                },
                'cleanup_silent_error_codes': [-2022, -2011],
            },
            'close_position': {
                'close_percent': 1.0,
            },
            'default_precision': {
                'tick_size': 0.01,
                'step_size': 0.001,
            },
        },
    }


def _deep_merge(base: dict, override: dict) -> None:
    """深度合并字典"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def create_executor(config_override: dict = None) -> TradingExecutor:
    """创建 TradingExecutor 实例（所有外部服务使用 mock）"""
    config = create_base_config()
    if config_override:
        _deep_merge(config, config_override)

    mock_binance = MagicMock()
    mock_binance._request = AsyncMock()
    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_notification = MagicMock()
    mock_notification.send = AsyncMock()
    mock_kline = MagicMock()

    return TradingExecutor(
        binance_api=mock_binance,
        db=mock_db,
        notification=mock_notification,
        config=config,
        kline_service=mock_kline,
    )


def setup_position_tracking(executor, symbol: str, **overrides) -> dict:
    """初始化 position_tracking 并设置默认值"""
    tracking = {
        'entry_price': 100.0,
        'entry_time': datetime.now(timezone.utc),
        'entry_quantity': 1.0,
        'atr': 2.0,
        'lowest_price': 100.0,
        'highest_price': 100.0,
        'target1_reached': False,
        'target2_reached': False,
        'remaining_quantity': 1.0,
        'algo_ids': {},
        'direction': 'SHORT',
        'trailing_activated': False,
        'trailing_stop_price': None,
        'pending_profit_pct': None,
        'current_tier_index': -1,
    }
    tracking.update(overrides)
    executor.position_tracking[symbol] = tracking
    return tracking


# ============================================================================
# detect_take_profit_fills 测试
# ============================================================================

class TestDetectTakeProfitFills:
    """detect_take_profit_fills() 测试"""

    def test_detect_disabled_returns_none(self):
        """position_detection.enabled=False 时返回 None"""
        executor = create_executor({
            'trading': {
                'position_detection': {
                    'enabled': False,
                }
            }
        })
        setup_position_tracking(executor, "BTCUSDT")
        executor._last_tracked_qty["BTCUSDT"] = 1.0
        assert executor.detect_take_profit_fills("BTCUSDT", 0.5) is None

    def test_first_tracking_returns_none(self):
        """首次跟踪记录数量并返回 None"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT")
        assert executor.detect_take_profit_fills("BTCUSDT", 1.0) is None
        assert executor._last_tracked_qty["BTCUSDT"] == 1.0

    def test_no_change_returns_none(self):
        """数量不变返回 None"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT")
        executor._last_tracked_qty["BTCUSDT"] = 1.0
        assert executor.detect_take_profit_fills("BTCUSDT", 1.0) is None

    def test_target1_fill_detected(self):
        """100→70 标记 target1_reached（返回1），remaining_quantity 用实际值"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT")
        executor._last_tracked_qty["BTCUSDT"] = 1.0
        result = executor.detect_take_profit_fills("BTCUSDT", 0.7)
        assert result == 1
        tracking = executor.position_tracking["BTCUSDT"]
        assert tracking["target1_reached"] is True
        assert tracking["target2_reached"] is False
        assert tracking["remaining_quantity"] == 0.7

    def test_target2_fill_detected(self):
        """70→30 标记 target2_reached（返回2）"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT", target1_reached=True)
        executor._last_tracked_qty["BTCUSDT"] = 0.7
        result = executor.detect_take_profit_fills("BTCUSDT", 0.3)
        assert result == 2
        tracking = executor.position_tracking["BTCUSDT"]
        assert tracking["target1_reached"] is True
        assert tracking["target2_reached"] is True
        assert tracking["remaining_quantity"] == 0.3

    def test_within_tolerance_no_fill(self):
        """减少量在容差内返回 None"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT")
        executor._last_tracked_qty["BTCUSDT"] = 1.0
        # 减少 0.005（容差 = max(1.0*0.01, 0.0001) = 0.01），在容差内
        assert executor.detect_take_profit_fills("BTCUSDT", 0.995) is None

    def test_all_closed_returns_zero(self):
        """全部平仓返回 0"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT")
        executor._last_tracked_qty["BTCUSDT"] = 1.0
        assert executor.detect_take_profit_fills("BTCUSDT", 0.0) == 0

    def test_no_tracking_returns_none(self):
        """持仓跟踪不存在返回 None"""
        executor = create_executor()
        # 不设置 position_tracking，但设置 _last_tracked_qty 以便进入减少分支
        executor._last_tracked_qty["BTCUSDT"] = 1.0
        assert executor.detect_take_profit_fills("BTCUSDT", 0.5) is None


# ============================================================================
# update_target_status 测试
# ============================================================================

class TestUpdateTargetStatus:
    """update_target_status() 测试"""

    def test_update_target_status_ratio_fallback(self):
        """不带 exchange_qty 时按比例估算 remaining_quantity"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT")
        # 初始 remaining_quantity = 1.0，target1 平仓比例 30%
        executor.update_target_status("BTCUSDT", 1)
        tracking = executor.position_tracking["BTCUSDT"]
        assert tracking["target1_reached"] is True
        assert tracking["remaining_quantity"] == pytest.approx(0.7)

    def test_update_target_status_with_exchange_qty(self):
        """带 exchange_qty 时优先使用实际剩余数量"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT", target1_reached=True)
        executor.update_target_status("BTCUSDT", 2, exchange_qty=0.3)
        tracking = executor.position_tracking["BTCUSDT"]
        assert tracking["target2_reached"] is True
        assert tracking["remaining_quantity"] == 0.3


# ============================================================================
# clear_position_tracking 测试
# ============================================================================

class TestClearPositionTracking:
    """clear_position_tracking() 测试"""

    def test_clear_position_tracking(self):
        """幂等清理两个字典"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT")
        executor._last_tracked_qty["BTCUSDT"] = 1.0

        executor.clear_position_tracking("BTCUSDT")
        assert "BTCUSDT" not in executor.position_tracking
        assert "BTCUSDT" not in executor._last_tracked_qty

        # 幂等：重复清理不报错
        executor.clear_position_tracking("BTCUSDT")


# ============================================================================
# _get_exchange_position_qty 测试
# ============================================================================

class TestGetExchangePositionQty:
    """_get_exchange_position_qty() 测试"""

    @pytest.mark.asyncio
    async def test_get_short_position_qty(self):
        """存在做空持仓时返回数量绝对值"""
        executor = create_executor()
        executor.binance_api._request.return_value = [
            {'symbol': 'BTCUSDT', 'positionAmt': '-0.700'},
        ]
        qty = await executor._get_exchange_position_qty("BTCUSDT")
        assert qty == 0.7

    @pytest.mark.asyncio
    async def test_get_no_position_returns_zero(self):
        """无做空持仓（positionAmt>=0 或空列表）返回 0.0"""
        executor = create_executor()
        executor.binance_api._request.return_value = [
            {'symbol': 'BTCUSDT', 'positionAmt': '0.000'},
        ]
        qty = await executor._get_exchange_position_qty("BTCUSDT")
        assert qty == 0.0

    @pytest.mark.asyncio
    async def test_get_exception_returns_none(self):
        """API 异常返回 None"""
        executor = create_executor()
        executor.binance_api._request.side_effect = Exception("网络错误")
        qty = await executor._get_exchange_position_qty("BTCUSDT")
        assert qty is None
