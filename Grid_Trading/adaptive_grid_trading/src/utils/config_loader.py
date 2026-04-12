"""
配置加载器
负责 YAML 配置文件解析和环境变量替换
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)


class ConfigLoader:
    """配置加载器"""
    
    def __init__(self, config_path: str = "config/config.yaml"):
        """
        初始化配置加载器
        
        Args:
            config_path: 配置文件路径
        """
        self.config_path = config_path
        self._config: Dict[str, Any] = {}
    
    def _load_env_file(self, env_file: str) -> Dict[str, str]:
        """
        加载.env 文件
        
        Args:
            env_file: .env 文件路径
            
        Returns:
            环境变量字典
        """
        env_vars = {}
        env_path = Path(env_file)
        
        if env_path.exists():
            with open(env_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        if '=' in line:
                            key, value = line.split('=', 1)
                            # 移除引号
                            value = value.strip().strip('"').strip("'")
                            env_vars[key.strip()] = value
        
        return env_vars
    
    def _replace_env_vars(self, value: Any, env_vars: Dict[str, str]) -> Any:
        """
        递归替换配置中的环境变量
        
        Args:
            value: 配置值
            env_vars: 环境变量字典
            
        Returns:
            替换后的值
        """
        if isinstance(value, str):
            # 匹配 ${VAR_NAME} 格式
            pattern = r'\$\{([^}]+)\}'
            
            def replace(match):
                var_name = match.group(1)
                return env_vars.get(var_name, match.group(0))
            
            return re.sub(pattern, replace, value)
        
        elif isinstance(value, dict):
            return {k: self._replace_env_vars(v, env_vars) for k, v in value.items()}
        
        elif isinstance(value, list):
            return [self._replace_env_vars(item, env_vars) for item in value]
        
        return value
    
    def load(self) -> Dict[str, Any]:
        """
        加载配置文件
        
        Returns:
            配置字典
        """
        config_file = Path(self.config_path)
        
        if not config_file.exists():
            logger.error(f"配置文件不存在：{self.config_path}")
            raise FileNotFoundError(f"配置文件不存在：{self.config_path}")
        
        # 加载.env 文件
        env_file = config_file.parent / '.env'
        env_vars = self._load_env_file(str(env_file))
        
        # 同时从系统环境变量加载
        env_vars.update(dict(os.environ))
        
        # 加载 YAML 配置
        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        # 替换环境变量
        config = self._replace_env_vars(config, env_vars)
        
        self._config = config
        
        logger.info(f"配置加载成功：{self.config_path}")
        logger.debug(f"配置内容：{config}")
        
        return config
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值
        
        Args:
            key: 配置键（支持点号分隔，如 'strategy.indicators.adx_period'）
            default: 默认值
            
        Returns:
            配置值
        """
        if not self._config:
            self.load()
        
        keys = key.split('.')
        value = self._config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value
    
    def get_exchange_config(self) -> Dict[str, Any]:
        """获取交易所配置"""
        return self.get('exchange', {})
    
    def get_strategy_config(self) -> Dict[str, Any]:
        """获取策略配置"""
        return self.get('strategy', {})
    
    def get_execution_config(self) -> Dict[str, Any]:
        """获取执行配置"""
        return self.get('execution', {})
    
    def get_monitoring_config(self) -> Dict[str, Any]:
        """获取监控配置"""
        return self.get('monitoring', {})
    
    def validate(self) -> bool:
        """
        验证配置完整性
        
        Returns:
            是否有效
        """
        required_keys = [
            'exchange.api_key',
            'exchange.api_secret',
            'exchange.symbol',
            'strategy.indicators.adx_period',
            'strategy.grid.base_grid_count',
            'strategy.risk.hard_stop_loss',
            'execution.inspection_interval'
        ]
        
        for key in required_keys:
            value = self.get(key)
            if value is None or value == '':
                logger.error(f"缺少必需的配置项：{key}")
                return False
        
        # 验证 API 密钥
        api_key = self.get('exchange.api_key')
        if api_key and ('your_api_key' in api_key.lower() or api_key == '${BINANCE_API_KEY}'):
            logger.error("请配置有效的币安 API 密钥")
            return False
        
        return True


def load_config(config_path: str = "config/config.yaml") -> ConfigLoader:
    """
    便捷函数：加载配置
    
    Args:
        config_path: 配置文件路径
        
    Returns:
        ConfigLoader 实例
    """
    loader = ConfigLoader(config_path)
    loader.load()
    return loader
