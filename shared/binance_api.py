"""
币安API封装
提供统一的API调用接口，包含频率控制、错误重试等
支持普通合约账户和PM账户（Portfolio Margin）
"""
import asyncio
import time
from typing import Dict, List, Optional, Any
from decimal import Decimal
import aiohttp
from aiohttp import ContentTypeError
import hmac
import hashlib
from urllib.parse import urlencode
import structlog

from .utils import retry_on_failure


logger = structlog.get_logger()

# 不可重试的币安API错误码（API常量，非业务参数）
# 这些错误无论重试多少次都不会自动恢复
_NON_RETRYABLE_ERROR_CODES = {-2011, -2019, -2021, -2022, -4108, -9999}  # -4108=交割/结算中, -9999=废弃API端点


class BinanceAPIError(Exception):
    """币安API异常"""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class RateLimiter:
    """频率控制器"""
    
    def __init__(self, max_requests: int = 1200, window: int = 60):
        self.max_requests = max_requests
        self.window = window
        self.requests = []
    
    async def acquire(self):
        """获取请求许可"""
        now = time.time()
        
        self.requests = [r for r in self.requests if now - r < self.window]
        
        if len(self.requests) >= self.max_requests:
            wait_time = self.window - (now - self.requests[0])
            logger.warning(f"频率限制，等待 {wait_time:.2f} 秒")
            await asyncio.sleep(wait_time)
        
        self.requests.append(now)


class BinanceClient:
    """币安API客户端，支持普通合约账户和PM账户"""
    
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = False,
        base_url: str = None,
        use_unified_account: bool = True
    ):
        if not api_key or not api_key.strip():
            raise ValueError("API密钥不能为空")
        if not api_secret or not api_secret.strip():
            raise ValueError("API密钥不能为空")
        
        self._api_key = api_key.strip()
        self._api_secret = api_secret.strip()
        self.testnet = testnet
        self.use_unified_account = use_unified_account
        
        if base_url:
            self.base_url = base_url
        elif testnet:
            self.base_url = "https://testnet.binancefuture.com"
        elif use_unified_account:
            self.base_url = "https://papi.binance.com"
        else:
            self.base_url = "https://fapi.binance.com"
        
        self.rate_limiter = RateLimiter()
        self.session: Optional[aiohttp.ClientSession] = None
        self.trade_logger = None  # 统一交易记录器，由外部注入
        
        # 精度信息缓存
        self._symbol_precision_cache: Dict[str, Dict] = {}
        
        logger.info(
            "币安客户端初始化",
            testnet=testnet,
            base_url=self.base_url,
            use_unified_account=use_unified_account,
            api_key=self.api_key
        )
    
    @property
    def api_key(self) -> str:
        if len(self._api_key) <= 8:
            return '*' * len(self._api_key)
        masked_length = len(self._api_key) - 8
        return f"{self._api_key[:4]}{'*' * masked_length}{self._api_key[-4:]}"
    
    @property
    def api_secret(self) -> str:
        if len(self._api_secret) <= 8:
            return '*' * len(self._api_secret)
        masked_length = len(self._api_secret) - 8
        return f"{self._api_secret[:4]}{'*' * masked_length}{self._api_secret[-4:]}"
    
    def _get_endpoint(self, standard_endpoint: str) -> str:
        """
        根据账户类型获取正确的API端点
        
        PM账户使用 /papi/v1/um/* 端点（仅限签名请求）
        普通账户使用 /fapi/v1/* 端点
        
        注意：
        - 对于 PM 独有的端点（如 /papi/v1/um/algo/order 条件单），
          调用方应直接传入正确的 papi 端点，本方法会直接返回不做映射。
        - 公共市场数据端点（ticker/price, klines, depth 等）在 PM API 上不存在，
          不应在此映射，调用方应直接使用 fapi 域名。
        """
        if not self.use_unified_account:
            return standard_endpoint
        
        # 如果端点已经是 PM 格式（如 /papi/v1/um/algo/order），直接返回，无需映射
        # 这避免了将 PM 独有端点误映射为普通端点的问题
        if standard_endpoint.startswith("/papi/"):
            return standard_endpoint
        
        # 注意：仅映射签名请求（交易/账户）端点
        # 公共市场数据端点（ticker/price, klines, depth 等）不应映射，
        # 因为它们不存在于 PM API（返回 404 HTML），应继续使用 fapi 域名
        endpoint_map = {
            # ----- 签名请求端点（交易/账户）：PM 账户需要映射到 papi -----
            "/fapi/v2/balance": "/papi/v1/um/account",
            "/fapi/v1/openOrders": "/papi/v1/um/openOrders",
            "/fapi/v1/order": "/papi/v1/um/order",
            "/fapi/v2/positionRisk": "/papi/v1/um/positionRisk",
            "/fapi/v1/leverage": "/papi/v1/um/leverage",
            # ----- 非签名公共端点（仅限 PM API 确实存在的）-----
            "/fapi/v1/ping": "/papi/v1/time",
            "/fapi/v1/time": "/papi/v1/time",
        }
        
        return endpoint_map.get(standard_endpoint, standard_endpoint)
    
    async def __aenter__(self):
        await self._init_session()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
    
    async def _init_session(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "X-MBX-APIKEY": self._api_key,
                    "Content-Type": "application/json"
                }
            )
    
    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    def set_trade_logger(self, trade_logger) -> None:
        """
        设置统一交易记录器

        Args:
            trade_logger: TradeLogger 实例，设置后每次下单成功自动记录到数据库
        """
        self.trade_logger = trade_logger
        logger.info("交易记录器已注入")
    
    def _generate_signature(self, params: Dict[str, Any]) -> str:
        query_string = urlencode(params)
        signature = hmac.new(
            self._api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    @retry_on_failure(max_retries=3, delay=1.0, non_retryable_codes=_NON_RETRYABLE_ERROR_CODES)
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        signed: bool = True
    ) -> Dict:
        await self._init_session()
        
        await self.rate_limiter.acquire()
        
        if params is None:
            params = {}
        
        if signed:
            endpoint = self._get_endpoint(endpoint)
            params.pop('signature', None)  # 移除旧签名，避免重试时残留导致签名错误
            params['timestamp'] = int(time.time() * 1000)
            params['signature'] = self._generate_signature(params)
            base_url = self.base_url
        else:
            # 公共端点：PM账户优先使用 papi 域名和映射，无映射时回退到 fapi 域名
            if self.use_unified_account:
                mapped_endpoint = self._get_endpoint(endpoint)
                if mapped_endpoint != endpoint:
                    # 有映射关系，使用 papi 域名
                    base_url = self.base_url  # https://papi.binance.com
                    endpoint = mapped_endpoint
                else:
                    # 无映射关系（如 exchangeInfo），回退到 fapi 域名
                    base_url = "https://fapi.binance.com"
            else:
                base_url = "https://fapi.binance.com"
        
        url = f"{base_url}{endpoint}"
        
        # 调试：记录实际请求的URL（仅对公共非签名端点，不影响性能）
        if not signed and 'openInterest' in endpoint:
            logger.debug(f"API请求URL: {url}", symbol=params.get('symbol', ''), base_url=base_url, endpoint=endpoint)
        
        async with self.session.request(method, url, params=params) as response:
            try:
                data = await response.json()
            except ContentTypeError:
                # API返回非JSON内容（如废弃端点返回HTML），转换为非重试错误
                raise BinanceAPIError(-9999, f"API返回非JSON格式(type={response.content_type}), 该端点可能已废弃")
            
            # 检查响应体中的错误码
            # 部分 Binance 端点（如 PM 条件单取消）在 HTTP 200 中返回错误码
            # 注意：有些端点（如 DELETE /papi/v1/um/algo/allOpenOrders）返回 code=200 表示成功
            code = data.get('code', 0) if isinstance(data, dict) else 0
            if code not in (0, 200):
                message = data.get('msg', response.reason) if isinstance(data, dict) else response.reason
                raise BinanceAPIError(code, message)

            if response.status != 200:
                code = data.get('code', response.status) if isinstance(data, dict) else response.status
                message = data.get('msg', response.reason) if isinstance(data, dict) else response.reason
                raise BinanceAPIError(code, message)
            
            return data
    
    async def get_account_balance(self) -> Dict[str, Decimal]:
        """
        获取账户可用余额

        Returns:
            各币种可用余额字典
        """
        if self.use_unified_account:
            # PM账户使用 /papi/v1/account 获取总可用余额（更可靠）
            data = await self._request("GET", "/papi/v1/account", signed=True)
            # PM账户返回单条记录，包含 totalAvailableBalance
            if isinstance(data, dict):
                total_available = data.get('totalAvailableBalance', '0')
                return {'USDT': Decimal(str(total_available))}
        else:
            endpoint = "/fapi/v2/balance"

        data = await self._request("GET", "/fapi/v2/balance" if not self.use_unified_account else "/papi/v1/balance", signed=True)

        balance = {}
        for item in data:
            asset = item['asset']
            # 按优先级尝试多个字段名（兼容不同账户类型）
            available_raw = (
                item.get('availableBalance')
                or item.get('umWalletBalance')
                or item.get('totalWalletBalance')
                or item.get('balance')
                or item.get('free')
            )
            if available_raw is None:
                logger.warning("余额数据缺少可用余额字段", asset=asset, keys=list(item.keys()))
                continue
            available = Decimal(str(available_raw))
            if available > 0:
                balance[asset] = available

        return balance
    
    async def get_account_info(self) -> Dict[str, Any]:
        """
        获取账户信息（包含总保证金余额）
        
        PM账户API返回字段：
        - totalAvailableBalance: 总可用余额
        - accountEquity: 账户权益
        - uniMMR: 统一保证金维持率
        
        Returns:
            账户信息字典，包含 totalMarginBalance, totalUnrealizedProfit 等
        """
        if self.use_unified_account:
            endpoint = "/papi/v1/account"
        else:
            endpoint = "/fapi/v2/account"
        
        data = await self._request("GET", endpoint, signed=True)
        
        if self.use_unified_account:
            total_available = Decimal(str(data.get('totalAvailableBalance', 0)))
            account_equity = Decimal(str(data.get('accountEquity', 0)))
            
            logger.info(
                "PM账户API返回数据",
                endpoint=endpoint,
                totalAvailableBalance=total_available,
                accountEquity=account_equity,
                uniMMR=data.get('uniMMR'),
                accountStatus=data.get('accountStatus'),
                data_keys=list(data.keys())
            )
            
            return {
                'totalMarginBalance': account_equity,
                'totalUnrealizedProfit': Decimal(0),
                'totalWalletBalance': account_equity,
                'availableBalance': total_available,
                'assets': [],
                'positions': []
            }
        else:
            return {
                'totalMarginBalance': Decimal(data.get('totalMarginBalance', 0)),
                'totalUnrealizedProfit': Decimal(data.get('totalUnrealizedProfit', 0)),
                'totalWalletBalance': Decimal(data.get('totalWalletBalance', 0)),
                'availableBalance': Decimal(data.get('availableBalance', 0)),
                'assets': data.get('assets', []),
                'positions': data.get('positions', [])
            }
    
    async def get_ticker_price(self, symbol: str) -> Decimal:
        """
        获取交易对当前价格
        
        公共数据使用 fapi 端点（不需要签名）
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        symbol = symbol.strip().upper()
        
        data = await self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
        
        if not isinstance(data, dict) or 'price' not in data:
            raise ValueError(f"响应数据格式错误: {data}")
        
        return Decimal(str(data['price']))
    
    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """
        获取当前挂单
        """
        params = {}
        if symbol:
            params["symbol"] = symbol.strip().upper()
        
        endpoint = "/papi/v1/um/openOrders" if self.use_unified_account else "/fapi/v1/openOrders"
        data = await self._request("GET", endpoint, params, signed=True)
        
        if not isinstance(data, list):
            raise ValueError(f"响应数据格式错误: 期望列表，实际为 {type(data).__name__}")
        
        return data
    
    async def get_order(self, symbol: str, order_id: int) -> Optional[Dict]:
        """
        查询单个订单状态
        
        Args:
            symbol: 交易对名称
            order_id: 订单ID
        
        Returns:
            订单详情字典，包含 symbol, orderId, status, executedQty, origQty 等字段
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        if not order_id:
            raise ValueError("必须提供 orderId")
        
        params = {
            "symbol": symbol.strip().upper(),
            "orderId": order_id
        }
        
        endpoint = "/papi/v1/um/order" if self.use_unified_account else "/fapi/v1/order"
        return await self._request("GET", endpoint, params, signed=True)
    
    async def get_open_algo_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """
        获取未成交的算法条件单（止盈止损单）

        Binance已于2026年6月底废弃此API端点，返回404 HTML。
        策略已改为基于内存order_id精确管理，不再依赖查询API。
        直接返回空列表，避免触发废弃端点的 -9999 错误日志。

        Args:
            symbol: 交易对名称（可选，不传则查询全部）

        Returns:
            始终返回空列表（API已废弃）
        """
        logger.debug(
            "条件单查询API已废弃，直接返回空列表",
            symbol=symbol
        )
        return []
    
    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: Optional[Decimal] = None,
        price: Optional[Decimal] = None,
        order_type: str = "MARKET",
        **kwargs
    ) -> Dict:
        """
        下单（自动调整精度）
        
        Args:
            symbol: 交易对名称
            side: 买卖方向 (BUY/SELL)
            quantity: 数量（自动调整到stepSize的整数倍）
            price: 价格（自动调整到tickSize的整数倍）
            order_type: 订单类型 (MARKET/LIMIT/STOP/STOP_MARKET/TAKE_PROFIT/TAKE_PROFIT_MARKET)
            **kwargs: 其他参数
        
        Returns:
            订单响应字典
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        symbol = symbol.strip().upper()
        
        if side not in ["BUY", "SELL"]:
            raise ValueError(f"无效的订单方向: {side}")
        
        close_position = str(kwargs.get("closePosition", "")).lower() == "true"
        
        if not close_position and quantity is None:
            raise ValueError("非平仓单必须提供数量")
        
        if not close_position and quantity is not None and quantity <= 0:
            raise ValueError(f"数量必须大于0: {quantity}")
        
        if order_type not in ["MARKET", "LIMIT", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"]:
            raise ValueError(f"无效的订单类型: {order_type}")
        
        if order_type == "LIMIT" and price is None:
            raise ValueError("限价单必须提供价格")
        
        if price is not None and price <= 0:
            raise ValueError(f"价格必须大于0: {price}")
        
        # 检测市价单并记录警告（不拒绝执行，仅提醒）
        if order_type == 'MARKET':
            logger.warning(
                "检测到市价单，建议使用限价单以节省手续费",
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=str(quantity) if quantity else None,
            )
        
        # 获取交易对精度信息
        precision_info = await self.get_symbol_info(symbol)
        step_size = precision_info.get('stepSize', '0.001')
        tick_size = precision_info.get('tickSize', Decimal('0.01'))
        
        # 自动调整数量精度
        adjusted_quantity = None
        if quantity is not None:
            adjusted_quantity = self._adjust_quantity_precision(quantity, step_size)
            logger.debug(
                f"{symbol} 数量精度调整",
                original_quantity=float(quantity),
                adjusted_quantity=float(adjusted_quantity),
                step_size=step_size
            )
        
        # 自动调整价格精度
        adjusted_price = None
        if price is not None:
            adjusted_price = self._adjust_price_precision(price, tick_size)
            logger.debug(
                f"{symbol} 价格精度调整",
                original_price=float(price),
                adjusted_price=float(adjusted_price),
                tick_size=float(tick_size)
            )
        
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
        }
        
        if adjusted_quantity is not None:
            params["quantity"] = str(adjusted_quantity)
        
        if order_type == "LIMIT":
            params["price"] = str(adjusted_price)
            params["timeInForce"] = kwargs.get("timeInForce", "GTC")
        
        for key, value in kwargs.items():
            if key not in params and key not in ("closePosition",):
                params[key] = str(value)

        # 平仓单需要显式传递 closePosition 参数
        if close_position:
            params["closePosition"] = "true"

        # reduce-only 订单
        if kwargs.get("reduce_only"):
            params["reduceOnly"] = "true"

        endpoint = "/papi/v1/um/order" if self.use_unified_account else "/fapi/v1/order"
        result = await self._request("POST", endpoint, params)

        # 自动记录交易到统一交易记录表（失败不影响正常流程）
        if self.trade_logger:
            try:
                await self.trade_logger.log_order(
                    result,
                    symbol,
                    side,
                    order_type
                )
            except Exception as e:
                logger.warning("交易记录失败", error=str(e))

        return result
    
    async def place_conditional_order(
        self,
        symbol: str,
        side: str,
        stop_price: Decimal,
        quantity: Optional[Decimal] = None,
        price: Optional[Decimal] = None,
        order_type: str = "STOP_MARKET",
        working_type: str = "CONTRACT_PRICE",
        **kwargs
    ) -> Dict:
        """
        条件单下单（止盈止损单，自动调整精度）
        
        统一账户使用 /papi/v1/um/algo/order 端点，
        普通合约账户使用 /fapi/v1/order 端点。
        
        Args:
            symbol: 交易对名称
            side: 买卖方向 (BUY/SELL)
            stop_price: 触发价格（自动调整到tickSize的整数倍）
            quantity: 数量（自动调整到stepSize的整数倍）
            price: 限价（自动调整到tickSize的整数倍，仅 STOP/TAKE_PROFIT 类型需要）
            order_type: 条件单类型
                - STOP_MARKET: 止损市价单（默认）
                - STOP: 止损限价单
                - TAKE_PROFIT_MARKET: 止盈市价单
                - TAKE_PROFIT: 止盈限价单
                - TRAILING_STOP_MARKET: 追踪止损市价单
            working_type: 触发价格类型
                - CONTRACT_PRICE: 合约价格（默认）
                - MARK_PRICE: 标记价格
        
        Returns:
            订单响应字典
        """
        # 参数验证
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        symbol = symbol.strip().upper()
        
        if side not in ["BUY", "SELL"]:
            raise ValueError(f"无效的订单方向: {side}")
        
        valid_types = ["STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"]
        if order_type not in valid_types:
            raise ValueError(f"无效的条件单类型: {order_type}，有效类型: {', '.join(valid_types)}")
        
        if working_type not in ["CONTRACT_PRICE", "MARK_PRICE"]:
            raise ValueError(f"无效的触发价格类型: {working_type}，有效类型: CONTRACT_PRICE, MARK_PRICE")
        
        if stop_price <= 0:
            raise ValueError(f"触发价格必须大于0: {stop_price}")
        
        # 限价类型需要提供价格
        if order_type in ("STOP", "TAKE_PROFIT") and price is None:
            raise ValueError(f"{order_type} 类型必须提供限价 price")
        
        # 检查是否平仓单
        close_position = str(kwargs.get("closePosition", "")).lower() == "true"
        
        if not close_position and quantity is None:
            raise ValueError("非平仓条件单必须提供数量")
        
        if not close_position and quantity is not None and quantity <= 0:
            raise ValueError(f"数量必须大于0: {quantity}")
        
        if price is not None and price <= 0:
            raise ValueError(f"价格必须大于0: {price}")
        
        # 获取交易对精度信息
        precision_info = await self.get_symbol_info(symbol)
        step_size = precision_info.get('stepSize', '0.001')
        tick_size = precision_info.get('tickSize', Decimal('0.01'))
        
        # 自动调整触发价格精度
        adjusted_stop_price = self._adjust_price_precision(stop_price, tick_size)

        # 价格合理性校验：防止 stop_price 距离当前价格太近导致 -2021 错误 ⭐
        # 场景：止损单已触发但限价未成交，重新补单时当前价格已越过 stop_price
        if order_type in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"):
            try:
                ticker = await self.get_ticker(symbol)
                current_price = Decimal(str(ticker.get("lastPrice", "0")))
                if current_price > 0:
                    # 最小安全距离：至少 5 个 tick_size 或 0.1%，取较大值
                    min_safe_distance = max(tick_size * 5, current_price * Decimal("0.001"))
                    adjusted_stop_price = self._validate_stop_price(
                        adjusted_stop_price, current_price, side, order_type, min_safe_distance, tick_size, symbol
                    )
            except Exception as e:
                logger.warning("价格校验失败，跳过（不影响订单执行）", symbol=symbol, error=str(e))

        logger.debug(
            f"{symbol} 触发价格精度调整",
            original_stop_price=float(stop_price),
            adjusted_stop_price=float(adjusted_stop_price),
            tick_size=float(tick_size)
        )
        
        # 检测市价条件单并记录警告（不拒绝执行，仅提醒）
        if order_type in ('STOP_MARKET', 'TAKE_PROFIT_MARKET'):
            logger.warning(
                "检测到市价条件单，建议使用限价条件单（STOP/TAKE_PROFIT）",
                symbol=symbol,
                side=side,
                order_type=order_type,
                stop_price=str(adjusted_stop_price),
            )
        
        # 自动调整数量精度
        adjusted_quantity = None
        if quantity is not None:
            adjusted_quantity = self._adjust_quantity_precision(quantity, step_size)
            logger.debug(
                f"{symbol} 数量精度调整",
                original_quantity=float(quantity),
                adjusted_quantity=float(adjusted_quantity),
                step_size=step_size
            )
        
        # 自动调整限价精度
        adjusted_price = None
        if price is not None:
            adjusted_price = self._adjust_price_precision(price, tick_size)
            logger.debug(
                f"{symbol} 限价精度调整",
                original_price=float(price),
                adjusted_price=float(adjusted_price),
                tick_size=float(tick_size)
            )
        
        params = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "workingType": working_type,
        }

        if self.use_unified_account:
            params["algoType"] = "CONDITIONAL"
            params["triggerPrice"] = str(adjusted_stop_price)
            endpoint = "/papi/v1/um/algo/order"

            if close_position:
                params["closePosition"] = "true"
                if adjusted_quantity is not None:
                    params["quantity"] = str(adjusted_quantity)
            elif adjusted_quantity is not None:
                params["quantity"] = str(adjusted_quantity)
        else:
            params["stopPrice"] = str(adjusted_stop_price)
            endpoint = "/fapi/v1/order"

            if not close_position and adjusted_quantity is not None:
                params["quantity"] = str(adjusted_quantity)
        
        if order_type in ("STOP", "TAKE_PROFIT"):
            params["price"] = str(adjusted_price)
            params["timeInForce"] = kwargs.get("timeInForce", "GTC")
        
        # reduce-only 订单（防止反手单）
        if kwargs.get("reduce_only"):
            params["reduceOnly"] = "true"
        
        for key, value in kwargs.items():
            if key not in params:
                params[key] = str(value)

        result = await self._request("POST", endpoint, params)

        # 自动记录条件单交易到统一交易记录表（失败不影响正常流程）
        if self.trade_logger:
            try:
                await self.trade_logger.log_order(
                    result,
                    symbol,
                    side,
                    order_type
                )
            except Exception as e:
                logger.warning("条件单交易记录失败", error=str(e))

        return result

    def _validate_stop_price(
        self,
        stop_price: Decimal,
        current_price: Decimal,
        side: str,
        order_type: str,
        min_safe_distance: Decimal,
        tick_size: Decimal,
        symbol: str,
    ) -> Decimal:
        """
        校验并调整 stop_price，防止因距离当前价格太近导致 -2021 错误
        
        Binance 规则：
        - STOP BUY (short 止损): stop_price > current_price，否则立即触发
        - STOP SELL (long 止损): stop_price < current_price，否则立即触发
        - TAKE_PROFIT BUY (short 止盈): stop_price < current_price，否则立即触发
        - TAKE_PROFIT SELL (long 止盈): stop_price > current_price，否则立即触发
        
        Args:
            stop_price: 原始触发价
            current_price: 当前最新价
            side: BUY/SELL
            order_type: STOP/STOP_MARKET/TAKE_PROFIT/TAKE_PROFIT_MARKET
            min_safe_distance: 最小安全距离
            tick_size: 价格精度
            symbol: 交易对（仅用于日志）
        
        Returns:
            调整后的触发价
        """
        is_stop = order_type in ("STOP", "STOP_MARKET")
        adjusted = stop_price

        if is_stop:
            # STOP 订单：BUY = 做空止损, SELL = 做多止损
            if side == "BUY" and stop_price <= current_price:
                # 做空止损价低于当前价 → 调整为当前价 + 安全距离
                adjusted = current_price + min_safe_distance
                logger.warning(
                    "做空止损价低于当前价，自动调整",
                    symbol=symbol,
                    original=float(stop_price),
                    adjusted=float(adjusted),
                    current_price=float(current_price),
                )
            elif side == "SELL" and stop_price >= current_price:
                # 做多止损价高于当前价 → 调整为当前价 - 安全距离
                adjusted = current_price - min_safe_distance
                logger.warning(
                    "做多止损价高于当前价，自动调整",
                    symbol=symbol,
                    original=float(stop_price),
                    adjusted=float(adjusted),
                    current_price=float(current_price),
                )
        else:
            # TAKE_PROFIT 订单：BUY = 做空止盈, SELL = 做多止盈
            if side == "BUY" and stop_price >= current_price:
                # 做空止盈价高于当前价 → 调整为当前价 - 安全距离
                adjusted = current_price - min_safe_distance
                logger.warning(
                    "做空止盈价高于当前价，自动调整",
                    symbol=symbol,
                    original=float(stop_price),
                    adjusted=float(adjusted),
                    current_price=float(current_price),
                )
            elif side == "SELL" and stop_price <= current_price:
                # 做多止盈价低于当前价 → 调整为当前价 + 安全距离
                adjusted = current_price + min_safe_distance
                logger.warning(
                    "做多止盈价低于当前价，自动调整",
                    symbol=symbol,
                    original=float(stop_price),
                    adjusted=float(adjusted),
                    current_price=float(current_price),
                )

        # 确保调整后的价格至少为 1 个 tick_size
        if adjusted <= 0:
            adjusted = tick_size

        return self._adjust_price_precision(adjusted, tick_size)

    async def get_position(self, symbol: Optional[str] = None) -> List[Dict]:
        """查询持仓"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        
        endpoint = "/papi/v1/um/positionRisk" if self.use_unified_account else "/fapi/v2/positionRisk"
        return await self._request("GET", endpoint, params)
    
    async def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None
    ) -> Dict:
        """
        撤销订单
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        if not order_id and not client_order_id:
            raise ValueError("必须提供 orderId 或 clientOrderId")
        
        if order_id is not None and not str(order_id).strip():
            raise ValueError("订单ID不能为空")
        
        if client_order_id is not None and not str(client_order_id).strip():
            raise ValueError("客户端订单ID不能为空")
        
        params = {"symbol": symbol.strip().upper()}
        
        if order_id:
            params["orderId"] = str(order_id).strip()
        elif client_order_id:
            params["origClientOrderId"] = str(client_order_id).strip()
        
        endpoint = "/papi/v1/um/order" if self.use_unified_account else "/fapi/v1/order"
        return await self._request("DELETE", endpoint, params)
    
    async def cancel_algo_order(
        self,
        symbol: str,
        algo_id: int
    ) -> Dict:
        """
        撤销条件单（仅统一账户支持）
        
        使用 DELETE /papi/v1/um/algo/order 端点取消条件止盈止损单。
        
        Args:
            symbol: 交易对名称
            algo_id: 条件单ID
        
        Returns:
            撤销结果字典，包含 algoId, algoStatus 等字段
        """
        if not self.use_unified_account:
            raise ValueError("撤销条件单仅支持统一账户模式")
        
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        if not algo_id or algo_id <= 0:
            raise ValueError(f"无效的条件单ID: {algo_id}")
        
        params = {
            "symbol": symbol.strip().upper(),
            "algoId": algo_id
        }
        
        endpoint = "/papi/v1/um/algo/order"
        return await self._request("DELETE", endpoint, params)
    
    async def cancel_all_algo_orders(
        self,
        symbol: str
    ) -> Dict:
        """
        批量撤销指定交易对的所有条件单（仅统一账户支持）
        
        使用 DELETE /papi/v1/um/algo/allOpenOrders 端点，一次取消该币种的所有 OPEN 条件单。
        相比逐个取消，此端点更可靠，不会出现"API 返回成功但订单未实际取消"的问题。
        
        Args:
            symbol: 交易对名称
        
        Returns:
            撤销结果字典，code=200 表示成功
        """
        if not self.use_unified_account:
            raise ValueError("批量撤销条件单仅支持统一账户模式")
        
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        params = {
            "symbol": symbol.strip().upper(),
        }
        
        endpoint = "/papi/v1/um/algo/allOpenOrders"
        return await self._request("DELETE", endpoint, params)
    
    async def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 500
    ) -> List[Dict]:
        """
        获取K线数据
        
        公共数据使用 fapi 端点（不需要签名）
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        symbol = symbol.strip().upper()
        
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
        
        data = await self._request("GET", "/fapi/v1/klines", params, signed=False)
        
        if not isinstance(data, list):
            raise ValueError(f"响应数据格式错误: 期望列表，实际为 {type(data).__name__}")
        
        klines = []
        for kline in data:
            if len(kline) >= 12:
                klines.append({
                    'open_time': kline[0],
                    'open': Decimal(str(kline[1])),
                    'high': Decimal(str(kline[2])),
                    'low': Decimal(str(kline[3])),
                    'close': Decimal(str(kline[4])),
                    'volume': Decimal(str(kline[5])),
                    'close_time': kline[6],
                    'quote_volume': Decimal(str(kline[7])),
                    'trades': kline[8],
                    'taker_buy_base': Decimal(str(kline[9])),
                    'taker_buy_quote': Decimal(str(kline[10])),
                })
        
        return klines
    
    async def set_leverage(self, symbol: str, leverage: int) -> Dict:
        """
        设置杠杆倍数
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        if leverage < 1 or leverage > 125:
            raise ValueError(f"杠杆倍数必须在1-125之间: {leverage}")
        
        params = {
            "symbol": symbol.strip().upper(),
            "leverage": leverage
        }
        
        endpoint = "/papi/v1/um/leverage" if self.use_unified_account else "/fapi/v1/leverage"
        return await self._request("POST", endpoint, params)
    
    async def get_exchange_info(self) -> Dict[str, Any]:
        """
        获取交易所信息
        
        公共数据使用 fapi 端点（不需要签名）
        
        Returns:
            交易所信息字典，包含交易对列表、服务器时间等
        """
        data = await self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
        
        if not isinstance(data, dict):
            raise ValueError(f"响应数据格式错误: 期望字典，实际为 {type(data).__name__}")
        
        return data
    
    async def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        """
        获取交易对精度信息（带缓存）
        
        Args:
            symbol: 交易对名称
        
        Returns:
            包含 quantityPrecision, pricePrecision, stepSize, tickSize 等字段的字典
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        symbol = symbol.strip().upper()
        
        # 检查缓存
        if symbol in self._symbol_precision_cache:
            return self._symbol_precision_cache[symbol]
        
        exchange_info = await self.get_exchange_info()
        
        for s in exchange_info.get('symbols', []):
            if s.get('symbol') == symbol:
                filters = {}
                for f in s.get('filters', []):
                    if f['filterType'] == 'LOT_SIZE':
                        step_size = f.get('stepSize', '0.001')
                        filters['stepSize'] = step_size  # v6.16.6：保存原始stepSize供精度截断
                        if '.' in step_size:
                            filters['quantityPrecision'] = len(step_size.split('.')[1].rstrip('0') or '0')
                        else:
                            filters['quantityPrecision'] = 0
                    elif f['filterType'] == 'PRICE_FILTER':
                        tick_size = f.get('tickSize', '0.01')
                        filters['tickSize'] = Decimal(tick_size)
                        if '.' in tick_size:
                            filters['pricePrecision'] = len(tick_size.split('.')[1].rstrip('0') or '0')
                        else:
                            filters['pricePrecision'] = 0
                    elif f['filterType'] == 'MIN_NOTIONAL':
                        filters['minNotional'] = f.get('notional', '5')
                
                # 缓存精度信息
                self._symbol_precision_cache[symbol] = filters
                logger.info(
                    f"{symbol} 精度信息已缓存",
                    quantity_precision=filters.get('quantityPrecision'),
                    price_precision=filters.get('pricePrecision'),
                    step_size=filters.get('stepSize'),
                    tick_size=filters.get('tickSize')
                )
                return filters
        
        logger.warning(f"未找到交易对{symbol}的精度信息，使用默认值")
        default_filters = {
            'quantityPrecision': 3,
            'pricePrecision': 2,
            'stepSize': '0.001',
            'tickSize': Decimal('0.01')
        }
        self._symbol_precision_cache[symbol] = default_filters
        return default_filters
    
    def _adjust_quantity_precision(
        self,
        quantity: Decimal,
        step_size: str
    ) -> Decimal:
        """
        调整数量精度（向下取整到stepSize的整数倍）
        
        注意：数量必须向下取整，避免超出账户余额
        
        Args:
            quantity: 原始数量
            step_size: 步长（如 '0.001'）
        
        Returns:
            调整后的数量
        """
        if not step_size or step_size == '0':
            return quantity
        
        step = Decimal(step_size)
        # 向下取整到stepSize的整数倍
        adjusted = (quantity // step) * step
        
        return adjusted
    
    def _adjust_price_precision(
        self,
        price: Decimal,
        tick_size: Decimal
    ) -> Decimal:
        """
        调整价格精度（四舍五入到tickSize的整数倍）
        
        注意：价格应该四舍五入到最近的tickSize整数倍
        
        Args:
            price: 原始价格
            tick_size: 价格步长
        
        Returns:
            调整后的价格
        """
        if not tick_size or tick_size == 0:
            return price
        
        # 四舍五入到tickSize的整数倍
        # 使用 Decimal 的 quantize 方法进行精确的四舍五入
        # normalize() 去除 tick_size 尾随零（如 0.01000 → 0.01），避免结果精度错误
        normalized = tick_size.normalize()
        adjusted = (price / normalized).quantize(Decimal('1'), rounding='ROUND_HALF_UP') * normalized
        
        return adjusted
    
    async def get_orderbook(self, symbol: str, limit: int = 5) -> Dict[str, Any]:
        """
        获取订单簿深度
        
        公共数据使用 fapi 端点（不需要签名）
        
        Args:
            symbol: 交易对名称
            limit: 深度限制（默认5）
        
        Returns:
            包含 bids 和 asks 的字典，每个元素为 [price, quantity]
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        symbol = symbol.strip().upper()
        
        params = {
            "symbol": symbol,
            "limit": limit
        }
        
        data = await self._request("GET", "/fapi/v1/depth", params, signed=False)
        
        if not isinstance(data, dict) or 'bids' not in data or 'asks' not in data:
            raise ValueError(f"订单簿数据格式错误: {data}")
        
        return data
    
    async def get_funding_rate(self, symbol: str) -> float:
        """
        获取当前资金费率
        
        公共数据使用 fapi 端点（不需要签名）
        
        Args:
            symbol: 交易对名称
        
        Returns:
            资金费率（小数形式）
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        symbol = symbol.strip().upper()
        
        data = await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, signed=False)
        
        if not isinstance(data, dict):
            raise ValueError(f"资金费率数据格式错误: {data}")
        
        last_funding_rate = float(data.get('lastFundingRate', 0))
        
        if last_funding_rate == 0:
            funding_rate_str = data.get('fundingRate', '0')
            last_funding_rate = float(funding_rate_str) if funding_rate_str else 0.0
        
        return last_funding_rate
    
    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        """
        获取24小时价格变动统计
        
        公共数据使用 fapi 端点（不需要签名）
        
        Args:
            symbol: 交易对名称
        
        Returns:
            包含 priceChangePercent, lastPrice, highPrice, lowPrice, volume 等字段的字典
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")
        
        symbol = symbol.strip().upper()
        
        data = await self._request("GET", "/fapi/v1/ticker/24hr", {"symbol": symbol}, signed=False)
        
        if not isinstance(data, dict):
            raise ValueError(f"24hr数据格式错误: {data}")
        
        return data
    
    async def get_income_history(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        income_type: str = "REALIZED_PNL",
        limit: int = 1000
    ) -> List[Dict]:
        """
        获取账户收入/支出流水

        用于日报统计胜率，查询指定时间范围内的已实现盈亏记录。
        仅 PMC 账户模式支持（papi endpoint）。

        Args:
            start_time: 起始时间戳（毫秒），默认当天00:00 UTC
            end_time: 结束时间戳（毫秒），默认当前时间
            income_type: 收入类型，默认 "REALIZED_PNL"
            limit: 最大返回条数（默认1000，最大1000）

        Returns:
            收入记录列表，每条包含 symbol, incomeType, income, time 等字段
        """
        if not self.use_unified_account:
            logger.warning("收入查询仅支持统一账户模式")
            return []

        params = {
            "incomeType": income_type,
            "limit": limit
        }

        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time

        data = await self._request("GET", "/papi/v1/um/income", params, signed=True)

        if not isinstance(data, list):
            return []

        return data

    async def get_order_history(
        self,
        symbol: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1000
    ) -> List[Dict]:
        """
        查询指定交易对的历史订单（含成交状态）

        用于周报统计本周实际成交笔数，解决 trade_records 只记录委托(NEW)
        而无法反映成交(FILLED)的数据断层问题。

        Args:
            symbol: 交易对（如 "BTCUSDT"）
            start_time: 起始时间戳（毫秒）
            end_time: 结束时间戳（毫秒）
            limit: 最大返回条数（默认1000，最大1000）

        Returns:
            订单列表，每条包含 symbol, orderId, status, avgPrice, executedQty 等
        """
        if not self.use_unified_account:
            logger.warning("订单历史查询仅支持统一账户模式")
            return []

        params = {
            "symbol": symbol.upper(),
            "limit": limit
        }

        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time

        data = await self._request("GET", "/papi/v1/um/allOrders", params, signed=True)

        if not isinstance(data, list):
            return []

        return data

    async def get_open_interest_hist(
        self,
        symbol: str,
        period: str = "5m",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100
    ) -> List[Dict]:
        """
        获取持仓量（OI）历史数据

        GET /futures/data/openInterestHist

        Args:
            symbol: 交易对
            period: 数据周期（5m/15m/30m/1h/2h/4h/6h/12h/1d）
            start_time: 起始时间戳（毫秒）
            end_time: 结束时间戳（毫秒）
            limit: 最大返回条数（默认100，最大500）

        Returns:
            OI历史数据列表，每条包含 symbol, sumOpenInterest, sumOpenInterestValue, timestamp
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")

        symbol = symbol.strip().upper()

        params = {
            "symbol": symbol,
            "period": period,
            "limit": min(limit, 500),
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time

        data = await self._request("GET", "/futures/data/openInterestHist", params, signed=False)

        if not isinstance(data, list):
            return []

        return data

    async def get_funding_rate_history(
        self,
        symbol: str,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 100
    ) -> List[Dict]:
        """
        获取历史资金费率数据

        GET /fapi/v1/fundingRate

        用于获取指定时间戳对应的历史费率快照，实现 HRS 策略的
        "使用历史费率快照而非实时费率" 的要求。

        Args:
            symbol: 交易对
            start_time: 起始时间戳（毫秒）
            end_time: 结束时间戳（毫秒）
            limit: 最大返回条数（默认100，最大1000）

        Returns:
            历史费率列表，每条包含 symbol, fundingRate, fundingTime, markPrice
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")

        symbol = symbol.strip().upper()

        params = {
            "symbol": symbol,
            "limit": min(limit, 1000),
        }
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time

        data = await self._request("GET", "/fapi/v1/fundingRate", params, signed=False)

        if not isinstance(data, list):
            return []

        return data

    async def get_all_tickers(self) -> List[Dict]:
        """
        获取所有交易对的24小时价格变动统计

        GET /fapi/v1/ticker/24hr

        Returns:
            所有交易对的24hr统计列表
        """
        data = await self._request("GET", "/fapi/v1/ticker/24hr", signed=False)

        if not isinstance(data, list):
            return []

        return data

    async def get_open_interest(self, symbol: str) -> float:
        """
        获取当前持仓量（OI）

        GET /fapi/v1/openInterest

        Args:
            symbol: 交易对

        Returns:
            持仓量（合约张数）
        """
        if not symbol or not symbol.strip():
            raise ValueError("交易对不能为空")

        symbol = symbol.strip().upper()

        data = await self._request("GET", "/fapi/v1/openInterest", {"symbol": symbol}, signed=False)

        if not isinstance(data, dict):
            return 0.0

        return float(data.get("openInterest", 0))
