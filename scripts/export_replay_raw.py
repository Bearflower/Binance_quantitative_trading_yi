#!/usr/bin/env python3
"""Locally export raw, incomplete AI Tuner replay evidence from a read-only DB role.

This never runs replay or attests a live golden. It is separate from the
existing ``export_replay_snapshot.py`` local integrity sealer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_tuner.replay.raw_export import export_raw_snapshot  # noqa: E402


def _aware_timestamp(raw: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ISO-8601 timestamp required") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise argparse.ArgumentTypeError("timezone-aware timestamp required")
    return value.astimezone(timezone.utc)


def _pair(raw: str) -> tuple[str, str]:
    try:
        symbol, interval = raw.split(":", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected SYMBOL:interval") from exc
    return symbol, interval


def _binding(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("expected schema.table.column=value")
    key, value = raw.split("=", 1)
    if not key or not value:
        raise argparse.ArgumentTypeError("nonempty schema.table.column=value required")
    return key, value


async def _run(args: argparse.Namespace) -> dict:
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        return {"decision": "BLOCKED_DATA", "gaps": ["readonly_dsn_env_missing"], "source_provenance": "unverified"}
    import asyncpg  # local-only optional dependency; no server-side replay

    conn = await asyncpg.connect(dsn=dsn)
    try:
        repo = Path(__file__).resolve().parents[1]
        output = repo / "var" / "replay" / "raw" / args.strategy / args.as_of.strftime("%Y%m%dT%H%M%SZ")
        zones = dict(args.time_column_zone)
        evidence = dict(args.time_column_evidence)
        bindings = {key: {"zone": zone, "evidence": evidence.get(key, "")} for key, zone in zones.items()}
        return await export_raw_snapshot(
            conn, output, args.as_of, args.window_start,
            strategy_id=args.strategy, kline_pairs=args.kline,
            timezone_bindings=bindings,
        )
    finally:
        await conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=_aware_timestamp, required=True)
    parser.add_argument("--window-start", type=_aware_timestamp, required=True)
    parser.add_argument("--kline", type=_pair, action="append", required=True, help="SYMBOL:interval")
    parser.add_argument("--strategy", choices=("btc_eth_aggressive", "hrs"), required=True)
    parser.add_argument("--time-column-zone", type=_binding, action="append", default=[],
                        help="schema.table.column=IANA zone; repeat for each naive time column")
    parser.add_argument("--time-column-evidence", type=_binding, action="append", default=[],
                        help="schema.table.column=verification reference; repeat per naive column")
    parser.add_argument("--dsn-env", default="REPLAY_READONLY_DSN", help="name of env var holding read-only role DSN")
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        result = {"decision": "BLOCKED_DATA", "gaps": ["export_failed:{0}".format(type(exc).__name__)], "source_provenance": "unverified"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
