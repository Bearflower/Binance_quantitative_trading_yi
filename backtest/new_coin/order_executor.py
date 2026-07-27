"""
订单执行器
模拟开仓、平仓、计算手续费和滑点
"""
from typing import Dict, Any
from decimal import Decimal
import structlog


logger = structlog.get_logger()


class OrderExecutor:
    """订单执行器
    
    职责：
    - 模拟开仓
    - 模拟平仓
    - 计算手续费和滑点
    - 管理止损止盈
    """
    
    def __init__(
        self,
        commission_rate: Decimal,
        slippage_rate: Decimal,
        leverage: int
    ):
        """
        初始化订单执行器
        
        Args:
            commission_rate: 手续费率
            slippage_rate: 滑点率
            leverage: 杠杆倍数
        """
        self.commission_rate = commission_rate
        self.slippage_rate = slippage_rate
        self.leverage = leverage
        
        logger.info(
            "订单执行器初始化完成",
            commission_rate=float(commission_rate),
            slippage_rate=float(slippage_rate),
            leverage=leverage
        )
    
    def execute_short(
        self,
        symbol: str,
        quantity: Decimal,
        price: float
    ) -> Dict[str, Any]:
        """
        模拟开空仓
        
        Args:
            symbol: 交易对
            quantity: 数量
            price: 价格
            
        Returns:
            订单信息
        """
        try:
            # 计算开仓成本（含手续费和滑点）
            entry_price = Decimal(str(price)) * (Decimal('1') + self.slippage_rate)
            entry_cost = entry_price * quantity * (Decimal('1') + self.commission_rate)
            
            logger.info(
                f"模拟开空仓: {symbol}",
                quantity=float(quantity),
                price=price,
                entry_price=float(entry_price),
                entry_cost=float(entry_cost)
            )
            
            return {
                'symbol': symbol,
                'side': 'SELL',
                'quantity': float(quantity),
                'price': price,
                'entry_price': float(entry_price),
                'entry_cost': float(entry_cost),
                'commission': float(entry_price * quantity * self.commission_rate),
                'slippage': float(entry_price * quantity * self.slippage_rate)
            }
            
        except Exception as e:
            logger.error(f"模拟开空仓失败: {symbol}, 错误: {e}")
            return None
    
    def close_position(
        self,
        symbol: str,
        quantity: Decimal,
        price: float
    ) -> Dict[str, Any]:
        """
        模拟平仓
        
        Args:
            symbol: 交易对
            quantity: 数量
            price: 价格
            
        Returns:
            订单信息
        """
        try:
            # 计算平仓收入（扣除手续费和滑点）
            exit_price = Decimal(str(price)) * (Decimal('1') - self.slippage_rate)
            exit_revenue = exit_price * quantity * (Decimal('1') - self.commission_rate)
            
            logger.info(
                f"模拟平仓: {symbol}",
                quantity=float(quantity),
                price=price,
                exit_price=float(exit_price),
                exit_revenue=float(exit_revenue)
            )
            
            return {
                'symbol': symbol,
                'side': 'BUY',
                'quantity': float(quantity),
                'price': price,
                'exit_price': float(exit_price),
                'exit_revenue': float(exit_revenue),
                'commission': float(exit_price * quantity * self.commission_rate),
                'slippage': float(exit_price * quantity * self.slippage_rate)
            }
            
        except Exception as e:
            logger.error(f"模拟平仓失败: {symbol}, 错误: {e}")
            return None
    
    def calculate_pnl(
        self,
        entry_price: float,
        exit_price: float,
        quantity: Decimal
    ) -> Decimal:
        """
        计算盈亏
        
        Args:
            entry_price: 开仓价格
            exit_price: 平仓价格
            quantity: 数量
            
        Returns:
            盈亏金额
        """
        try:
            # 做空盈亏 = 开仓卖出收入 - 平仓买入成本（V4.1修复：原公式方向反了）
            # 做空：开仓卖出，平仓买入
            # 开仓卖出收入 = 开仓价 * 数量 * (1 - 手续费 - 滑点)
            # 平仓买入成本 = 平仓价 * 数量 * (1 + 手续费 + 滑点)
            entry_price_decimal = Decimal(str(entry_price))
            entry_revenue = entry_price_decimal * quantity * (Decimal('1') - self.commission_rate - self.slippage_rate)
            
            exit_price_decimal = Decimal(str(exit_price))
            exit_cost = exit_price_decimal * quantity * (Decimal('1') + self.commission_rate + self.slippage_rate)
            
            # 做空盈亏 = 开仓收入 - 平仓成本
            pnl = entry_revenue - exit_cost
            
            logger.debug(
                "计算盈亏",
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=float(quantity),
                entry_revenue=float(entry_revenue),
                exit_cost=float(exit_cost),
                pnl=float(pnl)
            )
            
            return pnl
            
        except Exception as e:
            logger.error(f"计算盈亏失败: {e}")
            return Decimal('0')
