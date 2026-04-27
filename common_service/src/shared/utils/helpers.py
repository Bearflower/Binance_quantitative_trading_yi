"""
工具函数库

提供常用的工具函数和辅助方法
"""

import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode
import re


def generate_timestamp() -> int:
    """生成当前时间戳（毫秒）"""
    return int(time.time() * 1000)


def generate_signature(
    query_string: str,
    secret_key: str
) -> str:
    """
    生成 HMAC SHA256 签名
    
    Args:
        query_string: 查询字符串
        secret_key: 密钥
    
    Returns:
        签名结果（十六进制字符串）
    """
    signature = hmac.new(
        secret_key.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    
    return signature


def format_symbol(symbol: str) -> str:
    """
    格式化交易对符号
    
    Args:
        symbol: 交易对符号
    
    Returns:
        大写的标准格式
    """
    return symbol.upper().strip()


def parse_interval(interval: str) -> str:
    """
    解析时间周期
    
    Args:
        interval: 周期字符串（如 15m, 1h, 4h, 1d）
    
    Returns:
        标准化的周期格式
    """
    interval = interval.lower().strip()
    
    # 映射关系
    interval_map = {
        "15m": "15m",
        "30m": "30m",
        "1h": "1h",
        "2h": "2h",
        "4h": "4h",
        "6h": "6h",
        "12h": "12h",
        "1d": "1d",
        "3d": "3d",
        "1w": "1w",
        "1M": "1M",
    }
    
    return interval_map.get(interval, interval)


def timestamp_to_datetime(timestamp: int) -> datetime:
    """
    时间戳转 datetime
    
    Args:
        timestamp: 毫秒时间戳
    
    Returns:
        datetime 对象（UTC）
    """
    return datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)


def datetime_to_timestamp(dt: datetime) -> int:
    """
    datetime 转时间戳
    
    Args:
        dt: datetime 对象
    
    Returns:
        毫秒时间戳
    """
    return int(dt.timestamp() * 1000)


def format_price(price: float, precision: int = 2) -> str:
    """
    格式化价格
    
    Args:
        price: 价格
        precision: 小数位数
    
    Returns:
        格式化后的价格字符串
    """
    return f"{price:.{precision}f}"


def format_volume(volume: float, precision: int = 8) -> str:
    """
    格式化成交量
    
    Args:
        volume: 成交量
        precision: 小数位数
    
    Returns:
        格式化后的成交量字符串
    """
    return f"{volume:.{precision}f}"


def calculate_percentage_change(old_value: float, new_value: float) -> float:
    """
    计算百分比变化
    
    Args:
        old_value: 旧值
        new_value: 新值
    
    Returns:
        百分比变化（-100 到 +100）
    """
    if old_value == 0:
        return 0.0
    
    return ((new_value - old_value) / old_value) * 100


def round_to_step(value: float, step_size: float) -> float:
    """
    按步长舍入
    
    Args:
        value: 值
        step_size: 步长
    
    Returns:
        舍入后的值
    """
    if step_size <= 0:
        return value
    
    return round(value / step_size) * step_size


def validate_symbol(symbol: str) -> bool:
    """
    验证交易对符号
    
    Args:
        symbol: 交易对符号
    
    Returns:
        是否有效
    """
    pattern = r"^[A-Z]+/[A-Z]+$"
    return bool(re.match(pattern, symbol.upper()))


def validate_interval(interval: str) -> bool:
    """
    验证时间周期
    
    Args:
        interval: 周期字符串
    
    Returns:
        是否有效
    """
    valid_intervals = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"}
    return interval.lower() in valid_intervals


def generate_request_id() -> str:
    """
    生成请求 ID
    
    Returns:
        唯一的请求 ID
    """
    import uuid
    return str(uuid.uuid4())


def safe_get(data: Dict, *keys, default=None) -> Any:
    """
    安全获取嵌套字典的值
    
    Args:
        data: 字典
        *keys: 键路径
        default: 默认值
    
    Returns:
        获取的值或默认值
    """
    current = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def merge_dicts(dict1: Dict, dict2: Dict) -> Dict:
    """
    深度合并两个字典
    
    Args:
        dict1: 字典 1
        dict2: 字典 2
    
    Returns:
        合并后的字典
    """
    result = dict1.copy()
    
    for key, value in dict2.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = value
    
    return result


def chunk_list(lst: List, chunk_size: int) -> List[List]:
    """
    将列表分块
    
    Args:
        lst: 列表
        chunk_size: 每块大小
    
    Returns:
        分块后的列表
    """
    return [lst[i:i + chunk_size] for i in range(0, len(lst), chunk_size)]


def retry_async(max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """
    异步重试装饰器
    
    Args:
        max_retries: 最大重试次数
        delay: 初始延迟（秒）
        backoff: 延迟倍数
    
    Returns:
        装饰器函数
    
    Usage:
        @retry_async(max_retries=3, delay=1.0)
        async def my_function():
            ...
    """
    import asyncio
    from functools import wraps
    
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            current_delay = delay
            last_exception = None
            
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff
            
            raise last_exception
        
        return wrapper
    
    return decorator
