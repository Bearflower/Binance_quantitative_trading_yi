"""
Redis 消息队列模块

提供异步消息队列功能，支持消息入队、出队和队列管理
"""

import redis.asyncio as redis
import json
from typing import Dict, Any, Optional, List
from datetime import datetime
import os
from shared.utils.logger import get_logger

logger = get_logger("notification_queue")


class MessageQueue:
    """异步消息队列"""
    
    _instance: Optional["MessageQueue"] = None
    _redis: Optional[redis.Redis] = None
    
    def __new__(cls) -> "MessageQueue":
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """初始化消息队列"""
        if self._redis is None:
            redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
            self._redis = redis.from_url(
                redis_url,
                encoding="utf-8",
                decode_responses=True
            )
            self.queue_name = "notification_queue"
            self.failed_queue_name = "notification_failed"
            logger.info(f"消息队列初始化完成，Redis: {redis_url}")
    
    async def connect(self) -> None:
        """连接 Redis"""
        try:
            await self._redis.ping()
            logger.info("Redis 连接成功")
        except Exception as e:
            logger.error(f"Redis 连接失败：{e}")
            raise
    
    async def disconnect(self) -> None:
        """断开 Redis 连接"""
        if self._redis:
            await self._redis.close()
            logger.info("Redis 连接已关闭")
    
    async def enqueue(self, message: Dict[str, Any]) -> bool:
        """
        消息入队
        
        Args:
            message: 消息字典，包含 project, message, type, level 等字段
        
        Returns:
            是否成功入队
        """
        try:
            # 添加元数据
            message["queue_id"] = f"msg_{datetime.utcnow().timestamp()}_{id(message)}"
            message["queued_at"] = datetime.utcnow().isoformat()
            message["retry_count"] = 0
            
            # 序列化并入队
            await self._redis.lpush(self.queue_name, json.dumps(message, ensure_ascii=False))
            logger.debug(f"消息入队成功：{message['queue_id']}, project: {message.get('project')}")
            return True
        except Exception as e:
            logger.error(f"消息入队失败：{e}")
            return False
    
    async def dequeue(self, timeout: int = 5) -> Optional[Dict[str, Any]]:
        """
        消息出队（阻塞式）
        
        Args:
            timeout: 阻塞超时时间（秒）
        
        Returns:
            消息字典，如果超时则返回 None
        """
        try:
            result = await self._redis.brpop(self.queue_name, timeout=timeout)
            if result:
                _, message_json = result
                message = json.loads(message_json)
                logger.debug(f"消息出队成功：{message.get('queue_id')}")
                return message
            return None
        except Exception as e:
            logger.error(f"消息出队失败：{e}")
            return None
    
    async def requeue(self, message: Dict[str, Any], delay: int = 0) -> bool:
        """
        重新入队（用于重试）
        
        Args:
            message: 消息字典
            delay: 延迟时间（秒），0 表示立即
        
        Returns:
            是否成功重新入队
        """
        try:
            message["retry_count"] = message.get("retry_count", 0) + 1
            message["last_retry_at"] = datetime.utcnow().isoformat()
            
            if delay > 0:
                # 延迟队列（使用 sorted set）
                await self._redis.zadd(
                    f"{self.queue_name}_delayed",
                    {json.dumps(message, ensure_ascii=False): datetime.utcnow().timestamp() + delay}
                )
                logger.debug(f"消息延迟入队：{message.get('queue_id')}, 延迟：{delay}s")
            else:
                # 立即入队
                await self._redis.lpush(self.queue_name, json.dumps(message, ensure_ascii=False))
                logger.debug(f"消息重新入队：{message.get('queue_id')}")
            
            return True
        except Exception as e:
            logger.error(f"消息重新入队失败：{e}")
            return False
    
    async def move_to_failed(self, message: Dict[str, Any], error: str) -> bool:
        """
        移动到失败队列
        
        Args:
            message: 消息字典
            error: 错误信息
        
        Returns:
            是否成功移动到失败队列
        """
        try:
            message["failed_at"] = datetime.utcnow().isoformat()
            message["error"] = error
            
            await self._redis.lpush(self.failed_queue_name, json.dumps(message, ensure_ascii=False))
            logger.warning(f"消息移至失败队列：{message.get('queue_id')}, error: {error}")
            return True
        except Exception as e:
            logger.error(f"移动至失败队列失败：{e}")
            return False
    
    async def size(self) -> int:
        """获取队列长度"""
        return await self._redis.llen(self.queue_name)
    
    async def failed_size(self) -> int:
        """获取失败队列长度"""
        return await self._redis.llen(self.failed_queue_name)
    
    async def get_status(self) -> Dict[str, Any]:
        """
        获取队列状态
        
        Returns:
            队列状态字典
        """
        try:
            queue_size = await self.size()
            failed_size = await self.failed_size()
            
            return {
                "queue_size": queue_size,
                "failed_size": failed_size,
                "pending": queue_size,
                "failed": failed_size
            }
        except Exception as e:
            logger.error(f"获取队列状态失败：{e}")
            return {
                "queue_size": 0,
                "failed_size": 0,
                "error": str(e)
            }
    
    async def clear_queue(self) -> bool:
        """清空队列"""
        try:
            await self._redis.delete(self.queue_name)
            logger.info("队列已清空")
            return True
        except Exception as e:
            logger.error(f"清空队列失败：{e}")
            return False
    
    async def is_connected(self) -> bool:
        """检查 Redis 连接状态"""
        try:
            await self._redis.ping()
            return True
        except:
            return False


# 全局消息队列实例
message_queue = MessageQueue()
