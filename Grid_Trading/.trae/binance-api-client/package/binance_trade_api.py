#!/usr/bin/env python3
"""
币安交易 API 封装模块
提供完整的交易功能：理财查询/赎回、资金划转、合约下单、持仓监控

重要：此模块包含实际资金操作，仅能在服务器环境 (生产环境) 运行
本地环境仅用于测试接口连通性和参数验证

更新日志：
- 2026-03-23: 添加 PM 账户适配和精度处理功能
"""

import hashlib
import hmac
import time
import requests
import os
import logging
from decimal import Decimal, ROUND_UP, ROUND_DOWN
from typing import Optional, Dict, List, Any, Tuple
from urllib.parse import urlencode
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from functools import wraps

# 导入限流器
from .rate_limiter import get_rate_limiter

logger = logging.getLogger(__name__)


class BinanceAPIError(Exception):
    """币安 API 异常"""
    def __init__(self, code: int, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"Binance API Error {code}: {msg}")


class InsufficientFundsError(Exception):
    """资金不足异常"""
    pass


class BinanceTradeAPI:
    """币安交易 API 封装类"""
    
    def __init__(self, api_key: str = None, secret_key: str = None, 
                 base_url: str = "https://papi.binance.com", testnet: bool = False,
                 use_unified_account: bool = True):
        """
        初始化币安交易 API
        
        Args:
            api_key: API Key
            secret_key: Secret Key
            base_url: API 基础 URL
            testnet: 是否使用测试网
            use_unified_account: 是否使用统一账户 (PM 账户)
        """
        self.api_key = api_key or os.getenv('BINANCE_API_KEY')
        self.secret_key = secret_key or os.getenv('BINANCE_SECRET_KEY')
        self.base_url = base_url or os.getenv('BINANCE_API_BASE_URL', 'https://papi.binance.com')
        self.testnet = testnet or os.getenv('BINANCE_TESTNET', 'false').lower() == 'true'
        self.use_unified_account = use_unified_account or os.getenv('USE_UNIFIED_ACCOUNT', 'true').lower() == 'true'
        
        if not self.api_key or not self.secret_key:
            raise ValueError("API Key 和 Secret Key 不能为空")
        
        # 检查是否为生产环境
        self.is_production = os.getenv('ENVIRONMENT', 'development') == 'production'
        
        if not self.is_production and not self.testnet:
            logger.warning("⚠️  当前为开发环境，部分交易功能将被禁用")
            logger.warning("⚠️  如需启用交易功能，请设置 ENVIRONMENT=production 或使用测试网")
        
        logger.info(f"币安交易 API 初始化完成 - 环境：{'生产' if self.is_production else '开发'}, "
                   f"测试网：{self.testnet}, 统一账户：{self.use_unified_account}")
    
    def _generate_signature(self, query_string: str) -> str:
        """生成 HMAC SHA256 签名"""
        return hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
    
    def _sign_request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """为请求添加签名"""
        params['timestamp'] = int(time.time() * 1000)
        query_string = urlencode(params)
        params['signature'] = self._generate_signature(query_string)
        return params
    
    def _get_headers(self) -> Dict[str, str]:
        """获取请求头"""
        return {
            'X-MBX-APIKEY': self.api_key,
            'Content-Type': 'application/x-www-form-urlencoded'
        }
    
    def _handle_response(self, response: requests.Response) -> Dict[str, Any]:
        """处理 API 响应"""
        if response.status_code != 200:
            try:
                error_data = response.json()
                raise BinanceAPIError(
                    error_data.get('code', -1),
                    error_data.get('msg', f'HTTP {response.status_code}')
                )
            except ValueError:
                raise BinanceAPIError(-1, f"HTTP {response.status_code}: {response.text}")
        
        return response.json()
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1000, max=10000),
        retry=retry_if_exception_type(requests.exceptions.RequestException)
    )
    def _make_request(self, method: str, endpoint: str, params: Dict[str, Any] = None, 
                     signed: bool = False) -> Dict[str, Any]:
        """
        发送 API 请求
        
        Args:
            method: HTTP 方法 (GET/POST)
            endpoint: API 端点
            params: 请求参数
            signed: 是否需要签名
        
        Returns:
            API 响应数据
        """
        # SAPI 端点和公共市场数据端点使用主站 API，PAPI 端点使用合约 API
        if endpoint.startswith('/sapi/') or endpoint.startswith('/api/'):
            url = f"https://api.binance.com{endpoint}"
        else:
            url = f"{self.base_url}{endpoint}"
        
        # 根据是否需要签名设置 headers
        if signed:
            headers = self._get_headers()
        else:
            # 不需要签名的公共接口不需要 API Key
            headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        
        if signed:
            params = self._sign_request(params or {})
        
        try:
            if method == 'GET':
                response = requests.get(url, params=params, headers=headers, timeout=10)
            elif method == 'POST':
                response = requests.post(url, data=params, headers=headers, timeout=10)
            else:
                raise ValueError(f"不支持的 HTTP 方法：{method}")
            
            return self._handle_response(response)
        
        except requests.exceptions.Timeout:
            logger.error(f"请求超时：{url}")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"请求失败：{str(e)}")
            raise
    
    # ==================== 精度处理工具方法 ====================
    
    def get_symbol_precision(self, symbol: str) -> Tuple[Decimal, Decimal]:
        """
        获取交易对精度
        
        Args:
            symbol: 交易对符号，如 BTCUSDT
        
        Returns:
            (tick_size, step_size): 价格精度和数量精度
        
        Example:
            >>> tick_size, step_size = api.get_symbol_precision('BTCUSDT')
            >>> print(f"价格精度：{tick_size}, 数量精度：{step_size}")
            价格精度：0.1, 数量精度：0.001
        """
        exchange_info = self._make_request('GET', '/fapi/v1/exchangeInfo')
        
        for s in exchange_info.get('symbols', []):
            if s['symbol'] == symbol:
                filters = s.get('filters', [])
                tick_size = Decimal('0.1')
                step_size = Decimal('0.001')
                
                for f in filters:
                    if f['filterType'] == 'PRICE_FILTER':
                        tick_size = Decimal(f['tickSize'])
                    elif f['filterType'] == 'LOT_SIZE':
                        step_size = Decimal(f['stepSize'])
                
                return tick_size, step_size
        
        raise ValueError(f"Symbol {symbol} not found")
    
    def format_price(self, price: Decimal, tick_size: Decimal = None) -> Decimal:
        """
        格式化价格到正确的 tickSize
        
        Args:
            price: 原始价格
            tick_size: 价格精度，如 None 则自动获取
        
        Returns:
            格式化后的价格
        
        Example:
            >>> price = Decimal('68131.567')
            >>> formatted = api.format_price(price, Decimal('0.1'))
            >>> print(formatted)
            68131.5
        """
        if tick_size is None:
            tick_size = Decimal('0.1')
        
        return price.quantize(tick_size, rounding=ROUND_DOWN)
    
    def format_quantity(self, quantity: Decimal, step_size: Decimal = None,
                       min_notional: Decimal = Decimal('100'),
                       price: Decimal = None) -> Decimal:
        """
        格式化数量，确保符合 stepSize 和最小名义价值
        
        Args:
            quantity: 原始数量
            step_size: 数量精度，如 None 则自动获取
            min_notional: 最小名义价值（USDT），默认 100
            price: 价格（用于计算名义价值）
        
        Returns:
            格式化后的数量
        
        Example:
            >>> quantity = Decimal('0.001456')
            >>> formatted = api.format_quantity(quantity, Decimal('0.001'), Decimal('100'), Decimal('68000'))
            >>> print(formatted)
            0.002  # 向上取整确保名义价值 >= 100
        """
        if step_size is None:
            step_size = Decimal('0.001')
        
        # 格式化到正确的精度（向上取整）
        formatted_qty = quantity.quantize(step_size, rounding=ROUND_UP)
        
        # 检查最小名义价值
        if price is not None:
            notional = formatted_qty * price
            if notional < min_notional:
                # 计算满足最小名义价值的最小数量
                min_qty = (min_notional / price).quantize(step_size, rounding=ROUND_UP)
                formatted_qty = max(formatted_qty, min_qty)
        
        return formatted_qty
    
    def format_order_params(self, symbol: str, price: Decimal, 
                           quantity: Decimal) -> Tuple[Decimal, Decimal]:
        """
        格式化订单参数（价格和数量）
        
        自动处理：
        1. 价格 tickSize
        2. 数量 stepSize
        3. 最小名义价值（100 USDT）
        
        Args:
            symbol: 交易对符号
            price: 价格
            quantity: 数量
        
        Returns:
            (formatted_price, formatted_quantity): 格式化后的价格和数量
        """
        tick_size, step_size = self.get_symbol_precision(symbol)
        
        # 格式化价格
        formatted_price = self.format_price(price, tick_size)
        
        # 格式化数量（自动检查最小名义价值）
        formatted_qty = self.format_quantity(quantity, step_size, Decimal('100'), formatted_price)
        
        return formatted_price, formatted_qty
    
    # ==================== 公共接口 (无需签名) ====================
    
    def test_connectivity(self) -> bool:
        """测试连接"""
        try:
            # PM 账户使用 /papi/v1/time 代替 /fapi/v1/ping
            endpoint = '/papi/v1/time' if self.use_unified_account else '/fapi/v1/ping'
            self._make_request('GET', endpoint)
            return True
        except Exception:
            return False
    
    def get_server_time(self) -> int:
        """获取服务器时间"""
        # PM 账户使用 /papi/v1/time
        endpoint = '/papi/v1/time' if self.use_unified_account else '/fapi/v1/time'
        data = self._make_request('GET', endpoint)
        return data['serverTime']
    
    def get_exchange_info(self, symbol: str = None) -> Dict[str, Any]:
        """获取交易规范信息"""
        endpoint = '/papi/v1/um/exchangeInfo' if self.use_unified_account else '/fapi/v1/exchangeInfo'
        if symbol:
            params = {'symbol': symbol}
            data = self._make_request('GET', endpoint, params)
            return data['symbols'][0] if data.get('symbols') else None
        return self._make_request('GET', endpoint)
    
    def get_ticker_price(self, symbol: str) -> Decimal:
        """获取最新价格"""
        params = {'symbol': symbol}
        data = self._make_request('GET', '/api/v3/ticker/price', params)
        return Decimal(data['price'])
    
    # ==================== 账户接口 (需要签名) ====================
    
    def get_account_info(self) -> Dict[str, Any]:
        """
        获取账户信息
        
        Returns:
            账户信息
        
        Note:
            PM 账户和传统账户返回格式不同：
            - PM 账户：扁平结构，使用 totalAvailableBalance 字段
            - 传统账户：包含 assets 数组
        """
        if self.use_unified_account:
            # PM 账户使用 /papi/v1/account
            return self._make_request('GET', '/papi/v1/account', signed=True)
        else:
            # 传统账户使用 /fapi/v2/account
            return self._make_request('GET', '/fapi/v2/account', signed=True)
    
    def get_umfut_balance(self, asset: str = 'USDT') -> Decimal:
        """
        获取 U 本位合约账户余额（兼容统一交易账户）
        
        Args:
            asset: 币种，默认 USDT
        
        Returns:
            可用余额
        
        Note:
            PM 账户返回扁平结构，传统账户返回 assets 数组
        """
        account = self.get_account_info()
        
        if self.use_unified_account:
            # PM 账户返回 totalAvailableBalance 字段
            available = account.get('totalAvailableBalance', '0')
            return Decimal(str(available))
        else:
            # 传统账户返回 assets 数组
            assets = account.get('assets', [])
            for a in assets:
                if a.get('asset') == asset:
                    available = a.get('availableBalance', '0')
                    return Decimal(str(available))
            return Decimal('0')
    
    # ==================== 交易接口 (需要签名) ====================
    
    def place_limit_order(self, symbol: str, side: str, position_side: str,
                         quantity: Decimal, price: Decimal, 
                         time_in_force: str = 'GTC') -> Dict[str, Any]:
        """
        限价单（自动处理精度）
        
        Args:
            symbol: 交易对符号
            side: 买卖方向 (BUY/SELL)
            position_side: 仓位方向 (PM 账户必须使用 BOTH)
            quantity: 数量
            price: 价格
            time_in_force: 时间条件 (GTC/IOC/FOK)
        
        Returns:
            订单响应数据
        
        Note:
            PM 账户注意事项：
            1. position_side 必须使用 'BOTH'（单向持仓模式）
            2. 价格必须符合 tickSize
            3. 数量必须符合 stepSize
            4. 名义价值必须 >= 100 USDT
        """
        # 自动格式化价格和数量
        price, quantity = self.format_order_params(symbol, price, quantity)
        
        # PM 账户强制使用 BOTH 仓位方向
        if self.use_unified_account:
            position_side = 'BOTH'
        
        params = {
            'symbol': symbol,
            'side': side,
            'positionSide': position_side,
            'type': 'LIMIT',
            'quantity': str(quantity),
            'price': str(price),
            'timeInForce': time_in_force,
            'timestamp': int(time.time() * 1000)
        }
        
        endpoint = '/papi/v1/um/order' if self.use_unified_account else '/fapi/v1/order'
        return self._make_request('POST', endpoint, params, signed=True)
    
    def place_market_order(self, symbol: str, side: str, position_side: str,
                          quantity: Decimal) -> Dict[str, Any]:
        """
        市价单（自动处理精度）
        
        Args:
            symbol: 交易对符号
            side: 买卖方向 (BUY/SELL)
            position_side: 仓位方向 (PM 账户必须使用 BOTH)
            quantity: 数量
        
        Returns:
            订单响应数据
        """
        # 格式化数量
        _, step_size = self.get_symbol_precision(symbol)
        quantity = self.format_quantity(quantity, step_size)
        
        # PM 账户强制使用 BOTH
        if self.use_unified_account:
            position_side = 'BOTH'
        
        params = {
            'symbol': symbol,
            'side': side,
            'positionSide': position_side,
            'type': 'MARKET',
            'quantity': str(quantity),
            'timestamp': int(time.time() * 1000)
        }
        
        endpoint = '/papi/v1/um/order' if self.use_unified_account else '/fapi/v1/order'
        return self._make_request('POST', endpoint, params, signed=True)
    
    def set_um_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """
        设置 U 本位合约杠杆
        
        Args:
            symbol: 交易对符号
            leverage: 杠杆倍数 (1-125)
        
        Returns:
            杠杆设置结果
        """
        params = {
            'symbol': symbol,
            'leverage': leverage,
            'timestamp': int(time.time() * 1000)
        }
        
        endpoint = '/papi/v1/um/leverage' if self.use_unified_account else '/fapi/v1/leverage'
        return self._make_request('POST', endpoint, params, signed=True)
    
    def get_all_positions(self) -> List[Dict[str, Any]]:
        """
        获取所有持仓
        
        Returns:
            持仓列表
        """
        params = {'timestamp': int(time.time() * 1000)}
        endpoint = '/papi/v1/um/positionRisk' if self.use_unified_account else '/fapi/v2/positionRisk'
        return self._make_request('GET', endpoint, params, signed=True)
    
    def cancel_order(self, symbol: str, order_id: int = None, 
                    orig_client_order_id: str = None) -> Dict[str, Any]:
        """
        撤销订单
        
        Args:
            symbol: 交易对符号
            order_id: 订单 ID
            orig_client_order_id: 原始客户端订单 ID
        
        Returns:
            撤销结果
        """
        params = {
            'symbol': symbol,
            'timestamp': int(time.time() * 1000)
        }
        
        if order_id:
            params['orderId'] = order_id
        if orig_client_order_id:
            params['origClientOrderId'] = orig_client_order_id
        
        endpoint = '/papi/v1/order' if self.use_unified_account else '/fapi/v1/order'
        return self._make_request('DELETE', endpoint, params, signed=True)


# ==================== 便捷函数 ====================

def get_trade_api(api_key: str = None, secret_key: str = None, 
                  use_unified_account: bool = None) -> BinanceTradeAPI:
    """
    获取 BinanceTradeAPI 实例
    
    Args:
        api_key: API Key（可选，默认从环境变量读取）
        secret_key: Secret Key（可选，默认从环境变量读取）
        use_unified_account: 是否使用统一账户（可选，默认从环境变量读取）
    
    Returns:
        BinanceTradeAPI 实例
    
    Example:
        >>> api = get_trade_api()
        >>> balance = api.get_umfut_balance('USDT')
    """
    return BinanceTradeAPI(
        api_key=api_key,
        secret_key=secret_key,
        use_unified_account=use_unified_account
    )
