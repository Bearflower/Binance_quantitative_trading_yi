"""投递层：事件落库、outbox 优先级泵、TTL/锁存事实、退避重试与容量维护。

口径权威：需求 §4.4/§8.2、计划 §2.6。
- event 唯一键幂等；outbox 锁存事实（BOUNDARY_REACHED / HISTORICAL_REFERENCE_CROSSED）
  不适用 TTL、不因反弹删除；其余建议有 event_ttl_seconds。
- 发送适配为单次发送（True/False/None 三态，None=渠道结果未知）；重试退避由
  本层调度，不调用会内部长退避的旧发送路径。
- 发送前核对条件（condition_holds）；超时仅留档；>30s 送达由 render 改补报文案。
- shadow：只记录（status=SHADOW）；唯一例外 RESEARCH 通道（§9.3：同 episode +
  独立频率上限）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

import aiohttp

from .state import AlertEvent

# outbox 状态
PENDING, SENT, FAILED, UNKNOWN = "PENDING", "SENT", "FAILED", "UNKNOWN"
EXPIRED, CANCELLED, SHADOW = "EXPIRED", "CANCELLED", "SHADOW"

# 锁存事实类别
LATCH_KINDS = ("BOUNDARY_REACHED", "HISTORICAL_REFERENCE_CROSSED")
RESEARCH = "RESEARCH"

# 优先级排序（数字小优先）
_PRIORITY = {
    "BOUNDARY_REACHED": 0, "HISTORICAL_REFERENCE_CROSSED": 1,
    RESEARCH: 2, "URGENT": 3, "NOTICE": 4, "HEALTH": 5}

Sender = Callable[[str], Awaitable[Optional[bool]]]
RenderFunc = Callable[[str, sqlite3.Row, int], str]
ConditionFunc = Callable[[str], bool]


class Delivery:
    """实时进程的 event/outbox 写入与投递泵。"""

    def __init__(self, conn: sqlite3.Connection, cfg: Dict):
        self._conn = conn
        self._cfg = cfg

    def record(self, event: AlertEvent, payload: str, now_ms: int,
               config_hash: Optional[str] = None) -> bool:
        """落 event（幂等）+ outbox；返回是否新增（重复 event_id 返回 False）。"""
        inserted_event = self._insert_event(event, payload, config_hash)
        if not inserted_event:
            return False
        self._insert_outbox(event, payload, now_ms)
        return True

    def record_research(self, event: AlertEvent, payload: str,
                        now_ms: int) -> bool:
        """shadow 研究参考通道：§9.3 频率护栏（同 episode + 间隔），开关须显式开启。"""
        shadow_cfg = self._cfg.get("shadow_research", {})
        if not shadow_cfg.get("enabled"):
            return False
        if self._research_blocked(event, now_ms, shadow_cfg):
            return False
        return self._insert_research(event, payload, now_ms)

    async def pump(self, sender: Sender, now_ms: int, *,
                   render: Optional[RenderFunc] = None,
                   condition_holds: Optional[ConditionFunc] = None) -> int:
        """发送所有到期 PENDING；返回本次尝试发送的条数。"""
        self.evict_expired(now_ms)
        rows = self._due_rows(now_ms)
        attempted = 0
        for row in rows:
            if not self._ready_to_send(row, now_ms, condition_holds):
                continue
            text = self._render(row, now_ms, render)
            outcome = await sender(text)
            self._handle_outcome(row, outcome, now_ms)
            attempted += 1
        return attempted

    def evict_expired(self, now_ms: int) -> int:
        """淘汰未发送且 TTL 已过的非锁存消息（含待重试行；高优先级腾位，仅留档）。"""
        cursor = self._conn.execute(
            "UPDATE outbox SET status=? WHERE status IN (?,?,?) "
            "AND ttl_deadline_ms IS NOT NULL AND ttl_deadline_ms < ?",
            (CANCELLED, PENDING, FAILED, UNKNOWN, now_ms))
        self._conn.commit()
        return cursor.rowcount

    def maintenance(self, now_ms: int) -> Dict:
        """清理到期已送达/审计行；返回容量状态（db+wal+shm 合计 vs max_bytes）。"""
        deleted = self._cleanup_retention(now_ms)
        total = _database_total(self._conn)
        max_bytes = self._cfg["storage"]["max_bytes"]
        self._conn.commit()
        return {"deleted_rows": deleted, "total_bytes": total,
                "max_bytes": max_bytes, "storage_degraded": total > max_bytes}

    # ───────── 写入 ─────────

    def _insert_event(self, event: AlertEvent, payload: str,
                      config_hash: Optional[str]) -> bool:
        try:
            self._conn.execute(
                "INSERT INTO event(event_id, episode_id, reference_id, direction, "
                "level, first_held_ms, config_hash, payload) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (event.event_id, event.episode_id, event.reference_id,
                 event.direction, event.level, event.first_held_ms,
                 config_hash, payload))
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def _insert_outbox(self, event: AlertEvent, payload: str,
                       now_ms: int) -> None:
        shadow = self._cfg.get("mode") == "shadow"
        status = SHADOW if shadow else PENDING
        ttl = None if event.level in LATCH_KINDS else self._ttl(now_ms)
        self._conn.execute(
            "INSERT INTO outbox(event_id, kind, status, locked_at_ms, "
            "ttl_deadline_ms, payload) VALUES(?,?,?,?,?,?)",
            (event.event_id, event.level, status, now_ms, ttl, payload))
        self._conn.commit()

    def _insert_research(self, event: AlertEvent, payload: str,
                         now_ms: int) -> bool:
        inserted = self._insert_event(event, payload, None)
        if not inserted:
            return False
        ttl = None if event.level in LATCH_KINDS else self._ttl(now_ms)
        self._conn.execute(
            "INSERT INTO outbox(event_id, kind, status, locked_at_ms, "
            "ttl_deadline_ms, payload) VALUES(?,?,?,?,?,?)",
            (event.event_id, RESEARCH, PENDING, now_ms, ttl, payload))
        self._conn.commit()
        return True

    def _research_blocked(self, event: AlertEvent, now_ms: int,
                          shadow_cfg: Dict) -> bool:
        """同 episode 距上次研究参考“已送达”不足 interval → 拦截（§9.3）。

        仅统计真正送达（SENT，sent_ms 非空）的研究消息；未送达/结果未知/已取消
        的行不得用来压制后续研究提醒。
        """
        interval_ms = shadow_cfg["notice_interval_seconds"] * 1000
        row = self._conn.execute(
            "SELECT 1 FROM event e JOIN outbox o ON e.event_id=o.event_id "
            "WHERE e.episode_id=? AND o.kind=? AND o.status=? "
            "AND e.sent_ms IS NOT NULL AND e.sent_ms > ? LIMIT 1",
            (event.episode_id, RESEARCH, SENT, now_ms - interval_ms)).fetchone()
        return row is not None

    # ───────── 发送 ─────────

    def _ready_to_send(self, row: sqlite3.Row, now_ms: int,
                       condition_holds: Optional[ConditionFunc]) -> bool:
        """锁存事实恒可发；建议类条件不再成立则取消。"""
        if row["kind"] in LATCH_KINDS or row["kind"] == RESEARCH:
            return True
        if condition_holds is not None and not condition_holds(row["event_id"]):
            self._mark_status(row, CANCELLED, now_ms)
            return False
        return True

    def _handle_outcome(self, row: sqlite3.Row,
                        outcome: Optional[bool], now_ms: int) -> None:
        if outcome is True:
            self._mark_sent(row, now_ms)
        elif outcome is False:
            self._mark_retry(row, now_ms)
        else:
            self._mark_unknown(row, now_ms)

    def _mark_sent(self, row: sqlite3.Row, now_ms: int) -> None:
        self._conn.execute(
            "UPDATE outbox SET status=? WHERE outbox_id=?", (SENT, row["outbox_id"]))
        self._conn.execute(
            "UPDATE event SET sent_ms=? WHERE event_id=?",
            (now_ms, row["event_id"]))
        self._conn.commit()

    def _mark_retry(self, row: sqlite3.Row, now_ms: int) -> None:
        retries = row["retry_count"] + 1
        wait = self._backoff_seconds(retries)
        self._conn.execute(
            "UPDATE outbox SET status=?, retry_count=?, next_retry_ms=? "
            "WHERE outbox_id=?",
            (FAILED, retries, now_ms + int(wait * 1000), row["outbox_id"]))
        self._conn.commit()

    def _mark_unknown(self, row: sqlite3.Row, now_ms: int) -> None:
        retries = row["retry_count"] + 1
        wait = self._backoff_seconds(retries)
        self._conn.execute(
            "UPDATE outbox SET status=?, retry_count=?, next_retry_ms=? "
            "WHERE outbox_id=?",
            (UNKNOWN, retries, now_ms + int(wait * 1000), row["outbox_id"]))
        self._conn.commit()

    def _mark_status(self, row: sqlite3.Row, status: str,
                     now_ms: int) -> None:
        self._conn.execute(
            "UPDATE outbox SET status=? WHERE outbox_id=?",
            (status, row["outbox_id"]))
        self._conn.commit()

    def _render(self, row: sqlite3.Row, now_ms: int,
                render: Optional[RenderFunc]) -> str:
        if render is None:
            return row["payload"]
        return render(row["payload"], row, now_ms)

    # ───────── 辅助 ─────────

    def _due_rows(self, now_ms: int) -> List[sqlite3.Row]:
        """到期行：PENDING 立即可发；FAILED/UNKNOWN 到退避点后重新拾取。"""
        order = f"CASE kind {_when_priority()} ELSE {len(_PRIORITY)} END"
        return self._conn.execute(
            f"SELECT * FROM outbox WHERE status IN (?,?,?) AND next_retry_ms<=? "
            f"ORDER BY {order}, locked_at_ms",
            (PENDING, FAILED, UNKNOWN, now_ms)).fetchall()

    def _ttl(self, now_ms: int) -> int:
        return now_ms + self._cfg["delivery"]["event_ttl_seconds"] * 1000

    def _backoff_seconds(self, retries: int) -> float:
        delivery_cfg = self._cfg["delivery"]
        initial = delivery_cfg["retry_initial_seconds"]
        cap = delivery_cfg["retry_max_seconds"]
        return min(initial * 2 ** (retries - 1), cap)

    def _cleanup_retention(self, now_ms: int) -> Dict[str, int]:
        sent_cut = now_ms - self._cfg["storage"]["sent_retention_days"] * 86400000
        audit_cut = now_ms - self._cfg["storage"]["audit_retention_days"] * 86400000
        outbox_deleted = self._conn.execute(
            "DELETE FROM outbox WHERE status=? AND locked_at_ms<?",
            (SENT, sent_cut)).rowcount
        event_deleted = self._conn.execute(
            "DELETE FROM event WHERE event_id NOT IN (SELECT event_id FROM outbox) "
            "AND sent_ms IS NOT NULL AND sent_ms<?", (audit_cut,)).rowcount
        return {"outbox": outbox_deleted, "event": event_deleted}


def _when_priority() -> str:
    """构造 CASE 分支串（WHEN 'X' THEN n ...）。"""
    return " ".join(f"WHEN '{kind}' THEN {rank}"
                    for kind, rank in _PRIORITY.items())


def _database_total(conn: sqlite3.Connection) -> int:
    """db + WAL + SHM 合计字节（容量水位口径，§8.2）。"""
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.is_file():
            total += candidate.stat().st_size
    return total


class WebhookSingleSender:
    """实时 worker 单次发送适配：aiohttp POST，不内部退避（重试由 Delivery 调度）。

    返回口径：2xx → True；4xx/5xx → False；超时/连接故障 → None（结果未知）。
    webhook URL 取环境变量 FEISHU_WEBHOOK_GRID（与小时出口同渠道，共享限流实测）。
    """

    def __init__(self, timeout_seconds: float = 5.0):
        self._timeout = timeout_seconds
        self._url = os.getenv("FEISHU_WEBHOOK_GRID")

    async def __call__(self, text: str) -> Optional[bool]:
        if not self._url:
            return None
        body = {"msg_type": "text", "content": {"text": text}}
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        session = aiohttp.ClientSession(timeout=timeout)
        try:
            async with session.post(self._url, json=body) as resp:
                await resp.read()
                if resp.status == 200:
                    return await _feishu_ok(resp)
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        finally:
            await session.close()


async def _feishu_ok(resp: aiohttp.ClientResponse) -> bool:
    """飞书 webhook 业务码（StatusCode!=0 视为失败可重试）。"""
    try:
        data = json.loads(await resp.text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return data.get("StatusCode", data.get("code", 0)) == 0
