"""
数据源管理模块
支持 AKShare（主）、AData（备 1）和 Baostock（备 2）三数据源
AKShare 作为首选，因为它返回完整的 A 股列表（包括沪市、深市、创业板）
"""

import pandas as pd
from typing import Optional, Dict
from datetime import datetime

from utils.logger import get_logger

logger = get_logger()


class DataSourceManager:
    """数据源管理器，支持自动切换"""
    
    def __init__(self, primary_source: str = "akshare"):
        """
        初始化数据源管理器
        
        Args:
            primary_source: 首选数据源 ("akshare"、"adata" 或 "baostock")
        """
        self.primary_source = primary_source
        self.akshare_available = True
        self.adata_available = True
        self.baostock_available = True
        
    def get_stock_list(self) -> Optional[pd.DataFrame]:
        """
        获取股票列表，自动切换数据源
        
        Returns:
            DataFrame: 包含 code, name, symbol 列的股票列表
        """
        logger.info(f"开始获取股票列表（首选：{self.primary_source}）")
        
        # 尝试首选数据源 AKShare
        if self.primary_source == "akshare" and self.akshare_available:
            try:
                df = self._get_from_akshare()
                if df is not None and len(df) > 0:
                    logger.info(f"✅ 从 AKShare 获取成功：{len(df)} 只股票")
                    return df
            except Exception as e:
                logger.warning(f"⚠️ AKShare 获取失败：{e}")
                self.akshare_available = False
        
        # 尝试备用数据源 AData
        if self.adata_available:
            try:
                df = self._get_from_adata()
                if df is not None and len(df) > 0:
                    logger.info(f"✅ 从 AData 获取成功：{len(df)} 只股票")
                    return df
            except Exception as e:
                logger.warning(f"⚠️ AData 获取失败：{e}")
                self.adata_available = False
        
        # 尝试备用数据源 Baostock
        if self.baostock_available:
            try:
                df = self._get_from_baostock()
                if df is not None and len(df) > 0:
                    logger.info(f"✅ 从 Baostock 获取成功：{len(df)} 只股票")
                    return df
            except Exception as e:
                logger.warning(f"⚠️ Baostock 获取失败：{e}")
                self.baostock_available = False
        
        # 三个数据源都失败
        logger.error("❌ 所有数据源都不可用")
        return None
    
    def _get_from_akshare(self, max_retries: int = 3, retry_delay: int = 10) -> Optional[pd.DataFrame]:
        """
        从 AKShare 获取股票列表（支持重试）
        AKShare 返回完整的 A 股列表，包括沪市、深市主板、创业板
        """
        try:
            import akshare as ak
            
            for attempt in range(max_retries):
                try:
                    logger.info(f"正在连接 AKShare 数据源...（尝试 {attempt + 1}/{max_retries}）")
                    
                    # 设置超时
                    import signal
                    
                    def timeout_handler(signum, frame):
                        raise TimeoutError("AKShare 请求超时（60 秒）")
                    
                    signal.signal(signal.SIGALRM, timeout_handler)
                    signal.alarm(60)
                    
                    try:
                        # AKShare 获取全 A 股股票列表
                        stock_df = ak.stock_info_a_code_name()
                    finally:
                        signal.alarm(0)
                    
                    if stock_df is None or len(stock_df) == 0:
                        logger.warning("AKShare 返回空数据")
                        if attempt < max_retries - 1:
                            logger.info(f"{retry_delay}秒后重试...")
                            import time
                            time.sleep(retry_delay)
                            continue
                        return None
                    
                    logger.info(f"AKShare 获取到 {len(stock_df)} 只股票，列名：{stock_df.columns.tolist()}")
                    
                    # 统计市场分布
                    sh_count = len(stock_df[stock_df['code'].str.startswith('6')])
                    sz_main_count = len(stock_df[stock_df['code'].str.startswith('00')])
                    sz_chi_count = len(stock_df[stock_df['code'].str.startswith('30')])
                    logger.info(f"市场分布：沪市{sh_count}只，深市主板{sz_main_count}只，创业板{sz_chi_count}只")
                    
                    # 构建结果 DataFrame
                    result_df = pd.DataFrame()
                    
                    if 'code' in stock_df.columns:
                        result_df['code'] = stock_df['code'].astype(str)
                    else:
                        logger.warning(f"AKShare 缺少 code 列，可用列：{stock_df.columns.tolist()}")
                        if attempt < max_retries - 1:
                            logger.info(f"{retry_delay}秒后重试...")
                            import time
                            time.sleep(retry_delay)
                            continue
                        return None
                    
                    if 'name' in stock_df.columns:
                        result_df['name'] = stock_df['name']
                    else:
                        logger.warning(f"AKShare 缺少 name 列，可用列：{stock_df.columns.tolist()}")
                        if attempt < max_retries - 1:
                            logger.info(f"{retry_delay}秒后重试...")
                            import time
                            time.sleep(retry_delay)
                            continue
                        return None
                    
                    # 生成 symbol（代码。市场）
                    result_df['symbol'] = result_df['code'].apply(
                        lambda x: f"{x}.SH" if x.startswith('6') or x.startswith('5') else f"{x}.SZ"
                    )
                    
                    return result_df[['code', 'name', 'symbol']]
                    
                except (TimeoutError, ConnectionError, Exception) as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"AKShare 请求失败（{e}），{retry_delay}秒后重试...")
                        import time
                        time.sleep(retry_delay)
                    else:
                        raise
            
            return None
            
        except ImportError:
            logger.warning("未安装 akshare 库，跳过 AKShare 数据源")
            self.akshare_available = False
            return None
        except Exception as e:
            logger.error(f"AKShare 获取异常：{e}")
            raise
    
    def _get_from_adata(self, max_retries: int = 3, retry_delay: int = 10) -> Optional[pd.DataFrame]:
        """从 AData 获取股票列表（支持重试）"""
        try:
            import adata
            
            for attempt in range(max_retries):
                try:
                    logger.info(f"正在连接 AData 数据源...（尝试 {attempt + 1}/{max_retries}）")
                    
                    # 获取 A 股股票列表（设置超时）
                    import signal
                    
                    def timeout_handler(signum, frame):
                        raise TimeoutError("AData 请求超时（30 秒）")
                    
                    # 设置 30 秒超时
                    signal.signal(signal.SIGALRM, timeout_handler)
                    signal.alarm(30)
                    
                    try:
                        stock_df = adata.stock.info.all_code()
                    finally:
                        signal.alarm(0)  # 取消闹钟
                    
                    if stock_df is None or len(stock_df) == 0:
                        logger.warning("AData 返回空数据")
                        if attempt < max_retries - 1:
                            logger.info(f"{retry_delay}秒后重试...")
                            import time
                            time.sleep(retry_delay)
                            continue
                        return None
                    
                    logger.info(f"AData 获取到 {len(stock_df)} 只股票，列名：{stock_df.columns.tolist()}")
                    
                    # 处理不同的列名情况
                    # AData 可能返回：code/name, 或 ts_code/name, 或 stock_code/short_name
                    result_df = pd.DataFrame()
                    
                    if 'code' in stock_df.columns:
                        result_df['code'] = stock_df['code'].astype(str)
                    elif 'ts_code' in stock_df.columns:
                        result_df['code'] = stock_df['ts_code'].astype(str)
                    elif 'stock_code' in stock_df.columns:
                        result_df['code'] = stock_df['stock_code'].astype(str)
                    else:
                        logger.warning(f"AData 缺少 code 列，可用列：{stock_df.columns.tolist()}")
                        if attempt < max_retries - 1:
                            logger.info(f"{retry_delay}秒后重试...")
                            import time
                            time.sleep(retry_delay)
                            continue
                        return None
                    
                    if 'name' in stock_df.columns:
                        result_df['name'] = stock_df['name']
                    elif 'short_name' in stock_df.columns:
                        result_df['name'] = stock_df['short_name']
                    else:
                        logger.warning(f"AData 缺少 name 列，可用列：{stock_df.columns.tolist()}")
                        if attempt < max_retries - 1:
                            logger.info(f"{retry_delay}秒后重试...")
                            import time
                            time.sleep(retry_delay)
                            continue
                        return None
                    
                    # 确保 symbol 格式正确（代码。市场）
                    if 'symbol' in stock_df.columns:
                        result_df['symbol'] = stock_df['symbol']
                    else:
                        result_df['symbol'] = result_df['code'].apply(
                            lambda x: f"{x}.SH" if x.startswith('6') or x.startswith('5') else f"{x}.SZ"
                        )
                    
                    return result_df[['code', 'name', 'symbol']]
                    
                except (TimeoutError, ConnectionError, Exception) as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"AData 请求失败（{e}），{retry_delay}秒后重试...")
                        import time
                        time.sleep(retry_delay)
                    else:
                        raise
            
            return None
            
        except ImportError:
            logger.warning("未安装 adata 库，跳过 AData 数据源")
            self.adata_available = False
            return None
        except Exception as e:
            logger.error(f"AData 获取异常：{e}")
            raise
    
    def _get_from_baostock(self) -> Optional[pd.DataFrame]:
        """从 Baostock 获取股票列表"""
        try:
            import baostock as bs
            
            logger.info("正在连接 Baostock 数据源...")
            
            # 登录
            lg = bs.login()
            if lg.error_code != '0':
                logger.error(f"Baostock 登录失败：{lg.error_msg}")
                return None
            
            logger.info(f"Baostock 登录成功：{lg.error_msg}")
            
            # 获取股票基本信息
            query_result = bs.query_stock_basic()
            
            # 转换为 DataFrame
            data_list = []
            while query_result.next():
                row = query_result.get_row_data()
                data_list.append(row)
            
            # 登出
            bs.logout()
            
            if query_result.error_code != '0':
                logger.error(f"Baostock 获取股票列表失败：{query_result.error_msg}")
                return None
            
            if not data_list:
                logger.warning("Baostock 返回空数据")
                return None
            
            # 获取列名
            columns = query_result.fields
            stock_df = pd.DataFrame(data_list, columns=columns)
            
            logger.info(f"Baostock 获取到 {len(stock_df)} 只股票")
            
            # 提取所需列
            # Baostock 返回：code, code_name, ipoDate, outDate, stockType, status
            # 注意：Baostock 的 code 列包含前缀 (sh./sz.)，需要去掉
            result_df = pd.DataFrame()
            
            if 'code' in stock_df.columns:
                # 去掉前缀 (sh. 或 sz.)，只保留 6 位数字代码
                result_df['code'] = stock_df['code'].astype(str).str.replace(r'^[a-z]+\.', '', regex=True)
                # 过滤掉非 6 位数字的代码（指数、基金等）
                result_df = result_df[result_df['code'].str.match(r'^\d{6}$')]
            else:
                logger.warning(f"Baostock 缺少 code 列")
                return None
            
            if 'code_name' in stock_df.columns:
                result_df['name'] = stock_df['code_name']
            else:
                logger.warning(f"Baostock 缺少 code_name 列")
                return None
            
            # 生成 symbol（代码。市场）
            result_df['symbol'] = result_df['code'].apply(
                lambda x: f"{x}.SH" if x.startswith('6') or x.startswith('5') else f"{x}.SZ"
            )
            
            # 统计市场分布
            sh_count = len(result_df[result_df['code'].str.startswith('6')])
            sz_main_count = len(result_df[result_df['code'].str.startswith('00')])
            sz_chi_count = len(result_df[result_df['code'].str.startswith('30')])
            logger.info(f"市场分布：沪市{sh_count}只，深市主板{sz_main_count}只，创业板{sz_chi_count}只")
            
            return result_df[['code', 'name', 'symbol']]
            
        except ImportError:
            logger.warning("未安装 baostock 库，跳过 Baostock 数据源")
            self.baostock_available = False
            return None
        except Exception as e:
            logger.error(f"Baostock 获取异常：{e}")
            raise


# 全局数据源管理器实例
_data_source: Optional[DataSourceManager] = None


def get_data_source(primary: str = "adata") -> DataSourceManager:
    """获取全局数据源管理器"""
    global _data_source
    if _data_source is None:
        _data_source = DataSourceManager(primary_source=primary)
    return _data_source


def get_stock_list_from_data_source() -> pd.DataFrame:
    """
    从配置的数据源获取股票列表
    
    Returns:
        DataFrame: 股票列表
    """
    source_manager = get_data_source()
    df = source_manager.get_stock_list()
    
    if df is None:
        raise Exception("所有数据源都不可用")
    
    return df
