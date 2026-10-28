"""Deterministic, I/O-free ATR calibration helpers for the XAU shadow run."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd

from shared.indicators import TechnicalIndicators


ALGORITHM_VERSION = "xau-atr14-sma-v1"
_ONE_HOUR_MS = 3_600_000


class CalibrationError(ValueError):
    """Raised when calibration evidence cannot be accepted safely."""


@dataclass(frozen=True)
class CalibrationResult:
    cutoff_ms: int
    source_rows: int
    valid_rows: int
    atr_sample_count: int
    exclusions: Dict[str, int]
    gaps: List[Dict[str, int]]
    p25_percent: float
    p50_percent: float
    atr_abs_min: float
    snapshot_sha256: str
    snapshot_rows: List[Dict[str, Any]]

    def to_manifest(
        self,
        *,
        source: str,
        git_commit: str,
        snapshot_sha256: str,
        generated_at: str,
    ) -> Dict[str, Any]:
        manifest = {
            "schema_version": 1,
            "algorithm_version": ALGORITHM_VERSION,
            "symbol": "XAUUSDT",
            "interval": "1h",
            "source": source,
            "generated_at": generated_at,
            "cutoff_ms": self.cutoff_ms,
            "snapshot_start_close_time": self.snapshot_rows[0]["close_time"],
            "snapshot_end_close_time": self.snapshot_rows[-1]["close_time"],
            "source_rows": self.source_rows,
            "valid_rows": self.valid_rows,
            "atr_sample_count": self.atr_sample_count,
            "exclusions": self.exclusions,
            "gaps": self.gaps,
            "snapshot_sha256": snapshot_sha256,
            "git_commit": git_commit,
            "calibration": {
                "p25_percent": self.p25_percent,
                "p50_percent": self.p50_percent,
                "atr_abs_min": self.atr_abs_min,
                "fallback_atr_percent": self.p50_percent,
                "atr_percentile": 50,
                "min_history_count": 30,
            },
        }
        manifest["manifest_sha256"] = _manifest_hash(manifest)
        return manifest


def _manifest_hash(manifest: Dict[str, Any]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    try:
        payload = json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CalibrationError("manifest_not_canonicalizable") from exc
    return hashlib.sha256(payload).hexdigest()


def canonical_snapshot_bytes(rows: Iterable[Dict[str, Any]]) -> bytes:
    try:
        lines = [
            json.dumps(
                row,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            for row in rows
        ]
    except (TypeError, ValueError) as exc:
        raise CalibrationError("snapshot_not_canonicalizable") from exc
    return ("\n".join(lines) + "\n").encode("utf-8")


def _increment(exclusions: Dict[str, int], reason: str) -> None:
    exclusions[reason] = exclusions.get(reason, 0) + 1


def _finite_number(row: Dict[str, Any], key: str) -> float:
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CalibrationError(f"invalid_{key}") from exc
    if not math.isfinite(value):
        raise CalibrationError(f"invalid_{key}")
    return value


def _integer(row: Dict[str, Any], key: str) -> int:
    try:
        raw = row[key]
        value = int(raw)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise CalibrationError(f"invalid_{key}") from exc
    if isinstance(raw, float) and (not math.isfinite(raw) or raw != value):
        raise CalibrationError(f"invalid_{key}")
    return value


def _shape_candidate(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise CalibrationError("invalid_row")
    trades_key = "trades" if "trades" in raw else "trade_count"
    return {
        "open_time": _integer(raw, "open_time"),
        "close_time": _integer(raw, "close_time"),
        "open": _finite_number(raw, "open"),
        "high": _finite_number(raw, "high"),
        "low": _finite_number(raw, "low"),
        "close": _finite_number(raw, "close"),
        "volume": _finite_number(raw, "volume"),
        "trades": _integer(raw, trades_key),
    }


def _candidate_sort_key(row: Dict[str, Any]) -> Tuple[int, str]:
    canonical = json.dumps(row, sort_keys=True, separators=(",", ":"))
    return row["close_time"], canonical


def _normalize_rows(
    rows: Iterable[Dict[str, Any]], cutoff_ms: int
) -> Tuple[
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    Dict[str, int],
    List[Dict[str, int]],
]:
    source = list(rows)
    exclusions: Dict[str, int] = {}
    candidates: List[Dict[str, Any]] = []
    for raw in source:
        try:
            candidates.append(_shape_candidate(raw))
        except CalibrationError:
            _increment(exclusions, "invalid_shape")

    accepted: Dict[int, Dict[str, Any]] = {}
    for candidate in sorted(candidates, key=_candidate_sort_key):
        close_time = candidate["close_time"]
        open_time = candidate["open_time"]
        if close_time > cutoff_ms or close_time <= open_time:
            _increment(exclusions, "future_or_open")
            continue
        if close_time in accepted:
            _increment(exclusions, "duplicate_close_time")
            continue

        open_price = candidate["open"]
        high = candidate["high"]
        low = candidate["low"]
        close = candidate["close"]
        if (
            min(open_price, high, low, close) <= 0
            or high < max(open_price, close)
            or low > min(open_price, close)
        ):
            _increment(exclusions, "invalid_ohlc")
            continue
        if candidate["volume"] <= 0 or candidate["trades"] <= 0:
            _increment(exclusions, "non_positive_activity")
            continue
        if high <= low:
            _increment(exclusions, "zero_range")
            continue
        accepted[close_time] = candidate

    normalized = [accepted[close_time] for close_time in sorted(accepted)]
    gaps: List[Dict[str, int]] = []
    for previous, current in zip(normalized, normalized[1:]):
        delta = current["close_time"] - previous["close_time"]
        if delta != _ONE_HOUR_MS:
            gaps.append(
                {
                    "previous_close_time": previous["close_time"],
                    "next_close_time": current["close_time"],
                    "delta_ms": delta,
                }
            )
    return source, normalized, dict(sorted(exclusions.items())), gaps


def build_calibration(
    rows: Iterable[Dict[str, Any]], *, cutoff_ms: int
) -> CalibrationResult:
    source, normalized, exclusions, gaps = _normalize_rows(rows, cutoff_ms)
    if len(normalized) < 30:
        raise CalibrationError(f"valid_rows_below_30:{len(normalized)}")

    frame = pd.DataFrame(normalized)
    atr = TechnicalIndicators.calculate_atr(frame, period=14).dropna()
    close = frame.loc[atr.index, "close"]
    atr_percent = atr / close * 100
    finite_positive = np.isfinite(atr_percent.to_numpy()) & (
        atr_percent.to_numpy() > 0
    )
    atr_percent = atr_percent.iloc[np.flatnonzero(finite_positive)]
    if len(atr_percent) < 17:
        raise CalibrationError(f"atr_samples_below_17:{len(atr_percent)}")

    snapshot_bytes = canonical_snapshot_bytes(normalized)
    p25 = float(np.percentile(atr_percent.to_numpy(), 25))
    p50 = float(np.percentile(atr_percent.to_numpy(), 50))
    return CalibrationResult(
        cutoff_ms=cutoff_ms,
        source_rows=len(source),
        valid_rows=len(normalized),
        atr_sample_count=len(atr_percent),
        exclusions=exclusions,
        gaps=gaps,
        p25_percent=p25,
        p50_percent=p50,
        atr_abs_min=p25 / 100.0,
        snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
        snapshot_rows=normalized,
    )


def prepare_runtime_atr_history(
    rows: Iterable[Dict[str, Any]], *, scheduled_at: datetime
) -> Tuple[List[float], List[float]]:
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    cutoff_ms = int(scheduled_at.astimezone(timezone.utc).timestamp() * 1000)
    _source, normalized, _exclusions, _gaps = _normalize_rows(rows, cutoff_ms)
    if len(normalized) < 15:
        raise CalibrationError(f"runtime_rows_below_15:{len(normalized)}")

    frame = pd.DataFrame(normalized)
    atr = TechnicalIndicators.calculate_atr(frame, period=14)
    history: List[float] = []
    closes: List[float] = []
    for index in range(len(frame) - 1):
        value = float(atr.iloc[index])
        if math.isfinite(value) and value > 0:
            history.append(value)
            closes.append(float(frame.iloc[index]["close"]))
    return history, closes


def verify_manifest(manifest: Dict[str, Any], snapshot_bytes: bytes) -> None:
    if manifest.get("manifest_sha256") != _manifest_hash(manifest):
        raise CalibrationError("manifest_sha256 mismatch")
    actual_snapshot_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
    if manifest.get("snapshot_sha256") != actual_snapshot_sha256:
        raise CalibrationError("snapshot_sha256 mismatch")
