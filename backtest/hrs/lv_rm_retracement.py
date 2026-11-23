"""Offline, isolated six-hour LV-RM event study using captured depth receipts.

Run python -m backtest.hrs.lv_rm_retracement --help. Each scored direction is a
separate hypothetical $100 trade. Capital is not shared across observations.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import gzip
import json
import math
from pathlib import Path
from statistics import mean, median

from strategies.hrs.decision_capture import canonical_payload
from strategies.hrs.exit_geometry import calculate_atr
from strategies.hrs.research_liquidity import book_fields


HOUR = 3_600_000
HORIZON = timedelta(hours=6)
MAX_DELAY = timedelta(seconds=60)
SIZE = Decimal("100")
LEVELS = {"fib_382": Decimal(".382"), "fib_618": Decimal(".618"),
          "control_30": Decimal(".30"), "control_70": Decimal(".70")}
ANCHORS = ("wick", "close")
GROUPS = ("all", "aligned", "fib", "control", *LEVELS, "outside_bands", "unclassified")


class Missing(ValueError):
    """A specific evidence gap. The candidate stays in the denominator."""


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError) as exc:
        raise Missing("invalid_timestamp") from exc


def number(value, reason, *, positive=False):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or (positive and n <= 0):
            raise InvalidOperation
        return n
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise Missing(reason) from exc


def metric(value, low=None, high=None):
    try:
        n = float(number(value, "invalid_metric"))
        return n if math.isfinite(n) and (low is None or n >= low) and (high is None or n <= high) else None
    except Missing:
        return None


def closed_prices(raw, interval, observed, minimum):
    if not isinstance(raw, list) or len(raw) < minimum:
        raise Missing("insufficient_bars")
    bars = []
    for bar in raw:
        if not isinstance(bar, dict):
            raise Missing("invalid_bar")
        start = bar.get("open_time")
        if type(start) is not int or start % interval or (bars and start != bars[-1][0] + interval):
            raise Missing("noncontiguous_or_unaligned_bars")
        o, h, l, c = [number(bar.get(k), "invalid_ohlc", positive=True) for k in ("open", "high", "low", "close")]
        if not (l <= o <= h and l <= c <= h):
            raise Missing("invalid_ohlc")
        bars.append((start, h, l, c))
    last_close = bars[-1][0] + interval
    event_ms = int(observed.timestamp() * 1000)
    if not last_close <= event_ms < last_close + interval:
        raise Missing("unclosed_or_stale_signal_bar")
    return bars


def anchor(bars, direction, method):
    window = bars[-49:-1]
    highs = [b[1 if method == "wick" else 3] for b in window]
    lows = [b[2 if method == "wick" else 3] for b in window]
    high, low = max(highs), min(lows)
    high_times = [window[i][0] for i, v in enumerate(highs) if v == high]
    low_times = [window[i][0] for i, v in enumerate(lows) if v == low]
    result = dict(distances_to_levels=None, high=float(high), low=float(low), high_open_ms=high_times, low_open_ms=low_times,
                  wave_direction=None, retracement=None, band=None)
    if high == low:
        return dict(result, status="flat_range")
    # With repeated extrema require every ordering to agree.
    if low_times[-1] < high_times[0]:
        wave, first, second = "up", low_times[-1], high_times[0]
        depth = (high - bars[-1][3]) / (high - low)
    elif high_times[-1] < low_times[0]:
        wave, first, second = "down", high_times[-1], low_times[0]
        depth = (bars[-1][3] - low) / (high - low)
    else:
        return dict(result, status="ambiguous_wave")
    aligned = (wave == "up") == (direction == "long")
    label = next((name for name, level in LEVELS.items() if abs(depth - level) <= Decimal(".03")), None)
    return dict(result, status="aligned" if aligned else "opposite_wave", wave_direction=wave,
                first_open_ms=first, second_open_ms=second, retracement=float(depth), band=label if aligned else None,
                distances_to_levels={name: float(abs(depth - level)) for name, level in LEVELS.items()})


def ema_distance(bars):
    closes = [float(b[3]) for b in bars[-40:]]
    if len(closes) < 20:
        return None
    ema = mean(closes[:20])
    for close in closes[20:]:
        ema += (close - ema) * (2 / 21)
    return closes[-1] / ema - 1


def candidates(row):
    p = row["payload"]
    observed = timestamp(row["event_time"])
    available = timestamp(row["available_at"])
    if available < observed:
        raise ValueError("analysis available before observation")
    for direction in ("long", "short"):
        passed = p.get(direction + "_should_entry")
        if type(passed) is not bool:
            raise ValueError("missing eligibility on lv_rm_analysis " + str(row["evidence_id"]))
        if not passed:
            continue
        score = p.get(direction + "_score") or {}
        rsi = metric(score.get("rsi_value"), 0, 100)
        bb = metric(score.get("bb_position"), 0)
        record = dict(evidence_id=str(row["evidence_id"]), run_id=str(row["run_id"]), symbol=row["symbol"],
                      direction=direction, event_time=observed.isoformat(), available_at=available.isoformat(),
                      signal_origin=p.get("signal_origin"), rsi=rsi, bb_widths_beyond_band=bb,
                      score=metric(score.get("total_score")), volume_24h_usdt=metric(p.get("volume_24h"), 0),
                      oi_usdt=metric(p.get("oi_usd"), 0), ema20_distance_1h=None, ema20_distance_4h=None,
                      signal_close=None, signal_bar_open_ms=None, atr14_1h=None, atr14_over_price=None,
                      analysis_payload_sha256=row.get("payload_sha256"))
        try:
            bars = closed_prices(p.get("klines_1h"), HOUR, observed, 49)
        except Missing as exc:
            record["anchors"] = {a: {"status": str(exc)} for a in ANCHORS}
        else:
            atr = calculate_atr(p["klines_1h"])
            record.update(atr14_1h=atr, atr14_over_price=atr / float(bars[-1][3]), anchors={a: anchor(bars, direction, a) for a in ANCHORS},
                          signal_bar_open_ms=bars[-1][0], signal_close=float(bars[-1][3]), ema20_distance_1h=ema_distance(bars))
        try:
            bars_4h = closed_prices(p.get("klines_4h"), 4 * HOUR, observed, 20)
        except Missing:
            pass
        else:
            record["ema20_distance_4h"] = ema_distance(bars_4h)
        yield record


def read_book(receipt, event, phase):
    if receipt is None:
        raise Missing("missing_receipt")
    payload = receipt.get("payload", receipt)
    if not isinstance(payload, dict):
        raise Missing("invalid_receipt_payload")
    if payload.get("status") != "ok":
        raise Missing(str(payload.get("missing_reason") or "unavailable_book"))
    if (str(payload.get("source_evidence_id")) != event["evidence_id"]
            or str(payload.get("run_id")) != event["run_id"] or payload.get("symbol") != event["symbol"]
            or payload.get("phase") != phase or not isinstance(payload.get("directions"), list)
            or event["direction"] not in payload["directions"]):
        raise Missing("identity_mismatch")
    due = timestamp(event["event_time"]) + (HORIZON if phase == "exit_6h" else timedelta())
    requested = timestamp(payload.get("quote_requested_at"))
    received = timestamp(payload.get("quote_received_at"))
    earliest = max(due, timestamp(event["available_at"]))
    if (timestamp(payload.get("due_at")) != due or requested < earliest
            or received < requested or received > due + MAX_DELAY):
        raise Missing("receipt_time_mismatch")
    if (timestamp(payload.get("signal_time")) != timestamp(event["event_time"])
            or timestamp(payload.get("signal_available_at")) != timestamp(event["available_at"])
            or number(payload.get("probe_notional_usdt"), "probe_size_invalid") != SIZE):
        raise Missing("source_or_probe_mismatch")
    try:
        checked = book_fields({"T": payload.get("exchange_transaction_time_ms"),
                               "E": payload.get("exchange_event_time_ms"),
                               "bids": payload.get("bids"), "asks": payload.get("asks")}, earliest, received)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise Missing(str(exc) if type(exc) is ValueError else "invalid_book") from exc
    book = {side: [(Decimal(p), Decimal(q)) for p, q in checked[side]] for side in ("asks", "bids")}
    bid, ask = book["bids"][0][0], book["asks"][0][0]
    book["received"] = received.isoformat()
    book["spread_bps"] = float(20000 * (ask - bid) / (ask + bid))
    book["bid_depth_usdt"] = checked["bid_depth_usdt"]
    book["ask_depth_usdt"] = checked["ask_depth_usdt"]
    return book


def consume(levels, target, *, quote_units=False):
    """Walk a side by quote notional at entry and by identical base size at exit."""
    remaining = target
    base = cash = Decimal(0)
    for price, size in levels:
        available = price * size if quote_units else size
        used = min(remaining, available)
        base += used / price if quote_units else used
        cash += used if quote_units else used * price
        remaining -= used
        if remaining == 0:
            return base, cash
    raise Missing("insufficient_depth")


def outcome(event, receipts, fee_bps, slippage_bps):
    result = dict(status="excluded", entry_spread_bps=None)
    sign = 1 if event["direction"] == "long" else -1
    for phase in ("entry", "exit_6h"):
        try:
            book = read_book(receipts.get((event["evidence_id"], phase)), event, phase)
            if phase == "entry":
                result.update(entry_spread_bps=book["spread_bps"], entry_bid_depth_usdt=book["bid_depth_usdt"],
                              entry_ask_depth_usdt=book["ask_depth_usdt"])
                side_name = "asks" if sign == 1 else "bids"
                side = book[side_name]
                qty, entry_cash = consume(side, SIZE, quote_units=True)
                entry_vwap = entry_cash / qty
                entry_impact = abs(entry_vwap / side[0][0] - 1) * 10000
                entry_time = book["received"]
            else:
                side_name = "bids" if sign == 1 else "asks"
                side = book[side_name]
                _, exit_cash = consume(side, qty)
                exit_vwap = exit_cash / qty
                exit_impact = abs(exit_vwap / side[0][0] - 1) * 10000
        except Missing as exc:
            reason = f"insufficient_{side_name}_depth" if str(exc) == "insufficient_depth" else str(exc)
            return dict(result, reason=f"{phase}:{reason}")
    slip = slippage_bps / 10000
    modeled_entry = entry_cash * (1 + sign * slip)
    modeled_exit = exit_cash * (1 - sign * slip)
    fees = (modeled_entry + modeled_exit) * fee_bps / 10000
    net = sign * (modeled_exit - modeled_entry) - fees
    return dict(result, status="matched", quantity=float(qty), entry_vwap=float(entry_vwap),
                exit_vwap=float(exit_vwap), entry_depth_impact_bps=float(entry_impact), exit_depth_impact_bps=float(exit_impact),
                entry_received_at=entry_time, exit_received_at=book["received"], modeled_fees_usdt=float(fees),
                return_before_fees_and_extra_slippage_bps=float(sign * (exit_cash / entry_cash - 1) * 10000),
                pnl_after_transaction_costs_usdt=float(net), return_after_cost_bps=float(net / modeled_entry * 10000))


def strata(event):
    rsi = event["rsi"]
    excess = None if rsi is None else (rsi - 70 if event["direction"] == "short" else 30 - rsi)
    bb = event["bb_widths_beyond_band"]
    spread = event["outcome"]["entry_spread_bps"]
    entry_side = "ask" if event["direction"] == "long" else "bid"
    depth = event["outcome"].get(f"entry_{entry_side}_depth_usdt")
    return {"direction": event["direction"],
            "entry_side_depth": "unknown" if depth is None else ("depth_lt1000usdt" if depth < 1000 else "depth_ge1000usdt"),
            "rsi": "unknown" if excess is None else ("excess_ge10" if excess >= 10 else "excess_lt10"),
            "bb": "unknown" if bb is None else ("breakout_ge0.10" if bb >= .10 else "breakout_lt0.10"),
            "liquidity": "unknown" if spread is None else ("spread_gt10bps" if spread > 10 else "spread_le10bps")}


def summarize(events):
    returns = [e["outcome"]["return_after_cost_bps"] for e in events if e["outcome"]["status"] == "matched"]
    return dict(candidates=len(events), matched=len(returns), excluded=len(events) - len(returns),
                coverage=len(returns) / len(events) if events else None,
                mean_bps=mean(returns) if returns else None, median_bps=median(returns) if returns else None,
                win_rate=sum(x > 0 for x in returns) / len(returns) if returns else None)


def groups_for(event, anchor_name):
    a = event["anchors"][anchor_name]
    if a["status"] != "aligned":
        return ("all", "unclassified")
    if a["band"] is None:
        return ("all", "aligned", "outside_bands")
    return ("all", "aligned", a["band"].split("_")[0], a["band"])


def concentration(events, field):
    opportunities = Counter(e[field] for e in events)
    counts = Counter()
    pnl = Counter()
    gross_abs = 0.
    for e in events:
        if e["outcome"]["status"] == "matched":
            key = e[field]
            ret = e["outcome"]["return_after_cost_bps"]
            counts[key] += 1
            pnl[key] += ret
            gross_abs += abs(ret)
    n = sum(counts.values())
    total = sum(pnl.values())
    omitted = [(total - pnl[k]) / (n - v) for k, v in counts.items() if n > v]
    return dict(candidate_counts=dict(sorted(opportunities.items())), candidate_distinct=len(opportunities),
                largest_candidate_share=max(opportunities.values()) / len(events) if events else None,
                distinct=len(counts), largest_count_share=max(counts.values()) / n if n else None,
                largest_net_contribution_over_gross_abs=max(map(abs, pnl.values())) / gross_abs if gross_abs else None,
                leave_one_out_mean_range_bps=[min(omitted), max(omitted)] if omitted else None,
                by_key={k: dict(n=v, sum_return_bps=pnl[k]) for k, v in sorted(counts.items())})


def mean_difference(summaries, left, right):
    if not summaries[left]["matched"] or not summaries[right]["matched"]:
        return None
    return summaries[left]["mean_bps"] - summaries[right]["mean_bps"]


def compare(events, anchor_name):
    groups = {g: [] for g in GROUPS}
    for e in events:
        for g in groups_for(e, anchor_name):
            groups[g].append(e)
    cells = defaultdict(lambda: defaultdict(list))
    for g in ("all", "fib", "control"):
        for e in groups[g]:
            for key, value in e["strata"].items():
                cells[f"{key}/{value}"][g].append(e)
            joint = "/".join(e["strata"][k] for k in ("direction", "rsi", "bb", "liquidity", "entry_side_depth"))
            cells["joint/" + joint][g].append(e)
    weighted_diff = 0.
    support = 0
    stratified = {}
    for label, cell in sorted(cells.items()):
        s = {g: summarize(cell[g]) for g in ("all", "fib", "control")}
        both = s["fib"]["matched"] and s["control"]["matched"]
        delta = mean_difference(s, "fib", "control")
        stratified[label] = dict(**s, fib_minus_control_bps=delta, fib_minus_all_bps=mean_difference(s, "fib", "all"))
        if both and label.startswith("joint/") and "unknown" not in label:
            weight = s["fib"]["matched"] + s["control"]["matched"]
            weighted_diff += weight * delta
            support += weight
    summaries = {g: summarize(v) for g, v in groups.items()}
    return dict(groups=summaries, strata=stratified, fib_minus_control_bps=mean_difference(summaries, "fib", "control"),
                fib_minus_all_bps=mean_difference(summaries, "fib", "all"),
                pooled_common_strata_fib_minus_control_bps=weighted_diff / support if support else None,
                common_strata_matched=support, anchor_status=dict(Counter(e["anchors"][anchor_name]["status"] for e in events)),
                concentration={g: {k: concentration(groups[g], k) for k in ("symbol", "run_id", "overlap_event", "utc_day")}
                               for g in ("all", "fib", "control")})


def anchor_sensitivity(events):
    """Count a wick changing Fibonacci membership for the very same signal."""
    transitions = Counter()
    flips = comparable = 0
    for event in events:
        labels = []
        for anchor_name in ANCHORS:
            value = event["anchors"][anchor_name]
            labels.append((value.get("band") or "outside_bands") if value["status"] == "aligned" else value["status"])
        wick, close = labels
        transitions[(wick, close)] += 1
        if all(event["anchors"][name]["status"] == "aligned" for name in ANCHORS):
            comparable += 1
            flips += wick.startswith("fib_") != close.startswith("fib_")
    return dict(candidates=len(events), both_aligned=comparable, fib_membership_flips=flips,
                flip_share_when_both_aligned=flips / comparable if comparable else None,
                transitions=[dict(wick=w, close=c, count=n) for (w,c),n in sorted(transitions.items())])


def build_report(analysis, quotes, holdout_start, *, fee_bps=4, slippage_bps=0):
    cutoff = timestamp(holdout_start)
    fee = number(fee_bps, "invalid_fee")
    slip = number(slippage_bps, "invalid_slippage")
    if not (0 <= fee < 10000 and 0 <= slip < 10000):
        raise ValueError("costs must be in [0,10000) bps")
    receipts = {}
    for q in quotes:
        key = (str(q["evidence_id"]), q["phase"])
        if key in receipts:
            raise ValueError(f"duplicate quote identity: {key}")
        receipts[key] = q
    seen = set()
    events = []
    rows = 0
    for r in analysis:
        if r.get("kind", "lv_rm_analysis") != "lv_rm_analysis":
            continue
        if not all(r.get(k) is not None for k in ("evidence_id", "run_id", "symbol", "event_time", "available_at", "payload")):
            raise ValueError("incomplete analysis envelope")
        if r.get("payload_sha256") and canonical_payload(r["payload"])[1] != r["payload_sha256"]:
            raise ValueError(f"analysis payload hash mismatch: {r['evidence_id']}")
        key = str(r["evidence_id"])
        if key in seen:
            raise ValueError(f"duplicate analysis identity: {key}")
        seen.add(key)
        rows += 1
        events.extend(candidates(r))
    events.sort(key=lambda e: (e["event_time"], e["symbol"], e["direction"], e["evidence_id"]))
    clusters = {}
    periods = defaultdict(list)
    for e in events:
        when = timestamp(e["event_time"])
        cluster_key = (e["symbol"], e["direction"])
        previous = clusters.get(cluster_key)
        if previous is None or when > previous[0] + HORIZON:
            event_id = "/".join((*cluster_key, e["evidence_id"]))
        else:
            event_id = previous[1]
        clusters[cluster_key] = when, event_id
        e["overlap_event"] = event_id
        e["utc_day"] = when.date().isoformat()
        e["outcome"] = outcome(e, receipts, fee, slip)
        e["strata"] = strata(e)
        # Embargo the whole prospective exit quote window, regardless of result.
        e["period"] = ("holdout" if when >= cutoff else
                       "discovery" if when + HORIZON + MAX_DELAY < cutoff else "boundary_embargo")
        periods[e["period"]].append(e)
    return dict(study="hrs_lv_rm_fixed_6h_v1", holdout_start=cutoff.isoformat(), probe_notional_usdt=100,
                fee_bps_per_side=float(fee), extra_adverse_slippage_bps_per_side=float(slip),
                model="Independent pre-slot opportunities. Displayed depth VWAP, same base quantity on exit, before funding. "
                      "No initial-stop evidence, R multiple, lot rounding, shared capital, protective exits or live fills.",
                analysis_rows=rows, directions_in_exported_rows=2 * rows, candidates=len(events), quote_receipts=len(receipts),
                anchor_sensitivity={name: anchor_sensitivity(periods[name]) for name in
                                    ("discovery", "holdout", "boundary_embargo")},
                exclusions=dict(Counter(e["outcome"]["reason"] for e in events if e["outcome"]["status"] == "excluded")),
                periods={name: {a: compare(periods[name], a) for a in ANCHORS} for name in
                         ("discovery", "holdout", "boundary_embargo")}, events=events)


def jsonlines(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--analysis", type=Path, required=True, help="original lv_rm_analysis envelopes (JSONL or .gz)")
    ap.add_argument("--quotes", type=Path, required=True, help="receipts as {evidence_id,phase,payload} (JSONL or .gz); empty file allowed")
    ap.add_argument("--holdout-start", required=True, help="preregistered ISO8601 timestamp with timezone")
    ap.add_argument("--fee-bps", default="4", help="modeled fee per side in bps")
    ap.add_argument("--slippage-bps", default="0", help="extra adverse slippage per side beyond observed depth")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    report = build_report(jsonlines(args.analysis), jsonlines(args.quotes), args.holdout_start,
                          fee_bps=args.fee_bps, slippage_bps=args.slippage_bps)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
