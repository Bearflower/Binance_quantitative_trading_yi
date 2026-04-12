#!/usr/bin/env python3
"""
币安交易 API 封装模块
提供完整的交易功能：理财查询/赎回、资金划转、合约下单、持仓监控

重要：此模块包含实际资金操作，仅能在服务器环境 (生产环境) 运行
本地环境仅用于测试接口连通性和参数验证
"""

import hashlib
import hmac
import time
import requests
import os
import logging
from decimal import Decimal
from typing import Optional, Dict, List, Any
from urllib.parse import urlencode
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from functools import wraps

# 导入配置文件
from config.settings import ENVIRONMENT

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
                 base_url: str = "https://papi.binance.com", testnet: bool = False):
        """
        初始化币安交易 API
        
        Args:
            api_key: API Key
            secret_key: Secret Key
            base_url: API 基础 URL
            testnet: 是否使用测试网
        """
        self.api_key = api_key or os.getenv('BINANCE_API_KEY')
        self.secret_key = secret_key or os.getenv('BINANCE_SECRET_KEY')
        self.base_url = base_url or os.getenv('BINANCE_API_BASE_URL', 'https://papi.binance.com')
        self.testnet = testnet or os.getenv('BINANCE_TESTNET', 'false').lower() == 'true'
        
        if not self.api_key or not self.secret_key:
            raise ValueError("API Key 和 Secret Key 不能为空")
        
        # 检查是否为生产环境（使用配置文件中的 ENVIRONMENT）
        self.is_production = ENVIRONMENT == 'production'
        
        if not self.is_production and not self.testnet:
            logger.warning("⚠️  当前为开发环境，部分交易功能将被禁用")
            logger.warning("⚠️  如需启用交易功能，请设置 ENVIRONMENT=production 或使用测试网")
        
        logger.info(f"币安交易 API 初始化完成 - 环境：{'生产' if self.is_production else '开发'}, 测试网：{self.testnet}")
    
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
    
    # ==================== 公共接口 (无需签名) ====================
    
    def get_server_time(self) -> int:
        """获取服务器时间"""
        data = self._make_request('GET', '/papi/v1/time')
        return data['serverTime']
    
    def get_exchange_info(self, symbol: str = None) -> Dict[str, Any]:
        """获取交易规范信息"""
        endpoint = '/papi/v1/um/exchangeInfo'
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
    
    def get_umfut_balance(self, asset: str = 'USDT') -> Decimal:
        """
        获取 U 本位合约账户余额（兼容统一交易账户）
        
        Args:
            asset: 币种，默认 USDT
        
        Returns:
            可用余额（可用于开仓的保证金）
        """
        # 对于统一交易账户（Portfolio Margin），需要从 futures_account 获取合约账户信息
        # 因为 /papi/v1/balance 返回的是跨仓保证金账户（现货/理财），不是合约账户
        
        try:
            # 获取合约账户信息
            account_info = self.futures_account()
            
            for acc_asset in account_info.get('assets', []):
                if acc_asset.get('asset') == asset:
                    # 对于 PM 账户，可用保证金 = 合约账户权益 - 已用保证金
                    cross_wallet_balance = Decimal(acc_asset.get('crossWalletBalance', '0'))
                    initial_margin = Decimal(acc_asset.get('initialMargin', '0'))
                    
                    # 可用保证金 = 总权益 - 已用保证金（持仓保证金 + 挂单保证金）
                    available = cross_wallet_balance - initial_margin
                    
                    # 确保不会出现负数
                    if available < 0:
                        available = Decimal('0')
                    
                    # 调试日志
                    import logging
                    logger = logging.getLogger(__name__)
                    logger.debug(f"合约账户余额详情 [{asset}]: crossWalletBalance={cross_wallet_balance}, initialMargin={initial_margin}, available={available}")
                    
                    return available
            
            return Decimal('0')
            
        except Exception as e:
            # 如果失败，回退到旧方法（非 PM 账户）
            logger.warning(f"获取合约账户余额失败，使用备用方法：{e}")
            
            # 使用 /papi/v1/balance 接口（适用于非 PM 账户）
            data = self._make_request('GET', '/papi/v1/balance', signed=True)
            
            if isinstance(data, list):
                for item in data:
                    if item['asset'] == asset:
                        return Decimal(item.get('crossMarginFree', '0'))
            
            return Decimal('0')
    
    def get_spot_balance(self, asset: str = 'USDT') -> Decimal:
        """
        获取现货账户余额
        
        Args:
            asset: 币种，默认 USDT
        
        Returns:
            可用余额
        """
        params = {'asset': asset}
        data = self._make_request('GET', '/papi/v1/asset/wallet/balance', params, signed=True)
        return Decimal(data.get('balance', '0'))
    
    def get_account_info(self) -> Dict[str, Any]:
        """
        获取账户信息
        
        对于统一交易账户（Portfolio Margin），使用 /papi/v1/balance
        对于普通 U 本位合约账户，使用 /papi/v1/um/account
        """
        # 尝试统一交易账户接口
        try:
            return self._make_request('GET', '/papi/v1/balance', signed=True)
        except Exception:
            # 如果失败，回退到普通 U 本位合约账户接口
            return self._make_request('GET', '/papi/v1/um/account', signed=True)
    
    def get_um_account(self) -> Dict[str, Any]:
        """获取 U 本位合约账户信息"""
        return self._make_request('GET', '/papi/v1/um/account', signed=True)
    
    def futures_account(self) -> Dict[str, Any]:
        """
        获取 U 本位合约账户信息（包含余额和持仓详情）
        
        Returns:
            账户信息，包含 assets（资产列表）和 positions（持仓列表）
        """
        return self._make_request('GET', '/papi/v1/um/account', signed=True)
    
    def get_um_position_risk(self) -> List[Dict[str, Any]]:
        """获取 U 本位持仓风险信息"""
        return self._make_request('GET', '/papi/v1/um/positionRisk', signed=True)
    
    def set_um_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """设置 U 本位杠杆"""
        params = {
            'symbol': symbol,
            'leverage': leverage
        }
        return self._make_request('POST', '/papi/v1/um/leverage', params, signed=True)
    
    def get_portfolio_account(self) -> Dict[str, Any]:
        """
        获取统一交易账户信息
        
        Returns:
            统一交易账户信息
        """
        return self._make_request('GET', '/papi/v1/portfolio/account', signed=True)
    
    # ==================== 赚币 (活期) 接口 ====================
    
    def get_simple_earn_flexible_list(self, asset: str = None, current: int = 1, 
                                     size: int = 10) -> List[Dict[str, Any]]:
        """
        查询赚币活期产品列表
        
        Args:
            asset: 币种
            current: 当前页码，默认 1
            size: 每页数量，默认 10，最大 100
        
        Returns:
            产品列表
        """
        params = {
            'current': current,
            'size': size
        }
        
        if asset:
            params['asset'] = asset
        
        # 使用主站 API 而不是 PAPI
        data = self._make_request('GET', '/sapi/v1/simple-earn/flexible/list', params, signed=True)
        return data.get('rows', [])
    
    def get_spot_account(self) -> Dict[str, Any]:
        """
        查询现货账户余额
        
        Returns:
            现货账户信息
        """
        return self._make_request('GET', '/api/v3/account', signed=True)
    
    def get_simple_earn_flexible_holdings(self, asset: str = None) -> List[Dict[str, Any]]:
        """
        查询赚币活期持仓
        
        Args:
            asset: 币种
        
        Returns:
            持仓列表
        """
        params = {'size': 100}
        if asset:
            params['asset'] = asset
        
        data = self._make_request('GET', '/sapi/v1/simple-earn/flexible/position', params, signed=True)
        # 返回 rows 数组而不是整个响应
        return data.get('rows', [])
    
    def get_simple_earn_flexible_position(self, asset: str = None) -> List[Dict[str, Any]]:
        """
        查询赚币活期持仓
        
        Args:
            asset: 币种
        
        Returns:
            持仓列表
        """
        params = {'size': 100}
        if asset:
            params['asset'] = asset
        
        data = self._make_request('GET', '/sapi/v1/simple-earn/flexible/position', params, signed=True)
        return data.get('rows', [])
    
    def redeem_simple_earn_flexible(self, product_id: str, amount: Decimal = None, 
                                   redeem_all: bool = False, 
                                   dest_account: str = 'SPOT') -> Dict[str, Any]:
        """
        赎回活期产品
        
        Args:
            product_id: 产品 ID
            amount: 赎回数量
            redeem_all: 是否全部赎回
            dest_account: 目标账户 (SPOT/FUND)
        
        Returns:
            赎回结果
        """
        params = {
            'productId': product_id,
            'redeemAll': str(redeem_all).lower()
        }
        
        if not redeem_all and amount:
            params['amount'] = str(amount)
        
        params['destAccount'] = dest_account
        
        data = self._make_request('POST', '/sapi/v1/simple-earn/flexible/redeem', params, signed=True)
        return data
    
    def asset_transfer(self, type: str, asset: str, amount: Decimal) -> Dict[str, Any]:
        """
        万向划转
        
        Args:
            type: 划转类型 (MAIN_UMFUTURE: 现货→合约，UMFUTURE_MAIN: 合约→现货)
            asset: 币种
            amount: 数量
        
        Returns:
            划转结果
        """
        params = {
            'type': type,
            'asset': asset,
            'amount': str(amount)
        }
        
        data = self._make_request('POST', '/sapi/v1/asset/transfer', params, signed=True)
        return data
    
    # 常用划转类型封装
    def transfer_spot_to_umfut(self, asset: str, amount: Decimal) -> Dict[str, Any]:
        """现货钱包转向 U 本位合约钱包"""
        return self.asset_transfer('MAIN_UMFUTURE', asset, amount)
    
    def transfer_umfut_to_spot(self, asset: str, amount: Decimal) -> Dict[str, Any]:
        """U 本位合约钱包转向现货钱包"""
        return self.asset_transfer('UMFUTURE_MAIN', asset, amount)
    
    def transfer_spot_to_portfolio(self, asset: str, amount: Decimal) -> Dict[str, Any]:
        """现货钱包转向投资组合钱包"""
        return self.asset_transfer('MAIN_PM', asset, amount)
    
    def get_symbol_precision(self, symbol: str) -> tuple:
        """
        获取交易对的精度信息
        
        Args:
            symbol: 交易对，如 BTCUSDT
        
        Returns:
            (tick_size, step_size) 元组
            tick_size: 价格精度（最小价格变动）
            step_size: 数量精度（最小数量变动）
        """
        try:
            # 获取交易对信息
            url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
            params = {'symbol': symbol}
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            if 'symbols' not in data or len(data['symbols']) == 0:
                logger.warning(f"未找到交易对 {symbol} 的信息")
                return Decimal('0.1'), Decimal('0.001')
            
            symbol_info = data['symbols'][0]
            
            # 获取价格精度
            tick_size = Decimal('1')
            step_size = Decimal('0.001')
            
            for filter_item in symbol_info.get('filters', []):
                if filter_item['filterType'] == 'PRICE_FILTER':
                    tick_size = Decimal(filter_item.get('tickSize', '0.1'))
                elif filter_item['filterType'] == 'LOT_SIZE':
                    step_size = Decimal(filter_item.get('stepSize', '0.001'))
                elif filter_item['filterType'] == 'MARKET_LOT_SIZE':
                    # 市价单也使用 LOT_SIZE 的精度
                    market_step = Decimal(filter_item.get('stepSize', '0.001'))
                    # 取更严格的精度（更小的 step_size）
                    if market_step < step_size:
                        step_size = market_step
            
            # 验证精度：确保 step_size 是合理的值
            # 基于常见交易对的精度规范进行验证
            if symbol.startswith('BNB'):
                # BNB 通常需要 2 位小数（step_size=0.01）
                # 如果 API 返回更小的值，可能是数据错误，使用保守值
                if step_size < Decimal('0.01'):
                    logger.warning(f"{symbol} API 返回 step_size={step_size}，修正为 0.01")
                    step_size = Decimal('0.01')
            elif symbol.startswith('BTC'):
                # BTC 通常需要 3 位小数（step_size=0.001）
                if step_size < Decimal('0.001'):
                    step_size = Decimal('0.001')
            elif symbol.startswith('ETH'):
                # ETH 通常需要 3 位小数（step_size=0.001）
                if step_size < Decimal('0.001'):
                    step_size = Decimal('0.001')
            
            logger.info(f"{symbol} 精度：tick_size={tick_size}, step_size={step_size}")
            return tick_size, step_size
            
        except Exception as e:
            logger.error(f"获取 {symbol} 精度失败：{str(e)}")
            # 返回默认值
            return Decimal('0.1'), Decimal('0.001')
    
    def _format_price(self, symbol: str, price: Decimal, tick_size: Decimal) -> str:
        """
        格式化价格到正确的精度
        
        Args:
            symbol: 交易对
            price: 价格
            tick_size: 最小价格变动
        
        Returns:
            格式化后的价格字符串
        """
        # 计算 tick_size 的小数位数
        tick_str = str(tick_size)
        if '.' in tick_str:
            decimals = len(tick_str.split('.')[1])
        else:
            decimals = 0
        
        # 将价格四舍五入到正确的精度
        quantize_str = '0.' + '0' * decimals if decimals > 0 else '1'
        formatted_price = str(price.quantize(Decimal(quantize_str)))
        logger.debug(f"{symbol} 价格格式化：{price} → {formatted_price} (tick_size={tick_size}, decimals={decimals})")
        return formatted_price
    
    def _format_quantity(self, symbol: str, quantity: Decimal, step_size: Decimal) -> str:
        """
        格式化数量到正确的精度
        
        Args:
            symbol: 交易对
            quantity: 数量
            step_size: 最小数量变动
        
        Returns:
            格式化后的数量字符串
        """
        # 计算 step_size 的小数位数
        step_str = str(step_size)
        if '.' in step_str:
            decimals = len(step_str.split('.')[1])
        else:
            decimals = 0
        
        # 将数量四舍五入到正确的精度
        quantize_str = '0.' + '0' * decimals if decimals > 0 else '1'
        formatted_quantity = str(quantity.quantize(Decimal(quantize_str)))
        logger.debug(f"{symbol} 数量格式化：{quantity} → {formatted_quantity} (step_size={step_size}, decimals={decimals})")
        return formatted_quantity
    
    def transfer_portfolio_to_spot(self, asset: str, amount: Decimal) -> Dict[str, Any]:
        """统一交易账户转向现货钱包"""
        return self.asset_transfer('PORTFOLIO_MARGIN_MAIN', asset, amount)
    
    def transfer_funding_to_spot(self, asset: str, amount: Decimal) -> Dict[str, Any]:
        """资金钱包转向现货钱包"""
        return self.transfer_asset(asset, amount, 'FUNDING_MAIN')
    
    def transfer_spot_to_funding(self, asset: str, amount: Decimal) -> Dict[str, Any]:
        """现货钱包转向资金钱包"""
        return self.transfer_asset(asset, amount, 'MAIN_FUNDING')
    
    # ==================== 合约下单接口 ====================
    
    def _rate_limit_request(func):
        """限流装饰器（内部方法）"""
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            # 获取端点映射
            endpoint_map = {
                'place_um_order': '/papi/v1/um/order',
                'cancel_order': '/papi/v1/um/order',
                'get_open_order': '/papi/v1/um/openOrder',
                'get_all_open_orders': '/papi/v1/um/openOrders',
                'get_order_status': '/papi/v1/um/order',
                'get_position_risk': '/papi/v1/um/positionRisk',
                'get_um_klines': '/papi/v1/um/klines',
                'get_user_trades': '/papi/v1/um/userTrades',
                'get_commission_rate': '/papi/v1/um/commissionRate',
            }
            
            # 获取端点
            endpoint = endpoint_map.get(func.__name__, '/papi/v1/um/order')
            
            # 获取限流器并请求许可
            limiter = get_rate_limiter()
            limiter.acquire(endpoint)
            
            # 调用原方法
            return func(self, *args, **kwargs)
        return wrapper
    
    @_rate_limit_request
    def place_um_order(self, symbol: str, side: str, position_side: str, 
                      order_type: str, quantity: Decimal, price: Decimal = None,
                      time_in_force: str = 'GTC', reduce_only: bool = False,
                      new_client_order_id: str = None, 
                      new_order_resp_type: str = 'RESULT',
                      stop_price: Decimal = None) -> Dict[str, Any]:
        """
        UM 合约下单
        
        Args:
            symbol: 交易对 (如 BTCUSDT)
            side: 方向 (BUY/SELL)
            position_side: 持仓方向 (LONG/SHORT/BOTH)
            order_type: 订单类型 (LIMIT/MARKET/STOP_MARKET/TAKE_PROFIT_MARKET)
            quantity: 下单数量
            price: 委托价格 (限价单必填)
            time_in_force: 有效方法 (GTC/IOC/FOK/GTD)
            reduce_only: 是否只减仓，默认 False
            new_client_order_id: 用户自定义订单号
            new_order_resp_type: 返回类型 (ACK/RESULT), 默认 RESULT
            stop_price: 触发价 (止损止盈单需要)
        
        Returns:
            订单结果
        """
        if not self.is_production and not self.testnet:
            logger.warning(f"[开发环境] 模拟下单：{side} {position_side} {symbol}, 数量：{quantity}, 价格：{price}")
            return {
                'orderId': 999999,
                'symbol': symbol,
                'status': 'NEW',
                'type': order_type,
                'side': side,
                'positionSide': position_side,
                'origQty': str(quantity),
                'price': str(price) if price else '0',
                'updateTime': int(time.time() * 1000)
            }
        
        params = {
            'symbol': symbol,
            'side': side,
            'positionSide': position_side,
            'type': order_type,
            'quantity': str(quantity),
            'newOrderRespType': new_order_resp_type
        }
        
        if order_type == 'LIMIT' or (order_type in ['STOP_MARKET', 'TAKE_PROFIT_MARKET'] and price):
            params['price'] = str(price)
            params['timeInForce'] = time_in_force
        
        if reduce_only:
            params['reduceOnly'] = 'true'
        
        if new_client_order_id:
            params['newClientOrderId'] = new_client_order_id
        
        # 获取精度并格式化价格和数量
        tick_size, step_size = self.get_symbol_precision(symbol)
        
        if stop_price:
            # 确保是 Decimal 类型
            if isinstance(stop_price, str):
                stop_price = Decimal(stop_price)
            # 格式化触发价
            stop_price_str = self._format_price(symbol, stop_price, tick_size)
            params['stopPrice'] = stop_price_str
        
        if quantity:
            # 确保是 Decimal 类型
            if isinstance(quantity, str):
                quantity = Decimal(quantity)
            # 格式化数量
            quantity_str = self._format_quantity(symbol, quantity, step_size)
            params['quantity'] = quantity_str
            logger.info(f"🎯 {symbol} 数量精度处理：原始={quantity}, step_size={step_size}, 格式化后={quantity_str}, params 中的值={params['quantity']}")
        
        if price:
            # 确保是 Decimal 类型
            if isinstance(price, str):
                price = Decimal(price)
            # 格式化价格
            price_str = self._format_price(symbol, price, tick_size)
            if 'price' in params:
                params['price'] = price_str
        
        logger.info(f"发起下单：{symbol} {side} {position_side}, 类型：{order_type}")
        logger.info(f"  下单参数：quantity={params.get('quantity')} (type={type(params.get('quantity')).__name__}), price={params.get('price')}")
        logger.info(f"  完整 params: {params}")
        result = self._make_request('POST', '/papi/v1/um/order', params, signed=True)
        
        logger.info(f"下单成功：订单 ID={result.get('orderId')}, 状态：{result.get('status')}")
        return result
    
    def place_market_order(self, symbol: str, side: str, position_side: str,
                          quantity: Decimal, **kwargs) -> Dict[str, Any]:
        """市价单"""
        return self.place_um_order(symbol, side, position_side, 'MARKET', quantity, **kwargs)
    
    def place_limit_order(self, symbol: str, side: str, position_side: str,
                         quantity: Decimal, price: Decimal, 
                         time_in_force: str = 'GTC', **kwargs) -> Dict[str, Any]:
        """限价单"""
        return self.place_um_order(symbol, side, position_side, 'LIMIT', quantity, price, 
                                  time_in_force=time_in_force, **kwargs)
    
    def place_stop_market_order(self, symbol: str, side: str, position_side: str,
                               quantity: Decimal, stop_price: Decimal, **kwargs) -> Dict[str, Any]:
        """止损市价单"""
        return self.place_um_order(symbol, side, position_side, 'STOP_MARKET', quantity,
                                  stop_price=stop_price, **kwargs)
    
    def place_take_profit_market_order(self, symbol: str, side: str, position_side: str,
                               quantity: Decimal, stop_price: Decimal, **kwargs) -> Dict[str, Any]:
        """止盈市价单"""
        return self.place_um_order(symbol, side, position_side, 'TAKE_PROFIT_MARKET', quantity,
                                  stop_price=stop_price, **kwargs)
    
    def place_pm_conditional_order(self, symbol: str, side: str, position_side: str,
                                   strategy_type: str, quantity: Decimal = None,
                                   stop_price: Decimal = None, price: Decimal = None,
                                   reduce_only: bool = False, **kwargs) -> Dict[str, Any]:
        """
        PM 账户条件单下单 (止损/止盈单)
        使用接口：POST /papi/v1/um/conditional/order
        
        参数:
            symbol: 交易对
            side: 方向 (SELL/BUY)
            position_side: 持仓方向 (BOTH/LONG/SHORT)
            strategy_type: 条件单类型 ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET")
            quantity: 数量 (STOP/TAKE_PROFIT 必填)
            stop_price: 触发价格 (必填)
            price: 委托价格 (STOP/TAKE_PROFIT 必填)
            reduce_only: 只减仓，默认 False
        """
        params = {
            'symbol': symbol,
            'side': side,
            'positionSide': position_side,
            'strategyType': strategy_type,
            'timestamp': int(time.time() * 1000)
        }
        
        if quantity:
            params['quantity'] = str(quantity)
        
        if stop_price:
            params['stopPrice'] = str(stop_price)
        
        if price:
            params['price'] = str(price)
        
        if reduce_only:
            params['reduceOnly'] = 'true'
        
        # 获取精度并格式化价格和数量
        tick_size, step_size = self.get_symbol_precision(symbol)
        
        if quantity:
            # 根据精度格式化数量
            quantity_str = self._format_quantity(symbol, quantity, step_size)
            params['quantity'] = quantity_str
        
        if stop_price:
            # 根据精度格式化价格
            stop_price_str = self._format_price(symbol, stop_price, tick_size)
            params['stopPrice'] = stop_price_str
        
        if price:
            # 根据精度格式化价格
            price_str = self._format_price(symbol, price, tick_size)
            params['price'] = price_str
        
        # 可选参数
        if 'time_in_force' in kwargs:
            params['timeInForce'] = kwargs['time_in_force']
        if 'working_type' in kwargs:
            params['workingType'] = kwargs['working_type']
        if 'price_protect' in kwargs:
            params['priceProtect'] = 'true' if kwargs['price_protect'] else 'false'
        if 'new_client_strategy_id' in kwargs:
            params['newClientStrategyId'] = kwargs['new_client_strategy_id']
        
        logger.info(f"PM 条件单：{symbol} {side} {position_side}, 类型：{strategy_type}, 触发价：{stop_price}")
        result = self._make_request('POST', '/papi/v1/um/conditional/order', params, signed=True)
        
        logger.info(f"条件单成功：策略 ID={result.get('strategyId')}, 状态：{result.get('strategyStatus')}")
        return result
    
    # ==================== 订单管理接口 ====================
    
    def cancel_order(self, symbol: str, order_id: int = None, 
                    orig_client_order_id: str = None) -> Dict[str, Any]:
        """
        撤销订单
        
        Args:
            symbol: 交易对
            order_id: 订单 ID
            orig_client_order_id: 用户自定义订单号
        
        Returns:
            撤销结果
        """
        params = {'symbol': symbol}
        
        if order_id:
            params['orderId'] = order_id
        elif orig_client_order_id:
            params['origClientOrderId'] = orig_client_order_id
        else:
            raise ValueError("orderId 或 origClientOrderId 必须提供一个")
        
        logger.info(f"撤销订单：{symbol}, 订单 ID={order_id}")
        return self._make_request('DELETE', '/papi/v1/um/order', params, signed=True)
    
    def get_open_order(self, symbol: str, order_id: int = None,
                      orig_client_order_id: str = None) -> Dict[str, Any]:
        """
        查询当前挂单
        
        Args:
            symbol: 交易对
            order_id: 订单 ID
            orig_client_order_id: 用户自定义订单号
        
        Returns:
            订单信息
        """
        params = {'symbol': symbol}
        
        if order_id:
            params['orderId'] = order_id
        elif orig_client_order_id:
            params['origClientOrderId'] = orig_client_order_id
        else:
            raise ValueError("orderId 或 origClientOrderId 必须提供一个")
        
        return self._make_request('GET', '/papi/v1/um/openOrder', params, signed=True)
    
    def get_all_open_orders(self, symbol: str = None) -> List[Dict[str, Any]]:
        """
        查询所有当前挂单
        
        Args:
            symbol: 交易对 (可选，不提供则返回所有)
        
        Returns:
            订单列表
        """
        params = {}
        if symbol:
            params['symbol'] = symbol
        
        return self._make_request('GET', '/papi/v1/um/openOrders', params, signed=True)
    
    def get_order_status(self, symbol: str, order_id: int = None,
                        orig_client_order_id: str = None) -> Dict[str, Any]:
        """
        查询订单状态
        
        Args:
            symbol: 交易对
            order_id: 订单 ID
            orig_client_order_id: 用户自定义订单号
        
        Returns:
            订单状态
        """
        params = {'symbol': symbol}
        
        if order_id:
            params['orderId'] = order_id
        elif orig_client_order_id:
            params['origClientOrderId'] = orig_client_order_id
        else:
            raise ValueError("orderId 或 origClientOrderId 必须提供一个")
        
        return self._make_request('GET', '/papi/v1/um/order', params, signed=True)
    
    def get_um_order_history(self, symbol: str, limit: int = 100,
                            start_time: int = None,
                            end_time: int = None) -> List[Dict[str, Any]]:
        """
        查询所有 UM 订单（包括历史订单）
        端点：GET /papi/v1/um/allOrders
        用途：查询历史订单，用于平仓检测
        
        Args:
            symbol: 交易对
            limit: 返回数量限制，默认 100，最大 1000
            start_time: 开始时间戳（毫秒）
            end_time: 结束时间戳（毫秒）
        
        Returns:
            订单列表
        """
        params = {
            'symbol': symbol,
            'limit': limit
        }
        
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time
        
        return self._make_request('GET', '/papi/v1/um/allOrders', params, signed=True)
    
    def get_pm_conditional_order_history(self, symbol: str = None,
                                        limit: int = 100,
                                        start_time: int = None,
                                        end_time: int = None) -> List[Dict[str, Any]]:
        """
        查询 UM 所有条件订单（包括历史订单）
        端点：GET /papi/v1/um/conditional/allOrders
        用途：查询条件单历史，用于止盈止损触发检测
        
        Args:
            symbol: 交易对（可选，不提供则返回所有）
            limit: 返回数量限制，默认 100，最大 1000
            start_time: 开始时间戳（毫秒）
            end_time: 结束时间戳（毫秒）
        
        Returns:
            条件单列表
        """
        params = {
            'limit': limit
        }
        
        if symbol:
            params['symbol'] = symbol
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time
        
        return self._make_request('GET', '/papi/v1/um/conditional/allOrders', params, signed=True)
    
    # ==================== 持仓接口 ====================
    
    def get_position_risk(self, symbol: str = None) -> List[Dict[str, Any]]:
        """
        获取持仓风险
        
        Args:
            symbol: 交易对 (可选，不提供则返回所有)
        
        Returns:
            持仓风险列表
        """
        params = {}
        if symbol:
            params['symbol'] = symbol
        
        data = self._make_request('GET', '/papi/v1/um/positionRisk', params, signed=True)
        return data if isinstance(data, list) else []
    
    def get_position(self, symbol: str, position_side: str = 'BOTH') -> Optional[Dict[str, Any]]:
        """
        获取指定持仓
        
        Args:
            symbol: 交易对
            position_side: 持仓方向
        
        Returns:
            持仓信息，不存在返回 None
        """
        positions = self.get_position_risk(symbol)
        
        for pos in positions:
            if pos['symbol'] == symbol and pos['positionSide'] == position_side:
                if Decimal(pos['positionAmt']) != 0:
                    return pos
        
        return None
    
    def get_all_positions(self) -> List[Dict[str, Any]]:
        """获取所有持仓"""
        positions = self.get_position_risk()
        # 过滤掉持仓为 0 的记录
        return [pos for pos in positions if Decimal(pos['positionAmt']) != 0]
    
    # ==================== 辅助方法 ====================
    
    def calculate_required_margin(self, quantity: Decimal, price: Decimal, 
                                 leverage: int = 20) -> Decimal:
        """
        计算所需保证金
        
        Args:
            quantity: 数量
            price: 价格
            leverage: 杠杆倍数，默认 20
        
        Returns:
            所需保证金
        """
        notional_value = quantity * price
        margin = notional_value / leverage
        return margin.quantize(Decimal('0.00000001'))
    
    def calculate_pnl_rate(self, position: Dict[str, Any], 
                          current_price: Decimal) -> Decimal:
        """
        计算未实现盈亏率
        
        Args:
            position: 持仓信息
            current_price: 当前价格
        
        Returns:
            盈亏率 (%)
        """
        entry_price = Decimal(position['entryPrice'])
        position_amt = Decimal(position['positionAmt'])
        
        if position_amt > 0:  # 多头
            pnl_rate = (current_price - entry_price) / entry_price
        else:  # 空头
            pnl_rate = (entry_price - current_price) / entry_price
        
        return (pnl_rate * 100).quantize(Decimal('0.01'))
    
    def test_connectivity(self) -> bool:
        """测试 API 连通性"""
        try:
            server_time = self.get_server_time()
            logger.info(f"API 连通性测试成功，服务器时间：{server_time}")
            return True
        except Exception as e:
            logger.error(f"API 连通性测试失败：{str(e)}")
            return False
    
    # ==================== 第一阶段新增 API 接口（做空系统优化） ====================
    
    def get_user_trades(self, symbol: str, limit: int = 100, 
                       start_time: Optional[int] = None, 
                       end_time: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        获取账户成交历史
        端点：GET /papi/v1/um/userTrades
        用途：订单成交确认、每日对账
        
        Args:
            symbol: 交易对，如 BTCUSDT
            limit: 返回数量限制，默认 100
            start_time: 开始时间戳（毫秒）
            end_time: 结束时间戳（毫秒）
        
        Returns:
            成交历史列表
        """
        params = {
            'symbol': symbol,
            'limit': limit,
        }
        
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time
        
        return self._make_request('GET', '/papi/v1/um/userTrades', params=params, signed=True)
    
    def get_commission_rate(self, symbol: str) -> Dict[str, Any]:
        """
        获取账户佣金费率
        端点：GET /papi/v1/um/commissionRate
        用途：精确手续费预估
        
        Args:
            symbol: 交易对，如 BTCUSDT
        
        Returns:
            佣金费率信息，包含 maker 和 taker 费率
        """
        params = {'symbol': symbol}
        
        return self._make_request('GET', '/papi/v1/um/commissionRate', 
                            params=params, signed=True)
    
    def get_um_klines(self, symbol: str, interval: str = '1h', 
                     limit: int = 100) -> List[List[Any]]:
        """
        获取 K 线数据
        端点：GET /papi/v1/um/klines
        用途：动态止盈止损 ATR 计算
        
        Args:
            symbol: 交易对，如 BTCUSDT
            interval: K 线间隔，支持：1m, 5m, 15m, 1h, 4h, 1d
            limit: 返回数量限制，默认 100
        
        Returns:
            K 线数据列表，每项包含：[开盘时间，开盘价，最高价，最低价，收盘价，成交量，...]
        """
        params = {
            'symbol': symbol,
            'interval': interval,
            'limit': limit,
        }
        
        return self._make_request('GET', '/api/v3/klines', params=params)
    
    def get_simple_earn_flexible_product(self, productId: str) -> Dict[str, Any]:
        """
        查询活期理财产品详情
        端点：GET /sapi/v1/simple-earn/flexible/product
        用途：理财赎回规则查询
        
        Args:
            productId: 产品 ID，如 USDT
        
        Returns:
            理财产品详情，包含赎回规则、限额等
        """
        params = {'productId': productId}
        
        return self._make_request('GET', '/sapi/v1/simple-earn/flexible/product',
                            params=params, signed=True)
    
    def modify_um_order_stop_loss_take_profit(self, symbol: str, order_id: int,
                                               stop_loss_price: Optional[Decimal] = None,
                                               take_profit_price: Optional[Decimal] = None) -> Dict[str, Any]:
        """
        修改止盈止损订单
        端点：通过取消原订单 + 重新下单实现
        用途：动态调整止盈止损价格
        
        注意：币安不支持直接修改止盈止损，需先取消原订单再重新设置
        
        Args:
            symbol: 交易对，如 BTCUSDT
            order_id: 订单 ID
            stop_loss_price: 新的止损价格
            take_profit_price: 新的止盈价格
        
        Returns:
            新订单信息
        """
        # 1. 查询原订单
        order = self.get_um_order(symbol, order_id)
        
        # 2. 取消原订单
        self.cancel_um_order(symbol, order_id)
        
        # 3. 重新下单（带新的止盈止损）
        new_params = {
            'symbol': symbol,
            'side': order['side'],
            'type': order['type'],
            'quantity': order['origQty'],
        }
        
        # 如果是限价单，保留原价格
        if order['type'] == 'LIMIT':
            new_params['price'] = order['price']
        
        # 添加新的止盈止损
        if stop_loss_price:
            new_params['stopLossPrice'] = str(stop_loss_price)
            new_params['stopLossTriggerType'] = 'MARK_PRICE'
        if take_profit_price:
            new_params['takeProfitPrice'] = str(take_profit_price)
            new_params['takeProfitTriggerType'] = 'MARK_PRICE'
        
        logger.info(f"修改止盈止损：取消订单 {order_id}，重新下单 {new_params}")
        
        return self.place_um_order(**new_params)
    
    def wait_for_order_fill(self, symbol: str, order_id: int, 
                           timeout: int = 30, poll_interval: float = 1.0) -> Dict[str, Any]:
        """
        轮询订单状态直到成交或超时
        用途：确保订单完全成交后再进行后续操作
        
        Args:
            symbol: 交易对，如 BTCUSDT
            order_id: 订单 ID
            timeout: 超时时间（秒），默认 30 秒
            poll_interval: 轮询间隔（秒），默认 1 秒
        
        Returns:
            最终订单状态
            
        Raises:
            TimeoutError: 超时未成交
            OrderFailedError: 订单失败（取消/拒绝/过期）
        """
        import time
        
        start_time = time.time()
        last_status = None
        
        logger.info(f"开始轮询订单 {order_id}，超时时间 {timeout}秒")
        
        while time.time() - start_time < timeout:
            try:
                # 查询订单状态
                order = self.get_um_order(symbol, order_id)
                current_status = order['status']
                
                # 状态变化时记录日志
                if current_status != last_status:
                    logger.info(f"订单 {order_id} 状态：{current_status}")
                    last_status = current_status
                
                # 检查订单状态
                if current_status == 'FILLED':
                    logger.info(f"✅ 订单 {order_id} 已完全成交")
                    return order
                
                elif current_status in ['CANCELED', 'REJECTED', 'EXPIRED']:
                    error_msg = f"订单失败：{current_status}"
                    logger.error(f"❌ {error_msg} - 订单 {order_id}")
                    raise OrderFailedError(error_msg)
                
                elif current_status == 'PARTIALLY_FILLED':
                    # 部分成交，继续轮询
                    executed_qty = Decimal(order.get('executedQty', 0))
                    orig_qty = Decimal(order.get('origQty', 0))
                    logger.debug(f"订单部分成交：{executed_qty}/{orig_qty}")
                
                # 等待下次轮询
                time.sleep(poll_interval)
                
            except Exception as e:
                logger.error(f"轮询订单状态失败：{str(e)}")
                # 非致命错误，继续轮询
                time.sleep(poll_interval)
        
        # 超时处理
        logger.warning(f"⚠️ 订单 {order_id} 轮询超时 ({timeout}秒)，检查是否部分成交")
        order = self.get_um_order(symbol, order_id)
        return self._handle_timeout_order(order)
    
    def _handle_timeout_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """
        处理超时订单
        
        Args:
            order: 订单信息
        
        Returns:
            处理后的订单信息
            
        Raises:
            TimeoutError: 超时未成交
        """
        executed_qty = Decimal(order.get('executedQty', 0))
        orig_qty = Decimal(order.get('origQty', 0))
        
        if executed_qty > 0:
            # 部分成交
            logger.warning(f"⚠️ 订单超时，部分成交：{executed_qty}/{orig_qty}")
            order['timeout_status'] = 'PARTIALLY_FILLED'
            order['remaining_qty'] = str(orig_qty - executed_qty)
            return order
        else:
            # 完全未成交
            logger.warning(f"⚠️ 订单超时，完全未成交")
            order['timeout_status'] = 'PENDING'
            order['remaining_qty'] = str(orig_qty)
            raise TimeoutError(f"订单超时未成交：{order['orderId']}")
    
    def get_order_status_enum(self, status: str) -> str:
        """
        获取订单状态枚举值
        
        Args:
            status: 订单状态字符串
        
        Returns:
            标准化的状态枚举值
        """
        status_map = {
            'NEW': 'PENDING',
            'PARTIALLY_FILLED': 'PARTIALLY_FILLED',
            'FILLED': 'FILLED',
            'CANCELED': 'CANCELED',
            'REJECTED': 'REJECTED',
            'EXPIRED': 'EXPIRED',
        }
        return status_map.get(status, 'UNKNOWN')
    
    def is_order_active(self, status: str) -> bool:
        """
        判断订单是否活跃（可继续轮询）
        
        Args:
            status: 订单状态
        
        Returns:
            True 表示订单活跃，False 表示已结束
        """
        active_statuses = ['NEW', 'PARTIALLY_FILLED']
        return status in active_statuses
    
    def is_order_failed(self, status: str) -> bool:
        """
        判断订单是否失败
        
        Args:
            status: 订单状态
        
        Returns:
            True 表示失败，False 表示正常
        """
        failed_statuses = ['CANCELED', 'REJECTED', 'EXPIRED']
        return status in failed_statuses


# 全局 API 实例
_trade_api: Optional[BinanceTradeAPI] = None


def get_trade_api() -> BinanceTradeAPI:
    """获取全局交易 API 实例"""
    global _trade_api
    if _trade_api is None:
        _trade_api = BinanceTradeAPI()
    return _trade_api


# 便捷函数
def test_api_connection() -> bool:
    """测试 API 连接"""
    api = get_trade_api()
    return api.test_connectivity()


if __name__ == '__main__':
    # 测试代码
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 60)
    print("币安交易 API 测试")
    print("=" * 60)
    
    try:
        api = get_trade_api()
        
        # 测试连通性
        print("\n1. 测试 API 连通性...")
        if api.test_connectivity():
            print("✅ 连通性测试通过")
        else:
            print("❌ 连通性测试失败")
        
        # 获取账户信息
        print("\n2. 获取账户信息...")
        account_info = api.get_account_info()
        print(f"账户信息：{account_info}")
        
        # 获取余额
        print("\n3. 获取 USDT 余额...")
        umfut_balance = api.get_umfut_balance('USDT')
        print(f"U 本位合约账户 USDT 余额：{umfut_balance}")
        
        spot_balance = api.get_spot_balance('USDT')
        print(f"现货账户 USDT 余额：{spot_balance}")
        
        # 获取持仓
        print("\n4. 获取当前持仓...")
        positions = api.get_all_positions()
        if positions:
            for pos in positions:
                print(f"  {pos['symbol']} {pos['positionSide']}: {pos['positionAmt']} @ {pos['entryPrice']}")
        else:
            print("  无持仓")
        
        # 获取活期产品列表
        print("\n5. 获取赚币活期产品列表...")
        products = api.get_simple_earn_flexible_list(asset='USDT', size=5)
        if products:
            for prod in products:
                print(f"  {prod['productId']}: 年化收益率 {prod['latestAnnualPercentageRate']}%")
        else:
            print("  无 USDT 活期产品")
        
        print("\n" + "=" * 60)
        print("测试完成!")
        print("=" * 60)
        
    except BinanceAPIError as e:
        print(f"\n❌ API 错误：{e.code} - {e.msg}")
    except Exception as e:
        print(f"\n❌ 未知错误：{str(e)}", exc_info=True)
