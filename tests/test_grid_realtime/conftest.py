"""grid realtime M1 测试通用配置与构造器。

- 把仓库根加入 sys.path（strategies 为命名空间包）。
- 提供证据快照、profile 原始 dict、成交/特征构造器，避免测试间复制样板。
"""
import copy
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from strategies.grid.realtime.rules import ReferenceSnapshot  # noqa: E402
from factories import make_feature, make_trade  # noqa: E402

__all__ = ["make_trade", "make_feature"]

# 09:05 证据快照四边界（需求 §2.2）
SL, L, U, SU = Decimal("2599.45"), Decimal("2623.52"), Decimal("2767.96"), Decimal("2792.03")


@pytest.fixture
def snap() -> ReferenceSnapshot:
    return ReferenceSnapshot(
        reference_id="grid-ev-test", symbol="ETHUSDT",
        calculated_at_ms=1_000_000_000_000,
        effective_at_ms=1_000_000_000_000,
        grid_lower=L, grid_upper=U, stop_lower=SL, stop_upper=SU,
        stop_move_up_price=Decimal("2780.00"),
        stop_move_down_price=Decimal("2611.48"))


@pytest.fixture
def db_setup(tmp_path):
    """建好 schema 的库 + 一个 ACTIVE 会话；返回 (conn, db_path, session_id)。"""
    from strategies.grid.realtime.reference_store import (ExportStore,
                                                          connect, ensure_schema)
    db_path = str(tmp_path / "grid_realtime.sqlite3")
    conn = connect(db_path, 1000)
    ensure_schema(conn)
    session_id = ExportStore(conn).begin_session(100)
    return conn, db_path, session_id


@pytest.fixture
def profile_raw() -> dict:
    raw = {
        "enabled": False, "mode": "shadow", "symbol": "ETHUSDT",
        "price_source": "agg_trade", "profile": "test",
        "efficiency_filter": {"enabled": True},
        "reference": {"max_age_seconds": 21600},
        "features": {"sample_seconds": 1, "windows_seconds": [60, 180, 300],
                     "max_anchor_gap_seconds": 2, "e_resample_seconds": 1},
        "normal": {"return_3m": 0.005, "return_5m": 0.005,
                   "min_efficiency": 0.60, "near_grid_fraction": 0.40,
                   "hold_seconds": 10},
        "urgent": {"near_grid_fraction": 0.20, "return_3m": 0.0075,
                    "return_5m": 0.0075, "min_efficiency": 0.70,
                    "hold_seconds": 3, "buffer_return_1m": 0.003,
                    "critical_buffer_fraction": 0.75},
        "recovery": {"inside_fraction": 0.45, "hold_seconds": 60},
        "repeat": {"notice_seconds": 1800, "urgent_seconds": 300,
                   "boundary_seconds": 300, "health_seconds": 1800},
        "health": {"max_silence_seconds": 5, "max_event_lag_seconds": 3},
        "storage": {"path": "/app/data/grid_realtime.sqlite3",
                    "busy_timeout_ms": 1000, "sent_retention_days": 30,
                    "audit_retention_days": 90, "max_bytes": 1073741824},
        "transport": {"heartbeat_seconds": 20, "reconnect_initial_seconds": 1,
                      "reconnect_max_seconds": 30,
                      "refill_requests_per_second": 1},
        "reference_sync": {"heartbeat_seconds": 1, "max_silence_seconds": 5},
        "delivery": {"queue_capacity": 1000, "retry_initial_seconds": 1,
                     "retry_max_seconds": 30, "event_ttl_seconds": 30},
        "shadow_research": {"enabled": False, "notice_interval_seconds": 1800},
    }
    return copy.deepcopy(raw)
