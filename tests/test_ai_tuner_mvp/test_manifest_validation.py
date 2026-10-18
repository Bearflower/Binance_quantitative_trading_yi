# -*- coding: utf-8 -*-
"""功能矩阵 2：提案确定性校验（manifest 路径）

- 真实 manifest（btc_eth_aggressive / hrs）+ 真实策略配置验证合法路径与各拒绝分支
- 内存构造 manifest 覆盖真实数据触不到的分支（redline / int / 0 值基线等）
- 覆盖 manifest.py 全部纯函数分支与 models.py 的类型方法
"""

from copy import deepcopy

import pytest
import yaml

from ai_tuner.parameters.manifest import (
    ManifestRegistry,
    get_path,
    set_path,
    validate_invariants,
    validate_proposal,
)
from ai_tuner.parameters.models import (
    ParameterManifest,
    ParameterSpec,
    ParameterStatus,
    ValidatedProposal,
    ValueType,
)
from tests.test_ai_tuner_mvp.conftest import MANIFEST_DIR, PROJECT_ROOT


# ---------------------------------------------------------------------------
# 内存 manifest 构造 helper（覆盖真实 manifest 无法触发的分支）
# ---------------------------------------------------------------------------


def build_spec(
    path,
    status="tunable",
    value_type="float",
    min_value=0.0,
    max_value=100.0,
    max_change_pct=0.10,
):
    """构造单个参数规格"""
    return ParameterSpec(
        path=path,
        status=ParameterStatus(status),
        value_type=ValueType(value_type),
        min_value=float(min_value),
        max_value=float(max_value),
        max_change_pct=float(max_change_pct),
        group="entry_quality",
        live_usage_probe="probe",
    )


def build_manifest(params, invariants=None, max_adjustments=2):
    """构造内存 manifest"""
    return ParameterManifest(
        strategy_id="fake",
        schema_version="1",
        max_adjustments_per_run=max_adjustments,
        parameters=params,
        invariants=invariants or [],
    )


@pytest.fixture
def fake_manifest():
    """内存 manifest：p.a ∈ [0,100]，p.b ∈ [0,100]，a > b 不变量"""
    return build_manifest(
        {"p.a": build_spec("p.a", min_value=0.0, max_value=100.0),
         "p.b": build_spec("p.b", min_value=0.0, max_value=100.0)},
        invariants=[{"kind": "greater_than", "left": "p.a", "right": "p.b"}],
    )


# ===========================================================================
# ManifestRegistry 加载真实 manifest
# ===========================================================================


class TestManifestLoading:
    def test_load_aggressive_manifest(self, aggressive_manifest):
        """btc_eth_aggressive manifest 加载成功且字段完整"""
        m = aggressive_manifest
        assert m.strategy_id == "btc_eth_aggressive"
        assert m.schema_version == "1"
        assert m.max_adjustments_per_run == 2
        expected_paths = {
            "strategy.scoring.min_score",
            "strategy.scoring.trend_strength.adx_strong_threshold",
            "strategy.scoring.trend_strength.adx_medium_threshold",
            "strategy.scoring.breakout_pullback.proximity_pct",
        }
        assert set(m.parameters.keys()) == expected_paths
        spec = m.parameters["strategy.scoring.min_score"]
        assert spec.can_adjust is True
        assert spec.min_value == 55.0 and spec.max_value == 70.0
        assert spec.max_change_pct == 0.10
        assert spec.value_type is ValueType.FLOAT

    def test_load_hrs_manifest(self, hrs_manifest):
        """hrs manifest 加载成功且不变量为 greater_than_or_equal"""
        m = hrs_manifest
        assert m.strategy_id == "hrs"
        assert "scoring.entry_threshold" in m.parameters
        kinds = {inv["kind"] for inv in m.invariants}
        assert kinds == {"greater_than_or_equal"}

    def test_load_missing_manifest_raises(self, tmp_path):
        """manifest 文件不存在时抛 FileNotFoundError"""
        with pytest.raises(FileNotFoundError):
            ManifestRegistry(tmp_path).load("no_such_strategy")

    def test_load_unknown_invariant_kind_rejected(self, tmp_path):
        """manifest 含未知不变量类型时直接拒绝加载"""
        raw = {
            "strategy_id": "fake",
            "schema_version": "1",
            "max_adjustments_per_run": 1,
            "parameters": {
                "p.a": {
                    "status": "tunable", "value_type": "float",
                    "min_value": 0, "max_value": 10, "max_change_pct": 0.1,
                    "group": "g", "live_usage_probe": "probe",
                }
            },
            "invariants": [{"kind": "equal", "left": "p.a", "right": "p.b"}],
        }
        (tmp_path / "fake.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
        with pytest.raises(ValueError, match="未知不变量类型"):
            ManifestRegistry(tmp_path).load("fake")

    def test_load_aggressive_yaml_on_disk_matches_registry(self):
        """仓库内 manifest YAML 与注册表加载结果一致（防文件漂移）"""
        with open(MANIFEST_DIR / "btc_eth_aggressive.yaml", "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        m = ManifestRegistry(MANIFEST_DIR).load("btc_eth_aggressive")
        assert set(raw["parameters"].keys()) == set(m.parameters.keys())


# ===========================================================================
# get_path / set_path 路径工具
# ===========================================================================


class TestPathTools:
    def test_get_path_nested(self):
        config = {"a": {"b": {"c": 1}}}
        assert get_path(config, "a.b.c") == 1

    def test_get_path_missing_leaf(self):
        assert get_path({"a": {"b": 1}}, "a.b.c") is None

    def test_get_path_missing_middle(self):
        assert get_path({"a": 1}, "a.b.c") is None

    def test_get_path_non_mapping_middle(self):
        """中间层不是字典时返回 None 而非抛错"""
        assert get_path({"a": [1, 2]}, "a.b") is None

    def test_set_path_creates_intermediate(self):
        config = {}
        set_path(config, "a.b.c", 5)
        assert config == {"a": {"b": {"c": 5}}}

    def test_set_path_overwrites_non_dict_intermediate(self):
        """中间层被非字典占用时自动替换为字典"""
        config = {"a": 42}
        set_path(config, "a.b", 1)
        assert config == {"a": {"b": 1}}

    def test_set_path_overwrites_leaf(self):
        config = {"a": {"b": 1}}
        set_path(config, "a.b", 2)
        assert config["a"]["b"] == 2


# ===========================================================================
# validate_invariants
# ===========================================================================


class TestValidateInvariants:
    def test_unknown_kind_reported(self, fake_manifest):
        fake_manifest.invariants.append({"kind": "equal", "left": "p.a", "right": "p.b"})
        errors = validate_invariants(fake_manifest, {"p": {"a": 1, "b": 0}})
        assert any("未知不变量类型" in e for e in errors)

    def test_missing_value_reported(self):
        m = build_manifest(
            {"p.a": build_spec("p.a")},
            invariants=[{"kind": "greater_than", "left": "p.a", "right": "p.missing"}],
        )
        errors = validate_invariants(m, {"p": {"a": 1}})
        assert any("缺少值" in e for e in errors)

    def test_greater_than_violation(self):
        m = build_manifest(
            {},
            invariants=[{"kind": "greater_than", "left": "p.a", "right": "p.b"}],
        )
        errors = validate_invariants(m, {"p": {"a": 1, "b": 1}})
        assert any("应 >" in e for e in errors)

    def test_greater_than_pass(self):
        m = build_manifest(
            {},
            invariants=[{"kind": "greater_than", "left": "p.a", "right": "p.b"}],
        )
        assert validate_invariants(m, {"p": {"a": 2, "b": 1}}) == []

    def test_greater_than_or_equal_violation(self):
        m = build_manifest(
            {},
            invariants=[{"kind": "greater_than_or_equal", "left": "p.a", "right": "p.b"}],
        )
        errors = validate_invariants(m, {"p": {"a": 0, "b": 1}})
        assert any("应 >=" in e for e in errors)

    def test_greater_than_or_equal_boundary_pass(self):
        """等于边界时 greater_than_or_equal 通过"""
        m = build_manifest(
            {},
            invariants=[{"kind": "greater_than_or_equal", "left": "p.a", "right": "p.b"}],
        )
        assert validate_invariants(m, {"p": {"a": 1, "b": 1}}) == []


# ===========================================================================
# ParameterSpec.coerce 与状态
# ===========================================================================


class TestParameterSpec:
    def test_can_adjust_tunable_true(self):
        assert build_spec("p", status="tunable").can_adjust is True

    @pytest.mark.parametrize("status", ["redline", "shadow", "dead", "derived"])
    def test_can_adjust_non_tunable_false(self, status):
        """非 tunable 状态一律不允许调整"""
        assert build_spec("p", status=status).can_adjust is False

    def test_coerce_float(self):
        assert build_spec("p", value_type="float").coerce(3) == 3.0

    def test_coerce_int(self):
        assert build_spec("p", value_type="int").coerce(3.0) == 3
        assert isinstance(build_spec("p", value_type="int").coerce(3.0), int)

    def test_coerce_int_rejects_fraction(self):
        with pytest.raises(ValueError, match="需要整数"):
            build_spec("p", value_type="int").coerce(1.5)

    def test_coerce_rejects_bool(self):
        """bool 是 int 子类，必须显式拦截"""
        with pytest.raises(ValueError, match="boolean"):
            build_spec("p").coerce(True)

    def test_coerce_rejects_non_numeric(self):
        for bad in ("abc", None, [1], {"a": 1}):
            with pytest.raises(ValueError):
                build_spec("p").coerce(bad)

    def test_validated_proposal_valid_property(self):
        ok = ValidatedProposal("PROPOSE", {}, [], ["警告不影响"])
        bad = ValidatedProposal("PROPOSE", {}, ["错误"], [])
        assert ok.valid is True
        assert bad.valid is False


# ===========================================================================
# validate_proposal：真实 manifest + 真实策略配置
# ===========================================================================


class TestValidateProposalRealManifests:
    """真实参数路径 + 真实当前值（min_score=60, adx_strong=25, adx_medium=20, proximity=0.01）"""

    def test_aggressive_min_score_accepted(self, aggressive_manifest, aggressive_config):
        """strategy.scoring.min_score: 60 → 62 合法（3.3% < 10%）"""
        result = validate_proposal(
            aggressive_manifest, aggressive_config, "PROPOSE",
            {"strategy.scoring.min_score": {"from": 60, "to": 62}},
        )
        assert result.valid is True
        assert result.adjustments["strategy.scoring.min_score"]["to"] == 62.0
        assert isinstance(result.adjustments["strategy.scoring.min_score"]["to"], float)

    def test_aggressive_adx_pair_accepted(self, aggressive_manifest, aggressive_config):
        """adx_strong 25→26 + adx_medium 20→21 合法且不变量仍满足"""
        result = validate_proposal(
            aggressive_manifest, aggressive_config, "PROPOSE",
            {
                "strategy.scoring.trend_strength.adx_strong_threshold": {"from": 25, "to": 26},
                "strategy.scoring.trend_strength.adx_medium_threshold": {"from": 20, "to": 21},
            },
        )
        assert result.valid is True

    def test_aggressive_proximity_pct_accepted(self, aggressive_manifest, aggressive_config):
        """breakout_pullback.proximity_pct: 0.01 → 0.0105 合法（5% < 10%）"""
        result = validate_proposal(
            aggressive_manifest, aggressive_config, "PROPOSE",
            {"strategy.scoring.breakout_pullback.proximity_pct": {"from": 0.01, "to": 0.0105}},
        )
        assert result.valid is True

    def test_aggressive_unknown_path_rejected(self, aggressive_manifest, aggressive_config):
        result = validate_proposal(
            aggressive_manifest, aggressive_config, "PROPOSE",
            {"strategy.scoring.min_score_typo": {"from": 60, "to": 62}},
        )
        assert result.valid is False
        assert any("不在 manifest 中" in e for e in result.errors)

    def test_aggressive_to_above_max_rejected(self, aggressive_manifest, aggressive_config):
        """to=71 超出 [55,70] 被拒"""
        result = validate_proposal(
            aggressive_manifest, aggressive_config, "PROPOSE",
            {"strategy.scoring.min_score": {"from": 60, "to": 71}},
        )
        assert result.valid is False
        assert any("超出范围" in e for e in result.errors)

    def test_aggressive_to_below_min_rejected(self, aggressive_manifest, aggressive_config):
        """to=54 低于下限 55 被拒"""
        result = validate_proposal(
            aggressive_manifest, aggressive_config, "PROPOSE",
            {"strategy.scoring.min_score": {"from": 60, "to": 54}},
        )
        assert result.valid is False
        assert any("超出范围" in e for e in result.errors)

    def test_aggressive_change_rate_exceeded_rejected(self, aggressive_manifest, aggressive_config):
        """60 → 66.1（10.17% > 10%）被拒"""
        result = validate_proposal(
            aggressive_manifest, aggressive_config, "PROPOSE",
            {"strategy.scoring.min_score": {"from": 60, "to": 66.1}},
        )
        assert result.valid is False
        assert any("超过上限" in e for e in result.errors)

    def test_aggressive_change_rate_exact_boundary_accepted(self, aggressive_manifest, aggressive_config):
        """60 → 66 恰好 10%，落在容差内应通过"""
        result = validate_proposal(
            aggressive_manifest, aggressive_config, "PROPOSE",
            {"strategy.scoring.min_score": {"from": 60, "to": 66}},
        )
        assert result.valid is True

    def test_aggressive_from_mismatch_rejected(self, aggressive_manifest, aggressive_config):
        """from=59 与生效值 60 不匹配被拒（防 LLM 捏造基线）"""
        result = validate_proposal(
            aggressive_manifest, aggressive_config, "PROPOSE",
            {"strategy.scoring.min_score": {"from": 59, "to": 62}},
        )
        assert result.valid is False
        assert any("不匹配" in e for e in result.errors)

    def test_aggressive_bool_value_rejected(self, aggressive_manifest, aggressive_config):
        result = validate_proposal(
            aggressive_manifest, aggressive_config, "PROPOSE",
            {"strategy.scoring.min_score": {"from": True, "to": 62}},
        )
        assert result.valid is False
        assert any("值类型错误" in e for e in result.errors)

    def test_hrs_entry_threshold_accepted(self, hrs_manifest, hrs_config):
        """scoring.entry_threshold: 6.0 → 6.3 合法"""
        result = validate_proposal(
            hrs_manifest, hrs_config, "PROPOSE",
            {"scoring.entry_threshold": {"from": 6.0, "to": 6.3}},
        )
        assert result.valid is True
        assert result.adjustments["scoring.entry_threshold"]["to"] == 6.3

    def test_hrs_invariant_violation_rejected(self, hrs_manifest, hrs_config):
        """entry 6.0→5.5 + short_entry 5.5→6.04 触发 entry >= short_entry 违反"""
        result = validate_proposal(
            hrs_manifest, hrs_config, "PROPOSE",
            {
                "scoring.entry_threshold": {"from": 6.0, "to": 5.5},
                "scoring.short_entry_threshold": {"from": 5.5, "to": 6.04},
            },
        )
        assert result.valid is False
        assert any("应 >=" in e for e in result.errors)
        assert result.adjustments == {}

    def test_hrs_cross_invariant_pair_accepted(self, hrs_manifest, hrs_config):
        """entry 6.0→6.2 + short 5.5→5.6：单参合法且不变量满足"""
        result = validate_proposal(
            hrs_manifest, hrs_config, "PROPOSE",
            {
                "scoring.entry_threshold": {"from": 6.0, "to": 6.2},
                "scoring.short_entry_threshold": {"from": 5.5, "to": 5.6},
            },
        )
        assert result.valid is True

    def test_single_bad_param_blocks_whole_proposal(self, aggressive_manifest, aggressive_config):
        """严格安全：任何单参数错误都阻断整份提案（validated 为空）"""
        result = validate_proposal(
            aggressive_manifest, aggressive_config, "PROPOSE",
            {
                "strategy.scoring.min_score": {"from": 60, "to": 62},       # 合法
                "strategy.scoring.breakout_pullback.proximity_pct": {"from": 0.01, "to": 99},  # 超范围
            },
        )
        assert result.valid is False
        assert result.adjustments == {}

    def test_empty_effective_config_path_missing(self, aggressive_manifest):
        """配置为空时路径不存在被拒"""
        result = validate_proposal(
            aggressive_manifest, {}, "PROPOSE",
            {"strategy.scoring.min_score": {"from": 60, "to": 62}},
        )
        assert result.valid is False
        assert any("在当前配置中不存在" in e for e in result.errors)

    def test_actual_value_non_numeric_rejected(self, aggressive_manifest):
        """当前值是非数值字符串时拒绝（无法校验 from）"""
        config = {"strategy": {"scoring": {"min_score": "abc"}}}
        result = validate_proposal(
            aggressive_manifest, config, "PROPOSE",
            {"strategy.scoring.min_score": {"from": 60, "to": 62}},
        )
        assert result.valid is False
        assert any("非数值" in e for e in result.errors)

    def test_zero_baseline_rejects_any_change(self, fake_manifest):
        """边界：当前值为 0 时变化率分母为 0 → 任何变化都超限"""
        config = {"p": {"a": 0, "b": 0}}
        result = validate_proposal(
            fake_manifest, config, "PROPOSE",
            {"p.a": {"from": 0, "to": 1}},
        )
        assert result.valid is False
        assert any("超过上限" in e for e in result.errors)

    def test_no_invariant_check_when_single_param_errors(self, fake_manifest):
        """存在单参数错误时跳过不变量校验（错误不重复叠加）"""
        config = {"p": {"a": 10, "b": 10}}
        result = validate_proposal(
            fake_manifest, config, "PROPOSE",
            {"p.a": {"from": 10, "to": 200}},  # 超范围，同时 candidate 会违反 a>b
        )
        assert result.valid is False
        assert not any("不变量" in e for e in result.errors)

    def test_invariant_error_also_clears_accepted(self, fake_manifest):
        """不变量违反时最终 adjustments 清空"""
        config = {"p": {"a": 10, "b": 10}}
        result = validate_proposal(
            fake_manifest, config, "PROPOSE",
            {"p.b": {"from": 10, "to": 20}},  # 单参合法，但 a=10 > b=20 违反不变量
        )
        assert result.valid is False
        assert result.adjustments == {}

    def test_redline_status_rejected(self, fake_manifest):
        """非 tunable（redline）参数即使路径存在也被拒"""
        manifest = build_manifest({"p.a": build_spec("p.a", status="redline")})
        config = {"p": {"a": 10}}
        result = validate_proposal(
            manifest, config, "PROPOSE", {"p.a": {"from": 10, "to": 11}},
        )
        assert result.valid is False
        assert any("不在 manifest 中或不可调整" in e for e in result.errors)

    def test_int_type_coerced(self):
        """int 参数通过校验后目标值为 int"""
        manifest = build_manifest({"p.n": build_spec("p.n", value_type="int",
                                                    min_value=1, max_value=10,
                                                    max_change_pct=0.5)})
        config = {"p": {"n": 4}}
        result = validate_proposal(manifest, config, "PROPOSE", {"p.n": {"from": 4, "to": 5}})
        assert result.valid is True
        assert result.adjustments["p.n"]["to"] == 5
        assert isinstance(result.adjustments["p.n"]["to"], int)

    def test_int_type_rejects_fraction(self):
        manifest = build_manifest({"p.n": build_spec("p.n", value_type="int",
                                                    min_value=1, max_value=10,
                                                    max_change_pct=0.5)})
        result = validate_proposal(
            manifest, {"p": {"n": 4}}, "PROPOSE", {"p.n": {"from": 4, "to": 4.5}},
        )
        assert result.valid is False
        assert any("需要整数" in e for e in result.errors)

    def test_deepcopy_isolation(self, aggressive_manifest, aggressive_config):
        """校验不得修改传入的 effective_config（deepcopy 隔离）"""
        original = deepcopy(aggressive_config)
        validate_proposal(
            aggressive_manifest, aggressive_config, "PROPOSE",
            {"strategy.scoring.min_score": {"from": 60, "to": 62}},
        )
        assert aggressive_config == original


# ===========================================================================
# validate_proposal：decision 分支
# ===========================================================================


class TestValidateProposalDecisions:
    def test_hold_without_adjustments_valid(self, fake_manifest):
        result = validate_proposal(fake_manifest, {}, "HOLD", {})
        assert result.valid is True
        assert result.adjustments == {}
        assert result.errors == []

    def test_data_error_without_adjustments_valid(self, fake_manifest):
        result = validate_proposal(fake_manifest, {}, "DATA_ERROR", {})
        assert result.valid is True

    def test_hold_with_adjustments_rejected(self, fake_manifest):
        result = validate_proposal(fake_manifest, {}, "HOLD", {"p.a": {"from": 1, "to": 2}})
        assert result.valid is False
        assert any("HOLD 不得携带" in e for e in result.errors)

    def test_data_error_with_adjustments_rejected(self, fake_manifest):
        result = validate_proposal(fake_manifest, {}, "DATA_ERROR", {"p.a": {"from": 1, "to": 2}})
        assert result.valid is False
        assert any("DATA_ERROR 不得携带" in e for e in result.errors)

    def test_unknown_decision_rejected(self, fake_manifest):
        result = validate_proposal(fake_manifest, {}, "APPLY", {})
        assert result.valid is False
        assert any("不支持的 decision" in e for e in result.errors)

    def test_propose_empty_adjustments_rejected(self, fake_manifest):
        result = validate_proposal(fake_manifest, {}, "PROPOSE", {})
        assert result.valid is False
        assert any("至少 1 个" in e for e in result.errors)

    def test_propose_exceeds_max_adjustments_rejected(self):
        """超过 max_adjustments_per_run 被拒"""
        manifest = build_manifest(
            {"p.a": build_spec("p.a"), "p.b": build_spec("p.b")},
            max_adjustments=1,
        )
        result = validate_proposal(
            manifest, {"p": {"a": 1, "b": 1}}, "PROPOSE",
            {"p.a": {"from": 1, "to": 2}, "p.b": {"from": 1, "to": 2}},
        )
        assert result.valid is False
        assert any("最多允许 1 个" in e for e in result.errors)
