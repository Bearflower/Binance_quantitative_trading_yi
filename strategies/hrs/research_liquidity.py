"""Observe public depth for scored LV-RM candidates in a separate, read-only process."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

LOG = logging.getLogger(__name__)
NOTIONAL = Decimal("100")
EXIT_DELAY = timedelta(hours=6)
MAX_LAG = timedelta(seconds=60)
RETRY_DELAY = timedelta(seconds=5)
DISCOVERY_PAGE = 128
LIVE_BATCH = 4
EXPIRED_BATCH = 64
DEPTH_URL = "https://fapi.binance.com/fapi/v1/depth"

# Use the existing (event_time,kind) index. Return only the two actual scored
# flags; hourly candle arrays and private trading/account state stay in Postgres.
CANDIDATES = """
    SELECT evidence_id, run_id, symbol, event_time, available_at,
           payload->'short_should_entry' = 'true'::jsonb AS short_signal,
           payload->'long_should_entry' = 'true'::jsonb AS long_signal
      FROM hrs.hrs_decision_evidence
     WHERE event_time >= $1 AND event_time <= $2 AND available_at <= $3
       AND (event_time,evidence_id) > ($4,$5)
       AND kind = 'lv_rm_analysis'
       AND (payload->'short_should_entry' = 'true'::jsonb OR payload->'long_should_entry' = 'true'::jsonb)
     ORDER BY event_time,evidence_id LIMIT $6
"""


def utcnow():
    return datetime.now(timezone.utc)


def iso(stamp):
    return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")


def open_store(path):
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS scan_state (
            key TEXT PRIMARY KEY, value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            evidence_id TEXT NOT NULL, phase TEXT NOT NULL, due_at TEXT NOT NULL,
            run_id TEXT NOT NULL, symbol TEXT NOT NULL, directions TEXT NOT NULL,
            signal_time TEXT NOT NULL, signal_available_at TEXT NOT NULL,
            PRIMARY KEY(evidence_id, phase)
        );
        CREATE INDEX IF NOT EXISTS tasks_due ON tasks(due_at);
        CREATE TABLE IF NOT EXISTS attempts (
            evidence_id TEXT NOT NULL, phase TEXT NOT NULL, last_attempt_at TEXT NOT NULL,
            count INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(evidence_id, phase)
        );
        CREATE TABLE IF NOT EXISTS receipts (
            evidence_id TEXT NOT NULL, phase TEXT NOT NULL, due_at TEXT NOT NULL,
            status TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(evidence_id, phase)
        );
    """)
    return db


def schedule(db, rows):
    with db:
        for row in rows:
            directions = [side for side in ("short", "long") if row[side + "_signal"]]
            for phase, delay in (("entry", timedelta(0)), ("exit_6h", EXIT_DELAY)):
                db.execute("""INSERT OR IGNORE INTO tasks
                    (evidence_id,phase,due_at,run_id,symbol,directions,signal_time,signal_available_at)
                    SELECT ?,?,?,?,?,?,?,? WHERE NOT EXISTS
                        (SELECT 1 FROM receipts WHERE evidence_id=? AND phase=?)""",
                    (row["evidence_id"], phase, iso(row["event_time"] + delay), row["run_id"],
                     row["symbol"], json.dumps(directions), iso(row["event_time"]), iso(row["available_at"]),
                     row["evidence_id"], phase))


async def discover(db, source, now):
    state = dict(db.execute("SELECT key, value FROM scan_state"))
    # Service today's entry and exit windows first, even during outage recovery.
    # Paginate by both time and ID so equal timestamps do not drop candidates.
    for upper in (now - EXIT_DELAY, now):
        lower = upper - MAX_LAG
        cursor = (lower, "")
        while True:
            rows = await source.fetch(CANDIDATES, lower, upper, now, *cursor, DISCOVERY_PAGE)
            schedule(db, rows)
            if len(rows) < DISCOVERY_PAGE:
                break
            cursor = (rows[-1]["event_time"], rows[-1]["evidence_id"])

    previous = datetime.fromisoformat(state["scanned_at"]) if "scanned_at" in state else now - EXIT_DELAY
    sweep = "swept_at" not in state or now - datetime.fromisoformat(state["swept_at"]) >= timedelta(minutes=30)
    if "recovery" in state:
        lower_raw, upper_raw, after, after_id = json.loads(state["recovery"])
        lower, upper, after_time = map(datetime.fromisoformat, (lower_raw, upper_raw, after))
    elif sweep or previous < now - MAX_LAG:
        lower = (min(previous, now - EXIT_DELAY) if sweep else previous) - MAX_LAG
        upper = min(now, lower + EXIT_DELAY + MAX_LAG)
        after_time, after_id = lower, ""
    else:
        with db:
            db.execute("INSERT OR REPLACE INTO scan_state VALUES ('scanned_at',?)", (iso(now),))
        return

    # At most one historic page per poll. Advance its persistent cursor only
    # after scheduling the page. A retry can repeat a page without losing data.
    rows = await source.fetch(CANDIDATES, lower, upper, now, after_time, after_id, DISCOVERY_PAGE)
    schedule(db, rows)
    with db:
        if len(rows) == DISCOVERY_PAGE:
            cursor = [iso(lower), iso(upper), iso(rows[-1]["event_time"]), rows[-1]["evidence_id"]]
            db.execute("INSERT OR REPLACE INTO scan_state VALUES ('recovery',?)", (json.dumps(cursor),))
        else:
            db.execute("DELETE FROM scan_state WHERE key='recovery'")
            db.execute("INSERT OR REPLACE INTO scan_state VALUES ('scanned_at',?)", (iso(upper),))
            db.execute("INSERT OR REPLACE INTO scan_state VALUES ('swept_at',?)", (iso(now),))


def side_depth(raw_levels, descending):
    if not isinstance(raw_levels, list) or not 1 <= len(raw_levels) <= 20:
        raise ValueError("invalid_levels")
    levels = []
    remaining, quantity, depth = NOTIONAL, Decimal(0), Decimal(0)
    previous = None
    for level in raw_levels:
        if not isinstance(level, list) or len(level) != 2:
            raise ValueError("invalid_level")
        price, size = (Decimal(str(x)) for x in level)
        if not all(x.is_finite() and x > 0 and math.isfinite(float(x)) for x in (price, size)):
            raise ValueError("invalid_level")
        if previous is not None and (price >= previous if descending else price <= previous):
            raise ValueError("unordered_levels")
        previous = price
        quote = price * size
        depth += quote
        taken = min(remaining, quote)
        remaining -= taken
        quantity += taken / price
        levels.append([str(price), str(size)])
    if not math.isfinite(float(depth)):
        raise ValueError("invalid_depth")
    vwap = float(NOTIONAL / quantity) if not remaining else None
    return levels, float(depth), vwap


def book_fields(raw, due, received):
    # Binance's transaction time identifies the observed book; retain event
    # and local receipt times independently, with no substitution of a bar.
    event_ms, transaction_ms = raw["E"], raw["T"]
    if any(type(stamp) is not int or stamp <= 0 for stamp in (transaction_ms, event_ms)):
        raise ValueError("invalid_exchange_time")
    earliest_ms = int(due.timestamp() * 1000)
    latest_ms = int(received.timestamp() * 1000)
    if not earliest_ms <= transaction_ms <= event_ms <= latest_ms:
        raise ValueError("book_timestamp_outside_window")
    bids, bid_depth, sell_vwap = side_depth(raw["bids"], True)
    asks, ask_depth, buy_vwap = side_depth(raw["asks"], False)
    if Decimal(bids[0][0]) >= Decimal(asks[0][0]):
        raise ValueError("crossed_book")
    return {
        "exchange_event_time_ms": event_ms, "exchange_transaction_time_ms": transaction_ms,
        "last_update_id": raw.get("lastUpdateId"), "bids": bids, "asks": asks,
        "best_bid": float(bids[0][0]), "best_ask": float(asks[0][0]),
        "bid_quantity": float(bids[0][1]), "ask_quantity": float(asks[0][1]),
        "bid_depth_usdt": bid_depth, "ask_depth_usdt": ask_depth,
        "buy_vwap": buy_vwap, "sell_vwap": sell_vwap,
    }


async def collect_due(db, fetch_book, clock=utcnow, pace=None):
    now = clock()
    read_tasks = """
        SELECT t.*, a.payload AS last_attempt, COALESCE(a.count,0) AS attempt_count
          FROM tasks t LEFT JOIN attempts a USING(evidence_id,phase)
    """
    cutoff = iso(now - MAX_LAG)
    live = db.execute(read_tasks + """
         WHERE t.due_at > ? AND t.due_at <= ?
           AND (a.last_attempt_at IS NULL OR a.last_attempt_at <= ?)
         ORDER BY t.due_at,t.evidence_id,t.phase LIMIT ?
        """, (cutoff, iso(now), iso(now - RETRY_DELAY), LIVE_BATCH)).fetchall()
    expired = db.execute(read_tasks + """
         WHERE t.due_at <= ? ORDER BY t.due_at,t.evidence_id,t.phase LIMIT ?
        """, (cutoff, EXPIRED_BATCH)).fetchall()

    async def collect(task):
        due = datetime.fromisoformat(task["due_at"])
        payload = {
            "source_evidence_id": task["evidence_id"], "phase": task["phase"],
            "symbol": task["symbol"], "run_id": task["run_id"], "directions": json.loads(task["directions"]),
            "signal_time": task["signal_time"], "signal_available_at": task["signal_available_at"],
            "due_at": task["due_at"], "probe_notional_usdt": float(NOTIONAL),
            "quote_requested_at": None, "quote_received_at": None, "status": "missing",
            "missing_reason": "collection_window_elapsed",
        }
        attempt_count = task["attempt_count"]
        if task["last_attempt"]:
            payload = json.loads(task["last_attempt"])
        can_request = True
        if pace is not None:
            seconds_left = (due + MAX_LAG - clock()).total_seconds()
            if seconds_left > 0:
                try:
                    await asyncio.wait_for(pace(), timeout=seconds_left)
                except asyncio.TimeoutError:
                    can_request = False
        requested = clock()
        if can_request and due <= requested < due + MAX_LAG:
            attempt_count += 1
            payload["quote_received_at"] = None
            payload["quote_requested_at"] = iso(requested)
            try:
                raw = await asyncio.wait_for(fetch_book(task["symbol"]),
                                             timeout=(due + MAX_LAG - requested).total_seconds())
            except Exception as exc:
                payload["missing_reason"] = "depth_request_failed:" + type(exc).__name__
            else:
                received = clock()
                payload["quote_received_at"] = iso(received)
                if not requested <= received <= due + MAX_LAG:
                    payload["missing_reason"] = "depth_response_outside_window"
                else:
                    try:
                        fields = book_fields(raw, due, received)
                    except (ValueError, InvalidOperation, KeyError, TypeError, ArithmeticError) as exc:
                        payload["missing_reason"] = str(exc) if type(exc) is ValueError else "invalid_book"
                    else:
                        payload.update(fields, status="ok", missing_reason=None)
        finished = clock()
        payload["recorded_at"] = iso(finished)
        payload["attempt_count"] = attempt_count
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        key = (task["evidence_id"], task["phase"])
        with db:
            if payload["status"] == "missing" and finished < due + MAX_LAG:
                db.execute("""INSERT INTO attempts
                    SELECT ?,?,?,?,? WHERE EXISTS (SELECT 1 FROM tasks WHERE evidence_id=? AND phase=?)
                    ON CONFLICT(evidence_id,phase) DO UPDATE SET
                    last_attempt_at=excluded.last_attempt_at,count=excluded.count,payload=excluded.payload
                    WHERE excluded.last_attempt_at > attempts.last_attempt_at""",
                    (*key, iso(finished), attempt_count, encoded, *key))
            else:
                db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?,?,?,?)",
                           (*key, task["due_at"], payload["status"], encoded))
                db.execute("DELETE FROM attempts WHERE evidence_id=? AND phase=?", key)
                db.execute("DELETE FROM tasks WHERE evidence_id=? AND phase=?", key)

    for row in (*live, *expired):
        await collect(row)
        await asyncio.sleep(0)
    return len(live), len(expired)


async def observe(db, source, fetch_book, pace, *, once=False, poll_seconds=5):
    async def discovery_loop():
        while True:
            try:
                await discover(db, source, utcnow())
            except Exception as exc:
                LOG.error("HRS candidate discovery unavailable (%s)", type(exc).__name__)
                if once:
                    raise
            if once:
                return
            await asyncio.sleep(poll_seconds)

    if once:
        try:
            await discovery_loop()
        finally:
            await collect_due(db, fetch_book, pace=pace)
        return

    async def collection_loop():
        while True:
            live, expired = await collect_due(db, fetch_book, pace=pace)
            # Drain full batches without delaying the independent source poll.
            if live < LIVE_BATCH and expired < EXPIRED_BATCH:
                await asyncio.sleep(poll_seconds)

    workers = [asyncio.create_task(discovery_loop()), asyncio.create_task(collection_loop())]
    try:
        await asyncio.gather(*workers)
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


async def run(args):
    import aiohttp
    import asyncpg

    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        raise RuntimeError("readonly_dsn_missing")
    args.sqlite.parent.mkdir(parents=True, exist_ok=True)
    db = open_store(args.sqlite)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as http:
            # Limit this independent public reader to one request per second.
            # Honor exchange IP backoff across symbols and retries; the outer
            # deadline cancels waiting work before an obsolete GET is issued.
            next_request = 0.0
            loop = asyncio.get_running_loop()

            async def pace():
                await asyncio.sleep(max(0.0, next_request - loop.time()))

            async def fetch_book(symbol):
                nonlocal next_request
                next_request = loop.time() + 1
                async with http.get(DEPTH_URL, params={"symbol": symbol, "limit": 20},
                                    allow_redirects=False) as response:
                    if response.status in (418, 429):
                        delay = response.headers.get("Retry-After", "")
                        next_request = loop.time() + max(1, int(delay) if delay.isdecimal() else 60)
                    response.raise_for_status()
                    return await response.json()

            # Pool connects lazily so an outage during restart cannot prevent
            # collection of exits already scheduled in the local database.
            async with asyncpg.create_pool(dsn, min_size=0, max_size=1, timeout=4, command_timeout=5,
                    server_settings={"default_transaction_read_only": "on"}) as source:
                await observe(db, source, fetch_book, pace, once=args.once)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, type=Path)
    parser.add_argument("--dsn-env", default="REPLAY_READONLY_DSN")
    parser.add_argument("--once", action="store_true", help="run one bounded discovery and collection pass")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as exc:
        # Connection errors may contain a credential-bearing DSN.
        parser.exit(1, f"Liquidity observer failed ({type(exc).__name__})\n")


if __name__ == "__main__":
    main()
