"""R08 修复验收测试：孤儿条件单清理的「无状态」分支交易所持仓前置检查。

覆盖验收标准：
- R08-AC1：交易所有持仓 + 无状态 → 不调用 _cancel_order，记 skipped/告警
- R08-AC2：交易所无持仓 + 无状态 + _strategy_has_position=False → 才调用 _cancel_order
- R08-AC3：exchange_positions is None + 无状态 → 跳过并告警，不撤单
- R08-AC4：原有超时分支行为不变（不回归）
- R08-AC5：场景B（交易所无持仓 + 策略无活交易）行为不变（不回归）

说明：全部使用 mock，不连接真实数据库、交易所与通知服务。
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from ai_tuner.cleanup.orphan_cleanup import OrphanCleanupJob

_OPEN_ORDERS_TARGET = "ai_tuner.cleanup.orphan_cleanup.get_open_orders"


def _make_job(**kwargs):
    """构造被测任务及三个依赖替身（db / binance / notification）。"""
    db = MagicMock()
    binance = MagicMock()
    notification = MagicMock()
    notification.send = AsyncMock()
    job = OrphanCleanupJob(
        db=db,
        binance_client=binance,
        notification_client=notification,
        **kwargs,
    )
    return job, db, binance, notification


def _stop_loss_order(strategy="btc_eth", symbol="BTCUSDT"):
    """构造一条 STOP_LOSS 条件单（报告复现场景用）。"""
    return {
        "strategy_name": strategy,
        "symbol": symbol,
        "order_type": "STOP_LOSS",
        "algo_id": 111,
        "order_id": None,
    }


def _patch_execute(job, *, orders, positions, states, has_position):
    """统一装配 execute 依赖的补丁集合。"""
    return [
        patch.object(job, "_ensure_table", AsyncMock()),
        patch(_OPEN_ORDERS_TARGET, AsyncMock(return_value=orders)),
        patch.object(job, "_get_exchange_positions", AsyncMock(return_value=positions)),
        patch.object(job, "_query_strategy_states", AsyncMock(return_value=states)),
        patch.object(job, "_strategy_has_position", AsyncMock(return_value=has_position)),
    ]


# ============================================================
# 单元：_is_confirmed_no_position
# ============================================================

def test_is_confirmed_no_position_semantics():
    """仅「交易所明确返回且不含该 symbol」才算确认无仓；None 保守视为未确认。"""
    job, *_ = _make_job()
    assert job._is_confirmed_no_position("BTCUSDT", None) is False
    assert job._is_confirmed_no_position("BTCUSDT", {"BTCUSDT"}) is False
    assert job._is_confirmed_no_position("BTCUSDT", {"ETHUSDT"}) is True
    assert job._is_confirmed_no_position("BTCUSDT", set()) is True


# ============================================================
# R08-AC1：交易所有持仓 + 无状态 → 保留保护单并告警
# ============================================================

async def test_r08_ac1_exchange_has_position_keeps_stop_loss():
    job, db, binance, notification = _make_job()
    patches = _patch_execute(
        job,
        orders=[_stop_loss_order()],
        positions={"BTCUSDT"},
        states={},
        has_position=True,
    )
    cancel_mock = AsyncMock(return_value=(True, None))
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patch.object(job, "_cancel_order", cancel_mock):
        await job.execute()

    cancel_mock.assert_not_awaited()
    binance.cancel_algo_order.assert_not_called()
    notification.send.assert_awaited()


# ============================================================
# R08-AC2：交易所无持仓 + 无状态 + 无活交易 → 才撤单
# ============================================================

async def test_r08_ac2_no_position_no_state_cancels_orphan():
    job, db, binance, notification = _make_job()
    patches = _patch_execute(
        job,
        orders=[_stop_loss_order()],
        positions=set(),
        states={},
        has_position=False,
    )
    cancel_mock = AsyncMock(return_value=(True, None))
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patch.object(job, "_cancel_order", cancel_mock):
        await job.execute()

    cancel_mock.assert_awaited_once()


# ============================================================
# R08-AC3：无法确认持仓（API 失败）+ 无状态 → 跳过并告警
# ============================================================

async def test_r08_ac3_unknown_positions_keeps_stop_loss():
    job, db, binance, notification = _make_job()
    patches = _patch_execute(
        job,
        orders=[_stop_loss_order()],
        positions=None,
        states={},
        has_position=False,
    )
    cancel_mock = AsyncMock(return_value=(True, None))
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patch.object(job, "_cancel_order", cancel_mock):
        await job.execute()

    cancel_mock.assert_not_awaited()
    notification.send.assert_awaited()


# ============================================================
# R08-AC4：超时分支不回归
# ============================================================

async def test_r08_ac4_stale_branch_unchanged():
    job, db, binance, notification = _make_job(stale_hours_threshold=2.0)
    stale_updated = datetime.utcnow() - timedelta(hours=5)
    states = {"btc_eth": {"symbols": {"BTCUSDT"}, "updated_at": stale_updated}}
    patches = _patch_execute(
        job,
        orders=[_stop_loss_order()],
        positions=set(),
        states=states,
        has_position=True,
    )
    cancel_mock = AsyncMock(return_value=(True, None))
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patch.object(job, "_cancel_order", cancel_mock):
        await job.execute()

    cancel_mock.assert_awaited_once()


# ============================================================
# R08-AC5：场景B 不回归
# ============================================================

async def test_r08_ac5_scenario_b_unchanged():
    job, db, binance, notification = _make_job(stale_hours_threshold=2.0)
    fresh_updated = datetime.utcnow()
    states = {"btc_eth": {"symbols": {"BTCUSDT"}, "updated_at": fresh_updated}}
    patches = _patch_execute(
        job,
        orders=[_stop_loss_order()],
        positions=set(),
        states=states,
        has_position=False,
    )
    cancel_mock = AsyncMock(return_value=(True, None))
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patch.object(job, "_cancel_order", cancel_mock):
        await job.execute()

    cancel_mock.assert_awaited_once()


# ============================================================
# 开关关闭时回退既有行为（保证可配置、灰度）
# ============================================================

async def test_require_exchange_confirmation_disabled_restores_legacy_behavior():
    """require_exchange_confirmation=False 时，无状态分支回退为直接判孤儿。"""
    job, db, binance, notification = _make_job(require_exchange_confirmation=False)
    patches = _patch_execute(
        job,
        orders=[_stop_loss_order()],
        positions={"BTCUSDT"},
        states={},
        has_position=False,
    )
    cancel_mock = AsyncMock(return_value=(True, None))
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patch.object(job, "_cancel_order", cancel_mock):
        await job.execute()

    cancel_mock.assert_awaited_once()