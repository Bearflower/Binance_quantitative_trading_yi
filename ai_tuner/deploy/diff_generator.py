"""
差异生成器
将 AI 建议的参数调整格式化为人类可读的变更清单

输出格式：
参数路径: 旧值 → 新值
"""

from datetime import datetime
from typing import Any, Dict

import structlog

logger = structlog.get_logger()


class DiffGenerator:
    """
    差异生成器

    将参数调整字典格式化为人类可读的文本清单，
    用于飞书通知和日志展示。
    """

    def generate_diff(
        self,
        strategy_name: str,
        adjustments: Dict[str, Any],
        current_params: Dict[str, Any] = None,
    ) -> str:
        """
        生成变更清单文本

        Args:
            strategy_name: 策略显示名称
            adjustments: AI 建议的参数调整，格式为 {param_path: {"from": old, "to": new}}
            current_params: 当前参数值字典（可选，用于补充 from 值）

        Returns:
            格式化的变更清单文本
        """
        if not adjustments:
            return "无参数变更"

        current_params = current_params or {}
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        lines = [
            f"策略：{strategy_name}",
            f"时间：{now}",
            "",
            "变更清单：",
        ]

        for param_path, adjustment in adjustments.items():
            if isinstance(adjustment, dict):
                old_val = adjustment.get("from")
                new_val = adjustment.get("to")
            else:
                old_val = current_params.get(param_path, "?")
                new_val = adjustment

            # 格式化值
            old_str = self._format_value(old_val)
            new_str = self._format_value(new_val)

            lines.append(f"  {param_path}: {old_str} → {new_str}")

        diff_text = "\n".join(lines)
        logger.info("变更清单已生成", strategy_name=strategy_name, changes_count=len(adjustments))
        return diff_text

    def generate_full_report(
        self,
        strategy_name: str,
        adjustments: Dict[str, Any],
        ai_reasons: str,
        expected_impact: str,
        confidence: float = 0.0,
        current_params: Dict[str, Any] = None,
    ) -> str:
        """
        生成完整调优报告（含理由和预估影响）

        Args:
            strategy_name: 策略显示名称
            adjustments: 参数调整
            ai_reasons: AI 调优理由
            expected_impact: 预估影响
            confidence: AI 置信度
            current_params: 当前参数值

        Returns:
            完整调优报告文本
        """
        diff_text = self.generate_diff(strategy_name, adjustments, current_params)

        parts = [diff_text]

        if ai_reasons:
            parts.append(f"\nAI 理由：{ai_reasons}")

        if expected_impact:
            parts.append(f"\n预估影响：{expected_impact}")

        if confidence > 0:
            parts.append(f"\n置信度：{confidence:.0%}")

        return "\n".join(parts)

    @staticmethod
    def _format_value(value: Any) -> str:
        """
        格式化值为可读字符串

        Args:
            value: 任意值

        Returns:
            格式化后的字符串
        """
        if value is None:
            return "?"
        if isinstance(value, float):
            return f"{value:.4f}".rstrip("0").rstrip(".")
        if isinstance(value, bool):
            return "是" if value else "否"
        return str(value)