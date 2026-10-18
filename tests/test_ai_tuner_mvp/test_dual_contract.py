# -*- coding: utf-8 -*-
"""功能矩阵 1：双契约分流（response_parser.parse_response 的 contract_mode）

- new：合法 8 字段信封通过；缺字段 / objective 不匹配 / decision 非法被拒
- legacy：btc_eth / new_coin / grid 的旧 JSON 仍能正常解析（回归）
"""

import json

import pytest

from ai_tuner.engine.response_parser import OBJECTIVE, ResponseParser
from tests.test_ai_tuner_mvp.conftest import (
    dump_envelope,
    make_envelope,
    make_legacy_payload,
)


@pytest.fixture
def parser():
    return ResponseParser()


# ===========================================================================
# 新契约（contract_mode="new"）
# ===========================================================================


class TestNewContractValid:
    def test_valid_envelope_passes(self, parser):
        """合法 8 字段信封应通过并保留 decision 与 adjustments"""
        result = parser.parse_response(dump_envelope(), contract_mode="new")
        assert "error" not in result
        assert result["decision"] == "PROPOSE"
        assert result["objective"] == OBJECTIVE
        assert result["adjustments"]["strategy.scoring.min_score"]["to"] == 62

    def test_hold_envelope_without_adjustments_passes(self, parser):
        """HOLD 且 adjustments 为空是合法信封"""
        result = parser.parse_response(
            dump_envelope(decision="HOLD", adjustments={}), contract_mode="new"
        )
        assert "error" not in result
        assert result["decision"] == "HOLD"

    def test_data_error_envelope_passes(self, parser):
        """DATA_ERROR 且 adjustments 为空是合法信封"""
        result = parser.parse_response(
            dump_envelope(decision="DATA_ERROR", adjustments={}), contract_mode="new"
        )
        assert "error" not in result

    def test_confidence_boundary_0_and_1(self, parser):
        """confidence 边界值 0 与 1 均合法"""
        for value in (0, 1):
            result = parser.parse_response(dump_envelope(confidence=value), contract_mode="new")
            assert "error" not in result


class TestNewContractRejected:
    @pytest.mark.parametrize(
        "field",
        [
            "decision",
            "objective",
            "diagnosis",
            "target_metrics",
            "adjustments",
            "applicable_market_regimes",
            "expected_impact",
            "confidence",
        ],
    )
    def test_missing_field_rejected(self, parser, field):
        """8 个必需字段逐一验证：缺失即被拒，且错误信息点名该字段"""
        envelope = make_envelope()
        envelope.pop(field)
        result = parser.parse_response(json.dumps(envelope), contract_mode="new")
        assert "error" in result
        assert field in result["error"]

    def test_objective_mismatch_rejected(self, parser):
        """objective 与配置的优化目标不一致即被拒"""
        result = parser.parse_response(
            dump_envelope(objective="maximize_return"), contract_mode="new"
        )
        assert "error" in result
        assert "objective" in result["error"]

    def test_invalid_decision_rejected(self, parser):
        """decision 不在 PROPOSE|HOLD|DATA_ERROR 枚举内即被拒"""
        result = parser.parse_response(dump_envelope(decision="APPLY"), contract_mode="new")
        assert "error" in result
        assert "decision" in result["error"]

    def test_propose_with_empty_adjustments_rejected(self, parser):
        """PROPOSE 必须携带至少 1 个 adjustment"""
        result = parser.parse_response(dump_envelope(adjustments={}), contract_mode="new")
        assert "error" in result
        assert "PROPOSE" in result["error"]

    def test_hold_with_adjustments_rejected(self, parser):
        """HOLD 不得携带 adjustments"""
        result = parser.parse_response(dump_envelope(decision="HOLD"), contract_mode="new")
        assert "error" in result
        assert "HOLD" in result["error"]

    @pytest.mark.parametrize("confidence", [True, "0.7", None])
    def test_confidence_non_numeric_rejected(self, parser, confidence):
        """confidence 必须是数值（布尔/字符串/None 均拒绝）"""
        result = parser.parse_response(dump_envelope(confidence=confidence), contract_mode="new")
        assert "error" in result
        assert "confidence" in result["error"]

    @pytest.mark.parametrize("confidence", [-0.1, 1.5])
    def test_confidence_out_of_range_rejected(self, parser, confidence):
        """confidence 超出 [0, 1] 即被拒"""
        result = parser.parse_response(dump_envelope(confidence=confidence), contract_mode="new")
        assert "error" in result
        assert "confidence" in result["error"]

    @pytest.mark.parametrize(
        "override, field",
        [
            ({"diagnosis": "不是列表"}, "diagnosis"),
            ({"target_metrics": ["不是字典"]}, "target_metrics"),
            ({"applicable_market_regimes": "trending"}, "applicable_market_regimes"),
            ({"expected_impact": "提升"}, "expected_impact"),
            ({"adjustments": [{"path": "x"}]}, "adjustments"),
        ],
    )
    def test_field_type_mismatch_rejected(self, parser, override, field):
        """字段类型不符（diagnosis 非 list 等）即被拒"""
        result = parser.parse_response(dump_envelope(**override), contract_mode="new")
        assert "error" in result
        assert field in result["error"]


# ===========================================================================
# 旧契约（contract_mode="legacy"）回归：btc_eth / new_coin / grid
# ===========================================================================


class TestLegacyContractRegression:
    @pytest.mark.parametrize("strategy_id", ["btc_eth", "new_coin", "grid"])
    def test_legacy_json_still_parses(self, parser, strategy_id):
        """三个旧契约策略的典型报文仍能正常解析（回归）"""
        result = parser.parse_response(
            json.dumps(make_legacy_payload(), ensure_ascii=False), contract_mode="legacy"
        )
        assert "error" not in result
        assert result["summary"] == "建议下调入场门槛"
        assert result["adjustments"]["scoring.min_score"]["to"] == 62

    def test_legacy_simple_value_adjustment_normalized(self, parser):
        """旧契约允许 adjustment 项为简单数值（自动补 from=None）"""
        payload = make_legacy_payload(adjustments={"scoring.min_score": 65})
        result = parser.parse_response(json.dumps(payload), contract_mode="legacy")
        assert "error" not in result
        assert result["adjustments"]["scoring.min_score"] == {"from": None, "to": 65}

    def test_legacy_dict_without_from_normalized(self, parser):
        """旧契约允许 dict 缺 from（自动补 None）"""
        payload = make_legacy_payload(adjustments={"scoring.min_score": {"to": 63}})
        result = parser.parse_response(json.dumps(payload), contract_mode="legacy")
        assert "error" not in result
        assert result["adjustments"]["scoring.min_score"] == {"from": None, "to": 63}

    def test_legacy_unparseable_item_rejected(self, parser):
        """adjustment 项无法解析（如 list）时报错"""
        payload = make_legacy_payload(adjustments={"scoring.min_score": [60, 65]})
        result = parser.parse_response(json.dumps(payload), contract_mode="legacy")
        assert "error" in result
        assert "adjustments" in result["error"]

    @pytest.mark.parametrize("missing", ["reasons", "summary", "adjustments", "expected_impact"])
    def test_legacy_missing_required_field_rejected(self, parser, missing):
        """旧契约 4 个必需字段缺失即被拒"""
        payload = make_legacy_payload()
        payload.pop(missing)
        result = parser.parse_response(json.dumps(payload), contract_mode="legacy")
        assert "error" in result
        assert missing in result["error"]

    def test_new_envelope_rejected_under_legacy_missing_fields(self, parser):
        """新契约信封在 legacy 模式下因缺 reasons/summary 被拒（证明分流生效）"""
        result = parser.parse_response(dump_envelope(), contract_mode="legacy")
        assert "error" in result
        assert "reasons" in result["error"]


# ===========================================================================
# 通用解析路径（两种契约共用）
# ===========================================================================


class TestCommonParsing:
    def test_empty_response_rejected(self, parser):
        assert parser.parse_response("", contract_mode="new") == {"error": "LLM 响应为空"}

    def test_none_response_rejected(self, parser):
        assert parser.parse_response(None, contract_mode="legacy") == {"error": "LLM 响应为空"}

    def test_no_json_rejected(self, parser):
        result = parser.parse_response("抱歉，我无法给出建议", contract_mode="legacy")
        assert "error" in result

    def test_json_array_rejected(self, parser):
        """纯数组文本无法提取 JSON 对象，被拒"""
        result = parser.parse_response("[1, 2, 3]", contract_mode="legacy")
        assert "error" in result
        assert "无法从响应中提取 JSON" in result["error"]

    def test_adjustments_not_dict_rejected(self, parser):
        """adjustments 为数组时无论何种契约都报格式错误"""
        payload = make_legacy_payload(adjustments=[1, 2])
        result = parser.parse_response(json.dumps(payload), contract_mode="legacy")
        assert "error" in result
        assert "字典" in result["error"]

    def test_markdown_code_block_extracted(self, parser):
        """markdown ```json 代码块中的 JSON 能被提取"""
        raw = "前置说明\n```json\n" + json.dumps(make_legacy_payload()) + "\n```\n后记"
        result = parser.parse_response(raw, contract_mode="legacy")
        assert "error" not in result

    def test_reasoning_content_separated(self, parser):
        """__REASONING__ 前缀被剥离并附到 reasoning 字段"""
        body = json.dumps(make_legacy_payload())
        raw = "__REASONING__先思考一下____END_REASONING______CONTENT__" + body
        result = parser.parse_response(raw, contract_mode="legacy")
        assert "error" not in result
        assert result["reasoning"] == "先思考一下"

    def test_reasoning_marker_without_end_is_tolerated(self, parser):
        """只有 __REASONING__ 没有 ____END_REASONING____ 时去标记整体解析"""
        body = json.dumps(make_legacy_payload())
        raw = "__REASONING__" + body
        result = parser.parse_response(raw, contract_mode="legacy")
        assert "error" not in result

    def test_trailing_content_tolerated(self, parser):
        """JSON 后有尾随内容时经 raw_decode 容忍并解析成功"""
        body = json.dumps(make_legacy_payload())
        result = parser.parse_response(body + "  以上为建议。", contract_mode="legacy")
        assert "error" not in result

    def test_fix_json_repairs_unclosed_string_and_braces(self):
        """_fix_json 工具方法：补齐未闭合字符串与花括号"""
        fixed = ResponseParser._fix_json('{"a": "b"')
        assert json.loads(fixed) == {"a": "b"}
        fixed2 = ResponseParser._fix_json('{"a": {"b": 1')
        assert json.loads(fixed2) == {"a": {"b": 1}}

    def test_unfixable_json_rejected(self, parser):
        """无法修复的 JSON 报解析失败"""
        result = parser.parse_response("{abc:", contract_mode="legacy")
        assert "error" in result
