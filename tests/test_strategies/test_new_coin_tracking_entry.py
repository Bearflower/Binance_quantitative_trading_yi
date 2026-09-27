"""
新币做空策略 position_tracking 条目完整性 - 资金路径缺陷回归测试

背景（2026-09-24 线上缺陷，容器 trading_system-new_coin）：
    重启恢复路径曾只创建 {'algo_ids': {}} 残缺条目，导致读侧：
    - executor._mark_partial_close 写回 remaining_quantity 抛 KeyError（假失败日志，状态未更新）
    - update_target_status 的 `*=` 抛 KeyError
    - _check_trailing_stop 因 remaining_quantity 缺失被当作 0 而静默漏平尾仓

本测试覆盖：
    1. 复现与回归：残缺条目下三条读路径不再异常 / 不再静默漏平
    2. 工厂一致性：三条创建路径产出的条目字段集合完全一致
    3. ensure_tracking_entry 幂等：已有值不被覆盖
    4. algo_ids 可变对象隔离
    5. _sync_baseline_to_tracking 经工厂建 / 补条目并保留既有状态
"""
import sys
import os
import pytest
from decimal import Decimal
from datetime import datetime
from unittest.mock import AsyncMock, patch

# 复用同目录既有测试的配置与执行器工厂，避免重复代码
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

from test_take_profit_fill_detect import create_executor  # noqa: E402
from strategies.new_coin.strategy import NewCoinStrategy  # noqa: E402


# ============================================================================
# 1. 复现与回归：残缺条目下读侧不得异常 / 不得静默漏平
# ============================================================================

class TestResidualEntryReadPaths:
    """重启恢复的残缺条目（仅 algo_ids）下，读侧行为回归"""

    def test_mark_partial_close_no_keyerror_and_writes_back(self):
        """TP1 市价平仓回写：修复前抛 KeyError，修复后正确回写"""
        executor = create_executor()
        # 残缺条目：有 entry_quantity 但缺 remaining_quantity
        executor.position_tracking["APLDUSDT"] = {'algo_ids': {}, 'entry_quantity': 100.0}

        executor._mark_partial_close("APLDUSDT", 30.0, 1)  # 修复前此处 KeyError

        entry = executor.position_tracking["APLDUSDT"]
        assert entry['remaining_quantity'] == pytest.approx(70.0)
        assert entry['target1_reached'] is True
        assert executor._last_tracked_qty["APLDUSDT"] == pytest.approx(70.0)

    def test_mark_partial_close_only_algo_ids_does_not_raise(self):
        """仅有 algo_ids 的极端残缺条目：不得抛异常，且不覆盖 algo_ids"""
        executor = create_executor()
        executor.position_tracking["APLDUSDT"] = {'algo_ids': {'sl': 'algo-1'}}

        executor._mark_partial_close("APLDUSDT", 30.0, 2)  # 修复前此处 KeyError

        entry = executor.position_tracking["APLDUSDT"]
        assert 'remaining_quantity' in entry
        assert entry['target2_reached'] is True
        assert entry['algo_ids'] == {'sl': 'algo-1'}

    def test_update_target_status_no_keyerror(self):
        """按比例估算剩余量：修复前 `*=` 抛 KeyError，修复后仅告警不抛异常"""
        executor = create_executor()
        executor.position_tracking["APLDUSDT"] = {'algo_ids': {}}

        executor.update_target_status("APLDUSDT", 1)  # 修复前此处 KeyError

        entry = executor.position_tracking["APLDUSDT"]
        assert entry['target1_reached'] is True
        # 无法估算时保持缺失（不再抛异常、也不伪造数值）
        assert 'remaining_quantity' not in entry

    @pytest.mark.asyncio
    async def test_trailing_stop_falls_back_to_last_tracked_qty(self):
        """移动止盈读侧：剩余量缺失时回退到最近跟踪数量并平仓（修复前静默漏平）"""
        executor = create_executor()
        executor.position_tracking["APLDUSDT"] = {'algo_ids': {}}  # 残缺条目
        executor._last_tracked_qty["APLDUSDT"] = 100.0
        executor.binance_api._request = AsyncMock(return_value={'price': '10'})
        executor._close_position = AsyncMock(return_value=True)

        await executor._check_trailing_stop("APLDUSDT")

        executor._close_position.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trailing_stop_attempts_close_when_all_missing(self):
        """剩余量与最近跟踪数量均缺失时，交由 _close_position 依据交易所决策（不漏平）"""
        executor = create_executor()
        executor.position_tracking["APLDUSDT"] = {'algo_ids': {}}
        executor.binance_api._request = AsyncMock(return_value={'price': '10'})
        executor._close_position = AsyncMock(return_value=True)

        await executor._check_trailing_stop("APLDUSDT")

        executor._close_position.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trailing_stop_skips_when_remaining_explicit_zero(self):
        """剩余量显式为 0（确已平完）时应跳过平仓，避免多余下单"""
        executor = create_executor()
        executor.position_tracking["APLDUSDT"] = {'algo_ids': {}, 'remaining_quantity': 0.0}
        executor.binance_api._request = AsyncMock(return_value={'price': '10'})
        executor._close_position = AsyncMock(return_value=True)

        await executor._check_trailing_stop("APLDUSDT")

        executor._close_position.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replenish_tp1_market_close_hits_line_3080(self):
        """补单时 TP1 已触发走市价平仓：真实复现 L3080 写回路径（修复前 KeyError 被吞）"""
        executor = create_executor()
        # 模拟 _sync_baseline_to_tracking 写入 entry_quantity 后、remaining_quantity 仍缺失的状态
        executor.position_tracking["APLDUSDT"] = {'algo_ids': {}, 'entry_quantity': 1.0}

        async def fake_request(method, path, **kwargs):
            if path.endswith('/positionRisk'):
                return [{'symbol': 'APLDUSDT', 'positionAmt': '-1.0'}]
            return {'price': '90'}  # 当前价已低于 TP1/TP2 目标价 → 走市价平仓分支

        executor.binance_api._request = AsyncMock(side_effect=fake_request)
        executor.binance_api.place_order = AsyncMock(return_value={'orderId': '1'})
        executor.binance_api.place_conditional_order = AsyncMock(return_value={'algoId': 'o-1'})
        executor.cancel_all_algo_orders = AsyncMock()
        executor._calculate_atr = AsyncMock(return_value=Decimal('2'))
        executor._get_symbol_precision = AsyncMock(return_value=(Decimal('0.01'), Decimal('0.001')))

        with patch('strategies.new_coin.executor.record_condition_order', new=AsyncMock()):
            ok = await executor.replenish_conditional_orders("APLDUSDT", Decimal('100'))

        entry = executor.position_tracking["APLDUSDT"]
        assert ok is True
        # 修复前：L3080 KeyError 被内层 except 吞掉 → remaining_quantity 缺失、target1_reached 未置位
        assert entry['remaining_quantity'] == pytest.approx(0.3)
        assert entry['target1_reached'] is True
        assert entry['target2_reached'] is True


# ============================================================================
# 2. 工厂一致性 / 3. 幂等 / 4. 可变对象隔离
# ============================================================================

class TestTrackingEntryFactory:
    """统一条目工厂：字段集一致、幂等、可变对象隔离"""

    def test_three_creation_paths_have_identical_keys(self):
        """正常开仓 / 补单（工厂直建）、ensure 新建、重启补齐三条路径字段集一致"""
        executor = create_executor()
        # 路径1：工厂直接构建（正常开仓与补单共用同一工厂）
        built = executor._build_tracking_entry(entry_price=100.0, entry_quantity=1.0, atr=2.0)
        # 路径2：ensure 新建
        created = executor.ensure_tracking_entry("AAAUSDT", entry_price=100.0, entry_quantity=1.0, atr=2.0)
        # 路径3：重启恢复为残缺条目后经 ensure 补齐
        executor.position_tracking["BBBUSDT"] = {'algo_ids': {}}
        patched = executor.ensure_tracking_entry("BBBUSDT", entry_price=100.0, entry_quantity=1.0, atr=2.0)

        assert set(built) == set(created) == set(patched)
        assert built['remaining_quantity'] == built['entry_quantity'] == 1.0
        assert created['remaining_quantity'] == created['entry_quantity']
        assert patched['remaining_quantity'] == patched['entry_quantity']

    def test_ensure_is_idempotent_and_preserves_existing_values(self):
        """连续两次 ensure：已有值（algo_ids / lowest_price / entry_price）不被覆盖"""
        executor = create_executor()
        entry = executor.ensure_tracking_entry("AAAUSDT", entry_price=100.0, entry_quantity=1.0, atr=2.0)
        entry['algo_ids']['sl'] = 'algo-1'
        entry['lowest_price'] = 88.0

        again = executor.ensure_tracking_entry(
            "AAAUSDT", entry_price=999.0, entry_quantity=999.0, atr=999.0
        )

        assert again is entry
        assert again['algo_ids'] == {'sl': 'algo-1'}
        assert again['lowest_price'] == 88.0
        assert again['entry_price'] == 100.0
        assert again['entry_quantity'] == 1.0

    def test_algo_ids_are_isolated_between_entries(self):
        """两个条目的 algo_ids 必须是独立可变对象，互不串扰"""
        executor = create_executor()
        a = executor._build_tracking_entry(entry_price=100.0, entry_quantity=1.0, atr=2.0)
        b = executor._build_tracking_entry(entry_price=100.0, entry_quantity=1.0, atr=2.0)
        a['algo_ids']['sl'] = 'x'
        assert b['algo_ids'] == {}

        executor.ensure_tracking_entry("AAAUSDT")
        executor.ensure_tracking_entry("BBBUSDT")
        executor.position_tracking["AAAUSDT"]['algo_ids']['tp1'] = 'y'
        assert executor.position_tracking["BBBUSDT"]['algo_ids'] == {}


# ============================================================================
# 5. 基线同步经工厂建 / 补条目
# ============================================================================

class TestSyncBaselineToTracking:
    """_sync_baseline_to_tracking 经 ensure_tracking_entry 建 / 补条目"""

    def test_sync_sets_remaining_and_preserves_existing_state(self):
        """写入真实剩余量；已恢复的 algo_ids 与 atr 保留；entry_time 转 aware datetime"""
        executor = create_executor()
        # 模拟重启时已从 condition_orders 恢复的条件单
        executor.position_tracking["APLDUSDT"] = {
            'algo_ids': {'db_TAKE_PROFIT_1': 'algo-1'},
            'atr': 3.0,
        }
        strategy = NewCoinStrategy.__new__(NewCoinStrategy)  # 跳过重量级 __init__
        rebuilt = {
            "APLDUSDT": {
                'entry_price': 50.0,
                'quantity': 200.0,
                'entry_time': '2026-09-24T02:00:00+00:00',
            }
        }

        strategy._sync_baseline_to_tracking(executor, rebuilt)

        entry = executor.position_tracking["APLDUSDT"]
        assert entry['remaining_quantity'] == 200.0
        assert entry['entry_quantity'] == 200.0
        assert entry['entry_price'] == 50.0
        assert entry['algo_ids'] == {'db_TAKE_PROFIT_1': 'algo-1'}  # 保留已恢复的条件单
        assert entry['atr'] == 3.0  # 保留既有 atr
        assert isinstance(entry['entry_time'], datetime)
        assert entry['entry_time'].tzinfo is not None  # 必须为 aware datetime
