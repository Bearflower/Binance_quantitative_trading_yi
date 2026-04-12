"""
自定义异常类
"""


class BaseError(Exception):
    """基础异常类"""
    def __init__(self, message: str, code: int = None):
        self.message = message
        self.code = code
        super().__init__(self.message)


class ExchangeError(BaseError):
    """交易所相关异常"""
    pass


class APIError(ExchangeError):
    """API 调用错误"""
    pass


class NetworkError(ExchangeError):
    """网络连接错误"""
    pass


class RateLimitError(ExchangeError):
    """频率限制错误"""
    pass


class StrategyError(BaseError):
    """策略相关异常"""
    pass


class ParameterError(StrategyError):
    """参数错误"""
    pass


class StateError(StrategyError):
    """状态错误"""
    pass


class ExecutionError(BaseError):
    """执行相关异常"""
    pass


class OrderError(ExecutionError):
    """订单错误"""
    pass


class GridError(ExecutionError):
    """网格操作错误"""
    pass


class MonitoringError(BaseError):
    """监控相关异常"""
    pass


class AlertError(MonitoringError):
    """报警发送错误"""
    pass
