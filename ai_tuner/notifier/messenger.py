"""
飞书通知模块
负责将调优建议、审批结果、回滚通知等推送到飞书

复用 shared/notification.py 的 NotificationClient，
使用调优专用 Webhook（FEISHU_WEBHOOK_TUNER）。
"""

from datetime import datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from ai_tuner.allocation.allocation_calculator import AllocationResult

logger = structlog.get_logger()


class Messenger:
    """
    调优通知消息发送器

    封装飞书消息推送，支持：
    - 调优建议卡片（含确认/拒绝指令）
    - 已生效通知
    - 已拒绝通知
    - 回滚通知
    - 错误通知
    """

    def __init__(self, notification_client):
        """
        初始化消息发送器

        Args:
            notification_client: NotificationClient 实例
        """
        self.notification_client = notification_client
        self.tuner_project = "tuner"

    async def send_tuning_card(
        self,
        strategy_name: str,
        strategy_id: str,
        diff_text: str,
        ai_reasons: str,
        expected_impact: str,
        memory_id: int,
    ) -> bool:
        """
        发送调优建议卡片到飞书

        包含策略名、变更清单、AI 理由、预估影响。

        Args:
            strategy_name: 策略显示名称
            strategy_id: 策略唯一标识
            diff_text: 变更清单文本
            ai_reasons: AI 调优理由
            expected_impact: 预估影响
            memory_id: 记忆记录 ID

        Returns:
            是否发送成功
        """
        message = self._build_tuning_card(
            strategy_name=strategy_name,
            strategy_id=strategy_id,
            diff_text=diff_text,
            ai_reasons=ai_reasons,
            expected_impact=expected_impact,
        )

        try:
            # 使用调优专用 Webhook
            result = await self.notification_client.send(
                message=message,
                level="info",
                project=self.tuner_project,
            )
            logger.info(
                "调优建议卡片已发送",
                strategy_id=strategy_id,
                memory_id=memory_id,
            )
            return result
        except Exception as e:
            logger.error("发送调优建议卡片失败", error=str(e))
            return False

    async def send_applied_notification(
        self,
        strategy_name: str,
        strategy_id: str,
        diff_text: str,
    ) -> bool:
        """
        发送已生效通知

        Args:
            strategy_name: 策略显示名称
            strategy_id: 策略唯一标识
            diff_text: 变更清单文本

        Returns:
            是否发送成功
        """
        message = (
            f"【调优已生效】\n"
            f"策略：{strategy_name}\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"{diff_text}\n\n"
            f"以上参数变更已确认并生效。"
        )

        try:
            return await self.notification_client.send(
                message=message,
                level="info",
                project=self.tuner_project,
            )
        except Exception as e:
            logger.error("发送已生效通知失败", error=str(e))
            return False

    async def send_rejected_notification(
        self,
        strategy_name: str,
        strategy_id: str,
    ) -> bool:
        """
        发送已拒绝通知

        Args:
            strategy_name: 策略显示名称
            strategy_id: 策略唯一标识

        Returns:
            是否发送成功
        """
        message = (
            f"【调优建议已拒绝】\n"
            f"策略：{strategy_name}\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"本次调优建议已被人工拒绝，参数保持不变。"
        )

        try:
            return await self.notification_client.send(
                message=message,
                level="warning",
                project=self.tuner_project,
            )
        except Exception as e:
            logger.error("发送已拒绝通知失败", error=str(e))
            return False

    async def send_rollback_notification(
        self,
        strategy_name: str,
        strategy_id: str,
        reason: str,
    ) -> bool:
        """
        发送回滚通知

        Args:
            strategy_name: 策略显示名称
            strategy_id: 策略唯一标识
            reason: 回滚原因

        Returns:
            是否发送成功
        """
        message = (
            f"【参数已回滚】\n"
            f"策略：{strategy_name}\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"回滚原因：{reason}\n\n"
            f"策略配置已恢复到回滚前的版本。"
        )

        try:
            return await self.notification_client.send(
                message=message,
                level="error",
                project=self.tuner_project,
            )
        except Exception as e:
            logger.error("发送回滚通知失败", error=str(e))
            return False

    async def send_error_notification(
        self,
        strategy_name: str,
        strategy_id: str,
        error_message: str,
    ) -> bool:
        """
        发送错误通知

        Args:
            strategy_name: 策略显示名称
            strategy_id: 策略唯一标识
            error_message: 错误信息

        Returns:
            是否发送成功
        """
        message = (
            f"【StratTuneAI 调优异常】\n"
            f"策略：{strategy_name}\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            f"错误详情：{error_message}\n\n"
            f"请检查 AI 调优系统运行状态。"
        )

        try:
            return await self.notification_client.send(
                message=message,
                level="error",
                project=self.tuner_project,
            )
        except Exception as e:
            logger.error("发送错误通知失败", error=str(e))
            return False

    async def send_allocation_card(self, result: "AllocationResult") -> bool:
        """
        发送月度资金分配结果卡片

        包含总资金池、风险备用金、各策略排名和分配金额。

        Args:
            result: AllocationResult 对象

        Returns:
            是否发送成功
        """
        message = self._build_allocation_card(result)

        try:
            return await self.notification_client.send(
                message=message,
                level="info",
                project=self.tuner_project,
            )
        except Exception as e:
            logger.error("发送月度分配卡片失败", error=str(e))
            return False

    def _build_tuning_card(
        self,
        strategy_name: str,
        strategy_id: str,
        diff_text: str,
        ai_reasons: str,
        expected_impact: str,
    ) -> str:
        """
        构建调优建议卡片消息文本

        Args:
            strategy_name: 策略显示名称
            strategy_id: 策略唯一标识
            diff_text: 变更清单
            ai_reasons: AI 理由
            expected_impact: 预估影响

        Returns:
            格式化的飞书消息文本
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        card = (
            f"StratTuneAI 周度调优建议\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"策略：{strategy_name}\n"
            f"时间：{now}\n\n"
            f"变更清单：\n"
            f"{diff_text}\n\n"
            f"AI 理由：{ai_reasons}\n\n"
            f"预估影响：{expected_impact}"
        )

        return card

    def _build_allocation_card(self, result: "AllocationResult") -> str:
        """
        构建月度资金分配报告消息文本

        Args:
            result: AllocationResult 对象

        Returns:
            格式化的飞书消息文本
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 构建表头
        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━",
            "  月度资金分配报告",
            f"  {result.month}",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "",
            f"总资金池：{result.total_capital:,.2f} USDT",
            f"风险备用金：{result.reserve_amount:,.2f} USDT ({result.reserve_amount / result.total_capital * 100 if result.total_capital > 0 else 0:.0f}%)",
            f"可分配资金：{result.allocatable_amount:,.2f} USDT ({result.allocatable_amount / result.total_capital * 100 if result.total_capital > 0 else 0:.0f}%)",
            "",
            "排名与分配：",
        ]

        # 构建表格
        lines.append("┌──────┬──────────┬──────────┬──────────┬──────────┐")
        lines.append("│ 排名 │ 策略     │ 月收益率  │ 分配比例 │ 分配金额  │")
        lines.append("├──────┼──────────┼──────────┼──────────┼──────────┤")

        for entry in result.entries:
            display_name = entry.strategy_name
            # 收益率格式化
            if entry.return_rate != 0 or not result.is_first_month:
                return_str = f"{entry.return_rate * 100:+.2f}%"
            else:
                return_str = "  N/A  "
            ratio_str = f"{entry.allocated_ratio * 100:.0f}%"
            amount_str = f"{entry.allocated_amount:,.2f}"

            lines.append(
                f"│  {entry.rank:<3} │ {display_name:<8} │ {return_str:>8} │ {ratio_str:>7}  │ {amount_str:>7}  │"
            )

        lines.append("└──────┴──────────┴──────────┴──────────┴──────────┘")
        lines.append("")

        # 附加说明
        lines.append("注：仅以上策略参与月度资金分配。")
        lines.append("    下月各策略开仓将受上述分配金额限制。")

        # 首月额外说明
        if result.is_first_month:
            lines.append("")
            lines.append("注：本月为首次分配，使用默认基准比例。")
            # 构建默认基准说明
            fallback_desc_parts = []
            for entry in result.entries:
                fallback_desc_parts.append(f"{entry.strategy_name} {entry.allocated_ratio * 100:.0f}%")
            fallback_desc = " | ".join(fallback_desc_parts)
            lines.append(f"    默认基准：{fallback_desc}")
            lines.append("    下月起将根据实际收益率排名动态调整。")

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"执行时间：{now} CST")

        return "\n".join(lines)