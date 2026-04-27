#!/usr/bin/env python3
"""
V4.1.2 回测脚本
分批止盈 + 移动止损方案：
- 第一目标: 1.5 ATR，平仓 30%
- 第二目标: 3.0 ATR，平仓 40%
- 剩余 30%: 移动止损，从最高浮盈回撤 1.5 ATR 平仓
- 止损: 2.0 ATR
"""

import json
import sys
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'short_selling_system'))

from short_selling_system.core.scoring_engine_v41 import ScoringEngineV41, scoring_engine_v41
from short_selling_system.core.pattern_recognition_v4 import PatternRecognitionV4


@dataclass
class BacktestTrade:
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
    atr: float
    tp1_hit: bool = False
    tp2_hit: bool = False
    trailing_exit: bool = False


class BacktestV412:
    def __init__(self):
        self.scoring_engine = ScoringEngineV41()
        self.pattern_recognition = PatternRecognitionV4()
        
        self.config = {
            'initial_capital': 100.0,
            'position_size': 4.0,
            'max_leverage': 2,
            'stop_loss_atr': 2.0,
            'tp1_atr': 1.5,
            'tp1_ratio': 0.30,
            'tp2_atr': 3.0,
            'tp2_ratio': 0.40,
            'trailing_atr': 1.5,
            'remaining_ratio': 0.30,
            'max_holding_hours': 72,
            'atr_period': 14,
            'entry_threshold': 6.5
        }
        
        print("✅ V4.1.2 回测引擎初始化完成")
        print(f"   止损: {self.config['stop_loss_atr']} ATR")
        print(f"   第一目标: {self.config['tp1_atr']} ATR, 平仓 {self.config['tp1_ratio']*100:.0f}%")
        print(f"   第二目标: {self.config['tp2_atr']} ATR, 平仓 {self.config['tp2_ratio']*100:.0f}%")
        print(f"   剩余: 移动止损 {self.config['trailing_atr']} ATR")

    def load_kline_data(self, file_path: str) -> Dict:
        with open(file_path, 'r') as f:
            return json.load(f)

    def load_real_data(self, file_path: str) -> Dict:
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
        total_volume = 0.0
        for kline in klines:
            quote_volume = kline.get('quote_volume')
            if quote_volume:
                total_volume += float(quote_volume)
            else:
                volume = float(kline.get('volume', 0))
                close = float(kline.get('close', 0))
                total_volume += volume * close
        return total_volume

    def calculate_atr(self, klines: List[Dict], period: int = 14) -> float:
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
                    tp1 = current_price - atr * self.config['tp1_atr']
                    tp2 = current_price - atr * self.config['tp2_atr']
                    
                    total_pnl_usd = 0.0
                    remaining_position = 1.0
                    tp1_hit = False
                    tp2_hit = False
                    trailing_exit = False
                    best_price = current_price
                    trailing_stop = stop_loss
                    
                    exit_time = None
                    exit_reason = None
                    
                    for j in range(i+1, len(klines_1h)):
                        future_kline = klines_1h[j]
                        future_high = float(future_kline['high'])
                        future_low = float(future_kline['low'])
                        future_close = float(future_kline['close'])
                        future_time = datetime.fromtimestamp(future_kline['timestamp'] / 1000)
                        
                        holding_hours = (future_time - current_time).total_seconds() / 3600
                        
                        if future_high >= stop_loss:
                            pnl_pct = (current_price - stop_loss) / current_price
                            total_pnl_usd += self.config['position_size'] * remaining_position * pnl_pct
                            exit_time = future_time
                            exit_reason = "止损"
                            break
                        
                        if not tp1_hit and future_low <= tp1:
                            pnl_pct = (current_price - tp1) / current_price
                            total_pnl_usd += self.config['position_size'] * self.config['tp1_ratio'] * pnl_pct
                            remaining_position -= self.config['tp1_ratio']
                            tp1_hit = True
                            best_price = min(best_price, tp1)
                            trailing_stop = best_price + atr * self.config['trailing_atr']
                        
                        if tp1_hit and not tp2_hit and future_low <= tp2:
                            pnl_pct = (current_price - tp2) / current_price
                            total_pnl_usd += self.config['position_size'] * self.config['tp2_ratio'] * pnl_pct
                            remaining_position -= self.config['tp2_ratio']
                            tp2_hit = True
                            best_price = min(best_price, tp2)
                            trailing_stop = best_price + atr * self.config['trailing_atr']
                        
                        if tp1_hit:
                            if future_low < best_price:
                                best_price = future_low
                                trailing_stop = best_price + atr * self.config['trailing_atr']
                            
                            if future_high >= trailing_stop:
                                pnl_pct = (current_price - trailing_stop) / current_price
                                total_pnl_usd += self.config['position_size'] * remaining_position * pnl_pct
                                exit_time = future_time
                                exit_reason = "移动止损"
                                trailing_exit = True
                                break
                        
                        if holding_hours >= self.config['max_holding_hours']:
                            pnl_pct = (current_price - future_close) / current_price
                            total_pnl_usd += self.config['position_size'] * remaining_position * pnl_pct
                            exit_time = future_time
                            exit_reason = "时间止损"
                            break
                    
                    if exit_time is None and len(klines_1h) > 0:
                        last_kline = klines_1h[-1]
                        last_close = float(last_kline['close'])
                        last_time = datetime.fromtimestamp(last_kline['timestamp'] / 1000)
                        pnl_pct = (current_price - last_close) / current_price
                        total_pnl_usd += self.config['position_size'] * remaining_position * pnl_pct
                        exit_time = last_time
                        exit_reason = "数据结束"
                    
                    if exit_time:
                        total_pnl_pct = total_pnl_usd / self.config['position_size']
                        
                        trade = BacktestTrade(
                            symbol=symbol,
                            entry_time=current_time,
                            entry_price=current_price,
                            exit_time=exit_time,
                            exit_price=0,
                            pnl_pct=total_pnl_pct,
                            pnl_usd=total_pnl_usd,
                            total_score=result.total_score,
                            contract_score=result.contract_score,
                            technical_score=result.technical_score,
                            sentiment_score=result.sentiment_score,
                            oi_volume_ratio=result.oi_volume_ratio,
                            funding_rate=result.funding_rate,
                            exit_reason=exit_reason,
                            holding_hours=(exit_time - current_time).total_seconds() / 3600,
                            atr=atr,
                            tp1_hit=tp1_hit,
                            tp2_hit=tp2_hit,
                            trailing_exit=trailing_exit
                        )
                        trades.append(trade)
                        break
        
        print(f"\n处理了 {processed} 个币种")
        return trades

    def analyze_results(self, trades: List[BacktestTrade]) -> Dict:
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
        
        profit_factor = abs(sum(t.pnl_usd for t in winning_trades) / sum(t.pnl_usd for t in losing_trades)) if losing_trades and sum(t.pnl_usd for t in losing_trades) != 0 else 0
        
        exit_reasons = {}
        for t in trades:
            exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1
        
        tp1_count = sum(1 for t in trades if t.tp1_hit)
        tp2_count = sum(1 for t in trades if t.tp2_hit)
        trailing_count = sum(1 for t in trades if t.trailing_exit)
        
        return {
            'version': 'V4.1.2',
            'config': {
                'stop_loss_atr': self.config['stop_loss_atr'],
                'tp1_atr': self.config['tp1_atr'],
                'tp1_ratio': self.config['tp1_ratio'],
                'tp2_atr': self.config['tp2_atr'],
                'tp2_ratio': self.config['tp2_ratio'],
                'trailing_atr': self.config['trailing_atr'],
                'remaining_ratio': self.config['remaining_ratio']
            },
            'total_trades': total_trades,
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'avg_pnl': avg_pnl,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'profit_factor': profit_factor,
            'exit_reasons': exit_reasons,
            'tp1_hit_count': tp1_count,
            'tp2_hit_count': tp2_count,
            'trailing_exit_count': trailing_count
        }


def main():
    print("=" * 60)
    print("V4.1.2 回测 - 分批止盈 + 移动止损")
    print("=" * 60)
    
    kline_file = "/Users/yl/vscode/bianace_newtrade_trade/short_selling_system/data/2025_new_coins_data.json"
    real_data_file = "/Users/yl/vscode/bianace_newtrade_trade/short_selling_system/data/real_oi_funding_data.json"
    
    backtest = BacktestV412()
    
    print("\n加载数据...")
    kline_data = backtest.load_kline_data(kline_file)
    real_data = backtest.load_real_data(real_data_file)
    
    print("\n运行回测...")
    trades = backtest.run_backtest(kline_data, real_data)
    
    results = backtest.analyze_results(trades)
    
    print("\n" + "=" * 60)
    print("V4.1.2 回测结果")
    print("=" * 60)
    print(f"止损: {results['config']['stop_loss_atr']} ATR")
    print(f"第一目标: {results['config']['tp1_atr']} ATR ({results['config']['tp1_ratio']*100:.0f}%)")
    print(f"第二目标: {results['config']['tp2_atr']} ATR ({results['config']['tp2_ratio']*100:.0f}%)")
    print(f"移动止损: {results['config']['trailing_atr']} ATR (剩余{results['config']['remaining_ratio']*100:.0f}%)")
    print("-" * 60)
    print(f"总交易次数: {results['total_trades']}")
    print(f"盈利次数: {results['winning_trades']}")
    print(f"亏损次数: {results['losing_trades']}")
    print(f"胜率: {results['win_rate']:.1f}%")
    print(f"总盈亏: {results['total_pnl']:.2f}U")
    print(f"平均盈利: {results['avg_win']:.2f}U")
    print(f"平均亏损: {results['avg_loss']:.2f}U")
    print(f"盈亏比: {abs(results['avg_win']/results['avg_loss']):.2f}" if results['avg_loss'] != 0 else "盈亏比: N/A")
    print(f"盈利因子: {results['profit_factor']:.2f}")
    print(f"\n分批止盈统计:")
    print(f"  触发TP1: {results['tp1_hit_count']}次")
    print(f"  触发TP2: {results['tp2_hit_count']}次")
    print(f"  移动止损退出: {results['trailing_exit_count']}次")
    print(f"\n退出原因:")
    for reason, count in results['exit_reasons'].items():
        print(f"  {reason}: {count}次")
    
    output_file = "/Users/yl/vscode/bianace_newtrade_trade/short_selling_system/data/backtest_v412_results.json"
    with open(output_file, 'w') as f:
        json.dump({
            'metadata': {'version': 'V4.1.2', 'backtest_time': datetime.now().isoformat()},
            'summary': results,
            'trades': [
                {
                    'symbol': t.symbol, 'entry_time': t.entry_time.isoformat(),
                    'entry_price': t.entry_price, 'exit_time': t.exit_time.isoformat(),
                    'pnl_usd': t.pnl_usd, 'exit_reason': t.exit_reason,
                    'tp1_hit': t.tp1_hit, 'tp2_hit': t.tp2_hit, 'trailing_exit': t.trailing_exit
                }
                for t in trades
            ]
        }, f, indent=2, ensure_ascii=False)
    print(f"\n结果已保存到: {output_file}")


if __name__ == "__main__":
    main()
