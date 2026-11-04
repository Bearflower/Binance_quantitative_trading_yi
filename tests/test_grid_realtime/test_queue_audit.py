"""队列审计须持久、可判定覆盖范围，不能把缺证据写成零溢出。"""
from strategies.grid.realtime.reference_store import connect, ensure_schema, check_schema
from strategies.grid.realtime.queue_audit import (
    QueueAudit, summarize_window, summarize_shadow_window)


def test_v2_checkpoint_migrates_to_unknown_and_new_state_is_recorded(tmp_path):
    conn = connect(str(tmp_path / "grid.sqlite3"), 1000)
    ensure_schema(conn)
    conn.execute("DROP TABLE queue_audit_checkpoint")
    conn.execute(
        "CREATE TABLE queue_audit_checkpoint ("
        "instance_id TEXT NOT NULL, observed_at_ms INTEGER NOT NULL, "
        "dropped_total INTEGER NOT NULL, recovery_until_ms INTEGER NOT NULL, "
        "audit_ok INTEGER NOT NULL, PRIMARY KEY(instance_id, observed_at_ms))")
    conn.execute(
        "INSERT INTO queue_audit_checkpoint VALUES('old', 1000, 0, 0, 1)")
    conn.execute("UPDATE schema_meta SET value='2' WHERE key='version'")
    conn.commit()

    ensure_schema(conn)
    check_schema(conn)
    old = conn.execute(
        "SELECT reference_status, feed_healthy, storage_degraded "
        "FROM queue_audit_checkpoint WHERE instance_id='old'").fetchone()
    assert tuple(old) == ("UNKNOWN", 0, 1)
    assert "decision_ms" in {row[1] for row in conn.execute(
        "PRAGMA table_info(event)")}

    audit = QueueAudit(conn, "new", 2000)
    audit.checkpoint(
        2000, recovery_until_ms=0, reference_status="VALID",
        feed_healthy=True, storage_degraded=False)
    fresh = conn.execute(
        "SELECT reference_status, feed_healthy, storage_degraded "
        "FROM queue_audit_checkpoint WHERE instance_id='new'").fetchone()
    assert tuple(fresh) == ("VALID", 1, 0)


def test_schema_check_rejects_missing_eligibility_columns(tmp_path):
    conn = connect(str(tmp_path / "grid.sqlite3"), 1000)
    ensure_schema(conn)
    conn.execute("DROP TABLE queue_audit_checkpoint")
    conn.execute(
        "CREATE TABLE queue_audit_checkpoint ("
        "instance_id TEXT, observed_at_ms INTEGER, dropped_total INTEGER, "
        "recovery_until_ms INTEGER, audit_ok INTEGER)")
    conn.commit()
    import pytest
    with pytest.raises(RuntimeError, match="reference_status"):
        check_schema(conn)


def test_shadow_window_separates_valid_stale_and_evaluable_events(tmp_path):
    conn = connect(str(tmp_path / "grid.sqlite3"), 1000)
    ensure_schema(conn)
    audit = QueueAudit(conn, "one", 0)
    for t in range(0, 10000, 1000):
        audit.checkpoint(
            t, 0, reference_status="VALID" if t < 6000 else "STALE",
            feed_healthy=True, storage_degraded=False)
    for event_id, decision_ms, first_held in (
            ("valid", 3000, 2500), ("stale", 8000, 7500),
            ("direct-urgent", 4000, None)):
        conn.execute(
            "INSERT INTO event(event_id,episode_id,reference_id,direction,"
            "level,first_held_ms,decision_ms,payload) VALUES(?,?,?,?,?,?,?,?)",
            (event_id, event_id, "ref", "DOWN", "NOTICE",
             first_held, decision_ms, "{}"))
    conn.commit()

    report = summarize_shadow_window(
        conn, start_ms=0, end_ms=9000, max_gap_ms=1000)
    assert report["status_observations"]["VALID"] == 6
    assert report["status_observations"]["STALE"] == 4
    assert report["eligible_event_count"] == 2
    assert report["excluded_event_count"] == 1
    assert report["research_window_eligible"] is False


def test_shadow_window_unknown_and_missing_evidence_fail_closed(tmp_path):
    conn = connect(str(tmp_path / "grid.sqlite3"), 1000)
    ensure_schema(conn)
    audit = QueueAudit(conn, "one", 0)
    audit.checkpoint(0, 0)
    audit.checkpoint(
        10000, 0, reference_status="VALID",
        feed_healthy=True, storage_degraded=False)
    report = summarize_shadow_window(
        conn, start_ms=0, end_ms=10000, max_gap_ms=5000)
    assert report["coverage_complete"] is False
    assert report["status_observations"]["UNKNOWN"] == 1
    assert report["research_window_eligible"] is False


def test_shadow_window_distinguishes_no_active_and_sync_uncertain(tmp_path):
    conn = connect(str(tmp_path / "grid.sqlite3"), 1000)
    ensure_schema(conn)
    audit = QueueAudit(conn, "one", 0)
    audit.checkpoint(
        0, 0, reference_status="NO_ACTIVE_REFERENCE",
        feed_healthy=True, storage_degraded=False)
    audit.checkpoint(
        1000, 0, reference_status="SYNC_UNCERTAIN",
        feed_healthy=True, storage_degraded=False)
    report = summarize_shadow_window(
        conn, start_ms=0, end_ms=1000, max_gap_ms=1000)
    assert report["status_observations"] == {
        "NO_ACTIVE_REFERENCE": 1, "SYNC_UNCERTAIN": 1}
    assert report["eligible_observations"] == 0
    assert report["coverage_complete"] is True
    assert report["research_window_eligible"] is False


def test_upgrade_v1_and_restart_preserves_overflow(tmp_path):
    path = tmp_path / "grid.sqlite3"
    conn = connect(str(path), 1000)
    ensure_schema(conn)
    conn.execute("DROP TABLE queue_audit_checkpoint")
    conn.execute("DROP TABLE queue_overflow_episode")
    conn.execute("DROP TABLE queue_audit_instance")
    conn.execute("UPDATE schema_meta SET value='1' WHERE key='version'")
    conn.commit()
    ensure_schema(conn)
    check_schema(conn)
    audit = QueueAudit(conn, "first", 1000)
    audit.drop(1200, 42, 301200)
    conn.close()

    reopened = connect(str(path), 1000)
    episode = reopened.execute("SELECT * FROM queue_overflow_episode").fetchone()
    assert episode["first_dropped_id"] == 42
    assert episode["dropped_count"] == 1
    assert episode["recovery_until_ms"] == 301200
    QueueAudit(reopened, "second", 2000)
    previous = reopened.execute("SELECT status FROM queue_overflow_episode").fetchone()
    assert previous["status"] == "INTERRUPTED"


def test_episode_flush_recovery_and_window_coverage(tmp_path):
    conn = connect(str(tmp_path / "grid.sqlite3"), 1000)
    ensure_schema(conn)
    audit = QueueAudit(conn, "one", 0)
    for t in range(0, 11000, 1000):
        if t == 2000:
            audit.drop(t, 10, 5000)
            audit.drop(t, 11, 5000)
        audit.checkpoint(t, recovery_until_ms=5000 if t < 5000 else 0)
    row = conn.execute("SELECT * FROM queue_overflow_episode").fetchone()
    assert row["dropped_count"] == 2
    assert row["last_dropped_id"] == 11
    assert row["status"] == "RECOVERED"
    report = summarize_window(conn, start_ms=0, end_ms=10000, max_gap_ms=1000)
    assert report["coverage_complete"] is True
    assert report["overflow_count"] == 2
    assert report["queue_window_eligible"] is False


def test_missing_coverage_is_unknown_not_zero(tmp_path):
    conn = connect(str(tmp_path / "grid.sqlite3"), 1000)
    ensure_schema(conn)
    audit = QueueAudit(conn, "one", 0)
    audit.checkpoint(0, recovery_until_ms=0)
    audit.checkpoint(1000, recovery_until_ms=0)
    audit.checkpoint(10000, recovery_until_ms=0)
    report = summarize_window(conn, start_ms=0, end_ms=10000, max_gap_ms=1000)
    assert report["coverage_complete"] is False
    assert report["overflow_count"] == 0
    assert report["queue_window_eligible"] is False
    assert report["gaps"] == [{"from_ms": 1000, "to_ms": 10000}]


def test_complete_no_overflow_window_is_queue_eligible(tmp_path):
    conn = connect(str(tmp_path / "grid.sqlite3"), 1000)
    ensure_schema(conn)
    audit = QueueAudit(conn, "one", 0)
    for t in range(0, 11000, 1000):
        audit.checkpoint(t, 0)
    report = summarize_window(conn, start_ms=0, end_ms=10000,
                              max_gap_ms=1000)
    assert report["queue_window_eligible"] is True
    assert report["overflow_count"] == 0


def test_unclean_restart_disqualifies_window_even_with_dense_checkpoints(tmp_path):
    conn = connect(str(tmp_path / "grid.sqlite3"), 1000)
    ensure_schema(conn)
    old = QueueAudit(conn, "old", 0)
    for t in range(0, 5000, 1000):
        old.checkpoint(t, 0)
    new = QueueAudit(conn, "new", 5000)
    for t in range(5000, 11000, 1000):
        new.checkpoint(t, 0)
    report = summarize_window(conn, start_ms=0, end_ms=10000,
                              max_gap_ms=1000)
    assert report["unclean_instances"][0]["instance_id"] == "old"
    assert report["queue_window_eligible"] is False
