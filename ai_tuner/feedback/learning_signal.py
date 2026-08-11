"""
学习信号生成器

根据效果追踪的评级和历史记录，生成注入到 Prompt 中的学习指令文本。
不依赖 LLM 判断，用规则引擎做前置决策。

规则引擎（L1-L4）：
- L1：效果驱动的决策方向
- L2：避免过度优化
- L3：零交易处理
- L4：连续不变触发
"""

from typing import Any, Dict, List, Optional

import structlog

from ai_tuner.feedback.effect_tracker import EffectSummary
from ai_tuner.memory.db_handler import MemoryDBHandler

logger = structlog.get_logger()

# 内置指令文本（作为配置缺失时的 fallback）
_DEFAULT_INSTRUCTIONS = {
    "l1_good": "上次调优效果良好，建议延续上次参数调整方向，可在此基础上进一步微调（调整幅度不超过上次的 50%）",
    "l1_fair": "上次调优效果一般，建议谨慎评估当前参数，如有必要可小幅调整或维持不变",
    "l1_poor": "上次调优效果较差，建议回撤上次调整，或朝相反方向调整（如上次上调了某参数，本次应考虑下调）",
    "l1_insufficient": "上周交易数据不足，建议以更长时间维度的数据为准，不做基于噪音数据的调整",
}


class LearningSignalGenerator:
    """
    学习信号生成器

    根据效果追踪的评级和历史记录，生成注入到 Prompt 中的学习指令文本。
    使用规则引擎（L1-L4）做前置决策，不依赖 LLM 判断。

    输出位置：System Prompt（common_rules.txt）末尾。
    插值变量：{{ learning_instructions }}
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化学习信号生成器

        Args:
            config: 系统配置字典
        """
        self.config = config
        feedback_cfg = config.get("feedback", {})
        learning_cfg = feedback_cfg.get("learning", {})
        self.consecutive_same_direction = learning_cfg.get("consecutive_same_direction", 2)
        self.stale_unchanged_count = learning_cfg.get("stale_unchanged_count", 3)
        self.stale_adjustment_ratio = learning_cfg.get("stale_adjustment_ratio", 0.5)

        # 读取指令模板（从配置或使用内置 fallback）
        instructions_cfg = feedback_cfg.get("instructions", {})
        self._instructions = {
            "l1_good": instructions_cfg.get("l1_good", _DEFAULT_INSTRUCTIONS["l1_good"]),
            "l1_fair": instructions_cfg.get("l1_fair", _DEFAULT_INSTRUCTIONS["l1_fair"]),
            "l1_poor": instructions_cfg.get("l1_poor", _DEFAULT_INSTRUCTIONS["l1_poor"]),
            "l1_insufficient": instructions_cfg.get("l1_insufficient", _DEFAULT_INSTRUCTIONS["l1_insufficient"]),
        }

        logger.debug("学习信号生成器初始化完成")

    async def build_learning_instructions(
        self,
        strategy_id: str,
        effect_summary: EffectSummary,
        db_handler: MemoryDBHandler,
        current_report: Dict[str, Any],
    ) -> str:
        """
        构建学习指令文本

        按 L1 > L2 > L3 > L4 优先级合并输出：
        - L1（决策方向）：始终输出
        - L2（避免过度优化）：合并到"注意事项"段落
        - L3（零交易处理）：作为附加约束追加
        - L4（连续不变触发）：作为附加约束追加

        Args:
            strategy_id: 策略唯一标识
            effect_summary: EffectTracker 输出的效果摘要
            db_handler: 记忆库数据库处理器（用于查询历史记录）
            current_report: 当前的策略报告字典

        Returns:
            str: 学习指令文本，注入到 System Prompt 的末尾
        """
        try:
            # 无效果数据时，跳过 L1 规则
            if not effect_summary.has_data:
                return self._build_no_history_context()

            lines = ["## 学习指令（基于上次调优反馈）\n"]

            # ============ L1：效果驱动的决策方向（始终输出） ============
            l1_text = self._apply_l1_direction(effect_summary.rating)
            lines.append("### 决策方向")
            lines.append(f"- 上次调优评级：{effect_summary.rating}")
            lines.append(f"- 建议方向：{l1_text}")
            if effect_summary.win_rate_change != 0 or effect_summary.pnl_change != 0:
                wr_sign = "+" if effect_summary.win_rate_change >= 0 else ""
                pnl_sign = "+" if effect_summary.pnl_change >= 0 else ""
                lines.append(
                    f"- 理由：胜率变化{wr_sign}{effect_summary.win_rate_change:.1%}，"
                    f"盈亏变化{pnl_sign}{effect_summary.pnl_change:.2f} USDT"
                )
            lines.append("")

            # ============ L2：避免过度优化 ============
            l2_warnings = await self._apply_l2_avoid_over_optimization(db_handler, strategy_id)

            # ============ L3：零交易处理 ============
            l3_warning = self._apply_l3_low_trades_check(effect_summary.total_trades)

            # ============ L4：连续不变触发 ============
            l4_warning = await self._apply_l4_stale_trigger(db_handler, strategy_id)

            # 合并注意事项段落
            has_warnings = bool(l2_warnings) or l3_warning is not None
            if has_warnings:
                lines.append("### 注意事项")
                for w in l2_warnings:
                    lines.append(f"- {w}")
                if l3_warning:
                    lines.append(f"- {l3_warning}")

                # 交易数据充足提示
                if effect_summary.total_trades >= self._get_min_trades():
                    lines.append(f"- 交易数据充足（{effect_summary.total_trades}笔），可正常参考")
                lines.append("")

            # 合并约束段落
            if l4_warning:
                lines.append("### 约束")
                lines.append(f"- {l4_warning}")
                lines.append("")

            return "\n".join(lines)

        except Exception as e:
            logger.error(
                "构建学习指令异常",
                strategy_id=strategy_id,
                error=str(e),
                exc_info=True,
            )
            return ""

    def _get_min_trades(self) -> int:
        """获取最小有效交易笔数配置"""
        return self.config.get("feedback", {}).get("rating", {}).get("min_trades_for_valid", 3)

    @staticmethod
    def _build_no_history_context() -> str:
        """
        构建无历史数据时的占位文本

        Returns:
            占位文本
        """
        return (
            "## 学习指令（基于上次调优反馈）\n\n"
            "暂无历史调优数据，请基于当前数据做判断。\n"
        )

    def _apply_l1_direction(self, rating: str) -> str:
        """
        L1：效果驱动的决策方向

        Args:
            rating: 效果评级

        Returns:
            指令文本
        """
        mapping = {
            "良好": self._instructions["l1_good"],
            "一般": self._instructions["l1_fair"],
            "较差": self._instructions["l1_poor"],
            "数据不足": self._instructions["l1_insufficient"],
        }
        return mapping.get(rating, self._instructions["l1_fair"])

    async def _apply_l2_avoid_over_optimization(
        self,
        db_handler: MemoryDBHandler,
        strategy_id: str,
    ) -> List[str]:
        """
        L2：避免过度优化

        检查最近 N 条已生效记录，判断：
        - 是否连续同方向调整
        - 是否连续调整后效果不佳

        Args:
            db_handler: 记忆库数据库处理器
            strategy_id: 策略唯一标识

        Returns:
            警告信息列表
        """
        warnings = []

        try:
            recent = await db_handler.get_recent_applied_memories(
                strategy_id=strategy_id,
                limit=self.consecutive_same_direction,
            )

            if len(recent) < self.consecutive_same_direction:
                return warnings

            # 检查调整方向是否一致
            adjustments_list = []
            for mem in recent:
                ai_suggestions = mem.get("ai_suggestions", {}) or {}
                if isinstance(ai_suggestions, str):
                    import json
                    try:
                        ai_suggestions = json.loads(ai_suggestions)
                    except (json.JSONDecodeError, TypeError):
                        ai_suggestions = {}
                adjustments = ai_suggestions.get("adjustments", {}) if isinstance(ai_suggestions, dict) else {}
                adjustments_list.append(adjustments)

            # 检查是否有调整
            non_empty = [adj for adj in adjustments_list if adj]
            if len(non_empty) >= self.consecutive_same_direction:
                # 检查方向是否一致（所有参数调整方向是否相同）
                directions = set()
                for adj in non_empty:
                    for param_path, adjustment in adj.items():
                        if isinstance(adjustment, dict):
                            old_val = adjustment.get("from")
                            new_val = adjustment.get("to")
                            if old_val is not None and new_val is not None:
                                try:
                                    direction = "up" if float(new_val) > float(old_val) else "down"
                                    directions.add(f"{param_path}:{direction}")
                                except (ValueError, TypeError):
                                    pass

                if len(directions) > 0:
                    all_up = all(d.endswith(":up") for d in directions)
                    all_down = all(d.endswith(":down") for d in directions)
                    if all_up or all_down:
                        warnings.append(
                            "已连续多次朝同一方向调整策略参数，本周建议暂停调整，观察效果"
                        )

        except Exception as e:
            logger.error(
                "L2 规则检查异常",
                strategy_id=strategy_id,
                error=str(e),
            )

        return warnings

    def _apply_l3_low_trades_check(self, total_trades: int) -> Optional[str]:
        """
        L3：零交易处理

        Args:
            total_trades: 上周总交易笔数

        Returns:
            警告信息，无需警告时返回 None
        """
        min_trades = self._get_min_trades()
        if total_trades < min_trades:
            return (
                "上周数据样本量不足（仅{}笔交易），不予参考，"
                "建议仅在历史多周数据充足时进行调整"
            ).format(total_trades)
        return None

    async def _apply_l4_stale_trigger(
        self,
        db_handler: MemoryDBHandler,
        strategy_id: str,
    ) -> Optional[str]:
        """
        L4：连续不变触发

        检查最近 N 条已生效记录，如果连续 N 次"维持不变"（adjustments 为空），
        则必须输出至少 1 个参数调整。

        Args:
            db_handler: 记忆库数据库处理器
            strategy_id: 策略唯一标识

        Returns:
            警告信息，无需触发时返回 None
        """
        try:
            recent = await db_handler.get_recent_applied_memories(
                strategy_id=strategy_id,
                limit=self.stale_unchanged_count,
            )

            if len(recent) < self.stale_unchanged_count:
                return None

            # 检查是否全部"维持不变"
            all_empty = True
            for mem in recent:
                ai_suggestions = mem.get("ai_suggestions", {}) or {}
                if isinstance(ai_suggestions, str):
                    import json
                    try:
                        ai_suggestions = json.loads(ai_suggestions)
                    except (json.JSONDecodeError, TypeError):
                        ai_suggestions = {}
                adjustments = ai_suggestions.get("adjustments", {}) if isinstance(ai_suggestions, dict) else {}
                if adjustments:
                    all_empty = False
                    break

            if all_empty:
                ratio_pct = int(self.stale_adjustment_ratio * 100)
                return (
                    f"已连续{self.stale_unchanged_count}次维持不变，本次必须输出至少 1 个参数调整"
                    f"（即使幅度很小），调整幅度控制在正常范围的 {ratio_pct}% 以内（保守调整）"
                )

            return None

        except Exception as e:
            logger.error(
                "L4 规则检查异常",
                strategy_id=strategy_id,
                error=str(e),
            )
            return None