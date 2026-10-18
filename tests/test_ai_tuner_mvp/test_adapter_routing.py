# -*- coding: utf-8 -*-
"""base_adapter.validate_params 双契约分流测试

- 有 parameter_manifest 的适配器（btc_eth_aggressive / hrs）→ manifest 确定性校验
- 无 manifest 的适配器（btc_eth / new_coin / grid）→ 旧的白名单/红线/截断逻辑（回归）
"""

import pytest

from ai_tuner.adapters.grid_adapter import GridAdapter
from ai_tuner.adapters.hrs_adapter import HRSAdapter
from ai_tuner.adapters.mtpcs_adapter import MTPCSAdapter
from ai_tuner.adapters.mtpcs_aggressive_adapter import MTPCSAggressiveAdapter
from ai_tuner.adapters.new_coin_adapter import NewCoinAdapter


# ===========================================================================
# 新契约：manifest 适配器
# ===========================================================================


class TestManifestAdapters:
    def test_aggressive_routes_to_manifest_validation(self):
        """btc_eth_aggressive 有 parameter_manifest → 走 manifest 校验"""
        adapter = MTPCSAggressiveAdapter(db_manager=None)
        result = adapter.validate_params(
            {"strategy.scoring.min_score": {"from": 60, "to": 62}}, decision="PROPOSE"
        )
        assert result["valid"] is True
        # 新契约 validated 格式为 {path: {"from": old, "to": new}}
        assert result["validated"]["strategy.scoring.min_score"] == {"from": 60, "to": 62}

    def test_aggressive_rejects_out_of_range(self):
        adapter = MTPCSAggressiveAdapter(db_manager=None)
        result = adapter.validate_params(
            {"strategy.scoring.min_score": {"from": 60, "to": 71}}, decision="PROPOSE"
        )
        assert result["valid"] is False
        assert result["validated"] == {}

    def test_aggressive_rejects_unknown_path(self):
        adapter = MTPCSAggressiveAdapter(db_manager=None)
        result = adapter.validate_params(
            {"scoring.not_a_param": {"from": 1, "to": 2}}, decision="PROPOSE"
        )
        assert result["valid"] is False

    def test_aggressive_hold_decision_valid(self):
        """HOLD 空提案合法"""
        adapter = MTPCSAggressiveAdapter(db_manager=None)
        result = adapter.validate_params({}, decision="HOLD")
        assert result["valid"] is True

    def test_aggressive_change_rate_threshold_returns_zero(self):
        """manifest 策略的变化率由 manifest 定义，全局阈值返回 0"""
        assert MTPCSAggressiveAdapter(db_manager=None).get_change_rate_threshold() == 0.0

    def test_aggressive_whitelist_from_manifest(self):
        """manifest 策略的白名单 = manifest 的参数路径集合"""
        whitelist = MTPCSAggressiveAdapter(db_manager=None).get_param_whitelist()
        assert "strategy.scoring.min_score" in whitelist
        assert len(whitelist) == 4

    def test_aggressive_ranges_from_manifest(self):
        """manifest 策略的参数范围来自 manifest 而非 config param_ranges"""
        ranges = MTPCSAggressiveAdapter(db_manager=None).get_param_ranges()
        assert ranges["strategy.scoring.min_score"] == [55.0, 70.0]

    def test_hrs_routes_to_manifest_validation(self, hrs_config):
        """hrs 有 parameter_manifest → 走 manifest 校验"""
        adapter = HRSAdapter(db_manager=None)
        result = adapter.validate_params(
            {"scoring.entry_threshold": {"from": 6.0, "to": 6.3}}, decision="PROPOSE"
        )
        assert result["valid"] is True

    def test_hrs_invariant_violation_rejected(self):
        """hrs 双参提案违反 entry >= short_entry 被拒"""
        adapter = HRSAdapter(db_manager=None)
        result = adapter.validate_params(
            {
                "scoring.entry_threshold": {"from": 6.0, "to": 5.5},
                "scoring.short_entry_threshold": {"from": 5.5, "to": 6.04},
            },
            decision="PROPOSE",
        )
        assert result["valid"] is False
        assert any("应 >=" in e for e in result["errors"])

    def test_manifest_file_missing_returns_error(self):
        """manifest 文件未找到时返回阻断性错误而非崩溃"""
        adapter = MTPCSAggressiveAdapter(db_manager=None)
        # 绕过 cached_property，直接注入不存在的 manifest 路径
        adapter.__dict__["_strategy_cfg"] = {
            "parameter_manifest": "ai_tuner/parameters/manifests/no_such_file.yaml"
        }
        result = adapter.validate_params(
            {"strategy.scoring.min_score": {"from": 60, "to": 62}}, decision="PROPOSE"
        )
        assert result["valid"] is False
        assert any("manifest 文件未找到" in e for e in result["errors"])

    def test_manifest_cache_reused(self):
        """_load_manifest 缓存生效：二次调用返回同一对象"""
        adapter = MTPCSAggressiveAdapter(db_manager=None)
        first = adapter._load_manifest("ai_tuner/parameters/manifests/btc_eth_aggressive.yaml")
        second = adapter._load_manifest("ai_tuner/parameters/manifests/btc_eth_aggressive.yaml")
        assert first is second


# ===========================================================================
# 旧契约：legacy 适配器（回归）
# ===========================================================================


class TestLegacyAdapters:
    def test_mtpcs_valid_adjustment_passes(self):
        """btc_eth 旧 JSON 经 normalize 后的 dict 格式仍走通白名单/范围校验"""
        adapter = MTPCSAdapter(db_manager=None)
        result = adapter.validate_params({"scoring.min_score": {"from": 60, "to": 62}})
        assert result["valid"] is True
        # 旧契约 validated 格式为 {path: new_value}
        assert result["validated"]["scoring.min_score"] == 62

    def test_mtpcs_rejects_not_in_whitelist(self):
        adapter = MTPCSAdapter(db_manager=None)
        result = adapter.validate_params({"scoring.evil_param": 1})
        assert result["valid"] is False
        assert any("不在白名单" in e for e in result["errors"])

    def test_mtpcs_rejects_redline_param(self):
        """红线参数（如杠杆）直接拒绝"""
        adapter = MTPCSAdapter(db_manager=None)
        result = adapter.validate_params({"binance.leverage.S": 10})
        assert result["valid"] is False
        assert any("红线" in e for e in result["errors"])

    def test_mtpcs_clamps_to_range_with_warning(self):
        """旧契约：超范围值截断到边界并记警告（不阻断）"""
        adapter = MTPCSAdapter(db_manager=None)
        result = adapter.validate_params({"scoring.min_score": 200})
        assert result["valid"] is True
        assert result["validated"]["scoring.min_score"] == 95
        assert any("截断" in w for w in result["warnings"])

    def test_mtpcs_clamps_below_min(self):
        adapter = MTPCSAdapter(db_manager=None)
        result = adapter.validate_params({"scoring.min_score": 1})
        assert result["validated"]["scoring.min_score"] == 60

    def test_mtpcs_change_rate_warning(self):
        """旧契约：大变化率（>200%）记警告（截断后仍计算）"""
        adapter = MTPCSAdapter(db_manager=None)
        result = adapter.validate_params({"scoring.min_score": {"from": 1, "to": 95}})
        assert result["valid"] is True
        assert any("变化率" in w for w in result["warnings"])

    def test_mtpcs_missing_target_value_rejected(self):
        adapter = MTPCSAdapter(db_manager=None)
        result = adapter.validate_params({"scoring.min_score": {"from": 60}})
        assert result["valid"] is False
        assert any("缺少目标值" in e for e in result["errors"])

    def test_new_coin_legacy_regression(self):
        """new_coin 旧契约简单数值格式回归"""
        adapter = NewCoinAdapter(db_manager=None)
        result = adapter.validate_params({"scoring.entry_threshold": 5.0})
        assert result["valid"] is True
        assert result["validated"]["scoring.entry_threshold"] == 5.0

    def test_grid_legacy_regression(self):
        """grid 旧契约回归（无 manifest 策略不受 MVP 改造影响）"""
        adapter = GridAdapter(db_manager=None)
        result = adapter.validate_params({"grid.base_grid_count": {"from": 6, "to": 8}})
        assert result["valid"] is True
        assert result["validated"]["grid.base_grid_count"] == 8

    def test_grid_redline_rejected(self):
        adapter = GridAdapter(db_manager=None)
        result = adapter.validate_params({"trading.leverage": 20})
        assert result["valid"] is False

    def test_legacy_change_rate_threshold_from_config(self):
        """legacy 策略变化率阈值来自 config.yaml（btc_eth 为 2.0）"""
        assert MTPCSAdapter(db_manager=None).get_change_rate_threshold() == 2.0

    def test_legacy_whitelist_from_config(self):
        """legacy 策略白名单来自 config.yaml param_whitelist"""
        adapter = MTPCSAdapter(db_manager=None)
        assert "scoring.min_score" in adapter.get_param_whitelist()

    def test_missing_ranges_logs_warning_but_continues(self):
        """策略缺少 param_ranges 时旧契约降级为仅白名单检查（不崩溃）"""
        adapter = NewCoinAdapter(db_manager=None)
        adapter.__dict__["_strategy_cfg"] = {"param_whitelist": ["p.x"]}
        result = adapter.validate_params({"p.x": 5})
        assert result["valid"] is True
        assert result["validated"]["p.x"] == 5


# ===========================================================================
# 分流判定
# ===========================================================================


class TestRouting:
    def test_validate_params_dispatch_on_manifest_presence(self):
        """分流判定依据 parameter_manifest 字段而非策略 ID"""
        manifest_adapter = MTPCSAggressiveAdapter(db_manager=None)
        legacy_adapter = MTPCSAdapter(db_manager=None)
        manifest_path = manifest_adapter._strategy_cfg.get("parameter_manifest")
        assert manifest_path, "btc_eth_aggressive 应声明 parameter_manifest"
        assert not legacy_adapter._strategy_cfg.get("parameter_manifest")
