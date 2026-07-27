"""
方案四回测脚本
ADX: 总是返回true（不过滤）
成交量放大: 总是返回true（不过滤）
ATR%: 0.3%-10.0%
"""
import pandas as pd
import numpy as np
from datetime import datetime
from decimal import Decimal
from typing import Dict, List
import structlog
import os
import yaml

from shared.indicators import TechnicalIndicators

logger = structlog.get_logger()


class Position:
    def __init__(self):
        self.entry_time = None
        self.entry_price = Decimal('0')
        self.direction = None
        self.quantity = Decimal('0')
        self.position_size = Decimal('0')
        self.leverage = 1
        self.grade = 'C'
        self.atr = Decimal('0')
        self.tp1_price = Decimal('0')
        self.tp2_price = Decimal('0')
        self.stop_loss = Decimal('0')
        self.highest_price = Decimal('0')
        self.tp1_hit = False
        self.tp2_hit = False


class BacktestEngine:
    def __init__(self, config: Dict):
        self.config = config
        self.initial_capital = Decimal(str(config['strategy']['risk']['frequency_control']['initial_capital_usdt']))
        self.current_capital = self.initial_capital
        self.highest_capital = self.initial_capital
        self.positions: List[Position] = []
        self.trades: List[Dict] = []
        self.scoring_config = config['strategy']['scoring']
        self.binance_config = config['binance']
        self.tp1_atr_multiplier = 2.5
        self.tp2_atr_multiplier = 4.0
        self.trailing_stop_atr = 1.2
    
    def load_klines_from_csv(self, interval: str) -> pd.DataFrame:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        filename = os.path.join(script_dir, f"../data/btcusdt_{interval}.csv")
        df = pd.read_csv(filename)
        df['open_time'] = pd.to_datetime(df['open_time'])
        df.set_index('open_time', inplace=True)
        df.rename(columns={'open_price': 'open', 'high_price': 'high', 'low_price': 'low', 'close_price': 'close'}, inplace=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df
    
    def run_backtest(self, klines_1h, klines_4h, klines_1d) -> Dict:
        indicators_1h = pd.DataFrame(TechnicalIndicators.calculate_all(klines_1h))
        indicators_4h = pd.DataFrame(TechnicalIndicators.calculate_all(klines_4h))
        indicators_1d = pd.DataFrame(TechnicalIndicators.calculate_all(klines_1d))
        indicators_1h['volume'] = klines_1h['volume'].values
        
        for i in range(100, len(klines_1h)):
            current_time = klines_1h.index[i]
            current_price = Decimal(str(klines_1h['close'].iloc[i]))
            current_high = Decimal(str(klines_1h['high'].iloc[i]))
            current_low = Decimal(str(klines_1h['low'].iloc[i]))
            
            for position in self.positions[:]:
                self._check_and_close_position(position, current_time, current_price, current_high, current_low)
            
            self._check_and_open_position(current_time, current_price, indicators_1h.iloc[:i+1], indicators_4h.iloc[:i+1], indicators_1d.iloc[:i+1])
        
        for position in self.positions[:]:
            self._force_close_position(position, klines_1h.index[-1], klines_1h['close'].iloc[-1])
        
        return {
            'initial_capital': float(self.initial_capital),
            'final_capital': float(self.current_capital),
            'total_return': float((self.current_capital - self.initial_capital) / self.initial_capital * 100),
            'total_trades': len(self.trades),
            'win_trades': sum(1 for t in self.trades if t['pnl'] > 0),
            'loss_trades': sum(1 for t in self.trades if t['pnl'] <= 0),
            'trades': self.trades
        }
    
    def _check_and_open_position(self, current_time, current_price, indicators_1h, indicators_4h, indicators_1d):
        # 方案四过滤条件
        # ADX: 不过滤
        # 成交量放大: 不过滤
        
        # ATR% 0.3%-10.0%（几乎不过滤）
        atr = Decimal(str(indicators_1h['ATR'].iloc[-1]))
        atr_percent = float(atr / current_price * 100)
        if atr_percent < 0.3 or atr_percent > 10.0:
            return
        
        score = self._calculate_score(indicators_1h, indicators_4h, indicators_1d)
        
        if score >= self.scoring_config['grade_thresholds']['S']:
            grade = 'S'
        elif score >= self.scoring_config['grade_thresholds']['A']:
            grade = 'A'
        elif score >= self.scoring_config['grade_thresholds']['B']:
            grade = 'B'
        elif score >= self.scoring_config['grade_thresholds']['C']:
            grade = 'C'
        else:
            return
        
        direction = self._determine_direction(indicators_1h, indicators_4h)
        position_ratio = Decimal(str(self.binance_config['position_ratio'][grade]))
        leverage = self.binance_config['leverage'][grade]
        position_size = self.current_capital * position_ratio
        quantity = position_size / current_price
        
        position = Position()
        position.entry_time = current_time
        position.entry_price = current_price
        position.direction = direction
        position.quantity = quantity
        position.position_size = position_size
        position.leverage = leverage
        position.grade = grade
        position.atr = atr
        position.highest_price = current_price
        
        if direction == 'LONG':
            position.tp1_price = current_price + atr * Decimal(str(self.tp1_atr_multiplier))
            position.tp2_price = current_price + atr * Decimal(str(self.tp2_atr_multiplier))
            position.stop_loss = current_price - atr * Decimal('2.5')
        else:
            position.tp1_price = current_price - atr * Decimal(str(self.tp1_atr_multiplier))
            position.tp2_price = current_price - atr * Decimal(str(self.tp2_atr_multiplier))
            position.stop_loss = current_price + atr * Decimal('2.5')
        
        self.positions.append(position)
    
    def _check_and_close_position(self, position, current_time, current_price, current_high, current_low):
        if not position:
            return
        
        close_reason = None
        close_price = None
        
        if position.direction == 'LONG':
            if not position.tp1_hit and current_high >= position.tp1_price:
                position.tp1_hit = True
                close_quantity = position.quantity * Decimal('0.25')
                pnl = (position.tp1_price - position.entry_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity
            
            if not position.tp2_hit and current_high >= position.tp2_price:
                position.tp2_hit = True
                close_quantity = position.quantity * Decimal('0.5')
                pnl = (position.tp2_price - position.entry_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity
            
            position.highest_price = max(position.highest_price, current_high)
            trailing_stop = position.highest_price - position.atr * Decimal('1.2')
            if current_low <= trailing_stop:
                close_reason = "吊灯止损"
                close_price = trailing_stop
        else:
            if not position.tp1_hit and current_low <= position.tp1_price:
                position.tp1_hit = True
                close_quantity = position.quantity * Decimal('0.25')
                pnl = (position.entry_price - position.tp1_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity
            
            if not position.tp2_hit and current_low <= position.tp2_price:
                position.tp2_hit = True
                close_quantity = position.quantity * Decimal('0.5')
                pnl = (position.entry_price - position.tp2_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity
            
            position.highest_price = min(position.highest_price, current_low)
            trailing_stop = position.highest_price + position.atr * Decimal('1.2')
            if current_high >= trailing_stop:
                close_reason = "吊灯止损"
                close_price = trailing_stop
        
        holding_hours = (current_time - position.entry_time).total_seconds() / 3600
        if holding_hours >= 72 and not position.tp1_hit:
            close_reason = "时间止损"
            close_price = current_price
        
        if close_reason:
            self._close_position(position, current_time, close_price, close_reason)
    
    def _close_position(self, position, current_time, close_price, reason):
        if not position:
            return
        if position.direction == 'LONG':
            pnl = (close_price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - close_price) * position.quantity
        self.current_capital += pnl
        self.trades.append({
            'entry_time': position.entry_time,
            'entry_price': float(position.entry_price),
            'exit_time': current_time,
            'exit_price': float(close_price),
            'direction': position.direction,
            'grade': position.grade,
            'position_size': float(position.position_size),
            'leverage': position.leverage,
            'pnl': float(pnl),
            'pnl_percent': float(pnl / position.position_size * 100),
            'close_reason': reason
        })
        if position in self.positions:
            self.positions.remove(position)
    
    def _force_close_position(self, position, current_time, close_price):
        if position:
            self._close_position(position, current_time, Decimal(str(close_price)), "回测结束")
    
    def _calculate_score(self, indicators_1h, indicators_4h, indicators_1d):
        score = 0.0
        ma21 = indicators_1h['MA21'].iloc[-1]
        ma55 = indicators_1h['MA55'].iloc[-1]
        if pd.notna(ma21) and pd.notna(ma55) and ma21 > ma55:
            score += 40
        macd = indicators_1h['MACD'].iloc[-1]
        if pd.notna(macd) and macd > 0:
            score += 35
        rsi = indicators_1h['RSI'].iloc[-1]
        if pd.notna(rsi) and 30 < rsi < 70:
            score += 25
        return score
    
    def _determine_direction(self, indicators_1h, indicators_4h):
        long_votes = 0
        short_votes = 0
        ma21 = indicators_1h['MA21'].iloc[-1]
        ma55 = indicators_1h['MA55'].iloc[-1]
        if pd.notna(ma21) and pd.notna(ma55):
            if ma21 > ma55:
                long_votes += 1
            else:
                short_votes += 1
        ma21 = indicators_4h['MA21'].iloc[-1]
        ma55 = indicators_4h['MA55'].iloc[-1]
        if pd.notna(ma21) and pd.notna(ma55):
            if ma21 > ma55:
                long_votes += 1
            else:
                short_votes += 1
        return 'LONG' if long_votes > short_votes else 'SHORT'


def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
    config_path = os.path.join(project_root, 'strategies/btc_eth/config.yaml')
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    engine = BacktestEngine(config)
    klines_1h = engine.load_klines_from_csv('1h')
    klines_4h = engine.load_klines_from_csv('4h')
    klines_1d = engine.load_klines_from_csv('1d')
    
    results = engine.run_backtest(klines_1h, klines_4h, klines_1d)
    
    win_rate = results['win_trades'] / results['total_trades'] * 100 if results['total_trades'] > 0 else 0
    
    print(f"""
# 方案四回测结果

## 过滤参数
- ADX趋势强度: 不过滤
- 成交量放大: 不过滤
- ATR%范围: 0.3%-10.0%

## 回测结果
- 初始资金: {results['initial_capital']:.2f} USDT
- 最终资金: {results['final_capital']:.2f} USDT
- 总收益率: {results['total_return']:.2f}%
- 总交易次数: {results['total_trades']}
- 盈利次数: {results['win_trades']}
- 亏损次数: {results['loss_trades']}
- 胜率: {win_rate:.2f}%
""")


if __name__ == "__main__":
    main()
