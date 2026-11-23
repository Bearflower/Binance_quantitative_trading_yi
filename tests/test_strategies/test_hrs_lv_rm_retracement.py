import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import gzip
import json
import subprocess
import sys

import pytest

from backtest.hrs import lv_rm_retracement as study


UTC = timezone.utc
SIGNAL = datetime(2026, 11, 20, 8, 1, tzinfo=UTC)
CUTOFF = "2026-12-01T00:00:00+08:00"


def signal(eid="1", at=SIGNAL, direction="long", retracement=".382", symbol="AUSDT"):
    # Prior extrema are 100 -> 200 on an up wave and 200 -> 100 on a down wave.
    closes = [150] * 48
    closes[8], closes[26] = (100, 200) if direction == "long" else (200, 100)
    depth = Decimal(retracement)
    closes.append(200 - depth * 100 if direction == "long" else 100 + depth * 100)
    cutoff_ms = int(at.timestamp() * 1000) // study.HOUR * study.HOUR
    bars = [dict(open_time=cutoff_ms - (49 - i) * study.HOUR, open=str(px), close=str(px), high=str(px), low=str(px))
            for i, px in enumerate(closes)]
    payload = dict(klines_1h=bars, klines_4h=[], signal_origin="empty_pool", volume_24h=10000000,
                   short_should_entry=direction == "short", long_should_entry=direction == "long")
    payload[direction + "_score"] = dict(rsi_value=25 if direction == "long" else 75, bb_position=.08, total_score=6.5)
    return dict(evidence_id=eid, run_id="cycle-" + eid, kind="lv_rm_analysis", event_time=at.isoformat(),
                available_at=at.isoformat(), symbol=symbol, payload=payload)


def quote(signal, phase, bids=None, asks=None):
    when = study.timestamp(signal["event_time"])
    due = when if phase == "entry" else when + study.HORIZON
    ms = int(due.timestamp() * 1000)
    raw = dict(source_evidence_id=signal["evidence_id"], phase=phase, run_id=signal["run_id"], symbol=signal["symbol"],
               directions=[d for d in ("short", "long") if signal["payload"][d + "_should_entry"]], status="ok",
               probe_notional_usdt=100, signal_time=signal["event_time"], signal_available_at=signal["available_at"],
               due_at=due.isoformat(), quote_requested_at=(due + timedelta(seconds=1)).isoformat(),
               quote_received_at=(due + timedelta(seconds=2)).isoformat(), exchange_transaction_time_ms=ms + 1000,
               exchange_event_time_ms=ms + 1500, bids=bids if bids is not None else [["99", "4"]],
               asks=asks if asks is not None else [["100", "4"]])
    return dict(evidence_id=signal["evidence_id"], phase=phase, payload=raw)


def quotes(row):
    return [quote(row, p) for p in ("entry", "exit_6h")]


def event(row, receipts=None):
    return study.build_report([row], quotes(row) if receipts is None else receipts, CUTOFF)["events"][0]


@pytest.mark.parametrize("direction,label,fraction", [("long", "fib_382", ".352"), ("long", "fib_618", ".648"),
                                                      ("short", "control_30", ".33"), ("short", "control_70", ".67")])
def test_closed_directional_waves_and_exact_band_edges(direction, label, fraction):
    row = signal(direction=direction, retracement=fraction)
    e = event(row)
    assert e["anchors"]["wick"]["band"] == label
    assert e["anchors"]["close"]["band"] == label
    assert e["signal_close"] and e["ema20_distance_1h"] is not None
    assert e["ema20_distance_4h"] is None


def test_wicks_and_closes_distinct_anchors_exclude_signal_bar_and_unordered_waves():
    row = signal()
    bars = row["payload"]["klines_1h"]
    bars[-1]["high"] = "999"  # part of the signal bar only
    bars[26]["high"] = "240"
    e = event(row)
    assert e["anchors"]["wick"]["high"] == 240
    assert e["anchors"]["wick"]["band"] is None
    assert e["anchors"]["close"]["band"] == "fib_382"
    bars[-2]["low"] = "100"  # ambiguity about whether low or high came first
    assert event(row)["anchors"]["wick"]["status"] == "ambiguous_wave"


@pytest.mark.parametrize("mutation,reason", [
    (lambda b: b.pop(10), "insufficient_bars"),
    (lambda b: b[10].update(open_time=b[9]["open_time"]), "noncontiguous_or_unaligned_bars"),
    (lambda b: b[-1].update(low="NaN"), "invalid_ohlc"),
    (lambda b: [r.update(open_time=r["open_time"] + study.HOUR) for r in b], "unclosed_or_stale_signal_bar"),
])
def test_invalid_history_never_fabricates_retracements(mutation, reason):
    row = signal()
    mutation(row["payload"]["klines_1h"])
    r = study.build_report([row], [], CUTOFF)
    assert r["events"][0]["anchors"]["close"]["status"] == reason
    groups = r["periods"]["discovery"]["wick"]["groups"]
    assert groups["unclassified"]["candidates"] == groups["all"]["candidates"] == 1
    assert groups["fib"]["candidates"] == 0


def test_depth_walk_same_quantity_and_entry_and_exit_fees():
    row = signal()
    receipt = [quote(row, "entry", bids=[["48", "3"]], asks=[["50", "1"], ["100", "1"]]),
               quote(row, "exit_6h", bids=[["80", ".5"], ["76", "1"]], asks=[["81", "3"]])]
    r = study.build_report([row], receipt, CUTOFF, fee_bps=10, slippage_bps=20)
    out = r["events"][0]["outcome"]
    assert out["quantity"] == 1.5
    assert out["entry_vwap"] == pytest.approx(100 / 1.5)
    assert out["exit_vwap"] == pytest.approx(116 / 1.5)
    assert out["modeled_fees_usdt"] == pytest.approx((100.2 + 116 * .998) * .001)
    assert out["return_after_cost_bps"] == pytest.approx((116 * .998 - 100.2 - out["modeled_fees_usdt"]) / 100.2 * 10000)


def test_short_sells_bid_and_buys_ask():
    row = signal(direction="short")
    receipt = [quote(row, "entry", bids=[["100", "1"]], asks=[["110", "5"]]),
               quote(row, "exit_6h", bids=[["80", "4"]], asks=[["90", "1"]])]
    out = study.build_report([row], receipt, CUTOFF, fee_bps=0)["events"][0]["outcome"]
    assert out["return_after_cost_bps"] == 1000


@pytest.mark.parametrize("phase,modify,reason", [
    ("entry", lambda p: p.update(source_evidence_id="foreign"), "identity_mismatch"),
    ("entry", lambda p: p.update(status="missing", missing_reason="collector_down"), "collector_down"),
    ("exit_6h", lambda p: p.update(exchange_transaction_time_ms=5), "book_timestamp_outside_window"),
    ("exit_6h", lambda p: p.update(quote_received_at=(SIGNAL + study.HORIZON + timedelta(seconds=61)).isoformat()), "receipt_time_mismatch"),
    ("entry", lambda p: p.update(asks=[["100", ".1"]]), "insufficient_asks_depth"),
    ("exit_6h", lambda p: p.update(bids=[["99", ".1"]]), "insufficient_bids_depth"),
    ("entry", lambda p: p.update(bids=[["101", "5"]]), "crossed_book"),
    ("entry", lambda p: p.update(asks=[["100", "1"], ["90", "1"]]), "unordered_levels"),
])
def test_bad_receipts_exclude_outcomes_only(phase, modify, reason):
    row = signal()
    pair = quotes(row)
    modify(next(q["payload"] for q in pair if q["phase"] == phase))
    r = study.build_report([row], pair, CUTOFF)
    assert r["exclusions"] == {phase + ":" + reason: 1}
    g = r["periods"]["discovery"]["close"]["groups"]["fib"]
    assert g["candidates"] == g["excluded"] == 1
    assert g["mean_bps"] is None


def test_missing_pair_no_fallback_and_unseen_period_embargo():
    cutoff = study.timestamp(CUTOFF)
    pending = signal("a", at=cutoff - timedelta(minutes=45))
    future = signal("b", at=cutoff)
    r = study.build_report([pending, future], [quote(future, "entry")], CUTOFF)
    assert r["exclusions"] == {"entry:missing_receipt": 1, "exit_6h:missing_receipt": 1}
    assert [(e["evidence_id"], e["period"]) for e in r["events"]] == [("a", "boundary_embargo"), ("b", "holdout")]
    assert r["periods"]["discovery"]["wick"]["groups"]["all"]["candidates"] == 0


def test_shared_strata_comparison_and_overlap_event_concentration():
    a = signal("a")
    b = signal("b", at=SIGNAL + timedelta(hours=1), retracement=".70")
    c = signal("c", at=SIGNAL + timedelta(hours=12), symbol="BUSDT", retracement=".30")
    c["payload"]["long_score"]["rsi_value"] = 19  # unmatched stratum
    a_quotes = [quote(a, "entry"), quote(a, "exit_6h", bids=[["109", "2"]], asks=[["110", "2"]])]
    r = study.build_report([a, b, c], a_quotes + quotes(b) + quotes(c), CUTOFF, fee_bps=0)
    view = r["periods"]["discovery"]["wick"]
    assert view["common_strata_matched"] == 2
    assert view["pooled_common_strata_fib_minus_control_bps"] == 1000
    cc = view["concentration"]["all"]
    assert cc["symbol"]["largest_count_share"] == pytest.approx(2 / 3)
    assert cc["overlap_event"]["distinct"] == 2
    assert view["groups"]["control"]["matched"] == 2


def test_double_identity_and_unknown_eligibility_fail_explicitly():
    row = signal()
    with pytest.raises(ValueError, match="duplicate analysis"):
        study.build_report([row, copy.deepcopy(row)], [], CUTOFF)
    pair = quotes(row)
    with pytest.raises(ValueError, match="duplicate quote"):
        study.build_report([row], pair + pair, CUTOFF)
    row["payload"]["short_should_entry"] = None
    with pytest.raises(ValueError, match="eligibility"):
        study.build_report([row], [], CUTOFF)


def test_cli_gzipped_input_and_two_directions_per_evidence(tmp_path):
    row = signal()
    row["payload"]["short_should_entry"] = True
    src = tmp_path / "raw.jsonl.gz"
    with gzip.open(src, "wt") as f:
        f.write(json.dumps(row) + "\n")
    books = tmp_path / "books.jsonl"
    books.write_text("\n".join(map(json.dumps, quotes(row))))
    dest = tmp_path / "report.json"
    subprocess.run([sys.executable, "-m", "backtest.hrs.lv_rm_retracement", "--analysis", str(src),
                    "--quotes", str(books), "--holdout-start", CUTOFF, "--output", str(dest)], check=True)
    r = json.loads(dest.read_text())
    assert r["candidates"] == 2
    assert r["periods"]["discovery"]["close"]["groups"]["all"]["matched"] == 2


def test_known_opposite_wave_keeps_extrema_and_raw_ratio_but_no_membership():
    row = signal()
    row["payload"].update(short_should_entry=True, long_should_entry=False)
    e = event(row)
    a = e["anchors"]["wick"]
    assert a["status"] == "opposite_wave"
    assert a["wave_direction"] == "up" and a["band"] is None
    assert a["retracement"] == pytest.approx(.382)
    assert a["high_open_ms"] and a["low_open_ms"] and a["distances_to_levels"]["fib_382"] == 0
    assert e["atr14_1h"] > 0


def test_hashed_raw_analysis_and_mismatched_availability():
    from strategies.hrs.decision_capture import canonical_payload

    row = signal()
    row["available_at"] = (SIGNAL + timedelta(seconds=1)).isoformat()
    row["payload_sha256"] = canonical_payload(row["payload"])[1]
    pair = quotes(row)
    pair[0]["payload"]["exchange_transaction_time_ms"] -= 1000
    r = study.build_report([row], pair, CUTOFF)
    assert r["exclusions"] == {"entry:book_timestamp_outside_window": 1}
    row["payload"]["volume_24h"] = 11000000
    with pytest.raises(ValueError, match="hash mismatch"):
        study.build_report([row], pair, CUTOFF)


def test_single_wick_changes_same_event_fib_membership():
    row = signal()
    row["payload"]["klines_1h"][26]["high"] = "240"
    report = study.build_report([row], [], CUTOFF)
    comparison = report["anchor_sensitivity"]["discovery"]
    assert comparison["both_aligned"] == comparison["fib_membership_flips"] == 1
    assert comparison["transitions"] == [dict(wick="outside_bands", close="fib_382", count=1)]
    assert report["anchor_sensitivity"]["holdout"]["flip_share_when_both_aligned"] is None
