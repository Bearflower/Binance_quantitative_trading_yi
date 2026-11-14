"""Validate host-generated, credential-free XAU runtime evidence."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def runtime_issues(now):
    path = Path(os.getenv("XAU_RUNTIME_EVIDENCE_FILE", "/app/logs/xau_runtime.json"))
    try:
        evidence = json.loads(path.read_text())
        sampled_at = datetime.fromisoformat(evidence["sampled_at"])
        if not timedelta(0) <= now - sampled_at <= timedelta(hours=25):
            return ["运行身份未核验：宿主机证据已过期或时间异常"]
        issues = []
        previous = path.with_name("xau_runtime_previous.json")
        old = json.loads(previous.read_text()) if previous.exists() else {}
        containers = evidence["containers"]
        for name in ("trading_system-mtcps-xau-shadow", "trading_system-mtcps-xau-calibrated-shadow"):
            current = containers[name]
            if current["status"] != "running" or current["health"] != "healthy":
                issues.append(f"{name} 容器异常")
            if current["restart_count"]:
                issues.append(f"{name} 重启次数 {current['restart_count']}")
            prior = old.get("containers", {}).get(name)
            if prior and any(prior.get(key) != current.get(key) for key in ("id", "started_at", "image", "git_sha")):
                issues.append(f"{name} 容器身份变化")
        if not evidence.get("journal_usage"):
            issues.append("宿主机日志占用未核验")
        temporary = previous.with_suffix(".tmp")
        temporary.write_text(json.dumps(evidence))
        temporary.replace(previous)
        return issues
    except (OSError, ValueError, KeyError, TypeError):
        return ["运行身份未核验：宿主机容器及日志证据不可用"]
