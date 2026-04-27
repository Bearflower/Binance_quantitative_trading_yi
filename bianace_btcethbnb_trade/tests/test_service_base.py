#!/usr/bin/env python3
"""
服务基类单元测试

测试服务基类的核心功能：
1. 初始化流程
2. 状态管理
3. 错误处理
4. 配置访问
5. 上下文管理器

版本: v1.0.0
创建时间: 2026-04-27
"""

import unittest
import logging
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime

# 配置日志
logging.basicConfig(level=logging.INFO)

from services.base import BaseService, ServiceState, service_method
from config.config_manager import ConfigManager


class TestService(BaseService):
    """测试服务类"""

    def __init__(self, **kwargs):
        """初始化测试服务"""
        super().__init__(service_name="TestService", **kwargs)

    def _initialize(self):
        """初始化测试服务"""
        self.log_info("测试服务初始化中...")

        # 加载配置
        self.test_value = self.get_config_value(
            'test.value',
            default='default_value'
        )

        self.log_info("测试服务初始化完成")

    def test_method(self):
        """测试方法（使用上下文管理器）"""
        with self.error_context("测试方法"):
            self.log_info("执行测试方法")
            return "success"

    @service_method()
    def test_decorated_method(self):
        """测试方法（使用装饰器）"""
        self.log_info("执行装饰器测试方法")
        return "decorated_success"

    def test_error_method(self):
        """测试错误处理的方法"""
        with self.error_context("测试错误方法", reraise=False):
            raise ValueError("测试错误")


class TestBaseService(unittest.TestCase):
    """服务基类测试"""

    def setUp(self):
        """测试前准备"""
        # 创建模拟配置管理器
        self.mock_config = Mock(spec=ConfigManager)
        self.mock_config.get.return_value = 'test_value'

    def test_service_initialization(self):
        """测试服务初始化"""
        service = TestService(config_manager=self.mock_config, auto_initialize=False)

        # 验证初始状态
        self.assertEqual(service.state, ServiceState.UNINITIALIZED)
        self.assertEqual(service.service_name, "TestService")

        # 手动初始化
        service.initialize()

        # 验证初始化后状态
        self.assertEqual(service.state, ServiceState.READY)
        self.assertTrue(service.is_ready)

    def test_service_state_management(self):
        """测试服务状态管理"""
        service = TestService(config_manager=self.mock_config, auto_initialize=True)

        # 验证状态转换
        self.assertEqual(service.state, ServiceState.READY)

        # 启动服务
        service.start()
        self.assertEqual(service.state, ServiceState.RUNNING)
        self.assertTrue(service.is_running)

        # 停止服务
        service.stop()
        self.assertEqual(service.state, ServiceState.STOPPED)
        self.assertFalse(service.is_running)

    def test_service_context_manager(self):
        """测试服务上下文管理器"""
        with TestService(config_manager=self.mock_config) as service:
            # 验证服务已启动
            self.assertEqual(service.state, ServiceState.RUNNING)
            self.assertTrue(service.is_running)

            # 执行测试方法
            result = service.test_method()
            self.assertEqual(result, "success")

        # 验证服务已停止
        self.assertEqual(service.state, ServiceState.STOPPED)

    def test_error_handling(self):
        """测试错误处理"""
        service = TestService(config_manager=self.mock_config, auto_initialize=True)

        # 执行会抛出错误的方法（不重新抛出）
        service.test_error_method()

        # 验证服务状态正常（未进入错误状态）
        self.assertEqual(service.state, ServiceState.READY)

    def test_config_access(self):
        """测试配置访问"""
        service = TestService(config_manager=self.mock_config, auto_initialize=True)

        # 验证配置访问
        value = service.get_config_value('test.value', default='default')
        self.assertEqual(value, 'test_value')

        # 验证配置管理器被调用
        self.mock_config.get.assert_called()

    def test_service_metadata(self):
        """测试服务元数据"""
        service = TestService(config_manager=self.mock_config, auto_initialize=True)

        # 设置元数据
        service.set_metadata('key1', 'value1')
        service.set_metadata('key2', {'nested': 'data'})

        # 获取元数据
        self.assertEqual(service.get_metadata('key1'), 'value1')
        self.assertEqual(service.get_metadata('key2'), {'nested': 'data'})
        self.assertEqual(service.get_metadata('nonexistent', 'default'), 'default')

        # 获取服务信息
        info = service.get_service_info()
        self.assertEqual(info['service_name'], 'TestService')
        self.assertEqual(info['state'], ServiceState.READY)
        self.assertIn('key1', info['metadata'])

    def test_service_method_decorator(self):
        """测试服务方法装饰器"""
        service = TestService(config_manager=self.mock_config, auto_initialize=True)

        # 测试正常方法
        result = service.test_decorated_method()
        self.assertEqual(result, "decorated_success")

        # 测试未就绪状态
        service._set_state(ServiceState.UNINITIALIZED)
        with self.assertRaises(RuntimeError):
            service.test_decorated_method()


class TestServiceMethodDecorator(unittest.TestCase):
    """服务方法装饰器测试"""

    def test_service_method_success(self):
        """测试服务方法成功执行"""
        mock_config = Mock(spec=ConfigManager)

        class TestServiceImpl(BaseService):
            def _initialize(self):
                pass

            @service_method()
            def test_method(self):
                return "success"

        service = TestServiceImpl(config_manager=mock_config, auto_initialize=True)
        result = service.test_method()
        self.assertEqual(result, "success")

    def test_service_method_not_ready(self):
        """测试服务未就绪时调用方法"""
        mock_config = Mock(spec=ConfigManager)

        class TestServiceImpl(BaseService):
            def _initialize(self):
                pass

            @service_method()
            def test_method(self):
                return "success"

        service = TestServiceImpl(config_manager=mock_config, auto_initialize=False)

        with self.assertRaises(RuntimeError) as context:
            service.test_method()

        self.assertIn("未就绪", str(context.exception))


class TestServiceStateTransitions(unittest.TestCase):
    """服务状态转换测试"""

    def test_state_transition_sequence(self):
        """测试状态转换序列"""
        mock_config = Mock(spec=ConfigManager)

        service = TestService(config_manager=mock_config, auto_initialize=False)

        # 初始状态
        self.assertEqual(service.state, ServiceState.UNINITIALIZED)

        # 初始化
        service.initialize()
        self.assertEqual(service.state, ServiceState.READY)

        # 启动
        service.start()
        self.assertEqual(service.state, ServiceState.RUNNING)

        # 停止
        service.stop()
        self.assertEqual(service.state, ServiceState.STOPPED)

    def test_double_initialization(self):
        """测试重复初始化"""
        mock_config = Mock(spec=ConfigManager)

        service = TestService(config_manager=mock_config, auto_initialize=True)

        # 第一次初始化
        self.assertEqual(service.state, ServiceState.READY)

        # 第二次初始化（应该被忽略）
        service.initialize()
        self.assertEqual(service.state, ServiceState.READY)

    def test_start_without_initialize(self):
        """测试未初始化就启动"""
        mock_config = Mock(spec=ConfigManager)

        service = TestService(config_manager=mock_config, auto_initialize=False)

        # 尝试启动未初始化的服务
        with self.assertRaises(RuntimeError):
            service.start()


if __name__ == '__main__':
    # 运行测试
    unittest.main(verbosity=2)
