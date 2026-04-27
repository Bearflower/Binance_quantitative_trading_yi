#!/usr/bin/env python3
"""
通用 K 线数据服务调用模块

封装对通用 K 线服务的调用，提供统一的接口
"""

import os
import requests
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime

# 配置日志
logger = logging.getLogger(__name__)

# 通用 K 线服务配置
KLINE_SERVICE_URL = os.getenv('KLINE_SERVICE_URL', 'http://43.156.242.184:8765/api/v1')


class KlineServiceClient:
    """通用 K 线服务客户端"""
    
    def __init__(self, service_url: Optional[str] = None):
        """
        初始化 K 线服务客户端
        
        Args:
            service_url: K 线服务地址，默认使用环境变量配置
        """
        self.service_url = service_url or KLINE_SERVICE_URL
        logger.info(f"✅ K 线服务客户端初始化完成：{self.service_url}")
    
    def get_latest_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100
    ) -> Optional[List[Dict[str, Any]]]:
        """
        获取最新 K 线数据
        
        Args:
            symbol: 交易对，如 BTCUSDT
            interval: 时间间隔，如 1h, 4h, 1d, 15m
            limit: 获取数量，默认 100
            
        Returns:
            K 线数据列表，失败返回 None
        """
        try:
            url = f"{self.service_url}/klines/latest"
            params = {
                "symbol": symbol,
                "interval": interval,
                "limit": limit
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    klines = result.get('data', [])
                    logger.info(f"✅ 获取 {symbol} {interval} K 线数据 {len(klines)} 条")
                    return klines
                else:
                    logger.error(f"❌ K 线服务返回错误：{result.get('message')}")
                    return None
            else:
                logger.error(f"❌ K 线服务 HTTP 错误：{response.status_code}")
                return None
                
        except requests.exceptions.Timeout:
            logger.error(f"❌ K 线服务请求超时")
            return None
        except Exception as e:
            logger.error(f"❌ 获取 K 线数据异常：{e}")
            return None
    
    def get_indicators(
        self,
        symbol: str,
        interval: str,
        period: int = 100
    ) -> Optional[Dict[str, Any]]:
        """
        获取技术指标
        
        Args:
            symbol: 交易对
            interval: 时间间隔
            period: 计算周期，默认 100
            
        Returns:
            技术指标数据，失败返回 None
        """
        try:
            url = f"{self.service_url}/indicators"
            params = {
                "symbol": symbol,
                "interval": interval,
                "period": period
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    indicators = result.get('data')
                    logger.info(f"✅ 获取 {symbol} {interval} 技术指标成功")
                    return indicators
                else:
                    logger.error(f"❌ 技术指标服务返回错误：{result.get('message')}")
                    return None
            else:
                logger.error(f"❌ 技术指标服务 HTTP 错误：{response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"❌ 获取技术指标异常：{e}")
            return None
    
    def get_symbols(self) -> Optional[Dict[str, Any]]:
        """
        获取支持的币种列表
        
        Returns:
            币种列表，包含 symbols 和 intervals
        """
        try:
            url = f"{self.service_url}/symbols"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    return result.get('data')
                else:
                    logger.error(f"❌ 获取币种列表失败：{result.get('message')}")
                    return None
            else:
                logger.error(f"❌ 获取币种列表 HTTP 错误：{response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"❌ 获取币种列表异常：{e}")
            return None
    
    def manual_collect(
        self,
        symbol: str,
        interval: str,
        minutes: int = 5
    ) -> Optional[Dict[str, Any]]:
        """
        手动触发 K 线采集
        
        Args:
            symbol: 交易对
            interval: 时间间隔
            minutes: 采集最近 N 分钟，默认 5
            
        Returns:
            采集结果
        """
        try:
            url = f"{self.service_url}/collect/manual"
            params = {
                "symbol": symbol,
                "interval": interval,
                "minutes": minutes
            }
            
            response = requests.post(url, params=params, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    logger.info(f"✅ 手动采集 {symbol} {interval} 成功")
                    return result.get('data')
                else:
                    logger.error(f"❌ 手动采集失败：{result.get('message')}")
                    return None
            else:
                logger.error(f"❌ 手动采集 HTTP 错误：{response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"❌ 手动采集异常：{e}")
            return None
    
    def get_collector_stats(self) -> Optional[Dict[str, Any]]:
        """
        获取采集器统计信息
        
        Returns:
            统计信息
        """
        try:
            url = f"{self.service_url}/collector/stats"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    return result.get('data')
                else:
                    logger.error(f"❌ 获取统计信息失败：{result.get('message')}")
                    return None
            else:
                logger.error(f"❌ 获取统计信息 HTTP 错误：{response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"❌ 获取统计信息异常：{e}")
            return None


# 便捷函数
def get_klines(symbol: str, interval: str, limit: int = 100) -> Optional[List[Dict[str, Any]]]:
    """
    获取 K 线数据的便捷函数
    
    Args:
        symbol: 交易对
        interval: 时间间隔
        limit: 获取数量
        
    Returns:
        K 线数据列表
    """
    client = KlineServiceClient()
    return client.get_latest_klines(symbol, interval, limit)


def get_indicators(symbol: str, interval: str, period: int = 100) -> Optional[Dict[str, Any]]:
    """
    获取技术指标的便捷函数
    
    Args:
        symbol: 交易对
        interval: 时间间隔
        period: 计算周期
        
    Returns:
        技术指标数据
    """
    client = KlineServiceClient()
    return client.get_indicators(symbol, interval, period)
