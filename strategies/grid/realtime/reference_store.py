"""持久化层：grid_realtime.sqlite3 四表 schema、迁移锁、出口写侧与实时读侧。

口径权威：需求 §3.4/§4、计划 §2.6。
- 四表：reference（出口写）/ session（出口心跳写）/ event（实时写）/ outbox（实时写）。
- 迁移：ensure_schema 由一次性迁移命令持有迁移锁执行；业务进程只 check_schema。
- 读侧 current_snapshot 运行时判定五态（VALID/STALE/MISSING/INVALID/SYNC_UNCERTAIN），
  SYNC_UNCERTAIN 条件：无会话 / 会话非 ACTIVE / 心跳超时 / 存在未决发送。
- 任何失败都不得伪造 SENT；UNKNOWN/未决行不能作为恢复依据。
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, Optional, Tuple

from .rules import ReferenceSnapshot, validate_snapshot

SCHEMA_VERSION = 1

# 会话状态
ACTIVE = "ACTIVE"
SYNC_UNCERTAIN = "SYNC_UNCERTAIN"
CLOSED = "CLOSED"

# 发送状态
PREPARED, SENDING = "PREPARED", "SENDING"
SENT, FAILED, UNKNOWN = "SENT", "FAILED", "UNKNOWN"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- reference：权威建议快照（小时出口写，实时进程读）
CREATE TABLE IF NOT EXISTS reference (
  reference_id      TEXT PRIMARY KEY,
  session_id        INTEGER NOT NULL,
  seq               INTEGER NOT NULL,
  send_status       TEXT NOT NULL,
  symbol            TEXT NOT NULL,
  calculated_at_ms  INTEGER NOT NULL,
  effective_at_ms   INTEGER,
  grid_lower TEXT NOT NULL, grid_upper TEXT NOT NULL,
  stop_lower TEXT NOT NULL, stop_upper TEXT NOT NULL,
  stop_move_up_price TEXT, stop_move_down_price TEXT,
  market_state TEXT, atr TEXT, adx_1h TEXT, adx_4h TEXT,
  config_version TEXT, overrides_version TEXT, config_hash TEXT,
  message_id TEXT, source TEXT,
  created_at_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reference_session_seq
  ON reference(session_id, seq);

-- session：出口会话与心跳快照
CREATE TABLE IF NOT EXISTS session (
  session_id          INTEGER PRIMARY KEY,
  current_seq         INTEGER NOT NULL DEFAULT 0,
  current_reference_id TEXT,
  pending_seq         INTEGER,
  status              TEXT NOT NULL,
  updated_at_ms       INTEGER NOT NULL
);

-- event：分级事件（实时进程写，唯一键幂等）
CREATE TABLE IF NOT EXISTS event (
  event_id      TEXT PRIMARY KEY,
  episode_id    TEXT NOT NULL,
  reference_id  TEXT NOT NULL,
  direction     TEXT NOT NULL,
  level         TEXT NOT NULL,
  first_held_ms INTEGER,
  sent_ms       INTEGER,
  config_hash   TEXT,
  payload       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_episode ON event(episode_id);

-- outbox：投递事实锁存（锁存语义，不因 TTL/反弹删除）
CREATE TABLE IF NOT EXISTS outbox (
  outbox_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id       TEXT NOT NULL,
  kind           TEXT NOT NULL,
  status         TEXT NOT NULL,
  locked_at_ms   INTEGER NOT NULL,
  ttl_deadline_ms INTEGER,
  retry_count    INTEGER NOT NULL DEFAULT 0,
  next_retry_ms  INTEGER NOT NULL DEFAULT 0,
  payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
"""


@dataclass(frozen=True)
class SessionState:
    """出口进程的不可变状态快照（心跳只转嫁，不判定成功）。"""

    session_id: int
    current_seq: int
    current_reference_id: Optional[str]
    pending_seq: Optional[int]


# ───────────────────── 连接与迁移 ─────────────────────

def connect(path: str, busy_timeout_ms: int) -> sqlite3.Connection:
    """打开 WAL 连接（短事务 + 有限 busy_timeout；不跨进程共享连接）。"""
    conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000)
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """一次性迁移：建表/索引并登记版本（由迁移命令在业务进程启动前调用）。"""
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
        "ON CONFLICT(key) DO NOTHING", (str(SCHEMA_VERSION),))
    conn.commit()


def check_schema(conn: sqlite3.Connection) -> None:
    """业务进程启动校验：版本不符或四表缺失即拒绝运行（不并发迁移）。"""
    meta_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
    row = (conn.execute(
        "SELECT value FROM schema_meta WHERE key='version'").fetchone()
        if meta_exists else None)
    if row is None or row["value"] != str(SCHEMA_VERSION):
        raise RuntimeError(
            f"schema 版本不符：实际 {row['value'] if row else None}，"
            f"预期 {SCHEMA_VERSION}；请先执行迁移命令")
    required = ("reference", "session", "event", "outbox")
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [name for name in required if name not in tables]
    if missing:
        raise RuntimeError(f"持久化缺少表: {', '.join(missing)}；请先执行迁移命令")


def make_reference_id(calculated_at_ms: int, grid_lower: Decimal,
                      grid_upper: Decimal, stop_lower: Decimal,
                      stop_upper: Decimal) -> str:
    """确定性 reference_id：同一消息重试不生成新版本。"""
    basis = f"{calculated_at_ms}|{grid_lower}|{grid_upper}|{stop_lower}|{stop_upper}"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]
    return f"ref-{calculated_at_ms}-{digest}"


# ───────────────────── 出口写侧 ─────────────────────

_REFERENCE_COLUMNS = (
    "reference_id", "session_id", "seq", "send_status", "symbol",
    "calculated_at_ms", "effective_at_ms", "grid_lower", "grid_upper",
    "stop_lower", "stop_upper", "stop_move_up_price", "stop_move_down_price",
    "market_state", "atr", "adx_1h", "adx_4h", "config_version",
    "overrides_version", "config_hash", "message_id", "source", "created_at_ms")


class ExportStore:
    """小时出口进程的写入端：reference + session（含心跳转嫁）。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def begin_session(self, now_ms: int) -> int:
        """开启新出口会话（session_id 单调递增）。"""
        row = self._conn.execute("SELECT MAX(session_id) AS m FROM session").fetchone()
        session_id = (row["m"] or 0) + 1
        self._conn.execute(
            "INSERT INTO session(session_id, status, updated_at_ms) "
            "VALUES(?, ?, ?)", (session_id, ACTIVE, now_ms))
        self._conn.commit()
        return session_id

    def prepare(self, now_ms: int, state: SessionState,
                snapshot: ReferenceSnapshot) -> str:
        """发送前登记 PREPARED + 置未决序号（§4.2.1）。"""
        seq = state.current_seq + 1
        reference_id = make_reference_id(
            snapshot.calculated_at_ms, snapshot.grid_lower, snapshot.grid_upper,
            snapshot.stop_lower, snapshot.stop_upper)
        values = self._reference_values(reference_id, state.session_id, seq,
                                        PREPARED, snapshot, now_ms)
        placeholders = ",".join("?" for _ in _REFERENCE_COLUMNS)
        self._conn.execute(
            f"INSERT INTO reference({','.join(_REFERENCE_COLUMNS)}) "
            f"VALUES({placeholders})", values)
        self._set_pending(state, seq, now_ms)
        return reference_id

    def mark_sending(self, reference_id: str) -> None:
        """外部通知调用前置 SENDING。"""
        self._update_status(reference_id, SENDING)

    def mark_sent(self, reference_id: str, effective_at_ms: int,
                  state: SessionState, now_ms: int,
                  message_id: Optional[str] = None) -> None:
        """明确成功：置 SENT + 生效时间，序号前移、清未决。"""
        seq = state.pending_seq or state.current_seq
        self._conn.execute(
            "UPDATE reference SET send_status=?, effective_at_ms=?, message_id=? "
            "WHERE reference_id=?", (SENT, effective_at_ms, message_id, reference_id))
        new_state = SessionState(state.session_id, seq, reference_id, None)
        self._write_session_row(new_state, ACTIVE, now_ms)
        self._conn.commit()

    def mark_failed(self, reference_id: str, state: SessionState,
                    now_ms: int) -> None:
        """明确失败：置 FAILED 清未决（可恢复旧参考）。"""
        self._finish_unsent(reference_id, FAILED, state, now_ms)

    def mark_unknown(self, reference_id: str, state: SessionState,
                     now_ms: int) -> None:
        """结果未知：置 UNKNOWN 清未决（不假定成功或失败）。"""
        self._finish_unsent(reference_id, UNKNOWN, state, now_ms)

    def set_sync_uncertain(self, state: SessionState, now_ms: int) -> None:
        """持久化/交接失败：会话置 SYNC_UNCERTAIN，暂停操作建议。"""
        self._write_session_row(state, SYNC_UNCERTAIN, now_ms)
        self._conn.commit()

    def close_session(self, now_ms: int) -> None:
        """出口进程正常关闭会话。"""
        self._conn.execute(
            "UPDATE session SET status=?, updated_at_ms=?", (CLOSED, now_ms))
        self._conn.commit()

    def publish_heartbeat(self, state: SessionState, now_ms: int) -> bool:
        """转嫁心跳状态（不替出口判定成功）；行不存在返回 False。"""
        return write_heartbeat(self._conn, state, now_ms)

    def _finish_unsent(self, reference_id: str, status: str,
                       state: SessionState, now_ms: int) -> None:
        self._update_status(reference_id, status)
        cleared = SessionState(state.session_id, state.current_seq,
                               state.current_reference_id, None)
        self._write_session_row(cleared, ACTIVE, now_ms)
        self._conn.commit()

    def _set_pending(self, state: SessionState, seq: int, now_ms: int) -> None:
        pending = SessionState(state.session_id, state.current_seq,
                               state.current_reference_id, seq)
        self._write_session_row(pending, ACTIVE, now_ms)
        self._conn.commit()

    def _write_session_row(self, state: SessionState, status: str,
                           now_ms: int) -> None:
        self._conn.execute(
            "UPDATE session SET current_seq=?, current_reference_id=?, "
            "pending_seq=?, status=?, updated_at_ms=? WHERE session_id=?",
            (state.current_seq, state.current_reference_id, state.pending_seq,
             status, now_ms, state.session_id))

    def _update_status(self, reference_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE reference SET send_status=? WHERE reference_id=?",
            (status, reference_id))

    def _reference_values(self, reference_id: str, session_id: int, seq: int,
                          status: str, snapshot: ReferenceSnapshot,
                          now_ms: int) -> Tuple:
        optional = (
            snapshot.stop_move_up_price, snapshot.stop_move_down_price,
            snapshot.market_state, snapshot.atr, snapshot.adx_1h,
            snapshot.adx_4h, snapshot.config_version,
            snapshot.overrides_version, snapshot.config_hash,
            snapshot.message_id, snapshot.source)
        head = (
            reference_id, session_id, seq, status, snapshot.symbol,
            snapshot.calculated_at_ms, snapshot.effective_at_ms,
            _text(snapshot.grid_lower), _text(snapshot.grid_upper),
            _text(snapshot.stop_lower), _text(snapshot.stop_upper))
        return head + tuple(_text(v) for v in optional) + (now_ms,)


def write_heartbeat(conn: sqlite3.Connection, state: SessionState,
                    now_ms: int) -> bool:
    """心跳线程/出口共用：只转嫁不可变状态并刷新更新时间。"""
    cursor = conn.execute(
        "UPDATE session SET current_seq=?, current_reference_id=?, "
        "pending_seq=?, updated_at_ms=? WHERE session_id=? AND status=?",
        (state.current_seq, state.current_reference_id, state.pending_seq,
         now_ms, state.session_id, ACTIVE))
    conn.commit()
    return cursor.rowcount == 1


# ───────────────────── 实时读侧 ─────────────────────

class ReferenceReader:
    """实时进程读取端：五态判定 + 历史快照（只读 reference/session）。"""

    def __init__(self, conn: sqlite3.Connection, cfg: Dict):
        self._conn = conn
        self._cfg = cfg

    def current_snapshot(
            self, now_ms: int
    ) -> Tuple[Optional[ReferenceSnapshot], str]:
        """运行时判定 (快照, 五态)；非 VALID/STALE 时快照为 None（STALE 保留）。"""
        session = self._latest_session()
        if session is None:
            return None, SYNC_UNCERTAIN
        if session["status"] != ACTIVE:
            return None, SYNC_UNCERTAIN
        silence = self._cfg["reference_sync"]["max_silence_seconds"] * 1000
        if now_ms - session["updated_at_ms"] > silence:
            return None, SYNC_UNCERTAIN
        if session["pending_seq"] is not None:
            return None, SYNC_UNCERTAIN
        row = self._latest_sent()
        if row is None:
            return None, "MISSING"
        if row["session_id"] != session["session_id"]:
            # AC-21：旧会话遗留的 SENT 不能当作当前参考；出口重启后须等下一次明确成功
            return None, SYNC_UNCERTAIN
        snapshot = _row_to_snapshot(row)
        try:
            validate_snapshot(snapshot, self._cfg["symbol"])
        except ValueError:
            return None, "INVALID"
        age = now_ms - row["effective_at_ms"]
        if age > self._cfg["reference"]["max_age_seconds"] * 1000:
            return snapshot, "STALE"
        return snapshot, "VALID"

    def historical_snapshot(self) -> Optional[ReferenceSnapshot]:
        """SYNC_UNCERTAIN 时取最新一份合法 SENT 快照（未确认/非法行不可用）。"""
        row = self._latest_sent()
        if row is None:
            return None
        snapshot = _row_to_snapshot(row)
        try:
            validate_snapshot(snapshot, self._cfg["symbol"])
        except ValueError:
            return None
        return snapshot

    def _latest_session(self) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM session ORDER BY session_id DESC LIMIT 1").fetchone()

    def _latest_sent(self) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM reference WHERE send_status=? "
            "ORDER BY session_id DESC, seq DESC LIMIT 1", (SENT,)).fetchone()


def _row_to_snapshot(row: sqlite3.Row) -> ReferenceSnapshot:
    """读行转快照（运行时不解析消息文本；边界缺失由 validate 拒绝）。"""
    def money(key: str) -> Decimal:
        return Decimal(str(row[key]))

    def opt_decimal(key: str) -> Optional[Decimal]:
        value = row[key]
        return None if value is None else Decimal(str(value))

    def optional(key: str) -> Optional[str]:
        value = row[key]
        return None if value is None else str(value)

    return ReferenceSnapshot(
        reference_id=row["reference_id"], symbol=row["symbol"],
        calculated_at_ms=row["calculated_at_ms"],
        effective_at_ms=row["effective_at_ms"],
        grid_lower=money("grid_lower"), grid_upper=money("grid_upper"),
        stop_lower=money("stop_lower"), stop_upper=money("stop_upper"),
        stop_move_up_price=opt_decimal("stop_move_up_price"),
        stop_move_down_price=opt_decimal("stop_move_down_price"),
        market_state=optional("market_state"), atr=opt_decimal("atr"),
        adx_1h=opt_decimal("adx_1h"), adx_4h=opt_decimal("adx_4h"),
        config_version=optional("config_version"),
        overrides_version=optional("overrides_version"),
        config_hash=optional("config_hash"),
        message_id=optional("message_id"), source=optional("source"))


def _text(value) -> Optional[str]:
    """None 透传，其余转字符串（sqlite 不绑定 Decimal）。"""
    return None if value is None else str(value)


def _main(argv=None) -> int:
    """迁移命令：python -m strategies.grid.realtime.reference_store --db-path PATH"""
    parser = argparse.ArgumentParser(description="grid_realtime 一次性迁移")
    parser.add_argument("--db-path", required=True, help="sqlite3 绝对路径")
    parser.add_argument("--busy-timeout-ms", type=int, default=1000)
    args = parser.parse_args(argv)
    path = Path(args.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(path), args.busy_timeout_ms)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    print(f"迁移完成：{path}（schema v{SCHEMA_VERSION}）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
