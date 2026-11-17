"""Inspect historical funding settlements without attesting their schedule.

The Binance funding-rate history supplies observed settlements, not an
independent historical timetable. A regular series is useful raw evidence but
must remain BLOCKED_DATA until schedule provenance is independently established.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from .raw_export import _destination_is_private
from .snapshot_sealer import _has_symlink_ancestor


_CADENCE_HOURS = (1, 2, 4, 8)
_JITTER_MS = 1000
_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}$")
_MAX_ROWS_PER_SYMBOL = 3000
_MAX_ROW_BYTES = 8192
_MAX_TOTAL_BYTES = 64 * 1024 * 1024


def _utc_millis(value: datetime) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_aware_datetime_required")
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _iso_millis(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def assess_funding_rows(
    symbol: str,
    rows: Iterable[Mapping[str, Any]],
    window_start: datetime,
    as_of: datetime,
) -> Dict[str, Any]:
    """Check observable cadence and boundaries; never declare source complete."""
    start_ms, end_ms = _utc_millis(window_start), _utc_millis(as_of)
    if start_ms >= end_ms:
        raise ValueError("invalid_window")
    gaps = []
    parsed = []
    for row in rows:
        if row.get("symbol") != symbol:
            gaps.append("funding_symbol_mismatch:{0}".format(symbol))
            continue
        raw_time = row.get("fundingTime")
        if isinstance(raw_time, bool) or not isinstance(raw_time, int):
            gaps.append("funding_time_invalid:{0}".format(symbol))
            continue
        try:
            rate = Decimal(str(row.get("fundingRate")))
        except (InvalidOperation, ValueError, TypeError):
            gaps.append("funding_rate_invalid:{0}".format(symbol))
            continue
        if not rate.is_finite():
            gaps.append("funding_rate_invalid:{0}".format(symbol))
            continue
        if raw_time < start_ms or raw_time > end_ms:
            gaps.append("funding_time_outside_window:{0}".format(symbol))
            continue
        parsed.append((raw_time, rate))
    parsed.sort(key=lambda item: item[0])
    times = [item[0] for item in parsed]
    if len(times) != len(set(times)):
        gaps.append("funding_duplicate_time:{0}".format(symbol))
    cadence = None
    if len(times) < 3:
        gaps.append("funding_sample_insufficient:{0}".format(symbol))
    elif len(times) == len(set(times)):
        differences = [right - left for left, right in zip(times, times[1:])]
        matching = [
            hours for hours in _CADENCE_HOURS
            if all(abs(delta - hours * 3_600_000) <= _JITTER_MS for delta in differences)
        ]
        if len(matching) != 1:
            gaps.append("funding_cadence_inconsistent:{0}".format(symbol))
        else:
            cadence = matching[0]
            max_edge_ms = cadence * 3_600_000 + _JITTER_MS
            if times[0] - start_ms > max_edge_ms:
                gaps.append("funding_start_boundary_gap:{0}".format(symbol))
            if end_ms - times[-1] > max_edge_ms:
                gaps.append("funding_end_boundary_gap:{0}".format(symbol))
    gaps.append("historical_funding_schedule_unattested")
    return {
        "decision": "BLOCKED_DATA",
        "source_provenance": "unverified",
        "symbol": symbol,
        "cadence_hours": cadence,
        "events": [
            {"symbol": symbol, "funding_time": _iso_millis(ts), "funding_rate": str(rate)}
            for ts, rate in parsed
        ] if len(gaps) == 1 else [],
        "gaps": list(dict.fromkeys(gaps)),
    }


def freeze_funding_archive(
    destination: Any,
    source_rows: Mapping[str, Iterable[Mapping[str, Any]]],
    window_start: datetime,
    as_of: datetime,
    *,
    fetched_at: datetime,
) -> Dict[str, Any]:
    """Atomically preserve public API responses in a private, immutable folder.

    Hashes prove local byte integrity, not that the API response was complete.
    Every result remains BLOCKED_DATA until independent historical schedule
    evidence exists.
    """
    output = Path(destination)
    if output.exists() or output.is_symlink():
        return {"decision": "BLOCKED_DATA", "gaps": ["funding_archive_already_exists"]}
    if _has_symlink_ancestor(output) or not _destination_is_private(output):
        return {"decision": "BLOCKED_DATA", "gaps": ["funding_output_not_private"]}
    start_ms, end_ms, fetched_ms = (
        _utc_millis(window_start), _utc_millis(as_of), _utc_millis(fetched_at)
    )
    if start_ms >= end_ms or end_ms - start_ms > 90 * 86_400_000:
        return {"decision": "BLOCKED_DATA", "gaps": ["funding_window_invalid"]}
    if fetched_ms < end_ms:
        return {"decision": "BLOCKED_DATA", "gaps": ["funding_fetch_before_as_of"]}
    if not source_rows or len(source_rows) > 64 or any(
        not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
        for symbol in source_rows
    ):
        return {"decision": "BLOCKED_DATA", "gaps": ["funding_symbols_invalid"]}
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".funding-", dir=str(output.parent)))
    try:
        files = {}
        assessments = {}
        gaps = []
        total_bytes = 0
        for symbol in sorted(source_rows):
            rows_for_assessment = []
            filename = symbol + ".jsonl"
            digest = hashlib.sha256()
            row_count = 0
            with (stage / filename).open("wb") as handle:
                for row in source_rows[symbol]:
                    if row_count >= _MAX_ROWS_PER_SYMBOL:
                        return {"decision": "BLOCKED_DATA", "gaps": [
                            "funding_row_limit_exceeded:{0}".format(symbol)
                        ]}
                    if not isinstance(row, Mapping):
                        return {"decision": "BLOCKED_DATA", "gaps": [
                            "funding_row_invalid:{0}".format(symbol)
                        ]}
                    raw = dict(row)
                    try:
                        data = (
                            json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
                        ).encode("utf-8")
                    except (TypeError, ValueError):
                        return {"decision": "BLOCKED_DATA", "gaps": [
                            "funding_row_invalid:{0}".format(symbol)
                        ]}
                    if len(data) > _MAX_ROW_BYTES:
                        return {"decision": "BLOCKED_DATA", "gaps": [
                            "funding_row_bytes_exceeded:{0}".format(symbol)
                        ]}
                    total_bytes += len(data)
                    if total_bytes > _MAX_TOTAL_BYTES:
                        return {"decision": "BLOCKED_DATA", "gaps": [
                            "funding_total_bytes_exceeded"
                        ]}
                    handle.write(data)
                    digest.update(data)
                    rows_for_assessment.append({
                        "symbol": raw.get("symbol"),
                        "fundingTime": raw.get("fundingTime"),
                        "fundingRate": raw.get("fundingRate"),
                    })
                    row_count += 1
            assessment = assess_funding_rows(
                symbol, rows_for_assessment, window_start, as_of
            )
            assessments[symbol] = {
                "cadence_hours": assessment["cadence_hours"],
                "gaps": assessment["gaps"],
            }
            gaps.extend(assessment["gaps"])
            files[symbol] = {
                "file": filename,
                "rows": row_count,
                "sha256": digest.hexdigest(),
            }
        manifest = {
            "decision": "BLOCKED_DATA",
            "source_provenance": "unverified",
            "window_start": _iso_millis(start_ms),
            "as_of": _iso_millis(end_ms),
            "fetched_at": _iso_millis(fetched_ms),
            "files": files,
            "assessments": assessments,
            "gaps": sorted(set(gaps)),
        }
        (stage / "funding_manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if output.exists() or output.is_symlink():
            return {"decision": "BLOCKED_DATA", "gaps": ["funding_archive_already_exists"]}
        stage.rename(output)
        return {
            "decision": "BLOCKED_DATA",
            "source_provenance": "unverified",
            "output_dir": str(output),
            "gaps": manifest["gaps"],
        }
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def attested_funding_schedule(evidence, symbols, window_start, as_of):
    """Validate prospective five-minute forecasts, never infer past cadence.

    Evidence is exported from the existing funding-schedule collector and must
    be included in the replay manifest. Hashes establish retained-field
    integrity, not exchange signatures or retrospective completeness.
    """
    from .models import DataBlocked, parse_utc
    from ai_tuner.funding_schedule.models import FundingScheduleObservation
    if not isinstance(evidence, Mapping) or evidence.get('source') != 'prospective_exchange_forecast' or evidence.get('complete') is not True:
        raise DataBlocked(['historical_funding_schedule_unattested'])
    rows = evidence.get('observations')
    if not isinstance(rows, (tuple, list)):
        raise DataBlocked(['funding_schedule_observations_missing'])
    by_symbol = {symbol: [] for symbol in symbols}
    try:
        for raw in rows:
            symbol = raw['symbol']
            if symbol not in by_symbol:
                continue
            observation = FundingScheduleObservation(symbol,parse_utc(raw['source_time']),
                parse_utc(raw['received_at']),parse_utc(raw['next_funding_time']),
                raw.get('funding_interval_hours'),raw['payload_hash'])
            selected = observation.raw_payload
            digest = hashlib.sha256(json.dumps(selected,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if digest != observation.payload_hash:
                raise ValueError('payload_hash_mismatch')
            if not (observation.source_time <= observation.received_at < observation.next_funding_time):
                raise ValueError('forecast_not_prospective')
            if (observation.received_at-observation.source_time).total_seconds() > 120:
                raise ValueError('forecast_source_stale')
            interval=observation.funding_interval_hours
            if interval is not None and (type(interval) is not int or interval <= 0):
                raise ValueError('interval_invalid')
            if observation.received_at > as_of:
                raise ValueError('future_observation')
            by_symbol[symbol].append(observation)
    except (KeyError,TypeError,ValueError,OverflowError) as exc:
        raise DataBlocked(['funding_schedule_evidence_invalid:'+str(exc)]) from exc
    schedule = {}
    for symbol, observations in by_symbol.items():
        observations.sort(key=lambda item:item.received_at)
        if not observations or observations[0].received_at > window_start:
            raise DataBlocked(['funding_schedule_start_missing:'+symbol])
        relevant = [item for item in observations if item.received_at >= window_start]
        seed = max((item for item in observations if item.received_at <= window_start),key=lambda item:item.received_at)
        relevant = [seed] + [item for item in relevant if item is not seed]
        # Collector runs every five minutes; retain at most 120s transport jitter.
        bounds = [item.received_at for item in relevant]+[as_of]
        if window_start-seed.received_at > timedelta(seconds=420) or any(
            (right-left).total_seconds()>420 for left,right in zip(bounds,bounds[1:])):
            raise DataBlocked(['funding_schedule_coverage_gap:'+symbol])
        schedule[symbol] = tuple(sorted({item.next_funding_time for item in relevant
                                         if window_start < item.next_funding_time <= as_of}))
    return schedule
