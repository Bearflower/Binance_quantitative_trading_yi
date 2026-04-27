#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2.4 vs V2.5 全方位收益分析对比
包括：收益率、回撤、夏普比率、胜率、盈亏比等
"""

import json
import pandas as pd
import numpy as np
from pathlib import Path
import glob
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')
plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'SimHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

def load_trades():
    """加载 V2.4 和 V2.5 的交易记录"""
    # 加载 V2.4 交易记录
    v24_files = sorted(glob.glob('backtest_results/backtest_v24_full_*.csv'))
    if not v24_files:
        print("❌ 未找到 V2.4 交易记录")
        return None, None
    
    v24_df = pd.read_csv(v24_files[0])
    v24_df['buy_date'] = pd.to_datetime(v24_df['buy_date'])
    v24_df['sell_date'] = pd.to_datetime(v24_df['sell_date'])
    
    # 加载 V2.5 交易记录
    v25_files = sorted(glob.glob('backtest_results/backtest_v25_full_*.csv'))
    if not v25_files:
        print("❌ 未找到 V2.5 交易记录")
        return None, None
    
    v25_df = pd.read_csv(v25_files[0])
    v25_df['buy_date'] = pd.to_datetime(v25_df['buy_date'])
    v25_df['sell_date'] = pd.to_datetime(v25_df['sell_date'])
    
    return v24_df, v25_df

def calculate_portfolio_returns(trades_df, initial_capital=1000000):
    """
    计算投资组合收益曲线
    假设：每笔交易平均分配资金，最多同时持有 10 只股票
    """
    if trades_df.empty:
        return None
    
    # 按买入日期排序
    trades_df = trades_df.sort_values('buy_date').copy()
    
    # 计算每笔交易的收益
    trades_df['profit'] = trades_df['net_return'] * initial_capital / 10  # 假设每笔交易使用 1/10 资金
    
    # 生成日期序列
    all_dates = pd.date_range(
        start=trades_df['buy_date'].min(),
        end=trades_df['sell_date'].max(),
        freq='D'
    )
    
    # 计算每日收益
    daily_returns = pd.Series(index=all_dates, dtype=float).fillna(0)
    
    for idx, trade in trades_df.iterrows():
        buy_date = trade['buy_date']
        sell_date = trade['sell_date']
        daily_return = trade['net_return'] / (sell_date - buy_date).days if (sell_date - buy_date).days > 0 else trade['net_return']
        
        # 在持有期间分配收益
        hold_dates = pd.date_range(start=buy_date, end=sell_date, freq='D')
        for date in hold_dates:
            if date in daily_returns.index:
                daily_returns[date] += daily_return
    
    # 计算累计收益曲线
    cumulative_returns = (1 + daily_returns).cumprod()
    portfolio_values = initial_capital * cumulative_returns
    
    return {
        'daily_returns': daily_returns,
        'cumulative_returns': cumulative_returns,
        'portfolio_values': portfolio_values,
        'trades': trades_df
    }

def calculate_drawdown(portfolio_values):
    """计算回撤指标"""
    # 计算滚动最大值
    running_max = portfolio_values.cummax()
    
    # 计算回撤
    drawdown = (portfolio_values - running_max) / running_max
    
    # 最大回撤
    max_drawdown = drawdown.min()
    
    # 平均回撤
    avg_drawdown = drawdown.mean()
    
    # 回撤持续时间
    is_drawdown = drawdown < 0
    drawdown_periods = []
    current_period = 0
    
    for val in is_drawdown:
        if val:
            current_period += 1
        else:
            if current_period > 0:
                drawdown_periods.append(current_period)
            current_period = 0
    
    avg_drawdown_duration = np.mean(drawdown_periods) if drawdown_periods else 0
    max_drawdown_duration = max(drawdown_periods) if drawdown_periods else 0
    
    return {
        'max_drawdown': max_drawdown,
        'avg_drawdown': avg_drawdown,
        'avg_drawdown_duration': avg_drawdown_duration,
        'max_drawdown_duration': max_drawdown_duration,
        'drawdown_series': drawdown
    }

def calculate_sharpe_ratio(daily_returns, risk_free_rate=0.03):
    """计算夏普比率"""
    if daily_returns.std() == 0:
        return 0
    
    # 年化夏普比率
    excess_returns = daily_returns - risk_free_rate / 252
    sharpe = np.sqrt(252) * excess_returns.mean() / daily_returns.std()
    
    return sharpe

def calculate_sortino_ratio(daily_returns, risk_free_rate=0.03):
    """计算索提诺比率"""
    downside_returns = daily_returns[daily_returns < 0]
    
    if downside_returns.std() == 0:
        return 0
    
    excess_returns = daily_returns - risk_free_rate / 252
    sortino = np.sqrt(252) * excess_returns.mean() / downside_returns.std()
    
    return sortino

def calculate_calmar_ratio(cumulative_returns, max_drawdown):
    """计算卡尔玛比率"""
    if max_drawdown == 0:
        return 0
    
    # 年化收益率
    total_return = cumulative_returns.iloc[-1] / cumulative_returns.iloc[0] - 1
    years = len(cumulative_returns) / 252
    annual_return = (1 + total_return) ** (1 / years) - 1
    
    calmar = annual_return / abs(max_drawdown)
    
    return calmar

def analyze_returns(version_name, portfolio_data):
    """全面分析收益指标"""
    if portfolio_data is None:
        return None
    
    daily_returns = portfolio_data['daily_returns']
    cumulative_returns = portfolio_data['cumulative_returns']
    portfolio_values = portfolio_data['portfolio_values']
    trades = portfolio_data['trades']
    
    # 基础统计
    total_return = (portfolio_values.iloc[-1] / portfolio_values.iloc[0]) - 1
    annual_return = (1 + total_return) ** (252 / len(daily_returns)) - 1
    
    # 回撤分析
    drawdown = calculate_drawdown(portfolio_values)
    
    # 风险调整收益
    sharpe = calculate_sharpe_ratio(daily_returns)
    sortino = calculate_sortino_ratio(daily_returns)
    calmar = calculate_calmar_ratio(cumulative_returns, drawdown['max_drawdown'])
    
    # 交易统计
    total_trades = len(trades)
    winning_trades = len(trades[trades['net_return'] > 0])
    losing_trades = len(trades[trades['net_return'] <= 0])
    win_rate = winning_trades / total_trades if total_trades > 0 else 0
    
    avg_win = trades[trades['net_return'] > 0]['net_return'].mean() if winning_trades > 0 else 0
    avg_loss = abs(trades[trades['net_return'] <= 0]['net_return'].mean()) if losing_trades > 0 else 0
    profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0
    
    # 持仓统计
    avg_holding_days = trades['holding_days'].mean()
    max_holding_days = trades['holding_days'].max()
    
    return {
        'version': version_name,
        'total_return': total_return,
        'annual_return': annual_return,
        'max_drawdown': drawdown['max_drawdown'],
        'avg_drawdown': drawdown['avg_drawdown'],
        'max_drawdown_duration': drawdown['max_drawdown_duration'],
        'avg_drawdown_duration': drawdown['avg_drawdown_duration'],
        'sharpe_ratio': sharpe,
        'sortino_ratio': sortino,
        'calmar_ratio': calmar,
        'total_trades': total_trades,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_loss_ratio': profit_loss_ratio,
        'avg_holding_days': avg_holding_days,
        'max_holding_days': max_holding_days,
        'daily_returns': daily_returns,
        'cumulative_returns': cumulative_returns,
        'portfolio_values': portfolio_values,
        'drawdown': drawdown['drawdown_series']
    }

def compare_comprehensive(v24_analysis, v25_analysis):
    """全方位对比 V2.4 和 V2.5"""
    print("="*80)
    print("V2.4 vs V2.5 全方位收益对比分析")
    print("="*80)
    
    # 1. 收益指标对比
    print("\n" + "="*80)
    print("1. 收益指标对比")
    print("="*80)
    
    return_metrics = [
        ('总收益率', 'total_return'),
        ('年化收益率', 'annual_return'),
        ('最大回撤', 'max_drawdown'),
        ('平均回撤', 'avg_drawdown'),
    ]
    
    print(f"\n{'指标':<20} {'V2.4':>12} {'V2.5':>12} {'优势':>10}")
    print("-"*80)
    
    for name, key in return_metrics:
        v24_val = getattr(v24_analysis, key, 0) if hasattr(v24_analysis, key) else v24_analysis.get(key, 0)
        v25_val = getattr(v25_analysis, key, 0) if hasattr(v25_analysis, key) else v25_analysis.get(key, 0)
        
        if 'drawdown' in key.lower():
            better = 'V2.5' if v25_val > v24_val else 'V2.4'  # 回撤越小越好
            print(f"{name:<20} {v24_val:>11.2f}% {v25_val:>11.2f}% {better:>10}")
        else:
            better = 'V2.4' if v24_val > v25_val else 'V2.5'  # 收益越大越好
            print(f"{name:<20} {v24_val:>11.2f}% {v25_val:>11.2f}% {better:>10}")
    
    # 2. 风险调整收益对比
    print("\n" + "="*80)
    print("2. 风险调整收益对比")
    print("="*80)
    
    risk_metrics = [
        ('夏普比率', 'sharpe_ratio'),
        ('索提诺比率', 'sortino_ratio'),
        ('卡尔玛比率', 'calmar_ratio'),
    ]
    
    print(f"\n{'指标':<20} {'V2.4':>12} {'V2.5':>12} {'优势':>10}")
    print("-"*80)
    
    for name, key in risk_metrics:
        v24_val = v24_analysis.get(key, 0)
        v25_val = v25_analysis.get(key, 0)
        better = 'V2.4' if v24_val > v25_val else 'V2.5'
        print(f"{name:<20} {v24_val:>12.2f} {v25_val:>12.2f} {better:>10}")
    
    # 3. 交易统计对比
    print("\n" + "="*80)
    print("3. 交易统计对比")
    print("="*80)
    
    trade_metrics = [
        ('总交易数', 'total_trades'),
        ('盈利交易数', 'winning_trades'),
        ('亏损交易数', 'losing_trades'),
        ('胜率', 'win_rate'),
        ('盈亏比', 'profit_loss_ratio'),
        ('平均持仓天数', 'avg_holding_days'),
        ('最大持仓天数', 'max_holding_days'),
    ]
    
    print(f"\n{'指标':<20} {'V2.4':>12} {'V2.5':>12} {'优势':>10}")
    print("-"*80)
    
    for name, key in trade_metrics:
        v24_val = v24_analysis.get(key, 0)
        v25_val = v25_analysis.get(key, 0)
        
        if 'drawdown' in key.lower() or 'loss' in key.lower():
            better = 'V2.5' if v25_val < v24_val else 'V2.4'
        else:
            better = 'V2.4' if v24_val > v25_val else 'V2.5'
        
        if 'rate' in key.lower() or 'ratio' in key.lower():
            print(f"{name:<20} {v24_val:>11.2f} {v25_val:>11.2f} {better:>10}")
        else:
            print(f"{name:<20} {v24_val:>12.0f} {v25_val:>12.0f} {better:>10}")
    
    # 4. 综合评分
    print("\n" + "="*80)
    print("4. 综合评分")
    print("="*80)
    
    # 计算综合评分（满分 100）
    def calculate_score(analysis):
        score = 0
        
        # 年化收益（30 分）
        if analysis['annual_return'] > 0.2:
            score += 30
        elif analysis['annual_return'] > 0.1:
            score += 20
        elif analysis['annual_return'] > 0:
            score += 10
        
        # 最大回撤（25 分）- 越小越好
        if analysis['max_drawdown'] > -0.1:
            score += 25
        elif analysis['max_drawdown'] > -0.2:
            score += 15
        elif analysis['max_drawdown'] > -0.3:
            score += 10
        
        # 夏普比率（20 分）
        if analysis['sharpe_ratio'] > 1.5:
            score += 20
        elif analysis['sharpe_ratio'] > 1.0:
            score += 15
        elif analysis['sharpe_ratio'] > 0.5:
            score += 10
        
        # 胜率（15 分）
        if analysis['win_rate'] > 0.6:
            score += 15
        elif analysis['win_rate'] > 0.5:
            score += 10
        elif analysis['win_rate'] > 0.4:
            score += 5
        
        # 盈亏比（10 分）
        if analysis['profit_loss_ratio'] > 2.0:
            score += 10
        elif analysis['profit_loss_ratio'] > 1.5:
            score += 7
        elif analysis['profit_loss_ratio'] > 1.0:
            score += 5
        
        return score
    
    v24_score = calculate_score(v24_analysis)
    v25_score = calculate_score(v25_analysis)
    
    print(f"\nV2.4 综合评分：{v24_score}/100")
    print(f"V2.5 综合评分：{v25_score}/100")
    
    if v24_score > v25_score:
        print(f"\n✅ V2.4 综合表现更优")
    else:
        print(f"\n✅ V2.5 综合表现更优")
    
    return {
        'v24_score': v24_score,
        'v25_score': v25_score
    }

def plot_comprehensive_analysis(v24_analysis, v25_analysis):
    """生成全方位对比图表"""
    fig, axes = plt.subplots(3, 2, figsize=(16, 14))
    
    # 图 1: 累计收益曲线对比
    ax1 = axes[0, 0]
    ax1.plot(v24_analysis['portfolio_values'].index, 
             v24_analysis['portfolio_values'].values / 10000, 
             label='V2.4', color='red', linewidth=2, alpha=0.7)
    ax1.plot(v25_analysis['portfolio_values'].index, 
             v25_analysis['portfolio_values'].values / 10000, 
             label='V2.5', color='blue', linewidth=2, alpha=0.7)
    ax1.set_title('图 1: 累计收益曲线对比（单位：万元）', fontsize=14, fontweight='bold')
    ax1.set_xlabel('日期')
    ax1.set_ylabel('组合价值（万元）')
    ax1.legend()
    ax1.grid(True, alpha=0.3)
    
    # 图 2: 回撤曲线对比
    ax2 = axes[0, 1]
    ax2.plot(v24_analysis['drawdown'].index, 
             v24_analysis['drawdown'].values * 100, 
             label='V2.4', color='red', linewidth=2, alpha=0.7)
    ax2.plot(v25_analysis['drawdown'].index, 
             v25_analysis['drawdown'].values * 100, 
             label='V2.5', color='blue', linewidth=2, alpha=0.7)
    ax2.set_title('图 2: 回撤曲线对比', fontsize=14, fontweight='bold')
    ax2.set_xlabel('日期')
    ax2.set_ylabel('回撤 (%)')
    ax2.legend()
    ax2.grid(True, alpha=0.3)
    ax2.fill_between(v24_analysis['drawdown'].index, 
                     v24_analysis['drawdown'].values * 100, 0, 
                     alpha=0.3, color='red')
    ax2.fill_between(v25_analysis['drawdown'].index, 
                     v25_analysis['drawdown'].values * 100, 0, 
                     alpha=0.3, color='blue')
    
    # 图 3: 收益指标雷达图
    ax3 = axes[1, 0]
    categories = ['年化收益', '最大回撤', '夏普比率', '胜率', '盈亏比']
    
    # 归一化处理
    v24_values = [
        v24_analysis['annual_return'] * 100,
        abs(v24_analysis['max_drawdown']) * 100,
        v24_analysis['sharpe_ratio'],
        v24_analysis['win_rate'] * 100,
        v24_analysis['profit_loss_ratio']
    ]
    v25_values = [
        v25_analysis['annual_return'] * 100,
        abs(v25_analysis['max_drawdown']) * 100,
        v25_analysis['sharpe_ratio'],
        v25_analysis['win_rate'] * 100,
        v25_analysis['profit_loss_ratio']
    ]
    
    # 归一化到 0-10
    v24_normalized = [(v / max(v24_values[i], v25_values[i]) * 10) if max(v24_values[i], v25_values[i]) > 0 else 0 
                      for i, v in enumerate(v24_values)]
    v25_normalized = [(v / max(v24_values[i], v25_values[i]) * 10) if max(v24_values[i], v25_values[i]) > 0 else 0 
                      for i, v in enumerate(v25_values)]
    
    angles = np.linspace(0, 2 * np.pi, len(categories), endpoint=False).tolist()
    v24_normalized += v24_normalized[:1]
    v25_normalized += v25_normalized[:1]
    angles += angles[:1]
    
    ax3 = plt.subplot(2, 3, 3, polar=True)
    ax3.plot(angles, v24_normalized, 'o-', linewidth=2, label='V2.4', color='red')
    ax3.fill(angles, v24_normalized, alpha=0.25, color='red')
    ax3.plot(angles, v25_normalized, 'o-', linewidth=2, label='V2.5', color='blue')
    ax3.fill(angles, v25_normalized, alpha=0.25, color='blue')
    ax3.set_xticks(angles[:-1])
    ax3.set_xticklabels(categories)
    ax3.set_title('图 3: 收益指标雷达图', fontsize=14, fontweight='bold', pad=20)
    ax3.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1))
    ax3.grid(True)
    
    # 图 4: 年度收益对比
    ax4 = axes[1, 1]
    v24_yearly = v24_analysis['daily_returns'].groupby(
        v24_analysis['daily_returns'].index.year
    ).apply(lambda x: (1 + x).prod() - 1)
    v25_yearly = v25_analysis['daily_returns'].groupby(
        v25_analysis['daily_returns'].index.year
    ).apply(lambda x: (1 + x).prod() - 1)
    
    years = sorted(set(list(v24_yearly.index) + list(v25_yearly.index)))
    v24_counts = [v24_yearly.get(y, 0) * 100 for y in years]
    v25_counts = [v25_yearly.get(y, 0) * 100 for y in years]
    
    x = range(len(years))
    width = 0.35
    
    bars1 = ax4.bar([i - width/2 for i in x], v24_counts, width, label='V2.4', color='red', alpha=0.7)
    bars2 = ax4.bar([i + width/2 for i in x], v25_counts, width, label='V2.5', color='blue', alpha=0.7)
    
    ax4.set_title('图 4: 年度收益对比', fontsize=14, fontweight='bold')
    ax4.set_xlabel('年份')
    ax4.set_ylabel('收益率 (%)')
    ax4.set_xticks(x)
    ax4.set_xticklabels(years)
    ax4.legend()
    ax4.grid(True, alpha=0.3, axis='y')
    ax4.axhline(y=0, color='black', linestyle='-', linewidth=0.8)
    
    # 图 5: 月度收益分布
    ax5 = axes[2, 0]
    v24_monthly = v24_analysis['daily_returns'].groupby(
        v24_analysis['daily_returns'].index.to_period('M')
    ).apply(lambda x: (1 + x).prod() - 1)
    v25_monthly = v25_analysis['daily_returns'].groupby(
        v25_analysis['daily_returns'].index.to_period('M')
    ).apply(lambda x: (1 + x).prod() - 1)
    
    ax5.hist(v24_monthly.values * 100, bins=20, alpha=0.5, label='V2.4', color='red', density=True)
    ax5.hist(v25_monthly.values * 100, bins=20, alpha=0.5, label='V2.5', color='blue', density=True)
    ax5.set_title('图 5: 月度收益分布', fontsize=14, fontweight='bold')
    ax5.set_xlabel('月收益率 (%)')
    ax5.set_ylabel('频率')
    ax5.legend()
    ax5.grid(True, alpha=0.3)
    
    # 图 6: 关键指标对比柱状图
    ax6 = axes[2, 1]
    metrics = ['总收益', '年化收益', '夏普比率', '胜率']
    v24_vals = [
        v24_analysis['total_return'] * 100,
        v24_analysis['annual_return'] * 100,
        v24_analysis['sharpe_ratio'],
        v24_analysis['win_rate'] * 100
    ]
    v25_vals = [
        v25_analysis['total_return'] * 100,
        v25_analysis['annual_return'] * 100,
        v25_analysis['sharpe_ratio'],
        v25_analysis['win_rate'] * 100
    ]
    
    x6 = range(len(metrics))
    bars6_1 = ax6.bar([i - width/2 for i in x6], v24_vals, width, label='V2.4', color='red', alpha=0.7)
    bars6_2 = ax6.bar([i + width/2 for i in x6], v25_vals, width, label='V2.5', color='blue', alpha=0.7)
    
    ax6.set_title('图 6: 关键指标对比', fontsize=14, fontweight='bold')
    ax6.set_xlabel('指标')
    ax6.set_ylabel('数值')
    ax6.set_xticks(x6)
    ax6.set_xticklabels(metrics)
    ax6.legend()
    ax6.grid(True, alpha=0.3, axis='y')
    
    plt.tight_layout()
    output_file = 'backtest_results/v24_vs_v25_comprehensive_analysis.png'
    plt.savefig(output_file, dpi=150, bbox_inches='tight')
    print(f"\n全方位对比图表已保存到：{output_file}")

def main():
    """主函数"""
    print("="*80)
    print("开始 V2.4 vs V2.5 全方位收益分析")
    print("="*80)
    
    # 加载数据
    v24_trades, v25_trades = load_trades()
    
    if v24_trades is None or v25_trades is None:
        print("❌ 数据加载失败")
        return
    
    print(f"\nV2.4 交易记录：{len(v24_trades)} 笔")
    print(f"V2.5 交易记录：{len(v25_trades)} 笔")
    
    # 过滤 2019 年后的交易（更公平对比）
    v24_trades_excl = v24_trades[v24_trades['buy_date'] >= '2020-01-01'].copy()
    v25_trades_excl = v25_trades[v25_trades['buy_date'] >= '2020-01-01'].copy()
    
    print(f"\n排除 2019 年后：")
    print(f"V2.4 交易记录：{len(v24_trades_excl)} 笔")
    print(f"V2.5 交易记录：{len(v25_trades_excl)} 笔")
    
    # 计算投资组合收益
    print("\n计算投资组合收益曲线...")
    v24_portfolio = calculate_portfolio_returns(v24_trades_excl)
    v25_portfolio = calculate_portfolio_returns(v25_trades_excl)
    
    # 全面分析
    print("\n进行全方位收益分析...")
    v24_analysis = analyze_returns('V2.4', v24_portfolio)
    v25_analysis = analyze_returns('V2.5', v25_portfolio)
    
    # 对比
    scores = compare_comprehensive(v24_analysis, v25_analysis)
    
    # 生成图表
    print("\n生成对比图表...")
    plot_comprehensive_analysis(v24_analysis, v25_analysis)
    
    # 保存详细结果
    output = {
        'v24_analysis': {
            'total_return': v24_analysis['total_return'],
            'annual_return': v24_analysis['annual_return'],
            'max_drawdown': v24_analysis['max_drawdown'],
            'sharpe_ratio': v24_analysis['sharpe_ratio'],
            'sortino_ratio': v24_analysis['sortino_ratio'],
            'calmar_ratio': v24_analysis['calmar_ratio'],
            'win_rate': v24_analysis['win_rate'],
            'profit_loss_ratio': v24_analysis['profit_loss_ratio'],
            'total_trades': v24_analysis['total_trades'],
            'score': scores['v24_score']
        },
        'v25_analysis': {
            'total_return': v25_analysis['total_return'],
            'annual_return': v25_analysis['annual_return'],
            'max_drawdown': v25_analysis['max_drawdown'],
            'sharpe_ratio': v25_analysis['sharpe_ratio'],
            'sortino_ratio': v25_analysis['sortino_ratio'],
            'calmar_ratio': v25_analysis['calmar_ratio'],
            'win_rate': v25_analysis['win_rate'],
            'profit_loss_ratio': v25_analysis['profit_loss_ratio'],
            'total_trades': v25_analysis['total_trades'],
            'score': scores['v25_score']
        }
    }
    
    with open('backtest_results/v24_vs_v25_comprehensive_analysis.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    print(f"\n详细分析结果已保存到：backtest_results/v24_vs_v25_comprehensive_analysis.json")
    
    print("\n" + "="*80)
    print("全方位收益分析完成！")
    print("="*80)

if __name__ == '__main__':
    main()
