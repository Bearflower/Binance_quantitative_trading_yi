#!/usr/bin/env python3
"""
API 限流模块
实现请求速率限制，避免触发币安 API 频率限制
"""

import time
import logging
from functools import wraps
from threading import Lock
from collections import defaultdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class RateLimiter:
    """请求速率限制器"""
    
    def __init__(self, max_requests: int = 1200, time_window: int = 60):
        """
        初始化速率限制器
        
        Args:
            max_requests: 时间窗口内最大请求数，默认 1200 次/分钟（币安限制）
            time_window: 时间窗口（秒），默认 60 秒
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests: Dict[str, List[float]] = defaultdict(list)
        self.lock = Lock()
        
        logger.info(f"API 限流器初始化完成：{max_requests}次/{time_window}秒")
    
    def _clean_old_requests(self, key: str) -> None:
        """
        清理旧的请求记录
        
        Args:
            key: 请求标识（如 API 端点）
        """
        current_time = time.time()
        cutoff_time = current_time - self.time_window
        
        # 保留时间窗口内的请求
        self.requests[key] = [
            req_time for req_time in self.requests[key]
            if req_time > cutoff_time
        ]
    
    def _get_request_count(self, key: str) -> int:
        """
        获取当前时间窗口内的请求数
        
        Args:
            key: 请求标识
        
        Returns:
            请求数量
        """
        self._clean_old_requests(key)
        return len(self.requests[key])
    
    def _wait_if_needed(self, key: str) -> None:
        """
        如果需要，等待直到可以发送请求
        
        Args:
            key: 请求标识
        """
        while True:
            self._clean_old_requests(key)
            current_count = self._get_request_count(key)
            
            if current_count < self.max_requests:
                break
            
            # 计算需要等待的时间
            oldest_request = min(self.requests[key])
            wait_time = oldest_request + self.time_window - time.time() + 0.1
            
            if wait_time > 0:
                logger.debug(f"API 限流：等待 {wait_time:.2f}秒")
                time.sleep(wait_time)
    
    def acquire(self, key: str = 'default') -> bool:
        """
        获取请求许可
        
        Args:
            key: 请求标识
        
        Returns:
            True 表示获取成功
        """
        with self.lock:
            self._wait_if_needed(key)
            self.requests[key].append(time.time())
            return True
    
    def get_remaining_requests(self, key: str = 'default') -> int:
        """
        获取剩余可用请求数
        
        Args:
            key: 请求标识
        
        Returns:
            剩余请求数
        """
        with self.lock:
            current_count = self._get_request_count(key)
            return max(0, self.max_requests - current_count)
    
    def get_wait_time(self, key: str = 'default') -> float:
        """
        获取需要等待的时间（如果已超限）
        
        Args:
            key: 请求标识
        
        Returns:
            等待时间（秒），0 表示无需等待
        """
        with self.lock:
            self._clean_old_requests(key)
            
            if len(self.requests[key]) < self.max_requests:
                return 0.0
            
            oldest_request = min(self.requests[key])
            wait_time = oldest_request + self.time_window - time.time()
            
            return max(0.0, wait_time)


class BinanceRateLimiter:
    """币安 API 专用限流器"""
    
    def __init__(self):
        """初始化币安限流器"""
        # 币安不同端点的限流
        self.limiters: Dict[str, RateLimiter] = {
            # 普通接口：1200 次/分钟
            'general': RateLimiter(max_requests=1200, time_window=60),
            
            # 下单接口：更严格的限制
            'order': RateLimiter(max_requests=300, time_window=60),
            
            # 账户数据接口：60 次/10 秒
            'account': RateLimiter(max_requests=60, time_window=10),
            
            # K 线数据接口：60 次/分钟
            'klines': RateLimiter(max_requests=60, time_window=60),
        }
        
        # 端点到限流器的映射
        self.endpoint_map = {
            '/papi/v1/um/order': 'order',
            '/papi/v1/um/account': 'account',
            '/papi/v1/um/balance': 'account',
            '/papi/v1/um/klines': 'klines',
            '/papi/v1/um/positionRisk': 'account',
        }
        
        logger.info("币安 API 限流器初始化完成")
    
    def _get_limiter_key(self, endpoint: str) -> str:
        """
        根据端点获取限流器标识
        
        Args:
            endpoint: API 端点
        
        Returns:
            限流器标识
        """
        # 检查是否有关联的限流器
        for ep, key in self.endpoint_map.items():
            if endpoint.startswith(ep):
                return key
        
        # 默认使用普通限流器
        return 'general'
    
    def acquire(self, endpoint: str) -> bool:
        """
        获取 API 请求许可
        
        Args:
            endpoint: API 端点
        
        Returns:
            True 表示获取成功
        """
        limiter_key = self._get_limiter_key(endpoint)
        limiter = self.limiters.get(limiter_key, self.limiters['general'])
        
        logger.debug(f"API 请求：{endpoint} -> 限流器：{limiter_key}")
        
        return limiter.acquire(endpoint)
    
    def get_status(self) -> Dict[str, Dict]:
        """
        获取所有限流器状态
        
        Returns:
            状态字典
        """
        status = {}
        
        for key, limiter in self.limiters.items():
            remaining = limiter.get_remaining_requests(key)
            wait_time = limiter.get_wait_time(key)
            
            status[key] = {
                'remaining_requests': remaining,
                'wait_time': wait_time,
                'max_requests': limiter.max_requests,
                'time_window': limiter.time_window,
            }
        
        return status


# 全局限流器实例
_global_limiter: Optional[BinanceRateLimiter] = None


def get_rate_limiter() -> BinanceRateLimiter:
    """获取全局币安限流器实例"""
    global _global_limiter
    if _global_limiter is None:
        _global_limiter = BinanceRateLimiter()
    return _global_limiter


def rate_limit(endpoint: str = None):
    """
    API 限流装饰器
    
    Usage:
        @rate_limit('/papi/v1/um/order')
        def place_order(...):
            pass
    
    Args:
        endpoint: API 端点，如果为 None 则使用函数名
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            limiter = get_rate_limiter()
            ep = endpoint or func.__name__
            
            # 获取许可
            limiter.acquire(ep)
            
            # 记录日志
            logger.debug(f"API 限流检查：{ep}")
            
            # 调用原函数
            return func(*args, **kwargs)
        
        return wrapper
    return decorator


if __name__ == '__main__':
    # 测试代码
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 60)
    print("API 限流器测试")
    print("=" * 60)
    
    limiter = get_rate_limiter()
    
    # 测试普通请求
    print("\n测试普通请求（1200 次/分钟）:")
    for i in range(5):
        limiter.acquire('/papi/v1/um/ticker/price')
        remaining = limiter.get_remaining_requests('/papi/v1/um/ticker/price')
        print(f"  请求 {i+1}: 剩余 {remaining} 次")
    
    # 测试下单请求
    print("\n测试下单请求（300 次/分钟）:")
    for i in range(5):
        limiter.acquire('/papi/v1/um/order')
        remaining = limiter.get_remaining_requests('/papi/v1/um/order')
        print(f"  请求 {i+1}: 剩余 {remaining} 次")
    
    # 获取状态
    print("\n限流器状态:")
    status = limiter.get_status()
    import json
    print(json.dumps(status, indent=2))
    
    print("\nAPI 限流器测试完成")
