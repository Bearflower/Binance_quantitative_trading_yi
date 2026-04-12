#!/usr/bin/env python3
"""
真实数据回测器 (v5.5)

使用真实的历史多时间框架数据进行回测：
- 数据源：data/multi_timeframe_data.json
- 支持 BTCUSDT, ETHUSDT, BNBUSDT
- 时间框架：1d, 4h, 1h

功能：
1. 加载真实历史数据
2. 逐小时回测评分系统
3. 计算实际盈亏（基于后续价格）
4. 统计各分数段的胜率、盈亏比、夏普比率
5. 生成详细的 CSV 和 HTML 报告

使用方法：
python backtest/real_data_backtest.py --symbols BTCUSDT --output btc_backtest
"""

import json
import logging
import argparse
import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict
import math

# 添加项目根目录到路径
import sys
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.scoring_engine import get_scoring_engine

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('real_backtest')


class RealDataBacktester:
    """真实数据回测器"""
    
    def __init__(self, data_file: str = 'data/multi_timeframe_data.json'):
        """
        初始化回测器
        
        Args:
            data_file: 数据文件路径
        """
        self.data_file = Path(__file__).parent.parent / data_file
        self.data = None
        self.scoring_engine = get_scoring_engine()
        
        logger.info(f"真实数据回测器初始化")
        logger.info(f"数据文件：{self.data_file}")
    
    def load_data(self, symbols: List[str] = None):
        """
        加载数据
        
        Args:
            symbols: 交易对列表，None 表示全部
        """
        logger.info("加载数据...")
        
        with open(self.data_file, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
        
        if symbols is None:
            symbols = list(self.data.keys())
        
        # 验证数据
        for symbol in symbols:
            if symbol not in self.data:
                logger.warning(f"⚠️ {symbol} 数据不存在")
                continue
            
            tf_data = self.data[symbol]
            for tf in ['1d', '4h', '1h']:
                if tf in tf_data:
                    count = len(tf_data[tf])
                    logger.info(f"✅ {symbol} {tf}: {count} 条 K 线")
                else:
                    logger.warning(f"⚠️ {symbol} {tf} 数据缺失")
        
        logger.info(f"数据加载完成")
    
    def prepare_indicators(self, symbol: str, timestamp: datetime, 
                          current_idx: Dict[str, int]) -> Optional[Dict[str, Any]]:
        """
        准备指标数据
        
        Args:
            symbol: 交易对
            timestamp: 当前时间
            current_idx: 各时间框架当前索引
        
        Returns:
            指标数据字典
        """
        if symbol not in self.data:
            return None
        
        symbol_data = self.data[symbol]
        indicators = {}
        
        # 处理各时间框架
        for tf in ['1d', '4h', '1h']:
            if tf not in symbol_data:
                continue
            
            klines = symbol_data[tf]
            idx = current_idx.get(tf, 0)
            
            # 确保有足够的历史数据
            if idx < 55:  # 至少需要 55 根 K 线
                continue
            
            # 获取历史 K 线（用于计算指标）
            historical_klines = klines[max(0, idx-100):idx+1]
            
            # 计算指标
            indicators[tf] = self._calculate_indicators(historical_klines, tf)
        
        return indicators
    
    def _calculate_indicators(self, klines: List[Dict], timeframe: str) -> Dict[str, Any]:
        """
        计算技术指标
        
        Args:
            klines: K 线数据列表
            timeframe: 时间框架
        
        Returns:
            指标字典
        """
        if len(klines) < 2:
            return {}
        
        # 提取收盘价
        closes = [float(k['close']) for k in klines]
        highs = [float(k['high']) for k in klines]
        lows = [float(k['low']) for k in klines]
        volumes = [float(k['volume']) for k in klines]
        
        # EMA21
        ema21 = self._calculate_ema(closes, 21)
        
        # RSI14
        rsi14 = self._calculate_rsi(closes, 14)
        
        # MACD
        macd = self._calculate_macd(closes)
        
        # ATR14
        atr14 = self._calculate_atr(highs, lows, closes, 14)
        
        # 构建 K 线数据
        kline_data = []
        for k in klines[-10:]:  # 最近 10 根
            kline_data.append({
                'open': float(k['open']),
                'close': float(k['close']),
                'high': float(k['high']),
                'low': float(k['low']),
                'volume': float(k['volume'])
            })
        
        return {
            'close': closes,
            'high': highs,
            'low': lows,
            'volume': volumes,
            'ema21': ema21,
            'rsi14': rsi14,
            'macd': macd,
            'atr14': atr14,
            'klines': kline_data
        }
    
    def _calculate_ema(self, prices: List[float], period: int) -> List[float]:
        """计算 EMA"""
        if len(prices) < period:
            return [prices[-1]] * len(prices) if prices else []
        
        ema = []
        multiplier = 2 / (period + 1)
        
        # 第一个 EMA 使用 SMA
        sma = sum(prices[:period]) / period
        ema.append(sma)
        
        # 计算后续 EMA
        for i in range(1, len(prices)):
            ema_val = (prices[i] - ema[-1]) * multiplier + ema[-1]
            ema.append(ema_val)
        
        return ema
    
    def _calculate_rsi(self, prices: List[float], period: int = 14) -> List[float]:
        """计算 RSI"""
        if len(prices) < period + 1:
            return [50.0] * len(prices) if prices else []
        
        rsi = []
        gains = []
        losses = []
        
        # 计算价格变化
        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            gains.append(max(0, change))
            losses.append(max(0, -change))
        
        # 第一个 RSI
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        
        if avg_loss == 0:
            rsi.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100 - (100 / (1 + rs)))
        
        # 后续 RSI（平滑）
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            
            if avg_loss == 0:
                rsi.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi.append(100 - (100 / (1 + rs)))
        
        # 填充前面的值
        rsi = [50.0] * period + rsi
        
        return rsi
    
    def _calculate_macd(self, prices: List[float]) -> List[Dict[str, float]]:
        """计算 MACD"""
        if len(prices) < 26:
            return [{'dif': 0, 'dea': 0, 'histogram': 0}] * len(prices)
        
        ema12 = self._calculate_ema(prices, 12)
        ema26 = self._calculate_ema(prices, 26)
        
        macd = []
        for i in range(len(prices)):
            dif = ema12[i] - ema26[i]
            dea = self._calculate_ema([ema12[j] - ema26[j] for j in range(i+1)], 9)[-1] if i > 0 else 0
            histogram = dif - dea
            
            macd.append({
                'dif': dif,
                'dea': dea,
                'histogram': histogram
            })
        
        return macd
    
    def _calculate_atr(self, highs: List[float], lows: List[float], 
                      closes: List[float], period: int = 14) -> List[float]:
        """计算 ATR"""
        if len(highs) < period:
            return [0] * len(highs)
        
        tr = []
        for i in range(1, len(highs)):
            tr1 = highs[i] - lows[i]
            tr2 = abs(highs[i] - closes[i-1])
            tr3 = abs(lows[i] - closes[i-1])
            tr.append(max(tr1, tr2, tr3))
        
        # 简单移动平均
        atr = []
        for i in range(len(highs)):
            if i < period - 1:
                atr.append(0)
            else:
                atr.append(sum(tr[max(0, i-period+1):i+1]) / min(i+1, period))
        
        return atr
    
    def simulate_trade(self, symbol: str, entry_time: datetime, 
                      entry_price: float, direction: str,
                      hold_periods: int = 24) -> Tuple[float, float]:
        """
        模拟交易
        
        Args:
            symbol: 交易对
            entry_time: 入场时间
            entry_price: 入场价格
            direction: 方向（'多'/'空'）
            hold_periods: 持仓小时数
        
        Returns:
            (盈亏百分比，最高浮盈，最大回撤)
        """
        if symbol not in self.data:
            return 0.0, 0.0, 0.0
        
        # 获取 1 小时数据
        hourly_data = self.data[symbol].get('1h', [])
        
        # 找到入场时间点
        entry_idx = -1
        for i, k in enumerate(hourly_data):
            k_time = datetime.fromisoformat(k['timestamp'].replace('Z', '+00:00'))
            if k_time >= entry_time:
                entry_idx = i
                break
        
        if entry_idx == -1 or entry_idx >= len(hourly_data) - hold_periods:
            return 0.0, 0.0, 0.0
        
        # 模拟持仓过程
        max_profit = 0.0
        max_drawdown = 0.0
        exit_price = hourly_data[entry_idx + hold_periods]['close']
        
        for i in range(entry_idx, min(entry_idx + hold_periods, len(hourly_data))):
            current_price = float(hourly_data[i]['close'])
            
            if direction == '多':
                profit_pct = (current_price - entry_price) / entry_price * 100
            else:
                profit_pct = (entry_price - current_price) / entry_price * 100
            
            max_profit = max(max_profit, profit_pct)
            max_drawdown = min(max_drawdown, profit_pct)
        
        # 计算最终盈亏
        if direction == '多':
            final_profit = (float(exit_price) - entry_price) / entry_price * 100
        else:
            final_profit = (entry_price - float(exit_price)) / entry_price * 100
        
        return final_profit, max_profit, abs(max_drawdown)
    
    def run_backtest(self, symbols: List[str], 
                    start_date: str = None,
                    end_date: str = None,
                    hold_periods: int = 24) -> List[Dict[str, Any]]:
        """
        执行回测
        
        Args:
            symbols: 交易对列表
            start_date: 开始日期
            end_date: 结束日期
            hold_periods: 持仓小时数
        
        Returns:
            回测结果列表
        """
        logger.info("=" * 60)
        logger.info("开始执行真实数据回测")
        logger.info("=" * 60)
        
        self.load_data(symbols)
        
        results = []
        total_signals = 0
        
        for symbol in symbols:
            if symbol not in self.data:
                continue
            
            logger.info(f"\n回测 {symbol}...")
            
            # 获取 1 小时数据确定时间范围
            hourly_data = self.data[symbol].get('1h', [])
            if not hourly_data:
                continue
            
            # 时间范围
            start_time = datetime.fromisoformat(hourly_data[0]['timestamp'].replace('Z', '+00:00'))
            end_time = datetime.fromisoformat(hourly_data[-1]['timestamp'].replace('Z', '+00:00'))
            
            if start_date:
                start_time = max(start_time, datetime.fromisoformat(start_date))
            if end_date:
                end_time = min(end_time, datetime.fromisoformat(end_date))
            
            logger.info(f"时间范围：{start_time} 至 {end_time}")
            
            # 初始化索引
            current_idx = {'1d': 60, '4h': 60, '1h': 60}
            
            # 逐小时回测
            hour_count = 0
            for hour_idx in range(60, len(hourly_data)):
                current_kline = hourly_data[hour_idx]
                current_time = datetime.fromisoformat(current_kline['timestamp'].replace('Z', '+00:00'))
                
                if current_time > end_time:
                    break
                
                hour_count += 1
                
                # 更新索引
                current_idx['1h'] = hour_idx
                current_idx['4h'] = hour_idx // 4
                current_idx['1d'] = hour_idx // 24
                
                # 准备数据
                indicators = self.prepare_indicators(symbol, current_time, current_idx)
                
                if not indicators or len(indicators) < 3:
                    continue
                
                # 获取资金费率（模拟）
                funding_rate = 0.0001  # 默认值
                
                # 计算 24 小时涨跌幅
                if hour_idx >= 24:
                    price_24h_ago = float(hourly_data[hour_idx-24]['close'])
                    current_price = float(current_kline['close'])
                    price_change_24h = (current_price - price_24h_ago) / price_24h_ago
                else:
                    price_change_24h = 0.0
                
                # 构建数据
                data = {
                    'funding_rate': funding_rate,
                    'price_change_24h': price_change_24h,
                    'indicators': indicators
                }
                
                # 执行评分
                try:
                    score_result = self.scoring_engine.score(symbol, data)
                    
                    if score_result['grade']:
                        # 有交易信号
                        current_price = float(current_kline['close'])
                        
                        # 模拟交易
                        direction = '多' if score_result['grade'] else '空'  # 简化
                        profit, max_profit, max_dd = self.simulate_trade(
                            symbol, current_time, current_price, direction, hold_periods
                        )
                        
                        result = {
                            'timestamp': current_time.isoformat(),
                            'symbol': symbol,
                            'score': score_result['score'],
                            'grade': score_result['grade'],
                            'position_ratio': score_result['position_ratio'],
                            'trend_score': score_result['score_detail'].get('trend', 0),
                            'pattern_score': score_result['score_detail'].get('pattern', 0),
                            'momentum_score': score_result['score_detail'].get('momentum', 0),
                            'risk_score': score_result['score_detail'].get('risk', 0),
                            'entry_price': current_price,
                            'direction': direction,
                            'profit_pct': profit,
                            'max_profit': max_profit,
                            'max_drawdown': max_dd,
                            'is_win': profit > 0
                        }
                        
                        results.append(result)
                        total_signals += 1
                        
                        if total_signals % 10 == 0:
                            logger.info(f"  已处理 {total_signals} 个信号...")
                
                except Exception as e:
                    logger.error(f"评分失败 {symbol} @ {current_time}: {e}")
                    continue
            
            logger.info(f"✅ {symbol} 回测完成，生成 {len([r for r in results if r['symbol'] == symbol])} 个信号")
        
        logger.info("=" * 60)
        logger.info(f"回测完成，共 {len(results)} 个信号")
        logger.info("=" * 60)
        
        return results
    
    def analyze_results(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        分析回测结果
        
        Args:
            results: 回测结果列表
        
        Returns:
            分析统计字典
        """
        logger.info("=" * 60)
        logger.info("分析回测结果")
        logger.info("=" * 60)
        
        # 按分数段分组
        score_ranges = {
            '90-100': (90, 100),
            '80-89': (80, 89),
            '75-79': (75, 79),
            '70-74': (70, 74),
            '60-69': (60, 69),
            '<60': (0, 59)
        }
        
        stats = {}
        
        for range_name, (min_score, max_score) in score_ranges.items():
            trades = [r for r in results 
                     if min_score <= r['score'] <= max_score and r.get('is_win') is not None]
            
            if not trades:
                continue
            
            total_trades = len(trades)
            wins = sum(1 for t in trades if t['is_win'])
            win_rate = wins / total_trades * 100
            
            profits = [t['profit_pct'] for t in trades]
            total_profit = sum(profits)
            avg_profit = total_profit / total_trades
            
            win_profits = [p for p in profits if p > 0]
            loss_profits = [p for p in profits if p < 0]
            
            avg_win = sum(win_profits) / len(win_profits) if win_profits else 0
            avg_loss = sum(loss_profits) / len(loss_profits) if loss_profits else 0
            
            profit_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
            
            # 计算夏普比率（简化）
            if len(profits) > 1:
                import statistics
                avg = statistics.mean(profits)
                std = statistics.stdev(profits)
                sharpe = (avg / std) * math.sqrt(252) if std > 0 else 0
            else:
                sharpe = 0
            
            stats[range_name] = {
                'total_trades': total_trades,
                'wins': wins,
                'losses': total_trades - wins,
                'win_rate': f"{win_rate:.1f}%",
                'total_profit': f"{total_profit:.2f}%",
                'avg_profit': f"{avg_profit:.2f}%",
                'avg_win': f"{avg_win:.2f}%",
                'avg_loss': f"{avg_loss:.2f}%",
                'profit_loss_ratio': f"{profit_loss_ratio:.2f}",
                'sharpe_ratio': f"{sharpe:.2f}"
            }
        
        # 总体统计
        all_trades = [r for r in results if r.get('is_win') is not None]
        if all_trades:
            total_trades = len(all_trades)
            wins = sum(1 for t in all_trades if t['is_win'])
            profits = [t['profit_pct'] for t in all_trades]
            total_profit = sum(profits)
            
            import statistics
            sharpe = (statistics.mean(profits) / statistics.stdev(profits)) * math.sqrt(252) if len(profits) > 1 else 0
            
            stats['overall'] = {
                'total_trades': total_trades,
                'wins': wins,
                'win_rate': f"{wins/total_trades*100:.1f}%",
                'total_profit': f"{total_profit:.2f}%",
                'avg_profit': f"{total_profit/total_trades:.2f}%",
                'sharpe_ratio': f"{sharpe:.2f}"
            }
        
        # 输出统计
        logger.info("\n" + "=" * 80)
        logger.info("分数段统计")
        logger.info("=" * 80)
        
        for range_name, range_stats in stats.items():
            if range_name == 'overall':
                continue
            
            logger.info(f"\n{range_name}分:")
            for key, value in range_stats.items():
                logger.info(f"  {key}: {value}")
        
        if 'overall' in stats:
            logger.info(f"\n总体表现:")
            for key, value in stats['overall'].items():
                logger.info(f"  {key}: {value}")
        
        return stats
    
    def export_csv(self, results: List[Dict[str, Any]], filename: str = 'real_backtest_results.csv'):
        """导出 CSV"""
        if not results:
            logger.warning("没有结果可导出")
            return
        
        output_path = Path(__file__).parent / filename
        
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            fieldnames = [
                'timestamp', 'symbol', 'score', 'grade', 'position_ratio',
                'trend_score', 'pattern_score', 'momentum_score', 'risk_score',
                'entry_price', 'direction', 'profit_pct', 'max_profit', 'max_drawdown', 'is_win'
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            
            writer.writeheader()
            writer.writerows(results)
        
        logger.info(f"✅ CSV 报告已导出：{output_path}")
    
    def export_html_report(self, stats: Dict[str, Any], results: List[Dict[str, Any]],
                          filename: str = 'real_backtest_report.html'):
        """导出 HTML 报告"""
        output_path = Path(__file__).parent / filename
        
        # 按币种统计
        symbol_stats = defaultdict(lambda: {'trades': 0, 'wins': 0, 'profit': 0})
        for r in results:
            if r.get('is_win') is not None:
                symbol_stats[r['symbol']]['trades'] += 1
                if r['is_win']:
                    symbol_stats[r['symbol']]['wins'] += 1
                symbol_stats[r['symbol']]['profit'] += r['profit_pct']
        
        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>真实数据回测报告 (v5.5)</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        h1, h2 {{ color: #333; }}
        table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #4CAF50; color: white; }}
        tr:nth-child(even) {{ background-color: #f2f2f2; }}
        .highlight {{ background-color: #ffff99; }}
        .summary {{ background-color: #e7f3fe; padding: 15px; border-radius: 5px; margin: 20px 0; }}
        .good {{ color: green; font-weight: bold; }}
        .bad {{ color: red; }}
    </style>
</head>
<body>
    <h1>📊 真实数据回测报告 (v5.5)</h1>
    
    <div class="summary">
        <h2>回测概要</h2>
        <p><strong>总交易数：</strong> {stats.get('overall', {}).get('total_trades', 0)}</p>
        <p><strong>总胜率：</strong> <span class="{'good' if float(stats.get('overall', {}).get('win_rate', '0')[:-1]) > 50 else 'bad'}">{stats.get('overall', {}).get('win_rate', 'N/A')}</span></p>
        <p><strong>总盈利：</strong> <span class="{'good' if float(stats.get('overall', {}).get('total_profit', '0')[:-1]) > 0 else 'bad'}">{stats.get('overall', {}).get('total_profit', 'N/A')}</span></p>
        <p><strong>夏普比率：</strong> {stats.get('overall', {}).get('sharpe_ratio', 'N/A')}</p>
    </div>
    
    <h2>按币种统计</h2>
    <table>
        <tr>
            <th>币种</th>
            <th>交易数</th>
            <th>盈利</th>
            <th>亏损</th>
            <th>胜率</th>
            <th>总盈利</th>
            <th>平均盈利</th>
        </tr>
"""
        
        for symbol, sstats in symbol_stats.items():
            win_rate = sstats['wins'] / sstats['trades'] * 100 if sstats['trades'] > 0 else 0
            avg_profit = sstats['profit'] / sstats['trades'] if sstats['trades'] > 0 else 0
            
            html_content += f"""
        <tr>
            <td>{symbol}</td>
            <td>{sstats['trades']}</td>
            <td>{sstats['wins']}</td>
            <td>{sstats['trades'] - sstats['wins']}</td>
            <td>{win_rate:.1f}%</td>
            <td>{sstats['profit']:.2f}%</td>
            <td>{avg_profit:.2f}%</td>
        </tr>
"""
        
        html_content += """
    </table>
    
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
            <th>夏普比率</th>
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
            <td>{range_stats['sharpe_ratio']}</td>
        </tr>
"""
        
        html_content += """
    </table>
    
    <h2>结论</h2>
    <p>根据真实数据回测结果：</p>
    <ul>
"""
        
        # 找出胜率>50% 的分数段
        good_ranges = [(r, s) for r, s in stats.items() 
                      if r != 'overall' and float(s['win_rate'].rstrip('%')) > 50]
        
        if good_ranges:
            for r, s in sorted(good_ranges, key=lambda x: float(x[1]['win_rate'].rstrip('%')), reverse=True):
                html_content += f"<li><strong class='good'>{r}分</strong>: 胜率 {s['win_rate']}, 盈亏比 {s['profit_loss_ratio']}, 夏普 {s['sharpe_ratio']}</li>\n"
        else:
            html_content += "<li class='bad'>暂无胜率超过 50% 的分数段，可能需要调整评分参数</li>\n"
        
        html_content += """
    </ul>
    
    <p style="color: #999; font-size: 12px; margin-top: 30px;">
        注意：本报告使用真实历史数据回测生成，但交易模拟简化（固定持仓 24 小时）。<br>
        实际交易需考虑手续费、滑点、资金费率等因素。<br>
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
    parser = argparse.ArgumentParser(description='真实数据回测脚本')
    parser.add_argument('--symbols', type=str, default='BTCUSDT,ETHUSDT,BNBUSDT',
                       help='交易对列表，逗号分隔')
    parser.add_argument('--start', type=str, default=None,
                       help='开始日期 (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, default=None,
                       help='结束日期 (YYYY-MM-DD)')
    parser.add_argument('--hold', type=int, default=24,
                       help='持仓小时数（默认 24 小时）')
    parser.add_argument('--output', type=str, default='real_backtest',
                       help='输出文件名前缀')
    
    args = parser.parse_args()
    
    symbols = [s.strip() for s in args.symbols.split(',')]
    
    # 创建回测器
    backtester = RealDataBacktester()
    
    # 执行回测
    results = backtester.run_backtest(symbols, args.start, args.end, args.hold)
    
    # 分析结果
    stats = backtester.analyze_results(results)
    
    # 导出报告
    backtester.export_csv(results, f'{args.output}.csv')
    backtester.export_html_report(stats, results, f'{args.output}.html')
    
    logger.info("=" * 60)
    logger.info("回测完成！")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()
