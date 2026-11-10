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
            "(id INTEGER PRIMARY KEY, strategy TEXT, executed_at TIMESTAMP, realized_pnl REAL)"
        )

    def add(self, executed_at, realized_pnl):
        self.conn.execute(
            "INSERT INTO trading.trade_records (strategy, executed_at, realized_pnl) VALUES (?, ?, ?)",
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


def test_reduced_trial_strategies_are_excluded_from_legacy_pnl_alerts():
    class DB:
        calls = 0

        async def fetch_one(self, *args):
            self.calls += 1
            return {}

    config = {
        "strategies": [{"strategy_id": "hrs"}, {"strategy_id": "btc_eth_aggressive"}],
        "reduced_trial": {"enabled": True, "strategies": ["hrs", "btc_eth_aggressive"]},
    }
    db = DB()
    asyncio.run(DailyHealthCheck(config, db, None).run_check())
    assert db.calls == 0


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


def test_recent_profit_resets_consecutive_losses_even_after_old_loss_streak():
    db = SqliteDb()
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    for hours_ago, pnl in ((5, -1.0), (4, -1.0), (3, -1.0), (2, -1.0),
                           (1, 2.0), (1, None)):
        db.add(now - timedelta(hours=hours_ago), pnl)

    checker = DailyHealthCheck({"anomaly_detection": {}}, db, None)
    report = asyncio.run(checker._collect_recent_performance("hrs"))

    assert report["consecutive_losses"] == 0
    assert not any("连续亏损" in item for item in checker._detect_anomalies("hrs", report))


def test_consecutive_losses_count_only_after_latest_profit_with_same_timestamp():
    db = SqliteDb()
    now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    earlier = now - timedelta(hours=2)
    latest = now - timedelta(hours=1)
    for timestamp, pnl in ((earlier, -1.0), (earlier, -1.0), (latest, 2.0),
                           (latest, -1.0), (latest, -1.0)):
        db.add(timestamp, pnl)

    checker = DailyHealthCheck({"anomaly_detection": {}}, db, None)
    report = asyncio.run(checker._collect_recent_performance("hrs"))

    assert report["consecutive_losses"] == 2
