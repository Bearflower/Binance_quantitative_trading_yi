"""heartbeat.py 测试：线程启停、成功转嫁、失败计数不抛异常、重复启动安全。"""
import time

from strategies.grid.heartbeat import HeartbeatThread
from strategies.grid.realtime.reference_store import SessionState


def test_start_stop_lifecycle(db_setup):
    conn, db_path, sid = db_setup
    state = SessionState(sid, 0, None, None)
    hb = HeartbeatThread(
        lambda: state, db_path, 0.02, 1000, lambda: 1234)
    hb.start()
    assert hb._thread.is_alive() and hb._thread.daemon
    hb.stop(timeout=2)
    assert not hb._thread.is_alive()
    conn.close()


def test_publish_success_updates_session(db_setup):
    conn, db_path, sid = db_setup
    state = SessionState(sid, 5, "ref-1", None)
    hb = HeartbeatThread(
        lambda: state, db_path, 1, 1000, lambda: 4242)
    hb._publish_once(conn)
    assert hb.failure_count == 0
    session = conn.execute(
        "SELECT * FROM session WHERE session_id=?", (sid,)).fetchone()
    assert session["updated_at_ms"] == 4242 and session["current_seq"] == 5


def test_publish_missing_row_increments_failure(db_setup):
    conn, db_path, _ = db_setup
    missing = SessionState(999, 0, None, None)
    hb = HeartbeatThread(
        lambda: missing, db_path, 1, 1000, lambda: 1)
    hb._publish_once(conn)
    assert hb.failure_count == 1


def test_publish_exception_increments_failure(db_setup):
    conn, db_path, sid = db_setup
    state = SessionState(sid, 0, None, None)
    hb = HeartbeatThread(
        lambda: state, db_path, 1, 1000, lambda: 1)
    conn.close()  # 关闭连接后写入抛 ProgrammingError
    hb._publish_once(conn)
    assert hb.failure_count == 1


def test_state_getter_exception_increments_failure(db_setup):
    conn, db_path, sid = db_setup

    def boom():
        raise RuntimeError("状态获取失败")

    hb = HeartbeatThread(boom, db_path, 1, 1000, lambda: 1)
    hb._publish_once(conn)
    assert hb.failure_count == 1
    conn.close()


def test_duplicate_start_is_safe(db_setup):
    conn, db_path, sid = db_setup
    state = SessionState(sid, 0, None, None)
    hb = HeartbeatThread(
        lambda: state, db_path, 0.02, 1000, lambda: 1)
    hb.start()
    first = hb._thread
    hb.start()  # 不应创建第二个线程
    assert hb._thread is first
    hb.stop(2)
    conn.close()


def test_stop_without_start_is_safe(db_setup):
    _, db_path, _ = db_setup
    hb = HeartbeatThread(
        lambda: None, db_path, 1, 1000, lambda: 1)
    hb.stop()  # 未启动直接停止不报错


def test_thread_publishes_at_interval(db_setup):
    """真实线程跑若干拍：session 更新时间被心跳刷新。"""
    conn, db_path, sid = db_setup
    state = SessionState(sid, 0, None, None)
    counter = {"v": 0}

    def clock():
        counter["v"] += 100
        return counter["v"]

    hb = HeartbeatThread(
        lambda: state, db_path, 0.01, 1000, clock)
    hb.start()
    time.sleep(0.05)
    hb.stop(2)
    updated = conn.execute(
        "SELECT updated_at_ms FROM session WHERE session_id=?",
        (sid,)).fetchone()[0]
    assert updated > 0
    conn.close()
