"""按已核验的完整容器身份区分在役和退役实例，未知实例仍须巡检。"""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys


DEFAULT_REGISTRY = Path("/root/trading_system/.ops/container_retirements.json")


def command_sha256(item: dict) -> str:
    """只保存启动命令指纹，不把命令中的潜在凭据写进登记。"""
    config = item.get("Config", {})
    body = {key: config.get(key) for key in ("Entrypoint", "Cmd")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _validate_lifecycle(entry: dict) -> None:
    if entry.get("lifecycle") not in ("retired", "standby", "completed_task", "archived_failed_task"):
        raise ValueError("未知容器生命周期类别")
    if (not re.fullmatch(r"sha256:[0-9a-f]{64}", str(entry.get("image", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("command_sha256", "")))
            or any(not isinstance(entry.get(k), str) or not entry[k]
                   for k in ("evidence", "finished_at"))
            or type(entry.get("exit_code")) is not int
            or (entry["lifecycle"] == "completed_task" and entry["exit_code"] != 0)):
        raise ValueError("生命周期登记缺少已核验证据或完整身份")
    if entry["lifecycle"] in ("completed_task", "archived_failed_task"):
        result = entry.get("result")
        if (not isinstance(result, dict)
                or type(result.get("success")) is not bool
                or type(result.get("error_count")) is not int or result["error_count"] < 0
                or not re.fullmatch(r"[0-9a-f]{64}", str(result.get("logs_sha256", "")))):
            raise ValueError("一次性任务缺少明确结果与日志指纹")
        if entry["lifecycle"] == "completed_task" and (
                not result["success"] or result["error_count"] != 0):
            raise ValueError("失败任务不能登记为成功完成")
        if entry["lifecycle"] == "archived_failed_task":
            if (result["success"] or (result["error_count"] == 0 and entry["exit_code"] == 0)
                    or not isinstance(entry.get("failure_summary"), str) or not entry["failure_summary"]):
                raise ValueError("失败归档必须保留具体失败原因")
            try:
                stamp = datetime.fromisoformat(entry["failure_observed_at"].replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError("失败时间缺少时区")
            except (KeyError, TypeError, AttributeError, ValueError) as exc:
                raise ValueError("失败归档必须保留带时区的发生时间") from exc


def _source_matches(item: dict, entry: dict) -> bool:
    state = item["State"]
    expected = "created" if entry["lifecycle"] == "standby" else "exited"
    return (state["Status"] == expected
            and state.get("ExitCode") == entry["exit_code"]
            and state.get("OOMKilled") is False and not state.get("Error")
            and state.get("FinishedAt") == entry["finished_at"]
            and item.get("Image") == entry["image"]
            and command_sha256(item) == entry["command_sha256"]
            and item["HostConfig"].get("RestartPolicy", {}).get("Name") == "no")


def lifecycle_entry(item: dict, *, successor: str, lifecycle: str,
                    reason: str, evidence: str, result: dict | None = None,
                    failure_summary: str | None = None,
                    failure_observed_at: str | None = None) -> dict:
    """生成显式登记；调用者仍须核验任务结果或发布接替证据。"""
    entry = {"id": item["Id"], "name": item["Name"].lstrip("/"),
             "successor": successor, "reason": reason, "lifecycle": lifecycle,
             "evidence": evidence, "image": item["Image"],
             "command_sha256": command_sha256(item),
             "exit_code": item["State"].get("ExitCode"),
             "finished_at": item["State"].get("FinishedAt")}
    if lifecycle in ("completed_task", "archived_failed_task"):
        entry["result"] = result
    if lifecycle == "archived_failed_task":
        entry.update(failure_summary=failure_summary, failure_observed_at=failure_observed_at)
    _validate_lifecycle(entry)
    if not _source_matches(item, entry):
        raise ValueError("容器未安全停止或任务失败，不能登记排除: " + entry["name"])
    return entry


def historical_failures(registry: dict) -> list[dict]:
    """历史失败单独展示，不把已归档失败写成成功或在役宕机。"""
    return [dict(entry) for entry in registry["entries"]
            if entry.get("lifecycle") == "archived_failed_task"]


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
        explicit = "lifecycle" in entry
        if explicit:
            _validate_lifecycle(entry)
        item = by_id.get(entry["id"]) or by_name.get(entry["name"])
        if entry["successor"] not in by_name:
            issues.append(f"退役登记的接替实例缺失: {entry['successor']}")
        successor = by_name.get(entry["successor"])
        if explicit and successor is not None and (
                successor["State"]["Status"] != "running"
                or successor["State"].get("Health", {}).get("Status") != "healthy"):
            issues.append(f"生命周期登记的接替实例未通过健康检查: {entry['successor']}")
            continue
        if item is None:
            continue  # 已归档删除的旧实例不要求继续存在。
        if (item["Id"] != entry["id"] or item["Name"].lstrip("/") != entry["name"]
                or (not _source_matches(item, entry) if explicit else item["State"]["Status"] != "exited")
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
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--names", action="store_true")
    output.add_argument("--historical-failures", action="store_true")
    args = parser.parse_args()
    ids = subprocess.check_output(["docker", "ps", "-aq"], text=True).split()
    containers = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True)) if ids else []
    registry = read_registry(args.registry)
    active, retired, issues = partition(containers, registry)
    if issues:
        print("; ".join(issues), file=sys.stderr)
        return 1
    if args.historical_failures:
        print(json.dumps(historical_failures(registry), ensure_ascii=False))
    elif args.names:
        print("\n".join(c["Name"].lstrip("/") for c in active))
    else:
        print(json.dumps({"active": [c["Name"].lstrip("/") for c in active],
                          "retired": [c["Name"].lstrip("/") for c in retired],
                          "historical_failures": historical_failures(registry)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
