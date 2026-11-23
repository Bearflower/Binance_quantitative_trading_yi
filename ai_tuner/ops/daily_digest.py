"""每日巡检汇总器：07:35 读取各段 outbox，合并为一条飞书消息发送。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

try:
    from ai_tuner.ops import digest_outbox, hrs_observation, mtpcs_observation
except ImportError:  # 以脚本方式直接运行时，当前目录即 ai_tuner/ops
    import digest_outbox
    import hrs_observation
    import mtpcs_observation

TZ = digest_outbox.TZ
MAX_PAYLOAD_BYTES = 24000
DEFAULT_DIRS = (
    "/var/lib/trading-digest/outbox",
    "/root/trading_system/logs/ai_tuner/digest/outbox",
)


def missing_text(label: str, slot: str) -> str:
    """生成缺失段落正文。"""
    return f"【{label}】本次未生成（{slot} 任务未产出），请检查对应服务。"


def overview(entries: dict) -> str:
    """按段落状态生成首部「异常概览」。"""
    parts = []
    healthy = 0
    for key, label, slot, short in digest_outbox.SECTIONS:
        entry = entries.get(key)
        if entry is None:
            parts.append(f"{short} 未生成")
            continue
        if entry["status"] in ("warn", "error"):
            parts.append(f"{short} {entry['summary'] or '异常'}")
        else:
            healthy += 1
    if not parts:
        return "全部正常"
    if healthy:
        parts.append("其余正常")
    return "｜".join(parts)


def render(entries: dict, now: dt.datetime) -> str:
    """把各段合并成完整消息。"""
    head = [
        f"【交易系统每日巡检汇总】{now:%Y-%m-%d %H:%M}（北京时间）",
        "本次共 {} 项：{}".format(
            len(digest_outbox.SECTIONS),
            "｜".join(short for _, _, _, short in digest_outbox.SECTIONS),
        ),
        f"异常概览：{overview(entries)}",
    ]
    blocks = []
    total = len(digest_outbox.SECTIONS)
    for index, (key, label, slot, _) in enumerate(digest_outbox.SECTIONS, start=1):
        entry = entries.get(key)
        body = entry["text"] if entry else missing_text(label, slot)
        blocks.append(f"━━━━━━━━━━ {index}/{total} {label}（{slot}）━━━━━━━━━━\n{body}")
    return "\n".join(head) + "\n\n" + "\n\n".join(blocks)


def payload_bytes(message: str) -> int:
    """计算飞书文本消息 JSON 的字节数。"""
    payload = json.dumps({"msg_type": "text", "content": {"text": message}}, ensure_ascii=False)
    return len(payload.encode("utf-8"))


def build_messages(entries: dict, now: dt.datetime) -> list[str]:
    """生成一条或两条消息；超长时按段边界拆分。"""
    message = render(entries, now)
    if payload_bytes(message) <= MAX_PAYLOAD_BYTES:
        return [message]

    header = [
        f"【交易系统每日巡检汇总】{now:%Y-%m-%d}（北京时间）",
        f"异常概览：{overview(entries)}",
        "本次内容较长，分两条发送。",
    ]
    blocks = []
    total = len(digest_outbox.SECTIONS)
    for index, (key, label, slot, _) in enumerate(digest_outbox.SECTIONS, start=1):
        entry = entries.get(key)
        body = entry["text"] if entry else missing_text(label, slot)
        blocks.append(f"━━━━━━━━━━ {index}/{total} {label}（{slot}）━━━━━━━━━━\n{body}")

    first = "\n".join(header) + "\n\n"
    used = []
    for block in blocks:
        candidate = first + "\n\n".join(used + [block])
        if used and payload_bytes(candidate) > MAX_PAYLOAD_BYTES:
            break
        used.append(block)
    rest = blocks[len(used):]
    if not rest:  # 单段即超长时也只能整段发送
        return [first + "\n\n".join(used)]
    second_head = f"【交易系统每日巡检汇总】{now:%Y-%m-%d}（续 2/2）"
    return [first + "\n\n".join(used), second_head + "\n\n" + "\n\n".join(rest)]


def collect(dirs, day: dt.date) -> tuple[dict, list[str]]:
    """读取 outbox；返回段落与告警。"""
    return digest_outbox.read_all(dirs, day)


def wait_for_sections(dirs, day: dt.date, deadline_seconds: int, poll_seconds: int = 5) -> tuple[dict, list[str]]:
    """在截止时间内轮询，等待全部段落就位。"""
    deadline = time.monotonic() + max(0, deadline_seconds)
    while True:
        entries, warnings = collect(dirs, day)
        if len(entries) >= len(digest_outbox.SECTIONS) or time.monotonic() >= deadline:
            return entries, warnings
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


def send(webhook_file: Path, message: str) -> None:
    """通过飞书自定义机器人发送一条文本消息。"""
    webhook = webhook_file.read_text(encoding="utf-8").strip()
    if not webhook.startswith("https://open.feishu.cn/open-apis/bot/v2/hook/"):
        raise ValueError("invalid Feishu webhook URL")
    payload = json.dumps({"msg_type": "text", "content": {"text": message}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read())
    code = result.get("code", result.get("StatusCode")) if isinstance(result, dict) else None
    if type(code) is not int:
        raise RuntimeError("Feishu webhook returned no numeric status code")
    if code != 0:
        raise RuntimeError(f"Feishu webhook rejected the digest (code={code})")


def archive(entries: dict, now: dt.datetime) -> None:
    """把已发送的段落移动到同目录 archive/YYYY-MM-DD/。"""
    for entry in entries.values():
        source = Path(entry["path"])
        target_dir = source.parent / "archive" / f"{now:%Y-%m-%d}"
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target_dir / source.name))


def main() -> int:
    parser = argparse.ArgumentParser(description="交易系统每日巡检汇总")
    parser.add_argument("--outbox-dir", action="append", default=None,
                        help="outbox 目录，可重复；默认读取宿主机与容器两处")
    parser.add_argument("--webhook-file", type=Path,
                        default=Path("/root/trading_system/.secrets/daily_digest_webhook"))
    parser.add_argument("--wait-seconds", type=int, default=240)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-archive", action="store_true")
    args = parser.parse_args()

    dirs = tuple(args.outbox_dir) if args.outbox_dir else DEFAULT_DIRS
    hrs_preview = None
    mtpcs_preview = None
    if args.outbox_dir is None:
        hrs_preview = hrs_observation.prepare_digest(dry_run=args.dry_run)
        mtpcs_preview = mtpcs_observation.prepare_digest(dry_run=args.dry_run)
    now = dt.datetime.now(TZ)
    if args.dry_run:
        entries, warnings = collect(dirs, now.date())
        if hrs_preview is not None:
            entries["hrs_observation"] = hrs_preview
        if mtpcs_preview is not None:
            entries["mtpcs"] = mtpcs_preview
    else:
        entries, warnings = wait_for_sections(dirs, now.date(), args.wait_seconds)
    for warning in warnings:
        print(f"outbox 告警: {warning}", file=sys.stderr)

    now = dt.datetime.now(TZ)
    for message in build_messages(entries, now):
        print(message)
        if args.dry_run:
            continue
        send(args.webhook_file, message)

    if args.dry_run:
        return 0
    if entries and not args.no_archive:
        archive(entries, now)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"daily digest failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
