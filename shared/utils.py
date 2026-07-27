"""
工具函数
提供重试、日志等通用功能
"""
import asyncio
import functools
from typing import Callable, Any
import structlog


logger = structlog.get_logger()


def retry_on_failure(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,),
    non_retryable_codes: set = None,
):
    """
    重试装饰器
    
    Args:
        max_retries: 最大重试次数
        delay: 初始延迟（秒）
        backoff: 退避系数
        exceptions: 要捕获的异常类型
        non_retryable_codes: 不重试的BinanceAPIError错误码集合（如保证金不足、订单不存在等）
    
    Raises:
        ValueError: 参数验证失败
    """
    # 参数验证
    if not isinstance(max_retries, int):
        raise ValueError(f"最大重试次数必须是整数，实际为 {type(max_retries).__name__}")
    
    if max_retries < 0:
        raise ValueError(f"最大重试次数不能为负数: {max_retries}")
    
    if not isinstance(delay, (int, float)):
        raise ValueError(f"延迟时间必须是数字，实际为 {type(delay).__name__}")
    
    if delay < 0:
        raise ValueError(f"延迟时间不能为负数: {delay}")
    
    if not isinstance(backoff, (int, float)):
        raise ValueError(f"退避系数必须是数字，实际为 {type(backoff).__name__}")
    
    if backoff < 1:
        raise ValueError(f"退避系数必须大于等于1: {backoff}")
    
    if not isinstance(exceptions, tuple):
        raise ValueError(f"异常类型必须是元组，实际为 {type(exceptions).__name__}")
    
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            current_delay = delay
            
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    # 不可重试的错误码，立即抛出不重试
                    if non_retryable_codes and hasattr(e, 'code') and getattr(e, 'code') in non_retryable_codes:
                        code = getattr(e, 'code')
                        # -9999 是废弃API端点（已知预期行为），-2011 是订单已成交/已取消（正常竞态）
                        # 两者均为已知预期行为，降级为 debug 避免监控噪音
                        log_func = logger.debug if code in (-9999, -2011) else logger.warning
                        log_func(
                            "遇到不可重试的错误，立即抛出",
                            function=func.__name__,
                            error_code=code,
                            error=str(e)
                        )
                        raise
                    
                    if attempt == max_retries:
                        logger.error(
                            "重试次数已达上限",
                            function=func.__name__,
                            attempts=attempt,
                            error=str(e)
                        )
                        raise
                    
                    logger.warning(
                        "操作失败，准备重试",
                        function=func.__name__,
                        attempt=attempt + 1,
                        max_retries=max_retries,
                        delay=current_delay,
                        error=str(e)
                    )
                    
                    await asyncio.sleep(current_delay)
                    current_delay *= backoff
        
        return wrapper
    
    return decorator


def setup_logging(level: str = "INFO", format: str = "json"):
    """
    配置日志
    
    Args:
        level: 日志级别
        format: 日志格式 (json, text)
    
    Raises:
        ValueError: 参数验证失败
    """
    # 参数验证
    if not isinstance(level, str):
        raise ValueError(f"日志级别必须是字符串，实际为 {type(level).__name__}")
    
    valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    level_upper = level.upper()
    if level_upper not in valid_levels:
        raise ValueError(f"无效的日志级别: {level}, 有效级别: {', '.join(valid_levels)}")
    
    if not isinstance(format, str):
        raise ValueError(f"日志格式必须是字符串，实际为 {type(format).__name__}")
    
    valid_formats = ["json", "text"]
    if format not in valid_formats:
        raise ValueError(f"无效的日志格式: {format}, 有效格式: {', '.join(valid_formats)}")
    
    import logging
    import sys
    
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer() if format == "json" else structlog.dev.ConsoleRenderer()
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level_upper)
    )
