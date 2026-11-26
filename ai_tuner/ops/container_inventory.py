"""按已核验的完整容器身份区分在役和退役实例，未知实例仍须巡检。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


DEFAULT_REGISTRY = Path("/root/trading_system/.ops/container_retirements.json")


def read_registry(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "entries": []}
    return json.loads(path.read_text())


def partition(containers: list[dict], registry: dict) -> tuple[list, list, list]:
    if not isinstance(registry, dict) or registry.get("version") != 1 or not isinstance(registry.get("entries"), list):
        raise ValueError("退役登记格式不合法")
    by_name = {item["Name"].lstrip("/"): item for item in containers}
    by_id = {item["Id"]: item for item in containers}
    retired_ids, registered_ids, registered_names, issues = set(), set(), set(), []
    for entry in registry["entries"]:
        if (not isinstance(entry, dict)
                or any(not isinstance(entry.get(k), str) or not entry[k] for k in ("id", "name", "successor", "reason"))
                or not re.fullmatch(r"[0-9a-f]{64}", entry["id"])
                or entry["name"] == entry["successor"]
                or entry["id"] in registered_ids or entry["name"] in registered_names):
            raise ValueError("退役登记身份不合法或重复")
        registered_ids.add(entry["id"])
        registered_names.add(entry["name"])
        item = by_id.get(entry["id"]) or by_name.get(entry["name"])
        if entry["successor"] not in by_name:
            issues.append(f"退役登记的接替实例缺失: {entry['successor']}")
        if item is None:
            continue  # 已归档删除的旧实例不要求继续存在。
        if (item["Id"] != entry["id"] or item["Name"].lstrip("/") != entry["name"]
                or item["State"]["Status"] != "exited"
                or item["HostConfig"].get("RestartPolicy", {}).get("Name") != "no"
                or entry["successor"] not in by_name):
            issues.append(f"退役登记状态漂移或接替实例缺失: {entry['name']}")
            continue
        retired_ids.add(item["Id"])
    # 防止登记链把接替实例也排除；出现这种清单直接拒绝。
    if any(e["successor"] in registered_names for e in registry["entries"]):
        raise ValueError("退役接替实例不能同时登记为退役")
    return ([c for c in containers if c["Id"] not in retired_ids],
            [c for c in containers if c["Id"] in retired_ids], issues)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--names", action="store_true")
    args = parser.parse_args()
    ids = subprocess.check_output(["docker", "ps", "-aq"], text=True).split()
    containers = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True)) if ids else []
    active, retired, issues = partition(containers, read_registry(args.registry))
    if issues:
        print("; ".join(issues), file=sys.stderr)
        return 1
    if args.names:
        print("\n".join(c["Name"].lstrip("/") for c in active))
    else:
        print(json.dumps({"active": [c["Name"].lstrip("/") for c in active],
                          "retired": [c["Name"].lstrip("/") for c in retired]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
