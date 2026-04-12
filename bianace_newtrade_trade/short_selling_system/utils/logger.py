"""
日志工具
"""

import logging
import sys
from pathlib import Path

import structlog


def setup_logger(log_level: str = "INFO", log_file: str = None):
    """配置日志系统"""
    
    # 确保日志目录存在
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 配置 handlers
    handlers = [logging.StreamHandler(sys.stdout)]
    
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding='utf-8'))
    
    # 配置 logging
    logging.basicConfig(
        format='%(message)s',
        level=getattr(logging, log_level.upper()),
        handlers=handlers
    )
    
    # 配置 structlog
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
            structlog.dev.ConsoleRenderer()
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper())
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False
    )


# 创建全局 logger 实例
logger = structlog.get_logger()

# 初始化日志配置
# 注意：实际使用时会从配置文件读取 log_level 和 log_file
setup_logger(log_level="INFO", log_file="logs/app.log")
