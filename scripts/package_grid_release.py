#!/usr/bin/env python3
"""Package one reviewed grid release and its immutable CI image digest."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.scoped_grid_release import IMAGE_PATHS

SCRIPTS = (
    "scripts/scoped_grid_release.py", "scripts/scoped_stop_followup.py",
    "scripts/stop_followup_manifest.py",
)


def package(sha: str, digests: Path, output: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("full release SHA required")
    if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != sha:
        raise ValueError("checkout does not match release SHA")
    if subprocess.check_output(["git", "status", "--porcelain"]).strip():
        raise ValueError("checkout must be clean")
    if {p.name for p in digests.iterdir()} != {"grid.txt"}:
        raise ValueError("one grid digest artifact required")
    image = (digests / "grid.txt").read_text().strip()
    if not re.fullmatch(r"ghcr\.io/bearflower/trading-grid@sha256:[0-9a-f]{64}", image):
        raise ValueError("invalid immutable grid image")
    output.mkdir(parents=True, exist_ok=False)
    checksums = {}
    for path in (*SCRIPTS, *IMAGE_PATHS):
        data = subprocess.check_output(["git", "show", f"{sha}:{path}"])
        if path in SCRIPTS:
            target = output / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if path in IMAGE_PATHS or path in SCRIPTS:
            checksums[path] = hashlib.sha256(data).hexdigest()
    (output / "source-checksums.json").write_text(json.dumps(checksums, sort_keys=True))
    (output / "image-digests.json").write_text(json.dumps({"grid": image}, sort_keys=True))
    (output / "release-sha").write_text(sha)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--digests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        package(args.sha, args.digests, args.output)
    except Exception as exc:
        print("grid release packaging blocked: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
