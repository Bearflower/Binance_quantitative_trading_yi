"""
持仓归属隔离（方案C）单元测试

覆盖需求 8 类验收：
  1. 归属判定多分支（命中 / 无 / 多笔latest / status过滤）
  2. 保护单校验三态（自己 / 对家 / None）
  3. 互斥（单向 / 双向 / 加仓放行 / competing 过滤）
  4. 孤儿守卫（存在归属则不作为孤儿清理）
  5. 配置读取缺失默认
  6. DB 异常降级
"""
import pytest
import zlib
from unittest.mock import AsyncMock, MagicMock, patch

from shared.position_ownership import (
    load_ownership_config,
    resolve_position_owner,
    is_symbol_owned_by_other,
    filter_owned_positions,
    _open_entry_sql,
)


# ============================================================
# 基础：mock DatabaseManager
# ============================================================
def make_db(**kw):
    """构造 DatabaseManager 测试替身，默认提供 fetch_one / fetch_one_advisory_lock"""
    db = MagicMock()
    db.fetch_one = AsyncMock(return_value=kw.get("fetch_one_result", None))
    db.fetch_all = AsyncMock(return_value=[])

    def _advisory_fetcher(lock_key, query, *args):
        # 校验 advisory lock key 基于 symbol 计算
        return db.advisory_row

    db.fetch_one_advisory_lock = AsyncMock(side_effect=kw.get("advisory_side_effect", _advisory_fetcher))
    db.advisory_row = kw.get("advisory_row", None)
    return db


# ============================================================
# 1. 归属判定多分支
# ============================================================
class TestResolvePositionOwner:
    @pytest.mark.asyncio
    async def test_hit_returns_owner(self):
        """命中：返回最新开仓单归属策略名"""
        db = make_db(fetch_one_result={"strategy": "MTPCS激进策略"})
        owner = await resolve_position_owner(db, "SOLUSDT")
        assert owner == "MTPCS激进策略"

    @pytest.mark.asyncio
    async def test_no_record_returns_none(self):
        """无记录：fetch_one 返回 None => 归属 None"""
        db = make_db(fetch_one_result=None)
        assert await resolve_position_owner(db, "SOLUSDT") is None

    @pytest.mark.asyncio
    async def test_empty_strategy_treated_as_none(self):
        """脏数据：strategy 为空串 => 视为无归属"""
        db = make_db(fetch_one_result={"strategy": ""})
        assert await resolve_position_owner(db, "SOLUSDT") is None

    @pytest.mark.asyncio
    async def test_multiple_latest_by_sql_ordering(self):
        """多笔归属：SQL 应按 executed_at DESC, id DESC 取最新一条（LIMIT 1）"""
        sql_filled = _open_entry_sql(True)
        assert "ORDER BY executed_at DESC, id DESC LIMIT 1" in sql_filled
        assert "LIMIT 1" in sql_filled

    def test_status_filter_alters_sql(self):
        """status过滤：True 追加 FILLED；False 不含 status"""
        sql_filled = _open_entry_sql(True)
        sql_unfiltered = _open_entry_sql(False)
        assert "AND status = 'FILLED'" in sql_filled
        assert "status" not in sql_unfiltered
        # 两条 SQL 其余约束一致（归属用开仓订单单点）
        assert sql_unfiltered.startswith("SELECT strategy FROM")

    @pytest.mark.asyncio
    async def test_status_filter_passed_to_db(self):
        """status_filter=True 时应将 FILLED 版本的 SQL 传给 fetch_one"""
        db = make_db(fetch_one_result={"strategy": "MTPCS策略"})
        await resolve_position_owner(db, "BTCUSDT", status_filter=True)
        sql_used = db.fetch_one.call_args[0][0]
        assert "status = 'FILLED'" in sql_used


# ============================================================
# 2. 保护单校验三态（策略层接入）
# ============================================================
def make_orig_strategy():
    """以 __new__ 构造最小 BTCEthStrategy 实例，仅注入接点所需属性"""
    from strategies.btc_eth.strategy import BTCEthStrategy

    s = BTCEthStrategy.__new__(BTCEthStrategy)
    s.db_manager = make_db()
    s.notification = MagicMock()
    s.notification.send = AsyncMock(return_value=True)
    s.my_record_name = "MTPCS策略"
    s._competing_record_names = ["MTPCS激进策略"]
    s.strategy_name = "btc_eth"
    s.min_position_amt = 0.00001
    s.positions = {}
    return s


class TestProtectionOwnershipCheck:
    @pytest.mark.asyncio
    async def test_owner_is_self_proceeds(self):
        """自己：归属==本策略，进入后续补单逻辑（不触发告警）"""
        s = make_orig_strategy()
        pos_data = {"symbol": "SOLUSDT", "positionAmt": "1.2"}
        with patch(
            "strategies.btc_eth.strategy.resolve_position_owner",
            new=AsyncMock(return_value="MTPCS策略"),
        ), patch(
            "strategies.btc_eth.strategy.BTCEthStrategy._get_current_price",
            new=AsyncMock(return_value=None),
        ) as m_price:
            await s._ensure_symbol_protection(
                pos_data, {"SOLUSDT"}, {}, None, None, None
            )
        # 归属为自己 => 继续原逻辑（走到了取价步骤）
        m_price.assert_awaited_once_with("SOLUSDT")
        s.notification.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_owner_is_other_skip_and_notify(self):
        """对家：归属非本策略 => 不补挂保护单 + 飞书告警"""
        s = make_orig_strategy()
        pos_data = {"symbol": "SOLUSDT", "positionAmt": "1.2"}
        with patch(
            "strategies.btc_eth.strategy.resolve_position_owner",
            new=AsyncMock(return_value="MTPCS激进策略"),
        ), patch(
            "strategies.btc_eth.strategy.BTCEthStrategy._get_current_price",
            new=AsyncMock(return_value=100.0),
        ) as m_price:
            await s._ensure_symbol_protection(
                pos_data, {"SOLUSDT"}, {}, None, None, None
            )
        # 未进入取价/补单逻辑，且已告警
        m_price.assert_not_awaited()
        s.notification.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_owner_none_skip_and_notify(self):
        """None：无法判定归属 => 保守不补挂保护单 + 告警"""
        s = make_orig_strategy()
        pos_data = {"symbol": "SOLUSDT", "positionAmt": "1.2"}
        with patch(
            "strategies.btc_eth.strategy.resolve_position_owner",
            new=AsyncMock(return_value=None),
        ), patch(
            "strategies.btc_eth.strategy.BTCEthStrategy._get_current_price",
            new=AsyncMock(return_value=100.0),
        ) as m_price:
            await s._ensure_symbol_protection(
                pos_data, {"SOLUSDT"}, {}, None, None, None
            )
        m_price.assert_not_awaited()
        s.notification.send.assert_awaited_once()


# ============================================================
# 3. 互斥（边界B）
# ============================================================
class TestMutualExclusion:
    @pytest.mark.asyncio
    async def test_single_direction(self):
        """单向：归他对家（激进版持有 SOL），原版应被拦截"""
        db = make_db(advisory_row={"strategy": "MTPCS激进策略"})
        blocked = await is_symbol_owned_by_other(db, "SOLUSDT", "MTPCS策略")
        assert blocked is True

    @pytest.mark.asyncio
    async def test_bidirectional(self):
        """双向：各自视角都把对方判定为"他方持有"（原版 vs 激进版互为对家）"""
        # 原版视角：归属=激进版
        db_orig = make_db(advisory_row={"strategy": "MTPCS激进策略"})
        assert await is_symbol_owned_by_other(db_orig, "SOLUSDT", "MTPCS策略") is True
        # 激进版视角：归属=原版
        db_agg = make_db(advisory_row={"strategy": "MTPCS策略"})
        assert await is_symbol_owned_by_other(db_agg, "SOLUSDT", "MTPCS激进策略") is True

    @pytest.mark.asyncio
    async def test_add_position_allowed_when_owner_is_mine(self):
        """加仓放行：归属==本策略时返回 False（不加仓被拦截）"""
        db = make_db(advisory_row={"strategy": "MTPCS策略"})
        assert await is_symbol_owned_by_other(db, "SOLUSDT", "MTPCS策略") is False

    @pytest.mark.asyncio
    async def test_no_owner_not_blocked(self):
        """无归属：他人不存在，不拦截开仓"""
        db = make_db(advisory_row=None)
        assert await is_symbol_owned_by_other(db, "SOLUSDT", "MTPCS策略") is False

    @pytest.mark.asyncio
    async def test_competing_filter(self):
        """competing 过滤：归属不在对家列表内则互斥不生效"""
        db = make_db(advisory_row={"strategy": "网格交易策略"})
        # competing 限定只用 MTPCS 对家 => 网格非对家，不拦截
        assert (
            await is_symbol_owned_by_other(
                db, "SOLUSDT", "MTPCS策略", ["MTPCS激进策略"]
            )
            is False
        )
        # 对家列表内 => 拦截
        assert (
            await is_symbol_owned_by_other(
                db, "SOLUSDT", "MTPCS激进策略", ["MTPCS策略", "网格交易策略"]
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_advisory_lock_key_uses_symbol_crc32(self):
        """advisory lock：lock key 取自 symbol 的 crc32 32 位值"""
        db = make_db(advisory_row={"strategy": "MTPCS激进策略"})
        await is_symbol_owned_by_other(db, "SOLUSDT", "MTPCS策略")
        lock_key = db.fetch_one_advisory_lock.call_args[0][0]
        assert lock_key == (zlib.crc32(b"SOLUSDT") & 0xFFFFFFFF)

    @pytest.mark.asyncio
    async def test_open_new_position_blocked_by_other(self):
        """策略层互斥：开仓入口被对家持仓拦截，返回 False，未进入频率控制"""
        s = make_orig_strategy()
        s.frequency_controller = MagicMock()
        s.frequency_controller.record_trade = AsyncMock(return_value=None)
        with patch(
            "strategies.btc_eth.strategy.is_symbol_owned_by_other",
            new=AsyncMock(return_value=True),
        ):
            result = await s._open_new_position(
                {"symbol": "SOLUSDT", "direction": "SHORT", "grade": "A", "score": 80, "timestamp": 1}
            )
        assert result is False
        s.frequency_controller.record_trade.assert_not_awaited()
        s.notification.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_open_new_position_allowed_when_owned_by_mine(self):
        """策略层加仓放行：归属为本策略未拦截，进入后续频率控制"""
        s = make_orig_strategy()
        s.frequency_controller = MagicMock()
        s.frequency_controller.record_trade = AsyncMock(return_value=None)
        with patch(
            "strategies.btc_eth.strategy.is_symbol_owned_by_other",
            new=AsyncMock(return_value=False),
        ), patch(
            "strategies.btc_eth.strategy.BTCEthStrategy._place_entry_order",
            new=AsyncMock(return_value=None),
        ):
            result = await s._open_new_position(
                {"symbol": "SOLUSDT", "direction": "SHORT", "grade": "A", "score": 80, "timestamp": 1}
            )
        assert result is False
        s.frequency_controller.record_trade.assert_awaited_once()


# ============================================================
# 4. 孤儿守卫
# ============================================================
class TestOrphanGuard:
    @pytest.mark.asyncio
    async def test_skip_orphan_when_position_owned(self):
        """存在未平开仓单归属 => 该 symbol 不作为孤儿清理（不取消保护单）"""
        from strategies.btc_eth.strategy import BTCEthStrategy

        s = BTCEthStrategy.__new__(BTCEthStrategy)
        s.db_manager = MagicMock()
        s.binance = MagicMock()
        s.binance.get_position = AsyncMock(return_value=[{"symbol": "SOLUSDT", "positionAmt": "0"}])
        s.binance.cancel_all_algo_orders = AsyncMock(return_value=True)
        s.min_position_amt = 0.00001

        with patch(
            "shared.condition_orders.get_open_orders",
            new=AsyncMock(
                return_value=[
                    {"symbol": "SOLUSDT", "algo_id": "x", "order_type": "STOP_LOSS"}
                ]
            ),
        ), patch(
            "strategies.btc_eth.strategy.resolve_position_owner",
            new=AsyncMock(return_value="MTPCS激进策略"),
        ):
            await s._do_startup_orphan_cleanup()
        # 归属存在 => 跳过清理，不批量取消该 symbol
        s.binance.cancel_all_algo_orders.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clean_orphan_when_no_owner(self):
        """无归属 => 继续作为孤儿清理"""
        from strategies.btc_eth.strategy import BTCEthStrategy

        s = BTCEthStrategy.__new__(BTCEthStrategy)
        s.db_manager = MagicMock()
        s.binance = MagicMock()
        s.binance.get_position = AsyncMock(return_value=[{"symbol": "SOLUSDT", "positionAmt": "0"}])
        s.binance.cancel_all_algo_orders = AsyncMock(return_value=True)
        s.min_position_amt = 0.00001

        with patch(
            "shared.condition_orders.get_open_orders",
            new=AsyncMock(
                return_value=[
                    {"symbol": "SOLUSDT", "algo_id": "x", "order_type": "STOP_LOSS"}
                ]
            ),
        ), patch(
            "strategies.btc_eth.strategy.resolve_position_owner",
            new=AsyncMock(return_value=None),
        ):
            await s._do_startup_orphan_cleanup()
        s.binance.cancel_all_algo_orders.assert_awaited_once_with("SOLUSDT")


# ============================================================
# 5. 配置读取缺失默认
# ============================================================
class TestLoadOwnershipConfig:
    def test_normal(self):
        cfg = {
            "strategy": {"record_name": "MTPCS策略", "name": "btc_eth_trend"},
            "ownership": {"competing_record_names": ["MTPCS激进策略"]},
        }
        out = load_ownership_config(cfg)
        assert out["my_record_name"] == "MTPCS策略"
        assert out["competing_record_names"] == ["MTPCS激进策略"]

    def test_missing_record_name_falls_back_to_strategy_name(self):
        """record_name 缺失 => 回退 strategy.name 且不抛异常"""
        cfg = {"strategy": {"name": "btc_eth_aggressive"}}
        out = load_ownership_config(cfg)
        assert out["my_record_name"] == "btc_eth_aggressive"
        assert out["competing_record_names"] == []

    def test_missing_all_keys_graceful(self):
        """整段缺失 => 不抛异常，my_record_name 为 None，competing 空列表"""
        out = load_ownership_config({})
        assert out["my_record_name"] is None
        assert out["competing_record_names"] == []

    def test_none_config_graceful(self):
        """config 为 None => 优雅返回默认，不抛异常"""
        out = load_ownership_config(None)
        assert out["my_record_name"] is None
        assert out["competing_record_names"] == []


# ============================================================
# 6. DB 异常降级
# ============================================================
class TestDbDegradation:
    @pytest.mark.asyncio
    async def test_resolve_dberror_returns_none(self):
        """边界A：DB 查询异常 => 返回 None 不抛异常"""
        db = make_db()
        db.fetch_one = AsyncMock(side_effect=Exception("db down"))
        assert await resolve_position_owner(db, "SOLUSDT") is None

    @pytest.mark.asyncio
    async def test_resolve_db_none_returns_none(self):
        """边界A：db_manager 为 None => 返回 None"""
        assert await resolve_position_owner(None, "SOLUSDT") is None

    @pytest.mark.asyncio
    async def test_mutex_dberror_returns_false(self):
        """边界B：DB 异常 => 返回 False（不放行双开，记 ERROR）"""
        db = make_db()
        db.fetch_one_advisory_lock = AsyncMock(side_effect=Exception("db down"))
        assert await is_symbol_owned_by_other(db, "SOLUSDT", "MTPCS策略") is False

    @pytest.mark.asyncio
    async def test_mutex_db_none_returns_false(self):
        """边界B：db_manager 为 None => 返回 False"""
        assert await is_symbol_owned_by_other(None, "SOLUSDT", "MTPCS策略") is False

    @pytest.mark.asyncio
    async def test_mutex_no_advisory_method_degrades_to_fetch_one(self):
        """边界B：未实现 advisory 方法时退化为普通 fetch_one（竞态窗口由边界A兜底）"""
        db = make_db()
        db.fetch_one = AsyncMock(return_value={"strategy": "MTPCS激进策略"})
        db.fetch_one_advisory_lock = None  # 模拟未实现
        blocked = await is_symbol_owned_by_other(db, "SOLUSDT", "MTPCS策略")
        assert blocked is True  # 仍能正确判定
        db.fetch_one.assert_awaited_once()


# ============================================================
# 7. 补充覆盖率缺口（api-test-pro 回归补测）
# ============================================================
def make_agg_strategy():
    """构造 btc_eth_aggressive 最小策略实例（验证方案C两副本接入对称）"""
    from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as AggStrategy

    s = AggStrategy.__new__(AggStrategy)
    s.db_manager = make_db()
    s.notification = MagicMock()
    s.notification.send = AsyncMock(return_value=True)
    s.my_record_name = "MTPCS激进策略"
    s._competing_record_names = ["MTPCS策略"]
    s.strategy_name = "btc_eth_aggressive"
    s.min_position_amt = 0.00001
    s.positions = {}
    return s


class TestAggressiveSymmetry:
    """方案C对 aggressive 副本的接入是否与 btc_eth 主副本对称"""

    @pytest.mark.asyncio
    async def test_aggressive_owner_is_self_proceeds(self):
        """aggressive：归属==本策略 => 进入补单逻辑，不告警"""
        s = make_agg_strategy()
        pos_data = {"symbol": "SOLUSDT", "positionAmt": "1.2"}
        with patch(
            "strategies.btc_eth_aggressive.strategy.resolve_position_owner",
            new=AsyncMock(return_value="MTPCS激进策略"),
        ), patch(
            "strategies.btc_eth_aggressive.strategy.BTCEthStrategy._get_current_price",
            new=AsyncMock(return_value=None),
        ) as m_price:
            await s._ensure_symbol_protection(
                pos_data, {"SOLUSDT"}, {}, None, None, None
            )
        m_price.assert_awaited_once_with("SOLUSDT")
        s.notification.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_aggressive_owner_is_other_skip_and_notify(self):
        """aggressive：归属为对家(原版) => 不补挂保护单 + 告警"""
        s = make_agg_strategy()
        pos_data = {"symbol": "SOLUSDT", "positionAmt": "1.2"}
        with patch(
            "strategies.btc_eth_aggressive.strategy.resolve_position_owner",
            new=AsyncMock(return_value="MTPCS策略"),
        ), patch(
            "strategies.btc_eth_aggressive.strategy.BTCEthStrategy._get_current_price",
            new=AsyncMock(return_value=100.0),
        ) as m_price:
            await s._ensure_symbol_protection(
                pos_data, {"SOLUSDT"}, {}, None, None, None
            )
        m_price.assert_not_awaited()
        s.notification.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aggressive_open_blocked_by_other(self):
        """aggressive 开仓互斥：归属为对家 => 开仓被拦截，未进入频率控制"""
        s = make_agg_strategy()
        s.frequency_controller = MagicMock()
        s.frequency_controller.record_trade = AsyncMock(return_value=None)
        with patch(
            "strategies.btc_eth_aggressive.strategy.is_symbol_owned_by_other",
            new=AsyncMock(return_value=True),
        ):
            result = await s._open_new_position(
                {"symbol": "SOLUSDT", "direction": "SHORT", "grade": "A", "score": 80, "timestamp": 1}
            )
        assert result is False
        s.frequency_controller.record_trade.assert_not_awaited()
        s.notification.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aggressive_orphan_guard_with_owner(self):
        """aggressive 孤儿守卫：存在归属 => 不作为孤儿清理（与主副本对称）"""
        from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as AggStrategy

        s = AggStrategy.__new__(AggStrategy)
        s.db_manager = MagicMock()
        s.binance = MagicMock()
        s.binance.get_position = AsyncMock(return_value=[{"symbol": "SOLUSDT", "positionAmt": "0"}])
        s.binance.cancel_all_algo_orders = AsyncMock(return_value=True)
        s.min_position_amt = 0.00001
        with patch(
            "shared.condition_orders.get_open_orders",
            new=AsyncMock(
                return_value=[{"symbol": "SOLUSDT", "algo_id": "x", "order_type": "STOP_LOSS"}]
            ),
        ), patch(
            "strategies.btc_eth_aggressive.strategy.resolve_position_owner",
            new=AsyncMock(return_value="MTPCS策略"),
        ):
            await s._do_startup_orphan_cleanup()
        s.binance.cancel_all_algo_orders.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_aggressive_orphan_clean_when_no_owner(self):
        """aggressive 孤儿守卫：无归属 => 仍按孤儿清理"""
        from strategies.btc_eth_aggressive.strategy import BTCEthStrategy as AggStrategy

        s = AggStrategy.__new__(AggStrategy)
        s.db_manager = MagicMock()
        s.binance = MagicMock()
        s.binance.get_position = AsyncMock(return_value=[{"symbol": "SOLUSDT", "positionAmt": "0"}])
        s.binance.cancel_all_algo_orders = AsyncMock(return_value=True)
        s.min_position_amt = 0.00001
        with patch(
            "shared.condition_orders.get_open_orders",
            new=AsyncMock(
                return_value=[{"symbol": "SOLUSDT", "algo_id": "x", "order_type": "STOP_LOSS"}]
            ),
        ), patch(
            "strategies.btc_eth_aggressive.strategy.resolve_position_owner",
            new=AsyncMock(return_value=None),
        ):
            await s._do_startup_orphan_cleanup()
        s.binance.cancel_all_algo_orders.assert_awaited_once_with("SOLUSDT")


class TestOwnershipConfigBoundary:
    """配置读取缺省边界的补充覆盖"""

    def test_strategy_not_dict_graceful(self):
        """config['strategy'] 为 None/非 dict => 优雅返回默认，不抛异常"""
        out = load_ownership_config({"strategy": None, "ownership": None})
        assert out["my_record_name"] is None
        assert out["competing_record_names"] == []


class TestMutualExclusionBoundary:
    @pytest.mark.asyncio
    async def test_my_name_none_competing_filters(self):
        """本策略归属名缺失(None) + competing 非空：owner 在对家列表内则命中拦截"""
        db = make_db(advisory_row={"strategy": "MTPCS策略"})
        assert (
            await is_symbol_owned_by_other(db, "SOLUSDT", None, ["MTPCS策略"]) is True
        )

    @pytest.mark.asyncio
    async def test_my_name_none_default_all_intercepts(self):
        """本策略归属名缺失 + competing 为空：与任意其他策略互斥"""
        db = make_db(advisory_row={"strategy": "MTPCS激进策略"})
        assert await is_symbol_owned_by_other(db, "SOLUSDT", None) is True


# ============================================================
# 8. 上报归属过滤（filter_owned_positions）
# ============================================================
class TestFilterOwnedPositions:
    @pytest.mark.asyncio
    async def test_own_position_kept(self):
        """归属为本策略 => 保留该 symbol"""
        db = make_db(fetch_one_result={"strategy": "MTPCS策略"})
        margin, qty = await filter_owned_positions(
            db, "MTPCS策略", {"SOLUSDT": 20.8}, {"SOLUSDT": 0.28}
        )
        assert margin == {"SOLUSDT": 20.8}
        assert qty == {"SOLUSDT": 0.28}

    @pytest.mark.asyncio
    async def test_other_position_filtered(self):
        """归属为对家策略 => 剔除该 symbol（SOL 由激进版开、原版上报场景）"""
        db = make_db(fetch_one_result={"strategy": "MTPCS激进策略"})
        margin, qty = await filter_owned_positions(
            db, "MTPCS策略", {"SOLUSDT": 20.8}, {"SOLUSDT": 0.28}
        )
        assert margin == {}
        assert qty == {}

    @pytest.mark.asyncio
    async def test_mixed_filtering(self):
        """混合：本策略 ETH 保留、对家 SOL 剔除"""
        db = make_db(fetch_one_result={"strategy": "MTPCS激进策略"})

        async def _fetch_one(sql, sym):
            return {"strategy": "MTPCS策略"} if sym == "ETHUSDT" else {"strategy": "MTPCS激进策略"}

        db.fetch_one = AsyncMock(side_effect=_fetch_one)
        margin, qty = await filter_owned_positions(
            db, "MTPCS策略",
            {"ETHUSDT": 58.2, "SOLUSDT": 20.8},
            {"ETHUSDT": 1.0, "SOLUSDT": 0.28},
        )
        assert margin == {"ETHUSDT": 58.2}
        assert qty == {"ETHUSDT": 1.0}

    @pytest.mark.asyncio
    async def test_no_owner_kept(self):
        """归属为 None（无未平开仓单）=> 保留，不误删历史仓"""
        db = make_db(fetch_one_result=None)
        margin, qty = await filter_owned_positions(
            db, "MTPCS策略", {"XRPUSDT": 10.0}, {"XRPUSDT": 1.0}
        )
        assert margin == {"XRPUSDT": 10.0}
        assert qty == {"XRPUSDT": 1.0}

    @pytest.mark.asyncio
    async def test_db_none_returns_unchanged(self):
        """db_manager 为 None => 原样返回（保守不误伤）"""
        margin, qty = await filter_owned_positions(
            None, "MTPCS策略", {"SOLUSDT": 20.8}, {"SOLUSDT": 0.28}
        )
        assert margin == {"SOLUSDT": 20.8}
        assert qty == {"SOLUSDT": 0.28}

    @pytest.mark.asyncio
    async def test_my_name_none_returns_unchanged(self):
        """本策略名缺失 => 原样返回（不过滤）"""
        db = make_db(fetch_one_result={"strategy": "MTPCS激进策略"})
        margin, qty = await filter_owned_positions(
            db, None, {"SOLUSDT": 20.8}, {"SOLUSDT": 0.28}
        )
        assert margin == {"SOLUSDT": 20.8}
        assert qty == {"SOLUSDT": 0.28}

    @pytest.mark.asyncio
    async def test_query_error_keeps_symbol(self):
        """归属查询异常 => 保留该 symbol（宁多报不误删自家已成交仓）"""
        db = make_db(fetch_one_result=None)
        db.fetch_one = AsyncMock(side_effect=Exception("db down"))
        margin, qty = await filter_owned_positions(
            db, "MTPCS策略", {"SOLUSDT": 20.8}, {"SOLUSDT": 0.28}
        )
        assert margin == {"SOLUSDT": 20.8}
        assert qty == {"SOLUSDT": 0.28}

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self):
        """空持仓 => 原样返回空 dict"""
        db = make_db(fetch_one_result={"strategy": "MTPCS激进策略"})
        margin, qty = await filter_owned_positions(db, "MTPCS策略", None, None)
        assert margin == {}
        assert qty == {}

    @pytest.mark.asyncio
    async def test_uses_status_filter_false(self):
        """上报过滤必须传 status_filter=False（匹配 NEW 挂单，防过滤失效）"""
        db = make_db(fetch_one_result={"strategy": "MTPCS策略"})
        await filter_owned_positions(
            db, "MTPCS策略", {"SOLUSDT": 20.8}, {"SOLUSDT": 0.28}
        )
        sql_used = db.fetch_one.call_args[0][0]
        assert "status = 'FILLED'" not in sql_used