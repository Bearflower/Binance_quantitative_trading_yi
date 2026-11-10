#!/usr/bin/env python3
"""Package exactly the reviewed AI Tuner observer release and its CI digest."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.ai_tuner_trial_manifest import (  # noqa: E402
    BASE, IMAGES, MANIFEST, MARKER, SHA, ReleaseBlocked, validate_tree,
)
from scripts.select_deploy_mode import _read_changes  # noqa: E402

SCRIPTS = ["scripts/scoped_ai_tuner_trial.py", "scripts/ai_tuner_trial_manifest.py",
           "scripts/scoped_stop_followup.py", "scripts/stop_followup_manifest.py"]


def package(sha: str, digest_dir: Path, output: Path) -> None:
    if not SHA.fullmatch(sha):
        raise ReleaseBlocked("full release SHA required")

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args])

    if git("rev-parse", "HEAD").decode().strip() != sha or git("status", "--porcelain").strip():
        raise ReleaseBlocked("clean exact committed checkout required")
    manifest = json.loads(git("show", sha + ":" + MANIFEST))
    validate_tree(manifest, _read_changes(BASE, sha), lambda path: git("show", sha + ":" + path))
    if {p.name for p in digest_dir.iterdir()} != {name + ".txt" for name in IMAGES}:
        raise ReleaseBlocked("exactly one AI Tuner digest artifact required")
    digest = (digest_dir / "ai-tuner.txt").read_text().strip()
    if not re.fullmatch(r"ghcr\.io/bearflower/trading-ai-tuner@sha256:[0-9a-f]{64}", digest):
        raise ReleaseBlocked("invalid immutable AI Tuner digest")
    output.mkdir(parents=True, exist_ok=False)
    for path in [MANIFEST, MARKER, *SCRIPTS]:
        target = output / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git("show", sha + ":" + path))
    (output / "image-digests.json").write_text(json.dumps({"ai-tuner": digest}, sort_keys=True))
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
        print("AI Tuner release packaging blocked: " + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
