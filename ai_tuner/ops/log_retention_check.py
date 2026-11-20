"""Read-only host-side container log retention check and Feishu report."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import select
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


def journal_time(field: str, value: str, *, latest: bool) -> dt.datetime | None:
    args = [
        "journalctl", f"{field}={value}", "-q", "--no-pager", "-o", "json",
        "--output-fields=__REALTIME_TIMESTAMP",
    ]
    if latest:
        line = run(*args, "--reverse", "-n", "1").strip()
    else:
        # Read one record without loading the whole history into memory.
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
            assert proc.stdout is not None
            try:
                if not select.select([proc.stdout], [], [], 60)[0]:
                    raise subprocess.TimeoutExpired(args, 60)
                line = proc.stdout.readline()
                if not line and (status := proc.wait(timeout=3)):
                    raise subprocess.CalledProcessError(status, args)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
    if not line:
        return None
    return dt.datetime.fromtimestamp(int(json.loads(line)["__REALTIME_TIMESTAMP"]) / 1e6, TZ)


def journal_range(field: str, value: str) -> tuple[dt.datetime | None, dt.datetime | None]:
    first = journal_time(field, value, latest=False)
    if first is None:
        return None, None
    return first, journal_time(field, value, latest=True)


def docker_log_range(container_id: str) -> tuple[dt.datetime | None, dt.datetime | None]:
    output = subprocess.run(
        ["docker", "logs", "--timestamps", container_id],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", timeout=60,
    ).stdout
    first = last = None
    for line in output.splitlines():
        prefix = line.partition(" ")[0]
        try:
            timestamp = dt.datetime.fromisoformat(prefix.replace("Z", "+00:00"))
        except ValueError:
            continue
        if timestamp.tzinfo is None:
            continue
        timestamp = timestamp.astimezone(TZ)
        first = timestamp if first is None else min(first, timestamp)
        last = timestamp if last is None else max(last, timestamp)
    if first is None and output.strip():
        raise ValueError("Docker log timestamps missing")
    return first, last


LOG_QUERY_ERRORS = (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError)


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
        driver = container["HostConfig"]["LogConfig"]["Type"]
        container_id = container["Id"]
        first = last = history_first = history_last = None
        current_error = history_error = False
        try:
            if driver == "journald":
                first, last = journal_range("CONTAINER_ID_FULL", container_id)
            else:
                first, last = docker_log_range(container_id)
        except LOG_QUERY_ERRORS:
            current_error = True
        try:
            history_first, history_last = journal_range("CONTAINER_NAME", name)
        except LOG_QUERY_ERRORS:
            history_error = True
        rows.append({
            "service": labels.get("com.docker.compose.service") or expected_names.get(name, "?"),
            "state": container["State"]["Status"],
            "health": health["Status"] if health else "no-healthcheck",
            "driver": driver,
            "first": first,
            "last": last,
            "current_error": current_error,
            "history_first": history_first,
            "history_last": history_last,
            "history_error": history_error,
            "config_files": labels.get("com.docker.compose.project.config_files", ""),
        })
    return {
        "now": now,
        "configured": set(configured),
        "rows": sorted(rows, key=lambda row: row["service"]),
        "journal": run("journalctl", "--disk-usage").strip(),
        "free": int(run("df", "-B1", "--output=avail", "/").splitlines()[-1]),
    }


def format_window(first: dt.datetime | None, last: dt.datetime | None, failed: bool) -> str:
    if failed:
        return "查询失败"
    start = first.strftime("%m-%d %H:%M") if first else "无"
    end = last.strftime("%m-%d %H:%M") if last else "无"
    return f"{start} → {end}"


def evaluate(data: dict) -> list[str]:
    now = data["now"]
    rows = data["rows"]
    actual = {row["service"] for row in rows}
    missing = sorted(data["configured"] - actual)
    issues = []
    short_windows = []
    if missing:
        issues.append("缺少容器: " + ", ".join(missing))
    for row in rows:
        service = row["service"]
        if row["state"] != "running" or row["health"] != "healthy":
            issues.append(f"{service} 状态 {row['state']}/{row['health']}")
        if row["driver"] != "journald":
            issues.append(f"{service} 日志驱动 {row['driver']}")
        if row["current_error"]:
            issues.append(f"{service} 当前实例日志查询失败")
        elif row["last"] is None:
            issues.append(f"{service} 当前实例无日志")
        elif now - row["last"] > dt.timedelta(days=1) and service != "dashboard-api":
            issues.append(f"{service} 当前实例超过 24 小时无新日志")
        start = LATER.get(service, BASELINE)
        if row["history_error"]:
            issues.append(f"{service} 服务 journal 查询失败")
        elif row["history_first"] and now.date() >= start and now - row["history_first"] < dt.timedelta(days=14):
            short_windows.append(row["history_first"])
    if short_windows:
        issues.append(
            f"{len(short_windows)} 个服务可查窗口不足 14 天"
            f"（最早 {min(short_windows):%m-%d %H:%M}，要求 ≥14 天）"
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
    extra = sorted({row["service"] for row in rows} - data["configured"])
    issues = evaluate(data)
    head = f"生产容器日志巡检 {now:%Y-%m-%d %H:%M} 北京时间"
    lines = [head, f"结论: {'异常 ' + str(len(issues)) + ' 项' if issues else '正常'}", f"容器: {len(rows)} 个；基础 Compose: {len(data['configured'])} 个", f"journal: {data['journal']}；根分区剩余: {data['free']/1e9:.2f} GB"]
    if extra:
        lines.append("附加部署服务: " + ", ".join(extra))
    if issues:
        lines += ["异常:"] + ["- " + issue for issue in issues]
    lines.append("服务明细（最早 → 最近，北京时间）:")
    for row in rows:
        current = format_window(row["first"], row["last"], row["current_error"])
        history = format_window(row["history_first"], row["history_last"], row["history_error"])
        lines.append(f"{row['service']}: {row['health']}/{row['driver']} 当前实例 {current}；服务 journal {history}")
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
