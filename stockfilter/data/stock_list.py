"""
股票列表管理模块
负责获取全市场股票列表并过滤
"""

import pandas as pd
from datetime import datetime, timedelta
from typing import Optional, Dict

from utils.logger import get_logger
from data.database import DatabaseManager
from data.data_source import get_stock_list_from_data_source

logger = get_logger()


def get_stock_list_from_akshare(max_retries: int = 5, retry_delay: int = 15) -> pd.DataFrame:
    """
    从数据源获取 A 股股票列表（支持 AData 和 AKShare 双数据源）
    
    Args:
        max_retries: 最大重试次数（备用 AKShare 时使用）
        retry_delay: 重试间隔（秒）
    
    Returns:
        DataFrame: 包含 code, name, symbol 列的股票列表
    """
    return get_stock_list_from_data_source()


def filter_stock_list(df: pd.DataFrame, config: Optional[Dict] = None) -> pd.DataFrame:
    """
    过滤股票列表
    
    Args:
        df: 原始股票列表
        config: 过滤配置
    
    Returns:
        DataFrame: 过滤后的股票列表
    """
    if config is None:
        config = {}
    
    original_count = len(df)
    logger.info(f"原始股票数量：{original_count}")
    
    if config.get('exclude_st', True):
        df = df[~df['name'].str.contains('ST', na=False)]
        logger.info(f"剔除 ST 股票后：{len(df)} 只")
    
    if config.get('exclude_beijing', True):
        df = df[~df['code'].str.match('^(8|920|83|87)')]
        logger.info(f"剔除北交所后：{len(df)} 只")
    
    if config.get('exclude_delisting', True):
        df = df[~df['name'].str.contains('退', na=False)]
        logger.info(f"剔除退市股票后：{len(df)} 只")
    
    if config.get('exclude_star_market', True):
        df = df[~df['code'].str.startswith('688')]
        logger.info(f"剔除科创板后：{len(df)} 只")
    
    if config.get('exclude_chi_next', True):
        df = df[~df['code'].str.startswith(('300', '301'))]
        logger.info(f"剔除创业板后：{len(df)} 只")
    
    filtered_count = len(df)
    logger.info(f"过滤后股票数量：{filtered_count} 只（剔除 {original_count - filtered_count} 只）")
    
    return df


def sync_stock_list_to_db(db: DatabaseManager, config: Optional[Dict] = None, max_retries: int = 3):
    """
    同步股票列表到数据库
    
    Args:
        db: 数据库管理器
        config: 过滤配置
        max_retries: 获取股票列表的最大重试次数
    """
    logger.info("开始同步股票列表...")
    
    stock_df = get_stock_list_from_akshare(max_retries=max_retries, retry_delay=20)
    
    filtered_df = filter_stock_list(stock_df, config)
    
    db.save_stock_list(filtered_df)
    logger.info(f"成功同步 {len(filtered_df)} 只股票到数据库")
    
    return filtered_df


def get_stock_list_from_db(db: DatabaseManager, config: Optional[Dict] = None) -> pd.DataFrame:
    """
    从数据库获取股票列表
    
    Args:
        db: 数据库管理器
        config: 过滤配置
    
    Returns:
        DataFrame: 股票列表
    """
    df = db.get_stock_list(config)
    logger.info(f"从数据库获取到 {len(df)} 只股票")
    return df


def init_stock_list(config: Optional[Dict] = None) -> pd.DataFrame:
    """
    初始化股票列表（获取并同步到数据库）
    
    Args:
        config: 过滤配置
    
    Returns:
        DataFrame: 过滤后的股票列表
    """
    db = DatabaseManager()
    try:
        return sync_stock_list_to_db(db, config)
    finally:
        db.close()
