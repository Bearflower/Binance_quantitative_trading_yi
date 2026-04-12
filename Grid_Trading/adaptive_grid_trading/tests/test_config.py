"""
测试配置加载器
"""

import pytest
from src.utils.config_loader import ConfigLoader


def test_config_loader():
    """测试配置加载"""
    loader = ConfigLoader("config/config.yaml.template")
    
    # 测试加载模板文件
    config = loader.load()
    
    assert 'exchange' in config
    assert 'strategy' in config
    assert 'execution' in config
    assert 'monitoring' in config


def test_get_config_value():
    """测试获取配置值"""
    loader = ConfigLoader("config/config.yaml.template")
    loader.load()
    
    # 测试点号访问
    adx_period = loader.get('strategy.indicators.adx_period')
    assert adx_period == 14
    
    # 测试默认值
    value = loader.get('nonexistent.key', default=100)
    assert value == 100


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
