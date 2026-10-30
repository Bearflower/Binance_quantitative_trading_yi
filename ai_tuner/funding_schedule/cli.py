"""Run prospective funding-time capture, never backfill or replay."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from uuid import uuid4

import structlog

from shared.database import DatabaseManager
from shared.notification import NotificationClient
from shared.readonly_client import ReadOnlyBinanceClient

from .alert_state import ALERT_STATE_PATH, DELIVERED_FAILURE_COOLDOWN, PersistentAlertState
from .collector import CollectionResult, FundingScheduleCollector
from .health import HEARTBEAT_PATH, clear_monitor_heartbeat, write_monitor_heartbeat
from .repository import FundingScheduleRepository


logger = structlog.get_logger()
_PERIOD = timedelta(minutes=5)
_COLLECTION_TIMEOUT_SECONDS = 90
_DB_TIMEOUT_SECONDS = 15
_NOTIFICATION_TIMEOUT_SECONDS = 10
_MONITOR_GRACE = timedelta(seconds=10)
_MONITOR_INTERVAL_SECONDS = 30


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prospective funding schedule capture")
    parser.add_argument("command", choices=("once", "daemon"))
    return parser


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("UTC-aware timestamp required")
    return value


def current_slot(now: datetime) -> datetime:
    now = _utc(now)
    return now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)


def next_slot_after(now: datetime, *, last_slot: datetime | None = None) -> datetime:
    """Find a future wall-clock slot; never replay slots missed while offline."""
    now = _utc(now)
    floor = current_slot(now)
    next_slot = floor if now == floor else floor + _PERIOD
    if last_slot is not None:
        next_slot = max(next_slot, _utc(last_slot) + _PERIOD)
    return next_slot


def monitor_start_slot(first_slot: datetime, last_persisted: datetime | None,
                       now: datetime) -> datetime:
    """Do not invent pre-deployment gaps; restart audits recent closed slots."""
    if last_persisted is None:
        return _utc(first_slot)
    _utc(last_persisted)
    return current_slot(_utc(now) - 3 * _PERIOD)


async def run_once(collector: FundingScheduleCollector, notifier: NotificationClient,
                   *, scheduled_at: datetime) -> CollectionResult:
    # Do not log exception text: HTTP/DB exceptions can contain credentials.
    try:
        result = await asyncio.wait_for(
            collector.collect_once(scheduled_at, uuid4()),
            timeout=_COLLECTION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("资金费日程采集超时", slot=scheduled_at.isoformat())
        # wait_for cancels the entire collector, including any DB transaction.
        # No success or partial observation is fabricated; the monitor will
        # detect a missing persisted run if cancellation interrupted the write.
        return CollectionResult("failed", "COLLECTION_TIMEOUT", 0)
    except Exception as exc:
        logger.error("资金费日程采集异常", slot=scheduled_at.isoformat(), error_type=type(exc).__name__)
        return CollectionResult("failed", "RUNTIME_FAILURE", 0)
    logger.info("资金费日程采集完成", slot=scheduled_at.isoformat(), status=result.status,
                failure_code=result.failure_code, symbols=result.symbol_count)
    return result


class FailureTracker:
    def __init__(self, notifier: NotificationClient, *, threshold: int = 3,
                 alert_state_path: Path = ALERT_STATE_PATH) -> None:
        if threshold < 1:
            raise ValueError("threshold must be positive")
        self.notifier = notifier
        self.threshold = threshold
        self.consecutive = 0
        self.alerted = False
        self.alert_state = PersistentAlertState(alert_state_path)

    async def observe(self, result: CollectionResult, slot: datetime) -> None:
        if result.status == "success":
            self.consecutive = 0
            self.alerted = False
            try:
                self.alert_state.clear("COLLECTION_STREAK")
            except OSError as state_error:
                logger.error("资金费日程连续失败告警状态清理失败", error_type=type(state_error).__name__)
            return
        self.consecutive += 1
        if self.consecutive < self.threshold or self.alerted:
            return
        await self._send_alert(slot, result.failure_code)

    async def alert_persisted_streak(self, slot: datetime) -> None:
        if self.consecutive >= self.threshold and not self.alerted:
            await self._send_alert(slot, "PERSISTED_FAILED_STREAK")

    async def _send_alert(self, slot: datetime, failure_code: str | None) -> None:
        self.alerted = await _send_limited_alert(
            self.notifier, self.alert_state, "COLLECTION_STREAK",
            f"AI Tuner 资金费日程采集连续 {self.threshold} 个槽位失败；"
            f"最近槽位 {slot.isoformat()}，失败代码 {failure_code}",
            now=_utc(slot), delivered_cooldown=DELIVERED_FAILURE_COOLDOWN,
        )


class MissingSlotMonitor:
    """Check persisted run evidence independently from the collection loop."""

    def __init__(self, repository: FundingScheduleRepository, notifier: NotificationClient,
                 *, start_slot: datetime,
                 alert_state_path: Path = ALERT_STATE_PATH) -> None:
        self.repository = repository
        self.notifier = notifier
        self.start_slot = _utc(start_slot)
        self.alert_state = PersistentAlertState(alert_state_path)

    async def check(self, now: datetime) -> None:
        closed = current_slot(_utc(now) - timedelta(seconds=120) - _MONITOR_GRACE)
        first = max(self.start_slot, closed - 3 * _PERIOD)
        try:
            # Even an empty interval verifies the DB is reachable before a
            # bootstrap heartbeat is written by the loop.
            persisted = await asyncio.wait_for(
                self.repository.slots_with_runs(first, closed),
                timeout=_DB_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.error("资金费日程缺槽查询失败", error_type=type(exc).__name__)
            await _send_limited_alert(
                self.notifier, self.alert_state, "RUN_QUERY_FAILED",
                "AI Tuner 资金费日程监视告警：RUN_QUERY_FAILED", now=now,
                delivered_cooldown=DELIVERED_FAILURE_COOLDOWN,
            )
            raise RuntimeError("funding schedule monitor database unavailable") from None
        try:
            self.alert_state.clear("RUN_QUERY_FAILED")
            self.alert_state.clear("STARTUP_DB_UNAVAILABLE")
        except OSError as state_error:
            logger.error("资金费日程告警限频状态清理失败", error_type=type(state_error).__name__)
        if first > closed:
            return
        slot = first
        while slot <= closed:
            if slot not in persisted:
                await _send_limited_alert(
                    self.notifier, self.alert_state,
                    f"MISSING_SCHEDULE_SLOT:{slot.isoformat()}",
                    f"AI Tuner 资金费日程监视告警：MISSING_SCHEDULE_SLOT；槽位 {slot.isoformat()}",
                    now=now, delivered_cooldown=None,
                )
            slot += _PERIOD


async def _send_limited_alert(notifier: NotificationClient, state: PersistentAlertState,
                              key: str, message: str, *, now: datetime,
                              delivered_cooldown: timedelta | None) -> bool:
    if not state.should_attempt(key, now, delivered_cooldown=delivered_cooldown):
        return False
    try:
        state.mark_attempt(key, now)
    except OSError as state_error:
        logger.error("资金费日程告警限频状态写入失败", error_type=type(state_error).__name__)
    try:
        sent = await asyncio.wait_for(
            notifier.send(message, level="error", project="tuner"),
            timeout=_NOTIFICATION_TIMEOUT_SECONDS,
        )
        if not sent:
            logger.error("资金费日程告警未送达", key=key)
            return False
        try:
            state.mark_delivered(key, now)
        except OSError as state_error:
            logger.error("资金费日程告警送达状态写入失败", error_type=type(state_error).__name__)
        return True
    except Exception as exc:
        logger.error("资金费日程告警发送失败", key=key, error_type=type(exc).__name__)
        return False


async def _collection_loop(collector: FundingScheduleCollector, notifier: NotificationClient,
                           tracker: FailureTracker) -> None:
    last_slot = None
    while True:
        slot = next_slot_after(datetime.now(timezone.utc), last_slot=last_slot)
        await asyncio.sleep(max(0.0, (slot - datetime.now(timezone.utc)).total_seconds()))
        last_slot = slot
        result = await run_once(collector, notifier, scheduled_at=slot)
        await tracker.observe(result, slot)


async def _monitor_loop(monitor: MissingSlotMonitor, *, heartbeat_path: Path | None = None) -> None:
    heartbeat_path = HEARTBEAT_PATH if heartbeat_path is None else heartbeat_path
    while True:
        await monitor.check(datetime.now(timezone.utc))
        write_monitor_heartbeat(path=heartbeat_path)
        await asyncio.sleep(_MONITOR_INTERVAL_SECONDS)


async def run_daemon(collector: FundingScheduleCollector, notifier: NotificationClient) -> None:
    tracker = FailureTracker(notifier)
    first_slot = next_slot_after(datetime.now(timezone.utc))
    try:
        tracker.consecutive = await asyncio.wait_for(
            collector.repository.recent_failed_streak(first_slot, limit=tracker.threshold),
            timeout=_DB_TIMEOUT_SECONDS,
        )
        last_persisted = await asyncio.wait_for(
            collector.repository.latest_run_slot(), timeout=_DB_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.error("资金费日程失败计数恢复失败", error_type=type(exc).__name__)
        await _startup_alert(notifier, type(exc).__name__)
        raise RuntimeError("funding schedule startup state unavailable") from None
    await tracker.alert_persisted_streak(first_slot - _PERIOD)
    # First deployment has no prior evidence and must not label pre-deployment
    # slots missing. A restart scans the most recent closed slots immediately.
    monitor_start = monitor_start_slot(first_slot, last_persisted, datetime.now(timezone.utc))
    monitor = MissingSlotMonitor(collector.repository, notifier, start_slot=monitor_start)
    tasks = [
        asyncio.create_task(_collection_loop(collector, notifier, tracker)),
        asyncio.create_task(_monitor_loop(monitor)),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exception = task.exception()
            if exception is not None:
                raise exception
        raise RuntimeError("funding schedule daemon task stopped")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _runtime():
    if not os.environ.get("FEISHU_WEBHOOK_TUNER"):
        raise RuntimeError("FEISHU_WEBHOOK_TUNER is required for failure alerts")
    db = DatabaseManager(
        host=os.getenv("DB_HOST", "postgres"), port=int(os.getenv("DB_PORT", "5432")),
        database=os.environ["DB_NAME"], user=os.environ["AI_TUNER_FUNDING_DB_USER"],
        password=os.environ["AI_TUNER_FUNDING_DB_PASSWORD"],
        min_pool_size=1, max_pool_size=3,
    )
    binance = ReadOnlyBinanceClient(
        api_key="public-market-data", api_secret="public-market-data",
        testnet=False,
        use_unified_account=False, instance_id="ai-tuner-funding-schedule",
    )
    notifier = NotificationClient(
        service_url=os.getenv("NOTIFICATION_SERVICE_URL", ""), use_direct_webhook=True,
    )
    return db, binance, notifier, FundingScheduleCollector(FundingScheduleRepository(db), binance)


async def _startup_alert(notifier: NotificationClient, error_type: str,
                         *, alert_state_path: Path | None = None,
                         now: datetime | None = None) -> None:
    """Notify without emitting exception text, SQL DSNs, or credentials."""
    state = PersistentAlertState(ALERT_STATE_PATH if alert_state_path is None else alert_state_path)
    await _send_limited_alert(
        notifier, state, "STARTUP_DB_UNAVAILABLE",
        f"AI Tuner 资金费日程采集启动失败（{error_type}）；采集器未就绪，请检查容器与数据库。",
        now=datetime.now(timezone.utc) if now is None else _utc(now),
        delivered_cooldown=DELIVERED_FAILURE_COOLDOWN,
    )


async def _run(command: str) -> int:
    if command == "daemon":
        clear_monitor_heartbeat(path=HEARTBEAT_PATH)
    try:
        db, binance, notifier, collector = _runtime()
    except Exception as exc:
        logger.error("资金费日程运行时初始化失败", error_type=type(exc).__name__)
        if os.environ.get("FEISHU_WEBHOOK_TUNER"):
            fallback = NotificationClient(
                service_url=os.getenv("NOTIFICATION_SERVICE_URL", ""), use_direct_webhook=True,
            )
            try:
                await _startup_alert(fallback, type(exc).__name__)
            finally:
                await fallback.close()
        raise RuntimeError("funding schedule runtime initialization failed") from None
    try:
        try:
            await asyncio.wait_for(db.connect(), timeout=_DB_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.error("资金费日程数据库连接失败", error_type=type(exc).__name__)
            await _startup_alert(notifier, type(exc).__name__)
            raise RuntimeError("funding schedule database unavailable") from None
        if command == "once":
            result = await run_once(collector, notifier, scheduled_at=current_slot(datetime.now(timezone.utc)))
            return 0 if result.status == "success" else 1
        await run_daemon(collector, notifier)
        return 0
    finally:
        await notifier.close()
        await binance.close()
        await db.disconnect()


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_run(args.command)))


if __name__ == "__main__":
    main()
