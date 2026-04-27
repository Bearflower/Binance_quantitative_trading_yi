#!/usr/bin/env python3
"""
服务基类

提供统一的服务层接口，规范服务初始化、错误处理、日志记录等通用功能。

特性：
1. 统一的初始化方法（配置、日志、错误处理）
2. 统一的错误处理机制
3. 统一的日志记录
4. 统一的配置访问
5. 上下文管理器支持
6. 状态管理

版本: v1.0.0
创建时间: 2026-04-27
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, TypeVar, Generic
from contextlib import contextmanager
from datetime import datetime

from config.config_manager import ConfigManager, get_config_manager
from utils.error_handler import ErrorHandler, get_error_handler, error_handler
from utils.exceptions import TradingSystemError


# 泛型类型变量
T = TypeVar('T')


class ServiceState:
    """服务状态枚举"""
    UNINITIALIZED = "uninitialized"  # 未初始化
    INITIALIZING = "initializing"    # 初始化中
    READY = "ready"                  # 就绪
    RUNNING = "running"              # 运行中
    STOPPED = "stopped"              # 已停止
    ERROR = "error"                  # 错误状态


class BaseService(ABC, Generic[T]):
    """
    服务基类

    所有服务类的基类，提供统一的服务接口和通用功能。

    功能：
    1. 统一的初始化流程
    2. 统一的错误处理
    3. 统一的日志记录
    4. 统一的配置访问
    5. 上下文管理器支持
    6. 状态管理

    使用示例：
        class MyService(BaseService):
            def _initialize(self):
                # 初始化逻辑
                pass

            def do_something(self):
                with self.error_context("执行操作"):
                    # 业务逻辑
                    pass

        # 使用服务
        with MyService() as service:
            service.do_something()
    """

    def __init__(
        self,
        service_name: Optional[str] = None,
        config_manager: Optional[ConfigManager] = None,
        error_handler: Optional[ErrorHandler] = None,
        auto_initialize: bool = True
    ):
        """
        初始化服务基类

        Args:
            service_name: 服务名称（默认使用类名）
            config_manager: 配置管理器实例（可选）
            error_handler: 错误处理器实例（可选）
            auto_initialize: 是否自动初始化（默认True）
        """
        # 服务名称
        self.service_name = service_name or self.__class__.__name__

        # 状态管理
        self._state = ServiceState.UNINITIALIZED
        self._state_changed_at = datetime.now()

        # 核心组件
        self._config = config_manager
        self._error_handler = error_handler
        self._logger = None

        # 服务元数据
        self._metadata: Dict[str, Any] = {}
        self._start_time: Optional[datetime] = None

        # 自动初始化
        if auto_initialize:
            self.initialize()

    @property
    def config(self) -> ConfigManager:
        """获取配置管理器（懒加载）"""
        if self._config is None:
            self._config = get_config_manager()
        return self._config

    @property
    def error_handler(self) -> ErrorHandler:
        """获取错误处理器（懒加载）"""
        if self._error_handler is None:
            self._error_handler = get_error_handler()
        return self._error_handler

    @property
    def logger(self) -> logging.Logger:
        """获取日志记录器（懒加载）"""
        if self._logger is None:
            self._logger = logging.getLogger(self.service_name)
        return self._logger

    @property
    def state(self) -> str:
        """获取当前状态"""
        return self._state

    @property
    def is_ready(self) -> bool:
        """检查服务是否就绪"""
        return self._state in [ServiceState.READY, ServiceState.RUNNING]

    @property
    def is_running(self) -> bool:
        """检查服务是否运行中"""
        return self._state == ServiceState.RUNNING

    def initialize(self):
        """
        初始化服务

        执行完整的初始化流程：
        1. 设置状态为初始化中
        2. 调用子类初始化方法
        3. 设置状态为就绪
        """
        if self._state != ServiceState.UNINITIALIZED:
            self.logger.warning(f"服务 {self.service_name} 已经初始化过")
            return

        try:
            # 设置状态为初始化中
            self._set_state(ServiceState.INITIALIZING)

            # 调用子类初始化方法
            self._initialize()

            # 设置状态为就绪
            self._set_state(ServiceState.READY)

            self.logger.info(f"服务 {self.service_name} 初始化完成")

        except Exception as e:
            # 设置状态为错误
            self._set_state(ServiceState.ERROR)
            self.handle_error(e, context={"phase": "initialization"})
            raise

    @abstractmethod
    def _initialize(self):
        """
        子类初始化方法（抽象方法）

        子类必须实现此方法，完成具体的初始化逻辑。
        """
        pass

    def start(self):
        """
        启动服务

        将服务状态设置为运行中
        """
        if not self.is_ready:
            raise RuntimeError(f"服务 {self.service_name} 未就绪，无法启动")

        self._set_state(ServiceState.RUNNING)
        self._start_time = datetime.now()
        self.logger.info(f"服务 {self.service_name} 已启动")

    def stop(self):
        """
        停止服务

        执行清理逻辑并将状态设置为已停止
        """
        if self._state == ServiceState.STOPPED:
            return

        try:
            # 执行清理
            self._cleanup()

            # 设置状态为已停止
            self._set_state(ServiceState.STOPPED)

            self.logger.info(f"服务 {self.service_name} 已停止")

        except Exception as e:
            self.handle_error(e, context={"phase": "cleanup"})
            self._set_state(ServiceState.ERROR)
            raise

    def _cleanup(self):
        """
        清理资源（子类可重写）

        子类可以重写此方法，完成资源清理逻辑。
        """
        pass

    def _set_state(self, new_state: str):
        """
        设置服务状态

        Args:
            new_state: 新状态
        """
        old_state = self._state
        self._state = new_state
        self._state_changed_at = datetime.now()

        self.logger.debug(
            f"服务 {self.service_name} 状态变更：{old_state} -> {new_state}"
        )

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
        # 添加服务信息到上下文
        full_context = {
            'service': self.service_name,
            'state': self.state,
            **(context or {})
        }

        # 调用错误处理器
        return self.error_handler.handle_error(error, full_context, notify)

    @contextmanager
    def error_context(
        self,
        operation: str,
        notify: bool = False,
        reraise: bool = True
    ):
        """
        错误处理上下文管理器

        Args:
            operation: 操作名称
            notify: 是否发送通知
            reraise: 是否重新抛出异常

        Example:
            with self.error_context("执行交易"):
                # 业务逻辑
                pass
        """
        try:
            yield
        except Exception as e:
            # 处理错误
            self.handle_error(
                e,
                context={'operation': operation},
                notify=notify
            )

            # 重新抛出异常
            if reraise:
                raise

    def get_config_value(
        self,
        key_path: str,
        default: Any = None,
        required: bool = False
    ) -> Any:
        """
        获取配置值

        Args:
            key_path: 配置路径
            default: 默认值
            required: 是否必需

        Returns:
            配置值

        Raises:
            ConfigurationError: 如果required=True且配置不存在
        """
        value = self.config.get(key_path, default)

        if required and value is None:
            from utils.exceptions import ConfigurationError
            raise ConfigurationError(
                f"缺少必需的配置项：{key_path}",
                config_key=key_path
            )

        return value

    def set_metadata(self, key: str, value: Any):
        """
        设置服务元数据

        Args:
            key: 元数据键
            value: 元数据值
        """
        self._metadata[key] = value

    def get_metadata(self, key: str, default: Any = None) -> Any:
        """
        获取服务元数据

        Args:
            key: 元数据键
            default: 默认值

        Returns:
            元数据值
        """
        return self._metadata.get(key, default)

    def get_service_info(self) -> Dict[str, Any]:
        """
        获取服务信息

        Returns:
            服务信息字典
        """
        return {
            'service_name': self.service_name,
            'state': self.state,
            'state_changed_at': self._state_changed_at.isoformat(),
            'start_time': self._start_time.isoformat() if self._start_time else None,
            'is_ready': self.is_ready,
            'is_running': self.is_running,
            'metadata': self._metadata.copy()
        }

    # ==================== 上下文管理器支持 ====================

    def __enter__(self) -> 'BaseService':
        """进入上下文管理器"""
        if not self.is_ready:
            self.initialize()
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文管理器"""
        if exc_type is not None:
            # 处理异常
            self.handle_error(
                exc_val,
                context={'phase': 'context_manager_exit'}
            )

        # 停止服务
        self.stop()

        # 不抑制异常
        return False

    # ==================== 日志便捷方法 ====================

    def log_debug(self, message: str, **kwargs):
        """记录调试日志"""
        self.logger.debug(f"[{self.service_name}] {message}", **kwargs)

    def log_info(self, message: str, **kwargs):
        """记录信息日志"""
        self.logger.info(f"[{self.service_name}] {message}", **kwargs)

    def log_warning(self, message: str, **kwargs):
        """记录警告日志"""
        self.logger.warning(f"[{self.service_name}] {message}", **kwargs)

    def log_error(self, message: str, **kwargs):
        """记录错误日志"""
        self.logger.error(f"[{self.service_name}] {message}", **kwargs)

    def log_critical(self, message: str, **kwargs):
        """记录严重错误日志"""
        self.logger.critical(f"[{self.service_name}] {message}", **kwargs)


# ==================== 装饰器 ====================

def service_method(notify_on_error: bool = False):
    """
    服务方法装饰器

    为服务方法提供统一的错误处理和日志记录。

    Args:
        notify_on_error: 是否在错误时发送通知

    Example:
        class MyService(BaseService):
            @service_method(notify_on_error=True)
            def my_method(self):
                # 业务逻辑
                pass
    """
    def decorator(func):
        def wrapper(self, *args, **kwargs):
            # 检查服务状态
            if not self.is_ready:
                raise RuntimeError(f"服务 {self.service_name} 未就绪")

            # 执行方法
            with self.error_context(
                operation=func.__name__,
                notify=notify_on_error
            ):
                return func(self, *args, **kwargs)

        return wrapper
    return decorator
