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

        # DB UPDATE 次数 = 5 个孤儿逐个标记 + 1 次整币种状态同步（failed==0）= 6
        assert mock_db.execute.await_count == 6, f"期望 DB UPDATE 6 次，实际 {mock_db.execute.await_count}"
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


# ==================== 2026-10-09 修复：撤单后 DB 状态同步（防「假孤儿单」） ====================
# 根因：条件单只在「创建成功」时写入 condition_orders(status=OPEN)，而撤单此前只清本地
# 内存 algo_ids、不同步 DB，导致交易所早已不存在的旧单在 DB 中长期保留 OPEN，
# 沉积成「假孤儿单」，并被孤儿清理任务反复「取消」（Binance -2011 被当作取消成功）。

# 整币种状态同步 SQL 的识别标记（区别于按 algo_id 单条标记）
_BLANKET_SYNC_MARK = "strategy_name=$1 AND symbol=$2"


def _make_db_with_orders(algo_ids=None):
    """构造 mock DB：fetch_all 返回指定 OPEN 单；execute 记录调用"""
    mock_db = MagicMock()
    mock_db.fetch_all = AsyncMock(
        return_value=[{"algo_id": a} for a in (algo_ids or [])]
    )
    mock_db.execute = AsyncMock(return_value="UPDATE 1")
    return mock_db


def _blanket_sync_calls(mock_db):
    """取出调用记录中「整币种状态同步」那条 SQL 的参数"""
    return [
        c.args for c in mock_db.execute.await_args_list
        if _BLANKET_SYNC_MARK in c.args[0]
    ]


# ==================== 测试 5: AC1 — 批量撤单成功后同步 DB 状态 ====================

def test_ac1_batch_success_syncs_db():
    """批量撤单成功（{complete:true}）→ 必须把该 symbol 的 OPEN 行同步为 CANCELED"""
    async def run():
        pm = _make_manager()
        _add_position(pm, "LITEUSDT")
        pm.db = _make_db_with_orders()
        pm.binance_api.cancel_all_algo_orders = AsyncMock(return_value={"complete": True})

        result = await pm.cancel_all_orders("LITEUSDT")

        assert result["method"] == "batch", f"期望 batch，实际 {result['method']}"
        calls = _blanket_sync_calls(pm.db)
        assert len(calls) == 1, f"批量成功后应同步一次 DB 状态，实际 {len(calls)} 次"
        assert calls[0][1] == "hrs" and calls[0][2] == "LITEUSDT", "同步参数应为策略名+交易对"
        print("✅ 测试 5 通过：批量撤单成功后同步 DB 状态")

    asyncio.run(run())


# ==================== 测试 6: AC2 — 逐个撤单全部成功后同步 DB 状态 ====================

def test_ac2_individual_all_success_syncs_db():
    """批量接口失败回退逐个撤单，全部成功 → 同步 DB 状态"""
    async def run():
        pm = _make_manager()
        _add_position(pm, "HBARUSDT")
        pm.db = _make_db_with_orders()  # 本地 3 个，DB 无额外孤儿
        pm.binance_api.cancel_all_algo_orders = AsyncMock(
            side_effect=Exception("API timeout")
        )
        pm.binance_api.cancel_algo_order = AsyncMock(return_value={})

        result = await pm.cancel_all_orders("HBARUSDT")

        assert result["method"] == "individual"
        assert result["failed"] == 0
        assert len(_blanket_sync_calls(pm.db)) == 1, "逐个撤单全部成功后应同步一次 DB 状态"
        print("✅ 测试 6 通过：逐个撤单全部成功后同步 DB 状态")

    asyncio.run(run())


# ==================== 测试 7: AC3 — 部分失败时不得误标 ====================

def test_ac3_partial_failure_no_sync():
    """逐个撤单部分失败 → 不得把未撤成功的单标记为 CANCELED（保守保留 OPEN）"""
    async def run():
        pm = _make_manager()
        _add_position(pm, "HBARUSDT")
        pm.db = _make_db_with_orders()
        pm.binance_api.cancel_all_algo_orders = AsyncMock(
            side_effect=Exception("API timeout")
        )

        async def fake_cancel(symbol, algo_id):
            if algo_id == 111:
                raise RuntimeError("取消失败")
            return {}

        pm.binance_api.cancel_algo_order = AsyncMock(side_effect=fake_cancel)

        result = await pm.cancel_all_orders("HBARUSDT")

        assert result["failed"] == 1, f"期望 failed=1，实际 {result['failed']}"
        assert _blanket_sync_calls(pm.db) == [], "部分失败时不应整币种标记 CANCELED"
        print("✅ 测试 7 通过：部分失败时不同步 DB 状态")

    asyncio.run(run())


# ==================== 测试 8: AC4 — db 为 None 时不报错、不产生 DB 调用 ====================

def test_ac4_none_db_no_error():
    """db=None（回测/无持久化环境）→ 撤单流程不受影响、不报错"""
    async def run():
        pm = _make_manager(db=None)
        _add_position(pm, "LITEUSDT")
        pm.binance_api.cancel_all_algo_orders = AsyncMock(return_value={"complete": True})

        result = await pm.cancel_all_orders("LITEUSDT")

        assert result["method"] == "batch"
        print("✅ 测试 8 通过：db=None 时撤单流程正常")

    asyncio.run(run())


# ==================== 测试 9: AC6 — 同步失败不阻断主流程 ====================

def test_ac6_sync_failure_not_raised():
    """DB 同步异常 → 共享助手吞异常返回空串，不向上抛出"""
    async def run():
        from shared.condition_orders import mark_open_orders_canceled

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(side_effect=RuntimeError("DB 不可用"))

        result = await mark_open_orders_canceled(mock_db, "hrs", "LITEUSDT")

        assert result == "", f"异常时应返回空串，实际 {result!r}"
        print("✅ 测试 9 通过：同步失败不阻断主流程")

    asyncio.run(run())


if __name__ == "__main__":
    print("=" * 50)
    print("PositionManager.cancel_all_orders 单元测试")
    print("=" * 50)
    test_f1_complete_true_recognized_as_success()
    test_f1_error_json_not_treated_as_success()
    test_f2_db_fallback_cleans_orphans()
    test_f2_db_fallback_filter_by_strategy_name()
    test_ac1_batch_success_syncs_db()
    test_ac2_individual_all_success_syncs_db()
    test_ac3_partial_failure_no_sync()
    test_ac4_none_db_no_error()
    test_ac6_sync_failure_not_raised()
    print("\n🎉 全部 9 个测试通过！")
