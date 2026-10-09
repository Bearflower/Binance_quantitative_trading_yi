"""rules.py 单元测试：快照校验、区域/几何、四分支判定、profile 解析全分支。"""
from decimal import Decimal

import pytest

from strategies.grid.realtime.features import FeatureSlice
from strategies.grid.realtime.rules import (DOWN, UP, Level, Region,
                                           buffer_consumption,
                                           boundary_direction, classify_region,
                                           grid_distance, adverse,
                                           buffer_urgent_candidate,
                                           critical_now, inside_urgent_candidate,
                                           normal_candidate, parse_profile,
                                           pick_state, validate_snapshot)


def make_feat(price="2670.00", r60=Decimal("-0.01"), r180=Decimal("-0.01"),
               r300=Decimal("-0.01"), e300=Decimal("0.8")) -> FeatureSlice:
    price_v = Decimal(price) if price is not None else None
    return FeatureSlice(price_v, {60: r60, 180: r180, 300: r300}, {300: e300})


# ───────────────────── validate_snapshot ─────────────────────

def test_validate_snapshot_ok(snap):
    validate_snapshot(snap, "ETHUSDT")


def test_validate_snapshot_empty_id(snap):
    bad = type(snap)(reference_id="", **{k: getattr(snap, k)
                      for k in snap.__dataclass_fields__ if k != "reference_id"})
    with pytest.raises(ValueError):
        validate_snapshot(bad)


def test_validate_snapshot_symbol_mismatch(snap):
    with pytest.raises(ValueError):
        validate_snapshot(snap, "BTCUSDT")


def test_validate_snapshot_bad_time(snap):
    bad = type(snap)(calculated_at_ms=0, **{k: getattr(snap, k)
                      for k in snap.__dataclass_fields__ if k != "calculated_at_ms"})
    with pytest.raises(ValueError):
        validate_snapshot(bad)


def test_validate_snapshot_non_finite(snap):
    bad = type(snap)(grid_lower=Decimal("nan"), **{k: getattr(snap, k)
                      for k in snap.__dataclass_fields__ if k != "grid_lower"})
    with pytest.raises(ValueError):
        validate_snapshot(bad)


def test_validate_snapshot_order_violation(snap):
    bad = type(snap)(grid_lower=Decimal("2590"), **{k: getattr(snap, k)
                      for k in snap.__dataclass_fields__ if k != "grid_lower"})
    with pytest.raises(ValueError):
        validate_snapshot(bad)


# ───────────────────── 区域与几何 ─────────────────────

@pytest.mark.parametrize("price,region", [
    ("2590.00", Region.BELOW_SL),
    ("2599.45", Region.BELOW_SL),
    ("2623.52", Region.BUFFER_DOWN),
    ("2700.00", Region.INSIDE),
    ("2767.96", Region.BUFFER_UP),
    ("2792.03", Region.ABOVE_SU),
    ("2800.00", Region.ABOVE_SU),
])
def test_classify_region(snap, price, region):
    assert classify_region(snap, Decimal(price)) == region


def test_buffer_consumption_down(snap):
    u = buffer_consumption(snap, Decimal("2611.47"), DOWN)
    assert u == Decimal("12.05") / Decimal("24.07")


def test_buffer_consumption_up(snap):
    u = buffer_consumption(snap, Decimal("2780.00"), UP)
    assert u == Decimal("12.04") / Decimal("24.07")


def test_grid_distance_down(snap):
    g = grid_distance(snap, Decimal("2650.00"), DOWN)
    assert g == Decimal("26.48") / Decimal("144.44")


def test_grid_distance_up(snap):
    g = grid_distance(snap, Decimal("2750.00"), UP)
    assert g == Decimal("17.96") / Decimal("144.44")


def test_adverse():
    assert adverse(DOWN, Decimal("0.01")) == Decimal("-0.01")
    assert adverse(UP, Decimal("0.01")) == Decimal("0.01")
    assert adverse(DOWN, None) is None


# ───────────────────── 分支判定 ─────────────────────

def test_normal_candidate_inside(snap, profile_raw):
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2650.00", r300=Decimal("-0.006"))
    assert normal_candidate(DOWN, Region.INSIDE, f,
                          grid_distance(snap, Decimal("2650"), DOWN), cfg)


def test_normal_candidate_in_buffer(snap, profile_raw):
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2610.00", r300=Decimal("-0.01"))
    assert normal_candidate(DOWN, Region.BUFFER_DOWN, f, None, cfg)


def test_normal_candidate_no_change(snap, profile_raw):
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2650.00", r180=Decimal("-0.001"), r300=Decimal("-0.001"))
    assert not normal_candidate(DOWN, Region.INSIDE, f, Decimal("0.2"), cfg)


def test_normal_candidate_efficiency_blocks(snap, profile_raw):
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2650.00", r300=Decimal("-0.01"), e300=Decimal("0.3"))
    assert not normal_candidate(DOWN, Region.INSIDE, f, Decimal("0.2"), cfg)


def test_normal_candidate_position_far(snap, profile_raw):
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2700.00", r300=Decimal("-0.01"))
    assert not normal_candidate(DOWN, Region.INSIDE, f, Decimal("0.6"), cfg)


def test_normal_candidate_none_features(profile_raw):
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2650", r300=None, e300=None)
    assert not normal_candidate(DOWN, Region.INSIDE, f, Decimal("0.2"), cfg)


def test_normal_candidate_filter_disabled(snap, profile_raw):
    profile_raw["efficiency_filter"] = {"enabled": False}
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2650.00", r300=Decimal("-0.01"), e300=Decimal("0.1"))
    assert normal_candidate(DOWN, Region.INSIDE, f, Decimal("0.2"), cfg)


def test_inside_urgent_ok(snap, profile_raw):
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2640.00", r300=Decimal("-0.01"))
    assert inside_urgent_candidate(DOWN, Region.INSIDE, f, Decimal("0.1"), cfg)


def test_inside_urgent_far_blocks(profile_raw):
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2700.00")
    assert not inside_urgent_candidate(DOWN, Region.INSIDE, f, Decimal("0.6"), cfg)


def test_inside_urgent_efficiency_blocks(profile_raw):
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2640.00", r300=Decimal("-0.01"), e300=Decimal("0.5"))
    assert not inside_urgent_candidate(DOWN, Region.INSIDE, f, Decimal("0.1"), cfg)


def test_inside_urgent_filter_disabled(profile_raw):
    profile_raw["efficiency_filter"] = {"enabled": False}
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2640.00", r300=Decimal("-0.01"), e300=Decimal("0.1"))
    assert inside_urgent_candidate(DOWN, Region.INSIDE, f, Decimal("0.1"), cfg)


def test_buffer_urgent_ok(profile_raw):
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2610.00", r60=Decimal("-0.005"))
    assert buffer_urgent_candidate(DOWN, Region.BUFFER_DOWN, f, cfg)


def test_buffer_urgent_wrong_region(profile_raw):
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2700", r60=Decimal("-0.01"))
    assert not buffer_urgent_candidate(DOWN, Region.INSIDE, f, cfg)


def test_buffer_urgent_threshold(profile_raw):
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2610", r60=Decimal("-0.001"))
    assert not buffer_urgent_candidate(DOWN, Region.BUFFER_DOWN, f, cfg)


def test_buffer_urgent_none(profile_raw):
    cfg = parse_profile(profile_raw)
    f = make_feat(price="2610", r60=None)
    assert not buffer_urgent_candidate(DOWN, Region.BUFFER_DOWN, f, cfg)


def test_critical_now(profile_raw):
    cfg = parse_profile(profile_raw)
    assert critical_now(Decimal("0.8"), cfg)
    assert not critical_now(Decimal("0.5"), cfg)
    assert not critical_now(Decimal("1.0"), cfg)
    assert not critical_now(None, cfg)


def test_boundary_direction():
    assert boundary_direction(Region.BELOW_SL) == DOWN
    assert boundary_direction(Region.ABOVE_SU) == UP
    assert boundary_direction(Region.INSIDE) is None


def test_pick_state():
    assert pick_state([Level.IDLE]) == Level.IDLE
    assert pick_state([Level.CANDIDATE]) == Level.CANDIDATE
    assert pick_state([Level.NOTICE, Level.CANDIDATE]) == Level.NOTICE
    assert pick_state([Level.URGENT, Level.BOUNDARY_REACHED]) == Level.BOUNDARY_REACHED


# ───────────────────── parse_profile ─────────────────────

def test_parse_profile_ok(profile_raw):
    cfg = parse_profile(profile_raw)
    assert cfg["symbol"] == "ETHUSDT" and cfg["normal"]["return_3m"] == Decimal("0.005")


def test_parse_profile_bad_mode(profile_raw):
    profile_raw["mode"] = "live"
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_bad_enabled_type(profile_raw):
    profile_raw["enabled"] = "yes"
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_empty_symbol(profile_raw):
    profile_raw["symbol"] = ""
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_missing_price_source(profile_raw):
    del profile_raw["price_source"]
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_unknown_section(profile_raw):
    profile_raw["bogus"] = {}
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_missing_section(profile_raw):
    del profile_raw["delivery"]
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_fixed_sample(profile_raw):
    profile_raw["features"]["sample_seconds"] = 3
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_fixed_windows(profile_raw):
    profile_raw["features"]["windows_seconds"] = [60, 180]
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_stride_not_divide(profile_raw):
    profile_raw["features"]["e_resample_seconds"] = 7
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_non_number(profile_raw):
    profile_raw["normal"]["return_3m"] = "x"
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_non_finite_string(profile_raw):
    profile_raw["normal"]["return_3m"] = "nan"
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_delivery_max_less_than_initial(profile_raw):
    profile_raw["delivery"]["retry_max_seconds"] = 0.5
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_bad_fraction(profile_raw):
    profile_raw["normal"]["min_efficiency"] = 1.2
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_bad_inside_fraction(profile_raw):
    profile_raw["normal"]["near_grid_fraction"] = 0.5
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_non_positive(profile_raw):
    profile_raw["normal"]["hold_seconds"] = 0
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_non_integer(profile_raw):
    profile_raw["reference"]["max_age_seconds"] = 1.5
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_relative_path(profile_raw):
    profile_raw["storage"]["path"] = "data/x.sqlite"
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_max_less_than_initial(profile_raw):
    profile_raw["transport"]["reconnect_max_seconds"] = 0.5
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_sync_relation(profile_raw):
    profile_raw["reference_sync"]["max_silence_seconds"] = 2
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_shadow_research_interval(profile_raw):
    profile_raw["shadow_research"]["notice_interval_seconds"] = 100
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_shadow_research_bad_enabled(profile_raw):
    profile_raw["shadow_research"]["enabled"] = "no"
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_alert_rejects_stride(profile_raw):
    profile_raw["mode"] = "alert"
    profile_raw["features"]["e_resample_seconds"] = 3
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_parse_profile_alert_ok(profile_raw):
    profile_raw["mode"] = "alert"
    cfg = parse_profile(profile_raw)
    assert cfg["mode"] == "alert"


def test_parse_profile_missing_shadow_section_ok(profile_raw):
    del profile_raw["shadow_research"]
    cfg = parse_profile(profile_raw)
    assert cfg["shadow_research"]["enabled"] is False


def test_parse_profile_efficiency_filter_bad_type(profile_raw):
    profile_raw["efficiency_filter"]["enabled"] = 1
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


# ───────────────────── 跨字段约束 ─────────────────────

def test_cross_urgent_near(profile_raw):
    profile_raw["urgent"]["near_grid_fraction"] = 0.45
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_cross_normal_near_recovery(profile_raw):
    profile_raw["normal"]["near_grid_fraction"] = 0.46
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_cross_urgent_efficiency(profile_raw):
    profile_raw["urgent"]["min_efficiency"] = 0.50
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_cross_urgent_return(profile_raw):
    profile_raw["urgent"]["return_3m"] = 0.001
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_cross_urgent_hold(profile_raw):
    profile_raw["urgent"]["hold_seconds"] = 20
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_cross_boundary_repeat(profile_raw):
    profile_raw["repeat"]["boundary_seconds"] = 400
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_cross_busy_timeout(profile_raw):
    profile_raw["storage"]["busy_timeout_ms"] = 6000
    with pytest.raises(ValueError):
        parse_profile(profile_raw)


def test_cross_recovery_inside_half(profile_raw):
    profile_raw["recovery"]["inside_fraction"] = 0.5
    with pytest.raises(ValueError):
        parse_profile(profile_raw)
