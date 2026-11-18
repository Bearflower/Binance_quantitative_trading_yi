"""Reconcile frozen prospective observations with actual funding settlements."""

import hashlib
import json
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

_SLOT_MS = 5 * 60 * 1000
_FRESHNESS_MS = 120 * 1000
_MATCH_TOLERANCE = timedelta(seconds=1)


def _millis(value):
    if type(value) is not int or value <= 0:
        raise ValueError("expected a positive integer millisecond timestamp")
    return value


def _utc_ms(value):
    if value.tzinfo is None:
        raise ValueError("expected a timezone-aware datetime")
    return int(value.timestamp() * 1000)


def _time(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc)


def _observation(row, symbol):
    payload = row["raw_payload"]
    if payload["symbol"] != symbol:
        raise ValueError("observation symbol mismatch")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(encoded).hexdigest() != row["payload_hash"]:
        raise ValueError("observation hash mismatch")
    slot = _millis(row["scheduled_at_ms"])
    requested = _millis(row["request_started_at_ms"])
    received = _millis(row["received_at_ms"])
    source = _millis(payload["time"])
    target = _millis(payload["nextFundingTime"])
    if (slot % _SLOT_MS or not slot <= requested <= received <= slot + _FRESHNESS_MS
            or not 0 <= received - source <= _FRESHNESS_MS
            or received >= target):
        raise ValueError("invalid observation timing")
    interval = payload["fundingIntervalHours"]
    if interval is not None and (type(interval) is not int or interval <= 0):
        raise ValueError("invalid funding interval")
    return slot, target, received


def read_actual_funding(text, symbol):
    """Require explicit venue rows when certifying an actual settlement."""
    stripped = text.strip()
    rows = json.loads(stripped) if stripped.startswith("[") else [
        json.loads(line) for line in stripped.splitlines() if line.strip()
    ]
    actual = []
    for row in rows:
        rate = Decimal(str(row["fundingRate"]))
        if row["symbol"] != symbol or not rate.is_finite():
            raise ValueError("invalid actual funding row")
        actual.append((_time(_millis(row["fundingTime"])), rate))
    return actual


def verify_funding_schedule(rows, settlements, *, start_at, end_at, symbol):
    """Report matches and coverage. Never infer missing forecasts from realized fees."""
    start_ms, end_ms = _utc_ms(start_at), _utc_ms(end_at)
    if (start_at.microsecond or end_at.microsecond or start_ms % _SLOT_MS
            or end_ms % _SLOT_MS or start_ms >= end_ms):
        raise ValueError("funding audit window must align to five-minute slots")

    # A forecast just before the window can establish a settlement at its left edge.
    expected_slots = set(range(start_ms - _SLOT_MS, end_ms, _SLOT_MS))
    by_slot = defaultdict(set)
    forecasts = {}
    invalid = 0
    for row in rows:
        try:
            slot, target, received = _observation(row, symbol)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            invalid += 1
            continue
        if slot not in expected_slots:
            continue
        by_slot[slot].add(target)
        if start_ms <= target < end_ms:
            forecasts[target] = min(forecasts.get(target, received), received)

    ordered_actual = sorted(
        ((time, rate) for time, rate in settlements if start_at <= time < end_at),
        key=lambda pair: pair[0],
    )
    times = [time for time, _ in ordered_actual]
    assigned = set()
    matches, missing, ambiguous = [], [], []
    for target, receipt in sorted(forecasts.items()):
        expected_at = _time(target)
        left = bisect_left(times, expected_at - _MATCH_TOLERANCE)
        right = bisect_right(times, expected_at + _MATCH_TOLERANCE)
        candidates = [index for index in range(left, right) if _time(receipt) < times[index]]
        if len(candidates) == 1 and candidates[0] not in assigned:
            index = candidates[0]
            assigned.add(index)
            actual_at, rate = ordered_actual[index]
            matches.append({
                "expected_at": expected_at.isoformat(),
                "observed_at": _time(receipt).isoformat(),
                "actual_at": actual_at.isoformat(),
                "funding_rate": str(rate),
            })
        elif candidates:
            ambiguous.append(expected_at.isoformat())
        else:
            missing.append(expected_at.isoformat())

    missing_slots = sorted(expected_slots - by_slot.keys())
    conflicting_slots = sorted(slot for slot, targets in by_slot.items() if len(targets) > 1)
    unexpected = [time.isoformat() for index, time in enumerate(times) if index not in assigned]
    reasons = [name for name, count in (
        ("missing_observation_slots", len(missing_slots)),
        ("conflicting_observations", len(conflicting_slots)),
        ("invalid_observations", invalid),
        ("missing_settlement", len(missing)),
        ("ambiguous_settlement", len(ambiguous)),
        ("unexpected_settlement", len(unexpected)),
    ) if count]
    return {
        "verified": not reasons,
        "reasons": reasons,
        "expected_observation_slots": len(expected_slots),
        "observed_slots": len(by_slot),
        "missing_observation_slots": len(missing_slots),
        "first_missing_slot": _time(missing_slots[0]).isoformat() if missing_slots else None,
        "last_missing_slot": _time(missing_slots[-1]).isoformat() if missing_slots else None,
        "conflicting_observation_slots": [_time(slot).isoformat() for slot in conflicting_slots],
        "invalid_observations": invalid,
        "matches": matches,
        "missing_settlements": missing,
        "ambiguous_settlements": ambiguous,
        "unexpected_settlements": unexpected,
    }


def audit_funding_files(evidence_bytes, actual_text, *, start_at, end_at, symbol):
    try:
        rows = [json.loads(line) for line in evidence_bytes.splitlines() if line.strip()]
        return verify_funding_schedule(
            rows, read_actual_funding(actual_text, symbol),
            start_at=start_at, end_at=end_at, symbol=symbol,
        )
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError, OSError):
        return {"verified": False, "reasons": ["invalid_funding_evidence"]}
