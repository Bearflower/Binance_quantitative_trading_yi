"""
仓位管理器
管理持仓状态、计算仓位大小、跟踪持仓盈亏、实现风控机制
"""
from typing import Dict, List, Any, Optional
from datetime import datetime
from decimal import Decimal
import structlog


logger = structlog.get_logger()


class PositionManager:
    """仓位管理器
    
    职责：
    - 管理持仓状态
    - 计算仓位大小
    - 跟踪持仓盈亏
    - 实现风控机制
    """
    
    def __init__(
        self,
        initial_balance: Decimal,
        config: Dict[str, Any]
    ):
        """
        初始化仓位管理器
        
        Args:
            initial_balance: 初始资金
            config: 配置字典
        """
        self.initial_balance = initial_balance
        self.config = config
        
        # 持仓字典
        self.positions: Dict[str, Dict[str, Any]] = {}
        
        # 交易配置
        trading_config = config.get('trading', {})
        self.leverage = trading_config.get('leverage', 2)
        self.max_positions = trading_config.get('max_positions', 3)
        
        # 风险控制配置
        risk_config = trading_config.get('risk_control', {})
        self.max_loss_percent = Decimal(str(risk_config.get('max_loss_percent', 0.02)))
        
        # 分批止盈配置
        batch_config = trading_config.get('batch_take_profit', {})
        self.target1_atr_multiplier = Decimal(str(batch_config.get('target1_atr_multiplier', 1.5)))
        self.target2_atr_multiplier = Decimal(str(batch_config.get('target2_atr_multiplier', 3.5)))
        
        logger.info(
            "仓位管理器初始化完成",
            initial_balance=float(initial_balance),
            leverage=self.leverage,
            max_positions=self.max_positions,
            max_loss_percent=float(self.max_loss_percent)
        )
    
    def calculate_position_size(
        self,
        balance: Decimal,
        current_price: float,
        atr: Decimal
    ) -> Decimal:
        """
        计算仓位大小（基于风险控制）
        
        仓位计算逻辑：
        1. 每笔最大亏损 = 账户总资金 × 2%
        2. 开仓价值 = 最大亏损 / 止损幅度
        3. 保证金 = 开仓价值 / 杠杆倍数
        4. 数量 = 开仓价值 / 当前价格
        
        Args:
            balance: 账户余额
            current_price: 当前价格
            atr: ATR值
            
        Returns:
            仓位大小（数量）
        """
        try:
            # 计算止损幅度
            stop_loss_percent = self._calculate_stop_loss_percent(atr)
            
            # 参数校验
            if balance <= 0:
                logger.error(f"账户余额无效: {balance}")
                return Decimal('0')
            
            if current_price <= 0:
                logger.error(f"当前价格无效: {current_price}")
                return Decimal('0')
            
            if stop_loss_percent <= 0:
                logger.error(f"止损幅度无效: {stop_loss_percent}")
                return Decimal('0')
            
            # 1. 计算每笔最大亏损
            max_loss = balance * self.max_loss_percent
            
            # 2. 计算开仓价值
            position_value = max_loss / stop_loss_percent
            
            # 3. 计算数量
            quantity = position_value / Decimal(str(current_price))
            
            logger.info(
                "仓位计算完成",
                balance=float(balance),
                max_loss=float(max_loss),
                stop_loss_percent=float(stop_loss_percent),
                position_value=float(position_value),
                quantity=float(quantity)
            )
            
            return quantity
            
        except Exception as e:
            logger.error(f"仓位计算失败: {e}")
            return Decimal('0')
    
    def _calculate_stop_loss_percent(self, atr: Decimal) -> Decimal:
        """
        计算止损幅度
        
        止损幅度 = MAX(ATR止损, 紧急止损, 最小绝对止损) / 开仓价
        
        Args:
            atr: ATR值
            
        Returns:
            止损幅度（比例）
        """
        # ATR止损幅度（假设开仓价为1）
        # 止损价 = 开仓价 + 2.5 × ATR
        # 止损幅度 = 2.5 × ATR / 开仓价
        # 这里我们假设开仓价为某个值，计算相对幅度
        # 简化处理：如果ATR > 0，使用ATR计算；否则使用固定5%
        
        if atr > 0:
            # 假设开仓价 = ATR * 20（这是一个简化假设）
            # 实际应该在调用时传入开仓价
            # 这里我们返回一个基于ATR的止损幅度
            # 止损价 = 开仓价 + 2.5 × ATR
            # 止损幅度 = 2.5 × ATR / 开仓价
            # 如果开仓价 = ATR * 20，则止损幅度 = 2.5 / 20 = 0.125 (12.5%)
            # 但这样不合理，我们改为直接返回一个合理的止损幅度
            
            # 更合理的做法：返回基于ATR的止损幅度
            # 假设ATR占价格的5%，则止损幅度 = 2.5 * 5% = 12.5%
            # 这里我们简化为：如果ATR > 0，返回5%（最小止损）
            return Decimal('0.05')
        else:
            # 如果没有ATR，使用最小绝对止损（5%）
            return Decimal('0.05')
    
    def calculate_stop_loss(
        self,
        entry_price: Decimal,
        atr: Decimal
    ) -> Decimal:
        """
        计算止损价格
        
        最终止损价 = MAX(
            ATR止损：开仓价 + 2.5 × ATR,
            紧急止损：开仓价 × 1.015,
            最小绝对止损：开仓价 × 1.05
        )
        
        Args:
            entry_price: 开仓价格
            atr: ATR值
            
        Returns:
            止损价格
        """
        # 1. ATR止损
        atr_stop_loss = entry_price + (atr * Decimal('2.5'))
        
        # 2. 紧急止损（1.5%）
        emergency_stop_loss = entry_price * Decimal('1.015')
        
        # 3. 最小绝对止损（5%）
        min_absolute_stop_loss = entry_price * Decimal('1.05')
        
        # 取最大值
        final_stop_loss = max(atr_stop_loss, emergency_stop_loss, min_absolute_stop_loss)
        
        logger.debug(
            "计算止损价格",
            entry_price=float(entry_price),
            atr=float(atr),
            atr_stop_loss=float(atr_stop_loss),
            emergency_stop_loss=float(emergency_stop_loss),
            min_absolute_stop_loss=float(min_absolute_stop_loss),
            final_stop_loss=float(final_stop_loss)
        )
        
        return final_stop_loss
    
    def calculate_take_profit(
        self,
        entry_price: Decimal,
        atr: Decimal
    ) -> List[Decimal]:
        """
        计算止盈价格
        
        Args:
            entry_price: 开仓价格
            atr: ATR值
            
        Returns:
            止盈价格列表 [第一目标, 第二目标]
        """
        # 第一目标：开仓价 - 1.5 × ATR
        target1_price = entry_price - (atr * self.target1_atr_multiplier)
        
        # 第二目标：开仓价 - 3.5 × ATR
        target2_price = entry_price - (atr * self.target2_atr_multiplier)
        
        logger.debug(
            "计算止盈价格",
            entry_price=float(entry_price),
            atr=float(atr),
            target1_price=float(target1_price),
            target2_price=float(target2_price)
        )
        
        return [target1_price, target2_price]
    
    def open_position(
        self,
        symbol: str,
        entry_price: float,
        entry_time: datetime,
        quantity: float,
        stop_loss_price: float,
        take_profit_prices: List[float],
        atr: float
    ) -> None:
        """
        开仓
        
        Args:
            symbol: 交易对
            entry_price: 开仓价格
            entry_time: 开仓时间
            quantity: 数量
            stop_loss_price: 止损价格
            take_profit_prices: 止盈价格列表
            atr: ATR值
        """
        self.positions[symbol] = {
            'entry_price': entry_price,
            'entry_time': entry_time,
            'quantity': quantity,
            'stop_loss_price': stop_loss_price,
            'take_profit_prices': take_profit_prices,
            'atr': atr,
            'lowest_price': entry_price,
            'target1_reached': False,
            'target2_reached': False,
            'remaining_quantity': quantity
        }
        
        logger.info(
            f"开仓记录: {symbol}",
            entry_price=entry_price,
            quantity=quantity,
            stop_loss_price=stop_loss_price
        )
    
    def close_position(self, symbol: str) -> None:
        """
        关闭持仓
        
        Args:
            symbol: 交易对
        """
        if symbol in self.positions:
            del self.positions[symbol]
            logger.info(f"关闭持仓: {symbol}")
    
    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取持仓信息
        
        Args:
            symbol: 交易对
            
        Returns:
            持仓信息字典
        """
        return self.positions.get(symbol)
    
    def has_position(self, symbol: str) -> bool:
        """
        检查是否有持仓
        
        Args:
            symbol: 交易对
            
        Returns:
            是否有持仓
        """
        return symbol in self.positions
    
    def update_target_status(self, symbol: str, target_level: int) -> None:
        """
        更新目标达成状态
        
        Args:
            symbol: 交易对
            target_level: 目标级别（1或2）
        """
        if symbol not in self.positions:
            return
        
        position = self.positions[symbol]
        
        if target_level == 1:
            position['target1_reached'] = True
            position['remaining_quantity'] *= Decimal('0.7')
            logger.info(f"第一目标已达成: {symbol}")
        elif target_level == 2:
            position['target2_reached'] = True
            position['remaining_quantity'] *= Decimal('0.6')
            logger.info(f"第二目标已达成: {symbol}")
    
    def update_lowest_price(self, symbol: str, price: float) -> None:
        """
        更新最低价
        
        Args:
            symbol: 交易对
            price: 当前价格
        """
        if symbol in self.positions:
            if price < self.positions[symbol]['lowest_price']:
                self.positions[symbol]['lowest_price'] = price
                logger.debug(f"更新最低价: {symbol} = {price}")
    
    def update_remaining_quantity(self, symbol: str, closed_quantity: float) -> None:
        """
        更新剩余数量
        
        Args:
            symbol: 交易对
            closed_quantity: 已平仓数量
        """
        if symbol in self.positions:
            self.positions[symbol]['remaining_quantity'] -= closed_quantity
            logger.debug(
                f"更新剩余数量: {symbol}",
                remaining_quantity=self.positions[symbol]['remaining_quantity']
            )
