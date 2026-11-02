"""每日健康告警使用已实现盈亏交易与北京时间 24 小时窗口。"""

import asyncio
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ai_tuner.monitor.daily_health_check import DailyHealthCheck


class SqliteDb:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("ATTACH DATABASE ':memory:' AS trading")
        self.conn.execute(
            "CREATE TABLE trading.trade_records "
            "(strategy TEXT, executed_at TIMESTAMP, realized_pnl REAL)"
        )

    def add(self, executed_at, realized_pnl):
        self.conn.execute(
            "INSERT INTO trading.trade_records VALUES (?, ?, ?)",
            ("hrs", executed_at, realized_pnl),
        )

    async def fetch_one(self, query, *args):
        row = self.conn.execute(
            query.replace("$1", "?").replace("$2", "?").replace("$3", "?"), args
        ).fetchone()
        return dict(row) if row else None


def test_win_rate_excludes_records_without_realized_pnl():
    db = SqliteDb()
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    for pnl in (1.0, -1.0, -1.0, -1.0, -1.0, None, None, None, None, None):
        db.add(now - timedelta(hours=1), pnl)

    checker = DailyHealthCheck({"anomaly_detection": {}}, db, None)
    report = asyncio.run(checker._collect_recent_performance("hrs"))

    assert report["total_trades"] == 10
    assert report["realized_trades"] == 5
    assert report["win_count"] == 1
    assert report["loss_count"] == 4
    assert report["total_pnl"] == -3.0
    assert any("20.0%" in item for item in checker._detect_anomalies("hrs", report))


def test_window_is_previous_24_beijing_hours():
    db = SqliteDb()
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    db.add(now - timedelta(hours=25), -1.0)
    db.add(now - timedelta(hours=23), 1.0)
    db.add(now + timedelta(hours=1), -10.0)

    checker = DailyHealthCheck({"anomaly_detection": {}}, db, None)
    report = asyncio.run(checker._collect_recent_performance("hrs"))

    assert report["total_trades"] == 1
    assert report["realized_trades"] == 1
    assert report["win_count"] == 1
    assert report["total_pnl"] == 1.0


def test_loss_alerts_keep_existing_total_record_gate():
    db = SqliteDb()
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    for pnl in (-25.0, -25.0, -25.0, -25.0, None):
        db.add(now - timedelta(hours=1), pnl)

    checker = DailyHealthCheck(
        {"anomaly_detection": {"default_large_loss_threshold": -30}}, db, None
    )
    report = asyncio.run(checker._collect_recent_performance("hrs"))
    anomalies = checker._detect_anomalies("hrs", report)

    assert report["total_trades"] == 5
    assert report["realized_trades"] == 4
    assert any("总亏损" in item for item in anomalies)
    assert any("连续亏损" in item for item in anomalies)
    assert not any("胜率" in item for item in anomalies)
