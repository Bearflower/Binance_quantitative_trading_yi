"""
测试工具函数
"""

import pytest
from shared.utils.helpers import (
    generate_timestamp,
    format_symbol,
    parse_interval,
    format_price,
    format_volume,
    calculate_percentage_change,
    round_to_step,
    validate_symbol,
    validate_interval,
    safe_get,
    chunk_list,
)


def test_generate_timestamp():
    """测试时间戳生成"""
    timestamp = generate_timestamp()
    assert isinstance(timestamp, int)
    assert timestamp > 0


def test_format_symbol():
    """测试交易对格式化"""
    assert format_symbol("btcusdt") == "BTCUSDT"
    assert format_symbol("  btcusdt  ") == "BTCUSDT"
    assert format_symbol("BTCUSDT") == "BTCUSDT"


def test_parse_interval():
    """测试周期解析"""
    assert parse_interval("1h") == "1h"
    assert parse_interval("15m") == "15m"
    assert parse_interval("4h") == "4h"
    assert parse_interval("1d") == "1d"


def test_format_price():
    """测试价格格式化"""
    assert format_price(95000.5) == "95000.50"
    assert format_price(95000.123, precision=3) == "95000.123"
    assert format_price(95000.0) == "95000.00"


def test_format_volume():
    """测试成交量格式化"""
    assert format_volume(1234.56789) == "1234.56789000"
    assert format_volume(1234.5, precision=2) == "1234.50"


def test_calculate_percentage_change():
    """测试百分比变化计算"""
    assert calculate_percentage_change(100, 110) == 10.0
    assert calculate_percentage_change(100, 90) == -10.0
    assert calculate_percentage_change(0, 100) == 0.0


def test_round_to_step():
    """测试步长舍入"""
    assert round(round_to_step(123.456, 0.01), 2) == 123.46
    assert round_to_step(123.456, 0.1) == 123.5
    assert round_to_step(123.456, 1) == 123.0


def test_validate_symbol():
    """测试交易对验证"""
    assert validate_symbol("BTC/USDT") is True
    assert validate_symbol("btc/usdt") is True
    assert validate_symbol("BTCUSDT") is False
    assert validate_symbol("INVALID") is False


def test_validate_interval():
    """测试周期验证"""
    assert validate_interval("1h") is True
    assert validate_interval("15m") is True
    assert validate_interval("invalid") is False


def test_safe_get():
    """测试安全获取"""
    data = {"a": {"b": {"c": 123}}}
    
    assert safe_get(data, "a", "b", "c") == 123
    assert safe_get(data, "a", "x", "c", default="default") == "default"
    assert safe_get({}, "a", default="empty") == "empty"


def test_chunk_list():
    """测试列表分块"""
    lst = [1, 2, 3, 4, 5, 6, 7, 8]
    
    chunks = chunk_list(lst, 3)
    assert len(chunks) == 3
    assert chunks[0] == [1, 2, 3]
    assert chunks[1] == [4, 5, 6]
    assert chunks[2] == [7, 8]
