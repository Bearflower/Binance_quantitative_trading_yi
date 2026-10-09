"""state.py 单元测试：持续计时、级别转移、去重、恢复、快照接收（AC-02/04~09/18/23/30）。"""
import dataclasses
from decimal import Decimal

from strategies.grid.realtime.features import FeatureSlice
from strategies.grid.realtime.rules import (DOWN, UP, Level, ReferenceSnapshot,
                                           parse_profile)
from strategies.grid.realtime.state import (CRITICAL, NORMAL,
                                           RealtimeEngine)

S = 1_000_000_000_000  # 与 snap.effective_at_ms 对齐


def feat(price, r60=None, r180=None, r300=None, e300="0.8") -> FeatureSlice:
    """构造 DOWN 场景特征：r 传带符号字符串（下跌为负）。"""
    def dec(v):
        return Decimal(v) if v is not None else None
    return FeatureSlice(
        Decimal(price),
        {60: dec(r60), 180: dec(r180), 300: dec(r300)},
        {300: dec(e300)})


def neutral(price="2700.00") -> FeatureSlice:
    return feat(price)


def normal_only() -> FeatureSlice:
    # 2655：g=0.218（>0.2 仅普通位置），变化与 E 满足普通、不满足紧急
    return feat("2655.00", r180="-0.01", r300="-0.01")


def both_pred() -> FeatureSlice:
    # 2640：g=0.114，普通与区间内紧急同时成立
    return feat("2640.00", r180="-0.01", r300="-0.01")


def make_engine(profile_raw, snap=None, *, notice_s=None,
                normal_hold=None) -> RealtimeEngine:
    cfg = parse_profile(profile_raw)
    if notice_s is not None:
        cfg["repeat"]["notice_seconds"] = notice_s
    if normal_hold is not None:
        cfg["normal"]["hold_seconds"] = normal_hold
    return RealtimeEngine(cfg, snap)


def snap2() -> ReferenceSnapshot:
    return ReferenceSnapshot(
        reference_id="grid-ev-test-2", symbol="ETHUSDT",
        calculated_at_ms=S, effective_at_ms=S,
        grid_lower=Decimal("2660"), grid_upper=Decimal("2800"),
        stop_lower=Decimal("2600"), stop_upper=Decimal("2860"))


# ───────────────────── 参考状态与 AC-02 ─────────────────────

def test_reference_status_missing(snap, profile_raw):
    eng = make_engine(profile_raw)
    assert eng.reference_status(S) == "MISSING"


def test_reference_status_valid_and_stale(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    assert eng.reference_status(S) == "VALID"
    assert eng.reference_status(S + 21_601_000) == "STALE"


def test_reference_status_invalid(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    eng.snapshot = None
    eng.invalid = True
    assert eng.reference_status(S) == "INVALID"


def test_send_result_non_sent_keeps_reference(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    assert eng.apply_send_result("FAILED", snap, S) == []
    assert eng.snapshot is snap


def test_send_result_invalid_candidate_invalidates(snap, profile_raw):
    bad = ReferenceSnapshot(
        reference_id="bad", symbol="ETHUSDT", calculated_at_ms=S,
        effective_at_ms=S, grid_lower=Decimal("2590"),
        grid_upper=Decimal("2767.96"), stop_lower=Decimal("2599.45"),
        stop_upper=Decimal("2792.03"))
    eng = make_engine(profile_raw, snap)
    assert eng.apply_send_result("SENT", bad, S) == []
    assert eng.snapshot is None and eng.invalid
    assert eng.reference_status(S) == "INVALID"


def test_send_result_same_id_skips(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    assert eng.apply_send_result("SENT", snap, S) == []
    assert len(eng.events) == 0


def test_send_result_switch_clears_and_emits_boundary(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    eng.runtimes[DOWN].holds[NORMAL] = S
    # 2590 在新参考 SL=2600 之下 → 切换即锁存终止价
    events = eng.apply_send_result("SENT", snap2(), S, Decimal("2590"))
    assert len(events) == 1
    e = events[0]
    assert e.level == "BOUNDARY_REACHED" and e.reference_switch
    assert eng.runtimes[DOWN].holds == {}
    assert eng.snapshot.reference_id == "grid-ev-test-2"


def test_send_result_switch_without_current_price(snap, profile_raw):
    # 不传当前价：不做新参考下事实评估（覆盖 111->113 分支）
    events = make_engine(profile_raw, snap).apply_send_result(
        "SENT", snap2(), S)
    assert events == []


def test_send_result_switch_inside_price_silent(snap, profile_raw):
    # 当前价在新参考严格区间：无事实事件（覆盖 _facts_under 末行 return []）
    events = make_engine(profile_raw, snap).apply_send_result(
        "SENT", snap2(), S, Decimal("2700"))
    assert events == []


def test_send_result_switch_buffer_critical(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    # 2610 在新参考缓冲下：u=50/60=0.833 ≥ 0.75 → 临界事件
    events = eng.apply_send_result("SENT", snap2(), S, Decimal("2610"))
    assert len(events) == 1
    assert events[0].reason == CRITICAL and events[0].reference_switch


def test_send_result_switch_buffer_non_critical(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    # 2630 在新参考缓冲：u=30/60=0.5 < .75，仅记入 entry，无事件（覆盖 151->153）
    events = eng.apply_send_result("SENT", snap2(), S, Decimal("2630"))
    assert events == []
    assert eng.runtimes[DOWN].entries == [S]


# ───────────────────── 持续计时与级别 ─────────────────────

def test_normal_sustained_hold_then_interrupt(snap, profile_raw):
    # notice 间隔缩短到 10s，专门验证中断后 hold 重新计时（不受 1800s 重复间隔干扰）
    eng = make_engine(profile_raw, snap, notice_s=10)
    for k in range(10):
        assert eng.on_second(S + k * 1000, normal_only()) == []
    events = eng.on_second(S + 10_000, normal_only())
    assert len(events) == 1 and events[0].level == "NOTICE"
    assert events[0].first_held_ms == S
    # 中断一秒：计时 pop，重新满足须再等 10s
    assert eng.on_second(S + 11_000, neutral()) == []
    for k in range(12, 22):
        assert eng.on_second(S + k * 1000, normal_only()) == []
    events = eng.on_second(S + 22_000, normal_only())
    assert len(events) == 1
    assert events[0].first_held_ms == S + 12_000


def test_urgent_sustained_fires_before_notice(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    for k in range(3):
        assert eng.on_second(S + k * 1000, both_pred()) == []
    events = eng.on_second(S + 3_000, both_pred())
    assert len(events) == 1 and events[0].level == "URGENT"
    assert events[0].reason == "inside_urgent"


def test_upgrade_bypasses_notice_cooling(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    for k in range(10):
        eng.on_second(S + k * 1000, normal_only())
    events = eng.on_second(S + 10_000, normal_only())
    assert events[0].level == "NOTICE"
    # 紧急条件出现：3s 后 URGENT，不受 1800s 普通冷却阻挡
    for k in range(11, 14):
        assert eng.on_second(S + k * 1000, both_pred()) == []
    events = eng.on_second(S + 14_000, both_pred())
    assert len(events) == 1 and events[0].level == "URGENT"


def test_urgent_repeat_interval(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    for k in range(304):
        events = eng.on_second(S + k * 1000, both_pred())
        if k == 3:
            assert len(events) == 1 and events[0].level == "URGENT"
        elif k == 303:
            assert len(events) == 1 and events[0].level == "URGENT"
        else:
            assert events == []


def test_notice_downgrade_second_silent(snap, profile_raw):
    # normal/urgent hold 均 3s：S+3 紧急优先发 URGENT
    eng = make_engine(profile_raw, snap, normal_hold=3)
    for k in range(3):
        assert eng.on_second(S + k * 1000, both_pred()) == []
    assert eng.on_second(S + 3_000, both_pred())[0].level == "URGENT"
    # 紧急解除、普通当秒满 hold：由 URGENT 降级，不发 NOTICE
    events = eng.on_second(S + 4_000, normal_only())
    assert events == [] and eng.runtimes[DOWN].current_level == Level.NOTICE
    # 下一秒普通仍满足：正常发 NOTICE
    events = eng.on_second(S + 5_000, normal_only())
    assert len(events) == 1 and events[0].level == "NOTICE"


def test_boundary_level_downgrades_after_rebound(snap, profile_raw):
    """价格回到区间内后当前级别下降，但边界事实锁存不被抹去（§7.1）。"""
    eng = make_engine(profile_raw, snap)
    eng.on_fact(S, DOWN, "boundary", Decimal("2590"))
    rt = eng.runtimes[DOWN]
    assert rt.current_level == Level.BOUNDARY_REACHED and rt.boundary_fact
    eng.on_second(S + 1_000, neutral("2700.00"))
    assert rt.current_level == Level.IDLE and rt.boundary_fact


def test_degraded_pauses_window_rules(snap, profile_raw):
    """DEGRADED 暂停普通/紧急规则并清持续计时（§8.1）。"""
    eng = make_engine(profile_raw, snap, normal_hold=3)
    for k in range(4):
        eng.on_second(S + k * 1000, normal_only())
    assert eng.runtimes[DOWN].holds != {}
    assert eng.on_second(S + 5_000, normal_only(), degraded=True) == []
    assert eng.runtimes[DOWN].holds == {}


def test_degraded_keeps_boundary_fact(snap, profile_raw):
    """DEGRADED 下终止价事实仍由 on_fact 锁存（§8.1）。"""
    eng = make_engine(profile_raw, snap)
    assert eng.on_second(S, neutral("2590.00"), degraded=True) == []
    events = eng.on_fact(S + 1_000, DOWN, "boundary", Decimal("2590"))
    assert len(events) == 1 and events[0].level == "BOUNDARY_REACHED"


# ───────────────────── 逐笔事实 ─────────────────────

def test_critical_fact_once_per_episode(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    events = eng.on_fact(S, DOWN, CRITICAL, Decimal("2605"))
    assert len(events) == 1 and events[0].level == "URGENT"
    assert events[0].reason == CRITICAL
    assert eng.on_fact(S + 1, DOWN, CRITICAL, Decimal("2605")) == []


def test_sustained_after_critical_appends(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    eng.on_fact(S, DOWN, CRITICAL, Decimal("2605"))
    for k in range(1, 4):
        assert eng.on_second(S + k * 1000, both_pred()) == []
    events = eng.on_second(S + 4_000, both_pred())
    assert len(events) == 1
    assert events[0].level == "URGENT" and events[0].reason == "inside_urgent"
    # 追加仅一次，其后按 300s 重复
    assert eng.on_second(S + 5_000, both_pred()) == []


def test_boundary_fact_gap_and_repeat(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    events = eng.on_fact(S, DOWN, "boundary", Decimal("2590"),
                         gap_crossing=True)
    assert len(events) == 1
    assert events[0].level == "BOUNDARY_REACHED" and events[0].gap_crossing
    # 间隔不足：不重复
    assert eng.on_fact(S + 1_000, DOWN, "boundary", Decimal("2590")) == []
    # 间隔到：事实重复
    events = eng.on_fact(S + 300_000, DOWN, "boundary", Decimal("2590"))
    assert len(events) == 1 and events[0].level == "BOUNDARY_REACHED"


def test_boundary_repeat_at_second(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    eng.on_fact(S, DOWN, "boundary", Decimal("2590"))
    assert eng.on_second(S + 299_000, feat("2590")) == []
    events = eng.on_second(S + 300_000, feat("2590"))
    assert len(events) == 1 and events[0].level == "BOUNDARY_REACHED"


def test_boundary_stale_allowed_critical_not(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    t = S + 21_601_000
    events = eng.on_fact(t, DOWN, "boundary", Decimal("2590"))
    assert len(events) == 1 and events[0].stale
    assert eng.on_fact(t, DOWN, CRITICAL, Decimal("2605")) == []


def test_buffer_entry_dedup(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    assert eng.on_fact(S, DOWN, "buffer_entry", Decimal("2620")) == []
    assert eng.on_fact(S, DOWN, "buffer_entry", Decimal("2620")) == []
    assert eng.runtimes[DOWN].entries == [S]
    eng.on_fact(S + 1_000, DOWN, "buffer_entry", Decimal("2620"))
    assert eng.runtimes[DOWN].entries == [S, S + 1_000]


def test_unknown_fact_ignored(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    assert eng.on_fact(S, DOWN, "other", Decimal("2650")) == []


def test_up_direction_symmetry(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    events = eng.on_fact(S, UP, "boundary", Decimal("2800"))
    assert len(events) == 1 and events[0].direction == UP


def test_up_boundary_repeat_at_second(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    eng.on_fact(S, UP, "boundary", Decimal("2793"))
    events = eng.on_second(S + 300_000, feat("2793"))
    assert len(events) == 1
    assert events[0].direction == UP and events[0].level == "BOUNDARY_REACHED"


# ───────────────────── 恢复、状态、确定性 ─────────────────────

def test_recovery_closes_episode(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    eng.on_fact(S, DOWN, CRITICAL, Decimal("2605"))
    rt = eng.runtimes[DOWN]
    assert rt.episode_id is not None
    # 2700：g=0.529 ≥ 0.45，三分支均不成立；恢复计时从 S+1 起，满 60s 在 S+61
    for k in range(1, 61):
        eng.on_second(S + k * 1000, neutral())
    eng.on_second(S + 61_000, neutral())
    assert rt.episode_id is None and rt.holds == {}
    events = eng.on_fact(S + 62_000, DOWN, CRITICAL, Decimal("2605"))
    assert events[0].episode_id.endswith("#2")


def test_stale_clears_holds(snap, profile_raw):
    eng = make_engine(profile_raw, snap)
    rt = eng.runtimes[DOWN]
    rt.holds[NORMAL] = S
    eng.on_second(S + 21_601_000, neutral())
    assert rt.holds == {} and rt.recovery_t0 is None


def test_determinism(snap, profile_raw):
    def drive():
        eng = make_engine(profile_raw, snap)
        for k in range(12):
            eng.on_second(S + k * 1000, normal_only())
        return [(e.event_id, e.decision_ms, e.level) for e in eng.events]
    assert drive() == drive()


def test_stop_move_prices_do_not_change_decisions(snap, profile_raw):
    # AC-30：stop_move 价仅溯源不参与判定，极端值也不改变事件
    moved = dataclasses.replace(snap, stop_move_up_price=Decimal("9999"),
                                stop_move_down_price=Decimal("1"))

    def drive(snapshot):
        eng = make_engine(profile_raw, snapshot)
        for k in range(11):
            eng.on_second(S + k * 1000, normal_only())
        return [(e.level, e.decision_ms) for e in eng.events]

    assert drive(snap) == drive(moved)
