"""
飞书通知推送模块（已改造为使用通用通知服务）

负责：
- 推送交易信号通知
- 推送风险警告
- 推送每日汇总
- 支持分级推送
"""

import os
import requests
from typing import Optional, Dict, Any
from datetime import datetime

from .signal_manager import Signal
from utils.logger import logger
from config.settings import settings

# 通用通知服务配置
NOTIFICATION_SERVICE_URL = os.getenv('NOTIFICATION_SERVICE_URL', 'http://43.156.242.184:8766/api/v1')
NOTIFICATION_PROJECT = 'new_coin'


class FeishuNotifier:
    """飞书通知推送器（使用通用通知服务）"""
    
    def __init__(self, webhook_url: Optional[str] = None):
        """
        初始化飞书推送器
        
        Args:
            webhook_url: 飞书机器人 Webhook URL（已废弃，仅用于兼容）
        """
        self.notification_url = f"{NOTIFICATION_SERVICE_URL}/send"
        
        # 兼容旧配置
        if webhook_url:
            logger.warning("⚠️ webhook_url 参数已废弃，将使用通用通知服务")
        
        logger.info(f"✅ 飞书通知推送器初始化完成（通用服务：{NOTIFICATION_SERVICE_URL}）")
    
    def send_notification(self, message: str, level: str = "info") -> bool:
        """
        发送通知到通用服务
        
        Args:
            message: 消息内容
            level: 通知级别 (info, warning, error)
            
        Returns:
            是否发送成功
        """
        try:
            data = {
                "project": NOTIFICATION_PROJECT,
                "message": message,
                "type": "text",
                "level": level
            }
            
            response = requests.post(
                self.notification_url,
                json=data,
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    logger.info(f"✅ 通知发送成功：{message[:50]}...")
                    return True
                else:
                    logger.error(f"❌ 通知发送失败：{result}")
                    return False
            else:
                logger.error(f"❌ 通知服务 HTTP 错误：{response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"❌ 通知发送异常：{e}")
            return False
    
    def send_message(self, message: str, title: str = "币安做空系统") -> bool:
        """
        发送消息（兼容旧接口）
        
        Args:
            message: 消息内容
            title: 消息标题
            
        Returns:
            是否发送成功
        """
        # 将标题添加到消息内容中
        full_message = f"**{title}**\n\n{message}"
        return self.send_notification(full_message, level="info")
    
    def send_signal_notification(self, signal: Signal) -> bool:
        """
        发送交易信号通知
        
        Args:
            signal: 交易信号对象
            
        Returns:
            是否发送成功
        """
        result = signal.scoring_result
        
        # 构建消息内容
        message = self._build_signal_message(signal)
        
        # 根据评分设置通知级别
        if result.total_score >= 9.0:
            level = "error"  # 强烈推荐，使用 error 级别引起注意
        elif result.total_score >= 8.0:
            level = "warning"  # 推荐
        else:
            level = "info"  # 普通
        
        return self.send_notification(message, level=level)
    
    def _build_signal_message(self, signal: Signal) -> str:
        """
        构建信号消息内容
        
        Args:
            signal: 交易信号对象
            
        Returns:
            格式化的消息内容
        """
        result = signal.scoring_result
        
        # 评分详情
        score_details = (
            f"**【评分详情】**\n"
            f"• 合约数据：{result.contract_score:.1f}/10 (权重 35%)\n"
            f"• 基本面：{result.fundamental_score:.1f}/10 (权重 30%)\n"
            f"• 技术面：{result.technical_score:.1f}/10 (权重 25%)\n"
            f"• 情绪面：{result.sentiment_score:.1f}/10 (权重 10%)\n\n"
            f"**综合评分：{result.total_score:.2f}/10**\n"
        )
        
        # 操作建议
        if result.veto:
            recommendation = f"❌ **否决** - {result.veto_reason}"
        elif result.total_score >= 9.0:
            recommendation = "⭐⭐⭐⭐⭐ **强烈推荐**"
        elif result.total_score >= 8.0:
            recommendation = "⭐⭐⭐⭐ **推荐**"
        elif result.total_score >= 7.0:
            recommendation = "⭐⭐⭐ **建议关注**"
        else:
            recommendation = "⭐ **观望**"
        
        # 关键价位
        price_info = (
            f"**【建议操作】**\n"
            f"• 当前价格：{signal.current_price:.2f} USDT\n"
            f"• 入场区间：{signal.entry_min:.2f} - {signal.entry_max:.2f} USDT\n"
            f"• 止损位：{signal.stop_loss:.2f} USDT (+{(signal.stop_loss/signal.current_price-1)*100:.1f}%)\n"
            f"• 止盈 1：{signal.take_profit_1:.2f} USDT (-{(signal.current_price-signal.take_profit_1)/signal.current_price*100:.1f}%)\n"
            f"• 止盈 2：{signal.take_profit_2:.2f} USDT (-{(signal.current_price-signal.take_profit_2)/signal.current_price*100:.1f}%)\n\n"
        )
        
        # 信号有效期
        remaining = signal.time_remaining()
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        expire_info = f"⏰ 信号有效时间：{hours}小时{minutes}分钟\n"
        
        # 组装完整消息
        message = (
            f"**币种：{signal.symbol}**\n\n"
            f"{score_details}\n"
            f"**【操作建议】**\n"
            f"{recommendation}\n\n"
            f"{price_info}\n"
            f"{expire_info}"
        )
        
        # 添加否决原因
        if result.veto:
            message += f"\n⚠️ **否决原因：{result.veto_reason}**\n"
        
        return message
    
    def send_risk_warning(self, symbol: str, reason: str, current_value: float) -> bool:
        """
        发送风险警告
        
        Args:
            symbol: 币种符号
            reason: 警告原因
            current_value: 当前值
            
        Returns:
            是否发送成功
        """
        message = (
            f"⚠️ **风险警告**\n\n"
            f"**币种：{symbol}**\n\n"
            f"**警告原因：{reason}**\n"
            f"**当前值：{current_value:.4f}**\n\n"
            f"请及时关注并处理！"
        )
        
        return self.send_notification(message, level="warning")
    
    def send_daily_report(self, signals: list, trades: list) -> bool:
        """
        发送每日汇总报告
        
        Args:
            signals: 今日信号列表
            trades: 今日交易列表
            
        Returns:
            是否发送成功
        """
        today = datetime.now().strftime("%Y-%m-%d")
        
        # 统计信号
        signal_count = len(signals)
        pending_count = sum(1 for s in signals if s.status.value == "pending")
        confirmed_count = sum(1 for s in signals if s.status.value == "confirmed")
        executed_count = sum(1 for s in signals if s.status.value == "executed")
        
        # 统计交易
        trade_count = len(trades)
        
        message = (
            f"📊 **每日汇总报告**\n\n"
            f"**日期：{today}**\n\n"
            f"**【信号统计】**\n"
            f"• 总信号数：{signal_count}\n"
            f"• 待确认：{pending_count}\n"
            f"• 已确认：{confirmed_count}\n"
            f"• 已执行：{executed_count}\n\n"
            f"**【交易统计】**\n"
            f"• 交易次数：{trade_count}\n\n"
            f"---\n"
            f"祝您投资顺利！💰"
        )
        
        return self.send_notification(message, level="info")
    
    def send_new_listing_notification(
        self,
        symbol: str,
        listing_time: datetime,
        hours_since_listing: float,
        contract_type: str = "PERPETUAL"
    ) -> bool:
        """
        发送新永续合约上线通知
        
        Args:
            symbol: 币种符号
            listing_time: 上线时间
            hours_since_listing: 上线至今小时数
            contract_type: 合约类型
            
        Returns:
            是否发送成功
        """
        # 判断合约类型
        if contract_type == "TRADIFI_PERPETUAL":
            type_text = "传统金融永续合约"
            type_emoji = "🏦"
        else:
            type_text = "普通永续合约"
            type_emoji = "💰"
        
        message = (
            f"🆕 **新永续合约上线通知**\n\n"
            f"**币种：{symbol}**\n"
            f"{type_emoji} **合约类型：{type_text}**\n"
            f"📅 **上线时间：{listing_time.strftime('%Y-%m-%d %H:%M')}**\n"
            f"⏰ **距今：{hours_since_listing:.1f} 小时**\n\n"
            f"---\n"
            f"系统将自动进行评分监控，请关注后续通知。"
        )
        
        return self.send_notification(message, level="info")
    
    def send_scoring_complete_notification(
        self,
        symbol: str,
        total_score: float,
        scoring_attempt: int,
        signal_generated: bool,
        order_placed: bool = False,
        veto: bool = False,
        veto_reason: str = "",
        current_price: float = 0.0
    ) -> bool:
        """
        发送评分完成通知
        
        Args:
            symbol: 币种符号
            total_score: 综合评分
            scoring_attempt: 第几次评分
            signal_generated: 是否生成信号
            order_placed: 是否完成下单
            veto: 是否被否决
            veto_reason: 否决原因
            current_price: 当前价格
            
        Returns:
            是否发送成功
        """
        # 评分等级
        if total_score >= 9.0:
            score_level = "⭐⭐⭐⭐⭐ 强烈推荐"
        elif total_score >= 8.0:
            score_level = "⭐⭐⭐⭐ 推荐"
        elif total_score >= 7.0:
            score_level = "⭐⭐⭐ 建议"
        elif total_score >= 6.0:
            score_level = "⭐⭐ 关注"
        else:
            score_level = "⭐ 观望"
        
        # 构建消息
        message = f"📊 **评分完成通知**\n\n"
        message += f"**币种：{symbol}**\n"
        message += f"**评分：{total_score:.2f}/10** ({score_level})\n"
        message += f"**评分次数：第 {scoring_attempt} 次**\n"
        
        if current_price > 0:
            message += f"**当前价格：{current_price:.2f} USDT**\n"
        
        message += "\n**【结果】**\n"
        
        if veto:
            message += f"❌ **一票否决**\n"
            message += f"原因：{veto_reason}\n"
        elif signal_generated:
            message += f"✅ **已生成交易信号**\n"
            if order_placed:
                message += f"🎯 **已完成下单**\n"
            else:
                message += f"⏳ **等待手动确认下单**\n"
        else:
            message += f"❌ **未达到信号标准**\n"
            message += f"原因：评分不足 (需 ≥ 7.0)\n"
        
        message += "\n---\n"
        message += "系统自动评分，仅供参考。"
        
        # 根据结果设置通知级别
        if veto:
            level = "warning"
        elif signal_generated and order_placed:
            level = "error"  # 已下单，使用 error 级别
        elif signal_generated:
            level = "warning"
        else:
            level = "info"
        
        return self.send_notification(message, level=level)
    
    def send_coin_summary_report(
        self,
        symbol: str,
        listing_time: datetime,
        scoring_history: list,
        final_score: float,
        signal_generated: bool
    ) -> bool:
        """
        发送新币种评分汇总报告
        
        Args:
            symbol: 币种符号
            listing_time: 上线时间
            scoring_history: 评分历史列表
            final_score: 最终综合评分
            signal_generated: 是否生成了信号
            
        Returns:
            是否发送成功
        """
        # 计算监控时长
        hours_since_listing = (datetime.now() - listing_time).total_seconds() / 3600
        
        # 构建评分历史消息
        history_text = ""
        for i, record in enumerate(scoring_history, 1):
            score = record.get('score', 0)
            timestamp = record.get('timestamp', '')
            signal = record.get('signal_generated', False)
            
            # 解析时间
            try:
                dt = datetime.fromisoformat(timestamp)
                time_str = dt.strftime('%H:%M')
            except:
                time_str = timestamp
            
            history_text += (
                f"{i}️⃣ 第{i}次评分（{time_str}）\n"
                f"   • 综合评分：{score:.1f}/10\n"
                f"   • 信号：{'✅ 已生成' if signal else '❌ 未达标'}\n\n"
            )
        
        # 最终结果
        if signal_generated:
            result_text = "✅ 已生成交易信号"
            recommendation = "建议关注并考虑入场"
        else:
            result_text = "❌ 未生成交易信号"
            recommendation = "建议观望，等待更好时机"
        
        # 构建完整消息
        message = (
            f"🆕 **新币种评分汇总报告**\n\n"
            f"**币种：{symbol}**\n"
            f"**上线时间：{listing_time.strftime('%Y-%m-%d %H:%M')}**\n"
            f"**监控时长：{hours_since_listing:.1f} 小时**\n"
            f"**评分次数：{len(scoring_history)} 次**\n\n"
            f"**📊 评分历史：**\n"
            f"{history_text}\n"
            f"**📈 最终结果：**\n"
            f"• 综合评分：{final_score:.2f}/10\n"
            f"• 交易信号：{result_text}\n"
            f"• 建议操作：{recommendation}\n\n"
            f"---\n"
            f"系统自动监控，如有变化将另行通知。"
        )
        
        # 根据最终评分设置通知级别
        if final_score >= 8.0:
            level = "error"
        elif final_score >= 7.0:
            level = "warning"
        else:
            level = "info"
        
        return self.send_notification(message, level=level)


# 全局推送器实例
feishu_notifier = FeishuNotifier()
