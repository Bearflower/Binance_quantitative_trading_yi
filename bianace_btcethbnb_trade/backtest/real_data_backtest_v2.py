#!/usr/bin/env python3
"""
真实数据回测器 v2 (v5.5)

优化点：
1. 降低数据完整性要求（55 根 -> 30 根）
2. 改进方向判断（基于 EMA 方向）
3. 添加手续费和滑点模拟
4. 支持更灵活的持仓策略
5. 改进指标计算准确性

使用方法：
python backtest/real_data_backtest_v2.py --symbols BTCUSDT --hold 48 --output btc_optimized
"""

import json
import logging
import argparse
import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
from collections import defaultdict
import math

# 添加项目根目录到路径
import sys
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.scoring_engine_v2 import get_scoring_engine_v2

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('backtest_v2')


class RealDataBacktesterV2:
    """真实数据回测器 v2"""
    
    def __init__(self, data_file: str = 'data/multi_timeframe_data.json'):
        self.data_file = Path(__file__).parent.parent / data_file
        self.data = None
        self.scoring_engine = get_scoring_engine_v2()
        
        # 交易参数
        self.fee_rate = 0.0005  # 手续费 0.05%
        self.slippage = 0.0002  # 滑点 0.02%
    
    def load_data(self, symbols: List[str]):
        """加载数据"""
        logger.info("加载数据...")
        
        with open(self.data_file, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
        
        for symbol in symbols:
            if symbol not in self.data:
                logger.warning(f"⚠️ {symbol} 数据不存在")
                continue
            
            tf_data = self.data[symbol]
            for tf in ['1d', '4h', '1h']:
                if tf in tf_data:
                    count = len(tf_data[tf])
                    logger.info(f"✅ {symbol} {tf}: {count} 条 K 线")
        
        logger.info(f"数据加载完成")
    
    def _calculate_ema(self, prices: List[float], period: int) -> List[float]:
        """计算 EMA"""
        if len(prices) < period:
            return [prices[-1]] * len(prices) if prices else []
        
        ema = []
        multiplier = 2 / (period + 1)
        sma = sum(prices[:period]) / period
        ema.append(sma)
        
        for i in range(1, len(prices)):
            ema_val = (prices[i] - ema[-1]) * multiplier + ema[-1]
            ema.append(ema_val)
        
        return ema
    
    def _calculate_rsi(self, prices: List[float], period: int = 14) -> List[float]:
        """计算 RSI"""
        if len(prices) < period + 1:
            return [50.0] * len(prices)
        
        rsi = []
        gains = []
        losses = []
        
        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            gains.append(max(0, change))
            losses.append(max(0, -change))
        
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        
        if avg_loss == 0:
            rsi.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi.append(100 - (100 / (1 + rs)))
        
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            
            if avg_loss == 0:
                rsi.append(100.0)
            else:
                rs = avg_gain / avg_loss
                rsi.append(100 - (100 / (1 + rs)))
        
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
        
        atr = []
        for i in range(len(highs)):
            if i < period - 1:
                atr.append(0)
            else:
                atr.append(sum(tr[max(0, i-period+1):i+1]) / min(i+1, period))
        
        return atr
    
    def prepare_indicators_v2(self, symbol: str, current_idx: Dict[str, int]) -> Optional[Dict[str, Any]]:
        """
        准备指标数据（优化版）
        """
        if symbol not in self.data:
            return None
        
        symbol_data = self.data[symbol]
        indicators = {}
        
        for tf in ['1d', '4h', '1h']:
            if tf not in symbol_data:
                continue
            
            klines = symbol_data[tf]
            idx = current_idx.get(tf, 0)
            
            # 降低要求：30 根即可
            if idx < 30:
                continue
            
            historical_klines = klines[max(0, idx-100):idx+1]
            
            # 提取数据
            closes = [float(k['close']) for k in historical_klines]
            highs = [float(k['high']) for k in historical_klines]
            lows = [float(k['low']) for k in historical_klines]
            volumes = [float(k['volume']) for k in historical_klines]
            
            # 计算指标
            ema21 = self._calculate_ema(closes, 21)
            rsi14 = self._calculate_rsi(closes, 14)
            macd = self._calculate_macd(closes)
            atr14 = self._calculate_atr(highs, lows, closes, 14)
            
            # 构建 K 线数据
            kline_data = []
            for k in historical_klines[-10:]:
                kline_data.append({
                    'open': float(k['open']),
                    'close': float(k['close']),
                    'high': float(k['high']),
                    'low': float(k['low']),
                    'volume': float(k['volume'])
                })
            
            indicators[tf] = {
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
        
        return indicators
    
    def determine_direction(self, indicators: Dict[str, Any]) -> str:
        """
        判断交易方向（基于 EMA 方向）
        """
        # 多时间框架 EMA 方向判断
        directions = []
        
        for tf in ['1d', '4h', '1h']:
            if tf not in indicators:
                continue
            
            close = indicators[tf]['close'][-1]
            ema21 = indicators[tf]['ema21'][-1]
            
            if close > ema21:
                directions.append(1)  # 向上
            else:
                directions.append(-1)  # 向下
        
        # 多数决定
        if sum(directions) > 0:
            return '多'
        else:
            return '空'
    
    def simulate_trade_v2(self, symbol: str, entry_time: datetime, 
                         entry_price: float, direction: str,
                         hold_periods: int = 48) -> Dict[str, float]:
        """
        模拟交易（含手续费和滑点）
        """
        if symbol not in self.data:
            return {'profit_pct': 0.0, 'max_profit': 0.0, 'max_drawdown': 0.0}
        
        hourly_data = self.data[symbol].get('1h', [])
        
        # 找到入场点
        entry_idx = -1
        for i, k in enumerate(hourly_data):
            k_time = datetime.fromisoformat(k['timestamp'].replace('Z', '+00:00'))
            if k_time >= entry_time:
                entry_idx = i
                break
        
        if entry_idx == -1 or entry_idx >= len(hourly_data) - hold_periods:
            return {'profit_pct': 0.0, 'max_profit': 0.0, 'max_drawdown': 0.0}
        
        # 考虑滑点的入场价
        if direction == '多':
            actual_entry = entry_price * (1 + self.slippage)
        else:
            actual_entry = entry_price * (1 - self.slippage)
        
        max_profit = 0.0
        max_drawdown = 0.0
        
        # 模拟持仓过程
        for i in range(entry_idx, min(entry_idx + hold_periods, len(hourly_data))):
            current_price = float(hourly_data[i]['close'])
            
            if direction == '多':
                profit_pct = (current_price - actual_entry) / actual_entry * 100
            else:
                profit_pct = (actual_entry - current_price) / actual_entry * 100
            
            max_profit = max(max_profit, profit_pct)
            max_drawdown = min(max_drawdown, profit_pct)
        
        # 出场价（考虑滑点）
        exit_idx = min(entry_idx + hold_periods, len(hourly_data) - 1)
        exit_price = float(hourly_data[exit_idx]['close'])
        
        if direction == '多':
            actual_exit = exit_price * (1 - self.slippage)
        else:
            actual_exit = exit_price * (1 + self.slippage)
        
        # 计算最终盈亏（扣除手续费）
        if direction == '多':
            gross_profit = (actual_exit - actual_entry) / actual_entry * 100
        else:
            gross_profit = (actual_entry - actual_exit) / actual_entry * 100
        
        # 扣除双边手续费
        net_profit = gross_profit - self.fee_rate * 2 * 100
        
        return {
            'profit_pct': net_profit,
            'max_profit': max_profit - self.fee_rate * 2 * 100,
            'max_drawdown': abs(max_drawdown) - self.fee_rate * 2 * 100
        }
    
    def run_backtest_v2(self, symbols: List[str], 
                       hold_periods: int = 48,
                       min_score: int = 60) -> List[Dict[str, Any]]:
        """
        执行回测（优化版）
        """
        logger.info("=" * 60)
        logger.info("开始执行真实数据回测 v2")
        logger.info("=" * 60)
        
        self.load_data(symbols)
        
        results = []
        total_signals = 0
        
        for symbol in symbols:
            if symbol not in self.data:
                continue
            
            logger.info(f"\n回测 {symbol}...")
            
            hourly_data = self.data[symbol].get('1h', [])
            if not hourly_data:
                continue
            
            start_time = datetime.fromisoformat(hourly_data[0]['timestamp'].replace('Z', '+00:00'))
            end_time = datetime.fromisoformat(hourly_data[-1]['timestamp'].replace('Z', '+00:00'))
            
            logger.info(f"时间范围：{start_time} 至 {end_time}")
            
            # 初始化索引（从 30 开始）
            current_idx = {'1d': 30, '4h': 30, '1h': 30}
            
            hour_count = 0
            for hour_idx in range(30, len(hourly_data)):
                current_kline = hourly_data[hour_idx]
                current_time = datetime.fromisoformat(current_kline['timestamp'].replace('Z', '+00:00'))
                
                hour_count += 1
                
                # 更新索引
                current_idx['1h'] = hour_idx
                current_idx['4h'] = hour_idx // 4
                current_idx['1d'] = hour_idx // 24
                
                # 准备指标
                indicators = self.prepare_indicators_v2(symbol, current_idx)
                
                if not indicators or len(indicators) < 3:
                    continue
                
                # 获取资金费率（模拟）
                funding_rate = 0.0001
                
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
                
                try:
                    score_result = self.scoring_engine.score(symbol, data)
                    
                    # 只要有信号就交易（不限制等级）
                    if score_result['grade'] and score_result['score'] >= min_score:
                        current_price = float(current_kline['close'])
                        
                        # 判断方向
                        direction = self.determine_direction(indicators)
                        
                        # 模拟交易
                        trade_result = self.simulate_trade_v2(
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
                            'profit_pct': trade_result['profit_pct'],
                            'max_profit': trade_result['max_profit'],
                            'max_drawdown': trade_result['max_drawdown'],
                            'is_win': trade_result['profit_pct'] > 0
                        }
                        
                        results.append(result)
                        total_signals += 1
                        
                        if total_signals % 50 == 0:
                            logger.info(f"  已处理 {total_signals} 个信号...")
                
                except Exception as e:
                    logger.error(f"评分失败 {symbol} @ {current_time}: {e}")
                    continue
            
            logger.info(f"✅ {symbol} 回测完成，生成 {len([r for r in results if r['symbol'] == symbol])} 个信号")
        
        logger.info("=" * 60)
        logger.info(f"回测完成，共 {len(results)} 个信号")
        logger.info("=" * 60)
        
        return results
    
    def export_results(self, results: List[Dict[str, Any]], output_prefix: str):
        """导出结果"""
        # CSV
        csv_path = Path(__file__).parent / f'{output_prefix}.csv'
        
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            fieldnames = [
                'timestamp', 'symbol', 'score', 'grade', 'position_ratio',
                'trend_score', 'pattern_score', 'momentum_score', 'risk_score',
                'entry_price', 'direction', 'profit_pct', 'max_profit', 'max_drawdown', 'is_win'
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            
            writer.writeheader()
            writer.writerows(results)
        
        logger.info(f"✅ CSV 报告已导出：{csv_path}")
        
        # 简单统计
        if results:
            import statistics
            
            total_trades = len(results)
            wins = sum(1 for r in results if r['is_win'])
            win_rate = wins / total_trades * 100
            total_profit = sum(r['profit_pct'] for r in results)
            avg_profit = total_profit / total_trades
            
            profits = [r['profit_pct'] for r in results]
            if len(profits) > 1:
                std = statistics.stdev(profits)
                sharpe = (avg_profit / std) * math.sqrt(252) if std > 0 else 0
            else:
                sharpe = 0
            
            logger.info("\n" + "=" * 60)
            logger.info("回测统计")
            logger.info("=" * 60)
            logger.info(f"总交易数：{total_trades}")
            logger.info(f"盈利次数：{wins}")
            logger.info(f"亏损次数：{total_trades - wins}")
            logger.info(f"胜率：{win_rate:.1f}%")
            logger.info(f"总盈利：{total_profit:.2f}%")
            logger.info(f"平均盈利：{avg_profit:.2f}%")
            logger.info(f"夏普比率：{sharpe:.2f}")
            logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description='真实数据回测 v2')
    parser.add_argument('--symbols', type=str, default='BTCUSDT,ETHUSDT,BNBUSDT',
                       help='交易对列表')
    parser.add_argument('--hold', type=int, default=48,
                       help='持仓小时数')
    parser.add_argument('--min-score', type=int, default=60,
                       help='最小分数')
    parser.add_argument('--output', type=str, default='backtest_v2',
                       help='输出文件名前缀')
    
    args = parser.parse_args()
    
    symbols = [s.strip() for s in args.symbols.split(',')]
    
    backtester = RealDataBacktesterV2()
    results = backtester.run_backtest_v2(symbols, args.hold, args.min_score)
    backtester.export_results(results, args.output)
    
    logger.info("回测完成！")


if __name__ == '__main__':
    main()
