"""
频率控制中间件

限制每个项目的消息发送频率
"""

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import redis.asyncio as redis
import time
from typing import Dict, Any
import os
from shared.core.config import settings
from shared.utils.logger import get_logger

logger = get_logger("rate_limiter")


class RateLimiterMiddleware(BaseHTTPMiddleware):
    """频率限制中间件"""
    
    def __init__(self, app, redis_url: str = None):
        super().__init__(app)
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379")
        self.redis: redis.Redis = None
        self.rate_limit = settings.RATE_LIMIT_PER_MINUTE  # 每分钟限制
        self.window_size = 60  # 时间窗口（秒）
    
    async def dispatch(self, request: Request, call_next):
        """处理请求"""
        # 只对发送消息接口进行限流
        if request.url.path == f"{settings.API_PREFIX}/send" and request.method == "POST":
            try:
                # 获取项目标识（从 URL 参数或 body）
                project = request.query_params.get("project", "unknown")
                
                # 检查频率限制
                allowed, remaining = await self.is_allowed(project)
                
                if not allowed:
                    logger.warning(f"频率限制：{project}, remaining: {remaining}")
                    return JSONResponse(
                        status_code=429,
                        content={
                            "code": 429,
                            "message": "Rate limit exceeded",
                            "data": {
                                "retry_after": self.window_size,
                                "limit": self.rate_limit,
                                "remaining": remaining
                            }
                        }
                    )
                
                # 添加响应头
                response = await call_next(request)
                response.headers["X-RateLimit-Limit"] = str(self.rate_limit)
                response.headers["X-RateLimit-Remaining"] = str(remaining)
                
                return response
                
            except Exception as e:
                import traceback
                import sys
                print(f"频率控制异常：{e}", file=sys.stderr)
                print(f"异常详情：{traceback.format_exc()}", file=sys.stderr)
                logger.error(f"频率控制异常：{type(e).__name__}: {e}")
                # 如果频率控制失败，允许请求通过（fail-open）
                return await call_next(request)
        else:
            # 非发送接口，直接通过
            return await call_next(request)
    
    async def is_allowed(self, project: str) -> tuple:
        """
        检查是否允许请求
        
        Args:
            project: 项目标识
        
        Returns:
            (是否允许，剩余次数)
        """
        try:
            # 连接 Redis
            if not self.redis:
                self.redis = await redis.from_url(
                    self.redis_url,
                    encoding="utf-8",
                    decode_responses=True
                )
            
            # 生成 key
            current_time = int(time.time())
            window_start = current_time - (current_time % self.window_size)
            key = f"rate_limit:{project}:{window_start}"
            
            # 使用 Redis Pipeline 原子操作
            pipe = self.redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, self.window_size * 2)  # 设置过期时间
            results = await pipe.execute()
            
            current_count = results[0]
            remaining = max(0, self.rate_limit - current_count)
            
            if current_count > self.rate_limit:
                logger.warning(f"超出频率限制：{project}, count: {current_count}/{self.rate_limit}")
                return False, 0
            
            logger.debug(f"频率检查通过：{project}, count: {current_count}/{self.rate_limit}, remaining: {remaining}")
            return True, remaining
            
        except Exception as e:
            logger.error(f"频率检查异常：{e}")
            # 如果 Redis 不可用，允许请求通过（fail-open）
            return True, self.rate_limit
    
    async def close(self):
        """关闭 Redis 连接"""
        if self.redis:
            await self.redis.close()


# 简单的内存频率限制器（用于无 Redis 环境）
class SimpleRateLimiter:
    """简单频率限制器（内存版）"""
    
    def __init__(self):
        self.requests: Dict[str, list] = {}
        self.rate_limit = settings.RATE_LIMIT_PER_MINUTE
        self.window_size = 60
    
    def is_allowed(self, project: str) -> tuple:
        """检查是否允许请求"""
        current_time = time.time()
        window_start = current_time - self.window_size
        
        # 清理旧记录
        if project in self.requests:
            self.requests[project] = [t for t in self.requests[project] if t > window_start]
        else:
            self.requests[project] = []
        
        # 检查是否超出限制
        if len(self.requests[project]) >= self.rate_limit:
            return False, 0
        
        # 添加新记录
        self.requests[project].append(current_time)
        remaining = self.rate_limit - len(self.requests[project])
        
        return True, remaining


# 中间件类已经定义，不需要全局实例
# 在 main.py 中创建实例时传递 app 参数
