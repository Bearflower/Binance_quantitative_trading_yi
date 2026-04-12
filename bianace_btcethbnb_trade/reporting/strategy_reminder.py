#!/usr/bin/env python3
"""
策略优化提醒模块

基于 traderule.txt 9.3 实现策略优化提醒功能：
1. 交易笔数统计
2. 胜率统计
3. 盈亏比统计
4. 参数调整提醒
5. 策略优化建议

核心规则（9.3）：
- 交易笔数 < 20 → 不调整参数
- 胜率 < 40% → 建议优化入场信号
- 盈亏比 < 1.8 → 建议优化止盈策略
- 连续亏损 > 5 笔 → 建议暂停交易并复盘
"""

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Any, List, Optional
from config.strategy_params import StrategyParams, get_params

logger = logging.getLogger(__name__)


class StrategyReminder:
    """策略优化提醒类"""
    
    def __init__(self, params: StrategyParams = None):
        """
        初始化策略优化提醒器
        
        Args:
            params: 策略参数
        """
        self.params = params or get_params()
        
        # 绩效评估标准（10.2）
        self.performance_standards = {
            'min_trades': self.params.get('performance.min_trades_for_adjustment', 20),
            'target_win_rate': self.params.get('performance.target_win_rate', Decimal('0.45')),
            'target_profit_loss_ratio': self.params.get('performance.target_profit_loss_ratio', Decimal('1.8')),
            'max_consecutive_losses': self.params.get('performance.max_consecutive_losses', 5)
        }
    
    def analyze_trades(
        self,
        trades: List[Dict[str, Any]],
        period_days: int = 30
    ) -> Dict[str, Any]:
        """
        分析交易记录（9.1 交易记录）
        
        Args:
            trades: 交易记录列表
            period_days: 统计周期（天数）
        
        Returns:
            交易分析结果
        """
        # 过滤指定周期的交易
        cutoff_date = datetime.now() - timedelta(days=period_days)
        recent_trades = [
            trade for trade in trades
            if datetime.fromisoformat(trade.get('close_time', '2000-01-01')) >= cutoff_date
        ]
        
        if not recent_trades:
            return {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': Decimal('0'),
                'profit_loss_ratio': Decimal('0'),
                'total_pnl': Decimal('0'),
                'average_win': Decimal('0'),
                'average_loss': Decimal('0'),
                'consecutive_losses': 0,
                'period_days': period_days
            }
        
        # 统计盈亏
        winning_trades = [t for t in recent_trades if t.get('pnl', Decimal('0')) > 0]
        losing_trades = [t for t in recent_trades if t.get('pnl', Decimal('0')) <= 0]
        
        total_trades = len(recent_trades)
        win_count = len(winning_trades)
        loss_count = len(losing_trades)
        
        # 计算胜率
        win_rate = Decimal(win_count) / Decimal(total_trades) if total_trades > 0 else Decimal('0')
        
        # 计算总盈亏
        total_pnl = sum(t.get('pnl', Decimal('0')) for t in recent_trades)
        
        # 计算平均盈利和亏损
        average_win = sum(t.get('pnl', Decimal('0')) for t in winning_trades) / win_count if win_count > 0 else Decimal('0')
        average_loss = abs(sum(t.get('pnl', Decimal('0')) for t in losing_trades) / loss_count) if loss_count > 0 else Decimal('0')
        
        # 计算盈亏比
        profit_loss_ratio = average_win / average_loss if average_loss > 0 else Decimal('0')
        
        # 计算连续亏损
        consecutive_losses = self._calculate_consecutive_losses(recent_trades)
        
        analysis = {
            'total_trades': total_trades,
            'winning_trades': win_count,
            'losing_trades': loss_count,
            'win_rate': win_rate,
            'profit_loss_ratio': profit_loss_ratio,
            'total_pnl': total_pnl,
            'average_win': average_win,
            'average_loss': average_loss,
            'consecutive_losses': consecutive_losses,
            'period_days': period_days
        }
        
        logger.info(f"交易分析完成（{period_days}天）:")
        logger.info(f"  交易笔数：{total_trades}")
        logger.info(f"  胜率：{win_rate:.1%}")
        logger.info(f"  盈亏比：{profit_loss_ratio:.2f}")
        logger.info(f"  总盈亏：{total_pnl:.2f}U")
        
        return analysis
    
    def check_adjustment_need(
        self,
        analysis: Dict[str, Any]
    ) -> tuple[bool, List[str]]:
        """
        检查是否需要调整策略（9.3 策略优化提醒）
        
        Args:
            analysis: 交易分析结果
        
        Returns:
            (是否需要调整，建议列表)
        """
        suggestions = []
        need_adjustment = False
        
        total_trades = analysis['total_trades']
        win_rate = analysis['win_rate']
        profit_loss_ratio = analysis['profit_loss_ratio']
        consecutive_losses = analysis['consecutive_losses']
        
        # 规则 1: 交易笔数 < 20 → 不调整参数
        if total_trades < self.performance_standards['min_trades']:
            suggestions.append(
                f"📊 交易笔数不足 ({total_trades}/{self.performance_standards['min_trades']})\n"
                f"   建议：继续执行当前策略，积累更多交易数据"
            )
            return False, suggestions
        
        # 规则 2: 胜率 < 40% → 建议优化入场信号
        if win_rate < Decimal('0.4'):
            need_adjustment = True
            suggestions.append(
                f"📉 胜率偏低 ({win_rate:.1%} < 45%)\n"
                f"   建议：优化入场信号检测\n"
                f"   - 检查技术形态识别准确性\n"
                f"   - 调整信号等级判定标准\n"
                f"   - 加强趋势过滤器"
            )
        
        # 规则 3: 盈亏比 < 1.8 → 建议优化止盈策略
        if profit_loss_ratio < self.performance_standards['target_profit_loss_ratio']:
            need_adjustment = True
            suggestions.append(
                f"📉 盈亏比偏低 ({profit_loss_ratio:.2f} < {self.performance_standards['target_profit_loss_ratio']})\n"
                f"   建议：优化止盈策略\n"
                f"   - 调整止盈水平（TP1/TP2/TP3）\n"
                f"   - 优化移动止损规则\n"
                f"   - 考虑调整 R 值倍数"
            )
        
        # 规则 4: 连续亏损 > 5 笔 → 建议暂停交易并复盘
        if consecutive_losses >= self.performance_standards['max_consecutive_losses']:
            need_adjustment = True
            suggestions.append(
                f"🚨 连续亏损过多 ({consecutive_losses} 笔)\n"
                f"   建议：立即暂停交易并复盘\n"
                f"   - 检查市场环境是否适合策略\n"
                f"   - 审查近期交易记录\n"
                f"   - 考虑调整策略参数"
            )
        
        # 如果没有问题
        if not need_adjustment:
            suggestions.append(
                f"✅ 策略运行正常\n"
                f"   胜率：{win_rate:.1%} (目标 > 45%)\n"
                f"   盈亏比：{profit_loss_ratio:.2f} (目标 > 1.8)\n"
                f"   建议：继续执行当前策略"
            )
        
        return need_adjustment, suggestions
    
    def generate_performance_report(
        self,
        trades: List[Dict[str, Any]],
        period_days: int = 30
    ) -> Dict[str, Any]:
        """
        生成绩效报告（9.2 + 10.2）
        
        Args:
            trades: 交易记录列表
            period_days: 统计周期
        
        Returns:
            绩效报告字典
        """
        # 交易分析
        analysis = self.analyze_trades(trades, period_days)
        
        # 检查调整需求
        need_adjustment, suggestions = self.check_adjustment_need(analysis)
        
        # 绩效评估（10.2 标准）
        performance_assessment = self._assess_performance(analysis)
        
        report = {
            'timestamp': datetime.now().isoformat(),
            'period_days': period_days,
            'analysis': analysis,
            'need_adjustment': need_adjustment,
            'suggestions': suggestions,
            'performance_assessment': performance_assessment,
            'standards': self.performance_standards
        }
        
        logger.info("绩效报告生成完成")
        return report
    
    def _assess_performance(self, analysis: Dict[str, Any]) -> Dict[str, str]:
        """
        绩效评估（10.2 标准）
        
        Args:
            analysis: 交易分析结果
        
        Returns:
            评估结果字典
        """
        assessment = {}
        
        # 胜率评估
        win_rate = analysis['win_rate']
        if win_rate >= Decimal('0.55'):
            assessment['win_rate'] = '优秀'
        elif win_rate >= Decimal('0.45'):
            assessment['win_rate'] = '良好'
        elif win_rate >= Decimal('0.40'):
            assessment['win_rate'] = '及格'
        else:
            assessment['win_rate'] = '不及格'
        
        # 盈亏比评估
        profit_loss_ratio = analysis['profit_loss_ratio']
        if profit_loss_ratio >= Decimal('2.5'):
            assessment['profit_loss_ratio'] = '优秀'
        elif profit_loss_ratio >= Decimal('1.8'):
            assessment['profit_loss_ratio'] = '良好'
        elif profit_loss_ratio >= Decimal('1.5'):
            assessment['profit_loss_ratio'] = '及格'
        else:
            assessment['profit_loss_ratio'] = '不及格'
        
        # 综合评估
        if assessment['win_rate'] in ['优秀', '良好'] and assessment['profit_loss_ratio'] in ['优秀', '良好']:
            assessment['overall'] = '优秀'
        elif assessment['win_rate'] in ['及格'] or assessment['profit_loss_ratio'] in ['及格']:
            assessment['overall'] = '及格'
        else:
            assessment['overall'] = '不及格'
        
        return assessment
    
    def _calculate_consecutive_losses(self, trades: List[Dict[str, Any]]) -> int:
        """
        计算连续亏损次数
        
        Args:
            trades: 交易记录列表（按时间排序）
        
        Returns:
            最大连续亏损次数
        """
        max_consecutive = 0
        current_consecutive = 0
        
        for trade in trades:
            if trade.get('pnl', Decimal('0')) < 0:
                current_consecutive += 1
                max_consecutive = max(max_consecutive, current_consecutive)
            else:
                current_consecutive = 0
        
        return max_consecutive
    
    def get_parameter_adjustment_suggestions(
        self,
        analysis: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        获取参数调整建议（9.3）
        
        Args:
            analysis: 交易分析结果
        
        Returns:
            参数调整建议字典
        """
        suggestions = {}
        
        win_rate = analysis['win_rate']
        profit_loss_ratio = analysis['profit_loss_ratio']
        
        # 胜率偏低
        if win_rate < Decimal('0.45'):
            suggestions['signal_detection'] = {
                'current': '默认参数',
                'suggested': '更严格的信号条件',
                'reason': f'胜率偏低 ({win_rate:.1%})',
                'actions': [
                    '提高 S/A 级信号判定标准',
                    '增加 EMA21 趋势过滤权重',
                    '减少交易频率'
                ]
            }
        
        # 盈亏比偏低
        if profit_loss_ratio < Decimal('1.8'):
            suggestions['take_profit'] = {
                'current': 'TP1=1.5R, TP2=2.5R, TP3=移动止损',
                'suggested': 'TP1=2.0R, TP2=3.0R, TP3=移动止损',
                'reason': f'盈亏比偏低 ({profit_loss_ratio:.2f})',
                'actions': [
                    '提高止盈倍数',
                    '优化移动止损跟踪',
                    '调整仓位分配比例'
                ]
            }
        
        # 连续亏损过多
        if analysis.get('consecutive_losses', 0) >= 3:
            suggestions['risk_management'] = {
                'current': '单笔风险 10U',
                'suggested': '单笔风险 5U',
                'reason': f'连续亏损 {analysis["consecutive_losses"]} 笔',
                'actions': [
                    '降低单笔风险金额',
                    '降低杠杆倍数',
                    '暂停交易并复盘'
                ]
            }
        
        return suggestions


# 全局实例
_global_strategy_reminder: Optional[StrategyReminder] = None


def get_strategy_reminder(params: StrategyParams = None) -> StrategyReminder:
    """获取策略优化提醒器实例（单例模式）"""
    global _global_strategy_reminder
    if _global_strategy_reminder is None:
        _global_strategy_reminder = StrategyReminder(params)
    return _global_strategy_reminder


# 便捷函数
def analyze_trades(
    trades: List[Dict[str, Any]],
    period_days: int = 30
) -> Dict[str, Any]:
    """分析交易记录的便捷函数"""
    return get_strategy_reminder().analyze_trades(trades, period_days)


def check_adjustment_need(
    analysis: Dict[str, Any]
) -> tuple[bool, List[str]]:
    """检查是否需要调整策略的便捷函数"""
    return get_strategy_reminder().check_adjustment_need(analysis)


def generate_performance_report(
    trades: List[Dict[str, Any]],
    period_days: int = 30
) -> Dict[str, Any]:
    """生成绩效报告的便捷函数"""
    return get_strategy_reminder().generate_performance_report(trades, period_days)
