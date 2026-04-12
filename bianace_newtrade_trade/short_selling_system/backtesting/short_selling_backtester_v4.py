#!/usr/bin/env python3
"""
做空策略回测器 v4 - 新币做空策略 V2.0 完整版
支持多时间框架（1h 优先，30m 补充）
"""

import json
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from pathlib import Path


class ShortSellingBacktesterV4:
    """新币做空策略 V2.0 回测器"""
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        
        # 基础配置
        base_config = {
            'initial_capital': Decimal('500'),
            'backtest_days': 90,
            'leverage': 3,
            'fee_rate': Decimal('0.0008'),
            'slippage': Decimal('0.001'),
            'one_coin_one_trade': True,
            'risk_per_trade': Decimal('0.02'),
            'max_daily_trades': 2,
            'max_drawdown': Decimal('0.15'),
            'max_listing_hours': Decimal('48'),
            
            # 多时间框架配置
            'primary_timeframe': '1h',
            'secondary_timeframe': '30m',
            'use_secondary_if_missed': True,
            
            # 止损止盈
            'stop_loss_atr_multiplier': Decimal('2.0'),
            'min_stop_loss_pct': Decimal('0.04'),
            'emergency_stop_window_minutes': 15,
            'emergency_stop_pct': Decimal('0.015'),
            'take_profit_1_atr': Decimal('1.5'),
            'take_profit_1_ratio': Decimal('0.30'),
            'take_profit_2_atr': Decimal('3.0'),
            'take_profit_2_ratio': Decimal('0.40'),
            'trailing_stop_activation': Decimal('0.10'),
            'trailing_stop_atr': Decimal('1.5'),
            'time_stop_hours': 48,
            
            # 衰竭形态
            'exhaustion_pattern': {
                'upper_shadow_ratio': Decimal('2.0'),
                'volume_ratio': Decimal('1.5'),
                'triple_top_tolerance': Decimal('0.01'),
            },
        }
        
        self.config = {**base_config, **(config or {})}
        self.traded_coins = set()
        self.trades = []
        self.daily_trades = {}
        self.max_drawdown_reached = False
        self.capital = self.config['initial_capital']
        self.position = None
    
    def check_exhaustion_pattern(self, klines: List[Dict], index: int) -> bool:
        """检查衰竭形态"""
        if index < 5:
            return False
        
        current = klines[index]
        prev1 = klines[index - 1]
        prev2 = klines[index - 2]
        prev3 = klines[index - 3]
        prev4 = klines[index - 4]
        prev5 = klines[index - 5]
        
        # 1. 三次冲顶
        recent_highs = [
            Decimal(str(prev3['high'])),
            Decimal(str(prev2['high'])),
            Decimal(str(prev1['high'])),
            Decimal(str(current['high']))
        ]
        
        max_high = max(recent_highs)
        if recent_highs[2] >= recent_highs[1] and recent_highs[3] >= recent_highs[2]:
            return False
        
        tolerance = max_high * self.config['exhaustion_pattern']['triple_top_tolerance']
        if max_high - min(recent_highs) > tolerance * 2:
            return False
        
        # 2. K 线形态
        high = Decimal(str(current['high']))
        low = Decimal(str(current['low']))
        open_p = Decimal(str(current['open']))
        close = Decimal(str(current['close']))
        
        body = abs(close - open_p)
        upper_shadow = high - max(open_p, close)
        
        is_long_upper_shadow = body > 0 and upper_shadow >= body * self.config['exhaustion_pattern']['upper_shadow_ratio']
        
        prev_open = Decimal(str(prev1['open']))
        prev_close = Decimal(str(prev1['close']))
        is_bearish_engulfing = (close < open_p and 
                                prev_close > prev_open and
                                open_p > prev_close and 
                                close < prev_open)
        
        if not (is_long_upper_shadow or is_bearish_engulfing):
            return False
        
        # 3. 成交量
        current_volume = Decimal(str(current['volume']))
        avg_volume = sum([
            Decimal(str(prev1['volume'])),
            Decimal(str(prev2['volume'])),
            Decimal(str(prev3['volume'])),
            Decimal(str(prev4['volume'])),
            Decimal(str(prev5['volume']))
        ]) / 5
        
        if current_volume < avg_volume * self.config['exhaustion_pattern']['volume_ratio']:
            return False
        
        # 4. 确认收盘价
        if close >= max_high * Decimal('0.99'):
            return False
        
        return True
    
    def calculate_atr(self, klines: List[Dict], index: int, period: int = 14) -> Decimal:
        """计算 ATR"""
        if index < period:
            return Decimal('0.01')
        
        true_ranges = []
        for i in range(index - period + 1, index + 1):
            high = Decimal(str(klines[i]['high']))
            low = Decimal(str(klines[i]['low']))
            prev_close = Decimal(str(klines[i-1]['close']))
            
            tr1 = high - low
            tr2 = abs(high - prev_close)
            tr3 = abs(low - prev_close)
            tr = max(tr1, tr2, tr3)
            true_ranges.append(tr)
        
        return sum(true_ranges) / len(true_ranges)
    
    def calculate_position_size(self, entry_price: Decimal, stop_loss_price: Decimal) -> Decimal:
        """计算仓位"""
        stop_loss_pct = (stop_loss_price - entry_price) / entry_price
        stop_loss_pct = max(stop_loss_pct, self.config['min_stop_loss_pct'])
        
        risk_amount = self.capital * self.config['risk_per_trade']
        position_value = risk_amount / stop_loss_pct
        position_value = position_value * min(self.config['leverage'], 3)
        position_size = position_value / entry_price
        
        return position_size
    
    def run_backtest(self, data: Dict, start_date: datetime, end_date: datetime) -> Optional[Dict]:
        """运行回测（1h 优先，30m 补充）"""
        self.traded_coins = set()
        self.trades = []
        self.daily_trades = {}
        self.max_drawdown_reached = False
        self.capital = self.config['initial_capital']
        
        symbols = list(data.keys())
        
        primary_tf = self.config.get('primary_timeframe', '1h')
        secondary_tf = self.config.get('secondary_timeframe', '30m')
        use_secondary = self.config.get('use_secondary_if_missed', True)
        
        print(f"  准备回测 {len(symbols)} 个币种")
        print(f"  主时间框架：{primary_tf}")
        print(f"  备用时间框架：{secondary_tf} ({'启用' if use_secondary else '禁用'})")
        
        for symbol in symbols:
            if self.max_drawdown_reached:
                print(f"  ⚠️ 达到最大回撤，停止交易")
                break
            
            symbol_data = data[symbol]
            
            if symbol in self.traded_coins:
                continue
            
            signal_found = False
            timeframes_to_try = [primary_tf]
            if use_secondary:
                timeframes_to_try.append(secondary_tf)
            
            for tf in timeframes_to_try:
                klines = symbol_data.get(tf, [])
                
                if len(klines) < 20:
                    continue
                
                for i in range(20, len(klines)):
                    if self.check_exhaustion_pattern(klines, i):
                        entry_kline = klines[i]
                        entry_price = Decimal(str(entry_kline['open']))
                        entry_time = entry_kline['timestamp']
                        
                        listing_time = symbol_data.get('symbol_info', {}).get('listTime', 0)
                        if listing_time:
                            if isinstance(entry_time, str):
                                entry_datetime = datetime.fromisoformat(entry_time)
                            else:
                                entry_datetime = datetime.fromtimestamp(entry_time / 1000)
                            
                            if isinstance(listing_time, str):
                                listing_datetime = datetime.fromisoformat(listing_time)
                            else:
                                listing_datetime = datetime.fromtimestamp(listing_time / 1000)
                            
                            hours_since_listing = (entry_datetime - listing_datetime).total_seconds() / 3600
                            if hours_since_listing > self.config['max_listing_hours']:
                                continue
                        
                        primary_klines = symbol_data.get(primary_tf, klines)
                        atr = self.calculate_atr(primary_klines, i)
                        
                        stop_loss_atr = entry_price + atr * self.config['stop_loss_atr_multiplier']
                        min_stop_loss = entry_price * (Decimal('1') + self.config['min_stop_loss_pct'])
                        stop_loss_price = max(stop_loss_atr, min_stop_loss)
                        
                        position_size = self.calculate_position_size(entry_price, stop_loss_price)
                        
                        self.position = {
                            'symbol': symbol,
                            'direction': 'SHORT',
                            'entry_price': entry_price,
                            'entry_time': entry_time,
                            'size': position_size,
                            'stop_loss': stop_loss_price,
                            'take_profit_1': entry_price - atr * self.config['take_profit_1_atr'],
                            'take_profit_2': entry_price - atr * self.config['take_profit_2_atr'],
                            'atr': atr,
                            'risk_amount': self.capital * self.config['risk_per_trade'],
                            'signal_timeframe': tf
                        }
                        
                        self.traded_coins.add(symbol)
                        signal_found = True
                        
                        self.simulate_exit(klines, start_index=i+1)
                        break
                
                if signal_found:
                    break
            
            if not signal_found:
                print(f"  ⚠️ {symbol}: 未找到信号")
        
        return self.generate_report()
    
    def simulate_exit(self, klines: List[Dict], start_index: int):
        """模拟出场"""
        if not self.position:
            return
        
        position = self.position
        entry_price = position['entry_price']
        stop_loss = position['stop_loss']
        tp1 = position['take_profit_1']
        tp2 = position['take_profit_2']
        atr = position['atr']
        
        entry_time = position['entry_time']
        if isinstance(entry_time, str):
            entry_datetime = datetime.fromisoformat(entry_time)
        else:
            entry_datetime = datetime.fromtimestamp(entry_time / 1000)
        
        time_stop = entry_datetime + timedelta(hours=self.config['time_stop_hours'])
        emergency_stop_time = entry_datetime + timedelta(minutes=self.config['emergency_stop_window_minutes'])
        emergency_stop_price = entry_price * (Decimal('1') + self.config['emergency_stop_pct'])
        
        remaining_size = position['size']
        total_pnl = Decimal('0')
        total_fees = Decimal('0')
        exit_reason = None
        exit_price = None
        exit_time = None
        lowest_price = entry_price
        
        for i in range(start_index, len(klines)):
            kline = klines[i]
            high = Decimal(str(kline['high']))
            low = Decimal(str(kline['low']))
            close = Decimal(str(kline['close']))
            
            if low < lowest_price:
                lowest_price = low
            
            timestamp = kline['timestamp']
            if isinstance(timestamp, str):
                current_time = datetime.fromisoformat(timestamp)
            else:
                current_time = datetime.fromtimestamp(timestamp / 1000)
            
            if current_time >= time_stop and remaining_size > 0:
                exit_price = close
                exit_time = timestamp
                exit_reason = '时间止损'
                pnl = (entry_price - exit_price) * remaining_size
                fees = (entry_price + exit_price) * remaining_size * self.config['fee_rate']
                total_pnl += pnl - fees
                total_fees += fees
                remaining_size = Decimal('0')
                break
            
            if current_time <= emergency_stop_time and remaining_size > 0:
                if high >= emergency_stop_price:
                    exit_price = emergency_stop_price
                    exit_time = timestamp
                    exit_reason = '紧急止损'
                    pnl = (entry_price - exit_price) * remaining_size
                    fees = (entry_price + exit_price) * remaining_size * self.config['fee_rate']
                    total_pnl += pnl - fees
                    total_fees += fees
                    remaining_size = Decimal('0')
                    break
            
            if high >= stop_loss and remaining_size > 0:
                exit_price = stop_loss
                exit_time = timestamp
                exit_reason = '止损'
                pnl = (entry_price - exit_price) * remaining_size
                fees = (entry_price + exit_price) * remaining_size * self.config['fee_rate']
                total_pnl += pnl - fees
                total_fees += fees
                remaining_size = Decimal('0')
                break
            
            if low <= tp1 and remaining_size > 0:
                close_size = remaining_size * self.config['take_profit_1_ratio']
                pnl = (entry_price - tp1) * close_size
                fees = (entry_price + tp1) * close_size * self.config['fee_rate']
                total_pnl += pnl - fees
                total_fees += fees
                remaining_size -= close_size
                
                if exit_reason is None:
                    exit_reason = '第一止盈'
            
            if low <= tp2 and remaining_size > 0:
                close_size = remaining_size * self.config['take_profit_2_ratio']
                pnl = (entry_price - tp2) * close_size
                fees = (entry_price + tp2) * close_size * self.config['fee_rate']
                total_pnl += pnl - fees
                total_fees += fees
                remaining_size -= close_size
                
                if exit_reason is None:
                    exit_reason = '第二止盈'
            
            if remaining_size > 0:
                unrealized_pnl_pct = (entry_price - close) / entry_price
                
                if unrealized_pnl_pct >= self.config['trailing_stop_activation']:
                    trailing_stop = lowest_price * (Decimal('1') + atr * self.config['trailing_stop_atr'] / lowest_price)
                    
                    if high >= trailing_stop:
                        exit_price = trailing_stop
                        exit_time = timestamp
                        exit_reason = '移动止盈'
                        pnl = (entry_price - exit_price) * remaining_size
                        fees = (entry_price + exit_price) * remaining_size * self.config['fee_rate']
                        total_pnl += pnl - fees
                        total_fees += fees
                        remaining_size = Decimal('0')
                        break
        
        if remaining_size > 0 and len(klines) > start_index:
            last_kline = klines[-1]
            exit_price = Decimal(str(last_kline['close']))
            exit_time = last_kline['timestamp']
            exit_reason = exit_reason or '强制平仓'
            pnl = (entry_price - exit_price) * remaining_size
            fees = (entry_price + exit_price) * remaining_size * self.config['fee_rate']
            total_pnl += pnl - fees
            total_fees += fees
        
        self.trades.append({
            'symbol': position['symbol'],
            'direction': 'SHORT',
            'entry_price': float(entry_price),
            'exit_price': float(exit_price) if exit_price else 0,
            'entry_time': entry_time,
            'exit_time': exit_time,
            'size': float(position['size']),
            'pnl': float(total_pnl),
            'fees': float(total_fees),
            'exit_reason': exit_reason or '未知',
            'risk_amount': float(position['risk_amount']),
            'signal_timeframe': position.get('signal_timeframe', 'unknown')
        })
        
        self.capital += total_pnl
        
        if self.capital < self.config['initial_capital'] * (Decimal('1') - self.config['max_drawdown']):
            self.max_drawdown_reached = True
        
        self.position = None
    
    def generate_report(self) -> Dict:
        """生成报告"""
        if not self.trades:
            return None
        
        total_pnl = sum(t['pnl'] for t in self.trades)
        winning_trades = [t for t in self.trades if t['pnl'] > 0]
        losing_trades = [t for t in self.trades if t['pnl'] <= 0]
        
        win_rate = len(winning_trades) / len(self.trades) if self.trades else 0
        total_profit = sum(t['pnl'] for t in winning_trades)
        total_loss = abs(sum(t['pnl'] for t in losing_trades))
        
        final_capital = self.config['initial_capital'] + Decimal(str(total_pnl))
        return_pct = (final_capital - self.config['initial_capital']) / self.config['initial_capital'] * 100
        
        avg_win = total_profit / len(winning_trades) if winning_trades else Decimal('0')
        avg_loss = total_loss / len(losing_trades) if losing_trades else Decimal('0')
        profit_factor = avg_win / avg_loss if avg_loss > 0 else Decimal('0')
        
        return {
            'summary': {
                'total_trades': len(self.trades),
                'winning_trades': len(winning_trades),
                'losing_trades': len(losing_trades),
                'win_rate': win_rate,
                'total_pnl': Decimal(str(total_pnl)),
                'total_profit': Decimal(str(total_profit)),
                'total_loss': Decimal(str(total_loss)),
                'final_capital': final_capital,
                'total_return': return_pct,
                'avg_pnl': Decimal(str(total_pnl / len(self.trades))) if self.trades else Decimal('0'),
                'profit_factor': profit_factor,
                'max_drawdown_reached': self.max_drawdown_reached,
            },
            'trades': self.trades
        }
