"""持久化队列逐出和覆盖证据；缺行或未封闭 episode 均不得推断为零逐出。"""
from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional


class QueueAudit:
    """一个实时进程的审计写侧；只有迁移后的连接可构造。"""

    def __init__(self, conn: sqlite3.Connection, instance_id: str, started_at_ms: int):
        self.conn = conn
        self.instance_id = instance_id
        self.dropped_total = 0
        self._episode_id: Optional[int] = None
        self._episode_count = 0
        self._last_id = 0
        self._last_ms = 0
        self._recovery_until = 0
        conn.execute(
            "UPDATE queue_overflow_episode SET status='INTERRUPTED' "
            "WHERE status='OPEN' AND instance_id<>?", (instance_id,))
        conn.execute(
            "INSERT INTO queue_audit_instance(instance_id, started_at_ms) VALUES(?, ?)",
            (instance_id, started_at_ms))
        conn.commit()

    def drop(self, now_ms: int, trade_id: int, recovery_until_ms: int) -> None:
        """首笔立即落库；后续逐出按拍合并，未封闭行表示重启前可能少计。"""
        if self._episode_id is None or now_ms >= self._recovery_until:
            if self._episode_id is not None:
                self._flush("RECOVERED")
            cursor = self.conn.execute(
                "INSERT INTO queue_overflow_episode("
                "instance_id, first_dropped_ms, last_dropped_ms, "
                "first_dropped_id, last_dropped_id, dropped_count, "
                "recovery_until_ms, status) VALUES(?,?,?,?,?,?,?,'OPEN')",
                (self.instance_id, now_ms, now_ms, trade_id, trade_id, 1,
                 recovery_until_ms))
            self._episode_id = cursor.lastrowid
            self._episode_count = 1
            self.conn.commit()
        else:
            self._episode_count += 1
        self.dropped_total += 1
        self._last_id = trade_id
        self._last_ms = now_ms
        self._recovery_until = recovery_until_ms

    def checkpoint(self, now_ms: int, recovery_until_ms: int,
                   audit_ok: bool = True) -> None:
        """逐拍持久化总数与预热；只有持续 checkpoint 才构成覆盖证据。"""
        if self._episode_id is not None:
            status = "RECOVERED" if now_ms >= recovery_until_ms else "OPEN"
            self._flush(status)
            if status == "RECOVERED":
                self._episode_id = None
                self._episode_count = 0
        self.conn.execute(
            "INSERT OR REPLACE INTO queue_audit_checkpoint("
            "instance_id, observed_at_ms, dropped_total, recovery_until_ms, audit_ok) "
            "VALUES(?,?,?,?,?)",
            (self.instance_id, now_ms, self.dropped_total, recovery_until_ms,
             int(audit_ok)))
        self.conn.commit()

    def close(self, now_ms: int) -> None:
        if self._episode_id is not None:
            self._flush("INTERRUPTED")
        self.conn.execute(
            "UPDATE queue_audit_instance SET stopped_at_ms=? WHERE instance_id=?",
            (now_ms, self.instance_id))
        self.conn.commit()

    def prune(self, now_ms: int, retention_days: int = 5) -> None:
        """保留至少 72 小时加缓冲；episode 保留较长以便事件追溯。"""
        cutoff = now_ms - retention_days * 86400000
        self.conn.execute(
            "DELETE FROM queue_audit_checkpoint WHERE observed_at_ms<?", (cutoff,))
        self.conn.commit()

    def _flush(self, status: str) -> None:
        self.conn.execute(
            "UPDATE queue_overflow_episode SET last_dropped_ms=?, last_dropped_id=?, "
            "dropped_count=?, recovery_until_ms=?, status=? WHERE episode_id=?",
            (self._last_ms, self._last_id, self._episode_count,
             self._recovery_until, status, self._episode_id))
        self.conn.commit()


def summarize_window(conn: sqlite3.Connection, *, start_ms: int,
                     end_ms: int, max_gap_ms: int = 5000) -> Dict:
    """只读评估队列证据；跨进程重启允许，但时间轴必须连续。"""
    if end_ms <= start_ms or max_gap_ms <= 0:
        raise ValueError("窗口和最大间隔必须为正")
    rows = conn.execute(
        "SELECT observed_at_ms, audit_ok FROM queue_audit_checkpoint "
        "WHERE observed_at_ms BETWEEN ? AND ? ORDER BY observed_at_ms",
        (start_ms, end_ms)).fetchall()
    times = [r[0] for r in rows]
    failed_checkpoints = [r[0] for r in rows if not r[1]]
    gaps: List[Dict[str, int]] = []
    if not times:
        gaps.append({"from_ms": start_ms, "to_ms": end_ms})
    else:
        if times[0] - start_ms > max_gap_ms:
            gaps.append({"from_ms": start_ms, "to_ms": times[0]})
        gaps.extend({"from_ms": left, "to_ms": right}
                    for left, right in zip(times, times[1:])
                    if right - left > max_gap_ms)
        if end_ms - times[-1] > max_gap_ms:
            gaps.append({"from_ms": times[-1], "to_ms": end_ms})
    episodes = [dict(r) for r in conn.execute(
        "SELECT instance_id, first_dropped_ms, last_dropped_ms, "
        "first_dropped_id, last_dropped_id, dropped_count, "
        "recovery_until_ms, status FROM queue_overflow_episode "
        "WHERE first_dropped_ms<=? AND recovery_until_ms>=? "
        "ORDER BY first_dropped_ms", (end_ms, start_ms)).fetchall()]
    uncertain = any(r["status"] != "RECOVERED" for r in episodes)
    latest_instance = conn.execute(
        "SELECT instance_id FROM queue_audit_instance "
        "ORDER BY started_at_ms DESC, rowid DESC LIMIT 1").fetchone()
    unclean = [dict(r) for r in conn.execute(
        "SELECT instance_id, started_at_ms FROM queue_audit_instance "
        "WHERE stopped_at_ms IS NULL AND instance_id<>? AND started_at_ms<=? "
        "AND (started_at_ms>=? OR EXISTS("
        "SELECT 1 FROM queue_audit_checkpoint c "
        "WHERE c.instance_id=queue_audit_instance.instance_id "
        "AND c.observed_at_ms>=?))",
        (latest_instance[0] if latest_instance else "", end_ms,
         start_ms, start_ms)).fetchall()]
    overflow = sum(r["dropped_count"] for r in episodes)
    complete = not gaps and not failed_checkpoints and not unclean and bool(times)
    return {
        "start_ms": start_ms, "end_ms": end_ms,
        "first_checkpoint_ms": times[0] if times else None,
        "last_checkpoint_ms": times[-1] if times else None,
        "max_gap_ms": max_gap_ms, "gaps": gaps,
        "coverage_complete": complete,
        "failed_checkpoints_ms": failed_checkpoints,
        "unclean_instances": unclean,
        "overflow_count": overflow,
        "overflow_count_is_lower_bound": uncertain,
        "episodes": episodes,
        "queue_window_eligible": complete and not episodes,
    }
