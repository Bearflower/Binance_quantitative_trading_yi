"""
统计分析器
计算统计指标、分析交易结果、生成统计数据
"""
from typing import Dict, List, Any
from decimal import Decimal
import structlog
import pandas as pd
import numpy as np


logger = structlog.get_logger()


class StatisticsAnalyzer:
    """统计分析器
    
    职责：
    - 计算统计指标
    - 分析交易结果
    - 生成统计数据
    """
    
    def __init__(self):
        """初始化统计分析器"""
        logger.info("统计分析器初始化完成")
    
    def analyze(
        self,
        trades: List[Dict[str, Any]],
        equity_curve: List[Dict[str, Any]],
        initial_balance: Decimal
    ) -> Dict[str, Any]:
        """
        分析交易结果
        
        Args:
            trades: 交易记录列表
            equity_curve: 资金曲线
            initial_balance: 初始资金
            
        Returns:
            统计结果字典
        """
        if not trades:
            logger.warning("没有交易记录，无法分析")
            return self._empty_statistics()
        
        # 转换为DataFrame
        trades_df = pd.DataFrame(trades)
        equity_df = pd.DataFrame(equity_curve)
        
        # 计算各项指标
        statistics = {
            # 基础指标
            'total_trades': len(trades),
            'winning_trades': len(trades_df[trades_df['pnl'] > 0]),
            'losing_trades': len(trades_df[trades_df['pnl'] < 0]),
            'win_rate': self._calculate_win_rate(trades_df),
            
            # 盈亏指标
            'total_pnl': float(trades_df['pnl'].sum()),
            'total_return': float(trades_df['pnl'].sum() / float(initial_balance)),
            'average_pnl': float(trades_df['pnl'].mean()),
            'average_win': float(trades_df[trades_df['pnl'] > 0]['pnl'].mean()) if len(trades_df[trades_df['pnl'] > 0]) > 0 else 0,
            'average_loss': float(trades_df[trades_df['pnl'] < 0]['pnl'].mean()) if len(trades_df[trades_df['pnl'] < 0]) > 0 else 0,
            'profit_factor': self._calculate_profit_factor(trades_df),
            
            # 风险指标
            'max_drawdown': self._calculate_max_drawdown(equity_df),
            'max_drawdown_percent': self._calculate_max_drawdown_percent(equity_df, initial_balance),
            
            # 效率指标
            'sharpe_ratio': self._calculate_sharpe_ratio(trades_df),
            'average_holding_hours': float(trades_df['holding_hours'].mean()),
            
            # 最终资金
            'final_balance': float(initial_balance) + float(trades_df['pnl'].sum())
        }
        
        # 计算盈亏比
        if statistics['average_loss'] != 0:
            statistics['profit_loss_ratio'] = abs(statistics['average_win'] / statistics['average_loss'])
        else:
            statistics['profit_loss_ratio'] = 0
        
        logger.info("统计分析完成", **statistics)
        
        return statistics
    
    def _empty_statistics(self) -> Dict[str, Any]:
        """
        返回空统计结果
        
        Returns:
            空统计字典
        """
        return {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'win_rate': 0.0,
            'total_pnl': 0.0,
            'total_return': 0.0,
            'average_pnl': 0.0,
            'average_win': 0.0,
            'average_loss': 0.0,
            'profit_factor': 0.0,
            'profit_loss_ratio': 0.0,
            'max_drawdown': 0.0,
            'max_drawdown_percent': 0.0,
            'sharpe_ratio': 0.0,
            'average_holding_hours': 0.0,
            'final_balance': 0.0
        }
    
    def _calculate_win_rate(self, trades_df: pd.DataFrame) -> float:
        """
        计算胜率
        
        Args:
            trades_df: 交易记录DataFrame
            
        Returns:
            胜率（0-1）
        """
        if len(trades_df) == 0:
            return 0.0
        
        winning_trades = len(trades_df[trades_df['pnl'] > 0])
        return winning_trades / len(trades_df)
    
    def _calculate_profit_factor(self, trades_df: pd.DataFrame) -> float:
        """
        计算盈亏比
        
        Args:
            trades_df: 交易记录DataFrame
            
        Returns:
            盈亏比
        """
        total_profit = trades_df[trades_df['pnl'] > 0]['pnl'].sum()
        total_loss = abs(trades_df[trades_df['pnl'] < 0]['pnl'].sum())
        
        if total_loss == 0:
            return 0.0
        
        return total_profit / total_loss
    
    def _calculate_max_drawdown(self, equity_df: pd.DataFrame) -> float:
        """
        计算最大回撤
        
        Args:
            equity_df: 资金曲线DataFrame
            
        Returns:
            最大回撤金额
        """
        if len(equity_df) == 0:
            return 0.0
        
        # 计算累计最大值
        equity_df['cummax'] = equity_df['balance'].cummax()
        
        # 计算回撤
        equity_df['drawdown'] = equity_df['cummax'] - equity_df['balance']
        
        # 返回最大回撤
        return float(equity_df['drawdown'].max())
    
    def _calculate_max_drawdown_percent(
        self,
        equity_df: pd.DataFrame,
        initial_balance: Decimal
    ) -> float:
        """
        计算最大回撤百分比
        
        Args:
            equity_df: 资金曲线DataFrame
            initial_balance: 初始资金
            
        Returns:
            最大回撤百分比
        """
        max_drawdown = self._calculate_max_drawdown(equity_df)
        
        if float(initial_balance) == 0:
            return 0.0
        
        return max_drawdown / float(initial_balance)
    
    def _calculate_sharpe_ratio(self, trades_df: pd.DataFrame) -> float:
        """
        计算夏普比率
        
        Args:
            trades_df: 交易记录DataFrame
            
        Returns:
            夏普比率
        """
        if len(trades_df) < 2:
            return 0.0
        
        # 计算收益率
        returns = trades_df['pnl_percent'].values
        
        # 计算平均收益率
        mean_return = np.mean(returns)
        
        # 计算收益率标准差
        std_return = np.std(returns)
        
        # 计算夏普比率（假设无风险利率为0）
        if std_return == 0:
            return 0.0
        
        # 年化夏普比率（假设每年有365个交易日）
        sharpe_ratio = (mean_return / std_return) * np.sqrt(365)
        
        return float(sharpe_ratio)
