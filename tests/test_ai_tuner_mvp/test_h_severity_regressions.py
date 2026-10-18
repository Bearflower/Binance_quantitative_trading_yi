"""
H 严重度回归测试（真实文件系统路径，不走 Mock）

H1：BaseAdapter._read_effective_config 修复后必须能真实读到 strategies/<s>/config.yaml
H2：WeeklyJob._extract_allowed_parameters 修复后必须能真实读到 manifest 目录并注入
"""

import os
import sys
from pathlib import Path

import pytest


# 仓库根（真实路径，确保不 mock 路径拼接）
REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_aggressive_adapter():
    """构造 MTPCSAggressiveAdapter（最小化，不走 Mock）"""
    # 动态导入，避免 import 时加载整个 weekly_job 依赖链
    import importlib

    # 找到 aggressive 适配器模块
    adapter_module_path = REPO_ROOT / "ai_tuner" / "adapters" / "mtpcs_aggressive_adapter.py"
    assert adapter_module_path.exists(), f"适配器文件不存在: {adapter_module_path}"

    # 直接 import，构造实例
    mod = importlib.import_module("ai_tuner.adapters.mtpcs_aggressive_adapter")
    adapter_cls = getattr(mod, "MTPCSAggressiveAdapter")
    # config_path 是仓库根相对路径，和 config.yaml 里的声明一致
    adapter = adapter_cls(db_manager=None)
    # 强制设置 config_path 为真实值（防止类属性被改）
    adapter.config_path = "strategies/btc_eth_aggressive/config.yaml"
    return adapter


class TestH1EffectiveConfigReadable:
    """修复 H1：_read_effective_config 必须返回非空且能读到真实策略参数"""

    def test_read_effective_config_returns_dict(self):
        adapter = _make_aggressive_adapter()
        config = adapter._read_effective_config()
        assert isinstance(config, dict), "返回值必须是 dict"
        assert config, "H1 修复前恒为空 {}，修复后必须读到真实 YAML"

    def test_effective_config_contains_real_strategy_key(self):
        adapter = _make_aggressive_adapter()
        config = adapter._read_effective_config()
        # btc_eth_aggressive/config.yaml 里有 strategy: 顶级键
        assert "strategy" in config, f"应含 strategy 顶级键，实际顶级键: {list(config.keys())}"

    def test_read_effective_config_missing_path_returns_empty_dict(self):
        adapter = _make_aggressive_adapter()
        adapter.config_path = "strategies/nonexistent_xyz/config.yaml"
        config = adapter._read_effective_config()
        assert config == {}

    def test_read_effective_config_no_path_returns_empty_dict(self):
        adapter = _make_aggressive_adapter()
        adapter.config_path = ""
        config = adapter._read_effective_config()
        assert config == {}

    def test_project_root_constant_is_repo_root(self):
        from ai_tuner.adapters.base_adapter import BaseAdapter
        # 验证 _PROJECT_ROOT 实际等于仓库根（不是 ai_tuner/ 目录）
        assert BaseAdapter._PROJECT_ROOT.resolve() == REPO_ROOT.resolve(), (
            f"_PROJECT_ROOT={BaseAdapter._PROJECT_ROOT} != REPO_ROOT={REPO_ROOT}"
        )


class TestH2ManifestInjection:
    """修复 H2：_extract_allowed_parameters 必须能真实读到 manifest 并注入 Markdown"""

    def test_aggressive_gets_nonempty_allowed_parameters(self):
        """aggressive 有 manifest，应返回含 strategy.scoring.min_score 的 Markdown"""
        # 构造最小化 WeeklyJob 实例（不走完整初始化）
        from ai_tuner.scheduler.weekly_job import WeeklyTuningJob
        from unittest.mock import MagicMock, AsyncMock

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(WeeklyTuningJob, "__init__", lambda self, *a, **kw: None)
            job = WeeklyTuningJob.__new__(WeeklyTuningJob)

        # job.config 要有 strategies 列表（含 aggressive 的 parameter_manifest）
        job.config = {
            "strategies": [
                {
                    "strategy_id": "btc_eth_aggressive",
                    "parameter_manifest": "ai_tuner/parameters/manifests/btc_eth_aggressive.yaml",
                },
                {
                    "strategy_id": "btc_eth",
                    # 无 parameter_manifest
                },
            ]
        }
        # weekly_job 里 self.project_root = ai_tuner/ 目录
        # 我们验证的是 _extract_allowed_parameters 里的 manifest 加载是否用仓库根
        job.project_root = REPO_ROOT / "ai_tuner"

        # aggressive 有 manifest → 应返回非空 Markdown
        result = job._extract_allowed_parameters("btc_eth_aggressive")
        assert result, "修复 H2 前因路径错导致 FileNotFoundError 被吞，返回空字符串"
        assert "strategy.scoring.min_score" in result, (
            "返回的 Markdown 应包含真实 manifest 参数名"
        )
        assert "不变量" in result or "invariant" in result.lower(), (
            "应包含不变量说明"
        )

    def test_legacy_strategy_without_manifest_returns_empty(self):
        """btc_eth 无 parameter_manifest → 返回空字符串"""
        from ai_tuner.scheduler.weekly_job import WeeklyTuningJob

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(WeeklyTuningJob, "__init__", lambda self, *a, **kw: None)
            job = WeeklyTuningJob.__new__(WeeklyTuningJob)

        job.config = {
            "strategies": [
                {"strategy_id": "btc_eth"},  # 无 parameter_manifest
            ]
        }
        job.project_root = REPO_ROOT / "ai_tuner"

        result = job._extract_allowed_parameters("btc_eth")
        assert result == "", f"无 manifest 的策略应返回空字符串，实际: {result!r}"

    def test_manifest_path_does_not_duplicate_ai_tuner(self):
        """manifest 目录路径不应出现 ai_tuner/ai_tuner/parameters/manifests（H2 前的 bug）"""
        from ai_tuner.parameters.manifest import ManifestRegistry

        # 正确路径 = 仓库根 / ai_tuner / parameters / manifests
        correct_dir = REPO_ROOT / "ai_tuner" / "parameters" / "manifests"
        # H2 bug 路径 = ai_tuner/ai_tuner/parameters/manifests（不存在）
        buggy_dir = REPO_ROOT / "ai_tuner" / "ai_tuner" / "parameters" / "manifests"

        assert correct_dir.is_dir(), f"正确 manifest 目录不存在: {correct_dir}"
        assert not buggy_dir.exists(), (
            f"buggy 路径不应该存在（若存在说明别处硬编码了错误路径）: {buggy_dir}"
        )
