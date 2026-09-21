"""
position_manager.cancel_all_orders 单元测试

测试 F1（批量返回值判断修复）+ F2（DB 兜底查询）。

关键场景：
- Binance 批量成功返回 {"complete": true}（旧代码识别为失败，新代码识别为成功）
- 批量 API 抛异常 → fallback 到 DB 兜底取消所有 OPEN 单
- 错误 JSON {"code": -2011, "msg": "Unknown order"} → 正确跳过
"""
import asyncio
import sys
import os
from unittest.mock import MagicMock, AsyncMock, patch

# 把项目根目录加进来
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from strategies.hrs.position_manager import PositionManager


def _make_manager(binance_api=None, db=None):
    """构造一个最简 PositionManager"""
    config = {
        "target1_close_percent": 0.30,
        "target2_close_percent": 0.40,
        "default_atr": 5.0,
        "tp1_filled_ratio": 0.90,
    }
    if binance_api is None:
        binance_api = MagicMock()
        binance_api.use_unified_account = True
    return PositionManager(config=config, binance_api=binance_api, db=db)


def _add_position(pm, symbol, entry_price=900.0, entry_qty=0.05):
    """在 PositionManager 里塞一个持仓，含本地 algo_ids"""
    pm.add_position(
        symbol=symbol,
        direction="short",
        entry_price=entry_price,
        quantity=entry_qty,
        atr=5.5,
    )
    # 直接塞 algo_ids 到内部 dict（add_position 没有 algo_ids 参数）
    pos = pm._positions.get(symbol)
    if pos:
        pos["algo_ids"] = {"sl": 111, "tp1": 222, "tp2": 333}


# ==================== 测试 1: F1 — {"complete": true} 被正确识别 ====================

def test_f1_complete_true_recognized_as_success():
    """旧代码 code==200 判断永远失败；新代码 complete==True 应成功"""
    async def run():
        pm = _make_manager()
        _add_position(pm, "MUUSDT")

        # mock Binance 批量返回成功（只有 complete，无 code）
        pm.binance_api.cancel_all_algo_orders = AsyncMock(
            return_value={"complete": True}  # ← Binance 真实格式
        )

        result = await pm.cancel_all_orders("MUUSDT")

        assert result["method"] == "batch", f"期望 batch，实际 {result['method']}"
        assert pm.get_algo_ids("MUUSDT") == [], f"本地 algo_ids 应被清空，实际 {pm.get_algo_ids('MUUSDT')}"
        print("✅ 测试 1 通过：{complete: true} 被正确识别")

    asyncio.run(run())


# ==================== 测试 2: F1 — 错误 JSON 不被误判 ====================

def test_f1_error_json_not_treated_as_success():
    """Binance 返回错误 JSON（code=-2011）时不应走 batch 成功路径"""
    async def run():
        pm = _make_manager()
        _add_position(pm, "MUUSDT")

        pm.binance_api.cancel_all_algo_orders = AsyncMock(
            return_value={"code": -2011, "msg": "Unknown order sent"}
        )
        pm.binance_api.cancel_algo_order = AsyncMock(return_value={})

        result = await pm.cancel_all_orders("MUUSDT")

        # 应 fallback 到 individual（本地有 3 个 algo_id）
        assert result["method"] == "individual", f"期望 individual，实际 {result['method']}"
        assert result["cancelled"] == 3, f"期望取消 3 个，实际 {result['cancelled']}"
        print("✅ 测试 2 通过：错误 JSON 正确跳过 batch 路径")

    asyncio.run(run())


# ==================== 测试 3: F2 — DB 兜底清理孤儿单 ====================

def test_f2_db_fallback_cleans_orphans():
    """批量 API 抛异常 → DB 查出 5 个孤儿 OPEN 单 → 逐个取消"""
    async def run():
        pm = _make_manager()
        _add_position(pm, "MUUSDT")  # 本地 algo_ids 只有 3 个

        # mock 批量 API 抛异常
        pm.binance_api.cancel_all_algo_orders = AsyncMock(
            side_effect=Exception("API timeout")
        )
        pm.binance_api.cancel_algo_order = AsyncMock(return_value={})

        # mock DB — 查 condition_orders 返回 8 个 OPEN 单
        # 其中 3 个 = 本地已有，5 个 = 孤儿
        mock_db = MagicMock()
        mock_db.fetch_all = AsyncMock(return_value=[
            {"algo_id": 111},  # 本地已有
            {"algo_id": 222},  # 本地已有
            {"algo_id": 333},  # 本地已有
            {"algo_id": 401},  # 孤儿
            {"algo_id": 402},  # 孤儿
            {"algo_id": 403},  # 孤儿
            {"algo_id": 404},  # 孤儿
            {"algo_id": 405},  # 孤儿
        ])
        mock_db.execute = AsyncMock(return_value=None)
        pm.db = mock_db

        result = await pm.cancel_all_orders("MUUSDT")

        # 总取消 = 3 本地 + 5 孤儿 = 8
        assert result["method"] == "individual", f"期望 individual，实际 {result['method']}"
        assert result["total"] == 8, f"期望 total=8，实际 {result['total']}"
        assert result["cancelled"] == 8, f"期望 cancelled=8，实际 {result['cancelled']}"

        # DB UPDATE 应该被调了 5 次（孤儿那 5 个）
        assert mock_db.execute.await_count == 5, f"期望 DB UPDATE 5 次，实际 {mock_db.execute.await_count}"
        print("✅ 测试 3 通过：DB 兜底正确清理 5 个孤儿单")

    asyncio.run(run())


# ==================== 测试 4: F2 — 不碰其他策略的条件单 ====================

def test_f2_db_fallback_filter_by_strategy_name():
    """DB 查询 WHERE strategy_name='hrs'，不碰 new_coin 的条件单"""
    async def run():
        pm = _make_manager()
        _add_position(pm, "MUUSDT")

        pm.binance_api.cancel_all_algo_orders = AsyncMock(
            side_effect=Exception("API timeout")
        )
        pm.binance_api.cancel_algo_order = AsyncMock(return_value={})

        mock_db = MagicMock()
        # DB 里只有 hrs 的单，没有 new_coin 的 —— mock fetch_all 只返回 hrs
        mock_db.fetch_all = AsyncMock(return_value=[])
        mock_db.execute = AsyncMock(return_value=None)
        pm.db = mock_db

        await pm.cancel_all_orders("MUUSDT")

        # 验证查询用的 WHERE strategy_name='hrs'
        call_args = mock_db.fetch_all.await_args
        sql = call_args[0][0]
        assert "strategy_name" in sql and "hrs" in sql, "SQL 应包含 strategy_name = 'hrs'"
        print("✅ 测试 4 通过：DB 查询正确过滤 strategy_name='hrs'")

    asyncio.run(run())


if __name__ == "__main__":
    print("=" * 50)
    print("PositionManager.cancel_all_orders 单元测试")
    print("=" * 50)
    test_f1_complete_true_recognized_as_success()
    test_f1_error_json_not_treated_as_success()
    test_f2_db_fallback_cleans_orphans()
    test_f2_db_fallback_filter_by_strategy_name()
    print("\n🎉 全部 4 个测试通过！")
