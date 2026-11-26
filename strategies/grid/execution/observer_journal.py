"""将只读采集端的成交归属与执行器账本、通知账本对应起来。"""

from pathlib import Path
import sqlite3


def notice_path(env, pm):
    explicit = env.get("GRID_NOTIFICATION_DB")
    if explicit:
        return explicit
    if pm:
        return "/app/data/grid_notifications.sqlite3"
    execution = Path(env.get("GRID_EXECUTION_DB", "/app/data/grid_execution.sqlite3"))
    return str(execution.with_name(execution.stem + "_notifications.sqlite3"))


def verify_execution_identity(path, identity):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key='account_identity'").fetchone()
        if (row[0] if row else "pm") != identity:
            raise ValueError("grid executor and observer account mismatch")
    finally:
        conn.close()


def notice_identity(conn):
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='grid_account_binding' AND type='table'").fetchone()
    row = conn.execute("SELECT identity FROM grid_account_binding WHERE id=1").fetchone() if exists else None
    return row[0] if row else None


def bind_notices(conn, identity):
    conn.execute("BEGIN IMMEDIATE")
    try:
        bound = notice_identity(conn)
        if bound is None:
            if identity != "pm" and conn.execute("SELECT 1 FROM fact LIMIT 1").fetchone():
                raise ValueError("classic requires a fresh notification journal")
            conn.execute("CREATE TABLE IF NOT EXISTS grid_account_binding(id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL)")
            conn.execute("INSERT INTO grid_account_binding VALUES(1,?)", (identity,))
        elif bound != identity:
            raise ValueError("grid notification journal account mismatch")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
