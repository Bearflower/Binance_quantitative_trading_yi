#!/usr/bin/env python3
"""Package only the five reviewed images and their exact source proof."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.scoped_seven_gaps_release import IMAGE_PATHS, IMAGE_SERVICES

SCRIPTS = (
    "scripts/scoped_seven_gaps_release.py",
    "scripts/scoped_stop_followup.py",
    "scripts/stop_followup_manifest.py",
)


def package(sha: str, digests: Path, output: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("full release SHA required")
    git = lambda *args: subprocess.check_output(["git", *args])
    if git("rev-parse", "HEAD").decode().strip() != sha or git("status", "--porcelain").strip():
        raise ValueError("clean exact committed checkout required")
    if {p.name for p in digests.iterdir()} != {name + ".txt" for name in IMAGE_SERVICES}:
        raise ValueError("exactly five digest artifacts required")
    images = {}
    for name in IMAGE_SERVICES:
        ref = (digests / (name + ".txt")).read_text().strip()
        if not re.fullmatch(rf"ghcr\.io/bearflower/trading-{re.escape(name)}@sha256:[0-9a-f]{{64}}", ref):
            raise ValueError("invalid immutable image digest: " + name)
        images[name] = ref
    output.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for path in sorted(set(SCRIPTS) | {p for paths in IMAGE_PATHS.values() for p in paths}):
        data = git("show", f"{sha}:{path}")
        hashes[path] = hashlib.sha256(data).hexdigest()
        if path in SCRIPTS:
            dest = output / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
    (output / "source-checksums.json").write_text(json.dumps(hashes, sort_keys=True))
    (output / "image-digests.json").write_text(json.dumps(images, sort_keys=True))
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
        print("seven gaps packaging blocked: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
