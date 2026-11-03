"""队列审计须持久、可判定覆盖范围，不能把缺证据写成零溢出。"""
from strategies.grid.realtime.reference_store import connect, ensure_schema, check_schema
from strategies.grid.realtime.queue_audit import QueueAudit, summarize_window


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
