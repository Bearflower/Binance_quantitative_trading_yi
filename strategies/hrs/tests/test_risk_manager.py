"""
风控管理测试
测试 RiskManager 的风控规则：连续亏损、回撤熔断、黑名单、每日限制等
"""
import pytest
import yaml
from pathlib import Path
from datetime import datetime, timedelta, timezone

# 加载配置
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)

from strategies.hrs.risk_manager import RiskManager


class TestRiskManagerInit:
    """测试风控管理器初始化"""

    def test_从配置正确加载参数(self):
        rm = RiskManager(CONFIG)
        assert rm.max_loss_percent == 0.02
        assert rm.max_daily_positions == 3
        assert rm.max_daily_same_direction == 2
        assert rm.max_consecutive_losses == 3
        assert rm.pause_days == 2
        assert rm.drawdown_threshold == 0.15
        assert rm.drawdown_pause_days == 7
        assert rm.blacklist_check_hours == 24
        assert rm.reverse_surge_percent == 0.05

    def test_空配置使用默认值(self):
        rm = RiskManager({})
        assert rm.max_loss_percent == 0.02
        assert rm.max_daily_positions == 3
        assert rm.max_daily_same_direction == 2

    def test_初始状态正确(self):
        rm = RiskManager(CONFIG)
        assert rm.consecutive_losses == 0
        assert rm.pause_until is None
        assert rm.drawdown_pause_until is None
        assert rm.blacklist == set()
        assert rm.daily_open_count == {"short": 0, "long": 0}


class TestPositionOpening:
    """测试开仓限制"""

    @pytest.fixture
    def rm(self):
        return RiskManager(CONFIG)

    def test_初始状态可以开仓(self, rm):
        """初始状态，做空和做多都可以开仓"""
        assert rm.can_open_position("short") is True
        assert rm.can_open_position("long") is True

    def test_记录开仓后计数增加(self, rm):
        rm.record_open("short")
        assert rm.daily_open_count["short"] == 1
        assert rm.daily_open_count["long"] == 0

    def test_单日总开仓达上限后不可再开(self, rm):
        """单日最多开仓3个，第4个被拒绝"""
        # 先调用 can_open_position 初始化日期计数器
        rm.can_open_position("short")
        rm.record_open("short")
        rm.record_open("short")
        rm.record_open("long")
        # 已开仓 3 个，再开被拒绝
        assert rm.can_open_position("short") is False
        assert rm.can_open_position("long") is False

    def test_同方向单日最多2个(self, rm):
        """同方向最多2个，第3个被拒绝"""
        rm.can_open_position("short")
        rm.record_open("short")
        rm.record_open("short")
        # 同方向已开2个
        assert rm.can_open_position("short") is False
        # 做多方向还可以开
        assert rm.can_open_position("long") is True

    def test_混合方向开仓限制(self, rm):
        """做空2个 + 做多1个 = 3个，已达上限"""
        rm.can_open_position("short")
        rm.record_open("short")
        rm.record_open("short")
        rm.record_open("long")
        assert rm.can_open_position("short") is False
        assert rm.can_open_position("long") is False


class TestConsecutiveLoss:
    """测试连续亏损暂停"""

    @pytest.fixture
    def rm(self):
        return RiskManager(CONFIG)

    def test_连续亏损3次触发暂停(self, rm):
        """连续3次亏损后触发暂停"""
        rm.record_loss("DOGEUSDT", 1.0, 0.95)
        rm.record_loss("DOGEUSDT", 1.0, 0.95)
        rm.record_loss("DOGEUSDT", 1.0, 0.95)
        assert rm.consecutive_losses == 3
        assert rm.pause_until is not None
        # 暂停中不能开仓
        assert rm.can_open_position("short") is False

    def test_盈利后重置连续亏损计数(self, rm):
        """盈利后重置连续亏损计数"""
        rm.record_loss("DOGEUSDT", 1.0, 0.95)
        rm.record_loss("DOGEUSDT", 1.0, 0.95)
        assert rm.consecutive_losses == 2
        rm.record_profit()
        assert rm.consecutive_losses == 0

    def test_暂停结束后可恢复开仓(self, rm):
        """暂停结束后可恢复开仓"""
        rm.record_loss("DOGEUSDT", 1.0, 0.95)
        rm.record_loss("DOGEUSDT", 1.0, 0.95)
        rm.record_loss("DOGEUSDT", 1.0, 0.95)
        # 手动设置暂停已过期
        rm.pause_until = datetime.now(timezone.utc) - timedelta(hours=1)
        assert rm.can_open_position("short") is True
        assert rm.pause_until is None
        assert rm.consecutive_losses == 0


class TestDrawdown:
    """测试回撤熔断"""

    @pytest.fixture
    def rm(self):
        return RiskManager(CONFIG)

    def test_回撤未达阈值正常(self, rm):
        """回撤10%未达15%阈值，正常"""
        result = rm.check_drawdown(-100, 1000)  # 亏损100，回撤10%
        assert result is True
        assert rm.drawdown_pause_until is None

    def test_回撤达阈值触发熔断(self, rm):
        """回撤15%触发熔断"""
        result = rm.check_drawdown(-150, 1000)  # 亏损150，回撤15%
        assert result is False
        assert rm.drawdown_pause_until is not None

    def test_盈利不触发熔断(self, rm):
        """盈利不触发熔断"""
        result = rm.check_drawdown(100, 1000)
        assert result is True
        assert rm.drawdown_pause_until is None

    def test_账户余额为0不触发异常(self, rm):
        """账户余额为0时，返回True（安全处理）"""
        result = rm.check_drawdown(-100, 0)
        assert result is True

    def test_熔断结束后可恢复(self, rm):
        """熔断结束后可恢复"""
        rm.check_drawdown(-150, 1000)
        rm.drawdown_pause_until = datetime.now(timezone.utc) - timedelta(hours=1)
        assert rm.can_open_position("short") is True
        assert rm.drawdown_pause_until is None


class TestBlacklist:
    """测试黑名单"""

    @pytest.fixture
    def rm(self):
        return RiskManager(CONFIG)

    def test_添加黑名单(self, rm):
        rm.add_to_blacklist("DOGEUSDT", "止损后反向波动超过5%")
        assert rm.is_blacklisted("DOGEUSDT") is True
        assert rm.is_blacklisted("BTCUSDT") is False

    def test_黑名单永久生效(self, rm):
        rm.add_to_blacklist("DOGEUSDT", "测试")
        assert rm.is_blacklisted("DOGEUSDT") is True
        # 黑名单没有过期机制
        assert rm.is_blacklisted("DOGEUSDT") is True


class TestBlacklistMonitor:
    """测试黑名单监控"""

    @pytest.fixture
    def rm(self):
        return RiskManager(CONFIG)

    def test_止损后反向波动触发黑名单(self, rm):
        """止损后在监控期内反向波动超过5%"""
        rm.record_loss("DOGEUSDT", 1.0, 0.95)
        assert "DOGEUSDT" in rm._stop_loss_monitor
        # 当前价格 1.06，从入场价 1.0 涨了 6%
        result = rm.check_blacklist_monitor("DOGEUSDT", 1.06)
        assert result is False
        assert rm.is_blacklisted("DOGEUSDT") is True

    def test_止损后反向波动未达阈值正常(self, rm):
        """止损后反向波动3%，未达5%阈值"""
        rm.record_loss("DOGEUSDT", 1.0, 0.95)
        result = rm.check_blacklist_monitor("DOGEUSDT", 1.03)
        assert result is True
        assert rm.is_blacklisted("DOGEUSDT") is False

    def test_不在监控列表正常(self, rm):
        """不在监控列表中的币种，检查正常"""
        result = rm.check_blacklist_monitor("BTCUSDT", 50000)
        assert result is True

    def test_监控过期自动移除(self, rm):
        """监控过期后自动从监控列表移除"""
        rm.record_loss("DOGEUSDT", 1.0, 0.95)
        # 手动设置监控已过期
        rm._stop_loss_monitor["DOGEUSDT"]["monitor_until"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        result = rm.check_blacklist_monitor("DOGEUSDT", 1.0)
        assert result is True
        assert "DOGEUSDT" not in rm._stop_loss_monitor


class TestPositionSize:
    """测试仓位计算"""

    @pytest.fixture
    def rm(self):
        return RiskManager(CONFIG)

    def test_仓位计算正确(self, rm):
        """公式：开仓价值 = 账户总资金 × 2% / 止损幅度"""
        quantity = rm.calculate_position_size(
            account_balance=10000,
            stop_loss_percent=0.05,
            leverage=2,
            current_price=100,
        )
        # max_loss = 10000 * 0.02 = 200
        # position_value = 200 / 0.05 = 4000
        # quantity = 4000 / 100 = 40
        assert quantity == pytest.approx(40.0, rel=0.01)

    def test_价格为0的防护(self, rm):
        """价格为0时返回0，防止除零"""
        quantity = rm.calculate_position_size(
            account_balance=10000,
            stop_loss_percent=0.05,
            leverage=2,
            current_price=0,
        )
        assert quantity == 0.0

    def test_不同杠杆仓位不同(self, rm):
        """杠杆越高，所需保证金越少，但仓位数量不变"""
        q1 = rm.calculate_position_size(10000, 0.05, 2, 100)
        q2 = rm.calculate_position_size(10000, 0.05, 5, 100)
        # 仓位数量（quantity）与杠杆无关，只与开仓价值/价格有关
        assert q1 == pytest.approx(q2, rel=0.01)


class TestStateSerialization:
    """测试状态序列化"""

    @pytest.fixture
    def rm(self):
        return RiskManager(CONFIG)

    def test_to_dict_from_dict_往返(self, rm):
        """to_dict 和 from_dict 应该往返一致"""
        rm.record_loss("DOGEUSDT", 1.0, 0.95)
        rm.add_to_blacklist("XRPUSDT", "测试")
        rm.record_open("short")
        rm.record_open("short")
        rm.record_open("long")

        data = rm.to_dict()
        assert data["consecutive_losses"] == 1
        assert "XRPUSDT" in data["blacklist"]
        assert data["daily_open_count"]["short"] == 2
        assert data["daily_open_count"]["long"] == 1

        # 恢复
        rm2 = RiskManager(CONFIG)
        rm2.from_dict(data)
        assert rm2.consecutive_losses == 1
        assert rm2.is_blacklisted("XRPUSDT") is True
        assert rm2.daily_open_count["short"] == 2
        assert rm2.daily_open_count["long"] == 1

    def test_from_dict处理None值(self, rm):
        """from_dict 处理空值和 None"""
        rm.from_dict({})
        assert rm.consecutive_losses == 0
        assert rm.pause_until is None
        assert rm.drawdown_pause_until is None
        assert rm.blacklist == set()
        assert rm.daily_open_count == {"short": 0, "long": 0}