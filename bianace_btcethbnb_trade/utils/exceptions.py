#!/usr/bin/env python3
"""
自定义异常类

定义系统中使用的所有自定义异常类，提供清晰的错误分类和处理机制。

版本: v1.0.0
创建时间: 2026-04-27
"""

from typing import Optional, Dict, Any


class TradingSystemError(Exception):
    """
    交易系统基础异常类
    
    所有自定义异常的基类，提供统一的错误信息格式和处理接口
    """
    
    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        """
        初始化异常
        
        Args:
            message: 错误消息
            error_code: 错误代码（可选）
            details: 错误详情（可选）
        """
        self.message = message
        self.error_code = error_code or "UNKNOWN_ERROR"
        self.details = details or {}
        super().__init__(self.message)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            'error_type': self.__class__.__name__,
            'error_code': self.error_code,
            'message': self.message,
            'details': self.details
        }
    
    def __str__(self) -> str:
        """字符串表示"""
        if self.details:
            return f"[{self.error_code}] {self.message} - 详情: {self.details}"
        return f"[{self.error_code}] {self.message}"


# ==================== 配置相关异常 ====================

class ConfigurationError(TradingSystemError):
    """
    配置错误异常
    
    用于配置加载、验证、访问等过程中的错误
    """
    
    def __init__(
        self,
        message: str,
        config_key: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        """
        初始化配置错误
        
        Args:
            message: 错误消息
            config_key: 配置键（可选）
            details: 错误详情（可选）
        """
        self.config_key = config_key
        error_code = "CONFIG_ERROR"
        if config_key:
            error_code = f"CONFIG_ERROR_{config_key.upper().replace('.', '_')}"
        
        super().__init__(message, error_code, details)


class ConfigFileNotFoundError(ConfigurationError):
    """配置文件不存在异常"""
    
    def __init__(self, file_path: str):
        super().__init__(
            message=f"配置文件不存在：{file_path}",
            config_key="file_path",
            details={'file_path': file_path}
        )


class ConfigValidationError(ConfigurationError):
    """配置验证失败异常"""
    
    def __init__(self, errors: list):
        super().__init__(
            message=f"配置验证失败，发现 {len(errors)} 个错误",
            config_key="validation",
            details={'errors': errors}
        )


# ==================== 数据相关异常 ====================

class DataError(TradingSystemError):
    """
    数据错误异常
    
    用于数据获取、处理、验证等过程中的错误
    """
    
    def __init__(
        self,
        message: str,
        data_source: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        """
        初始化数据错误
        
        Args:
            message: 错误消息
            data_source: 数据源（可选）
            details: 错误详情（可选）
        """
        self.data_source = data_source
        error_code = "DATA_ERROR"
        if data_source:
            error_code = f"DATA_ERROR_{data_source.upper()}"
        
        super().__init__(message, error_code, details)


class DataFetchError(DataError):
    """数据获取失败异常"""
    
    def __init__(
        self,
        data_source: str,
        reason: str,
        details: Optional[Dict[str, Any]] = None
    ):
        super().__init__(
            message=f"数据获取失败：{data_source} - {reason}",
            data_source=data_source,
            details=details
        )


class DataValidationError(DataError):
    """数据验证失败异常"""
    
    def __init__(
        self,
        data_type: str,
        reason: str,
        details: Optional[Dict[str, Any]] = None
    ):
        super().__init__(
            message=f"数据验证失败：{data_type} - {reason}",
            data_source=data_type,
            details=details
        )


class InsufficientDataError(DataError):
    """数据不足异常"""
    
    def __init__(
        self,
        required: int,
        actual: int,
        data_type: str = "K线数据"
    ):
        super().__init__(
            message=f"{data_type}不足：需要 {required} 条，实际 {actual} 条",
            data_source=data_type,
            details={'required': required, 'actual': actual}
        )


# ==================== 交易相关异常 ====================

class TradingError(TradingSystemError):
    """
    交易错误异常
    
    用于交易执行、订单管理等过程中的错误
    """
    
    def __init__(
        self,
        message: str,
        symbol: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        """
        初始化交易错误
        
        Args:
            message: 错误消息
            symbol: 交易对（可选）
            details: 错误详情（可选）
        """
        self.symbol = symbol
        error_code = "TRADING_ERROR"
        if symbol:
            error_code = f"TRADING_ERROR_{symbol}"
        
        super().__init__(message, error_code, details)


class OrderExecutionError(TradingError):
    """订单执行失败异常"""
    
    def __init__(
        self,
        symbol: str,
        order_type: str,
        reason: str,
        details: Optional[Dict[str, Any]] = None
    ):
        super().__init__(
            message=f"订单执行失败：{symbol} {order_type} - {reason}",
            symbol=symbol,
            details={'order_type': order_type, 'reason': reason, **(details or {})}
        )


class InsufficientBalanceError(TradingError):
    """余额不足异常"""
    
    def __init__(
        self,
        required: float,
        available: float,
        currency: str = "USDT"
    ):
        super().__init__(
            message=f"余额不足：需要 {required} {currency}，可用 {available} {currency}",
            symbol=currency,
            details={'required': required, 'available': available}
        )


class RiskLimitExceededError(TradingError):
    """风险限制超出异常"""
    
    def __init__(
        self,
        risk_type: str,
        limit: float,
        actual: float
    ):
        super().__init__(
            message=f"风险限制超出：{risk_type} 限制 {limit}，实际 {actual}",
            symbol=risk_type,
            details={'limit': limit, 'actual': actual}
        )


class SignalNotFoundError(TradingError):
    """信号未找到异常"""
    
    def __init__(self, symbol: str):
        super().__init__(
            message=f"未找到有效信号：{symbol}",
            symbol=symbol
        )


# ==================== API 相关异常 ====================

class APIError(TradingSystemError):
    """
    API 错误异常
    
    用于 API 调用、网络请求等过程中的错误
    """
    
    def __init__(
        self,
        message: str,
        api_name: Optional[str] = None,
        status_code: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        """
        初始化 API 错误
        
        Args:
            message: 错误消息
            api_name: API 名称（可选）
            status_code: HTTP 状态码（可选）
            details: 错误详情（可选）
        """
        self.api_name = api_name
        self.status_code = status_code
        error_code = "API_ERROR"
        if api_name:
            error_code = f"API_ERROR_{api_name.upper()}"
        
        super().__init__(message, error_code, details)


class APIConnectionError(APIError):
    """API 连接失败异常"""
    
    def __init__(
        self,
        api_name: str,
        reason: str,
        details: Optional[Dict[str, Any]] = None
    ):
        super().__init__(
            message=f"API 连接失败：{api_name} - {reason}",
            api_name=api_name,
            details=details
        )


class APIResponseError(APIError):
    """API 响应错误异常"""
    
    def __init__(
        self,
        api_name: str,
        status_code: int,
        reason: str,
        details: Optional[Dict[str, Any]] = None
    ):
        super().__init__(
            message=f"API 响应错误：{api_name} 返回 {status_code} - {reason}",
            api_name=api_name,
            status_code=status_code,
            details=details
        )


class APIRateLimitError(APIError):
    """API 限流异常"""
    
    def __init__(
        self,
        api_name: str,
        retry_after: Optional[int] = None
    ):
        details = {}
        if retry_after:
            details['retry_after'] = retry_after
        
        super().__init__(
            message=f"API 限流：{api_name}，请稍后重试",
            api_name=api_name,
            details=details
        )


class APIAuthenticationError(APIError):
    """API 认证失败异常"""
    
    def __init__(self, api_name: str, reason: str = "认证失败"):
        super().__init__(
            message=f"API 认证失败：{api_name} - {reason}",
            api_name=api_name
        )


# ==================== 通知相关异常 ====================

class NotificationError(TradingSystemError):
    """
    通知错误异常
    
    用于飞书通知、邮件通知等过程中的错误
    """
    
    def __init__(
        self,
        message: str,
        notification_type: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        """
        初始化通知错误
        
        Args:
            message: 错误消息
            notification_type: 通知类型（可选）
            details: 错误详情（可选）
        """
        self.notification_type = notification_type
        error_code = "NOTIFICATION_ERROR"
        if notification_type:
            error_code = f"NOTIFICATION_ERROR_{notification_type.upper()}"
        
        super().__init__(message, error_code, details)


class LarkNotificationError(NotificationError):
    """飞书通知失败异常"""
    
    def __init__(self, reason: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            message=f"飞书通知失败：{reason}",
            notification_type="lark",
            details=details
        )


# ==================== 数据库相关异常 ====================

class DatabaseError(TradingSystemError):
    """
    数据库错误异常
    
    用于数据库操作过程中的错误
    """
    
    def __init__(
        self,
        message: str,
        operation: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        """
        初始化数据库错误
        
        Args:
            message: 错误消息
            operation: 操作类型（可选）
            details: 错误详情（可选）
        """
        self.operation = operation
        error_code = "DATABASE_ERROR"
        if operation:
            error_code = f"DATABASE_ERROR_{operation.upper()}"
        
        super().__init__(message, error_code, details)


class DatabaseConnectionError(DatabaseError):
    """数据库连接失败异常"""
    
    def __init__(self, reason: str):
        super().__init__(
            message=f"数据库连接失败：{reason}",
            operation="connection"
        )


class DatabaseQueryError(DatabaseError):
    """数据库查询失败异常"""
    
    def __init__(
        self,
        query: str,
        reason: str
    ):
        super().__init__(
            message=f"数据库查询失败：{reason}",
            operation="query",
            details={'query': query, 'reason': reason}
        )
