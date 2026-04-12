#!/usr/bin/env python3
"""
评分系统回测脚本 (v5.5)

功能：
1. 加载历史数据
2. 逐小时回测评分系统
3. 统计各分数段的胜率、盈亏比
4. 生成可视化报告（CSV + HTML）

使用方法：
python backtest/scoring_backtest.py --start 2026-01-01 --end 2026-04-01 --symbols BTCUSDT,ETHUSDT,BNBUSDT
"""

import argparse
import logging
import json
import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
from collections import defaultdict

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
import sys
sys.path.insert(0, str(project_root))

from core.scoring_engine import get_scoring_engine

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('backtest')


class ScoringBacktester:
    """评分系统回测器"""
    
    def __init__(self, symbols: List[str], start_date: str, end_date: str):
        """
        初始化回测器
        
        Args:
            symbols: 交易对列表
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
        """
        self.symbols = symbols
        self.start_date = datetime.strptime(start_date, '%Y-%m-%d')
        self.end_date = datetime.strptime(end_date, '%Y-%m-%d')
        self.scoring_engine = get_scoring_engine()
        
        # 回测结果
        self.results = []
        
        logger.info(f"回测器初始化完成")
        logger.info(f"  交易对：{symbols}")
        logger.info(f"  时间范围：{start_date} 至 {end_date}")
    
    def load_historical_data(self, symbol: str, timestamp: datetime) -> Optional[Dict[str, Any]]:
        """
        加载历史数据（模拟）
        
        实际应该从数据库或文件加载真实的历史数据
        这里使用模拟数据演示回测流程
        
        Args:
            symbol: 交易对
            timestamp: 时间戳
        
        Returns:
            历史数据字典
        """
        # TODO: 实现真实的历史数据加载
        # 这里使用模拟数据
        
        # 生成模拟的指标数据
        import random
        import numpy as np
        
        base_price = {
            'BTCUSDT': 95000,
            'ETHUSDT': 3500,
            'BNBUSDT': 600
        }.get(symbol, 10000)
        
        # 添加随机波动
        price_factor = 1 + random.uniform(-0.1, 0.1)
        current_price = base_price * price_factor
        
        # 生成 EMA 数据（向上趋势）
        ema21_base = current_price * 0.99
        ema21_1d = [ema21_base * (1 + i * 0.001) for i in range(55)]
        
        # 生成 RSI 数据
        rsi = random.uniform(40, 70)
        
        # 生成 MACD 数据
        macd_dif = random.uniform(-50, 50)
        macd_dea = random.uniform(-50, 50)
        
        # 生成 ATR 数据
        atr = current_price * random.uniform(0.02, 0.04)
        
        # 生成资金费率
        funding_rate = random.uniform(-0.0005, 0.0005)
        
        # 生成 24 小时涨跌幅
        price_change_24h = random.uniform(-0.15, 0.15)
        
        data = {
            'funding_rate': funding_rate,
            'price_change_24h': price_change_24h,
            'indicators': {
                '1d': {
                    'close': [current_price] * 55,
                    'ema21': ema21_1d,
                    'rsi14': [rsi],
                    'macd': [
                        {'dif': macd_dif, 'dea': macd_dea, 'histogram': macd_dif - macd_dea},
                        {'dif': macd_dif + 10, 'dea': macd_dea + 5, 'histogram': macd_dif - macd_dea + 5}
                    ],
                    'klines': [
                        {'open': current_price * 0.99, 'close': current_price * 1.01, 
                         'high': current_price * 1.02, 'low': current_price * 0.98}
                        for _ in range(2)
                    ]
                },
                '4h': {
                    'close': [current_price] * 20,
                    'ema21': [current_price * 0.995] * 20,
                    'klines': [
                        {'high': current_price * 1.01, 'low': current_price * 0.99, 
                         'close': current_price, 'volume': 1000}
                        for _ in range(20)
                    ]
                },
                '1h': {
                    'close': [current_price] * 20,
                    'ema21': [current_price * 0.998] * 20,
                    'atr14': [atr]
                }
            }
        }
        
        return data
    
    def run_backtest(self) -> List[Dict[str, Any]]:
        """
        执行回测
        
        Returns:
            回测结果列表
        """
        logger.info("=" * 60)
        logger.info("开始执行回测")
        logger.info("=" * 60)
        
        current_time = self.start_date
        total_hours = int((self.end_date - self.start_date).total_seconds() / 3600)
        hour_count = 0
        
        while current_time <= self.end_date:
            for symbol in self.symbols:
                # 加载历史数据
                data = self.load_historical_data(symbol, current_time)
                
                if data is None:
                    continue
                
                # 执行评分
                try:
                    score_result = self.scoring_engine.score(symbol, data)
                    
                    # 记录结果
                    result = {
                        'timestamp': current_time.isoformat(),
                        'symbol': symbol,
                        'score': score_result['score'],
                        'grade': score_result['grade'] or 'None',
                        'position_ratio': score_result['position_ratio'],
                        'trend_score': score_result['score_detail'].get('trend', 0),
                        'pattern_score': score_result['score_detail'].get('pattern', 0),
                        'momentum_score': score_result['score_detail'].get('momentum', 0),
                        'risk_score': score_result['score_detail'].get('risk', 0),
                        'veto_reason': score_result.get('veto_reason', None)
                    }
                    
                    # 模拟交易结果（实际应该根据后续价格变化计算盈亏）
                    if score_result['grade']:
                        # 简化模拟：根据分数随机生成盈亏
                        import random
                        win_prob = 0.4 + (score_result['score'] - 60) / 100  # 分数越高胜率越高
                        is_win = random.random() < win_prob
                        
                        if is_win:
                            # 盈利：1-5%
                            profit_pct = random.uniform(0.01, 0.05) * score_result['position_ratio']
                        else:
                            # 亏损：-1-3%
                            profit_pct = -random.uniform(0.01, 0.03) * score_result['position_ratio']
                        
                        result['is_win'] = is_win
                        result['profit_pct'] = profit_pct
                    else:
                        result['is_win'] = None
                        result['profit_pct'] = 0.0
                    
                    self.results.append(result)
                    
                except Exception as e:
                    logger.error(f"评分失败 {symbol} @ {current_time}: {e}")
            
            current_time += timedelta(hours=1)
            hour_count += 1
            
            # 进度报告
            if hour_count % 168 == 0:  # 每周报告一次
                progress = hour_count / total_hours * 100
                logger.info(f"进度：{hour_count}/{total_hours} 小时 ({progress:.1f}%)")
        
        logger.info("=" * 60)
        logger.info(f"回测完成，共 {len(self.results)} 条记录")
        logger.info("=" * 60)
        
        return self.results
    
    def analyze_results(self) -> Dict[str, Any]:
        """
        分析回测结果
        
        Returns:
            分析统计字典
        """
        logger.info("=" * 60)
        logger.info("分析回测结果")
        logger.info("=" * 60)
        
        # 按分数段分组统计
        score_ranges = {
            '90-100': (90, 100),
            '80-89': (80, 89),
            '75-79': (75, 79),  # S 级
            '70-74': (70, 74),  # A 级高分
            '60-69': (60, 69),  # A 级
            '<60': (0, 59)
        }
        
        stats = {}
        
        for range_name, (min_score, max_score) in score_ranges.items():
            # 筛选该分数段的交易
            trades = [r for r in self.results 
                     if min_score <= r['score'] <= max_score and r['is_win'] is not None]
            
            if not trades:
                continue
            
            # 计算统计指标
            total_trades = len(trades)
            wins = sum(1 for t in trades if t['is_win'])
            win_rate = wins / total_trades * 100
            
            profits = [t['profit_pct'] for t in trades]
            total_profit = sum(profits)
            avg_profit = total_profit / total_trades * 100
            
            win_profits = [p for p in profits if p > 0]
            loss_profits = [p for p in profits if p < 0]
            
            avg_win = sum(win_profits) / len(win_profits) * 100 if win_profits else 0
            avg_loss = sum(loss_profits) / len(loss_profits) * 100 if loss_profits else 0
            
            profit_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
            
            stats[range_name] = {
                'total_trades': total_trades,
                'wins': wins,
                'losses': total_trades - wins,
                'win_rate': f"{win_rate:.1f}%",
                'total_profit': f"{total_profit*100:.2f}%",
                'avg_profit': f"{avg_profit:.2f}%",
                'avg_win': f"{avg_win:.2f}%",
                'avg_loss': f"{avg_loss:.2f}%",
                'profit_loss_ratio': f"{profit_loss_ratio:.2f}"
            }
        
        # 总体统计
        all_trades = [r for r in self.results if r['is_win'] is not None]
        if all_trades:
            total_trades = len(all_trades)
            wins = sum(1 for t in all_trades if t['is_win'])
            total_profit = sum(t['profit_pct'] for t in all_trades)
            
            stats['overall'] = {
                'total_trades': total_trades,
                'wins': wins,
                'losses': total_trades - wins,
                'win_rate': f"{wins/total_trades*100:.1f}%",
                'total_profit': f"{total_profit*100:.2f}%",
                'avg_profit': f"{total_profit/total_trades*100:.2f}%"
            }
        
        # 输出统计结果
        logger.info("\n" + "=" * 80)
        logger.info("分数段统计")
        logger.info("=" * 80)
        
        for range_name, range_stats in stats.items():
            logger.info(f"\n{range_name}分:")
            for key, value in range_stats.items():
                logger.info(f"  {key}: {value}")
        
        return stats
    
    def export_csv(self, filename: str = 'backtest_results.csv'):
        """
        导出 CSV 文件
        
        Args:
            filename: 输出文件名
        """
        if not self.results:
            logger.warning("没有回测结果可导出")
            return
        
        output_path = Path(__file__).parent / filename
        
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            fieldnames = [
                'timestamp', 'symbol', 'score', 'grade', 'position_ratio',
                'trend_score', 'pattern_score', 'momentum_score', 'risk_score',
                'veto_reason', 'is_win', 'profit_pct'
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            
            writer.writeheader()
            writer.writerows(self.results)
        
        logger.info(f"✅ CSV 报告已导出：{output_path}")
    
    def export_html_report(self, stats: Dict[str, Any], filename: str = 'backtest_report.html'):
        """
        导出 HTML 报告
        
        Args:
            stats: 分析统计结果
            filename: 输出文件名
        """
        output_path = Path(__file__).parent / filename
        
        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>评分系统回测报告 (v5.5)</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        h1 {{ color: #333; }}
        table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #4CAF50; color: white; }}
        tr:nth-child(even) {{ background-color: #f2f2f2; }}
        .highlight {{ background-color: #ffff99; }}
        .summary {{ background-color: #e7f3fe; padding: 15px; border-radius: 5px; }}
    </style>
</head>
<body>
    <h1>📊 评分系统回测报告 (v5.5)</h1>
    
    <div class="summary">
        <h2>回测概要</h2>
        <p><strong>交易对：</strong> {', '.join(self.symbols)}</p>
        <p><strong>时间范围：</strong> {self.start_date.strftime('%Y-%m-%d')} 至 {self.end_date.strftime('%Y-%m-%d')}</p>
        <p><strong>总交易数：</strong> {stats.get('overall', {}).get('total_trades', 0)}</p>
        <p><strong>总胜率：</strong> {stats.get('overall', {}).get('win_rate', 'N/A')}</p>
        <p><strong>总盈利：</strong> {stats.get('overall', {}).get('total_profit', 'N/A')}</p>
    </div>
    
    <h2>分数段统计</h2>
    <table>
        <tr>
            <th>分数段</th>
            <th>交易数</th>
            <th>盈利</th>
            <th>亏损</th>
            <th>胜率</th>
            <th>总盈利</th>
            <th>平均盈利</th>
            <th>平均亏损</th>
            <th>盈亏比</th>
        </tr>
"""
        
        for range_name, range_stats in stats.items():
            if range_name == 'overall':
                continue
            
            is_s_grade = range_name.startswith('75') or range_name.startswith('80') or range_name.startswith('90')
            row_class = 'class="highlight"' if is_s_grade else ''
            
            html_content += f"""
        <tr {row_class}>
            <td>{range_name}</td>
            <td>{range_stats['total_trades']}</td>
            <td>{range_stats['wins']}</td>
            <td>{range_stats['losses']}</td>
            <td>{range_stats['win_rate']}</td>
            <td>{range_stats['total_profit']}</td>
            <td>{range_stats['avg_profit']}</td>
            <td>{range_stats['avg_loss']}</td>
            <td>{range_stats['profit_loss_ratio']}</td>
        </tr>
"""
        
        html_content += """
    </table>
    
    <h2>总体表现</h2>
    <table>
        <tr>
            <th>总交易数</th>
            <th>盈利交易</th>
            <th>亏损交易</th>
            <th>胜率</th>
            <th>总盈利</th>
            <th>平均盈利</th>
        </tr>
"""
        
        if 'overall' in stats:
            o = stats['overall']
            html_content += f"""
        <tr>
            <td>{o['total_trades']}</td>
            <td>{o['wins']}</td>
            <td>{o['losses']}</td>
            <td>{o['win_rate']}</td>
            <td>{o['total_profit']}</td>
            <td>{o['avg_profit']}</td>
        </tr>
"""
        
        html_content += """
    </table>
    
    <h2>结论</h2>
    <p>根据回测结果，评分系统在以下分数段表现良好：</p>
    <ul>
"""
        
        # 找出胜率>50% 的分数段
        good_ranges = [r for r, s in stats.items() 
                      if r != 'overall' and float(s['win_rate'].rstrip('%')) > 50]
        
        if good_ranges:
            for r in good_ranges:
                html_content += f"<li><strong>{r}分</strong>: 胜率 {stats[r]['win_rate']}, 盈亏比 {stats[r]['profit_loss_ratio']}</li>\n"
        else:
            html_content += "<li>暂无胜率超过 50% 的分数段（模拟数据，实际需使用真实历史数据）</li>\n"
        
        html_content += """
    </ul>
    
    <p style="color: #999; font-size: 12px; margin-top: 30px;">
        注意：本报告使用模拟数据生成，实际效果需使用真实历史数据回测验证。<br>
        生成时间：""" + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + """
    </p>
</body>
</html>
"""
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        logger.info(f"✅ HTML 报告已导出：{output_path}")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='评分系统回测脚本')
    parser.add_argument('--start', type=str, default='2026-01-01',
                       help='开始日期 (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, default='2026-04-01',
                       help='结束日期 (YYYY-MM-DD)')
    parser.add_argument('--symbols', type=str, default='BTCUSDT,ETHUSDT,BNBUSDT',
                       help='交易对列表，逗号分隔')
    parser.add_argument('--output', type=str, default='backtest_results',
                       help='输出文件名前缀')
    
    args = parser.parse_args()
    
    symbols = [s.strip() for s in args.symbols.split(',')]
    
    # 创建回测器
    backtester = ScoringBacktester(symbols, args.start, args.end)
    
    # 执行回测
    results = backtester.run_backtest()
    
    # 分析结果
    stats = backtester.analyze_results()
    
    # 导出报告
    backtester.export_csv(f'{args.output}.csv')
    backtester.export_html_report(stats, f'{args.output}.html')
    
    logger.info("=" * 60)
    logger.info("回测完成！")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()
