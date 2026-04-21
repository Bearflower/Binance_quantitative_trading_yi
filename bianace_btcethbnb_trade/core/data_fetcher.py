#!/usr/bin/env python3
"""
行情数据获取模块

功能：
1. 每小时从通用 K 线服务获取行情数据
2. 支持多时间框架（日线、4 小时、1 小时、15 分钟）
3. 计算技术指标（EMA、ATR、RSI 等）
4. 数据缓存和去重

数据流：
通用 K 线服务 → 数据获取 → 指标计算 → 缓存 → 提供给信号检测模块
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional
from utils.technical_indicators import calculate_ema, calculate_atr, calculate_rsi

logger = logging.getLogger(__name__)


class MarketDataFetcher:
    """行情数据获取类"""
    
    def __init__(self):
        """初始化数据获取器"""
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.last_fetch_time: Optional[datetime] = None
        self.cache_duration: timedelta = timedelta(hours=1)  # 缓存 1 小时
    
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
        
        logger.info(f"从通用 K 线服务获取行情数据：{symbols}")
        
        try:
            # 从通用 K 线服务获取数据
            api_data = self._fetch_from_kline_service(symbols)
            
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
    
    def _fetch_from_kline_service(self, symbols: List[str]) -> Dict[str, Any]:
        """
        从通用 K 线服务获取数据
        
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
                    klines = self._get_klines_from_service(symbol, interval, limit=limit)
                    if klines:
                        klines_data[interval] = klines
                
                if klines_data:
                    result[symbol] = {
                        'klines': klines_data,
                        'symbol': symbol
                    }
                else:
                    logger.warning(f"无法获取 {symbol} 的 K 线数据")
            except Exception as e:
                logger.error(f"获取 {symbol} 数据失败：{e}")
        
        return result
    
    def _get_klines_from_service(self, symbol: str, interval: str, limit: int = 100) -> Optional[Dict]:
        """
        从通用 K 线服务获取 K 线数据
        
        Args:
            symbol: 交易对
            interval: 时间间隔
            limit: 获取数量
        
        Returns:
            K 线数据字典
        """
        try:
            import requests
            url = f"http://43.156.242.184:8765/api/v1/klines/latest"
            params = {
                "symbol": symbol,
                "interval": interval,
                "limit": limit
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    return result['data']
                else:
                    logger.error(f"K 线服务返回错误：{result.get('message')}")
                    return None
            else:
                logger.error(f"K 线服务 HTTP 错误：{response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"获取 K 线数据异常：{e}")
            return None
    
    def _process_api_data(self, api_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """
        处理 API 数据并计算技术指标
        
        Args:
            api_data: 原始 API 数据
        
        Returns:
            处理后的数据
        """
        processed = {}
        
        for symbol, data in api_data.items():
            try:
                # 检查是否有 indicators 字段（新的 API 格式）
                if 'indicators' in data:
                    indicators_data = data['indicators']
                    indicators = {}
                    
                    for timeframe in ['1d', '4h', '1h', '15m']:
                        if timeframe in indicators_data:
                            tf_data = indicators_data[timeframe]
                            # 转换为标准格式，只取最后一个值并转换为 Decimal
                            indicators[timeframe] = {
                                'close': Decimal(str(tf_data.get('prices', [])[-1])) if tf_data.get('prices') else None,
                                'close_list': [Decimal(str(p)) for p in tf_data.get('prices', [])],
                                'ema21': Decimal(str(tf_data.get('ema21', [])[-1])) if tf_data.get('ema21') else None,
                                'ema21_list': [Decimal(str(e)) for e in tf_data.get('ema21', [])],
                                'rsi': Decimal(str(tf_data.get('rsi', [])[-1])) if tf_data.get('rsi') else None,
                                'rsi_list': [Decimal(str(r)) for r in tf_data.get('rsi', [])],
                                'atr14': Decimal(str(tf_data.get('atr14', [])[-1])) if tf_data.get('atr14') else None,
                                'atr14_list': [Decimal(str(a)) for a in tf_data.get('atr14', [])],
                            }
                            # 添加布林带数据
                            if 'bollinger' in tf_data:
                                indicators[timeframe]['bollinger'] = tf_data['bollinger']
                else:
                    # 旧的 API 格式，使用 klines（列表格式）
                    klines = data.get('klines', {})
                    indicators = {}
                    
                    for timeframe in ['1d', '4h', '1h', '15m']:
                        if timeframe in klines:
                            # 将 K 线列表转换为字典格式
                            kline_list = klines[timeframe]
                            kline_dict = {
                                'close': [k[4] for k in kline_list],
                                'high': [k[2] for k in kline_list],
                                'low': [k[3] for k in kline_list],
                                'open': [k[1] for k in kline_list],
                                'volume': [k[5] for k in kline_list],
                            }
                            indicators[timeframe] = self._calculate_timeframe_indicators(
                                kline_dict, 
                                timeframe
                            )
                
                # 组装处理后的数据
                processed[symbol] = {
                    'symbol': symbol,
                    'last_price': Decimal(str(data.get('lastPrice', '0'))),
                    'price_change_24h': Decimal(str(data.get('priceChangePercent', '0'))) / Decimal('100'),
                    'funding_rate': Decimal(str(data.get('funding_rate', '0'))),
                    'indicators': indicators,
                    'timestamp': datetime.now(),
                }
                
            except Exception as e:
                logger.error(f"处理 {symbol} 数据失败：{str(e)}")
                continue
        
        return processed
    
    def _calculate_timeframe_indicators(self, kline_data: Dict[str, List], timeframe: str) -> Dict[str, Any]:
        """
        计算单个时间框架的技术指标
        
        Args:
            kline_data: K 线数据
            timeframe: 时间框架
        
        Returns:
            指标字典
        """
        try:
            closes = [Decimal(str(price)) for price in kline_data.get('close', [])]
            highs = [Decimal(str(price)) for price in kline_data.get('high', [])]
            lows = [Decimal(str(price)) for price in kline_data.get('low', [])]
            
            if len(closes) < 21:
                logger.warning(f"{timeframe} K 线数据不足，无法计算指标")
                return {}
            
            # 计算 EMA21
            ema21 = calculate_ema(closes, period=21)
            
            # 计算 ATR14
            atr14 = calculate_atr(highs, lows, closes, period=14)
            
            # 计算 RSI14
            rsi14 = calculate_rsi(closes, period=14)
            
            return {
                'ema21': ema21[-1] if ema21 else None,
                'atr14': atr14[-1] if atr14 else None,
                'rsi14': rsi14[-1] if rsi14 else None,
                'close': closes[-1] if closes else None,
                'high': highs[-1] if highs else None,
                'low': lows[-1] if lows else None,
            }
            
        except Exception as e:
            logger.error(f"计算 {timeframe} 指标失败：{str(e)}")
            return {}
    
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
        logger.info("行情数据缓存已清除")


# 全局实例
_global_fetcher: Optional[MarketDataFetcher] = None


def get_data_fetcher() -> MarketDataFetcher:
    """获取数据获取器实例（单例模式）"""
    global _global_fetcher
    if _global_fetcher is None:
        _global_fetcher = MarketDataFetcher()
    return _global_fetcher
