from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone

import structlog

from shared.binance_api import BinanceClient
from shared.database import DatabaseManager
from shared.market_cap import MarketCapService

from .collector import SnapshotCollector
from .repository import SnapshotRepository


logger = structlog.get_logger()


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


def _runtime():
    db = DatabaseManager(
        host=os.getenv("DB_HOST", "postgres"), port=int(os.getenv("DB_PORT", "5432")),
        database=os.environ["DB_NAME"], user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"], min_pool_size=1, max_pool_size=5,
    )
    binance = BinanceClient(
        api_key=os.environ["BINANCE_API_KEY"], api_secret=os.environ["BINANCE_API_SECRET"],
        testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true",
        use_unified_account=True,
    )
    market_cap = MarketCapService(api_key=os.getenv("COINGECKO_API_KEY") or None)
    return db, binance, market_cap, SnapshotCollector(SnapshotRepository(db), binance, market_cap)


async def _run(args) -> None:
    db, binance, market_cap, collector = _runtime()
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
            while True:
                now = datetime.now(timezone.utc)
                next_run = now.replace(minute=20, second=0, microsecond=0)
                if next_run <= now:
                    next_run += timedelta(hours=1)
                await asyncio.sleep((next_run - now).total_seconds())
                summary = await collector.collect_once(next_run)
                consecutive_failures = consecutive_failures + 1 if summary.failed else 0
                logger.info(
                    "HRS外部快照时间桶完成", bucket_start=next_run.replace(minute=0).isoformat(),
                    universe=summary.requested, complete=summary.complete, proxy=summary.proxy,
                    missing=summary.missing, failed=summary.failed,
                    consecutive_failures=consecutive_failures,
                )
    finally:
        await market_cap.close()
        await binance.close()
        await db.disconnect()


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
