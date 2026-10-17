"""
HRS 持仓生命周期元数据测试（B5）

覆盖口径：
1. add_position 写入 opened_at/next_review_at/position_id/entry_mode/entry_config_hash
2. 分档时限：lv_rm → 48h；standard/emm/semi_emm 与缺失模式（unknown）→ 72h
3. reset_time_stop 只前进 next_review_at，绝不改写 opened_at
4. 时间止损以 next_review_at 为判据，在 t0+时限 触发
5. to_dict/from_dict 往返保留新字段
6. D9 迁移：仅含旧 entry_time 的存量状态 → 作为 opened_at 并标记不确定/模式未知
"""
import pytest
import yaml
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(CONFIG_PATH, "r") as f:
    CONFIG = yaml.safe_load(f)

from strategies.hrs.position_manager import PositionManager


@pytest.fixture
def mock_binance_api():
    """模拟币安 API 客户端"""
    api = MagicMock()
    api.get_open_algo_orders = AsyncMock(return_value=[])
    api.cancel_algo_order = AsyncMock(return_value={"status": "CANCELED"})
    return api


@pytest.fixture
def pm(mock_binance_api):
    return PositionManager(CONFIG, mock_binance_api)


class TestLifecycleMetadataOnAdd:
    """add_position 写入生命周期元数据"""

    def test_写入开仓时刻与复核时刻与身份(self, pm):
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005, entry_mode="standard", opened_at=t0)
        pos = pm.get_position("DOGEUSDT")

        assert pos["opened_at"] == t0
        # standard → 标准时限 72h
        assert pos["next_review_at"] == t0 + timedelta(hours=72)
        assert pos["position_id"] == f"hrs:DOGEUSDT:short:{int(t0.timestamp() * 1000)}"
        assert pos["entry_mode"] == "standard"
        assert len(pos["entry_config_hash"]) == 16
        assert pos["opening_time_uncertain"] is False
        assert pos["entry_mode_unknown"] is False

    def test_lv_rm使用独立48小时时限(self, pm):
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005, entry_mode="lv_rm", opened_at=t0)
        pos = pm.get_position("DOGEUSDT")
        assert pos["next_review_at"] == t0 + timedelta(hours=48)

    def test_缺失模式记为unknown并按标准时限(self, pm):
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005, opened_at=t0)
        pos = pm.get_position("DOGEUSDT")
        assert pos["entry_mode"] == "unknown"
        assert pos["entry_mode_unknown"] is True
        assert pos["next_review_at"] == t0 + timedelta(hours=72)

    def test_同币平仓后再开为新周期身份(self, pm):
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005, entry_mode="standard", opened_at=t0)
        first_id = pm.get_position("DOGEUSDT")["position_id"]
        pm.remove_position("DOGEUSDT")

        t1 = t0 + timedelta(hours=5)
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005, entry_mode="standard", opened_at=t1)
        assert pm.get_position("DOGEUSDT")["position_id"] != first_id


class TestTimeStopReview:
    """时间止损以 next_review_at 为判据"""

    def test_t0加72小时触发(self, pm):
        now = datetime.now(timezone.utc)
        pm.add_position(
            "DOGEUSDT", "short", 1.0, 100, 0.005,
            entry_mode="standard", opened_at=now - timedelta(hours=72, seconds=1),
        )
        assert pm.check_time_stop("DOGEUSDT") is True

    def test_未到复核时刻不触发(self, pm):
        now = datetime.now(timezone.utc)
        pm.add_position(
            "DOGEUSDT", "short", 1.0, 100, 0.005,
            entry_mode="standard", opened_at=now - timedelta(hours=71),
        )
        assert pm.check_time_stop("DOGEUSDT") is False

    def test_lv_rm在t0加48小时触发(self, pm):
        now = datetime.now(timezone.utc)
        pm.add_position(
            "DOGEUSDT", "short", 1.0, 100, 0.005,
            entry_mode="lv_rm", opened_at=now - timedelta(hours=48, seconds=1),
        )
        assert pm.check_time_stop("DOGEUSDT") is True

    def test_reset只前进复核时刻且不改开仓时刻(self, pm):
        now = datetime.now(timezone.utc)
        opened = now - timedelta(hours=73)
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005, entry_mode="standard", opened_at=opened)
        assert pm.check_time_stop("DOGEUSDT") is True

        pm.reset_time_stop("DOGEUSDT")
        pos = pm.get_position("DOGEUSDT")
        # 开仓时刻不可变，复核时刻前进到 now+72h
        assert pos["opened_at"] == opened
        assert pos["next_review_at"] > now
        assert pm.check_time_stop("DOGEUSDT") is False


class TestSerializationRoundTrip:
    """to_dict / from_dict 往返"""

    def test_往返保留新字段(self, pm):
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        pm.add_position("DOGEUSDT", "short", 1.0, 100, 0.005, entry_mode="lv_rm", opened_at=t0)
        original = dict(pm.get_position("DOGEUSDT"))
        data = pm.to_dict()

        # 回滚别名：to_dict 仍写出 entry_time 键（值 = opened_at）
        assert data["positions"]["DOGEUSDT"]["entry_time"] == t0.isoformat()

        pm2 = PositionManager(CONFIG, pm.binance_api)
        pm2.from_dict(data)
        restored = pm2.get_position("DOGEUSDT")

        assert restored["opened_at"] == original["opened_at"]
        assert restored["next_review_at"] == original["next_review_at"]
        assert restored["position_id"] == original["position_id"]
        assert restored["entry_mode"] == "lv_rm"
        # 内存记录不再保留 entry_time 别名，避免与 opened_at 并存
        assert "entry_time" not in restored

    def test_存量仅entry_time恢复为opened_at并标记不确定(self, pm):
        legacy = datetime(2025, 12, 1, 0, 0, tzinfo=timezone.utc)
        pm.from_dict({"positions": {
            "DOGEUSDT": {
                "direction": "short",
                "entry_price": 1.0,
                "entry_quantity": 100,
                "remaining_quantity": 70,
                "entry_time": legacy.isoformat(),
            }
        }})
        pos = pm.get_position("DOGEUSDT")

        # 旧值可能已被重置，只能作为 opened_at 且标记不确定，不编造
        assert pos["opened_at"] == legacy
        assert pos["opening_time_uncertain"] is True
        assert pos["entry_mode"] == "unknown"
        assert pos["entry_mode_unknown"] is True
        # 未知模式按标准时限复核
        assert pos["next_review_at"] == legacy + timedelta(hours=72)
        assert pos["position_id"].startswith("hrs:DOGEUSDT:short:")
        # 恢复后跟踪量对齐剩余数量
        assert pm._last_tracked_qty["DOGEUSDT"] == pytest.approx(70)
