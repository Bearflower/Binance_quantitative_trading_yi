"""每日巡检汇总的共享 outbox：各巡检段落只发布，不直接发送飞书。

宿主脚本与 ai-tuner 容器内脚本共用本模块（纯标准库）。
发布目录由环境变量 ``DIGEST_OUTBOX_DIR`` 指定：
    宿主：/var/lib/trading-digest/outbox
    容器：/app/logs/digest/outbox
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
DEFAULT_OUTBOX = "/var/lib/trading-digest/outbox"
STATUSES = ("ok", "warn", "error", "skip")

# (key, 段标题, 计划时间, 首部简称)
SECTIONS = [
    ("hrs", "HRS 生产只读巡检", "07:31", "HRS 生产只读"),
    ("grid", "网格实盘与行情巡检", "07:32", "网格实盘与行情"),
    ("btc_xau", "BTC–XAU 影子巡检", "07:33", "BTC–XAU 影子"),
    ("xau", "XAU 影子巡检", "07:33", "XAU 影子"),
    ("profit", "利润提取提醒", "07:34", "利润提取"),
    ("logs", "生产容器日志巡检", "07:34", "生产容器日志"),
    ("mtpcs", "MTPCS 开仓归因与影子观察", "07:35", "MTPCS 观察"),
]
SECTION_KEYS = tuple(key for key, _, _, _ in SECTIONS)
SECTION_BY_KEY = {key: (label, slot, short) for key, label, slot, short in SECTIONS}


def outbox_dir() -> Path:
    """返回当前进程应写入的 outbox 目录。"""
    return Path(os.environ.get("DIGEST_OUTBOX_DIR", DEFAULT_OUTBOX))


def publish(key: str, text: str, *, status: str, summary: str = "", now: datetime | None = None) -> Path:
    """把一段巡检正文写入 outbox，原子替换同名文件。

    Args:
        key: 段落标识，必须属于 ``SECTION_KEYS``。
        text: 该段完整正文，不能为空。
        status: ``ok`` / ``warn`` / ``error`` / ``skip``。
        summary: 写入首部「异常概览」的短语。
        now: 生成时间，默认当前北京时间。

    Returns:
        写入的文件路径。
    """
    if key not in SECTION_BY_KEY:
        raise ValueError(f"unknown digest section: {key!r}")
    if status not in STATUSES:
        raise ValueError(f"invalid digest status: {status!r}")
    body = (text or "").strip()
    if not body:
        raise ValueError("digest section text is empty")

    stamp = (now or datetime.now(TZ)).astimezone(TZ)
    payload = {
        "key": key,
        "generated_at": stamp.isoformat(),
        "status": status,
        "summary": (summary or "").strip(),
        "text": body,
    }
    target_dir = outbox_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{key}.json"
    fd, tmp_path = tempfile.mkstemp(dir=str(target_dir), prefix=f".{key}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return target


def read_all(dirs, day: date) -> tuple[dict, list[str]]:
    """读取多个 outbox 目录中属于 ``day`` 的段落。

    同名段落按目录顺序先到先得；损坏或字段非法的文件跳过并记入告警。

    Returns:
        (entries, warnings)：entries 为 ``key -> entry``。
    """
    entries: dict[str, dict] = {}
    warnings: list[str] = []
    for directory in dirs:
        for key in SECTION_KEYS:
            if key in entries:
                continue
            path = Path(directory) / f"{key}.json"
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                stamp = datetime.fromisoformat(str(data["generated_at"]))
                text = str(data["text"]).strip()
                status = str(data["status"])
                summary = str(data.get("summary") or "")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                warnings.append(f"{path}: {type(exc).__name__}")
                continue
            if status not in STATUSES or not text:
                warnings.append(f"{path}: invalid payload")
                continue
            if stamp.astimezone(TZ).date() != day:
                continue
            entries[key] = {
                "key": key,
                "generated_at": stamp,
                "status": status,
                "summary": summary,
                "text": text,
                "path": path,
            }
    return entries, warnings
