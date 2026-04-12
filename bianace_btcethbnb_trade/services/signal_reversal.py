#!/usr/bin/env python3
"""
信号反转判断模块
用于检测新信号与当前持仓方向是否相反
"""

import logging
from typing import Dict, Any, Optional, Tuple
from decimal import Decimal

logger = logging.getLogger(__name__)


class SignalReversalChecker:
    """信号反转检查器"""
    
    def __init__(self):
        """初始化信号反转检查器"""
        self.max_reversal_per_day = 2  # 每天最多反转次数
        logger.info("信号反转检查器初始化完成")
    
    def check_reversal(self, current_positions: list, 
                      new_signal: Dict[str, Any]) -> Tuple[bool, str]:
        """
        检查是否发生信号反转
        
        Args:
            current_positions: 当前持仓列表
            new_signal: 新信号数据
        
        Returns:
            (是否反转，反转原因)
        """
        if not new_signal or 'direction' not in new_signal:
            return False, "新信号无效"
        
        new_direction = new_signal.get('direction', '').upper()
        
        for position in current_positions:
            position_side = position.get('position_side', '').upper()
            position_amt = Decimal(position.get('position_amt', 0))
            
            # 跳过无持仓
            if position_amt == 0:
                continue
            
            # 判断方向
            current_direction = 'LONG' if position_amt > 0 else 'SHORT'
            
            # 检查是否相反
            if self._is_opposite_direction(current_direction, new_direction):
                reason = f"当前持有{self._direction_to_cn(current_direction)}，新信号为{self._direction_to_cn(new_direction)}"
                logger.warning(f"⚠️ 检测到信号反转：{reason}")
                return True, reason
        
        return False, "无反转"
    
    def _is_opposite_direction(self, direction1: str, direction2: str) -> bool:
        """
        判断两个方向是否相反
        
        Args:
            direction1: 方向 1 (LONG/SHORT)
            direction2: 方向 2 (LONG/SHORT)
        
        Returns:
            True 表示相反，False 表示相同或无效
        """
        if direction1 == direction2:
            return False
        
        if direction1 in ['LONG', 'SHORT'] and direction2 in ['LONG', 'SHORT']:
            return True
        
        return False
    
    def _direction_to_cn(self, direction: str) -> str:
        """
        方向英文转中文
        
        Args:
            direction: LONG/SHORT
        
        Returns:
            多头/空头
        """
        return '多头' if direction == 'LONG' else '空头'
    
    def should_close_position(self, position: Dict[str, Any], 
                             new_signal: Dict[str, Any],
                             reversal_check: bool = True) -> Tuple[bool, str]:
        """
        判断是否应该平仓
        
        Args:
            position: 持仓信息
            new_signal: 新信号数据
            reversal_check: 是否进行反转检查
        
        Returns:
            (是否平仓，平仓原因)
        """
        if not new_signal:
            return False, "新信号为空"
        
        # 1. 信号反转检查
        if reversal_check:
            is_reversal, reason = self.check_reversal([position], new_signal)
            if is_reversal:
                return True, f"信号反转：{reason}"
        
        # 2. 信号强度检查
        signal_level = new_signal.get('signal_level', '')
        if signal_level not in ['S', 'A']:
            logger.info(f"信号等级 {signal_level} 不是 S/A 级，不触发平仓")
            return False, f"信号等级不足：{signal_level}"
        
        # 3. 新信号方向确认
        new_direction = new_signal.get('direction', '').upper()
        if not new_direction:
            return False, "新信号方向不明确"
        
        # 4. 检查是否有开仓计划
        if new_signal.get('action') == 'OPEN':
            # 如果要开新仓，先平仓
            return True, "准备开新仓，先平旧仓"
        
        return False, "无需平仓"
    
    def get_reversal_strategy(self, position: Dict[str, Any], 
                             new_signal: Dict[str, Any]) -> Dict[str, Any]:
        """
        获取反转策略
        
        Args:
            position: 持仓信息
            new_signal: 新信号数据
        
        Returns:
            反转策略字典
        """
        symbol = position.get('symbol', '')
        position_amt = Decimal(position.get('position_amt', 0))
        entry_price = Decimal(position.get('entry_price', 0))
        
        # 计算当前盈亏
        current_price = Decimal(new_signal.get('current_price', 0))
        pnl = self._calculate_pnl(position_amt, entry_price, current_price)
        pnl_ratio = pnl / entry_price if entry_price > 0 else Decimal('0')
        
        strategy = {
            'symbol': symbol,
            'action': 'REVERSAL',
            'close_position': {
                'side': 'CLOSE_LONG' if position_amt > 0 else 'CLOSE_SHORT',
                'quantity': abs(float(position_amt)),
                'reason': '信号反转',
                'pnl': float(pnl),
                'pnl_ratio': float(pnl_ratio),
            },
            'open_position': {
                'side': new_signal.get('direction', '').upper(),
                'quantity': float(new_signal.get('quantity', 0)),
                'signal_level': new_signal.get('signal_level', ''),
            }
        }
        
        logger.info(f"反转策略：{symbol} - 平仓 {position_amt}，开仓 {new_signal.get('quantity')}")
        
        return strategy
    
    def _calculate_pnl(self, position_amt: Decimal, entry_price: Decimal, 
                      current_price: Decimal) -> Decimal:
        """
        计算未实现盈亏
        
        Args:
            position_amt: 持仓数量
            entry_price: 入场价格
            current_price: 当前价格
        
        Returns:
            未实现盈亏
        """
        if position_amt > 0:
            # 多头盈亏 = (当前价 - 入场价) * 数量
            pnl = (current_price - entry_price) * position_amt
        else:
            # 空头盈亏 = (入场价 - 当前价) * 数量
            pnl = (entry_price - current_price) * abs(position_amt)
        
        return pnl.quantize(Decimal('0.01'))


def create_signal_reversal_checker() -> SignalReversalChecker:
    """创建信号反转检查器实例"""
    return SignalReversalChecker()


if __name__ == '__main__':
    # 测试代码
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 60)
    print("信号反转检查器测试")
    print("=" * 60)
    
    checker = create_signal_reversal_checker()
    
    # 测试用例 1: 持有多头，新信号为空头（反转）
    current_positions = [
        {
            'symbol': 'BTCUSDT',
            'position_side': 'LONG',
            'position_amt': '0.5',
            'entry_price': '50000',
        }
    ]
    
    new_signal = {
        'direction': 'SHORT',
        'signal_level': 'S',
        'current_price': '51000',
        'quantity': '0.5',
    }
    
    is_reversal, reason = checker.check_reversal(current_positions, new_signal)
    print(f"\n测试 1: 反转={is_reversal}, 原因={reason}")
    
    # 测试用例 2: 持有多头，新信号仍为多头（无反转）
    new_signal['direction'] = 'LONG'
    is_reversal, reason = checker.check_reversal(current_positions, new_signal)
    print(f"测试 2: 反转={is_reversal}, 原因={reason}")
    
    # 测试用例 3: 无反转，判断是否平仓
    should_close, reason = checker.should_close_position(
        current_positions[0], 
        new_signal,
        reversal_check=True
    )
    print(f"测试 3: 平仓={should_close}, 原因={reason}")
    
    print("\n信号反转检查器测试完成")
