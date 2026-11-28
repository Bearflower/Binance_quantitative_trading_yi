import pytest
import datetime as dt

from ai_tuner.ops.log_retention_check import TZ, render


def test_report_flags_unhealthy_and_short_retention():
    now = dt.datetime(2026, 11, 13, 9, tzinfo=TZ)
    report = render({
        "now": now,
        "configured": {"kline-service", "grid-executor"},
        "rows": [
            {"service": "kline-service", "state": "running", "health": "healthy", "driver": "journald", "first": now - dt.timedelta(days=3), "last": now, "current_error": False, "history_first": now - dt.timedelta(days=3), "history_last": now, "history_error": False, "config_files": ""},
            {"service": "grid-executor", "state": "running", "health": "unhealthy", "driver": "journald", "first": now - dt.timedelta(hours=2), "last": now, "current_error": False, "history_first": now - dt.timedelta(hours=2), "history_last": now, "history_error": False, "config_files": ""},
        ],
        "journal": "Archived and active journals take up 300.0M in the file system.",
        "free": 16_000_000_000,
    })
    assert "2 个服务可查窗口不足 14 天" in report
    assert "grid-executor 状态 running/unhealthy" in report
    assert "根分区剩余不足" not in report


def test_new_service_retention_waits_for_start_date():
    now = dt.datetime(2026, 11, 13, 9, tzinfo=TZ)
    report = render({
        "now": now,
        "configured": {"execution-facts-collector"},
        "rows": [{"service": "execution-facts-collector", "state": "running", "health": "healthy", "driver": "journald", "first": now - dt.timedelta(days=3), "last": now, "current_error": False, "history_first": now - dt.timedelta(days=3), "history_last": now, "history_error": False, "config_files": ""}],
        "journal": "Archived and active journals take up 300.0M in the file system.",
        "free": 16_000_000_000,
    })
    assert "可查窗口不足 14 天" not in report


def test_collect_keeps_current_instance_and_retention_separate(monkeypatch, tmp_path):
    import json
    import subprocess

    from ai_tuner.ops import log_retention_check as check

    now = dt.datetime(2026, 11, 20, 9, tzinfo=TZ)
    old = now - dt.timedelta(days=15)
    drivers = {
        "market-cap-service": "json-file",
        "hrs-acceptance-reporter": "json-file",
        "grid-strategy": "journald",
        "kline-service": "journald",
    }
    containers = [
        {
            "Id": service + "-id",
            "Name": "/" + service,
            "Config": {"Labels": {"com.docker.compose.service": service}},
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
            "HostConfig": {"LogConfig": {"Type": driver}},
        }
        for service, driver in drivers.items()
    ]
    containers[2]["Config"]["Labels"] = {}
    config = {
        "name": "trading_system",
        "services": {
            service: {"container_name": service}
            for service in ("grid-strategy", "kline-service")
        },
    }

    def fake_run(*args):
        if args[-1] == "--profiles":
            return ""
        if args[-2:] == ("--format", "json"):
            return json.dumps(config)
        if args[:3] == ("docker", "ps", "-aq"):
            return "market-id reporter-id kline-id"
        if args[:3] == ("docker", "ps", "-a"):
            return "grid-strategy\n"
        if args == ("docker", "inspect", "grid-strategy"):
            return json.dumps([containers[2]])
        if args[:2] == ("docker", "inspect"):
            return json.dumps(containers[:2] + containers[3:])
        if args == ("journalctl", "--disk-usage"):
            return "300M"
        if args[0] == "df":
            return "Avail\n16000000000"
        raise AssertionError(args)

    def fake_journal(field, value, *, latest):
        if field == "CONTAINER_NAME":
            return now if latest else old
        assert field == "CONTAINER_ID_FULL"
        if value == "kline-service-id":
            raise subprocess.CalledProcessError(1, "journalctl")
        assert value == "grid-strategy-id"
        return None

    def fake_docker_logs(container_id):
        if container_id == "market-cap-service-id":
            return now - dt.timedelta(hours=2), now - dt.timedelta(hours=1)
        assert container_id == "hrs-acceptance-reporter-id"
        return None, None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(check, "run", fake_run)
    monkeypatch.setattr(check, "journal_time", fake_journal)
    monkeypatch.setattr(check, "docker_log_range", fake_docker_logs, raising=False)
    report = check.render(check.collect(tmp_path, now))

    assert "容器: 4 个；基础 Compose: 2 个" in report
    assert "当前实例 11-20 07:00 → 11-20 08:00" in report
    assert "hrs-acceptance-reporter 当前实例无日志" in report
    assert "grid-strategy 当前实例无日志" in report
    assert "kline-service 当前实例日志查询失败" in report
    assert "kline-service 当前实例无日志" not in report
    assert "服务 journal 11-05 09:00 → 11-20 09:00" in report
    assert "可查窗口不足 14 天" not in report


def test_docker_log_range_uses_full_id_and_reads_both_streams(monkeypatch):
    from ai_tuner.ops import log_retention_check as check

    def fake_run(args, **kwargs):
        assert args == ["docker", "logs", "--timestamps", "full-id"]
        assert kwargs["stderr"] == check.subprocess.STDOUT
        return check.subprocess.CompletedProcess(args, 0, stdout=(
            "2026-11-20T00:23:57.123456789Z stderr\n"
            "2026-11-19T15:20:02.123456789Z stdout\n"
        ))

    monkeypatch.setattr(check.subprocess, "run", fake_run)
    first, last = check.docker_log_range("full-id")
    assert first == dt.datetime(2026, 11, 19, 23, 20, 2, 123456, tzinfo=TZ)
    assert last == dt.datetime(2026, 11, 20, 8, 23, 57, 123456, tzinfo=TZ)


@pytest.mark.parametrize("configured", [True, False])
def test_collect_replaced_label_does_not_hide_unlabelled_live_instance(monkeypatch, tmp_path, configured):
    import json
    from ai_tuner.ops import log_retention_check as check

    now = dt.datetime(2026, 11, 26, 9, tzinfo=TZ)
    old = {"Id": "a" * 64, "Name": "/hrs-before-release",
           "Config": {"Labels": {"com.docker.compose.service": "hrs"}},
           "State": {"Status": "exited", "Health": {"Status": "unhealthy"}},
           "HostConfig": {"LogConfig": {"Type": "journald"}, "RestartPolicy": {"Name": "no"}}}
    live = {**old, "Id": "b" * 64, "Name": "/hrs", "Config": {"Labels": {}},
            "State": {"Status": "running", "Health": {"Status": "healthy"}}}
    (tmp_path / '.ops').mkdir()
    (tmp_path / '.ops/container_retirements.json').write_text(json.dumps({"version": 1, "entries": [
        {"id": old["Id"], "name": "hrs-before-release", "successor": "hrs", "reason": "已替换"}]}))

    def fake_run(*args):
        if args[-1] == '--profiles': return ''
        if args[-2:] == ('--format', 'json'):
            return json.dumps({"name": "trading_system", "services": {"hrs": {"container_name": "hrs"}} if configured else {}})
        if args[:3] == ('docker', 'ps', '-aq'): return old['Id']
        if args[:3] == ('docker', 'ps', '-a'): return 'hrs\nhrs-before-release'
        if args == ('docker', 'inspect', 'hrs'): return json.dumps([live])
        if args[:2] == ('docker', 'inspect'): return json.dumps([old])
        if args[0] == 'journalctl': return '300M'
        if args[0] == 'df': return 'Avail\n16000000000'
        raise AssertionError(args)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(check, 'run', fake_run)
    monkeypatch.setattr(check, 'journal_range', lambda *args: (now-dt.timedelta(days=15), now))
    data = check.collect(tmp_path, now)
    assert len(data['rows']) == 1
    assert data['rows'][0]['state'] == 'running'
    assert data['retired'] == ['hrs-before-release']
    assert check.evaluate(data) == []
    assert '退役实例: 1' in check.render(data)


def test_inventory_drift_is_reported():
    from ai_tuner.ops.log_retention_check import evaluate
    data = {'now': dt.datetime.now(TZ), 'rows': [], 'configured': {'hrs'},
            'free': 16_000_000_000, 'journal': '300M',
            'inventory_issues': ['退役登记状态漂移或接替实例缺失: hrs-before-release']}
    assert '退役登记状态漂移或接替实例缺失: hrs-before-release' in evaluate(data)
    assert '缺少容器: hrs' in evaluate(data)
