"""
飞书推送模块
负责通过飞书 webhook 发送卡片消息
"""

import requests
import os
from typing import Dict, Any, Optional, List
from datetime import datetime

from utils.logger import get_logger
from data.database import DatabaseManager

logger = get_logger()


class FeishuNotifier:
    """飞书通知器"""

    def __init__(self, webhook_url: Optional[str] = None):
        """
        初始化飞书通知器
        
        Args:
            webhook_url: 飞书 webhook URL，如不提供则从环境变量读取
        """
        self.webhook_url = webhook_url or os.getenv('FEISHU_WEBHOOK')
        
        if not self.webhook_url:
            logger.warning("飞书 webhook URL 未配置，推送功能将不生效")

    def send_card_message(self, stock_info: Dict[str, Any]) -> bool:
        """
        发送股票筛选结果卡片消息
        
        Args:
            stock_info: 股票信息字典
        
        Returns:
            是否发送成功
        """
        if not self.webhook_url:
            return False
        
        try:
            message = self._build_card(stock_info)
            
            response = requests.post(
                self.webhook_url,
                json=message,
                headers={'Content-Type': 'application/json'},
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('StatusCode') == 0 or result.get('code') == 0:
                    logger.info(f"飞书推送成功：{stock_info.get('code')} {stock_info.get('name', '')}")
                    return True
                else:
                    logger.error(f"飞书推送失败：{result}")
                    return False
            else:
                logger.error(f"飞书推送 HTTP 错误：{response.status_code}")
                return False
        
        except Exception as e:
            logger.error(f"飞书推送异常：{e}")
            return False

    def _build_card(self, stock_info: Dict[str, Any]) -> Dict:
        """构建飞书卡片消息"""
        
        surge_date = stock_info.get('surge_date', '')
        if isinstance(surge_date, (datetime, pd.Timestamp)):
            surge_date = surge_date.strftime('%Y-%m-%d')
        
        score = stock_info.get('score', 0)
        score_color = 'red' if score >= 80 else 'orange' if score >= 70 else 'blue'
        
        support_level = stock_info.get('support_level', 0)
        current_close = stock_info.get('current_close', 0)
        stop_loss_price = support_level * 0.97
        
        content = f"""**{stock_info.get('name', '')}** ({stock_info.get('code', '')})

📊 形态评分：<font color=\"{score_color}\">{score:.2f}</font>

📈 关键指标：
• 放量日期：{surge_date}
• 当前价：{current_close:.2f} 元
• 支撑位：{support_level:.2f} 元
• 放量涨幅：{stock_info.get('surge_pct', 0):.2%}
• 放量倍数：{stock_info.get('surge_volume_ratio', 0):.2f} 倍

💡 交易建议：
• 建议买入价：{current_close:.2f} 元（次日开盘）
• 止损价：{stop_loss_price:.2f} 元
• 回踩低点：{stock_info.get('low_after_surge', 0):.2f} 元"""

        message = {
            "msg_type": "interactive",
            "card": {
                "config": {
                    "wide_screen_mode": True
                },
                "header": {
                    "template": "blue",
                    "title": {
                        "tag": "plain_text",
                        "content": "📈 新筛选股票提醒"
                    }
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": content
                        }
                    },
                    {
                        "tag": "hr"
                    },
                    {
                        "tag": "note",
                        "elements": [
                            {
                                "tag": "plain_text",
                                "content": "风险提示：以上信息仅供参考，不构成投资建议"
                            }
                        ]
                    }
                ]
            }
        }
        
        return message

    def send_position_update(self, positions: List[Dict]) -> bool:
        """
        发送持仓更新
        
        Args:
            positions: 持仓列表
        
        Returns:
            是否发送成功
        """
        if not self.webhook_url or not positions:
            return False
        
        try:
            content = "📊 **持仓日报**\n\n"
            
            for pos in positions:
                pnl = pos.get('pnl', 0)
                pnl_color = 'red' if pnl >= 0 else 'green'
                
                content += f"**{pos.get('name', '')}** ({pos.get('code', '')})\n"
                content += f"• 持仓价：{pos.get('entry_price', 0):.2f} 现价：{pos.get('current_price', 0):.2f}\n"
                content += f"• 盈亏：<font color=\"{pnl_color}\">{pnl:.2f} ({pos.get('pnl_pct', 0):.2%})</font>\n\n"
            
            message = {
                "msg_type": "text",
                "content": {
                    "text": content
                }
            }
            
            response = requests.post(
                self.webhook_url,
                json=message,
                timeout=10
            )
            
            if response.status_code == 200:
                logger.info(f"持仓更新推送成功，共 {len(positions)} 只")
                return True
            else:
                logger.error(f"持仓更新推送失败：{response.status_code}")
                return False
        
        except Exception as e:
            logger.error(f"持仓更新推送异常：{e}")
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
    
    notifier = FeishuNotifier()
    success = notifier.send_card_message(stock_info)
    
    if success and db:
        db.insert_push_record(code, today, 'new_signal')
    
    return success
