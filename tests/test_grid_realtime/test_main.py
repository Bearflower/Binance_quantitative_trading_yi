"""main.py 测试：延迟分位、健康监控、在线编排全链路、历史穿越、文案、CLI。"""
import asyncio
from decimal import Decimal

import pytest

from strategies.grid.realtime import feed as feed_mod
from strategies.grid.realtime import main as main_mod
from strategies.grid.realtime.features import SecondSample
from strategies.grid.realtime.reference_store import (ExportStore,
                                                      SessionState, connect,
                                                      ensure_schema)
from strategies.grid.realtime.rules import parse_profile
from strategies.grid.realtime.state import AlertEvent
from strategies.grid.realtime.main import RealtimeService
from strategies.grid.realtime.reference_store import make_reference_id
from factories import make_trade


@pytest.fixture
def cfg(profile_raw):
    return parse_profile(profile_raw)


@pytest.fixture
def conn(tmp_path):
    connection = connect(str(tmp_path / "x.sqlite3"), 1000)
    ensure_schema(connection)
    return connection


def _publish_reference(conn, snap, base_ms):
    """在 base_ms 附近落一份 SENT 参考；返回 (sid, ref_id)。"""
    store = ExportStore(conn)
    sid = store.begin_session(base_ms - 100)
    state = SessionState(sid, 0, None, None)
    ref_id = store.prepare(base_ms - 50, state, snap)
    store.mark_sending(ref_id)
    store.mark_sent(
        ref_id, snap.effective_at_ms, SessionState(sid, 0, None, 1),
        base_ms - 10)
    return sid, ref_id


def _service(cfg, conn, now_ms, *, trade_feed=None, sender=None, sleep=None):
    kwargs = {"now_ms": now_ms}
    if trade_feed is not None:
        kwargs["trade_feed"] = trade_feed
    if sender is not None:
        kwargs["sender"] = sender
    if sleep is not None:
        kwargs["sleep"] = sleep
    return main_mod.RealtimeService(cfg, conn=conn, **kwargs)


async def _fill_queue(service, trades):
    service._trade_queue = asyncio.Queue()
    for trade in trades:
        service._trade_queue.put_nowait(trade)


# ───────────────────── LatencyTracker ─────────────────────

def test_latency_tracker_empty():
    tracker = main_mod.LatencyTracker()
    assert tracker.quantiles() == {} and tracker.p99() == 0.0


def test_latency_tracker_records_quantiles():
    tracker = main_mod.LatencyTracker()
    tracker.record(0.1)
    tracker.record(0.2)
    quantiles = tracker.quantiles()
    assert set(quantiles) == {"p50", "p90", "p99", "p999"}
    assert quantiles["p50"] == 0.1 and quantiles["p999"] == 0.2
    assert tracker.p99() == 0.2


# ───────────────────── HealthMonitor ─────────────────────

def test_health_monitor_lifecycle():
    monitor = main_mod.HealthMonitor()
    events = monitor.update({"feed_interrupted": 100}, 200, Decimal("1"))
    assert len(events) == 1
    assert events[0].event_id == "health-degraded-feed_interrupted"
    assert monitor.active_reasons == {"feed_interrupted": 100}
    # 同原因再来不重复
    assert monitor.update({"feed_interrupted": 100}, 300, Decimal("1")) == []
    # 原因消失发恢复
    recovered = monitor.update({}, 400, Decimal("1"))
    assert recovered[0].event_id == "health-recovered-feed_interrupted"
    assert monitor.active_reasons == {}


# ───────────────────── 在线编排 ─────────────────────

async def test_tick_valid_boundary_cross_shadow_only(conn, cfg, snap):
    base = snap.effective_at_ms
    _publish_reference(conn, snap, base)
    trades = [make_trade(1, base + 500, "2700"),
              make_trade(2, base + 500, "2800")]
    service = _service(cfg, conn, lambda: base + 1000)
    await _fill_queue(service, trades)
    snapshot = await service.tick()
    assert snapshot["reference_id"] is not None
    assert snapshot["events_total"] >= 1
    assert snapshot["feed_healthy"] is True
    # shadow：只记录不外发，outbox 行全部 SHADOW
    rows = conn.execute("SELECT DISTINCT status FROM outbox").fetchall()
    assert [r["status"] for r in rows] == ["SHADOW"]
    assert snapshot["latency"] != {}


async def test_sync_uncertain_no_session_records_health(conn, cfg):
    service = _service(cfg, conn, lambda: 5_000_000)
    await _fill_queue(service, [])
    await service.tick()
    rows = conn.execute(
        "SELECT o.kind, o.status FROM outbox o").fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "HEALTH" and rows[0]["status"] == "SHADOW"
    health = service._health.active_reasons
    assert "sync_uncertain" in health


async def test_sync_uncertain_with_historical_cross(conn, cfg, snap,
                                                    monkeypatch):
    base = snap.effective_at_ms
    _publish_reference(conn, snap, base)
    store = ExportStore(conn)
    store.set_sync_uncertain(SessionState(1, 1, snap.reference_id, None),
                             base + 200)
    fixed_now = base + 1000
    monkeypatch.setattr(feed_mod, "now_ms", lambda: fixed_now)
    trades = [make_trade(9, fixed_now - 100, "2800")]
    service = _service(cfg, conn, lambda: base + 1000)
    await _fill_queue(service, trades)
    await service.tick()
    expected_ref = make_reference_id(
        snap.calculated_at_ms, snap.grid_lower, snap.grid_upper,
        snap.stop_lower, snap.stop_upper)
    event = conn.execute(
        "SELECT * FROM event WHERE level='HISTORICAL_REFERENCE_CROSSED'"
        ).fetchone()
    assert event is not None
    assert event["event_id"] == f"hist-{expected_ref}-UP"


async def test_sync_uncertain_price_stale_no_cross(conn, cfg, snap,
                                                   monkeypatch):
    base = snap.effective_at_ms
    _publish_reference(conn, snap, base)
    ExportStore(conn).set_sync_uncertain(
        SessionState(1, 1, snap.reference_id, None), base + 200)
    fixed_now = base + 10_000
    monkeypatch.setattr(feed_mod, "now_ms", lambda: fixed_now)
    # 成交过旧（超出新鲜窗口）→ _fresh_price None → 不产生穿越事件
    trades = [make_trade(9, base, "2800")]
    service = _service(cfg, conn, lambda: fixed_now)
    await _fill_queue(service, trades)
    await service.tick()
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM event WHERE level="
        "'HISTORICAL_REFERENCE_CROSSED'").fetchone()["c"] == 0


async def test_historical_direction_none_no_event(conn, cfg, snap,
                                                  monkeypatch):
    base = snap.effective_at_ms
    _publish_reference(conn, snap, base)
    ExportStore(conn).set_sync_uncertain(
        SessionState(1, 1, snap.reference_id, None), base + 200)
    fixed_now = base + 1000
    monkeypatch.setattr(feed_mod, "now_ms", lambda: fixed_now)
    trades = [make_trade(9, fixed_now - 100, "2700")]  # 区间内不穿越
    service = _service(cfg, conn, lambda: fixed_now)
    await _fill_queue(service, trades)
    await service.tick()
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM event WHERE level="
        "'HISTORICAL_REFERENCE_CROSSED'").fetchone()["c"] == 0


async def test_process_no_trades_uses_zero_price(conn, cfg):
    service = _service(cfg, conn, lambda: 5_000_000)
    await _fill_queue(service, [])
    await service.process(5_000_000, [])
    # 无会话 → SYNC_UNCERTAIN 健康事件照常记录
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM event").fetchone()["c"] == 1


async def test_feed_interrupted_health_reason(conn, cfg):
    service = _service(cfg, conn, lambda: 5_000_000)
    await _fill_queue(service, [])
    await service._on_feed_health(False, "断线")
    await service.tick()
    assert "feed_interrupted" in service._health.active_reasons


class _ListFeed:
    def __init__(self, trades):
        self._trades = trades

    async def stream(self):
        for trade in self._trades:
            yield trade


async def test_pump_feed_queue_full_sets_storage_degraded(conn, cfg):
    small_cfg = {**cfg, "delivery": {**cfg["delivery"], "queue_capacity": 1}}
    service = _service(small_cfg, conn, lambda: 1)
    service._feed = _ListFeed(
        [make_trade(1, 1000, "1"), make_trade(2, 1000, "1")])
    service._trade_queue = asyncio.Queue(maxsize=1)
    await service._pump_feed()
    assert service._storage_degraded is True
    queued = service._trade_queue.get_nowait()
    assert queued.agg_trade_id == 1


# ───────────────────── run 主循环 ─────────────────────

class _SlowFeed:
    """stream 被取消时记录，用于验证 run 的 finally 取消 feed 任务。"""

    cancelled = False

    async def stream(self):
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            _SlowFeed.cancelled = True
            raise
        yield make_trade(1, 1, "1")  # pragma: no cover


async def test_run_cancels_feed_task_on_error(conn, cfg):
    async def stop_sleep(seconds):
        await asyncio.sleep(0)  # 让出控制，让 pump 任务实际进入 stream
        raise RuntimeError("终止循环")

    service = _service(cfg, conn, feed_mod.now_ms, trade_feed=_SlowFeed(),
                       sleep=stop_sleep)
    with pytest.raises(RuntimeError, match="终止循环"):
        await service.run()
    await asyncio.sleep(0)  # 让 CancelledError 在 stream 内落地
    assert _SlowFeed.cancelled


# ───────────────────── 条件与锁存文案 ─────────────────────

async def test_condition_holds_system(conn, cfg):
    service = _service(cfg, conn, lambda: 1)
    service._event_refs["e-sys"] = ("SYSTEM", "ep", "HEALTH")
    assert service._condition_holds("e-sys") is True


async def test_condition_holds_tracked_runtime(conn, cfg, snap):
    base = snap.effective_at_ms
    _publish_reference(conn, snap, base)
    trades = [make_trade(1, base + 500, "2700"),
              make_trade(2, base + 500, "2800")]
    service = _service(cfg, conn, lambda: base + 1000)
    await _fill_queue(service, trades)
    await service.tick()
    # 引擎已记录的事件其条件应仍成立（runtime 同 episode 同级）
    event_id, values = next(iter(service._event_refs.items()))
    assert values[0] != "SYSTEM"
    assert service._condition_holds(event_id) is True


def test_render_latch_titles(conn, cfg, snap):
    service = _service(cfg, conn, lambda: 1)
    service._engine.snapshot = snap
    service._event_refs["e-up"] = ("UP", "ep", "BOUNDARY_REACHED")
    row = {"kind": "BOUNDARY_REACHED", "locked_at_ms": 1000,
           "event_id": "e-up"}
    # 2700 在区间内 → 已反弹，附当前价；≤30s 无补报标题
    service._samples.append(SecondSample(1, Decimal("2700"), 1, 1))
    text = service._render_latch("原文", row, 31000)
    assert text == "原文\n曾于某时触及，当前价格已回到：2700"
    # >30s（毫秒口径）补报标题
    text = service._render_latch("原文", row, 31001)
    assert text.startswith("【历史触及事件补报】")
    # 仍越界（2800 ≥ SU=2792.03）→ 不附反弹
    service._samples[-1] = SecondSample(1, Decimal("2800"), 1, 1)
    text = service._render_latch("原文", row, 32000)
    assert text == "【历史触及事件补报】原文"


def test_render_latch_without_context(conn, cfg):
    """无事件登记/无快照时保守不附反弹文案。"""
    service = _service(cfg, conn, lambda: 1)
    service._samples.append(SecondSample(1, Decimal("2700"), 1, 1))
    assert service._render_latch(
        "原文", {"kind": "BOUNDARY_REACHED", "locked_at_ms": 0,
                 "event_id": "unknown"}, 1) == "原文"


def test_render_latch_research_keeps_prefix(conn, cfg):
    """研究参考行即使超过补报窗口也不前置补报标题（§9.3 每条以【研究参考】开头）。"""
    service = _service(cfg, conn, lambda: 1)
    report_ms = cfg["delivery"]["report_delay_seconds"] * 1000
    row = {"kind": "RESEARCH", "locked_at_ms": 0, "event_id": "e-res"}
    assert service._render_latch(
        "【研究参考】内容", row, report_ms + 1) == "【研究参考】内容"


def test_rebound_line_historical_not_appended(conn, cfg, snap):
    """历史参考线事件不据当前参考判定反弹（§8.2）。"""
    service = _service(cfg, conn, lambda: 1)
    service._engine.snapshot = snap
    service._event_refs["e-hist"] = ("UP", "hist-ep",
                                     "HISTORICAL_REFERENCE_CROSSED")
    service._samples.append(SecondSample(1, Decimal("2700"), 1, 1))
    assert service._rebound_line({"event_id": "e-hist"}) == ""


def test_rebound_line_without_samples(conn, cfg, snap):
    """无采样价可判定时保守不附反弹文案。"""
    service = _service(cfg, conn, lambda: 1)
    service._engine.snapshot = snap
    service._event_refs["e-up"] = ("UP", "ep", "BOUNDARY_REACHED")
    assert not service._samples
    assert service._rebound_line({"event_id": "e-up"}) == ""


# ───────────────────── 文案与辅助 ─────────────────────

def test_build_payload_health():
    event = AlertEvent(
        event_id="h", episode_id="h", reference_id="",
        direction="SYSTEM", level="HEALTH", reason="degraded:x",
        decision_ms=1_000_000_000_000, price=Decimal("0"))
    text = main_mod.build_payload(event, None, {})
    assert text.startswith("【系统健康】degraded:x")


def test_build_payload_normal(snap):
    event = AlertEvent(
        event_id="e", episode_id="ep", reference_id=snap.reference_id,
        direction="UP", level="URGENT", reason="原因X",
        decision_ms=1_000_000_000_000, price=Decimal("2800"))
    text = main_mod.build_payload(event, snap, {})
    assert "风险升级" in text and "上行" in text
    assert "原因X" in text and snap.reference_id in text
    assert "2800" in text


def test_build_payload_down(snap):
    event = AlertEvent(
        event_id="e", episode_id="ep", reference_id=snap.reference_id,
        direction="DOWN", level="NOTICE", reason="r",
        decision_ms=1_000_000_000_000, price=Decimal("2700"))
    assert "下行" in main_mod.build_payload(event, snap, {})


def test_build_research_payload(snap):
    event = AlertEvent(
        event_id="e", episode_id="ep", reference_id=snap.reference_id,
        direction="UP", level="URGENT", reason="inside_urgent",
        decision_ms=1_000_000_000_000, price=Decimal("2800"))
    text = main_mod.build_research_payload(event, snap)
    assert text.startswith("【研究参考】")
    assert "不构成操作建议" in text and "2800" in text
    # §9.3：不套用 §8.3 操作建议模板，无指令语义
    assert "【实时预警】" not in text and "建议终止价" not in text
    assert "参考线：" in text


def test_build_research_payload_boundary_level_neutral(snap):
    """越界事实研究文案不得泄漏“建议终止”指令语义（§9.3）。"""
    event = AlertEvent(
        event_id="e", episode_id="ep", reference_id=snap.reference_id,
        direction="UP", level="BOUNDARY_REACHED", reason="crossed",
        decision_ms=1_000_000_000_000, price=Decimal("2800"))
    text = main_mod.build_research_payload(event, snap)
    assert text.startswith("【研究参考】")
    assert "越界事实" in text
    assert "建议终止" not in text and "【实时预警】" not in text


def test_build_research_payload_historical_labels(snap):
    event = main_mod._historical_event(snap, "UP", Decimal("2800"), 999)
    text = main_mod.build_research_payload(event, snap)
    assert "历史参考时间：" in text and "历史参考线：" in text


def test_build_research_payload_without_snapshot():
    event = AlertEvent(
        event_id="e", episode_id="ep", reference_id="r",
        direction="DOWN", level="NOTICE", reason="r",
        decision_ms=1_000_000_000_000, price=Decimal("2700"))
    assert main_mod.build_research_payload(event, None).startswith("【研究参考】")


def test_historical_event_fields(snap):
    event = main_mod._historical_event(snap, "UP", Decimal("2800"), 999)
    assert event.level == "HISTORICAL_REFERENCE_CROSSED"
    assert event.event_id == f"hist-{snap.reference_id}-UP"
    assert event.episode_id == f"hist-{snap.reference_id}"


def test_level_text():
    assert main_mod._level_text("NOTICE") == "异动关注"
    assert main_mod._level_text("OTHER") == "OTHER"


def test_beijing_format():
    text = main_mod._beijing(0)
    assert text == "1970-01-01 08:00:00"


def test_fresh_price():
    cfg_features = {"features": {"max_anchor_gap_seconds": 2}}
    fresh_trades = [make_trade(1, 1000, "100")]
    # 直接构造 now 注入场景：monkeypatch now_ms
    original = feed_mod.now_ms
    feed_mod.now_ms = lambda: 2000
    try:
        assert main_mod._fresh_price(fresh_trades, cfg_features) == Decimal("100")
        feed_mod.now_ms = lambda: 4001
        assert main_mod._fresh_price(fresh_trades, cfg_features) is None
        assert main_mod._fresh_price([], cfg_features) is None
    finally:
        feed_mod.now_ms = original


def test_gap_ok():
    sample = SecondSample(1000, Decimal("1"), 1000, 1)
    cfg_features = {"features": {"max_anchor_gap_seconds": 2}}
    assert main_mod._gap_ok(sample, 3000, cfg_features) is True
    assert main_mod._gap_ok(sample, 3001, cfg_features) is False
    no_anchor = SecondSample(1000, None, None, None)
    assert main_mod._gap_ok(no_anchor, 2000, cfg_features) is False


# ───────────────────── 同步/评估/采样/维护 分支补齐 ─────────────────────

async def test_sync_engine_same_reference_returns_empty(conn, cfg, snap):
    """同一参考再次同步直接返回 []（current 非 None 且 reference_id 相同）。"""
    base = snap.effective_at_ms
    _publish_reference(conn, snap, base)
    service = _service(cfg, conn, lambda: base + 1000)
    await _fill_queue(service, [])
    await service.tick()
    assert service._sync_engine(snap, base + 2000, None) == []


async def test_evaluate_missing_returns_empty(conn, cfg):
    """有 ACTIVE 会话但无 SENT 参考 → MISSING → 不产生建议。"""
    now = 5_000_000
    ExportStore(conn).begin_session(now)  # updated_at=now，心跳新鲜
    service = _service(cfg, conn, lambda: now)
    assert service._evaluate(now, [], None, "MISSING") == []


def test_emit_facts_lazy_inits_scanner(conn, cfg, snap):
    """scanner 未初始化时懒加载（防御分支直接单测）。"""
    service = _service(cfg, conn, lambda: 1)
    assert service._scanner is None
    assert service._emit_facts([], snap) == []
    assert service._scanner is not None


async def test_build_sample_carries_price_within_gap(conn, cfg):
    """无成交但锚点在 gap 内 → 沿用上一采样价与锚点。"""
    base = 1_000_000_000_000
    service = _service(cfg, conn, lambda: base)
    await service.process(base, [make_trade(7, base, "2700")])
    await service.process(base + 1000, [])  # 本拍无成交
    latest = service._samples[-1]
    assert latest.price == Decimal("2700")
    assert latest.anchor_trade_id == 7 and latest.anchor_time_ms == base


async def test_shadow_research_uses_research_channel(conn, cfg, snap):
    """shadow + research 开启 → 穿越事件走 RESEARCH 通道（发送成功即 SENT）。"""
    research_cfg = {**cfg,
                    "shadow_research": {"enabled": True,
                                        "notice_interval_seconds": 1800}}
    base = snap.effective_at_ms
    _publish_reference(conn, snap, base)

    async def fake_sender(text):
        return True

    service = _service(research_cfg, conn, lambda: base + 1000,
                       sender=fake_sender)
    trades = [make_trade(1, base + 500, "2700"),
              make_trade(2, base + 500, "2800")]
    await _fill_queue(service, trades)
    await service.tick()
    assert "RESEARCH" in {r["kind"] for r in conn.execute(
        "SELECT kind FROM outbox")}


def test_reasons_reference_states(conn, cfg):
    """MISSING/INVALID → reference_missing/reference_invalid。"""
    service = _service(cfg, conn, lambda: 1)
    assert "reference_missing" in service._reasons("MISSING")
    assert "reference_invalid" in service._reasons("INVALID")


def test_reasons_storage_degraded(conn, cfg):
    service = _service(cfg, conn, lambda: 1)
    service._storage_degraded = True
    assert service._reasons("VALID")["storage_degraded"] == 1


def test_reasons_processing_lag(conn, cfg):
    """p99 超 max_event_lag_seconds → processing_lag。"""
    service = _service(cfg, conn, lambda: 1)
    service._latency.record(cfg["health"]["max_event_lag_seconds"] + 1)
    assert "processing_lag" in service._reasons("VALID")


def test_run_maintenance_every_60_ticks(conn, cfg):
    """第 60 拍执行维护；维护返回的水位可翻 storage_degraded。"""
    small_cfg = {**cfg, "storage": {**cfg["storage"], "max_bytes": 1}}
    service = _service(small_cfg, conn, lambda: 1000)
    service._tick_count = 59
    service._run_maintenance(1000)  # 59 % 60 != 0 → 跳过
    assert service._storage_degraded is False
    service._tick_count = 60
    service._run_maintenance(1000)
    assert service._storage_degraded is True


async def test_start_feed_creates_default(conn, cfg):
    """未注入 feed 时创建默认 TradeFeed 并返回泵任务；立即取消无真实连接。"""
    service = _service(cfg, conn, lambda: 1)
    task = await service._start_feed()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task  # 任务体零执行即被取消，不发起真实 WS
    assert isinstance(service._feed, feed_mod.TradeFeed)


async def test_drain_trades_without_queue(conn, cfg):
    """队列未建立 → 空列表。"""
    service = _service(cfg, conn, lambda: 1)
    assert service._drain_trades() == []


def test_load_cfg_reads_real_config():
    """_load_cfg 读真实 config.yaml 的 realtime_alert 节。"""
    parsed = main_mod._load_cfg()
    assert parsed["mode"] == "shadow" and parsed["symbol"] == "ETHUSDT"
    assert parsed["normal"]["min_efficiency"] == Decimal("0.30")
    # 补报阈值/维护周期为配置项，禁硬编码（§8.2、编码规范）
    assert parsed["delivery"]["report_delay_seconds"] == 30
    assert parsed["delivery"]["maintenance_interval_seconds"] == 60


# ───────────────────── CLI ─────────────────────

def _patch_cli(monkeypatch, cfg, conn):
    monkeypatch.setattr(main_mod, "_load_cfg", lambda: cfg)
    monkeypatch.setattr(main_mod, "connect", lambda *a: conn)
    captured = {}

    async def fake_run(self):
        captured["sender"] = self._sender

    monkeypatch.setattr(RealtimeService, "run", fake_run)
    return captured


def test_main_shadow_sender_none(monkeypatch, cfg, conn):
    captured = _patch_cli(monkeypatch, cfg, conn)
    assert main_mod.main(["--db-path", "/x.sqlite3"]) == 0
    assert captured["sender"] is None


def test_main_alert_sender_built(monkeypatch, cfg, conn):
    alert_cfg = {**cfg, "mode": "alert"}
    captured = _patch_cli(monkeypatch, alert_cfg, conn)
    marker = object()
    monkeypatch.setattr(main_mod, "WebhookSingleSender", lambda: marker)
    assert main_mod.main([]) == 0
    assert captured["sender"] is marker


def test_main_research_sender_built(monkeypatch, cfg, conn):
    research_cfg = {**cfg, "shadow_research": {"enabled": True,
                                               "notice_interval_seconds": 1}}
    captured = _patch_cli(monkeypatch, research_cfg, conn)
    marker = object()
    monkeypatch.setattr(main_mod, "WebhookSingleSender", lambda: marker)
    main_mod.main([])
    assert captured["sender"] is marker
