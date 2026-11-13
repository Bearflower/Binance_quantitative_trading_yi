import datetime as dt

from ai_tuner.ops.log_retention_check import TZ, render


def test_report_flags_unhealthy_and_short_retention():
    now = dt.datetime(2026, 11, 13, 9, tzinfo=TZ)
    report = render({
        "now": now,
        "configured": {"kline-service", "grid-executor"},
        "rows": [
            {"service": "kline-service", "state": "running", "health": "healthy", "driver": "journald", "first": now - dt.timedelta(days=3), "last": now, "config_files": ""},
            {"service": "grid-executor", "state": "running", "health": "unhealthy", "driver": "journald", "first": now - dt.timedelta(hours=2), "last": now, "config_files": ""},
        ],
        "journal": "Archived and active journals take up 300.0M in the file system.",
        "free": 16_000_000_000,
    })
    assert "kline-service 可查窗口不足 14 天" in report
    assert "grid-executor 状态 running/unhealthy" in report
    assert "根分区剩余不足" not in report


def test_new_service_retention_waits_for_start_date():
    now = dt.datetime(2026, 11, 13, 9, tzinfo=TZ)
    report = render({
        "now": now,
        "configured": {"execution-facts-collector"},
        "rows": [{"service": "execution-facts-collector", "state": "running", "health": "healthy", "driver": "journald", "first": now - dt.timedelta(days=3), "last": now, "config_files": ""}],
        "journal": "Archived and active journals take up 300.0M in the file system.",
        "free": 16_000_000_000,
    })
    assert "可查窗口不足 14 天" not in report
