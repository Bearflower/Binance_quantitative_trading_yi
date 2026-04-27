#!/usr/bin/env python3
"""
统一日志配置

提供统一的日志配置和管理功能。

版本: v1.0.0
创建时间: 2026-04-27
"""

import os
import logging
import logging.handlers
from pathlib import Path
from typing import Optional


def setup_logger(
    name: Optional[str] = None,
    log_file: Optional[str] = None,
    level: str = "INFO",
    format_str: Optional[str] = None,
    max_bytes: int = 10485760,  # 10MB
    backup_count: int = 5
) -> logging.Logger:
    """
    设置日志记录器
    
    Args:
        name: 日志记录器名称（None 表示根记录器）
        log_file: 日志文件路径（可选）
        level: 日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）
        format_str: 日志格式字符串
        max_bytes: 日志文件最大字节数
        backup_count: 保留的日志文件数量
    
    Returns:
        配置好的日志记录器
    """
    # 获取日志记录器
    logger = logging.getLogger(name)
    
    # 设置日志级别
    level_map = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }
    logger.setLevel(level_map.get(level.upper(), logging.INFO))
    
    # 默认日志格式
    if format_str is None:
        format_str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    formatter = logging.Formatter(format_str)
    
    # 清除现有的处理器
    logger.handlers.clear()
    
    # 添加控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logger.level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 添加文件处理器（如果指定了日志文件）
    if log_file:
        # 确保日志目录存在
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 使用 RotatingFileHandler 实现日志滚动
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(logger.level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    获取日志记录器
    
    Args:
        name: 日志记录器名称
    
    Returns:
        日志记录器
    """
    return logging.getLogger(name)


def setup_root_logger(
    log_dir: str = "./logs",
    log_level: str = "INFO",
    log_format: Optional[str] = None
):
    """
    设置根日志记录器
    
    Args:
        log_dir: 日志目录
        log_level: 日志级别
        log_format: 日志格式
    """
    # 确保日志目录存在
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    
    # 设置根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # 设置为最低级别，由处理器控制
    
    # 清除现有的处理器
    root_logger.handlers.clear()
    
    # 默认日志格式
    if log_format is None:
        log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    formatter = logging.Formatter(log_format)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)
    
    # 主日志文件处理器
    main_log_file = os.path.join(log_dir, "trading_system.log")
    file_handler = logging.handlers.RotatingFileHandler(
        main_log_file,
        maxBytes=10485760,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)  # 文件记录所有级别
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # 错误日志文件处理器（只记录 ERROR 及以上级别）
    error_log_file = os.path.join(log_dir, "error.log")
    error_handler = logging.handlers.RotatingFileHandler(
        error_log_file,
        maxBytes=10485760,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)
    
    logging.info(f"根日志记录器已初始化（日志目录：{log_dir}，级别：{log_level}）")


# 初始化日志系统
def init_logging():
    """初始化日志系统"""
    try:
        # 尝试从配置文件读取日志配置
        from config.config_manager import get_config
        
        log_dir = get_config('paths.log_dir', './logs')
        log_level = get_config('logging.level', 'INFO')
        log_format = get_config('logging.format', None)
        
        setup_root_logger(log_dir, log_level, log_format)
    except Exception:
        # 如果配置文件不可用，使用默认配置
        setup_root_logger()


# 自动初始化（可选）
# init_logging()
