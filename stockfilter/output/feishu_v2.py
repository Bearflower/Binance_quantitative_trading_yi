"""
飞书推送模块（已改造为使用通用通知服务）
负责通过通用通知服务发送股票筛选结果
"""

import requests
import os
from typing import Dict, Any, Optional, List
from datetime import datetime

from utils.logger import get_logger
from data.database import DatabaseManager

logger = get_logger()

# 通用通知服务配置
NOTIFICATION_SERVICE_URL = os.getenv('NOTIFICATION_SERVICE_URL', 'http://43.156.242.184:8766/api/v1')
NOTIFICATION_PROJECT = 'stock'


class StockNotifier:
    """股票通知器（使用通用通知服务）"""

    def __init__(self):
        """初始化股票通知器"""
        self.notification_url = f"{NOTIFICATION_SERVICE_URL}/send"
        
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
                    logger.info(f"通知发送成功：{message[:50]}...")
                    return True
                else:
                    logger.error(f"通知发送失败：{result}")
                    return False
            else:
                logger.error(f"通知服务 HTTP 错误：{response.status_code}")
                return False
        
        except Exception as e:
            logger.error(f"通知发送异常：{e}")
            return False

    def send_stock_signal(self, stock_info: Dict[str, Any]) -> bool:
        """
        发送股票筛选信号
        
        Args:
            stock_info: 股票信息字典
        
        Returns:
            是否发送成功
        """
        try:
            surge_date = stock_info.get('surge_date', '')
            if isinstance(surge_date, (datetime,)):
                surge_date = surge_date.strftime('%Y-%m-%d')
            
            score = stock_info.get('score', 0)
            support_level = stock_info.get('support_level', 0)
            current_close = stock_info.get('current_close', 0)
            stop_loss_price = support_level * 0.97
            
            # 构建简洁的文本消息
            message = f"""📈 新股票筛选信号

{stock_info.get('name', '')} ({stock_info.get('code', '')})

📊 形态评分：{score:.2f}

📈 关键指标：
• 放量日期：{surge_date}
• 当前价：{current_close:.2f} 元
• 支撑位：{support_level:.2f} 元
• 放量涨幅：{stock_info.get('surge_pct', 0):.2%}
• 放量倍数：{stock_info.get('surge_volume_ratio', 0):.2f} 倍

💡 交易建议：
• 建议买入价：{current_close:.2f} 元（次日开盘）
• 止损价：{stop_loss_price:.2f} 元
• 回踩低点：{stock_info.get('low_after_surge', 0):.2f} 元

⚠️ 风险提示：以上信息仅供参考，不构成投资建议"""
            
            return self.send_notification(message, level="info")
            
        except Exception as e:
            logger.error(f"构建股票信号消息失败：{e}")
            return False

    def send_daily_summary(self, signals: List[Dict]) -> bool:
        """
        发送每日汇总
        
        Args:
            signals: 信号列表
        
        Returns:
            是否发送成功
        """
        try:
            if not signals:
                message = "📊 今日股票形态扫描\n\n今日无符合形态的买入信号\n\n继续监控中..."
                return self.send_notification(message, level="info")
            
            # 构建汇总消息
            message = f"📊 今日股票形态扫描\n\n共筛选出 {len(signals)} 只股票，建议开盘后择机买入（高开>5% 请放弃）\n\n⚠️ 风险提示：支撑位×0.97 为止损价，移动止盈回撤 8%\n\n"
            
            for idx, sig in enumerate(signals[:5], 1):  # 最多显示 5 只
                message += f"{idx}. {sig.get('name', '')} ({sig.get('code', '')})\n"
                message += f"   支撑位：{sig.get('support_level', 0):.2f}元 | 止损：{sig.get('support_level', 0) * 0.97:.2f}元\n\n"
            
            if len(signals) > 5:
                message += f"... 还有 {len(signals) - 5} 只股票，请查看详细报告\n"
            
            return self.send_notification(message, level="info")
            
        except Exception as e:
            logger.error(f"构建每日汇总消息失败：{e}")
            return False

    def send_position_update(self, positions: List[Dict]) -> bool:
        """
        发送持仓更新
        
        Args:
            positions: 持仓列表
        
        Returns:
            是否发送成功
        """
        try:
            if not positions:
                return True
            
            content = "📊 持仓日报\n\n"
            
            for pos in positions:
                pnl = pos.get('pnl', 0)
                pnl_color = '盈利' if pnl >= 0 else '亏损'
                
                content += f"{pos.get('name', '')} ({pos.get('code', '')})\n"
                content += f"• 持仓价：{pos.get('entry_price', 0):.2f} 现价：{pos.get('current_price', 0):.2f}\n"
                content += f"• 盈亏：{pnl:.2f} ({pos.get('pnl_pct', 0):.2%}) [{pnl_color}]\n\n"
            
            return self.send_notification(content, level="info")
            
        except Exception as e:
            logger.error(f"构建持仓更新消息失败：{e}")
            return False


def notify_new_signal(stock_info: Dict, db: Optional[DatabaseManager] = None) -> bool:
    """
    推送新筛选股票
    
    Args:
        stock_info: 股票信息
        db: 数据库管理器（用于去重）
    
    Returns:
        是否推送成功
    """
    code = stock_info.get('code', '')
    today = datetime.now().strftime('%Y-%m-%d')
    
    if db and db.has_pushed_today(code, today):
        logger.info(f"{code} 今日已推送，跳过")
        return False
    
    notifier = StockNotifier()
    success = notifier.send_stock_signal(stock_info)
    
    if success and db:
        db.insert_push_record(code, today, 'new_signal')
    
    return success


def notify_daily_summary(signals: List[Dict]) -> bool:
    """
    推送每日汇总
    
    Args:
        signals: 信号列表
    
    Returns:
        是否推送成功
    """
    notifier = StockNotifier()
    return notifier.send_daily_summary(signals)
