"""
K 线数据管理器
负责多时间框架 K 线数据的获取、更新和缓存
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.data.binance_client import BinanceClient

logger = logging.getLogger(__name__)


class KlineManager:
    """K 线数据管理器"""
    
    # 支持的时间间隔
    SUPPORTED_INTERVALS = ['1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w']
    
    def __init__(self, client: BinanceClient, symbol: str = 'BTCUSDT'):
        """
        初始化 K 线管理器
        
        Args:
            client: 币安 API 客户端
            symbol: 交易对
        """
        self.client = client
        self.symbol = symbol
        self._kline_cache: Dict[str, pd.DataFrame] = {}
        self._last_update: Dict[str, datetime] = {}
        self._update_lock = asyncio.Lock()
    
    async def initialize(self):
        """初始化 K 线管理器"""
        logger.info(f"K 线管理器初始化完成：{self.symbol}")
    
    async def get_klines(
        self,
        interval: str,
        limit: int = 100,
        force_update: bool = False
    ) -> pd.DataFrame:
        """
        获取 K 线数据
        
        Args:
            interval: 时间间隔
            limit: 返回数量
            force_update: 是否强制更新
            
        Returns:
            DataFrame 格式的 K 线数据
        """
        if interval not in self.SUPPORTED_INTERVALS:
            raise ValueError(f"不支持的时间间隔：{interval}")
        
        # 检查缓存
        if not force_update and interval in self._kline_cache:
            # 检查是否需要更新（5 分钟内不重复获取）
            last_update = self._last_update.get(interval)
            if last_update and (datetime.now() - last_update).seconds < 300:
                return self._kline_cache[interval]
        
        # 获取数据
        async with self._update_lock:
            # 双重检查锁
            if not force_update and interval in self._kline_cache:
                last_update = self._last_update.get(interval)
                if last_update and (datetime.now() - last_update).seconds < 300:
                    return self._kline_cache[interval]
            
            try:
                klines = await self.client.get_klines(
                    symbol=self.symbol,
                    interval=interval,
                    limit=limit
                )
                
                # 转换为 DataFrame
                df = self._klines_to_dataframe(klines)
                
                # 更新缓存
                self._kline_cache[interval] = df
                self._last_update[interval] = datetime.now()
                
                logger.info(f"获取 {interval} K 线数据成功，共 {len(df)} 条")
                return df
                
            except Exception as e:
                logger.error(f"获取 {interval} K 线数据失败：{e}")
                
                # 如果有缓存，返回旧数据
                if interval in self._kline_cache:
                    logger.warning(f"使用缓存的 {interval} K 线数据")
                    return self._kline_cache[interval]
                
                # 没有缓存，抛出异常
                raise
    
    def _klines_to_dataframe(self, klines: List[Dict]) -> pd.DataFrame:
        """
        将 K 线数据列表转换为 DataFrame，并计算技术指标
        
        Args:
            klines: K 线数据列表
            
        Returns:
            DataFrame（包含技术指标）
        """
        if not klines:
            return pd.DataFrame()
        
        df = pd.DataFrame(klines)
        
        # 转换时间戳
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        df.set_index('open_time', inplace=True)
        
        # 确保数据类型正确
        numeric_columns = ['open', 'high', 'low', 'close', 'volume', 'quote_volume']
        for col in numeric_columns:
            if col in df.columns:
                df[col] = df[col].astype(float)
        
        # 计算技术指标
        self._calculate_indicators(df)
        
        return df
    
    def _calculate_indicators(self, df: pd.DataFrame):
        """
        计算技术指标
        
        Args:
            df: K 线数据 DataFrame
        """
        try:
            # 计算 EMA（使用通用名称）
            df['ema_fast'] = df['close'].ewm(span=20, adjust=False).mean()
            df['ema_slow'] = df['close'].ewm(span=50, adjust=False).mean()
            
            # 同时保留旧名称以兼容
            df['ema_20'] = df['ema_fast']
            df['ema_50'] = df['ema_slow']
            
            # 计算 ATR
            high = df['high']
            low = df['low']
            close = df['close']
            
            tr1 = high - low
            tr2 = abs(high - close.shift(1))
            tr3 = abs(low - close.shift(1))
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            df['atr_14'] = tr.ewm(span=14, adjust=False).mean()
            
            # 计算 ADX
            high_diff = high.diff()
            low_diff = -low.diff()
            
            plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0)
            minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0)
            
            plus_dm = pd.Series(plus_dm, index=high.index)
            minus_dm = pd.Series(minus_dm, index=low.index)
            
            # 平滑 +DM, -DM, TR
            plus_dm_smooth = plus_dm.ewm(span=14, adjust=False).mean()
            minus_dm_smooth = minus_dm.ewm(span=14, adjust=False).mean()
            tr_smooth = tr.ewm(span=14, adjust=False).mean()
            
            # 计算 +DI 和 -DI
            plus_di = 100 * (plus_dm_smooth / tr_smooth)
            minus_di = 100 * (minus_dm_smooth / tr_smooth)
            
            # 计算 DX
            di_sum = plus_di + minus_di
            di_diff = abs(plus_di - minus_di)
            dx = 100 * (di_diff / di_sum)
            
            # 计算 ADX
            df['adx'] = dx.ewm(span=14, adjust=False).mean()
            
        except Exception as e:
            logger.error(f"计算技术指标失败：{e}")
    
    async def get_latest_price(self, interval: str = '1h') -> Optional[float]:
        """
        获取最新价格
        
        Args:
            interval: 时间间隔
            
        Returns:
            最新价格
        """
        try:
            df = await self.get_klines(interval, limit=1)
            if len(df) > 0:
                return df['close'].iloc[-1]
            return None
        except Exception as e:
            logger.error(f"获取最新价格失败：{e}")
            return None
    
    async def get_price_change(
        self,
        interval: str = '1h',
        periods: int = 1
    ) -> Optional[float]:
        """
        获取价格变化率
        
        Args:
            interval: 时间间隔
            periods: 比较的周期数
            
        Returns:
            价格变化率（百分比）
        """
        try:
            df = await self.get_klines(interval, limit=periods + 1)
            if len(df) <= periods:
                return None
            
            current_price = df['close'].iloc[-1]
            old_price = df['close'].iloc[-periods - 1]
            
            return ((current_price - old_price) / old_price) * 100
            
        except Exception as e:
            logger.error(f"计算价格变化率失败：{e}")
            return None
    
    async def get_high_low(
        self,
        interval: str = '1h',
        periods: int = 24
    ) -> Dict[str, float]:
        """
        获取指定周期内的最高价和最低价
        
        Args:
            interval: 时间间隔
            periods: 周期数
            
        Returns:
            {'high': 最高价，'low': 最低价}
        """
        try:
            df = await self.get_klines(interval, limit=periods)
            if len(df) == 0:
                return {'high': 0.0, 'low': 0.0}
            
            return {
                'high': df['high'].max(),
                'low': df['low'].min()
            }
            
        except Exception as e:
            logger.error(f"获取高低点失败：{e}")
            return {'high': 0.0, 'low': 0.0}
    
    async def update_all_intervals(self, intervals: List[str] = None) -> None:
        """
        更新所有指定时间间隔的 K 线数据
        
        Args:
            intervals: 时间间隔列表，默认更新 1h 和 4h
        """
        if intervals is None:
            intervals = ['1h', '4h']
        
        tasks = []
        for interval in intervals:
            if interval in self.SUPPORTED_INTERVALS:
                tasks.append(self.get_klines(interval, force_update=True))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    def get_cached_intervals(self) -> List[str]:
        """获取已缓存的时间间隔列表"""
        return list(self._kline_cache.keys())
    
    def clear_cache(self, interval: str = None) -> None:
        """
        清除缓存
        
        Args:
            interval: 指定清除的时间间隔，None 表示清除所有
        """
        if interval:
            if interval in self._kline_cache:
                del self._kline_cache[interval]
                logger.info(f"已清除 {interval} K 线缓存")
        else:
            self._kline_cache.clear()
            self._last_update.clear()
            logger.info("已清除所有 K 线缓存")
