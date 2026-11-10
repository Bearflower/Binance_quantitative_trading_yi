#!/usr/bin/env python3
"""Package the exact accounting release sources and two immutable images."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


SOURCES = (
    "scripts/scoped_income_attribution_release.py",
    "scripts/run_income_attribution.sh",
    "deployment/systemd/income-attribution.service",
    "deployment/systemd/income-attribution.timer",
)
IMAGES = ("hrs", "dashboard-api")


def package(sha: str, digests: Path, output: Path) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("exact commit SHA required")
    if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != sha:
        raise ValueError("checkout is not the requested commit")
    if subprocess.check_output(["git", "status", "--porcelain"]).strip():
        raise ValueError("checkout must be clean")
    if {p.name for p in digests.iterdir()} != {f"{name}.txt" for name in IMAGES}:
        raise ValueError("exactly two image digest artifacts required")
    images = {}
    for name in IMAGES:
        ref = (digests / f"{name}.txt").read_text().strip()
        if not re.fullmatch(rf"ghcr\.io/bearflower/trading-{name}@sha256:[0-9a-f]{{64}}", ref):
            raise ValueError(f"invalid image digest: {name}")
        images[name] = ref
    output.mkdir(parents=True, exist_ok=False)
    for path in SOURCES:
        data = subprocess.check_output(["git", "show", f"{sha}:{path}"])
        destination = output / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    (output / "image-digests.json").write_text(json.dumps(images, sort_keys=True))
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
