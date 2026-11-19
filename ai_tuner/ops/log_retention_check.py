"""Read-only host-side container log retention check and Feishu report."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    from ai_tuner.ops import digest_outbox
except ImportError:  # 以脚本方式直接运行时，当前目录即 ai_tuner/ops
    import digest_outbox


TZ = dt.timezone(dt.timedelta(hours=8))
BASELINE = dt.date(2026, 11, 12)
LATER = {
    "mtcps-xau-calibrated-shadow": dt.date(2026, 11, 13),
    "execution-facts-collector": dt.date(2026, 11, 18),
    "ai-tuner-funding-schedule": dt.date(2026, 11, 18),
}


def run(*args: str) -> str:
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=60).stdout


def journal_time(name: str, *, latest: bool) -> dt.datetime | None:
    args = ["journalctl", f"CONTAINER_NAME={name}", "-o", "json", "--no-pager"]
    if latest:
        args += ["--reverse", "-n", "1"]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if not line:
        return None
    return dt.datetime.fromtimestamp(int(json.loads(line)["__REALTIME_TIMESTAMP"]) / 1e6, TZ)


def collect(project_dir: Path, now: dt.datetime) -> dict:
    os.chdir(project_dir)
    profiles = run("docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.logging.yml", "config", "--profiles").split()
    cmd = ["docker", "compose"]
    for profile in profiles:
        cmd += ["--profile", profile]
    cmd += ["-f", "docker-compose.yml", "-f", "docker-compose.logging.yml"]
    config = json.loads(run(*cmd, "config", "--format", "json"))
    project = config["name"]
    configured = config["services"]
    ids = run("docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}").split()
    containers = json.loads(run("docker", "inspect", *ids)) if ids else []
    labelled = {
        (container["Config"].get("Labels") or {}).get("com.docker.compose.service")
        for container in containers
    }
    expected_names = {
        settings["container_name"]: service
        for service, settings in configured.items()
        if service not in labelled and settings.get("container_name")
    }
    # Restricted releases may replace a Compose container directly through Docker.
    if expected_names:
        actual_names = run("docker", "ps", "-a", "--format", "{{.Names}}").splitlines()
        candidates = sorted(expected_names.keys() & set(actual_names))
        if candidates:
            for container in json.loads(run("docker", "inspect", *candidates)):
                labels = container["Config"].get("Labels") or {}
                if not (labels.get("com.docker.compose.project") or labels.get("com.docker.compose.service")):
                    containers.append(container)
    rows = []
    for container in containers:
        labels = container["Config"].get("Labels") or {}
        name = container["Name"].lstrip("/")
        health = container["State"].get("Health")
        first = journal_time(name, latest=False)
        last = journal_time(name, latest=True)
        rows.append({
            "service": labels.get("com.docker.compose.service") or expected_names.get(name, "?"),
            "state": container["State"]["Status"],
            "health": health["Status"] if health else "no-healthcheck",
            "driver": container["HostConfig"]["LogConfig"]["Type"],
            "first": first,
            "last": last,
            "config_files": labels.get("com.docker.compose.project.config_files", ""),
        })
    return {
        "now": now,
        "configured": set(configured),
        "rows": sorted(rows, key=lambda row: row["service"]),
        "journal": run("journalctl", "--disk-usage").strip(),
        "free": int(run("df", "-B1", "--output=avail", "/").splitlines()[-1]),
    }


def evaluate(data: dict) -> list[str]:
    """返回巡检异常清单；窗口不足合并为一条全局异常。"""
    now = data["now"]
    rows = data["rows"]
    actual = {row["service"] for row in rows}
    missing = sorted(data["configured"] - actual)
    issues = []
    if missing:
        issues.append("缺少容器: " + ", ".join(missing))
    short_windows = []
    for row in rows:
        service = row["service"]
        if row["state"] != "running" or row["health"] != "healthy":
            issues.append(f"{service} 状态 {row['state']}/{row['health']}")
        if row["driver"] != "journald":
            issues.append(f"{service} 日志驱动 {row['driver']}")
        if row["last"] is None:
            issues.append(f"{service} 无可查日志")
        elif now - row["last"] > dt.timedelta(days=1) and service != "dashboard-api":
            issues.append(f"{service} 超过 24 小时无新日志")
        start = LATER.get(service, BASELINE)
        if now.date() >= start and row["first"] and now - row["first"] < dt.timedelta(days=14):
            short_windows.append(row["first"])
    if short_windows:
        earliest = min(short_windows)
        issues.append(
            f"{len(short_windows)} 个服务可查窗口不足 14 天"
            f"（最早 {earliest:%m-%d %H:%M}，要求 ≥14 天）"
        )
    if data["free"] < 5_000_000_000:
        issues.append("根分区剩余不足 5 GB")
    usage = re.search(r"([\d.]+)([KMGT])\b", data["journal"])
    if usage and float(usage.group(1)) * 1024 ** ("KMGT".index(usage.group(2)) + 1) >= 3 * 1024 ** 3:
        issues.append("journal 占用达到 3 GiB")
    return issues


def render(data: dict) -> str:
    now = data["now"]
    rows = data["rows"]
    actual = {row["service"] for row in rows}
    extra = sorted(actual - data["configured"])
    issues = evaluate(data)
    head = f"生产容器日志巡检 {now:%Y-%m-%d %H:%M} 北京时间"
    lines = [head, f"结论: {'异常 ' + str(len(issues)) + ' 项' if issues else '正常'}", f"容器: {len(rows)} 个；基础 Compose: {len(data['configured'])} 个", f"journal: {data['journal']}；根分区剩余: {data['free']/1e9:.2f} GB"]
    if extra:
        lines.append("附加部署服务: " + ", ".join(extra))
    if issues:
        lines += ["异常:"] + ["- " + issue for issue in issues]
    lines.append("服务明细（最早 → 最近，北京时间）:")
    for row in rows:
        first = row["first"].strftime("%m-%d %H:%M") if row["first"] else "无"
        last = row["last"].strftime("%m-%d %H:%M") if row["last"] else "无"
        lines.append(f"{row['service']}: {row['health']}/{row['driver']} {first} → {last}")
    return "\n".join(lines)


def send(webhook_file: Path, message: str) -> None:
    webhook = webhook_file.read_text().strip()
    if not webhook.startswith("https://open.feishu.cn/open-apis/bot/v2/hook/"):
        raise ValueError("invalid Feishu webhook URL")
    payload = json.dumps({"msg_type": "text", "content": {"text": message}}, ensure_ascii=False).encode()
    request = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read())
    code = result.get("code", result.get("StatusCode")) if isinstance(result, dict) else None
    if type(code) is not int:
        raise RuntimeError("Feishu webhook returned no numeric status code")
    if code != 0:
        raise RuntimeError(f"Feishu webhook rejected the report (code={code})")
    print("Feishu webhook accepted the report (code=0)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", type=Path, default=Path("/root/trading_system"))
    parser.add_argument("--webhook-file", type=Path, default=Path("/root/trading_system/.secrets/log_retention_webhook"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send", action="store_true", help="直接发送飞书，默认只写入 07:35 汇总 outbox")
    args = parser.parse_args()
    data = collect(args.project_dir, dt.datetime.now(TZ))
    report = render(data)
    print(report)
    if args.dry_run:
        return 0
    if args.send:
        send(args.webhook_file, report)
        return 0
    issues = evaluate(data)
    try:
        digest_outbox.publish(
            "logs",
            report,
            status="error" if issues else "ok",
            summary=f"{len(issues)} 项异常" if issues else "正常",
        )
    except (OSError, ValueError):
        # outbox 不可写时回退直发，避免报告丢失
        send(args.webhook_file, report)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"log retention check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
