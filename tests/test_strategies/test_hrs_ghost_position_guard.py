"""
HRS 幽灵持仓补单守卫回归测试（2026-10-10 修复）

缺陷背景：
    HRS 在 target2_reached=True 时，每个监控循环都会重建 tp2_trailing 的
    STOP_MARKET closePosition 条件单。当交易所已无持仓（本地幽灵持仓未同步）时，
    该 closePosition 单会持续被拒：[-4509] TIF GTE can only be used with open positions。

修复口径：
    1. _monitor_positions 以「交易所实际持仓」为唯一判据，取数失败/无持仓时不再补单。
    2. _replenish_single_position 在交易所零持仓时清理本地幽灵持仓并撤单后返回；
       取数失败时直接返回，不盲目下单。
    3. PositionManager.from_dict 恢复 _last_tracked_qty，使重启后能识别「已全部平仓」。

测试覆盖：
    - _replenish_single_position：零持仓清理（含 PnL 回写）、取数失败跳过、有持仓正常补单
    - _monitor_positions：取数失败不补单、有持仓且 target2_reached 才补单
    - _get_exchange_position_qty：有/无持仓取值、重试耗尽返回 None、瞬时失败可恢复
    - PositionManager.from_dict：恢复 _last_tracked_qty
"""
import os
import sys
import pytest
import yaml
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.abspath(PROJECT_ROOT))

from strategies.hrs.strategy import HRSStrategy
from strategies.hrs.position_manager import PositionManager

CONFIG_PATH = Path(__file__).parent.parent.parent / "strategies" / "hrs" / "config.yaml"
with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)

SYMBOL = "DRAMUSDT"


def _make_position(**overrides) -> dict:
    """构造一条本地跟踪持仓记录"""
    pos = {
        "direction": "short",
        "entry_price": 100.0,
        "entry_time": datetime.now(timezone.utc),
        "entry_quantity": 100.0,
        "atr": 2.0,
        "target1_reached": True,
        "target2_reached": True,
        "best_price": 90.0,
        "remaining_quantity": 30.0,
        "algo_ids": {"tp2_trailing": 12345},
    }
    pos.update(overrides)
    return pos


def _make_strategy(**pm_overrides) -> HRSStrategy:
    """构造注入了 mock 依赖的 HRSStrategy（不调用 initialize）

    真实持仓管理器逻辑不在本测试范围，统一用 MagicMock 控制其返回值，
    仅校验「是否触发补单/清理」这一行为契约。
    """
    strategy = HRSStrategy(CONFIG)
    # 测试中关闭重试等待，避免拖慢用例
    strategy._position_query_retry_delay = 0.0
    strategy._writeback_pnl_for_full_close = AsyncMock()

    mock_pm = MagicMock()
    mock_pm.zero_qty_threshold = 0.0001
    mock_pm.get_all_positions.return_value = {SYMBOL: _make_position()}
    mock_pm.detect_take_profit_fills.return_value = None
    mock_pm.check_time_stop.return_value = False
    mock_pm.cancel_all_orders = AsyncMock()
    for key, value in pm_overrides.items():
        setattr(mock_pm, key, value)

    strategy.position_manager = mock_pm

    mock_binance = MagicMock()
    mock_binance.get_ticker = AsyncMock(return_value={"lastPrice": "95"})
    strategy.binance_client = mock_binance

    mock_executor = MagicMock()
    mock_executor.replenish_position_orders = AsyncMock(return_value=MagicMock(note=""))
    strategy.trading_executor = mock_executor

    strategy.risk_manager = MagicMock()
    strategy._save_state = AsyncMock()
    strategy._should_unregister = MagicMock(return_value=False)
    return strategy


# ============================================================================
# _replenish_single_position
# ============================================================================

class TestReplenishSinglePositionGhostGuard:
    """_replenish_single_position() 幽灵持仓守卫"""

    @pytest.mark.asyncio
    async def test_zero_exchange_qty_cleans_ghost_and_skips_order(self):
        """交易所零持仓 → 撤单+清理本地持仓+保存状态，且不下任何补单"""
        strategy = _make_strategy()
        strategy.position_manager.get_position.return_value = _make_position()
        strategy.binance_client.get_position = AsyncMock(
            return_value=[{"symbol": SYMBOL, "positionAmt": "0.000"}]
        )

        await strategy._replenish_single_position(SYMBOL)

        # 清理前应先回写已实现盈亏（与全平路径对齐，避免丢失 PnL 记录）
        strategy._writeback_pnl_for_full_close.assert_awaited_once()
        strategy.position_manager.cancel_all_orders.assert_awaited_once_with(SYMBOL)
        strategy.position_manager.remove_position.assert_called_once_with(SYMBOL)
        strategy._save_state.assert_awaited_once()
        strategy.trading_executor.replenish_position_orders.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_position_error_skips_order(self):
        """取数失败 → 直接返回，既不补单也不清理（避免误删有效持仓）"""
        strategy = _make_strategy()
        strategy.position_manager.get_position.return_value = _make_position()
        strategy.binance_client.get_position = AsyncMock(side_effect=Exception("网络错误"))

        await strategy._replenish_single_position(SYMBOL)

        strategy.trading_executor.replenish_position_orders.assert_not_called()
        strategy.position_manager.cancel_all_orders.assert_not_awaited()
        strategy.position_manager.remove_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_real_position_proceeds_with_replenish(self):
        """交易所仍有持仓 → 正常执行补单"""
        strategy = _make_strategy()
        strategy.position_manager.get_position.return_value = _make_position()
        strategy.binance_client.get_position = AsyncMock(
            return_value=[{"symbol": SYMBOL, "positionAmt": "-30.000"}]
        )

        await strategy._replenish_single_position(SYMBOL)

        strategy.trading_executor.replenish_position_orders.assert_awaited_once()
        strategy.position_manager.remove_position.assert_not_called()


# ============================================================================
# _monitor_positions
# ============================================================================

class TestMonitorPositionsGhostGuard:
    """_monitor_positions() 补单守卫"""

    @pytest.mark.asyncio
    async def test_fetch_error_does_not_replenish(self):
        """取数异常时 position_open 保持 False → 不触发补单（原缺陷点）"""
        strategy = _make_strategy()
        strategy.binance_client.get_position = AsyncMock(side_effect=Exception("网络错误"))
        strategy._replenish_single_position = AsyncMock()

        await strategy._monitor_positions()

        strategy._replenish_single_position.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_target2_reached_with_position_replenishes(self):
        """仍有持仓且 target2_reached=True → 触发补单（移动止盈需每轮重建）"""
        strategy = _make_strategy()
        strategy.binance_client.get_position = AsyncMock(
            return_value=[{"symbol": SYMBOL, "positionAmt": "-30.000"}]
        )
        strategy._replenish_single_position = AsyncMock()

        await strategy._monitor_positions()

        strategy._replenish_single_position.assert_awaited_once_with(SYMBOL)

    @pytest.mark.asyncio
    async def test_flat_position_does_not_replenish(self):
        """交易所零持仓但未被 detect 判为全平（返回 None）→ 仍不补单"""
        strategy = _make_strategy()
        strategy.binance_client.get_position = AsyncMock(
            return_value=[{"symbol": SYMBOL, "positionAmt": "0.000"}]
        )
        strategy.position_manager.detect_take_profit_fills.return_value = None
        strategy._replenish_single_position = AsyncMock()

        await strategy._monitor_positions()

        strategy._replenish_single_position.assert_not_awaited()


# ============================================================================
# PositionManager.from_dict
# ============================================================================

class TestPositionManagerFromDict:
    """PositionManager.from_dict() 恢复 _last_tracked_qty"""

    def test_from_dict_restores_last_tracked_qty(self):
        """恢复状态后 _last_tracked_qty 应为剩余数量，使 detect 能识别全平"""
        pm = PositionManager(CONFIG, binance_api=MagicMock(), db=None)

        pm.from_dict({"positions": {SYMBOL: _make_position(remaining_quantity=30.0)}})

        assert pm._last_tracked_qty[SYMBOL] == pytest.approx(30.0)

    def test_from_dict_fallbacks_to_entry_quantity(self):
        """缺少 remaining_quantity 时回退用 entry_quantity"""
        pm = PositionManager(CONFIG, binance_api=MagicMock(), db=None)
        pos = _make_position()
        pos.pop("remaining_quantity")

        pm.from_dict({"positions": {SYMBOL: pos}})

        assert pm._last_tracked_qty[SYMBOL] == pytest.approx(100.0)

    def test_from_dict_restored_state_detects_full_close(self):
        """恢复后 detect_take_profit_fills(0) 应返回 0（识别全平）"""
        pm = PositionManager(CONFIG, binance_api=MagicMock(), db=None)
        pm.from_dict({"positions": {SYMBOL: _make_position(remaining_quantity=30.0)}})

        assert pm.detect_take_profit_fills(SYMBOL, 0.0) == 0


# ============================================================================
# _get_exchange_position_qty
# ============================================================================

class TestGetExchangePositionQty:
    """_get_exchange_position_qty() 交易所持仓量查询（含轻量重试）"""

    @pytest.mark.asyncio
    async def test_returns_abs_qty_when_position_exists(self):
        """有持仓返回绝对值"""
        strategy = _make_strategy()
        strategy.binance_client.get_position = AsyncMock(
            return_value=[{"symbol": SYMBOL, "positionAmt": "-30.000"}]
        )

        assert await strategy._get_exchange_position_qty(SYMBOL) == pytest.approx(30.0)

    @pytest.mark.asyncio
    async def test_returns_zero_when_flat(self):
        """无持仓返回 0.0（确认为零持仓，非取数失败）"""
        strategy = _make_strategy()
        strategy.binance_client.get_position = AsyncMock(
            return_value=[{"symbol": SYMBOL, "positionAmt": "0.000"}]
        )

        assert await strategy._get_exchange_position_qty(SYMBOL) == 0.0

    @pytest.mark.asyncio
    async def test_returns_none_after_retries_exhausted(self):
        """持续异常返回 None，且按配置重试（max_retries=1 → 共 2 次调用）"""
        strategy = _make_strategy()
        strategy.binance_client.get_position = AsyncMock(side_effect=Exception("网络错误"))

        assert await strategy._get_exchange_position_qty(SYMBOL) is None
        assert strategy.binance_client.get_position.await_count == 2

    @pytest.mark.asyncio
    async def test_retry_recovers_transient_failure(self):
        """首次失败、重试成功 → 返回持仓量（不因瞬时抖动跳过整轮）"""
        strategy = _make_strategy()
        strategy.binance_client.get_position = AsyncMock(
            side_effect=[Exception("瞬时抖动"), [{"symbol": SYMBOL, "positionAmt": "-30.000"}]]
        )

        assert await strategy._get_exchange_position_qty(SYMBOL) == pytest.approx(30.0)