#!/usr/bin/env python3
"""Seal already-frozen local replay files; this CLI does not query production.

Despite its historical export name, the current slice only creates an
integrity manifest with ``source_provenance=unverified``. It cannot attest that
the input came from live trading or serve as a real golden/parity verdict.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_tuner.replay.snapshot_sealer import seal_snapshot  # noqa: E402


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("invalid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include timezone")
    return parsed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="existing local frozen data directory")
    parser.add_argument("--output-root", type=Path, default=Path("var/replay"))
    parser.add_argument("--strategy", required=True, choices=("btc_eth_aggressive", "hrs"))
    parser.add_argument("--as-of", type=_timestamp, required=True)
    parser.add_argument("--window-start", type=_timestamp, required=True)
    parser.add_argument("--window-end", type=_timestamp, required=True)
    args = parser.parse_args(argv)

    result = seal_snapshot(
        args.source, args.output_root, args.strategy,
        args.as_of, args.window_start, args.window_end,
    )
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0 if result.get("status") == "SEALED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
