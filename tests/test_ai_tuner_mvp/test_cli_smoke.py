"""
CLI 可运行性冒烟测试

覆盖：
  1. `python3 scripts/ai_tuner_mvp.py --help` 在子进程里 exit 0 且输出含 audit
     — 防止 sys.path 注入逻辑被再次删除导致 ModuleNotFoundError
  2. `... audit --as-of ... --output ...` 应走到 DB 连接阶段（exit=1 是预期，
     因为本地 pytest 容器没有 Postgres，但绝不能是 ModuleNotFoundError）
"""

import subprocess
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI = str(_REPO_ROOT / "scripts" / "ai_tuner_mvp.py")


def _run_cli(*args, timeout=8):
    """子进程跑 CLI，返回 CompletedProcess"""
    return subprocess.run(
        [sys.executable, _CLI, *args],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=timeout,
    )


def test_cli_help_runs_without_module_not_found():
    """python3 scripts/ai_tuner_mvp.py --help 应 exit 0 且输出含 audit"""
    proc = _run_cli("--help")
    assert proc.returncode == 0, (
        f"--help exit code 应为 0，实际 {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "audit" in proc.stdout, f"--help 输出应包含 audit 子命令，实际:\n{proc.stdout}"
    # 绝不能出现 ModuleNotFoundError（旧 bug：sys.path 没注入仓库根）
    assert "ModuleNotFoundError" not in proc.stderr
    assert "ModuleNotFoundError" not in proc.stdout


def test_cli_audit_reaches_db_stage_no_module_not_found():
    """
    python3 scripts/ai_tuner_mvp.py audit ... 应走到 DB 连接阶段。

    本地 pytest 环境没有 Postgres → exit 1 是预期（DB 连接失败），
    但绝不能是 ModuleNotFoundError（shared/ai_tuner 导入失败）。
    """
    proc = _run_cli(
        "audit",
        "--as-of", "2026-10-17T00:00:00Z",
        "--output", "/tmp/ai_tuner_mvp_smoke.json",
    )
    # 允许非零退出（DB 连接失败时 exit=1），但禁止 ModuleNotFoundError
    assert "ModuleNotFoundError" not in proc.stderr + proc.stdout, (
        f"audit 出现 ModuleNotFoundError — CLI 不能独立运行:\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    # 确证已走到 DB 阶段（日志含"数据库管理器初始化"或"数据库连接失败"）
    combined = proc.stdout + proc.stderr
    assert (
        "数据库" in combined
        or "database" in combined.lower()
        or proc.returncode == 0  # 万一 CI 环境有本地 DB 跑通
    ), f"未走到 DB 阶段 — 实际输出:\n{combined}"
