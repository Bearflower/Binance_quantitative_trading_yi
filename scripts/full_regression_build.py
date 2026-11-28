"""Build evidence only: immutable digest inventory, no deployment operations."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "deployment/full-regression/build-plan.json"


def read_plan():
    plan = json.loads(PLAN.read_text())
    names = [image["name"] for image in plan["images"]]
    containers = [name for image in plan["images"] for name in image["containers"]]
    preserved = [entry["container"] for entry in plan["preserve_existing"]]
    if len(names) != len(set(names)) or len(containers + preserved) != len(set(containers + preserved)):
        raise ValueError("duplicate image or container assignment")
    return plan


def identity(sha, run_id, attempt):
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or not re.fullmatch(r"[1-9][0-9]*", run_id) or not re.fullmatch(r"[1-9][0-9]*", attempt):
        raise ValueError("invalid build identity")


def build_receipt(name, digest, sha, run_id, attempt):
    identity(sha, run_id, attempt)
    image = next((item for item in read_plan()["images"] if item["name"] == name), None)
    if image is None or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("unknown image or invalid digest")
    repository = f"ghcr.io/bearflower/trading-{name}"
    return dict(image, source_sha=sha, run_id=run_id, run_attempt=attempt,
                tag=f"{repository}:full-regression-{sha}-{run_id}-{attempt}",
                digest=digest, image=f"{repository}@{digest}", platform=read_plan()["platform"])


def assemble(receipts, sha, run_id, attempt):
    identity(sha, run_id, attempt)
    plan = read_plan()
    by_name = {}
    for row in receipts:
        expected = build_receipt(row["name"], row["digest"], sha, run_id, attempt)
        if row != expected or row["name"] in by_name:
            raise ValueError("duplicate, mixed-build, or altered image receipt")
        by_name[row["name"]] = row
    if set(by_name) != {image["name"] for image in plan["images"]}:
        raise ValueError("incomplete image set")
    return dict(schema_version=1, source_sha=sha, run_id=run_id, run_attempt=attempt,
                platform=plan["platform"], images=by_name,
                preserve_existing=plan["preserve_existing"], deployment_performed=False)


def source_inventory():
    """Checkout hashes for later comparison; these are not container verification."""
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    roots = {"ai_tuner", "dashboard", "strategies", "shared", "services", "scripts", "database", "deployment", "tools"}
    return {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in sorted(paths) if path and (ROOT / path).is_file()
            and (Path(path).parts[0] in roots or path in {"VERSION", "requirements.txt", ".dockerignore", "deployment/full-regression/build-plan.json"})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("matrix", "receipt", "assemble", "sources"))
    parser.add_argument("--sha")
    parser.add_argument("--run-id")
    parser.add_argument("--attempt")
    parser.add_argument("--name")
    parser.add_argument("--digest")
    parser.add_argument("--receipts", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "matrix":
        result = {"include": [{"name": row["name"], "dockerfile": row["dockerfile"],
                               "modules": ",".join(row["modules"])} for row in read_plan()["images"]]}
    elif args.action == "receipt":
        result = build_receipt(args.name, args.digest, args.sha, args.run_id, args.attempt)
    elif args.action == "assemble":
        result = assemble([json.loads(path.read_text()) for path in sorted(args.receipts.glob("*.json"))],
                          args.sha, args.run_id, args.attempt)
    else:
        result = {"kind": "checkout_source_hashes", "files": source_inventory()}
    encoded = json.dumps(result, sort_keys=True, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
