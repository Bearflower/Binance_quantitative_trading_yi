"""
币安合约交易 API 客户端
提供完整的交易、账户管理、资金划转等功能
"""

import hashlib
import hmac
import logging
import time
from decimal import Decimal
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


class BinanceTradeAPI:
    """币安合约交易 API 客户端"""
    
    def __init__(
        self,
        api_key: str = '',
        api_secret: str = '',
        base_url: str = 'https://papi.binance.com',
        testnet: bool = False
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        
        if testnet:
            self.base_url = 'https://testnet.binancefuture.com'
        
        self.session = requests.Session()
        self.session.headers.update({
            'X-MBX-APIKEY': self.api_key,
            'Content-Type': 'application/json'
        })
    
    def _get_signature(self, query_string: str) -> str:
        """生成 HMAC SHA256 签名"""
        return hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
    
    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        signed: bool = False
    ) -> Dict:
        """发送 HTTP 请求"""
        url = f'{self.base_url}{endpoint}'
        
        if params is None:
            params = {}
        
        if signed:
            params['timestamp'] = int(time.time() * 1000)
            query_string = '&'.join([f'{k}={v}' for k, v in params.items()])
            params['signature'] = self._get_signature(query_string)
        
        try:
            response = self.session.request(method, url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"API 请求失败：{method} {endpoint}, 错误：{e}")
            raise
    
    # ===== 行情数据 =====
    
    def get_ticker_price(self, symbol: str = 'BTCUSDT') -> str:
        """获取当前价格"""
        # 行情数据使用 fapi 接口
        url = f'https://fapi.binance.com/fapi/v1/ticker/price'
        params = {'symbol': symbol}
        try:
            response = self.session.request('GET', url, params=params, timeout=10)
            response.raise_for_status()
            return response.json().get('price', '0')
        except Exception as e:
            logger.error(f"获取价格失败：{e}")
            return '0'
    
    def get_klines(
        self,
        symbol: str = 'BTCUSDT',
        interval: str = '1h',
        limit: int = 100
    ) -> List[Dict]:
        """获取 K 线数据"""
        # 行情数据使用 fapi 接口
        url = f'https://fapi.binance.com/fapi/v1/klines'
        params = {
            'symbol': symbol,
            'interval': interval,
            'limit': limit
        }
        try:
            response = self.session.request('GET', url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"获取 K 线失败：{e}")
            return []
    
    # ===== 账户信息 =====
    
    def get_account_info(self) -> Dict:
        """获取账户信息（PM 账户）"""
        endpoint = '/papi/v1/account'
        return self._request('GET', endpoint, signed=True)
    
    def get_position(self, symbol: str = 'BTCUSDT') -> Optional[Dict]:
        """获取持仓信息"""
        account = self.get_account_info()
        positions = account.get('positions', [])
        
        for pos in positions:
            if pos.get('symbol') == symbol:
                return pos
        
        return None
    
    def get_all_positions(self) -> List[Dict]:
        """获取所有持仓"""
        account = self.get_account_info()
        positions = account.get('positions', [])
        
        # 返回有持仓的数据
        return [
            pos for pos in positions
            if float(pos.get('positionAmt', 0)) != 0
        ]
    
    # ===== 杠杆设置 =====
    
    def set_um_leverage(
        self,
        symbol: str = 'BTCUSDT',
        leverage: int = 10
    ) -> Dict:
        """设置 U 本位合约杠杆（PM 账户）"""
        endpoint = '/papi/v1/um/leverage'
        params = {
            'symbol': symbol,
            'leverage': leverage
        }
        return self._request('POST', endpoint, params, signed=True)
    
    # ===== 订单管理 =====
    
    def place_limit_order(
        self,
        symbol: str = 'BTCUSDT',
        side: str = 'BUY',
        position_side: str = 'LONG',
        quantity: Decimal = Decimal('0.001'),
        price: Decimal = Decimal('50000'),
        time_in_force: str = 'GTC'
    ) -> Dict:
        """限价单（PM 账户）"""
        endpoint = '/papi/v1/um/order'
        params = {
            'symbol': symbol,
            'side': side,
            'positionSide': position_side,
            'type': 'LIMIT',
            'quantity': str(quantity),
            'price': str(price),
            'timeInForce': time_in_force
        }
        return self._request('POST', endpoint, params, signed=True)
    
    def place_market_order(
        self,
        symbol: str = 'BTCUSDT',
        side: str = 'BUY',
        position_side: str = 'LONG',
        quantity: Decimal = Decimal('0.001')
    ) -> Dict:
        """市价单（PM 账户）"""
        endpoint = '/papi/v1/um/order'
        params = {
            'symbol': symbol,
            'side': side,
            'positionSide': position_side,
            'type': 'MARKET',
            'quantity': str(quantity)
        }
        return self._request('POST', endpoint, params, signed=True)
    
    def cancel_order(
        self,
        symbol: str = 'BTCUSDT',
        order_id: Optional[int] = None
    ) -> Dict:
        """撤销订单（PM 账户）"""
        endpoint = '/papi/v1/order'
        params = {'symbol': symbol}
        
        if order_id:
            params['orderId'] = order_id
        
        return self._request('DELETE', endpoint, params, signed=True)
    
    def get_order_status(
        self,
        symbol: str = 'BTCUSDT',
        order_id: int = 0
    ) -> Dict:
        """查询订单状态（PM 账户）"""
        endpoint = '/papi/v1/order'
        params = {
            'symbol': symbol,
            'orderId': order_id
        }
        return self._request('GET', endpoint, params, signed=True)
    
    def get_all_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """获取所有未成交订单（PM 账户）"""
        endpoint = '/papi/v1/openOrders'
        params = {}
        
        if symbol:
            params['symbol'] = symbol
        
        return self._request('GET', endpoint, params, signed=True)
    
    # ===== 资金管理 =====
    
    def get_umfut_balance(self, asset: str = 'USDT') -> Decimal:
        """
        获取 U 本位合约余额（PM 账户）
        
        PM 账户（统一账户）的 /papi/v1/account 返回格式：
        {
          "uniMMR": "99999999",
          "accountEquity": "142.09091742",
          "actualEquity": "142.1051343",
          "accountInitialMargin": "0.0",
          "accountMaintMargin": "0.0",
          "accountStatus": "NORMAL",
          "virtualMaxWithdrawAmount": "142.09091742",
          "totalAvailableBalance": "142.09091742",
          "totalMarginOpenLoss": "0.0",
          "updateTime": 1774229167996
        }
        """
        account = self.get_account_info()
        
        # PM 账户返回 totalAvailableBalance 字段
        available = account.get('totalAvailableBalance', '0')
        return Decimal(str(available))
    
    def get_spot_balance(self, asset: str = 'USDT') -> Decimal:
        """获取现货余额"""
        # 现货余额使用 api.binance.com
        url = f'https://api.binance.com/api/v3/account'
        timestamp = int(time.time() * 1000)
        params = {'timestamp': timestamp}
        query_string = '&'.join([f'{k}={v}' for k, v in params.items()])
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        params['signature'] = signature
        
        headers = {'X-MBX-APIKEY': self.api_key}
        try:
            response = self.session.request('GET', url, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            for balance in data.get('balances', []):
                if balance['asset'] == asset:
                    return Decimal(balance['free'])
            return Decimal('0')
        except Exception as e:
            logger.error(f"获取现货余额失败：{e}")
            return Decimal('0')
        balances = account.get('balances', [])
        
        for b in balances:
            if b.get('asset') == asset:
                free = Decimal(b.get('free', '0'))
                locked = Decimal(b.get('locked', '0'))
                return free + locked
        
        return Decimal('0')
    
    def transfer_spot_to_umfut(
        self,
        asset: str = 'USDT',
        amount: Decimal = Decimal('100')
    ) -> Dict:
        """现货账户转向 U 本位合约账户（万向划转）"""
        # 万向划转使用 api.binance.com
        url = f'https://api.binance.com/sapi/v1/asset/transfer'
        timestamp = int(time.time() * 1000)
        params = {
            'asset': asset,
            'amount': str(amount),
            'type': 'MAIN_UMFUTURE',  # 现货钱包转向 U 本位合约钱包
            'timestamp': timestamp
        }
        query_string = '&'.join([f'{k}={v}' for k, v in params.items()])
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        params['signature'] = signature
        
        headers = {'X-MBX-APIKEY': self.api_key}
        try:
            response = self.session.request('POST', url, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"划转失败：{e}")
            return {}
    
    def transfer_umfut_to_spot(
        self,
        asset: str = 'USDT',
        amount: Decimal = Decimal('100')
    ) -> Dict:
        """U 本位合约账户转向现货账户"""
        endpoint = '/sapi/v1/futures/transfer'
        params = {
            'asset': asset,
            'amount': str(amount),
            'type': '2'  # U 本位合约→现货
        }
        return self._request('POST', endpoint, params, signed=True)
    
    # ===== 赚币理财 =====
    
    def get_simple_earn_flexible_list(
        self,
        asset: str = 'USDT',
        size: int = 10
    ) -> List[Dict]:
        """获取赚币活期产品列表"""
        endpoint = '/sapi/v1/simple-earn/flexible/list'
        params = {
            'asset': asset,
            'size': size
        }
        return self._request('GET', endpoint, params, signed=True)
    
    def get_simple_earn_flexible_product(
        self,
        product_id: str = 'USDT'
    ) -> Dict:
        """获取赚币活期持仓"""
        # 赚币产品使用 api.binance.com
        url = f'https://api.binance.com/sapi/v1/simple-earn/flexible/position'
        timestamp = int(time.time() * 1000)
        params = {'productId': product_id, 'timestamp': timestamp}
        query_string = '&'.join([f'{k}={v}' for k, v in params.items()])
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        params['signature'] = signature
        
        headers = {'X-MBX-APIKEY': self.api_key}
        try:
            response = self.session.request('GET', url, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            result = response.json()
            
            if isinstance(result, dict) and 'rows' in result:
                rows = result.get('rows', [])
                if rows:
                    return rows[0]
            
            return result
        except Exception as e:
            logger.error(f"获取持仓失败：{e}")
            return {}
    
    def redeem_simple_earn_flexible(
        self,
        product_id: str = 'USDT',
        redeem_all: bool = True,
        dest_account: str = 'SPOT'
    ) -> Dict:
        """赎回赚币活期产品"""
        # 赚币产品使用 api.binance.com
        url = f'https://api.binance.com/sapi/v1/simple-earn/flexible/redeem'
        timestamp = int(time.time() * 1000)
        params = {
            'productId': product_id,
            'redeemAll': str(redeem_all).lower(),
            'destAccount': dest_account,
            'timestamp': timestamp
        }
        query_string = '&'.join([f'{k}={v}' for k, v in params.items()])
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        params['signature'] = signature
        
        headers = {'X-MBX-APIKEY': self.api_key}
        try:
            response = self.session.request('POST', url, params=params, headers=headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"赎回失败：{e}")
            return {}
    
    # ===== 工具方法 =====
    
    def test_connectivity(self) -> bool:
        """测试连接"""
        try:
            # PM 账户使用 /papi/v1/time 代替 /fapi/v1/ping
            endpoint = '/papi/v1/time'
            self._request('GET', endpoint)
            return True
        except Exception:
            return False
    
    def get_server_time(self) -> int:
        """获取服务器时间"""
        # PM 账户使用 /papi/v1/time
        endpoint = '/papi/v1/time'
        result = self._request('GET', endpoint)
        return result.get('serverTime', 0)


# 全局 API 实例
_trade_api_instance: Optional[BinanceTradeAPI] = None


def get_trade_api() -> BinanceTradeAPI:
    """获取全局 API 实例"""
    global _trade_api_instance
    
    if _trade_api_instance is None:
        # 从环境变量或配置加载
        import os
        from dotenv import load_dotenv
        
        load_dotenv()
        
        api_key = os.getenv('BINANCE_API_KEY', '')
        api_secret = os.getenv('BINANCE_SECRET_KEY', '')
        testnet = os.getenv('BINANCE_TESTNET', 'false').lower() == 'true'
        
        _trade_api_instance = BinanceTradeAPI(
            api_key=api_key,
            api_secret=api_secret,
            testnet=testnet
        )
    
    return _trade_api_instance
