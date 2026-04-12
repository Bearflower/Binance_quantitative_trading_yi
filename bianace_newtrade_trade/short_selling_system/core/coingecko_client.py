"""
CoinGecko API 数据采集客户端

负责获取：
- 流通市值 (Circulating Market Cap)
- 代币信息
- 价格数据
"""

import time
import requests
from typing import Optional, Dict, Any, List
from functools import lru_cache

from utils.logger import logger


class CoinGeckoClient:
    """CoinGecko 数据客户端"""
    
    def __init__(self):
        """初始化 CoinGecko 客户端"""
        self.base_url = "https://api.coingecko.com/api/v3"
        
        # 请求配置
        self.timeout = 10
        self.max_retries = 3
        self.retry_delay = 2
        
        # API 限流控制 (免费版：10-50 次/分钟)
        self.rate_limit_delay = 2  # 2 秒一次请求，确保安全
        
        # 缓存
        self._cache: Dict[str, Dict] = {}
        self._cache_timestamp: Dict[str, float] = {}
        self._cache_ttl = 600  # 10 分钟缓存
        
        # 币种映射 (币安符号 -> CoinGecko ID)
        # 需要在实际使用中不断完善
        self.symbol_to_id_map = {
            'BTCUSDT': 'bitcoin',
            'ETHUSDT': 'ethereum',
            'BNBUSDT': 'binancecoin',
            # 新币需要手动添加或使用搜索 API
        }
        
        logger.info("✅ CoinGecko 数据客户端初始化完成")
    
    def _make_request(
        self,
        url: str,
        params: Optional[Dict] = None
    ) -> Optional[Dict]:
        """
        发送 HTTP 请求
        
        Args:
            url: 请求 URL
            params: 请求参数
            
        Returns:
            响应数据字典，失败返回 None
        """
        for retry in range(self.max_retries):
            try:
                # 遵守 API 限流
                time.sleep(self.rate_limit_delay)
                
                response = requests.get(
                    url,
                    params=params,
                    timeout=self.timeout
                )
                
                if response.status_code == 200:
                    return response.json()
                elif response.status_code == 429:
                    # API 限流
                    logger.warning(f"⚠️ CoinGecko API 限流，等待 {self.retry_delay * (retry + 1)} 秒")
                    time.sleep(self.retry_delay * (retry + 1))
                else:
                    logger.warning(
                        f"⚠️ 请求失败：{response.status_code}, "
                        f"URL: {url}, 重试 {retry + 1}/{self.max_retries}"
                    )
                    if retry < self.max_retries - 1:
                        time.sleep(self.retry_delay)
                    else:
                        return None
                        
            except requests.exceptions.Timeout as e:
                logger.error(f"⏱️ 请求超时：{url}, 错误：{e}")
                if retry < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                else:
                    return None
            except requests.exceptions.ConnectionError as e:
                logger.error(f"🌐 网络连接错误：{e}")
                if retry < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                else:
                    return None
            except Exception as e:
                logger.error(f"❌ 请求异常：{e}")
                return None
        
        return None
    
    def search_token(self, query: str) -> Optional[List[Dict[str, Any]]]:
        """
        搜索代币
        
        Args:
            query: 搜索关键词 (如币种名称)
            
        Returns:
            搜索结果列表，每项包含：
            - id: CoinGecko ID
            - symbol: 币种符号
            - name: 币种名称
        """
        logger.debug(f"🔍 搜索代币：{query}")
        url = f"{self.base_url}/search"
        
        params = {"query": query}
        data = self._make_request(url, params)
        
        if data and 'coins' in data:
            results = data['coins']
            logger.debug(f"✅ 搜索到 {len(results)} 个结果")
            return results
        else:
            logger.warning(f"⚠️ 未搜索到代币：{query}")
            return []
    
    def get_token_info(self, token_id: str) -> Optional[Dict[str, Any]]:
        """
        获取代币详细信息
        
        Args:
            token_id: CoinGecko 代币 ID (如 'bitcoin')
            
        Returns:
            代币信息字典，包含：
            - market_cap_rank: 市值排名
            - market_data: 市场数据
            - ...
        """
        # 检查缓存
        cache_key = f"token_info_{token_id}"
        current_time = time.time()
        
        if (
            cache_key in self._cache and
            (current_time - self._cache_timestamp[cache_key]) < self._cache_ttl
        ):
            logger.debug(f"📦 使用缓存的代币信息：{token_id}")
            return self._cache[cache_key]
        
        logger.debug(f"📊 获取代币信息：{token_id}")
        url = f"{self.base_url}/coins/{token_id}"
        
        params = {
            "localization": False,
            "tickers": False,
            "community_data": False,
            "developer_data": False
        }
        
        data = self._make_request(url, params)
        
        if data:
            # 缓存结果
            self._cache[cache_key] = data
            self._cache_timestamp[cache_key] = current_time
            logger.debug(f"✅ 获取代币信息成功：{token_id}")
            return data
        else:
            logger.error(f"❌ 获取代币信息失败：{token_id}")
            return None
    
    def get_circulating_market_cap(self, token_id: str) -> Optional[float]:
        """
        获取流通市值 (USDT)
        
        Args:
            token_id: CoinGecko 代币 ID
            
        Returns:
            流通市值 (USDT), 失败返回 None
        """
        token_info = self.get_token_info(token_id)
        
        if not token_info:
            return None
        
        market_data = token_info.get('market_data', {})
        circulating_market_cap = market_data.get('market_cap', {}).get('usd')
        
        if circulating_market_cap:
            logger.debug(
                f"💰 {token_id} 流通市值：${circulating_market_cap:,.2f}"
            )
            return float(circulating_market_cap)
        else:
            logger.warning(f"⚠️ 未找到 {token_id} 的流通市值数据")
            return None
    
    def get_circulating_supply(self, token_id: str) -> Optional[float]:
        """
        获取流通供应量
        
        Args:
            token_id: CoinGecko 代币 ID
            
        Returns:
            流通供应量，失败返回 None
        """
        token_info = self.get_token_info(token_id)
        
        if not token_info:
            return None
        
        market_data = token_info.get('market_data', {})
        circulating_supply = market_data.get('circulating_supply')
        
        if circulating_supply:
            logger.debug(
                f"📊 {token_id} 流通供应量：{circulating_supply:,.2f}"
            )
            return float(circulating_supply)
        else:
            logger.warning(f"⚠️ 未找到 {token_id} 的流通供应量数据")
            return None
    
    def get_current_price(self, token_id: str) -> Optional[float]:
        """
        获取当前价格 (USDT)
        
        Args:
            token_id: CoinGecko 代币 ID
            
        Returns:
            当前价格 (USDT), 失败返回 None
        """
        token_info = self.get_token_info(token_id)
        
        if not token_info:
            return None
        
        market_data = token_info.get('market_data', {})
        current_price = market_data.get('current_price', {}).get('usd')
        
        if current_price:
            logger.debug(f"💵 {token_id} 当前价格：${current_price:,.2f}")
            return float(current_price)
        else:
            logger.warning(f"⚠️ 未找到 {token_id} 的当前价格数据")
            return None
    
    def get_market_data(self, token_id: str) -> Optional[Dict[str, Any]]:
        """
        获取完整市场数据
        
        Args:
            token_id: CoinGecko 代币 ID
            
        Returns:
            市场数据字典，包含：
            - current_price: 当前价格
            - market_cap: 流通市值
            - circulating_supply: 流通供应量
            - total_supply: 总供应量
            - max_supply: 最大供应量
        """
        token_info = self.get_token_info(token_id)
        
        if not token_info:
            return None
        
        market_data = token_info.get('market_data', {})
        
        result = {
            'current_price': market_data.get('current_price', {}).get('usd'),
            'market_cap': market_data.get('market_cap', {}).get('usd'),
            'circulating_supply': market_data.get('circulating_supply'),
            'total_supply': market_data.get('total_supply'),
            'max_supply': market_data.get('max_supply'),
            'fully_diluted_valuation': market_data.get(
                'fully_diluted_valuation', {}
            ).get('usd'),
        }
        
        return result
    
    def symbol_to_coingecko_id(self, symbol: str) -> Optional[str]:
        """
        将币安符号转换为 CoinGecko ID
        
        Args:
            symbol: 币安交易对符号 (如 BTCUSDT)
            
        Returns:
            CoinGecko ID, 未找到返回 None
        """
        # 检查映射表
        if symbol in self.symbol_to_id_map:
            return self.symbol_to_id_map[symbol]
        
        # 尝试搜索
        # 移除 USDT 后缀
        base_symbol = symbol.replace('USDT', '').lower()
        results = self.search_token(base_symbol)
        
        if results and len(results) > 0:
            # 取第一个结果 (通常是最匹配的)
            token_id = results[0]['id']
            # 更新映射表
            self.symbol_to_id_map[symbol] = token_id
            logger.debug(f"🔍 找到映射：{symbol} -> {token_id}")
            return token_id
        
        logger.warning(f"⚠️ 未找到 {symbol} 对应的 CoinGecko ID")
        return None
    
    def get_market_cap_by_symbol(self, symbol: str) -> Optional[float]:
        """
        根据币安符号获取流通市值
        
        Args:
            symbol: 币安交易对符号
            
        Returns:
            流通市值 (USDT), 失败返回 None
        """
        token_id = self.symbol_to_coingecko_id(symbol)
        
        if not token_id:
            return None
        
        return self.get_circulating_market_cap(token_id)


# 全局客户端实例
coingecko_client = CoinGeckoClient()
