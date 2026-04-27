"""
报警通知器（已改造为使用通用通知服务）
支持通过通用通知服务发送飞书消息
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# 通用通知服务配置
NOTIFICATION_SERVICE_URL = os.getenv('NOTIFICATION_SERVICE_URL', 'http://43.156.242.184:8766/api/v1')
NOTIFICATION_PROJECT = 'grid'


class AlertNotifier:
    """报警通知器（使用通用通知服务）"""
    
    def __init__(
        self,
        enabled: bool = True
    ):
        """
        初始化报警通知器
        
        Args:
            enabled: 是否启用
        """
        self.enabled = enabled
        self.notification_url = f"{NOTIFICATION_SERVICE_URL}/send"
        
        # 报警历史
        self._alert_history: List[Dict] = []
        
        # 频率限制（防止报警轰炸）
        self._last_alert_time: Dict[str, datetime] = {}
        self._alert_cooldown = 60  # 秒
    
    def send_notification(
        self,
        message: str,
        level: str = "info"
    ) -> bool:
        """
        发送通知到通用服务
        
        Args:
            message: 消息内容
            level: 通知级别 (info, warning, error)
            
        Returns:
            是否发送成功
        """
        if not self.enabled:
            logger.debug("报警通知器未启用")
            return False
        
        # 检查频率限制
        if not self._check_rate_limit(f"{NOTIFICATION_PROJECT}_{level}"):
            logger.warning(f"报警频率限制：{message[:50]}")
            return False
        
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
                    logger.info(f"报警发送成功：{message[:50]}...")
                    self._record_alert(f"{NOTIFICATION_PROJECT}_{level}")
                    return True
                else:
                    logger.error(f"报警发送失败：{result}")
            else:
                logger.error(f"报警服务 HTTP 错误：{response.status_code}")
                
        except Exception as e:
            logger.error(f"报警发送异常：{str(e)}")
        
        return False
    
    def send_alert(
        self,
        title: str,
        content: str,
        level: str = "info"
    ) -> bool:
        """
        发送报警
        
        Args:
            title: 标题
            content: 内容
            level: 级别 (info/warning/error)
            
        Returns:
            是否发送成功
        """
        # 构建完整消息
        full_message = f"**{title}**\n\n{content}\n\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        return self.send_notification(full_message, level=level)
    
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
            f"**投资金额**: {investment} USDT"
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
