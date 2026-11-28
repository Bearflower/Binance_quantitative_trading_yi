"""从人工审阅的精确请求生成登记草案；不修改生产登记或 Docker 状态。"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ai_tuner.ops.container_inventory import lifecycle_entry, partition, read_registry


def plan(containers: list[dict], registry: dict, requests: list[dict]) -> dict:
    result = copy.deepcopy(registry)
    # 旧登记必须先有效，不能靠生成新登记掩盖已有漂移。
    if partition(containers, result)[2]:
        raise ValueError("已有生命周期登记不一致")
    by_name = {c["Name"].lstrip("/"): c for c in containers}
    for request in requests:
        item = by_name.get(request["name"])
        if item is None or item["Id"] != request["id"]:
            raise ValueError("请求身份与容器快照不符: " + request["name"])
        result["entries"].append(lifecycle_entry(
            item, **{key: request[key] for key in ("successor", "lifecycle", "reason", "evidence")},
            **{key: request[key] for key in ("result", "failure_summary", "failure_observed_at") if key in request}))
    _, _, issues = partition(containers, result)
    if issues:
        raise ValueError("; ".join(issues))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, help="读取离线脱敏快照；省略则只读检查 Docker")
    args = parser.parse_args()
    if args.snapshot:
        value = json.loads(args.snapshot.read_text())
        containers = value["containers"] if isinstance(value, dict) else value
    else:
        ids = subprocess.check_output(["docker", "ps", "-aq"], text=True).split()
        containers = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True)) if ids else []
    result = plan(containers, read_registry(args.registry), json.loads(args.requests.read_text()))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
