#!/usr/bin/env python3
"""
配置管理模块
包含可调参数管理和热加载机制
"""

__version__ = '1.0.0'
__author__ = 'Bianace Trading System'

from .strategy_params import StrategyParams, get_params

__all__ = ['StrategyParams', 'get_params']
