"""
异步 Worker 模块

负责从队列中消费消息并发送到飞书
"""

import asyncio
from typing import List, Optional
from datetime import datetime
import os
from shared.core.config import settings
from shared.utils.logger import get_logger
from .queue import message_queue
from .sender import feishu_sender

logger = get_logger("notification_worker")


class NotificationWorker:
    """通知 Worker"""
    
    def __init__(self, worker_id: int = 0):
        """
        初始化 Worker
        
        Args:
            worker_id: Worker ID
        """
        self.worker_id = worker_id
        self.running = False
        self.processed_count = 0
        self.failed_count = 0
        self.last_process_time: Optional[datetime] = None
    
    async def start(self):
        """启动 Worker"""
        self.running = True
        logger.info(f"🚀 Worker {self.worker_id} 启动")
        
        while self.running:
            try:
                # 从队列中获取消息
                message = await message_queue.dequeue(timeout=5)
                
                if message:
                    await self.process_message(message)
                else:
                    # 队列为空，短暂等待
                    await asyncio.sleep(0.1)
                    
            except asyncio.CancelledError:
                logger.info(f"Worker {self.worker_id} 被取消")
                break
            except Exception as e:
                logger.error(f"Worker {self.worker_id} 异常：{e}")
                await asyncio.sleep(1)
        
        logger.info(f"🛑 Worker {self.worker_id} 已停止")
    
    async def process_message(self, message: dict):
        """
        处理单条消息
        
        Args:
            message: 消息字典
        """
        queue_id = message.get("queue_id", "unknown")
        project = message.get("project", "unknown")
        
        logger.debug(f"处理消息：{queue_id}, project: {project}")
        
        try:
            # 获取 webhook URL
            webhook_url = message.get("webhook_url")
            if not webhook_url:
                raise ValueError(f"项目 {project} 的 webhook 未配置")
            
            # 发送消息（带重试）
            success = feishu_sender.send_with_retry(
                webhook_url=webhook_url,
                message=message,
                max_retries=3,
                retry_delay=2
            )
            
            if success:
                self.processed_count += 1
                self.last_process_time = datetime.utcnow()
                logger.info(f"✅ 消息发送成功：{queue_id}")
            else:
                self.failed_count += 1
                logger.error(f"❌ 消息发送失败：{queue_id}")
                
                # 移至失败队列
                await message_queue.move_to_failed(
                    message,
                    error="发送失败，已达到最大重试次数"
                )
                
        except Exception as e:
            self.failed_count += 1
            logger.error(f"❌ 消息处理异常：{queue_id}, error: {e}")
            
            # 检查是否需要重试
            retry_count = message.get("retry_count", 0)
            if retry_count < 3:
                # 重新入队，延迟 2 秒
                await message_queue.requeue(message, delay=2)
                logger.warning(f"消息重新入队：{queue_id}, retry: {retry_count + 1}")
            else:
                # 移至失败队列
                await message_queue.move_to_failed(message, error=str(e))
                logger.error(f"消息移至失败队列：{queue_id}")
    
    def stop(self):
        """停止 Worker"""
        self.running = False
    
    def get_status(self) -> dict:
        """获取 Worker 状态"""
        return {
            "worker_id": self.worker_id,
            "running": self.running,
            "processed_count": self.processed_count,
            "failed_count": self.failed_count,
            "last_process_time": self.last_process_time.isoformat() if self.last_process_time else None
        }


class WorkerPool:
    """Worker 池"""
    
    def __init__(self, worker_count: int = 3):
        """
        初始化 Worker 池
        
        Args:
            worker_count: Worker 数量
        """
        self.worker_count = worker_count
        self.workers: List[NotificationWorker] = []
        self.tasks: List[asyncio.Task] = []
        logger.info(f"Worker 池初始化完成，Worker 数量：{worker_count}")
    
    async def start(self):
        """启动所有 Worker"""
        logger.info(f"🚀 启动 {self.worker_count} 个 Worker")
        
        for i in range(self.worker_count):
            worker = NotificationWorker(worker_id=i)
            self.workers.append(worker)
            
            # 创建异步任务
            task = asyncio.create_task(worker.start())
            self.tasks.append(task)
        
        logger.info(f"✅ 所有 Worker 已启动")
    
    async def stop(self):
        """停止所有 Worker"""
        logger.info("🛑 停止所有 Worker...")
        
        # 停止所有 Worker
        for worker in self.workers:
            worker.stop()
        
        # 等待所有任务完成
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        
        logger.info("✅ 所有 Worker 已停止")
    
    def get_status(self) -> dict:
        """获取 Worker 池状态"""
        workers_status = [worker.get_status() for worker in self.workers]
        
        total_processed = sum(w["processed_count"] for w in workers_status)
        total_failed = sum(w["failed_count"] for w in workers_status)
        active_workers = sum(1 for w in workers_status if w["running"])
        
        return {
            "worker_count": self.worker_count,
            "active_workers": active_workers,
            "total_processed": total_processed,
            "total_failed": total_failed,
            "workers": workers_status
        }


# 全局 Worker 池实例
worker_pool = WorkerPool(worker_count=settings.WORKER_COUNT)
