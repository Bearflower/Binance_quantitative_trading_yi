"""配置加载与校验测试：禁硬编码/缺项拒绝（coding-standards + §9 校验精神）。"""
from pathlib import Path

import pytest
import yaml

from services.aggtrade_collector.config import load_config, resolve_base_dir

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_default_config_loads_and_paths_resolve():
    cfg = load_config()
    assert cfg.symbol == "ETHUSDT"
    assert cfg.page_limit == 1000
    assert cfg.slice_hours == 1
    assert cfg.agg_trades_retention_hours == 72
    assert cfg.insert_batch_size == 2000
    assert cfg.rate_limit_wait_seconds == 60
    assert cfg.rate_limit_max_wait_seconds == 120
    assert cfg.page_sleep_min_seconds < cfg.page_sleep_max_seconds
    assert cfg.validation_start_beijing == "2026-10-08T00:00:00+08:00"
    assert cfg.validation_min_full_days == 30
    assert cfg.daemon_incremental_interval_seconds == 10
    assert cfg.daemon_cleanup_interval_seconds == 3600
    assert cfg.db_path.is_absolute()
    assert cfg.db_path.name == "ethusdt_aggtrades.sqlite"
    # 固定指纹
    assert len(cfg.sample_sha256) == 64


def test_base_dir_overrides_relative_paths(tmp_path):
    cfg = load_config(base_dir=str(tmp_path))
    assert cfg.db_path == tmp_path / "data/aggtrades/ethusdt_aggtrades.sqlite"
    assert cfg.sample_path == tmp_path / "ethusdt_aggtrades_20261007_0950_1010.json"


def test_resolve_base_dir_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("AGGTRADE_BASE_DIR", str(tmp_path / "envdir"))
    assert resolve_base_dir() == (tmp_path / "envdir").resolve()
    assert resolve_base_dir(str(tmp_path / "cli")) == (tmp_path / "cli").resolve()


def _write_bad_config(tmp_path, mutator):
    path = Path(__file__).resolve().parents[2] / \
        "services/aggtrade_collector/config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutator(raw)
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return bad


@pytest.mark.parametrize("mutator", [
    lambda r: r["rest"].update(page_limit=5000),       # 超币安上限
    lambda r: r["rest"].update(slice_hours=2),         # 窗口超 1h
    lambda r: r["storage"].update(agg_trades_retention_hours=0),
    lambda r: r["storage"].update(insert_batch_size=0),
    lambda r: r["rest"].update(max_retries=-1),
    lambda r: r.update(symbol=""),
    lambda r: r["rest"].update(page_sleep_min_seconds=0.5,
                               page_sleep_max_seconds=0.1),
    lambda r: r["rest"].update(rate_limit_wait_seconds=0),
    lambda r: r["rest"].update(rate_limit_wait_seconds=200,
                               rate_limit_max_wait_seconds=60),
    lambda r: r["sample_file"].update(sha256=""),   # 固定指纹缺失
    lambda r: r["validation_segment"].update(start_rule_beijing="  "),
    lambda r: r["validation_segment"].update(min_full_days=0),
    lambda r: r["daemon"].update(incremental_interval_seconds=0),
    lambda r: r["daemon"].update(incremental_interval_seconds=60,
                                 cleanup_interval_seconds=10),  # 清理周期短于增量周期
])
def test_invalid_config_rejected(tmp_path, mutator):
    bad = _write_bad_config(tmp_path, mutator)
    with pytest.raises(ValueError):
        load_config(str(bad), base_dir=str(tmp_path))


def test_repo_root_constant_matches_layout():
    """仓库根定位常量必须指向真实仓库（含 requirements.txt）。"""
    assert (_REPO_ROOT / "requirements.txt").exists()
