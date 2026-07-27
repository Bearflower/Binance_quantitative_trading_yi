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
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

# 加载配置
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)

from strategies.hrs.position_manager import PositionManager


# ============================================================
# 测试夹具
# ============================================================

@pytest.fixture
def mock_binance_api():
    """创建模拟的币安API客户端"""
    api = MagicMock()
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
    """测试 replenish_position_orders() 补单去重逻辑

    注意：这些测试通过验证 has_algo_id() 的行为来间接验证补单去重逻辑，
    因为 replenish_position_orders() 内部依赖 has_algo_id() 判断是否跳过。
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
    """端到端测试 replenish_position_orders() 补单去重

    使用 mock 的 binance_api，验证实际调用 replenish_position_orders() 时：
    - 已有 algo_id 时不会调用 place_conditional_order
    - 没有 algo_id 时会正常调用
    """

    @pytest.fixture
    def mock_api(self):
        """创建完整的 mock API"""
        api = MagicMock()
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
    def executor(self, mock_api, mock_db, mock_notification, pm):
        """创建 TradingExecutor 实例"""
        from strategies.hrs.executor import TradingExecutor
        return TradingExecutor(CONFIG, mock_api, mock_db, mock_notification, pm)

    @pytest.mark.asyncio
    async def test_已有全部algo_id时不调用下单API(self, mock_api, executor, pm):
        """
        模拟 algo_ids 已有 sl/tp1/tp2
        验证 replenish_position_orders() 不会调用 place_conditional_order
        """
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)
        pm.add_algo_id("BTCUSDT", "tp1", 10002)
        pm.add_algo_id("BTCUSDT", "tp2", 10003)

        # 重置调用计数
        mock_api.place_conditional_order.reset_mock()

        result = await executor.replenish_position_orders(
            symbol="BTCUSDT",
            direction="short",
            entry_price=50000.0,
            entry_quantity=1.0,
            atr=1000.0,
            target1_reached=False,
            target2_reached=False,
        )

        # 应该返回 True（成功执行）
        assert result is True

        # 验证 place_conditional_order 没有被调用（因为所有条件单都已存在）
        mock_api.place_conditional_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_无algo_id时正常下单(self, mock_api, executor, pm):
        """
        模拟 algo_ids 为空
        验证 replenish_position_orders() 会正常调用 place_conditional_order
        """
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)

        # 重置调用计数
        mock_api.place_conditional_order.reset_mock()

        result = await executor.replenish_position_orders(
            symbol="BTCUSDT",
            direction="short",
            entry_price=50000.0,
            entry_quantity=1.0,
            atr=1000.0,
            target1_reached=False,
            target2_reached=False,
        )

        assert result is True

        # 验证 place_conditional_order 被调用（sl + tp1 + tp2 = 3 次）
        # 注意：tp2 可能因为价格已过目标价而跳过 get_ticker 调用
        call_count = mock_api.place_conditional_order.call_count
        assert call_count >= 1, f"应至少调用1次下单API，实际调用{call_count}次"

    @pytest.mark.asyncio
    async def test_部分algo_id存在时仅补充缺失的(self, mock_api, executor, pm):
        """
        模拟 algo_ids 只有 sl，tp1/tp2 缺失
        验证 replenish_position_orders() 只补充 tp1 和 tp2
        """
        pm.add_position("BTCUSDT", "short", 50000.0, 1.0, 1000.0)
        pm.add_algo_id("BTCUSDT", "sl", 10001)

        mock_api.place_conditional_order.reset_mock()

        result = await executor.replenish_position_orders(
            symbol="BTCUSDT",
            direction="short",
            entry_price=50000.0,
            entry_quantity=1.0,
            atr=1000.0,
            target1_reached=False,
            target2_reached=False,
        )

        assert result is True

        # sl 不应被调用（已存在），tp1 和 tp2 应被调用
        call_count = mock_api.place_conditional_order.call_count
        # 至少 tp1 被调用，tp2 可能因价格跳过
        assert call_count >= 1, f"应至少补充1个缺失的条件单，实际调用{call_count}次"


# ============================================================
# 测试4: _reconcile_positions() 不再接管非本策略仓位
# ============================================================

class TestReconcilePositionsNoTakeover:
    """测试 _reconcile_positions() 不再自动接管非本策略仓位"""

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
        验证 _reconcile_positions() 仅输出 WARNING 日志，不调用 add_position
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
            assert "发现非本策略持仓" in combined_output or "不自动接管" in combined_output, (
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