"""
配置管理模块
加载和管理配置文件
"""

import os
import logging
from typing import Any, Dict
from pathlib import Path

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


class ConfigManager:
    """配置管理器"""
    
    def __init__(self, config_path: str = None):
        """
        初始化配置管理器
        
        Args:
            config_path: 配置文件路径
        """
        # 加载环境变量
        load_dotenv()
        
        # 加载配置文件
        if config_path is None:
            config_path = os.getenv(
                "CONFIG_PATH",
                str(Path(__file__).parent.parent.parent / "config" / "config.yaml")
            )
        
        self.config_path = config_path
        self.config = self._load_config()
        
        logger.info(f"配置管理器初始化完成：{config_path}")
    
    def _load_config(self) -> Dict[str, Any]:
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            # 替换环境变量
            config = self._replace_env_vars(config)
            
            logger.info("配置文件加载成功")
            return config
            
        except Exception as e:
            logger.error(f"加载配置文件失败：{e}")
            return {}
    
    def _replace_env_vars(self, config: Dict) -> Dict:
        """递归替换配置中的环境变量"""
        if isinstance(config, dict):
            return {k: self._replace_env_vars(v) for k, v in config.items()}
        elif isinstance(config, list):
            return [self._replace_env_vars(item) for item in config]
        elif isinstance(config, str) and config.startswith("${") and config.endswith("}"):
            # 提取环境变量名
            env_var = config[2:-1]
            env_value = os.getenv(env_var)
            if env_value is None:
                logger.warning(f"环境变量 {env_var} 未设置")
                return config
            return env_value
        else:
            return config
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置值
        
        Args:
            key: 配置键（支持点号分隔的多层键）
            default: 默认值
            
        Returns:
            配置值
        """
        keys = key.split('.')
        value = self.config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value
    
    def get_trading_config(self) -> Dict[str, Any]:
        """获取交易配置"""
        return self.get('trading', {})
    
    def get_strategy_config(self) -> Dict[str, Any]:
        """获取策略配置"""
        return self.get('strategy', {})
    
    def get_services_config(self) -> Dict[str, Any]:
        """获取服务配置"""
        return self.get('services', {})
    
    def get_database_config(self) -> Dict[str, Any]:
        """获取数据库配置"""
        return self.get('database', {})
    
    def get_logging_config(self) -> Dict[str, Any]:
        """获取日志配置"""
        return self.get('logging', {})
    
    def reload(self):
        """重新加载配置"""
        self.config = self._load_config()
        logger.info("配置已重新加载")
