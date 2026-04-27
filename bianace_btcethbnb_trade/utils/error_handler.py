#!/usr/bin/env python3
"""
统一错误处理器

提供统一的错误处理机制，包括错误日志记录、错误通知等。

版本: v1.0.0
创建时间: 2026-04-27
"""

import logging
import traceback
from typing import Optional, Dict, Any, Callable
from functools import wraps
from datetime import datetime

from utils.exceptions import TradingSystemError

logger = logging.getLogger(__name__)


class ErrorHandler:
    """
    统一错误处理器
    
    功能：
    1. 统一的错误日志格式
    2. 错误分类和级别
    3. 错误通知机制
    4. 错误统计和分析
    """
    
    def __init__(self):
        """初始化错误处理器"""
        self.error_counts: Dict[str, int] = {}
        self.error_history: list = []
        self.max_history_size = 1000
    
    def handle_error(
        self,
        error: Exception,
        context: Optional[Dict[str, Any]] = None,
        notify: bool = False
    ) -> Dict[str, Any]:
        """
        处理错误
        
        Args:
            error: 异常对象
            context: 错误上下文信息
            notify: 是否发送通知
        
        Returns:
            错误信息字典
        """
        # 构建错误信息
        error_info = self._build_error_info(error, context)
        
        # 记录日志
        self._log_error(error_info)
        
        # 统计错误
        self._count_error(error_info['error_type'])
        
        # 记录历史
        self._record_history(error_info)
        
        # 发送通知（可选）
        if notify:
            self._notify_error(error_info)
        
        return error_info
    
    def _build_error_info(
        self,
        error: Exception,
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        构建错误信息字典
        
        Args:
            error: 异常对象
            context: 错误上下文
        
        Returns:
            错误信息字典
        """
        # 如果是自定义异常
        if isinstance(error, TradingSystemError):
            error_info = error.to_dict()
        else:
            # 普通异常
            error_info = {
                'error_type': error.__class__.__name__,
                'error_code': 'SYSTEM_ERROR',
                'message': str(error),
                'details': {}
            }
        
        # 添加上下文信息
        if context:
            error_info['context'] = context
        
        # 添加时间戳
        error_info['timestamp'] = datetime.now().isoformat()
        
        # 添加堆栈信息
        error_info['traceback'] = traceback.format_exc()
        
        return error_info
    
    def _log_error(self, error_info: Dict[str, Any]):
        """
        记录错误日志
        
        Args:
            error_info: 错误信息字典
        """
        error_type = error_info.get('error_type', 'UnknownError')
        error_code = error_info.get('error_code', 'UNKNOWN')
        message = error_info.get('message', '未知错误')
        details = error_info.get('details', {})
        context = error_info.get('context', {})
        
        # 根据错误类型选择日志级别
        if 'CRITICAL' in error_code or 'FATAL' in error_code:
            log_func = logger.critical
        elif 'ERROR' in error_code:
            log_func = logger.error
        elif 'WARNING' in error_code or 'WARN' in error_code:
            log_func = logger.warning
        else:
            log_func = logger.error
        
        # 构建日志消息
        log_message = f"[{error_type}] [{error_code}] {message}"
        if details:
            log_message += f" | 详情: {details}"
        if context:
            log_message += f" | 上下文: {context}"
        
        log_func(log_message)
    
    def _count_error(self, error_type: str):
        """
        统计错误次数
        
        Args:
            error_type: 错误类型
        """
        if error_type not in self.error_counts:
            self.error_counts[error_type] = 0
        self.error_counts[error_type] += 1
    
    def _record_history(self, error_info: Dict[str, Any]):
        """
        记录错误历史
        
        Args:
            error_info: 错误信息字典
        """
        self.error_history.append(error_info)
        
        # 限制历史记录大小
        if len(self.error_history) > self.max_history_size:
            self.error_history = self.error_history[-self.max_history_size:]
    
    def _notify_error(self, error_info: Dict[str, Any]):
        """
        发送错误通知（集成飞书通知）
        
        Args:
            error_info: 错误信息字典
        """
        try:
            # 延迟导入避免循环依赖
            from utils.lark_notifier import send_error_notification
            
            send_error_notification(
                error_type=error_info.get('error_type', 'UnknownError'),
                error_code=error_info.get('error_code', 'UNKNOWN'),
                message=error_info.get('message', '未知错误'),
                details=error_info.get('details', {})
            )
        except Exception as e:
            logger.warning(f"发送错误通知失败：{str(e)}")
    
    def get_error_stats(self) -> Dict[str, Any]:
        """
        获取错误统计信息
        
        Returns:
            错误统计字典
        """
        return {
            'total_errors': sum(self.error_counts.values()),
            'error_counts': self.error_counts.copy(),
            'recent_errors': self.error_history[-10:]  # 最近 10 条错误
        }
    
    def clear_stats(self):
        """清空错误统计"""
        self.error_counts.clear()
        self.error_history.clear()


# 全局错误处理器实例
_error_handler: Optional[ErrorHandler] = None


def get_error_handler() -> ErrorHandler:
    """获取全局错误处理器实例"""
    global _error_handler
    if _error_handler is None:
        _error_handler = ErrorHandler()
    return _error_handler


def handle_error(
    error: Exception,
    context: Optional[Dict[str, Any]] = None,
    notify: bool = False
) -> Dict[str, Any]:
    """
    处理错误的便捷函数
    
    Args:
        error: 异常对象
        context: 错误上下文信息
        notify: 是否发送通知
    
    Returns:
        错误信息字典
    """
    return get_error_handler().handle_error(error, context, notify)


def error_handler(
    notify_on_error: bool = False,
    reraise: bool = False,
    default_return: Any = None
):
    """
    错误处理装饰器
    
    Args:
        notify_on_error: 是否在错误时发送通知
        reraise: 是否重新抛出异常
        default_return: 发生错误时的默认返回值
    
    Example:
        @error_handler(notify_on_error=True)
        def some_function():
            # 可能抛出异常的代码
            pass
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                # 处理错误
                error_info = handle_error(
                    e,
                    context={
                        'function': func.__name__,
                        'args': str(args)[:200],  # 限制长度
                        'kwargs': str(kwargs)[:200]
                    },
                    notify=notify_on_error
                )
                
                # 是否重新抛出异常
                if reraise:
                    raise
                
                # 返回默认值
                return default_return
        
        return wrapper
    return decorator
