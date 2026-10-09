"""delivery.py 测试：落库幂等、shadow/锁存语义、pump 三态重试、TTL、容量、sender。"""
import asyncio
import json
from decimal import Decimal

import pytest

from strategies.grid.realtime.delivery import (CANCELLED, FAILED, PENDING,
                                               RESEARCH, SENT, SHADOW,
                                               UNKNOWN, Delivery,
                                               WebhookSingleSender,
                                               _database_total)
from strategies.grid.realtime.reference_store import (connect, ensure_schema)
from strategies.grid.realtime.rules import parse_profile
from strategies.grid.realtime.state import AlertEvent


@pytest.fixture
def cfg(profile_raw):
    parsed = parse_profile(profile_raw)
    parsed["mode"] = "alert"  # 本文件测试非 shadow 投递语义
    return parsed


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "x.sqlite3"), 1000)
    ensure_schema(connection)
    return connection


def make_event(event_id="e-1", level="URGENT", direction="UP",
                episode="ep-1"):
    return AlertEvent(
        event_id=event_id, episode_id=episode, reference_id="ref-1",
        direction=direction, level=level, reason="test",
        decision_ms=1000, price=Decimal("2700"))


# ───────────────────── record ─────────────────────

def test_record_inserts_event_and_outbox(conn, cfg):
    delivery = Delivery(conn, cfg)
    assert delivery.record(make_event(), "payload", 1000) is True
    event = conn.execute("SELECT * FROM event WHERE event_id='e-1'").fetchone()
    assert event is not None and event["payload"] == "payload"
    row = conn.execute("SELECT * FROM outbox WHERE event_id='e-1'").fetchone()
    assert row["status"] == PENDING
    assert row["ttl_deadline_ms"] == 1000 + cfg["delivery"]["event_ttl_seconds"] * 1000


def test_record_idempotent(conn, cfg):
    delivery = Delivery(conn, cfg)
    assert delivery.record(make_event(), "p", 1000) is True
    assert delivery.record(make_event(), "p", 1000) is False
    assert conn.execute("SELECT COUNT(*) AS c FROM outbox").fetchone()["c"] == 1


def test_record_shadow_status(conn, cfg):
    shadow_cfg = {**cfg, "mode": "shadow"}
    delivery = Delivery(conn, shadow_cfg)
    delivery.record(make_event(), "p", 1000)
    status = conn.execute("SELECT status FROM outbox").fetchone()[0]
    assert status == SHADOW


def test_record_latch_no_ttl(conn, cfg):
    delivery = Delivery(conn, cfg)
    delivery.record(make_event(level="BOUNDARY_REACHED"), "p", 1000)
    row = conn.execute("SELECT * FROM outbox").fetchone()
    assert row["ttl_deadline_ms"] is None and row["kind"] == "BOUNDARY_REACHED"


def test_record_with_config_hash(conn, cfg):
    delivery = Delivery(conn, cfg)
    delivery.record(make_event(), "p", 1000, config_hash="h-1")
    assert conn.execute("SELECT config_hash FROM event").fetchone()[0] == "h-1"


# ───────────────────── research ─────────────────────

def test_research_disabled(conn, cfg):
    delivery = Delivery(conn, cfg)  # profile 默认 shadow_research.enabled=False
    assert delivery.record_research(make_event(), "p", 1000) is False
    assert conn.execute("SELECT COUNT(*) AS c FROM event").fetchone()["c"] == 0


def test_research_enabled_and_sent(conn, cfg):
    research_cfg = {**cfg,
                    "shadow_research": {"enabled": True,
                                        "notice_interval_seconds": 1800}}
    delivery = Delivery(conn, research_cfg)
    assert delivery.record_research(make_event(), "p", 1000) is True
    row = conn.execute("SELECT * FROM outbox").fetchone()
    assert row["kind"] == RESEARCH and row["status"] == PENDING


async def test_research_blocked_by_frequency(conn, cfg):
    research_cfg = {**cfg,
                    "shadow_research": {"enabled": True,
                                        "notice_interval_seconds": 1800}}
    delivery = Delivery(conn, research_cfg)
    assert delivery.record_research(
        make_event(event_id="e-1"), "p", 1000) is True
    assert await delivery.pump(_track_sender([True]), 1000) == 1  # e-1 已送达
    # 同 episode 第二发（30 分钟内）→ 拦截
    assert delivery.record_research(
        make_event(event_id="e-2"), "p", 2000) is False
    assert conn.execute("SELECT COUNT(*) AS c FROM event").fetchone()["c"] == 1


async def test_research_unsent_does_not_block(conn, cfg):
    """未送达（仍 PENDING）的研究消息不压制后续研究提醒（§9.3）。"""
    research_cfg = {**cfg,
                    "shadow_research": {"enabled": True,
                                        "notice_interval_seconds": 1800}}
    delivery = Delivery(conn, research_cfg)
    assert delivery.record_research(
        make_event(event_id="e-1"), "p", 1000) is True
    assert delivery.record_research(
        make_event(event_id="e-2"), "p", 2000) is True


async def test_research_not_blocked_after_interval(conn, cfg):
    research_cfg = {**cfg,
                    "shadow_research": {"enabled": True,
                                        "notice_interval_seconds": 1800}}
    delivery = Delivery(conn, research_cfg)
    delivery.record_research(make_event(event_id="e-1"), "p", 1000)
    await delivery.pump(_track_sender([True]), 1000)
    later = 1000 + 1801 * 1000
    assert delivery.record_research(
        make_event(event_id="e-2"), "p", later) is True


def test_research_duplicate_event_id(conn, cfg):
    research_cfg = {**cfg,
                    "shadow_research": {"enabled": True,
                                        "notice_interval_seconds": 1800}}
    delivery = Delivery(conn, research_cfg)
    delivery.record_research(make_event(event_id="e-1"), "p", 1000)
    assert delivery.record_research(make_event(event_id="e-1"), "p", 2000) \
        is False


# ───────────────────── pump ─────────────────────

def _track_sender(outcomes):
    sent_text = []

    async def sender(text):
        sent_text.append(text)
        return outcomes.pop(0)
    sender.texts = sent_text
    return sender


async def test_pump_sends_and_marks_sent(conn, cfg):
    delivery = Delivery(conn, cfg)
    delivery.record(make_event(), "payload", 0)
    sender = _track_sender([True])
    attempted = await delivery.pump(sender, 1000)
    assert attempted == 1 and sender.texts == ["payload"]
    row = conn.execute("SELECT * FROM outbox").fetchone()
    assert row["status"] == SENT
    assert conn.execute("SELECT sent_ms FROM event").fetchone()[0] == 1000


async def test_pump_failure_retries_then_succeeds(conn, cfg):
    delivery = Delivery(conn, cfg)
    delivery.record(make_event(), "p", 0)
    sender = _track_sender([False, True])
    assert await delivery.pump(sender, 1000) == 1
    row = conn.execute("SELECT * FROM outbox").fetchone()
    assert row["status"] == FAILED and row["retry_count"] == 1
    backoff_ms = int(cfg["delivery"]["retry_initial_seconds"] * 1000)
    assert row["next_retry_ms"] == 1000 + backoff_ms
    # 未到退避点 → 不拾取
    assert await delivery.pump(sender, 1000 + backoff_ms - 1) == 0
    # 到期 → 重发成功
    assert await delivery.pump(sender, 1000 + backoff_ms) == 1
    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == SENT


async def test_pump_unknown_outcome(conn, cfg):
    delivery = Delivery(conn, cfg)
    delivery.record(make_event(), "p", 0)
    sender = _track_sender([None])
    assert await delivery.pump(sender, 1000) == 1
    row = conn.execute("SELECT * FROM outbox").fetchone()
    assert row["status"] == UNKNOWN and row["retry_count"] == 1
    due = 1000 + int(cfg["delivery"]["retry_initial_seconds"] * 1000)
    # 未到点不拾取
    assert await delivery.pump(sender, due - 1) == 0
    # 到点重新拾取，渠道仍未知
    sender2 = _track_sender([None])
    assert await delivery.pump(sender2, due) == 1
    row = conn.execute("SELECT * FROM outbox").fetchone()
    assert row["status"] == UNKNOWN and row["retry_count"] == 2


async def test_pump_condition_false_cancels(conn, cfg):
    delivery = Delivery(conn, cfg)
    delivery.record(make_event(), "p", 0)
    sender = _track_sender([True])

    assert await delivery.pump(
        sender, 1000, condition_holds=lambda event_id: False) == 0
    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == CANCELLED
    assert sender.texts == []


async def test_pump_render_used(conn, cfg):
    delivery = Delivery(conn, cfg)
    delivery.record(make_event(), "orig", 0)

    def render(payload, row, now_ms):
        return f"rendered:{payload}"

    sender = _track_sender([True])
    await delivery.pump(sender, 1000, render=render)
    assert sender.texts == ["rendered:orig"]


async def test_pump_priority_order(conn, cfg):
    delivery = Delivery(conn, cfg)
    delivery.record(make_event(event_id="e-n", level="NOTICE"), "n", 0)
    delivery.record(make_event(event_id="e-u", level="URGENT"), "u", 0)
    delivery.record(make_event(event_id="e-b", level="BOUNDARY_REACHED"),
                    "b", 0)
    sender = _track_sender([True, True, True])
    await delivery.pump(sender, 1000)
    assert sender.texts == ["b", "u", "n"]


async def test_pump_no_due_rows(conn, cfg):
    sender = _track_sender([True])
    assert await Delivery(conn, cfg).pump(sender, 1000) == 0
    assert sender.texts == []


async def test_pump_latch_bypasses_condition(conn, cfg):
    delivery = Delivery(conn, cfg)
    delivery.record(make_event(level="BOUNDARY_REACHED"), "b", 0)
    sender = _track_sender([True])
    attempted = await delivery.pump(
        sender, 1000, condition_holds=lambda e: False)
    assert attempted == 1 and sender.texts == ["b"]


# ───────────────────── TTL 淘汰 ─────────────────────

def test_evict_expired_pending(conn, cfg):
    delivery = Delivery(conn, cfg)
    delivery.record(make_event(), "p", 0)
    ttl = cfg["delivery"]["event_ttl_seconds"] * 1000
    assert delivery.evict_expired(ttl) == 0
    assert delivery.evict_expired(ttl + 1) == 1
    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == CANCELLED


async def test_evict_expired_failed_and_unknown(conn, cfg):
    delivery = Delivery(conn, cfg)
    delivery.record(make_event(event_id="e-f"), "p", 0)
    delivery.record(make_event(event_id="e-u"), "p", 0)
    sender = _track_sender([False, None])
    await delivery.pump(sender, 1000)
    ttl = cfg["delivery"]["event_ttl_seconds"] * 1000
    assert delivery.evict_expired(1000 + ttl + 1) == 2
    statuses = {r["event_id"]: r["status"]
                for r in conn.execute("SELECT event_id, status FROM outbox")}
    assert statuses == {"e-f": CANCELLED, "e-u": CANCELLED}


def test_evict_latch_never(conn, cfg):
    delivery = Delivery(conn, cfg)
    delivery.record(make_event(level="BOUNDARY_REACHED"), "p", 0)
    assert delivery.evict_expired(10 ** 15) == 0
    assert conn.execute("SELECT status FROM outbox").fetchone()[0] == PENDING


# ───────────────────── 容量维护 ─────────────────────

def test_maintenance_basic(conn, cfg):
    delivery = Delivery(conn, cfg)
    result = delivery.maintenance(1000)
    assert result["deleted_rows"] == {"outbox": 0, "event": 0}
    assert result["total_bytes"] > 0
    assert result["max_bytes"] == cfg["storage"]["max_bytes"]
    assert result["storage_degraded"] is False


def test_maintenance_storage_degraded(conn, cfg):
    small_cfg = {**cfg, "storage": {**cfg["storage"], "max_bytes": 1}}
    result = Delivery(conn, small_cfg).maintenance(1000)
    assert result["storage_degraded"] is True


def test_maintenance_cleans_old_sent(conn, cfg):
    delivery = Delivery(conn, cfg)
    delivery.record(make_event(), "p", 0)
    sender = _track_sender([True])
    loop = asyncio.new_event_loop()
    loop.run_until_complete(delivery.pump(sender, 1000))
    loop.close()
    # outbox 保留 30 天、event 审计保留 90 天；取 91 天后维护两者才都到期
    result = delivery.maintenance(1000 + 91 * 86400000)
    assert result["deleted_rows"]["outbox"] == 1
    assert conn.execute("SELECT COUNT(*) AS c FROM outbox").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM event").fetchone()["c"] == 0


def test_database_total_counts_files(conn, cfg):
    # 强制 WAL 落盘：写一行并 checkpoint
    conn.execute("CREATE TABLE IF NOT EXISTS t(x)")
    conn.execute("INSERT INTO t VALUES(1)")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(FULL)")
    total = _database_total(conn)
    assert total > 0


def test_research_insert_fails_when_event_id_taken(conn, cfg):
    """event_id 已被普通记录占用 → research 插入 event 失败，返回 False。"""
    Delivery(conn, cfg).record(make_event(event_id="e-1"), "p", 1000)
    research_cfg = {**cfg,
                    "shadow_research": {"enabled": True,
                                        "notice_interval_seconds": 1800}}
    research_delivery = Delivery(conn, research_cfg)
    assert research_delivery.record_research(
        make_event(event_id="e-1"), "p", 2000) is False
    # 不产生第二条 outbox
    assert conn.execute("SELECT COUNT(*) AS c FROM outbox").fetchone()["c"] == 1


def test_database_total_without_sidecar_files(conn, cfg):
    """-wal/-shm 文件不存在时只计主库字节（候选文件非普通文件分支）。"""
    from pathlib import Path
    conn.execute("PRAGMA wal_checkpoint(FULL)")
    db_file = Path(conn.execute(
        "PRAGMA database_list").fetchone()["file"])
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_file) + suffix)
        if sidecar.exists():  # 测试库可安全删除，后续事务 SQLite 会重建
            sidecar.unlink()
    assert _database_total(conn) == db_file.stat().st_size


# ───────────────────── WebhookSingleSender ─────────────────────

class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def read(self):
        pass

    async def text(self):
        return json.dumps(self._body)

    async def json(self):
        return self._body


class FakePostSession:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.closed = False

    def post(self, url, json):  # noqa: A002
        if self._exc is not None:
            raise self._exc

        class Ctx:
            async def __aenter__(self_inner):
                return self._response

            async def __aexit__(self_inner, *args):
                return False

        return Ctx()

    async def close(self):
        self.closed = True


def _patch_session(monkeypatch, session):
    import aiohttp

    def factory(*args, **kwargs):
        return session

    monkeypatch.setattr(aiohttp, "ClientSession", factory)


async def test_sender_success(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_GRID", "https://x/hook")
    _patch_session(monkeypatch, FakePostSession(
        response=FakeResponse(200, {"StatusCode": 0})))
    result = await WebhookSingleSender()("text")
    assert result is True


async def test_sender_feishu_business_error(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_GRID", "https://x/hook")
    _patch_session(monkeypatch, FakePostSession(
        response=FakeResponse(200, {"StatusCode": 1})))
    result = await WebhookSingleSender()("text")
    assert result is False


async def test_sender_code_field_zero(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_GRID", "https://x/hook")
    _patch_session(monkeypatch, FakePostSession(
        response=FakeResponse(200, {"code": 0})))
    assert await WebhookSingleSender()("text") is True


async def test_sender_non_200(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_GRID", "https://x/hook")
    _patch_session(monkeypatch, FakePostSession(
        response=FakeResponse(500, {})))
    assert await WebhookSingleSender()("text") is False


async def test_sender_client_error_unknown(monkeypatch):
    import aiohttp
    monkeypatch.setenv("FEISHU_WEBHOOK_GRID", "https://x/hook")
    _patch_session(monkeypatch, FakePostSession(
        exc=aiohttp.ClientError("boom")))
    assert await WebhookSingleSender()("text") is None


async def test_sender_timeout_unknown(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_GRID", "https://x/hook")
    _patch_session(monkeypatch, FakePostSession(
        exc=asyncio.TimeoutError()))
    assert await WebhookSingleSender()("text") is None


async def test_sender_no_url_returns_none(monkeypatch):
    monkeypatch.delenv("FEISHU_WEBHOOK_GRID", raising=False)
    assert await WebhookSingleSender()("text") is None


async def test_sender_bad_json_body(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_GRID", "https://x/hook")

    class BadTextResponse(FakeResponse):
        async def text(self_inner):
            return "not-json"

    _patch_session(monkeypatch, FakePostSession(
        response=BadTextResponse(200, {})))
    assert await WebhookSingleSender()("text") is False
