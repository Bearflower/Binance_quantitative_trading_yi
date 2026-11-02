"""Read-only, consistent PostgreSQL extraction of *raw* replay evidence.

The result is deliberately not a DataPortal snapshot or a live golden. It
cannot establish trade/config attribution or qualify any candidate. Database
access is injectable so the SQL and transaction contract can be tested locally.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .snapshot_sealer import _has_symlink_ancestor


SQL_VERSION = "raw-export-v2"
MAX_WINDOW_DAYS = 90
MAX_KLINE_PAIRS = 64
MAX_ROWS_PER_SOURCE = 50_000
MAX_TOTAL_ESTIMATED_BYTES = 64 * 1024 * 1024
_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}$")
_INTERVALS = frozenset(("1m", "5m", "15m", "1h", "4h", "1d"))
_KLINE_TABLE = re.compile(r"^kline_[a-z0-9]{2,20}_(1m|5m|15m|1h|4h|1d)$")
_NAIVE = "timestamp without time zone"
_AWARE = "timestamp with time zone"
_KNOWN_GAPS = (
    "real_funding_settlements_missing",
    "real_exchange_info_missing",
    "historical_config_identity_missing",
    "strategy_equity_curve_missing",
    "actual_slippage_unattributed",
    "historical_signal_status_unavailable",
    "kline_historical_revision_unavailable",
)
_HRS_GAPS = (
    "market_snapshot_historical_version_unavailable",
    "hrs_signal_attribution_unavailable",
)


def _blocked(*gaps: str) -> Dict[str, Any]:
    return {"decision": "BLOCKED_DATA", "source_provenance": "unverified", "gaps": list(gaps)}


def _utc(value: datetime) -> Optional[datetime]:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _destination_is_private(output: Path) -> bool:
    """The *directory* must be ignored, not just a manifest filename."""
    existing = output
    while not existing.exists():
        if existing == existing.parent:
            return False
        existing = existing.parent
    try:
        lookup = subprocess.run(
            ["git", "-C", str(existing), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return False
    if lookup.returncode != 0:
        return "not a git repository" in lookup.stderr.lower()
    repo = Path(lookup.stdout.strip())
    targets = (str(output) + "/", str(output / "raw_manifest.json"), str(output / "__raw_probe__.jsonl"))
    try:
        return all(subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", target],
            cwd=repo, capture_output=True, check=False,
        ).returncode == 0 for target in targets)
    except OSError:
        return False


def _normalize(value: Any, zone: Optional[ZoneInfo]) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None and zone is None:
            raise ValueError("naive_datetime_without_column_timezone")
        return _iso(value.replace(tzinfo=zone) if value.tzinfo is None else value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _normalize(v, zone) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize(item, zone) for item in value]
    return value


def _query_specs(strategy_id: str, kline_pairs: Iterable[Tuple[str, str]]) -> list[tuple[str, str, str, tuple[str, ...], tuple[str, ...]]]:
    if strategy_id not in ("btc_eth_aggressive", "hrs"):
        raise ValueError("strategy_unsupported")
    specs = []
    for symbol, interval in sorted(set(kline_pairs)):
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol) or interval not in _INTERVALS:
            raise ValueError("invalid_symbol_or_interval")
        table = "kline_{0}_{1}".format(symbol.lower(), interval)
        if not _KLINE_TABLE.fullmatch(table):
            raise ValueError("invalid_kline_table")
        specs.append(("btc_eth", table, "open_time", ("id", "open_time", "close_time", "open_price", "high_price", "low_price", "close_price", "volume", "quote_volume", "trade_count"), ("close_time",), ("id",)))
    if strategy_id == "btc_eth_aggressive":
        specs.append(("btc_eth_aggressive", "trade_signals", "created_at", ("id", "symbol", "direction", "created_at"), ("created_at",), ("id",)))
    else:
        specs.extend((
            ("hrs", "hrs_signals", "triggered_at", ("id", "symbol", "direction", "triggered_at"), ("triggered_at",), ("id",)),
            ("public", "ai_tuner_universe_scan_runs", "observed_at", ("strategy_id", "observed_at", "created_at", "source_run_id", "config_hash", "symbol_count"), ("observed_at", "created_at"), ("source_run_id",)),
            ("public", "ai_tuner_universe_snapshots", "observed_at", ("strategy_id", "observed_at", "created_at", "symbol", "membership", "source_run_id", "config_hash"), ("observed_at", "created_at"), ("symbol",)),
            ("public", "ai_tuner_market_snapshots", "observed_at", ("strategy_id", "symbol", "observed_at", "bucket_start", "source_observed_at", "collected_at", "open_interest_usd", "funding_rate", "market_cap_usd", "oi_market_cap_ratio", "sentiment_score", "oi_source", "funding_source", "market_cap_source", "payload_hash"), ("observed_at", "source_observed_at", "collected_at"), ("symbol", "bucket_start")),
        ))
    return specs


def _bounds_for_dtype(dtype: str, start: datetime, end: datetime, zone: Optional[ZoneInfo]) -> tuple[datetime, datetime]:
    if dtype == _AWARE:
        return start, end
    if dtype == _NAIVE and zone is not None:
        return start.astimezone(zone).replace(tzinfo=None), end.astimezone(zone).replace(tzinfo=None)
    raise ValueError("unsupported_time_column_type:{0}".format(dtype))


def _select_sql_and_args(name, time_col, columns, cutoff_cols, sort_cols, types, zones, start, end):
    lower, upper = _bounds_for_dtype(types[time_col], start, end, zones[time_col])
    cutoff = " AND ".join("{0} <= ${1}".format(col, i + 3) for i, col in enumerate(cutoff_cols))
    # Dynamic identifiers are fixed literals or guarded by _query_specs.
    sql = "SELECT {0} FROM {1} AS t WHERE {2} >= $1 AND {2} <= $2 AND {3}".format(
        ", ".join(columns), name, time_col, cutoff,
    )
    args = [lower, upper]
    for column in cutoff_cols:
        _, bound = _bounds_for_dtype(types[column], start, end, zones[column])
        args.append(bound)
    if name.startswith("public."):
        sql += " AND strategy_id = ${0}".format(len(args) + 1)
        args.append("hrs")
    if name == "public.ai_tuner_market_snapshots":
        sql += " AND date_trunc('hour', bucket_start AT TIME ZONE 'UTC') = bucket_start AT TIME ZONE 'UTC'"
        sql += " AND source_observed_at <= bucket_start + INTERVAL '1 hour'"
        sql += " AND bucket_start + INTERVAL '1 hour' <= ${0}".format(len(args) + 1)
        args.append(end)
    canonical_row = "to_jsonb(ROW({0}))::text".format(", ".join("t." + column for column in columns))
    sql += " ORDER BY {0}, {1}".format(", ".join((time_col, *sort_cols)), canonical_row)
    sql += " LIMIT ${0}".format(len(args) + 1)
    args.append(MAX_ROWS_PER_SOURCE + 1)
    return sql, args


def _zone_for(name: str, column: str, dtype: str, bindings: Mapping[str, Mapping[str, str]]) -> Optional[ZoneInfo]:
    if dtype == _AWARE:
        return None
    if dtype != _NAIVE:
        raise ValueError("time_column_type_unknown:{0}.{1}".format(name, column))
    binding = bindings.get("{0}.{1}".format(name, column))
    if not binding or not binding.get("zone") or not binding.get("evidence"):
        raise ValueError("naive_column_timezone_unverified:{0}.{1}".format(name, column))
    try:
        return ZoneInfo(binding["zone"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError("naive_column_timezone_invalid:{0}.{1}".format(name, column)) from exc


def _as_utc(value: datetime, zone: Optional[ZoneInfo]) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("time_value_missing")
    if value.tzinfo is None:
        if zone is None:
            raise ValueError("naive_datetime_without_column_timezone")
        return value.replace(tzinfo=zone).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_rows(fetched, time_col, cutoff_cols, sort_cols, start, end, zones, name):
    for record in fetched:
        for column in (time_col, *cutoff_cols):
            value = record[column]
            instant = _as_utc(value, zones[column])
            if instant > end:
                raise ValueError("future_source_row:{0}.{1}".format(name, column))
            if column == time_col and instant < start:
                raise ValueError("before_window_source_row:{0}.{1}".format(name, column))
        if name == "public.ai_tuner_market_snapshots":
            bucket_utc = _as_utc(record["bucket_start"], zones["bucket_start"])
            if bucket_utc.minute or bucket_utc.second or bucket_utc.microsecond:
                raise ValueError("market_bucket_not_hour_aligned")
            if bucket_utc + timedelta(hours=1) > end:
                raise ValueError("market_bucket_not_closed")
            if _as_utc(record["source_observed_at"], zones["source_observed_at"]) > bucket_utc + timedelta(hours=1):
                raise ValueError("market_source_after_bucket_end")
    rows = [{key: _normalize(value, zones.get(key)) for key, value in dict(row).items()} for row in fetched]
    rows.sort(key=lambda row: (row[time_col], *(row[col] for col in sort_cols),
                               json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)))
    return rows


async def _extract_one(db, spec, start, end, bindings):
    schema, table, time_col, columns, cutoff_cols, sort_cols = spec
    name = "{0}.{1}".format(schema, table)
    meta = await db.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = $2", schema, table,
    )
    types = {row["column_name"]: row["data_type"] for row in meta}
    missing = sorted(set(columns) - set(types))
    if missing:
        return None, None, "columns_missing:{0}:{1}".format(name, ",".join(missing))
    time_columns = (time_col, *cutoff_cols)
    if name == "public.ai_tuner_market_snapshots":
        time_columns += ("bucket_start",)
    zones = {column: _zone_for(name, column, types[column], bindings) for column in time_columns}
    sql, args = _select_sql_and_args(name, time_col, columns, cutoff_cols, sort_cols, types, zones, start, end)
    fetched = await db.fetch(sql, *args)
    if len(fetched) > MAX_ROWS_PER_SOURCE:
        raise ValueError("row_limit_exceeded:{0}:{1}".format(name, MAX_ROWS_PER_SOURCE))
    if not fetched:
        return None, None, "dataset_empty:{0}".format(name)
    # Fake DBs and drivers may return extra fields; only approved columns leave this boundary.
    projected = [{column: row[column] for column in columns} for row in fetched]
    rows = _canonical_rows(projected, time_col, cutoff_cols, sort_cols, start, end, zones, name)
    times = [row[time_col] for row in rows]
    evidence = {
        "row_count": len(rows), "min_time": min(times), "max_time": max(times),
        "sql_version": SQL_VERSION, "window_start": _iso(start), "as_of": _iso(end),
        "time_column": time_col, "time_column_type": types[time_col],
        "sort_key": ",".join((time_col, *sort_cols, "canonical_row")),
        "selected_columns": list(columns),
        "naive_timezones": {
            "{0}.{1}".format(name, column): dict(bindings["{0}.{1}".format(name, column)])
            for column in sorted(set(time_columns)) if types[column] == _NAIVE
        },
    }
    if name == "public.ai_tuner_market_snapshots":
        evidence["closed_bucket_rule"] = "UTC hour-aligned bucket_start; source_observed_at <= bucket_start + 1 hour <= as_of"
    return rows, evidence, None


def _write_raw(output: Path, rows_by_source, manifest):
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".raw-export-", dir=str(output.parent)))
    try:
        for name, rows in sorted(rows_by_source.items()):
            data = "".join(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" for row in rows)
            filename = name.replace(".", "__") + ".jsonl"
            (stage / filename).write_text(data, encoding="utf-8")
            manifest["queries"][name]["sha256"] = hashlib.sha256(data.encode("utf-8")).hexdigest()
            manifest["queries"][name]["file"] = filename
        (stage / "raw_manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if output.exists() or output.is_symlink():
            raise FileExistsError("raw_export_already_exists")
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


async def export_raw_snapshot(
    db: Any,
    output_dir: Any,
    as_of: datetime,
    window_start: datetime,
    *,
    strategy_id: str,
    kline_pairs: Iterable[Tuple[str, str]],
    timezone_bindings: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> Dict[str, Any]:
    """Query raw rows in one read-only repeatable-read transaction.

    ``db`` is an asyncpg-like acquired connection (``transaction`` and
    ``fetch``). The caller must use a SELECT-only DB role as a second barrier.
    Missing/ambiguous evidence is BLOCKED_DATA; no apparently complete file is
    written for a missing table, missing column or confirmed empty dataset.
    """
    end, start = _utc(as_of), _utc(window_start)
    if end is None or start is None or start >= end:
        return _blocked("invalid_utc_window")
    if end - start > timedelta(days=MAX_WINDOW_DAYS):
        return _blocked("window_limit_exceeded:{0}d".format(MAX_WINDOW_DAYS))
    bindings = timezone_bindings or {}
    try:
        pairs = list(kline_pairs)
        if len(pairs) > MAX_KLINE_PAIRS:
            return _blocked("kline_pair_limit_exceeded:{0}".format(MAX_KLINE_PAIRS))
        specs = _query_specs(strategy_id, pairs)
    except (ValueError, TypeError) as exc:
        return _blocked("export_configuration_invalid:{0}".format(exc))
    if not specs or not any(schema == "btc_eth" for schema, *_ in specs):
        return _blocked("kline_pairs_missing")
    output = Path(output_dir)
    if output.exists() or output.is_symlink():
        return _blocked("raw_export_already_exists")
    if _has_symlink_ancestor(output) or not _destination_is_private(output):
        return _blocked("raw_output_not_safe_or_git_ignored")

    rows_by_source, query_evidence, gaps = {}, {}, []
    estimated_bytes = 0
    async with db.transaction(readonly=True, isolation="repeatable_read"):
        for spec in specs:
            schema, table = spec[:2]
            name = "{0}.{1}".format(schema, table)
            try:
                rows, evidence, gap = await _extract_one(db, spec, start, end, bindings)
                if gap:
                    gaps.append(gap)
                else:
                    estimated_bytes += sum(
                        len(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) + 1
                        for row in rows
                    )
                    if estimated_bytes > MAX_TOTAL_ESTIMATED_BYTES:
                        gaps.append("total_estimated_bytes_exceeded:{0}".format(MAX_TOTAL_ESTIMATED_BYTES))
                        break
                    rows_by_source[name], query_evidence[name] = rows, evidence
            except ValueError as exc:
                gaps.append(str(exc))
                break
            except Exception as exc:
                # PostgreSQL errors (including UndefinedTable/UndefinedColumn)
                # differ by driver; all are evidence gaps, never empty data.
                gaps.append("source_unavailable:{0}:{1}".format(name, exc))
                break

    structural_gaps = (*_KNOWN_GAPS, *(_HRS_GAPS if strategy_id == "hrs" else ()))
    if any(not gap.startswith("dataset_empty:") for gap in gaps):
        return _blocked(*gaps, *structural_gaps)
    gaps.extend(structural_gaps)
    manifest = {
        "decision": "BLOCKED_DATA", "source_provenance": "unverified",
        "sql_version": SQL_VERSION, "strategy_id": strategy_id,
        "as_of": _iso(end), "window_start": _iso(start),
        "limits": {"max_window_days": MAX_WINDOW_DAYS, "max_kline_pairs": MAX_KLINE_PAIRS,
                   "max_rows_per_source": MAX_ROWS_PER_SOURCE,
                   "max_total_estimated_bytes": MAX_TOTAL_ESTIMATED_BYTES},
        "naive_timezone_bindings": {
            key: value for evidence in query_evidence.values()
            for key, value in evidence["naive_timezones"].items()
        },
        "queries": query_evidence, "gaps": gaps,
    }
    try:
        _write_raw(output, rows_by_source, manifest)
    except (OSError, ValueError) as exc:
        return _blocked("raw_export_write_failed:{0}".format(exc), *gaps)
    return {"decision": "BLOCKED_DATA", "source_provenance": "unverified", "gaps": gaps, "output_dir": str(output)}
