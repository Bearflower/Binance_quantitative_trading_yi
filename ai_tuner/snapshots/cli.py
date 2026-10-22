from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
import yaml

from shared.database import DatabaseManager
from shared.market_cap import MarketCapService
from shared.notification import NotificationClient
from shared.readonly_client import ReadOnlyBinanceClient

from .collector import CollectionSummary, SnapshotCollector
from .repository import SnapshotRepository


logger = structlog.get_logger()


def is_failed_bucket(summary: CollectionSummary) -> bool:
    return summary.requested > 0 and summary.complete == 0


async def collect_safely(collector: SnapshotCollector, as_of: datetime) -> CollectionSummary:
    try:
        return await collector.collect_once(as_of)
    except Exception as exc:
        logger.error("HRS外部快照整桶采集失败", error=str(exc), bucket=as_of.isoformat())
        return CollectionSummary(requested=1, complete=0, proxy=0, missing=0, failed=1)


def _snapshot_config() -> dict:
    path = Path(__file__).resolve().parents[1] / "config.yaml"
    with path.open(encoding="utf-8") as handle:
        return (yaml.safe_load(handle) or {}).get("snapshot_collection", {})


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HRS point-in-time snapshot collector")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("once")
    sub.add_parser("daemon")
    backfill = sub.add_parser("backfill-supported")
    backfill.add_argument("--start", required=True, type=_utc)
    backfill.add_argument("--end", required=True, type=_utc)
    return parser


def _runtime(config: dict):
    db = DatabaseManager(
        host=os.getenv("DB_HOST", "postgres"), port=int(os.getenv("DB_PORT", "5432")),
        database=os.environ["DB_NAME"], user=os.environ["AI_TUNER_SNAPSHOT_DB_USER"],
        password=os.environ["AI_TUNER_SNAPSHOT_DB_PASSWORD"],
        min_pool_size=1, max_pool_size=5,
    )
    binance = ReadOnlyBinanceClient(
        api_key="public-market-data", api_secret="public-market-data",
        testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true",
        use_unified_account=False, instance_id="ai-tuner-snapshot",
    )
    market_cap = MarketCapService(api_key=os.getenv("COINGECKO_API_KEY") or None)
    notifier = NotificationClient(
        service_url=os.getenv("NOTIFICATION_SERVICE_URL", ""), use_direct_webhook=True
    )
    return (
        db, binance, market_cap, notifier,
        SnapshotCollector(
            SnapshotRepository(db), binance, market_cap,
            concurrency=int(config.get("concurrency", 5)),
        ),
    )


async def _run(args) -> None:
    config = _snapshot_config()
    db, binance, market_cap, notifier, collector = _runtime(config)
    try:
        await db.connect()
        if args.command == "once":
            summary = await collector.collect_once(datetime.now(timezone.utc))
            logger.info("HRS外部快照采集完成", **summary.__dict__)
        elif args.command == "backfill-supported":
            if args.start >= args.end:
                raise ValueError("start must be before end")
            summary = await collector.backfill_supported(args.start, args.end)
            logger.info("HRS可支持历史快照回填完成", **summary.__dict__)
        else:
            consecutive_failures = 0
            schedule_minute = int(config.get("schedule_minute", 20))
            notify_after = int(config.get("notify_after_consecutive_failures", 3))
            while True:
                now = datetime.now(timezone.utc)
                next_run = now.replace(minute=schedule_minute, second=0, microsecond=0)
                if next_run <= now:
                    next_run += timedelta(hours=1)
                await asyncio.sleep((next_run - now).total_seconds())
                summary = await collect_safely(collector, next_run)
                consecutive_failures = consecutive_failures + 1 if is_failed_bucket(summary) else 0
                logger.info(
                    "HRS外部快照时间桶完成", bucket_start=next_run.replace(minute=0).isoformat(),
                    universe=summary.requested, complete=summary.complete, proxy=summary.proxy,
                    missing=summary.missing, failed=summary.failed,
                    consecutive_failures=consecutive_failures,
                )
                if consecutive_failures == notify_after:
                    try:
                        await notifier.send(
                            f"AI Tuner HRS 外部快照已连续 {notify_after} 个时间桶全部采集失败",
                            level="error", project="tuner",
                        )
                    except Exception as exc:
                        logger.error("HRS快照失败通知发送失败", error=str(exc))
    finally:
        await notifier.close()
        await market_cap.close()
        await binance.close()
        await db.disconnect()


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
