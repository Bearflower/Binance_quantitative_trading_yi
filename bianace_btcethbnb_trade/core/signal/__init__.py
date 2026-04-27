#!/usr/bin/env python3
"""
信号检测模块

基于 traderule.txt 第三章实现信号检测功能：
1. 趋势过滤器（日线 EMA21 方向判断）
2. 入场信号等级判定（S/A 级）
3. 禁止入场条件检查
4. 技术形态识别（基础）

模块结构：
- detector.py: 信号检测核心
- filter.py: 过滤器（ADX、成交量、ATR等）
- validator.py: 验证器（一票否决项）
"""

from .detector import SignalDetector, get_signal_detector
from .filter import SignalFilter
from .validator import SignalValidator

__all__ = [
    'SignalDetector',
    'get_signal_detector',
    'SignalFilter',
    'SignalValidator',
]
