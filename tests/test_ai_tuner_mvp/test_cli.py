# -*- coding: utf-8 -*-
"""功能矩阵 4：审计 CLI（scripts/ai_tuner_mvp.py）

- --help 正常
- 无 DB 时 audit 返回码 1 且打印"数据库连接失败"（不得是 ModuleNotFoundError）
- SELECT → exit 0；全候选不合格 → exit 2；报告 JSON 原子写入且字段完整
  （真实 DB 用例因本机无 Postgres 跳过，SELECT/STOP 用 FakeDB 桩替代验证）
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai_tuner.readiness.models import CandidateReadiness, ReadinessResult
from tests.test_ai_tuner_mvp.conftest import PROJECT_ROOT

CLI_PATH = PROJECT_ROOT / "scripts" / "ai_tuner_mvp.py"
NO_DB_ENV = {
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "5432",   # 本机 5432 无监听 → 连接被拒绝
    "DB_NAME": "no_such_db",
    "DB_USER": "no_such_user",
    "DB_PASSWORD": "no_such_password",
}


def run_cli(args, env_overrides=None):
    """以干净环境运行 CLI（继承 PATH，注入 DB 环境变量）"""
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith("DB_")}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, str(CLI_PATH)] + args,
        capture_output=True, text=True, env=env, timeout=60,
    )


def make_candidate(strategy_id, eligible):
    return CandidateReadiness(
        strategy_id=strategy_id,
        closed_trades=100 if eligible else 0,
        effective_signals=200 if eligible else 0,
        regime_count=5 if eligible else 0,
        complete_trade_ratio=1.0 if eligible else 0.0,
        config_identity_ratio=0.9,
        kline_coverage=1.0 if eligible else 0.0,
        external_snapshot_coverage=0.0,
        failures=[] if eligible else ["已平仓交易 0 < 20"],
        gaps=["无历史外部快照表"],
    )


def make_result(decision, selected, candidates):
    return ReadinessResult(
        as_of="2026-10-17T00:00:00+00:00",
        window_days=90,
        decision=decision,
        selected_strategy=selected,
        candidates=candidates,
    )


# ===========================================================================
# 子进程级测试（真实 CLI 入口）
# ===========================================================================


class TestCliSubprocess:
    def test_help_exits_zero(self):
        """--help 正常输出且退出码 0"""
        proc = run_cli(["--help"])
        assert proc.returncode == 0
        assert "audit" in proc.stdout
        assert "准备度审计" in proc.stdout

    def test_audit_without_db_exits_1_with_chinese_error(self, tmp_path):
        """无 DB：退出码 1，stderr 打印'数据库连接失败'，不得是 ModuleNotFoundError"""
        out_file = tmp_path / "readiness.json"
        proc = run_cli(
            ["audit", "--as-of", "2026-10-17T00:00:00Z", "--output", str(out_file)],
            env_overrides=NO_DB_ENV,
        )
        assert proc.returncode == 1
        assert "数据库连接失败" in proc.stderr
        assert "ModuleNotFoundError" not in proc.stderr
        assert not out_file.exists()  # 失败时不得写报告

    def test_audit_invalid_asof_format_exits_1(self, tmp_path):
        """--as-of 非 ISO 格式 → 退出码 1 + 格式错误提示"""
        proc = run_cli(
            ["audit", "--as-of", "not-a-date", "--output", str(tmp_path / "r.json")],
            env_overrides=NO_DB_ENV,
        )
        assert proc.returncode == 1
        assert "--as-of 格式错误" in proc.stderr

    def test_audit_missing_required_args_exits_nonzero(self):
        """缺 --as-of / --output 参数时 argparse 报错"""
        proc = run_cli(["audit"])
        assert proc.returncode != 0

    def test_script_imports_cleanly(self):
        """脚本可直接执行（sys.path 注入仓库根生效，无 ImportError）"""
        proc = subprocess.run(
            [sys.executable, "-c",
             f"import runpy; runpy.run_path(r'{CLI_PATH}', run_name='not_main')"],
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, proc.stderr


# ===========================================================================
# 函数级测试（FakeDB 桩验证 SELECT/STOP 与报告写入，替代真实 DB 用例）
# ===========================================================================


class TestRunAuditWithStub:
    @pytest.fixture
    def cli_module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("ai_tuner_mvp_cli", CLI_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    async def test_select_exits_0_and_writes_complete_report(self, cli_module, monkeypatch, tmp_path):
        """decision=SELECT → exit 0；报告 JSON 原子写入且字段完整"""
        from datetime import datetime, timezone

        result = make_result(
            "SELECT", "btc_eth_aggressive",
            {"btc_eth_aggressive": make_candidate("btc_eth_aggressive", True),
             "hrs": make_candidate("hrs", False)},
        )

        class FakeDBMgr:
            def __init__(self, **kwargs):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        class FakeAuditor:
            def __init__(self, db, config):
                pass

            async def run(self, as_of=None):
                return result

        import shared.database as shared_db
        import ai_tuner.readiness.auditor as auditor_mod

        monkeypatch.setattr(shared_db, "DatabaseManager", FakeDBMgr)
        monkeypatch.setattr(auditor_mod, "ReadinessAuditor", FakeAuditor)

        out_path = tmp_path / "nested" / "readiness.json"
        exit_code = await cli_module.run_audit(
            datetime(2026, 10, 17, tzinfo=timezone.utc), out_path
        )

        assert exit_code == 0
        assert out_path.exists()  # 父目录不存在时自动创建
        assert not out_path.with_suffix(".json.tmp").exists()  # 原子替换后 tmp 已消失

        data = json.loads(out_path.read_text("utf-8"))
        assert data["decision"] == "SELECT"
        assert data["selected_strategy"] == "btc_eth_aggressive"
        assert data["window_days"] == 90
        assert data["as_of"] == "2026-10-17T00:00:00+00:00"
        assert set(data["candidates"].keys()) == {"btc_eth_aggressive", "hrs"}
        assert data["candidates"]["hrs"]["failures"] == ["已平仓交易 0 < 20"]
        assert data["candidates"]["hrs"]["gaps"] == ["无历史外部快照表"]

    async def test_stop_exits_2_and_writes_report(self, cli_module, monkeypatch, tmp_path):
        """全候选不合格 → exit 2，报告列出失败原因"""
        from datetime import datetime, timezone

        result = make_result(
            "STOP", None,
            {"btc_eth_aggressive": make_candidate("btc_eth_aggressive", False),
             "hrs": make_candidate("hrs", False)},
        )

        class FakeDBMgr:
            def __init__(self, **kwargs):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        class FakeAuditor:
            def __init__(self, db, config):
                pass

            async def run(self, as_of=None):
                return result

        import shared.database as shared_db
        import ai_tuner.readiness.auditor as auditor_mod

        monkeypatch.setattr(shared_db, "DatabaseManager", FakeDBMgr)
        monkeypatch.setattr(auditor_mod, "ReadinessAuditor", FakeAuditor)

        out_path = tmp_path / "readiness.json"
        exit_code = await cli_module.run_audit(
            datetime(2026, 10, 17, tzinfo=timezone.utc), out_path
        )

        assert exit_code == 2
        data = json.loads(out_path.read_text("utf-8"))
        assert data["decision"] == "STOP"
        assert data["selected_strategy"] is None

    async def test_db_connect_failure_returns_1(self, cli_module, monkeypatch, tmp_path, capsys):
        """connect 抛异常 → 返回 1 且 stderr 打印'数据库连接失败'"""
        from datetime import datetime, timezone

        class BrokenDB:
            def __init__(self, **kwargs):
                pass

            async def connect(self):
                raise ConnectionRefusedError("连接被拒绝")

        import shared.database as shared_db

        monkeypatch.setattr(shared_db, "DatabaseManager", BrokenDB)

        exit_code = await cli_module.run_audit(
            datetime(2026, 10, 17, tzinfo=timezone.utc), tmp_path / "r.json"
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "数据库连接失败" in captured.err

    def test_asof_naive_gets_utc(self, cli_module, monkeypatch):
        """无时区的 --as-of 被 main() 补 UTC（真正驱动被测 main，而非本地复现逻辑）"""
        from unittest.mock import AsyncMock

        captured = {}

        async def fake_run_audit(as_of, output_path):
            captured["as_of"] = as_of
            return 0

        monkeypatch.setattr(cli_module, "run_audit", fake_run_audit)
        # 注入包含完整 audit 子命令的 argv（注意第一个元素是脚本名占位）
        monkeypatch.setattr(
            cli_module.sys, "argv",
            ["ai_tuner_mvp.py", "audit",
             "--as-of", "2026-10-17T00:00:00",  # 无时区 naive
             "--output", "/tmp/test_report.json"],
        )

        rc = cli_module.main()

        assert rc == 0
        assert captured["as_of"].tzinfo is not None
        # 确认 UTC timezone（不是别的时区）
        assert captured["as_of"].utcoffset().total_seconds() == 0

    def test_resolve_env_variants(self, cli_module, monkeypatch):
        """${VAR} / ${VAR:default} / 普通字符串三种解析分支"""
        monkeypatch.setenv("MVP_TEST_VAR", "hello")
        assert cli_module._resolve_env("${MVP_TEST_VAR}") == "hello"
        assert cli_module._resolve_env("${MVP_MISSING_VAR:fallback}") == "fallback"
        assert cli_module._resolve_env("plain") == "plain"
        assert cli_module._resolve_env({"a": ["${MVP_TEST_VAR}"]}) == {"a": ["hello"]}
