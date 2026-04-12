#!/usr/bin/env python3
"""
交易统计模块
计算交易统计数据：胜率、盈亏比、连续盈亏等
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, Optional, List

from models.database import DatabaseManager, get_db_manager

logger = logging.getLogger(__name__)


class TradeStatistics:
    """交易统计类"""
    
    def __init__(self):
        self.db: DatabaseManager = get_db_manager()
    
    def calculate_weekly_statistics(self, 
                                   symbol: str = 'ALL') -> Dict[str, Any]:
        """
        计算本周的统计数据
        
        Args:
            symbol: 交易对，'ALL' 表示所有
        
        Returns:
            统计数据字典
        """
        # 获取本周的平仓记录
        now = datetime.now()
        monday = now - timedelta(days=now.weekday())
        start_of_week = datetime(monday.year, monday.month, monday.day)
        
        closed_trades = self.db.get_closed_positions(
            symbol=None if symbol == 'ALL' else symbol,
            start_time=start_of_week
        )
        
        return self._calculate_stats_from_trades(
            closed_trades, 
            'WEEKLY',
            start_of_week,
            now,
            symbol
        )
    
    def calculate_monthly_statistics(self,
                                    symbol: str = 'ALL') -> Dict[str, Any]:
        """
        计算本月的统计数据
        
        Args:
            symbol: 交易对，'ALL' 表示所有
        
        Returns:
            统计数据字典
        """
        now = datetime.now()
        start_of_month = datetime(now.year, now.month, 1)
        
        closed_trades = self.db.get_closed_positions(
            symbol=None if symbol == 'ALL' else symbol,
            start_time=start_of_month
        )
        
        return self._calculate_stats_from_trades(
            closed_trades,
            'MONTHLY',
            start_of_month,
            now,
            symbol
        )
    
    def _calculate_stats_from_trades(self,
                                    closed_trades: List[Dict[str, Any]],
                                    period_type: str,
                                    period_start: datetime,
                                    period_end: datetime,
                                    symbol: str = 'ALL') -> Dict[str, Any]:
        """
        从平仓记录计算统计数据
        
        Args:
            closed_trades: 平仓记录列表
            period_type: 周期类型
            period_start: 周期开始
            period_end: 周期结束
            symbol: 交易对
        
        Returns:
            统计数据字典
        """
        if not closed_trades:
            return {
                'period_type': period_type,
                'period_start': period_start.strftime('%Y-%m-%d'),
                'period_end': period_end.strftime('%Y-%m-%d'),
                'symbol': symbol,
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'total_net_pnl': Decimal('0'),
                'total_commission': Decimal('0'),
                'avg_pnl_rate': Decimal('0'),
                'max_pnl_rate': Decimal('0'),
                'min_pnl_rate': Decimal('0'),
                'win_rate': Decimal('0'),
                'profit_loss_ratio': Decimal('0'),
                'max_consecutive_wins': 0,
                'max_consecutive_losses': 0
            }
        
        # 基础统计
        total_trades = len(closed_trades)
        winning_trades = sum(1 for t in closed_trades if t['net_pnl'] > 0)
        losing_trades = sum(1 for t in closed_trades if t['net_pnl'] < 0)
        
        # 盈亏统计
        total_net_pnl = sum(Decimal(t['net_pnl']) for t in closed_trades)
        total_commission = sum(Decimal(t['commission']) for t in closed_trades)
        
        # 收益率统计
        pnl_rates = [Decimal(t['pnl_rate']) for t in closed_trades]
        avg_pnl_rate = sum(pnl_rates) / len(pnl_rates) if pnl_rates else Decimal('0')
        max_pnl_rate = max(pnl_rates) if pnl_rates else Decimal('0')
        min_pnl_rate = min(pnl_rates) if pnl_rates else Decimal('0')
        
        # 胜率
        win_rate = Decimal(winning_trades) / Decimal(total_trades) * 100 if total_trades > 0 else Decimal('0')
        
        # 盈亏比
        winning_pnls = [Decimal(t['net_pnl']) for t in closed_trades if t['net_pnl'] > 0]
        losing_pnls = [abs(Decimal(t['net_pnl'])) for t in closed_trades if t['net_pnl'] < 0]
        
        avg_win = sum(winning_pnls) / len(winning_pnls) if winning_pnls else Decimal('0')
        avg_loss = sum(losing_pnls) / len(losing_pnls) if losing_pnls else Decimal('0')
        
        profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else Decimal('0')
        
        # 连续统计
        max_consecutive_wins, max_consecutive_losses = self._calculate_consecutive_trades(closed_trades)
        
        return {
            'period_type': period_type,
            'period_start': period_start.strftime('%Y-%m-%d'),
            'period_end': period_end.strftime('%Y-%m-%d'),
            'symbol': symbol,
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'total_net_pnl': total_net_pnl.quantize(Decimal('0.00000001')),
            'total_commission': total_commission.quantize(Decimal('0.00000001')),
            'avg_pnl_rate': avg_pnl_rate.quantize(Decimal('0.0001')),
            'max_pnl_rate': max_pnl_rate.quantize(Decimal('0.0001')),
            'min_pnl_rate': min_pnl_rate.quantize(Decimal('0.0001')),
            'win_rate': win_rate.quantize(Decimal('0.0001')),
            'profit_loss_ratio': profit_loss_ratio.quantize(Decimal('0.0001')),
            'max_consecutive_wins': max_consecutive_wins,
            'max_consecutive_losses': max_consecutive_losses
        }
    
    def _calculate_consecutive_trades(self, 
                                     closed_trades: List[Dict[str, Any]]) -> tuple:
        """
        计算连续盈利/亏损次数
        
        Args:
            closed_trades: 平仓记录列表（按时间排序）
        
        Returns:
            (最大连续盈利次数，最大连续亏损次数)
        """
        # 按平仓时间排序
        sorted_trades = sorted(closed_trades, key=lambda x: x['close_time'])
        
        max_wins = 0
        max_losses = 0
        current_wins = 0
        current_losses = 0
        
        for trade in sorted_trades:
            net_pnl = Decimal(trade['net_pnl'])
            
            if net_pnl > 0:  # 盈利
                current_wins += 1
                current_losses = 0
                max_wins = max(max_wins, current_wins)
            elif net_pnl < 0:  # 亏损
                current_losses += 1
                current_wins = 0
                max_losses = max(max_losses, current_losses)
            else:  # 保本
                current_wins = 0
                current_losses = 0
        
        return max_wins, max_losses
    
    def update_statistics(self):
        """更新统计数据（调用此方法保存统计结果）"""
        try:
            # 计算周统计
            weekly_stats = self.calculate_weekly_statistics()
            self.db.save_trade_statistics(weekly_stats)
            logger.info(f"周统计已更新：交易数={weekly_stats['total_trades']}, 胜率={weekly_stats['win_rate']:.2f}%")
            
            # 计算月统计
            monthly_stats = self.calculate_monthly_statistics()
            self.db.save_trade_statistics(monthly_stats)
            logger.info(f"月统计已更新：交易数={monthly_stats['total_trades']}, 胜率={monthly_stats['win_rate']:.2f}%")
            
        except Exception as e:
            logger.error(f"更新统计数据失败：{e}", exc_info=True)
    
    def get_current_week_stats(self) -> Dict[str, Any]:
        """获取本周统计数据"""
        stats = self.calculate_weekly_statistics()
        return {
            'total_trades': stats['total_trades'],
            'winning_trades': stats['winning_trades'],
            'losing_trades': stats['losing_trades'],
            'win_rate': float(stats['win_rate']),
            'total_net_pnl': float(stats['total_net_pnl']),
            'avg_pnl_rate': float(stats['avg_pnl_rate']),
            'profit_loss_ratio': float(stats['profit_loss_ratio'])
        }


# 全局实例
_stats_calculator: Optional[TradeStatistics] = None


def get_stats_calculator() -> TradeStatistics:
    """获取统计计算器实例"""
    global _stats_calculator
    if _stats_calculator is None:
        _stats_calculator = TradeStatistics()
    return _stats_calculator


if __name__ == '__main__':
    # 测试代码
    logging.basicConfig(level=logging.INFO)
    
    print("=" * 60)
    print("交易统计模块测试")
    print("=" * 60)
    
    calculator = get_stats_calculator()
    
    print("\n计算本周统计数据...")
    weekly_stats = calculator.calculate_weekly_statistics()
    
    print(f"\n本周统计:")
    print(f"  交易次数：{weekly_stats['total_trades']}")
    print(f"  盈利次数：{weekly_stats['winning_trades']}")
    print(f"  亏损次数：{weekly_stats['losing_trades']}")
    print(f"  胜率：{weekly_stats['win_rate']:.2f}%")
    print(f"  总盈亏：{weekly_stats['total_net_pnl']} USDT")
    print(f"  平均收益率：{weekly_stats['avg_pnl_rate']:.2f}%")
    print(f"  盈亏比：{weekly_stats['profit_loss_ratio']:.2f}")
    print(f"  最大连胜：{weekly_stats['max_consecutive_wins']}")
    print(f"  最大连败：{weekly_stats['max_consecutive_losses']}")
    
    print("\n更新统计数据...")
    calculator.update_statistics()
    
    print("\n" + "=" * 60)
    print("测试完成")
