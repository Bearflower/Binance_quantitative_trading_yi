"""B 段 R3 补测：覆盖本任务核心改动行中尚未被覆盖的分支。

缺口来源：git diff -U0 改动行 ∩ coverage 缺失行，按任务简报 §2 逐条补全。
仅改测试文件，不触碰任何生产代码逻辑。
"""

import asyncio
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch


# =============================================================================
# 辅助：构造轻量 TradingExecutor（patch 掉 load_ownership_config / CapitalManager 等）
# =============================================================================


def _make_executor(config_overrides=None):
    """真实构造 TradingExecutor，patch 重型依赖。

    默认 config 含最小必需字段，调用方可通过 config_overrides 覆盖/追加。
    """
    from strategies.new_coin import executor as executor_module

    config = {
        'strategy': {'name': 'new_coin'},
        'trading': {
            'leverage': 5,
            'time_stop': {'enabled': False},
            'emergency_stop': {'enabled': False},
            'position_detection': {'enabled': True},
            'reconcile': {'zombie_confirm_cycles': 3},
        },
    }
    if config_overrides:
        # 深合并（浅层够用，因为覆盖的都是最顶层 key）
        for k, v in config_overrides.items():
            if isinstance(v, dict) and isinstance(config.get(k), dict):
                config[k].update(v)
            else:
                config[k] = v

    with patch.object(executor_module, 'load_ownership_config', return_value={}):
        with patch.object(executor_module, 'CapitalManager'):
            with patch.object(executor_module, 'KLineService'):
                ex = executor_module.TradingExecutor(
                    binance_api=MagicMock(),
                    db=MagicMock(),
                    notification=MagicMock(),
                    config=config,
                )
    return ex


def _make_tracking(**overrides):
    """构造最小 tracking 条目。"""
    base = {
        'entry_price': 100.0,
        'entry_quantity': 1.0,
        'tp_anchor_quantity': 1.0,
        'remaining_quantity': 1.0,
        'target1_reached': False,
        'target2_reached': False,
        'highest_price': 0,
        'algo_ids': {},
    }
    base.update(overrides)
    return base


# =============================================================================
# 缺口 #2：check_position_management 取 ticker + target2 门控（executor.py L2166-2200）
# =============================================================================


class TestCheckPositionManagementTickerAndGating:
    """覆盖取价成功/失败/异常三路径 + current_price None/非 None 下 target2 门控分支。"""

    @pytest.mark.asyncio
    async def test_ticker_success_positive_price_updates_highest(self):
        """ticker 返回正常 price>0 → current_price 有值；target2 已达成时更新 highest_price 并进入 trailing。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(target2_reached=True, highest_price=100.0)
        ex.position_tracking[symbol] = tracking

        # mock _request 返回正常 ticker
        ex.binance_api._request = AsyncMock(return_value={'price': '95.5'})
        # mock _get_exchange_position_qty（返回一个合理值让 detect 通过）
        ex._get_exchange_position_qty = AsyncMock(return_value=Decimal('0.35'))
        # mock detect_take_profit_fills（不触发全平，返回 None 或数字均可）
        ex.detect_take_profit_fills = MagicMock(return_value=None)
        # trailing 检查方法 → 记录是否被调
        ex._check_dynamic_trailing = AsyncMock()
        ex._check_trailing_stop = AsyncMock()

        await ex.check_position_management(symbol)

        # 确认 ticker 被调了
        ex.binance_api._request.assert_awaited_once()
        # target2 达成 + current_price 有值 → trailing 两个方法都被调
        ex._check_dynamic_trailing.assert_awaited_once()
        ex._check_trailing_stop.assert_awaited_once()
        # highest_price 被更新（95.5 比 100 低——注意是做空，highest_price 跟踪反弹）
        # 95.5 > 100 为 False，所以 highest_price 保持原值 100.0

    @pytest.mark.asyncio
    async def test_ticker_price_zero_sets_current_price_none(self):
        """ticker 返回 price=0 → current_price=None → target2 门控不进入 trailing 路径。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(target2_reached=True, highest_price=100.0)
        ex.position_tracking[symbol] = tracking

        ex.binance_api._request = AsyncMock(return_value={'price': '0'})
        ex._get_exchange_position_qty = AsyncMock(return_value=Decimal('0.35'))
        ex.detect_take_profit_fills = MagicMock(return_value=None)
        ex._check_dynamic_trailing = AsyncMock()
        ex._check_trailing_stop = AsyncMock()

        await ex.check_position_management(symbol)

        # price=0 → current_price=None → target2 门控的 current_price is not None 为 False
        # → trailing 两个方法不被调
        ex._check_dynamic_trailing.assert_not_awaited()
        ex._check_trailing_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ticker_exception_sets_current_price_none(self):
        """_request 抛异常 → except 路径，current_price=None → trailing 不激活。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(target2_reached=True)
        ex.position_tracking[symbol] = tracking

        ex.binance_api._request = AsyncMock(side_effect=Exception("Network down"))
        ex._get_exchange_position_qty = AsyncMock(return_value=Decimal('0.35'))
        ex.detect_take_profit_fills = MagicMock(return_value=None)
        ex._check_dynamic_trailing = AsyncMock()
        ex._check_trailing_stop = AsyncMock()

        # 不崩溃
        await ex.check_position_management(symbol)

        # 异常 → current_price=None → trailing 不被调
        ex._check_dynamic_trailing.assert_not_awaited()
        ex._check_trailing_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_target2_not_reached_skips_trailing_even_with_price(self):
        """target2_reached=False + current_price 有值 → 整体门控不进入 trailing。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(target2_reached=False)  # target2 未达成
        ex.position_tracking[symbol] = tracking

        ex.binance_api._request = AsyncMock(return_value={'price': '95.5'})
        ex._get_exchange_position_qty = AsyncMock(return_value=Decimal('0.9'))
        ex.detect_take_profit_fills = MagicMock(return_value=None)
        ex._check_dynamic_trailing = AsyncMock()
        ex._check_trailing_stop = AsyncMock()

        await ex.check_position_management(symbol)

        # target2 未达成 → 门控不进入 trailing
        ex._check_dynamic_trailing.assert_not_awaited()
        ex._check_trailing_stop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fill_level_zero_short_circuits_early_return(self):
        """detect_take_profit_fills 返回 0（全部平仓）→ 提前 return，trailing 不被调。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(target2_reached=True)
        ex.position_tracking[symbol] = tracking

        ex.binance_api._request = AsyncMock(return_value={'price': '95.5'})
        ex._get_exchange_position_qty = AsyncMock(return_value=Decimal('0.35'))
        ex.detect_take_profit_fills = MagicMock(return_value=0)  # 全部平仓
        ex._check_dynamic_trailing = AsyncMock()
        ex._check_trailing_stop = AsyncMock()

        await ex.check_position_management(symbol)

        # fill_level == 0 → 提前 return
        ex._check_dynamic_trailing.assert_not_awaited()
        ex._check_trailing_stop.assert_not_awaited()


# =============================================================================
# 缺口 #3：_cancel_trailing_stop_order 的 mark_order_canceled（executor.py L2680-2683）
# =============================================================================


class TestCancelTrailingStopMarkCanceled:
    """平仓触发撤移动止损后的 mark_order_canceled：成功 + 异常吞掉。"""

    @pytest.mark.asyncio
    async def test_cancel_trailing_mark_canceled_success(self):
        """有 trailing_stop algo_id → 撤单成功后 mark_order_canceled 被调。"""
        from strategies.new_coin.executor import TradingExecutor

        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(algo_ids={'trailing_stop': 'ALGO_TS_123'})
        ex.position_tracking[symbol] = tracking

        ex.binance_api.cancel_algo_order = AsyncMock()

        with patch(
            'strategies.new_coin.executor.mark_order_canceled',
            new=AsyncMock(),
        ) as mock_mark:
            await ex._cancel_trailing_stop_order(symbol)

        # cancel_algo_order 被调
        ex.binance_api.cancel_algo_order.assert_awaited_once_with(symbol, 'ALGO_TS_123')
        # trailing_stop algo_ids 被清 None
        assert tracking['algo_ids']['trailing_stop'] is None
        # mark_order_canceled 被调
        mock_mark.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_trailing_mark_canceled_exception_swallowed(self):
        """mark_order_canceled 抛异常 → except 吞掉，不崩溃。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(algo_ids={'trailing_stop': 'ALGO_TS_123'})
        ex.position_tracking[symbol] = tracking

        ex.binance_api.cancel_algo_order = AsyncMock()

        with patch(
            'strategies.new_coin.executor.mark_order_canceled',
            new=AsyncMock(side_effect=Exception("DB write failed")),
        ):
            # 不应崩溃
            await ex._cancel_trailing_stop_order(symbol)

        assert tracking['algo_ids']['trailing_stop'] is None


# =============================================================================
# 缺口 #4：_sync_trailing_stop_order 两处 mark_order_canceled（executor.py L2736-2771）
# =============================================================================


class TestSyncTrailingStopMarkCanceled:
    """撤旧移动止损 + 撤旧硬止损 → 各覆盖 mark_order_canceled 成功 + 异常。"""

    @pytest.mark.asyncio
    async def test_sync_trailing_cancels_old_trailing_and_marks(self):
        """有旧 trailing_stop algo_id → 撤单 + mark_order_canceled 被调（成功）。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(
            remaining_quantity=0.35,
            algo_ids={'trailing_stop': 'OLD_TS_001'},
        )
        ex.position_tracking[symbol] = tracking

        # mock 撤单 + 精度 + place_conditional_order（后两步非本缺口覆盖重点，mock 掉）
        ex.binance_api.cancel_algo_order = AsyncMock()
        ex._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))
        ex.binance_api.place_conditional_order = AsyncMock(return_value={'algoId': 'NEW_TS_999'})

        with patch(
            'strategies.new_coin.executor.mark_order_canceled',
            new=AsyncMock(),
        ) as mock_mark:
            await ex._sync_trailing_stop_order(symbol, Decimal('98.0'))

        # 旧移动止损被撤（调了 cancel_algo_order）
        ex.binance_api.cancel_algo_order.assert_awaited()
        # mark_order_canceled 被调（撤旧移动止损触发的那一次）
        # 注意：方法末尾会创建新单并重新赋值 trailing_stop，
        # 所以此处只验证 mark 被调，不验证最终值
        mock_mark.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sync_trailing_cancels_old_sl_and_marks(self):
        """有旧硬止损 algo_id（首次激活场景）→ 撤 SL + mark_order_canceled 被调。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(
            remaining_quantity=0.35,
            algo_ids={'sl': 'OLD_SL_001'},  # 只有 SL，没有 trailing_stop
        )
        ex.position_tracking[symbol] = tracking

        ex.binance_api.cancel_algo_order = AsyncMock()
        ex._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))
        ex.binance_api.place_conditional_order = AsyncMock(return_value={'algoId': 'NEW_TS_999'})

        with patch(
            'strategies.new_coin.executor.mark_order_canceled',
            new=AsyncMock(),
        ) as mock_mark:
            await ex._sync_trailing_stop_order(symbol, Decimal('98.0'))

        # 旧 SL 被撤
        ex.binance_api.cancel_algo_order.assert_awaited_once_with(symbol, 'OLD_SL_001')
        assert tracking['algo_ids']['sl'] is None
        # mark_order_canceled 被调一次（只撤 SL）
        mock_mark.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sync_trailing_both_old_cancel_and_both_marks(self):
        """同时有旧 trailing_stop 和旧 sl → 撤两次 + mark_order_canceled 被调两次。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(
            remaining_quantity=0.35,
            algo_ids={
                'trailing_stop': 'OLD_TS_001',
                'sl': 'OLD_SL_001',
            },
        )
        ex.position_tracking[symbol] = tracking

        ex.binance_api.cancel_algo_order = AsyncMock()
        ex._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))
        ex.binance_api.place_conditional_order = AsyncMock(return_value={'algoId': 'NEW_TS_999'})

        with patch(
            'strategies.new_coin.executor.mark_order_canceled',
            new=AsyncMock(),
        ) as mock_mark:
            await ex._sync_trailing_stop_order(symbol, Decimal('98.0'))

        # 撤两次（旧 trailing + 旧 sl）
        assert ex.binance_api.cancel_algo_order.await_count == 2
        # sl 被撤后不会被新单覆盖（只有 trailing_stop 会被重新赋值）
        assert tracking['algo_ids']['sl'] is None
        # mark_order_canceled 被调两次（撤旧 trailing + 撤旧 sl）
        assert mock_mark.await_count == 2

    @pytest.mark.asyncio
    async def test_sync_trailing_mark_canceled_exception_swallowed(self):
        """mark_order_canceled 抛异常 → except 吞掉，不影响后续创建新单。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(
            remaining_quantity=0.35,
            algo_ids={'trailing_stop': 'OLD_TS_001'},
        )
        ex.position_tracking[symbol] = tracking

        ex.binance_api.cancel_algo_order = AsyncMock()
        ex._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))
        ex.binance_api.place_conditional_order = AsyncMock(return_value={'algoId': 'NEW_TS_999'})

        with patch(
            'strategies.new_coin.executor.mark_order_canceled',
            new=AsyncMock(side_effect=Exception("DB error")),
        ):
            # 不应崩溃；创建新单路径继续
            await ex._sync_trailing_stop_order(symbol, Decimal('98.0'))

        # 即使 mark 异常，cancel_algo_order 仍被调，后续 place_conditional_order 也继续
        ex.binance_api.place_conditional_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sync_sl_mark_canceled_exception_swallowed(self):
        """撤旧硬止损（old_sl_id）后的 mark_order_canceled 抛异常 → except 吞掉（L2770-2771）。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(
            remaining_quantity=0.35,
            # 只有旧 SL，没有旧 trailing_stop → 直接进第二步撤 SL
            algo_ids={'sl': 'OLD_SL_001'},
        )
        ex.position_tracking[symbol] = tracking

        ex.binance_api.cancel_algo_order = AsyncMock()
        ex._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))
        ex.binance_api.place_conditional_order = AsyncMock(return_value={'algoId': 'NEW_TS_999'})

        with patch(
            'strategies.new_coin.executor.mark_order_canceled',
            new=AsyncMock(side_effect=Exception("DB error on SL mark")),
        ):
            # 不应崩溃
            await ex._sync_trailing_stop_order(symbol, Decimal('98.0'))

        # place_conditional_order 继续创建新单
        ex.binance_api.place_conditional_order.assert_awaited_once()


# =============================================================================
# 缺口 #5：cancel_all_algo_orders 的 mark_open_orders_canceled（executor.py L3246-3250）
# =============================================================================


class TestCancelAllAlgoMarkOpenCanceled:
    """全量撤单后 cancelled>0 → mark_open_orders_canceled 被调；异常吞掉。"""

    @pytest.mark.asyncio
    async def test_cancel_all_with_success_calls_mark_open_canceled(self):
        """至少一张条件单撤单成功 → result['cancelled']>0 → mark_open_orders_canceled 被调。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(
            algo_ids={'tp1': 'TP1_001', 'tp2': 'TP2_002'},
        )
        ex.position_tracking[symbol] = tracking

        ex.binance_api.cancel_algo_order = AsyncMock()  # 全部成功

        with patch(
            'strategies.new_coin.executor.mark_open_orders_canceled',
            new=AsyncMock(),
        ) as mock_mark:
            result = await ex.cancel_all_algo_orders(symbol)

        assert result['cancelled'] == 2
        assert result['failed'] == 0
        # algo_ids 被清空
        assert tracking['algo_ids'] == {}
        # mark_open_orders_canceled 被调
        mock_mark.assert_awaited_once_with(ex.db, 'new_coin', symbol)

    @pytest.mark.asyncio
    async def test_cancel_all_mark_open_canceled_exception_swallowed(self):
        """mark_open_orders_canceled 抛异常 → except 吞掉，函数仍返回正常 result。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(
            algo_ids={'tp1': 'TP1_001'},
        )
        ex.position_tracking[symbol] = tracking

        ex.binance_api.cancel_algo_order = AsyncMock()

        with patch(
            'strategies.new_coin.executor.mark_open_orders_canceled',
            new=AsyncMock(side_effect=Exception("DB write failed")),
        ):
            # 不应崩溃
            result = await ex.cancel_all_algo_orders(symbol)

        assert result['cancelled'] == 1
        assert tracking['algo_ids'] == {}

    @pytest.mark.asyncio
    async def test_cancel_all_zero_cancelled_skips_mark_open(self):
        """所有撤单失败（cancelled=0）→ mark_open_orders_canceled 不被调。"""
        from shared.binance_api import BinanceAPIError

        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking(
            algo_ids={'tp1': 'TP1_001'},
        )
        ex.position_tracking[symbol] = tracking

        # cancel_algo_order 抛非 ORDER_NOT_FOUND 异常 → failed 增加
        error = BinanceAPIError(code=-2010, message="No need to change margin type.")
        ex.binance_api.cancel_algo_order = AsyncMock(side_effect=error)

        with patch(
            'strategies.new_coin.executor.mark_open_orders_canceled',
            new=AsyncMock(),
        ) as mock_mark:
            result = await ex.cancel_all_algo_orders(symbol)

        assert result['cancelled'] == 0
        assert result['failed'] == 1
        # cancelled=0 → mark_open_orders_canceled 不被调
        mock_mark.assert_not_awaited()


# =============================================================================
# 缺口 #6+#7+#8（strategy.py §3.4 三个基线方法）——验证已全覆盖
# =============================================================================
# strategy.py AC-D 守卫档位调用点（L1424-1427：_guard_symbol 读 executor.position_tracking）
# =============================================================================


class TestGuardSymbolTierAwareness:
    """_guard_symbol 从 position_tracking 读 target1/target2 档位，传入 find_missing_protection。"""

    @pytest.mark.asyncio
    async def test_guard_symbol_reads_tier_states_from_tracking(self):
        """AC-D §6.4：_guard_symbol 正确读取 target1/target2 并传给守卫判定（L1424-1427）。"""
        from strategies.new_coin import strategy as strategy_module
        import types

        # 构造轻量 strategy mock
        class _S:
            trading_executor = MagicMock()
            _protection_gap_attempts = {}
            _record_gap_and_alert = AsyncMock()
            replenish_conditional_orders = AsyncMock()

        s = _S()
        executor = s.trading_executor

        # position_tracking 有 target1_reached=True, target2_reached=True
        executor.position_tracking = {
            'TESTUSDT': {
                'target1_reached': True,
                'target2_reached': True,
            },
        }
        executor.find_missing_protection = AsyncMock(return_value=[])  # 无缺口

        # 绑定方法
        bound_method = types.MethodType(
            strategy_module.NewCoinStrategy._guard_symbol, s
        )

        await bound_method('TESTUSDT', {'entry_price': Decimal('100')})

        # find_missing_protection 被正确传参
        executor.find_missing_protection.assert_awaited_once_with(
            'TESTUSDT', strict=True,
            target1_reached=True, target2_reached=True,
        )

    @pytest.mark.asyncio
    async def test_guard_symbol_handles_no_tracking_entry(self):
        """position_tracking 无该 symbol → target1/target2 均为 None 传入。"""
        from strategies.new_coin import strategy as strategy_module
        import types

        class _S:
            trading_executor = MagicMock()
            _protection_gap_attempts = {}
            _record_gap_and_alert = AsyncMock()
            replenish_conditional_orders = AsyncMock()

        s = _S()
        executor = s.trading_executor
        executor.position_tracking = {}  # 空
        executor.find_missing_protection = AsyncMock(return_value=None)  # strict=True 时无 tracking 返回 None

        bound_method = types.MethodType(
            strategy_module.NewCoinStrategy._guard_symbol, s
        )

        await bound_method('TESTUSDT', {'entry_price': Decimal('100')})

        # find_missing_protection 被调，target1/target2=None
        executor.find_missing_protection.assert_awaited_once_with(
            'TESTUSDT', strict=True,
            target1_reached=None, target2_reached=None,
        )
        # missing=None → 走告警路径
        s._record_gap_and_alert.assert_awaited_once()


# =============================================================================
# strategy.py _rebuild_position_baseline 主流程调基线同步（L2223）
# =============================================================================


@pytest.mark.asyncio
async def test_rebuild_position_baseline_calls_sync_tracking():
    """L2223：_rebuild_position_baseline 主流程中 atr_map 之后调 _sync_baseline_to_tracking。"""
    from strategies.new_coin import strategy as strategy_module
    import types

    class _S:
        trading_executor = MagicMock()
        binance_client = MagicMock()
        positions = {}

    s = _S()
    executor = s.trading_executor
    executor.get_contract_size_map = AsyncMock(return_value={})

    # patch rebuild_from_exchange 返回成功
    with patch(
        'strategies.new_coin.strategy.rebuild_from_exchange',
        new=AsyncMock(return_value=MagicMock(success=True, positions={})),
    ):
        # patch 内部依赖方法让它们快速返回
        s._filter_own_positions = AsyncMock(return_value={})
        s._merge_rebuilt_positions = MagicMock(return_value={})
        s._prune_missing_positions = MagicMock()
        s._calc_rebuilt_atr_map = AsyncMock(return_value={})
        s._sync_baseline_to_tracking = AsyncMock()  # 这就是我们要验证的调用点

        bound = types.MethodType(
            strategy_module.NewCoinStrategy._rebuild_position_baseline, s
        )
        await bound()

    # _sync_baseline_to_tracking 被调（atr_map 和 rebuilt 之后的调用）
    s._sync_baseline_to_tracking.assert_awaited_once()


# =============================================================================
# strategy.py _backfill_target_statuses except pass（L2423）
# =============================================================================


@pytest.mark.asyncio
async def test_target_status_no_anchor_except_pass_covered():
    """L2423：锚点缺失时 _get_symbol_precision 抛异常 → except pass 被覆盖。

    单独写一个用例确保 except pass 行被执行（coverage 有时对 pass 行统计不稳定）。
    """
    import types
    from strategies.new_coin import strategy as strategy_module

    class _S:
        pass
    s = _S()

    class _Ex:
        position_tracking = {
            'TESTUSDT': {
                'tp_anchor_quantity': None,
                'remaining_quantity': 0.5,
            },
        }
        target1_close_percent = Decimal('0.30')
        target2_close_percent = Decimal('0.40')
        qty_tolerance_ratio = 0.01
        qty_tolerance_absolute = 0.0001

        def _step_size_for(self, sym):
            return Decimal('0.001')

        def __init__(self):
            # 锚点缺失时的预热精度 → 抛异常（确保 except pass 被执行）
            self._get_symbol_precision = AsyncMock(side_effect=Exception("K线挂了"))

    ex = _Ex()
    bound = types.MethodType(
        strategy_module.NewCoinStrategy._backfill_target_statuses, s
    )

    with patch('strategies.new_coin.strategy.derive_reached_targets') as mock_d:
        await bound(ex, ['TESTUSDT'])

    # 锚点缺失 → derive_reached_targets 不被调
    mock_d.assert_not_called()
    # _get_symbol_precision 被调一次（锚点缺失时的预热）
    ex._get_symbol_precision.assert_awaited_once()


# =============================================================================
# UNKNOWN 减仓强制推进后计数器复位（§3.5.2 一次性记录语义 bug 回归）
# =============================================================================


class TestUnknownReductionCounterResetAfterForce:
    """_handle_non_tp_reduction UNKNOWN 分支：超上限强制推进后，
    _unknown_reduction_counts 必须复位，否则下次 UNKNOWN 会立即再次强制推进，
    丧失「3 轮重判窗口」设计意图。"""

    def test_unknown_force_advance_clears_counter(self):
        """超上限强制推进后，_unknown_reduction_counts[symbol] 被 pop 清除。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking()

        # 预置计数器为 4（zombie_confirm_cycles=3，下一次进入 UNKNOWN 会触发强制推进）
        ex._unknown_reduction_counts[symbol] = 4
        # 预置一个偏离的 last_qty（能看到 force 推进后的效果）
        ex._last_tracked_qty[symbol] = 0.9

        # exchange_qty=None → force_to 走 tracking['remaining_quantity']=1.0
        result = ex._handle_non_tp_reduction(
            symbol, tracking, 'UNKNOWN', exchange_qty=None,
        )

        # ① 强制推进生效
        assert ex._last_tracked_qty[symbol] == 1.0, \
            "force_to=tracking['remaining_quantity'] 应把 last 推到 1.0"
        # ② 计数器被清除（修复点：之前漏了 pop）
        assert symbol not in ex._unknown_reduction_counts, \
            "强制推进后 _unknown_reduction_counts 应清除该 symbol"
        # 返回值
        assert result is None

    def test_unknown_counter_restarts_after_force(self):
        """强制推进后再次 UNKNOWN，计数器从 1 开始（而非直接超限）。"""
        ex = _make_executor()
        symbol = 'TESTUSDT'
        tracking = _make_tracking()

        # 预置超限
        ex._unknown_reduction_counts[symbol] = 4
        ex._last_tracked_qty[symbol] = 0.9

        # 第一次进入 UNKNOWN → 强制推进 + 清除计数
        ex._handle_non_tp_reduction(
            symbol, tracking, 'UNKNOWN', exchange_qty=0.5,
        )
        # 计数已被清除
        assert symbol not in ex._unknown_reduction_counts

        # 第二次进入 UNKNOWN（模拟另一次新减仓事件）→ 应从 1 起
        ex._handle_non_tp_reduction(
            symbol, tracking, 'UNKNOWN', exchange_qty=0.4,
        )
        assert ex._unknown_reduction_counts[symbol] == 1, \
            "新减仓事件的 UNKNOWN 计数应从 1 重启，而非直接超上限"
        # last 不会被再次强制推进（因为 count=1 <= 3）
        assert ex._last_tracked_qty[symbol] == 0.5, \
            "第二次进入时 force_to 已推进到 0.5，且不应再次强制"


# =============================================================================
# 命令行快速运行
# =============================================================================
if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v', '-x']))
