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


# ===========================================================================
# kline-audit 子命令测试（TDD：先写失败测试）
# ===========================================================================


class TestRunKlineAuditWithStub:
    """kline-audit 子命令函数级测试：FakeDB + monkeypatch compute_kline_coverage"""

    REQUIRED_TOP_KEYS = {
        "strategy_id", "window", "weighted_coverage",
        "symbols", "missing_ranges", "recommended_commands",
    }

    @pytest.fixture
    def cli_module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("ai_tuner_mvp_cli", CLI_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    # ------------------------------------------------------------------
    # Test 1: 输出 JSON 六个固定键齐全 + recommended_commands 格式
    # ------------------------------------------------------------------
    async def test_kline_audit_emits_required_fields(self, cli_module, monkeypatch, tmp_path):
        """kline-audit 输出 JSON 含六个固定键，window 三键齐全且 days 来自配置，
        missing_ranges 时间以 Z 结尾，recommended_commands 含四参数"""
        from datetime import datetime, timedelta, timezone
        from ai_tuner.readiness.kline_coverage import (
            KlineCoverageResult, SymbolCoverage,
        )

        # --- 构造假的 KlineCoverageResult：1 个 symbol 有 1 条缺失 ---
        missing_start = datetime(2026, 8, 1, tzinfo=timezone.utc).replace(tzinfo=None)
        missing_end = missing_start + timedelta(hours=24)
        sym_row = SymbolCoverage(
            symbol="BTCUSDT", interval="1h",
            expected=2160, actual=2000,
            missing_ranges=((missing_start, missing_end),),
            gap=None,
        )
        fake_result = KlineCoverageResult(
            weighted_coverage=2000 / 2160,
            symbols=(sym_row,),
            evidence_start=missing_start,
            evidence_end=missing_end,
            gaps=(),
        )

        # --- monkeypatch DatabaseManager + compute_kline_coverage ---
        class FakeDBMgr:
            def __init__(self, **kwargs):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        import shared.database as shared_db
        import ai_tuner.readiness.kline_coverage as kc_mod

        monkeypatch.setattr(shared_db, "DatabaseManager", FakeDBMgr)

        async def fake_compute(*args, **kwargs):
            return fake_result

        monkeypatch.setattr(kc_mod, "compute_kline_coverage", fake_compute)

        # --- 调用被测函数 ---
        as_of = datetime(2026, 10, 21, tzinfo=timezone.utc)
        out_path = tmp_path / "kline-audit.json"
        exit_code = await cli_module.run_kline_audit(
            "btc_eth_aggressive", as_of, out_path
        )
        assert exit_code == 0

        # --- 断言六个固定键齐全 ---
        data = json.loads(out_path.read_text("utf-8"))
        assert self.REQUIRED_TOP_KEYS.issubset(data.keys()), (
            f"缺失键: {self.REQUIRED_TOP_KEYS - data.keys()}"
        )
        # gaps 可选键：若存在必须是 list
        if "gaps" in data:
            assert isinstance(data["gaps"], list)

        # --- window 三键齐全，days 从配置读 ---
        assert {"start", "end", "days"}.issubset(data["window"].keys())
        from tests.test_ai_tuner_mvp.conftest import PROJECT_ROOT as _PR
        import yaml as _yaml
        with open(_PR / "ai_tuner" / "config.yaml", "r", encoding="utf-8") as f:
            _cfg = _yaml.safe_load(f)
        expected_days = _cfg["purpose_driven_mvp"]["readiness"]["window_days"]
        assert data["window"]["days"] == expected_days

        # --- missing_ranges 跨 symbol 展平，时间以 Z 结尾 ---
        assert len(data["missing_ranges"]) == 1
        mr = data["missing_ranges"][0]
        assert mr["symbol"] == "BTCUSDT"
        assert mr["interval"] == "1h"
        assert mr["start"].endswith("Z")
        assert mr["end"].endswith("Z")

        # --- recommended_commands 包含四参数 ---
        assert len(data["recommended_commands"]) == 1
        cmd = data["recommended_commands"][0]
        assert "--symbols BTCUSDT" in cmd
        assert "--intervals 1h" in cmd
        assert "--start" in cmd
        assert "--end" in cmd

    # ------------------------------------------------------------------
    # Test 2: 只读铁律 —— DB 桩无 execute，源码静态断言无写库/采集痕迹
    # ------------------------------------------------------------------
    async def test_kline_audit_is_read_only(self, cli_module, monkeypatch):
        """kline-audit 只允许 SELECT/fetch_*，execute 被禁止；
        静态断言源码不含写 SQL / subprocess / fetch_history_bulk"""
        import shared.database as shared_db

        class ReadOnlyDBMgr:
            """只读 DB 桩：fetch_one/fetch_all 返回 None/[]（fail-closed），
            execute 方法不存在让调用方根本无法触发写路径"""
            def __init__(self, **kwargs):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

            async def fetch_one(self, sql, *args):
                return None

            async def fetch_all(self, sql, *args):
                return []

        monkeypatch.setattr(shared_db, "DatabaseManager", ReadOnlyDBMgr)

        from datetime import datetime, timezone
        as_of = datetime(2026, 10, 21, tzinfo=timezone.utc)
        exit_code = await cli_module.run_kline_audit(
            "btc_eth_aggressive", as_of, None
        )
        # 退出码 0（只打印摘要）或 1（数据库连接失败）均可；核心是不抛异常
        assert exit_code in (0, 1)

        # --- 静态断言：源码不含写库/采集痕迹 ---
        src = CLI_PATH.read_text("utf-8")
        # 真正危险的痕迹：写 SQL / 启动外部进程 / 导入采集脚本 / 调用 execute
        forbidden = [
            "subprocess",
            "import fetch_history_bulk",
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            ".execute(",
        ]
        for token in forbidden:
            assert token not in src, f"源码中禁止出现 {token!r}"

    # ------------------------------------------------------------------
    # Test 3: 原子写入 —— 父目录创建 / tmp 消失 / JSON 合法
    # ------------------------------------------------------------------
    async def test_kline_audit_writes_output_file(self, cli_module, monkeypatch, tmp_path):
        """传入嵌套路径，断言父目录创建、tmp 消失、JSON 合法、weighted_coverage 是 float"""
        from datetime import datetime, timezone
        from ai_tuner.readiness.kline_coverage import (
            KlineCoverageResult, SymbolCoverage,
        )

        sym_row = SymbolCoverage(
            symbol="BTCUSDT", interval="1h",
            expected=2160, actual=2160,
            missing_ranges=(),
            gap=None,
        )
        fake_result = KlineCoverageResult(
            weighted_coverage=1.0,
            symbols=(sym_row,),
            evidence_start=None,
            evidence_end=None,
            gaps=(),
        )

        class FakeDBMgr:
            def __init__(self, **kwargs):
                pass

            async def connect(self):
                pass

            async def disconnect(self):
                pass

        import shared.database as shared_db
        import ai_tuner.readiness.kline_coverage as kc_mod

        monkeypatch.setattr(shared_db, "DatabaseManager", FakeDBMgr)

        async def fake_compute(*args, **kwargs):
            return fake_result

        monkeypatch.setattr(kc_mod, "compute_kline_coverage", fake_compute)

        nested = tmp_path / "nested" / "deep"
        out_path = nested / "kline-audit.json"
        # 父目录 nested/deep 不存在，run_kline_audit 应自动创建
        assert not nested.exists()

        exit_code = await cli_module.run_kline_audit(
            "btc_eth_aggressive",
            datetime(2026, 10, 21, tzinfo=timezone.utc),
            out_path,
        )
        assert exit_code == 0
        # 父目录已创建
        assert nested.exists()
        # 文件存在
        assert out_path.exists()
        # tmp 后缀已消失（原子替换完成）
        assert not out_path.with_suffix(".json.tmp").exists()
        # 合法 JSON
        data = json.loads(out_path.read_text("utf-8"))
        assert isinstance(data["weighted_coverage"], float)
