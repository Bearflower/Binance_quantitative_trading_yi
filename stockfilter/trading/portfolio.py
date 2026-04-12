"""
仓位管理模块
管理持仓数量、资金分配和持仓跟踪
"""

import pandas as pd
from datetime import datetime
from typing import Dict, Any, List, Optional

from utils.logger import get_logger

logger = get_logger()


class PortfolioManager:
    """仓位管理器"""

    def __init__(self, initial_cash: float = 1000000, params: Optional[Dict[str, Any]] = None):
        """
        初始化仓位管理器
        
        Args:
            initial_cash: 初始资金
            params: 仓位管理参数
        """
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.params = self._get_default_params()
        if params:
            self.params.update(params)
        
        self.positions = []

    def _get_default_params(self) -> Dict[str, Any]:
        """获取默认参数"""
        return {
            'max_positions': 5,
            'position_size': 'equal_weight'
        }

    def get_open_positions(self) -> List[Dict]:
        """获取当前持仓"""
        return [p for p in self.positions if p.get('status') == 'open']

    def get_available_slots(self) -> int:
        """获取可用仓位数量"""
        open_count = len(self.get_open_positions())
        return self.params['max_positions'] - open_count

    def calculate_position_size(self, num_positions: int) -> float:
        """
        计算单只股票的仓位大小
        
        Args:
            num_positions: 待买入股票数量
        
        Returns:
            单只股票的资金量
        """
        available_cash = self.cash
        position_size = available_cash / num_positions if num_positions > 0 else 0
        return position_size

    def open_position(self, signal: Dict, entry_price: float, position_size: float):
        """
        开仓
        
        Args:
            signal: 买入信号
            entry_price: 实际买入价格
            position_size: 仓位大小（资金量）
        """
        position = {
            'code': signal['code'],
            'name': signal.get('name', ''),
            'status': 'open',
            'entry_date': datetime.now().strftime('%Y-%m-%d'),
            'entry_price': entry_price,
            'position_size': position_size,
            'shares': int(position_size / entry_price / 100) * 100,
            'support_level': signal.get('support_level', 0),
            'stop_loss_price': signal.get('stop_loss_price', 0),
            'peak_price': entry_price,
            'current_value': entry_price * int(position_size / entry_price / 100) * 100
        }
        
        self.positions.append(position)
        self.cash -= position_size
        
        logger.info(f"开仓：{position['code']} 价格：{entry_price:.2f} 数量：{position['shares']} 金额：{position_size:.2f}")

    def close_position(self, position: Dict, exit_price: float, reason: str = ''):
        """
        平仓
        
        Args:
            position: 持仓信息
            exit_price: 实际卖出价格
            reason: 平仓原因
        """
        shares = position.get('shares', 0)
        entry_value = position.get('position_size', 0)
        exit_value = exit_price * shares
        
        pnl = exit_value - entry_value
        pnl_pct = pnl / entry_value if entry_value > 0 else 0
        
        position['status'] = 'closed'
        position['exit_date'] = datetime.now().strftime('%Y-%m-%d')
        position['exit_price'] = exit_price
        position['pnl'] = pnl
        position['pnl_pct'] = pnl_pct
        position['close_reason'] = reason
        
        self.cash += exit_value
        
        logger.info(f"平仓：{position['code']} 价格：{exit_price:.2f} 盈亏：{pnl:.2f} ({pnl_pct:.2%}) 原因：{reason}")

    def update_position_value(self, position: Dict, current_price: float):
        """更新持仓市值"""
        if position.get('status') != 'open':
            return
        
        shares = position.get('shares', 0)
        position['current_value'] = current_price * shares
        position['current_price'] = current_price
        
        peak_price = position.get('peak_price', position.get('entry_price', 0))
        position['peak_price'] = max(peak_price, current_price)

    def get_portfolio_summary(self) -> Dict:
        """获取投资组合汇总"""
        open_positions = self.get_open_positions()
        
        total_value = sum(p.get('current_value', 0) for p in open_positions)
        total_cost = sum(p.get('position_size', 0) for p in open_positions)
        
        total_pnl = total_value - total_cost
        total_pnl_pct = total_pnl / total_cost if total_cost > 0 else 0
        
        return {
            'cash': self.cash,
            'total_value': total_value,
            'total_assets': self.cash + total_value,
            'initial_cash': self.initial_cash,
            'total_pnl': total_pnl,
            'total_pnl_pct': total_pnl_pct,
            'position_count': len(open_positions),
            'available_slots': self.get_available_slots()
        }

    def get_position_history(self) -> pd.DataFrame:
        """获取持仓历史"""
        closed_positions = [p for p in self.positions if p.get('status') == 'closed']
        
        if not closed_positions:
            return pd.DataFrame()
        
        df = pd.DataFrame(closed_positions)
        df['exit_date'] = pd.to_datetime(df['exit_date'])
        df.sort_values('exit_date', ascending=False, inplace=True)
        
        return df
