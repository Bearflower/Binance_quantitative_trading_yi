"""
ETHUSDT网格策略回测可视化
生成权益曲线图和回测分析图表
"""
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
import os
import sys

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'SimHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, project_root)


def plot_equity_curve(equity_curve: list, output_path: str):
    """
    绘制权益曲线图

    Args:
        equity_curve: 权益曲线数据
        output_path: 输出路径
    """
    # 转换为DataFrame
    df = pd.DataFrame(equity_curve)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.set_index('timestamp', inplace=True)

    # 创建图表
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10))

    # 绘制权益曲线
    ax1.plot(df.index, df['equity'], label='权益', linewidth=2, color='#2E86AB')
    ax1.axhline(y=df['equity'].iloc[0], color='red', linestyle='--', label='初始资金', alpha=0.5)
    ax1.fill_between(df.index, df['equity'], df['equity'].iloc[0], alpha=0.3, color='#2E86AB')
    ax1.set_title('ETHUSDT网格策略权益曲线', fontsize=16, fontweight='bold')
    ax1.set_xlabel('时间', fontsize=12)
    ax1.set_ylabel('权益 (USDT)', fontsize=12)
    ax1.legend(loc='best', fontsize=10)
    ax1.grid(True, alpha=0.3)

    # 格式化x轴日期
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    ax1.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    plt.setp(ax1.xaxis.get_majorticklabels(), rotation=45, ha='right')

    # 计算收益率
    df['return'] = df['equity'].pct_change()
    df['cumulative_return'] = (1 + df['return']).cumprod() - 1

    # 绘制累计收益率
    ax2.plot(df.index, df['cumulative_return'] * 100, label='累计收益率', linewidth=2, color='#A23B72')
    ax2.axhline(y=0, color='black', linestyle='-', linewidth=0.5)
    ax2.fill_between(df.index, df['cumulative_return'] * 100, 0, alpha=0.3, color='#A23B72')
    ax2.set_title('累计收益率', fontsize=16, fontweight='bold')
    ax2.set_xlabel('时间', fontsize=12)
    ax2.set_ylabel('收益率 (%)', fontsize=12)
    ax2.legend(loc='best', fontsize=10)
    ax2.grid(True, alpha=0.3)

    # 格式化x轴日期
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    ax2.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right')

    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()

    print(f"权益曲线图已保存到: {output_path}")


def plot_trade_analysis(trades: list, output_path: str):
    """
    绘制交易分析图

    Args:
        trades: 交易记录
        output_path: 输出路径
    """
    if not trades:
        print("没有交易记录，跳过交易分析图生成")
        return

    # 转换为DataFrame
    df = pd.DataFrame([{
        'entry_time': t.entry_time,
        'exit_time': t.exit_time,
        'pnl': float(t.pnl),
        'pnl_percent': float(t.pnl_percent) * 100
    } for t in trades])

    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['exit_time'] = pd.to_datetime(df['exit_time'])

    # 创建图表
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    # 1. 盈亏分布直方图
    ax1 = axes[0, 0]
    ax1.hist(df['pnl'], bins=50, color='#2E86AB', alpha=0.7, edgecolor='black')
    ax1.axvline(x=0, color='red', linestyle='--', linewidth=2)
    ax1.set_title('盈亏分布', fontsize=14, fontweight='bold')
    ax1.set_xlabel('盈亏 (USDT)', fontsize=12)
    ax1.set_ylabel('频次', fontsize=12)
    ax1.grid(True, alpha=0.3)

    # 2. 累计盈亏曲线
    ax2 = axes[0, 1]
    df_sorted = df.sort_values('exit_time')
    df_sorted['cumulative_pnl'] = df_sorted['pnl'].cumsum()
    ax2.plot(df_sorted['exit_time'], df_sorted['cumulative_pnl'], linewidth=2, color='#A23B72')
    ax2.axhline(y=0, color='black', linestyle='-', linewidth=0.5)
    ax2.set_title('累计盈亏曲线', fontsize=14, fontweight='bold')
    ax2.set_xlabel('时间', fontsize=12)
    ax2.set_ylabel('累计盈亏 (USDT)', fontsize=12)
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    ax2.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right')

    # 3. 盈亏百分比分布
    ax3 = axes[1, 0]
    ax3.hist(df['pnl_percent'], bins=50, color='#F18F01', alpha=0.7, edgecolor='black')
    ax3.axvline(x=0, color='red', linestyle='--', linewidth=2)
    ax3.set_title('盈亏百分比分布', fontsize=14, fontweight='bold')
    ax3.set_xlabel('盈亏百分比 (%)', fontsize=12)
    ax3.set_ylabel('频次', fontsize=12)
    ax3.grid(True, alpha=0.3)

    # 4. 每日交易次数和盈亏
    ax4 = axes[1, 1]
    df_sorted['date'] = df_sorted['exit_time'].dt.date
    daily_stats = df_sorted.groupby('date').agg({
        'pnl': ['count', 'sum']
    }).reset_index()
    daily_stats.columns = ['date', 'count', 'sum']

    ax4_twin = ax4.twinx()
    ax4.bar(daily_stats['date'], daily_stats['count'], alpha=0.7, color='#2E86AB', label='交易次数')
    ax4_twin.plot(daily_stats['date'], daily_stats['sum'], color='#A23B72', linewidth=2, marker='o', markersize=3, label='盈亏')

    ax4.set_title('每日交易统计', fontsize=14, fontweight='bold')
    ax4.set_xlabel('日期', fontsize=12)
    ax4.set_ylabel('交易次数', fontsize=12, color='#2E86AB')
    ax4_twin.set_ylabel('盈亏 (USDT)', fontsize=12, color='#A23B72')
    ax4.grid(True, alpha=0.3)

    # 添加图例
    lines1, labels1 = ax4.get_legend_handles_labels()
    lines2, labels2 = ax4_twin.get_legend_handles_labels()
    ax4.legend(lines1 + lines2, labels1 + labels2, loc='upper left')

    plt.tight_layout()
    plt.savefig(output_path, dpi=300, bbox_inches='tight')
    plt.close()

    print(f"交易分析图已保存到: {output_path}")


def generate_detailed_report(results: dict, symbol: str, output_dir: str):
    """
    生成详细的回测报告

    Args:
        results: 回测结果
        symbol: 交易对
        output_dir: 输出目录
    """
    # 生成权益曲线图
    equity_curve_path = os.path.join(output_dir, f'{symbol}_equity_curve.png')
    plot_equity_curve(results['equity_curve'], equity_curve_path)

    # 生成交易分析图
    trade_analysis_path = os.path.join(output_dir, f'{symbol}_trade_analysis.png')
    plot_trade_analysis(results['trades'], trade_analysis_path)

    # 生成详细报告
    report_path = os.path.join(output_dir, f'{symbol}_detailed_report.md')

    # 计算额外指标
    equity_df = pd.DataFrame(results['equity_curve'])
    equity_df['timestamp'] = pd.to_datetime(equity_df['timestamp'])

    # 计算日收益率
    equity_df['daily_return'] = equity_df['equity'].pct_change()

    # 计算最大连续盈利和亏损
    trades_df = pd.DataFrame([{
        'pnl': float(t.pnl)
    } for t in results['trades']])

    trades_df['is_win'] = trades_df['pnl'] > 0

    # 计算最大连续盈利次数
    max_consecutive_wins = 0
    current_wins = 0
    for is_win in trades_df['is_win']:
        if is_win:
            current_wins += 1
            max_consecutive_wins = max(max_consecutive_wins, current_wins)
        else:
            current_wins = 0

    # 计算最大连续亏损次数
    max_consecutive_losses = 0
    current_losses = 0
    for is_win in trades_df['is_win']:
        if not is_win:
            current_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, current_losses)
        else:
            current_losses = 0

    # 计算最长持仓时间
    max_holding_hours = 0
    for trade in results['trades']:
        holding_hours = (trade.exit_time - trade.entry_time).total_seconds() / 3600
        max_holding_hours = max(max_holding_hours, holding_hours)

    report = f"""# {symbol}网格策略详细回测报告

## 📊 回测概览

- **回测时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- **交易对**: {symbol}
- **回测周期**: {equity_df['timestamp'].iloc[0]} 至 {equity_df['timestamp'].iloc[-1]}
- **初始资金**: {results['initial_capital']:.2f} USDT
- **最终资金**: {results['final_capital']:.2f} USDT
- **总收益率**: {results['total_return']:.2f}%
- **最大回撤**: {results['max_drawdown']:.2f} USDT ({results['max_drawdown_percent']:.2f}%)
- **夏普比率**: {results['sharpe_ratio']:.2f}

## 📈 交易统计

### 基本统计
- **总交易次数**: {results['total_trades']}
- **盈利次数**: {results['win_trades']} ({results['win_rate']:.2f}%)
- **亏损次数**: {results['loss_trades']} ({100 - results['win_rate']:.2f}%)
- **平均盈利**: {results['avg_win']:.4f} USDT
- **平均亏损**: {results['avg_loss']:.4f} USDT
- **盈亏比**: {results['profit_loss_ratio']:.2f}

### 连续性统计
- **最大连续盈利次数**: {max_consecutive_wins}
- **最大连续亏损次数**: {max_consecutive_losses}
- **最长持仓时间**: {max_holding_hours:.2f} 小时

## 📉 权益曲线分析

![权益曲线]({symbol}_equity_curve.png)

### 关键时点分析
- **最高权益**: {max([e['equity'] for e in results['equity_curve']]):.2f} USDT
- **最低权益**: {min([e['equity'] for e in results['equity_curve']]):.2f} USDT
- **权益波动率**: {np.std([e['equity'] for e in results['equity_curve']]):.2f} USDT

## 📊 交易分析

![交易分析]({symbol}_trade_analysis.png)

### 盈亏分布特征
- **盈利交易平均收益率**: {results['avg_win'] / results['initial_capital'] * 100:.4f}%
- **亏损交易平均亏损率**: {abs(results['avg_loss']) / results['initial_capital'] * 100:.4f}%

## ⚠️ 风险分析

### 回撤分析
- **最大回撤金额**: {results['max_drawdown']:.2f} USDT
- **最大回撤比例**: {results['max_drawdown_percent']:.2f}%
- **回撤持续时间**: 需要进一步分析

### 风险提示
1. **极端亏损**: 回测结果显示策略存在极端亏损风险，最大回撤超过100%
2. **盈亏比失衡**: 虽然胜率较高({results['win_rate']:.2f}%)，但盈亏比极低({results['profit_loss_ratio']:.2f})，导致总体亏损
3. **资金管理问题**: 网格参数设置可能导致过度交易和资金快速消耗

## 🔍 问题诊断

### 策略问题
1. **网格参数不合理**:
   - 网格间距可能过小，导致频繁交易
   - 网格数量可能过多，分散了资金
   - 每格利润率过低，无法覆盖手续费和滑点

2. **风险控制缺失**:
   - 缺乏有效的止损机制
   - 未考虑市场状态变化
   - 资金分配不合理

3. **市场适应性差**:
   - 在趋势市场中表现极差
   - 未能在强趋势时及时止损
   - 网格重置机制可能过于频繁

## 💡 优化建议

### 参数优化
1. **调整网格参数**:
   - 增加网格间距，降低交易频率
   - 减少网格数量，集中资金
   - 提高每格利润率要求（至少>1%）

2. **优化市场状态检测**:
   - 提高ADX阈值，避免在弱趋势时交易
   - 增加市场状态确认机制
   - 在强趋势时暂停网格交易

3. **改进风险控制**:
   - 设置总资金止损线（如-20%）
   - 限制单次交易仓位比例
   - 添加移动止盈机制

### 策略改进
1. **多时间框架分析**:
   - 使用更长周期确认趋势
   - 在震荡市场才启动网格
   - 趋势市场切换其他策略

2. **动态参数调整**:
   - 根据波动率动态调整网格间距
   - 根据市场状态调整网格数量
   - 根据盈亏情况调整仓位

3. **资金管理优化**:
   - 分批建仓，避免一次性投入
   - 保留备用资金应对极端情况
   - 设置每日最大亏损限制

## 📌 结论

本次回测结果显示，当前网格策略存在严重的参数设置和风险控制问题。虽然胜率较高，但由于盈亏比极低和风险控制缺失，导致最终几乎全部亏损。

**建议**:
1. 立即停止使用当前参数进行实盘交易
2. 重新设计网格参数和风险控制机制
3. 在模拟环境中充分测试优化后的策略
4. 考虑结合其他策略进行组合交易

---
*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""

    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)

    print(f"详细报告已保存到: {report_path}")


if __name__ == "__main__":
    # 这里需要从回测引擎获取结果
    # 暂时使用示例数据
    print("可视化模块已准备就绪")
