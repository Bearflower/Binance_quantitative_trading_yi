"""
币安 API 客户端封装
提供 REST API 和 WebSocket 连接管理
支持网格参数修改（无需终止重建）
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp
import websockets
from src.utils.exceptions import APIError, NetworkError, RateLimitError

logger = logging.getLogger(__name__)


class BinanceClient:
    """币安 API 客户端"""
    
    # API 端点
    TESTNET_REST_BASE = "https://testnet.binancefuture.com"
    TESTNET_WS_BASE = "wss://stream.binancefuture.com"
    LIVE_REST_BASE = "https://fapi.binance.com"
    LIVE_WS_BASE = "wss://fstream.binance.com"
    SPOT_REST_BASE = "https://api.binance.com"  # 现货 API
    PRO_REST_BASE = "https://papi.binance.com"  # 专业账户/统一账户 API
    
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        use_unified_account: bool = True  # 使用统一账户
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.use_unified_account = use_unified_account
        
        # 如果使用统一账户，使用专业账户 API
        if use_unified_account:
            self.rest_base = self.PRO_REST_BASE
        else:
            self.rest_base = self.TESTNET_REST_BASE if testnet else self.LIVE_REST_BASE
        
        self.ws_base = self.TESTNET_WS_BASE if testnet else self.LIVE_WS_BASE
        self.spot_rest_base = self.SPOT_REST_BASE  # 现货 API 始终使用正式网
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_callbacks: List[Callable] = []
        self._ws_running = False
        
    async def _get_session(self) -> aiohttp.ClientSession:
        """获取 HTTP Session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    def _generate_signature(self, params: Dict[str, Any]) -> str:
        """生成请求签名"""
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def _get_timestamp(self) -> int:
        """获取当前时间戳（毫秒）"""
        return int(time.time() * 1000)
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        retry: int = 3
    ) -> Dict:
        """
        发送 HTTP 请求
        
        Args:
            method: HTTP 方法
            endpoint: API 端点
            params: 请求参数
            signed: 是否需要签名
            retry: 重试次数
            
        Returns:
            响应数据
        """
        url = f"{self.rest_base}{endpoint}"
        
        if params is None:
            params = {}
        
        if signed:
            params['timestamp'] = self._get_timestamp()
            params['signature'] = self._generate_signature(params)
        
        headers = {
            'X-MBX-APIKEY': self.api_key,
            'Content-Type': 'application/json'
        }
        
        session = await self._get_session()
        
        for attempt in range(retry):
            try:
                async with session.request(
                    method,
                    url,
                    params=params if method == 'GET' else None,
                    data=json.dumps(params) if method == 'POST' else None,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    data = await response.json()
                    
                    if response.status == 200:
                        return data
                    elif response.status == 429:
                        raise RateLimitError(f"API 频率限制：{data.get('msg', '')}")
                    else:
                        raise APIError(f"API 错误 {response.status}: {data.get('msg', '')}")
                        
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == retry - 1:
                    raise NetworkError(f"网络错误，重试{retry}次后失败：{str(e)}")
                wait_time = 2 ** attempt
                logger.warning(f"请求失败，{wait_time}秒后重试：{str(e)}")
                await asyncio.sleep(wait_time)
    
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None
    ) -> List[Dict]:
        """
        获取 K 线数据
        
        Args:
            symbol: 交易对符号
            interval: 时间间隔 (1m, 5m, 15m, 30m, 1h, 4h, 1d)
            limit: 返回数量限制 (1-1500)
            start_time: 开始时间（毫秒）
            end_time: 结束时间（毫秒）
            
        Returns:
            K 线数据列表
        """
        params = {
            'symbol': symbol,
            'interval': interval,
            'limit': min(limit, 1500)
        }
        
        if start_time:
            params['startTime'] = start_time
        if end_time:
            params['endTime'] = end_time
        
        # K 线数据使用传统合约 API，不使用统一账户 API
        old_rest_base = self.rest_base
        self.rest_base = self.LIVE_REST_BASE
        try:
            data = await self._request('GET', '/fapi/v1/klines', params)
        finally:
            self.rest_base = old_rest_base
        
        # 格式化 K 线数据
        klines = []
        for k in data:
            klines.append({
                'open_time': k[0],
                'open': float(k[1]),
                'high': float(k[2]),
                'low': float(k[3]),
                'close': float(k[4]),
                'volume': float(k[5]),
                'close_time': k[6],
                'quote_volume': float(k[7]),
                'trades': k[8]
            })
        
        return klines
    
    async def get_account_info(self) -> Dict:
        """
        获取账户信息
        
        Returns:
            账户信息
        """
        params = {}
        # 根据账户类型使用不同的端点
        if self.use_unified_account:
            # 统一账户/专业账户使用 papi API
            return await self._request('GET', '/papi/v1/account', params, signed=True)
        else:
            # 传统合约账户使用 fapi API
            return await self._request('GET', '/fapi/v2/account', params, signed=True)
    
    async def get_spot_balance(self, asset: str = 'USDT') -> Optional[float]:
        """
        获取现货账户余额
        
        Args:
            asset: 资产类型，默认 USDT
            
        Returns:
            可用余额
        """
        params = {}
        try:
            # 使用现货 API 端点
            url = f"{self.spot_rest_base}/api/v3/account"
            
            # 生成签名
            params['timestamp'] = self._get_timestamp()
            params['signature'] = self._generate_signature(params)
            
            headers = {'X-MBX-APIKEY': self.api_key}
            
            session = await self._get_session()
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    error_data = await resp.json()
                    logger.error(f"获取现货余额失败：{error_data}")
                    return None
                
                data = await resp.json()
                for balance in data.get('balances', []):
                    if balance['asset'] == asset:
                        return float(balance['free'])
                return 0.0
        except Exception as e:
            logger.error(f"获取现货余额失败：{e}")
            return None
    
    async def transfer_spot_to_umfut(self, asset: str = 'USDT', amount: float = 0.0) -> Dict:
        """
        现货钱包转向合约钱包（全账户模式）
        
        Args:
            asset: 资产类型
            amount: 划转金额（0 表示全部）
            
        Returns:
            划转结果
        """
        if amount <= 0:
            # 先获取现货余额
            amount = await self.get_spot_balance(asset)
            if amount is None or amount <= 0:
                return {'success': False, 'msg': '现货余额不足'}
        
        params = {
            'asset': asset,
            'amount': str(amount),
            'recvWindow': 60000,
            'type': 'MAIN_UMFUTURE',  # 现货账户转向 U 本位合约账户
            'timestamp': self._get_timestamp()
        }
        params['signature'] = self._generate_signature(params)
        
        try:
            # 使用现货 API 端点
            url = f"{self.spot_rest_base}/sapi/v1/account/universalTransfer"
            headers = {'X-MBX-APIKEY': self.api_key}
            
            session = await self._get_session()
            async with session.post(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    error_data = await resp.json()
                    logger.error(f"划转失败：{error_data}")
                    return {'success': False, 'msg': str(error_data)}
                
                result = await resp.json()
                logger.info(f"划转成功：{amount} {asset} 现货 -> 合约")
                logger.info(f"交易 ID: {result.get('tranId')}")
                return {'success': True, 'data': result}
        except Exception as e:
            logger.error(f"划转失败：{e}")
            return {'success': False, 'msg': str(e)}
    
    async def get_umfut_balance(self, asset: str = 'USDT') -> Optional[float]:
        """
        获取 U 本位合约账户余额
        
        Args:
            asset: 资产类型，默认 USDT
            
        Returns:
            可用余额
        """
        try:
            account_info = await self.get_account_info()
            
            # 根据账户类型处理不同的返回格式
            if self.use_unified_account:
                # 统一账户返回 balances 数组
                # {
                #   "balances": [
                #     {
                #       "asset": "USDT",
                #       "availableBalance": "1000.00000000",
                #       "totalBalance": "1000.00000000"
                #     }
                #   ]
                # }
                balances = account_info.get('balances', [])
                for balance in balances:
                    if balance.get('asset') == asset:
                        return float(balance.get('availableBalance', 0))
                return 0.0
            else:
                # 传统合约账户直接返回 availableBalance
                # {
                #   "availableBalance": "0.00000000",
                #   "totalWalletBalance": "0.00000000",
                #   ...
                # }
                return float(account_info.get('availableBalance', 0))
        except Exception as e:
            logger.error(f"获取合约余额失败：{e}")
            return None
    
    async def get_position(self, symbol: str) -> Optional[Dict]:
        """
        获取指定仓位信息
        
        Args:
            symbol: 交易对符号
            
        Returns:
            仓位信息
        """
        # 统一账户使用传统合约 API 获取持仓
        try:
            params = {'timestamp': self._get_timestamp()}
            params['signature'] = self._generate_signature(params)
            headers = {'X-MBX-APIKEY': self.api_key}
            
            session = await self._get_session()
            # 使用传统合约 API 获取持仓
            old_rest_base = self.rest_base
            self.rest_base = self.LIVE_REST_BASE
            try:
                async with session.get(
                    f'{self.LIVE_REST_BASE}/fapi/v2/positionRisk',
                    params=params,
                    headers=headers
                ) as resp:
                    if resp.status == 200:
                        positions = await resp.json()
                        for pos in positions:
                            if pos.get('symbol') == symbol:
                                return {
                                    'symbol': pos['symbol'],
                                    'entry_price': float(pos['entryPrice']),
                                    'position_amt': float(pos['positionAmt']),
                                    'unrealized_profit': float(pos['unRealizedProfit']),
                                    'leverage': int(pos.get('leverage', 1)),
                                    'margin_type': pos.get('marginType', 'ISOLATED')
                                }
                    else:
                        logger.debug(f"获取持仓失败：{await resp.text()}")
            finally:
                self.rest_base = old_rest_base
        except Exception as e:
            logger.debug(f"获取持仓异常：{e}")
        
        return None
    
    async def set_leverage(self, symbol: str, leverage: int) -> Dict:
        """
        设置合约杠杆
        
        Args:
            symbol: 交易对
            leverage: 杠杆倍数 (1-125)
            
        Returns:
            设置结果
        """
        try:
            params = {
                'symbol': symbol,
                'leverage': leverage,
                'timestamp': self._get_timestamp()
            }
            params['signature'] = self._generate_signature(params)
            
            logger.info(f"设置杠杆：{symbol} {leverage}x")
            
            result = await self._request(
                'POST',
                '/fapi/v1/leverage',
                params,
                signed=True
            )
            
            return result
            
        except Exception as e:
            logger.error(f"设置杠杆失败：{str(e)}")
            return {'success': False, 'msg': str(e)}
    
    async def create_grid(self, params: Dict) -> Dict:
        """
        创建合约网格（使用币安官方网格 API）
        
        Args:
            params: 网格参数，包含：
                - symbol: 交易对 (如 BTCUSDT)
                - upper_price: 上边界价格
                - lower_price: 下边界价格
                - grid_count: 网格数量
                - grid_direction: 网格方向 (LONG/SHORT/NEUTRAL)
                - total_investment: 总投资金额 (USDT)
                - leverage: 杠杆倍数 (可选，默认 10)
            
        Returns:
            创建结果：{
                'success': bool,
                'grid_id': str,
                'message': str
            }
        """
        try:
            symbol = params.get('symbol', 'BTCUSDT')
            upper_price = params.get('upper_price')
            lower_price = params.get('lower_price')
            grid_count = params.get('grid_count', 30)
            grid_direction = params.get('grid_direction', 'NEUTRAL')
            total_investment = params.get('total_investment', 1000)
            leverage = params.get('leverage', 10)
            
            # 1. 设置杠杆
            leverage_result = await self.set_leverage(symbol, leverage)
            logger.info(f"设置杠杆：{symbol} {leverage}x")
            
            # 2. 计算每网格投资金额
            investment_per_grid = total_investment / grid_count
            
            # 3. 计算网格间距
            price_range = upper_price - lower_price
            grid_spacing = price_range / grid_count if grid_count > 1 else 0
            
            # 4. 根据网格方向确定持仓模式
            # LONG: 只做多（先买后卖）
            # SHORT: 只做空（先卖后买）
            # NEUTRAL: 双向网格
            position_mode = 'BOTH'  # 单向持仓模式
            
            # 5. 创建网格订单
            # 使用合约网格 API 端点
            grid_params = {
                'symbol': symbol,
                'upperPrice': float(upper_price),
                'lowerPrice': float(lower_price),
                'gridNum': int(grid_count),
                'gridType': 'GEOMETRIC',  # 几何网格（等比）或 ARITHMETIC（等差）
                'investmentAmount': float(total_investment),
                'isReverse': False,  # 是否反向网格
                'leverage': int(leverage)
            }
            
            # 添加持仓方向参数
            if grid_direction == 'LONG':
                grid_params['direction'] = 'LONG'
            elif grid_direction == 'SHORT':
                grid_params['direction'] = 'SHORT'
            else:
                grid_params['direction'] = 'NEUTRAL'
            
            logger.info(
                f"创建网格：symbol={symbol}, 区间=[{lower_price:.2f}, {upper_price:.2f}], "
                f"数量={grid_count}, 方向={grid_direction}, 投资={total_investment:.2f} USDT"
            )
            
            # 调用币安网格 API
            # 注意：币安合约网格 API 端点可能需要特殊权限
            result = await self._request(
                'POST', 
                '/fapi/v1/grid/order', 
                grid_params, 
                signed=True
            )
            
            if result.get('code') == 0 or result.get('status') == 'SUCCEEDED':
                grid_id = result.get('gridId', result.get('orderId', 'unknown'))
                logger.info(f"网格创建成功：grid_id={grid_id}")
                return {
                    'success': True,
                    'grid_id': grid_id,
                    'message': '网格创建成功',
                    'data': result
                }
            else:
                logger.error(f"网格创建失败：{result}")
                return {
                    'success': False,
                    'grid_id': None,
                    'message': result.get('msg', '网格创建失败'),
                    'data': result
                }
                
        except Exception as e:
            logger.error(f"创建网格异常：{str(e)}", exc_info=True)
            return {
                'success': False,
                'grid_id': None,
                'message': f'创建网格异常：{str(e)}',
                'data': None
            }
    
    async def switch_grid(
        self,
        old_grid_id: str,
        symbol: str,
        new_params: Dict
    ) -> Dict:
        """
        切换网格（终止旧网格 + 创建新网格）
        
        **重要**: 币安不支持直接修改网格参数，此方法通过终止旧网格并创建新网格实现参数调整
        
        Args:
            old_grid_id: 当前网格 ID
            symbol: 交易对
            new_params: 新网格参数，包含：
                - upper_price: 上边界价格
                - lower_price: 下边界价格
                - grid_count: 网格数量
                - grid_direction: 网格方向
                - total_investment: 总投资金额
                - leverage: 杠杆倍数
        
        Returns:
            切换结果：{
                'success': bool,
                'message': str,
                'old_grid_profit': float,  # 旧网格实现盈亏
                'new_grid_id': str,
                'data': dict
            }
        """
        try:
            logger.info(f"开始切换网格：symbol={symbol}, old_grid_id={old_grid_id}")
            logger.info(f"新参数：{new_params}")
            
            # 1. 终止旧网格
            logger.info("步骤 1: 终止旧网格")
            terminate_result = await self.terminate_grid(old_grid_id, symbol)
            
            if not terminate_result['success']:
                logger.error(f"终止旧网格失败：{terminate_result['message']}")
                return {
                    'success': False,
                    'message': f'终止旧网格失败：{terminate_result["message"]}',
                    'old_grid_profit': 0,
                    'new_grid_id': None,
                    'data': None
                }
            
            old_profit = terminate_result.get('profit', 0)
            logger.info(f"旧网格终止成功，实现盈亏：{old_profit} USDT")
            
            # 2. 等待订单完成（等待 2 秒）
            logger.info("步骤 2: 等待订单完成...")
            await asyncio.sleep(2)
            
            # 3. 创建新网格
            logger.info("步骤 3: 创建新网格")
            new_grid_params = {
                'symbol': symbol,
                'upper_price': new_params.get('upper_price'),
                'lower_price': new_params.get('lower_price'),
                'grid_count': new_params.get('grid_count', 30),
                'grid_direction': new_params.get('grid_direction', 'NEUTRAL'),
                'total_investment': new_params.get('total_investment', 1000),
                'leverage': new_params.get('leverage', 10)
            }
            
            create_result = await self.create_grid(new_grid_params)
            
            if not create_result['success']:
                logger.error(f"创建新网格失败：{create_result['message']}")
                return {
                    'success': False,
                    'message': f'创建新网格失败：{create_result["message"]}',
                    'old_grid_profit': old_profit,
                    'new_grid_id': None,
                    'data': None
                }
            
            new_grid_id = create_result['grid_id']
            logger.info(f"新网格创建成功：grid_id={new_grid_id}")
            
            return {
                'success': True,
                'message': '网格切换成功',
                'old_grid_profit': old_profit,
                'new_grid_id': new_grid_id,
                'data': {
                    'terminate': terminate_result,
                    'create': create_result
                }
            }
            
        except Exception as e:
            logger.error(f"切换网格异常：{str(e)}", exc_info=True)
            return {
                'success': False,
                'message': f'切换网格异常：{str(e)}',
                'old_grid_profit': 0,
                'new_grid_id': None,
                'data': None
            }
    
    async def modify_grid(
        self,
        grid_id: str,
        symbol: str,
        new_params: Dict
    ) -> Dict:
        """
        修改网格参数（已废弃，使用 switch_grid 替代）
        
        **重要**: 经实测，币安不支持直接修改网格参数的 API
        请使用 switch_grid 方法（终止旧网格 + 创建新网格）
        
        Args:
            grid_id: 网格 ID
            symbol: 交易对
            new_params: 新的网格参数
            
        Returns:
            修改结果
        """
        logger.warning("modify_grid API 已废弃，请使用 switch_grid 方法")
        logger.warning("将使用 switch_grid 方式实现参数调整")
        
        # 自动切换到 switch_grid 方式
        return await self.switch_grid(grid_id, symbol, new_params)
    
    async def terminate_grid(self, grid_id: str, symbol: str = 'BTCUSDT') -> Dict:
        """
        终止合约网格
        
        Args:
            grid_id: 网格 ID
            symbol: 交易对
            
        Returns:
            终止结果：{
                'success': bool,
                'message': str,
                'profit': float (实现盈亏)
            }
        """
        try:
            logger.info(f"终止网格：symbol={symbol}, grid_id={grid_id}")
            
            # 调用币安网格终止 API
            params = {
                'symbol': symbol,
                'gridId': grid_id
            }
            
            result = await self._request(
                'DELETE',
                '/fapi/v1/grid/order',
                params,
                signed=True
            )
            
            if result.get('code') == 0 or result.get('status') == 'SUCCEEDED':
                profit = result.get('realizedProfit', 0)
                logger.info(f"网格终止成功：grid_id={grid_id}, 实现盈亏={profit:.2f} USDT")
                return {
                    'success': True,
                    'message': '网格终止成功',
                    'profit': profit,
                    'data': result
                }
            else:
                logger.error(f"网格终止失败：{result}")
                return {
                    'success': False,
                    'message': result.get('msg', '网格终止失败'),
                    'profit': 0,
                    'data': result
                }
                
        except Exception as e:
            logger.error(f"终止网格异常：{str(e)}", exc_info=True)
            return {
                'success': False,
                'message': f'终止网格异常：{str(e)}',
                'profit': 0,
                'data': None
            }
    
    async def get_grid_status(self, grid_id: str, symbol: str = 'BTCUSDT') -> Dict:
        """
        获取网格状态
        
        Args:
            grid_id: 网格 ID
            symbol: 交易对
            
        Returns:
            网格状态信息
        """
        try:
            params = {
                'symbol': symbol,
                'gridId': grid_id
            }
            
            result = await self._request(
                'GET',
                '/fapi/v1/grid/order',
                params,
                signed=True
            )
            
            if result.get('code') == 0 or result.get('status') == 'SUCCEEDED':
                return {
                    'success': True,
                    'data': result
                }
            else:
                return {
                    'success': False,
                    'message': result.get('msg', '查询失败'),
                    'data': None
                }
                
        except Exception as e:
            logger.error(f"查询网格状态异常：{str(e)}")
            return {
                'success': False,
                'message': f'查询异常：{str(e)}',
                'data': None
            }
    
    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        time_in_force: str = 'GTC'
    ) -> Dict:
        """
        下单
        
        Args:
            symbol: 交易对
            side: 方向 (BUY/SELL)
            order_type: 订单类型 (LIMIT/MARKET/STOP_MARKET)
            quantity: 数量
            price: 价格（限价单需要）
            time_in_force: 有效期 (GTC/IOC/FOK)
            
        Returns:
            订单结果
        """
        params = {
            'symbol': symbol,
            'side': side,
            'type': order_type,
            'timestamp': self._get_timestamp()
        }
        
        if quantity:
            params['quantity'] = quantity
        
        if price:
            params['price'] = price
        
        if order_type == 'LIMIT':
            params['timeInForce'] = time_in_force
        
        return await self._request('POST', '/fapi/v1/order', params, signed=True)
    
    async def cancel_order(self, symbol: str, order_id: str) -> Dict:
        """
        撤销订单
        
        Args:
            symbol: 交易对
            order_id: 订单 ID
            
        Returns:
            撤销结果
        """
        params = {
            'symbol': symbol,
            'orderId': order_id
        }
        
        return await self._request('DELETE', '/fapi/v1/order', params, signed=True)
    
    async def subscribe_market_data(
        self,
        symbol: str,
        callback: Callable[[Dict], None]
    ) -> None:
        """
        订阅市场数据（WebSocket）
        
        Args:
            symbol: 交易对
            callback: 数据回调函数
        """
        self._ws_callbacks.append(callback)
        
        if not self._ws_running:
            asyncio.create_task(self._ws_connect(symbol))
    
    async def _ws_connect(self, symbol: str) -> None:
        """
        WebSocket 连接
        
        Args:
            symbol: 交易对
        """
        self._ws_running = True
        
        while self._ws_running:
            try:
                ws_url = f"{self.ws_base}/ws/{symbol.lower()}@kline_1h"
                
                async with websockets.connect(ws_url) as websocket:
                    self._ws = websocket
                    logger.info(f"WebSocket 已连接：{ws_url}")
                    
                    while self._ws_running:
                        try:
                            message = await asyncio.wait_for(
                                self._ws.recv(),
                                timeout=30
                            )
                            data = json.loads(message)
                            
                            # 调用所有回调
                            for callback in self._ws_callbacks:
                                if asyncio.iscoroutinefunction(callback):
                                    await callback(data)
                                else:
                                    callback(data)
                                    
                        except asyncio.TimeoutError:
                            # 发送心跳
                            await self._ws.ping()
                        except websockets.ConnectionClosed:
                            logger.warning("WebSocket 连接关闭，准备重连")
                            break
                            
            except Exception as e:
                logger.error(f"WebSocket 错误：{str(e)}")
                await asyncio.sleep(5)
        
        self._ws = None
    
    async def stop_ws(self) -> None:
        """停止 WebSocket 连接"""
        self._ws_running = False
        if self._ws:
            await self._ws.close()
        self._ws_callbacks.clear()
    
    async def close(self) -> None:
        """关闭客户端"""
        await self.stop_ws()
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
