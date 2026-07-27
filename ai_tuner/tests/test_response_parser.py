"""
测试 response_parser 模块

覆盖 ResponseParser 类的核心功能：
- __REASONING__/__CONTENT__ 思考模式响应解析
- 普通 JSON 解析
- Markdown 代码块包裹 JSON 解析
- adjustments 格式校验
- 必需字段缺失检测
- 空响应/异常输入处理
"""

import json
import sys
from typing import Any, Dict, List

import pytest

sys.path.insert(0, ".")

from ai_tuner.engine.response_parser import ResponseParser


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def parser() -> ResponseParser:
    """返回一个干净的 ResponseParser 实例"""
    return ResponseParser()


@pytest.fixture
def mock_adapter():
    """返回一个模拟的策略适配器，支持 validate_params 调用"""

    class MockAdapter:
        strategy_id = "test_strategy"

        def validate_params(self, adjustments: Dict[str, Any]) -> Dict[str, Any]:
            if "invalid_param" in adjustments:
                return {
                    "valid": False,
                    "errors": ["无效参数: invalid_param"],
                    "validated": {},
                }
            return {"valid": True, "errors": [], "validated": adjustments}

    return MockAdapter()


# ---------------------------------------------------------------------------
# 辅助函数：构建合法的完整调整响应
# ---------------------------------------------------------------------------


def _valid_adjustments() -> Dict[str, Any]:
    """返回一个合法的 adjustments 字典"""
    return {
        "stop_loss_pct": {"from": 5.0, "to": 6.0},
        "take_profit_pct": {"from": 10.0, "to": 12.0},
    }


def _valid_response_body() -> Dict[str, Any]:
    """返回一个合法的完整响应体"""
    return {
        "reasons": ["当前波动率偏高", "连续 3 日下跌后可能反弹"],
        "summary": "建议适当放宽止损范围，增加容错空间",
        "adjustments": _valid_adjustments(),
        "expected_impact": "预期提升胜率 2%~3%，略微降低盈亏比",
    }


# ===========================================================================
# 第 1 类：__REASONING__/__CONTENT__ 格式（思考模式）
# ===========================================================================


class TestReasoningContentFormat:
    """测试思考模式下的 __REASONING__/__CONTENT__ 格式解析"""

    def test_standard_reasoning_content(self, parser: ResponseParser):
        """标准思考模式响应：__REASONING__ + 思考内容 + ____END_REASONING____ + __CONTENT__ + JSON"""
        body = _valid_response_body()
        raw = (
            "__REASONING__\n这是 AI 的思考过程\n包含多行推理\n"
            "____END_REASONING____\n__CONTENT__\n" + json.dumps(body, ensure_ascii=False)
        )
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        # 验证字段完整性
        assert result["reasons"] == body["reasons"]
        assert result["summary"] == body["summary"]
        assert result["adjustments"] == body["adjustments"]
        assert result["expected_impact"] == body["expected_impact"]
        # 验证 reasoning 被正确提取
        assert result["reasoning"] == "这是 AI 的思考过程\n包含多行推理"
        assert result["reasoning"].startswith("这是 AI 的思考过程")

    def test_reasoning_without_content_marker(self, parser: ResponseParser):
        """
        仅 __REASONING__ 但没有 __CONTENT__ 分隔符的兼容场景
        （代码中 fallback 为去掉 __REASONING__ 后整体解析）
        """
        body = _valid_response_body()
        raw = (
            "__REASONING__\n简短思考\n"
            + json.dumps(body, ensure_ascii=False)
        )
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        assert result["reasons"] == body["reasons"]
        # 当格式异常时 reasoning 可能为空（取决于具体 fallback 逻辑）
        assert "reasoning" not in result or isinstance(result.get("reasoning"), str)

    def test_reasoning_content_empty_reasoning(self, parser: ResponseParser):
        """__REASONING__ 后无内容的情况"""
        body = _valid_response_body()
        raw = "__REASONING__\n__CONTENT__\n" + json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        assert result["reasons"] == body["reasons"]
        # 完全没有思考内容时，不应包含 reasoning 字段
        assert "reasoning" not in result or result.get("reasoning") == ""

    def test_reasoning_content_trailing_whitespace(self, parser: ResponseParser):
        """reasoning 内容前后有空白字符时应被 strip"""
        body = _valid_response_body()
        raw = (
            "__REASONING__  \n  思考内容带空白  \n  "
            "____END_REASONING____\n__CONTENT__\n" + json.dumps(body, ensure_ascii=False)
        )
        result = parser.parse_response(raw)

        assert "error" not in result
        # 验证 strip 后结果（结果中不应保留尾随空格）
        assert result.get("reasoning") == "思考内容带空白"


# ===========================================================================
# 第 2 类：普通 JSON 解析（无 reasoning）
# ===========================================================================


class TestPlainJsonParsing:
    """测试无思考模式的纯 JSON 解析"""

    def test_plain_json(self, parser: ResponseParser):
        """标准纯 JSON 响应"""
        body = _valid_response_body()
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        assert result["reasons"] == body["reasons"]
        assert result["summary"] == body["summary"]
        assert result["adjustments"] == body["adjustments"]
        assert result["expected_impact"] == body["expected_impact"]
        # 纯 JSON 不应包含 reasoning 字段
        assert "reasoning" not in result

    def test_plain_json_with_extra_text_before(self, parser: ResponseParser):
        """JSON 前有无关文字（AI 有时会在 JSON 前加说明）"""
        body = _valid_response_body()
        raw = "以下是我对当前参数的建议：\n" + json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        assert result["reasons"] == body["reasons"]

    def test_plain_json_with_trailing_text(self, parser: ResponseParser):
        """JSON 后有无关文字"""
        body = _valid_response_body()
        raw = json.dumps(body, ensure_ascii=False) + "\n以上分析完毕。"
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        assert result["reasons"] == body["reasons"]


# ===========================================================================
# 第 3 类：Markdown 代码块包裹的 JSON
# ===========================================================================


class TestMarkdownCodeBlockJson:
    """测试 Markdown 代码块包裹的 JSON 解析"""

    def test_json_code_block(self, parser: ResponseParser):
        """标准 ```json ... ``` 代码块"""
        body = _valid_response_body()
        raw = "```json\n" + json.dumps(body, ensure_ascii=False, indent=2) + "\n```"
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        assert result["reasons"] == body["reasons"]

    def test_json_code_block_no_lang(self, parser: ResponseParser):
        """``` ... ``` 不带语言标识的代码块"""
        body = _valid_response_body()
        raw = "```\n" + json.dumps(body, ensure_ascii=False) + "\n```"
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        assert result["reasons"] == body["reasons"]

    def test_json_code_block_surrounded_by_text(self, parser: ResponseParser):
        """代码块前后有额外文本"""
        body = _valid_response_body()
        raw = (
            "分析如下：\n\n"
            "```json\n" + json.dumps(body, ensure_ascii=False) + "\n```\n\n"
            "如需调整请告知。"
        )
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        assert result["reasons"] == body["reasons"]

    def test_json_code_block_with_reasoning(self, parser: ResponseParser):
        """思考模式 + markdown 代码块"""
        body = _valid_response_body()
        raw = (
            "__REASONING__\n思考过程\n"
            "____END_REASONING____\n__CONTENT__\n"
            "```json\n" + json.dumps(body, ensure_ascii=False) + "\n```"
        )
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        assert result["reasons"] == body["reasons"]
        assert result.get("reasoning") == "思考过程"

    def test_multiple_code_blocks_uses_first(self, parser: ResponseParser):
        """多个代码块时取第一个"""
        block1 = {"reasons": ["原因1"], "summary": "概要1", "adjustments": {}, "expected_impact": "影响1"}
        block2 = _valid_response_body()
        raw = (
            "```json\n" + json.dumps(block1, ensure_ascii=False) + "\n```\n"
            "```json\n" + json.dumps(block2, ensure_ascii=False) + "\n```"
        )
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        # 应取到第一个代码块
        assert result["summary"] == "概要1"


# ===========================================================================
# 第 4 类：adjustments 格式验证
# ===========================================================================


class TestAdjustmentsValidation:
    """测试 adjustments 字段的格式校验与兼容处理"""

    def test_adjustments_valid_dict(self, parser: ResponseParser):
        """标准 adjustments：每个值是 {"from": ..., "to": ...} 格式"""
        body = _valid_response_body()
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" not in result
        assert isinstance(result["adjustments"]["stop_loss_pct"], dict)
        assert result["adjustments"]["stop_loss_pct"]["from"] == 5.0
        assert result["adjustments"]["stop_loss_pct"]["to"] == 6.0

    def test_adjustments_number_value_compat(self, parser: ResponseParser):
        """
        兼容场景：adjustments 中的值是纯数字
        代码应包装成 {"from": None, "to": value}
        """
        body = _valid_response_body()
        body["adjustments"] = {"stop_loss_pct": 6.0}
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        assert result["adjustments"]["stop_loss_pct"]["from"] is None
        assert result["adjustments"]["stop_loss_pct"]["to"] == 6.0

    def test_adjustments_dict_missing_from_compat(self, parser: ResponseParser):
        """
        兼容场景：adjustments 中的值有 to 但无 from
        代码应补全 from 为 None
        """
        body = _valid_response_body()
        body["adjustments"] = {"stop_loss_pct": {"to": 6.0}}
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        assert result["adjustments"]["stop_loss_pct"]["from"] is None
        assert result["adjustments"]["stop_loss_pct"]["to"] == 6.0

    def test_adjustments_invalid_format(self, parser: ResponseParser):
        """adjustments 值为非法格式（不符合任何兼容规则）"""
        body = _valid_response_body()
        body["adjustments"] = {"stop_loss_pct": "字符串值"}
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" in result
        assert "格式错误" in result["error"]

    def test_adjustments_not_dict(self, parser: ResponseParser):
        """adjustments 本身不是字典"""
        body = _valid_response_body()
        body["adjustments"] = "not_a_dict"
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" in result
        assert "格式错误" in result["error"] or "格式错误" in str(result)

    def test_adjustments_empty(self, parser: ResponseParser):
        """空 adjustments 应被视为合法"""
        body = _valid_response_body()
        body["adjustments"] = {}
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" not in result, f"不应返回错误: {result.get('error')}"
        assert result["adjustments"] == {}


# ===========================================================================
# 第 5 类：缺少必需字段
# ===========================================================================


class TestMissingRequiredFields:
    """测试必需字段缺失检测"""

    def test_missing_reasons(self, parser: ResponseParser):
        """缺少 reasons 字段"""
        body = _valid_response_body()
        del body["reasons"]
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" in result
        assert "缺少必需字段" in result["error"]
        assert "reasons" in result["error"]

    def test_missing_summary(self, parser: ResponseParser):
        """缺少 summary 字段"""
        body = _valid_response_body()
        del body["summary"]
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" in result
        assert "缺少必需字段" in result["error"]
        assert "summary" in result["error"]

    def test_missing_adjustments(self, parser: ResponseParser):
        """缺少 adjustments 字段"""
        body = _valid_response_body()
        del body["adjustments"]
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" in result
        assert "缺少必需字段" in result["error"]
        assert "adjustments" in result["error"]

    def test_missing_expected_impact(self, parser: ResponseParser):
        """缺少 expected_impact 字段"""
        body = _valid_response_body()
        del body["expected_impact"]
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" in result
        assert "缺少必需字段" in result["error"]
        assert "expected_impact" in result["error"]

    def test_missing_multiple_fields(self, parser: ResponseParser):
        """同时缺少多个必需字段"""
        body = _valid_response_body()
        del body["reasons"]
        del body["adjustments"]
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" in result
        assert "缺少必需字段" in result["error"]
        assert "reasons" in result["error"]
        assert "adjustments" in result["error"]


# ===========================================================================
# 第 6 类：空响应 / 异常输入
# ===========================================================================


class TestEmptyAndInvalidInput:
    """测试空响应和非法输入处理"""

    def test_empty_string(self, parser: ResponseParser):
        """空字符串"""
        result = parser.parse_response("")
        assert "error" in result
        assert "空" in result["error"]

    def test_whitespace_only(self, parser: ResponseParser):
        """仅空白字符"""
        result = parser.parse_response("   \n  \t  ")
        assert "error" in result
        assert "空" in result["error"]

    def test_none_input(self, parser: ResponseParser):
        """None 输入"""
        result = parser.parse_response(None)
        assert "error" in result

    def test_no_json_at_all(self, parser: ResponseParser):
        """完全不包含 JSON 内容的文本"""
        raw = "这是一个没有 JSON 格式的纯文本回复。"
        result = parser.parse_response(raw)
        assert "error" in result
        assert "无法提取 JSON" in result["error"] or "无法从响应中提取" in result["error"]

    def test_invalid_json_syntax(self, parser: ResponseParser):
        """包含花括号但不是合法 JSON"""
        raw = '{"reasons": "未闭合的字符串'
        result = parser.parse_response(raw)
        assert "error" in result
        assert "JSON 解析失败" in result["error"]

    def test_json_is_array_not_object_invalid(self, parser: ResponseParser):
        """解析结果是数组而非字典（因为 _extract_json 只找 {}，数组无法被提取）"""
        raw = json.dumps(["a", "b", "c"])
        result = parser.parse_response(raw)
        assert "error" in result
        # 数组以 [ 开头，_extract_json 只匹配 {，所以会报"无法提取 JSON"
        assert "无法" in result["error"] or "提取 JSON" in result["error"]


# ===========================================================================
# 第 7 类：validate_adjustments 方法测试
# ===========================================================================


class TestValidateAdjustments:
    """测试 validate_adjustments 方法"""

    def test_valid_adjustments(self, parser: ResponseParser, mock_adapter):
        """合法参数应通过校验"""
        adj = {"stop_loss_pct": 6.0}
        result = parser.validate_adjustments(adj, mock_adapter)

        assert result["valid"] is True
        assert result["errors"] == []

    def test_empty_adjustments(self, parser: ResponseParser, mock_adapter):
        """空参数应直接通过"""
        result = parser.validate_adjustments({}, mock_adapter)

        assert result["valid"] is True
        assert result["errors"] == []
        assert result["validated"] == {}

    def test_none_adjustments(self, parser: ResponseParser, mock_adapter):
        """None 参数"""
        result = parser.validate_adjustments(None, mock_adapter)

        assert result["valid"] is True
        assert result["errors"] == []
        assert result["validated"] == {}

    def test_adapter_validation_failure(self, parser: ResponseParser, mock_adapter):
        """适配器校验失败"""
        adj = {"invalid_param": 1.0}
        result = parser.validate_adjustments(adj, mock_adapter)

        assert result["valid"] is False
        assert len(result["errors"]) > 0

    def test_adapter_raises_exception(self, parser: ResponseParser):
        """适配器抛出异常时返回友好错误"""

        class ErrorAdapter:
            strategy_id = "error_strategy"

            def validate_params(self, adjustments):
                raise RuntimeError("校验服务暂时不可用")

        adj = {"stop_loss_pct": 6.0}
        result = parser.validate_adjustments(adj, ErrorAdapter())

        assert result["valid"] is False
        assert any("异常" in e or "不可用" in e for e in result["errors"])


# ===========================================================================
# 第 8 类：边界 / 综合场景
# ===========================================================================


class TestEdgeCases:
    """边界条件和综合场景"""

    def test_deeply_nested_adjustments(self, parser: ResponseParser):
        """adjustments 包含多个复杂参数"""
        body = _valid_response_body()
        body["adjustments"] = {
            "param_a": {"from": 1, "to": 2},
            "param_b": {"from": 10.5, "to": 20.5},
            "param_c": {"from": True, "to": False},
            "param_d": {"from": None, "to": 100},
        }
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" not in result
        assert len(result["adjustments"]) == 4

    def test_unicode_content(self, parser: ResponseParser):
        """中文和其他 Unicode 字符"""
        body = _valid_response_body()
        body["summary"] = "参数调优建议：止损从 5% 调整到 6% （波动率升高）"
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" not in result
        assert "波动率" in result["summary"]

    def test_long_reasoning_truncated_in_log(self, parser: ResponseParser):
        """超长 reasoning 不应影响解析结果"""
        body = _valid_response_body()
        long_reasoning = "推理 " * 1000
        raw = (
            f"__REASONING__\n{long_reasoning}\n____END_REASONING____\n__CONTENT__\n"
            + json.dumps(body, ensure_ascii=False)
        )
        result = parser.parse_response(raw)

        assert "error" not in result
        assert len(result["reasoning"]) > 0
        assert result["reasons"] == body["reasons"]

    def test_brace_in_summary_text(self, parser: ResponseParser):
        """summary 中包含花括号字符不应干扰 JSON 提取"""
        body = _valid_response_body()
        body["summary"] = "建议调整阈值（从 {old} 到 {new}）"
        raw = json.dumps(body, ensure_ascii=False)
        result = parser.parse_response(raw)

        assert "error" not in result
        assert "{old}" in result["summary"]