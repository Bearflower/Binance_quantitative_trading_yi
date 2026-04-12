"""
参数加载与验证模块
负责从配置文件加载参数并提供默认值
"""

import yaml
import os
from typing import Dict, Any, Optional
from pathlib import Path

from utils.logger import get_logger

logger = get_logger()


class ConfigLoader:
    """配置文件加载器"""

    DEFAULT_CONFIG = {
        'stock_pool': {
            'exclude_st': True,
            'exclude_beijing': True,
            'exclude_delisting': True,
            'exclude_star_market': True,      # 剔除科创板（688 开头）
            'exclude_chi_next': True,          # 剔除创业板（300/301 开头）
            'min_list_days': 60,
            'min_market_cap': 0,
            'max_market_cap': 0,
            'include_sectors': [],
            'exclude_sectors': [],
            'price_range': [0, 100]
        },
        'pattern': {
            'drop_period': 25,
            'drop_threshold': 0.20,
            'support_lookback': 5,
            'support_method': 'both',
            'consolidation_days': 5,
            'consolidation_range': 0.03,
            'volume_shrink_ratio': 0.6,
            'volume_shrink_period': 10,
            'shrink_before_surge_days': 10,
            'surge_volume_ratio': 1.5,
            'surge_price_ratio': 0.05,
            'surge_lookback': 15,
            'surge_condition': 'either',
            'exclude_long_upper_shadow': False,
            'retrace_ratio': 0.5,
            'retrace_volume_ratio': 0.6,
            'retrace_max_days': 10,
            'support_level_combine': 'lowest_or_ma',
            'support_ma_period': 20
        },
        'scoring': {
            'weights': {
                'drop_depth': 0.25,
                'shrink_degree': 0.20,
                'surge_strength': 0.20,
                'retrace_depth': 0.20,
                'retrace_shrink': 0.15
            }
        },
        'trading': {
            'max_positions': 5,
            'entry_timing': 'next_open',
            'stop_loss_ratio': 0.03,
            'trailing_stop': 0.05,
            'min_hold_days': 1,
            'max_hold_days': 60
        },
        'risk': {
            'index_filter': True,
            'index_code': '000300.SH',
            'index_ma_period': 20
        },
        'backtest': {
            'start_date': '2018-01-01',
            'end_date': '2024-12-31',
            'initial_cash': 1000000,
            'commission_rate': 0.00025,
            'stamp_tax': 0.001,
            'slippage': 0.001
        },
        'notification': {
            'enabled': True,
            'send_new_signals': True,
            'send_position_updates': False,
            'position_update_time': '17:00'
        },
        'logging': {
            'level': 'INFO',
            'file': 'logs/stock_scanner.log',
            'max_bytes': 10485760,
            'backup_count': 5
        },
        'concurrency': {
            'max_workers': 5,
            'request_delay': 0.5,
            'max_retries': 3
        },
        'output': {
            'export_csv': True,
            'export_excel': False,
            'export_dir': 'output'
        }
    }

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件"""
        config = self.DEFAULT_CONFIG.copy()

        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    file_config = yaml.safe_load(f)
                
                if file_config:
                    config = self._merge_config(config, file_config)
                logger.info(f"成功加载配置文件：{self.config_path}")
            except Exception as e:
                logger.warning(f"加载配置文件失败：{e}，使用默认配置")
        else:
            logger.warning(f"配置文件不存在：{self.config_path}，使用默认配置")

        return config

    def _merge_config(self, default: Dict, custom: Dict) -> Dict:
        """合并配置（递归合并嵌套字典）"""
        result = default.copy()
        for key, value in custom.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value
        return result

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        keys = key.split('.')
        value = self.config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def get_pattern_params(self) -> Dict[str, Any]:
        """获取形态检测参数"""
        return self.config.get('pattern', {})

    def get_scoring_weights(self) -> Dict[str, float]:
        """获取评分权重"""
        return self.config.get('scoring', {}).get('weights', {})

    def get_trading_params(self) -> Dict[str, Any]:
        """获取交易参数"""
        return self.config.get('trading', {})

    def get_stock_pool_config(self) -> Dict[str, Any]:
        """获取股票池配置"""
        return self.config.get('stock_pool', {})

    def get_backtest_params(self) -> Dict[str, Any]:
        """获取回测参数"""
        return self.config.get('backtest', {})

    def validate(self) -> bool:
        """验证配置有效性"""
        pattern = self.get_pattern_params()
        
        if not 0 < pattern.get('drop_threshold', 0) < 1:
            logger.error("drop_threshold 必须在 0-1 之间")
            return False
        
        if not 0 < pattern.get('volume_shrink_ratio', 0) < 1:
            logger.error("volume_shrink_ratio 必须在 0-1 之间")
            return False
        
        if not pattern.get('surge_volume_ratio', 0) > 1:
            logger.error("surge_volume_ratio 必须大于 1")
            return False
        
        weights = self.get_scoring_weights()
        total_weight = sum(weights.values())
        if abs(total_weight - 1.0) > 0.01:
            logger.warning(f"评分权重总和为 {total_weight}，建议调整为 1.0")
        
        return True

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def __contains__(self, key: str) -> bool:
        keys = key.split('.')
        value = self.config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return False
        return True


def load_config(config_path: str = "config.yaml") -> ConfigLoader:
    """加载配置的便捷函数"""
    return ConfigLoader(config_path)
