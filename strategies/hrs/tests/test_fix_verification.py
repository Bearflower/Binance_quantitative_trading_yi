"""
修复验证测试

测试 HRS 策略中以下两个修复：
1. has_algo_id() 方法 - 检查条件单是否已有 algoId 记录
2. replenish_position_orders() 补单去重 - 补单前通过 has_algo_id() 检查已有条件单
3. _reconcile_positions() 不再接管 - 交易所有多余持仓但本地无记录时仅告警，不调用 add_position
"""
import pytest
import yaml
import asyncio
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

# 加载配置
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)

from strategies.hrs.position_manager import PositionManager
from strategies.hrs.executor import TradingExecutor


# ============================================================
# 测试夹具
# ============================================================

@pytest.fixture
def mock_binance_api():
    """创建模拟的币安API客户端"""
    api = MagicMock()
    api.use_unified_account = False
    api.get_open_algo_orders = AsyncMock(return_value=[])
    api.cancel_algo_order = AsyncMock(return_value={"status": "CANCELED"})
    api.get_position = AsyncMock(return_value=[])
    api.get_ticker = AsyncMock(return_value={"lastPrice": "1.0"})
    api.place_conditional_order = AsyncMock(return_value={"algoId": 12345})
    return api


@pytest.fixture
def pm(mock_binance_api):
    """创建持仓管理器实例"""
    return PositionManager(CONFIG, mock_binance_api)


@pytest.fixture
def pm_with_position(mock_binance_api):
    """创建已有持仓的持仓管理器"""
    pm = PositionManager(CONFIG, mock_binance_api)
    pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
    return pm


# ============================================================
# 测试1: has_algo_id() 方法
# ============================================================

class TestHasAlgoId:
    """测试 has_algo_id() 方法的三种场景"""

    def test_持仓存在且有algo_ids_返回True(self, pm):
        """持仓存在且 algo_ids 中有对应角色 -> 返回 True"""
        # 添加持仓
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        # 手动注入 algo_ids
        pm.add_algo_id("BTCUSDT", "sl", 10001)
        pm.add_algo_id("BTCUSDT", "tp1", 10002)
        pm.add_algo_id("BTCUSDT", "tp2", 10003)

        assert pm.has_algo_id("BTCUSDT", "sl") is True
        assert pm.has_algo_id("BTCUSDT", "tp1") is True
        assert pm.has_algo_id("BTCUSDT", "tp2") is True

    def test_持仓存在但algo_ids为空_返回False(self, pm):
        """持仓存在但 algo_ids 为空 -> 返回 False"""
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        # algo_ids 初始为空字典

        assert pm.has_algo_id("BTCUSDT", "sl") is False
        assert pm.has_algo_id("BTCUSDT", "tp1") is False
        assert pm.has_algo_id("BTCUSDT", "tp2") is False

    def test_持仓不存在_返回False(self, pm):
        """持仓不存在 -> 返回 False"""
        assert pm.has_algo_id("NONEXIST", "sl") is False
        assert pm.has_algo_id("NONEXIST", "tp1") is False
        assert pm.has_algo_id("NONEXIST", "tp2") is False

    def test_持仓存在但algo_ids中无该角色_返回False(self, pm):
        """持仓存在，algo_ids 中有其他角色但没有目标角色 -> 返回 False"""
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)  # 只有 sl

        assert pm.has_algo_id("BTCUSDT", "sl") is True
        assert pm.has_algo_id("BTCUSDT", "tp1") is False  # tp1 不存在
        assert pm.has_algo_id("BTCUSDT", "tp2") is False  # tp2 不存在


# ============================================================
# 测试2: replenish_position_orders() 补单去重
# ============================================================

class TestReplenishPositionOrdersDedup:
    """测试 has_algo_id() 方法本身对三个角色的判断

    注意：补单流程已改为「先批量取消清场 → 全量重建」（FR-07/09），不再依赖
    has_algo_id() 做跳过判断；本组测试仅验证 has_algo_id() 方法自身的三种场景。
    """

    def test_algo_ids已存在tp1_tp2_sl_补单应跳过(self, pm):
        """
        模拟 algo_ids 中已有 "tp1"、"tp2"、"sl"
        验证 has_algo_id() 对三个角色都返回 True，即补单逻辑会跳过
        """
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)
        pm.add_algo_id("BTCUSDT", "tp1", 10002)
        pm.add_algo_id("BTCUSDT", "tp2", 10003)

        # 验证所有三个角色都已存在
        assert pm.has_algo_id("BTCUSDT", "sl") is True, "止损单应已存在，补单应跳过"
        assert pm.has_algo_id("BTCUSDT", "tp1") is True, "止盈1单应已存在，补单应跳过"
        assert pm.has_algo_id("BTCUSDT", "tp2") is True, "止盈2单应已存在，补单应跳过"

        # 验证 get_algo_ids 返回所有三个
        algo_ids = pm.get_algo_ids("BTCUSDT")
        assert len(algo_ids) == 3
        assert 10001 in algo_ids
        assert 10002 in algo_ids
        assert 10003 in algo_ids

    def test_algo_ids为空_补单应正常下发(self, pm):
        """
        模拟 algo_ids 为空
        验证 has_algo_id() 对所有角色都返回 False，即补单逻辑不会跳过
        """
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)

        # 验证所有角色都不存在，补单应正常下发
        assert pm.has_algo_id("BTCUSDT", "sl") is False, "止损单不存在，应正常补单"
        assert pm.has_algo_id("BTCUSDT", "tp1") is False, "止盈1单不存在，应正常补单"
        assert pm.has_algo_id("BTCUSDT", "tp2") is False, "止盈2单不存在，应正常补单"

        # 验证 get_algo_ids 返回空列表
        algo_ids = pm.get_algo_ids("BTCUSDT")
        assert len(algo_ids) == 0

    def test_algo_ids部分存在_部分补单(self, pm):
        """
        模拟 algo_ids 中只有 "sl"，"tp1"和"tp2"不存在
        验证 has_algo_id() 对 sl 返回 True，对 tp1/tp2 返回 False
        """
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)

        # sl 已存在，补单应跳过
        assert pm.has_algo_id("BTCUSDT", "sl") is True, "止损单已存在，补单应跳过"
        # tp1/tp2 不存在，补单应正常下发
        assert pm.has_algo_id("BTCUSDT", "tp1") is False, "止盈1单不存在，应正常补单"
        assert pm.has_algo_id("BTCUSDT", "tp2") is False, "止盈2单不存在，应正常补单"

        algo_ids = pm.get_algo_ids("BTCUSDT")
        assert len(algo_ids) == 1


# ============================================================
# 测试3: replenish_position_orders() 端到端补单去重
# ============================================================

class TestReplenishPositionOrdersE2E:
    """端到端测试 replenish_position_orders() 补单逻辑（FR-02/07/09）

    新语义：补单入口先批量取消清场（cancel_all_orders），取消后本地 algo_ids
    已清空 → SL/TP1/TP2 全量重建（target1_reached/target2_reached 逻辑不变）。
    """

    @pytest.fixture
    def mock_api(self):
        """创建完整的 mock API"""
        api = MagicMock()
        api.use_unified_account = False
        api.get_ticker = AsyncMock(return_value={"lastPrice": "50000.0"})
        api.place_conditional_order = AsyncMock(return_value={"algoId": 99999})
        api.cancel_algo_order = AsyncMock(return_value={"status": "CANCELED"})
        api.get_position = AsyncMock(return_value=[
            {"symbol": "BTCUSDT", "positionAmt": "-1.0", "entryPrice": "50000.0"}
        ])
        return api

    @pytest.fixture
    def mock_db(self):
        """创建 mock 数据库"""
        return MagicMock()

    @pytest.fixture
    def mock_notification(self):
        """创建 mock 通知客户端"""
        return MagicMock()

    @pytest.fixture
    def pm(self, mock_api):
        """覆盖全局 pm：与 executor 共用同一 mock_api，确保取消/下单断言一致"""
        return PositionManager(CONFIG, mock_api)

    @pytest.fixture
    def executor(self, mock_api, mock_db, mock_notification, pm):
        """创建 TradingExecutor 实例"""
        from strategies.hrs.executor import TradingExecutor
        return TradingExecutor(CONFIG, mock_api, mock_db, mock_notification, pm)

    @pytest.mark.asyncio
    async def test_已有全部algo_id时先取消清场再全量重建(self, mock_api, executor, pm):
        """
        FR-07/09：即使本地已有全部 algo_id，补单仍先批量取消清场，再全量重建，
        避免依赖「可能失效的本地记录」跳过导致保护单缺失。
        """
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)
        pm.add_algo_id("BTCUSDT", "tp1", 10002)
        pm.add_algo_id("BTCUSDT", "tp2", 10003)

        # 重置调用计数
        mock_api.place_conditional_order.reset_mock()
        mock_api.cancel_algo_order.reset_mock()

        result = await executor.replenish_position_orders(
            symbol="BTCUSDT",
            direction="short",
            entry_price=50000.0,
            entry_quantity=1.0,
            atr=1000.0,
            target1_reached=False,
            target2_reached=False,
        )

        # ReplenishResult 整体成功，三类保护单全部重建
        assert result.success is True
        assert result.failed_roles == []
        assert result.placed == 3

        # 先取消旧单（3 个旧 algo_id），再全量重建（3 次下单）
        assert mock_api.cancel_algo_order.call_count == 3, \
            f"应取消 3 个旧条件单，实际取消 {mock_api.cancel_algo_order.call_count} 个"
        assert mock_api.place_conditional_order.call_count == 3, \
            f"应重建 3 个保护单，实际下单 {mock_api.place_conditional_order.call_count} 次"

        # 重建后本地 algo_ids 重新写入
        assert len(pm.get_algo_ids("BTCUSDT")) == 3

    @pytest.mark.asyncio
    async def test_无algo_id时正常下单(self, mock_api, executor, pm):
        """
        模拟 algo_ids 为空
        验证 replenish_position_orders() 会正常调用 place_conditional_order 全量重建
        """
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)

        # 重置调用计数
        mock_api.place_conditional_order.reset_mock()
        mock_api.cancel_algo_order.reset_mock()

        result = await executor.replenish_position_orders(
            symbol="BTCUSDT",
            direction="short",
            entry_price=50000.0,
            entry_quantity=1.0,
            atr=1000.0,
            target1_reached=False,
            target2_reached=False,
        )

        assert result.success is True
        assert result.failed_roles == []
        assert result.placed == 3

        # 无旧单可取消；全量重建 sl + tp1 + tp2 = 3 次
        assert mock_api.cancel_algo_order.call_count == 0
        assert mock_api.place_conditional_order.call_count == 3, \
            f"应重建 3 个保护单，实际下单 {mock_api.place_conditional_order.call_count} 次"

    @pytest.mark.asyncio
    async def test_部分algo_id存在时也全量重建(self, mock_api, executor, pm):
        """
        FR-07/09：部分 algo_id 存在时，取消残留后全量重建（不依赖本地记录跳过）
        """
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)

        mock_api.place_conditional_order.reset_mock()
        mock_api.cancel_algo_order.reset_mock()

        result = await executor.replenish_position_orders(
            symbol="BTCUSDT",
            direction="short",
            entry_price=50000.0,
            entry_quantity=1.0,
            atr=1000.0,
            target1_reached=False,
            target2_reached=False,
        )

        assert result.success is True
        assert result.failed_roles == []
        assert result.placed == 3
        # 取消残留的 sl 后全量重建 sl + tp1 + tp2
        assert mock_api.cancel_algo_order.call_count == 1
        assert mock_api.place_conditional_order.call_count == 3

    @pytest.mark.asyncio
    async def test_target1已成交时跳过TP1补单(self, mock_api, executor, pm):
        """
        target1_reached=True 时只重建 sl + tp2（TP1 已成交不再补）
        """
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)

        mock_api.place_conditional_order.reset_mock()

        result = await executor.replenish_position_orders(
            symbol="BTCUSDT",
            direction="short",
            entry_price=50000.0,
            entry_quantity=1.0,
            atr=1000.0,
            target1_reached=True,
            target2_reached=False,
        )

        assert result.success is True
        assert result.failed_roles == []
        assert result.placed == 2  # sl + tp2
        assert mock_api.place_conditional_order.call_count == 2, \
            f"TP1 已成交应跳过，仅重建 sl+tp2，实际下单 {mock_api.place_conditional_order.call_count} 次"


# ============================================================
# 测试4: _reconcile_positions() 不再接管非本策略仓位
# ============================================================

class TestReconcilePositionsNoTakeover:
    """测试 _reconcile_positions() 对「交易所有而本地无」持仓的处理（FR-04）

    HRS 与其他策略共用账户，对非本策略持仓一律仅告警、不接管（硬性规定，无配置开关）
    """

    @pytest.fixture
    def mock_api(self):
        """创建 mock API"""
        api = MagicMock()
        api.get_position = AsyncMock(return_value=[])
        return api

    @pytest.fixture
    def mock_kline_service(self):
        """创建 mock K线服务"""
        return MagicMock()

    @pytest.fixture
    def mock_db(self):
        """创建 mock 数据库"""
        db = MagicMock()
        db.fetch_all = AsyncMock(return_value=[])
        db.fetch_one = AsyncMock(return_value=None)
        db.execute = AsyncMock(return_value=None)
        db.execute_ddl = AsyncMock(return_value=None)
        return db

    @pytest.fixture
    def mock_notification(self):
        """创建 mock 通知客户端"""
        return MagicMock()

    def test_交易所有多余持仓但本地无记录_仅告警不接管(self, mock_api, mock_db, mock_notification, pm, capsys):
        """
        模拟交易所有多余持仓但本地无记录
        验证 _reconcile_positions() 仅输出 WARNING 日志，不调用 add_position 接管
        """
        # 设置交易所返回持仓（BTCUSDT 有持仓，但本地没有）
        mock_api.get_position = AsyncMock(return_value=[
            {"symbol": "BTCUSDT", "positionAmt": "-1.0", "entryPrice": "50000.0"}
        ])

        from strategies.hrs.strategy import HRSStrategy

        # 创建策略实例（仅用于测试 _reconcile_positions）
        strategy = HRSStrategy(CONFIG)
        strategy.binance_client = mock_api
        strategy.position_manager = pm
        strategy.db = mock_db
        strategy.notification_client = mock_notification

        # 监控 add_position 调用
        original_add_position = pm.add_position
        call_count = [0]

        def tracking_add_position(*args, **kwargs):
            call_count[0] += 1
            return original_add_position(*args, **kwargs)

        pm.add_position = tracking_add_position

        try:
            # 执行对账
            asyncio.run(strategy._reconcile_positions())

            # 验证 add_position 没有被调用（核心断言）
            assert call_count[0] == 0, (
                f"_reconcile_positions 不应调用 add_position 接管非本策略持仓，"
                f"实际调用了 {call_count[0]} 次"
            )

            # 验证 structlog 输出了 WARNING 日志（structlog 默认输出到 stdout）
            captured = capsys.readouterr()
            combined_output = captured.out + captured.err
            assert "发现非本策略持仓" in combined_output, (
                f"structlog 应输出 WARNING 日志，实际输出:\n{captured.out[-500:]}"
            )

        finally:
            # 恢复原始的 add_position
            pm.add_position = original_add_position

    def test_交易所无持仓_本地也无_对账正常完成(self, mock_api, mock_db, mock_notification, pm):
        """
        模拟交易所无持仓、本地也无持仓
        验证 _reconcile_positions() 正常完成，不报错
        """
        mock_api.get_position = AsyncMock(return_value=[])

        from strategies.hrs.strategy import HRSStrategy

        strategy = HRSStrategy(CONFIG)
        strategy.binance_client = mock_api
        strategy.position_manager = pm
        strategy.db = mock_db
        strategy.notification_client = mock_notification

        # 不应抛出异常
        asyncio.run(strategy._reconcile_positions())

        # 本地持仓应仍为空
        assert pm.get_all_positions() == {}

    def test_本地有持仓但交易所无_清理本地记录(self, mock_api, mock_db, mock_notification, pm):
        """
        模拟本地有持仓且交易所也有持仓（但币种不同），验证本地持仓被清理

        注意：当交易所返回空列表时，_reconcile_positions() 会直接返回跳过对账，
        这是设计行为（交易所无持仓则无需对账）。因此本测试模拟交易所返回了其他
        币种的持仓，验证本地多余持仓被正确清理。
        """
        # 交易所返回 ETHUSDT 持仓，但本地有 BTCUSDT 持仓
        mock_api.get_position = AsyncMock(return_value=[
            {"symbol": "ETHUSDT", "positionAmt": "2.0", "entryPrice": "3000.0"}
        ])

        from strategies.hrs.strategy import HRSStrategy

        strategy = HRSStrategy(CONFIG)
        strategy.binance_client = mock_api
        strategy.position_manager = pm
        strategy.db = mock_db
        strategy.notification_client = mock_notification

        # 先添加本地持仓（BTCUSDT）
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        assert pm.has_position("BTCUSDT") is True

        # 执行对账
        asyncio.run(strategy._reconcile_positions())

        # BTCUSDT 本地持仓应被清理（因为交易所没有 BTCUSDT）
        assert pm.has_position("BTCUSDT") is False, (
            "交易所无 BTCUSDT 持仓时，本地 BTCUSDT 记录应被清理"
        )


# ============================================================
# 测试5: add_algo_id 和 get_algo_ids 联动
# ============================================================

class TestAlgoIdTracking:
    """测试 algoId 跟踪的完整流程"""

    def test_add_algo_id_正常记录(self, pm):
        """测试 add_algo_id 正常记录 algoId"""
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)
        pm.add_algo_id("BTCUSDT", "tp1", 10002)

        algo_ids = pm.get_algo_ids("BTCUSDT")
        assert len(algo_ids) == 2
        assert 10001 in algo_ids
        assert 10002 in algo_ids

    def test_add_algo_id_覆盖已有角色(self, pm):
        """测试 add_algo_id 覆盖已有角色的 algoId"""
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)
        # 覆盖
        pm.add_algo_id("BTCUSDT", "sl", 20001)

        algo_ids = pm.get_algo_ids("BTCUSDT")
        assert len(algo_ids) == 1
        assert 20001 in algo_ids
        assert 10001 not in algo_ids

    def test_add_algo_id_持仓不存在_不报错(self, pm):
        """测试对不存在的持仓调用 add_algo_id 不报错"""
        pm.add_algo_id("NONEXIST", "sl", 10001)  # 不应报错

    def test_get_algo_ids_持仓不存在_返回空列表(self, pm):
        """测试对不存在的持仓调用 get_algo_ids 返回空列表"""
        result = pm.get_algo_ids("NONEXIST")
        assert result == []


# ============================================================
# 测试6: cancel_all_orders() 批量取消三路径（FR-07）
# ============================================================

class TestCancelAllOrders:
    """测试 cancel_all_orders() 的批量/回退路径

    - use_unified_account=True 且 code=200 → 批量成功（method="batch"）
    - 批量返回 code≠200 → 回退逐个取消（method="individual"）
    - 批量抛异常 → 回退逐个取消（method="individual"）
    - 非统一账户 → 直接逐个取消
    """

    @pytest.fixture
    def pm(self, mock_binance_api):
        return PositionManager(CONFIG, mock_binance_api)

    def _add_position_with_algo_ids(self, pm):
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)
        pm.add_algo_id("BTCUSDT", "tp1", 10002)

    def test_统一账户批量取消成功(self, pm):
        """use_unified_account=True 且 code=200 → 批量成功，清空本地记录"""
        self._add_position_with_algo_ids(pm)
        pm.binance_api.use_unified_account = True
        pm.binance_api.cancel_all_algo_orders = AsyncMock(return_value={
            "code": 200, "total": 2, "cancelled": 2, "failed": 0,
        })

        result = asyncio.run(pm.cancel_all_orders("BTCUSDT"))

        assert result["method"] == "batch"
        assert result["cancelled"] == 2
        assert result["failed"] == 0
        # 批量成功 → 清空本地 algo_ids，且不逐个取消
        assert pm.get_algo_ids("BTCUSDT") == []
        pm.binance_api.cancel_algo_order.assert_not_called()

    def test_统一账户批量返回非200_回退逐个取消(self, pm):
        """批量返回 code≠200 → 回退逐个取消"""
        self._add_position_with_algo_ids(pm)
        pm.binance_api.use_unified_account = True
        pm.binance_api.cancel_all_algo_orders = AsyncMock(return_value={
            "code": 400, "total": 0, "cancelled": 0, "failed": 1,
        })

        result = asyncio.run(pm.cancel_all_orders("BTCUSDT"))

        assert result["method"] == "individual"
        assert result["total"] == 2
        assert result["cancelled"] == 2
        assert result["failed"] == 0
        assert pm.binance_api.cancel_algo_order.call_count == 2
        # 回退逐个取消成功后同样清空本地
        assert pm.get_algo_ids("BTCUSDT") == []

    def test_统一账户批量抛异常_回退逐个取消(self, pm):
        """批量接口抛异常 → 回退逐个取消"""
        self._add_position_with_algo_ids(pm)
        pm.binance_api.use_unified_account = True
        pm.binance_api.cancel_all_algo_orders = AsyncMock(
            side_effect=RuntimeError("网络错误")
        )

        result = asyncio.run(pm.cancel_all_orders("BTCUSDT"))

        assert result["method"] == "individual"
        assert result["cancelled"] == 2
        assert pm.binance_api.cancel_algo_order.call_count == 2
        assert pm.get_algo_ids("BTCUSDT") == []

    def test_非统一账户直接逐个取消(self, pm):
        """use_unified_account=False → 不调用批量接口，直接逐个取消"""
        self._add_position_with_algo_ids(pm)
        pm.binance_api.use_unified_account = False

        result = asyncio.run(pm.cancel_all_orders("BTCUSDT"))

        assert result["method"] == "individual"
        assert result["cancelled"] == 2
        pm.binance_api.cancel_all_algo_orders.assert_not_called()

    def test_逐个取消单个失败计入failed(self, pm):
        """逐个取消时单个失败 → 计入 failed，其余正常取消"""
        self._add_position_with_algo_ids(pm)
        pm.binance_api.use_unified_account = False

        async def fake_cancel(symbol, algo_id):
            if algo_id == 10001:
                raise RuntimeError("取消失败")
            return {"status": "CANCELED"}

        pm.binance_api.cancel_algo_order = AsyncMock(side_effect=fake_cancel)

        result = asyncio.run(pm.cancel_all_orders("BTCUSDT"))

        assert result["method"] == "individual"
        assert result["total"] == 2
        assert result["cancelled"] == 1
        assert result["failed"] == 1


# ============================================================
# 测试7: _place_protection_order() 幂等入口（FR-05）
# ============================================================

class TestPlaceProtectionOrder:
    """测试 _place_protection_order() 的三种分支

    - 数量≤0 → 跳过（非失败）
    - 下单抛异常 → 返回失败，不抛异常
    - 下单成功但 algoId=None → 视为失败
    """

    @pytest.fixture
    def executor(self, mock_binance_api):
        pm = PositionManager(CONFIG, mock_binance_api)
        return TradingExecutor(CONFIG, mock_binance_api, MagicMock(), MagicMock(), pm)

    def test_数量小于等于0_跳过(self, executor):
        """数量<=0 → skipped=True，不调用下单"""
        result = asyncio.run(executor._place_protection_order(
            symbol="BTCUSDT", role="tp1", side="BUY",
            order_type="TAKE_PROFIT", stop_price=Decimal("49000"),
            limit_price=Decimal("49073.5"), quantity=Decimal("0"),
        ))
        assert result["success"] is True
        assert result["skipped"] is True
        assert result["algo_id"] is None
        executor.binance_api.place_conditional_order.assert_not_called()

    def test_下单抛异常_返回失败不抛出(self, executor):
        """下单抛异常 → 返回 success=False，异常被捕获不向外抛"""
        executor.binance_api.place_conditional_order = AsyncMock(
            side_effect=RuntimeError("下单失败")
        )
        result = asyncio.run(executor._place_protection_order(
            symbol="BTCUSDT", role="sl", side="BUY", order_type="STOP",
            stop_price=Decimal("51000"), limit_price=Decimal("51102"),
            quantity=Decimal("1.0"), close_position=True,
        ))
        assert result["success"] is False
        assert result["skipped"] is False
        assert result["algo_id"] is None
        assert result["error"] == "下单失败"

    def test_下单成功但algoId为None_视为失败(self, executor):
        """下单成功但未返回 algoId → 视为失败（FR-05）"""
        executor.binance_api.place_conditional_order = AsyncMock(
            return_value={"algoId": None}
        )
        result = asyncio.run(executor._place_protection_order(
            symbol="BTCUSDT", role="tp1", side="BUY", order_type="TAKE_PROFIT",
            stop_price=Decimal("49000"), limit_price=Decimal("49073.5"),
            quantity=Decimal("0.3"),
        ))
        assert result["success"] is False
        assert "未返回algoId" in result["error"]

    def test_下单返回None_视为失败(self, executor):
        """下单返回空 → 视为失败"""
        executor.binance_api.place_conditional_order = AsyncMock(return_value=None)
        result = asyncio.run(executor._place_protection_order(
            symbol="BTCUSDT", role="tp1", side="BUY", order_type="TAKE_PROFIT",
            stop_price=Decimal("49000"), limit_price=Decimal("49073.5"),
            quantity=Decimal("0.3"),
        ))
        assert result["success"] is False
        assert result["error"] == "下单失败"

    def test_下单成功且返回algoId_写入本地记录(self, executor):
        """下单成功且返回 algoId → 写入本地记录（持仓未建立时写入 pending 缓冲）"""
        result = asyncio.run(executor._place_protection_order(
            symbol="BTCUSDT", role="tp1", side="BUY", order_type="TAKE_PROFIT",
            stop_price=Decimal("49000"), limit_price=Decimal("49073.5"),
            quantity=Decimal("0.3"),
        ))
        assert result["success"] is True
        assert result["skipped"] is False
        assert result["algo_id"] == 12345
        # 持仓未建立 → 写入 pending 缓冲，add_position 时合并
        assert executor.position_manager._pending_algo_ids["BTCUSDT"]["tp1"] == 12345


# ============================================================
# 测试8: detect_take_profit_fills() + _detect_target_filled() 状态机（P1-6）
# ============================================================

class TestDetectTakeProfitFills:
    """测试通过持仓数量变化检测止盈成交的状态机

    - 100% → 70%：TP1 成交（返回 1）
    - 70% → 30%：TP2 成交（返回 2）
    - 0%：全平（返回 0）
    - 双目标均已达成再减少：无新目标（返回 None）
    """

    @pytest.fixture
    def pm(self, mock_binance_api):
        return PositionManager(CONFIG, mock_binance_api)

    def test_无持仓返回None(self, pm):
        assert pm.detect_take_profit_fills("BTCUSDT", 1.0) is None

    def test_首次跟踪仅记录返回None(self, pm):
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        assert pm.detect_take_profit_fills("BTCUSDT", 1.0) is None
        assert pm._last_tracked_qty["BTCUSDT"] == 1.0

    def test_持仓减少到70_标记TP1(self, pm):
        """100% → 70%：TP1 成交，返回 1"""
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        assert pm.detect_take_profit_fills("BTCUSDT", 0.7) == 1
        pos = pm.get_position("BTCUSDT")
        assert pos["target1_reached"] is True
        assert pos["target2_reached"] is False
        # 剩余数量 = 1 * (1 - 0.30) = 0.7
        assert pos["remaining_quantity"] == pytest.approx(0.7)

    def test_TP1后再减少到30_标记TP2(self, pm):
        """70% → 30%：TP2 成交，返回 2，激活移动止损"""
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.detect_take_profit_fills("BTCUSDT", 0.7)
        assert pm.detect_take_profit_fills("BTCUSDT", 0.3) == 2
        pos = pm.get_position("BTCUSDT")
        assert pos["target1_reached"] is True
        assert pos["target2_reached"] is True

    def test_持仓清零_全平返回0(self, pm):
        """持仓清零 → 全部平仓，返回 0"""
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        assert pm.detect_take_profit_fills("BTCUSDT", 0.0) == 0

    def test_持仓无变化_返回None(self, pm):
        """持仓数量无变化（容差内）→ 返回 None"""
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.detect_take_profit_fills("BTCUSDT", 1.0)  # 首次跟踪
        assert pm.detect_take_profit_fills("BTCUSDT", 1.0) is None
        # 微小增加不触发
        assert pm.detect_take_profit_fills("BTCUSDT", 1.005) is None

    def test_持仓增加_返回None(self, pm):
        """持仓增加（如加仓）→ 不触发止盈检测"""
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.detect_take_profit_fills("BTCUSDT", 1.0)
        assert pm.detect_take_profit_fills("BTCUSDT", 1.2) is None

    def test_双目标均已达成_再次减少返回None(self, pm):
        """TP1/TP2 均已达成后再减少 → 无新目标，返回 None"""
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.detect_take_profit_fills("BTCUSDT", 0.7)
        pm.detect_take_profit_fills("BTCUSDT", 0.3)
        assert pm.detect_take_profit_fills("BTCUSDT", 0.1) is None


# ============================================================
# 测试9: _check_exchange_position_allows_entry() 四分支（FR-03）
# ============================================================

class TestCheckExchangePositionAllowsEntry:
    """测试开仓前交易所持仓核对

    - none → 放行（True）
    - opposite → 拒绝（False）
    - same → 拒绝（False，保守跳过交由对账接管）
    - error → 拒绝（False，查询异常保守拒绝）
    """

    @pytest.fixture
    def strategy(self):
        from strategies.hrs.strategy import HRSStrategy
        s = HRSStrategy(CONFIG)
        s.binance_client = MagicMock()
        return s

    def test_无持仓放行(self, strategy):
        strategy._get_exchange_position_status = AsyncMock(return_value="none")
        assert asyncio.run(strategy._check_exchange_position_allows_entry("BTCUSDT", "short")) is True

    def test_反向持仓拒绝(self, strategy):
        strategy._get_exchange_position_status = AsyncMock(return_value="opposite")
        assert asyncio.run(strategy._check_exchange_position_allows_entry("BTCUSDT", "short")) is False

    def test_同向持仓拒绝(self, strategy):
        strategy._get_exchange_position_status = AsyncMock(return_value="same")
        assert asyncio.run(strategy._check_exchange_position_allows_entry("BTCUSDT", "short")) is False

    def test_查询失败拒绝(self, strategy):
        strategy._get_exchange_position_status = AsyncMock(return_value="error")
        assert asyncio.run(strategy._check_exchange_position_allows_entry("BTCUSDT", "short")) is False

    def test_查询异常保守拒绝(self, strategy):
        """get_position 抛异常 → status="error" → 保守拒绝开仓"""
        strategy.binance_client.get_position = AsyncMock(side_effect=RuntimeError("查询失败"))
        assert asyncio.run(strategy._get_exchange_position_status("BTCUSDT", "short")) == "error"
        assert asyncio.run(strategy._check_exchange_position_allows_entry("BTCUSDT", "short")) is False

    def test_get_exchange_position_status_无持仓(self, strategy):
        strategy.binance_client.get_position = AsyncMock(return_value=[])
        assert asyncio.run(strategy._get_exchange_position_status("BTCUSDT", "short")) == "none"

    def test_get_exchange_position_status_反向持仓(self, strategy):
        """做空信号遇多头持仓 → opposite；做多信号遇空头持仓 → opposite"""
        strategy.binance_client.get_position = AsyncMock(return_value=[{"positionAmt": "-1.0"}])
        assert asyncio.run(strategy._get_exchange_position_status("BTCUSDT", "short")) == "same"
        assert asyncio.run(strategy._get_exchange_position_status("BTCUSDT", "long")) == "opposite"

    def test_get_exchange_position_status_同向持仓(self, strategy):
        strategy.binance_client.get_position = AsyncMock(return_value=[{"positionAmt": "1.0"}])
        assert asyncio.run(strategy._get_exchange_position_status("BTCUSDT", "long")) == "same"
        assert asyncio.run(strategy._get_exchange_position_status("BTCUSDT", "short")) == "opposite"

    def test_get_exchange_position_status_忽略零持仓(self, strategy):
        """零持仓跳过，遇有效持仓才判定方向"""
        strategy.binance_client.get_position = AsyncMock(return_value=[
            {"positionAmt": "0.0"}, {"positionAmt": "-2.0"},
        ])
        assert asyncio.run(strategy._get_exchange_position_status("BTCUSDT", "short")) == "same"


# ============================================================
# 测试10: _send_anomaly_alert() 开关与异常（FR-01/04）
# ============================================================

class TestSendAnomalyAlert:
    """测试异常告警发送

    - 告警开关关闭 → 跳过不发送
    - 事件开关关闭 → 跳过不发送
    - 发送成功 → 调用 notification_client.send(warning)
    - 发送异常 → 记录 warning，不抛出
    """

    @pytest.fixture
    def strategy(self):
        from strategies.hrs.strategy import HRSStrategy
        s = HRSStrategy(CONFIG)
        s.notification_client = MagicMock()
        return s

    def test_告警总开关关闭_不发送(self, strategy):
        strategy._notif_enabled = False
        asyncio.run(strategy._send_anomaly_alert("测试告警"))
        strategy.notification_client.send.assert_not_called()

    def test_告警事件开关关闭_不发送(self, strategy):
        strategy._notif_enabled = True
        strategy._notif_events = {"anomaly_alert": False}
        asyncio.run(strategy._send_anomaly_alert("测试告警"))
        strategy.notification_client.send.assert_not_called()

    def test_发送成功_使用warning级别(self, strategy):
        strategy._notif_enabled = True
        strategy._notif_events = {"anomaly_alert": True}
        strategy.notification_client.send = AsyncMock(return_value=None)
        asyncio.run(strategy._send_anomaly_alert("测试告警"))
        strategy.notification_client.send.assert_awaited_once_with(
            message="测试告警", level="warning", project="hrs"
        )

    def test_发送异常_记录warning不抛出(self, strategy):
        strategy._notif_enabled = True
        strategy._notif_events = {"anomaly_alert": True}
        strategy.notification_client.send = AsyncMock(side_effect=RuntimeError("发送失败"))
        # 不应抛异常
        asyncio.run(strategy._send_anomaly_alert("测试告警"))


# ============================================================
# 测试11: execute_signal() 配置组合（reject_on_exchange_position）
# ============================================================

@pytest.fixture
def mock_db():
    """创建 mock 数据库"""
    db = MagicMock()
    db.fetch_all = AsyncMock(return_value=[])
    db.fetch_one = AsyncMock(return_value=None)
    db.execute = AsyncMock(return_value=None)
    db.execute_ddl = AsyncMock(return_value=None)
    return db


@pytest.fixture
def mock_notification():
    """创建 mock 通知客户端"""
    return MagicMock()


@pytest.fixture
def hrs_env(mock_db, mock_notification):
    """构建真实 HRSStrategy + 真实 TradingExecutor + mock 外部依赖

    交易所、风控、资金、K线/费率前置检查均 mock，禁止真实网络调用；
    execute_signal 会真实走完 开仓 → 保护单 → 记录持仓 全链路。
    """
    from strategies.hrs.strategy import HRSStrategy

    api = MagicMock()
    api.use_unified_account = False
    api.get_ticker = AsyncMock(return_value={"lastPrice": "50000.0"})
    api.get_symbol_info = AsyncMock(return_value={"tickSize": "0.1", "stepSize": "0.001"})
    api.set_leverage = AsyncMock(return_value=None)
    api.get_account_info = AsyncMock(return_value={"totalMarginBalance": "1000.0"})
    api.place_order = AsyncMock(return_value={
        "orderId": 100, "executedQty": "0.1", "origQty": "0.1", "status": "FILLED",
    })
    api.place_conditional_order = AsyncMock(return_value={"algoId": 10001})
    api.get_position = AsyncMock(return_value=[])
    api.cancel_algo_order = AsyncMock(return_value={"status": "CANCELED"})

    pm = PositionManager(CONFIG, api)
    executor = TradingExecutor(CONFIG, api, mock_db, mock_notification, pm)
    executor.calculate_atr = AsyncMock(return_value=1000.0)

    strategy = HRSStrategy(CONFIG)
    strategy.binance_client = api
    strategy.position_manager = pm
    strategy.trading_executor = executor
    strategy.db = mock_db
    strategy.notification_client = mock_notification

    risk = MagicMock()
    risk.can_open_position = MagicMock(return_value=True)
    risk.is_blacklisted = MagicMock(return_value=False)
    risk.record_open = MagicMock(return_value=None)
    risk.calculate_position_size = MagicMock(return_value=0.1)
    strategy.risk_manager = risk

    capital = MagicMock()
    capital.can_open_position = MagicMock(return_value=True)
    capital.get_allocated_capital = MagicMock(return_value=10000.0)
    strategy.capital_mgr = capital

    # 简化信号前置检查与状态持久化
    strategy._check_oi_before_entry = AsyncMock(return_value=True)
    strategy._check_funding_rate_before_entry = AsyncMock(return_value=True)
    strategy._save_state = AsyncMock(return_value=None)

    return {
        "strategy": strategy, "executor": executor, "pm": pm, "api": api,
        "risk": risk, "capital": capital, "notification": mock_notification, "db": mock_db,
    }


def _short_signal():
    return {
        "symbol": "BTCUSDT", "direction": "short", "score": 0.9,
        "current_price": 50000.0, "klines": [], "entry_mode": "standard",
    }


def _long_signal():
    return {
        "symbol": "BTCUSDT", "direction": "long", "score": 0.9,
        "current_price": 50000.0, "klines": [], "entry_mode": "standard",
    }


class TestExecuteSignalRejectOnExchangePosition:
    """测试 reject_on_exchange_position 开关对 execute_signal 的影响（FR-03）"""

    def test_开启开关且交易所不允许开仓_拒绝(self, hrs_env):
        """reject=True 且交易所持仓检查返回 False → 直接拒绝，不开仓"""
        strategy = hrs_env["strategy"]
        api = hrs_env["api"]
        strategy._reject_on_exchange_position = True
        strategy._check_exchange_position_allows_entry = AsyncMock(return_value=False)

        assert asyncio.run(strategy.execute_signal(_short_signal())) is False
        assert hrs_env["pm"].has_position("BTCUSDT") is False
        api.place_order.assert_not_called()

    def test_开启开关且交易所允许开仓_正常执行(self, hrs_env):
        """reject=True 且交易所无持仓（真实检查 none → 放行）→ 正常开仓"""
        strategy = hrs_env["strategy"]
        strategy._reject_on_exchange_position = True

        assert asyncio.run(strategy.execute_signal(_short_signal())) is True
        assert hrs_env["pm"].has_position("BTCUSDT") is True
        hrs_env["api"].place_order.assert_awaited_once()

    def test_关闭开关_不执行交易所持仓检查(self, hrs_env):
        """reject=False → 即使持仓检查会返回 False 也不调用，正常开仓"""
        strategy = hrs_env["strategy"]
        api = hrs_env["api"]
        strategy._reject_on_exchange_position = False
        strategy._check_exchange_position_allows_entry = AsyncMock(return_value=False)

        assert asyncio.run(strategy.execute_signal(_long_signal())) is True
        strategy._check_exchange_position_allows_entry.assert_not_called()
        assert hrs_env["pm"].has_position("BTCUSDT") is True
        api.place_order.assert_awaited_once()


# ============================================================
# 测试12: _create_protection_orders() 失败角色收集（FR-01）
# ============================================================

class TestCreateProtectionOrdersFailure:
    """测试保护单创建失败时 failed_roles 收集

    - TP 下单失败 → failed_roles 非空，protection_complete=False
    - 即使保护单不完整，开仓成交仍记录持仓，并触发补单告警
    """

    def test_TP下单失败_收集失败角色_仍记录持仓并触发补单告警(self, hrs_env):
        """TP1/TP2 下单抛异常 → failed_roles=['tp1','tp2']，
        开仓成交仍记录持仓，触发补单与异常告警"""
        strategy = hrs_env["strategy"]
        api = hrs_env["api"]
        pm = hrs_env["pm"]
        strategy._reject_on_exchange_position = True
        strategy._replenish_single_position = AsyncMock(return_value=None)
        strategy._send_anomaly_alert = AsyncMock(return_value=None)

        async def fake_conditional(symbol, side, order_type, stop_price, price, quantity, **kwargs):
            if order_type == "TAKE_PROFIT":
                raise RuntimeError("TP下单失败")
            return {"algoId": 10001}

        api.place_conditional_order = AsyncMock(side_effect=fake_conditional)

        assert asyncio.run(strategy.execute_signal(_short_signal())) is True

        # 仍记录持仓
        assert pm.has_position("BTCUSDT") is True
        # 触发补单
        strategy._replenish_single_position.assert_awaited_once_with("BTCUSDT")
        # 触发异常告警，告警内容含失败角色
        strategy._send_anomaly_alert.assert_awaited_once()
        message = strategy._send_anomaly_alert.await_args.args[0]
        assert "tp1" in message and "tp2" in message

    def test_execute_short_保护单全部成功_无失败角色(self, hrs_env):
        """全部保护单成功 → failed_roles 为空，protection_complete=True"""
        result = asyncio.run(hrs_env["executor"].execute_short(
            symbol="BTCUSDT", entry_price=50000.0, atr=1000.0,
            quantity=Decimal("0.1"), score=0.9,
        ))
        assert result.order_filled is True
        assert result.protection_complete is True
        assert result.failed_roles == []

    def test_execute_short_TP返回algoId为None_收集失败角色(self, hrs_env):
        """TP 下单成功但 algoId=None → 视为失败，收集 tp1/tp2"""
        api = hrs_env["api"]

        async def fake_conditional(symbol, side, order_type, stop_price, price, quantity, **kwargs):
            if order_type == "TAKE_PROFIT":
                return {"algoId": None}
            return {"algoId": 10001}

        api.place_conditional_order = AsyncMock(side_effect=fake_conditional)

        result = asyncio.run(hrs_env["executor"].execute_short(
            symbol="BTCUSDT", entry_price=50000.0, atr=1000.0,
            quantity=Decimal("0.1"), score=0.9,
        ))
        assert result.order_filled is True
        assert result.protection_complete is False
        assert set(result.failed_roles) == {"tp1", "tp2"}

    def test_execute_long_保护单全部成功(self, hrs_env):
        """做多全链路保护单成功"""
        result = asyncio.run(hrs_env["executor"].execute_long(
            symbol="BTCUSDT", entry_price=50000.0, atr=1000.0,
            quantity=Decimal("0.1"), score=0.9,
        ))
        assert result.order_filled is True
        assert result.protection_complete is True
        assert result.failed_roles == []


# ============================================================
# 测试13: 补单链路边界（replenish_position_orders）
# ============================================================

class TestReplenishChainBoundary:
    """测试补单链路的边界分支

    - atr≤0 → 返回 invalid_atr，不查询精度/不下单
    - 当前价格已过 TP2 → note="price_past_tp2" 透传，TP2 跳过
    - 双目标均已达成 → 跳过 SL 补单，不重建任何保护单
    """

    @pytest.fixture
    def executor(self, mock_binance_api):
        pm = PositionManager(CONFIG, mock_binance_api)
        return TradingExecutor(CONFIG, mock_binance_api, MagicMock(), MagicMock(), pm)

    def test_atr无效_返回invalid_atr(self, executor):
        """atr<=0 → success=False，note='invalid_atr'，不查询精度"""
        result = asyncio.run(executor.replenish_position_orders(
            symbol="BTCUSDT", direction="short", entry_price=50000.0,
            entry_quantity=1.0, atr=0.0,
        ))
        assert result.success is False
        assert result.failed_roles == []
        assert result.note == "invalid_atr"
        executor.binance_api.get_symbol_info.assert_not_called()

    def test_当前价格已过TP2_返回price_past_tp2透传(self, executor):
        """做空价格已跌破 TP2 → note='price_past_tp2'，TP2 跳过，SL+TP1 重建"""
        executor.binance_api.get_ticker = AsyncMock(return_value={"lastPrice": "46000.0"})
        executor.binance_api.get_symbol_info = AsyncMock(
            return_value={"tickSize": "0.1", "stepSize": "0.001"}
        )
        executor.binance_api.place_conditional_order = AsyncMock(return_value={"algoId": 99999})

        result = asyncio.run(executor.replenish_position_orders(
            symbol="BTCUSDT", direction="short", entry_price=50000.0,
            entry_quantity=1.0, atr=1000.0,
            target1_reached=False, target2_reached=False,
        ))

        assert result.success is True
        assert result.failed_roles == []
        assert result.note == "price_past_tp2"
        # SL + TP1 成功重建，TP2 跳过
        assert result.placed == 2

    def test_双目标均已达成_跳过SL不重建(self, executor):
        """target1/target2 均已达成 → 跳过 SL，不重建任何保护单"""
        executor.binance_api.get_symbol_info = AsyncMock(
            return_value={"tickSize": "0.1", "stepSize": "0.001"}
        )
        executor.binance_api.place_conditional_order = AsyncMock(return_value={"algoId": 99999})

        result = asyncio.run(executor.replenish_position_orders(
            symbol="BTCUSDT", direction="short", entry_price=50000.0,
            entry_quantity=1.0, atr=1000.0,
            target1_reached=True, target2_reached=True,
        ))

        assert result.success is True
        assert result.failed_roles == []
        assert result.placed == 0
        executor.binance_api.place_conditional_order.assert_not_called()


# ============================================================
# 测试14: 平仓链路（close_position）
# ============================================================

class TestClosePosition:
    """测试 close_position() 的限价/市价/未找到持仓分支"""

    @pytest.fixture
    def executor(self, mock_binance_api):
        pm = PositionManager(CONFIG, mock_binance_api)
        return TradingExecutor(CONFIG, mock_binance_api, MagicMock(), MagicMock(), pm)

    def test_平仓成功_限价单(self, executor):
        """找到持仓且有有效价格 → 限价平仓"""
        executor.binance_api.get_position = AsyncMock(return_value=[
            {"symbol": "BTCUSDT", "positionAmt": "-1.0"},
        ])
        executor.binance_api.get_ticker = AsyncMock(return_value={"lastPrice": "51000.0"})
        executor.binance_api.get_symbol_info = AsyncMock(
            return_value={"tickSize": "0.1", "stepSize": "0.001"}
        )
        executor.binance_api.place_order = AsyncMock(return_value={"orderId": 200})

        order = asyncio.run(executor.close_position("BTCUSDT", "short", reason="测试平仓"))

        assert order == {"orderId": 200}
        call_kwargs = executor.binance_api.place_order.await_args.kwargs
        assert call_kwargs["side"] == "BUY"
        assert call_kwargs["order_type"] == "LIMIT"

    def test_平仓未找到持仓_返回None(self, executor):
        """交易所无对应方向持仓 → 返回 None"""
        executor.binance_api.get_position = AsyncMock(return_value=[])
        assert asyncio.run(executor.close_position("BTCUSDT", "short")) is None

    def test_平仓价格无效_市价平仓(self, executor):
        """当前价格无效（<=0）→ 市价平仓"""
        executor.binance_api.get_position = AsyncMock(return_value=[
            {"symbol": "BTCUSDT", "positionAmt": "-1.0"},
        ])
        executor.binance_api.get_ticker = AsyncMock(return_value={"lastPrice": "0.0"})
        executor.binance_api.get_symbol_info = AsyncMock(
            return_value={"tickSize": "0.1", "stepSize": "0.001"}
        )
        executor.binance_api.place_order = AsyncMock(return_value={"orderId": 201})

        order = asyncio.run(executor.close_position("BTCUSDT", "short"))

        assert order == {"orderId": 201}
        assert executor.binance_api.place_order.await_args.kwargs["order_type"] == "MARKET"

    def test_平仓下单失败_返回None(self, executor):
        """平仓下单返回空 → 返回 None"""
        executor.binance_api.get_position = AsyncMock(return_value=[
            {"symbol": "BTCUSDT", "positionAmt": "-1.0"},
        ])
        executor.binance_api.get_ticker = AsyncMock(return_value={"lastPrice": "51000.0"})
        executor.binance_api.get_symbol_info = AsyncMock(
            return_value={"tickSize": "0.1", "stepSize": "0.001"}
        )
        executor.binance_api.place_order = AsyncMock(return_value=None)
        assert asyncio.run(executor.close_position("BTCUSDT", "short")) is None


# ============================================================
# 测试15: 加仓链路（add_to_position）
# ============================================================

class TestAddToPosition:
    """测试 add_to_position() 的数量校验/取消清场/保护单重建"""

    @pytest.fixture
    def executor(self, mock_binance_api):
        pm = PositionManager(CONFIG, mock_binance_api)
        return TradingExecutor(CONFIG, mock_binance_api, MagicMock(), MagicMock(), pm)

    def test_加仓数量为零_跳过(self, executor):
        """加仓数量<=0 → 返回 None，不下单"""
        assert asyncio.run(executor.add_to_position(
            "BTCUSDT", "short", 50000.0, 1000.0, Decimal("0"), 0.8,
        )) is None
        executor.binance_api.place_order.assert_not_called()

    def test_加仓成功_取消旧单并重下保护单(self, executor):
        """加仓成交 → 取消旧条件单 → 基于交易所最新总量重下 3 个保护单"""
        pm = executor.position_manager
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)

        executor.binance_api.use_unified_account = False
        executor.binance_api.get_symbol_info = AsyncMock(
            return_value={"tickSize": "0.1", "stepSize": "0.001"}
        )
        executor.binance_api.place_order = AsyncMock(return_value={"orderId": 300})
        executor.binance_api.get_position = AsyncMock(return_value=[
            {"symbol": "BTCUSDT", "positionAmt": "-1.5"},
        ])
        executor.binance_api.place_conditional_order = AsyncMock(return_value={"algoId": 20001})
        executor.binance_api.cancel_algo_order = AsyncMock(return_value={"status": "CANCELED"})

        order = asyncio.run(executor.add_to_position(
            "BTCUSDT", "short", 50000.0, 1000.0, Decimal("0.5"), 0.8,
        ))

        assert order == {"orderId": 300}
        # 取消旧单 1 个
        assert executor.binance_api.cancel_algo_order.call_count == 1
        # 保护单重建 3 个
        assert executor.binance_api.place_conditional_order.call_count == 3

    def test_加仓取消旧单失败_中止重下(self, executor):
        """取消旧条件单失败 → 中止重下保护单，返回 None"""
        pm = executor.position_manager
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)

        executor.binance_api.use_unified_account = False
        executor.binance_api.get_symbol_info = AsyncMock(
            return_value={"tickSize": "0.1", "stepSize": "0.001"}
        )
        executor.binance_api.place_order = AsyncMock(return_value={"orderId": 301})
        executor.binance_api.cancel_algo_order = AsyncMock(
            side_effect=RuntimeError("取消失败")
        )

        assert asyncio.run(executor.add_to_position(
            "BTCUSDT", "short", 50000.0, 1000.0, Decimal("0.5"), 0.8,
        )) is None
        executor.binance_api.place_conditional_order.assert_not_called()


# ============================================================
# 测试16: 开仓通知（_send_notification）
# ============================================================

class TestSendNotification:
    """测试 _send_notification() 的事件开关与异常处理"""

    @pytest.fixture
    def executor(self, mock_binance_api, mock_notification):
        pm = PositionManager(CONFIG, mock_binance_api)
        return TradingExecutor(CONFIG, mock_binance_api, MagicMock(), mock_notification, pm)

    def test_通知发送成功(self, executor, mock_notification):
        executor._should_notify = MagicMock(return_value=True)
        mock_notification.send = AsyncMock(return_value=None)
        asyncio.run(executor._send_notification("BTCUSDT", "做空", 50000.0, 0.1, 0.9, 51000.0))
        mock_notification.send.assert_awaited_once()
        assert mock_notification.send.await_args.kwargs["project"] == "hrs"
        assert mock_notification.send.await_args.kwargs["level"] == "info"

    def test_通知事件开关关闭_不发送(self, executor, mock_notification):
        executor._should_notify = MagicMock(return_value=False)
        asyncio.run(executor._send_notification("BTCUSDT", "做空", 50000.0, 0.1, 0.9, 51000.0))
        mock_notification.send.assert_not_called()

    def test_通知发送异常_记录warning不抛出(self, executor, mock_notification):
        executor._should_notify = MagicMock(return_value=True)
        mock_notification.send = AsyncMock(side_effect=RuntimeError("发送失败"))
        # 不应抛异常
        asyncio.run(executor._send_notification("BTCUSDT", "做空", 50000.0, 0.1, 0.9, 51000.0))


# ============================================================
# 测试17: calculate_atr()（补充 executor 覆盖率）
# ============================================================

def _make_klines(n: int):
    """生成 n 根递增的模拟 K 线（high/low/close 均为可解析字符串）"""
    return [
        {"high": str(100 + i), "low": str(99 + i), "close": str(99.5 + i)}
        for i in range(n)
    ]


class TestCalculateAtr:
    """测试 calculate_atr() 的充足/不足/异常分支"""

    @pytest.fixture
    def executor(self, mock_binance_api):
        pm = PositionManager(CONFIG, mock_binance_api)
        return TradingExecutor(CONFIG, mock_binance_api, MagicMock(), MagicMock(), pm)

    def test_klines充足_返回正值(self, executor):
        atr = asyncio.run(executor.calculate_atr("BTCUSDT", _make_klines(20)))
        assert atr > 0

    def test_klines不足_返回0(self, executor):
        assert asyncio.run(executor.calculate_atr("BTCUSDT", [{"high": "1", "low": "1", "close": "1"}])) == 0.0

    def test_klines为空_返回0(self, executor):
        assert asyncio.run(executor.calculate_atr("BTCUSDT", [])) == 0.0

    def test_计算异常_返回0(self, executor):
        # high 字段无法转为 float → 触发异常路径
        bad = [{"high": "x", "low": "y", "close": "z"} for _ in range(20)]
        assert asyncio.run(executor.calculate_atr("BTCUSDT", bad)) == 0.0


# ============================================================
# 测试18: 开仓/加仓边界（下单失败、订单无ID、lv_rm 模式）
# ============================================================

class TestExecutorEdgeBranches:
    """补充 executor 未覆盖的边界分支"""

    def test_execute_short_下单失败_返回order_filledFalse(self, hrs_env):
        """开仓下单返回空 → order_filled=False"""
        hrs_env["api"].place_order = AsyncMock(return_value=None)
        result = asyncio.run(hrs_env["executor"].execute_short(
            symbol="BTCUSDT", entry_price=50000.0, atr=1000.0,
            quantity=Decimal("0.1"), score=0.9,
        ))
        assert result.order_filled is False
        assert result.protection_complete is False

    def test_execute_long_下单失败_返回order_filledFalse(self, hrs_env):
        hrs_env["api"].place_order = AsyncMock(return_value=None)
        result = asyncio.run(hrs_env["executor"].execute_long(
            symbol="BTCUSDT", entry_price=50000.0, atr=1000.0,
            quantity=Decimal("0.1"), score=0.9,
        ))
        assert result.order_filled is False

    def test_execute_short_订单无ID_信任订单结果(self, hrs_env):
        """订单无 orderId → 无法检查成交状态，信任订单结果继续"""
        hrs_env["api"].place_order = AsyncMock(return_value={
            "executedQty": "0.1", "origQty": "0.1",
        })
        result = asyncio.run(hrs_env["executor"].execute_short(
            symbol="BTCUSDT", entry_price=50000.0, atr=1000.0,
            quantity=Decimal("0.1"), score=0.9,
        ))
        assert result.order_filled is True

    def test_execute_short_使用lv_rm入场模式(self, hrs_env):
        """lv_rm 模式走独立止损止盈参数分支"""
        result = asyncio.run(hrs_env["executor"].execute_short(
            symbol="BTCUSDT", entry_price=50000.0, atr=1000.0,
            quantity=Decimal("0.1"), score=0.9, entry_mode="lv_rm",
        ))
        assert result.order_filled is True
        assert result.protection_complete is True

    def test_execute_short_设置杠杆失败_不影响开仓(self, hrs_env):
        """set_leverage 抛异常被内部捕获，不影响开仓"""
        hrs_env["api"].set_leverage = AsyncMock(side_effect=RuntimeError("设置失败"))
        result = asyncio.run(hrs_env["executor"].execute_short(
            symbol="BTCUSDT", entry_price=50000.0, atr=1000.0,
            quantity=Decimal("0.1"), score=0.9,
        ))
        assert result.order_filled is True

    def test_execute_short_止损单失败_收集sl角色(self, hrs_env):
        """SL 下单抛异常 → failed_roles 含 'sl'"""
        api = hrs_env["api"]

        async def fake_conditional(symbol, side, order_type, stop_price, price, quantity, **kwargs):
            if order_type in ("STOP", "STOP_MARKET"):
                raise RuntimeError("SL失败")
            return {"algoId": 10001}

        api.place_conditional_order = AsyncMock(side_effect=fake_conditional)
        result = asyncio.run(hrs_env["executor"].execute_short(
            symbol="BTCUSDT", entry_price=50000.0, atr=1000.0,
            quantity=Decimal("0.1"), score=0.9,
        ))
        assert result.order_filled is True
        assert result.protection_complete is False
        assert "sl" in result.failed_roles

    def test_format_quantity_step_size非正_原样返回(self, hrs_env):
        assert hrs_env["executor"]._format_quantity(Decimal("0.5"), Decimal("0")) == Decimal("0.5")

    def test_format_price_tick_size非正_原样返回(self, hrs_env):
        assert hrs_env["executor"]._format_price(Decimal("50000"), Decimal("0")) == Decimal("50000")


class TestAddToPositionExtra:
    """补充 add_to_position 边界分支"""

    @pytest.fixture
    def executor(self, mock_binance_api):
        pm = PositionManager(CONFIG, mock_binance_api)
        return TradingExecutor(CONFIG, mock_binance_api, MagicMock(), MagicMock(), pm)

    def test_加仓下单失败_返回None(self, executor):
        executor.binance_api.get_symbol_info = AsyncMock(
            return_value={"tickSize": "0.1", "stepSize": "0.001"}
        )
        executor.binance_api.place_order = AsyncMock(return_value=None)
        assert asyncio.run(executor.add_to_position(
            "BTCUSDT", "short", 50000.0, 1000.0, Decimal("0.5"), 0.8,
        )) is None


# ============================================================
# 测试19: _handle_entry_timeout() 开仓超时处理
# ============================================================

class TestHandleEntryTimeout:
    """测试开仓超时的取消与反向平仓分支"""

    @pytest.fixture
    def executor(self, mock_binance_api):
        pm = PositionManager(CONFIG, mock_binance_api)
        return TradingExecutor(CONFIG, mock_binance_api, MagicMock(), MagicMock(), pm)

    def test_部分成交_反向限价平仓(self, executor):
        """部分成交且价格有效 → 限价反向平仓（做空回补 BUY）"""
        executor.binance_api.cancel_order = AsyncMock(return_value={"status": "CANCELED"})
        executor.binance_api.get_ticker = AsyncMock(return_value={"lastPrice": "51000.0"})
        executor.binance_api.place_order = AsyncMock(return_value={"orderId": 400})

        asyncio.run(executor._handle_entry_timeout("BTCUSDT", 100, "short", 0.5, Decimal("0.001")))

        assert executor.binance_api.cancel_order.await_args.args == ("BTCUSDT", 100)
        call_kwargs = executor.binance_api.place_order.await_args.kwargs
        assert call_kwargs["side"] == "BUY"
        assert call_kwargs["order_type"] == "LIMIT"

    def test_无部分成交_不反向平仓(self, executor):
        """无部分成交 → 仅取消，不下反向平仓单"""
        executor.binance_api.cancel_order = AsyncMock(return_value={"status": "CANCELED"})
        asyncio.run(executor._handle_entry_timeout("BTCUSDT", 100, "short", 0.0, Decimal("0.001")))
        executor.binance_api.place_order.assert_not_called()

    def test_当前价格无效_市价反向平仓(self, executor):
        """价格无效 → 市价反向平仓"""
        executor.binance_api.cancel_order = AsyncMock(return_value={"status": "CANCELED"})
        executor.binance_api.get_ticker = AsyncMock(return_value={"lastPrice": "0.0"})
        executor.binance_api.place_order = AsyncMock(return_value={"orderId": 401})

        asyncio.run(executor._handle_entry_timeout("BTCUSDT", 100, "short", 0.5, Decimal("0.001")))

        assert executor.binance_api.place_order.await_args.kwargs["order_type"] == "MARKET"

    def test_取消订单异常_忽略不抛出(self, executor):
        """取消订单抛异常 → 仅 debug 日志，不抛出"""
        executor.binance_api.cancel_order = AsyncMock(side_effect=RuntimeError("取消失败"))
        asyncio.run(executor._handle_entry_timeout("BTCUSDT", 100, "short", 0.0, Decimal("0.001")))
        executor.binance_api.place_order.assert_not_called()


# ============================================================
# 测试20: 平仓做多/异常分支
# ============================================================

class TestClosePositionExtra:
    """补充 close_position 的做多与异常分支"""

    @pytest.fixture
    def executor(self, mock_binance_api):
        pm = PositionManager(CONFIG, mock_binance_api)
        return TradingExecutor(CONFIG, mock_binance_api, MagicMock(), MagicMock(), pm)

    def test_平仓成功_做多(self, executor):
        """做多平仓 → 卖出方向 SELL"""
        executor.binance_api.get_position = AsyncMock(return_value=[
            {"symbol": "BTCUSDT", "positionAmt": "1.0"},
        ])
        executor.binance_api.get_ticker = AsyncMock(return_value={"lastPrice": "51000.0"})
        executor.binance_api.get_symbol_info = AsyncMock(
            return_value={"tickSize": "0.1", "stepSize": "0.001"}
        )
        executor.binance_api.place_order = AsyncMock(return_value={"orderId": 202})

        order = asyncio.run(executor.close_position("BTCUSDT", "long", reason="测试平仓"))

        assert order == {"orderId": 202}
        assert executor.binance_api.place_order.await_args.kwargs["side"] == "SELL"

    def test_平仓异常_返回None(self, executor):
        """查询持仓抛异常 → 返回 None"""
        executor.binance_api.get_position = AsyncMock(side_effect=RuntimeError("查询失败"))
        assert asyncio.run(executor.close_position("BTCUSDT", "short")) is None


# ============================================================
# 测试21: 补单链路补充边界
# ============================================================

class TestReplenishChainBoundaryExtra:
    """补充补单链路的 SL 失败/TP 失败/取消失败仍重建/数量为零分支"""

    @pytest.fixture
    def executor(self, mock_binance_api):
        pm = PositionManager(CONFIG, mock_binance_api)
        return TradingExecutor(CONFIG, mock_binance_api, MagicMock(), MagicMock(), pm)

    def test_补单止损单失败_收集sl角色(self, executor):
        executor.binance_api.get_symbol_info = AsyncMock(
            return_value={"tickSize": "0.1", "stepSize": "0.001"}
        )

        async def fake_conditional(symbol, side, order_type, stop_price, price, quantity, **kwargs):
            if order_type in ("STOP", "STOP_MARKET"):
                raise RuntimeError("SL失败")
            return {"algoId": 99999}

        executor.binance_api.place_conditional_order = AsyncMock(side_effect=fake_conditional)

        result = asyncio.run(executor.replenish_position_orders(
            symbol="BTCUSDT", direction="short", entry_price=50000.0,
            entry_quantity=1.0, atr=1000.0,
        ))

        assert result.success is False
        assert "sl" in result.failed_roles

    def test_补单止盈单失败_收集tp角色(self, executor):
        # 当前价格设为入场价附近（未跌破 TP2=46500），确保 TP1/TP2 都被实际尝试下单
        executor.binance_api.get_ticker = AsyncMock(return_value={"lastPrice": "50000.0"})
        executor.binance_api.get_symbol_info = AsyncMock(
            return_value={"tickSize": "0.1", "stepSize": "0.001"}
        )

        async def fake_conditional(symbol, side, order_type, stop_price, price, quantity, **kwargs):
            if order_type == "TAKE_PROFIT":
                raise RuntimeError("TP失败")
            return {"algoId": 99999}

        executor.binance_api.place_conditional_order = AsyncMock(side_effect=fake_conditional)

        result = asyncio.run(executor.replenish_position_orders(
            symbol="BTCUSDT", direction="short", entry_price=50000.0,
            entry_quantity=1.0, atr=1000.0,
        ))

        assert result.success is False
        assert set(result.failed_roles) == {"tp1", "tp2"}

    def test_补单数量为零_全部跳过(self, executor):
        """entry_quantity=0 → SL/TP1/TP2 均视为跳过，成功且不重建"""
        executor.binance_api.get_symbol_info = AsyncMock(
            return_value={"tickSize": "0.1", "stepSize": "0.001"}
        )

        result = asyncio.run(executor.replenish_position_orders(
            symbol="BTCUSDT", direction="short", entry_price=50000.0,
            entry_quantity=0.0, atr=1000.0,
        ))

        assert result.success is True
        assert result.failed_roles == []
        assert result.placed == 0
        executor.binance_api.place_conditional_order.assert_not_called()

    def test_补单前取消旧单部分失败_仍继续重建(self, executor):
        """取消旧单部分失败仅告警，仍全量重建（FR-09）"""
        pm = executor.position_manager
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)
        pm.add_algo_id("BTCUSDT", "tp1", 10002)

        executor.binance_api.use_unified_account = False
        executor.binance_api.cancel_algo_order = AsyncMock(side_effect=RuntimeError("取消失败"))
        # 当前价格设为入场价附近（未跌破 TP2=46500），确保 SL/TP1/TP2 全量重建 3 单
        executor.binance_api.get_ticker = AsyncMock(return_value={"lastPrice": "50000.0"})
        executor.binance_api.get_symbol_info = AsyncMock(
            return_value={"tickSize": "0.1", "stepSize": "0.001"}
        )
        executor.binance_api.place_conditional_order = AsyncMock(return_value={"algoId": 99999})

        result = asyncio.run(executor.replenish_position_orders(
            symbol="BTCUSDT", direction="short", entry_price=50000.0,
            entry_quantity=1.0, atr=1000.0,
        ))

        assert result.success is True
        assert result.placed == 3  # 即使取消失败仍全量重建


# ============================================================
# 测试22: E-1 取消失败保留 algo_ids（FR-07 防 Defect C 复发）
# ============================================================

class TestCancelFailureRetainsAlgoIds:
    """逐个取消部分失败 → 保留本地 algo_ids，下一轮补单重试仍可取消残留旧单"""

    @pytest.fixture
    def pm(self, mock_binance_api):
        return PositionManager(CONFIG, mock_binance_api)

    def _add_position_with_algo_ids(self, pm):
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)
        pm.add_algo_id("BTCUSDT", "tp1", 10002)

    def test_部分取消失败_保留本地algo_ids(self, pm):
        """2 个条件单中 1 个取消失败 → 本地 algo_ids 不被清空"""
        self._add_position_with_algo_ids(pm)
        pm.binance_api.use_unified_account = False

        async def fake_cancel(symbol, algo_id):
            if algo_id == 10001:
                raise RuntimeError("取消失败")
            return {"status": "CANCELED"}

        pm.binance_api.cancel_algo_order = AsyncMock(side_effect=fake_cancel)

        result = asyncio.run(pm.cancel_all_orders("BTCUSDT"))

        assert result["failed"] == 1
        # 部分失败 → 保留本地记录，防止补单重试时无法取消残留旧单（Defect C 防护）
        assert len(pm.get_algo_ids("BTCUSDT")) == 2

    def test_全部取消失败_保留本地algo_ids(self, pm):
        """全部取消失败 → 本地 algo_ids 不被清空"""
        self._add_position_with_algo_ids(pm)
        pm.binance_api.use_unified_account = False
        pm.binance_api.cancel_algo_order = AsyncMock(
            side_effect=RuntimeError("取消失败")
        )

        result = asyncio.run(pm.cancel_all_orders("BTCUSDT"))

        assert result["failed"] == 2
        assert len(pm.get_algo_ids("BTCUSDT")) == 2

    def test_全部取消成功_清空本地algo_ids(self, pm):
        """全部取消成功 → 清空本地 algo_ids，避免陈旧记录误导补单"""
        self._add_position_with_algo_ids(pm)
        pm.binance_api.use_unified_account = False
        pm.binance_api.cancel_algo_order = AsyncMock(return_value={"status": "CANCELED"})

        result = asyncio.run(pm.cancel_all_orders("BTCUSDT"))

        assert result["failed"] == 0
        assert pm.get_algo_ids("BTCUSDT") == []


# ============================================================
# 测试23: E-4 algo_ids 持久化 roundtrip（FR-06）
# ============================================================

class TestAlgoIdsPersistence:
    """_save_state / _restore_state 的 algo_ids JSONB 持久化往返"""

    def _make_strategy(self, mock_db):
        from strategies.hrs.strategy import HRSStrategy

        s = HRSStrategy(CONFIG)
        s.db = mock_db
        s.position_manager = PositionManager(CONFIG, MagicMock())
        s.risk_manager = MagicMock()
        s.risk_manager.to_dict = MagicMock(return_value={})
        s.risk_manager.blacklist = set()
        s._registered_symbols = set()
        s._add_position_count = {}
        s._paused = False
        s._klines_cache = {}
        s._klines_4h_cache = {}
        return s

    def test_save_state_持久化algo_ids_jsonb(self, mock_db):
        """_save_state 将 algo_ids 以 JSONB 形式写入 hrs_positions"""
        s = self._make_strategy(mock_db)
        s.position_manager.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        s.position_manager.add_algo_id("BTCUSDT", "sl", 10001)
        s.position_manager.add_algo_id("BTCUSDT", "tp1", 10002)

        asyncio.run(s._save_state())

        # 断言存在一次 INSERT/UPDATE hrs_positions 且携带 algo_ids JSON 参数
        algo_json = '{"sl": 10001, "tp1": 10002}'
        assert any(
            "hrs.hrs_positions" in call.args[0]
            and any(isinstance(a, str) and algo_json == a for a in call.args)
            for call in mock_db.execute.call_args_list
        )

    def test_restore_state_恢复algo_ids映射(self, mock_db):
        """_restore_state 从 hrs_positions 恢复 algo_ids 到 position_manager"""
        s = self._make_strategy(mock_db)
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)

        async def fake_fetch_all(query, *args):
            if "hrs_positions" in query:
                return [{
                    "symbol": "BTCUSDT", "direction": "short",
                    "entry_price": 50000.0, "quantity": 1.0, "entry_time": ts,
                    "algo_ids": {"sl": 10001, "tp1": 10002},
                }]
            if "hrs_blacklist" in query or "hrs_active_symbols" in query or "hrs_meta" in query:
                return []
            return []

        mock_db.fetch_all = AsyncMock(side_effect=fake_fetch_all)
        s._restore_klines_from_tables = AsyncMock(return_value=None)
        s._reconcile_positions = AsyncMock(return_value=None)

        asyncio.run(s._restore_state())

        pos = s.position_manager.get_position("BTCUSDT")
        assert pos is not None
        assert pos["algo_ids"] == {"sl": 10001, "tp1": 10002}

    def test_restore_state_兼容字符串存储的algo_ids(self, mock_db):
        """存量库 algo_ids 以 str 存储 → 兼容解析为 dict"""
        s = self._make_strategy(mock_db)
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)

        async def fake_fetch_all(query, *args):
            if "hrs_positions" in query:
                return [{
                    "symbol": "BTCUSDT", "direction": "short",
                    "entry_price": 50000.0, "quantity": 1.0, "entry_time": ts,
                    "algo_ids": '{"sl": 10001}',
                }]
            if "hrs_blacklist" in query or "hrs_active_symbols" in query or "hrs_meta" in query:
                return []
            return []

        mock_db.fetch_all = AsyncMock(side_effect=fake_fetch_all)
        s._restore_klines_from_tables = AsyncMock(return_value=None)
        s._reconcile_positions = AsyncMock(return_value=None)

        asyncio.run(s._restore_state())

        pos = s.position_manager.get_position("BTCUSDT")
        assert pos is not None
        assert pos["algo_ids"] == {"sl": 10001}


# ============================================================
# 测试24: E-4 数据库迁移幂等（FR-06）
# ============================================================

class TestAlgoIdsMigrationIdempotent:
    """_ensure_db_schema 使用 ADD COLUMN IF NOT EXISTS，重复执行不报错"""

    def test_迁移SQL使用幂等ADD_COLUMN_IF_NOT_EXISTS(self, mock_db):
        from strategies.hrs.strategy import HRSStrategy

        s = HRSStrategy(CONFIG)
        s.db = mock_db

        asyncio.run(s._ensure_db_schema())

        ddl_statements = [call.args[0] for call in mock_db.execute_ddl.call_args_list]
        assert any(
            "ALTER TABLE hrs.hrs_positions ADD COLUMN IF NOT EXISTS algo_ids JSONB" in sql
            for sql in ddl_statements
        )

    def test_建表SQL包含algo_ids列(self, mock_db):
        """hrs_positions 建表语句含 algo_ids JSONB 列定义"""
        from strategies.hrs.strategy import HRSStrategy

        s = HRSStrategy(CONFIG)
        s.db = mock_db

        asyncio.run(s._ensure_db_schema())

        ddl_statements = [call.args[0] for call in mock_db.execute_ddl.call_args_list]
        create_stmt = next(
            (sql for sql in ddl_statements if "CREATE TABLE IF NOT EXISTS hrs.hrs_positions" in sql),
            "",
        )
        assert "algo_ids JSONB" in create_stmt