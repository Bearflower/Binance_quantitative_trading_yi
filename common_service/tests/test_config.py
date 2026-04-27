"""
测试配置模块
"""

import pytest
from shared.core.config import get_settings, Settings


def test_settings_creation():
    """测试配置创建"""
    settings = get_settings()
    
    assert settings is not None
    assert isinstance(settings, Settings)
    assert settings.APP_NAME == "common_service"
    assert settings.APP_VERSION == "1.0.0"


def test_settings_symbols():
    """测试币种列表解析"""
    settings = get_settings()
    
    symbols = settings.symbols_list
    assert isinstance(symbols, list)
    assert len(symbols) > 0
    assert "BTCUSDT" in symbols


def test_settings_intervals():
    """测试周期列表解析"""
    settings = get_settings()
    
    intervals = settings.intervals_list
    assert isinstance(intervals, list)
    assert len(intervals) > 0
    assert "1h" in intervals


def test_settings_webhooks():
    """测试 Webhook 配置"""
    settings = get_settings()
    
    webhooks = settings.all_webhooks
    assert isinstance(webhooks, dict)
    assert "btc_eth" in webhooks
    assert "new_coin" in webhooks
    assert "grid" in webhooks
    assert "inspection" in webhooks
    assert "stock" in webhooks
