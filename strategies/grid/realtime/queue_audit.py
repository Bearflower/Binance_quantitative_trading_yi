"""持久化队列逐出和覆盖证据；缺行或未封闭 episode 均不得推断为零逐出。"""
from __future__ import annotations

import sqlite3
from collections import Counter
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
                   audit_ok: bool = True, reference_status: str = "UNKNOWN",
                   feed_healthy: bool = False,
                   storage_degraded: bool = True) -> None:
        """逐拍持久化总数与预热；只有持续 checkpoint 才构成覆盖证据。"""
        if self._episode_id is not None:
            status = "RECOVERED" if now_ms >= recovery_until_ms else "OPEN"
            self._flush(status)
            if status == "RECOVERED":
                self._episode_id = None
                self._episode_count = 0
        self.conn.execute(
            "INSERT OR REPLACE INTO queue_audit_checkpoint("
            "instance_id, observed_at_ms, dropped_total, recovery_until_ms, "
            "audit_ok, reference_status, feed_healthy, storage_degraded) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (self.instance_id, now_ms, self.dropped_total, recovery_until_ms,
             int(audit_ok), reference_status, int(feed_healthy),
             int(storage_degraded)))
        self.conn.commit()

    def close(self, now_ms: int) -> None:
        if self._episode_id is not None:
            self._flush("INTERRUPTED")
        self.conn.execute(
            "UPDATE queue_audit_instance SET stopped_at_ms=? WHERE instance_id=?",
            (now_ms, self.instance_id))
        self.conn.commit()

    def prune(self, now_ms: int, retention_days: int = 35) -> None:
        """覆盖 30 个完整自然日验证段和跨日审计缓冲。"""
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
        (start_ms, end_ms))
    first = last = None
    failed_checkpoints: List[int] = []
    gaps: List[Dict[str, int]] = []
    for at, audit_ok in rows:
        if first is None:
            first = at
            if at - start_ms > max_gap_ms:
                gaps.append({"from_ms": start_ms, "to_ms": at})
        elif at - last > max_gap_ms:
            gaps.append({"from_ms": last, "to_ms": at})
        if not audit_ok:
            failed_checkpoints.append(at)
        last = at
    if first is None:
        gaps.append({"from_ms": start_ms, "to_ms": end_ms})
    elif end_ms - last > max_gap_ms:
        gaps.append({"from_ms": last, "to_ms": end_ms})
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
    complete = not gaps and not failed_checkpoints and not unclean and first is not None
    return {
        "start_ms": start_ms, "end_ms": end_ms,
        "first_checkpoint_ms": first,
        "last_checkpoint_ms": last,
        "max_gap_ms": max_gap_ms, "gaps": gaps,
        "coverage_complete": complete,
        "failed_checkpoints_ms": failed_checkpoints,
        "unclean_instances": unclean,
        "overflow_count": overflow,
        "overflow_count_is_lower_bound": uncertain,
        "episodes": episodes,
        "queue_window_eligible": complete and not episodes,
    }


def summarize_shadow_window(conn: sqlite3.Connection, *, start_ms: int,
                            end_ms: int, max_gap_ms: int = 5000) -> Dict:
    """逐拍汇总参考和行情资格；缺证据、旧版 UNKNOWN 均不可评估。"""
    if end_ms <= start_ms or max_gap_ms <= 0:
        raise ValueError("窗口和最大间隔必须为正")
    rows = conn.execute(
        "SELECT observed_at_ms, reference_status, feed_healthy, "
        "storage_degraded, audit_ok, recovery_until_ms "
        "FROM queue_audit_checkpoint WHERE observed_at_ms BETWEEN ? AND ? "
        "ORDER BY observed_at_ms", (start_ms, end_ms))
    statuses: Counter = Counter()
    status_observed_ms: Counter = Counter()
    periods: List[Dict] = []
    gaps: List[Dict[str, int]] = []
    first = last = None
    eligible_count = 0
    eligible_time_ms = 0
    previous_status = None
    previous_eligible = False

    def account_interval(next_ms: int) -> None:
        nonlocal eligible_time_ms
        if last is None:
            return
        span = next_ms - last
        # A long checkpoint gap cannot inherit the last observed state.
        observed = span if span <= max_gap_ms else min(span, 1000)
        if observed > 0:
            status_observed_ms[previous_status] += observed
            if previous_eligible:
                eligible_time_ms += observed

    for row in rows:
        at, status, feed_ok, storage_bad, audit_ok, recovery_until = row
        eligible = (status == "VALID" and bool(feed_ok) and
                    not storage_bad and bool(audit_ok) and
                    recovery_until <= at)
        statuses[status] += 1
        eligible_count += int(eligible)
        account_interval(at)
        if first is None:
            first = at
            if at - start_ms > max_gap_ms:
                gaps.append({"from_ms": start_ms, "to_ms": at})
        elif at - last > max_gap_ms:
            gaps.append({"from_ms": last, "to_ms": at})
        if (periods and periods[-1]["status"] == status and
                periods[-1]["eligible"] == eligible and
                last is not None and at - last <= max_gap_ms):
            periods[-1]["end_ms"] = at
            periods[-1]["observations"] += 1
        else:
            periods.append({
                "start_ms": at, "end_ms": at, "status": status,
                "eligible": eligible, "observations": 1})
        last = at
        previous_status = status
        previous_eligible = eligible
    account_interval(end_ms)
    if last is None:
        gaps.append({"from_ms": start_ms, "to_ms": end_ms})
    elif end_ms - last > max_gap_ms:
        gaps.append({"from_ms": last, "to_ms": end_ms})

    events = conn.execute(
        "SELECT event_id, decision_ms FROM event "
        "WHERE decision_ms BETWEEN ? AND ? AND level<>'HEALTH'",
        (start_ms, end_ms))
    eligible_events = excluded_events = 0
    for _, event_ms in events:
        checkpoint = conn.execute(
            "SELECT observed_at_ms, reference_status, feed_healthy, "
            "storage_degraded, audit_ok, recovery_until_ms "
            "FROM queue_audit_checkpoint WHERE observed_at_ms>=? "
            "ORDER BY observed_at_ms LIMIT 1", (event_ms,)).fetchone()
        if (checkpoint is not None and
                checkpoint[0] - event_ms <= max_gap_ms and
                checkpoint[1] == "VALID" and checkpoint[2] and
                not checkpoint[3] and checkpoint[4] and
                checkpoint[5] <= checkpoint[0]):
            eligible_events += 1
        else:
            excluded_events += 1
    complete = first is not None and not gaps
    total_ms = end_ms - start_ms
    unobserved_ms = total_ms - sum(status_observed_ms.values())
    return {
        "start_ms": start_ms, "end_ms": end_ms,
        "first_checkpoint_ms": first, "last_checkpoint_ms": last,
        "coverage_complete": complete, "gaps": gaps,
        "status_observations": dict(statuses),
        "status_observed_ms": dict(status_observed_ms),
        "unobserved_ms": unobserved_ms,
        "numeric_reference_availability":
            status_observed_ms["VALID"] / total_ms,
        "eligible_time_ms": eligible_time_ms,
        "eligible_observations": eligible_count,
        "periods": periods,
        "eligible_event_count": eligible_events,
        "excluded_event_count": excluded_events,
        "unlocated_legacy_event_count": conn.execute(
            "SELECT COUNT(*) FROM event WHERE level<>'HEALTH' "
            "AND decision_ms IS NULL").fetchone()[0],
        "research_window_eligible": complete and unobserved_ms == 0 and
        eligible_count == sum(statuses.values()),
    }
