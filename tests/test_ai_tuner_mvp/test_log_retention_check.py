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
    assert "2 个服务可查窗口不足 14 天（最早 11-10 09:00，要求 ≥14 天）" in report
    assert report.count("可查窗口不足 14 天") == 1
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


def test_journal_usage_above_three_gib_is_flagged():
    now = dt.datetime(2026, 11, 13, 9, tzinfo=TZ)
    report = render({
        "now": now,
        "configured": {"kline-service"},
        "rows": [{"service": "kline-service", "state": "running", "health": "healthy", "driver": "journald", "first": now - dt.timedelta(days=20), "last": now, "config_files": ""}],
        "journal": "Archived and active journals take up 3.5G in the file system.",
        "free": 16_000_000_000,
    })
    assert "journal 占用达到 3 GiB" in report


def test_collect_matches_unlabelled_grid_by_configured_container_name(monkeypatch, tmp_path):
    import json

    from ai_tuner.ops import log_retention_check

    now = dt.datetime(2026, 11, 18, 9, tzinfo=TZ)
    grid = {
        "Name": "/trading_system-grid",
        "Config": {"Labels": {"grid.repair.revision": "current"}},
        "State": {"Status": "running", "Health": {"Status": "healthy"}},
        "HostConfig": {"LogConfig": {"Type": "journald"}},
    }
    config = {"name": "trading_system", "services": {
        "grid-strategy": {"container_name": "trading_system-grid"},
        "worker": {"container_name": "trading_system-worker"},
    }}

    def docker_and_journal(*args):
        if args[-1] == "--profiles":
            return ""
        if args[-2:] == ("--format", "json"):
            return json.dumps(config)
        if args[-1] == "--services":
            return "grid-strategy\nworker"
        if args[:3] == ("docker", "ps", "-aq"):
            return ""
        if args[:2] == ("docker", "ps"):
            return "trading_system-grid\ntrading_system-grid-copy"
        if args == ("docker", "inspect", "trading_system-grid"):
            return json.dumps([grid])
        if args == ("journalctl", "--disk-usage"):
            return "300M"
        if args[0] == "df":
            return "Avail\n16000000000\n"
        raise AssertionError(args)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(log_retention_check, "run", docker_and_journal)
    monkeypatch.setattr(log_retention_check, "journal_time", lambda *a, **k: now)
    report = render(log_retention_check.collect(tmp_path, now))

    assert "grid-strategy: healthy/journald" in report
    assert "缺少容器: worker" in report
    assert "grid-copy" not in report
    grid["Config"]["Labels"]["com.docker.compose.project"] = "unrelated"
    report = render(log_retention_check.collect(tmp_path, now))
    assert "缺少容器: grid-strategy, worker" in report


def test_webhook_error_reports_numeric_code_without_response_content(monkeypatch, tmp_path):
    import io

    import pytest

    from ai_tuner.ops import log_retention_check

    secret = tmp_path / "webhook"
    secret.write_text("https://open.feishu.cn/open-apis/bot/v2/hook/dummy")
    monkeypatch.setattr(
        log_retention_check.urllib.request,
        "urlopen",
        lambda *a, **k: io.BytesIO(b'{"code":19024,"msg":"untrusted response"}'),
    )

    with pytest.raises(RuntimeError, match=r"^Feishu webhook rejected the report \(code=19024\)$"):
        log_retention_check.send(secret, "test")
