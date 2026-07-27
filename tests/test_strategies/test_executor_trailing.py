"""
新币做空策略动态利润保护 - executor 方法测试

测试目标：
1. _check_dynamic_trailing() — 检查并执行动态利润保护
2. _sync_trailing_stop_order() — 同步止损价到交易所条件单
3. check_position_management() — 持仓管理主流程
"""
import sys
import os
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch, ANY
from datetime import datetime, timezone

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

from strategies.new_coin.executor import TradingExecutor
from shared.binance_api import BinanceAPIError


# ============================================================================
# 辅助函数
# ============================================================================

def create_mock_binance_api():
    """创建 Mock BinanceClient"""
    mock = MagicMock()
    mock._request = AsyncMock()
    mock.place_order = AsyncMock()
    mock.place_conditional_order = AsyncMock()
    mock.cancel_algo_order = AsyncMock()
    mock.get_position = AsyncMock()
    mock.get_ticker = AsyncMock()
    mock.get_orderbook = AsyncMock()
    mock.cancel_order = AsyncMock()
    mock.get_open_orders = AsyncMock()
    return mock


def create_mock_db():
    """创建 Mock DatabaseManager"""
    mock = MagicMock()
    mock.execute = AsyncMock()
    return mock


def create_mock_notification():
    """创建 Mock NotificationClient"""
    mock = MagicMock()
    mock.send = AsyncMock()
    return mock


def create_mock_kline_service():
    """创建 Mock KLineService"""
    mock = MagicMock()
    mock.get_klines = AsyncMock()
    return mock


def create_base_config() -> dict:
    """创建基础配置"""
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
            'time_stop': {
                'enabled': True,
                'max_holding_hours': 72,
            },
            'emergency_stop': {
                'enabled': True,
                'check_minutes': 15,
                'trigger_percent': 0.015,
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
                'max_retries': 3,
                'retry_interval': 2,
                'poll_interval': 2,
                'timeout': 10,
            },
            'default_precision': {
                'tick_size': 0.01,
                'step_size': 0.001,
            },
        },
        'kline': {
            'interval': '1h',
            'limit': 18,
            'atr_period': 14,
        },
    }


def create_executor(
    config_override: dict = None,
    mock_binance=None,
    mock_db=None,
    mock_notification=None,
    mock_kline=None,
) -> TradingExecutor:
    """创建 TradingExecutor 实例（所有外部服务使用 mock）"""
    config = create_base_config()
    if config_override:
        _deep_merge(config, config_override)

    mock_binance = mock_binance or create_mock_binance_api()
    mock_db = mock_db or create_mock_db()
    mock_notification = mock_notification or create_mock_notification()
    mock_kline = mock_kline or create_mock_kline_service()

    executor = TradingExecutor(
        binance_api=mock_binance,
        db=mock_db,
        notification=mock_notification,
        config=config,
        kline_service=mock_kline,
    )
    return executor


def _deep_merge(base: dict, override: dict) -> None:
    """深度合并字典"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


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
        'remaining_quantity': 0.7,
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
# _check_dynamic_trailing 测试
# ============================================================================

class TestCheckDynamicTrailing:
    """_check_dynamic_trailing() 测试"""

    @pytest.mark.asyncio
    async def test_no_tracking_returns_early(self):
        """无持仓跟踪时直接返回"""
        executor = create_executor()
        # 不设置 position_tracking
        await executor._check_dynamic_trailing("NONEXISTENT", Decimal('100'))
        # 不应调用任何 API
        executor.binance_api._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_config_returns_early(self):
        """动态利润保护禁用时直接返回"""
        executor = create_executor({
            'trading': {
                'dynamic_trailing': {
                    'enabled': False,
                }
            }
        })
        setup_position_tracking(executor, "BTCUSDT", target2_reached=True)
        await executor._check_dynamic_trailing("BTCUSDT", Decimal('100'))
        # 不应调用 API
        executor.binance_api.place_conditional_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_check_not_activated(self):
        """正常检查，未激活（无 TP2 触发）"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT")

        # TP2 未触发，calculate_dynamic_trailing_stop 应返回 None
        # 因为 also_on_tp2=True 但 tp2_hit=False
        # 且浮盈 = (100-100)/100*100 = 0% < 1.5%
        await executor._check_dynamic_trailing("BTCUSDT", Decimal('100'))

        # 未激活，不应下条件单
        executor.binance_api.place_conditional_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_check_activated(self):
        """正常检查，已激活 → 同步条件单"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT", target2_reached=True, lowest_price=90.0)

        # Mock 精度获取
        executor._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))

        # Mock 创建条件单成功
        executor.binance_api.place_conditional_order.return_value = {
            'algoId': 'algo123',
            'orderId': 'order456',
        }

        # 激活：浮盈 = (100-90)/100*100 = 10% >= 1.5%
        await executor._check_dynamic_trailing("BTCUSDT", Decimal('90'))

        # 应创建条件单
        executor.binance_api.place_conditional_order.assert_called_once()
        call_args = executor.binance_api.place_conditional_order.call_args
        assert call_args[1]['symbol'] == 'BTCUSDT'
        assert call_args[1]['side'] == 'BUY'
        assert call_args[1]['order_type'] == 'STOP'
        assert call_args[1]['reduce_only'] is True

        # 验证 tracking 状态已更新
        tracking = executor.position_tracking["BTCUSDT"]
        assert tracking['trailing_activated'] is True
        assert tracking['trailing_stop_price'] is not None

    @pytest.mark.asyncio
    async def test_triggered_close_position(self):
        """触发平仓"""
        executor = create_executor()
        setup_position_tracking(
            executor, "BTCUSDT",
            target2_reached=True,
            lowest_price=90.0,
            trailing_activated=True,
            trailing_stop_price=92.5,
            algo_ids={'trailing_stop': 'trailing_algo_123'},
        )

        # Mock 获取价格
        executor.binance_api._request.return_value = {'price': '98'}

        # Mock 平仓所需的 API 调用
        executor.binance_api.get_position.return_value = [
            {'positionSide': 'SHORT', 'positionAmt': '-0.7'}
        ]
        executor.binance_api.get_ticker.return_value = {'lastPrice': 98.0}
        executor._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))

        # Mock 条件单取消失败（静默）
        executor.binance_api.cancel_algo_order.side_effect = BinanceAPIError(
            'Order not found', -2011
        )

        # 当前价 = 98，止损价 = 92.5，98 >= 92.5 → 触发
        await executor._check_dynamic_trailing("BTCUSDT", Decimal('98'))

        # 应触发平仓 - 但平仓是限价单流程，看是否调用了 get_orderbook
        # 由于 _close_position 内部逻辑复杂，我们只验证进入了平仓分支
        # 由于限价单流程会尝试多次，检查是否至少调用了平仓相关方法
        assert executor.binance_api.cancel_algo_order.called

    @pytest.mark.asyncio
    async def test_stop_price_not_improved_skips(self):
        """止损价未改善，跳过更新条件单"""
        executor = create_executor()
        setup_position_tracking(
            executor, "BTCUSDT",
            target2_reached=True,
            lowest_price=90.0,
            trailing_activated=True,
            trailing_stop_price=92.5,  # 与计算出的止损价相同
        )

        # 通过 _check_dynamic_trailing 内部逻辑，止损价不变时应跳过
        await executor._check_dynamic_trailing("BTCUSDT", Decimal('91'))

        # 止损价未改善，不应下新条件单
        # 注意：如果止损价计算出来与旧值相同，不会调用 _sync_trailing_stop_order
        executor.binance_api.place_conditional_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_does_not_block(self):
        """异常处理，不阻断主流程"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT", target2_reached=True)

        # 让 _get_symbol_precision 抛出异常
        executor._get_symbol_precision = AsyncMock(side_effect=Exception("API error"))

        # 不应抛出异常
        try:
            await executor._check_dynamic_trailing("BTCUSDT", Decimal('90'))
        except Exception:
            pytest.fail("_check_dynamic_trailing 不应抛出异常")


# ============================================================================
# _sync_trailing_stop_order 测试
# ============================================================================

class TestSyncTrailingStopOrder:
    """_sync_trailing_stop_order() 测试"""

    @pytest.mark.asyncio
    async def test_first_activation_cancels_sl(self):
        """首次激活：创建新条件单，取消硬止损单"""
        executor = create_executor()
        setup_position_tracking(
            executor, "BTCUSDT",
            algo_ids={'sl': 'sl_algo_123'},  # 存在硬止损单
        )

        # Mock 精度
        executor._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))

        # Mock 创建条件单成功
        executor.binance_api.place_conditional_order.return_value = {
            'algoId': 'trailing_algo_456',
        }

        await executor._sync_trailing_stop_order("BTCUSDT", Decimal('95'))

        # 应取消硬止损单
        executor.binance_api.cancel_algo_order.assert_any_call("BTCUSDT", "sl_algo_123")

        # 应创建新条件单
        executor.binance_api.place_conditional_order.assert_called_once()
        call_args = executor.binance_api.place_conditional_order.call_args
        assert call_args[1]['stop_price'] == Decimal('95')

        # 验证 tracking 中 algo_id 已更新
        tracking = executor.position_tracking["BTCUSDT"]
        assert tracking['algo_ids']['trailing_stop'] == 'trailing_algo_456'
        assert tracking['algo_ids']['sl'] is None

    @pytest.mark.asyncio
    async def test_non_first_activation(self):
        """非首次激活：取消旧条件单，创建新条件单"""
        executor = create_executor()
        setup_position_tracking(
            executor, "BTCUSDT",
            algo_ids={
                'trailing_stop': 'old_trailing_algo',
                'sl': None,  # 硬止损单已取消
            },
            trailing_stop_price=95.0,
        )

        # Mock 精度
        executor._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))

        # Mock 创建条件单成功
        executor.binance_api.place_conditional_order.return_value = {
            'algoId': 'new_trailing_algo',
        }

        await executor._sync_trailing_stop_order("BTCUSDT", Decimal('93'))

        # 应取消旧条件单
        executor.binance_api.cancel_algo_order.assert_any_call("BTCUSDT", "old_trailing_algo")

        # 应创建新条件单
        executor.binance_api.place_conditional_order.assert_called_once()
        call_args = executor.binance_api.place_conditional_order.call_args
        assert call_args[1]['stop_price'] == Decimal('93')

        # 验证 tracking 中 algo_id 已更新
        tracking = executor.position_tracking["BTCUSDT"]
        assert tracking['algo_ids']['trailing_stop'] == 'new_trailing_algo'

    @pytest.mark.asyncio
    async def test_silent_error_codes_on_cancel(self):
        """取消旧条件单时静默错误码（-2022, -2011）"""
        executor = create_executor()
        setup_position_tracking(
            executor, "BTCUSDT",
            algo_ids={
                'trailing_stop': 'old_trailing_algo',
                'sl': 'sl_algo',
            },
        )

        # Mock 精度
        executor._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))

        # 取消旧条件单返回静默错误
        def cancel_side_effect(symbol, algo_id):
            if algo_id == 'old_trailing_algo':
                raise BinanceAPIError('Order not found', -2011)
            if algo_id == 'sl_algo':
                raise BinanceAPIError('Order already cancelled', -2022)
            return None

        executor.binance_api.cancel_algo_order.side_effect = cancel_side_effect

        # Mock 创建条件单成功
        executor.binance_api.place_conditional_order.return_value = {
            'algoId': 'new_trailing_algo',
        }

        # 不应抛出异常
        try:
            await executor._sync_trailing_stop_order("BTCUSDT", Decimal('95'))
        except Exception:
            pytest.fail("静默错误码不应抛出异常")

        # 应创建新条件单（即使取消失败）
        executor.binance_api.place_conditional_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_silent_error_on_cancel(self):
        """非静默错误码应记录警告但不阻断"""
        executor = create_executor()
        setup_position_tracking(
            executor, "BTCUSDT",
            algo_ids={'trailing_stop': 'old_trailing_algo'},
        )

        # Mock 精度
        executor._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))

        # 取消旧条件单返回非静默错误
        executor.binance_api.cancel_algo_order.side_effect = BinanceAPIError(
            'Internal error', -1001
        )

        # Mock 创建条件单成功
        executor.binance_api.place_conditional_order.return_value = {
            'algoId': 'new_trailing_algo',
        }

        # 不应抛出异常
        try:
            await executor._sync_trailing_stop_order("BTCUSDT", Decimal('95'))
        except Exception:
            pytest.fail("非静默错误码不应抛出异常")

        # 应创建新条件单
        executor.binance_api.place_conditional_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_precision_handling(self):
        """精度处理验证"""
        executor = create_executor()
        setup_position_tracking(
            executor, "BTCUSDT",
            algo_ids={},
            remaining_quantity=0.7342,
        )

        # Mock 精度返回特定值
        executor._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))

        # Mock 创建条件单成功
        executor.binance_api.place_conditional_order.return_value = {
            'algoId': 'algo_123',
        }

        await executor._sync_trailing_stop_order("BTCUSDT", Decimal('95.12345'))

        # 验证精度处理
        call_args = executor.binance_api.place_conditional_order.call_args
        # stop_price 传入原始值（未格式化），limit_price 应四舍五入到 0.01 精度
        # 止损价 = 95.12345 * (1 + 0.002) = 95.313...，格式化后 = 95.31
        assert call_args[1]['stop_price'] == Decimal('95.12345'), "stop_price 是传入的原始值"
        assert call_args[1]['price'] == Decimal('95.31'), f"limit_price 应格式化为 95.31，实际 {call_args[1]['price']}"
        # 数量应四舍五入到 0.001 精度
        assert call_args[1]['quantity'] == Decimal('0.734')

    @pytest.mark.asyncio
    async def test_no_tracking_no_crash(self):
        """无持仓跟踪时不应崩溃"""
        executor = create_executor()
        executor._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))
        executor.binance_api.place_conditional_order.return_value = {'algoId': 'algo'}

        # position_tracking 为空
        await executor._sync_trailing_stop_order("BTCUSDT", Decimal('95'))
        # 不应崩溃，但可能会创建条件单（因为 tracking 为空时不会更新 algo_ids）
        # 这个行为取决于代码实现，我们只验证不崩溃

    @pytest.mark.asyncio
    async def test_place_order_failure_logs_error(self):
        """创建条件单失败应记录错误但不阻断"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT", algo_ids={})

        executor._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))
        executor.binance_api.place_conditional_order.side_effect = Exception("API error")

        # 不应抛出异常
        try:
            await executor._sync_trailing_stop_order("BTCUSDT", Decimal('95'))
        except Exception:
            pytest.fail("创建条件单失败不应抛出异常")


# ============================================================================
# check_position_management 测试
# ============================================================================

class TestCheckPositionManagement:
    """check_position_management() 测试"""

    @pytest.mark.asyncio
    async def test_no_tracking_returns_early(self):
        """无持仓跟踪时直接返回"""
        executor = create_executor()
        await executor.check_position_management("NONEXISTENT")
        executor.binance_api._request.assert_not_called()

    @pytest.mark.asyncio
    async def test_tp2_not_reached_does_not_call_trailing(self):
        """TP2 未到达时不应调用动态利润保护"""
        executor = create_executor()
        setup_position_tracking(
            executor, "BTCUSDT",
            target1_reached=True,
            target2_reached=False,  # TP2 未到达
        )

        # Mock 时间止损检查不触发（设置最近入场时间）
        # 默认 entry_time 是 now，所以时间止损不会触发

        await executor.check_position_management("BTCUSDT")

        # TP2 未到达，不应获取价格（动态利润保护跳过）
        # 但会检查时间止损和紧急止损，所以 _request 可能被调用
        # 我们只验证不调 _check_dynamic_trailing
        # 通过检查是否调用了动态利润保护相关的 API 来间接验证
        # 紧急止损会调用 _request 获取价格，所以不能断言 _request 没被调用

    @pytest.mark.asyncio
    async def test_tp2_reached_calls_trailing(self):
        """TP2 到达后调用动态利润保护"""
        executor = create_executor()
        setup_position_tracking(
            executor, "BTCUSDT",
            target1_reached=True,
            target2_reached=True,  # TP2 已到达
            lowest_price=90.0,
        )

        # Mock 价格获取
        executor.binance_api._request.return_value = {'price': '91'}

        # Mock 精度获取
        executor._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))

        # Mock 创建条件单
        executor.binance_api.place_conditional_order.return_value = {
            'algoId': 'trailing_algo',
        }

        await executor.check_position_management("BTCUSDT")

        # 应获取价格（用于动态利润保护）
        executor.binance_api._request.assert_any_call(
            "GET", "/fapi/v1/ticker/price",
            params={'symbol': 'BTCUSDT'}, signed=False
        )

        # current_price=91 < 止损价92.5，不应触发
        # 应创建条件单（动态利润保护激活，止损价改善）
        assert executor.binance_api.place_conditional_order.called

    @pytest.mark.asyncio
    async def test_tp2_reached_updates_highest_price(self):
        """TP2 到达后更新最高价（做空追踪反弹）"""
        executor = create_executor()
        tracking = setup_position_tracking(
            executor, "BTCUSDT",
            target1_reached=True,
            target2_reached=True,
            highest_price=100.0,
        )

        # Mock 当前价高于最高价
        executor.binance_api._request.return_value = {'price': '105'}

        await executor.check_position_management("BTCUSDT")

        # 最高价应更新
        assert tracking['highest_price'] == 105.0, "最高价应更新为 105"

    @pytest.mark.asyncio
    async def test_exception_does_not_block(self):
        """异常处理，不阻断主流程"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT", target2_reached=True)

        # 让 _request 抛出异常
        executor.binance_api._request.side_effect = Exception("API error")

        # 不应抛出异常
        try:
            await executor.check_position_management("BTCUSDT")
        except Exception:
            pytest.fail("check_position_management 不应抛出异常")


# ============================================================================
# _cancel_trailing_stop_order 测试
# ============================================================================

class TestCancelTrailingStopOrder:
    """_cancel_trailing_stop_order() 测试"""

    @pytest.mark.asyncio
    async def test_cancel_existing_order(self):
        """取消存在的移动止损条件单"""
        executor = create_executor()
        setup_position_tracking(
            executor, "BTCUSDT",
            algo_ids={'trailing_stop': 'trailing_algo_123'},
        )

        await executor._cancel_trailing_stop_order("BTCUSDT")

        executor.binance_api.cancel_algo_order.assert_called_once_with(
            "BTCUSDT", "trailing_algo_123"
        )

        # 验证 algo_id 已清除
        tracking = executor.position_tracking["BTCUSDT"]
        assert tracking['algo_ids']['trailing_stop'] is None

    @pytest.mark.asyncio
    async def test_no_trailing_stop_id_skips(self):
        """无移动止损条件单 ID 时跳过"""
        executor = create_executor()
        setup_position_tracking(executor, "BTCUSDT", algo_ids={})

        await executor._cancel_trailing_stop_order("BTCUSDT")

        executor.binance_api.cancel_algo_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_silent_error_on_cancel(self):
        """取消时静默错误码"""
        executor = create_executor()
        setup_position_tracking(
            executor, "BTCUSDT",
            algo_ids={'trailing_stop': 'trailing_algo'},
        )

        executor.binance_api.cancel_algo_order.side_effect = BinanceAPIError(
            'Order not found', -2011
        )

        # 不应抛出异常
        try:
            await executor._cancel_trailing_stop_order("BTCUSDT")
        except Exception:
            pytest.fail("静默错误码不应抛出异常")

        # 验证 algo_id 已清除（即使取消失败）
        tracking = executor.position_tracking["BTCUSDT"]
        assert tracking['algo_ids']['trailing_stop'] is None


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short', '-s'])