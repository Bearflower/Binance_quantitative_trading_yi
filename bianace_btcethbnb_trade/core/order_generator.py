#!/usr/bin/env python3
"""
订单生成模块

基于 traderule.txt 第六章实现自动化交易执行流程：
1. 订单参数计算（止损价、止盈价、保证金、杠杆）
2. 订单模板生成
3. 精度格式化（适配币安 API 要求）
4. PM 账户适配

核心公式（第六章）：
- 止损价（多头）= 开仓价 × (1 – 止损幅度)
- 止损价（空头）= 开仓价 × (1 + 止损幅度)
- TP1 = 开仓价 ± 1.5R
- TP2 = 开仓价 ± 2.5R
- TP3 = 剩余 40% 仓位，移动止损跟踪
- 名义价值 = 风险金额 / 止损百分比
- 合约数量 = 名义价值 / 开仓价格
- 保证金 = 名义价值 / 杠杆
"""

import logging
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any, List, Optional, Tuple
from config.strategy_params import StrategyParams, get_params

logger = logging.getLogger(__name__)


class OrderGenerator:
    """订单生成器类"""
    
    def __init__(self, params: StrategyParams = None):
        """
        初始化订单生成器
        
        Args:
            params: 策略参数
        """
        self.params = params or get_params()
    
    def generate_order_template(
        self,
        symbol: str,
        direction: int,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        signal_grade: str,
        position_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        生成订单模板（第六章订单参数计算）
        
        Args:
            symbol: 交易对
            direction: 方向（1=多，-1=空）
            entry_price: 开仓价
            stop_loss_price: 止损价
            signal_grade: 信号等级（S/A/B）
            position_data: 仓位计算结果
        
        Returns:
            订单模板字典
        """
        # 计算 R 值
        r_value = abs(entry_price - stop_loss_price)
        
        # 计算止盈水平
        tp_levels = self._calculate_take_profit_prices(entry_price, direction, r_value)
        
        # 获取杠杆（基于信号等级）
        leverage = self._get_leverage_for_grade(signal_grade)
        
        # 计算保证金
        notional_value = position_data.get('notional_value', Decimal('0'))
        margin = notional_value / leverage
        
        # 计算合约数量
        quantity = notional_value / entry_price
        
        order_template = {
            'symbol': symbol,
            'direction': 'LONG' if direction == 1 else 'SHORT',
            'entry_price': entry_price,
            'stop_loss_price': stop_loss_price,
            'take_profit_levels': tp_levels,
            'leverage': leverage,
            'margin': margin,
            'notional_value': notional_value,
            'quantity': quantity,
            'signal_grade': signal_grade,
            'risk_amount': position_data.get('risk_amount', Decimal('10')),
            'risk_ratio': position_data.get('risk_ratio', Decimal('0.02'))
        }
        
        logger.info(f"订单模板生成完成：{symbol}")
        logger.info(f"  方向：{order_template['direction']}")
        logger.info(f"  开仓价：{entry_price:.2f}")
        logger.info(f"  止损价：{stop_loss_price:.2f}")
        logger.info(f"  止盈水平：{len(tp_levels)} 个")
        logger.info(f"  杠杆：{leverage}x")
        logger.info(f"  保证金：{margin:.2f}U")
        logger.info(f"  名义价值：{notional_value:.2f}U")
        
        return order_template
    
    def format_order_for_api(
        self,
        order_template: Dict[str, Any],
        api_precision: Dict[str, Decimal] = None
    ) -> Dict[str, Any]:
        """
        格式化订单参数以适配币安 API（精度处理）
        
        Args:
            order_template: 订单模板
            api_precision: API 精度信息 {tick_size, step_size}
        
        Returns:
            格式化后的订单参数
        """
        symbol = order_template['symbol']
        price = order_template['entry_price']
        quantity = order_template['quantity']
        stop_loss = order_template['stop_loss_price']
        
        # 如果没有提供精度信息，使用默认值
        if api_precision is None:
            api_precision = self._get_default_precision(symbol)
        
        tick_size = api_precision.get('tick_size', Decimal('0.1'))
        step_size = api_precision.get('step_size', Decimal('0.001'))
        
        # 格式化价格（向下取整到最近的 tick）
        formatted_price = self._format_price(price, tick_size)
        formatted_stop_loss = self._format_price(stop_loss, tick_size)
        
        # 格式化数量（向下取整到最近的 step，并确保最小名义价值）
        min_notional = self.params.get('account.min_notional_value', Decimal('100'))
        formatted_qty = self._format_quantity(quantity, step_size, min_notional, formatted_price)
        
        # 格式化止盈价格
        formatted_tp_levels = []
        for tp in order_template['take_profit_levels']:
            if tp['price'] is not None:
                formatted_tp = {
                    **tp,
                    'price': self._format_price(tp['price'], tick_size)
                }
                formatted_tp_levels.append(formatted_tp)
            else:
                formatted_tp_levels.append(tp)
        
        formatted_order = {
            **order_template,
            'entry_price': formatted_price,
            'stop_loss_price': formatted_stop_loss,
            'quantity': formatted_qty,
            'take_profit_levels': formatted_tp_levels
        }
        
        logger.info(f"订单参数格式化完成：{symbol}")
        logger.info(f"  价格：{price} → {formatted_price} (tick_size={tick_size})")
        logger.info(f"  数量：{quantity} → {formatted_qty} (step_size={step_size})")
        
        return formatted_order
    
    def generate_market_order_params(
        self,
        order_template: Dict[str, Any],
        formatted_order: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        生成市价单参数（用于开仓）
        
        Args:
            order_template: 订单模板
            formatted_order: 格式化后的订单（可选）
        
        Returns:
            市价单参数字典
        """
        order = formatted_order or order_template
        
        # PM 账户必须使用 BOTH
        position_side = 'BOTH'
        
        params = {
            'symbol': order['symbol'],
            'side': 'BUY' if order['direction'] == 'LONG' else 'SELL',
            'position_side': position_side,
            'type': 'MARKET',
            'quantity': order['quantity']
        }
        
        logger.info(f"市价单参数生成：{params}")
        return params
    
    def generate_stop_loss_order_params(
        self,
        order_template: Dict[str, Any],
        formatted_order: Dict[str, Any] = None,
        position_qty: Decimal = None
    ) -> Dict[str, Any]:
        """
        生成止损单参数（条件单接口）
        
        Args:
            order_template: 订单模板
            formatted_order: 格式化后的订单
            position_qty: 持仓数量（用于平仓）
        
        Returns:
            止损单参数字典
        """
        order = formatted_order or order_template
        
        # 平仓方向（与开仓方向相反）
        close_side = 'SELL' if order['direction'] == 'LONG' else 'BUY'
        
        # 使用持仓数量（如果没有提供，使用订单数量）
        qty = position_qty or order['quantity']
        
        params = {
            'symbol': order['symbol'],
            'side': close_side,
            'position_side': 'BOTH',  # PM 账户
            'strategy_type': 'STOP_MARKET',
            'quantity': qty,
            'stop_price': order['stop_loss_price'],
            'reduce_only': True  # 只减仓，不开新仓
        }
        
        logger.info(f"止损单参数生成：{params}")
        return params
    
    def generate_take_profit_order_params(
        self,
        order_template: Dict[str, Any],
        tp_level: Dict[str, Any],
        formatted_order: Dict[str, Any] = None,
        position_qty: Decimal = None
    ) -> Dict[str, Any]:
        """
        生成止盈单参数（条件单接口）
        
        Args:
            order_template: 订单模板
            tp_level: 止盈水平信息
            formatted_order: 格式化后的订单
            position_qty: 持仓数量
        
        Returns:
            止盈单参数字典
        """
        order = formatted_order or order_template
        
        # 平仓方向
        close_side = 'SELL' if order['direction'] == 'LONG' else 'BUY'
        
        # 计算该止盈水平的数量
        ratio = tp_level.get('ratio', Decimal('0.3'))
        qty = (position_qty or order['quantity']) * ratio
        
        params = {
            'symbol': order['symbol'],
            'side': close_side,
            'position_side': 'BOTH',
            'strategy_type': 'TAKE_PROFIT_MARKET',
            'quantity': qty,
            'stop_price': tp_level['price'],
            'reduce_only': True
        }
        
        logger.info(f"止盈单参数生成 ({tp_level['level']}): {params}")
        return params
    
    def generate_all_orders(
        self,
        order_template: Dict[str, Any],
        formatted_order: Dict[str, Any] = None,
        position_qty: Decimal = None
    ) -> Dict[str, Any]:
        """
        生成所有订单参数（开仓 + 止损 + 止盈）
        
        Args:
            order_template: 订单模板
            formatted_order: 格式化后的订单
            position_qty: 持仓数量
        
        Returns:
            包含所有订单的字典
        """
        orders = {
            'entry': self.generate_market_order_params(order_template, formatted_order),
            'stop_loss': self.generate_stop_loss_order_params(
                order_template, 
                formatted_order, 
                position_qty
            ),
            'take_profits': []
        }
        
        # 生成所有止盈单
        tp_levels = order_template.get('take_profit_levels', [])
        for tp in tp_levels:
            if tp['price'] is not None:  # TP3 是移动止损，无固定价格
                tp_order = self.generate_take_profit_order_params(
                    order_template,
                    tp,
                    formatted_order,
                    position_qty
                )
                orders['take_profits'].append(tp_order)
        
        logger.info(f"所有订单参数生成完成：")
        logger.info(f"  开仓单：1 个")
        logger.info(f"  止损单：1 个")
        logger.info(f"  止盈单：{len(orders['take_profits'])} 个")
        
        return orders
    
    def _calculate_take_profit_prices(
        self,
        entry_price: Decimal,
        direction: int,
        r_value: Decimal
    ) -> List[Dict[str, Any]]:
        """
        计算止盈价格水平
        
        Args:
            entry_price: 开仓价
            direction: 方向
            r_value: R 值（止损距离）
        
        Returns:
            止盈价格列表
        """
        tp_config = self.params.get('risk_management.take_profit_levels', {})
        
        tp1_mult = tp_config.get('tp1_multiplier', Decimal('1.5'))
        tp2_mult = tp_config.get('tp2_multiplier', Decimal('2.5'))
        tp1_ratio = tp_config.get('tp1_ratio', Decimal('0.3'))
        tp2_ratio = tp_config.get('tp2_ratio', Decimal('0.3'))
        tp3_ratio = tp_config.get('tp3_ratio', Decimal('0.4'))
        
        tp_levels = []
        
        if direction == 1:  # 多头
            tp1_price = entry_price + r_value * tp1_mult
            tp2_price = entry_price + r_value * tp2_mult
        else:  # 空头
            tp1_price = entry_price - r_value * tp1_mult
            tp2_price = entry_price - r_value * tp2_mult
        
        # TP1
        tp_levels.append({
            'level': 'TP1',
            'price': tp1_price,
            'ratio': tp1_ratio,
            'description': f'盈利{tp1_mult}R，平{tp1_ratio * 100:.0f}%仓位'
        })
        
        # TP2
        tp_levels.append({
            'level': 'TP2',
            'price': tp2_price,
            'ratio': tp2_ratio,
            'description': f'盈利{tp2_mult}R，平{tp2_ratio * 100:.0f}%仓位'
        })
        
        # TP3（移动止损）
        tp_levels.append({
            'level': 'TP3',
            'price': None,
            'ratio': tp3_ratio,
            'description': f'剩余{tp3_ratio * 100:.0f}%仓位，移动止损跟踪'
        })
        
        return tp_levels
    
    def _get_leverage_for_grade(self, signal_grade: str) -> int:
        """
        根据信号等级获取杠杆倍数
        
        Args:
            signal_grade: 信号等级（S/A/B）
        
        Returns:
            杠杆倍数
        """
        leverage_config = self.params.get('position_sizing.leverage_by_grade', {})
        
        grade_map = {
            'S': leverage_config.get('S', 5),
            'A': leverage_config.get('A', 4),
            'B': leverage_config.get('B', 3)
        }
        
        leverage = grade_map.get(signal_grade, 3)
        return leverage
    
    def _format_price(self, price: Decimal, tick_size: Decimal) -> Decimal:
        """
        格式化价格到指定的 tick_size
        
        Args:
            price: 原始价格
            tick_size: 价格精度
        
        Returns:
            格式化后的价格
        """
        # 向下取整到最近的 tick
        formatted = (price / tick_size).quantize(Decimal('1'), rounding=ROUND_DOWN) * tick_size
        return formatted
    
    def _format_quantity(
        self,
        quantity: Decimal,
        step_size: Decimal,
        min_notional: Decimal,
        price: Decimal
    ) -> Decimal:
        """
        格式化数量到指定的 step_size，并确保最小名义价值
        
        Args:
            quantity: 原始数量
            step_size: 数量精度
            min_notional: 最小名义价值（100 USDT）
            price: 价格
        
        Returns:
            格式化后的数量
        """
        # 向下取整到最近的 step
        formatted = (quantity / step_size).quantize(Decimal('1'), rounding=ROUND_DOWN) * step_size
        
        # 检查名义价值
        notional_value = formatted * price
        if notional_value < min_notional:
            # 向上调整到最小名义价值
            min_qty = min_notional / price
            formatted = (min_qty / step_size).quantize(Decimal('1'), rounding=ROUND_DOWN) * step_size
            # 如果还是小于最小名义价值，向上取整一个 step
            if formatted * price < min_notional:
                formatted += step_size
        
        return formatted
    
    def _get_default_precision(self, symbol: str) -> Dict[str, Decimal]:
        """
        获取默认精度（基于交易对）
        
        Args:
            symbol: 交易对
        
        Returns:
            精度字典 {tick_size, step_size}
        """
        precision_map = {
            'BTCUSDT': {'tick_size': Decimal('0.1'), 'step_size': Decimal('0.001')},
            'ETHUSDT': {'tick_size': Decimal('0.1'), 'step_size': Decimal('0.001')},
            'BNBUSDT': {'tick_size': Decimal('0.1'), 'step_size': Decimal('0.001')}
        }
        
        return precision_map.get(symbol, {'tick_size': Decimal('0.1'), 'step_size': Decimal('0.001')})


# 全局实例
_global_order_generator: Optional[OrderGenerator] = None


def get_order_generator(params: StrategyParams = None) -> OrderGenerator:
    """获取订单生成器实例（单例模式）"""
    global _global_order_generator
    if _global_order_generator is None:
        _global_order_generator = OrderGenerator(params)
    return _global_order_generator


# 便捷函数
def generate_order_template(
    symbol: str,
    direction: int,
    entry_price: Decimal,
    stop_loss_price: Decimal,
    signal_grade: str,
    position_data: Dict[str, Any]
) -> Dict[str, Any]:
    """生成订单模板的便捷函数"""
    return get_order_generator().generate_order_template(
        symbol, direction, entry_price, stop_loss_price, signal_grade, position_data
    )


def generate_all_orders(
    order_template: Dict[str, Any],
    formatted_order: Dict[str, Any] = None,
    position_qty: Decimal = None
) -> Dict[str, Any]:
    """生成所有订单的便捷函数"""
    return get_order_generator().generate_all_orders(
        order_template, formatted_order, position_qty
    )
