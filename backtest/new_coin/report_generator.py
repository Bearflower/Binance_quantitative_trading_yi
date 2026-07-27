"""
报告生成器
生成回测报告、创建可视化图表、导出交易记录
"""
from typing import Dict, List, Any
from datetime import datetime
import os
import csv
import structlog


logger = structlog.get_logger()


class ReportGenerator:
    """报告生成器
    
    职责：
    - 生成回测报告
    - 创建可视化图表
    - 导出交易记录
    """
    
    def __init__(self):
        """初始化报告生成器"""
        self.reports_dir = 'backtest/new_coin/reports'
        self.charts_dir = os.path.join(self.reports_dir, 'charts')
        
        # 确保目录存在
        os.makedirs(self.reports_dir, exist_ok=True)
        os.makedirs(self.charts_dir, exist_ok=True)
        
        logger.info(
            "报告生成器初始化完成",
            reports_dir=self.reports_dir,
            charts_dir=self.charts_dir
        )
    
    def generate(
        self,
        statistics: Dict[str, Any],
        trades: List[Dict[str, Any]],
        equity_curve: List[Dict[str, Any]],
        config: Dict[str, Any]
    ) -> str:
        """
        生成回测报告
        
        Args:
            statistics: 统计结果
            trades: 交易记录
            equity_curve: 资金曲线
            config: 配置字典
            
        Returns:
            报告文件路径
        """
        logger.info("开始生成回测报告")
        
        # 1. 生成Markdown报告
        report_path = self._generate_markdown_report(statistics, trades, config)
        
        # 2. 导出交易记录CSV
        trades_path = self._export_trades_csv(trades)
        
        # 3. 生成可视化图表
        self._generate_charts(equity_curve, trades)
        
        logger.info(f"回测报告生成完成: {report_path}")
        
        return report_path
    
    def _generate_markdown_report(
        self,
        statistics: Dict[str, Any],
        trades: List[Dict[str, Any]],
        config: Dict[str, Any]
    ) -> str:
        """
        生成Markdown报告
        
        Args:
            statistics: 统计结果
            trades: 交易记录
            config: 配置字典
            
        Returns:
            报告文件路径
        """
        report_path = os.path.join(self.reports_dir, 'backtest_report.md')
        
        # 生成报告内容
        content = self._build_report_content(statistics, trades, config)
        
        # 写入文件
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        logger.info(f"Markdown报告已生成: {report_path}")
        
        return report_path
    
    def _build_report_content(
        self,
        statistics: Dict[str, Any],
        trades: List[Dict[str, Any]],
        config: Dict[str, Any]
    ) -> str:
        """
        构建报告内容
        
        Args:
            statistics: 统计结果
            trades: 交易记录
            config: 配置字典
            
        Returns:
            报告内容字符串
        """
        # 报告标题
        content = f"""# 新币做空策略回测报告

**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

---

## 1. 回测概览

### 1.1 回测参数

| 参数 | 值 |
|------|------|
| 初始资金 | {config.get('backtest', {}).get('initial_balance', 500)} USDT |
| 手续费率 | {config.get('backtest', {}).get('commission_rate', 0.0004) * 100}% |
| 滑点率 | {config.get('backtest', {}).get('slippage_rate', 0.0001) * 100}% |
| 杠杆倍数 | {config.get('backtest', {}).get('leverage', 2)}x |
| 回测时间范围 | {config.get('backtest', {}).get('start_date', '2025-01-01')} ~ {config.get('backtest', {}).get('end_date', '2025-12-31')} |

### 1.2 核心指标

| 指标 | 数值 |
|------|------|
| **总交易次数** | {statistics['total_trades']} |
| **盈利交易** | {statistics['winning_trades']} |
| **亏损交易** | {statistics['losing_trades']} |
| **胜率** | {statistics['win_rate'] * 100:.2f}% |
| **总盈亏** | {statistics['total_pnl']:.2f} USDT |
| **总收益率** | {statistics['total_return'] * 100:.2f}% |
| **最终资金** | {statistics['final_balance']:.2f} USDT |

---

## 2. 盈亏分析

### 2.1 盈亏指标

| 指标 | 数值 |
|------|------|
| 平均盈亏 | {statistics['average_pnl']:.2f} USDT |
| 平均盈利 | {statistics['average_win']:.2f} USDT |
| 平均亏损 | {statistics['average_loss']:.2f} USDT |
| **盈亏比** | {statistics['profit_loss_ratio']:.2f} |
| **盈亏因子** | {statistics['profit_factor']:.2f} |

### 2.2 盈亏分布

"""
        
        # 盈亏分布统计
        if trades:
            pnl_list = [t['pnl'] for t in trades]
            max_pnl = max(pnl_list)
            min_pnl = min(pnl_list)
            median_pnl = sorted(pnl_list)[len(pnl_list) // 2]
            
            content += f"""| 统计项 | 数值 |
|--------|------|
| 最大盈利 | {max_pnl:.2f} USDT |
| 最大亏损 | {min_pnl:.2f} USDT |
| 中位数盈亏 | {median_pnl:.2f} USDT |

"""
        
        content += """---

## 3. 风险分析

### 3.1 风险指标

"""
        
        content += f"""| 指标 | 数值 |
|------|------|
| **最大回撤** | {statistics['max_drawdown']:.2f} USDT |
| **最大回撤率** | {statistics['max_drawdown_percent'] * 100:.2f}% |
| **夏普比率** | {statistics['sharpe_ratio']:.2f} |

---

## 4. 效率分析

### 4.1 效率指标

| 指标 | 数值 |
|------|------|
| 平均持仓时间 | {statistics['average_holding_hours']:.1f} 小时 |

---

## 5. 交易详情

### 5.1 交易记录摘要

"""
        
        # 添加交易记录表格（前10笔）
        if trades:
            content += """| 交易对 | 开仓时间 | 开仓价格 | 平仓时间 | 平仓价格 | 数量 | 盈亏 | 平仓原因 |
|--------|----------|----------|----------|----------|------|------|----------|
"""
            
            for trade in trades[:10]:
                content += f"| {trade['symbol']} | {trade['entry_time'].strftime('%Y-%m-%d %H:%M')} | {trade['entry_price']:.4f} | {trade['exit_time'].strftime('%Y-%m-%d %H:%M')} | {trade['exit_price']:.4f} | {trade['quantity']:.4f} | {trade['pnl']:.2f} | {trade['exit_reason']} |\n"
            
            if len(trades) > 10:
                content += f"\n*注：仅显示前10笔交易，完整交易记录见 `trades.csv`*\n"
        
        content += """
---

## 6. 可视化图表

### 6.1 资金曲线

![资金曲线](charts/equity_curve.png)

### 6.2 回撤曲线

![回撤曲线](charts/drawdown_curve.png)

### 6.3 盈亏分布

![盈亏分布](charts/pnl_distribution.png)

---

## 7. 结论与建议

### 7.1 策略表现

"""
        
        # 根据统计结果给出评价
        if statistics['win_rate'] >= 0.6:
            content += "- ✅ **胜率良好**：胜率达到60%以上，策略有效性较高\n"
        else:
            content += "- ⚠️ **胜率偏低**：胜率低于60%，需要优化入场条件\n"
        
        if statistics['profit_loss_ratio'] >= 2.0:
            content += "- ✅ **盈亏比优秀**：盈亏比达到2.0以上，风险收益比合理\n"
        else:
            content += "- ⚠️ **盈亏比偏低**：盈亏比低于2.0，需要优化止损止盈策略\n"
        
        if statistics['max_drawdown_percent'] <= 0.15:
            content += "- ✅ **回撤控制良好**：最大回撤在15%以内，风险可控\n"
        else:
            content += "- ⚠️ **回撤偏大**：最大回撤超过15%，需要加强风控\n"
        
        content += """
### 7.2 优化建议

1. **入场条件优化**：根据回测结果调整评分阈值和形态识别参数
2. **止损止盈优化**：根据市场波动性动态调整止损止盈幅度
3. **仓位管理优化**：根据账户盈亏情况动态调整仓位大小
4. **风控机制优化**：完善连续亏损暂停和最大回撤熔断机制

---

**报告生成器**: 新币做空策略回测框架 v1.0
**策略版本**: V4.1（信号质量优化版）
"""
        
        return content
    
    def _export_trades_csv(self, trades: List[Dict[str, Any]]) -> str:
        """
        导出交易记录CSV
        
        Args:
            trades: 交易记录列表
            
        Returns:
            CSV文件路径
        """
        if not trades:
            logger.warning("没有交易记录，跳过CSV导出")
            return ""
        
        csv_path = os.path.join(self.reports_dir, 'trades.csv')
        
        # 写入CSV文件
        with open(csv_path, 'w', encoding='utf-8', newline='') as f:
            fieldnames = [
                'symbol', 'entry_time', 'entry_price', 'exit_time',
                'exit_price', 'quantity', 'pnl', 'pnl_percent',
                'exit_reason', 'score', 'holding_hours'
            ]
            
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            
            for trade in trades:
                # 格式化时间
                trade_copy = trade.copy()
                trade_copy['entry_time'] = trade['entry_time'].strftime('%Y-%m-%d %H:%M:%S')
                trade_copy['exit_time'] = trade['exit_time'].strftime('%Y-%m-%d %H:%M:%S')
                
                writer.writerow(trade_copy)
        
        logger.info(f"交易记录CSV已导出: {csv_path}")
        
        return csv_path
    
    def _generate_charts(
        self,
        equity_curve: List[Dict[str, Any]],
        trades: List[Dict[str, Any]]
    ) -> None:
        """
        生成可视化图表
        
        Args:
            equity_curve: 资金曲线
            trades: 交易记录
        """
        try:
            # 尝试导入matplotlib
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            from datetime import datetime
            
            # 设置中文字体
            plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'SimHei', 'DejaVu Sans']
            plt.rcParams['axes.unicode_minus'] = False
            
            # 1. 资金曲线图
            if equity_curve:
                self._plot_equity_curve(equity_curve, plt)
            
            # 2. 回撤曲线图
            if equity_curve:
                self._plot_drawdown_curve(equity_curve, plt)
            
            # 3. 盈亏分布图
            if trades:
                self._plot_pnl_distribution(trades, plt)
            
            logger.info("可视化图表已生成")
            
        except ImportError:
            logger.warning("matplotlib未安装，跳过图表生成")
        except Exception as e:
            logger.error(f"生成图表失败: {e}")
    
    def _plot_equity_curve(self, equity_curve: List[Dict[str, Any]], plt) -> None:
        """
        绘制资金曲线图
        
        Args:
            equity_curve: 资金曲线
            plt: matplotlib.pyplot对象
        """
        import pandas as pd
        
        # 转换为DataFrame
        df = pd.DataFrame(equity_curve)
        df['time'] = pd.to_datetime(df['time'])
        
        # 绘制
        fig, ax = plt.subplots(figsize=(12, 6))
        ax.plot(df['time'], df['balance'], label='账户余额', linewidth=2)
        ax.set_xlabel('时间')
        ax.set_ylabel('余额 (USDT)')
        ax.set_title('资金曲线')
        ax.legend()
        ax.grid(True, alpha=0.3)
        
        # 格式化x轴日期
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        plt.xticks(rotation=45)
        
        plt.tight_layout()
        plt.savefig(os.path.join(self.charts_dir, 'equity_curve.png'), dpi=300)
        plt.close()
    
    def _plot_drawdown_curve(self, equity_curve: List[Dict[str, Any]], plt) -> None:
        """
        绘制回撤曲线图
        
        Args:
            equity_curve: 资金曲线
            plt: matplotlib.pyplot对象
        """
        import pandas as pd
        
        # 转换为DataFrame
        df = pd.DataFrame(equity_curve)
        df['time'] = pd.to_datetime(df['time'])
        
        # 计算回撤
        df['cummax'] = df['balance'].cummax()
        df['drawdown'] = (df['cummax'] - df['balance']) / df['cummax'] * 100
        
        # 绘制
        fig, ax = plt.subplots(figsize=(12, 6))
        ax.fill_between(df['time'], df['drawdown'], 0, alpha=0.3, color='red', label='回撤')
        ax.set_xlabel('时间')
        ax.set_ylabel('回撤率 (%)')
        ax.set_title('回撤曲线')
        ax.legend()
        ax.grid(True, alpha=0.3)
        
        # 格式化x轴日期
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        plt.xticks(rotation=45)
        
        plt.tight_layout()
        plt.savefig(os.path.join(self.charts_dir, 'drawdown_curve.png'), dpi=300)
        plt.close()
    
    def _plot_pnl_distribution(self, trades: List[Dict[str, Any]], plt) -> None:
        """
        绘制盈亏分布图
        
        Args:
            trades: 交易记录
            plt: matplotlib.pyplot对象
        """
        import pandas as pd
        
        # 提取盈亏数据
        pnl_list = [t['pnl'] for t in trades]
        
        # 绘制
        fig, ax = plt.subplots(figsize=(12, 6))
        ax.hist(pnl_list, bins=30, alpha=0.7, color='blue', edgecolor='black')
        ax.axvline(x=0, color='red', linestyle='--', linewidth=2, label='盈亏平衡线')
        ax.set_xlabel('盈亏 (USDT)')
        ax.set_ylabel('交易次数')
        ax.set_title('盈亏分布')
        ax.legend()
        ax.grid(True, alpha=0.3)
        
        plt.tight_layout()
        plt.savefig(os.path.join(self.charts_dir, 'pnl_distribution.png'), dpi=300)
        plt.close()
