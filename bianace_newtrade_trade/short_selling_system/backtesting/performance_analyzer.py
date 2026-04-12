#!/usr/bin/env python3
"""
绩效分析器
深度分析回测结果，提供策略优化建议
"""

from decimal import Decimal
from typing import Dict, List
from collections import defaultdict


class PerformanceAnalyzer:
    """绩效分析器"""
    
    def __init__(self, report: Dict):
        self.report = report
        self.summary = report.get('summary', {})
        self.trades = report.get('trades', [])
    
    def analyze_win_rate(self) -> Dict:
        """分析胜率"""
        total = self.summary.get('total_trades', 0)
        wins = self.summary.get('winning_trades', 0)
        losses = self.summary.get('losing_trades', 0)
        
        win_rate = self.summary.get('win_rate', 0)
        
        assessment = 'N/A'
        if win_rate >= 0.60:
            assessment = '优秀'
        elif win_rate >= 0.45:
            assessment = '良好'
        elif win_rate >= 0.35:
            assessment = '一般'
        else:
            assessment = '较差'
        
        return {
            'total_trades': total,
            'wins': wins,
            'losses': losses,
            'win_rate': win_rate,
            'assessment': assessment
        }
    
    def analyze_profit_loss_ratio(self) -> Dict:
        """分析盈亏比"""
        pl_ratio = self.summary.get('profit_loss_ratio', 0)
        
        assessment = 'N/A'
        if pl_ratio >= 2.0:
            assessment = '优秀'
        elif pl_ratio >= 1.5:
            assessment = '良好'
        elif pl_ratio >= 1.0:
            assessment = '一般'
        else:
            assessment = '较差'
        
        return {
            'profit_loss_ratio': pl_ratio,
            'assessment': assessment
        }
    
    def analyze_returns(self) -> Dict:
        """分析收益率"""
        total_return = self.summary.get('total_return', 0)
        initial_capital = self.summary.get('initial_capital', 0)
        final_capital = self.summary.get('final_capital', 0)
        total_pnl = self.summary.get('total_pnl', 0)
        
        assessment = 'N/A'
        if total_return >= 0.50:
            assessment = '优秀'
        elif total_return >= 0.20:
            assessment = '良好'
        elif total_return >= 0:
            assessment = '一般'
        else:
            assessment = '较差'
        
        return {
            'initial_capital': initial_capital,
            'final_capital': final_capital,
            'total_pnl': total_pnl,
            'total_return': total_return,
            'assessment': assessment
        }
    
    def analyze_fees(self) -> Dict:
        """分析手续费影响"""
        total_fees = self.summary.get('total_fees', 0)
        total_pnl = self.summary.get('total_pnl', 0)
        total_trades = self.summary.get('total_trades', 0)
        
        fee_ratio = total_fees / abs(total_pnl) if total_pnl != 0 else 0
        avg_fee_per_trade = total_fees / total_trades if total_trades > 0 else 0
        
        return {
            'total_fees': total_fees,
            'fee_to_pnl_ratio': fee_ratio,
            'avg_fee_per_trade': avg_fee_per_trade
        }
    
    def analyze_by_grade(self) -> Dict:
        """
        按信号等级分析
        注：README.md 中没有信号分级，此方法保留用于扩展
        """
        return {}
    
    def analyze_exit_reasons(self) -> Dict:
        """分析出场原因"""
        exit_stats = self.report.get('exit_reason_stats', {})
        total = self.summary.get('total_trades', 0)
        
        analysis = {}
        for reason, count in exit_stats.items():
            analysis[reason] = {
                'count': count,
                'percentage': count / total if total > 0 else 0
            }
        
        return analysis
    
    def analyze_by_symbol(self) -> Dict:
        """按币种分析"""
        symbol_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'total_pnl': 0})
        
        for trade in self.trades:
            symbol = trade['symbol']
            symbol_stats[symbol]['trades'] += 1
            if trade.get('pnl', 0) > 0:
                symbol_stats[symbol]['wins'] += 1
            symbol_stats[symbol]['total_pnl'] += trade.get('pnl', 0)
        
        analysis = {}
        for symbol, stats in symbol_stats.items():
            analysis[symbol] = {
                'trades': stats['trades'],
                'win_rate': stats['wins'] / stats['trades'] if stats['trades'] > 0 else 0,
                'total_pnl': stats['total_pnl'],
                'avg_pnl': stats['total_pnl'] / stats['trades'] if stats['trades'] > 0 else 0
            }
        
        return analysis
    
    def generate_assessment(self) -> Dict:
        """生成综合评估"""
        win_rate_analysis = self.analyze_win_rate()
        pl_ratio_analysis = self.analyze_profit_loss_ratio()
        returns_analysis = self.analyze_returns()
        
        scores = []
        
        win_rate_map = {'优秀': 5, '良好': 4, '一般': 3, '较差': 2, 'N/A': 1}
        scores.append(win_rate_map.get(win_rate_analysis['assessment'], 1))
        scores.append(pl_ratio_map := {'优秀': 5, '良好': 4, '一般': 3, '较差': 2, 'N/A': 1}[pl_ratio_analysis['assessment']])
        scores.append(returns_map := {'优秀': 5, '良好': 4, '一般': 3, '较差': 2, 'N/A': 1}[returns_analysis['assessment']])
        
        avg_score = sum(scores) / len(scores)
        
        if avg_score >= 4.5:
            overall = '优秀 - 策略表现非常出色，建议实盘'
        elif avg_score >= 3.5:
            overall = '良好 - 策略表现不错，可以优化后实盘'
        elif avg_score >= 2.5:
            overall = '一般 - 策略需要进一步优化'
        else:
            overall = '较差 - 策略需要重大调整'
        
        return {
            'win_rate': win_rate_analysis['assessment'],
            'profit_loss_ratio': pl_ratio_analysis['assessment'],
            'returns': returns_analysis['assessment'],
            'overall': overall,
            'score': avg_score
        }
    
    def generate_recommendations(self) -> List[str]:
        """生成优化建议"""
        recommendations = []
        
        win_rate = self.summary.get('win_rate', 0)
        pl_ratio = self.summary.get('profit_loss_ratio', 0)
        total_return = self.summary.get('total_return', 0)
        
        if win_rate < 0.35:
            recommendations.append('❗ 胜率过低，建议收紧入场条件或优化止损策略')
        
        if pl_ratio < 1.0:
            recommendations.append('❗ 盈亏比过低，建议扩大止盈或收紧止损')
        
        if total_return < 0:
            recommendations.append('❗ 总体亏损，建议重新评估策略逻辑')
        
        exit_reasons = self.analyze_exit_reasons()
        if exit_reasons.get('STOP_LOSS', {}).get('percentage', 0) > 0.6:
            recommendations.append('⚠️  止损触发过多，建议放宽止损或优化入场时机')
        
        if exit_reasons.get('TIME_STOP', {}).get('percentage', 0) > 0.3:
            recommendations.append('⚠️  时间止损过多，建议延长持仓时间或优化出场策略')
        
        grade_analysis = self.analyze_by_grade()
        if 'S' in grade_analysis and grade_analysis['S'].get('win_rate', 0) < 0.6:
            recommendations.append('⚠️  S 级信号胜率不高，建议优化评分系统')
        
        if not recommendations:
            recommendations.append('✅ 策略表现良好，无明显问题')
        
        return recommendations
    
    def full_analysis(self) -> Dict:
        """完整分析"""
        return {
            'win_rate_analysis': self.analyze_win_rate(),
            'profit_loss_ratio_analysis': self.analyze_profit_loss_ratio(),
            'returns_analysis': self.analyze_returns(),
            'fee_analysis': self.analyze_fees(),
            'grade_analysis': self.analyze_by_grade(),
            'exit_reason_analysis': self.analyze_exit_reasons(),
            'symbol_analysis': self.analyze_by_symbol(),
            'performance_assessment': self.generate_assessment(),
            'recommendations': self.generate_recommendations()
        }
