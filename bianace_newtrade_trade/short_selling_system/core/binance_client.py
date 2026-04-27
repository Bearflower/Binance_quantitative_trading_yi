"""
币安 API 客户端

修改：使用通用 K 线服务获取 K 线数据
"""

import time
import requests
from typing import Optional, Dict, Any, List
from datetime import datetime

from utils.logger import logger

# 通用 K 线服务配置
KLINE_SERVICE_URL = "http://43.156.242.184:8765/api/v1"
KLINE_REGISTER_URL = f"{KLINE_SERVICE_URL}/register"


class BinanceDataClient:
    """币安数据客户端"""
    
    def __init__(self):
        """初始化币安数据客户端"""
        self.futures_base_url = "https://fapi.binance.com"
        self.spot_base_url = "https://api.binance.com"
        
        # 请求配置
        self.timeout = 10
        self.max_retries = 3
        self.retry_delay = 2
        
        # 缓存
        self._symbol_info_cache: Optional[Dict] = None
        self._cache_timestamp: float = 0
        self._cache_ttl = 300  # 5 分钟缓存
        
        logger.info("✅ 币安数据客户端初始化完成")
    
    def _make_request(
        self,
        method: str,
        url: str,
        params: Optional[Dict] = None,
        signed: bool = False
    ) -> Optional[Dict]:
        """
        发送 HTTP 请求
        
        Args:
            method: HTTP 方法 (GET/POST)
            url: 请求 URL
            params: 请求参数
            signed: 是否需要签名 (私有接口)
            
        Returns:
            响应数据字典，失败返回 None
        """
        for retry in range(self.max_retries):
            try:
                if method == "GET":
                    response = requests.get(
                        url,
                        params=params,
                        timeout=self.timeout
                    )
                elif method == "POST":
                    response = requests.post(
                        url,
                        params=params,
                        timeout=self.timeout
                    )
                else:
                    logger.error(f"❌ 不支持的 HTTP 方法：{method}")
                    return None
                
                if response.status_code == 200:
                    return response.json()
                else:
                    logger.warning(
                        f"⚠️ 请求失败：{response.status_code}, "
                        f"URL: {url}, 重试 {retry + 1}/{self.max_retries}"
                    )
                    if retry < self.max_retries - 1:
                        time.sleep(self.retry_delay * (retry + 1))
                    else:
                        return None
                        
            except requests.exceptions.Timeout as e:
                logger.error(f"⏱️ 请求超时：{url}, 错误：{e}")
                if retry < self.max_retries - 1:
                    time.sleep(self.retry_delay * (retry + 1))
                else:
                    return None
            except requests.exceptions.ConnectionError as e:
                logger.error(f"🌐 网络连接错误：{e}")
                if retry < self.max_retries - 1:
                    time.sleep(self.retry_delay * (retry + 1))
                else:
                    return None
            except Exception as e:
                logger.error(f"❌ 请求异常：{e}")
                return None
        
        return None
    
    def get_exchange_info(self) -> Optional[Dict[str, Any]]:
        """
        获取交易所交易对信息
        
        Returns:
            交易对信息字典，包含 symbols 列表
        """
        # 检查缓存
        current_time = time.time()
        if (
            self._symbol_info_cache and 
            (current_time - self._cache_timestamp) < self._cache_ttl
        ):
            logger.debug("📦 使用缓存的交易对信息")
            return self._symbol_info_cache
        
        logger.info("🌐 正在获取币安期货交易对信息...")
        url = f"{self.futures_base_url}/fapi/v1/exchangeInfo"
        
        data = self._make_request("GET", url)
        
        if data:
            self._symbol_info_cache = data
            self._cache_timestamp = current_time
            symbol_count = len(data.get('symbols', []))
            logger.info(f"✅ 获取成功，共 {symbol_count} 个合约")
        else:
            logger.error("❌ 获取交易对信息失败")
        
        return data
    
    def get_open_interest_history(
        self,
        symbol: str,
        period: str = "5m",
        limit: int = 10,
        log_error: bool = True
    ) -> Optional[List[Dict[str, Any]]]:
        """
        获取持仓量历史数据
        
        Args:
            symbol: 交易对符号 (如 BTCUSDT)
            period: 时间周期 (5m/15m/30m 等)
            limit: 返回数量 (最多 500)
            log_error: 是否记录错误日志（默认 True）
            
        Returns:
            持仓量数据列表，每项包含：
            - symbol: 交易对
            - sumOpenInterest: 持仓量
            - sumOpenInterestValue: 持仓量价值
            - timestamp: 时间戳
        """
        logger.debug(f"📊 获取 {symbol} 持仓量历史数据...")
        url = f"{self.futures_base_url}/futures/data/openInterestHist"
        
        params = {
            "symbol": symbol,
            "period": period,
            "limit": limit
        }
        
        data = self._make_request("GET", url, params)
        
        if data:
            logger.debug(f"✅ 获取 {symbol} 持仓量数据成功，共 {len(data)} 条")
            return data
        else:
            if log_error:
                logger.warning(
                    f"⚠️ 获取 {symbol} 持仓量数据失败，可能原因："
                    f"1) 网络问题 2) API 限流 3) 该币种数据不存在（新币常见）"
                )
            return None
    
    def get_current_open_interest(self, symbol: str) -> Optional[float]:
        """
        获取当前持仓量 (USDT)
        
        Args:
            symbol: 交易对符号
            
        Returns:
            当前持仓量 (USDT), 失败返回 None
        """
        # 获取最近一条持仓量数据
        data = self.get_open_interest_history(symbol, period="5m", limit=1)
        
        if data and len(data) > 0:
            open_interest_value = float(data[0].get('sumOpenInterestValue', 0))
            logger.debug(f"📊 {symbol} 当前持仓量：{open_interest_value:.2f} USDT")
            return open_interest_value
        
        return None
    
    def get_funding_rate(self, symbol: str, max_retries: int = 3) -> Optional[float]:
        """
        获取当前资金费率（带重试机制）
        
        Args:
            symbol: 交易对符号
            max_retries: 最大重试次数（默认 3 次）
            
        Returns:
            当前资金费率 (小数形式，如 0.0001), 失败返回 None
        """
        logger.debug(f"💹 获取 {symbol} 资金费率...")
        url = f"{self.futures_base_url}/fapi/v1/premiumIndex"
        
        params = {"symbol": symbol}
        
        # 重试机制
        for attempt in range(1, max_retries + 1):
            data = self._make_request("GET", url, params)
            
            if data:
                funding_rate = float(data.get('lastFundingRate', 0))
                logger.debug(f"✅ {symbol} 资金费率：{funding_rate:.6f} (尝试{attempt}/{max_retries})")
                return funding_rate
            else:
                if attempt < max_retries:
                    logger.warning(f"⚠️  {symbol} 获取资金费率失败，{attempt}秒后重试 ({attempt}/{max_retries})")
                    time.sleep(attempt)  # 递增延迟
                else:
                    logger.error(f"❌ {symbol} 获取资金费率失败，已重试{max_retries}次")
        
        return None
    
    def get_annualized_funding_rate(self, symbol: str) -> Optional[float]:
        """
        获取年化资金费率
        
        Args:
            symbol: 交易对符号
            
        Returns:
            年化资金费率 (小数形式，如 1.5 表示 150%), 失败返回 None
        """
        funding_rate = self.get_funding_rate(symbol)
        
        if funding_rate is not None:
            # 资金费率每 8 小时结算一次，一年 365 天共 1095 次
            annual_rate = funding_rate * 3 * 365
            logger.debug(
                f"📈 {symbol} 年化资金费率：{annual_rate:.2%} "
                f"(8 小时费率：{funding_rate:.4%})"
            )
            return annual_rate
        
        return None
    
    def get_kline_data(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 100
    ) -> Optional[List[Dict[str, Any]]]:
        """
        获取 K 线数据（使用通用 K 线服务）
        
        Args:
            symbol: 交易对符号
            interval: K 线周期 (1m/5m/15m/1h/4h 等)
            limit: 返回数量 (最多 100)
            
        Returns:
            K 线数据列表，每项包含：
            - open_time: 开盘时间
            - open: 开盘价
            - high: 最高价
            - low: 最低价
            - close: 收盘价
            - volume: 成交量
            - close_time: 收盘时间
        """
        logger.debug(f"📈 从通用服务获取 {symbol} K 线数据 (周期：{interval})...")
        
        # 使用通用 K 线服务 API
        url = f"{KLINE_SERVICE_URL}/klines/latest"
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": min(limit, 100)  # K 线服务限制每次最多 100 条
        }
        
        try:
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    klines = result.get('data', [])
                    logger.debug(f"✅ 从通用服务获取 {symbol} K 线数据成功，共 {len(klines)} 条")
                    return klines
                else:
                    logger.warning(f"⚠️ 通用服务返回失败：{result.get('message')}")
                    # 降级到直接调用币安 API
                    return self._get_klines_from_binance(symbol, interval, limit)
            else:
                logger.error(f"❌ 通用服务 HTTP 错误：{response.status_code}")
                # 降级到直接调用币安 API
                return self._get_klines_from_binance(symbol, interval, limit)
                
        except Exception as e:
            logger.error(f"❌ 从通用服务获取 K 线数据失败：{e}")
            # 降级到直接调用币安 API
            return self._get_klines_from_binance(symbol, interval, limit)
    
    def _get_klines_from_binance(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        直接从币安 API 获取 K 线数据（降级方案）
        
        Args:
            symbol: 交易对符号
            interval: 时间间隔
            limit: 返回数量
            
        Returns:
            K 线数据列表
        """
        logger.debug(f"📈 从币安 API 获取 {symbol} K 线数据 (周期：{interval})...")
        url = f"{self.futures_base_url}/fapi/v1/klines"
        
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        
        data = self._make_request("GET", url, params)
        
        if data:
            # 转换数据格式
            klines = []
            for k in data:
                kline = {
                    "open_time": k[0],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": k[6],
                    "quote_volume": float(k[7]),
                }
                klines.append(kline)
            
            logger.debug(f"✅ 从币安 API 获取 {symbol} K 线数据成功，共 {len(klines)} 条")
            return klines
        else:
            logger.error(f"❌ 从币安 API 获取 {symbol} K 线数据失败")
            return None
    
    def register_new_symbol(
        self,
        symbol: str,
        intervals: List[str],
        duration_days: int = 10,
        priority: str = "normal"
    ) -> bool:
        """
        注册新币到 K 线服务，开始自动采集
        
        Args:
            symbol: 交易对符号，如 "NEWCOINUSDT"
            intervals: 需要采集的周期列表，如 ["1m", "5m", "15m", "1h"]
            duration_days: 采集持续天数（1-30 天）
            priority: 优先级（high, normal, low）
            
        Returns:
            是否注册成功
        """
        logger.info(f"📝 注册新币 {symbol} 到 K 线服务...")
        
        try:
            data = {
                "symbol": symbol,
                "intervals": intervals,
                "duration_days": duration_days,
                "priority": priority
            }
            
            response = requests.post(
                KLINE_REGISTER_URL,
                json=data,
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    expires_at = result.get('data', {}).get('expires_at', 'unknown')
                    logger.info(f"✅ 新币 {symbol} 注册成功，过期时间：{expires_at}")
                    return True
                else:
                    logger.error(f"❌ 注册失败：{result.get('message')}")
                    return False
            else:
                logger.error(f"❌ 注册服务 HTTP 错误：{response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"❌ 注册新币失败：{e}")
            return False
    
    def unregister_symbol(self, symbol: str) -> bool:
        """
        取消标的注册，停止采集
        
        Args:
            symbol: 交易对符号
            
        Returns:
            是否取消成功
        """
        logger.info(f"📝 取消注册 {symbol}...")
        
        try:
            response = requests.delete(
                f"{KLINE_REGISTER_URL}?symbol={symbol}",
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    logger.info(f"✅ {symbol} 已取消注册")
                    return True
                else:
                    logger.error(f"❌ 取消失败：{result.get('message')}")
                    return False
            else:
                logger.error(f"❌ 取消服务 HTTP 错误：{response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"❌ 取消注册失败：{e}")
            return False
    
    def renew_symbol(self, symbol: str, additional_days: int) -> bool:
        """
        续期已注册的标的
        
        Args:
            symbol: 交易对符号
            additional_days: 续期天数
            
        Returns:
            是否续期成功
        """
        logger.info(f"📝 续期 {symbol}，增加 {additional_days} 天...")
        
        try:
            data = {
                "symbol": symbol,
                "additional_days": additional_days
            }
            
            response = requests.put(
                f"{KLINE_REGISTER_URL}/renew",
                json=data,
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('code') == 0:
                    expires_at = result.get('data', {}).get('expires_at', 'unknown')
                    logger.info(f"✅ {symbol} 续期成功，新过期时间：{expires_at}")
                    return True
                else:
                    logger.error(f"❌ 续期失败：{result.get('message')}")
                    return False
            else:
                logger.error(f"❌ 续期服务 HTTP 错误：{response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"❌ 续期失败：{e}")
            return False
        """
        获取特定交易对信息
        
        Args:
            symbol: 交易对符号
            
        Returns:
            交易对信息字典，包含上市时间等
        """
        exchange_info = self.get_exchange_info()
        
        if not exchange_info:
            return None
        
        symbols = exchange_info.get('symbols', [])
        for symbol_info in symbols:
            if symbol_info.get('symbol') == symbol:
                return symbol_info
        
        logger.warning(f"⚠️ 未找到交易对：{symbol}")
        return None
    
    def get_listing_time(self, symbol: str) -> Optional[datetime]:
        """
        获取新币上线时间
        
        Args:
            symbol: 交易对符号
            
        Returns:
            上线时间，失败返回 None
        """
        symbol_info = self.get_symbol_info(symbol)
        
        if symbol_info:
            # 币安使用 onboardDate 字段
            listing_time = symbol_info.get('onboardDate', 0) or symbol_info.get('listingTime', 0)
            if listing_time > 0:
                # 转换为毫秒级时间戳
                listing_datetime = datetime.fromtimestamp(listing_time / 1000)
                logger.debug(f"📅 {symbol} 上线时间：{listing_datetime}")
                return listing_datetime
        
        return None
    
    def get_all_trading_symbols(self) -> List[str]:
        """
        获取所有可交易的币种符号
        
        Returns:
            币种符号列表
        """
        exchange_info = self.get_exchange_info()
        
        if not exchange_info:
            return []
        
        symbols = []
        for symbol_info in exchange_info.get('symbols', []):
            if (
                symbol_info.get('status') == 'TRADING' and
                symbol_info.get('contractType') == 'PERPETUAL'
            ):
                symbols.append(symbol_info.get('symbol'))
        
        logger.debug(f"📋 获取到 {len(symbols)} 个永续合约")
        return symbols


# 全局客户端实例
binance_client = BinanceDataClient()
