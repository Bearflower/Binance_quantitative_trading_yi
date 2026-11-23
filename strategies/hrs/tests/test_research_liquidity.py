import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from strategies.hrs import research_liquidity as observer


BASE = datetime(2026, 11, 23, 8, tzinfo=timezone.utc)


def candidate(signal_time=BASE):
    return {
        "evidence_id": "analysis-id", "run_id": "run-id", "symbol": "TESTUSDT",
        "short_signal": True, "long_signal": False,
        "event_time": signal_time, "available_at": signal_time,
    }


def book(ex_time=BASE):
    stamp = int(ex_time.timestamp() * 1000)
    return {"E": stamp, "T": stamp, "lastUpdateId": 42,
            "bids": [["10", "5"], ["9", "8"]], "asks": [["11", "2"], ["12", "3"]]}


def test_restart_dedup_and_opposite_side_quotes(tmp_path):
    path = tmp_path / "research.sqlite"
    source = AsyncMock()
    source.fetch.return_value = [candidate()]
    db = observer.open_store(path)
    asyncio.run(observer.discover(db, source, BASE))
    entry = AsyncMock(return_value=book())
    asyncio.run(observer.collect_due(db, entry, clock=lambda: BASE))
    db.close()
    db = observer.open_store(path)
    end = BASE + timedelta(hours=6)
    asyncio.run(observer.discover(db, source, end))
    exit_book = AsyncMock(return_value=book(end))
    asyncio.run(observer.collect_due(db, exit_book, clock=lambda: end))
    asyncio.run(observer.collect_due(db, exit_book, clock=lambda: end))
    entry.assert_awaited_once_with("TESTUSDT")
    exit_book.assert_awaited_once_with("TESTUSDT")
    receipts = [json.loads(row[0]) for row in db.execute("SELECT payload FROM receipts ORDER BY phase")]
    assert [row["phase"] for row in receipts] == ["entry", "exit_6h"]
    for row in receipts:
        assert row["status"] == "ok"
        assert row["source_evidence_id"] == "analysis-id"
        assert row["directions"] == ["short"]
        assert row["bid_quantity"] == 5
        assert row["ask_quantity"] == 2
        assert row["bid_depth_usdt"] == 122
        assert row["ask_depth_usdt"] == 58
        assert row["sell_vwap"] == pytest.approx(100 / (5 + 50 / 9))
        assert row["buy_vwap"] is None
        assert row["asks"] == [["11", "2"], ["12", "3"]]
    assert receipts[1]["signal_time"] == observer.iso(BASE)
    assert receipts[1]["quote_received_at"] == observer.iso(end)
    db.close()


def test_expired_entry_and_exit_never_fetch_current_book():
    db = observer.open_store(":memory:")
    source = AsyncMock()
    source.fetch.return_value = [candidate()]
    late = BASE + observer.EXIT_DELAY + observer.MAX_LAG + timedelta(seconds=1)
    asyncio.run(observer.discover(db, source, late))
    depth = AsyncMock()
    asyncio.run(observer.collect_due(db, depth, clock=lambda: late))
    depth.assert_not_awaited()
    rows = [json.loads(row[0]) for row in db.execute("SELECT payload FROM receipts")]
    assert len(rows) == 2
    assert all(row["missing_reason"] == "collection_window_elapsed" for row in rows)
    assert all(row["quote_received_at"] is None for row in rows)
    db.close()


@pytest.mark.parametrize("mutation,reason", [
    ({"bids": [["10", "1"], ["11", "1"]]}, "unordered_levels"),
    ({"bids": [["11", "2"]]}, "crossed_book"),
    ({"asks": [["NaN", "1"]]}, "invalid_level"),
    ({"E": int(BASE.timestamp() * 1000) + 1000}, "book_timestamp_outside_window"),
    ({"T": int(BASE.timestamp() * 1000) - 1000}, "book_timestamp_outside_window"),
])
def test_rejects_unusable_books(mutation, reason):
    with pytest.raises(ValueError, match=reason):
        observer.book_fields(dict(book(), **mutation), BASE, BASE)


def test_quote_response_after_window_is_missing():
    db = observer.open_store(":memory:")
    source = AsyncMock()
    source.fetch.return_value = [candidate()]
    asyncio.run(observer.discover(db, source, BASE))
    late = BASE + observer.MAX_LAG + timedelta(milliseconds=1)
    times = iter([BASE, BASE, late, late])
    asyncio.run(observer.collect_due(db, AsyncMock(return_value=book()), clock=lambda: next(times)))
    result = json.loads(db.execute("SELECT payload FROM receipts").fetchone()[0])
    assert result["status"] == "missing"
    assert result["missing_reason"] == "depth_response_outside_window"
    assert result["quote_received_at"] == observer.iso(late)
    assert "bids" not in result
    db.close()


def test_transient_failure_retries_across_restart_within_window(tmp_path):
    path = tmp_path / "restart.sqlite"
    db = observer.open_store(path)
    source = AsyncMock()
    source.fetch.return_value = [candidate()]
    asyncio.run(observer.discover(db, source, BASE))
    failed = AsyncMock(side_effect=TimeoutError)
    asyncio.run(observer.collect_due(db, failed, clock=lambda: BASE))
    assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    db.close()
    db = observer.open_store(path)
    retry = BASE + observer.RETRY_DELAY
    depth = AsyncMock(return_value=book(retry))
    asyncio.run(observer.collect_due(db, depth, clock=lambda: BASE))
    depth.assert_not_awaited()
    asyncio.run(observer.collect_due(db, depth, clock=lambda: retry))
    row = json.loads(db.execute("SELECT payload FROM receipts").fetchone()[0])
    assert row["status"] == "ok"
    assert row["attempt_count"] == 2
    assert row["quote_received_at"] == observer.iso(retry)
    assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    db.close()


def test_failure_at_window_expiry_retains_last_attempt_without_late_fetch():
    db = observer.open_store(":memory:")
    source = AsyncMock()
    source.fetch.return_value = [candidate()]
    asyncio.run(observer.discover(db, source, BASE))
    failed = AsyncMock(return_value=book(BASE - timedelta(seconds=1)))
    asyncio.run(observer.collect_due(db, failed, clock=lambda: BASE))
    late = BASE + observer.MAX_LAG + timedelta(seconds=1)
    asyncio.run(observer.collect_due(db, failed, clock=lambda: late))
    failed.assert_awaited_once()
    row = json.loads(db.execute("SELECT payload FROM receipts").fetchone()[0])
    assert row["status"] == "missing"
    assert row["missing_reason"] == "book_timestamp_outside_window"
    assert row["quote_received_at"] == observer.iso(BASE)
    assert row["recorded_at"] == observer.iso(late)
    assert row["attempt_count"] == 1
    db.close()


def test_request_timestamp_follows_rate_limit_and_stale_work_never_sends():
    for wait, wanted in [(5, True), (65, False)]:
        db = observer.open_store(":memory:")
        source = AsyncMock()
        source.fetch.return_value = [candidate()]
        asyncio.run(observer.discover(db, source, BASE))
        wall = [BASE]

        async def pace():
            wall[0] += timedelta(seconds=wait)

        fetch = AsyncMock(return_value=book(BASE + timedelta(seconds=wait)))
        asyncio.run(observer.collect_due(db, fetch, clock=lambda: wall[0], pace=pace))
        payload = json.loads(db.execute("SELECT payload FROM receipts").fetchone()[0])
        assert bool(fetch.await_count) == wanted
        assert payload["quote_requested_at"] == (observer.iso(wall[0]) if wanted else None)
        db.close()


def test_collector_prioritizes_expiring_quotes_and_bounds_dead_backlog(monkeypatch):
    db = observer.open_store(":memory:")
    monkeypatch.setattr(observer, "LIVE_BATCH", 2)
    monkeypatch.setattr(observer, "EXPIRED_BATCH", 3)
    for index, age in enumerate([1, 12, 59, *range(100, 110)]):
        row = dict(candidate(BASE - timedelta(seconds=age)), evidence_id=str(index), symbol=str(age))
        observer.schedule(db, [row])
    fetch = AsyncMock(return_value=book(BASE))
    sizes = asyncio.run(observer.collect_due(db, fetch, clock=lambda: BASE))
    assert sizes == (2, 3)
    assert [call.args[0] for call in fetch.await_args_list] == ["59", "12"]
    statuses = dict(db.execute("SELECT status,count(*) FROM receipts GROUP BY status"))
    assert statuses == {"ok": 2, "missing": 3}
    db.close()


def test_discovery_keeps_running_during_blocked_public_book(monkeypatch):
    async def run_check():
        db = observer.open_store(":memory:")
        blocked, discovered = asyncio.Event(), asyncio.Event()
        first = candidate()
        second = dict(first, evidence_id="next", symbol="NEXTUSDT")
        calls = 0

        async def scan(db, source, now):
            nonlocal calls
            calls += 1
            observer.schedule(db, [first] if not blocked.is_set() else [second])
            if blocked.is_set():
                discovered.set()

        async def stalled_public_pacing():
            blocked.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(observer, "discover", scan)
        # collect_due's real clock must see the pending task as due right now.
        first.update(event_time=observer.utcnow(), available_at=observer.utcnow())
        second.update(event_time=first["event_time"], available_at=first["available_at"])
        worker = asyncio.create_task(observer.observe(
            db, None, AsyncMock(), stalled_public_pacing, poll_seconds=.001))
        try:
            await asyncio.wait_for(discovered.wait(), timeout=2)
            assert calls >= 2
            assert db.execute("SELECT count(*) FROM tasks WHERE evidence_id='next'").fetchone()[0] == 2
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            db.close()

    asyncio.run(run_check())


def test_paginated_outage_recovery_resumes_tied_ids_and_discovers_live_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, "DISCOVERY_PAGE", 2)
    path = tmp_path / "pages.sqlite"
    db = observer.open_store(path)
    old = BASE - timedelta(hours=3)
    samples = [dict(candidate(old), evidence_id=str(i)) for i in range(5)]
    samples.append(dict(candidate(BASE), evidence_id="new"))

    async def fetch(query, lower, upper, as_of, after, after_id, limit):
        assert limit == 2
        assert upper - lower <= observer.EXIT_DELAY + observer.MAX_LAG
        eligible = [r for r in samples if lower <= r["event_time"] <= upper
                    and (r["event_time"], r["evidence_id"]) > (after, after_id)]
        return sorted(eligible, key=lambda r: (r["event_time"], r["evidence_id"]))[:limit]

    source = AsyncMock()
    source.fetch.side_effect = fetch
    asyncio.run(observer.discover(db, source, BASE))
    assert {r[0] for r in db.execute("SELECT DISTINCT evidence_id FROM tasks")} == {"0", "1", "new"}
    db.close()
    db = observer.open_store(path)
    for offset in (5, 10, 15):
        asyncio.run(observer.discover(db, source, BASE + timedelta(seconds=offset)))
    assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 12
    assert db.execute("SELECT 1 FROM scan_state WHERE key='recovery'").fetchone() is None
    db.close()
