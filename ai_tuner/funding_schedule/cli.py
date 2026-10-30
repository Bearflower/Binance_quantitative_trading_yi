"""Run prospective funding-time capture, never backfill or replay."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import structlog

from shared.database import DatabaseManager
from shared.notification import NotificationClient
from shared.readonly_client import ReadOnlyBinanceClient

from .collector import CollectionResult, FundingScheduleCollector
from .repository import FundingScheduleRepository


logger = structlog.get_logger()
_PERIOD = timedelta(minutes=5)


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


async def run_once(collector: FundingScheduleCollector, notifier: NotificationClient,
                   *, scheduled_at: datetime) -> CollectionResult:
    # Do not log exception text: HTTP/DB exceptions can contain credentials.
    try:
        result = await collector.collect_once(scheduled_at, uuid4())
    except Exception as exc:
        logger.error("资金费日程采集异常", slot=scheduled_at.isoformat(), error_type=type(exc).__name__)
        return CollectionResult("failed", "RUNTIME_FAILURE", 0)
    logger.info("资金费日程采集完成", slot=scheduled_at.isoformat(), status=result.status,
                failure_code=result.failure_code, symbols=result.symbol_count)
    return result


class FailureTracker:
    def __init__(self, notifier: NotificationClient, *, threshold: int = 3) -> None:
        if threshold < 1:
            raise ValueError("threshold must be positive")
        self.notifier = notifier
        self.threshold = threshold
        self.consecutive = 0
        self.alerted = False

    async def observe(self, result: CollectionResult, slot: datetime) -> None:
        if result.status == "success":
            self.consecutive = 0
            self.alerted = False
            return
        self.consecutive += 1
        if self.consecutive < self.threshold or self.alerted:
            return
        try:
            sent = await self.notifier.send(
                f"AI Tuner 资金费日程采集连续 {self.threshold} 个槽位失败；"
                f"最近槽位 {slot.isoformat()}，失败代码 {result.failure_code}",
                level="error", project="tuner",
            )
            self.alerted = bool(sent)
            if not self.alerted:
                logger.error("资金费日程告警未送达", slot=slot.isoformat())
        except Exception as exc:
            logger.error("资金费日程告警发送失败", error_type=type(exc).__name__)


async def run_daemon(collector: FundingScheduleCollector, notifier: NotificationClient) -> None:
    tracker = FailureTracker(notifier)
    last_slot = None
    while True:
        slot = next_slot_after(datetime.now(timezone.utc), last_slot=last_slot)
        await asyncio.sleep(max(0.0, (slot - datetime.now(timezone.utc)).total_seconds()))
        last_slot = slot
        result = await run_once(collector, notifier, scheduled_at=slot)
        await tracker.observe(result, slot)


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


async def _run(command: str) -> int:
    db, binance, notifier, collector = _runtime()
    try:
        await db.connect()
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
