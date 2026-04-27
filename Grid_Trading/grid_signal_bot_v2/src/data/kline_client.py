"""
K 线数据服务客户端
对接通用 K 线服务 REST API
"""

import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)


class KlineServiceClient:
    """K 线数据服务客户端"""
    
    def __init__(self, base_url: str, timeout: int = 10):
        """
        初始化 K 线服务客户端
        
        Args:
            base_url: K 线服务基础 URL
            timeout: 请求超时时间（秒）
        """
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.session = requests.Session()
        
        logger.info(f"K 线服务客户端初始化完成：{self.base_url}")
    
    def get_latest_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取最新 K 线数据
        
        Args:
            symbol: 交易对，如 BTCUSDT
            interval: 时间间隔，如 1h, 4h
            limit: 获取数量，默认 100
            
        Returns:
            K 线数据列表
            
        Raises:
            RequestException: 请求失败
        """
        try:
            url = f"{self.base_url}/api/v1/klines/latest"
            params = {
                "symbol": symbol,
                "interval": interval,
                "limit": limit
            }
            
            response = self.session.get(
                url,
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()
            
            result = response.json()
            
            if result.get("code") == 0:
                klines = result.get("data", [])
                logger.info(
                    f"获取 K 线数据成功：{symbol} {interval}, "
                    f"数量={len(klines)}"
                )
                return klines
            else:
                logger.error(f"获取 K 线数据失败：{result.get('message')}")
                return []
                
        except RequestException as e:
            logger.error(f"请求 K 线服务失败：{e}")
            raise
    
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
            period: 计算周期
            
        Returns:
            技术指标数据
            
        Raises:
            RequestException: 请求失败
        """
        try:
            url = f"{self.base_url}/api/v1/indicators"
            params = {
                "symbol": symbol,
                "interval": interval,
                "period": period
            }
            
            response = self.session.get(
                url,
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()
            
            result = response.json()
            
            if result.get("code") == 0:
                indicators = result.get("data")
                if indicators:
                    adx_value = indicators.get('adx', 'N/A')
                    adx_str = f"{adx_value:.2f}" if isinstance(adx_value, (int, float)) else str(adx_value)
                    logger.info(
                        f"获取技术指标成功：{symbol} {interval}, "
                        f"ADX={adx_str}"
                    )
                return indicators
            else:
                logger.error(f"获取技术指标失败：{result.get('message')}")
                return None
                
        except RequestException as e:
            logger.error(f"请求技术指标失败：{e}")
            raise
    
    def get_klines_with_indicators(
        self,
        symbol: str,
        interval: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取带技术指标的 K 线数据
        
        Args:
            symbol: 交易对
            interval: 时间间隔
            limit: 获取数量
            
        Returns:
            K 线数据列表（含技术指标）
        """
        try:
            # 获取 K 线数据
            klines = self.get_latest_klines(symbol, interval, limit)
            
            if not klines:
                return []
            
            # 获取技术指标
            indicators = self.get_indicators(symbol, interval, limit)
            
            # 如果技术指标获取失败，返回原始 K 线数据
            if not indicators:
                return klines
            
            # 将技术指标添加到最后一条 K 线
            if klines:
                last_kline = klines[-1]
                last_kline['adx'] = indicators.get('adx')
                last_kline['ema_fast'] = indicators.get('ema_fast')
                last_kline['ema_slow'] = indicators.get('ema_slow')
                last_kline['atr'] = indicators.get('atr')
                last_kline['rsi'] = indicators.get('rsi')
            
            return klines
            
        except Exception as e:
            logger.error(f"获取带指标的 K 线数据失败：{e}")
            return []
    
    def health_check(self) -> bool:
        """
        健康检查
        
        Returns:
            服务是否健康
        """
        try:
            url = f"{self.base_url}/api/v1/health"
            response = self.session.get(url, timeout=5)
            response.raise_for_status()
            
            result = response.json()
            is_healthy = result.get("code") == 0
            
            if is_healthy:
                logger.info("K 线服务健康检查通过")
            else:
                logger.warning("K 线服务健康检查失败")
            
            return is_healthy
            
        except Exception as e:
            logger.error(f"K 线服务健康检查失败：{e}")
            return False
    
    def close(self):
        """关闭会话"""
        self.session.close()
        logger.info("K 线服务客户端会话已关闭")


# 使用示例
if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)-8s | %(message)s'
    )
    
    # 初始化客户端
    kline_url = os.getenv("KLINE_SERVICE_URL", "http://localhost:8000")
    client = KlineServiceClient(kline_url)
    
    # 健康检查
    if client.health_check():
        print("✅ K 线服务健康")
    
    # 获取 K 线数据
    klines = client.get_latest_klines("BTCUSDT", "1h", limit=10)
    print(f"\n📊 获取到 {len(klines)} 条 K 线数据")
    
    # 获取技术指标
    indicators = client.get_indicators("BTCUSDT", "1h")
    if indicators:
        print(f"\n📈 技术指标：")
        print(f"  ADX: {indicators.get('adx', 'N/A')}")
        print(f"  EMA Fast: {indicators.get('ema_fast', 'N/A')}")
        print(f"  EMA Slow: {indicators.get('ema_slow', 'N/A')}")
        print(f"  ATR: {indicators.get('atr', 'N/A')}")
    
    # 关闭客户端
    client.close()
