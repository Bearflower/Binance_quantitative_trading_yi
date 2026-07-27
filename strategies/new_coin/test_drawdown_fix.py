"""
回撤熔断bug修复单元测试

测试目标：验证 strategies/new_coin/strategy.py 中回撤熔断修复代码的正确性

覆盖场景：
1. _check_max_drawdown 方法（平仓后调用，累加 cumulative_pnl 并检查熔断）
2. _refresh_drawdown_status 方法（周期刷新，不修改 cumulative_pnl）
3. _get_position_pnl 方法（查不到平仓记录返回 None）
4. 平仓后处理逻辑（pnl 为 None 时跳过回撤检查与连续亏损计数）
5. 状态迁移（旧版 state 重置 peak_pnl，新版正常加载）
6. 跨策略隔离（基于 cumulative_pnl 序列计算，与账户 balance 解耦）

配置参数（来自 config.yaml）：
- trading.max_drawdown.threshold: 0.15 (15%)
- trading.max_drawdown.pause_days: 7
"""
from unittest.mock import AsyncMock, MagicMock
from decimal import Decimal
from datetime import datetime, timedelta, timezone

import pytest

from strategies.new_coin.strategy import NewCoinStrategy


# ---------------------------------------------------------------------------
# 公共 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def strategy_config():
    """构造策略配置（与 config.yaml 中 max_drawdown 参数一致）"""
    return {
        "strategy": {"name": "new_coin"},
        "detector": {"check_interval": 300},
        "trading": {
            "max_drawdown": {
                "threshold": 0.15,   # 15% 回撤阈值
                "pause_days": 7,     # 熔断暂停 7 天
            }
        },
    }


@pytest.fixture
def strategy(strategy_config):
    """构造 NewCoinStrategy 实例，并 mock 所有外部依赖

    被 mock 的依赖：
    - notification_client: 飞书通知客户端（AsyncMock）
    - db: 数据库管理器（AsyncMock）
    - binance_client: 币安客户端（AsyncMock）
    - kline_service: K线服务（AsyncMock）
    - trading_executor: 交易执行器（MagicMock + AsyncMock 方法）
    """
    s = NewCoinStrategy(strategy_config)

    # mock 外部客户端
    s.notification_client = AsyncMock()
    s.db = AsyncMock()
    s.binance_client = AsyncMock()
    s.kline_service = AsyncMock()

    # mock 交易执行器
    s.trading_executor = MagicMock()
    s.trading_executor.position_tracking = {}
    s.trading_executor.check_position_management = AsyncMock(return_value=None)
    s.trading_executor.cancel_all_algo_orders = AsyncMock(
        return_value={"failed": 0, "cancelled": 0}
    )

    return s


# ---------------------------------------------------------------------------
# 测试套件
# ---------------------------------------------------------------------------


class TestDrawdownFix:
    """回撤熔断修复测试套件"""

    # ==================== 1. _check_max_drawdown 方法测试 ====================

    async def test_check_max_drawdown_zero_pnl_no_trigger(self, strategy):
        """AC-1: cumulative_pnl=0 时，回撤率必为 0，不触发熔断

        逻辑：传入 pnl=0，cumulative_pnl 仍为 0；
        peak_pnl 被更新为 0，但 `peak_pnl > 0` 为 False，不计算回撤率。
        """
        await strategy._check_max_drawdown(0.0)

        assert strategy.cumulative_pnl == Decimal("0")
        assert strategy.drawdown_pause_until is None
        # 不应发送熔断通知
        strategy.notification_client.send.assert_not_called()

    async def test_check_max_drawdown_single_profit_no_duplicate(self, strategy):
        """AC-3: 单笔盈利 10 USDT 后，cumulative_pnl 增量=10（不重复计算）

        修复前的 bug：基于 balance+cumulative_pnl 计算会导致重复计算。
        修复后：仅累加 pnl 到 cumulative_pnl 一次。
        """
        initial = strategy.cumulative_pnl
        await strategy._check_max_drawdown(10.0)

        # 增量正好为 10，不重复计算
        assert strategy.cumulative_pnl == initial + Decimal("10")
        assert strategy.peak_pnl == Decimal("10")
        assert strategy.drawdown_pause_until is None

    async def test_check_max_drawdown_peak_pnl_none_init(self, strategy):
        """边界: peak_pnl 为 None 时正确初始化为 cumulative_pnl

        初始 peak_pnl 为 None，首次调用后应被初始化为 cumulative_pnl。
        """
        assert strategy.peak_pnl is None

        await strategy._check_max_drawdown(5.0)

        # peak_pnl 应被初始化为 cumulative_pnl（=5）
        assert strategy.peak_pnl == Decimal("5")

    async def test_check_max_drawdown_peak_updates_on_profit(self, strategy):
        """边界: 连续盈利后 peak_pnl 更新到最高点

        peak_pnl 应跟随 cumulative_pnl 的历史最高值。
        """
        # 第一笔盈利 10
        await strategy._check_max_drawdown(10.0)
        assert strategy.peak_pnl == Decimal("10")

        # 第二笔盈利 20，cumulative_pnl=30
        await strategy._check_max_drawdown(20.0)
        assert strategy.cumulative_pnl == Decimal("30")
        assert strategy.peak_pnl == Decimal("30")

        # 第三笔盈利 5，cumulative_pnl=35
        await strategy._check_max_drawdown(5.0)
        assert strategy.cumulative_pnl == Decimal("35")
        assert strategy.peak_pnl == Decimal("35")

    async def test_check_max_drawdown_loss_after_profit_triggers(self, strategy):
        """边界: 盈利后亏损触发熔断（drawdown >= 15%）

        场景：先盈利 100，peak_pnl=100；
        再亏损 20，cumulative_pnl=80，drawdown=(100-80)/100=20% >= 15%，触发熔断。
        """
        # 先盈利 100
        await strategy._check_max_drawdown(100.0)
        assert strategy.peak_pnl == Decimal("100")
        assert strategy.drawdown_pause_until is None

        # 亏损 20，回撤 20% >= 15%
        await strategy._check_max_drawdown(-20.0)

        assert strategy.cumulative_pnl == Decimal("80")
        assert strategy.drawdown_pause_until is not None
        # 应发送一次熔断通知
        strategy.notification_client.send.assert_called_once()

    async def test_check_max_drawdown_small_loss_no_trigger(self, strategy):
        """补充: 回撤未达阈值（<15%）不触发熔断

        场景：先盈利 100，再亏损 10，drawdown=10% < 15%，不触发。
        """
        await strategy._check_max_drawdown(100.0)
        # 亏损 10，drawdown = 10% < 15%
        await strategy._check_max_drawdown(-10.0)

        assert strategy.cumulative_pnl == Decimal("90")
        assert strategy.peak_pnl == Decimal("100")
        assert strategy.drawdown_pause_until is None
        strategy.notification_client.send.assert_not_called()

    async def test_check_max_drawdown_threshold_boundary(self, strategy):
        """补充: 回撤率恰好等于阈值（15%）触发熔断（>= 判断）"""
        await strategy._check_max_drawdown(100.0)
        # 亏损 15，drawdown = 15% == threshold，应触发（>=）
        await strategy._check_max_drawdown(-15.0)

        assert strategy.cumulative_pnl == Decimal("85")
        assert strategy.drawdown_pause_until is not None

    # ==================== 2. _refresh_drawdown_status 方法测试 ====================

    async def test_refresh_drawdown_status_updates_peak(self, strategy):
        """AC-4: 连续多个周期未平仓，cumulative_pnl 上涨时 peak_pnl 更新

        场景：cumulative_pnl 由外部途径上涨（如状态恢复后手动修正），
        _refresh_drawdown_status 应更新 peak_pnl 到新高点。
        """
        # 模拟已有 cumulative_pnl
        strategy.cumulative_pnl = Decimal("50")
        strategy.peak_pnl = Decimal("50")

        # cumulative_pnl 上涨到 80（模拟外部修正）
        strategy.cumulative_pnl = Decimal("80")
        await strategy._refresh_drawdown_status()

        # peak_pnl 应更新到 80
        assert strategy.peak_pnl == Decimal("80")
        assert strategy.drawdown_pause_until is None

    async def test_refresh_drawdown_status_no_modify_cumulative(self, strategy):
        """验证: _refresh_drawdown_status 不修改 cumulative_pnl（仅检查不累加）

        多次调用后 cumulative_pnl 应保持不变。
        """
        strategy.cumulative_pnl = Decimal("100")
        strategy.peak_pnl = Decimal("100")

        await strategy._refresh_drawdown_status()
        await strategy._refresh_drawdown_status()
        await strategy._refresh_drawdown_status()

        # 多次调用后 cumulative_pnl 不变
        assert strategy.cumulative_pnl == Decimal("100")

    async def test_refresh_drawdown_status_no_repeat_trigger(self, strategy):
        """验证: 已处于熔断状态时不重复触发

        场景：已设置 drawdown_pause_until，即使回撤超过阈值也不重复触发。
        """
        # 先设置熔断状态
        original_pause = datetime.now(timezone.utc) + timedelta(days=7)
        strategy.cumulative_pnl = Decimal("100")
        strategy.peak_pnl = Decimal("100")
        strategy.drawdown_pause_until = original_pause

        # 模拟回撤扩大到 50%
        strategy.cumulative_pnl = Decimal("50")

        await strategy._refresh_drawdown_status()

        # 不应再次发送通知
        strategy.notification_client.send.assert_not_called()
        # drawdown_pause_until 保持原值（不重复设置）
        assert strategy.drawdown_pause_until == original_pause

    async def test_refresh_drawdown_status_triggers_when_drawdown_exceeds(self, strategy):
        """补充: _refresh_drawdown_status 在回撤超阈值时触发熔断

        场景：cumulative_pnl 已下降但 peak_pnl 未更新，刷新时应触发。
        """
        # 模拟之前累计盈利 100，peak_pnl=100
        strategy.cumulative_pnl = Decimal("100")
        strategy.peak_pnl = Decimal("100")

        # 模拟后续亏损导致 cumulative_pnl 下降到 80（回撤 20%）
        # 注意：这里直接设置 cumulative_pnl，模拟"平仓间隔内的回撤状态变化"
        strategy.cumulative_pnl = Decimal("80")

        await strategy._refresh_drawdown_status()

        # 应触发熔断
        assert strategy.drawdown_pause_until is not None
        strategy.notification_client.send.assert_called_once()

    # ==================== 3. _get_position_pnl 方法测试 ====================

    async def test_get_position_pnl_no_record_returns_none(self, strategy):
        """AC-5: 查不到平仓记录时返回 None（不是 0.0）

        修复前的 bug：返回 0.0 会导致 cumulative_pnl 被错误累加。
        修复后：返回 None，调用方据此跳过回撤检查。
        """
        strategy.db.fetch_one = AsyncMock(return_value=None)

        result = await strategy._get_position_pnl("BTCUSDT", 100.0)

        assert result is None  # 必须是 None，不能是 0.0

    async def test_get_position_pnl_normal_returns_pnl(self, strategy):
        """验证: 正常情况返回盈亏金额

        做空盈亏 = (入场价 - 出场价) * 数量
        入场价 100，出场价 80，数量 2 → (100-80)*2 = 40
        """
        strategy.db.fetch_one = AsyncMock(
            return_value={"quantity": 2, "price": 80, "side": "BUY"}
        )

        result = await strategy._get_position_pnl("BTCUSDT", 100.0)

        assert result == 40.0

    async def test_get_position_pnl_loss_returns_negative(self, strategy):
        """补充: 亏损情况返回负数盈亏

        入场价 100，出场价 110，数量 2 → (100-110)*2 = -20
        """
        strategy.db.fetch_one = AsyncMock(
            return_value={"quantity": 2, "price": 110, "side": "BUY"}
        )

        result = await strategy._get_position_pnl("BTCUSDT", 100.0)

        assert result == -20.0

    async def test_get_position_pnl_exception_returns_none(self, strategy):
        """验证: 异常情况返回 None

        数据库异常时不应抛出，而应返回 None 由调用方处理。
        """
        strategy.db.fetch_one = AsyncMock(side_effect=Exception("DB connection error"))

        result = await strategy._get_position_pnl("BTCUSDT", 100.0)

        assert result is None

    # ==================== 4. 平仓后处理逻辑测试 ====================

    async def test_post_close_skip_when_pnl_none(self, strategy):
        """AC-5: pnl 为 None 时跳过回撤检查与连续亏损计数

        场景：_monitor_positions 检测到持仓已平仓，但 _get_position_pnl 返回 None
        （查不到平仓记录）。此时应跳过 _check_max_drawdown 和 consecutive_losses 更新。
        """
        # 准备一个持仓
        strategy.positions = {
            "BTCUSDT": {
                "entry_price": 100.0,
                "entry_time": datetime.now(timezone.utc),
            }
        }
        strategy.consecutive_losses = 0
        strategy.cumulative_pnl = Decimal("0")

        # mock: 币安返回无持仓（已平仓）
        strategy.binance_client.get_position = AsyncMock(return_value=[])
        # mock: 查不到平仓记录
        strategy._get_position_pnl = AsyncMock(return_value=None)
        strategy._save_state = AsyncMock()
        # 监视 _check_max_drawdown 是否被调用
        strategy._check_max_drawdown = AsyncMock()

        await strategy._monitor_positions()

        # pnl 为 None 时不应调用 _check_max_drawdown
        strategy._check_max_drawdown.assert_not_called()
        # consecutive_losses 不变
        assert strategy.consecutive_losses == 0
        # cumulative_pnl 不变
        assert strategy.cumulative_pnl == Decimal("0")
        # 持仓应被删除
        assert "BTCUSDT" not in strategy.positions

    async def test_post_close_consecutive_losses_not_increment_when_pnl_none(
        self, strategy
    ):
        """验证: pnl 为 None 时 consecutive_losses 不增加

        场景：已有 2 次连续亏损，pnl 为 None 时应保持 2 不变。
        """
        strategy.positions = {
            "BTCUSDT": {
                "entry_price": 100.0,
                "entry_time": datetime.now(timezone.utc),
            }
        }
        strategy.consecutive_losses = 2  # 已有 2 次连续亏损

        strategy.binance_client.get_position = AsyncMock(return_value=[])
        strategy._get_position_pnl = AsyncMock(return_value=None)
        strategy._save_state = AsyncMock()

        await strategy._monitor_positions()

        # consecutive_losses 保持 2
        assert strategy.consecutive_losses == 2

    async def test_post_close_consecutive_losses_increment_when_pnl_negative(
        self, strategy
    ):
        """验证: pnl 为负数时 consecutive_losses 增加

        场景：pnl=-5（亏损），consecutive_losses 应 +1，
        且 cumulative_pnl 应通过 _check_max_drawdown 更新。
        """
        strategy.positions = {
            "BTCUSDT": {
                "entry_price": 100.0,
                "entry_time": datetime.now(timezone.utc),
            }
        }
        strategy.consecutive_losses = 1

        strategy.binance_client.get_position = AsyncMock(return_value=[])
        strategy._get_position_pnl = AsyncMock(return_value=-5.0)
        strategy._save_state = AsyncMock()
        # mock 掉与回撤熔断无关的副作用方法
        strategy._check_consecutive_loss_pause = AsyncMock()
        strategy._add_to_stop_loss_monitor = AsyncMock()

        await strategy._monitor_positions()

        # consecutive_losses 应增加
        assert strategy.consecutive_losses == 2
        # cumulative_pnl 应被更新（通过 _check_max_drawdown 真实执行）
        assert strategy.cumulative_pnl == Decimal("-5")
        # 持仓应被删除
        assert "BTCUSDT" not in strategy.positions

    async def test_post_close_consecutive_losses_reset_when_pnl_positive(
        self, strategy
    ):
        """补充: pnl 为正数（盈利）时 consecutive_losses 重置为 0"""
        strategy.positions = {
            "BTCUSDT": {
                "entry_price": 100.0,
                "entry_time": datetime.now(timezone.utc),
            }
        }
        strategy.consecutive_losses = 3  # 已有 3 次连续亏损

        strategy.binance_client.get_position = AsyncMock(return_value=[])
        strategy._get_position_pnl = AsyncMock(return_value=5.0)  # 盈利
        strategy._save_state = AsyncMock()

        await strategy._monitor_positions()

        # 盈利则重置连续亏损计数
        assert strategy.consecutive_losses == 0
        # cumulative_pnl 应被更新
        assert strategy.cumulative_pnl == Decimal("5")

    # ==================== 5. 状态迁移测试 ====================

    async def test_restore_state_old_version_resets_peak(self, strategy):
        """AC-7: 旧版 state（无 state_version 字段）加载时 peak_pnl 重置为 None

        修复前的 bug：旧版 peak_equity 基于错误公式（balance+cumulative_pnl），
        直接加载会导致回撤计算错误。
        修复后：检测到 state_version < 2 时强制重置 peak_pnl=None。
        """
        old_state = {
            "positions": {},
            "traded_symbols": [],
            "consecutive_losses": 0,
            "cumulative_pnl": "100",
            "peak_equity": "200",  # 旧字段名（基于错误公式）
            # 无 state_version 字段 → 默认为 1
        }
        strategy.db.fetch_one = AsyncMock(return_value=old_state)

        await strategy._restore_state()

        # 旧版 state 应重置 peak_pnl
        assert strategy.peak_pnl is None
        # cumulative_pnl 正常加载
        assert strategy.cumulative_pnl == Decimal("100")

    async def test_restore_state_new_version_loads_peak(self, strategy):
        """验证: 新版 state（state_version=2）正常加载 peak_pnl"""
        new_state = {
            "positions": {},
            "traded_symbols": [],
            "consecutive_losses": 0,
            "cumulative_pnl": "100",
            "peak_pnl": "150",
            "state_version": 2,
        }
        strategy.db.fetch_one = AsyncMock(return_value=new_state)

        await strategy._restore_state()

        assert strategy.peak_pnl == Decimal("150")
        assert strategy.cumulative_pnl == Decimal("100")

    async def test_restore_state_old_field_name_compatible(self, strategy):
        """验证: 旧版 peak_equity 字段名兼容读取（但仍重置为 None）

        兼容性：能读取旧字段名 peak_equity（避免 KeyError），
        但因 state_version < 2，仍重置 peak_pnl=None。
        """
        old_state = {
            "positions": {},
            "traded_symbols": [],
            "consecutive_losses": 0,
            "cumulative_pnl": "50",
            "peak_equity": "300",  # 旧字段名
            "state_version": 1,  # 显式旧版本
        }
        strategy.db.fetch_one = AsyncMock(return_value=old_state)

        # 不应抛出异常（兼容旧字段名）
        await strategy._restore_state()

        # 旧版本无论字段名如何，peak_pnl 都应重置
        assert strategy.peak_pnl is None
        assert strategy.cumulative_pnl == Decimal("50")

    async def test_restore_state_new_version_none_peak(self, strategy):
        """补充: 新版 state 但 peak_pnl 为 None 时正常处理"""
        new_state = {
            "positions": {},
            "traded_symbols": [],
            "consecutive_losses": 0,
            "cumulative_pnl": "0",
            "peak_pnl": None,
            "state_version": 2,
        }
        strategy.db.fetch_one = AsyncMock(return_value=new_state)

        await strategy._restore_state()

        assert strategy.peak_pnl is None
        assert strategy.cumulative_pnl == Decimal("0")

    async def test_restore_state_drawdown_pause_loaded(self, strategy):
        """补充: 熔断暂停时间 drawdown_pause_until 正常恢复"""
        pause_time = datetime.now(timezone.utc) + timedelta(days=3)
        new_state = {
            "positions": {},
            "traded_symbols": [],
            "consecutive_losses": 0,
            "cumulative_pnl": "80",
            "peak_pnl": "100",
            "state_version": 2,
            "drawdown_pause_until": pause_time.isoformat(),
        }
        strategy.db.fetch_one = AsyncMock(return_value=new_state)

        await strategy._restore_state()

        assert strategy.drawdown_pause_until is not None
        # 验证时间近似相等（fromisoformat 可能丢失时区信息，但值应一致）
        assert strategy.drawdown_pause_until.year == pause_time.year
        assert strategy.drawdown_pause_until.month == pause_time.month
        assert strategy.drawdown_pause_until.day == pause_time.day

    # ==================== 6. 跨策略隔离测试（核心） ====================

    async def test_cross_strategy_isolation_balance_change_no_impact(self, strategy):
        """AC-2: 模拟 balance 变化（其他策略亏损），本策略回撤率不变

        核心修复点：回撤计算基于 cumulative_pnl 序列，与账户 balance 完全解耦。
        其他策略的亏损导致 balance 下降，不应影响本策略的回撤率计算。
        """
        # 本策略累计盈利 100
        await strategy._check_max_drawdown(100.0)
        assert strategy.peak_pnl == Decimal("100")
        assert strategy.cumulative_pnl == Decimal("100")

        # 模拟其他策略导致账户 balance 大幅变化（本策略不感知）
        # 由于 _check_max_drawdown 不读取 balance，本策略的回撤计算不受影响
        # 再次平仓，本策略盈利 10
        await strategy._check_max_drawdown(10.0)

        # 本策略回撤率仍为 0（cumulative_pnl 持续上涨）
        assert strategy.cumulative_pnl == Decimal("110")
        assert strategy.peak_pnl == Decimal("110")
        assert strategy.drawdown_pause_until is None
        strategy.notification_client.send.assert_not_called()

    async def test_check_max_drawdown_not_read_balance(self, strategy):
        """验证: _check_max_drawdown 不读取 balance 相关属性

        通过设置干扰性的 balance 属性，确认计算结果仅基于 cumulative_pnl。
        """
        # 故意设置干扰性的 balance 相关属性
        strategy.balance = Decimal("99999")
        strategy.account_equity = Decimal("99999")
        strategy.initial_balance = Decimal("99999")
        strategy.peak_equity = Decimal("99999")

        await strategy._check_max_drawdown(50.0)

        # 计算结果应仅基于 cumulative_pnl，不受 balance 影响
        assert strategy.cumulative_pnl == Decimal("50")
        assert strategy.peak_pnl == Decimal("50")
        assert strategy.drawdown_pause_until is None

    async def test_cross_strategy_no_false_trigger_on_others_loss(self, strategy):
        """AC-2 核心: 其他策略亏损导致账户回撤，本策略不误触发熔断

        场景：本策略盈利 100 后，其他策略大幅亏损（账户 balance 下降 50%）。
        通过 _refresh_drawdown_status 检查时，本策略不应触发熔断，
        因为 cumulative_pnl 未变，peak_pnl 未变，回撤率为 0。
        """
        # 本策略先盈利 100
        await strategy._check_max_drawdown(100.0)
        assert strategy.peak_pnl == Decimal("100")

        # 模拟其他策略亏损，账户 balance 下降 50%
        # 但本策略 cumulative_pnl 不变，_refresh_drawdown_status 不应触发熔断
        await strategy._refresh_drawdown_status()

        assert strategy.cumulative_pnl == Decimal("100")
        assert strategy.peak_pnl == Decimal("100")
        assert strategy.drawdown_pause_until is None
        strategy.notification_client.send.assert_not_called()

    async def test_cross_strategy_isolation_only_own_pnl_affects_drawdown(
        self, strategy
    ):
        """AC-2 补充: 只有本策略的平仓 pnl 会影响 cumulative_pnl 和回撤率

        场景：本策略盈利 100 → 亏损 20（触发熔断，回撤 20%）。
        即使其他策略期间盈利 1000，本策略回撤率仍基于自己的 cumulative_pnl。
        """
        # 本策略盈利 100
        await strategy._check_max_drawdown(100.0)
        assert strategy.peak_pnl == Decimal("100")

        # 其他策略盈利 1000（本策略不感知，不调用 _check_max_drawdown）
        # 本策略亏损 20
        await strategy._check_max_drawdown(-20.0)

        # 本策略回撤率 = (100-80)/100 = 20% >= 15%，触发熔断
        assert strategy.cumulative_pnl == Decimal("80")
        assert strategy.peak_pnl == Decimal("100")
        assert strategy.drawdown_pause_until is not None
        strategy.notification_client.send.assert_called_once()
