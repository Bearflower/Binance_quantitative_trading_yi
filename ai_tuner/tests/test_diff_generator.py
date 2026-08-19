"""
测试差异生成器（DiffGenerator）

覆盖用例：
- 正常参数变更：adjustments 含多个参数 → 格式化变更清单，含旧值→新值
- 空调整：adjustments={} → 返回"无参数变更"
- 无 current_params：adjustments 有值，current_params=None → 旧值显示为 "?"
- 单参数变更：一个参数调整 → 正确格式化
- 多个策略参数变更：混合字符串和数字类型的参数
"""

import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, ".")

from ai_tuner.deploy.diff_generator import DiffGenerator


@pytest.fixture
def diff_generator():
    """创建 DiffGenerator 实例"""
    return DiffGenerator()


# ============================================================
# 测试用例 1：正常参数变更
# ============================================================


class TestNormalParameterChanges:
    """测试正常参数变更"""

    @patch("ai_tuner.deploy.diff_generator.datetime")
    def test_multiple_params_formatted(self, mock_dt, diff_generator):
        """验证多个参数调整时正确格式化变更清单"""
        mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 14, 30, 0)

        adjustments = {
            "scoring.min_score": {"from": 0.70, "to": 0.75},
            "scoring.weights.trend_strength": {"from": 0.40, "to": 0.45},
        }
        current_params = {}

        result = diff_generator.generate_diff("测试策略", adjustments, current_params)

        assert "策略：测试策略" in result
        assert "时间：2026-08-11 14:30" in result
        assert "变更清单：" in result
        assert "scoring.min_score: 0.7 → 0.75" in result
        assert "scoring.weights.trend_strength: 0.4 → 0.45" in result

    @patch("ai_tuner.deploy.diff_generator.datetime")
    def test_single_param_change(self, mock_dt, diff_generator):
        """验证单参数调整正确格式化"""
        mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 14, 30, 0)

        adjustments = {
            "scoring.min_score": {"from": 0.70, "to": 0.75},
        }

        result = diff_generator.generate_diff("测试策略", adjustments)

        assert "scoring.min_score: 0.7 → 0.75" in result
        # 验证只有一行变更
        change_lines = [line for line in result.split("\n") if "→" in line]
        assert len(change_lines) == 1


# ============================================================
# 测试用例 2：空调整
# ============================================================


class TestEmptyAdjustments:
    """测试空调整"""

    def test_empty_adjustments(self, diff_generator):
        """验证 adjustments={} 时返回"无参数变更" """
        result = diff_generator.generate_diff("测试策略", {})
        assert result == "无参数变更"


# ============================================================
# 测试用例 3：无 current_params
# ============================================================


class TestNoCurrentParams:
    """测试无 current_params 的情况"""

    @patch("ai_tuner.deploy.diff_generator.datetime")
    def test_current_params_none(self, mock_dt, diff_generator):
        """验证 current_params=None 时旧值显示为 ?"""
        mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 14, 30, 0)

        # adjustments 的值不是 dict，而是直接数值
        adjustments = {
            "scoring.min_score": 0.75,
        }

        result = diff_generator.generate_diff("测试策略", adjustments, current_params=None)

        assert "scoring.min_score: ? → 0.75" in result

    @patch("ai_tuner.deploy.diff_generator.datetime")
    def test_current_params_empty_dict(self, mock_dt, diff_generator):
        """验证 current_params={} 且 adjustment 不是 dict 时旧值显示为 ?"""
        mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 14, 30, 0)

        adjustments = {
            "scoring.min_score": 0.75,
        }

        result = diff_generator.generate_diff("测试策略", adjustments, current_params={})
        # 因为 adjustments 的值不是 dict 格式，会从 current_params.get(param_path, "?") 取值
        # current_params 为空，所以返回 "?"
        assert "scoring.min_score: ? → 0.75" in result


# ============================================================
# 测试用例 4：混合类型参数
# ============================================================


class TestMixedTypeParams:
    """测试混合类型参数"""

    @patch("ai_tuner.deploy.diff_generator.datetime")
    def test_mixed_string_and_number(self, mock_dt, diff_generator):
        """验证字符串和数字类型的参数正确格式化"""
        mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 14, 30, 0)

        adjustments = {
            "scoring.min_score": {"from": 0.70, "to": 0.75},
            "strategy.name": {"from": "旧名称", "to": "新名称"},
            "features.enabled": {"from": False, "to": True},
        }

        result = diff_generator.generate_diff("测试策略", adjustments)

        assert "scoring.min_score: 0.7 → 0.75" in result
        assert "strategy.name: 旧名称 → 新名称" in result
        assert "features.enabled: 否 → 是" in result

    @patch("ai_tuner.deploy.diff_generator.datetime")
    def test_float_formatting(self, mock_dt, diff_generator):
        """验证浮点数格式化（去掉多余尾随 0）"""
        mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 14, 30, 0)

        adjustments = {
            "scoring.min_score": {"from": 0.7000, "to": 0.7500},
            "scoring.precision": {"from": 0.1000, "to": 0.2000},
        }

        result = diff_generator.generate_diff("测试策略", adjustments)

        assert "0.7 → 0.75" in result
        assert "0.1 → 0.2" in result


# ============================================================
# 测试用例 5：_format_value 单元测试
# ============================================================


class TestFormatValue:
    """测试 _format_value 方法"""

    def test_format_none(self, diff_generator):
        """验证 None 格式化为 ?"""
        assert diff_generator._format_value(None) == "?"

    def test_format_float(self, diff_generator):
        """验证浮点数格式化"""
        assert diff_generator._format_value(0.7500) == "0.75"
        assert diff_generator._format_value(1.0) == "1"
        assert diff_generator._format_value(0.1234) == "0.1234"

    def test_format_bool_true(self, diff_generator):
        """验证 True 格式化为"是" """
        assert diff_generator._format_value(True) == "是"

    def test_format_bool_false(self, diff_generator):
        """验证 False 格式化为"否" """
        assert diff_generator._format_value(False) == "否"

    def test_format_string(self, diff_generator):
        """验证字符串直接返回"""
        assert diff_generator._format_value("hello") == "hello"

    def test_format_int(self, diff_generator):
        """验证整数直接返回"""
        assert diff_generator._format_value(42) == "42"


# ============================================================
# 测试用例 6：generate_full_report 测试
# ============================================================


class TestGenerateFullReport:
    """测试 generate_full_report 方法"""

    @patch("ai_tuner.deploy.diff_generator.datetime")
    def test_full_report_with_reasons(self, mock_dt, diff_generator):
        """验证完整报告包含理由和预估影响"""
        mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 14, 30, 0)

        adjustments = {
            "scoring.min_score": {"from": 0.70, "to": 0.75},
        }

        result = diff_generator.generate_full_report(
            strategy_name="测试策略",
            adjustments=adjustments,
            ai_reasons="胜率偏低，建议提高最低评分阈值",
            expected_impact="预计胜率提升 5%",
            confidence=0.85,
        )

        assert "策略：测试策略" in result
        assert "scoring.min_score: 0.7 → 0.75" in result
        assert "AI 理由：胜率偏低，建议提高最低评分阈值" in result
        assert "预估影响：预计胜率提升 5%" in result
        assert "置信度：85%" in result

    @patch("ai_tuner.deploy.diff_generator.datetime")
    def test_full_report_without_optional_fields(self, mock_dt, diff_generator):
        """验证不传可选字段时不包含对应段落"""
        mock_dt.now.return_value = __import__("datetime").datetime(2026, 8, 11, 14, 30, 0)

        adjustments = {
            "scoring.min_score": {"from": 0.70, "to": 0.75},
        }

        result = diff_generator.generate_full_report(
            strategy_name="测试策略",
            adjustments=adjustments,
            ai_reasons="",
            expected_impact="",
            confidence=0.0,
        )

        assert "AI 理由" not in result
        assert "预估影响" not in result
        assert "置信度" not in result