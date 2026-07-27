"""
日志配置模块

提供统一的日志记录和格式化功能
"""

import logging
import sys
import json
from datetime import datetime
from typing import Optional
from pathlib import Path


class JSONFormatter(logging.Formatter):
    """JSON 格式日志格式化器"""
    
    def format(self, record: logging.LogRecord) -> str:
        """格式化为 JSON 字符串"""
        log_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # 添加异常信息
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # 添加额外字段
        for key, value in record.__dict__.items():
            if key not in ["name", "msg", "args", "created", "filename", "funcName", 
                          "levelname", "levelno", "lineno", "module", "msecs", 
                          "pathname", "process", "processName", "relativeCreated",
                          "stack_info", "exc_info", "exc_text", "thread", "threadName"]:
                log_data[key] = value
        
        return json.dumps(log_data, ensure_ascii=False)


class ColoredFormatter(logging.Formatter):
    """彩色日志格式化器（终端用）"""
    
    # ANSI 颜色代码
    COLORS = {
        "DEBUG": "\033[36m",     # 青色
        "INFO": "\033[32m",      # 绿色
        "WARNING": "\033[33m",   # 黄色
        "ERROR": "\033[31m",     # 红色
        "CRITICAL": "\033[35m",  # 紫色
        "RESET": "\033[0m",      # 重置
    }
    
    def format(self, record: logging.LogRecord) -> str:
        """格式化日志（带颜色）"""
        color = self.COLORS.get(record.levelname, self.COLORS["RESET"])
        reset = self.COLORS["RESET"]
        
        log_format = (
            f"{color}%(asctime)s | %(levelname)-8s | %(name)s | %(message)s{reset}"
        )
        
        formatter = logging.Formatter(log_format, "%Y-%m-%d %H:%M:%S")
        return formatter.format(record)


def get_logger(
    name: str,
    level: Optional[str] = None,
    log_file: Optional[str] = None,
    json_format: bool = False
) -> logging.Logger:
    """
    获取日志记录器
    
    Args:
        name: 日志器名称
        level: 日志级别，默认 INFO
        log_file: 日志文件路径，如果提供则写入文件
        json_format: 是否使用 JSON 格式，默认 False（彩色终端格式）
    
    Returns:
        配置好的 Logger 实例
    """
    logger = logging.getLogger(name)
    
    # 如果已经有处理器，直接返回（避免重复添加）
    if logger.handlers:
        return logger
    
    # 设置日志级别
    log_level = level or getattr(logging, "INFO")
    logger.setLevel(log_level)
    
    # 创建控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    
    # 设置格式化器
    if json_format:
        formatter = JSONFormatter()
    else:
        formatter = ColoredFormatter()
    
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 如果指定了日志文件，添加文件处理器
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(log_level)
        file_handler.setFormatter(JSONFormatter())
        logger.addHandler(file_handler)
    
    # 禁止传播到父级日志器
    logger.propagate = False
    
    return logger


# 预定义的日志器
def get_app_logger(service_name: str = "app") -> logging.Logger:
    """获取应用日志器"""
    return get_logger(
        name=service_name,
        level="INFO",
        log_file=f"logs/{service_name}/app.log",
        json_format=False
    )


def get_error_logger(service_name: str = "app") -> logging.Logger:
    """获取错误日志器"""
    return get_logger(
        name=f"{service_name}.error",
        level="ERROR",
        log_file=f"logs/{service_name}/error.log",
        json_format=True
    )


# 全局日志器实例
app_logger = get_app_logger()
error_logger = get_error_logger()
