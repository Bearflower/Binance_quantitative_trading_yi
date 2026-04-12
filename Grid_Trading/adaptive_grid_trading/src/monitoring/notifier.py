"""
报警通知器
支持飞书、钉钉、Telegram 等报警渠道
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class AlertNotifier:
    """报警通知器"""
    
    def __init__(
        self,
        feishu_webhook: Optional[str] = None,
        dingding_webhook: Optional[str] = None,
        telegram_bot_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        enabled: bool = True
    ):
        """
        初始化报警通知器
        
        Args:
            feishu_webhook: 飞书机器人 Webhook
            dingding_webhook: 钉钉机器人 Webhook
            telegram_bot_token: Telegram Bot Token
            telegram_chat_id: Telegram 聊天 ID
            enabled: 是否启用
        """
        self.feishu_webhook = feishu_webhook
        self.dingding_webhook = dingding_webhook
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.enabled = enabled
        
        # 报警历史
        self._alert_history: List[Dict] = []
        
        # 频率限制（防止报警轰炸）
        self._last_alert_time: Dict[str, datetime] = {}
        self._alert_cooldown = 60  # 秒
    
    def send_feishu(
        self,
        title: str,
        content: str,
        alert_type: str = "info"
    ) -> bool:
        """
        发送飞书消息
        
        Args:
            title: 标题
            content: 内容
            alert_type: 类型 (info/warning/error)
            
        Returns:
            是否发送成功
        """
        if not self.enabled or not self.feishu_webhook:
            logger.debug("飞书报警未启用或配置缺失")
            return False
        
        # 检查频率限制
        if not self._check_rate_limit(f"feishu_{alert_type}"):
            logger.warning(f"飞书报警频率限制：{title}")
            return False
        
        # 根据类型设置表情
        emojis = {
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "🚨"
        }
        emoji = emojis.get(alert_type, "ℹ️")
        
        # 构建消息（飞书支持富文本）
        message = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": [
                            [
                                {
                                    "tag": "text",
                                    "text": f"{emoji} {content}"
                                }
                            ],
                            [
                                {
                                    "tag": "text",
                                    "text": f"\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                                }
                            ]
                        ]
                    }
                }
            }
        }
        
        try:
            response = requests.post(
                self.feishu_webhook,
                json=message,
                timeout=5
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('StatusCode') == 0 or result.get('code') == 0:
                    logger.info(f"飞书报警发送成功：{title}")
                    self._record_alert(f"feishu_{alert_type}")
                    return True
                else:
                    logger.error(f"飞书报警失败：{result}")
            else:
                logger.error(f"飞书报警 HTTP 错误：{response.status_code}")
                
        except Exception as e:
            logger.error(f"飞书报警异常：{str(e)}")
        
        return False
    
    def send_dingding(
        self,
        title: str,
        content: str,
        alert_type: str = "info"
    ) -> bool:
        """
        发送钉钉消息
        
        Args:
            title: 标题
            content: 内容
            alert_type: 类型 (info/warning/error)
            
        Returns:
            是否发送成功
        """
        if not self.enabled or not self.dingding_webhook:
            logger.debug("钉钉报警未启用或配置缺失")
            return False
        
        # 检查频率限制
        if not self._check_rate_limit(f"dingding_{alert_type}"):
            logger.warning(f"钉钉报警频率限制：{title}")
            return False
        
        # 根据类型设置颜色
        colors = {
            "info": "#0099ff",
            "warning": "#ff9900",
            "error": "#ff0000"
        }
        color = colors.get(alert_type, "#0099ff")
        
        # 构建消息
        message = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"## {title}\n\n{content}\n\n> 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            }
        }
        
        try:
            response = requests.post(
                self.dingding_webhook,
                json=message,
                timeout=5
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('errcode') == 0:
                    logger.info(f"钉钉报警发送成功：{title}")
                    self._record_alert(f"dingding_{alert_type}")
                    return True
                else:
                    logger.error(f"钉钉报警失败：{result}")
            else:
                logger.error(f"钉钉报警 HTTP 错误：{response.status_code}")
                
        except Exception as e:
            logger.error(f"钉钉报警异常：{str(e)}")
        
        return False
    
    def send_telegram(
        self,
        message: str,
        alert_type: str = "info"
    ) -> bool:
        """
        发送 Telegram 消息
        
        Args:
            message: 消息内容
            alert_type: 类型 (info/warning/error)
            
        Returns:
            是否发送成功
        """
        if not self.enabled or not self.telegram_bot_token or not self.telegram_chat_id:
            logger.debug("Telegram 报警未启用或配置缺失")
            return False
        
        # 检查频率限制
        if not self._check_rate_limit(f"telegram_{alert_type}"):
            logger.warning(f"Telegram 报警频率限制：{message[:50]}")
            return False
        
        # 添加表情符号
        emojis = {
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "🚨"
        }
        emoji = emojis.get(alert_type, "ℹ️")
        
        full_message = f"{emoji} {message}\n\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        
        try:
            response = requests.post(
                url,
                json={
                    'chat_id': self.telegram_chat_id,
                    'text': full_message,
                    'parse_mode': 'HTML'
                },
                timeout=5
            )
            
            if response.status_code == 200:
                logger.info(f"Telegram 报警发送成功")
                self._record_alert(f"telegram_{alert_type}")
                return True
            else:
                logger.error(f"Telegram 报警 HTTP 错误：{response.status_code}")
                
        except Exception as e:
            logger.error(f"Telegram 报警异常：{str(e)}")
        
        return False
    
    def send_alert(
        self,
        title: str,
        content: str,
        level: str = "info",
        channels: Optional[List[str]] = None
    ) -> bool:
        """
        发送报警（多通道）
        
        Args:
            title: 标题
            content: 内容
            level: 级别 (info/warning/error)
            channels: 通道列表 (['dingding', 'telegram'], None 表示全部)
            
        Returns:
            是否至少有一个通道发送成功
        """
        if not self.enabled:
            return False
        
        if channels is None:
            channels = []
            if self.feishu_webhook:
                channels.append('feishu')
            if self.dingding_webhook:
                channels.append('dingding')
            if self.telegram_bot_token and self.telegram_chat_id:
                channels.append('telegram')
        
        success = False
        
        for channel in channels:
            if channel == 'feishu':
                if self.send_feishu(title, content, level):
                    success = True
            elif channel == 'dingding':
                if self.send_dingding(title, content, level):
                    success = True
            elif channel == 'telegram':
                if self.send_telegram(f"{title}\n\n{content}", level):
                    success = True
        
        return success
    
    def notify_state_change(
        self,
        old_state: str,
        new_state: str,
        price: float,
        adx: float
    ) -> None:
        """
        通知市场状态变化
        
        Args:
            old_state: 旧状态
            new_state: 新状态
            price: 当前价格
            adx: ADX 值
        """
        title = "🔄 市场状态变更"
        content = (
            f"**状态变化**: {old_state} → {new_state}\n"
            f"**当前价格**: {price}\n"
            f"**ADX**: {adx:.2f}"
        )
        
        self.send_alert(title, content, level="warning")
    
    def notify_risk_event(
        self,
        event_type: str,
        trigger_price: float,
        trigger_pnl: float,
        action: str
    ) -> None:
        """
        通知风险事件
        
        Args:
            event_type: 事件类型
            trigger_price: 触发价格
            trigger_pnl: 触发盈亏
            action: 行动
        """
        title = "🚨 风险事件触发"
        content = (
            f"**事件类型**: {event_type}\n"
            f"**触发价格**: {trigger_price}\n"
            f"**触发盈亏**: {trigger_pnl:.2%}\n"
            f"**执行行动**: {action}"
        )
        
        self.send_alert(title, content, level="error")
    
    def notify_grid_created(
        self,
        grid_id: str,
        upper_price: float,
        lower_price: float,
        grid_count: int,
        investment: float
    ) -> None:
        """
        通知网格创建
        
        Args:
            grid_id: 网格 ID
            upper_price: 上边界
            lower_price: 下边界
            grid_count: 网格数量
            investment: 投资金额
        """
        title = "📊 网格创建成功"
        content = (
            f"**网格 ID**: {grid_id}\n"
            f"**价格区间**: {lower_price} - {upper_price}\n"
            f"**网格数量**: {grid_count}\n"
            f"**投资金额**: {investment}"
        )
        
        self.send_alert(title, content, level="info")
    
    def notify_grid_terminated(
        self,
        grid_id: str,
        profit: float = 0.0
    ) -> None:
        """
        通知网格终止
        
        Args:
            grid_id: 网格 ID
            profit: 实现盈亏
        """
        title = "🛑 网格已终止"
        profit_str = f"+{profit:.2f}" if profit >= 0 else f"{profit:.2f}"
        content = (
            f"**网格 ID**: {grid_id}\n"
            f"**实现盈亏**: {profit_str} USDT"
        )
        
        self.send_alert(title, content, level="warning")
    
    def notify_error(
        self,
        error_type: str,
        error_message: str,
        details: Optional[str] = None
    ) -> None:
        """
        通知系统错误
        
        Args:
            error_type: 错误类型
            error_message: 错误消息
            details: 详细信息
        """
        title = "❌ 系统错误"
        content = f"**错误类型**: {error_type}\n\n**错误消息**: {error_message}"
        
        if details:
            content += f"\n\n**详细信息**: {details}"
        
        self.send_alert(title, content, level="error")
    
    def _check_rate_limit(self, key: str) -> bool:
        """
        检查频率限制
        
        Args:
            key: 限制键
            
        Returns:
            是否允许发送
        """
        now = datetime.now()
        
        if key in self._last_alert_time:
            elapsed = (now - self._last_alert_time[key]).total_seconds()
            if elapsed < self._alert_cooldown:
                return False
        
        return True
    
    def _record_alert(self, key: str) -> None:
        """
        记录报警时间
        
        Args:
            key: 限制键
        """
        self._last_alert_time[key] = datetime.now()
        
        # 记录历史
        self._alert_history.append({
            'key': key,
            'time': datetime.now()
        })
        
        # 限制历史记录
        if len(self._alert_history) > 1000:
            self._alert_history.pop(0)
    
    def get_alert_history(self, limit: int = 50) -> List[Dict]:
        """
        获取报警历史
        
        Args:
            limit: 数量限制
            
        Returns:
            报警历史列表
        """
        return self._alert_history[-limit:]
    
    def set_enabled(self, enabled: bool) -> None:
        """
        设置启用状态
        
        Args:
            enabled: 是否启用
        """
        self.enabled = enabled
        logger.info(f"报警通知器已{'启用' if enabled else '禁用'}")
