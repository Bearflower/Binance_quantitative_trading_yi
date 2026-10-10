"""
new_coin 无新币周期的持仓上报回归测试（2026-10-10 修复）。

缺陷背景：
    _execute_cycle 在「未检测到新币」时直接 return，跳过了第 4 步
    _monitor_positions（其内部保存状态 → 同步 strategy_open_positions）。
    因此只要近期没有新币上线，trading.strategy_open_positions /
    strategy_states 就停留在最后一次「有新币」周期的快照，导致看板持仓
    数量/保证金与实际不一致（如 VKTXUSDT 上报 2.47 而交易所实为 1.07）。

修复口径：
    把「监控持仓 + 保存上报 + 周报」抽为 _run_cycle_tail()，由各提前返回分支
    （无新币、回撤熔断暂停）在 return 前统一调用，保证每周期都同步持仓上报。

覆盖范围：
    1. 无新币时仍监控持仓并调用 _save_state（含 _sync_open_positions_to_db）。
    2. 回撤熔断暂停时仍监控持仓并保存上报，且不进入新币检测。
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from strategies.new_coin.strategy import NewCoinStrategy


def make_strategy(new_coins: list) -> NewCoinStrategy:
    """构造绕过 __init__ 的策略实例，注入 _execute_cycle 早退前置流程的依赖 mock。"""
    strategy = object.__new__(NewCoinStrategy)
    strategy.drawdown_pause_until = None

    # 交易执行器：清理占用/基线就绪检查均为空操作
    strategy.trading_executor = MagicMock()
    strategy.trading_executor.maybe_cleanup_expired_claims = AsyncMock()
    strategy.trading_executor.baseline_ready = True

    # 早退前的固定前置步骤：全部打桩，聚焦「是否保存上报」
    strategy._check_blacklist_monitor = AsyncMock()
    strategy._refresh_drawdown_status = AsyncMock()
    strategy._reconcile_positions_with_exchange = AsyncMock()
    strategy._guard_protection_orders = AsyncMock()
    strategy._monitor_positions = AsyncMock()
    strategy._save_state = AsyncMock()

    strategy.listing_detector = MagicMock()
    strategy.listing_detector.detect_new_listings = AsyncMock(return_value=new_coins)
    return strategy


async def test_无新币周期仍监控持仓并保存上报():
    """未检测到新币时，return 前仍须监控持仓并调用 _save_state 刷新上报（原缺陷点）。"""
    strategy = make_strategy(new_coins=[])

    await strategy._execute_cycle()

    strategy._monitor_positions.assert_awaited_once()
    strategy._save_state.assert_awaited_once()


async def test_熔断暂停周期仍监控持仓并保存上报():
    """回撤熔断暂停期间，return 前仍须监控持仓并刷新上报，且不进入新币检测。"""
    strategy = make_strategy(new_coins=[])
    # 熔断未到期 → 走暂停分支（熔断只暂停开仓，不应暂停监控/上报）
    strategy.drawdown_pause_until = datetime.now(timezone.utc) + timedelta(hours=1)

    await strategy._execute_cycle()

    strategy._monitor_positions.assert_awaited_once()
    strategy._save_state.assert_awaited_once()
    strategy.listing_detector.detect_new_listings.assert_not_awaited()