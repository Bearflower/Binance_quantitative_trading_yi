"""六个不可变报告的 canonical 序列化与原子写。

口径（契约 §5）：
- canonical JSON：``sort_keys=True``、``separators=(",", ":")``、Decimal→字符串、
  datetime→UTC ISO Z、UTF-8、末尾单个换行；
- 相同输入重复执行，文件逐字节一致；
- 六个文件顶层 ``status`` 必须一致，否则视为序列化 bug（写盘前断言）。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import to_utc_z

REPORT_FILES = (
    "run_manifest.json",
    "data_quality.json",
    "parity_report.json",
    "scorecard.json",
    "qualification_decision.json",
)

AUDIT_FILE = "audit_events.jsonl"


def to_jsonable(value: Any) -> Any:
    """递归把 Decimal/datetime/dataclass/tuple 转成可序列化且确定性的结构。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return to_utc_z(value)
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    return value


def canonical_json(value: Any) -> str:
    """canonical JSON 文本（含末尾单个换行）。"""
    return json.dumps(
        to_jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ) + "\n"


def write_report(path: Any, payload: Dict[str, Any]) -> Path:
    """原子写入一个 canonical JSON 报告。"""
    target = Path(path)
    os.makedirs(str(target.parent), exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(payload))
    os.replace(str(tmp), str(target))
    return target


def write_audit(path: Any, events: List[Dict[str, Any]]) -> Path:
    """写 audit_events.jsonl：行序按传入顺序（已由 engine 按 (ts, seq) 排序）。"""
    target = Path(path)
    os.makedirs(str(target.parent), exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(canonical_json(event))
    os.replace(str(tmp), str(target))
    return target


def assert_status_consistent(reports: Dict[str, Dict[str, Any]]) -> None:
    """六文件（五 JSON + 决策）顶层 status 必须一致。"""
    seen: Dict[str, str] = {}
    for name, payload in sorted(reports.items()):
        status = payload.get("status")
        if status is None:
            raise ValueError("报告 {0} 缺少顶层 status".format(name))
        seen[name] = status
    distinct = set(seen.values())
    if len(distinct) != 1:
        raise ValueError("六文件顶层 status 不一致：{0}".format(seen))


def write_bundle(
    out_dir: Any,
    reports: Dict[str, Dict[str, Any]],
    audit_events: List[Dict[str, Any]],
    expected_status: Optional[str] = None,
) -> Dict[str, str]:
    """写出五个 JSON + audit_events.jsonl，返回文件名 → 绝对路径映射。"""
    assert_status_consistent(
        {
            "run_manifest.json": reports["run_manifest"],
            "data_quality.json": reports["data_quality"],
            "parity_report.json": reports["parity_report"],
            "scorecard.json": reports["scorecard"],
            "qualification_decision.json": reports["qualification_decision"],
        }
    )
    if expected_status is not None:
        actual = reports["qualification_decision"]["status"]
        if actual != expected_status:
            raise ValueError("期望状态 {0}，实际 {1}".format(expected_status, actual))

    directory = Path(out_dir)
    os.makedirs(str(directory), exist_ok=True)
    written: Dict[str, str] = {}
    for name in REPORT_FILES:
        key = name.replace(".json", "")
        written[name] = str(write_report(directory / name, reports[key]))
    written[AUDIT_FILE] = str(write_audit(directory / AUDIT_FILE, audit_events))
    return written
