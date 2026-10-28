"""Seal existing local replay files without claiming they came from production.

This module performs no network or database IO. It copies bytes into a new,
git-ignored snapshot directory and writes the DataPortal manifest last.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Optional

from .data_portal import compute_manifest_envelope_sha256, compute_manifest_sha256, sha256_file


_STRATEGIES = frozenset(("btc_eth_aggressive", "hrs"))
_REQUIRED = frozenset((
    "config_hashes.json",
    "universe_slices.json",
    "exchange_info.json",
    "funding_rates.jsonl",
    "trade_signals.jsonl",
))


def _blocked(*gaps: str) -> Dict[str, Any]:
    return {"decision": "BLOCKED_DATA", "source_provenance": "unverified", "source_attested": False, "gaps": list(gaps)}


def _utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _relative_files(source: Path, file_paths: Optional[Iterable[str]]) -> list[str]:
    if file_paths is not None:
        return list(file_paths)
    return sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file() and path.name != "manifest.json")


def _has_symlink_ancestor(path: Path) -> bool:
    absolute = path.absolute()
    return any(part.is_symlink() for part in (absolute, *absolute.parents))


def _repo_destination_is_ignored(destination: Path) -> bool:
    existing = destination
    while not existing.exists():
        if existing == existing.parent:
            return False
        existing = existing.parent
    lookup = subprocess.run(
        ["git", "-C", str(existing), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    if lookup.returncode != 0:
        # A true non-repository local directory is allowed; other git errors fail closed.
        return "not a git repository" in lookup.stderr.lower()
    repo = Path(lookup.stdout.strip())
    check = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", str(destination / "manifest.json")],
        cwd=repo, capture_output=True, check=False,
    )
    return check.returncode == 0


def seal_snapshot(
    source_dir: Any,
    output_root: Any,
    strategy_id: str,
    as_of: datetime,
    window_start: datetime,
    window_end: datetime,
    *,
    file_paths: Optional[Iterable[str]] = None,
    claimed_source_provenance: str = "unverified",
) -> Dict[str, Any]:
    """Copy frozen files to ``output_root/snapshots/<strategy>/<as_of>``.

    Missing/ambiguous evidence returns structured ``BLOCKED_DATA``; success is
    only an integrity seal, never a verified live-data or parity verdict.
    """
    gaps = []
    if strategy_id not in _STRATEGIES:
        gaps.append("strategy_unsupported:{0}".format(strategy_id))
    times = tuple(_utc(value) for value in (as_of, window_start, window_end))
    if any(value is None for value in times):
        gaps.append("timezone_required:as_of/window_start/window_end")
    elif not (times[1] < times[2] <= times[0]):
        gaps.append("invalid_window")
    if claimed_source_provenance != "unverified":
        gaps.append("source_provenance_unverifiable:{0}".format(claimed_source_provenance))
    source = Path(source_dir)
    if not source.is_dir() or _has_symlink_ancestor(source):
        gaps.append("source_missing_or_symlink:{0}".format(source))
    if gaps:
        return _blocked(*gaps)

    destination = Path(output_root) / "snapshots" / strategy_id / times[0].strftime("%Y%m%dT%H%M%SZ")
    if _has_symlink_ancestor(destination):
        return _blocked("output_symlink")
    if not _repo_destination_is_ignored(destination):
        return _blocked("output_not_git_ignored")
    if destination.exists() or destination.is_symlink():
        return _blocked("snapshot_already_exists:{0}".format(destination))

    raw_files = _relative_files(source, file_paths)
    selected = []
    seen = set()
    basenames = set()
    for raw in raw_files:
        if not isinstance(raw, str):
            gaps.append("invalid_file_path:{0}".format(raw))
            continue
        rel = PurePosixPath(raw)
        if rel.is_absolute() or not raw or "." in rel.parts or ".." in rel.parts or "\\" in raw:
            gaps.append("path_escape:{0}".format(raw))
            continue
        if raw == "manifest.json":
            gaps.append("reserved_manifest_input")
            continue
        if raw in seen or rel.name in basenames:
            gaps.append("duplicate_file:{0}".format(raw))
            continue
        seen.add(raw)
        basenames.add(rel.name)
        path = source / raw
        if any(part.is_symlink() for part in (path, *path.parents) if part == source or source in part.parents):
            gaps.append("source_symlink:{0}".format(raw))
            continue
        if not path.is_file():
            gaps.append("source_file_missing:{0}".format(raw))
            continue
        selected.append(raw)

    for required in sorted(_REQUIRED - set(selected)):
        gaps.append("dataset_missing:{0}".format(required))
    if strategy_id == "hrs" and "market_snapshots.jsonl" not in selected:
        gaps.append("dataset_missing:market_snapshots.jsonl")
    if not any(name.startswith("klines/") and name.endswith(".jsonl") for name in selected):
        gaps.append("dataset_missing:klines/*.jsonl")
    if "universe_slices.json" in selected:
        try:
            universe = json.loads((source / "universe_slices.json").read_text(encoding="utf-8"))
            slices = universe.get("slices") if isinstance(universe, dict) else universe
            if not isinstance(slices, list) or not slices:
                raise ValueError("universe_slices_empty")
            for slice_ in slices:
                if not isinstance(slice_, dict) or not slice_.get("symbols") or not slice_.get("intervals"):
                    raise ValueError("universe_slice_invalid")
                for symbol in slice_["symbols"]:
                    for interval in slice_["intervals"]:
                        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9]+", symbol) or not isinstance(interval, str) or not re.fullmatch(r"[A-Za-z0-9]+", interval):
                            raise ValueError("universe_symbol_or_interval_invalid")
                        required = "klines/{0}_{1}.jsonl".format(symbol, interval)
                        if required not in selected:
                            gaps.append("dataset_missing:{0}".format(required))
        except (OSError, ValueError, TypeError) as exc:
            gaps.append("universe_unreadable:{0}".format(exc))
    if gaps:
        return _blocked(*gaps)

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".sealing-", dir=str(destination.parent)))
    except OSError as exc:
        return _blocked("snapshot_seal_failed:{0}".format(exc))
    try:
        files: Dict[str, Dict[str, str]] = {}
        for rel in sorted(selected):
            target = temporary / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            source_file = source / rel
            descriptor = os.open(str(source_file), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as input_file, target.open("wb") as output_file:
                if not stat.S_ISREG(os.fstat(input_file.fileno()).st_mode):
                    raise ValueError("not_regular_file:{0}".format(rel))
                shutil.copyfileobj(input_file, output_file)
            files[rel] = {"path": rel, "sha256": sha256_file(str(target))}

        manifest = {
            "manifest_version": 2,
            "strategy_id": strategy_id,
            "as_of": times[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_start": times[1].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_end": times[2].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source_provenance": "unverified",
            "files": files,
            "sha256": compute_manifest_sha256(files),
        }
        manifest["envelope_sha256"] = compute_manifest_envelope_sha256(manifest)
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if destination.exists() or destination.is_symlink():
            return _blocked("snapshot_already_exists:{0}".format(destination))
        temporary.rename(destination)
        return {
            "status": "SEALED",
            "source_provenance": "unverified",
            "source_attested": False,
            "snapshot_dir": str(destination),
            "manifest_sha256": manifest["sha256"],
            "envelope_sha256": manifest["envelope_sha256"],
        }
    except (OSError, ValueError) as exc:
        return _blocked("snapshot_seal_failed:{0}".format(exc))
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
