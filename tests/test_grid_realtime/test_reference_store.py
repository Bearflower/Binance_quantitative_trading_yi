"""reference_store.py 测试：连接/迁移/出口写侧/五态读侧/CLI。"""
import sqlite3
from decimal import Decimal

import pytest

from strategies.grid.realtime.reference_store import (
    ACTIVE, CLOSED, FAILED, PREPARED, SENDING, SENT, UNKNOWN,
    SCHEMA_VERSION, ExportStore, ReferenceReader, SessionState,
    check_schema, connect, ensure_schema, make_reference_id)
from strategies.grid.realtime.rules import (ReferenceSnapshot,
                                            parse_profile)


@pytest.fixture
def cfg(profile_raw):
    return parse_profile(profile_raw)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "grid_realtime.sqlite3")


@pytest.fixture
def conn(db_path):
    connection = connect(db_path, 1000)
    ensure_schema(connection)
    return connection


@pytest.fixture
def store(conn):
    return ExportStore(conn)


def _publish_sent(store, snap, now_ms, *, session_id=None, eff_ms=None):
    """完整出口流程：begin(可跳过)→prepare→sending→sent；返回 (sid, ref_id)。"""
    sid = session_id or store.begin_session(now_ms)
    state = SessionState(sid, 0, None, None)
    ref_id = store.prepare(now_ms, state, snap)
    store.mark_sending(ref_id)
    sent_state = SessionState(sid, 0, None, 1)
    store.mark_sent(ref_id, eff_ms or snap.effective_at_ms, sent_state, now_ms)
    return sid, ref_id


# ───────────────────── 连接 ─────────────────────

def test_connect_wal_and_row_factory(db_path):
    connection = connect(db_path, 2000)
    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    assert connection.row_factory is sqlite3.Row
    connection.close()


# ───────────────────── 迁移 ─────────────────────

def test_ensure_schema_idempotent(conn):
    ensure_schema(conn)  # 重复执行不报错
    check_schema(conn)
    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key='version'").fetchone()[0]
    assert version == str(SCHEMA_VERSION)


def test_check_schema_without_meta_raises(db_path):
    connection = connect(db_path, 1000)
    connection.executescript(
        "CREATE TABLE reference(x); CREATE TABLE session(x);"
        "CREATE TABLE event(x); CREATE TABLE outbox(x)")
    with pytest.raises(RuntimeError, match="版本不符"):
        check_schema(connection)
    connection.close()


def test_check_schema_wrong_version(conn):
    conn.execute("UPDATE schema_meta SET value='999' WHERE key='version'")
    conn.commit()
    with pytest.raises(RuntimeError, match="版本不符"):
        check_schema(conn)


def test_check_schema_missing_table(conn):
    conn.execute("DROP TABLE outbox")
    conn.commit()
    with pytest.raises(RuntimeError, match="缺少表"):
        check_schema(conn)


def test_make_reference_id_deterministic(snap):
    args = (snap.calculated_at_ms, snap.grid_lower, snap.grid_upper,
            snap.stop_lower, snap.stop_upper)
    first = make_reference_id(*args)
    assert make_reference_id(*args) == first
    assert first.startswith(f"ref-{snap.calculated_at_ms}-")
    changed = (snap.calculated_at_ms + 1, *args[1:])
    assert make_reference_id(*changed) != first


# ───────────────────── 出口写侧 ─────────────────────

def test_begin_session_monotonic(store):
    assert store.begin_session(100) == 1
    assert store.begin_session(200) == 2
    rows = store._conn.execute(
        "SELECT session_id, status FROM session ORDER BY session_id").fetchall()
    assert [r["status"] for r in rows] == [ACTIVE, ACTIVE]


def test_prepare_records_prepared_and_pending(store, snap):
    sid = store.begin_session(100)
    state = SessionState(sid, 0, None, None)
    ref_id = store.prepare(200, state, snap)
    row = store._conn.execute(
        "SELECT * FROM reference WHERE reference_id=?", (ref_id,)).fetchone()
    assert row["send_status"] == PREPARED
    assert row["session_id"] == sid and row["seq"] == 1
    session = store._conn.execute(
        "SELECT * FROM session WHERE session_id=?", (sid,)).fetchone()
    assert session["pending_seq"] == 1


def test_mark_sending(store, snap):
    sid = store.begin_session(100)
    ref_id = store.prepare(200, SessionState(sid, 0, None, None), snap)
    store.mark_sending(ref_id)
    status = store._conn.execute(
        "SELECT send_status FROM reference WHERE reference_id=?",
        (ref_id,)).fetchone()[0]
    assert status == SENDING


def test_mark_sent_advances_seq(store, snap):
    sid = store.begin_session(100)
    state = SessionState(sid, 0, None, None)
    ref_id = store.prepare(200, state, snap)
    store.mark_sending(ref_id)
    store.mark_sent(
        ref_id, snap.effective_at_ms, SessionState(sid, 0, None, 1), 300,
        message_id="m-1")
    row = store._conn.execute(
        "SELECT * FROM reference WHERE reference_id=?", (ref_id,)).fetchone()
    assert row["send_status"] == SENT
    assert row["effective_at_ms"] == snap.effective_at_ms
    assert row["message_id"] == "m-1"
    session = store._conn.execute(
        "SELECT * FROM session WHERE session_id=?", (sid,)).fetchone()
    assert session["current_seq"] == 1
    assert session["current_reference_id"] == ref_id
    assert session["pending_seq"] is None
    assert session["status"] == ACTIVE


def test_mark_sent_without_message_id(store, snap):
    sid, ref_id = _publish_sent(store, snap, 300)
    row = store._conn.execute(
        "SELECT message_id FROM reference WHERE reference_id=?",
        (ref_id,)).fetchone()
    assert row["message_id"] is None


def test_mark_failed_clears_pending(store, snap):
    sid = store.begin_session(100)
    state = SessionState(sid, 0, None, None)
    ref_id = store.prepare(200, state, snap)
    store.mark_failed(ref_id, SessionState(sid, 0, None, 1), 300)
    row = store._conn.execute(
        "SELECT send_status FROM reference WHERE reference_id=?",
        (ref_id,)).fetchone()
    assert row[0] == FAILED
    session = store._conn.execute(
        "SELECT * FROM session WHERE session_id=?", (sid,)).fetchone()
    assert session["pending_seq"] is None and session["current_seq"] == 0


def test_mark_unknown_clears_pending(store, snap):
    sid = store.begin_session(100)
    state = SessionState(sid, 0, None, None)
    ref_id = store.prepare(200, state, snap)
    store.mark_unknown(ref_id, SessionState(sid, 0, None, 1), 300)
    status = store._conn.execute(
        "SELECT send_status FROM reference WHERE reference_id=?",
        (ref_id,)).fetchone()[0]
    assert status == UNKNOWN


def test_set_sync_uncertain(store):
    sid = store.begin_session(100)
    store.set_sync_uncertain(SessionState(sid, 0, None, None), 200)
    status = store._conn.execute(
        "SELECT status FROM session WHERE session_id=?", (sid,)).fetchone()[0]
    assert status == "SYNC_UNCERTAIN"


def test_close_session(store):
    sid = store.begin_session(100)
    store.close_session(200)
    status = store._conn.execute(
        "SELECT status FROM session WHERE session_id=?", (sid,)).fetchone()[0]
    assert status == CLOSED


def test_heartbeat_publish_true_and_transparent(store):
    sid = store.begin_session(100)
    state = SessionState(sid, 3, "ref-x", None)
    assert store.publish_heartbeat(state, 150) is True
    session = store._conn.execute(
        "SELECT * FROM session WHERE session_id=?", (sid,)).fetchone()
    assert session["status"] == ACTIVE  # 心跳不改状态
    assert session["current_seq"] == 3
    assert session["current_reference_id"] == "ref-x"
    assert session["updated_at_ms"] == 150


def test_heartbeat_false_when_session_missing(store):
    state = SessionState(999, 0, None, None)
    assert store.publish_heartbeat(state, 150) is False


def test_heartbeat_false_after_close(store):
    sid = store.begin_session(100)
    store.close_session(200)
    assert store.publish_heartbeat(SessionState(sid, 0, None, None), 300) \
        is False


# ───────────────────── 读侧五态 ─────────────────────

def test_reader_no_session_sync_uncertain(conn, cfg):
    _, status = ReferenceReader(conn, cfg).current_snapshot(1000)
    assert status == "SYNC_UNCERTAIN"


def test_reader_session_closed_sync_uncertain(store, conn, cfg):
    store.begin_session(100)
    store.close_session(200)
    _, status = ReferenceReader(conn, cfg).current_snapshot(300)
    assert status == "SYNC_UNCERTAIN"


def test_reader_silence_exceeded_sync_uncertain(store, conn, cfg, snap):
    sid, _ = _publish_sent(store, snap, 100)
    _, status = ReferenceReader(conn, cfg).current_snapshot(
        100 + cfg["reference_sync"]["max_silence_seconds"] * 1000 + 1)
    assert status == "SYNC_UNCERTAIN"


def test_reader_pending_seq_sync_uncertain(store, conn, cfg, snap):
    sid = store.begin_session(100)
    store.prepare(200, SessionState(sid, 0, None, None), snap)
    _, status = ReferenceReader(conn, cfg).current_snapshot(300)
    assert status == "SYNC_UNCERTAIN"


def test_reader_missing_sent(store, conn, cfg):
    store.begin_session(100)
    _, status = ReferenceReader(conn, cfg).current_snapshot(200)
    assert status == "MISSING"


def test_reader_old_session_sent_sync_uncertain(store, conn, cfg, snap):
    """AC-21：出口重启开新会话后，旧会话遗留的 SENT 不能当作当前参考。"""
    _publish_sent(store, snap, 100)      # 旧会话已确认发送
    store.begin_session(200)             # 出口重启 → 新会话（尚无 SENT）
    reader = ReferenceReader(conn, cfg)
    snapshot, status = reader.current_snapshot(201)
    assert snapshot is None and status == "SYNC_UNCERTAIN"
    # 历史快照仍取该旧 SENT，供历史参考穿越事实使用
    assert reader.historical_snapshot() is not None


def _snap_with_symbol(symbol):
    return ReferenceSnapshot(
        reference_id="ref-other", symbol=symbol,
        calculated_at_ms=1_000_000_000_000, effective_at_ms=1_000_000_000_000,
        grid_lower=Decimal("2623.52"), grid_upper=Decimal("2767.96"),
        stop_lower=Decimal("2599.45"), stop_upper=Decimal("2792.03"))


def test_reader_invalid_sent(store, conn, cfg):
    other = _snap_with_symbol("BTCUSDT")
    _publish_sent(store, other, 100)
    _, status = ReferenceReader(conn, cfg).current_snapshot(200)
    assert status == "INVALID"


def test_reader_stale_returns_snapshot(store, conn, cfg, snap):
    sid, ref_id = _publish_sent(store, snap, 100)
    now = snap.effective_at_ms + cfg["reference"]["max_age_seconds"] * 1000 + 1
    # 心跳线程持续刷新 updated_at（否则静默判定先于年龄判定）
    store.publish_heartbeat(SessionState(sid, 1, ref_id, None), now)
    snapshot, status = ReferenceReader(conn, cfg).current_snapshot(now)
    assert status == "STALE"
    assert snapshot.reference_id == ref_id


def test_reader_valid(store, conn, cfg, snap):
    _, ref_id = _publish_sent(store, snap, snap.effective_at_ms - 10)
    snapshot, status = ReferenceReader(conn, cfg).current_snapshot(
        snap.effective_at_ms + 1000)
    assert status == "VALID"
    assert snapshot.reference_id == ref_id


def test_historical_snapshot_ok(store, conn, cfg, snap):
    _, ref_id = _publish_sent(store, snap, 100)
    historical = ReferenceReader(conn, cfg).historical_snapshot()
    assert historical is not None and historical.reference_id == ref_id


def test_historical_snapshot_none_when_empty(conn, cfg):
    assert ReferenceReader(conn, cfg).historical_snapshot() is None


def test_historical_snapshot_none_when_invalid(store, conn, cfg):
    other = _snap_with_symbol("BTCUSDT")
    _publish_sent(store, other, 100)
    assert ReferenceReader(conn, cfg).historical_snapshot() is None


# ───────────────────── 迁移 CLI ─────────────────────

def test_migration_cli(tmp_path):
    from strategies.grid.realtime.reference_store import _main
    target = tmp_path / "nested" / "grid.sqlite3"
    rc = _main(["--db-path", str(target), "--busy-timeout-ms", "500"])
    assert rc == 0
    connection = connect(str(target), 500)
    check_schema(connection)
    connection.close()
