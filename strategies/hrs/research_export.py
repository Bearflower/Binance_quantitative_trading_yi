"""Export bounded, read-only LV-RM research inputs. Write one JSONL stream to stdout."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys


def timestamp(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.utcoffset() is None:
        raise ValueError("require a time zone")
    return date.astimezone(timezone.utc)


def emit(record):
    print(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")))


async def export_analysis(start, end, as_of):
    import asyncpg

    query = """
        SELECT evidence_id, run_id, kind, symbol, event_time, available_at, recorded_at, payload_sha256, payload
        FROM hrs.hrs_decision_evidence
        WHERE kind = 'lv_rm_analysis' AND event_time >= $1 AND event_time < $2
          AND available_at <= $3 AND recorded_at <= $3
          AND (payload->'long_should_entry' = 'true'::jsonb OR payload->'short_should_entry' = 'true'::jsonb)
        ORDER BY event_time, evidence_id
    """
    connection = await asyncpg.connect(os.environ["DATABASE_URL"], timeout=10,
                                       server_settings={"default_transaction_read_only": "on"})
    try:
        async with connection.transaction(readonly=True, isolation="repeatable_read"):
            async for row in connection.cursor(query, start, end, as_of, prefetch=10):
                data = dict(row)
                for name in ("event_time", "available_at", "recorded_at"):
                    data[name] = data[name].isoformat()
                data["payload"] = json.loads(data["payload"])
                emit(data)
    finally:
        await connection.close()


def export_quotes(path, start, end, as_of):
    # A study window selects signals by decision time. A six-hour exit can be
    # outside that window, but must already have been observed by the as-of.
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        for evidence_id, phase, raw in db.execute(
            """SELECT evidence_id, phase, payload FROM receipts
               WHERE due_at >= ? AND due_at < ? ORDER BY due_at, evidence_id, phase""",
            (start.isoformat(timespec="microseconds"), (end + timedelta(hours=6)).isoformat(timespec="microseconds")),
        ):
            payload = json.loads(raw)
            if start <= timestamp(payload["signal_time"]) < end and timestamp(payload["recorded_at"]) <= as_of:
                emit(dict(evidence_id=evidence_id, phase=phase, payload=payload))
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["analysis", "quotes"])
    parser.add_argument("--start", type=timestamp, required=True)
    parser.add_argument("--end", type=timestamp, required=True)
    parser.add_argument("--as-of", type=timestamp, required=True)
    parser.add_argument("--sqlite", type=Path)
    args = parser.parse_args()
    if not args.start < args.end <= args.as_of <= datetime.now(timezone.utc):
        parser.error("require start < end <= as-of <= now")
    if args.kind == "quotes" and not args.sqlite:
        parser.error("quote export requires --sqlite")
    if args.kind == "analysis":
        asyncio.run(export_analysis(args.start, args.end, args.as_of))
    else:
        export_quotes(args.sqlite, args.start, args.end, args.as_of)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.exit(f"Research export failed ({type(exc).__name__})")
