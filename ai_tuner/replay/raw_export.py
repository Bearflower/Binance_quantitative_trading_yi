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
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .snapshot_sealer import _has_symlink_ancestor, _repo_destination_is_ignored


SQL_VERSION = "raw-export-v1"
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
)


def _blocked(*gaps: str) -> Dict[str, Any]:
    return {"decision": "BLOCKED_DATA", "source_provenance": "unverified", "gaps": list(gaps)}


def _utc(value: datetime) -> Optional[datetime]:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize(value: Any, zone: ZoneInfo) -> Any:
    if isinstance(value, datetime):
        return _iso(value.replace(tzinfo=zone) if value.tzinfo is None else value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _normalize(v, zone) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize(item, zone) for item in value]
    return value


def _query_specs(kline_pairs: Iterable[Tuple[str, str]]) -> list[tuple[str, str, str, tuple[str, ...], tuple[str, ...]]]:
    specs = []
    for symbol, interval in sorted(set(kline_pairs)):
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol) or interval not in _INTERVALS:
            raise ValueError("invalid_symbol_or_interval")
        table = "kline_{0}_{1}".format(symbol.lower(), interval)
        if not _KLINE_TABLE.fullmatch(table):
            raise ValueError("invalid_kline_table")
        specs.append(("btc_eth", table, "open_time", ("open_time", "close_time", "open_price", "high_price", "low_price", "close_price", "volume", "quote_volume", "trade_count"), ("close_time",)))
    specs.extend((
        ("btc_eth_aggressive", "trade_signals", "created_at", ("id", "created_at", "status", "symbol", "direction", "entry_price"), ("created_at",)),
        ("hrs", "hrs_signals", "triggered_at", ("id", "triggered_at", "symbol", "direction", "status"), ("triggered_at",)),
        ("public", "ai_tuner_universe_scan_runs", "observed_at", ("strategy_id", "observed_at", "created_at", "source_run_id", "config_hash"), ("observed_at", "created_at")),
        ("public", "ai_tuner_universe_snapshots", "observed_at", ("strategy_id", "observed_at", "created_at", "symbol", "membership"), ("observed_at", "created_at")),
        ("public", "ai_tuner_market_snapshots", "observed_at", ("strategy_id", "observed_at", "source_observed_at", "collected_at", "symbol", "bucket_start"), ("observed_at", "source_observed_at", "collected_at")),
    ))
    return specs


def _bounds_for_dtype(dtype: str, start: datetime, end: datetime, zone: ZoneInfo) -> tuple[datetime, datetime]:
    if dtype == _AWARE:
        return start, end
    if dtype == _NAIVE:
        return start.astimezone(zone).replace(tzinfo=None), end.astimezone(zone).replace(tzinfo=None)
    raise ValueError("unsupported_time_column_type:{0}".format(dtype))


def _select_sql_and_args(name, time_col, cutoff_cols, types, start, end, zone):
    lower, upper = _bounds_for_dtype(types[time_col], start, end, zone)
    cutoff = " AND ".join("{0} <= ${1}".format(col, i + 3) for i, col in enumerate(cutoff_cols))
    # Dynamic identifiers are fixed literals or guarded by _query_specs.
    sql = "SELECT * FROM {0} AS t WHERE {1} >= $1 AND {1} <= $2 AND {2}".format(name, time_col, cutoff)
    args = [lower, upper]
    for column in cutoff_cols:
        _, bound = _bounds_for_dtype(types[column], start, end, zone)
        args.append(bound)
    if name == "btc_eth_aggressive.trade_signals":
        sql += " AND status IN (${0}, ${1})".format(len(args) + 1, len(args) + 2)
        args.extend(("pending", "executed"))
    elif name.startswith("public."):
        sql += " AND strategy_id = ${0}".format(len(args) + 1)
        args.append("hrs")
    return sql + " ORDER BY {0}, to_jsonb(t)".format(time_col), args


def _canonical_rows(fetched, time_col, cutoff_cols, start, end, zone, name):
    for record in fetched:
        for column in (time_col, *cutoff_cols):
            value = record[column]
            if not isinstance(value, datetime):
                raise ValueError("time_value_missing:{0}.{1}".format(name, column))
            instant = value.replace(tzinfo=zone).astimezone(timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
            if instant > end:
                raise ValueError("future_source_row:{0}.{1}".format(name, column))
            if column == time_col and instant < start:
                raise ValueError("before_window_source_row:{0}.{1}".format(name, column))
    rows = [_normalize(dict(row), zone) for row in fetched]
    rows.sort(key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return rows


async def _extract_one(db, spec, start, end, zone):
    schema, table, time_col, required, cutoff_cols = spec
    name = "{0}.{1}".format(schema, table)
    meta = await db.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = $2", schema, table,
    )
    types = {row["column_name"]: row["data_type"] for row in meta}
    missing = sorted(set(required) - set(types))
    if missing:
        return None, None, "columns_missing:{0}:{1}".format(name, ",".join(missing))
    for column in (time_col, *cutoff_cols):
        if types[column] not in (_NAIVE, _AWARE):
            raise ValueError("time_column_type_unknown:{0}.{1}".format(name, column))
    sql, args = _select_sql_and_args(name, time_col, cutoff_cols, types, start, end, zone)
    fetched = await db.fetch(sql, *args)
    if not fetched:
        return None, None, "dataset_empty:{0}".format(name)
    rows = _canonical_rows(fetched, time_col, cutoff_cols, start, end, zone, name)
    times = [row[time_col] for row in rows]
    evidence = {
        "row_count": len(rows), "min_time": min(times), "max_time": max(times),
        "sql_version": SQL_VERSION, "window_start": _iso(start), "as_of": _iso(end),
        "time_column": time_col, "time_column_type": types[time_col],
    }
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
    kline_pairs: Iterable[Tuple[str, str]],
    verified_naive_timezone: Optional[str] = None,
    timezone_evidence: Optional[str] = None,
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
    if not verified_naive_timezone or not timezone_evidence or not timezone_evidence.strip():
        return _blocked("naive_db_timezone_unverified")
    try:
        zone = ZoneInfo(verified_naive_timezone)
        specs = _query_specs(kline_pairs)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        return _blocked("export_configuration_invalid:{0}".format(exc))
    if not specs or not any(schema == "btc_eth" for schema, *_ in specs):
        return _blocked("kline_pairs_missing")
    output = Path(output_dir)
    if output.exists() or output.is_symlink():
        return _blocked("raw_export_already_exists")
    if _has_symlink_ancestor(output) or not _repo_destination_is_ignored(output):
        return _blocked("raw_output_not_safe_or_git_ignored")

    rows_by_source, query_evidence, gaps = {}, {}, []
    async with db.transaction(readonly=True, isolation="repeatable_read"):
        for spec in specs:
            schema, table = spec[:2]
            name = "{0}.{1}".format(schema, table)
            try:
                rows, evidence, gap = await _extract_one(db, spec, start, end, zone)
                if gap:
                    gaps.append(gap)
                else:
                    rows_by_source[name], query_evidence[name] = rows, evidence
            except Exception as exc:
                # PostgreSQL errors (including UndefinedTable/UndefinedColumn)
                # differ by driver; all are evidence gaps, never empty data.
                gaps.append("source_unavailable:{0}:{1}".format(name, exc))
                break

    if any(gap.startswith(("columns_missing:", "source_unavailable:")) for gap in gaps):
        return _blocked(*gaps, *_KNOWN_GAPS)
    gaps.extend(_KNOWN_GAPS)
    manifest = {
        "decision": "BLOCKED_DATA", "source_provenance": "unverified",
        "sql_version": SQL_VERSION, "as_of": _iso(end), "window_start": _iso(start),
        "verified_naive_timezone": verified_naive_timezone,
        "timezone_evidence": timezone_evidence,
        "queries": query_evidence, "gaps": gaps,
    }
    try:
        _write_raw(output, rows_by_source, manifest)
    except (OSError, ValueError) as exc:
        return _blocked("raw_export_write_failed:{0}".format(exc), *gaps)
    return {"decision": "BLOCKED_DATA", "source_provenance": "unverified", "gaps": gaps, "output_dir": str(output)}
