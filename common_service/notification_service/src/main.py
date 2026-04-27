"""
通知服务

提供统一的消息通知服务，支持异步队列和多种消息格式
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
import asyncio
import os

from shared.core.config import settings
from shared.utils.logger import get_logger
from notification_service.core.queue import message_queue
from notification_service.core.worker import worker_pool
from notification_service.api.routes import router
from notification_service.middleware.rate_limiter import RateLimiterMiddleware

logger = get_logger("notification_service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    logger.info("🚀 通知服务启动中...")
    
    # 初始化 Redis 连接
    await message_queue.connect()
    logger.info("✅ Redis 连接成功")
    
    # 启动 Worker 池
    asyncio.create_task(worker_pool.start())
    logger.info(f"✅ Worker 池已启动 ({settings.WORKER_COUNT} 个 Worker)")
    
    logger.info("✅ 通知服务启动完成")
    
    yield
    
    # 关闭时
    logger.info("🛑 通知服务关闭中...")
    
    # 停止 Worker 池
    await worker_pool.stop()
    logger.info("✅ Worker 池已停止")
    
    # 关闭 Redis 连接
    await message_queue.disconnect()
    logger.info("✅ Redis 连接已关闭")
    logger.info("✅ 通知服务已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="通知服务",
    description="统一的消息通知服务，支持异步队列和多种消息格式",
    version=settings.APP_VERSION,
    lifespan=lifespan
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 配置频率限制
app.add_middleware(RateLimiterMiddleware, redis_url=os.getenv("REDIS_URL", "redis://localhost:6379"))

# 注册路由
app.include_router(router, prefix=settings.API_PREFIX)


@app.get("/")
async def root():
    """根路径"""
    return {
        "service": "notification",
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "health": "/api/v1/health"
    }


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG
    )
