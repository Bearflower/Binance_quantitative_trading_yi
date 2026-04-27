#!/usr/bin/env python3
"""
通知管理模块

功能：
1. 发送分析结果通知
2. 发送交易执行通知
3. 发送每日交易报告
4. 发送错误通知
5. 通知冷却期管理
6. 被抑制通知记录

版本: v2.0.0 (优化版 - 添加冷却期管理)
更新时间: 2026-04-27
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple
from collections import defaultdict
import threading

from utils.lark_notifier import LarkNotifier
from config.settings import LARK_WEBHOOK_URL
from services.frequency_controller import get_frequency_controller

logger = logging.getLogger(__name__)


class NotificationManager:
    """
    通知管理类

    功能：
    1. 发送各类通知
    2. 通知冷却期管理
    3. 被抑制通知记录
    """

    def __init__(self):
        """初始化通知管理器"""
        self.lark_notifier = LarkNotifier(LARK_WEBHOOK_URL) if LARK_WEBHOOK_URL else None
        logger.info(f"飞书通知：{'已启用' if self.lark_notifier else '已禁用'}")

        # 通知冷却期配置（分钟）
        self.trade_notification_cooldown = 30  # 交易通知冷却期：30分钟
        self.error_notification_cooldown = 60  # 错误通知冷却期：60分钟

        # 记录最近通知时间
        self._last_notification_times: Dict[str, datetime] = {}

        # 被抑制的通知记录
        self._suppressed_notifications: List[Dict[str, Any]] = []

        # 线程锁
        self._lock = threading.Lock()

        # 频率控制器（用于检查交易冷却期）
        try:
            self.frequency_controller = get_frequency_controller()
            logger.info("频率控制器已集成")
        except Exception as e:
            logger.warning(f"频率控制器初始化失败: {e}，将使用默认冷却期")
            self.frequency_controller = None

    def send_analysis_result(self, result: Dict[str, Any]):
        """
        发送分析结果通知（只发检测信号，不发执行结果）

        Args:
            result: 分析结果字典
        """
        if not self.lark_notifier:
            return

        signals = result.get('signals', [])

        # 构建通知消息
        title = "✅ 规则引擎分析完成" if result['success'] else "❌ 规则引擎分析失败"

        content = f"{title}\n\n"
        content += f"检测信号：{len(signals)} 个\n"

        if signals:
            content += "\n信号详情:\n"
            for signal in signals[:3]:  # 只显示前 3 个
                content += f"├─ {signal['币种']} {signal['开仓方向']} "
                content += f"等级:{signal['信号等级']} 推荐度:{signal['开仓推荐度']}\n"

        content += f"\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

        self.lark_notifier.send_text_message(content)

    def send_trade_execution_result(self, signal_messages: list):
        """
        发送交易执行结果通知

        Args:
            signal_messages: 信号执行结果消息列表
        """
        if not self.lark_notifier or not signal_messages:
            return

        message = "📊 交易信号执行结果:\n\n" + "\n".join(signal_messages)
        self.lark_notifier.send_text_message(message)

    def send_trade_notification_with_cooldown(
        self,
        symbol: str,
        message: str,
        force: bool = False
    ) -> Tuple[bool, str]:
        """
        发送交易通知（带冷却期检查）

        在冷却期内不发送重复通知，避免频繁通知。
        同时检查FrequencyController的交易冷却期。

        Args:
            symbol: 交易对（如 BTCUSDT）
            message: 通知消息
            force: 是否强制发送（忽略冷却期）

        Returns:
            (是否发送成功, 原因说明)
        """
        if not self.lark_notifier:
            return False, "飞书通知未启用"

        # 检查FrequencyController的交易冷却期
        if not force and self.frequency_controller:
            in_cooldown, cooldown_end = self.frequency_controller._check_cooldown(symbol)
            if in_cooldown:
                # 记录被抑制的通知
                self._record_suppressed_notification(
                    notification_type='trade',
                    symbol=symbol,
                    message=message,
                    reason=f'交易冷却期内（{cooldown_end.strftime("%H:%M")} 结束）'
                )
                return False, f"{symbol} 在交易冷却期内（{cooldown_end.strftime('%H:%M')} 结束）"

        # 检查通知冷却期
        if not force and self._is_in_trade_cooldown(symbol):
            # 记录被抑制的通知
            self._record_suppressed_notification(
                notification_type='trade',
                symbol=symbol,
                message=message,
                reason='通知冷却期内'
            )
            return False, f"{symbol} 在通知冷却期内"

        # 发送通知
        try:
            self.lark_notifier.send_text_message(message)

            # 记录通知时间
            self._record_notification_time(symbol, 'trade')

            logger.info(f"交易通知发送成功: {symbol}")
            return True, "通知发送成功"

        except Exception as e:
            logger.error(f"交易通知发送失败: {e}")
            return False, f"发送失败: {str(e)}"

    def send_daily_report(self, report_data: Dict[str, Any]):
        """
        发送每日交易报告

        Args:
            report_data: 报告数据字典
        """
        if not self.lark_notifier:
            return

        yesterday = report_data['date']
        total_executed = report_data['executed_count']
        win_count = report_data['win_count']
        win_rate = report_data['win_rate']

        # 构建报告内容
        win_line = f'├─ 盈利：{win_count} 笔' if win_count > 0 else ''
        loss_line = f'└─ 亏损：{report_data["loss_count"]} 笔' if report_data['loss_count'] > 0 else ''

        # 胜率显示：无交易时显示 N/A
        win_rate_str = f"{win_rate:.1f}%" if win_rate is not None else "N/A (无交易)"

        content = f"""📊 交易日报 ({yesterday})

📈 执行统计:
├─ 检测次数：{report_data['signals_count']} 次
├─ 有效信号：{report_data['signals_count']} 个
├─ 成功执行：{total_executed} 笔
└─ 胜率：{win_rate_str}

{win_line}
{loss_line}

请查看完整报告获取详细分析。
"""

        self.lark_notifier.send_text_message(content)
        logger.info(f"昨日日报已发送：{yesterday}")

    def send_error_notification(self, error_message: str):
        """
        发送错误通知

        Args:
            error_message: 错误消息
        """
        if not self.lark_notifier:
            return

        content = f"⛔ 系统错误\n\n{error_message}\n\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        self.lark_notifier.send_text_message(content)

    def send_trading_halt_notification(self, reason: str):
        """
        发送交易停止通知

        Args:
            reason: 停止原因
        """
        if not self.lark_notifier:
            return

        content = f"⛔ 停止交易\n{reason}"
        self.lark_notifier.send_text_message(content)

    # ==================== 冷却期管理 ====================

    def _is_in_trade_cooldown(self, symbol: str) -> bool:
        """
        检查是否在交易通知冷却期内

        Args:
            symbol: 交易对

        Returns:
            是否在冷却期内
        """
        notification_key = f"trade_{symbol}"

        if notification_key not in self._last_notification_times:
            return False

        last_time = self._last_notification_times[notification_key]
        cooldown = timedelta(minutes=self.trade_notification_cooldown)

        return datetime.now() - last_time < cooldown

    def _record_notification_time(self, symbol: str, notification_type: str):
        """
        记录通知时间

        Args:
            symbol: 交易对
            notification_type: 通知类型
        """
        with self._lock:
            notification_key = f"{notification_type}_{symbol}"
            self._last_notification_times[notification_key] = datetime.now()

    def _record_suppressed_notification(
        self,
        notification_type: str,
        symbol: str,
        message: str,
        reason: str
    ):
        """
        记录被抑制的通知

        Args:
            notification_type: 通知类型
            symbol: 交易对
            message: 通知消息
            reason: 抑制原因
        """
        with self._lock:
            suppressed_record = {
                'type': notification_type,
                'symbol': symbol,
                'message': message[:200],  # 限制消息长度
                'reason': reason,
                'suppressed_at': datetime.now().isoformat()
            }
            self._suppressed_notifications.append(suppressed_record)

            # 限制记录大小
            if len(self._suppressed_notifications) > 1000:
                self._suppressed_notifications = self._suppressed_notifications[-1000:]

        logger.info(f"记录被抑制的通知: {notification_type} - {symbol} - {reason}")

    def get_suppressed_notifications(
        self,
        limit: int = 100,
        notification_type: Optional[str] = None,
        symbol: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        获取被抑制的通知记录

        Args:
            limit: 返回记录数
            notification_type: 通知类型（可选）
            symbol: 交易对（可选）

        Returns:
            被抑制的通知列表
        """
        with self._lock:
            notifications = self._suppressed_notifications.copy()

        # 过滤
        if notification_type:
            notifications = [n for n in notifications if n['type'] == notification_type]

        if symbol:
            notifications = [n for n in notifications if n['symbol'] == symbol]

        # 返回最近的记录
        return notifications[-limit:]

    def clear_suppressed_notifications(self):
        """清空被抑制的通知记录"""
        with self._lock:
            self._suppressed_notifications.clear()

        logger.info("被抑制的通知记录已清空")

    def set_cooldown_config(
        self,
        trade_cooldown: Optional[int] = None,
        error_cooldown: Optional[int] = None
    ):
        """
        设置冷却期配置

        Args:
            trade_cooldown: 交易通知冷却期（分钟）
            error_cooldown: 错误通知冷却期（分钟）
        """
        if trade_cooldown is not None:
            self.trade_notification_cooldown = trade_cooldown
            logger.info(f"交易通知冷却期设置为: {trade_cooldown} 分钟")

        if error_cooldown is not None:
            self.error_notification_cooldown = error_cooldown
            logger.info(f"错误通知冷却期设置为: {error_cooldown} 分钟")
