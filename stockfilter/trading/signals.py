"""
买卖信号生成模块
根据形态筛选结果生成买入和卖出信号
"""

import pandas as pd
from datetime import datetime
from typing import Dict, Any, List, Optional

from utils.logger import get_logger

logger = get_logger()


class SignalGenerator:
    """信号生成器"""

    def __init__(self, params: Optional[Dict[str, Any]] = None):
        """
        初始化信号生成器
        
        Args:
            params: 交易参数
        """
        self.params = self._get_default_params()
        if params:
            self.params.update(params)

    def _get_default_params(self) -> Dict[str, Any]:
        """获取默认参数"""
        return {
            'max_positions': 5,
            'entry_timing': 'next_open',
            'stop_loss_ratio': 0.03,
            'trailing_stop': 0.05,
            'min_hold_days': 1,
            'max_hold_days': 60,
            'index_filter': True,
            'index_code': '000300.SH',
            'index_ma_period': 20
        }

    def generate_buy_signals(self, scan_results: List[Dict], 
                             current_positions: List[Dict],
                             market_condition: bool = True) -> List[Dict]:
        """
        生成买入信号
        
        Args:
            scan_results: 筛选结果列表（已按评分排序）
            current_positions: 当前持仓列表
            market_condition: 大盘环境（True=允许开仓）
        
        Returns:
            买入信号列表
        """
        max_positions = self.params['max_positions']
        current_count = len([p for p in current_positions if p.get('status') == 'open'])
        
        available_slots = max_positions - current_count
        
        if available_slots <= 0:
            logger.info(f"仓位已满（{current_count}/{max_positions}），不生成买入信号")
            return []
        
        if not market_condition and self.params['index_filter']:
            logger.info("大盘环境不佳，暂停开仓")
            return []
        
        buy_signals = []
        
        for result in scan_results[:available_slots]:
            signal = {
                'type': 'buy',
                'code': result['code'],
                'name': result.get('name', ''),
                'score': result.get('score', 0),
                'support_level': result.get('support_level', 0),
                'current_close': result.get('current_close', 0),
                'entry_timing': self.params['entry_timing'],
                'entry_price': None,
                'stop_loss_price': result.get('support_level', 0) * (1 - self.params['stop_loss_ratio']),
                'surge_date': result.get('surge_date'),
                'generated_at': datetime.now()
            }
            
            buy_signals.append(signal)
            logger.info(f"生成买入信号：{result['code']} {result.get('name', '')} 评分：{result.get('score', 0):.2f}")
        
        return buy_signals

    def check_sell_signals(self, position: Dict, current_price: float,
                           current_high: float) -> Optional[Dict]:
        """
        检查卖出信号
        
        Args:
            position: 持仓信息
            current_price: 当前价格
            current_high: 当前最高价（用于移动止盈）
        
        Returns:
            卖出信号（如有）
        """
        entry_price = position.get('entry_price', 0)
        support_level = position.get('support_level', 0)
        entry_date = position.get('entry_date')
        
        if isinstance(entry_date, str):
            entry_date = pd.to_datetime(entry_date)
        
        hold_days = (datetime.now() - entry_date).days if entry_date else 0
        
        if hold_days < self.params['min_hold_days']:
            return None
        
        if hold_days > self.params['max_hold_days'] > 0:
            return {
                'type': 'sell',
                'code': position['code'],
                'reason': '达到最长持仓时间',
                'hold_days': hold_days
            }
        
        stop_loss_price = support_level * (1 - self.params['stop_loss_ratio'])
        if current_price < stop_loss_price:
            return {
                'type': 'sell',
                'code': position['code'],
                'reason': '止损',
                'current_price': current_price,
                'stop_loss_price': stop_loss_price,
                'pnl_pct': (current_price - entry_price) / entry_price if entry_price > 0 else 0
            }
        
        peak_price = max(position.get('peak_price', entry_price), current_high)
        
        if peak_price > entry_price:
            trailing_stop_price = peak_price * (1 - self.params['trailing_stop'])
            if current_price < trailing_stop_price:
                return {
                    'type': 'sell',
                    'code': position['code'],
                    'reason': '移动止盈',
                    'peak_price': peak_price,
                    'current_price': current_price,
                    'trailing_stop_price': trailing_stop_price,
                    'pnl_pct': (current_price - entry_price) / entry_price if entry_price > 0 else 0
                }
        
        return None

    def update_peak_price(self, position: Dict, current_high: float) -> Dict:
        """更新持仓的最高价"""
        peak_price = position.get('peak_price', position.get('entry_price', 0))
        position['peak_price'] = max(peak_price, current_high)
        return position
