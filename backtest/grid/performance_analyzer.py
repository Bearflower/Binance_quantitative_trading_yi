"""
网格交易策略回测性能分析器
计算回测性能指标,包括收益率、风险指标、交易统计等
"""
from typing import Dict, List, Any, Optional
from decimal import Decimal
from datetime import datetime
import pandas as pd
import numpy as np
import structlog


logger = structlog.get_logger()


class PerformanceAnalyzer:
    """
    性能分析器

    职责:
    - 计算收益率指标
    - 计算风险指标
    - 计算交易统计
    - 生成性能报告
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        初始化性能分析器

        Args:
            config: 配置字典(可选)
        """
        self.config = config or {}

        # 分析配置
        analysis_config = self.config.get('analysis', {})
        self.calculate_sharpe_ratio = analysis_config.get('calculate_sharpe_ratio', True)
        self.risk_free_rate = Decimal(str(analysis_config.get('risk_free_rate', 0.02)))
        self.calculate_max_drawdown = analysis_config.get('calculate_max_drawdown', True)
        self.calculate_win_rate = analysis_config.get('calculate_win_rate', True)
        self.calculate_profit_loss_ratio = analysis_config.get('calculate_profit_loss_ratio', True)

        logger.info(
            "性能分析器初始化完成",
            calculate_sharpe_ratio=self.calculate_sharpe_ratio,
            risk_free_rate=float(self.risk_free_rate)
        )

    def analyze(
        self,
        trades: List[Dict[str, Any]],
        equity_curve: List[Dict[str, Any]],
        initial_balance: Decimal
    ) -> Dict[str, Any]:
        """
        分析回测性能

        Args:
            trades: 交易记录列表
            equity_curve: 资金曲线
            initial_balance: 初始资金

        Returns:
            性能分析结果字典
        """
        logger.info("开始性能分析")

        try:
            # 1. 基础统计
            basic_stats = self._calculate_basic_stats(trades, equity_curve, initial_balance)

            # 2. 收益率指标
            return_metrics = self._calculate_return_metrics(equity_curve, initial_balance)

            # 3. 风险指标
            risk_metrics = self._calculate_risk_metrics(equity_curve, initial_balance)

            # 4. 交易统计
            trade_stats = self._calculate_trade_stats(trades)

            # 5. 综合评分
            overall_score = self._calculate_overall_score(
                basic_stats,
                return_metrics,
                risk_metrics,
                trade_stats
            )

            # 汇总结果
            result = {
                'basic_stats': basic_stats,
                'return_metrics': return_metrics,
                'risk_metrics': risk_metrics,
                'trade_stats': trade_stats,
                'overall_score': overall_score
            }

            logger.info("性能分析完成", overall_score=overall_score)

            return result

        except Exception as e:
            logger.error(f"性能分析失败: {e}", exc_info=True)
            raise

    def _calculate_basic_stats(
        self,
        trades: List[Dict[str, Any]],
        equity_curve: List[Dict[str, Any]],
        initial_balance: Decimal
    ) -> Dict[str, Any]:
        """
        计算基础统计指标

        Args:
            trades: 交易记录列表
            equity_curve: 资金曲线
            initial_balance: 初始资金

        Returns:
            基础统计指标字典
        """
        if not equity_curve:
            return {
                'initial_balance': float(initial_balance),
                'final_balance': float(initial_balance),
                'total_return': 0,
                'total_return_percent': 0,
                'trading_days': 0
            }

        final_balance = Decimal(str(equity_curve[-1]['balance']))
        total_return = final_balance - initial_balance
        total_return_percent = (total_return / initial_balance) * 100

        # 计算交易天数
        start_time = datetime.fromisoformat(equity_curve[0]['time'])
        end_time = datetime.fromisoformat(equity_curve[-1]['time'])
        trading_days = (end_time - start_time).days

        return {
            'initial_balance': float(initial_balance),
            'final_balance': float(final_balance),
            'total_return': float(total_return),
            'total_return_percent': float(total_return_percent),
            'trading_days': trading_days
        }

    def _calculate_return_metrics(
        self,
        equity_curve: List[Dict[str, Any]],
        initial_balance: Decimal
    ) -> Dict[str, Any]:
        """
        计算收益率指标

        Args:
            equity_curve: 资金曲线
            initial_balance: 初始资金

        Returns:
            收益率指标字典
        """
        if not equity_curve or len(equity_curve) < 2:
            return {
                'annualized_return': 0,
                'monthly_return': 0,
                'daily_return_avg': 0,
                'daily_return_std': 0
            }

        # 转换为DataFrame
        df = pd.DataFrame(equity_curve)
        df['time'] = pd.to_datetime(df['time'])
        df = df.set_index('time')

        # 计算日收益率
        df['daily_return'] = df['balance'].pct_change()

        # 计算年化收益率
        total_days = (df.index[-1] - df.index[0]).days
        if total_days > 0:
            total_return = (Decimal(str(df['balance'].iloc[-1])) - initial_balance) / initial_balance
            annualized_return = float(total_return) * (365 / total_days)
        else:
            annualized_return = 0

        # 计算月收益率
        monthly_return = annualized_return / 12 if annualized_return else 0

        # 计算日收益率统计
        daily_return_avg = float(df['daily_return'].mean()) if not df['daily_return'].empty else 0
        daily_return_std = float(df['daily_return'].std()) if not df['daily_return'].empty else 0

        return {
            'annualized_return': annualized_return * 100,
            'monthly_return': monthly_return * 100,
            'daily_return_avg': daily_return_avg * 100,
            'daily_return_std': daily_return_std * 100
        }

    def _calculate_risk_metrics(
        self,
        equity_curve: List[Dict[str, Any]],
        initial_balance: Decimal
    ) -> Dict[str, Any]:
        """
        计算风险指标

        Args:
            equity_curve: 资金曲线
            initial_balance: 初始资金

        Returns:
            风险指标字典
        """
        if not equity_curve or len(equity_curve) < 2:
            return {
                'max_drawdown': 0,
                'max_drawdown_duration': 0,
                'volatility': 0,
                'sharpe_ratio': 0,
                'sortino_ratio': 0
            }

        # 转换为DataFrame
        df = pd.DataFrame(equity_curve)
        df['time'] = pd.to_datetime(df['time'])
        df = df.set_index('time')

        # 计算最大回撤
        df['cummax'] = df['balance'].cummax()
        df['drawdown'] = (df['cummax'] - df['balance']) / df['cummax']
        max_drawdown = float(df['drawdown'].max())

        # 计算最大回撤持续时间
        drawdown_periods = df[df['drawdown'] > 0]
        if not drawdown_periods.empty:
            max_drawdown_duration = (drawdown_periods.index[-1] - drawdown_periods.index[0]).days
        else:
            max_drawdown_duration = 0

        # 计算波动率
        df['daily_return'] = df['balance'].pct_change()
        volatility = float(df['daily_return'].std()) * np.sqrt(252) if not df['daily_return'].empty else 0

        # 计算夏普比率
        if self.calculate_sharpe_ratio and volatility > 0:
            daily_return_avg = df['daily_return'].mean()
            annualized_return = daily_return_avg * 252
            sharpe_ratio = (annualized_return - float(self.risk_free_rate)) / volatility
        else:
            sharpe_ratio = 0

        # 计算索提诺比率
        negative_returns = df[df['daily_return'] < 0]['daily_return']
        if not negative_returns.empty:
            downside_std = float(negative_returns.std()) * np.sqrt(252)
            if downside_std > 0:
                daily_return_avg = df['daily_return'].mean()
                annualized_return = daily_return_avg * 252
                sortino_ratio = (annualized_return - float(self.risk_free_rate)) / downside_std
            else:
                sortino_ratio = 0
        else:
            sortino_ratio = 0

        return {
            'max_drawdown': max_drawdown * 100,
            'max_drawdown_duration': max_drawdown_duration,
            'volatility': volatility * 100,
            'sharpe_ratio': sharpe_ratio,
            'sortino_ratio': sortino_ratio
        }

    def _calculate_trade_stats(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        计算交易统计指标

        Args:
            trades: 交易记录列表

        Returns:
            交易统计指标字典
        """
        if not trades:
            return {
                'total_trades': 0,
                'buy_trades': 0,
                'sell_trades': 0,
                'total_commission': 0,
                'avg_trade_size': 0
            }

        # 统计买卖次数
        buy_trades = [t for t in trades if t['side'] == 'BUY']
        sell_trades = [t for t in trades if t['side'] == 'SELL']

        # 计算总手续费
        total_commission = sum(t.get('commission', 0) for t in trades)

        # 计算平均交易规模
        avg_trade_size = sum(t.get('quantity', 0) * t.get('price', 0) for t in trades) / len(trades)

        return {
            'total_trades': len(trades),
            'buy_trades': len(buy_trades),
            'sell_trades': len(sell_trades),
            'total_commission': total_commission,
            'avg_trade_size': avg_trade_size
        }

    def _calculate_overall_score(
        self,
        basic_stats: Dict[str, Any],
        return_metrics: Dict[str, Any],
        risk_metrics: Dict[str, Any],
        trade_stats: Dict[str, Any]
    ) -> float:
        """
        计算综合评分

        Args:
            basic_stats: 基础统计指标
            return_metrics: 收益率指标
            risk_metrics: 风险指标
            trade_stats: 交易统计指标

        Returns:
            综合评分(0-100)
        """
        # 收益率得分(40%)
        annualized_return = return_metrics.get('annualized_return', 0)
        if annualized_return > 50:
            return_score = 100
        elif annualized_return > 30:
            return_score = 80
        elif annualized_return > 10:
            return_score = 60
        elif annualized_return > 0:
            return_score = 40
        else:
            return_score = 0

        # 风险控制得分(40%)
        max_drawdown = risk_metrics.get('max_drawdown', 0)
        if max_drawdown < 5:
            risk_score = 100
        elif max_drawdown < 10:
            risk_score = 80
        elif max_drawdown < 20:
            risk_score = 60
        elif max_drawdown < 30:
            risk_score = 40
        else:
            risk_score = 0

        # 交易活跃度得分(20%)
        total_trades = trade_stats.get('total_trades', 0)
        if total_trades > 100:
            activity_score = 100
        elif total_trades > 50:
            activity_score = 80
        elif total_trades > 20:
            activity_score = 60
        elif total_trades > 10:
            activity_score = 40
        else:
            activity_score = 0

        # 综合评分
        overall_score = return_score * 0.4 + risk_score * 0.4 + activity_score * 0.2

        return round(overall_score, 2)

    def generate_summary(self, analysis_result: Dict[str, Any]) -> str:
        """
        生成性能分析摘要

        Args:
            analysis_result: 性能分析结果

        Returns:
            摘要文本
        """
        basic_stats = analysis_result.get('basic_stats', {})
        return_metrics = analysis_result.get('return_metrics', {})
        risk_metrics = analysis_result.get('risk_metrics', {})
        trade_stats = analysis_result.get('trade_stats', {})
        overall_score = analysis_result.get('overall_score', 0)

        summary = f"""
性能分析摘要
{'=' * 60}

【基础统计】
- 初始资金: {basic_stats.get('initial_balance', 0):.2f} USDT
- 最终资金: {basic_stats.get('final_balance', 0):.2f} USDT
- 总收益: {basic_stats.get('total_return', 0):.2f} USDT ({basic_stats.get('total_return_percent', 0):.2f}%)
- 交易天数: {basic_stats.get('trading_days', 0)} 天

【收益率指标】
- 年化收益率: {return_metrics.get('annualized_return', 0):.2f}%
- 月均收益率: {return_metrics.get('monthly_return', 0):.2f}%
- 日均收益率: {return_metrics.get('daily_return_avg', 0):.4f}%
- 日收益率标准差: {return_metrics.get('daily_return_std', 0):.4f}%

【风险指标】
- 最大回撤: {risk_metrics.get('max_drawdown', 0):.2f}%
- 最大回撤持续: {risk_metrics.get('max_drawdown_duration', 0)} 天
- 年化波动率: {risk_metrics.get('volatility', 0):.2f}%
- 夏普比率: {risk_metrics.get('sharpe_ratio', 0):.2f}
- 索提诺比率: {risk_metrics.get('sortino_ratio', 0):.2f}

【交易统计】
- 总交易次数: {trade_stats.get('total_trades', 0)}
- 买入次数: {trade_stats.get('buy_trades', 0)}
- 卖出次数: {trade_stats.get('sell_trades', 0)}
- 总手续费: {trade_stats.get('total_commission', 0):.2f} USDT
- 平均交易规模: {trade_stats.get('avg_trade_size', 0):.2f} USDT

【综合评分】
- 总分: {overall_score}/100

{'=' * 60}
"""

        return summary
