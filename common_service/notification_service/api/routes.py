"""
通知服务 API 路由

提供消息发送、队列状态查询等接口
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional, List
from datetime import datetime
import os

from shared.core.config import settings
from shared.utils.logger import get_logger
from notification_service.core.queue import message_queue
from notification_service.core.sender import feishu_sender
from notification_service.core.worker import worker_pool

logger = get_logger("notification_api")

router = APIRouter()


# ========== 数据模型 ==========

class SendMessageRequest(BaseModel):
    """发送消息请求"""
    project: str = Field(..., description="项目标识", example="btc_eth")
    message: str = Field(..., description="消息内容", example="测试消息")
    type: str = Field(default="text", description="消息类型", example="text")
    level: str = Field(default="info", description="消息级别", example="info")
    card_data: Optional[Dict[str, Any]] = Field(default=None, description="卡片数据（type=card 时使用）")
    
    class Config:
        schema_extra = {
            "example": {
                "project": "btc_eth",
                "message": "✅ 交易信号：BTCUSDT 买入",
                "type": "markdown",
                "level": "info"
            }
        }


class SendMessageResponse(BaseModel):
    """发送消息响应"""
    code: int
    message: str
    data: Dict[str, Any]


class QueueStatusResponse(BaseModel):
    """队列状态响应"""
    code: int
    message: str
    data: Dict[str, Any]


class HealthResponse(BaseModel):
    """健康检查响应"""
    code: int
    message: str
    data: Dict[str, Any]


# ========== API 路由 ==========

@router.post("/send", response_model=SendMessageResponse)
async def send_message(request: SendMessageRequest):
    """
    发送消息
    
    将消息放入队列，异步发送到飞书
    """
    try:
        # 验证项目配置
        webhook_url = settings.all_webhooks.get(request.project)
        if not webhook_url:
            raise HTTPException(status_code=400, detail=f"项目 {request.project} 的 webhook 未配置")
        
        # 构建消息
        message = {
            "project": request.project,
            "message": request.message,
            "type": request.type,
            "level": request.level,
            "webhook_url": webhook_url,
            "sent_at": datetime.utcnow().isoformat()
        }
        
        if request.card_data:
            message["card_data"] = request.card_data
        
        # 消息入队
        success = await message_queue.enqueue(message)
        
        if not success:
            raise HTTPException(status_code=500, detail="消息入队失败")
        
        # 获取队列状态
        status = await message_queue.get_status()
        
        return SendMessageResponse(
            code=0,
            message="Message queued",
            data={
                "queue_id": message.get("queue_id"),
                "queued_at": message.get("queued_at"),
                "queue_size": status.get("queue_size", 0)
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"发送消息失败：{e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/queue/status", response_model=QueueStatusResponse)
async def get_queue_status():
    """获取队列状态"""
    try:
        status = await message_queue.get_status()
        
        return QueueStatusResponse(
            code=0,
            message="success",
            data=status
        )
    except Exception as e:
        logger.error(f"获取队列状态失败：{e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/worker/status")
async def get_worker_status():
    """获取 Worker 状态"""
    try:
        status = worker_pool.get_status()
        
        return {
            "code": 0,
            "message": "success",
            "data": status
        }
    except Exception as e:
        logger.error(f"获取 Worker 状态失败：{e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """健康检查"""
    try:
        # 检查 Redis 连接
        redis_connected = await message_queue.is_connected()
        
        # 获取队列状态
        status = await message_queue.get_status()
        
        # 检查 webhook 配置
        webhooks_configured = sum(1 for v in settings.all_webhooks.values() if v)
        
        data = {
            "status": "healthy" if redis_connected else "degraded",
            "service": "notification",
            "version": settings.APP_VERSION,
            "redis_connected": redis_connected,
            "webhooks_configured": webhooks_configured,
            "queue_size": status.get("queue_size", 0),
            "failed_size": status.get("failed_size", 0)
        }
        
        return HealthResponse(
            code=0,
            message="ok" if redis_connected else "degraded",
            data=data
        )
    except Exception as e:
        logger.error(f"健康检查失败：{e}")
        return HealthResponse(
            code=500,
            message="error",
            data={
                "status": "error",
                "error": str(e)
            }
        )


# ========== 辅助函数 ==========

def validate_project(project: str) -> bool:
    """验证项目标识"""
    return project in settings.all_webhooks


def get_webhook_url(project: str) -> Optional[str]:
    """获取项目的 webhook URL"""
    return settings.all_webhooks.get(project)
