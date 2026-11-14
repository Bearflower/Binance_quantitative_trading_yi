"""The prospective collector is an independent, least-privilege daemon."""

import asyncio
import json
import re

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from ai_tuner.funding_schedule.cli import (
    build_parser,
    next_slot_after,
    run_once,
)


ROOT = Path(__file__).resolve().parents[2]
SLOT = datetime(2026, 10, 30, 5, 0, tzinfo=timezone.utc)


def test_only_once_and_daemon_commands_are_exposed():
    parser = build_parser()
    assert parser.parse_args(["once"]).command == "once"
    assert parser.parse_args(["daemon"]).command == "daemon"
    with pytest.raises(SystemExit):
        parser.parse_args(["backfill-supported"])


def test_next_slot_is_utc_aligned_and_skips_missed_slots():
    assert next_slot_after(SLOT - timedelta(microseconds=1)) == SLOT
    assert next_slot_after(SLOT + timedelta(seconds=1)) == SLOT + timedelta(minutes=5)
    assert next_slot_after(SLOT + timedelta(minutes=16), last_slot=SLOT) == SLOT + timedelta(minutes=20)
    assert next_slot_after(SLOT, last_slot=SLOT) == SLOT + timedelta(minutes=5)


def test_monitor_start_skips_predeployment_slots_but_rechecks_recent_after_restart():
    from ai_tuner.funding_schedule.cli import monitor_start_slot

    next_slot = SLOT + timedelta(minutes=20)
    now = SLOT + timedelta(minutes=16)
    assert monitor_start_slot(next_slot, None, now) == next_slot
    assert monitor_start_slot(next_slot, SLOT, now) == SLOT


def test_runtime_requires_an_alert_destination(monkeypatch):
    from ai_tuner.funding_schedule.cli import _runtime

    monkeypatch.delenv("FEISHU_WEBHOOK_TUNER", raising=False)
    with pytest.raises(RuntimeError, match="FEISHU_WEBHOOK_TUNER"):
        _runtime()


@pytest.mark.asyncio
async def test_once_collects_only_current_slot_and_does_not_loop():
    collector = SimpleNamespace(collect_once=AsyncMock(return_value=SimpleNamespace(
        status="success", failure_code=None, symbol_count=2,
    )))
    notifier = SimpleNamespace(send=AsyncMock())
    result = await run_once(collector, notifier, scheduled_at=SLOT)
    assert result.status == "success"
    assert collector.collect_once.await_count == 1
    assert collector.collect_once.await_args.args[0] == SLOT
    assert notifier.send.await_count == 0


@pytest.mark.asyncio
async def test_hung_collection_is_cancelled_within_ninety_seconds(monkeypatch):
    from ai_tuner.funding_schedule import cli

    cancelled = asyncio.Event()

    async def hanging_collect(*_):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable, timeout):
        assert timeout == 90
        return await real_wait_for(awaitable, 0.01)

    monkeypatch.setattr(cli.asyncio, "wait_for", short_wait_for)
    result = await real_wait_for(
        run_once(SimpleNamespace(collect_once=hanging_collect), SimpleNamespace(), scheduled_at=SLOT),
        0.2,
    )
    assert result.status == "failed"
    assert result.failure_code == "COLLECTION_TIMEOUT"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_monitor_detects_missing_persisted_slot_once_and_restart_rechecks_recent_slots(tmp_path):
    from ai_tuner.funding_schedule.cli import MissingSlotMonitor

    repo = SimpleNamespace(
        slots_with_runs=AsyncMock(return_value={SLOT, SLOT + timedelta(minutes=10)}),
    )
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    now = SLOT + timedelta(minutes=13, seconds=10)
    alert_path = tmp_path / "alerts.json"
    monitor = MissingSlotMonitor(repo, notifier, start_slot=SLOT, alert_state_path=alert_path)
    await monitor.check(now)
    await monitor.check(now + timedelta(seconds=30))
    assert notifier.send.await_count == 1
    assert "MISSING_SCHEDULE_SLOT" in notifier.send.await_args.args[0]
    assert (SLOT + timedelta(minutes=5)).isoformat() in notifier.send.await_args.args[0]
    assert repo.slots_with_runs.await_count == 2

    # A restarted process scans the slot but respects persisted delivery.
    restarted = MissingSlotMonitor(repo, notifier, start_slot=SLOT, alert_state_path=alert_path)
    await restarted.check(now)
    assert notifier.send.await_count == 1


@pytest.mark.asyncio
async def test_missing_slot_failed_delivery_retries_after_one_minute_across_restarts(tmp_path):
    from ai_tuner.funding_schedule.cli import MissingSlotMonitor

    state = tmp_path / "alerts.json"
    repo = SimpleNamespace(slots_with_runs=AsyncMock(return_value=set()))
    notifier = SimpleNamespace(send=AsyncMock(side_effect=[False, True]))
    for minutes, expected_calls in ((3, 1), (3.5, 1), (4, 2), (4.5, 2)):
        monitor = MissingSlotMonitor(repo, notifier, start_slot=SLOT, alert_state_path=state)
        await monitor.check(SLOT + timedelta(minutes=minutes))
        assert notifier.send.await_count == expected_calls


@pytest.mark.asyncio
async def test_monitor_db_query_failure_alerts_sanitized_code_and_exits(tmp_path):
    from ai_tuner.funding_schedule.cli import MissingSlotMonitor

    repo = SimpleNamespace(slots_with_runs=AsyncMock(side_effect=RuntimeError("password=secret")))
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    monitor = MissingSlotMonitor(repo, notifier, start_slot=SLOT,
                                 alert_state_path=tmp_path / "query-alert.json")
    with pytest.raises(RuntimeError, match="monitor database unavailable") as raised:
        await monitor.check(SLOT + timedelta(minutes=3))
    assert "secret" not in str(raised.value)
    notifier.send.assert_awaited_once()
    assert "RUN_QUERY_FAILED" in notifier.send.await_args.args[0]
    assert "secret" not in notifier.send.await_args.args[0]


@pytest.mark.asyncio
async def test_db_failure_alert_is_rate_limited_across_restarts_then_reset_after_recovery(tmp_path):
    from ai_tuner.funding_schedule.cli import MissingSlotMonitor

    state = tmp_path / "query-alert.json"
    repo = SimpleNamespace(slots_with_runs=AsyncMock(side_effect=[
        RuntimeError("secret"), RuntimeError("secret"), RuntimeError("secret"),
        set(), RuntimeError("secret"),
    ]))
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    start = SLOT + timedelta(days=1)  # No missing-slot messages during recovery.

    async def query_at(now):
        monitor = MissingSlotMonitor(repo, notifier, start_slot=start, alert_state_path=state)
        try:
            await monitor.check(now)
        except RuntimeError as exc:
            assert "secret" not in str(exc)

    await query_at(SLOT)
    assert state.exists()
    assert notifier.send.await_count == 1
    await query_at(SLOT + timedelta(minutes=1))
    assert notifier.send.await_count == 1  # Restart did not re-alert.
    await query_at(SLOT + timedelta(minutes=15))
    assert notifier.send.await_count == 2  # Cooldown expired.
    await query_at(SLOT + timedelta(minutes=16))
    assert not state.exists()  # A successful DB check clears outage state.
    await query_at(SLOT + timedelta(minutes=17))
    assert notifier.send.await_count == 3


@pytest.mark.asyncio
async def test_db_failure_unsent_alert_retries_after_one_minute_not_each_restart(tmp_path):
    from ai_tuner.funding_schedule.cli import MissingSlotMonitor

    state = tmp_path / "alerts.json"
    repo = SimpleNamespace(slots_with_runs=AsyncMock(side_effect=RuntimeError("secret")))
    notifier = SimpleNamespace(send=AsyncMock(side_effect=[False, True]))
    for seconds, expected_calls in ((0, 1), (30, 1), (60, 2), (90, 2)):
        monitor = MissingSlotMonitor(repo, notifier, start_slot=SLOT, alert_state_path=state)
        with pytest.raises(RuntimeError, match="monitor database unavailable"):
            await monitor.check(SLOT + timedelta(seconds=seconds))
        assert notifier.send.await_count == expected_calls


@pytest.mark.asyncio
async def test_startup_db_failure_uses_same_persistent_attempt_and_delivery_cooldowns(tmp_path):
    from ai_tuner.funding_schedule.cli import _startup_alert

    state = tmp_path / "alerts.json"
    notifier = SimpleNamespace(send=AsyncMock(side_effect=[False, True]))
    for seconds, expected_calls in ((0, 1), (30, 1), (60, 2), (90, 2)):
        await _startup_alert(notifier, "TimeoutError", alert_state_path=state,
                             now=SLOT + timedelta(seconds=seconds))
        assert notifier.send.await_count == expected_calls


@pytest.mark.asyncio
async def test_startup_failure_cooldown_is_not_cleared_until_monitor_query_recovers(tmp_path, monkeypatch):
    from ai_tuner.funding_schedule import cli

    state = tmp_path / "alerts.json"
    monkeypatch.setattr(cli, "ALERT_STATE_PATH", state)
    repo = SimpleNamespace(recent_failed_streak=AsyncMock(side_effect=RuntimeError("db secret")))
    collector = SimpleNamespace(repository=repo)
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="startup state unavailable"):
            await cli.run_daemon(collector, notifier)
    assert notifier.send.await_count == 1

    after = datetime.now(timezone.utc)
    recovered_repo = SimpleNamespace(slots_with_runs=AsyncMock(return_value=set()))
    monitor = cli.MissingSlotMonitor(recovered_repo, notifier, start_slot=after + timedelta(days=1),
                                     alert_state_path=state)
    await monitor.check(after + timedelta(minutes=3))
    await cli._startup_alert(notifier, "TimeoutError", alert_state_path=state,
                             now=after + timedelta(minutes=4))
    assert notifier.send.await_count == 2


@pytest.mark.asyncio
async def test_db_failure_still_exits_when_alert_state_cannot_be_written(tmp_path):
    from ai_tuner.funding_schedule.cli import MissingSlotMonitor

    invalid_path = tmp_path / "absent-directory" / "query-alert.json"
    repo = SimpleNamespace(slots_with_runs=AsyncMock(side_effect=RuntimeError("secret")))
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    monitor = MissingSlotMonitor(repo, notifier, start_slot=SLOT, alert_state_path=invalid_path)
    with pytest.raises(RuntimeError, match="alert state unavailable"):
        await monitor.check(SLOT + timedelta(minutes=3))
    notifier.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_slot_state_failure_exits_without_heartbeat_or_external_alert(tmp_path):
    from ai_tuner.funding_schedule.cli import MissingSlotMonitor, _monitor_loop

    invalid_path = tmp_path / "absent-directory" / "alerts.json"
    heartbeat = tmp_path / "heartbeat"
    repo = SimpleNamespace(slots_with_runs=AsyncMock(return_value=set()))
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    monitor = MissingSlotMonitor(repo, notifier, start_slot=SLOT, alert_state_path=invalid_path)
    with pytest.raises(RuntimeError, match="alert state unavailable"):
        await monitor.check(SLOT + timedelta(minutes=3))
    notifier.send.assert_not_awaited()
    assert not heartbeat.exists()


@pytest.mark.asyncio
async def test_empty_successful_query_cannot_bootstrap_health_without_writable_state_dir(tmp_path):
    from ai_tuner.funding_schedule.cli import MissingSlotMonitor, _monitor_loop

    heartbeat = tmp_path / "heartbeat"
    repo = SimpleNamespace(slots_with_runs=AsyncMock(return_value=set()))
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    monitor = MissingSlotMonitor(repo, notifier, start_slot=SLOT + timedelta(days=1),
                                 alert_state_path=tmp_path / "absent-directory" / "alerts.json")
    with pytest.raises(RuntimeError, match="alert state unavailable"):
        await asyncio.wait_for(_monitor_loop(monitor, heartbeat_path=heartbeat), 0.2)
    assert not heartbeat.exists()
    notifier.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_alert_state_fails_closed_before_notifier(tmp_path):
    from ai_tuner.funding_schedule.cli import MissingSlotMonitor

    state = tmp_path / "alerts.json"
    state.write_text("{bad-json", encoding="utf-8")
    repo = SimpleNamespace(slots_with_runs=AsyncMock(return_value=set()))
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    monitor = MissingSlotMonitor(repo, notifier, start_slot=SLOT, alert_state_path=state)
    with pytest.raises(RuntimeError, match="alert state unavailable"):
        await monitor.check(SLOT + timedelta(minutes=3))
    notifier.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_db_query_cannot_refresh_heartbeat_when_state_clear_fails(tmp_path):
    from ai_tuner.funding_schedule.cli import MissingSlotMonitor, _monitor_loop

    heartbeat = tmp_path / "heartbeat"
    repo = SimpleNamespace(slots_with_runs=AsyncMock(return_value=set()))
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    monitor = MissingSlotMonitor(repo, notifier, start_slot=SLOT + timedelta(days=1),
                                 alert_state_path=tmp_path / "alerts.json")
    monitor.alert_state.clear = MagicMock(side_effect=PermissionError("secret"))
    with pytest.raises(RuntimeError, match="alert state unavailable") as raised:
        await asyncio.wait_for(_monitor_loop(monitor, heartbeat_path=heartbeat), 0.2)
    assert "secret" not in str(raised.value)
    assert not heartbeat.exists()
    notifier.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_notification_timeout_cannot_block_next_check(tmp_path, monkeypatch):
    from ai_tuner.funding_schedule import cli

    async def hanging_send(*_, **__):
        await asyncio.Future()

    repo = SimpleNamespace(slots_with_runs=AsyncMock(return_value=set()))
    notifier = SimpleNamespace(send=AsyncMock(side_effect=hanging_send))
    monitor = cli.MissingSlotMonitor(repo, notifier, start_slot=SLOT,
                                     alert_state_path=tmp_path / "alerts.json")
    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable, timeout):
        assert timeout in (10, 15)
        return await real_wait_for(awaitable, 0.01 if timeout == 10 else timeout)

    monkeypatch.setattr(cli.asyncio, "wait_for", short_wait_for)
    await real_wait_for(monitor.check(SLOT + timedelta(minutes=3)), 0.2)
    await real_wait_for(monitor.check(SLOT + timedelta(minutes=3, seconds=30)), 0.2)
    assert notifier.send.await_count == 1
    assert repo.slots_with_runs.await_count == 2


@pytest.mark.asyncio
async def test_monitor_heartbeat_only_after_successful_persisted_query(tmp_path, monkeypatch):
    from ai_tuner.funding_schedule import cli

    heartbeat = tmp_path / "heartbeat"
    ok = SimpleNamespace(check=AsyncMock(return_value=None))

    async def stop_after_check(_):
        raise asyncio.CancelledError()

    monkeypatch.setattr(cli.asyncio, "sleep", stop_after_check)
    with pytest.raises(asyncio.CancelledError):
        await cli._monitor_loop(ok, heartbeat_path=heartbeat)
    assert heartbeat.exists()

    heartbeat.unlink()
    failed = SimpleNamespace(check=AsyncMock(side_effect=RuntimeError("database unavailable")))
    with pytest.raises(RuntimeError):
        await cli._monitor_loop(failed, heartbeat_path=heartbeat)
    assert not heartbeat.exists()


@pytest.mark.asyncio
async def test_db_connect_is_bounded_and_alerted(tmp_path, monkeypatch):
    from ai_tuner.funding_schedule import cli

    async def hanging_connect():
        await asyncio.Future()

    db = SimpleNamespace(connect=AsyncMock(side_effect=hanging_connect), disconnect=AsyncMock())
    binance = SimpleNamespace(close=AsyncMock())
    notifier = SimpleNamespace(send=AsyncMock(return_value=True), close=AsyncMock())
    monkeypatch.setattr(cli, "_runtime", lambda: (db, binance, notifier, MagicMock()))
    monkeypatch.setattr(cli, "ALERT_STATE_PATH", tmp_path / "alerts.json")
    real_wait_for = asyncio.wait_for

    async def capture_wait_for(awaitable, timeout):
        assert timeout in (10, 15)
        return await real_wait_for(awaitable, 0.01 if timeout == 15 else timeout)

    monkeypatch.setattr(cli.asyncio, "wait_for", capture_wait_for)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await real_wait_for(cli._run("daemon"), 0.2)
    notifier.send.assert_awaited_once()
    assert "TimeoutError" in notifier.send.await_args.args[0]


@pytest.mark.asyncio
async def test_daemon_clears_previous_process_heartbeat_before_connect(tmp_path, monkeypatch):
    from ai_tuner.funding_schedule import cli

    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text("9999999999", encoding="ascii")

    async def check_cleared_then_fail():
        assert not heartbeat.exists()
        raise RuntimeError("unavailable")

    db = SimpleNamespace(connect=check_cleared_then_fail, disconnect=AsyncMock())
    notifier = SimpleNamespace(send=AsyncMock(return_value=True), close=AsyncMock())
    monkeypatch.setattr(cli, "HEARTBEAT_PATH", heartbeat)
    monkeypatch.setattr(cli, "ALERT_STATE_PATH", tmp_path / "alerts.json")
    monkeypatch.setattr(cli, "_runtime", lambda: (db, SimpleNamespace(close=AsyncMock()), notifier, MagicMock()))
    with pytest.raises(RuntimeError, match="database unavailable"):
        await cli._run("daemon")
    assert not heartbeat.exists()


@pytest.mark.asyncio
async def test_consecutive_failures_alert_at_threshold_without_secret_leak(tmp_path):
    from ai_tuner.funding_schedule.cli import FailureTracker

    notifier = SimpleNamespace(send=AsyncMock())
    tracker = FailureTracker(notifier, threshold=2, alert_state_path=tmp_path / "alerts.json")
    failure = SimpleNamespace(status="failed", failure_code="FETCH_FAILED", symbol_count=0)
    await tracker.observe(failure, SLOT)
    assert notifier.send.await_count == 0
    await tracker.observe(failure, SLOT + timedelta(minutes=5))
    assert notifier.send.await_count == 1
    assert "FETCH_FAILED" in notifier.send.await_args.args[0]
    await tracker.observe(failure, SLOT + timedelta(minutes=10))
    assert notifier.send.await_count == 1
    await tracker.observe(SimpleNamespace(status="success", failure_code=None, symbol_count=2), SLOT)
    await tracker.observe(failure, SLOT)
    assert notifier.send.await_count == 1


@pytest.mark.asyncio
async def test_failed_notification_is_retried_on_next_failed_slot(tmp_path):
    from ai_tuner.funding_schedule.cli import FailureTracker

    notifier = SimpleNamespace(send=AsyncMock(side_effect=[False, True]))
    clock = [SLOT + timedelta(minutes=5)]
    tracker = FailureTracker(notifier, threshold=2, alert_state_path=tmp_path / "alerts.json",
                             clock=lambda: clock[0])
    failure = SimpleNamespace(status="failed", failure_code="FETCH_FAILED", symbol_count=0)
    await tracker.observe(failure, SLOT)
    await tracker.observe(failure, SLOT + timedelta(minutes=5))
    clock[0] = SLOT + timedelta(minutes=10)
    await tracker.observe(failure, SLOT + timedelta(minutes=10))
    assert notifier.send.await_count == 2


@pytest.mark.asyncio
async def test_persisted_collection_streak_does_not_realert_each_restart(tmp_path):
    from ai_tuner.funding_schedule.cli import FailureTracker

    state = tmp_path / "alerts.json"
    notifier = SimpleNamespace(send=AsyncMock(side_effect=[False, True]))
    failure = SimpleNamespace(status="failed", failure_code="FETCH_FAILED", symbol_count=0)
    clock = [SLOT + timedelta(minutes=5)]
    first = FailureTracker(notifier, threshold=2, alert_state_path=state, clock=lambda: clock[0])
    await first.observe(failure, SLOT)
    await first.observe(failure, SLOT + timedelta(minutes=5))
    assert notifier.send.await_count == 1

    clock[0] = SLOT + timedelta(minutes=5, seconds=30)
    restarted = FailureTracker(notifier, threshold=2, alert_state_path=state, clock=lambda: clock[0])
    restarted.consecutive = 2
    await restarted.alert_persisted_streak(SLOT + timedelta(minutes=5, seconds=30))
    assert notifier.send.await_count == 1
    clock[0] = SLOT + timedelta(minutes=6)
    await restarted.alert_persisted_streak(SLOT + timedelta(minutes=6))
    assert notifier.send.await_count == 2

    restarted_again = FailureTracker(notifier, threshold=2, alert_state_path=state,
                                     clock=lambda: SLOT + timedelta(minutes=10))
    restarted_again.consecutive = 2
    await restarted_again.alert_persisted_streak(SLOT + timedelta(minutes=10))
    assert notifier.send.await_count == 2
    await restarted_again.observe(SimpleNamespace(status="success", failure_code=None, symbol_count=2), SLOT)
    assert not state.exists()


@pytest.mark.asyncio
async def test_collection_streak_cooldown_starts_at_actual_send_not_old_slot(tmp_path):
    from ai_tuner.funding_schedule.cli import FailureTracker

    state = tmp_path / "alerts.json"
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    failure = SimpleNamespace(status="failed", failure_code="FETCH_FAILED", symbol_count=0)
    sent_at = SLOT + timedelta(minutes=20)
    tracker = FailureTracker(notifier, threshold=1, alert_state_path=state, clock=lambda: sent_at)
    await tracker.observe(failure, SLOT)
    assert notifier.send.await_count == 1
    assert SLOT.isoformat() in notifier.send.await_args.args[0]

    restarted = FailureTracker(notifier, threshold=1, alert_state_path=state,
                               clock=lambda: sent_at + timedelta(minutes=14))
    restarted.consecutive = 1
    await restarted.alert_persisted_streak(SLOT + timedelta(minutes=5))
    assert notifier.send.await_count == 1

    after_cooldown = FailureTracker(notifier, threshold=1, alert_state_path=state,
                                    clock=lambda: sent_at + timedelta(minutes=15))
    after_cooldown.consecutive = 1
    await after_cooldown.alert_persisted_streak(SLOT + timedelta(minutes=10))
    assert notifier.send.await_count == 2


def test_alert_state_prunes_old_missing_slots_but_keeps_failure_events(tmp_path):
    from ai_tuner.funding_schedule.alert_state import PersistentAlertState

    path = tmp_path / "alerts.json"
    state = PersistentAlertState(path)
    old_slot = "MISSING_SCHEDULE_SLOT:2026-10-30T05:00:00+00:00"
    recent_slot = "MISSING_SCHEDULE_SLOT:2026-10-31T06:00:00+00:00"
    state.mark_attempt(old_slot, SLOT)
    state.mark_delivered(old_slot, SLOT)
    state.mark_attempt("RUN_QUERY_FAILED", SLOT)
    state.mark_attempt(recent_slot, SLOT + timedelta(hours=25))
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert old_slot not in stored
    assert recent_slot in stored
    assert "RUN_QUERY_FAILED" in stored


def test_compose_service_has_isolated_secret_and_no_trading_credentials():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["ai-tuner-funding-schedule"]
    assert service["command"] == ["python3", "-m", "ai_tuner.funding_schedule.cli", "daemon"]
    assert service["env_file"] == [".secrets/ai_tuner_funding_schedule.env"]
    assert service["restart"] == "unless-stopped"
    assert service["profiles"] == ["funding-schedule"]
    env = "\n".join(service["environment"])
    assert "BINANCE_API_KEY" not in env
    assert "BINANCE_API_SECRET" not in env
    assert "DB_USER=" not in env
    assert "DB_PASSWORD=" not in env
    assert "DEEPSEEK_API_KEY" not in env
    assert "BINANCE_TESTNET" not in env
    assert service["healthcheck"]["test"]
    assert service["healthcheck"]["test"] == [
        "CMD", "python3", "-m", "ai_tuner.funding_schedule.health", "check",
    ]
    assert service["deploy"]["resources"]["limits"]["memory"]


def test_image_and_deploy_script_include_separate_runtime_boundary():
    dockerfile = (ROOT / "ai_tuner" / "Dockerfile").read_text(encoding="utf-8")
    runtime = (ROOT / "scripts" / "remote_deploy_runtime.sh").read_text(encoding="utf-8")
    assert 'import ai_tuner.funding_schedule.cli' in dockerfile
    assert "FUNDING_SECRET_FILE=.secrets/ai_tuner_funding_schedule.env" in runtime
    assert runtime.index("FUNDING_SECRET_FILE=.secrets/ai_tuner_funding_schedule.env") < runtime.index(
        'if [ "$DEPLOY_MODE" = "calibrated-first-create" ]'
    )
    assert "chmod 600 \"$FUNDING_SECRET_FILE\"" in runtime
    assert "mkdir -p logs/ai_tuner_funding_schedule" in runtime
    assert "chown 1000:1000 logs/ai_tuner_funding_schedule" in runtime
    assert runtime.index("chown 1000:1000 logs/ai_tuner_funding_schedule") < runtime.index(
        "docker compose --profile funding-schedule up -d --no-deps ai-tuner-funding-schedule"
    )
    assert "ALTER ROLE ai_tuner_funding_writer LOGIN PASSWORD :'funding_password'" in runtime
    assert "FUNDING_PERMISSION_BOUNDARY_OK" in runtime
    gate = runtime.index('if [ "$FUNDING_PERMISSION_BOUNDARY_OK" != "1" ]; then')
    assert runtime.index("--parallel 1 up -d --remove-orphans") < gate
    assert 'if [ "$FUNDING_DEPLOY_ACTION" = "keep" ]; then' in runtime
    assert '--parallel 1 up -d --no-deps "${KEEP_SERVICES[@]}"' in runtime
    # Clear inherited profiles before any one-off Compose call; only stop the
    # existing collector on the normal deployment path after one-off modes exit.
    assert runtime.index("export COMPOSE_PROFILES=") < runtime.index(
        "docker compose pull mtcps-xau-calibrated-shadow"
    )
    normal_branch = runtime.index("# allocation-reconcile end")
    first_normal_compose = runtime.index("PROJECT_IDS=$(docker compose ps", normal_branch)
    assert normal_branch < runtime.index('if [ "$FUNDING_DEPLOY_ACTION" = "keep" ]; then') < first_normal_compose
    assert normal_branch < runtime.index("\n  stop_funding_collector_if_running\n") < first_normal_compose
    assert runtime.index("docker compose --profile funding-schedule up -d --no-deps ai-tuner-funding-schedule") > gate
    assert "docker compose restart ai-tuner-funding-schedule" not in runtime
    assert "docker stop ai-tuner-funding-schedule" in runtime
    assert "pg_auth_members" in runtime
    assert "n.nspname NOT IN ('pg_catalog','information_schema')" in runtime
    assert "has_database_privilege('ai_tuner_funding_writer'" in runtime
    assert "has_sequence_privilege('ai_tuner_funding_writer'" in runtime
    assert "has_column_privilege('ai_tuner_funding_writer'" in runtime
    assert "'REFERENCES,TRIGGER'" in runtime
    assert "ai-tuner-funding-schedule" in runtime
    for table in ("ai_tuner_funding_schedule_runs", "ai_tuner_funding_schedule_observations"):
        assert table in runtime
    for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
        assert f"NOT has_table_privilege('ai_tuner_funding_writer','public.ai_tuner_funding_schedule_runs','{privilege}')" in runtime


def test_funding_acl_probe_only_calls_sequence_privilege_for_sequences():
    """PostgreSQL may reorder WHERE clauses and visit TOAST relations first."""
    runtime = (ROOT / "scripts" / "remote_deploy_runtime.sh").read_text(encoding="utf-8")
    sequence_scan = runtime.split(
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace", 1
    )[1].split("AND NOT EXISTS (", 1)[0]
    assert re.search(
        r"CASE\s+WHEN\s+c\.relkind\s*=\s*'S'\s+THEN\s+"
        r"has_sequence_privilege\([^)]*\)\s+ELSE\s+FALSE\s+END",
        sequence_scan,
        flags=re.IGNORECASE,
    )


@pytest.mark.asyncio
async def test_db_connect_failure_sends_immediate_sanitized_alert(tmp_path, monkeypatch):
    from ai_tuner.funding_schedule import cli

    secret = "secret-value-should-not-leak"
    db = SimpleNamespace(connect=AsyncMock(side_effect=RuntimeError(secret)), disconnect=AsyncMock())
    binance = SimpleNamespace(close=AsyncMock())
    notifier = SimpleNamespace(send=AsyncMock(return_value=True), close=AsyncMock())
    monkeypatch.setattr(cli, "_runtime", lambda: (db, binance, notifier, MagicMock()))
    monkeypatch.setattr(cli, "ALERT_STATE_PATH", tmp_path / "alerts.json")
    with pytest.raises(RuntimeError) as raised:
        await cli._run("daemon")
    assert secret not in str(raised.value)
    notifier.send.assert_awaited_once()
    assert secret not in notifier.send.await_args.args[0]
    notifier.close.assert_awaited_once()
    binance.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_daemon_restores_failed_slots_before_next_collection(tmp_path, monkeypatch):
    from ai_tuner.funding_schedule import cli

    repo = SimpleNamespace(recent_failed_streak=AsyncMock(return_value=2),
                           latest_run_slot=AsyncMock(return_value=None),
                           slots_with_runs=AsyncMock(return_value={SLOT}))
    collector = SimpleNamespace(repository=repo, collect_once=AsyncMock(return_value=SimpleNamespace(
        status="failed", failure_code="FETCH_FAILED", symbol_count=0,
    )))
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    monkeypatch.setattr(cli, "ALERT_STATE_PATH", tmp_path / "alerts.json")

    async def stop_after_alert(_):
        if notifier.send.await_count:
            raise asyncio.CancelledError()

    monkeypatch.setattr(cli.asyncio, "sleep", stop_after_alert)
    with pytest.raises(asyncio.CancelledError):
        await cli.run_daemon(collector, notifier)
    repo.recent_failed_streak.assert_awaited_once()
    notifier.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_streak_restore_sends_sanitized_startup_alert(tmp_path, monkeypatch):
    from ai_tuner.funding_schedule import cli

    repo = SimpleNamespace(recent_failed_streak=AsyncMock(
        side_effect=RuntimeError("password=do-not-log"),
    ))
    collector = SimpleNamespace(repository=repo)
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    monkeypatch.setattr(cli, "ALERT_STATE_PATH", tmp_path / "alerts.json")
    with pytest.raises(RuntimeError) as raised:
        await cli.run_daemon(collector, notifier)
    assert "do-not-log" not in str(raised.value)
    notifier.send.assert_awaited_once()
    assert "do-not-log" not in notifier.send.await_args.args[0]


@pytest.mark.asyncio
async def test_runtime_build_failure_alerts_when_webhook_is_available(tmp_path, monkeypatch):
    from ai_tuner.funding_schedule import cli

    notifier = SimpleNamespace(send=AsyncMock(return_value=True), close=AsyncMock())
    monkeypatch.setenv("FEISHU_WEBHOOK_TUNER", "test-webhook")
    monkeypatch.setattr(cli, "_runtime", lambda: (_ for _ in ()).throw(RuntimeError("secret")))
    monkeypatch.setattr(cli, "NotificationClient", lambda **kwargs: notifier)
    monkeypatch.setattr(cli, "ALERT_STATE_PATH", tmp_path / "alerts.json")
    with pytest.raises(RuntimeError) as raised:
        await cli._run("daemon")
    assert "secret" not in str(raised.value)
    notifier.send.assert_awaited_once()
    assert "secret" not in notifier.send.await_args.args[0]
    notifier.close.assert_awaited_once()


def test_post_deploy_readiness_gate_does_not_equate_liveness_with_collection():
    doc = (ROOT / "docs/deployment/ai-tuner-funding-schedule-readiness.md").read_text(encoding="utf-8")
    assert "3" in doc and "scheduled" in doc.lower()
    assert "180 秒" in doc and "健康态" in doc
    assert "告警" in doc


def test_health_requires_fresh_successful_monitor_heartbeat(tmp_path):
    from ai_tuner.funding_schedule.health import is_monitor_fresh

    heartbeat = tmp_path / "heartbeat"
    assert not is_monitor_fresh(now=1000, path=heartbeat)
    heartbeat.write_text("900", encoding="ascii")
    assert is_monitor_fresh(now=1000, path=heartbeat)
    assert not is_monitor_fresh(now=1100, path=heartbeat, max_age_seconds=180)
    heartbeat.write_text("not-a-timestamp", encoding="ascii")
    assert not is_monitor_fresh(now=1000, path=heartbeat)
