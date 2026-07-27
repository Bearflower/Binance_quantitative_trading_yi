"""
持仓管理测试
测试 PositionManager 的持仓跟踪、止损止盈、分批止盈、时间止损等
"""
import pytest
import yaml
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# 加载配置
CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)


@pytest.fixture
def mock_binance_api():
    """创建模拟的币安API客户端"""
    api = MagicMock()
    api.get_open_algo_orders = AsyncMock(return_value=[])
    api.cancel_algo_order = AsyncMock(return_value={"status": "CANCELED"})
    return api


from strategies.hrs.position_manager import PositionManager


class TestPositionManagerInit:
    """测试持仓管理器初始化"""

    def test_从配置正确加载参数(self, mock_binance_api):
        pm = PositionManager(CONFIG, mock_binance_api)
        assert pm.target1_atr_multiplier == 1.5
        assert pm.target1_close_percent == 0.30
        assert pm.target2_atr_multiplier == 3.5
        assert pm.target2_close_percent == 0.40
        assert pm.trailing_stop_atr_multiplier == 1.5
        assert pm.max_holding_hours == 72

    def test_初始状态无持仓(self, mock_binance_api):
        pm = PositionManager(CONFIG, mock_binance_api)
        assert pm.get_all_positions() == {}
        assert pm.has_position("DOGEUSDT") is False


class TestPositionTracking:
    """测试持仓跟踪"""

    @pytest.fixture
    def pm(self, mock_binance_api):
        return PositionManager(CONFIG, mock_binance_api)

    def test_添加持仓(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        assert pm.has_position("DOGEUSDT") is True
        pos = pm.get_position("DOGEUSDT")
        assert pos["direction"] == "short"
        assert pos["entry_price"] == 1.0
        assert pos["entry_quantity"] == 100
        assert pos["atr"] == 0.005
        assert pos["target1_reached"] is False
        assert pos["target2_reached"] is False
        assert pos["remaining_quantity"] == 100

    def test_移除持仓(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        pm.remove_position("DOGEUSDT")
        assert pm.has_position("DOGEUSDT") is False
        assert pm.get_position("DOGEUSDT") is None

    def test_获取所有持仓(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        pm.add_position("SHIBUSDT", "long", 0.00001, 1000000, 0.0000001)
        all_pos = pm.get_all_positions()
        assert len(all_pos) == 2
        assert "DOGEUSDT" in all_pos
        assert "SHIBUSDT" in all_pos

    def test_获取方向(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        assert pm.get_direction("DOGEUSDT") == "short"
        assert pm.get_direction("NONEXIST") is None

    def test_添加重复持仓覆盖(self, pm):
        """添加同名持仓应覆盖旧持仓"""
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        pm.add_position("DOGEUSDT", "long", 0.5, 200, 0.003)
        pos = pm.get_position("DOGEUSDT")
        assert pos["direction"] == "long"
        assert pos["entry_price"] == 0.5
        assert pos["entry_quantity"] == 200


class TestBestPrice:
    """测试最佳价格跟踪"""

    @pytest.fixture
    def pm(self, mock_binance_api):
        return PositionManager(CONFIG, mock_binance_api)

    def test_做空记录最低价(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        assert pm.get_position("DOGEUSDT")["best_price"] == 1.0
        pm.update_best_price("DOGEUSDT", 0.95)
        assert pm.get_position("DOGEUSDT")["best_price"] == 0.95
        pm.update_best_price("DOGEUSDT", 0.98)  # 更高，不更新
        assert pm.get_position("DOGEUSDT")["best_price"] == 0.95

    def test_做多记录最高价(self, pm):
        pm.add_position("DOGEUSDT", "long", 1.0, 100, 0.005)
        assert pm.get_position("DOGEUSDT")["best_price"] == 1.0
        pm.update_best_price("DOGEUSDT", 1.05)
        assert pm.get_position("DOGEUSDT")["best_price"] == 1.05
        pm.update_best_price("DOGEUSDT", 1.02)  # 更低，不更新
        assert pm.get_position("DOGEUSDT")["best_price"] == 1.05

    def test_不存在的持仓不报错(self, pm):
        pm.update_best_price("NONEXIST", 1.0)  # 不应报错


class TestTrailingStop:
    """测试移动止盈"""

    @pytest.fixture
    def pm(self, mock_binance_api):
        return PositionManager(CONFIG, mock_binance_api)

    def test_未达第二目标不触发移动止盈(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        # target2_reached = False, 不触发
        assert pm.check_trailing_stop("DOGEUSDT", 0.95) is False

    def test_做空移动止盈触发(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        pm.mark_target_reached("DOGEUSDT", 1)
        pm.mark_target_reached("DOGEUSDT", 2)
        # 最佳价格更新为 0.90
        pm.update_best_price("DOGEUSDT", 0.90)
        # 反弹: 0.91 - 0.90 = 0.01, ATR*1.5 = 0.005*1.5 = 0.0075
        assert pm.check_trailing_stop("DOGEUSDT", 0.91) is True

    def test_做多移动止盈触发(self, pm):
        pm.add_position("DOGEUSDT", "long", 1.0, 100, 0.005)
        pm.mark_target_reached("DOGEUSDT", 1)
        pm.mark_target_reached("DOGEUSDT", 2)
        pm.update_best_price("DOGEUSDT", 1.10)
        # 回撤: 1.10 - 1.09 = 0.01, ATR*1.5 = 0.0075
        assert pm.check_trailing_stop("DOGEUSDT", 1.09) is True

    def test_移动止盈未触发(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        pm.mark_target_reached("DOGEUSDT", 1)
        pm.mark_target_reached("DOGEUSDT", 2)
        pm.update_best_price("DOGEUSDT", 0.90)
        # 反弹: 0.905 - 0.90 = 0.005, ATR*1.5 = 0.0075, 未达阈值
        assert pm.check_trailing_stop("DOGEUSDT", 0.905) is False

    def test_ATR为0不触发(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0)
        pm.mark_target_reached("DOGEUSDT", 1)
        pm.mark_target_reached("DOGEUSDT", 2)
        assert pm.check_trailing_stop("DOGEUSDT", 2.0) is False


class TestTimeStop:
    """测试时间止损"""

    @pytest.fixture
    def pm(self, mock_binance_api):
        return PositionManager(CONFIG, mock_binance_api)

    def test_未达第一目标且持仓超时触发(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        # 手动设置开仓时间为 73 小时前
        pos = pm.get_position("DOGEUSDT")
        pos["entry_time"] = datetime.now(timezone.utc) - timedelta(hours=73)
        assert pm.check_time_stop("DOGEUSDT") is True

    def test_未达第一目标但持仓未超时不触发(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        # 持仓才1小时
        assert pm.check_time_stop("DOGEUSDT") is False

    def test_已达第一目标不触发时间止损(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        pm.mark_target_reached("DOGEUSDT", 1)
        pos = pm.get_position("DOGEUSDT")
        pos["entry_time"] = datetime.now(timezone.utc) - timedelta(hours=73)
        # 已达第一目标，不触发时间止损
        assert pm.check_time_stop("DOGEUSDT") is False

    def test_不存在的持仓不触发(self, pm):
        assert pm.check_time_stop("NONEXIST") is False


class TestBatchTakeProfit:
    """测试分批止盈"""

    @pytest.fixture
    def pm(self, mock_binance_api):
        return PositionManager(CONFIG, mock_binance_api)

    def test_第一目标达成平仓30(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        pm.mark_target_reached("DOGEUSDT", 1)
        pos = pm.get_position("DOGEUSDT")
        assert pos["target1_reached"] is True
        # 剩余数量 = 100 * (1 - 0.30) = 70
        assert pos["remaining_quantity"] == pytest.approx(70.0)

    def test_第二目标达成再平仓40(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        pm.mark_target_reached("DOGEUSDT", 1)
        pos = pm.get_position("DOGEUSDT")
        # 第一目标后剩余 70
        assert pos["remaining_quantity"] == pytest.approx(70.0)
        pm.mark_target_reached("DOGEUSDT", 2)
        # 第二目标再平仓 40%，剩余 70 * (1-0.40) = 42
        assert pos["remaining_quantity"] == pytest.approx(42.0)

    def test_不存在持仓标记不报错(self, pm):
        pm.mark_target_reached("NONEXIST", 1)  # 不应报错


class TestStateSerialization:
    """测试状态序列化"""

    @pytest.fixture
    def pm(self, mock_binance_api):
        return PositionManager(CONFIG, mock_binance_api)

    def test_to_dict_from_dict_往返(self, pm):
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005)
        pm.add_position("SHIBUSDT", "long", 0.00001, 1000000, 0.0000001)
        pm.mark_target_reached("DOGEUSDT", 1)

        data = pm.to_dict()
        assert "positions" in data
        assert "DOGEUSDT" in data["positions"]
        assert "SHIBUSDT" in data["positions"]

        # 恢复
        pm2 = PositionManager(CONFIG, mock_binance_api)
        pm2.from_dict(data)
        assert pm2.has_position("DOGEUSDT") is True
        assert pm2.has_position("SHIBUSDT") is True
        pos = pm2.get_position("DOGEUSDT")
        assert pos["direction"] == "short"
        assert pos["entry_price"] == 1.0
        assert pos["target1_reached"] is True
        assert pos["remaining_quantity"] == pytest.approx(70.0)

    def test_from_dict空数据(self, pm):
        pm.from_dict({})
        assert pm.get_all_positions() == {}