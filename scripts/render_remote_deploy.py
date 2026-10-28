#!/usr/bin/env python3
"""Validate and atomically render the remote deployment runtime."""
from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Sequence


PLACEHOLDERS = {
    "__DEPLOY_MODE__": "mode",
    "__COMMIT_SHA__": "commit_sha",
    "__DEPLOY_ID__": "deploy_id",
}
UNRESOLVED_PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")


class RenderError(ValueError):
    """The template or supplied deployment identity is unsafe."""


def _validate(mode: str, commit_sha: str, deploy_id: str) -> None:
    if mode not in {"full", "calibrated-first-create"}:
        raise RenderError("invalid deployment mode")
    if re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha) is None:
        raise RenderError("commit SHA must be exactly 40 hexadecimal characters")
    if re.fullmatch(r"[0-9A-F]{8}", deploy_id) is None:
        raise RenderError("deploy ID must be exactly 8 uppercase hexadecimal characters")


def render_remote_deploy(
    *,
    source: str | Path,
    output: str | Path,
    mode: str,
    commit_sha: str,
    deploy_id: str,
) -> None:
    _validate(mode, commit_sha, deploy_id)
    source_path = Path(source)
    output_path = Path(output)
    template = source_path.read_text(encoding="utf-8")

    values = {
        "mode": mode,
        "commit_sha": commit_sha,
        "deploy_id": deploy_id,
    }
    rendered = template
    for placeholder, field in PLACEHOLDERS.items():
        count = rendered.count(placeholder)
        if count != 1:
            raise RenderError(
                f"placeholder {placeholder} must occur exactly once, found {count}"
            )
        rendered = rendered.replace(placeholder, values[field], 1)
    unresolved = UNRESOLVED_PLACEHOLDER.search(rendered)
    if unresolved is not None:
        raise RenderError(f"unresolved placeholder: {unresolved.group(0)}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--deploy-id", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        render_remote_deploy(
            source=args.source,
            output=args.output,
            mode=args.mode,
            commit_sha=args.commit_sha,
            deploy_id=args.deploy_id,
        )
    except (OSError, RenderError) as exc:
        print(f"remote deploy render failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
