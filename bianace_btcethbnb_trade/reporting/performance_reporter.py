#!/usr/bin/env python3
"""
绩效报告模块

整合 traderule.txt 9.2（定期复盘）和 10.2（绩效评估）：
1. 交易统计（胜率、盈亏比、最大回撤）
2. 绩效评估（优秀/良好/及格/不及格）
3. 周报/月报生成
4. 飞书通知集成

核心标准（10.2）：
- 胜率 > 45% → 良好
- 盈亏比 > 1.8 → 良好
- 最大回撤 < 10% → 良好
- 综合评估 = 胜率 + 盈亏比 + 回撤
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional
from collections import defaultdict
from config.strategy_params import StrategyParams, get_params

logger = logging.getLogger(__name__)


class PerformanceReporter:
    """绩效报告类"""
    
    def __init__(self, params: StrategyParams = None):
        """
        初始化绩效报告器
        
        Args:
            params: 策略参数
        """
        self.params = params or get_params()
        
        # 绩效评估标准（10.2）
        self.standards = {
            'win_rate': {
                'excellent': Decimal('0.55'),
                'good': Decimal('0.45'),
                'pass': Decimal('0.40')
            },
            'profit_loss_ratio': {
                'excellent': Decimal('2.5'),
                'good': Decimal('1.8'),
                'pass': Decimal('1.5')
            },
            'max_drawdown': {
                'excellent': Decimal('0.05'),
                'good': Decimal('0.10'),
                'pass': Decimal('0.15')
            }
        }
    
    def calculate_trade_statistics(
        self,
        trades: List[Dict[str, Any]],
        start_date: datetime = None,
        end_date: datetime = None
    ) -> Dict[str, Any]:
        """
        计算交易统计（9.1 交易记录）
        
        Args:
            trades: 交易记录列表
            start_date: 开始日期
            end_date: 结束日期
        
        Returns:
            交易统计字典
        """
        # 过滤日期范围
        filtered_trades = self._filter_trades_by_date(trades, start_date, end_date)
        
        if not filtered_trades:
            return self._empty_statistics()
        
        # 基础统计
        total_trades = len(filtered_trades)
        winning_trades = [t for t in filtered_trades if t.get('pnl', Decimal('0')) > 0]
        losing_trades = [t for t in filtered_trades if t.get('pnl', Decimal('0')) <= 0]
        
        win_count = len(winning_trades)
        loss_count = len(losing_trades)
        
        # 胜率
        win_rate = Decimal(win_count) / Decimal(total_trades) if total_trades > 0 else Decimal('0')
        
        # 盈亏统计
        total_pnl = sum(t.get('pnl', Decimal('0')) for t in filtered_trades)
        total_profit = sum(t.get('pnl', Decimal('0')) for t in winning_trades)
        total_loss = abs(sum(t.get('pnl', Decimal('0')) for t in losing_trades))
        
        # 平均盈利和亏损
        average_win = total_profit / win_count if win_count > 0 else Decimal('0')
        average_loss = total_loss / loss_count if loss_count > 0 else Decimal('0')
        
        # 盈亏比
        profit_loss_ratio = average_win / average_loss if average_loss > 0 else Decimal('0')
        
        # 最大连续盈利/亏损
        max_consecutive_wins = self._calculate_max_consecutive(filtered_trades, positive=True)
        max_consecutive_losses = self._calculate_max_consecutive(filtered_trades, positive=False)
        
        # 最大回撤
        max_drawdown = self._calculate_max_drawdown(filtered_trades)
        
        # 平均持仓时间
        average_holding_time = self._calculate_average_holding_time(filtered_trades)
        
        # 按币种统计
        symbol_stats = self._calculate_symbol_statistics(filtered_trades)
        
        # 按信号等级统计
        grade_stats = self._calculate_grade_statistics(filtered_trades)
        
        statistics = {
            'total_trades': total_trades,
            'winning_trades': win_count,
            'losing_trades': loss_count,
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'total_profit': total_profit,
            'total_loss': total_loss,
            'average_win': average_win,
            'average_loss': average_loss,
            'profit_loss_ratio': profit_loss_ratio,
            'max_consecutive_wins': max_consecutive_wins,
            'max_consecutive_losses': max_consecutive_losses,
            'max_drawdown': max_drawdown,
            'average_holding_time': average_holding_time,
            'symbol_stats': symbol_stats,
            'grade_stats': grade_stats,
            'period': {
                'start': start_date.isoformat() if start_date else None,
                'end': end_date.isoformat() if end_date else None
            }
        }
        
        logger.info(f"交易统计计算完成：{total_trades} 笔交易")
        return statistics
    
    def assess_performance(
        self,
        statistics: Dict[str, Any]
    ) -> Dict[str, str]:
        """
        绩效评估（10.2 标准）
        
        Args:
            statistics: 交易统计
        
        Returns:
            评估结果字典
        """
        assessment = {}
        
        win_rate = statistics['win_rate']
        profit_loss_ratio = statistics['profit_loss_ratio']
        max_drawdown = statistics['max_drawdown']
        
        # 胜率评估
        if win_rate >= self.standards['win_rate']['excellent']:
            assessment['win_rate'] = '优秀'
        elif win_rate >= self.standards['win_rate']['good']:
            assessment['win_rate'] = '良好'
        elif win_rate >= self.standards['win_rate']['pass']:
            assessment['win_rate'] = '及格'
        else:
            assessment['win_rate'] = '不及格'
        
        # 盈亏比评估
        if profit_loss_ratio >= self.standards['profit_loss_ratio']['excellent']:
            assessment['profit_loss_ratio'] = '优秀'
        elif profit_loss_ratio >= self.standards['profit_loss_ratio']['good']:
            assessment['profit_loss_ratio'] = '良好'
        elif profit_loss_ratio >= self.standards['profit_loss_ratio']['pass']:
            assessment['profit_loss_ratio'] = '及格'
        else:
            assessment['profit_loss_ratio'] = '不及格'
        
        # 回撤评估
        if max_drawdown <= self.standards['max_drawdown']['excellent']:
            assessment['max_drawdown'] = '优秀'
        elif max_drawdown <= self.standards['max_drawdown']['good']:
            assessment['max_drawdown'] = '良好'
        elif max_drawdown <= self.standards['max_drawdown']['pass']:
            assessment['max_drawdown'] = '及格'
        else:
            assessment['max_drawdown'] = '不及格'
        
        # 综合评估（加权平均）
        score_map = {'优秀': 4, '良好': 3, '及格': 2, '不及格': 1}
        scores = [
            score_map[assessment['win_rate']],
            score_map[assessment['profit_loss_ratio']],
            score_map[assessment['max_drawdown']]
        ]
        average_score = sum(scores) / len(scores)
        
        if average_score >= 3.5:
            assessment['overall'] = '优秀'
        elif average_score >= 2.5:
            assessment['overall'] = '良好'
        elif average_score >= 1.5:
            assessment['overall'] = '及格'
        else:
            assessment['overall'] = '不及格'
        
        return assessment
    
    def generate_weekly_report(
        self,
        trades: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        生成周报（9.2 定期复盘）
        
        Args:
            trades: 交易记录列表
        
        Returns:
            周报字典
        """
        # 计算本周交易统计
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)
        
        statistics = self.calculate_trade_statistics(trades, start_date, end_date)
        assessment = self.assess_performance(statistics)
        
        # 生成周报内容
        report = {
            'type': 'weekly',
            'timestamp': datetime.now().isoformat(),
            'period': {
                'start': start_date.strftime('%Y-%m-%d'),
                'end': end_date.strftime('%Y-%m-%d')
            },
            'summary': self._generate_summary(statistics, assessment),
            'statistics': statistics,
            'assessment': assessment,
            'highlights': self._generate_highlights(statistics),
            'improvements': self._generate_improvement_suggestions(statistics, assessment),
            'next_week_focus': self._generate_next_week_focus(assessment)
        }
        
        logger.info("周报生成完成")
        return report
    
    def generate_monthly_report(
        self,
        trades: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        生成月报（9.2 定期复盘）
        
        Args:
            trades: 交易记录列表
        
        Returns:
            月报字典
        """
        # 计算本月交易统计
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30)
        
        statistics = self.calculate_trade_statistics(trades, start_date, end_date)
        assessment = self.assess_performance(statistics)
        
        # 生成月报内容
        report = {
            'type': 'monthly',
            'timestamp': datetime.now().isoformat(),
            'period': {
                'start': start_date.strftime('%Y-%m-%d'),
                'end': end_date.strftime('%Y-%m-%d')
            },
            'summary': self._generate_summary(statistics, assessment),
            'statistics': statistics,
            'assessment': assessment,
            'trend_analysis': self._analyze_trend(trades, days=30),
            'parameter_review': self._review_parameters(statistics, assessment),
            'next_month_plan': self._generate_monthly_plan(assessment)
        }
        
        logger.info("月报生成完成")
        return report
    
    def format_lark_message(self, report: Dict[str, Any]) -> str:
        """
        格式化为飞书消息
        
        Args:
            report: 报告字典
        
        Returns:
            飞书消息文本
        """
        report_type = '周报' if report['type'] == 'weekly' else '月报'
        period = report['period']
        stats = report['statistics']
        assessment = report['assessment']
        
        # 标题
        message = f"📊 {report_type} ({period['start']} ~ {period['end']})\n\n"
        
        # 综合评估
        emoji_map = {'优秀': '🌟', '良好': '✅', '及格': '⚠️', '不及格': '❌'}
        message += f"综合评估：{emoji_map.get(assessment['overall'], '')} {assessment['overall']}\n\n"
        
        # 核心指标
        message += "📈 核心指标:\n"
        message += f"├─ 交易笔数：{stats['total_trades']} 笔\n"
        message += f"├─ 胜率：{stats['win_rate']:.1%} ({assessment['win_rate']})\n"
        message += f"├─ 盈亏比：{stats['profit_loss_ratio']:.2f} ({assessment['profit_loss_ratio']})\n"
        message += f"├─ 总盈亏：{stats['total_pnl']:.2f} U\n"
        message += f"└─ 最大回撤：{stats['max_drawdown']:.1%} ({assessment['max_drawdown']})\n\n"
        
        # 亮点
        if report.get('highlights'):
            message += "✨ 亮点:\n"
            for highlight in report['highlights'][:3]:
                message += f"├─ {highlight}\n"
            message += "\n"
        
        # 改进建议
        if report.get('improvements'):
            message += "💡 改进建议:\n"
            for improvement in report['improvements'][:3]:
                message += f"├─ {improvement}\n"
            message += "\n"
        
        # 下一步重点
        if report.get('next_week_focus') or report.get('next_month_plan'):
            focus = report.get('next_week_focus') or report.get('next_month_plan')
            message += "🎯 下一步重点:\n"
            for item in focus[:3]:
                message += f"├─ {item}\n"
        
        return message
    
    def _filter_trades_by_date(
        self,
        trades: List[Dict[str, Any]],
        start_date: datetime = None,
        end_date: datetime = None
    ) -> List[Dict[str, Any]]:
        """按日期过滤交易记录"""
        if not start_date and not end_date:
            return trades
        
        filtered = []
        for trade in trades:
            close_time = trade.get('close_time')
            if not close_time:
                continue
            
            trade_date = datetime.fromisoformat(close_time)
            
            if start_date and trade_date < start_date:
                continue
            if end_date and trade_date > end_date:
                continue
            
            filtered.append(trade)
        
        return filtered
    
    def _empty_statistics(self) -> Dict[str, Any]:
        """返回空统计"""
        return {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'win_rate': Decimal('0'),
            'total_pnl': Decimal('0'),
            'total_profit': Decimal('0'),
            'total_loss': Decimal('0'),
            'average_win': Decimal('0'),
            'average_loss': Decimal('0'),
            'profit_loss_ratio': Decimal('0'),
            'max_consecutive_wins': 0,
            'max_consecutive_losses': 0,
            'max_drawdown': Decimal('0'),
            'average_holding_time': timedelta(0),
            'symbol_stats': {},
            'grade_stats': {},
            'period': {}
        }
    
    def _calculate_max_consecutive(
        self,
        trades: List[Dict[str, Any]],
        positive: bool = True
    ) -> int:
        """计算最大连续盈利/亏损"""
        max_count = 0
        current_count = 0
        
        for trade in trades:
            pnl = trade.get('pnl', Decimal('0'))
            is_profit = pnl > 0
            
            if is_profit == positive:
                current_count += 1
                max_count = max(max_count, current_count)
            else:
                current_count = 0
        
        return max_count
    
    def _calculate_max_drawdown(self, trades: List[Dict[str, Any]]) -> Decimal:
        """计算最大回撤"""
        if not trades:
            return Decimal('0')
        
        peak = Decimal('0')
        max_drawdown = Decimal('0')
        cumulative_pnl = Decimal('0')
        
        for trade in trades:
            pnl = trade.get('pnl', Decimal('0'))
            cumulative_pnl += pnl
            
            # 更新峰值
            if cumulative_pnl > peak:
                peak = cumulative_pnl
            
            # 计算回撤
            if peak > 0:
                drawdown = (peak - cumulative_pnl) / peak
                max_drawdown = max(max_drawdown, drawdown)
        
        return max_drawdown
    
    def _calculate_average_holding_time(
        self,
        trades: List[Dict[str, Any]]
    ) -> timedelta:
        """计算平均持仓时间"""
        holding_times = []
        
        for trade in trades:
            open_time = trade.get('open_time')
            close_time = trade.get('close_time')
            
            if open_time and close_time:
                open_dt = datetime.fromisoformat(open_time)
                close_dt = datetime.fromisoformat(close_time)
                holding_time = close_dt - open_dt
                holding_times.append(holding_time)
        
        if not holding_times:
            return timedelta(0)
        
        return sum(holding_times, timedelta(0)) / len(holding_times)
    
    def _calculate_symbol_statistics(
        self,
        trades: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """按币种统计"""
        symbol_data = defaultdict(lambda: {
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'pnl': Decimal('0')
        })
        
        for trade in trades:
            symbol = trade.get('symbol', 'UNKNOWN')
            pnl = trade.get('pnl', Decimal('0'))
            
            symbol_data[symbol]['trades'] += 1
            symbol_data[symbol]['pnl'] += pnl
            
            if pnl > 0:
                symbol_data[symbol]['wins'] += 1
            else:
                symbol_data[symbol]['losses'] += 1
        
        # 计算胜率
        result = {}
        for symbol, data in symbol_data.items():
            win_rate = Decimal(data['wins']) / Decimal(data['trades']) if data['trades'] > 0 else Decimal('0')
            result[symbol] = {
                'trades': data['trades'],
                'wins': data['wins'],
                'losses': data['losses'],
                'total_pnl': data['pnl'],
                'win_rate': win_rate
            }
        
        return result
    
    def _calculate_grade_statistics(
        self,
        trades: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """按信号等级统计"""
        grade_data = defaultdict(lambda: {
            'trades': 0,
            'wins': 0,
            'losses': 0,
            'pnl': Decimal('0')
        })
        
        for trade in trades:
            grade = trade.get('signal_grade', 'UNKNOWN')
            pnl = trade.get('pnl', Decimal('0'))
            
            grade_data[grade]['trades'] += 1
            grade_data[grade]['pnl'] += pnl
            
            if pnl > 0:
                grade_data[grade]['wins'] += 1
            else:
                grade_data[grade]['losses'] += 1
        
        # 计算胜率
        result = {}
        for grade, data in grade_data.items():
            win_rate = Decimal(data['wins']) / Decimal(data['trades']) if data['trades'] > 0 else Decimal('0')
            result[grade] = {
                'trades': data['trades'],
                'wins': data['wins'],
                'losses': data['losses'],
                'total_pnl': data['pnl'],
                'win_rate': win_rate
            }
        
        return result
    
    def _generate_summary(
        self,
        statistics: Dict[str, Any],
        assessment: Dict[str, str]
    ) -> str:
        """生成摘要"""
        total_pnl = statistics['total_pnl']
        win_rate = statistics['win_rate']
        overall = assessment['overall']
        
        if total_pnl > 0:
            pnl_status = f"盈利 {total_pnl:.2f}U"
        else:
            pnl_status = f"亏损 {abs(total_pnl):.2f}U"
        
        return f"本期共{statistics['total_trades']}笔交易，{pnl_status}，胜率{win_rate:.1%}，综合评估：{overall}"
    
    def _generate_highlights(
        self,
        statistics: Dict[str, Any]
    ) -> List[str]:
        """生成亮点"""
        highlights = []
        
        # 胜率亮点
        if statistics['win_rate'] >= Decimal('0.55'):
            highlights.append(f"胜率优秀：{statistics['win_rate']:.1%}")
        
        # 盈亏比亮点
        if statistics['profit_loss_ratio'] >= Decimal('2.5'):
            highlights.append(f"盈亏比优秀：{statistics['profit_loss_ratio']:.2f}")
        
        # 连续盈利亮点
        if statistics['max_consecutive_wins'] >= 3:
            highlights.append(f"最大连续盈利：{statistics['max_consecutive_wins']}笔")
        
        # 币种亮点
        symbol_stats = statistics.get('symbol_stats', {})
        for symbol, data in symbol_stats.items():
            if data['win_rate'] >= Decimal('0.6') and data['trades'] >= 3:
                highlights.append(f"{symbol} 表现优异：胜率{data['win_rate']:.1%}")
        
        return highlights
    
    def _generate_improvement_suggestions(
        self,
        statistics: Dict[str, Any],
        assessment: Dict[str, str]
    ) -> List[str]:
        """生成改进建议"""
        suggestions = []
        
        # 胜率偏低
        if assessment.get('win_rate') in ['不及格', '及格']:
            suggestions.append("优化入场信号检测，提高胜率")
        
        # 盈亏比偏低
        if assessment.get('profit_loss_ratio') in ['不及格', '及格']:
            suggestions.append("调整止盈策略，提高盈亏比")
        
        # 回撤偏大
        if assessment.get('max_drawdown') in ['不及格', '及格']:
            suggestions.append("加强风险控制，降低回撤")
        
        return suggestions
    
    def _generate_next_week_focus(
        self,
        assessment: Dict[str, str]
    ) -> List[str]:
        """生成下周重点"""
        focus = []
        
        if assessment['overall'] == '优秀':
            focus.append("保持当前策略，继续稳定执行")
        elif assessment['overall'] == '良好':
            focus.append("微调参数，争取更优表现")
        elif assessment['overall'] == '及格':
            focus.append("重点优化薄弱环节")
        else:
            focus.append("全面复盘，考虑调整策略")
        
        return focus
    
    def _analyze_trend(
        self,
        trades: List[Dict[str, Any]],
        days: int = 30
    ) -> Dict[str, Any]:
        """分析趋势"""
        # 按周分组统计
        weekly_data = defaultdict(lambda: {'pnl': Decimal('0'), 'trades': 0})
        
        for trade in trades:
            close_time = trade.get('close_time')
            if not close_time:
                continue
            
            trade_date = datetime.fromisoformat(close_time)
            week_key = trade_date.isocalendar()[1]  # 周数
            
            weekly_data[week_key]['pnl'] += trade.get('pnl', Decimal('0'))
            weekly_data[week_key]['trades'] += 1
        
        return {
            'weekly_pnl': dict(weekly_data),
            'trend': 'up' if len(weekly_data) > 1 else 'stable'
        }
    
    def _review_parameters(
        self,
        statistics: Dict[str, Any],
        assessment: Dict[str, str]
    ) -> Dict[str, Any]:
        """参数审查"""
        review = {
            'need_adjustment': False,
            'suggestions': []
        }
        
        # 交易笔数不足
        if statistics['total_trades'] < 20:
            review['suggestions'].append("交易笔数不足，暂不调整参数")
        else:
            # 胜率偏低
            if statistics['win_rate'] < Decimal('0.45'):
                review['need_adjustment'] = True
                review['suggestions'].append("建议优化信号检测参数")
            
            # 盈亏比偏低
            if statistics['profit_loss_ratio'] < Decimal('1.8'):
                review['need_adjustment'] = True
                review['suggestions'].append("建议调整止盈策略")
        
        return review
    
    def _generate_monthly_plan(
        self,
        assessment: Dict[str, str]
    ) -> List[str]:
        """生成月度计划"""
        plan = []
        
        if assessment['overall'] in ['优秀', '良好']:
            plan.append("继续执行当前策略")
            plan.append("监控市场变化，适时微调")
        else:
            plan.append("全面复盘策略")
            plan.append("考虑参数优化")
            plan.append("加强风险控制")
        
        return plan


# 全局实例
_global_performance_reporter: Optional[PerformanceReporter] = None


def get_performance_reporter(params: StrategyParams = None) -> PerformanceReporter:
    """获取绩效报告器实例（单例模式）"""
    global _global_performance_reporter
    if _global_performance_reporter is None:
        _global_performance_reporter = PerformanceReporter(params)
    return _global_performance_reporter


# 便捷函数
def calculate_trade_statistics(
    trades: List[Dict[str, Any]],
    start_date: datetime = None,
    end_date: datetime = None
) -> Dict[str, Any]:
    """计算交易统计的便捷函数"""
    return get_performance_reporter().calculate_trade_statistics(trades, start_date, end_date)


def generate_weekly_report(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """生成周报的便捷函数"""
    return get_performance_reporter().generate_weekly_report(trades)


def generate_monthly_report(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """生成月报的便捷函数"""
    return get_performance_reporter().generate_monthly_report(trades)
