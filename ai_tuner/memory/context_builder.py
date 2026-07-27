"""
上下文构建器
将历史调优记忆拼接为 AI 可理解的上下文段落

每次调用 AI 调优前，从记忆库中拉取最近 N 条已生效记忆，
提取摘要和推理链要点，拼接成「历史调优简史」文本段落，注入到 Prompt 中。
"""

from typing import Any, Dict

import structlog

from ai_tuner.memory.db_handler import MemoryDBHandler

logger = structlog.get_logger()


class ContextBuilder:
    """
    上下文构建器

    负责从记忆库中提取历史记录，构建 AI 调优所需的上下文信息。
    支持提取历史 AI 的思考链要点，形成推理连续性。
    """

    def __init__(self, context_window_size: int):
        """
        初始化上下文构建器

        Args:
            context_window_size: 滑动窗口大小（保留最近 N 条记忆），从配置读取
        """
        self.context_window_size = context_window_size

    async def build_context(
        self,
        strategy_id: str,
        db_handler: MemoryDBHandler,
        current_report: Dict[str, Any],
    ) -> str:
        """
        构建历史调优上下文

        从记忆库中拉取最近 N 条已生效的记忆，提取摘要和推理链要点，
        拼接成历史调优简史文本段落。每个策略独立维护自己的记忆链，
        实现"每个策略各自一个对话线程"的效果。

        Args:
            strategy_id: 策略唯一标识
            db_handler: 记忆库数据库处理器
            current_report: 当前的策略报告字典

        Returns:
            格式化的上下文文本段落
        """
        try:
            memories = await db_handler.get_recent_memories(
                strategy_id, limit=self.context_window_size
            )

            if not memories:
                return "暂无历史调优记录，这是首次调优。"

            lines = [f"## {strategy_id} 历史调优简史（最近 {len(memories)} 次）\n"]
            for i, mem in enumerate(memories, 1):
                summary = mem.get("summary", "") or "无摘要"
                post_win = mem.get("post_win_rate")
                post_pnl = mem.get("post_total_pnl")

                # 提取历史推理链摘要（思考模式下的 reasoning 内容）
                ai_suggestions = mem.get("ai_suggestions", {}) or {}
                reasoning = ""
                if isinstance(ai_suggestions, dict):
                    reasoning = ai_suggestions.get("reasoning", "") or ""

                effect_line = ""
                if post_win is not None:
                    effect_line += f"应用后胜率: {post_win:.1%}"
                if post_pnl is not None:
                    effect_line += f" | 应用后盈亏: {post_pnl:.2f} USDT"

                lines.append(f"第{i}次调优（{mem.get('created_at', '未知时间')}）：")
                lines.append(f"  摘要: {summary}")
                if effect_line:
                    lines.append(f"  效果: {effect_line}")
                if reasoning:
                    # 只取推理链前 200 字作为要点
                    reasoning_short = reasoning[:200]
                    if len(reasoning) > 200:
                        reasoning_short += "..."
                    lines.append(f"  推理要点: {reasoning_short}")
                lines.append("")

            context = "\n".join(lines)
            logger.debug(
                "历史上下文构建完成",
                strategy_id=strategy_id,
                memory_count=len(memories),
            )
            return context

        except Exception as e:
            logger.error(
                "构建历史上下文异常",
                strategy_id=strategy_id,
                error=str(e),
            )
            return "历史调优记忆获取失败，请仅基于当前数据进行判断。"