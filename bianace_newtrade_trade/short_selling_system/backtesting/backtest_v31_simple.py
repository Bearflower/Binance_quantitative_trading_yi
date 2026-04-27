#!/usr/bin/env python3
"""
回测系统 v3.1（简化独立版）

基于 1 小时 K 线的技术面分析
- 三次冲顶形态识别
- 成交量分析
- 信号冷却机制
- 每小时第 1 分钟评分

使用方法:
    python backtest_v31_simple.py --data data/backtest_data.json --days 90
"""

import json
import argparse
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from pathlib import Path
import statistics
import numpy as np

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class TechnicalAnalyzerV31Simple:
    """技术分析器 v3.1（简化独立版）"""
    
    def __init__(self):
        self.volume_ma_period = 5
        self.volume_multiplier = 1.5
        self.top_price_tolerance = 0.02
    
    def analyze_trend(self, klines: List[Dict]) -> str:
        """分析趋势（简化版，支持最少 10 根 K 线）"""
        if len(klines) < 10:
            return 'unknown'
        
        closes = [k['close'] for k in klines]
        
        # 根据可用数据量调整判断周期
        if len(closes) >= 50:
            compare_period = 20
        elif len(closes) >= 20:
            compare_period = 10
        else:  # 10-19 根
            compare_period = 5
        
        # 简单趋势判断：比较当前价格与 N 周期前的价格
        if closes[-1] < closes[-compare_period]:
            return 'downtrend'
        elif closes[-1] > closes[-compare_period] * 1.05:
            return 'uptrend'
        else:
            return 'sideways'
    
    def detect_three_tops(self, klines: List[Dict], lookback: int = 5) -> Tuple[bool, Optional[float]]:
        """检测三次冲顶"""
        if len(klines) < lookback:
            return False, None
        
        recent_klines = klines[-lookback:]
        highs = [k['high'] for k in recent_klines]
        
        if len(highs) < 3:
            return False, None
        
        # 找出前 3 个高点
        sorted_highs = sorted(highs, reverse=True)[:3]
        
        # 检查是否在同一水平（容忍 2% 误差）
        resistance_level = sorted_highs[0]
        tolerance = resistance_level * self.top_price_tolerance
        
        tops_at_resistance = sum(
            1 for high in sorted_highs 
            if abs(high - resistance_level) <= tolerance
        )
        
        if tops_at_resistance >= 3:
            return True, resistance_level
        
        # 检查高点是否逐次降低
        if sorted_highs[0] > sorted_highs[1] > sorted_highs[2]:
            decrease_threshold = 0.005
            if (sorted_highs[0] - sorted_highs[1]) / sorted_highs[0] > decrease_threshold and \
               (sorted_highs[1] - sorted_highs[2]) / sorted_highs[1] > decrease_threshold:
                return True, sorted_highs[0]
        
        return False, None
    
    def analyze_volume(self, klines: List[Dict]) -> Dict:
        """分析成交量"""
        if len(klines) < self.volume_ma_period + 1:
            return {'is_high_volume': False, 'volume_ratio': 0}
        
        recent_volumes = [k['volume'] for k in klines[-self.volume_ma_period-1:-1]]
        avg_volume = sum(recent_volumes) / len(recent_volumes)
        current_volume = klines[-1]['volume']
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0
        
        return {
            'is_high_volume': volume_ratio >= self.volume_multiplier,
            'volume_ratio': round(volume_ratio, 2)
        }
    
    def check_volume_price_divergence(self, klines: List[Dict]) -> bool:
        """检查量价背离"""
        if len(klines) < self.volume_ma_period + 1:
            return False
        
        volume_analysis = self.analyze_volume(klines)
        if not volume_analysis['is_high_volume']:
            return False
        
        current_high = klines[-1]['high']
        previous_high = max(k['high'] for k in klines[-self.volume_ma_period-1:-1])
        
        return current_high <= previous_high
    
    def calculate_technical_score(self, klines: List[Dict]) -> Tuple[float, Dict]:
        """计算技术面评分（支持最少 10 根 K 线）"""
        details = {
            'trend': 'unknown',
            'three_tops': False,
            'volume_price_divergence': False,
            'volume_ratio': 0
        }
        
        # 支持最少 10 根 K 线
        if len(klines) < 10:
            return 5.0, details
        
        # 趋势评分（4 分）
        trend = self.analyze_trend(klines)
        details['trend'] = trend
        trend_score = 4.0 if trend == 'downtrend' else (2.0 if trend == 'sideways' else 0.0)
        
        # 三次冲顶检测
        three_tops, _ = self.detect_three_tops(klines)
        details['three_tops'] = three_tops
        pattern_bonus = 1.0 if three_tops else 0.0
        
        # 量价背离检测
        divergence = self.check_volume_price_divergence(klines)
        details['volume_price_divergence'] = divergence
        divergence_bonus = 1.0 if divergence else 0.0
        
        volume_analysis = self.analyze_volume(klines)
        details['volume_ratio'] = volume_analysis['volume_ratio']
        
        # 总分（简化版，满分 10 分）
        total_score = min(trend_score + 3.0 + 2.0 + pattern_bonus + divergence_bonus, 10.0)
        
        return total_score, details


class BacktestEngineV31Simple:
    """回测引擎 v3.1（简化版）"""
    
    def __init__(self):
        self.analyzer = TechnicalAnalyzerV31Simple()
        self.trades: List[Dict] = []
        self.cooldown_records: Dict[str, datetime] = {}
        self.config = {
            'signal_threshold': 6.0,
            'listing_hours_max': 48,
            'cooldown_hours': 2
        }
    
    def is_in_cooldown(self, symbol: str, current_time: datetime) -> bool:
        """检查冷却期"""
        if symbol not in self.cooldown_records:
            return False
        
        last_trade = self.cooldown_records[symbol]
        hours = (current_time - last_trade).total_seconds() / 3600
        return hours < self.config['cooldown_hours']
    
    def run_backtest(self, data: Dict, start_date: datetime, end_date: datetime) -> Dict:
        """运行回测"""
        logger.info(f"🚀 开始回测 v3.1: {start_date} ~ {end_date}")
        
        # 生成评分时点（每小时第 1 分钟）
        scoring_times = []
        current = start_date
        while current <= end_date:
            if current.minute == 1:
                scoring_times.append(current)
            current += timedelta(minutes=1)
        
        logger.info(f"📊 共有 {len(scoring_times)} 个评分时点")
        
        # 遍历每个评分时点
        for scoring_time in scoring_times:
            for symbol, symbol_data in data.items():
                # 获取上线时间
                symbol_info = symbol_data.get('symbol_info', {})
                list_time = symbol_info.get('listTime')
                
                if not list_time:
                    continue
                
                listing_time = datetime.fromtimestamp(list_time / 1000)
                
                # 检查是否已上线
                if scoring_time < listing_time:
                    continue
                
                # 检查时间窗口
                hours_since = (scoring_time - listing_time).total_seconds() / 3600
                if hours_since > self.config['listing_hours_max']:
                    continue
                
                # 检查冷却期
                if self.is_in_cooldown(symbol, scoring_time):
                    continue
                
                # 获取 K 线
                klines_1h = symbol_data.get('1h', [])
                if len(klines_1h) < 10:  # 支持最少 10 根
                    continue
                
                # 找到可用 K 线
                scoring_ts = int(scoring_time.timestamp() * 1000)
                available = [k for k in klines_1h if k['timestamp'] < scoring_ts]
                
                if len(available) < 10:  # 支持最少 10 根
                    continue
                
                # 计算评分
                score, details = self.analyzer.calculate_technical_score(available)
                
                # 判断是否开仓
                if score >= self.config['signal_threshold']:
                    current_price = available[-1]['close']
                    
                    trade = {
                        'symbol': symbol,
                        'entry_time': scoring_time,
                        'entry_price': current_price,
                        'score': score,
                        'details': details,
                        'listing_hours': hours_since
                    }
                    
                    self.trades.append(trade)
                    self.cooldown_records[symbol] = scoring_time
                    
                    logger.info(
                        f"🎯 {symbol} 开仓："
                        f"时间={scoring_time}, "
                        f"价格={current_price:.4f}, "
                        f"评分={score:.2f}, "
                        f"三次冲顶={details['three_tops']}, "
                        f"量价背离={details['volume_price_divergence']}"
                    )
        
        logger.info(f"✅ 回测完成，共 {len(self.trades)} 笔交易")
        
        return {
            'trades': self.trades,
            'total_trades': len(self.trades),
            'start_date': start_date,
            'end_date': end_date
        }
    
    def analyze_performance(self, results: Dict) -> Dict:
        """分析表现"""
        trades = results.get('trades', [])
        
        if not trades:
            return {'total_trades': 0, 'message': '没有交易记录'}
        
        # 按币种统计
        symbol_trades: Dict[str, List] = {}
        for trade in trades:
            symbol = trade['symbol']
            if symbol not in symbol_trades:
                symbol_trades[symbol] = []
            symbol_trades[symbol].append(trade)
        
        stats = {
            'total_trades': len(trades),
            'unique_symbols': len(symbol_trades),
            'avg_score': statistics.mean([t['score'] for t in trades]),
            'avg_listing_hours': statistics.mean([t['listing_hours'] for t in trades]),
            'three_tops_count': sum(1 for t in trades if t['details']['three_tops']),
            'volume_divergence_count': sum(1 for t in trades if t['details']['volume_price_divergence']),
            'trades_by_symbol': {s: len(lst) for s, lst in symbol_trades.items()}
        }
        
        return stats


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='回测系统 v3.1（简化版）')
    parser.add_argument('--data', type=str, default='data/backtest_data.json',
                        help='回测数据文件')
    parser.add_argument('--days', type=int, default=90,
                        help='回测天数')
    parser.add_argument('--output', type=str, default='results/backtest_v31_simple.json',
                        help='输出文件')
    
    args = parser.parse_args()
    
    # 加载数据
    logger.info(f"📂 加载回测数据：{args.data}")
    with open(args.data, 'r', encoding='utf-8') as f:
        data = json.load(f)
    logger.info(f"✅ 加载成功，共 {len(data)} 个币种")
    
    # 确定时间范围
    all_timestamps = []
    for symbol_data in data.values():
        klines_1h = symbol_data.get('1h', [])
        for kline in klines_1h:
            all_timestamps.append(kline['timestamp'])
    
    if not all_timestamps:
        logger.error("❌ 没有 K 线数据")
        return
    
    end_date = datetime.fromtimestamp(max(all_timestamps) / 1000)
    start_date = end_date - timedelta(days=args.days)
    
    logger.info(f"📊 回测期间：{start_date} ~ {end_date}")
    
    # 运行回测
    engine = BacktestEngineV31Simple()
    results = engine.run_backtest(data, start_date, end_date)
    
    # 分析表现
    performance = engine.analyze_performance(results)
    
    # 保存结果
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({
            'results': results,
            'performance': performance
        }, f, indent=2, ensure_ascii=False, default=str)
    
    logger.info(f"💾 回测结果已保存：{output_path}")
    
    # 打印摘要
    print("\n" + "=" * 80)
    print("回测结果 v3.1 摘要")
    print("=" * 80)
    print(f"\n📊 基础统计")
    print(f"  总交易数：{performance.get('total_trades', 0)} 笔")
    print(f"  涉及币种：{performance.get('unique_symbols', 0)} 个")
    print(f"  平均评分：{performance.get('avg_score', 0):.2f} 分")
    print(f"  平均上线时间：{performance.get('avg_listing_hours', 0):.1f} 小时")
    
    print(f"\n🔝 形态统计")
    print(f"  三次冲顶：{performance.get('three_tops_count', 0)} 次")
    print(f"  量价背离：{performance.get('volume_divergence_count', 0)} 次")
    
    print(f"\n📈 按币种统计（Top 10）")
    trades_by_symbol = performance.get('trades_by_symbol', {})
    sorted_symbols = sorted(trades_by_symbol.items(), key=lambda x: x[1], reverse=True)
    for symbol, count in sorted_symbols[:10]:
        print(f"  {symbol}: {count} 笔")
    
    print("\n" + "=" * 80 + "\n")


if __name__ == '__main__':
    main()
