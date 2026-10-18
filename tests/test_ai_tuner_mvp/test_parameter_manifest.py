"""
Task2：manifest 加载 + 真实 config 路径存在性测试
"""

from pathlib import Path

import pytest
import yaml

from ai_tuner.parameters.manifest import ManifestRegistry, get_path
from ai_tuner.parameters.models import ParameterManifest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def registry() -> ManifestRegistry:
    return ManifestRegistry(ROOT / "ai_tuner/parameters/manifests")


@pytest.fixture(scope="module")
def aggressive_cfg() -> dict:
    return yaml.safe_load(
        (ROOT / "strategies/btc_eth_aggressive/config.yaml").read_text()
    )


@pytest.fixture(scope="module")
def hrs_cfg() -> dict:
    return yaml.safe_load(
        (ROOT / "strategies/hrs/config.yaml").read_text()
    )


def test_load_aggressive_manifest(registry):
    manifest = registry.load("btc_eth_aggressive")
    assert isinstance(manifest, ParameterManifest)
    assert manifest.strategy_id == "btc_eth_aggressive"
    assert manifest.max_adjustments_per_run == 2
    assert 3 <= len(manifest.parameters) <= 5


def test_load_hrs_manifest(registry):
    manifest = registry.load("hrs")
    assert manifest.strategy_id == "hrs"
    assert {spec.group for spec in manifest.parameters.values()} == {"entry_quality"}
    assert all(spec.max_change_pct == 0.10 for spec in manifest.parameters.values())


def test_aggressive_paths_exist_in_effective_config(registry, aggressive_cfg):
    """manifest 中每个路径在真实 config 上用 get_path 均能取到非 None 值"""
    manifest = registry.load("btc_eth_aggressive")
    for path in manifest.parameters:
        value = get_path(aggressive_cfg, path)
        assert value is not None, f"路径 {path} 在 btc_eth_aggressive/config.yaml 中不存在"
    # 所有路径都以 strategy.scoring. 开头（主计划约束）
    assert all(path.startswith("strategy.scoring.") for path in manifest.parameters)


def test_hrs_paths_exist_in_effective_config(registry, hrs_cfg):
    """hrs manifest 每个路径在真实 config 上均存在"""
    manifest = registry.load("hrs")
    for path in manifest.parameters:
        value = get_path(hrs_cfg, path)
        assert value is not None, f"路径 {path} 在 hrs/config.yaml 中不存在"


def test_manifest_rejects_unknown_invariant_kind():
    """加载含未知不变量类型的 manifest 应抛 ValueError"""
    import tempfile, os
    bad_yaml = """
strategy_id: bad
schema_version: "1"
max_adjustments_per_run: 1
parameters:
  a.b:
    status: tunable
    value_type: float
    min_value: 1.0
    max_value: 2.0
    max_change_pct: 0.10
    group: misc
    live_usage_probe: probe
invariants:
  - kind: nonsense
    left: a.b
    right: a.b
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "bad.yaml").write_text(bad_yaml)
        reg = ManifestRegistry(Path(tmpdir))
        with pytest.raises(ValueError) as exc:
            reg.load("bad")
        assert "未知不变量" in str(exc.value)
