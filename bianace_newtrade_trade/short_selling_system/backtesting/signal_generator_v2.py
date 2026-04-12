#!/usr/bin/env python3
"""
做空策略回测器 v2 - 优化版
特性：
1. ATR 动态止损
2. 分批止盈 + 移动止盈
3. 币种筛选过滤器
4. 支持 5 分钟/15 分钟 K 线
"""

from decimal import Decimal
from typing import Dict, List, Optional
from .signal_generator import SignalGenerator


class SignalGeneratorV2(SignalGenerator):
    """优化版信号生成器"""
    
    def __init__(self, config: Optional[Dict] = None):
        # 合并配置
        merged_config = {
            # 基础配置
            'stop_loss_atr': Decimal('2.5'),
            'tp1_atr': Decimal('4.0'),
            'tp2_atr': Decimal('6.0'),
            'tp1_percentage': Decimal('0.20'),
            'tp2_percentage': Decimal('0.30'),
            'time_stop_hours': 24,
            'stop_loss_percentage': Decimal('0.04'),
            'sar_af_start': 0.02,
            'sar_af_max': 0.2,
            'position_size': Decimal('4'),
            'leverage': 5,
        }
        merged_config.update(config or {})
        
        super().__init__(merged_config)
        
        # 优化参数（覆盖）
        self.config.update({
            # ATR 动态止损（2 倍 ATR）
            'stop_loss_atr_multiplier': Decimal('2.0'),
            
            # 分批止盈
            'tp1_percentage': Decimal('0.10'),  # 10% 止盈 30%
            'tp2_percentage': Decimal('0.20'),  # 20% 止盈 40%
            'tp3_use_trailing': True,
            
            # 移动止盈
            'trailing_start_percentage': Decimal('0.05'),
            'trailing_stop_percentage': Decimal('0.03'),
        })
    
    def calculate_stop_loss_atr(self, entry_price: Decimal, atr: Decimal) -> Decimal:
        """ATR 动态止损"""
        stop_distance = atr * self.config['stop_loss_atr_multiplier']
        return entry_price + stop_distance
    
    def calculate_take_profit_levels(self, entry_price: Decimal, atr: Decimal) -> Dict:
        """分批止盈"""
        tp1 = entry_price * (Decimal('1') - self.config['tp1_percentage'])
        tp2 = entry_price * (Decimal('1') - self.config['tp2_percentage'])
        
        return {
            'tp1': tp1,
            'tp2': tp2,
            'tp1_percentage': float(self.config['tp1_percentage']),
            'tp2_percentage': float(self.config['tp2_percentage']),
            'tp3_use_trailing': self.config['tp3_use_trailing'],
            'trailing_start': entry_price * (Decimal('1') - self.config['trailing_start_percentage']),
            'trailing_stop_distance': entry_price * self.config['trailing_stop_percentage']
        }
    
    def update_trailing_stop(self, position: Dict, current_price: Decimal) -> Decimal:
        """移动止盈"""
        if not position.get('trailing_active', False):
            # 检查是否启动移动止盈
            if position['entry_price'] > current_price:
                profit_pct = (position['entry_price'] - current_price) / position['entry_price']
                if profit_pct >= self.config['trailing_start_percentage']:
                    position['trailing_active'] = True
                    position['trailing_stop'] = current_price * (
                        Decimal('1') + self.config['trailing_stop_percentage']
                    )
                    return position['trailing_stop']
            return position['stop_loss']
        
        # 更新移动止损位
        new_trailing_stop = current_price * (
            Decimal('1') + self.config['trailing_stop_percentage']
        )
        
        if new_trailing_stop < position.get('trailing_stop', Decimal('999999')):
            position['trailing_stop'] = new_trailing_stop
        
        return position['trailing_stop']
    
    def check_exit_conditions_v2(self, position: Dict, current_kline: Dict) -> Dict:
        """优化版出场条件检查"""
        current_price = Decimal(str(current_kline['close']))
        current_high = Decimal(str(current_kline['high']))
        current_low = Decimal(str(current_kline['low']))
        
        entry_price = position['entry_price']
        stop_loss = position['stop_loss']
        tp1 = position['take_profit_1']
        tp2 = position['take_profit_2']
        
        # 移动止盈
        if position.get('trailing_active', False):
            trailing_stop = position.get('trailing_stop', stop_loss)
            if current_high >= trailing_stop:
                return {
                    'should_exit': True,
                    'type': 'TRAILING_STOP',
                    'price': trailing_stop,
                    'reason': '移动止盈'
                }
        
        # 检查止损
        if current_high >= stop_loss:
            return {
                'should_exit': True,
                'type': 'STOP_LOSS',
                'price': stop_loss,
                'reason': '止损'
            }
        
        # 检查第一止盈
        if not position.get('tp1_hit', False):
            if current_low <= tp1:
                return {
                    'should_exit': True,
                    'type': 'TAKE_PROFIT_1',
                    'price': tp1,
                    'reason': '第一止盈'
                }
        
        # 检查第二止盈
        if position.get('tp1_hit', False) and not position.get('tp2_hit', False):
            if current_low <= tp2:
                return {
                    'should_exit': True,
                    'type': 'TAKE_PROFIT_2',
                    'price': tp2,
                    'reason': '第二止盈'
                }
        
        # 时间止损
        if position.get('time_stop'):
            from datetime import datetime
            time_stop_val = position['time_stop']
            if isinstance(time_stop_val, str):
                time_stop = datetime.fromisoformat(time_stop_val)
            else:
                time_stop = time_stop_val
            
            # 处理 timestamp 可能是数字或字符串的情况
            timestamp = current_kline['timestamp']
            if isinstance(timestamp, str):
                current_time = datetime.fromisoformat(timestamp)
            else:
                current_time = datetime.fromtimestamp(timestamp / 1000)
            
            if current_time >= time_stop:
                return {
                    'should_exit': True,
                    'type': 'TIME_STOP',
                    'price': current_price,
                    'reason': '时间止损'
                }
        
        return {'should_exit': False}
