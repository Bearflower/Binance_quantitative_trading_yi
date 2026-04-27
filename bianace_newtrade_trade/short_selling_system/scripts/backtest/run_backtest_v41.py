#!/usr/bin/env python3
"""
V4.1 回测脚本
使用 OI/上线以来总交易量 替代 OI/市值比
使用真实资金费率数据
"""

import json
import sys
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import random

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'short_selling_system'))

from short_selling_system.core.scoring_engine_v41 import ScoringEngineV41, scoring_engine_v41
from short_selling_system.core.pattern_recognition_v4 import PatternRecognitionV4


@dataclass
class BacktestTrade:
    """回测交易记录"""
    symbol: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    pnl_pct: float
    pnl_usd: float
    total_score: float
    contract_score: float
    technical_score: float
    sentiment_score: float
    oi_volume_ratio: float
    funding_rate: float
    exit_reason: str
    holding_hours: float


class BacktestV41:
    """V4.1 回测引擎"""

    def __init__(self):
        self.scoring_engine = scoring_engine_v41
        self.pattern_recognition = PatternRecognitionV4()
        
        self.config = {
            'initial_capital': 100.0,
            'position_size': 4.0,
            'max_leverage': 2,
            'stop_loss_atr': 2.0,
            'take_profit_atr': -1.2,
            'max_holding_hours': 72,
            'atr_period': 14,
            'entry_threshold': 6.5
        }
        
        print("✅ V4.1 回测引擎初始化完成")

    def load_kline_data(self, file_path: str) -> Dict:
        """加载K线数据"""
        with open(file_path, 'r') as f:
            return json.load(f)

    def load_real_data(self, file_path: str) -> Dict:
        """加载真实OI和资金费率数据"""
        with open(file_path, 'r') as f:
            data = json.load(f)
        
        result = {}
        for item in data['data']:
            symbol = item['symbol']
            result[symbol] = {
                'price': item.get('price'),
                'oi': item.get('oi'),
                'oi_usd': item.get('oi_usd'),
                'funding_rate': item.get('funding_rate'),
                'volume_24h': item.get('volume_24h')
            }
        return result

    def calculate_total_volume(self, klines: List[Dict]) -> float:
        """计算上线以来总交易量（USD）"""
        total_volume = 0.0
        for kline in klines:
            quote_volume = float(kline.get('quote_volume', 0))
            total_volume += quote_volume
        return total_volume

    def calculate_atr(self, klines: List[Dict], period: int = 14) -> float:
        """计算ATR"""
        if len(klines) < period:
            period = len(klines)
        
        tr_list = []
        for i in range(1, len(klines)):
            high = float(klines[i]['high'])
            low = float(klines[i]['low'])
            prev_close = float(klines[i-1]['close'])
            
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_list.append(tr)
        
        if len(tr_list) < period:
            return sum(tr_list) / len(tr_list) if tr_list else 0
        
        return sum(tr_list[-period:]) / period

    def run_backtest(self, kline_data: Dict, real_data: Dict) -> List[BacktestTrade]:
        """运行回测"""
        trades = []
        
        symbols = kline_data.get('metadata', {}).get('symbols', [])
        kline_data_dict = kline_data.get('data', {})
        if not symbols:
            symbols = list(kline_data_dict.keys())
        
        all_oi_usd = []
        for symbol in symbols:
            if symbol in real_data and real_data[symbol].get('oi_usd'):
                all_oi_usd.append(real_data[symbol]['oi_usd'])
        
        print(f"\n共有 {len(symbols)} 个币种，{len(all_oi_usd)} 个有OI数据")
        
        processed = 0
        for symbol in symbols:
            symbol_data = kline_data_dict.get(symbol, {})
            if not symbol_data:
                continue
            
            klines_1h = symbol_data.get('1h', {}).get('data', [])
            if not klines_1h or len(klines_1h) < 20:
                continue
            
            real_info = real_data.get(symbol, {})
            funding_rate = real_info.get('funding_rate', 0.00005)
            oi_usd = real_info.get('oi_usd', 0)
            
            if not oi_usd:
                continue
            
            total_volume = self.calculate_total_volume(klines_1h)
            if total_volume <= 0:
                continue
            
            processed += 1
            
            for i in range(20, len(klines_1h)):
                current_kline = klines_1h[i]
                current_price = float(current_kline['close'])
                current_time = datetime.fromtimestamp(current_kline['timestamp'] / 1000)
                
                listing_hours = i
                
                historical_klines = klines_1h[:i+1]
                cumulative_volume = self.calculate_total_volume(historical_klines)
                
                atr = self.calculate_atr(historical_klines, self.config['atr_period'])
                if atr <= 0:
                    continue
                
                pattern_result = self.pattern_recognition.analyze_patterns(historical_klines)
                
                three_tops_score = pattern_result.get('three_tops', {}).get('score', 0)
                technical_score = pattern_result.get('total_score', 0)
                
                if three_tops_score < 2 or technical_score < 4:
                    continue
                
                result = self.scoring_engine.score(
                    symbol=symbol,
                    oi_usd=oi_usd,
                    total_volume_usd=cumulative_volume,
                    funding_rate=funding_rate,
                    three_tops_detected=pattern_result.get('three_tops', {}).get('detected', False),
                    three_tops_score=three_tops_score,
                    long_upper_shadow=pattern_result.get('long_upper_shadow', {}).get('detected', False),
                    long_upper_shadow_score=pattern_result.get('long_upper_shadow', {}).get('score', 0),
                    volume_divergence=pattern_result.get('volume_divergence', {}).get('detected', False),
                    volume_divergence_score=pattern_result.get('volume_divergence', {}).get('score', 0),
                    listing_hours=listing_hours,
                    current_price=current_price,
                    recent_coins_oi=all_oi_usd[-10:] if len(all_oi_usd) >= 10 else all_oi_usd
                )
                
                if result.total_score >= self.config['entry_threshold'] and not result.veto:
                    stop_loss = current_price + atr * self.config['stop_loss_atr']
                    take_profit = current_price - atr * abs(self.config['take_profit_atr'])
                    
                    exit_price = None
                    exit_time = None
                    exit_reason = None
                    
                    for j in range(i+1, len(klines_1h)):
                        future_kline = klines_1h[j]
                        future_high = float(future_kline['high'])
                        future_low = float(future_kline['low'])
                        future_time = datetime.fromtimestamp(future_kline['timestamp'] / 1000)
                        
                        holding_hours = (future_time - current_time).total_seconds() / 3600
                        
                        if future_high >= stop_loss:
                            exit_price = stop_loss
                            exit_time = future_time
                            exit_reason = "止损"
                            break
                        
                        if future_low <= take_profit:
                            exit_price = take_profit
                            exit_time = future_time
                            exit_reason = "止盈"
                            break
                        
                        if holding_hours >= self.config['max_holding_hours']:
                            exit_price = float(future_kline['close'])
                            exit_time = future_time
                            exit_reason = "时间止损"
                            break
                    
                    if exit_price is None and len(klines_1h) > 0:
                        last_kline = klines_1h[-1]
                        exit_price = float(last_kline['close'])
                        exit_time = datetime.fromtimestamp(last_kline['timestamp'] / 1000)
                        exit_reason = "数据结束"
                    
                    if exit_price:
                        pnl_pct = (current_price - exit_price) / current_price
                        pnl_usd = self.config['position_size'] * pnl_pct
                        
                        trade = BacktestTrade(
                            symbol=symbol,
                            entry_time=current_time,
                            entry_price=current_price,
                            exit_time=exit_time,
                            exit_price=exit_price,
                            pnl_pct=pnl_pct,
                            pnl_usd=pnl_usd,
                            total_score=result.total_score,
                            contract_score=result.contract_score,
                            technical_score=result.technical_score,
                            sentiment_score=result.sentiment_score,
                            oi_volume_ratio=result.oi_volume_ratio,
                            funding_rate=result.funding_rate,
                            exit_reason=exit_reason,
                            holding_hours=(exit_time - current_time).total_seconds() / 3600
                        )
                        trades.append(trade)
                        
                        break
        
        print(f"\n处理了 {processed} 个币种")
        return trades

    def analyze_results(self, trades: List[BacktestTrade]) -> Dict:
        """分析回测结果"""
        if not trades:
            return {'error': '没有交易记录'}
        
        total_trades = len(trades)
        winning_trades = [t for t in trades if t.pnl_usd > 0]
        losing_trades = [t for t in trades if t.pnl_usd <= 0]
        
        total_pnl = sum(t.pnl_usd for t in trades)
        win_rate = len(winning_trades) / total_trades * 100 if total_trades > 0 else 0
        
        avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
        avg_win = sum(t.pnl_usd for t in winning_trades) / len(winning_trades) if winning_trades else 0
        avg_loss = sum(t.pnl_usd for t in losing_trades) / len(losing_trades) if losing_trades else 0
        
        oi_ratios = [t.oi_volume_ratio for t in trades]
        funding_rates = [t.funding_rate for t in trades]
        
        exit_reasons = {}
        for t in trades:
            exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1
        
        return {
            'total_trades': total_trades,
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'avg_pnl': avg_pnl,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'oi_volume_ratio': {
                'min': min(oi_ratios) if oi_ratios else 0,
                'max': max(oi_ratios) if oi_ratios else 0,
                'avg': sum(oi_ratios) / len(oi_ratios) if oi_ratios else 0
            },
            'funding_rate': {
                'min': min(funding_rates) if funding_rates else 0,
                'max': max(funding_rates) if funding_rates else 0,
                'avg': sum(funding_rates) / len(funding_rates) if funding_rates else 0
            },
            'exit_reasons': exit_reasons
        }


def main():
    print("=" * 60)
    print("V4.1 回测 - 使用 OI/上线以来总交易量 替代 OI/市值比")
    print("=" * 60)
    
    kline_file = "/Users/yl/vscode/bianace_newtrade_trade/short_selling_system/data/2025_new_coins_data.json"
    real_data_file = "/Users/yl/vscode/bianace_newtrade_trade/short_selling_system/data/real_oi_funding_data.json"
    
    backtest = BacktestV41()
    
    print("\n加载K线数据...")
    kline_data = backtest.load_kline_data(kline_file)
    print(f"加载了 {len(kline_data.get('metadata', {}).get('symbols', []))} 个币种")
    
    print("\n加载真实OI和资金费率数据...")
    real_data = backtest.load_real_data(real_data_file)
    print(f"加载了 {len(real_data)} 个币种的真实数据")
    
    print("\n运行回测...")
    trades = backtest.run_backtest(kline_data, real_data)
    
    print("\n分析结果...")
    results = backtest.analyze_results(trades)
    
    print("\n" + "=" * 60)
    print("回测结果")
    print("=" * 60)
    print(f"总交易次数: {results.get('total_trades', 0)}")
    print(f"盈利次数: {results.get('winning_trades', 0)}")
    print(f"亏损次数: {results.get('losing_trades', 0)}")
    print(f"胜率: {results.get('win_rate', 0):.1f}%")
    print(f"总盈亏: {results.get('total_pnl', 0):.2f}U")
    print(f"平均盈亏: {results.get('avg_pnl', 0):.2f}U")
    print(f"平均盈利: {results.get('avg_win', 0):.2f}U")
    print(f"平均亏损: {results.get('avg_loss', 0):.2f}U")
    
    oi_ratio_stats = results.get('oi_volume_ratio', {})
    print(f"\nOI/总交易量比率统计:")
    print(f"  最小值: {oi_ratio_stats.get('min', 0):.4f}")
    print(f"  最大值: {oi_ratio_stats.get('max', 0):.4f}")
    print(f"  平均值: {oi_ratio_stats.get('avg', 0):.4f}")
    
    funding_stats = results.get('funding_rate', {})
    print(f"\n资金费率统计:")
    print(f"  最小值: {funding_stats.get('min', 0):.6f}")
    print(f"  最大值: {funding_stats.get('max', 0):.6f}")
    print(f"  平均值: {funding_stats.get('avg', 0):.6f}")
    
    print(f"\n退出原因:")
    for reason, count in results.get('exit_reasons', {}).items():
        print(f"  {reason}: {count}次")
    
    if trades:
        print("\n" + "=" * 60)
        print("交易详情（前10笔）")
        print("=" * 60)
        for i, trade in enumerate(trades[:10]):
            print(f"\n交易 {i+1}: {trade.symbol}")
            print(f"  入场时间: {trade.entry_time}")
            print(f"  入场价格: {trade.entry_price:.6f}")
            print(f"  出场时间: {trade.exit_time}")
            print(f"  出场价格: {trade.exit_price:.6f}")
            print(f"  盈亏: {trade.pnl_usd:.2f}U ({trade.pnl_pct*100:.2f}%)")
            print(f"  总分: {trade.total_score:.2f}")
            print(f"  合约分: {trade.contract_score:.2f}")
            print(f"  技术分: {trade.technical_score:.2f}")
            print(f"  情绪分: {trade.sentiment_score:.2f}")
            print(f"  OI/总交易量: {trade.oi_volume_ratio:.4f}")
            print(f"  资金费率: {trade.funding_rate:.6f}")
            print(f"  持仓时间: {trade.holding_hours:.1f}小时")
            print(f"  退出原因: {trade.exit_reason}")
    
    output = {
        'metadata': {
            'backtest_time': datetime.now().isoformat(),
            'version': 'V4.1',
            'description': '使用OI/上线以来总交易量替代OI/市值比'
        },
        'summary': results,
        'trades': [
            {
                'symbol': t.symbol,
                'entry_time': t.entry_time.isoformat(),
                'entry_price': t.entry_price,
                'exit_time': t.exit_time.isoformat(),
                'exit_price': t.exit_price,
                'pnl_pct': t.pnl_pct,
                'pnl_usd': t.pnl_usd,
                'total_score': t.total_score,
                'contract_score': t.contract_score,
                'technical_score': t.technical_score,
                'sentiment_score': t.sentiment_score,
                'oi_volume_ratio': t.oi_volume_ratio,
                'funding_rate': t.funding_rate,
                'exit_reason': t.exit_reason,
                'holding_hours': t.holding_hours
            }
            for t in trades
        ]
    }
    
    output_file = "/Users/yl/vscode/bianace_newtrade_trade/short_selling_system/data/backtest_v41_results.json"
    with open(output_file, 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n结果已保存到: {output_file}")


if __name__ == "__main__":
    main()
