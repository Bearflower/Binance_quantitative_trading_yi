#!/usr/bin/env python3
"""
服务模块导出

提供统一的服务层接口，包括：
- 服务基类
- 频率控制器
- 规则执行器
- 交易执行器
- 其他服务模块

版本: v2.0.0 (重构版 - 使用服务基类)
更新时间: 2026-04-27
"""

# 服务基类
from .base import BaseService, ServiceState, service_method

# 频率控制器
from .frequency_controller import FrequencyController, get_frequency_controller

# 规则执行器
from .rule_executor import RuleTradeExecutor, get_rule_executor, execute_signals

# 交易执行器
from .trade_executor import TradeExecutor, TradeSignal, get_trade_executor

# 其他服务模块
from .performance_reporter import PerformanceReporter, get_performance_reporter
from .emergency_handler import EmergencyHandler, get_emergency_handler, can_trade, get_emergency_status

__all__ = [
    # 服务基类
    'BaseService',
    'ServiceState',
    'service_method',

    # 频率控制器
    'FrequencyController',
    'get_frequency_controller',

    # 规则执行器
    'RuleTradeExecutor',
    'get_rule_executor',
    'execute_signals',

    # 交易执行器
    'TradeExecutor',
    'TradeSignal',
    'get_trade_executor',

    # 其他服务模块
    'PerformanceReporter',
    'get_performance_reporter',
    'EmergencyHandler',
    'get_emergency_handler',
    'can_trade',
    'get_emergency_status',
]
