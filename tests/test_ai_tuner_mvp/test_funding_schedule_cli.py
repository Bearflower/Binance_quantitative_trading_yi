"""The prospective collector is an independent, least-privilege daemon."""

import asyncio

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
async def test_consecutive_failures_alert_at_threshold_without_secret_leak():
    from ai_tuner.funding_schedule.cli import FailureTracker

    notifier = SimpleNamespace(send=AsyncMock())
    tracker = FailureTracker(notifier, threshold=2)
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
async def test_failed_notification_is_retried_on_next_failed_slot():
    from ai_tuner.funding_schedule.cli import FailureTracker

    notifier = SimpleNamespace(send=AsyncMock(side_effect=[False, True]))
    tracker = FailureTracker(notifier, threshold=2)
    failure = SimpleNamespace(status="failed", failure_code="FETCH_FAILED", symbol_count=0)
    await tracker.observe(failure, SLOT)
    await tracker.observe(failure, SLOT + timedelta(minutes=5))
    await tracker.observe(failure, SLOT + timedelta(minutes=10))
    assert notifier.send.await_count == 2


def test_compose_service_has_isolated_secret_and_no_trading_credentials():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["ai-tuner-funding-schedule"]
    assert service["command"] == ["python3", "-m", "ai_tuner.funding_schedule.cli", "daemon"]
    assert service["env_file"] == [".secrets/ai_tuner_funding_schedule.env"]
    assert service["restart"] == "unless-stopped"
    env = "\n".join(service["environment"])
    assert "BINANCE_API_KEY" not in env
    assert "BINANCE_API_SECRET" not in env
    assert "DB_USER=" not in env
    assert "DB_PASSWORD=" not in env
    assert "DEEPSEEK_API_KEY" not in env
    assert "BINANCE_TESTNET" not in env
    assert service["healthcheck"]["test"]
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
    assert "ALTER ROLE ai_tuner_funding_writer LOGIN PASSWORD :'funding_password'" in runtime
    assert "FUNDING_PERMISSION_BOUNDARY_OK" in runtime
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


@pytest.mark.asyncio
async def test_db_connect_failure_sends_immediate_sanitized_alert(monkeypatch):
    from ai_tuner.funding_schedule import cli

    secret = "secret-value-should-not-leak"
    db = SimpleNamespace(connect=AsyncMock(side_effect=RuntimeError(secret)), disconnect=AsyncMock())
    binance = SimpleNamespace(close=AsyncMock())
    notifier = SimpleNamespace(send=AsyncMock(return_value=True), close=AsyncMock())
    monkeypatch.setattr(cli, "_runtime", lambda: (db, binance, notifier, MagicMock()))
    with pytest.raises(RuntimeError) as raised:
        await cli._run("daemon")
    assert secret not in str(raised.value)
    notifier.send.assert_awaited_once()
    assert secret not in notifier.send.await_args.args[0]
    notifier.close.assert_awaited_once()
    binance.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_daemon_restores_failed_slots_before_next_collection(monkeypatch):
    from ai_tuner.funding_schedule import cli

    repo = SimpleNamespace(recent_failed_streak=AsyncMock(return_value=2))
    collector = SimpleNamespace(repository=repo, collect_once=AsyncMock(return_value=SimpleNamespace(
        status="failed", failure_code="FETCH_FAILED", symbol_count=0,
    )))
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))

    async def stop_after_alert(_):
        if notifier.send.await_count:
            raise asyncio.CancelledError()

    monkeypatch.setattr(cli.asyncio, "sleep", stop_after_alert)
    with pytest.raises(asyncio.CancelledError):
        await cli.run_daemon(collector, notifier)
    repo.recent_failed_streak.assert_awaited_once()
    notifier.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_streak_restore_sends_sanitized_startup_alert():
    from ai_tuner.funding_schedule.cli import run_daemon

    repo = SimpleNamespace(recent_failed_streak=AsyncMock(
        side_effect=RuntimeError("password=do-not-log"),
    ))
    collector = SimpleNamespace(repository=repo)
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    with pytest.raises(RuntimeError) as raised:
        await run_daemon(collector, notifier)
    assert "do-not-log" not in str(raised.value)
    notifier.send.assert_awaited_once()
    assert "do-not-log" not in notifier.send.await_args.args[0]


@pytest.mark.asyncio
async def test_runtime_build_failure_alerts_when_webhook_is_available(monkeypatch):
    from ai_tuner.funding_schedule import cli

    notifier = SimpleNamespace(send=AsyncMock(return_value=True), close=AsyncMock())
    monkeypatch.setenv("FEISHU_WEBHOOK_TUNER", "test-webhook")
    monkeypatch.setattr(cli, "_runtime", lambda: (_ for _ in ()).throw(RuntimeError("secret")))
    monkeypatch.setattr(cli, "NotificationClient", lambda **kwargs: notifier)
    with pytest.raises(RuntimeError) as raised:
        await cli._run("daemon")
    assert "secret" not in str(raised.value)
    notifier.send.assert_awaited_once()
    assert "secret" not in notifier.send.await_args.args[0]
    notifier.close.assert_awaited_once()


def test_post_deploy_readiness_gate_does_not_equate_liveness_with_collection():
    doc = (ROOT / "docs/deployment/ai-tuner-funding-schedule-readiness.md").read_text(encoding="utf-8")
    assert "3" in doc and "scheduled" in doc.lower()
    assert "liveness" in doc.lower()
    assert "告警" in doc
