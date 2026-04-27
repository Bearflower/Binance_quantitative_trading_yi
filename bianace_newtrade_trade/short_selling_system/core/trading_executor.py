"""
交易执行模块

负责：
- 执行做空交易
- 设置止损止盈
- 持仓监控
- 移动止盈
- 时间止损
- 订单管理（查询、撤销）
"""

import time
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta

from utils.logger import logger
from config.settings import settings
from short_selling_system.core.binance_trading_api import binance_trading_api


class TradingExecutor:
    """交易执行器"""
    
    def __init__(self):
        """初始化交易执行器"""
        # 从配置加载默认参数
        self.default_position_size = settings.default_position_size  # 4.0 USDT
        self.default_leverage = settings.default_leverage  # 5 倍
        self.default_stop_loss_percent = settings.default_stop_loss_percent  # 5%
        self.default_take_profit_percent_1 = settings.default_take_profit_percent_1  # 20%
        self.default_take_profit_percent_2 = settings.default_take_profit_percent_2  # 30%
        self.max_holding_hours = settings.max_holding_hours  # 24 小时
        
        # 持仓记录
        self.positions: Dict[str, Dict[str, Any]] = {}
        
        # 订单记录
        self.orders: Dict[str, Dict[str, Any]] = {}
        
        logger.info("✅ 交易执行器初始化完成")
    
    def calculate_atr(self, klines: List[Dict[str, Any]], period: Optional[int] = None) -> float:
        """
        计算 ATR（Average True Range）
        
        Args:
            klines: K线数据列表，需包含 high, low, close 字段
            period: ATR 计算周期（默认使用配置值）
            
        Returns:
            ATR 值，失败返回 0
        """
        atr_period = period or settings.atr_period
        
        if not klines or len(klines) < atr_period + 1:
            logger.warning(f"K线数据不足，无法计算 ATR（需要 {atr_period + 1} 条，实际 {len(klines) if klines else 0} 条）")
            return 0.0
        
        try:
            # 计算 True Range
            true_ranges = []
            for i in range(1, len(klines)):
                high = float(klines[i].get('high', 0))
                low = float(klines[i].get('low', 0))
                prev_close = float(klines[i-1].get('close', 0))
                
                tr = max(
                    high - low,
                    abs(high - prev_close),
                    abs(low - prev_close)
                )
                true_ranges.append(tr)
            
            # 计算 ATR（简单移动平均）
            if len(true_ranges) < atr_period:
                return 0.0
            
            # 使用最近 period 个 TR 值计算 ATR
            recent_trs = true_ranges[-atr_period:]
            atr = sum(recent_trs) / atr_period
            
            logger.debug(f"📊 ATR 计算：周期={atr_period}, ATR={atr:.4f}")
            return atr
        except Exception as e:
            logger.error(f"ATR 计算失败：{e}")
            return 0.0
    
    def calculate_atr_stop_loss(self, entry_price: float, atr: float) -> float:
        """
        计算 ATR 止损价（V4.1.1）
        
        Args:
            entry_price: 入场价格
            atr: ATR 值
            
        Returns:
            止损价格（做空：入场价 + ATR 倍数）
        """
        stop_distance = atr * settings.stop_loss_atr_multiplier
        stop_loss = entry_price + stop_distance
        
        logger.info(
            f"📊 V4.1.1 ATR 止损：入场={entry_price:.4f}, "
            f"ATR={atr:.4f}, 倍数={settings.stop_loss_atr_multiplier}, "
            f"止损价={stop_loss:.4f}"
        )
        
        return stop_loss
    
    def calculate_atr_take_profit(self, entry_price: float, atr: float) -> float:
        """
        计算 ATR 止盈价（V4.1.1）
        
        Args:
            entry_price: 入场价格
            atr: ATR 值
            
        Returns:
            止盈价格（做空：入场价 - ATR 倍数）
        """
        tp_distance = atr * settings.take_profit_atr_multiplier
        take_profit = entry_price - tp_distance
        
        logger.info(
            f"📊 V4.1.1 ATR 止盈：入场={entry_price:.4f}, "
            f"ATR={atr:.4f}, 倍数={settings.take_profit_atr_multiplier}, "
            f"止盈价={take_profit:.4f}"
        )
        
        return take_profit
    
    def calculate_stop_loss(
        self,
        entry_price: float,
        recent_high: Optional[float] = None,
        stop_loss_percent: Optional[float] = None
    ) -> float:
        """
        计算止损价
        
        Args:
            entry_price: 入场价格
            recent_high: 近期高点（可选）
            stop_loss_percent: 止损比例（可选，默认 5%）
            
        Returns:
            止损价格
            
        计算规则:
            - 固定止损：入场价上方 3-5%
            - 技术止损：近期高点上方 1-2%
            - 取两者中更宽松的值
        """
        percent = stop_loss_percent or self.default_stop_loss_percent
        
        # 固定止损
        fixed_stop = entry_price * (1 + percent)
        
        # 技术止损（如果有近期高点）
        if recent_high:
            technical_stop = recent_high * 1.02  # 高点上方 2%
            # 取更宽松的值
            stop_loss = max(fixed_stop, technical_stop)
        else:
            stop_loss = fixed_stop
        
        logger.debug(
            f"📊 止损价计算：入场={entry_price:.4f}, "
            f"固定={fixed_stop:.4f}, 技术={technical_stop if recent_high else 'N/A'}, "
            f"最终={stop_loss:.4f}"
        )
        
        return stop_loss
    
    def calculate_take_profit(
        self,
        entry_price: float,
        take_profit_percent_1: Optional[float] = None,
        take_profit_percent_2: Optional[float] = None
    ) -> tuple:
        """
        计算止盈价
        
        Args:
            entry_price: 入场价格
            take_profit_percent_1: 第一止盈比例（可选，默认 20%）
            take_profit_percent_2: 第二止盈比例（可选，默认 30%）
            
        Returns:
            (take_profit_1, take_profit_2) 元组
        """
        percent_1 = take_profit_percent_1 or self.default_take_profit_percent_1
        percent_2 = take_profit_percent_2 or self.default_take_profit_percent_2
        
        take_profit_1 = entry_price * (1 - percent_1)
        take_profit_2 = entry_price * (1 - percent_2)
        
        logger.debug(
            f"📊 止盈价计算：入场={entry_price:.4f}, "
            f"止盈 1={take_profit_1:.4f} (-{percent_1:.1%}), "
            f"止盈 2={take_profit_2:.4f} (-{percent_2:.1%})"
        )
        
        return take_profit_1, take_profit_2
    
    def execute_short_trade(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: Optional[float] = None,
        take_profit_1: Optional[float] = None,
        take_profit_2: Optional[float] = None,
        quantity: Optional[float] = None,
        leverage: Optional[int] = None,
        reason: str = "",
        klines: Optional[List[Dict[str, Any]]] = None
    ) -> Optional[str]:
        """
        执行做空交易（使用币安交易 API）
        
        Args:
            symbol: 币种符号
            entry_price: 入场价格
            stop_loss: 止损价（可选，自动计算）
            take_profit_1: 第一止盈价（可选，自动计算）
            take_profit_2: 第二止盈价（可选，自动计算）
            quantity: 开仓数量（可选，自动计算）
            leverage: 杠杆倍数（可选，默认 5 倍）
            reason: 开仓原因
            klines: K线数据（可选，用于 ATR 计算）
            
        Returns:
            订单 ID，失败返回 None
        """
        logger.info(f"🎯 准备执行做空交易：{symbol}")
        
        try:
            # 参数验证
            if not symbol or not entry_price:
                logger.error("❌ 参数错误：symbol 和 entry_price 必填")
                return None
            
            # 使用默认值
            leverage = leverage or self.default_leverage
            
            # V4.1.1: 优先使用 ATR 止损止盈
            if settings.use_atr_sl_tp and klines:
                atr = self.calculate_atr(klines)
                if atr > 0:
                    # 使用 ATR 计算止损止盈
                    if not stop_loss:
                        stop_loss = self.calculate_atr_stop_loss(entry_price, atr)
                    
                    if not take_profit_1 or not take_profit_2:
                        tp = self.calculate_atr_take_profit(entry_price, atr)
                        take_profit_1 = take_profit_1 or tp
                        take_profit_2 = take_profit_2 or tp
                else:
                    logger.warning(f"ATR 计算失败，使用传统百分比止损止盈")
            
            # 如果未提供或 ATR 计算失败，使用传统百分比计算
            if not stop_loss:
                stop_loss = self.calculate_stop_loss(entry_price)
            
            if not take_profit_1 or not take_profit_2:
                tp1, tp2 = self.calculate_take_profit(entry_price)
                take_profit_1 = take_profit_1 or tp1
                take_profit_2 = take_profit_2 or tp2
            
            # 计算开仓数量（如果未提供）
            if not quantity:
                position_size = self.default_position_size
                quantity = (position_size * leverage) / entry_price
            
            logger.info(
                f"📊 交易参数："
                f"币种={symbol}, 入场={entry_price:.4f}, "
                f"数量={quantity:.4f}, 杠杆={leverage}x"
            )
            logger.info(
                f"📊 风控参数："
                f"止损={stop_loss:.4f} (+{(stop_loss/entry_price-1)*100:.1f}%), "
                f"止盈 1={take_profit_1:.4f} (-{(entry_price-take_profit_1)/entry_price*100:.1f}%), "
                f"止盈 2={take_profit_2:.4f} (-{(entry_price-take_profit_2)/entry_price*100:.1f}%)"
            )
            
            # 1. 设置杠杆
            binance_trading_api.set_leverage(symbol, leverage, "SHORT")
            
            # 2. 开空单（市价单）
            order_result = binance_trading_api.place_market_order(
                symbol=symbol,
                side="SELL",
                quantity=quantity,
                position_side="SHORT"
            )
            
            if not order_result:
                logger.error(f"❌ 开仓失败：{symbol}")
                return None
            
            # 检查订单状态
            order_status = order_result.get('status')
            if order_status not in ['FILLED', 'PARTIALLY_FILLED']:
                logger.warning(f"⚠️ 订单未完全成交：{order_status}")
            
            order_id = str(order_result.get('orderId'))
            filled_qty = float(order_result.get('executedQty', 0))
            avg_price = float(order_result.get('avgPrice', entry_price))
            
            logger.info(
                f"✅ 开仓成功：{symbol}, "
                f"订单 ID={order_id}, "
                f"成交数量={filled_qty:.4f}, "
                f"成交均价={avg_price:.4f}"
            )
            
            # 3. 设置止损单（STOP_MARKET）
            stop_order = binance_trading_api.place_stop_loss_order(
                symbol=symbol,
                side="BUY",
                quantity=filled_qty,
                stop_price=stop_loss,
                position_side="SHORT"
            )
            
            if stop_order:
                stop_order_id = str(stop_order.get('orderId'))
                logger.info(f"✅ 止损单设置成功：{stop_order_id}, 触发价={stop_loss:.4f}")
            else:
                logger.warning(f"⚠️ 止损单设置失败：{symbol}")
                stop_order_id = None
            
            # 4. 设置止盈单（TAKE_PROFIT_MARKET）
            # 第一止盈位：平仓 50%
            tp1_qty = filled_qty * 0.5
            tp1_order = binance_trading_api.place_take_profit_order(
                symbol=symbol,
                side="BUY",
                quantity=tp1_qty,
                stop_price=take_profit_1,
                position_side="SHORT"
            )
            
            if tp1_order:
                tp1_order_id = str(tp1_order.get('orderId'))
                logger.info(f"✅ 第一止盈单设置成功：{tp1_order_id}, 触发价={take_profit_1:.4f}")
            else:
                logger.warning(f"⚠️ 第一止盈单设置失败：{symbol}")
                tp1_order_id = None
            
            # 第二止盈位：平仓剩余
            tp2_qty = filled_qty - tp1_qty
            tp2_order = binance_trading_api.place_take_profit_order(
                symbol=symbol,
                side="BUY",
                quantity=tp2_qty,
                stop_price=take_profit_2,
                position_side="SHORT"
            )
            
            if tp2_order:
                tp2_order_id = str(tp2_order.get('orderId'))
                logger.info(f"✅ 第二止盈单设置成功：{tp2_order_id}, 触发价={take_profit_2:.4f}")
            else:
                logger.warning(f"⚠️ 第二止盈单设置失败：{symbol}")
                tp2_order_id = None
            
            # 5. 记录持仓和订单
            self.positions[symbol] = {
                'order_id': order_id,
                'symbol': symbol,
                'entry_price': avg_price,
                'quantity': filled_qty,
                'leverage': leverage,
                'stop_loss': stop_loss,
                'stop_loss_order_id': stop_order_id,
                'take_profit_1': take_profit_1,
                'take_profit_1_order_id': tp1_order_id,
                'take_profit_2': take_profit_2,
                'take_profit_2_order_id': tp2_order_id,
                'entry_time': datetime.now(),
                'status': 'open',
                'reason': reason
            }
            
            # 记录订单
            self.orders[order_id] = {
                'symbol': symbol,
                'side': 'SELL',
                'type': 'MARKET',
                'quantity': quantity,
                'avg_price': avg_price,
                'status': order_status,
                'created_at': datetime.now()
            }
            
            if stop_order_id:
                self.orders[stop_order_id] = {
                    'symbol': symbol,
                    'side': 'BUY',
                    'type': 'STOP_MARKET',
                    'quantity': filled_qty,
                    'stop_price': stop_loss,
                    'status': stop_order.get('status'),
                    'created_at': datetime.now()
                }
            
            logger.info(f"✅ 交易执行完成，订单 ID: {order_id}")
            return order_id
            
        except Exception as e:
            logger.error(f"❌ 交易执行失败：{e}", exc_info=True)
            return None
    
    def close_position(
        self,
        symbol: str,
        exit_price: Optional[float] = None,
        reason: str = "manual",
        quantity: Optional[float] = None
    ) -> Optional[float]:
        """
        平仓（使用币安交易 API）
        
        Args:
            symbol: 币种符号
            exit_price: 平仓价格（可选，市价平仓）
            reason: 平仓原因 (manual/stop_loss/take_profit/time)
            quantity: 平仓数量（可选，默认全部）
            
        Returns:
            盈亏金额（USDT），失败返回 None
        """
        if symbol not in self.positions:
            logger.error(f"❌ 未找到持仓：{symbol}")
            return None
        
        position = self.positions[symbol]
        
        if position['status'] != 'open':
            logger.error(f"❌ 持仓已关闭：{symbol}")
            return None
        
        try:
            # 获取持仓数量
            position_qty = position['quantity']
            close_qty = quantity or position_qty
            
            # 获取当前价格（如果未提供）
            if not exit_price:
                ticker = binance_trading_api.get_mark_price(symbol)
                if not ticker:
                    logger.error(f"❌ 获取标记价格失败：{symbol}")
                    return None
                exit_price = ticker
            
            # 执行平仓（市价买单）
            order_result = binance_trading_api.place_market_order(
                symbol=symbol,
                side="BUY",
                quantity=close_qty,
                position_side="SHORT"
            )
            
            if not order_result:
                logger.error(f"❌ 平仓失败：{symbol}")
                return None
            
            order_status = order_result.get('status')
            filled_qty = float(order_result.get('executedQty', 0))
            avg_price = float(order_result.get('avgPrice', exit_price))
            
            logger.info(
                f"✅ 平仓成功：{symbol}, "
                f"数量={filled_qty:.4f}, "
                f"成交均价={avg_price:.4f}, "
                f"原因={reason}"
            )
            
            # 计算盈亏
            # 做空盈亏 = (入场价 - 出场价) × 数量 × 杠杆
            profit_loss = (
                (position['entry_price'] - avg_price) * filled_qty * position['leverage']
            )
            
            logger.info(
                f"📊 平仓盈亏：{symbol}, "
                f"盈亏={profit_loss:.2f} USDT, "
                f"原因={reason}"
            )
            
            # 更新持仓状态
            position['exit_price'] = avg_price
            position['profit_loss'] = profit_loss
            position['exit_time'] = datetime.now()
            position['status'] = 'closed'
            position['close_reason'] = reason
            
            # 记录平仓订单
            order_id = str(order_result.get('orderId'))
            self.orders[order_id] = {
                'symbol': symbol,
                'side': 'BUY',
                'type': 'MARKET',
                'quantity': filled_qty,
                'avg_price': avg_price,
                'status': order_status,
                'close_reason': reason,
                'created_at': datetime.now()
            }
            
            return profit_loss
            
        except Exception as e:
            logger.error(f"❌ 平仓失败：{e}", exc_info=True)
            return None
    
    def check_stop_loss(
        self,
        symbol: str,
        current_price: float
    ) -> Optional[float]:
        """
        检查是否触发止损
        
        Args:
            symbol: 币种符号
            current_price: 当前价格
            
        Returns:
            平仓盈亏，未触发返回 None
        """
        if symbol not in self.positions:
            return None
        
        position = self.positions[symbol]
        
        if position['status'] != 'open':
            return None
        
        # 做空：价格上涨触及止损
        if current_price >= position['stop_loss']:
            logger.warning(
                f"⚠️ 触发止损：{symbol}, "
                f"当前价={current_price:.4f}, 止损价={position['stop_loss']:.4f}"
            )
            return self.close_position(symbol, current_price, reason='stop_loss')
        
        return None
    
    def check_take_profit(
        self,
        symbol: str,
        current_price: float
    ) -> Optional[float]:
        """
        检查是否触发止盈
        
        Args:
            symbol: 币种符号
            current_price: 当前价格
            
        Returns:
            平仓盈亏，未触发返回 None
        """
        if symbol not in self.positions:
            return None
        
        position = self.positions[symbol]
        
        if position['status'] != 'open':
            return None
        
        # 做空：价格下跌触及止盈
        if current_price <= position['take_profit_2']:
            # 第二止盈位，全部平仓
            logger.info(
                f"✅ 触发第二止盈：{symbol}, "
                f"当前价={current_price:.4f}, 止盈价={position['take_profit_2']:.4f}"
            )
            return self.close_position(symbol, current_price, reason='take_profit_2')
        
        elif current_price <= position['take_profit_1']:
            # 第一止盈位，平仓 50%
            logger.info(
                f"✅ 触发第一止盈：{symbol}, "
                f"当前价={current_price:.4f}, 止盈价={position['take_profit_1']:.4f}"
            )
            # TODO: 实现部分平仓
            return self.close_position(symbol, current_price, reason='take_profit_1')
        
        return None
    
    def check_time_stop(
        self,
        symbol: str
    ) -> Optional[float]:
        """
        检查是否触发时间止损
        
        Args:
            symbol: 币种符号
            
        Returns:
            平仓盈亏，未触发返回 None
        """
        if symbol not in self.positions:
            return None
        
        position = self.positions[symbol]
        
        if position['status'] != 'open':
            return None
        
        # 检查持仓时间
        holding_time = datetime.now() - position['entry_time']
        
        if holding_time.total_seconds() > self.max_holding_hours * 3600:
            logger.warning(
                f"⏰ 触发时间止损：{symbol}, "
                f"持仓时间={holding_time.total_seconds()/3600:.1f}小时, "
                f"最大允许={self.max_holding_hours}小时"
            )
            # 使用当前市价平仓
            # TODO: 获取当前价格
            return self.close_position(symbol, position['entry_price'], reason='time_stop')
        
        return None
    
    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取持仓信息"""
        return self.positions.get(symbol)
    
    def get_open_positions(self) -> list:
        """获取所有未平仓位"""
        return [
            pos for pos in self.positions.values()
            if pos['status'] == 'open'
        ]
    
    def get_position_count(self) -> Dict[str, int]:
        """获取持仓统计"""
        total = len(self.positions)
        open_count = len(self.get_open_positions())
        closed_count = total - open_count
        
        return {
            'total': total,
            'open': open_count,
            'closed': closed_count
        }
    
    def query_order(self, symbol: str, order_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        查询订单状态
        
        Args:
            symbol: 币种符号
            order_id: 订单 ID（可选，从持仓获取）
            
        Returns:
            订单状态数据，失败返回 None
        """
        try:
            # 如果未提供 order_id，从持仓获取
            if not order_id:
                if symbol not in self.positions:
                    logger.error(f"❌ 未找到持仓：{symbol}")
                    return None
                order_id = int(self.positions[symbol].get('order_id'))
            
            # 调用 API 查询
            order_data = binance_trading_api.query_order(symbol, order_id)
            
            if order_data:
                logger.info(f"✅ 订单查询成功：{symbol}, ID={order_id}")
                return order_data
            else:
                logger.error(f"❌ 订单查询失败：{symbol}, ID={order_id}")
                return None
                
        except Exception as e:
            logger.error(f"❌ 订单查询异常：{e}", exc_info=True)
            return None
    
    def cancel_order(self, symbol: str, order_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        撤销订单
        
        Args:
            symbol: 币种符号
            order_id: 订单 ID（可选，从持仓获取）
            
        Returns:
            撤销结果，失败返回 None
        """
        try:
            # 如果未提供 order_id，从持仓获取
            if not order_id:
                if symbol not in self.positions:
                    logger.error(f"❌ 未找到持仓：{symbol}")
                    return None
                # 获取止损止盈订单 ID
                position = self.positions[symbol]
                order_ids = []
                if position.get('stop_loss_order_id'):
                    order_ids.append(int(position['stop_loss_order_id']))
                if position.get('take_profit_1_order_id'):
                    order_ids.append(int(position['take_profit_1_order_id']))
                if position.get('take_profit_2_order_id'):
                    order_ids.append(int(position['take_profit_2_order_id']))
                
                # 撤销所有关联订单
                results = []
                for oid in order_ids:
                    result = binance_trading_api.cancel_order(symbol, oid)
                    if result:
                        results.append(result)
                        logger.info(f"✅ 撤销订单成功：{symbol}, ID={oid}")
                    else:
                        logger.warning(f"⚠️ 撤销订单失败：{symbol}, ID={oid}")
                
                return {'results': results} if results else None
            
            # 撤销指定订单
            result = binance_trading_api.cancel_order(symbol, order_id)
            
            if result:
                logger.info(f"✅ 撤销订单成功：{symbol}, ID={order_id}")
                return result
            else:
                logger.error(f"❌ 撤销订单失败：{symbol}, ID={order_id}")
                return None
                
        except Exception as e:
            logger.error(f"❌ 撤销订单异常：{e}", exc_info=True)
            return None
    
    def cancel_all_orders(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        撤销币种所有挂单
        
        Args:
            symbol: 币种符号
            
        Returns:
            撤销结果
        """
        try:
            logger.info(f"🔄 撤销 {symbol} 所有挂单")
            
            result = binance_trading_api.cancel_all_orders(symbol)
            
            if result:
                logger.info(f"✅ 撤销 {symbol} 所有挂单成功")
                return result
            else:
                logger.error(f"❌ 撤销 {symbol} 所有挂单失败")
                return None
                
        except Exception as e:
            logger.error(f"❌ 撤销所有订单异常：{e}", exc_info=True)
            return None
    
    def get_all_positions(self) -> List[Dict[str, Any]]:
        """
        获取所有持仓（从 API 同步）
        
        Returns:
            持仓列表
        """
        try:
            positions = binance_trading_api.get_position()
            return positions
        except Exception as e:
            logger.error(f"❌ 获取持仓失败：{e}")
            return []
    
    def get_account_balance(self) -> List[Dict[str, Any]]:
        """
        获取账户余额
        
        Returns:
            余额列表
        """
        try:
            balances = binance_trading_api.get_account_balance()
            return balances
        except Exception as e:
            logger.error(f"❌ 获取余额失败：{e}")
            return []
    
    def get_order_history(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取订单历史（本地记录）
        
        Args:
            symbol: 币种符号（可选）
            
        Returns:
            订单历史列表
        """
        if symbol:
            return [
                order for order in self.orders.values()
                if order.get('symbol') == symbol
            ]
        return list(self.orders.values())


# 全局交易执行器实例
trading_executor = TradingExecutor()
