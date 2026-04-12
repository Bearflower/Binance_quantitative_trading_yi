#!/usr/bin/env python3
"""
核心模块导出
"""

from .data_fetcher import MarketDataFetcher, get_data_fetcher
from .signal_detector import SignalDetector, get_signal_detector
from .position_calculator import PositionCalculator, get_position_calculator, calculate_position
from .risk_manager import RiskManager, get_risk_manager, calculate_stop_loss, calculate_take_profit_levels, check_margin_ratio
from .order_generator import OrderGenerator, get_order_generator, generate_order_template, generate_all_orders
from .emergency_handler import EmergencyHandler, get_emergency_handler, check_extreme_market, check_daily_loss, check_consecutive_losses, is_trading_allowed

__all__ = [
    'MarketDataFetcher',
    'get_data_fetcher',
    'SignalDetector',
    'get_signal_detector',
    'PositionCalculator',
    'get_position_calculator',
    'calculate_position',
    'RiskManager',
    'get_risk_manager',
    'calculate_stop_loss',
    'calculate_take_profit_levels',
    'check_margin_ratio',
    'OrderGenerator',
    'get_order_generator',
    'generate_order_template',
    'generate_all_orders',
    'EmergencyHandler',
    'get_emergency_handler',
    'check_extreme_market',
    'check_daily_loss',
    'check_consecutive_losses',
    'is_trading_allowed',
]
