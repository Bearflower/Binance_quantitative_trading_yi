#!/usr/bin/env python3
"""
评分引擎工厂

提供统一的评分引擎创建接口，支持版本选择
"""

from typing import Optional
from .base import ScoringEngineBase
from .v612 import ScoringEngineV612


# 支持的版本映射
VERSION_MAP = {
    'v6.12': ScoringEngineV612,
    'v612': ScoringEngineV612,
    'latest': ScoringEngineV612,
    'production': ScoringEngineV612,
}

# 默认版本
DEFAULT_VERSION = 'v6.12'


def create_scoring_engine(
    version: Optional[str] = None,
    config_file: Optional[str] = None
) -> ScoringEngineBase:
    """
    创建评分引擎实例

    Args:
        version: 版本号，支持：
            - 'v6.12' 或 'v612': v6.12版本
            - 'latest': 最新版本
            - 'production': 生产环境版本
            - None: 使用默认版本
        config_file: 配置文件路径

    Returns:
        评分引擎实例

    Raises:
        ValueError: 不支持的版本号

    Examples:
        >>> # 使用默认版本
        >>> engine = create_scoring_engine()

        >>> # 指定版本
        >>> engine = create_scoring_engine('v6.12')

        >>> # 指定配置文件
        >>> engine = create_scoring_engine(config_file='config/scoring_params.yaml')
    """
    # 确定版本
    version = version or DEFAULT_VERSION

    # 获取引擎类
    engine_class = VERSION_MAP.get(version)
    if engine_class is None:
        raise ValueError(
            f"不支持的评分引擎版本: {version}. "
            f"支持的版本: {list(VERSION_MAP.keys())}"
        )

    # 创建实例
    if config_file:
        return engine_class(config_file=config_file)
    else:
        return engine_class()


def get_scoring_engine() -> ScoringEngineBase:
    """
    获取当前生产环境的评分引擎实例

    这是向后兼容的接口，返回默认的生产环境版本

    Returns:
        评分引擎实例（v6.12）

    Examples:
        >>> engine = get_scoring_engine()
        >>> result = engine.score('BTCUSDT', data)
    """
    return create_scoring_engine('production')


def get_scoring_engine_v612() -> ScoringEngineV612:
    """
    获取 v6.12 评分引擎实例

    这是向后兼容的接口

    Returns:
        v6.12 评分引擎实例

    Examples:
        >>> engine = get_scoring_engine_v612()
        >>> result = engine.score('BTCUSDT', data)
    """
    return create_scoring_engine('v6.12')


def list_available_versions() -> list:
    """
    列出所有可用的评分引擎版本

    Returns:
        可用版本列表

    Examples:
        >>> versions = list_available_versions()
        >>> print(versions)
        ['v6.12', 'v612', 'latest', 'production']
    """
    return list(VERSION_MAP.keys())
