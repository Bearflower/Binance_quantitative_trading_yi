"""
飞书消息发送器

支持文本、Markdown、卡片消息格式
"""

import requests
from typing import Dict, Any, Optional
from datetime import datetime
import os
from shared.utils.logger import get_logger

logger = get_logger("feishu_sender")


class FeishuSender:
    """飞书消息发送器"""
    
    def __init__(self):
        """初始化飞书发送器"""
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json"
        })
        logger.info("飞书发送器初始化完成")
    
    def send(self, webhook_url: str, message: Dict[str, Any]) -> bool:
        """
        发送消息到飞书
        
        Args:
            webhook_url: Webhook URL
            message: 消息字典
        
        Returns:
            是否发送成功
        """
        try:
            msg_type = message.get("type", "text")
            content = message.get("message", "")
            
            if msg_type == "text":
                payload = self._build_text_message(content)
            elif msg_type == "markdown":
                payload = self._build_markdown_message(content)
            elif msg_type == "card":
                card_data = message.get("card_data", {})
                payload = self._build_card_message(card_data)
            else:
                logger.error(f"不支持的消息类型：{msg_type}")
                return False
            
            # 发送请求
            response = self.session.post(webhook_url, json=payload, timeout=10)
            response.raise_for_status()
            
            # 检查响应
            result = response.json()
            if result.get("StatusCode") == 0 or result.get("code") == 0:
                logger.info(f"飞书消息发送成功：{message.get('project')}")
                return True
            else:
                logger.error(f"飞书消息发送失败：{result}")
                return False
                
        except requests.exceptions.Timeout:
            logger.error("飞书消息发送超时")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"飞书消息发送异常：{e}")
            return False
        except Exception as e:
            logger.error(f"飞书消息发送错误：{e}")
            return False
    
    def _build_text_message(self, content: str) -> Dict[str, Any]:
        """构建文本消息"""
        return {
            "msg_type": "text",
            "content": {
                "text": content
            }
        }
    
    def _build_markdown_message(self, content: str) -> Dict[str, Any]:
        """构建 Markdown 消息"""
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": "📢 通知消息"
                    },
                    "template": "blue"
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content
                    }
                ]
            }
        }
    
    def _build_card_message(self, card_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        构建卡片消息
        
        Args:
            card_data: 卡片数据字典，应包含 header, elements 等
        
        Returns:
            飞书卡片消息
        """
        # 如果提供了完整的卡片数据，直接使用
        if "header" in card_data and "elements" in card_data:
            return {
                "msg_type": "interactive",
                "card": card_data
            }
        
        # 否则构建默认卡片
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": card_data.get("title", "通知消息")
                    },
                    "template": card_data.get("template", "blue")
                },
                "elements": card_data.get("elements", [])
            }
        }
    
    def send_with_retry(self, webhook_url: str, message: Dict[str, Any], 
                       max_retries: int = 3, retry_delay: int = 2) -> bool:
        """
        发送消息（带重试）
        
        Args:
            webhook_url: Webhook URL
            message: 消息字典
            max_retries: 最大重试次数
            retry_delay: 重试延迟（秒）
        
        Returns:
            是否发送成功
        """
        import time
        
        for attempt in range(max_retries):
            try:
                success = self.send(webhook_url, message)
                if success:
                    return True
                
                # 失败后等待重试
                if attempt < max_retries - 1:
                    delay = retry_delay * (attempt + 1)  # 递增延迟
                    logger.warning(f"消息发送失败，{delay}秒后重试 (尝试 {attempt+1}/{max_retries})")
                    time.sleep(delay)
            except Exception as e:
                logger.error(f"重试异常：{e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
        
        logger.error(f"消息发送失败，已达到最大重试次数 {max_retries}")
        return False


# 全局飞书发送器实例
feishu_sender = FeishuSender()
