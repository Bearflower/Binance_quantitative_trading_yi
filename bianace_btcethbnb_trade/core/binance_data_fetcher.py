#!/usr/bin/env python3
"""
币安 API 数据获取模块

功能：
1. 直接从币安 API 获取 K 线数据
2. 计算技术指标（EMA、ATR、RSI 等）
3. 提供与 K 线服务相同格式的数据
"""

import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class BinanceDataFetcher:
    """币安 API 数据获取类"""
    
    def __init__(self):
        """初始化数据获取器"""
        self.base_url = "https://fapi.binance.com"
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.last_fetch_time: Optional[datetime] = None
        self.cache_duration: timedelta = timedelta(hours=1)
    
    def fetch_market_data(self, symbols: List[str] = None) -> Dict[str, Dict[str, Any]]:
        """
        获取市场行情数据
        
        Args:
            symbols: 交易对列表，默认 ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
        
        Returns:
            行情数据字典 {symbol: data}
        """
        if symbols is None:
            symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']
        
        # 检查缓存是否有效
        if self._is_cache_valid(symbols):
            logger.info("使用缓存的行情数据")
            return self.cache
        
        logger.info(f"从币安 API 获取行情数据：{symbols}")
        
        try:
            # 从币安 API 获取数据
            api_data = self._fetch_from_binance(symbols)
            
            # 处理数据并计算技术指标
            processed_data = self._process_api_data(api_data)
            
            # 更新缓存
            self.cache = processed_data
            self.last_fetch_time = datetime.now()
            
            logger.info(f"成功获取 {len(processed_data)} 个交易对的行情数据")
            return processed_data
            
        except Exception as e:
            logger.error(f"获取行情数据失败：{str(e)}")
            # 如果缓存存在，返回旧缓存
            if self.cache:
                logger.warning("使用旧的缓存数据")
                return self.cache
            raise
    
    def _fetch_from_binance(self, symbols: List[str]) -> Dict[str, Any]:
        """
        从币安 API 获取数据
        
        Args:
            symbols: 交易对列表
        
        Returns:
            API 数据字典
        """
        result = {}
        for symbol in symbols:
            try:
                # 获取多个时间框架的 K 线数据
                klines_data = {}
                for interval in ['1d', '4h', '1h', '15m']:
                    limit = 100 if interval in ['1d', '4h'] else 100
                    klines = self._get_klines_from_binance(symbol, interval, limit=limit)
                    if klines:
                        klines_data[interval] = klines
                
                if klines_data:
                    # 获取最新价格和涨跌幅
                    latest_1h = klines_data.get('1h', [])
                    latest_1d = klines_data.get('1d', [])
                    
                    # 从 1h K 线获取最新价格
                    last_price = float(latest_1h[-1][4]) if latest_1h else 0
                    
                    # 计算 24 小时涨跌幅
                    price_change_percent = 0.0
                    if latest_1d and len(latest_1d) > 0:
                        # 使用日线数据计算涨跌幅
                        open_price = float(latest_1d[-1][1])
                        close_price = float(latest_1d[-1][4])
                        price_change_percent = ((close_price - open_price) / open_price * 100) if open_price > 0 else 0.0
                    elif latest_1h and len(latest_1h) >= 24:
                        # 从 24 根 1h K 线计算涨跌幅
                        open_24h = float(latest_1h[0][1])
                        price_change_percent = ((last_price - open_24h) / open_24h * 100) if open_24h > 0 else 0.0
                    
                    # 获取资金费率
                    funding_rate = self._get_funding_rate(symbol)
                    
                    result[symbol] = {
                        'klines': klines_data,
                        'symbol': symbol,
                        'lastPrice': str(last_price),
                        'priceChangePercent': str(round(price_change_percent, 2)),
                        'funding_rate': str(funding_rate)
                    }
                else:
                    logger.warning(f"无法获取 {symbol} 的 K 线数据")
            except Exception as e:
                logger.error(f"获取 {symbol} 数据失败：{e}")
        
        return result
    
    def _get_klines_from_binance(self, symbol: str, interval: str, limit: int = 100) -> Optional[List]:
        """
        从币安 API 获取 K 线数据
        
        Args:
            symbol: 交易对
            interval: 时间间隔
            limit: 获取数量
        
        Returns:
            K 线数据列表
        """
        try:
            url = f"{self.base_url}/fapi/v1/klines"
            params = {
                "symbol": symbol,
                "interval": interval,
                "limit": limit
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                klines = response.json()
                logger.info(f"✅ 从币安 API 获取 {symbol} {interval} K 线数据 {len(klines)} 条")
                return klines
            else:
                logger.error(f"币安 API HTTP 错误：{response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"获取币安 K 线数据异常：{e}")
            return None
    
    def _get_funding_rate(self, symbol: str) -> float:
        """
        获取资金费率
        
        Args:
            symbol: 交易对
        
        Returns:
            最新资金费率
        """
        try:
            url = f"{self.base_url}/fapi/v1/fundingRate"
            params = {
                "symbol": symbol,
                "limit": 1
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data and len(data) > 0:
                    return float(data[0].get('fundingRate', 0))
            return 0.0
        except Exception as e:
            logger.error(f"获取资金费率失败：{e}")
            return 0.0
    
    def _process_api_data(self, api_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """
        处理 API 数据并计算技术指标
        
        Args:
            api_data: 原始 API 数据
        
        Returns:
            处理后的数据
        """
        logger.info(f"开始处理 API 数据，包含 {len(api_data)} 个交易对")
        processed = {}
        
        for symbol, data in api_data.items():
            logger.info(f"========== 开始处理 {symbol} ==========")
            
            try:
                # 处理 K 线数据
                klines_data = data.get('klines', {})
                indicators = {}
                
                for timeframe in ['1d', '4h', '1h', '15m']:
                    if timeframe not in klines_data:
                        logger.warning(f"{symbol} {timeframe} 不在 klines_data 中")
                        indicators[timeframe] = {}
                        continue
                    
                    kline_list = klines_data[timeframe]
                    
                    if not kline_list or len(kline_list) == 0:
                        logger.warning(f"{symbol} {timeframe} K 线数据为空")
                        indicators[timeframe] = {}
                        continue
                    
                    # 转换币安 K 线数据为字典格式
                    # 币安 K 线格式：[open_time, open, high, low, close, volume, ...]
                    kline_dict = {
                        'close': [float(k[4]) for k in kline_list],
                        'high': [float(k[2]) for k in kline_list],
                        'low': [float(k[3]) for k in kline_list],
                        'open': [float(k[1]) for k in kline_list],
                        'volume': [float(k[5]) for k in kline_list],
                    }
                    
                    logger.info(f"{symbol} {timeframe} K 线数据转换完成，close 长度：{len(kline_dict['close'])}")
                    
                    indicators[timeframe] = self._calculate_timeframe_indicators(
                        kline_dict, 
                        timeframe
                    )
                    logger.info(f"{symbol} {timeframe} 指标计算完成")
                
                # 组装处理后的数据
                logger.info(f"{symbol} 开始组装最终数据")
                
                last_price = Decimal(str(data.get('lastPrice', '0')))
                price_change_val = data.get('priceChangePercent', '0')
                try:
                    price_change_24h = Decimal(str(price_change_val)) / Decimal('100')
                except Exception as e:
                    logger.error(f"{symbol} price_change_24h 计算失败：{e}")
                    price_change_24h = Decimal('0')
                
                funding_rate = Decimal(str(data.get('funding_rate', '0')))
                
                processed[symbol] = {
                    'symbol': symbol,
                    'last_price': last_price,
                    'price_change_24h': price_change_24h,
                    'funding_rate': funding_rate,
                    'indicators': indicators,
                    'timestamp': datetime.now(),
                }
                
                logger.info(f"{symbol} 处理成功！last_price={last_price}, indicators 时间框架数：{len(indicators)}")
                logger.info(f"========== {symbol} 处理完成 ==========\n")
                
            except Exception as e:
                import traceback
                error_details = traceback.format_exc()
                logger.error(f"❌❌❌ {symbol} 处理失败：{str(e)}")
                logger.error(f"详细错误：{error_details}")
                continue
        
        logger.info(f"所有交易对处理完成，成功：{len(processed)}")
        return processed
    
    def _calculate_timeframe_indicators(self, kline_data: Dict[str, List], timeframe: str) -> Dict[str, Any]:
        """
        计算单个时间框架的技术指标
        
        Args:
            kline_data: K 线数据字典 {'close': [...], 'high': [...], ...}
            timeframe: 时间框架
        
        Returns:
            指标字典
        """
        try:
            closes = kline_data.get('close', [])
            highs = kline_data.get('high', [])
            lows = kline_data.get('low', [])
            
            # 检查是否有数据
            if not closes or len(closes) == 0:
                logger.warning(f"{timeframe} 没有 K 线数据")
                return {
                    'close': None,
                    'high': None,
                    'low': None,
                }
            
            # 转换为 pandas Series
            close_series = pd.Series(closes)
            high_series = pd.Series(highs)
            low_series = pd.Series(lows)
            
            # 基础数据
            result = {
                'close': float(close_series.iloc[-1]) if len(close_series) > 0 else None,
                'high': float(high_series.iloc[-1]) if len(high_series) > 0 else None,
                'low': float(low_series.iloc[-1]) if len(low_series) > 0 else None,
            }
            
            # 检查数据是否足够计算指标
            if len(closes) < 21:
                logger.warning(f"{timeframe} K 线数据不足 (只有{len(closes)}条)，无法计算 EMA21 等指标")
                return result
            
            # 计算 EMA21
            ema21 = self._calculate_ema(close_series, period=21)
            
            # 计算 ATR14
            atr14 = self._calculate_atr(high_series, low_series, close_series, period=14)
            
            # 计算 RSI14 - 使用标准算法
            rsi14 = self._calculate_rsi(close_series, period=14)
            
            # 调试日志
            logger.info(f"{timeframe} RSI 计算结果：长度={len(rsi14)}, 最后 5 个值={rsi14.tail().tolist() if len(rsi14) > 0 else '空'}")
            logger.info(f"{timeframe} RSI 是否有 NaN: {rsi14.isna().sum()}")
            
            # 修复 NaN 值处理：使用 ffill + bfill 填充初始 NaN
            # RSI 计算会在前 period 个位置产生 NaN，需要填充
            if rsi14.isna().sum() > 0:
                logger.info(f"{timeframe} 填充 RSI NaN 值，数量={rsi14.isna().sum()}")
                rsi14 = rsi14.ffill().bfill()
                logger.info(f"{timeframe} 填充后 RSI 是否有 NaN: {rsi14.isna().sum()}")
            
            # 设置指标值
            if len(ema21) > 0 and not pd.isna(ema21.iloc[-1]):
                result['ema21'] = float(ema21.iloc[-1])
                result['ema21_list'] = [float(e) for e in ema21]
            else:
                result['ema21'] = None
                result['ema21_list'] = []
                
            if len(atr14) > 0 and not pd.isna(atr14.iloc[-1]):
                result['atr14'] = float(atr14.iloc[-1])
                result['atr14_list'] = [float(a) for a in atr14]
            else:
                result['atr14'] = None
                result['atr14_list'] = []
                
            if len(rsi14) > 0 and not pd.isna(rsi14.iloc[-1]):
                result['rsi'] = float(rsi14.iloc[-1])
                result['rsi_list'] = [float(r) for r in rsi14]
                logger.info(f"{timeframe} RSI 设置成功：{result['rsi']}")
            else:
                result['rsi'] = None
                result['rsi_list'] = []
                logger.warning(f"{timeframe} RSI 计算结果为空或 NaN")
            
            return result
            
        except Exception as e:
            import traceback
            logger.error(f"计算 {timeframe} 指标失败：{str(e)}")
            logger.error(f"堆栈跟踪：{traceback.format_exc()}")
            return {}
    
    def _calculate_ema(self, prices: pd.Series, period: int) -> pd.Series:
        """计算指数移动平均线"""
        return prices.ewm(span=period, adjust=False).mean()
    
    def _calculate_atr(self, high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        """计算平均真实波动幅度"""
        tr1 = high - low
        tr2 = abs(high - close.shift())
        tr3 = abs(low - close.shift())
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(window=period).mean()
    
    def _calculate_rsi(self, close: pd.Series, period: int = 14) -> pd.Series:
        """
        计算相对强弱指标（标准算法）
        
        使用与 K 线服务相同的 RSI 计算逻辑，确保一致性
        """
        delta = close.diff()
        
        # 分离涨跌
        gain = delta.where(delta > 0, 0)
        loss = (-delta).where(delta < 0, 0)
        
        # 计算平均涨跌幅（使用 rolling mean）
        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()
        
        # 计算 RS 和 RSI
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def _is_cache_valid(self, symbols: List[str]) -> bool:
        """检查缓存是否有效"""
        if not self.cache:
            return False
        
        if self.last_fetch_time is None:
            return False
        
        # 检查缓存时间
        if datetime.now() - self.last_fetch_time > self.cache_duration:
            return False
        
        # 检查是否包含所有请求的交易对
        for symbol in symbols:
            if symbol not in self.cache:
                return False
        
        return True
    
    def get_symbol_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取单个交易对的行情数据
        
        Args:
            symbol: 交易对
        
        Returns:
            行情数据，如果不存在则返回 None
        """
        if symbol not in self.cache:
            self.fetch_market_data([symbol])
        
        return self.cache.get(symbol)
    
    def clear_cache(self):
        """清除缓存"""
        self.cache.clear()
        self.last_fetch_time = None
        logger.info("币安 API 数据缓存已清除")


# 全局实例
_global_fetcher: Optional[BinanceDataFetcher] = None


def get_binance_data_fetcher() -> BinanceDataFetcher:
    """获取币安数据获取器实例（单例模式）"""
    global _global_fetcher
    if _global_fetcher is None:
        _global_fetcher = BinanceDataFetcher()
    return _global_fetcher
