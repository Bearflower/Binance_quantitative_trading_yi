#!/usr/bin/env python3
"""Package an exact reviewed successor tree and two immutable CI image digests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.strategy_successor_manifest import BASE, IMAGES, MANIFEST, MARKER, RECOVERY_MARKER, SHA, ReleaseBlocked, validate_tree
from scripts.select_deploy_mode import _read_changes

SCRIPTS = ["scripts/scoped_strategy_successor.py", "scripts/strategy_successor_manifest.py"]


def package(sha: str, digest_dir: Path, output: Path) -> None:
    if not SHA.fullmatch(sha):
        raise ReleaseBlocked("full successor SHA required")
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args])
    if git("rev-parse", "HEAD").decode().strip() != sha or git("status", "--porcelain").strip():
        raise ReleaseBlocked("clean exact committed checkout required")
    manifest = json.loads(git("show", sha + ":" + MANIFEST))
    validate_tree(manifest, _read_changes(BASE, sha), lambda path: git("show", sha + ":" + path))
    if {p.name for p in digest_dir.iterdir()} != {name + ".txt" for name in IMAGES}:
        raise ReleaseBlocked("exactly two digest artifacts required")
    digests = {}
    for name in IMAGES:
        value = (digest_dir / (name + ".txt")).read_text().strip()
        if not re.fullmatch(r"ghcr\.io/bearflower/trading-" + re.escape(name) + r"@sha256:[0-9a-f]{64}", value):
            raise ReleaseBlocked("invalid immutable image digest: " + name)
        digests[name] = value
    output.mkdir(parents=True, exist_ok=False)
    for path in [MANIFEST, MARKER, RECOVERY_MARKER, *SCRIPTS]:
        dest = output / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(git("show", sha + ":" + path))
    (output / "image-digests.json").write_text(json.dumps(digests, sort_keys=True))
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
        print("Successor packaging blocked: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
