"""
K 线数据获取模块
负责从 Baostock（主）、AKShare（备 1）、AData（备 2）获取股票日线数据并缓存到数据库
Baostock 作为首选数据源，因为它更稳定可靠
"""

import pandas as pd
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import time

from utils.logger import get_logger
from data.database import DatabaseManager

logger = get_logger()


def get_stock_daily_kline(symbol: str, end_date: Optional[str] = None,
                           days: int = 120) -> Optional[pd.DataFrame]:
    """
    获取单只股票的日 K 线数据（Baostock 为主，AKShare 和 AData 为备）
    
    Args:
        symbol: 股票代码，如 '600519.SH'
        end_date: 截止日期，格式 YYYY-MM-DD，默认今天
        days: 获取最近多少天
    
    Returns:
        DataFrame: K 线数据，包含 date, open, high, low, close, volume, amount
    """
    # 1. 优先尝试 Baostock（稳定可靠）
    try:
        logger.debug(f"尝试 Baostock: {symbol}")
        df = _get_from_baostock(symbol, end_date, days)
        if df is not None and len(df) > 0:
            logger.info(f"✅ Baostock: {symbol} 获取到 {len(df)} 条")
            return df
        else:
            logger.debug(f"Baostock: {symbol} 返回空数据，尝试 AKShare")
    except Exception as e:
        logger.warning(f"Baostock 获取 {symbol} 失败：{e}，尝试备用数据源")
    
    # 2. 尝试 AKShare（带重试机制）
    try:
        df = _get_from_akshare_with_retry(symbol, end_date, days)
        if df is not None and len(df) > 0:
            logger.debug(f"AKShare: {symbol} 获取到 {len(df)} 条")
            return df
    except Exception as e:
        logger.warning(f"AKShare 获取 {symbol} 失败：{e}，尝试 AData")
    
    # 3. 尝试 AData
    try:
        df = _get_from_adata(symbol, end_date, days)
        if df is not None and len(df) > 0:
            logger.debug(f"AData: {symbol} 获取到 {len(df)} 条")
            return df
    except Exception as e:
        logger.error(f"AData 获取 {symbol} 失败：{e}")
    
    return None


def _get_from_akshare_with_retry(symbol: str, end_date: Optional[str] = None,
                                  days: int = 120, max_retries: int = 5,
                                  base_retry_delay: float = 3.0) -> Optional[pd.DataFrame]:
    """
    从 AKShare 获取 K 线数据（带重试机制，使用指数退避）
    
    Args:
        symbol: 股票代码
        end_date: 截止日期
        days: 获取天数
        max_retries: 最大重试次数
        base_retry_delay: 基础重试间隔（秒）
    
    Returns:
        DataFrame: K 线数据
    """
    from requests.exceptions import ConnectionError, Timeout
    from urllib3.exceptions import HTTPError
    
    for attempt in range(max_retries):
        try:
            df = _get_from_akshare(symbol, end_date, days)
            if df is not None and len(df) > 0:
                return df
            return None
        except (ConnectionError, Timeout, HTTPError, 
                BrokenPipeError, ConnectionResetError) as e:
            if attempt < max_retries - 1:
                delay = base_retry_delay * (2 ** attempt)
                logger.warning(f"AKShare 获取 {symbol} 失败（网络异常，尝试 {attempt + 1}/{max_retries}）: {e}，{delay}秒后重试...")
                time.sleep(delay)
            else:
                logger.error(f"AKShare 获取 {symbol} 失败（网络异常，已重试 {max_retries} 次）: {e}")
                raise
        except Exception as e:
            if attempt < max_retries - 1:
                delay = base_retry_delay * (2 ** attempt)
                logger.warning(f"AKShare 获取 {symbol} 失败（尝试 {attempt + 1}/{max_retries}）: {e}，{delay}秒后重试...")
                time.sleep(delay)
            else:
                logger.error(f"AKShare 获取 {symbol} 失败（已重试 {max_retries} 次）: {e}")
                raise
    return None


def _get_from_akshare(symbol: str, end_date: Optional[str] = None,
                      days: int = 120) -> Optional[pd.DataFrame]:
    """从 AKShare 获取 K 线数据"""
    try:
        import akshare as ak
        
        code = symbol.split('.')[0]
        market = symbol.split('.')[1]
        
        # 计算日期范围
        if end_date is None:
            end_dt = datetime.now() - timedelta(days=1)
        else:
            end_dt = datetime.strptime(end_date.replace('-', ''), '%Y%m%d')
        
        # 确保不超过今天
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if end_dt >= today:
            end_dt = today - timedelta(days=1)
        
        start_dt = end_dt - timedelta(days=days + 30)
        start_date = start_dt.strftime('%Y%m%d')
        end_date_str = end_dt.strftime('%Y%m%d')
        
        logger.debug(f"AKShare: {symbol} 查询日期 {start_date} 到 {end_date_str}")
        
        # AKShare 获取日 K 线
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date,
            end_date=end_date_str,
            adjust="hfq"  # 后复权
        )
        
        if df is None or len(df) == 0:
            logger.debug(f"AKShare: {symbol} 返回空数据")
            return None
        
        # 重命名列
        df.rename(columns={
            '日期': 'date',
            '开盘': 'open',
            '最高': 'high',
            '最低': 'low',
            '收盘': 'close',
            '成交量': 'volume',
            '成交额': 'amount'
        }, inplace=True, errors='ignore')
        
        # 确保 date 是 datetime 类型
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
        
        # 筛选所需列
        required_cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
        available_cols = [col for col in required_cols if col in df.columns]
        
        if len(available_cols) < 5:
            logger.warning(f"AKShare 返回数据缺少必需列：{df.columns.tolist()}")
            return None
        
        df = df[available_cols]
        df.sort_values('date', inplace=True)
        df.reset_index(drop=True, inplace=True)
        
        # 限制天数
        if len(df) > days:
            df = df.tail(days)
        
        return df
        
    except Exception as e:
        logger.error(f"AKShare 获取 {symbol} 异常：{e}")
        raise


def _get_from_adata(symbol: str, end_date: Optional[str] = None,
                    days: int = 120, adjust: str = 'hfq') -> Optional[pd.DataFrame]:
    """从 AData 获取 K 线数据（备用）"""
    try:
        import adata
        
        code = symbol.split('.')[0]
        
        # 计算日期范围 - 使用实际过去的日期
        if end_date is None:
            end_dt = datetime.now() - timedelta(days=1)
        else:
            end_dt = datetime.strptime(end_date.replace('-', ''), '%Y%m%d')
        
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if end_dt >= today:
            end_dt = today - timedelta(days=1)
        
        start_dt = end_dt - timedelta(days=days + 30)
        start_date = start_dt.strftime('%Y-%m-%d')
        end_date_str = end_dt.strftime('%Y-%m-%d')
        
        # AData 获取日 K 线
        df = adata.stock.market.get_market(
            stock_code=code,
            k_type=1,  # 1=日 K
            start_date=start_date,
            end_date=end_date_str
        )
        
        if df is None or len(df) == 0:
            return None
        
        # 重命名列
        df.rename(columns={
            'time': 'date',
            'open': 'open',
            'high': 'high',
            'low': 'low',
            'close': 'close',
            'volume': 'volume',
            'amount': 'amount'
        }, inplace=True, errors='ignore')
        
        # 确保 date 是 datetime 类型
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
        
        # 筛选所需列
        required_cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
        available_cols = [col for col in required_cols if col in df.columns]
        
        if len(available_cols) < 5:
            return None
        
        df = df[available_cols]
        df.sort_values('date', inplace=True)
        df.reset_index(drop=True, inplace=True)
        
        # 限制天数
        if len(df) > days:
            df = df.tail(days)
        
        return df
        
    except Exception as e:
        logger.error(f"AData 获取 {symbol} 异常：{e}")
        raise


def _get_from_baostock(symbol: str, end_date: Optional[str] = None,
                       days: int = 120) -> Optional[pd.DataFrame]:
    """从 Baostock 获取 K 线数据（最后备用）"""
    try:
        import baostock as bs
        
        code = symbol.split('.')[0]
        market = symbol.split('.')[1]
        
        # 转换市场标识
        bs_market = 'sh' if market == 'SH' else 'sz'
        bs_symbol = f"{bs_market}.{code}"
        
        # 计算日期范围
        if end_date is None:
            # 不减去 1 天，直接获取到今天的数据
            end_dt = datetime.now()
        else:
            end_dt = datetime.strptime(end_date.replace('-', ''), '%Y%m%d')
        
        start_dt = end_dt - timedelta(days=days + 30)
        start_date = start_dt.strftime('%Y-%m-%d')
        end_date_str = end_dt.strftime('%Y-%m-%d')
        
        # 登录
        lg = bs.login()
        if lg.error_code != '0':
            logger.warning(f"Baostock 登录失败：{lg.error_msg}")
            return None
        
        # 获取日 K 线 - 使用标准字段
        # frequency: d=日 K 线，w=周线，m=月线
        # adjustflag: 3=不复权，1=后复权，2=前复权
        rs = bs.query_history_k_data_plus(
            bs_symbol,
            "date,open,high,low,close,volume,amount,turn",
            start_date=start_date,
            end_date=end_date_str,
            frequency="d",
            adjustflag="3"
        )
        
        if rs.error_code != '0':
            bs.logout()
            return None
        
        # 转换为 DataFrame
        data_list = []
        while rs.next():
            row = rs.get_row_data()
            data_list.append(row)
        
        bs.logout()
        
        if not data_list:
            return None
        
        columns = rs.fields
        df = pd.DataFrame(data_list, columns=columns)
        
        # 数据清洗
        df['date'] = pd.to_datetime(df['date'])
        numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        df.sort_values('date', inplace=True)
        df.reset_index(drop=True, inplace=True)
        
        # 限制天数
        if len(df) > days:
            df = df.tail(days)
        
        return df
        
    except Exception as e:
        logger.error(f"Baostock 获取 {symbol} 异常：{e}")
        raise


def fetch_and_cache_kline(db: DatabaseManager, symbol: str, code: str,
                          days: int = 120) -> Optional[pd.DataFrame]:
    """
    获取 K 线数据并缓存到数据库
    
    Args:
        db: 数据库管理器
        symbol: 股票代码（带后缀）
        code: 股票代码（不带后缀）
        days: 获取天数
    
    Returns:
        DataFrame: K 线数据
    """
    # 检查缓存
    cached_df = db.get_kline_history(code, days)
    
    if cached_df is not None and len(cached_df) > 0:
        logger.debug(f"{code} 使用缓存数据：{len(cached_df)} 条")
        return cached_df
    
    # 获取新数据
    kline_df = get_stock_daily_kline(symbol, days=days)
    
    if kline_df is None or len(kline_df) == 0:
        logger.warning(f"{code} 无法获取 K 线数据")
        return None
    
    # 保存到数据库
    db.save_kline_history(code, kline_df)
    logger.debug(f"{code} 已缓存 {len(kline_df)} 条 K 线数据")
    
    return kline_df


def update_all_klines(db: DatabaseManager, stocks: List[Dict], days: int = 120) -> int:
    """
    批量更新所有股票的 K 线数据
    
    Args:
        db: 数据库管理器
        stocks: 股票列表
        days: 获取天数
    
    Returns:
        int: 成功更新的股票数量
    """
    success_count = 0
    total = len(stocks)
    
    for idx, stock in enumerate(stocks):
        code = stock['code']
        symbol = stock['symbol']
        
        try:
            df = fetch_and_cache_kline(db, symbol, code, days)
            if df is not None and len(df) > 0:
                success_count += 1
        except Exception as e:
            logger.error(f"{code} 更新 K 线失败：{e}")
        
        # 每 100 只股票打印进度
        if (idx + 1) % 100 == 0:
            logger.info(f"K 线更新进度：{idx + 1}/{total}，成功：{success_count}")
    
    logger.info(f"K 线更新完成：成功 {success_count}/{total}")
    return success_count


def get_kline_for_pattern(db: DatabaseManager, symbol: str, code: str,
                          days: int = 120) -> Optional[pd.DataFrame]:
    """
    获取 K 线数据用于形态检测
    
    Args:
        db: 数据库管理器
        symbol: 股票代码（带后缀）
        code: 股票代码（不带后缀）
        days: 获取天数
    
    Returns:
        DataFrame: K 线数据
    """
    return fetch_and_cache_kline(db, symbol, code, days)
