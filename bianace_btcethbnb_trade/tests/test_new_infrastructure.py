#!/usr/bin/env python3
"""
测试新配置系统和基础设施

验证统一配置管理器、异常类、错误处理器和日志系统是否正常工作。

版本: v1.0.0
创建时间: 2026-04-27
更新时间: 2026-04-27
修改说明: 使用 pytest 框架重写测试，符合单元测试规范
"""

import sys
import threading
from pathlib import Path
from decimal import Decimal
import pytest

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# ==================== 配置管理器测试 ====================

class TestConfigManager:
    """配置管理器测试类"""
    
    def test_get_config_manager_instance(self):
        """测试获取配置管理器实例"""
        from config.config_manager import get_config_manager
        
        manager = get_config_manager()
        
        # 验证实例创建成功
        assert manager is not None, "配置管理器实例创建失败"
        assert manager.get_config_file() is not None, "配置文件路径为空"
        assert manager.get_last_loaded() is not None, "最后加载时间为空"
    
    def test_get_basic_config_values(self):
        """测试获取基本配置值"""
        from config.config_manager import (
            get_config,
            get_config_int
        )
        
        # 测试获取配置值
        total_capital = get_config('account.total_capital')
        assert total_capital is not None, "总资金配置为空"
        assert total_capital > 0, "总资金必须大于0"
        
        single_margin = get_config('account.single_position_margin')
        assert single_margin is not None, "单仓保证金配置为空"
        assert single_margin > 0, "单仓保证金必须大于0"
        
        max_positions = get_config_int('account.max_positions')
        assert max_positions > 0, "最大持仓数必须大于0"
    
    def test_get_decimal_config(self):
        """测试获取 Decimal 类型配置"""
        from config.config_manager import get_config_decimal
        
        risk_amount = get_config_decimal('position_sizing.risk_amount')
        
        assert risk_amount is not None, "单笔风险金额配置为空"
        assert isinstance(risk_amount, Decimal), "返回类型不是 Decimal"
        assert risk_amount > 0, "单笔风险金额必须大于0"
    
    def test_get_bool_config(self):
        """测试获取布尔类型配置"""
        from config.config_manager import get_config_bool
        
        enable_auto_trade = get_config_bool('trading.enable_auto_trade')
        
        assert isinstance(enable_auto_trade, bool), "返回类型不是 bool"
    
    def test_get_list_config(self):
        """测试获取列表类型配置"""
        from config.config_manager import get_config
        
        allowed_grades = get_config('allowed_signal_grades')
        
        assert allowed_grades is not None, "允许的信号等级配置为空"
        assert isinstance(allowed_grades, list), "返回类型不是 list"
        assert len(allowed_grades) > 0, "允许的信号等级列表为空"
    
    def test_get_nested_config(self):
        """测试获取嵌套配置"""
        from config.config_manager import (
            get_config_int,
            get_config_decimal
        )
        
        s_grade_leverage = get_config_int('signal_grades.S.max_leverage')
        assert s_grade_leverage > 0, "S 级杠杆倍数必须大于0"
        
        tp1_multiplier = get_config_decimal('risk_management.take_profit_levels.tp1_multiplier')
        assert tp1_multiplier is not None, "TP1 ATR 倍数配置为空"
        assert tp1_multiplier > 0, "TP1 ATR 倍数必须大于0"
    
    def test_backward_compatibility_interface(self):
        """测试向后兼容接口"""
        from config.config_manager import get_params
        
        params = get_params()
        
        # 验证参数字典结构
        assert 'account' in params, "参数字典缺少 account 键"
        assert 'position_sizing' in params, "参数字典缺少 position_sizing 键"
        assert 'signal_grades' in params, "参数字典缺少 signal_grades 键"
        
        # 验证具体值
        assert params['account']['total_capital'] > 0, "总资金必须大于0"
        assert 'S' in params['position_sizing']['position_coefficient'], "缺少 S 级仓位系数"
    
    def test_config_validation(self):
        """测试配置验证"""
        from config.config_manager import validate_config
        
        errors = validate_config()
        
        # 配置验证应该返回列表
        assert isinstance(errors, list), "验证结果不是列表"
        
        # 注意：某些配置可能未设置（如 API Key），所以不强制要求 errors 为空
        # 但至少应该能正常执行验证逻辑
    
    def test_singleton_thread_safety(self):
        """测试单例模式的线程安全性"""
        from config.config_manager import ConfigManager
        
        # 重置单例实例以进行测试
        ConfigManager._instance = None
        
        instances = []
        
        def create_instance():
            """在多线程环境中创建实例"""
            instance = ConfigManager()
            instances.append(id(instance))
        
        # 创建多个线程同时获取实例
        threads = []
        for _ in range(10):
            thread = threading.Thread(target=create_instance)
            threads.append(thread)
            thread.start()
        
        # 等待所有线程完成
        for thread in threads:
            thread.join()
        
        # 验证所有实例的 id 都相同（单例模式）
        assert len(set(instances)) == 1, "多线程环境下创建了多个实例，单例模式线程安全性失败"


# ==================== 自定义异常类测试 ====================

class TestExceptions:
    """自定义异常类测试"""
    
    def test_base_exception(self):
        """测试基础异常类"""
        from utils.exceptions import TradingSystemError
        
        # 测试创建异常
        error = TradingSystemError(
            message="测试错误",
            error_code="TEST_ERROR",
            details={'key': 'value'}
        )
        
        # 验证异常属性
        assert error.message == "测试错误", "异常消息不正确"
        assert error.error_code == "TEST_ERROR", "错误代码不正确"
        assert error.details == {'key': 'value'}, "错误详情不正确"
        
        # 验证 __str__ 方法返回格式
        error_str = str(error)
        assert "TEST_ERROR" in error_str, "错误代码未包含在字符串表示中"
        assert "测试错误" in error_str, "错误消息未包含在字符串表示中"
        
        # 测试捕获异常
        with pytest.raises(TradingSystemError) as exc_info:
            raise error
        
        assert exc_info.value.error_code == "TEST_ERROR"
    
    def test_configuration_error(self):
        """测试配置错误异常"""
        from utils.exceptions import ConfigurationError
        
        with pytest.raises(ConfigurationError) as exc_info:
            raise ConfigurationError(
                message="配置项缺失",
                config_key="api.binance.api_key"
            )
        
        assert "配置项缺失" in str(exc_info.value)
    
    def test_insufficient_balance_error(self):
        """测试余额不足异常"""
        from utils.exceptions import InsufficientBalanceError
        
        with pytest.raises(InsufficientBalanceError) as exc_info:
            raise InsufficientBalanceError(
                required=100.0,
                available=50.0,
                currency="USDT"
            )
        
        # 验证异常消息包含关键信息
        assert "余额不足" in str(exc_info.value)
        
        # 验证 details 中包含 required 和 available
        assert exc_info.value.details['required'] == 100.0
        assert exc_info.value.details['available'] == 50.0
    
    def test_order_execution_error(self):
        """测试订单执行错误异常"""
        from utils.exceptions import OrderExecutionError
        
        error = OrderExecutionError(
            symbol="BTCUSDT",
            order_type="MARKET",
            reason="余额不足"
        )
        
        # 验证异常消息包含关键信息
        assert "BTCUSDT" in str(error)
        assert error.symbol == "BTCUSDT"
        
        # 验证 details 中包含 order_type
        assert error.details['order_type'] == "MARKET"
        assert error.details['reason'] == "余额不足"
        
        # 测试 to_dict 方法
        error_dict = error.to_dict()
        assert error_dict['error_type'] == 'OrderExecutionError'
        assert error_dict['message'] == error.message
        assert error_dict['details']['order_type'] == "MARKET"


# ==================== 错误处理器测试 ====================

class TestErrorHandler:
    """错误处理器测试"""
    
    def test_handle_exception(self):
        """测试处理异常"""
        from utils.error_handler import handle_error
        from utils.exceptions import TradingError
        
        try:
            raise TradingError(
                message="测试交易错误",
                symbol="BTCUSDT"
            )
        except Exception as e:
            error_info = handle_error(e, context={'test': 'context'})
            
            assert error_info is not None, "错误处理返回 None"
            assert 'error_type' in error_info, "错误信息缺少 error_type"
            assert 'error_code' in error_info, "错误信息缺少 error_code"
            assert error_info['error_type'] == 'TradingError'
    
    def test_error_statistics(self):
        """测试错误统计"""
        from utils.error_handler import get_error_handler
        from utils.exceptions import TradingError
        
        # 触发一些错误
        try:
            raise TradingError(message="统计测试错误1")
        except Exception as e:
            get_error_handler().handle_error(e)
        
        try:
            raise TradingError(message="统计测试错误2")
        except Exception as e:
            get_error_handler().handle_error(e)
        
        # 获取统计信息
        stats = get_error_handler().get_error_stats()
        
        assert 'total_errors' in stats, "统计信息缺少 total_errors"
        assert 'error_counts' in stats, "统计信息缺少 error_counts"
        assert stats['total_errors'] >= 2, "错误统计数量不正确"
    
    def test_error_handler_decorator(self):
        """测试错误处理装饰器"""
        from utils.error_handler import error_handler
        
        @error_handler(reraise=False, default_return=None)
        def test_function():
            raise ValueError("测试装饰器错误处理")
        
        # 装饰器应该捕获异常并返回默认值
        result = test_function()
        assert result is None, "装饰器未正确处理异常"


# ==================== 日志系统测试 ====================

class TestLogger:
    """日志系统测试"""
    
    def test_init_logging(self):
        """测试初始化日志系统"""
        from utils.logger import init_logging
        
        # 初始化应该成功，不抛出异常
        init_logging()
        
        # 再次初始化也应该成功（幂等性）
        init_logging()
    
    def test_get_logger(self):
        """测试获取日志记录器"""
        from utils.logger import get_logger
        
        logger = get_logger('test_module')
        
        assert logger is not None, "日志记录器获取失败"
        assert logger.name == 'test_module', "日志记录器名称不正确"
    
    def test_log_different_levels(self):
        """测试记录不同级别的日志"""
        from utils.logger import get_logger
        
        logger = get_logger('test_log_levels')
        
        # 这些日志调用不应该抛出异常
        logger.debug("这是一条 DEBUG 日志")
        logger.info("这是一条 INFO 日志")
        logger.warning("这是一条 WARNING 日志")
        logger.error("这是一条 ERROR 日志")
        
        # 如果执行到这里，说明日志记录成功
        assert True


# ==================== 集成测试 ====================

class TestIntegration:
    """集成测试"""
    
    def test_integration_scenario(self):
        """测试集成场景：检查余额并下单"""
        from config.config_manager import get_config_decimal
        from utils.exceptions import InsufficientBalanceError, OrderExecutionError
        from utils.error_handler import handle_error
        from utils.logger import get_logger
        
        logger = get_logger('integration_test')
        
        # 模拟场景：检查余额并下单
        try:
            # 1. 获取配置
            single_margin = get_config_decimal('account.single_position_margin')
            assert single_margin is not None, "单仓保证金配置为空"
            logger.info(f"单仓保证金配置: {single_margin} U")
            
            # 2. 模拟检查余额
            available_balance = Decimal('30')  # 模拟可用余额
            
            if available_balance < single_margin:
                raise InsufficientBalanceError(
                    required=float(single_margin),
                    available=float(available_balance)
                )
            
            # 如果余额充足，模拟下单失败
            raise OrderExecutionError(
                symbol="BTCUSDT",
                order_type="LIMIT",
                reason="测试订单执行失败"
            )
            
        except Exception as e:
            error_info = handle_error(e, context={'test_scenario': 'integration'})
            logger.error(f"集成测试捕获异常: {error_info['message']}")
            
            # 验证错误处理成功
            assert error_info is not None, "错误处理失败"


# ==================== pytest 配置 ====================

if __name__ == "__main__":
    # 运行 pytest
    pytest.main([__file__, "-v", "--tb=short"])
