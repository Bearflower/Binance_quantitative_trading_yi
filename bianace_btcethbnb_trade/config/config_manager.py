#!/usr/bin/env python3
"""
统一配置管理器

提供统一的配置管理接口，支持：
1. 从 YAML 文件加载配置
2. 环境变量覆盖
3. 配置验证
4. 配置缓存和热加载

版本: v1.0.0
创建时间: 2026-04-27
"""

import os
import yaml
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional, List
from decimal import Decimal
from datetime import datetime
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class ConfigManager:
    """
    统一配置管理器
    
    功能：
    1. 从 YAML 文件加载配置
    2. 支持环境变量覆盖（优先级最高）
    3. 支持配置验证
    4. 提供便捷的配置访问接口
    5. 支持配置缓存和热加载
    
    线程安全：
    - 使用双重检查锁定（Double-Checked Locking）确保单例模式在多线程环境下的安全性
    """
    
    _instance: Optional['ConfigManager'] = None
    _lock: threading.Lock = threading.Lock()  # 类级别的线程锁
    _config: Dict[str, Any] = {}
    _last_loaded: Optional[datetime] = None
    _config_file: str = "config/config.yaml"
    
    def __new__(cls, config_file: str = None):
        """
        单例模式（线程安全）
        
        使用双重检查锁定确保在多线程环境下只创建一个实例
        """
        if cls._instance is None:
            with cls._lock:
                # 双重检查：再次确认实例是否已被其他线程创建
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self, config_file: str = None):
        """
        初始化配置管理器
        
        Args:
            config_file: 配置文件路径（可选）
        """
        if config_file:
            self._config_file = config_file
        
        # 加载环境变量
        load_dotenv()
        
        # 加载配置
        self.reload()
    
    def reload(self):
        """重新加载配置"""
        try:
            # 1. 从 YAML 文件加载配置
            self._load_from_yaml()
            
            # 2. 应用环境变量覆盖
            self._apply_env_overrides()
            
            # 3. 验证配置
            errors = self.validate()
            if errors:
                logger.warning(f"配置验证发现 {len(errors)} 个问题：{errors}")
            
            self._last_loaded = datetime.now()
            logger.info(f"配置加载完成（文件：{self._config_file}）")
            
        except Exception as e:
            logger.error(f"配置加载失败：{str(e)}")
            raise
    
    def _load_from_yaml(self):
        """从 YAML 文件加载配置"""
        config_path = Path(self._config_file)
        
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在：{self._config_file}")
        
        with open(config_path, 'r', encoding='utf-8') as f:
            self._config = yaml.safe_load(f) or {}
        
        logger.debug(f"从 YAML 文件加载配置：{self._config_file}")
    
    def _apply_env_overrides(self):
        """应用环境变量覆盖"""
        # 敏感信息从环境变量读取（优先级最高）
        env_mappings = {
            # 币安 API
            'BINANCE_API_KEY': 'api.binance.api_key',
            'BINANCE_SECRET_KEY': 'api.binance.secret_key',
            'BINANCE_TESTNET': 'api.binance.testnet',
            
            # DeepSeek API
            'DEEPSEEK_API_KEY': 'api.deepseek.api_key',
            'DEEPSEEK_MODEL': 'api.deepseek.model',
            
            # 飞书通知
            'LARK_WEBHOOK_URL': 'api.lark_webhook_url',
            
            # 通用服务
            'NOTIFICATION_SERVICE_URL': 'api.services.notification_url',
            'KLINE_SERVICE_URL': 'api.services.kline_url',
            'NOTIFICATION_PROJECT': 'api.services.notification_project',
            
            # 数据库
            'DATABASE_URL': 'database.url',
            
            # 系统配置
            'ENVIRONMENT': 'system.environment',
            'TIMEZONE': 'system.timezone',
            
            # 交易配置
            'ENABLE_AUTO_TRADE': 'trading.enable_auto_trade',
        }
        
        for env_key, config_path in env_mappings.items():
            env_value = os.getenv(env_key)
            if env_value:
                self._set_nested_value(config_path, env_value)
                logger.debug(f"环境变量覆盖配置：{config_path}")
    
    def _set_nested_value(self, path: str, value: Any):
        """
        设置嵌套字典的值
        
        Args:
            path: 配置路径，如 'api.binance.api_key'
            value: 配置值
        """
        keys = path.split('.')
        current = self._config
        
        # 创建嵌套结构
        for key in keys[:-1]:
            if key not in current:
                current[key] = {}
            current = current[key]
        
        # 设置值
        # 处理布尔值
        if isinstance(value, str):
            if value.lower() in ('true', '1', 'yes'):
                value = True
            elif value.lower() in ('false', '0', 'no'):
                value = False
        
        current[keys[-1]] = value
    
    def get(self, key_path: str, default: Any = None) -> Any:
        """
        获取配置值（支持点分隔的路径）
        
        Args:
            key_path: 配置路径，如 'account.total_capital'
            default: 默认值
        
        Returns:
            配置值
        
        Example:
            >>> config.get('account.total_capital')
            500
            >>> config.get('signal_grades.S.max_leverage')
            5
        """
        keys = key_path.split('.')
        value = self._config
        
        try:
            for key in keys:
                value = value[key]
            return value
        except (KeyError, TypeError):
            if default is not None:
                return default
            logger.debug(f"配置项不存在：{key_path}")
            return None
    
    def get_decimal(self, key_path: str, default: Any = None) -> Optional[Decimal]:
        """
        获取配置值并转换为 Decimal 类型
        
        Args:
            key_path: 配置路径
            default: 默认值
        
        Returns:
            Decimal 类型的配置值
        """
        value = self.get(key_path, default)
        if value is None:
            return None
        
        try:
            return Decimal(str(value))
        except Exception as e:
            logger.warning(f"配置值转换 Decimal 失败：{key_path} = {value}, 错误：{str(e)}")
            return None
    
    def get_int(self, key_path: str, default: int = 0) -> int:
        """
        获取配置值并转换为 int 类型
        
        Args:
            key_path: 配置路径
            default: 默认值
        
        Returns:
            int 类型的配置值
        """
        value = self.get(key_path, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    
    def get_float(self, key_path: str, default: float = 0.0) -> float:
        """
        获取配置值并转换为 float 类型
        
        Args:
            key_path: 配置路径
            default: 默认值
        
        Returns:
            float 类型的配置值
        """
        value = self.get(key_path, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    
    def get_bool(self, key_path: str, default: bool = False) -> bool:
        """
        获取配置值并转换为 bool 类型
        
        Args:
            key_path: 配置路径
            default: 默认值
        
        Returns:
            bool 类型的配置值
        """
        value = self.get(key_path, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ('true', '1', 'yes')
        return bool(value)
    
    def get_list(self, key_path: str, default: List = None) -> List:
        """
        获取配置值并转换为 list 类型
        
        Args:
            key_path: 配置路径
            default: 默认值
        
        Returns:
            list 类型的配置值
        """
        value = self.get(key_path, default or [])
        if isinstance(value, list):
            return value
        return [value] if value else []
    
    def set(self, key_path: str, value: Any):
        """
        设置配置值（支持点分隔的路径）
        
        Args:
            key_path: 配置路径
            value: 配置值
        
        Example:
            >>> config.set('account.single_position_margin', 50)
        """
        self._set_nested_value(key_path, value)
        logger.debug(f"配置已更新：{key_path} = {value}")
    
    def validate(self) -> List[str]:
        """
        验证配置有效性
        
        Returns:
            错误消息列表（空列表表示验证通过）
        """
        errors = []
        
        # 验证账户配置
        if self.get('account.total_capital', 0) <= 0:
            errors.append("账户总资金必须 > 0")
        
        if self.get('account.max_positions', 0) <= 0:
            errors.append("最大持仓数必须 > 0")
        
        # 验证止损参数
        min_stop = self.get_decimal('position_sizing.min_stop_loss_pct')
        max_stop = self.get_decimal('position_sizing.max_stop_loss_pct')
        if min_stop and max_stop and min_stop >= max_stop:
            errors.append("最小止损幅度必须 < 最大止损幅度")
        
        # 验证信号等级配置
        for grade in ['S', 'A', 'B']:
            grade_config = self.get(f'signal_grades.{grade}')
            if grade_config:
                if grade_config.get('min_profit_loss_ratio', 0) <= 0:
                    errors.append(f"{grade}级信号的最小盈亏比必须 > 0")
        
        # 验证评分系统配置
        s_threshold = self.get('scoring.grade_thresholds.S', 0)
        a_threshold = self.get('scoring.grade_thresholds.A', 0)
        if s_threshold <= a_threshold:
            errors.append("S 级阈值必须 > A 级阈值")
        
        # 验证 API 配置
        if not self.get('api.binance.api_key'):
            errors.append("币安 API Key 未配置")
        
        if not self.get('api.binance.secret_key'):
            errors.append("币安 Secret Key 未配置")
        
        return errors
    
    def get_all(self) -> Dict[str, Any]:
        """获取所有配置"""
        return self._config.copy()
    
    def get_last_loaded(self) -> Optional[datetime]:
        """获取最后加载时间"""
        return self._last_loaded
    
    def get_config_file(self) -> str:
        """获取配置文件路径"""
        return self._config_file


# ==================== 便捷函数 ====================

def get_config_manager(config_file: str = None, force_reload: bool = False) -> ConfigManager:
    """
    获取配置管理器实例（单例模式）
    
    Args:
        config_file: 配置文件路径（可选）
        force_reload: 是否强制重新加载
    
    Returns:
        ConfigManager 实例
    """
    manager = ConfigManager(config_file)
    if force_reload:
        manager.reload()
    return manager


def get_config(key_path: str, default: Any = None) -> Any:
    """
    获取配置值的便捷函数
    
    Args:
        key_path: 配置路径
        default: 默认值
    
    Returns:
        配置值
    """
    return get_config_manager().get(key_path, default)


def get_config_decimal(key_path: str, default: Any = None) -> Optional[Decimal]:
    """获取 Decimal 类型的配置值"""
    return get_config_manager().get_decimal(key_path, default)


def get_config_int(key_path: str, default: int = 0) -> int:
    """获取 int 类型的配置值"""
    return get_config_manager().get_int(key_path, default)


def get_config_float(key_path: str, default: float = 0.0) -> float:
    """获取 float 类型的配置值"""
    return get_config_manager().get_float(key_path, default)


def get_config_bool(key_path: str, default: bool = False) -> bool:
    """获取 bool 类型的配置值"""
    return get_config_manager().get_bool(key_path, default)


def get_config_list(key_path: str, default: List = None) -> List:
    """获取 list 类型的配置值"""
    return get_config_manager().get_list(key_path, default)


def set_config(key_path: str, value: Any):
    """设置配置值的便捷函数"""
    get_config_manager().set(key_path, value)


def validate_config() -> List[str]:
    """验证配置有效性"""
    return get_config_manager().validate()


def reload_config():
    """重新加载配置"""
    get_config_manager().reload()


# ==================== 向后兼容接口 ====================
# 为了保持与旧代码的兼容性，提供以下接口

def get_params() -> Dict[str, Any]:
    """
    获取策略参数（向后兼容接口）
    
    返回格式与 config/strategy_params.py 保持一致
    """
    manager = get_config_manager()
    
    # 构建与旧接口兼容的参数字典
    params = {
        'account': {
            'total_capital': manager.get_decimal('account.total_capital'),
            'single_position_margin': manager.get_decimal('account.single_position_margin'),
            'max_positions': manager.get_int('account.max_positions'),
            'reserve_capital_ratio': manager.get_decimal('account.reserve_capital_ratio'),
            'max_total_margin_ratio': manager.get_decimal('account.max_total_margin_ratio'),
        },
        'prohibited_conditions': {
            'max_24h_price_change': manager.get_decimal('prohibited_conditions.max_24h_price_change'),
            'max_24h_price_drop': manager.get_decimal('prohibited_conditions.max_24h_price_drop'),
            'max_funding_rate': manager.get_decimal('prohibited_conditions.max_funding_rate'),
            'max_spread_ratio': manager.get_decimal('prohibited_conditions.max_spread_ratio'),
            'news_blackout_window': manager.get_int('prohibited_conditions.news_blackout_window'),
        },
        'signal_grades': {},
        'allowed_signal_grades': manager.get_list('allowed_signal_grades'),
        'trend_filter': {
            'ema21_period': manager.get_int('trend_filter.ema21_period'),
            'trend_slope_threshold': manager.get_decimal('trend_filter.trend_slope_threshold'),
            'support_resistance_tolerance': manager.get_decimal('trend_filter.support_resistance_tolerance'),
            'high_low_distance_threshold': manager.get_decimal('trend_filter.high_low_distance_threshold'),
        },
        'position_sizing': {
            'risk_amount': manager.get_decimal('position_sizing.risk_amount'),
            'min_stop_loss_pct': manager.get_decimal('position_sizing.min_stop_loss_pct'),
            'max_stop_loss_pct': manager.get_decimal('position_sizing.max_stop_loss_pct'),
            'max_position_notional': manager.get_decimal('position_sizing.max_position_notional'),
            'max_total_notional': manager.get_decimal('position_sizing.max_total_notional'),
            'position_coefficient': {
                'S': manager.get_decimal('position_sizing.position_coefficient.S'),
                'A': manager.get_decimal('position_sizing.position_coefficient.A'),
                'B': manager.get_decimal('position_sizing.position_coefficient.B'),
            },
        },
        'risk_management': {
            'stop_loss_multiplier': manager.get_decimal('risk_management.stop_loss_multiplier'),
            'take_profit_levels': {
                'tp1_ratio': manager.get_decimal('risk_management.take_profit_levels.tp1_ratio'),
                'tp1_multiplier': manager.get_decimal('risk_management.take_profit_levels.tp1_multiplier'),
                'tp2_ratio': manager.get_decimal('risk_management.take_profit_levels.tp2_ratio'),
                'tp2_multiplier': manager.get_decimal('risk_management.take_profit_levels.tp2_multiplier'),
                'tp3_ratio': manager.get_decimal('risk_management.take_profit_levels.tp3_ratio'),
            },
            'margin_ratio_warning': manager.get_decimal('risk_management.margin_ratio_warning'),
            'margin_ratio_emergency': manager.get_decimal('risk_management.margin_ratio_emergency'),
            'max_margin_usage': manager.get_decimal('risk_management.max_margin_usage'),
            'max_float_loss': manager.get_decimal('risk_management.max_float_loss'),
        },
        'trailing_stop': {
            'enable_after_tp1': manager.get_bool('trailing_stop.enable_after_tp1'),
            'enable_after_tp2': manager.get_bool('trailing_stop.enable_after_tp2'),
            'use_sar_or_ema': manager.get('trailing_stop.use_sar_or_ema'),
        },
        'emergency_handling': {
            'extreme_price_drop': manager.get_decimal('emergency_handling.extreme_price_drop'),
            'emergency_close_ratio': manager.get_decimal('emergency_handling.emergency_close_ratio'),
            'emergency_stop_loss': manager.get_decimal('emergency_handling.emergency_stop_loss'),
            'consecutive_losses_limit': manager.get_int('emergency_handling.consecutive_losses_limit'),
            'consecutive_losses_pause_days': manager.get_int('emergency_handling.consecutive_losses_pause_days'),
            'weekly_loss_limit': manager.get_decimal('emergency_handling.weekly_loss_limit'),
            'weekly_loss_pause_days': manager.get_int('emergency_handling.weekly_loss_pause_days'),
        },
        'strategy_optimization': {
            'min_trades_for_optimization': manager.get_int('strategy_optimization.min_trades_for_optimization'),
            'optimization_sample_size': manager.get_int('strategy_optimization.optimization_sample_size'),
            'adjustable_params': manager.get_list('strategy_optimization.adjustable_params'),
            'forbidden_adjustments': manager.get_list('strategy_optimization.forbidden_adjustments'),
        },
        'performance_metrics': {
            'min_win_rate': manager.get_decimal('performance_metrics.min_win_rate'),
            'good_win_rate': manager.get_decimal('performance_metrics.good_win_rate'),
            'min_profit_loss_ratio': manager.get_decimal('performance_metrics.min_profit_loss_ratio'),
            'good_profit_loss_ratio': manager.get_decimal('performance_metrics.good_profit_loss_ratio'),
            'min_leverage_efficiency': manager.get_decimal('performance_metrics.min_leverage_efficiency'),
            'max_bankruptcy_rate': manager.get_decimal('performance_metrics.max_bankruptcy_rate'),
            'max_drawdown': manager.get_decimal('performance_metrics.max_drawdown'),
        },
    }
    
    # 构建信号等级配置
    for grade in ['S', 'A', 'B']:
        grade_config = manager.get(f'signal_grades.{grade}', {})
        params['signal_grades'][grade] = {
            'min_profit_loss_ratio': Decimal(str(grade_config.get('min_profit_loss_ratio', 0))),
            'max_leverage': grade_config.get('max_leverage', 0),
            'min_recommendation_score': grade_config.get('min_recommendation_score', 0),
            'require_multi_timeframe': grade_config.get('require_multi_timeframe', False),
        }
    
    return params


def get_param(key_path: str, default: Any = None) -> Any:
    """获取参数值的便捷函数（向后兼容接口）"""
    return get_config(key_path, default)


def set_param(key_path: str, value: Any):
    """设置参数值的便捷函数（向后兼容接口）"""
    set_config(key_path, value)


def validate_params() -> List[str]:
    """验证参数有效性（向后兼容接口）"""
    return validate_config()


def reload_params():
    """重新加载参数（向后兼容接口）"""
    reload_config()
