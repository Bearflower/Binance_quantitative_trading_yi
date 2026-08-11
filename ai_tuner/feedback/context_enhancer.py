"""
上下文增强器

将效果追踪的摘要格式化为 LLM 可理解的结构化上下文。
与现有 ContextBuilder 合并输出，而非替代。

输出格式为 Markdown，包含：
- 效果评级标题
- 调优前 vs 调优后指标对比表格
- 定性评价段落（含建议方向）
"""

from typing import Any, Dict

import structlog

from ai_tuner.feedback.effect_tracker import EffectSummary

logger = structlog.get_logger()


class ContextEnhancer:
    """
    上下文增强器

    把 EffectTracker 输出的效果摘要，格式化为 LLM 可理解的结构化上下文。
    与现有 ContextBuilder 合并输出，而非替代。

    输出位置：User Prompt 中"历史调优记忆"之后，"请分析以上数据"之前。
    插值变量：{{ feedback_context }}
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化上下文增强器

        Args:
            config: 系统配置字典
        """
        self.config = config
        logger.debug("上下文增强器初始化完成")

    def build_feedback_context(
        self,
        effect_summary: EffectSummary,
        current_report: Dict[str, Any],
    ) -> str:
        """
        构建反馈上下文文本

        Args:
            effect_summary: EffectTracker 输出的效果摘要
            current_report: 当前的策略报告字典（用于获取本周的 BTC 表现等）

        Returns:
            Markdown 格式的反馈上下文文本
        """
        try:
            # 无效果数据时返回占位文本
            if not effect_summary.has_data:
                return self._build_no_data_context()

            # 有效果数据时构建完整反馈上下文
            lines = ["## 上次调优效果追踪\n"]

            # 效果评级标题
            lines.append(f"### 效果评级：{effect_summary.rating}\n")

            # 对比表格
            lines.append(self._format_table(effect_summary))
            lines.append("")

            # 定性评价
            lines.append(self._format_rating_comment(effect_summary))
            lines.append("")

            # 分隔线
            lines.append("---\n")

            return "\n".join(lines)

        except Exception as e:
            logger.error(
                "构建反馈上下文异常",
                error=str(e),
                exc_info=True,
            )
            return "## 上次调优效果追踪\n\n反馈上下文构建失败。\n\n---\n"

    def _build_no_data_context(self) -> str:
        """
        构建无数据时的占位文本

        Returns:
            占位文本
        """
        return (
            "## 上次调优效果追踪\n\n"
            "暂无历史调优效果数据，这是首次反馈追踪。\n\n"
            "---\n"
        )

    @staticmethod
    def _format_table(summary: EffectSummary) -> str:
        """
        生成 Markdown 对比表格

        Args:
            summary: 效果摘要

        Returns:
            Markdown 表格字符串
        """
        # 格式化指标值
        def fmt_pct(val: float) -> str:
            """格式化百分比"""
            return f"{val:.1%}" if val != 0 else "0.0%"

        def fmt_usdt(val: float) -> str:
            """格式化 USDT 金额"""
            sign = "+" if val >= 0 else ""
            return f"{sign}{val:.2f} USDT"

        # 胜率变化带符号
        wr_change = summary.win_rate_change
        wr_change_str = f"{'+' if wr_change >= 0 else ''}{wr_change:.1%}"

        # 盈亏变化带符号
        pnl_change = summary.pnl_change
        pnl_change_str = f"{'+' if pnl_change >= 0 else ''}{pnl_change:.2f} USDT"

        # 最大回撤和交易笔数只在"调优后"列显示
        max_dd_str = f"{summary.max_drawdown_pct:.1%}" if summary.max_drawdown_pct > 0 else "0.0%"
        trades_str = str(summary.total_trades)

        table = (
            "| 指标 | 调优前 | 调优后 | 变化 |\n"
            "|------|--------|--------|------|\n"
            f"| 胜率 | {fmt_pct(summary.pre_win_rate)} | {fmt_pct(summary.post_win_rate)} | {wr_change_str} |\n"
            f"| 周收益 | {fmt_usdt(summary.pre_total_pnl)} | {fmt_usdt(summary.post_total_pnl)} | {pnl_change_str} |\n"
            f"| 最大回撤 | — | {max_dd_str} | — |\n"
            f"| 上周交易笔数 | — | {trades_str} | — |\n"
        )

        return table

    @staticmethod
    def _format_rating_comment(summary: EffectSummary) -> str:
        """
        生成定性评价段落

        Args:
            summary: 效果摘要

        Returns:
            评价文本段落
        """
        lines = ["### 定性分析"]

        # 调优版本
        if summary.original_version:
            lines.append(f"- 上次调优版本：{summary.original_version}")

        # 效果评价
        rating_comments = {
            "良好": "调优效果显著，参数调整方向正确。",
            "一般": "调优效果中性，需结合本周数据综合判断。",
            "较差": "调优效果不佳，参数调整方向可能需要重新评估。",
            "数据不足": "上周交易数据不足，建议以更长时间维度的数据为准。",
        }
        comment = rating_comments.get(summary.rating, "效果待评估。")
        lines.append(f"- 效果评价：{comment}")

        # 交易笔数提示
        if summary.total_trades > 0 and summary.total_trades < 3:
            lines.append(f"- 注意：数据量不足（仅{summary.total_trades}笔交易），请谨慎参考。")

        # 建议方向
        direction = ContextEnhancer._build_suggestion_direction(summary.rating)
        lines.append(f"- 建议：**{direction}**")

        return "\n".join(lines)

    @staticmethod
    def _build_suggestion_direction(rating: str) -> str:
        """
        根据评级生成建议方向

        Args:
            rating: 评级（良好/一般/较差/数据不足）

        Returns:
            建议方向文本
        """
        directions = {
            "良好": "延续上次调优方向，可在此基础上进一步微调。",
            "一般": "谨慎评估当前参数，如有必要可小幅调整或维持不变。",
            "较差": "回撤上次调整，或朝相反方向调整（如上次上调了某参数，本次应考虑下调）。",
            "数据不足": "以更长时间维度的数据为准，不做基于噪音数据的调整。",
        }
        return directions.get(rating, "请基于当前数据做出判断。")