#!/usr/bin/env python3
"""
绩效报告模块

整合 traderule.txt 9.2 和 10.2 功能：
- 每周交易统计（胜率、盈亏比）
- 每月绩效评估
- 策略优化建议
- 飞书推送

使用方式:
    from services.performance_reporter import PerformanceReporter
    
    reporter = PerformanceReporter()
    weekly_report = reporter.generate_weekly_report()
    monthly_report = reporter.generate_monthly_report()
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional
import os

# 导入数据库模块
from models.database import get_db_manager, DatabaseManager

# 导入通知模块
from utils.lark_notifier import LarkNotifier
from config.settings import LARK_WEBHOOK_URL

logger = logging.getLogger('performance_reporter')


class PerformanceReporter:
    """绩效报告类"""
    
    def __init__(self):
        """初始化绩效报告器"""
        self.db = get_db_manager()
        self.lark_notifier = LarkNotifier(LARK_WEBHOOK_URL) if LARK_WEBHOOK_URL else None
        
        # 绩效评估标准（10.2）
        self.metrics = {
            'min_win_rate': Decimal('0.45'),  # 胜率 > 45% 可接受
            'good_win_rate': Decimal('0.55'),  # 胜率 > 55% 优秀
            'min_profit_loss_ratio': Decimal('1.8'),  # 盈亏比 > 1.8 可接受
            'good_profit_loss_ratio': Decimal('2.5'),  # 盈亏比 > 2.5 优秀
            'min_leverage_efficiency': Decimal('0.5'),  # 杠杆效率 > 0.5
            'max_bankruptcy_rate': Decimal('0.15'),  # 爆仓率 < 15%
            'max_drawdown': Decimal('0.25'),  # 最大回撤 < 25%
        }
        
        logger.info("绩效报告器初始化完成")
    
    def generate_weekly_report(self) -> Dict[str, Any]:
        """
        生成周报（整合 9.2 和 10.2）
        
        Returns:
            周报数据字典
        """
        logger.info("生成周报...")
        
        # 获取本周数据
        now = datetime.now()
        monday = now - timedelta(days=now.weekday())
        start_of_week = datetime(monday.year, monday.month, monday.day)
        
        # 获取本周平仓记录
        closed_trades = self.db.get_closed_positions(start_time=start_of_week)
        
        # 计算统计数据
        stats = self._calculate_statistics(closed_trades)
        
        # 生成报告
        report = {
            'report_type': 'WEEKLY',
            'period_start': start_of_week.strftime('%Y-%m-%d'),
            'period_end': now.strftime('%Y-%m-%d'),
            'generation_time': now.strftime('%Y-%m-%d %H:%M:%S'),
            'total_trades': stats['total_trades'],
            'winning_trades': stats['winning_trades'],
            'losing_trades': stats['losing_trades'],
            'win_rate': stats['win_rate'],
            'total_net_pnl': stats['total_net_pnl'],
            'avg_pnl_rate': stats['avg_pnl_rate'],
            'profit_loss_ratio': stats['profit_loss_ratio'],
            'max_consecutive_wins': stats['max_consecutive_wins'],
            'max_consecutive_losses': stats['max_consecutive_losses'],
            'evaluation': self._evaluate_performance(stats),
        }
        
        logger.info(f"周报生成完成：{stats['total_trades']} 笔交易")
        
        return report
    
    def generate_monthly_report(self) -> Dict[str, Any]:
        """
        生成月报
        
        Returns:
            月报数据字典
        """
        logger.info("生成月报...")
        
        # 获取本月数据
        now = datetime.now()
        start_of_month = datetime(now.year, now.month, 1)
        
        # 获取本月平仓记录
        closed_trades = self.db.get_closed_positions(start_time=start_of_month)
        
        # 计算统计数据
        stats = self._calculate_statistics(closed_trades)
        
        # 生成报告
        report = {
            'report_type': 'MONTHLY',
            'period_start': start_of_month.strftime('%Y-%m-%d'),
            'period_end': now.strftime('%Y-%m-%d'),
            'generation_time': now.strftime('%Y-%m-%d %H:%M:%S'),
            'total_trades': stats['total_trades'],
            'winning_trades': stats['winning_trades'],
            'losing_trades': stats['losing_trades'],
            'win_rate': stats['win_rate'],
            'total_net_pnl': stats['total_net_pnl'],
            'avg_pnl_rate': stats['avg_pnl_rate'],
            'profit_loss_ratio': stats['profit_loss_ratio'],
            'max_consecutive_wins': stats['max_consecutive_wins'],
            'max_consecutive_losses': stats['max_consecutive_losses'],
            'evaluation': self._evaluate_performance(stats),
        }
        
        logger.info(f"月报生成完成：{stats['total_trades']} 笔交易")
        
        return report
    
    def _calculate_statistics(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        计算统计数据
        
        Args:
            trades: 交易列表
        
        Returns:
            统计数据字典
        """
        if not trades:
            return {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': Decimal('0'),
                'total_net_pnl': Decimal('0'),
                'avg_pnl_rate': Decimal('0'),
                'profit_loss_ratio': Decimal('0'),
                'max_consecutive_wins': 0,
                'max_consecutive_losses': 0,
            }
        
        total_trades = len(trades)
        winning_trades = sum(1 for t in trades if t.get('net_pnl', Decimal('0')) > 0)
        losing_trades = sum(1 for t in trades if t.get('net_pnl', Decimal('0')) <= 0)
        
        # 胜率
        win_rate = Decimal(str(winning_trades)) / Decimal(str(total_trades)) if total_trades > 0 else Decimal('0')
        
        # 总盈亏
        total_net_pnl = sum(Decimal(str(t.get('net_pnl', '0'))) for t in trades)
        
        # 平均收益率
        pnl_rates = [Decimal(str(t.get('pnl_rate', '0'))) for t in trades]
        avg_pnl_rate = sum(pnl_rates) / len(pnl_rates) if pnl_rates else Decimal('0')
        
        # 盈亏比
        winning_pnl = [Decimal(str(t.get('net_pnl', '0'))) for t in trades if t.get('net_pnl', Decimal('0')) > 0]
        losing_pnl = [abs(Decimal(str(t.get('net_pnl', '0')))) for t in trades if t.get('net_pnl', Decimal('0')) <= 0]
        
        avg_win = sum(winning_pnl) / len(winning_pnl) if winning_pnl else Decimal('0')
        avg_loss = sum(losing_pnl) / len(losing_pnl) if losing_pnl else Decimal('0')
        
        profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else Decimal('0')
        
        # 计算连续盈亏
        max_consecutive_wins, max_consecutive_losses = self._calculate_consecutive_trades(trades)
        
        return {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': win_rate,
            'total_net_pnl': total_net_pnl,
            'avg_pnl_rate': avg_pnl_rate,
            'profit_loss_ratio': profit_loss_ratio,
            'max_consecutive_wins': max_consecutive_wins,
            'max_consecutive_losses': max_consecutive_losses,
        }
    
    def _calculate_consecutive_trades(self, trades: List[Dict[str, Any]]) -> tuple:
        """
        计算最大连续盈利/亏损次数
        
        Args:
            trades: 交易列表（按时间排序）
        
        Returns:
            (最大连续盈利次数，最大连续亏损次数)
        """
        if not trades:
            return 0, 0
        
        max_wins = 0
        max_losses = 0
        current_wins = 0
        current_losses = 0
        
        for trade in trades:
            net_pnl = Decimal(str(trade.get('net_pnl', '0')))
            
            if net_pnl > 0:
                current_wins += 1
                current_losses = 0
                max_wins = max(max_wins, current_wins)
            else:
                current_losses += 1
                current_wins = 0
                max_losses = max(max_losses, current_losses)
        
        return max_wins, max_losses
    
    def _evaluate_performance(self, stats: Dict[str, Any]) -> Dict[str, Any]:
        """
        评估交易表现（基于 10.2 标准）
        
        Args:
            stats: 统计数据
        
        Returns:
            评估结果
        """
        evaluation = {
            'win_rate_level': 'POOR',
            'profit_loss_ratio_level': 'POOR',
            'overall_rating': 'NEEDS_IMPROVEMENT',
            'suggestions': []
        }
        
        win_rate = stats['win_rate']
        profit_loss_ratio = stats['profit_loss_ratio']
        
        # 评估胜率
        if win_rate >= self.metrics['good_win_rate']:
            evaluation['win_rate_level'] = 'EXCELLENT'
        elif win_rate >= self.metrics['min_win_rate']:
            evaluation['win_rate_level'] = 'ACCEPTABLE'
        
        # 评估盈亏比
        if profit_loss_ratio >= self.metrics['good_profit_loss_ratio']:
            evaluation['profit_loss_ratio_level'] = 'EXCELLENT'
        elif profit_loss_ratio >= self.metrics['min_profit_loss_ratio']:
            evaluation['profit_loss_ratio_level'] = 'ACCEPTABLE'
        
        # 综合评级
        if (evaluation['win_rate_level'] == 'EXCELLENT' and 
            evaluation['profit_loss_ratio_level'] == 'EXCELLENT'):
            evaluation['overall_rating'] = 'EXCELLENT'
        elif (evaluation['win_rate_level'] in ['EXCELLENT', 'ACCEPTABLE'] and
              evaluation['profit_loss_ratio_level'] in ['EXCELLENT', 'ACCEPTABLE']):
            evaluation['overall_rating'] = 'GOOD'
        elif (evaluation['win_rate_level'] != 'POOR' or 
              evaluation['profit_loss_ratio_level'] != 'POOR'):
            evaluation['overall_rating'] = 'NEEDS_IMPROVEMENT'
        
        # 生成建议
        if win_rate < self.metrics['min_win_rate']:
            evaluation['suggestions'].append('胜率偏低，建议优化入场信号质量')
        
        if profit_loss_ratio < self.metrics['min_profit_loss_ratio']:
            evaluation['suggestions'].append('盈亏比偏低，建议调整止盈止损策略')
        
        if stats['max_consecutive_losses'] > 3:
            evaluation['suggestions'].append(f'连续亏损次数过多（{stats["max_consecutive_losses"]}次），建议加强风险控制')
        
        if stats['total_trades'] < 10:
            evaluation['suggestions'].append('交易样本不足，建议继续积累交易数据')
        
        return evaluation
    
    def format_report_message(self, report: Dict[str, Any]) -> str:
        """
        格式化报告消息（用于飞书推送）
        
        Args:
            report: 报告数据
        
        Returns:
            格式化的消息字符串
        """
        period_type = '周报' if report['report_type'] == 'WEEKLY' else '月报'
        
        message = f"📊 {period_type} ({report['period_start']} 至 {report['period_end']})\n\n"
        
        message += "📈 交易统计\n"
        message += f"├─ 完成交易：{report['total_trades']} 笔\n"
        message += f"├─ 盈利次数：{report['winning_trades']} 笔\n"
        message += f"├─ 亏损次数：{report['losing_trades']} 笔\n"
        message += f"└─ 胜率：{'🎯' if report['win_rate'] >= self.metrics['good_win_rate'] else '📊'} {report['win_rate']:.1%}\n\n"
        
        message += "💰 盈亏统计\n"
        pnl_icon = '🟢' if report['total_net_pnl'] > 0 else '🔴' if report['total_net_pnl'] < 0 else '⚪'
        message += f"├─ {pnl_icon} 总盈亏：{report['total_net_pnl']:.2f} USDT\n"
        message += f"├─ 平均收益率：{report['avg_pnl_rate']:.2f}%\n"
        
        plr_icon = '🎯' if report['profit_loss_ratio'] >= self.metrics['good_profit_loss_ratio'] else '📊'
        message += f"└─ {plr_icon} 盈亏比：{report['profit_loss_ratio']:.2f}\n\n"
        
        message += "📊 连续统计\n"
        message += f"├─ 最大连胜：{report['max_consecutive_wins']} 次\n"
        message += f"└─ 最大连败：{report['max_consecutive_losses']} 次\n\n"
        
        # 评估结果
        eval_data = report.get('evaluation', {})
        rating = eval_data.get('overall_rating', 'UNKNOWN')
        
        if rating == 'EXCELLENT':
            message += "⭐ 综合评级：优秀 🌟\n"
        elif rating == 'GOOD':
            message += "⭐ 综合评级：良好 👍\n"
        else:
            message += "⭐ 综合评级：待改进 💪\n"
        
        # 建议
        suggestions = eval_data.get('suggestions', [])
        if suggestions:
            message += "\n💡 优化建议:\n"
            for i, suggestion in enumerate(suggestions[:3], 1):
                message += f"{i}. {suggestion}\n"
        
        message += f"\n生成时间：{report['generation_time']}"
        
        return message
    
    def send_weekly_report(self, report: Dict[str, Any] = None):
        """
        发送周报（飞书推送）
        
        Args:
            report: 报告数据（如果为 None 则自动生成）
        """
        if report is None:
            report = self.generate_weekly_report()
        
        if not self.lark_notifier:
            logger.warning("飞书通知未配置，无法发送报告")
            return
        
        message = self.format_report_message(report)
        self.lark_notifier.send_text_message(message)
        
        logger.info("周报已发送")
    
    def send_monthly_report(self, report: Dict[str, Any] = None):
        """
        发送月报（飞书推送）
        
        Args:
            report: 报告数据（如果为 None 则自动生成）
        """
        if report is None:
            report = self.generate_monthly_report()
        
        if not self.lark_notifier:
            logger.warning("飞书通知未配置，无法发送报告")
            return
        
        message = self.format_report_message(report)
        self.lark_notifier.send_text_message(message)
        
        logger.info("月报已发送")


# 全局实例
_global_reporter: Optional[PerformanceReporter] = None


def get_performance_reporter() -> PerformanceReporter:
    """获取绩效报告器实例（单例模式）"""
    global _global_reporter
    if _global_reporter is None:
        _global_reporter = PerformanceReporter()
    return _global_reporter


# 便捷函数
def generate_weekly_report() -> Dict[str, Any]:
    """生成周报的便捷函数"""
    return get_performance_reporter().generate_weekly_report()


def generate_monthly_report() -> Dict[str, Any]:
    """生成月报的便捷函数"""
    return get_performance_reporter().generate_monthly_report()


def send_weekly_report():
    """发送周报的便捷函数"""
    get_performance_reporter().send_weekly_report()


def send_monthly_report():
    """发送月报的便捷函数"""
    get_performance_reporter().send_monthly_report()
