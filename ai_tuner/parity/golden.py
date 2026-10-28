"""Read two local frozen JSON snapshots and report field-level golden parity.

Golden files must explicitly identify their source as ``live``. A synthetic or
unlabelled file cannot satisfy the real-trade gate, even when its values match.
This module performs no database access and does not write report files.
"""

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ai_tuner.replay.models import parse_utc


TRADE_FIELDS = (
    "trade_id", "direction", "signal_time", "signal_price", "entry_time",
    "entry_price", "exit_time", "exit_price", "exit_reason", "realized_pnl",
    "fee_paid", "slippage_cost", "funding_fee", "net_pnl",
)
EQUITY_FIELDS = ("ts", "realized_equity", "marked_equity")
PRICE_FIELDS = frozenset(("signal_price", "entry_price", "exit_price"))
TIME_FIELDS = frozenset(("signal_time", "entry_time", "exit_time", "ts"))
MONEY_FIELDS = frozenset((
    "realized_pnl", "fee_paid", "slippage_cost", "funding_fee", "net_pnl",
    "realized_equity", "marked_equity",
))
CALIBRATION_KEYS = (
    "price_tolerance_bps", "time_tolerance_seconds", "pnl_tolerance_usdt"
)


def _read_snapshot(path):
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def _valid_shape(snapshot):
    return (
        isinstance(snapshot, dict)
        and all(isinstance(snapshot.get(group), list) for group in ("trades", "equity_curve"))
        and all(isinstance(row, dict) for group in ("trades", "equity_curve")
                for row in snapshot[group])
        and bool(snapshot["equity_curve"])
    )


def _calibration(config):
    """Return calibrated tolerances or None when any required value is absent."""
    try:
        values = {key: Decimal(str(config[key])) for key in CALIBRATION_KEYS}
        if any(not value.is_finite() or value < 0 for value in values.values()):
            return None
        return values
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return None


def _json_scalar(value):
    return str(value) if isinstance(value, Decimal) else value


def _compare_value(field, golden, replay, calibration):
    if golden is None or replay is None:
        return False, None
    if field in PRICE_FIELDS:
        tolerance = abs(Decimal(str(golden))) * calibration["price_tolerance_bps"] / Decimal("10000")
        return abs(Decimal(str(replay)) - Decimal(str(golden))) <= tolerance, str(tolerance)
    if field in TIME_FIELDS:
        tolerance = calibration["time_tolerance_seconds"]
        delta = abs(Decimal(str((parse_utc(replay) - parse_utc(golden)).total_seconds())))
        return delta <= tolerance, str(tolerance)
    if field in MONEY_FIELDS:
        tolerance = calibration["pnl_tolerance_usdt"]
        return abs(Decimal(str(replay)) - Decimal(str(golden))) <= tolerance, str(tolerance)
    return golden == replay, None


def _record_compare(compares, field, golden, replay, calibration):
    try:
        passed, tolerance = _compare_value(field.rsplit(".", 1)[-1], golden, replay, calibration)
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        passed, tolerance = False, None
    compares.append({
        "field": field, "golden": golden, "replay": replay,
        "tolerance": tolerance, "pass": passed,
    })


def _future_events(snapshot, as_of):
    """List timestamp paths that cannot belong to this frozen snapshot."""
    invalid = []
    groups = (("trades", TRADE_FIELDS), ("equity_curve", EQUITY_FIELDS))
    for group, fields in groups:
        for index, row in enumerate(snapshot.get(group, [])):
            for field in fields:
                if field not in TIME_FIELDS:
                    continue
                path = "{0}[{1}].{2}".format(group, index, field)
                try:
                    if parse_utc(row[field]) > as_of:
                        invalid.append(path)
                except (KeyError, TypeError, ValueError, OverflowError):
                    invalid.append(path)
    return invalid


def compare_golden(golden_snapshot_path, replay_snapshot_path, config):
    """Return a §5.3 parity report for frozen local JSON files.

    Expected JSON: ``snapshot_ref``, ``source_kind`` (golden: ``live``),
    ``trades`` containing ``TRADE_FIELDS``, and ``equity_curve`` containing
    ``EQUITY_FIELDS``. The caller supplies every tolerance and
    ``min_golden_trades`` in ``config``. No defaults are applied. The
    ``snapshot_ref`` and source markers are declarations in the JSON, not
    cryptographic proof. Before a report is treated as real golden parity, the
    caller must verify the frozen file SHA against a trusted manifest.
    """
    settings = config if isinstance(config, Mapping) else {}
    golden_path = Path(golden_snapshot_path)
    report = {
        "parity_status": "no_baseline", "golden_snapshot_ref": "",
        "compares": [], "field_schema": ["as_of"] + list(TRADE_FIELDS + EQUITY_FIELDS),
        "sample_count": 0,
        "tolerance": {key: _json_scalar(settings.get(key)) for key in CALIBRATION_KEYS},
        "status": "BLOCKED_DATA",
    }
    if not golden_path.is_file():
        return report

    golden = _read_snapshot(golden_path)
    if not _valid_shape(golden):
        report["parity_status"] = "partial_parity"
        return report
    report["golden_snapshot_ref"] = golden.get("snapshot_ref", "")
    trades = golden.get("trades", [])
    is_live = golden.get("source_kind") == "live" and golden.get("is_synthetic") is False
    report["sample_count"] = len(trades) if is_live else 0

    try:
        golden_as_of = parse_utc(golden["as_of"])
        baseline_timing_valid = not _future_events(golden, golden_as_of)
    except (KeyError, TypeError, ValueError, OverflowError):
        baseline_timing_valid = False

    calibration = _calibration(settings)
    try:
        minimum = int(settings["min_golden_trades"])
        if minimum < 1 or minimum != Decimal(str(settings["min_golden_trades"])):
            minimum = None
    except (KeyError, TypeError, ValueError, InvalidOperation):
        minimum = None
    if (not is_live or not baseline_timing_valid or calibration is None
            or minimum is None or report["sample_count"] < minimum):
        report["parity_status"] = "partial_parity"
        return report

    replay = _read_snapshot(replay_snapshot_path)
    compares = report["compares"]
    if not _valid_shape(replay):
        compares.append({
            "field": "replay.snapshot_schema", "golden": "valid frozen snapshot",
            "replay": "missing or malformed", "tolerance": None, "pass": False,
        })
        report["parity_status"] = "mismatch"
        report["status"] = "FAIL"
        return report
    _record_compare(compares, "as_of", golden["as_of"], replay.get("as_of"), calibration)
    try:
        replay_as_of = parse_utc(replay["as_of"])
        for path in _future_events(replay, replay_as_of):
            compares.append({
                "field": path + ".future_guard", "golden": golden["as_of"],
                "replay": replay.get("as_of"), "tolerance": None, "pass": False,
            })
    except (KeyError, TypeError, ValueError, OverflowError):
        compares.append({
            "field": "as_of.valid", "golden": golden["as_of"],
            "replay": replay.get("as_of"), "tolerance": None, "pass": False,
        })
    replay_trades = replay.get("trades", [])
    _record_compare(compares, "trades.count", len(trades), len(replay_trades), calibration)
    for index in range(max(len(trades), len(replay_trades))):
        golden_trade = trades[index] if index < len(trades) else {}
        replay_trade = replay_trades[index] if index < len(replay_trades) else {}
        for field in TRADE_FIELDS:
            _record_compare(compares, "trades[{0}].{1}".format(index, field),
                            golden_trade.get(field), replay_trade.get(field), calibration)

    golden_equity = golden.get("equity_curve", [])
    replay_equity = replay.get("equity_curve", [])
    _record_compare(compares, "equity_curve.count", len(golden_equity), len(replay_equity), calibration)
    for index in range(max(len(golden_equity), len(replay_equity))):
        golden_point = golden_equity[index] if index < len(golden_equity) else {}
        replay_point = replay_equity[index] if index < len(replay_equity) else {}
        for field in EQUITY_FIELDS:
            _record_compare(compares, "equity_curve[{0}].{1}".format(index, field),
                            golden_point.get(field), replay_point.get(field), calibration)

    matched = all(item["pass"] for item in compares)
    report["parity_status"] = "match" if matched else "mismatch"
    report["status"] = "PASS" if matched else "FAIL"
    return report
