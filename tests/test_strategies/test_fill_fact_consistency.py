"""
B6 验收测试 —— 真实成交事实一致性

覆盖：
- B6a: HRS 零成交不建档 / 胜负计数助手 _record_outcome_by_pnl
- B6b: new_coin 零成交不建档 / 真实均价驱动落仓
- B6c: MTPCS resolve_fill_avg_price / merge_position 加权均价 / 回放不翻倍

全部 mock 离线，不访问真实交易所。
"""
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

# 确保项目根目录在 path 中
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

import pytest

from shared.order_fill_waiter import (
    OrderFillResult,
    STATUS_FILLED,
    STATUS_PARTIAL,
    STATUS_CANCELED_UNFILLED,
)
from shared.protection_retry import (
    resolve_fill_avg_price,
    merge_position,
    build_position_state,
)


# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------

def _make_fill(
    *,
    executed: Decimal = Decimal('0'),
    orig: Decimal = Decimal('10'),
    status: str = STATUS_FILLED,
    avg_price: Decimal = Decimal('0'),
    order_id: int = 100,
    raw: Optional[Dict[str, Any]] = None,
) -> OrderFillResult:
    """构造 OrderFillResult（与 test_mtpcs_v628_add_position 风格一致）"""
    remaining = orig - executed
    return OrderFillResult(
        status=status,
        executed_qty=executed,
        orig_qty=orig,
        remaining_qty=remaining,
        avg_price=avg_price,
        order_id=order_id,
        raw=raw or {},
    )


class _DummyPosition:
    """merge_position / build_position_state 的轻量占位对象"""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


# ===================================================================
# B6c 单元测试：resolve_fill_avg_price
# ===================================================================

class TestResolveFillAvgPrice:
    """B6c — 真实成交均价解析的回退链"""

    def test_dict_avg_price(self):
        """dict 的 avgPrice 字段有效"""
        assert resolve_fill_avg_price({'avgPrice': '1234.56'}) == Decimal('1234.56')

    def test_dict_avg_price_alternative(self):
        """dict 的 avg_price（下划线）备选字段有效"""
        assert resolve_fill_avg_price({'avg_price': '100.0'}) == Decimal('100.0')

    def test_object_attr(self):
        """对象 .avg_price 属性有效"""
        fill = _make_fill(executed=Decimal('4'), avg_price=Decimal('50000'))
        assert resolve_fill_avg_price(fill) == Decimal('50000')

    def test_missing_returns_none(self):
        """avgPrice / avg_price 都缺失 → None"""
        assert resolve_fill_avg_price({'executedQty': '4'}) is None

    def test_zero_returns_none(self):
        """avgPrice=0 → None（0 不是有效均价）"""
        assert resolve_fill_avg_price({'avgPrice': '0'}) is None

    def test_negative_returns_none(self):
        """负值 → None"""
        assert resolve_fill_avg_price({'avgPrice': '-1'}) is None

    def test_none_input_returns_none(self):
        """字段值为 None → None"""
        assert resolve_fill_avg_price({'avgPrice': None}) is None

    def test_object_zero_returns_none(self):
        """OrderFillResult 默认 avg_price=0 → None"""
        fill = _make_fill(executed=Decimal('4'), avg_price=Decimal('0'))
        assert resolve_fill_avg_price(fill) is None


# ===================================================================
# B6c 验收：merge_position 加权均价
# ===================================================================

class TestMergePositionWeightedAvg:
    """B6c — merge_position 的加权均价公式与回放幂等"""

    def _make_position(self, qty='2', entry='100', **kwargs):
        pos = _DummyPosition()
        pos.initial_quantity = Decimal(qty)
        pos.current_quantity = Decimal(qty)
        pos.entry_price = Decimal(entry)
        pos.atr = Decimal('1000')
        pos.grade = 'A'
        for k, v in kwargs.items():
            setattr(pos, k, v)
        return pos

    def test_weighted_avg_real_fill(self):
        """old 2@100 + 新真实均价 110, actual=3 → entry_price = (200+110)/3 = 310/3"""
        pos = self._make_position()
        signal = {
            'entry_price': Decimal('105'),   # 信号价（应该被真实均价覆盖）
            'atr': Decimal('1200'),
            'grade': 'A',
        }
        entry_order = _make_fill(
            executed=Decimal('1'),
            avg_price=Decimal('110'),
            orig=Decimal('1'),
            status=STATUS_FILLED,
            order_id=200,
        )

        merge_position(pos, signal, entry_order, actual_quantity=Decimal('3'))

        # (2*100 + 1*110) / 3 = 310/3， ≈ 103.333…
        expected = Decimal(310) / Decimal(3)
        assert pos.entry_price == expected
        # 信号价 105 不应影响结果
        assert pos.entry_price != Decimal('105')
        assert pos.initial_quantity == Decimal('3')
        assert pos.current_quantity == Decimal('3')

    def test_weighted_avg_signal_fallback_when_no_real_price(self):
        """真实均价缺失时回退信号价"""
        pos = self._make_position()
        signal = {'entry_price': Decimal('105'), 'atr': Decimal('1200'), 'grade': 'A'}
        # avg_price=Decimal('0')，resolve_fill_avg_price 返回 None
        entry_order = _make_fill(
            executed=Decimal('1'),
            avg_price=Decimal('0'),
            orig=Decimal('1'),
            status=STATUS_FILLED,
            order_id=200,
        )

        merge_position(pos, signal, entry_order, actual_quantity=Decimal('3'))

        # (2*100 + 1*105) / 3 = 305/3
        assert pos.entry_price == Decimal(305) / Decimal(3)

    def test_replay_no_double_count(self):
        """同一成交事件对同一 position 调两次，initial_quantity 保持不变"""
        pos = self._make_position()
        signal = {'entry_price': Decimal('105'), 'atr': Decimal('1200'), 'grade': 'A'}
        entry_order = _make_fill(
            executed=Decimal('1'),
            avg_price=Decimal('110'),
            orig=Decimal('1'),
            status=STATUS_FILLED,
            order_id=200,
        )

        merge_position(pos, signal, entry_order, actual_quantity=Decimal('3'))
        qty_after_first = pos.initial_quantity
        merge_position(pos, signal, entry_order, actual_quantity=Decimal('3'))
        assert pos.initial_quantity == qty_after_first == Decimal('3')

    def test_first_position_new_entry(self):
        """第一次建仓（old_quantity=0 或缺失），entry_price = 新均价"""
        pos = self._make_position(qty='0', entry='0')
        signal = {'entry_price': Decimal('95'), 'atr': Decimal('800'), 'grade': 'B'}
        entry_order = _make_fill(
            executed=Decimal('2'),
            avg_price=Decimal('100'),
            orig=Decimal('2'),
            status=STATUS_FILLED,
            order_id=200,
        )

        merge_position(pos, signal, entry_order, actual_quantity=Decimal('2'))
        assert pos.entry_price == Decimal('100')
        assert pos.initial_quantity == Decimal('2')


# ===================================================================
# B6a + B6b + new_coin：需要完整 executor 实例的测试
# ===================================================================

# 这些测试需要 mock executor 的外部依赖（db/binance/kline 等），
# 结构较重量级，这里用独立函数 + AsyncMock 构造。

def _make_new_coin_executor_config() -> Dict[str, Any]:
    """new_coin executor 测试用配置（精简）"""
    return {
        'trading': {
            'entry_order_timeout_seconds': 5,
            'market_order_score_threshold': 70,
        },
        'leverage': 5,
        'batch_take_profit_enabled': False,
        'score_rules': {},
    }


async def _construct_mock_new_coin_executor():
    """用 AsyncMock 构造 new_coin TradingExecutor 的轻量替身"""
    from strategies.new_coin.executor import TradingExecutor

    config = _make_new_coin_executor_config()
    executor = TradingExecutor.__new__(TradingExecutor)
    executor.config = config
    executor.leverage = Decimal(str(config['leverage']))
    executor._leverage_valid = True
    executor.baseline_ready = True
    executor.min_notional = Decimal(str(config['trading'].get('min_notional', 5)))
    executor.batch_take_profit_enabled = config['batch_take_profit_enabled']
    executor.market_order_score_threshold = config['trading']['market_order_score_threshold']

    # 需要的方法全部替换为 mock
    executor._get_account_balance = AsyncMock(return_value=1000.0)
    executor._is_symbol_occupied = AsyncMock(return_value=False)
    executor._claim_symbol = AsyncMock(return_value={'claimed': True, 'owner': 'me'})
    executor._release_claim_quietly = AsyncMock()
    executor._resolve_open_margin_budget = AsyncMock(
        return_value=(Decimal('1.0'), '')
    )
    executor._get_symbol_precision = AsyncMock(
        return_value=(Decimal('0.01'), Decimal('0.01'))
    )
    executor._format_quantity = MagicMock(
        side_effect=lambda q, step: (
            (Decimal(str(q)) / step).to_integral_value() * step
        )
    )
    executor._set_leverage = AsyncMock()
    executor._place_short_order = AsyncMock(return_value={
        'orderId': 1001,
        'symbol': 'BTCUSDT',
        'status': 'NEW',
        'origQty': '1.0',
        'clientOrderId': 'test_001',
    })
    executor._wait_for_order_fill = AsyncMock()
    executor._cancel_entry_order_quietly = AsyncMock()
    executor._zero_micro_entry = AsyncMock()
    executor._save_order = AsyncMock()
    executor._calculate_atr = AsyncMock(return_value=Decimal('1000'))
    executor._build_tracking_entry = MagicMock(return_value={})
    executor._insert_short_position = AsyncMock()
    executor._set_stop_loss_take_profit = AsyncMock()
    executor._set_batch_take_profit = AsyncMock()
    executor._send_notification = AsyncMock()
    executor.position_tracking = {}
    executor._last_tracked_qty = {}
    return executor


class TestNewCoinFillFacts:
    """B6b — new_coin executor 真实成交事实一致性"""

    @pytest.mark.asyncio
    async def test_zero_fill_no_position(self):
        """执行器收到 executedQty=0 的终态订单 → 返回失败、不建档、已释放"""
        from strategies.new_coin.executor import _parse_fill_facts

        executor = await _construct_mock_new_coin_executor()

        # _wait_for_order_fill 返回完全无成交
        executor._wait_for_order_fill = AsyncMock(
            return_value=_make_fill(
                executed=Decimal('0'),
                orig=Decimal('1'),
                status=STATUS_CANCELED_UNFILLED,
                order_id=1001,
            )
        )

        order, reason = await executor.execute_short(
            symbol='BTCUSDT',
            score_result={'total_score': 85},
            current_price=50000.0,
        )

        assert order is None
        assert reason is not None and len(reason) > 0
        # 建档路径不应该被调用
        executor._insert_short_position.assert_not_called()
        executor._save_order.assert_not_called()
        # 占位应该被释放
        executor._release_claim_quietly.assert_called()

    @pytest.mark.asyncio
    async def test_partial_fill_real_qty_and_price(self):
        """计划 1.0 / 实成 0.4 → 建档数量用 0.4、均价用真实成交均价"""
        executor = await _construct_mock_new_coin_executor()

        fill = _make_fill(
            executed=Decimal('0.4'),
            orig=Decimal('1.0'),
            status=STATUS_PARTIAL,
            avg_price=Decimal('49500'),
            order_id=1001,
            raw={
                'orderId': 1001, 'symbol': 'BTCUSDT',
                'status': 'PARTIALLY_FILLED', 'executedQty': '0.4',
                'avgPrice': '49500', 'origQty': '1.0',
            },
        )
        executor._wait_for_order_fill = AsyncMock(return_value=fill)

        order, reason = await executor.execute_short(
            symbol='BTCUSDT',
            score_result={'total_score': 85},
            current_price=50000.0,
        )

        assert order is not None
        assert reason == ""
        # _insert_short_position 被调用，参数里 quantity 是 0.4、entry_price 是 49500
        executor._insert_short_position.assert_called_once()
        call_kwargs = executor._insert_short_position.call_args[1]
        assert call_kwargs['symbol'] == 'BTCUSDT'
        assert call_kwargs['quantity'] == Decimal('0.4')
        assert call_kwargs['entry_price'] == Decimal('49500')
        # _save_order 应该收到显式 symbol= 参数（B6b 签名变化）
        executor._save_order.assert_called_once()
        save_kwargs = executor._save_order.call_args[1]
        assert save_kwargs['symbol'] == 'BTCUSDT'
        # 通知里用的是真实成交价而非计划价
        executor._send_notification.assert_called_once()
        notify_args = executor._send_notification.call_args[0]
        # _send_notification(symbol, order, current_price, score_result)
        assert notify_args[0] == 'BTCUSDT'
        assert float(notify_args[2]) == 49500.0

    @pytest.mark.asyncio
    async def test_avg_price_unknown_fallback_with_warning(self):
        """真实成交均价缺失时回退计划价 + 告警，不编造均价"""
        import logging
        from strategies.new_coin.executor import logger as nc_logger

        executor = await _construct_mock_new_coin_executor()

        # fill.avg_price=0，resolve_fill_avg_price 会返回 None
        fill = _make_fill(
            executed=Decimal('0.4'),
            orig=Decimal('1.0'),
            status=STATUS_PARTIAL,
            avg_price=Decimal('0'),
            order_id=1001,
            raw={
                'orderId': 1001, 'symbol': 'BTCUSDT',
                'status': 'PARTIALLY_FILLED', 'executedQty': '0.4',
                'avgPrice': '0', 'origQty': '1.0',
            },
        )
        executor._wait_for_order_fill = AsyncMock(return_value=fill)

        with patch.object(nc_logger, 'warning') as mock_warn:
            order, reason = await executor.execute_short(
                symbol='BTCUSDT',
                score_result={'total_score': 85},
                current_price=50000.0,
            )

        assert order is not None
        # 应该产生了"成交均价未知"的告警
        warn_msgs = [
            str(call_args[0]) for call_args in mock_warn.call_args_list
            if '成交均价未知' in str(call_args[0])
        ]
        assert len(warn_msgs) >= 1, f"预期出现均价未知告警，实际 calls={mock_warn.call_args_list}"
        # _insert_short_position 的 entry_price 应该是回退到的 current_price
        call_kwargs = executor._insert_short_position.call_args[1]
        assert call_kwargs['entry_price'] == Decimal('50000')


# ===================================================================
# B6a：HRS 零成交 / 胜负计数助手
# ===================================================================

class TestHRSFillFactsAndOutcomeRecord:
    """B6a — HRS 真实成交 + _record_outcome_by_pnl 助手"""

    @pytest.mark.asyncio
    async def test_outcome_profit(self):
        """pnl > 0 → record_profit 被调"""
        from strategies.hrs.strategy import HRSStrategy

        strategy = HRSStrategy.__new__(HRSStrategy)
        rm = MagicMock()
        rm.record_profit = MagicMock()
        rm.record_loss = AsyncMock()
        strategy.risk_manager = rm

        await strategy._record_outcome_by_pnl(
            pnl=100.5, symbol='BTCUSDT', direction='long',
            entry_price=50000.0, current_price=51000.0, context="测试",
        )

        rm.record_profit.assert_called_once()
        rm.record_loss.assert_not_called()

    @pytest.mark.asyncio
    async def test_outcome_loss(self):
        """pnl < 0 → record_loss 被调"""
        from strategies.hrs.strategy import HRSStrategy

        strategy = HRSStrategy.__new__(HRSStrategy)
        rm = MagicMock()
        rm.record_profit = MagicMock()
        rm.record_loss = AsyncMock()
        strategy.risk_manager = rm

        await strategy._record_outcome_by_pnl(
            pnl=-50.3, symbol='BTCUSDT', direction='long',
            entry_price=50000.0, current_price=49500.0, context="测试",
        )

        rm.record_profit.assert_not_called()
        rm.record_loss.assert_awaited_once_with('BTCUSDT', 50000.0, 49500.0)

    @pytest.mark.asyncio
    async def test_outcome_zero_no_count(self):
        """pnl == 0 → 都不调（盈亏平衡不计数）"""
        from strategies.hrs.strategy import HRSStrategy

        strategy = HRSStrategy.__new__(HRSStrategy)
        rm = MagicMock()
        rm.record_profit = MagicMock()
        rm.record_loss = AsyncMock()
        strategy.risk_manager = rm

        await strategy._record_outcome_by_pnl(
            pnl=Decimal('0'), symbol='BTCUSDT', direction='long',
            entry_price=50000.0, current_price=50000.0, context="测试",
        )

        rm.record_profit.assert_not_called()
        rm.record_loss.assert_not_called()

    @pytest.mark.asyncio
    async def test_outcome_none_warning(self):
        """pnl is None → 都不调且产生 warning"""
        from strategies.hrs.strategy import HRSStrategy
        from strategies.hrs.strategy import logger as hrs_logger

        strategy = HRSStrategy.__new__(HRSStrategy)
        rm = MagicMock()
        rm.record_profit = MagicMock()
        rm.record_loss = AsyncMock()
        strategy.risk_manager = rm

        with patch.object(hrs_logger, 'warning') as mock_warn:
            await strategy._record_outcome_by_pnl(
                pnl=None, symbol='BTCUSDT', direction='long',
                entry_price=50000.0, current_price=51000.0, context="全平仓",
            )

        rm.record_profit.assert_not_called()
        rm.record_loss.assert_not_called()
        warn_msgs = [
            str(c[0]) for c in mock_warn.call_args_list
            if '全平仓' in str(c[0]) and 'PnL 无法确定' in str(c[0])
        ]
        assert len(warn_msgs) >= 1, f"预期 PnL 无法确定告警，实际 calls={mock_warn.call_args_list}"

    # --- HRS execute_short 零成交 / 保护围绕真实成交量 ---

    def _make_hrs_executor(self):
        """构造 HRS TradingExecutor 的最小 mock（补齐 binance_api / order_type 等）"""
        from strategies.hrs.executor import TradingExecutor as HRSTradingExecutor

        executor = HRSTradingExecutor.__new__(HRSTradingExecutor)
        executor.config = {}
        executor.order_type_entry = 'LIMIT'
        executor.binance_api = MagicMock()
        executor.binance_api.place_order = AsyncMock()  # async 下单方法
        executor.kline_service = MagicMock()
        executor.db = MagicMock()
        executor.notification = MagicMock()

        executor._get_symbol_precision = AsyncMock(
            return_value=(Decimal('0.01'), Decimal('0.01'))
        )
        executor._set_leverage = AsyncMock()
        executor._check_order_fill_with_timeout = AsyncMock()
        executor._format_quantity = MagicMock(
            side_effect=lambda q, step: Decimal(str(q))
        )
        executor._get_lv_rm_params = MagicMock(return_value={
            'atr_multiplier': 2.0, 'emergency_percent': 0.01,
            'min_absolute_percent': 0.03,
            'target1_atr_multiplier': 1.5, 'target1_close_percent': 0.5,
            'target2_atr_multiplier': 3.0, 'target2_close_percent': 0.3,
        })
        executor._calc_sl_price = MagicMock(return_value=48000.0)
        executor._create_protection_orders = AsyncMock(return_value=[])
        executor._send_notification = AsyncMock()
        return executor

    @pytest.mark.asyncio
    async def test_hrs_zero_fill_no_position(self):
        """B6a — HRS execute_short 收到 executedQty=0 → OpenResult(order_filled=False, filled_quantity=0)"""
        executor = self._make_hrs_executor()

        # binance_api.place_order 返回下单成功（但终态 executedQty=0）
        executor.binance_api.place_order = AsyncMock(return_value={
            'orderId': 3001, 'symbol': 'ETHUSDT', 'status': 'NEW',
            'executedQty': '0', 'avgPrice': '0', 'origQty': '1',
        })
        executor._check_order_fill_with_timeout = AsyncMock(return_value={
            'orderId': 3001, 'symbol': 'ETHUSDT', 'status': 'CANCELED',
            'executedQty': '0', 'avgPrice': '0', 'origQty': '1',
        })

        result = await executor.execute_short(
            symbol='ETHUSDT',
            entry_price=3000.0,
            atr=100.0,
            quantity=Decimal('1'),
            score=80.0,
            entry_mode='standard',
        )

        assert result.order_filled is False
        assert result.filled_quantity == 0.0
        executor._create_protection_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_hrs_protection_around_real_fill_qty(self):
        """B6a — 计划 10 / 实成 4 → _create_protection_orders 收到的数量是 4"""
        executor = self._make_hrs_executor()

        executor.binance_api.place_order = AsyncMock(return_value={
            'orderId': 4001, 'symbol': 'BTCUSDT', 'status': 'FILLED',
            'executedQty': '4', 'avgPrice': '49000', 'origQty': '10',
        })
        # _check_order_fill_with_timeout 返回终态 order dict（含 executedQty=4）
        executor._check_order_fill_with_timeout = AsyncMock(return_value={
            'orderId': 4001, 'symbol': 'BTCUSDT', 'status': 'FILLED',
            'executedQty': '4', 'avgPrice': '49000', 'origQty': '10',
        })

        result = await executor.execute_short(
            symbol='BTCUSDT',
            entry_price=50000.0,
            atr=1000.0,
            quantity=Decimal('10'),    # 计划 10
            score=80.0,
            entry_mode='standard',
        )

        assert result.order_filled is True
        assert result.filled_quantity == 4.0
        assert result.avg_fill_price == 49000.0
        # _create_protection_orders 收到 actual_qty = 4（不是计划的 10）
        executor._create_protection_orders.assert_awaited_once()
        call_args = executor._create_protection_orders.call_args[0]
        # call_args: (symbol, direction, entry_price, atr, actual_qty, tick_size, step_size, params)
        assert Decimal(str(call_args[4])) == Decimal('4'), (
            f"_create_protection_orders 实际收到 qty={call_args[4]}，预期 4"
        )
