"""
网格交易策略回测报告生成器
生成Markdown格式的回测报告
"""
from typing import Dict, List, Any, Optional
from datetime import datetime
import os
import structlog


logger = structlog.get_logger()


class ReportGenerator:
    """
    报告生成器

    职责:
    - 生成Markdown格式报告
    - 生成图表数据
    - 保存报告文件
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        初始化报告生成器

        Args:
            config: 配置字典(可选)
        """
        self.config = config or {}

        # 报告配置
        backtest_config = self.config.get('backtest', {})
        self.report_dir = backtest_config.get('report_dir', './reports')

        # 确保报告目录存在
        os.makedirs(self.report_dir, exist_ok=True)

        logger.info(
            "报告生成器初始化完成",
            report_dir=self.report_dir
        )

    def generate(
        self,
        statistics: Dict[str, Any],
        trades: List[Dict[str, Any]],
        equity_curve: List[Dict[str, Any]],
        analysis_result: Dict[str, Any],
        config: Dict[str, Any]
    ) -> str:
        """
        生成回测报告

        Args:
            statistics: 统计数据
            trades: 交易记录
            equity_curve: 资金曲线
            analysis_result: 性能分析结果
            config: 配置信息

        Returns:
            报告文件路径
        """
        logger.info("开始生成回测报告")

        try:
            # 生成报告内容
            report_content = self._generate_report_content(
                statistics=statistics,
                trades=trades,
                equity_curve=equity_curve,
                analysis_result=analysis_result,
                config=config
            )

            # 生成报告文件名
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            report_filename = f"backtest_report_{timestamp}.md"
            report_path = os.path.join(self.report_dir, report_filename)

            # 保存报告
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(report_content)

            logger.info(f"回测报告生成完成: {report_path}")

            return report_path

        except Exception as e:
            logger.error(f"生成回测报告失败: {e}", exc_info=True)
            raise

    def _generate_report_content(
        self,
        statistics: Dict[str, Any],
        trades: List[Dict[str, Any]],
        equity_curve: List[Dict[str, Any]],
        analysis_result: Dict[str, Any],
        config: Dict[str, Any]
    ) -> str:
        """
        生成报告内容

        Args:
            statistics: 统计数据
            trades: 交易记录
            equity_curve: 资金曲线
            analysis_result: 性能分析结果
            config: 配置信息

        Returns:
            报告内容字符串
        """
        # 报告头部
        header = self._generate_header(config)

        # 回测配置
        backtest_config = self._generate_backtest_config(config)

        # 性能分析
        performance = self._generate_performance_section(analysis_result)

        # 交易记录
        trade_records = self._generate_trade_records(trades)

        # 资金曲线
        equity_section = self._generate_equity_section(equity_curve)

        # 汇总报告
        report = f"""
{header}

{backtest_config}

{performance}

{trade_records}

{equity_section}

---

**报告生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""

        return report

    def _generate_header(self, config: Dict[str, Any]) -> str:
        """
        生成报告头部

        Args:
            config: 配置信息

        Returns:
            头部内容字符串
        """
        backtest_config = config.get('backtest', {})
        symbol = config.get('symbol', 'UNKNOWN')

        return f"""
# ETHUSDT网格交易策略回测报告

**策略名称**: {backtest_config.get('name', 'grid_trading')}
**交易对**: {symbol}
**回测版本**: {backtest_config.get('version', '1.0.0')}
**回测时间**: {backtest_config.get('start_date', 'N/A')} 至 {backtest_config.get('end_date', 'N/A')}
"""

    def _generate_backtest_config(self, config: Dict[str, Any]) -> str:
        """
        生成回测配置部分

        Args:
            config: 配置信息

        Returns:
            配置部分内容字符串
        """
        backtest_config = config.get('backtest', {})
        strategy_config = config.get('strategy', {})
        trading_config = strategy_config.get('trading', {})
        grid_config = strategy_config.get('grid', {})

        return f"""
## 1. 回测配置

### 1.1 基本参数

| 参数 | 值 |
|------|------|
| 初始资金 | {backtest_config.get('initial_balance', 0):.2f} USDT |
| 手续费率 | {backtest_config.get('commission_rate', 0) * 100:.2f}% |
| 滑点率 | {backtest_config.get('slippage_rate', 0) * 100:.2f}% |
| 杠杆倍数 | {trading_config.get('leverage', 1)}x |
| 保证金 | {trading_config.get('margin', 0):.2f} USDT |

### 1.2 网格参数

| 参数 | 值 |
|------|------|
| 网格类型 | {grid_config.get('type', 'arithmetic')} |
| 网格数量 | {grid_config.get('count', 0)} |
| 最小网格数 | {grid_config.get('min_grid_count', 0)} |
| 最大网格数 | {grid_config.get('max_grid_count', 0)} |
| 网格间距 | {grid_config.get('spacing', 0)} USDT |
"""

    def _generate_performance_section(self, analysis_result: Dict[str, Any]) -> str:
        """
        生成性能分析部分

        Args:
            analysis_result: 性能分析结果

        Returns:
            性能分析部分内容字符串
        """
        basic_stats = analysis_result.get('basic_stats', {})
        return_metrics = analysis_result.get('return_metrics', {})
        risk_metrics = analysis_result.get('risk_metrics', {})
        trade_stats = analysis_result.get('trade_stats', {})
        overall_score = analysis_result.get('overall_score', 0)

        return f"""
## 2. 性能分析

### 2.1 基础统计

| 指标 | 值 |
|------|------|
| 初始资金 | {basic_stats.get('initial_balance', 0):.2f} USDT |
| 最终资金 | {basic_stats.get('final_balance', 0):.2f} USDT |
| 总收益 | {basic_stats.get('total_return', 0):.2f} USDT |
| 总收益率 | {basic_stats.get('total_return_percent', 0):.2f}% |
| 交易天数 | {basic_stats.get('trading_days', 0)} 天 |

### 2.2 收益率指标

| 指标 | 值 |
|------|------|
| 年化收益率 | {return_metrics.get('annualized_return', 0):.2f}% |
| 月均收益率 | {return_metrics.get('monthly_return', 0):.2f}% |
| 日均收益率 | {return_metrics.get('daily_return_avg', 0):.4f}% |
| 日收益率标准差 | {return_metrics.get('daily_return_std', 0):.4f}% |

### 2.3 风险指标

| 指标 | 值 |
|------|------|
| 最大回撤 | {risk_metrics.get('max_drawdown', 0):.2f}% |
| 最大回撤持续 | {risk_metrics.get('max_drawdown_duration', 0)} 天 |
| 年化波动率 | {risk_metrics.get('volatility', 0):.2f}% |
| 夏普比率 | {risk_metrics.get('sharpe_ratio', 0):.2f} |
| 索提诺比率 | {risk_metrics.get('sortino_ratio', 0):.2f} |

### 2.4 交易统计

| 指标 | 值 |
|------|------|
| 总交易次数 | {trade_stats.get('total_trades', 0)} |
| 买入次数 | {trade_stats.get('buy_trades', 0)} |
| 卖出次数 | {trade_stats.get('sell_trades', 0)} |
| 总手续费 | {trade_stats.get('total_commission', 0):.2f} USDT |
| 平均交易规模 | {trade_stats.get('avg_trade_size', 0):.2f} USDT |

### 2.5 综合评分

**总分**: {overall_score}/100

评分说明:
- 90-100分: 优秀
- 80-89分: 良好
- 70-79分: 中等
- 60-69分: 及格
- 60分以下: 需要改进
"""

    def _generate_trade_records(self, trades: List[Dict[str, Any]]) -> str:
        """
        生成交易记录部分

        Args:
            trades: 交易记录列表

        Returns:
            交易记录部分内容字符串
        """
        if not trades:
            return """
## 3. 交易记录

无交易记录
"""

        # 只显示前20条和最后20条
        display_trades = trades[:20] + trades[-20:] if len(trades) > 40 else trades

        trade_table = "| 时间 | 方向 | 价格 | 数量 | 手续费 | 余额 |\n"
        trade_table += "|------|------|------|------|--------|------|\n"

        for trade in display_trades:
            trade_table += f"| {trade.get('time', 'N/A')} | {trade.get('side', 'N/A')} | {trade.get('price', 0):.2f} | {trade.get('quantity', 0):.4f} | {trade.get('commission', 0):.2f} | {trade.get('balance', 0):.2f} |\n"

        return f"""
## 3. 交易记录

总交易次数: {len(trades)}

{trade_table}

*注: 仅显示前20条和最后20条交易记录*
"""

    def _generate_equity_section(self, equity_curve: List[Dict[str, Any]]) -> str:
        """
        生成资金曲线部分

        Args:
            equity_curve: 资金曲线

        Returns:
            资金曲线部分内容字符串
        """
        if not equity_curve:
            return """
## 4. 资金曲线

无资金曲线数据
"""

        # 计算资金曲线统计
        balances = [point['balance'] for point in equity_curve]
        min_balance = min(balances)
        max_balance = max(balances)
        avg_balance = sum(balances) / len(balances)

        return f"""
## 4. 资金曲线

### 4.1 资金统计

| 指标 | 值 |
|------|------|
| 最低资金 | {min_balance:.2f} USDT |
| 最高资金 | {max_balance:.2f} USDT |
| 平均资金 | {avg_balance:.2f} USDT |
| 数据点数 | {len(equity_curve)} |

### 4.2 资金曲线数据

*注: 完整的资金曲线数据已保存到CSV文件中,可用于绘图分析*
"""
