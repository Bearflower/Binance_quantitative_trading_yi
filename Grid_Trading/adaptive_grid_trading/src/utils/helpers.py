"""
工具函数
提供常用的辅助函数
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def format_price(price: float, precision: int = 2) -> str:
    """
    格式化价格显示
    
    Args:
        price: 价格
        precision: 小数位数
        
    Returns:
        格式化后的价格字符串
    """
    return f"{price:.{precision}f}"


def format_quantity(quantity: float, precision: int = 4) -> str:
    """
    格式化数量显示
    
    Args:
        quantity: 数量
        precision: 小数位数
        
    Returns:
        格式化后的数量字符串
    """
    return f"{quantity:.{precision}f}"


def format_percentage(value: float, precision: int = 2) -> str:
    """
    格式化百分比显示
    
    Args:
        value: 值（0-1 之间）
        precision: 小数位数
        
    Returns:
        格式化后的百分比字符串
    """
    return f"{value * 100:.{precision}f}%"


def calculate_pnl(
    entry_price: float,
    exit_price: float,
    quantity: float,
    side: str,
    fee_rate: float = 0.0004
) -> Dict[str, float]:
    """
    计算盈亏
    
    Args:
        entry_price: 开仓价格
        exit_price: 平仓价格
        quantity: 数量
        side: 方向 (BUY/SELL)
        fee_rate: 手续费率
        
    Returns:
        包含盈亏、手续费等的字典
    """
    if side.upper() == 'BUY':
        gross_pnl = (exit_price - entry_price) * quantity
    else:
        gross_pnl = (entry_price - exit_price) * quantity
    
    # 计算手续费（开仓 + 平仓）
    entry_fee = entry_price * quantity * fee_rate
    exit_fee = exit_price * quantity * fee_rate
    total_fee = entry_fee + exit_fee
    
    net_pnl = gross_pnl - total_fee
    
    # 计算收益率
    margin = entry_price * quantity
    return_rate = net_pnl / margin if margin > 0 else 0
    
    return {
        'gross_pnl': gross_pnl,
        'entry_fee': entry_fee,
        'exit_fee': exit_fee,
        'total_fee': total_fee,
        'net_pnl': net_pnl,
        'return_rate': return_rate
    }


def round_to_step(value: float, step_size: float) -> float:
    """
    按步长舍入
    
    Args:
        value: 值
        step_size: 步长
        
    Returns:
        舍入后的值
    """
    if step_size <= 0:
        return value
    
    return round(value / step_size) * step_size


def is_market_hours() -> bool:
    """
    判断是否在交易时间（加密货币 24/7 交易）
    
    Returns:
        总是返回 True（加密货币市场全天候交易）
    """
    return True


def parse_datetime(dt_string: str) -> Optional[datetime]:
    """
    解析日期时间字符串
    
    Args:
        dt_string: 日期时间字符串
        
    Returns:
        datetime 对象，解析失败返回 None
    """
    formats = [
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%dT%H:%M:%S.%f',
        '%Y-%m-%d'
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(dt_string, fmt)
        except ValueError:
            continue
    
    logger.error(f"无法解析日期时间：{dt_string}")
    return None


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """
    安全除法
    
    Args:
        numerator: 分子
        denominator: 分母
        default: 分母为 0 时的默认值
        
    Returns:
        除法结果
    """
    if denominator == 0:
        return default
    return numerator / denominator


def clamp(value: float, min_value: float, max_value: float) -> float:
    """
    限制值在指定范围内
    
    Args:
        value: 值
        min_value: 最小值
        max_value: 最大值
        
    Returns:
        限制后的值
    """
    return max(min_value, min(max_value, value))


def merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    深度合并两个字典
    
    Args:
        base: 基础字典
        override: 覆盖字典
        
    Returns:
        合并后的字典
    """
    result = base.copy()
    
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = value
    
    return result


def flatten_dict(d: Dict[str, Any], parent_key: str = '', sep: str = '.') -> Dict[str, str]:
    """
    扁平化嵌套字典
    
    Args:
        d: 字典
        parent_key: 父键
        sep: 分隔符
        
    Returns:
        扁平化后的字典
    """
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def mask_sensitive_info(info: str, visible_chars: int = 4) -> str:
    """
    脱敏敏感信息
    
    Args:
        info: 敏感信息
        visible_chars: 显示的字符数
        
    Returns:
        脱敏后的字符串
    """
    if len(info) <= visible_chars:
        return '*' * len(info)
    
    return info[:visible_chars] + '*' * (len(info) - visible_chars)
