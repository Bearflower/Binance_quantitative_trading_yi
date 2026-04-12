"""
币安期货交易 API 模块（PM 账户专用）

提供完整的币安期货交易功能（PM 账户专用接口）：
- 下单（市价/限价）- POST /papi/v1/um/order
- 查询订单 - GET /papi/v1/um/openOrder
- 撤销订单 - DELETE /papi/v1/um/order
- 条件单（止盈止损）- POST /papi/v1/um/conditional/order
- 持仓查询 - GET /papi/v1/um/account
- 账户余额查询 - GET /papi/v1/um/account

PM 账户（投资组合保证金账户）使用 /papi/v1/um/* 接口
"""

import time
import hmac
import hashlib
import requests
from typing import Optional, Dict, Any, List
from datetime import datetime

from utils.logger import logger
from config.settings import settings


class BinanceTradingAPI:
    """币安期货交易 API 客户端"""
    
    def __init__(self):
        """初始化币安交易 API 客户端（支持 PM 账户）"""
        self.api_key = settings.binance_api_key
        # 兼容处理：优先使用 binance_secret_key，如果没有则使用 binance_api_secret
        try:
            self.secret_key = settings.binance_secret_key
        except AttributeError:
            self.secret_key = settings.binance_api_secret
        
        if not self.api_key or not self.secret_key:
            logger.warning("⚠️ 币安 API 密钥未配置，交易功能将不可用")
        
        # 请求配置
        self.timeout = 10
        self.recv_window = 5000  # 5 秒
        
        # PM 账户配置（投资组合保证金账户）
        self.is_pm_account = True  # 默认使用 PM 账户接口
        # PM 账户使用 papi.binance.com，普通账户使用 fapi.binance.com
        self.base_url = "https://papi.binance.com" if self.is_pm_account else "https://fapi.binance.com"
        
        # 精度配置缓存
        self._symbol_precision_cache: Dict[str, Dict[str, int]] = {}
        
        logger.info("✅ 币安交易 API 客户端初始化完成（PM 账户模式）")
    
    def _generate_signature(self, params: Dict[str, Any]) -> str:
        """
        生成签名
        
        Args:
            params: 请求参数字典
            
        Returns:
            HMAC SHA256 签名
        """
        query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def _get_timestamp(self) -> int:
        """获取当前时间戳（毫秒）"""
        return int(time.time() * 1000)
    
    def _prepare_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        准备请求参数（添加时间戳和签名）
        
        Args:
            params: 原始参数
            
        Returns:
            完整参数
        """
        params['timestamp'] = self._get_timestamp()
        params['recvWindow'] = self.recv_window
        params['signature'] = self._generate_signature(params)
        return params
    
    def _make_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        retry: int = 3
    ) -> Optional[Dict[str, Any]]:
        """
        发送 HTTP 请求
        
        Args:
            method: HTTP 方法
            endpoint: API 端点
            params: 请求参数
            signed: 是否需要签名
            retry: 重试次数
            
        Returns:
            响应数据，失败返回 None
        """
        url = f"{self.base_url}{endpoint}"
        headers = {
            'X-MBX-APIKEY': self.api_key,
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        for attempt in range(retry):
            try:
                # 如果是签名请求，初始化 params 并添加签名
                if signed:
                    if params is None:
                        params = {}
                    params = self._prepare_params(params)
                
                if method == "GET":
                    response = requests.get(
                        url,
                        params=params,
                        headers=headers,
                        timeout=self.timeout
                    )
                elif method == "POST":
                    response = requests.post(
                        url,
                        data=params if not signed else None,
                        params=params if signed else None,
                        headers=headers,
                        timeout=self.timeout
                    )
                elif method == "DELETE":
                    response = requests.delete(
                        url,
                        params=params,
                        headers=headers,
                        timeout=self.timeout
                    )
                else:
                    logger.error(f"❌ 不支持的 HTTP 方法：{method}")
                    return None
                
                response_data = response.json()
                
                # 检查错误
                if response.status_code != 200:
                    logger.error(
                        f"❌ 请求失败：{response.status_code}, "
                        f"端点：{endpoint}, "
                        f"错误：{response_data}"
                    )
                    
                    # 如果是签名或时间戳问题，重试
                    if response_data.get('code') in [-1021, -1003] and attempt < retry - 1:
                        logger.warning(f"⚠️ 时间戳或签名错误，{attempt + 1}秒后重试")
                        time.sleep(attempt + 1)
                        continue
                    
                    return None
                
                return response_data
                
            except Exception as e:
                logger.error(f"❌ 请求异常：{e}, 端点：{endpoint}")
                if attempt < retry - 1:
                    time.sleep(attempt + 1)
                else:
                    return None
        
        return None
    
    def get_symbol_precision(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取币种精度信息（增强版 - 带重试和验证）
        
        Args:
            symbol: 币种符号（如 BTCUSDT）
            
        Returns:
            精度字典：{
                'quantity_precision': int,
                'price_precision': int,
                'step_size': float,
                'tick_size': float,
                'min_qty': float,
                'max_qty': float
            }
        """
        # 检查缓存
        if symbol in self._symbol_precision_cache:
            logger.debug(f"📦 使用缓存的精度信息：{symbol}")
            return self._symbol_precision_cache[symbol]
        
        try:
            # 从 exchangeInfo 获取精度
            endpoint = "/fapi/v1/exchangeInfo"
            data = self._make_request("GET", endpoint)
            
            if not data or 'symbols' not in data:
                logger.error(f"❌ 获取交易对信息失败：{symbol}")
                return None
            
            for symbol_info in data['symbols']:
                if symbol_info['symbol'] == symbol:
                    # 获取 filters 中的精度信息（更准确）
                    filters = symbol_info.get('filters', [])
                    
                    # LOT_SIZE filter - 数量精度
                    lot_size_filter = next(
                        (f for f in filters if f.get('filterType') == 'LOT_SIZE'),
                        None
                    )
                    
                    # PRICE_FILTER - 价格精度
                    price_filter = next(
                        (f for f in filters if f.get('filterType') == 'PRICE_FILTER'),
                        None
                    )
                    
                    # 从 filters 提取精度（优先）
                    if lot_size_filter:
                        step_size = float(lot_size_filter.get('stepSize', 0))
                        min_qty = float(lot_size_filter.get('minQty', 0))
                        max_qty = float(lot_size_filter.get('maxQty', 0))
                        # 计算数量精度（step_size 的小数位数）
                        quantity_precision = self._count_decimal_places(step_size)
                    else:
                        # 回退到 symbol_info
                        step_size = float(symbol_info.get('stepSize', 0.001))
                        min_qty = 0.001
                        max_qty = 100000
                        quantity_precision = symbol_info.get('quantityPrecision', 3)
                    
                    if price_filter:
                        tick_size = float(price_filter.get('tickSize', 0))
                        # 计算价格精度（tick_size 的小数位数）
                        price_precision = self._count_decimal_places(tick_size)
                    else:
                        # 回退到 symbol_info
                        tick_size = float(symbol_info.get('tickSize', 0.01))
                        price_precision = symbol_info.get('pricePrecision', 2)
                    
                    # 验证 step_size 和 tick_size
                    if step_size <= 0:
                        logger.warning(f"⚠️ {symbol} step_size 无效：{step_size}，使用默认值 0.001")
                        step_size = 0.001
                    
                    if tick_size <= 0:
                        logger.warning(f"⚠️ {symbol} tick_size 无效：{tick_size}，使用默认值 0.01")
                        tick_size = 0.01
                    
                    precision = {
                        'quantity_precision': quantity_precision,
                        'price_precision': price_precision,
                        'step_size': step_size,
                        'tick_size': tick_size,
                        'min_qty': min_qty,
                        'max_qty': max_qty
                    }
                    
                    self._symbol_precision_cache[symbol] = precision
                    logger.info(
                        f"📊 {symbol} 精度信息：qty_precision={quantity_precision}, "
                        f"step_size={step_size}, price_precision={price_precision}, "
                        f"tick_size={tick_size}"
                    )
                    return precision
            
            logger.warning(f"⚠️ 未找到币种精度信息：{symbol}")
            return None
            
        except Exception as e:
            logger.error(f"❌ 获取精度信息异常：{e}", exc_info=True)
            return None
    
    def _count_decimal_places(self, value: float) -> int:
        """
        计算浮点数的有效小数位数
        
        Args:
            value: 浮点数
            
        Returns:
            小数位数
        """
        if value <= 0:
            return 8
        
        # 转换为字符串，避免浮点数精度问题
        str_value = f"{value:.10f}".rstrip('0').rstrip('.')
        
        if '.' in str_value:
            decimal_part = str_value.split('.')[1]
            return len(decimal_part)
        else:
            return 0
    
    def adjust_quantity(self, symbol: str, quantity: float) -> float:
        """
        根据币种精度调整数量（增强版 - 严格处理 step_size）
        
        Args:
            symbol: 币种符号
            quantity: 原始数量
            
        Returns:
            调整后的数量（确保是 step_size 的整数倍）
        """
        precision_info = self.get_symbol_precision(symbol)
        
        if not precision_info:
            # 默认保留 3 位小数
            logger.warning(f"⚠️ {symbol} 精度信息缺失，使用默认精度 3 位小数")
            return round(quantity, 3)
        
        step_size = precision_info['step_size']
        quantity_precision = precision_info['quantity_precision']
        min_qty = precision_info.get('min_qty', 0.001)
        max_qty = precision_info.get('max_qty', 100000)
        
        # 1. 先向下取整到 step_size 的整数倍（避免超过）
        adjusted_quantity = int(quantity / step_size) * step_size
        
        # 2. 使用 Decimal 避免浮点数精度问题
        from decimal import Decimal, ROUND_DOWN
        
        qty_decimal = Decimal(str(quantity))
        step_decimal = Decimal(str(step_size))
        
        # 3. 计算是 step_size 的多少倍（向下取整）
        multiples = int(qty_decimal / step_decimal)
        
        # 4. 重新计算调整后的数量
        adjusted_decimal = step_decimal * multiples
        
        # 5. 转换为浮点数并保留指定精度
        adjusted_quantity = float(adjusted_decimal.quantize(
            Decimal(10) ** -quantity_precision,
            rounding=ROUND_DOWN
        ))
        
        # 6. 确保不小于最小值
        if adjusted_quantity < min_qty:
            logger.warning(
                f"⚠️ {symbol} 数量 {adjusted_quantity} 小于最小值 {min_qty}，调整为 {min_qty}"
            )
            adjusted_quantity = min_qty
        
        # 7. 确保不大于最大值
        if adjusted_quantity > max_qty:
            logger.warning(
                f"⚠️ {symbol} 数量 {adjusted_quantity} 大于最大值 {max_qty}，调整为 {max_qty}"
            )
            adjusted_quantity = max_qty
        
        # 8. 最终验证：确保是 step_size 的整数倍
        remainder = adjusted_quantity % step_size
        if abs(remainder) > 1e-10:  # 允许极小误差
            logger.warning(
                f"⚠️ {symbol} 调整后数量 {adjusted_quantity} 不是 step_size {step_size} 的整数倍，余数={remainder}"
            )
            # 重新调整
            adjusted_quantity = int(adjusted_quantity / step_size) * step_size
            adjusted_quantity = round(adjusted_quantity, quantity_precision)
        
        logger.info(
            f"🔧 数量调整：{symbol}, "
            f"原始={quantity:.8f} → 调整后={adjusted_quantity:.8f} "
            f"(step_size={step_size}, precision={quantity_precision})"
        )
        
        return adjusted_quantity
    
    def adjust_price(self, symbol: str, price: float) -> float:
        """
        根据币种精度调整价格（增强版 - 严格处理 tick_size）
        
        Args:
            symbol: 币种符号
            price: 原始价格
            
        Returns:
            调整后的价格（确保是 tick_size 的整数倍）
        """
        precision_info = self.get_symbol_precision(symbol)
        
        if not precision_info:
            # 默认保留 2 位小数
            logger.warning(f"⚠️ {symbol} 精度信息缺失，使用默认精度 2 位小数")
            return round(price, 2)
        
        tick_size = precision_info['tick_size']
        price_precision = precision_info['price_precision']
        
        # 1. 使用 Decimal 避免浮点数精度问题
        from decimal import Decimal, ROUND_DOWN
        
        price_decimal = Decimal(str(price))
        tick_decimal = Decimal(str(tick_size))
        
        # 2. 计算是 tick_size 的多少倍（向下取整）
        multiples = int(price_decimal / tick_decimal)
        
        # 3. 重新计算调整后的价格
        adjusted_decimal = tick_decimal * multiples
        
        # 4. 转换为浮点数并保留指定精度
        adjusted_price = float(adjusted_decimal.quantize(
            Decimal(10) ** -price_precision,
            rounding=ROUND_DOWN
        ))
        
        # 5. 最终验证：确保是 tick_size 的整数倍
        remainder = adjusted_price % tick_size
        if abs(remainder) > 1e-10:  # 允许极小误差
            logger.warning(
                f"⚠️ {symbol} 调整后价格 {adjusted_price} 不是 tick_size {tick_size} 的整数倍，余数={remainder}"
            )
            # 重新调整
            adjusted_price = int(adjusted_price / tick_size) * tick_size
            adjusted_price = round(adjusted_price, price_precision)
        
        logger.info(
            f"🔧 价格调整：{symbol}, "
            f"原始={price:.8f} → 调整后={adjusted_price:.8f} "
            f"(tick_size={tick_size}, precision={price_precision})"
        )
        
        return adjusted_price
    
    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        position_side: str = "SHORT",
        reduce_only: bool = False,
        time_in_force: str = "GTC",
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """
        下单（PM 账户专用接口）
        
        PM 账户使用 /papi/v1/um/order 接口
        
        Args:
            symbol: 币种符号（如 BTCUSDT）
            side: 买卖方向（BUY/SELL）
            order_type: 订单类型（MARKET/LIMIT/STOP_MARKET/TAKE_PROFIT_MARKET）
            quantity: 数量
            price: 价格（限价单必填）
            position_side: 持仓方向（BOTH/LONG/SHORT）
            reduce_only: 是否只减仓（默认 False）
            time_in_force: 有效方式（GTC/IOC/FOK，默认 GTC）
            **kwargs: 其他参数（如 stopPrice 等）
            
        Returns:
            订单响应数据，失败返回 None
        """
        logger.info(
            f"📝 下单（PM 账户）: {symbol} {side} {order_type}, "
            f"数量={quantity}, 价格={price}, 持仓方向={position_side}"
        )
        
        # 参数验证
        if not quantity:
            logger.error("❌ 下单失败：数量必填")
            return None
        
        if order_type == "LIMIT" and not price:
            logger.error("❌ 下单失败：限价单必须指定价格")
            return None
        
        # 精度调整
        quantity = self.adjust_quantity(symbol, quantity)
        if price:
            price = self.adjust_price(symbol, price)
        
        # 准备参数
        params = {
            'symbol': symbol,
            'side': side,
            'type': order_type,
            'quantity': quantity,
            'positionSide': position_side,
            'reduceOnly': reduce_only,
        }
        
        if order_type == "LIMIT":
            params['price'] = price
            params['timeInForce'] = time_in_force
        elif order_type in ["STOP_MARKET", "TAKE_PROFIT_MARKET"]:
            params['stopPrice'] = kwargs.get('stopPrice')
            params['price'] = kwargs.get('price', 0)  # 触发后的限价
            params['workingType'] = kwargs.get('workingType', 'MARK_PRICE')
        
        # PM 账户使用 /papi/v1/um/order 接口
        endpoint = "/papi/v1/um/order"
        response = self._make_request("POST", endpoint, params, signed=True)
        
        if response:
            logger.info(
                f"✅ 下单成功（PM 账户）: {symbol}, "
                f"订单 ID={response.get('orderId')}, "
                f"状态={response.get('status')}"
            )
            return response
        else:
            logger.error(f"❌ 下单失败（PM 账户）: {symbol}")
            return None
    
    def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        position_side: str = "SHORT"
    ) -> Optional[Dict[str, Any]]:
        """
        市价单
        
        Args:
            symbol: 币种符号
            side: 买卖方向（BUY/SELL）
            quantity: 数量
            position_side: 持仓方向（BOTH/LONG/SHORT）
            
        Returns:
            订单响应数据
        """
        return self.place_order(
            symbol=symbol,
            side=side,
            order_type="MARKET",
            quantity=quantity,
            position_side=position_side
        )
    
    def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        position_side: str = "SHORT",
        time_in_force: str = "GTC"
    ) -> Optional[Dict[str, Any]]:
        """
        限价单
        
        Args:
            symbol: 币种符号
            side: 买卖方向
            quantity: 数量
            price: 价格
            position_side: 持仓方向
            time_in_force: 有效方式（GTC/IOC/FOK）
            
        Returns:
            订单响应数据
        """
        return self.place_order(
            symbol=symbol,
            side=side,
            order_type="LIMIT",
            quantity=quantity,
            price=price,
            position_side=position_side,
            time_in_force=time_in_force
        )
    
    def place_conditional_order(
        self,
        symbol: str,
        side: str,
        strategy_type: str,
        quantity: Optional[float] = None,
        stop_price: Optional[float] = None,
        price: Optional[float] = None,
        position_side: str = "SHORT",
        reduce_only: bool = False,
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """
        条件单下单（PM 账户专用接口）
        
        PM 账户使用 /papi/v1/um/conditional/order 接口
        
        Args:
            symbol: 币种符号
            side: 买卖方向（BUY/SELL）
            strategy_type: 条件单类型（STOP/STOP_MARKET/TAKE_PROFIT/TAKE_PROFIT_MARKET/TRAILING_STOP_MARKET）
            quantity: 数量
            stop_price: 触发价格（STOP/STOP_MARKET/TAKE_PROFIT/TAKE_PROFIT_MARKET 需要）
            price: 委托价格（STOP/TAKE_PROFIT 需要）
            position_side: 持仓方向
            reduce_only: 是否只减仓
            **kwargs: 其他参数（activationPrice, callbackRate, priceProtect 等）
            
        Returns:
            订单响应数据
        """
        logger.info(
            f"📝 条件单（PM 账户）: {symbol} {side} {strategy_type}, "
            f"数量={quantity}, 触发价={stop_price}, 持仓方向={position_side}"
        )
        
        # 精度调整
        if quantity:
            quantity = self.adjust_quantity(symbol, quantity)
        if price:
            price = self.adjust_price(symbol, price)
        if stop_price:
            stop_price = self.adjust_price(symbol, stop_price)
        
        # 准备参数
        params = {
            'symbol': symbol,
            'side': side,
            'strategyType': strategy_type,
            'positionSide': position_side,
            'reduceOnly': reduce_only,
        }
        
        if quantity:
            params['quantity'] = quantity
        
        if strategy_type in ["STOP", "TAKE_PROFIT"]:
            # 限价条件单
            if not price or not stop_price:
                logger.error(f"❌ 条件单失败：{strategy_type} 必须指定 price 和 stopPrice")
                return None
            params['price'] = price
            params['stopPrice'] = stop_price
            params['timeInForce'] = kwargs.get('timeInForce', 'GTC')
        elif strategy_type in ["STOP_MARKET", "TAKE_PROFIT_MARKET"]:
            # 市价条件单
            if not stop_price:
                logger.error(f"❌ 条件单失败：{strategy_type} 必须指定 stopPrice")
                return None
            params['stopPrice'] = stop_price
            params['price'] = kwargs.get('price', 0)
        elif strategy_type == "TRAILING_STOP_MARKET":
            # 跟踪止损
            params['activationPrice'] = kwargs.get('activationPrice')
            params['callbackRate'] = kwargs.get('callbackRate')
        
        # 其他可选参数
        if 'priceProtect' in kwargs:
            params['priceProtect'] = 'TRUE' if kwargs['priceProtect'] else 'FALSE'
        if 'workingType' in kwargs:
            params['workingType'] = kwargs['workingType']
        
        # PM 账户使用 /papi/v1/um/conditional/order 接口
        endpoint = "/papi/v1/um/conditional/order"
        response = self._make_request("POST", endpoint, params, signed=True)
        
        if response:
            logger.info(
                f"✅ 条件单成功（PM 账户）: {symbol}, "
                f"策略 ID={response.get('strategyId')}, "
                f"状态={response.get('strategyStatus')}"
            )
            return response
        else:
            logger.error(f"❌ 条件单失败（PM 账户）: {symbol}")
            return None
    
    def place_stop_loss_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
        position_side: str = "SHORT",
        price: float = 0
    ) -> Optional[Dict[str, Any]]:
        """
        止损单（STOP_MARKET）- PM 账户专用接口
        
        Args:
            symbol: 币种符号
            side: 买卖方向
            quantity: 数量
            stop_price: 触发价格
            position_side: 持仓方向
            price: 触发后的执行价格（0 表示市价）
            
        Returns:
            订单响应数据
        """
        return self.place_conditional_order(
            symbol=symbol,
            side=side,
            strategy_type="STOP_MARKET",
            quantity=quantity,
            stop_price=stop_price,
            position_side=position_side,
            price=price,
            workingType="MARK_PRICE"
        )
    
    def place_take_profit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_price: float,
        position_side: str = "SHORT",
        price: float = 0
    ) -> Optional[Dict[str, Any]]:
        """
        止盈单（TAKE_PROFIT_MARKET）- PM 账户专用接口
        
        Args:
            symbol: 币种符号
            side: 买卖方向
            quantity: 数量
            stop_price: 触发价格
            position_side: 持仓方向
            price: 触发后的执行价格（0 表示市价）
            
        Returns:
            订单响应数据
        """
        return self.place_conditional_order(
            symbol=symbol,
            side=side,
            strategy_type="TAKE_PROFIT_MARKET",
            quantity=quantity,
            stop_price=stop_price,
            position_side=position_side,
            price=price,
            workingType="MARK_PRICE"
        )
    
    def cancel_order(
        self,
        symbol: str,
        order_id: Optional[int] = None,
        orig_client_order_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        撤销订单（PM 账户专用接口）
        
        PM 账户使用 /papi/v1/um/order 接口
        
        Args:
            symbol: 币种符号
            order_id: 订单 ID
            orig_client_order_id: 客户端自定义订单 ID
            
        Returns:
            撤销响应数据，失败返回 None
        """
        logger.info(f"❌ 撤销订单（PM 账户）: {symbol}, 订单 ID={order_id}")
        
        if not order_id and not orig_client_order_id:
            logger.error("❌ 撤销失败：order_id 或 orig_client_order_id 必填一个")
            return None
        
        params = {
            'symbol': symbol
        }
        
        if order_id:
            params['orderId'] = order_id
        if orig_client_order_id:
            params['origClientOrderId'] = orig_client_order_id
        
        # PM 账户使用 /papi/v1/um/order 接口
        endpoint = "/papi/v1/um/order"
        response = self._make_request("DELETE", endpoint, params, signed=True)
        
        if response:
            logger.info(f"✅ 撤销成功（PM 账户）: {symbol}, 订单 ID={order_id}")
            return response
        else:
            logger.error(f"❌ 撤销失败（PM 账户）: {symbol}, 订单 ID={order_id}")
            return None
    
    def cancel_all_orders(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        撤销所有挂单（PM 账户专用接口）
        
        PM 账户使用 /papi/v1/um/allOpenOrder 接口
        
        Args:
            symbol: 币种符号
            
        Returns:
            撤销响应数据
        """
        logger.info(f"❌ 撤销 {symbol} 所有挂单（PM 账户）")
        
        params = {'symbol': symbol}
        # PM 账户使用 /papi/v1/um/allOpenOrder 接口
        endpoint = "/papi/v1/um/allOpenOrder"
        response = self._make_request("DELETE", endpoint, params, signed=True)
        
        if response:
            logger.info(f"✅ 撤销 {symbol} 所有挂单成功（PM 账户）")
            return response
        else:
            logger.error(f"❌ 撤销 {symbol} 所有挂单失败（PM 账户）")
            return None
    
    def query_order(
        self,
        symbol: str,
        order_id: Optional[int] = None,
        orig_client_order_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        查询订单状态（PM 账户专用接口）
        
        PM 账户使用 /papi/v1/um/openOrder 接口查询挂单
        
        Args:
            symbol: 币种符号
            order_id: 订单 ID
            orig_client_order_id: 客户端自定义订单 ID
            
        Returns:
            订单状态数据，失败返回 None
        """
        logger.debug(f"🔍 查询订单（PM 账户）: {symbol}, 订单 ID={order_id}")
        
        if not order_id and not orig_client_order_id:
            logger.error("❌ 查询失败：order_id 或 orig_client_order_id 必填一个")
            return None
        
        params = {
            'symbol': symbol
        }
        
        if order_id:
            params['orderId'] = order_id
        if orig_client_order_id:
            params['origClientOrderId'] = orig_client_order_id
        
        # PM 账户使用 /papi/v1/um/openOrder 接口查询挂单
        endpoint = "/papi/v1/um/openOrder"
        response = self._make_request("GET", endpoint, params, signed=True)
        
        if response:
            logger.debug(
                f"✅ 查询成功（PM 账户）: {symbol}, "
                f"订单 ID={response.get('orderId')}, "
                f"状态={response.get('status')}, "
                f"均价={response.get('avgPrice')}"
            )
            return response
        else:
            logger.error(f"❌ 查询失败（PM 账户）: {symbol}, 订单 ID={order_id}")
            return None
    
    def query_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        查询当前所有挂单（PM 账户专用接口）
        
        PM 账户使用 /papi/v1/um/openOrder 接口
        
        Args:
            symbol: 币种符号（可选，不传则查询所有币种）
            
        Returns:
            挂单列表
        """
        logger.debug(f"🔍 查询当前挂单（PM 账户）: {symbol or '所有币种'}")
        
        params = {}
        if symbol:
            params['symbol'] = symbol
        
        # PM 账户使用 /papi/v1/um/openOrder 接口
        endpoint = "/papi/v1/um/openOrder"
        response = self._make_request("GET", endpoint, params, signed=True)
        
        if response:
            logger.info(f"✅ 查询挂单成功（PM 账户）, 共 {len(response)} 个")
            return response
        else:
            logger.error(f"❌ 查询挂单失败（PM 账户）")
            return []
    
    def get_account(self) -> Optional[Dict[str, Any]]:
        """
        获取账户信息（PM 账户专用接口）
        
        PM 账户使用 /papi/v1/um/account 接口
        此接口返回账户余额和持仓信息
        
        Returns:
            账户信息，包含：
            - assets: 资产列表
            - positions: 持仓列表
            - totalWalletBalance: 总钱包余额
            - totalUnrealizedProfit: 总未实现盈亏
            - totalMarginBalance: 总保证金余额
            - availableBalance: 可用余额
        """
        logger.debug("🔍 查询账户信息（PM 账户）")
        
        # PM 账户使用 /papi/v1/um/account 接口
        endpoint = "/papi/v1/um/account"
        response = self._make_request("GET", endpoint, None, signed=True)
        
        if response:
            logger.info(
                f"✅ 查询账户信息成功（PM 账户）, "
                f"总权益={response.get('totalWalletBalance')}, "
                f"未实现盈亏={response.get('totalUnrealizedProfit')}"
            )
            return response
        else:
            logger.error(f"❌ 查询账户信息失败（PM 账户）")
            return None
    
    def get_position(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取当前持仓信息（PM 账户专用接口）
        
        PM 账户使用 /papi/v1/um/account 接口返回的 positions 字段
        
        Args:
            symbol: 币种符号（可选，不传则查询所有持仓）
            
        Returns:
            持仓列表，每项包含：
            - symbol: 币种符号
            - positionAmt: 持仓数量
            - entryPrice: 入场价格
            - markPrice: 标记价格
            - unrealizedProfit: 未实现盈亏
            - positionSide: 持仓方向
            - leverage: 杠杆
            - notional: 名义本金
        """
        logger.debug(f"🔍 查询持仓（PM 账户）：{symbol or '所有'}")
        
        # PM 账户使用 /papi/v1/um/account 接口
        account_info = self.get_account()
        
        if not account_info or 'positions' not in account_info:
            logger.error(f"❌ 查询持仓失败（PM 账户）")
            return []
        
        positions = account_info['positions']
        
        # 如果指定了 symbol，过滤
        if symbol:
            positions = [pos for pos in positions if pos.get('symbol') == symbol]
        
        # 过滤掉持仓为 0 的
        positions = [
            pos for pos in positions
            if float(pos.get('positionAmt', 0)) != 0
        ]
        
        logger.info(f"✅ 查询持仓成功（PM 账户），共 {len(positions)} 个持仓")
        return positions
    
    def get_account_balance(self) -> List[Dict[str, Any]]:
        """
        获取账户余额（PM 账户专用接口）
        
        PM 账户使用 /papi/v1/um/account 接口返回的 assets 字段
        
        Returns:
            余额列表，每项包含：
            - asset: 资产名称
            - walletBalance: 钱包余额
            - unrealizedProfit: 未实现盈亏
            - availableBalance: 可用余额
            - crossWalletBalance: 交叉钱包余额
        """
        logger.debug("🔍 查询账户余额（PM 账户）")
        
        # PM 账户使用 /papi/v1/um/account 接口
        account_info = self.get_account()
        
        if not account_info or 'assets' not in account_info:
            logger.error(f"❌ 查询余额失败（PM 账户）")
            return []
        
        assets = account_info['assets']
        logger.info(f"✅ 查询余额成功（PM 账户），共 {len(assets)} 个资产")
        return assets
    
    def set_leverage(
        self,
        symbol: str,
        leverage: int,
        position_side: str = "SHORT"
    ) -> Optional[Dict[str, Any]]:
        """
        设置杠杆倍数（PM 账户专用接口）
        
        PM 账户使用 /papi/v1/um/leverage 接口
        
        Args:
            symbol: 币种符号
            leverage: 杠杆倍数
            position_side: 持仓方向（BOTH/LONG/SHORT）
            
        Returns:
            设置结果
        """
        logger.info(f"📊 设置杠杆（PM 账户）: {symbol}, {leverage}x, 方向={position_side}")
        
        params = {
            'symbol': symbol,
            'leverage': leverage
        }
        
        if position_side:
            params['positionSide'] = position_side
        
        # PM 账户使用 /papi/v1/um/leverage 接口
        endpoint = "/papi/v1/um/leverage"
        response = self._make_request("POST", endpoint, params, signed=True)
        
        if response:
            logger.info(f"✅ 设置杠杆成功（PM 账户）: {symbol}, {leverage}x")
            return response
        else:
            logger.error(f"❌ 设置杠杆失败（PM 账户）: {symbol}, {leverage}x")
            return None
    
    def get_futures_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取 24 小时行情（公开接口，PM 和普通账户通用）
        
        Args:
            symbol: 币种符号
            
        Returns:
            行情数据
        """
        params = {'symbol': symbol}
        endpoint = "/fapi/v1/ticker/24hr"
        response = self._make_request("GET", endpoint, params, signed=False)
        
        if response:
            return response
        else:
            logger.error(f"❌ 获取行情失败：{symbol}")
            return None
    
    def get_mark_price(self, symbol: str) -> Optional[float]:
        """
        获取标记价格（公开接口，PM 和普通账户通用）
        
        Args:
            symbol: 币种符号
            
        Returns:
            标记价格
        """
        params = {'symbol': symbol}
        endpoint = "/fapi/v1/premiumIndex"
        response = self._make_request("GET", endpoint, params, signed=False)
        
        if response:
            mark_price = float(response.get('markPrice', 0))
            logger.debug(f"📊 {symbol} 标记价格：{mark_price}")
            return mark_price
        else:
            logger.error(f"❌ 获取标记价格失败：{symbol}")
            return None


# 全局实例
binance_trading_api = BinanceTradingAPI()
