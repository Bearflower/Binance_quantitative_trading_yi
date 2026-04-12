"""
日志管理器
实现结构化日志输出、文件轮转等功能
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Optional


class LoggerManager:
    """日志管理器"""
    
    _initialized = False
    _loggers: Dict[str, logging.Logger] = {}
    
    @classmethod
    def initialize(
        cls,
        log_level: str = "INFO",
        log_file: str = "logs/adaptive_grid.log",
        max_size: int = 10 * 1024 * 1024,  # 10MB
        backup_count: int = 5
    ) -> None:
        """
        初始化日志系统
        
        Args:
            log_level: 日志级别
            log_file: 日志文件路径
            max_size: 单个文件最大大小
            backup_count: 备份文件数量
        """
        if cls._initialized:
            return
        
        # 创建日志目录
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 配置根日志记录器
        root_logger = logging.getLogger()
        root_logger.setLevel(getattr(logging, log_level.upper()))
        
        # 清除现有处理器
        root_logger.handlers.clear()
        
        # 创建格式化器
        formatter = logging.Formatter(
            fmt='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # 控制台处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(getattr(logging, log_level.upper()))
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
        
        # 文件处理器（轮转）
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_size,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(getattr(logging, log_level.upper()))
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        
        cls._initialized = True
        
        # 获取并保存常用 logger
        cls._loggers = {
            'main': cls.get_logger('main'),
            'data': cls.get_logger('data'),
            'strategy': cls.get_logger('strategy'),
            'execution': cls.get_logger('execution'),
            'monitoring': cls.get_logger('monitoring')
        }
        
        logging.info(f"日志系统初始化完成：{log_file}")
    
    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        """
        获取指定名称的 logger
        
        Args:
            name: logger 名称
            
        Returns:
            Logger 实例
        """
        if name in cls._loggers:
            return cls._loggers[name]
        
        return logging.getLogger(name)
    
    @classmethod
    def set_level(cls, level: str, logger_name: Optional[str] = None) -> None:
        """
        设置日志级别
        
        Args:
            level: 日志级别
            logger_name: logger 名称（None 表示所有）
        """
        if logger_name:
            logging.getLogger(logger_name).setLevel(getattr(logging, level.upper()))
        else:
            logging.getLogger().setLevel(getattr(logging, level.upper()))
    
    @classmethod
    def log_trade(
        cls,
        symbol: str,
        side: str,
        price: float,
        quantity: float,
        pnl: Optional[float] = None
    ) -> None:
        """
        记录交易日志
        
        Args:
            symbol: 交易对
            side: 方向
            price: 价格
            quantity: 数量
            pnl: 盈亏
        """
        logger = cls.get_logger('trade')
        
        if pnl is not None:
            logger.info(
                f"TRADE | {symbol} | {side} | "
                f"price={price} | qty={quantity} | pnl={pnl:.2f}"
            )
        else:
            logger.info(
                f"TRADE | {symbol} | {side} | "
                f"price={price} | qty={quantity}"
            )
    
    @classmethod
    def log_grid_event(
        cls,
        event_type: str,
        grid_id: str,
        details: str
    ) -> None:
        """
        记录网格事件日志
        
        Args:
            event_type: 事件类型
            grid_id: 网格 ID
            details: 详细信息
        """
        logger = cls.get_logger('grid')
        logger.info(f"GRID | {event_type} | {grid_id} | {details}")
    
    @classmethod
    def log_risk_event(
        cls,
        event_type: str,
        trigger_price: float,
        trigger_pnl: float,
        action: str
    ) -> None:
        """
        记录风险事件日志
        
        Args:
            event_type: 事件类型
            trigger_price: 触发价格
            trigger_pnl: 触发盈亏
            action: 行动
        """
        logger = cls.get_logger('risk')
        logger.warning(
            f"RISK | {event_type} | price={trigger_price} | "
            f"pnl={trigger_pnl:.2%} | action={action}"
        )
    
    @classmethod
    def log_system_status(
        cls,
        market_state: str,
        price: float,
        atr: float,
        adx: float,
        total_pnl: float
    ) -> None:
        """
        记录系统状态日志
        
        Args:
            market_state: 市场状态
            price: 价格
            atr: ATR
            adx: ADX
            total_pnl: 总盈亏
        """
        logger = cls.get_logger('status')
        logger.info(
            f"STATUS | state={market_state} | price={price} | "
            f"atr={atr:.2f} | adx={adx:.2f} | pnl={total_pnl:.2%}"
        )


def get_logger(name: str) -> logging.Logger:
    """
    便捷函数：获取 logger
    
    Args:
        name: logger 名称
        
    Returns:
        Logger 实例
    """
    return LoggerManager.get_logger(name)
