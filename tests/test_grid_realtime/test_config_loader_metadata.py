"""config_loader metadata 路径测试：同源元数据、降级口径、config_hash 脱敏。"""
import os

import yaml

from shared.config_loader import (compute_config_hash, load_strategy_config,
                                  load_strategy_config_with_metadata)


def _write(path, content):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _make_strategy(tmp_path, base=None, active=None, override=None):
    """搭建策略目录（各部分可选）。"""
    strategy_dir = tmp_path / "strat"
    strategy_dir.mkdir()
    if base is not None:
        _write(strategy_dir / "config.yaml", base)
    if active is not None or override is not None:
        override_dir = strategy_dir / "tuning_overrides"
        override_dir.mkdir()
        if active is not None:
            _write(override_dir / ".active", active)
        if override is not None:
            _write(override_dir / "V20260101.yaml", override)
    return str(strategy_dir)


def test_base_missing_returns_empty_with_none_hash(tmp_path):
    strategy_dir = _make_strategy(tmp_path)
    config, metadata = load_strategy_config_with_metadata(strategy_dir)
    assert config == {}
    assert metadata["config_hash"] is None
    assert metadata["requested_overrides_version"] is None
    assert metadata["applied_overrides_version"] is None
    assert "config.yaml" in metadata["load_error"]


def test_no_active_returns_base_with_applied_none(tmp_path):
    base = yaml.safe_dump({"a": 1})
    strategy_dir = _make_strategy(tmp_path, base=base)
    config, metadata = load_strategy_config_with_metadata(strategy_dir)
    assert config == {"a": 1}
    assert metadata["requested_overrides_version"] is None
    assert metadata["applied_overrides_version"] is None
    assert metadata["load_error"] is None
    assert metadata["config_hash"] == compute_config_hash({"a": 1})


def test_active_file_missing(tmp_path):
    base = yaml.safe_dump({"a": 1})
    strategy_dir = _make_strategy(tmp_path, base=base)
    os.mkdir(os.path.join(strategy_dir, "tuning_overrides"))
    _, metadata = load_strategy_config_with_metadata(strategy_dir)
    assert metadata["requested_overrides_version"] is None


def test_active_empty(tmp_path):
    base = yaml.safe_dump({"a": 1})
    strategy_dir = _make_strategy(tmp_path, base=base, active="  \n")
    _, metadata = load_strategy_config_with_metadata(strategy_dir)
    assert metadata["applied_overrides_version"] is None


def test_active_malformed(tmp_path):
    base = yaml.safe_dump({"a": 1})
    strategy_dir = _make_strategy(tmp_path, base=base, active="XXX")
    _, metadata = load_strategy_config_with_metadata(strategy_dir)
    assert metadata["requested_overrides_version"] is None


def test_override_file_missing_records_requested(tmp_path):
    base = yaml.safe_dump({"a": 1})
    # 只给 .active，不给 override 文件
    strategy_dir = _make_strategy(tmp_path, base=base, active="V20260101")
    config, metadata = load_strategy_config_with_metadata(strategy_dir)
    assert config == {"a": 1}
    assert metadata["requested_overrides_version"] == "V20260101"
    assert metadata["applied_overrides_version"] is None
    assert metadata["load_error"] is not None


def test_override_yaml_invalid(tmp_path):
    base = yaml.safe_dump({"a": 1})
    strategy_dir = _make_strategy(
        tmp_path, base=base, active="V20260101", override="a: [")
    _, metadata = load_strategy_config_with_metadata(strategy_dir)
    assert metadata["requested_overrides_version"] == "V20260101"
    assert metadata["applied_overrides_version"] is None


def test_override_empty_file(tmp_path):
    base = yaml.safe_dump({"a": 1})
    strategy_dir = _make_strategy(
        tmp_path, base=base, active="V20260101", override="")
    config, metadata = load_strategy_config_with_metadata(strategy_dir)
    assert config == {"a": 1}
    assert metadata["applied_overrides_version"] is None
    assert metadata["requested_overrides_version"] == "V20260101"


def test_override_top_level_not_dict(tmp_path):
    base = yaml.safe_dump({"a": 1})
    strategy_dir = _make_strategy(
        tmp_path, base=base, active="V20260101", override="- 1\n- 2")
    config, metadata = load_strategy_config_with_metadata(strategy_dir)
    assert config == {"a": 1}
    assert metadata["applied_overrides_version"] is None


def test_successful_merge_metadata(tmp_path):
    base = yaml.safe_dump({"a": 1, "nested": {"x": 1, "y": 2}})
    override = yaml.safe_dump({"nested": {"y": 9}, "b": 2})
    strategy_dir = _make_strategy(
        tmp_path, base=base, active="V20260101", override=override)
    config, metadata = load_strategy_config_with_metadata(strategy_dir)
    assert config == {"a": 1, "b": 2, "nested": {"x": 1, "y": 9}}
    assert metadata["requested_overrides_version"] == "V20260101"
    assert metadata["applied_overrides_version"] == "V20260101"
    assert metadata["load_error"] is None
    assert metadata["config_hash"] == compute_config_hash(config)


def test_legacy_loader_delegates(tmp_path):
    base = yaml.safe_dump({"a": 1})
    strategy_dir = _make_strategy(tmp_path, base=base)
    assert load_strategy_config(strategy_dir) == {"a": 1}
    merged, _ = load_strategy_config_with_metadata(strategy_dir)
    assert load_strategy_config(strategy_dir) == merged


def test_config_hash_deterministic_and_sanitized():
    config = {
        "a": 1, "nested": {"webhook_url": "http://x", "keep": 1},
        "items": [{"api_key": "k", "v": 1}, 2]}
    hash_with = compute_config_hash(config)
    # 剔除敏感键后等价配置应同哈希
    sanitized_equivalent = {"a": 1, "nested": {"keep": 1}, "items": [{"v": 1}, 2]}
    assert hash_with == compute_config_hash(sanitized_equivalent)
    # 原文仍保留敏感键（哈希不修改入参）
    assert config["nested"]["webhook_url"] == "http://x"
    # 非敏感值变化 → 哈希变化
    assert compute_config_hash({"a": 2}) != hash_with
