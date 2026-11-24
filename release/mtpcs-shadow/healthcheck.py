"""Check the latest committed hour in the MTPCS shadow ledger."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3


def check(ledger, now):
    # The collector starts an hourly close at :10 and retries once a minute.
    due = (now - timedelta(minutes=15)).replace(minute=0, second=0, microsecond=0)
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    try:
        with closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            row = db.execute(
                "SELECT at,json_array_length(records) FROM hours ORDER BY at DESC LIMIT 1"
            ).fetchone()
        if row is None or not row[1]:
            raise ValueError("no committed observations")
        at = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
        if at != at.replace(minute=0, second=0, microsecond=0):
            raise ValueError("invalid observation hour")
    except (sqlite3.Error, ValueError, TypeError) as error:
        return 1, f"shadow ledger unreadable or incomplete ({error})"
    status = "current" if due <= at <= current_hour else "outside expected range"
    return int(status != "current"), f"shadow ledger {status}: latest={at.isoformat()} expected>={due.isoformat()}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path)
    parser.add_argument("--now", type=datetime.fromisoformat)
    args = parser.parse_args()
    now = args.now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        parser.error("--now requires a timezone")
    code, detail = check(args.ledger, now.astimezone(timezone.utc))
    print(detail)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
