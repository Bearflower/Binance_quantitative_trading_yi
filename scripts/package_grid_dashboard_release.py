#!/usr/bin/env python3
"""Package the grid subaccount dashboard images with exact reviewed source checksums."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.scoped_grid_dashboard_release import SOURCE_PATHS, SCRIPT_PATHS  # noqa: E402


def package(sha: str, digests: Path, output: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("exact commit SHA required")
    git = lambda *args: subprocess.check_output(["git", *args])
    if git("rev-parse", "HEAD").decode().strip() != sha or git("status", "--porcelain").strip():
        raise ValueError("clean exact committed checkout required")
    if {path.name for path in digests.iterdir()} != {"dashboard-api.txt", "grid-dashboard-exporter.txt"}:
        raise ValueError("exactly two grid dashboard image digests required")
    images = {name: (digests / (name + ".txt")).read_text().strip()
              for name in ("dashboard-api", "grid-dashboard-exporter")}
    for name, image in images.items():
        if not re.fullmatch(r"ghcr\.io/bearflower/trading-" + name + r"@sha256:[0-9a-f]{64}", image):
            raise ValueError("invalid immutable image: " + name)
    output.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for path in SOURCE_PATHS + SCRIPT_PATHS:
        data = git("show", f"{sha}:{path}")
        hashes[path] = hashlib.sha256(data).hexdigest()
        if path in SCRIPT_PATHS:
            destination = output / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
    (output / "source-checksums.json").write_text(json.dumps(hashes, sort_keys=True))
    (output / "image-digests.json").write_text(json.dumps(images))
    (output / "release-sha").write_text(sha)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--digests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package(args.sha, args.digests, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
