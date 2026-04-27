#!/usr/bin/env python3
"""
真实数据回测器 v6 - 夏普比率优化版

v6 核心改进：
1. 放宽频率限制（每日 3 笔，冷却 24 小时）
2. 放宽质量过滤（ADX≥18，量比≥1.8，ATR 1.8%-5%）
3. 降低杠杆至 3 倍
4. 吊灯止损（自适应移动止盈）

目标：
- 总交易数：100-150 笔
- 夏普比率：≥0.8
- 最大回撤：<15%

使用方法：
python backtest/real_data_backtest_v6.py --symbols BTCUSDT,ETHUSDT,BNBUSDT --output v6_sharpe
"""

import json
import logging
import argparse
import csv
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional
from collections import defaultdict, deque

# 添加项目根目录到路径
import sys
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.scoring import get_scoring_engine_v612

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('backtest_v6')


class RealDataBacktesterV6:
    """真实数据回测器 v6（夏普比率优化版）"""
    
    def __init__(self, data_file: str = 'data/multi_timeframe_data.json'):
        self.data_file = Path(__file__).parent.parent / data_file
        self.data = None
        self.scoring_engine = get_scoring_engine_v612()
        
        # v6 交易参数
        self.fee_rate = 0.0003  # 综合手续费 0.03%
        self.slippage = 0.0002  # 滑点 0.02%
        
        # v6.11 评分阈值（进一步放宽）
        self.min_score_s = 85        # S 级≥85 分（不变）
        self.min_score_a = 70        # A 级 75→70（降低 5 分）
        self.min_score_b = 60        # B 级≥60 分（新增）
        self.min_score_c = 50        # C 级≥50 分（新增）
        
        # v6.11 频率控制（进一步放宽）
        self.max_trades_per_day = 5  # v6: 3 → v6.11: 5
        self.cooldown_hours = 12     # v6: 24 → v6.11: 12
        self.max_consecutive_losses = 5  # v6.11 新增：连续 5 笔亏损暂停
        
        # v6 止盈止损（吊灯止损）
        self.stop_atr = 1.5           # 初始止损 1.5x ATR
        self.chandelier_atr = 2.0     # 吊灯止损 2.0x ATR
        self.target_atr = 3.0         # 3x ATR 后启动吊灯
        
        # v6 浮动仓位管理
        self.trade_history = deque(maxlen=10)
        self.win_streak = 0
        self.loss_streak = 0
        self.position_multiplier = 1.0
        
        # v6 熔断监控
        self.equity_curve = []
        self.peak_equity = 1000.0
        self.current_drawdown = 0.0
        self.is_paused = False
        self.pause_until = None
        
        # 状态跟踪
        self.daily_trades = defaultdict(int)
        self.last_trade_time = {}
        
        logger.info("="*60)
        logger.info("回测器 v6.11 初始化完成")
        logger.info("="*60)
        logger.info(f"✅ 保留 v6 原版前置过滤器（ADX/成交量/ATR）")
        logger.info(f"✅ 新增 B 级（≥60 分，15%）和 C 级（≥50 分，5%）")
        logger.info(f"✅ S 级阈值：≥{self.min_score_s}分")
        logger.info(f"✅ A 级阈值：≥{self.min_score_a}分（75→70）")
        logger.info(f"✅ 频率限制：{self.max_trades_per_day}笔/天（v6: 3→5）")
        logger.info(f"✅ 冷却时间：{self.cooldown_hours}小时（v6: 24→12）")
        logger.info(f"✅ 连续亏损暂停：{self.max_consecutive_losses}笔")
        logger.info(f"✅ 吊灯止损：{self.chandelier_atr}x ATR")
        logger.info("="*60)
    
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
    
    def check_signal_quality(self, symbol: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """检查信号质量（v6）"""
        score_result = self.scoring_engine.score(symbol, data)
        
        if score_result['grade'] is None:
            return None
        
        if score_result['grade'] == 'S' and score_result['score'] < self.min_score_s:
            return None
        if score_result['grade'] == 'A' and score_result['score'] < self.min_score_a:
            return None
        
        return score_result
    
    def check_trade_frequency(self, symbol: str, current_time: datetime) -> bool:
        """检查交易频率（v6 放宽）"""
        # 1. 熔断检查
        if self.is_paused:
            if self.pause_until and current_time < self.pause_until:
                logger.debug(f"⛔ 熔断暂停中")
                return False
            else:
                self.is_paused = False
        
        # 2. 每日次数（v6 放宽至 3 笔）
        day_key = current_time.strftime('%Y-%m-%d')
        if self.daily_trades[day_key] >= self.max_trades_per_day:
            logger.debug(f"⛔ 当日已达交易上限")
            return False
        
        # 3. 冷却期（v6 恢复至 24 小时）
        if symbol in self.last_trade_time:
            cooldown_end = self.last_trade_time[symbol] + timedelta(hours=self.cooldown_hours)
            if current_time < cooldown_end:
                logger.debug(f"⛔ {symbol}: 冷却期内")
                return False
        
        return True
    
    def record_trade(self, symbol: str, trade_time: datetime, profit_pct: float):
        """记录交易（v6）"""
        day_key = trade_time.strftime('%Y-%m-%d')
        self.daily_trades[day_key] += 1
        self.last_trade_time[symbol] = trade_time
        
        self.trade_history.append({
            'time': trade_time,
            'symbol': symbol,
            'profit': profit_pct
        })
        
        # 更新连胜/连亏
        if profit_pct > 0:
            self.win_streak += 1
            self.loss_streak = 0
        else:
            self.loss_streak += 1
            self.win_streak = 0
        
        # v6.11 连续亏损暂停
        if self.loss_streak >= self.max_consecutive_losses:
            self.is_paused = True
            self.pause_until = current_time + timedelta(days=1)
            logger.warning(f"⛔ 连续{self.loss_streak}笔亏损，暂停 1 天")
        
        # 浮动仓位调整
        self._adjust_position_multiplier()
        self._update_equity_and_drawdown(profit_pct)
        
        logger.debug(f"✅ {symbol} 交易记录：{trade_time} (盈利={profit_pct:.2f}%)")
    
    def _adjust_position_multiplier(self):
        """浮动仓位调整"""
        if self.win_streak >= 3:
            self.position_multiplier = 0.5
            logger.info(f"⚠️ 连续{self.win_streak}笔盈利，仓位减半")
        elif self.loss_streak >= 2:
            self.position_multiplier = 0.5
            logger.info(f"⚠️ 连续{self.loss_streak}笔亏损，仓位减半")
        else:
            self.position_multiplier = 1.0
    
    def _update_equity_and_drawdown(self, profit_pct: float):
        """更新权益曲线和熔断"""
        position_size = 0.20 * self.position_multiplier
        profit_impact = profit_pct * position_size
        
        if not self.equity_curve:
            current_equity = 1000.0
        else:
            current_equity = self.equity_curve[-1]
        
        new_equity = current_equity * (1 + profit_impact / 100)
        self.equity_curve.append(new_equity)
        
        if new_equity > self.peak_equity:
            self.peak_equity = new_equity
        
        self.current_drawdown = (self.peak_equity - new_equity) / self.peak_equity
        
        if self.current_drawdown > 0.15:
            self.is_paused = True
            self.pause_until = datetime.now() + timedelta(days=1)
            logger.warning(f"⛔ 触发熔断：回撤={self.current_drawdown:.2%}")
    
    def simulate_trade_v6(self, symbol: str, entry_time: datetime, 
                         entry_price: float, direction: str) -> Dict[str, float]:
        """
        模拟交易 v6（吊灯止损）
        
        1. 初始止损：1.5x ATR
        2. 达到 3x ATR 后启动吊灯止损（最高点 - 2x ATR）
        3. 吊灯止损跟踪移动
        """
        if symbol not in self.data:
            return {'profit_pct': 0.0, 'fee': 0.0}
        
        hourly_data = self.data[symbol].get('1h', [])
        
        # 找到入场点
        entry_idx = -1
        for i, k in enumerate(hourly_data):
            k_time = datetime.fromisoformat(k['timestamp'].replace('Z', '+00:00'))
            if k_time >= entry_time:
                entry_idx = i
                break
        
        if entry_idx == -1 or entry_idx >= len(hourly_data) - 10:
            return {'profit_pct': 0.0, 'fee': 0.0}
        
        # 计算 ATR
        entry_price_float = float(entry_price)
        atr = entry_price_float * 0.03
        
        # 初始止损
        stop_distance = atr * self.stop_atr
        if direction == '多':
            stop_price = entry_price_float - stop_distance
            target_price = entry_price_float + atr * self.target_atr
        else:
            stop_price = entry_price_float + stop_distance
            target_price = entry_price_float - atr * self.target_atr
        
        # 考虑滑点
        if direction == '多':
            actual_entry = entry_price_float * (1 + self.slippage)
        else:
            actual_entry = entry_price_float * (1 - self.slippage)
        
        # 吊灯止损变量
        highest_price = actual_entry
        chandelier_stop = stop_price
        chandelier_active = False
        
        # 模拟持仓
        hold_periods = 0
        exit_price = None
        
        for i in range(entry_idx + 1, len(hourly_data)):
            k = hourly_data[i]
            high = float(k['high'])
            low = float(k['low'])
            close = float(k['close'])
            hold_periods += 1
            
            # 更新最高价（用于吊灯）
            if direction == '多':
                if high > highest_price:
                    highest_price = high
            else:
                if low < highest_price:
                    highest_price = low
            
            # 检查初始止损
            if direction == '多':
                if low <= stop_price:
                    exit_price = stop_price
                    break
            else:
                if high >= stop_price:
                    exit_price = stop_price
                    break
            
            # 检查是否达到目标，启动吊灯
            if not chandelier_active:
                if direction == '多' and high >= target_price:
                    chandelier_active = True
                    # 计算吊灯止损
                    chandelier_stop = highest_price - atr * self.chandelier_atr
                elif direction == '空' and low <= target_price:
                    chandelier_active = True
                    chandelier_stop = highest_price + atr * self.chandelier_atr
            
            # 吊灯止损跟踪
            if chandelier_active:
                if direction == '多':
                    # 更新吊灯止损（只向上移动）
                    new_chandelier = highest_price - atr * self.chandelier_atr
                    if new_chandelier > chandelier_stop:
                        chandelier_stop = new_chandelier
                    
                    # 检查吊灯止损
                    if low <= chandelier_stop:
                        exit_price = chandelier_stop
                        break
                else:
                    new_chandelier = highest_price + atr * self.chandelier_atr
                    if new_chandelier < chandelier_stop:
                        chandelier_stop = new_chandelier
                    
                    if high >= chandelier_stop:
                        exit_price = chandelier_stop
                        break
            
            # 最大持仓 72 小时
            if hold_periods >= 72:
                exit_price = close
                break
        
        # 如果未退出，使用最后价格
        if exit_price is None:
            exit_price = float(hourly_data[-1]['close'])
        
        # 计算盈亏
        if direction == '多':
            gross_profit = (exit_price - actual_entry) / actual_entry * 100
        else:
            gross_profit = (actual_entry - exit_price) / actual_entry * 100
        
        # 扣除手续费
        fee = self.fee_rate * 2 * 100
        net_profit = gross_profit - fee
        
        return {
            'profit_pct': net_profit,
            'fee': fee,
            'gross_profit': gross_profit,
            'hold_periods': hold_periods
        }
    
    def run_backtest_v6(self, symbols: List[str], output_prefix: str = 'v6_sharpe') -> List[Dict[str, Any]]:
        """执行回测 v6"""
        logger.info("=" * 60)
        logger.info("开始执行回测 v6（夏普比率优化版）")
        logger.info("=" * 60)
        
        self.load_data(symbols)
        
        results = []
        total_signals = 0
        filtered_signals = 0
        rejected_signals = 0
        
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
            
            current_idx = {'1d': 30, '4h': 30, '1h': 30}
            
            for hour_idx in range(30, len(hourly_data)):
                current_kline = hourly_data[hour_idx]
                current_time = datetime.fromisoformat(current_kline['timestamp'].replace('Z', '+00:00'))
                
                current_idx['1h'] = hour_idx
                current_idx['4h'] = hour_idx // 4
                current_idx['1d'] = hour_idx // 24
                
                indicators = self.prepare_indicators_v6(symbol, current_idx)
                
                if not indicators or len(indicators) < 3:
                    continue
                
                funding_rate = 0.0001
                
                if hour_idx >= 24:
                    price_24h_ago = float(hourly_data[hour_idx-24]['close'])
                    current_price = float(current_kline['close'])
                    price_change_24h = (current_price - price_24h_ago) / price_24h_ago
                else:
                    price_change_24h = 0.0
                
                data = {
                    'funding_rate': funding_rate,
                    'price_change_24h': price_change_24h,
                    'indicators': indicators
                }
                
                try:
                    score_result = self.check_signal_quality(symbol, data)
                    
                    if score_result is None:
                        rejected_signals += 1
                        continue
                    
                    if not self.check_trade_frequency(symbol, current_time):
                        filtered_signals += 1
                        continue
                    
                    current_price = float(current_kline['close'])
                    direction = score_result.get('direction', '多')
                    
                    # 二次确认
                    if hour_idx + 1 < len(hourly_data):
                        next_k = hourly_data[hour_idx + 1]
                        next_close = float(next_k['close'])
                        
                        if direction == '多' and next_close < current_price:
                            filtered_signals += 1
                            continue
                        elif direction == '空' and next_close > current_price:
                            filtered_signals += 1
                            continue
                    
                    trade_result = self.simulate_trade_v6(
                        symbol, current_time, current_price, direction
                    )
                    
                    if trade_result['profit_pct'] != 0.0:
                        self.record_trade(symbol, current_time, trade_result['profit_pct'])
                        
                        result = {
                            'timestamp': current_time.isoformat(),
                            'symbol': symbol,
                            'score': score_result['score'],
                            'grade': score_result['grade'],
                            'position_ratio': score_result['position_ratio'],
                            'entry_price': current_price,
                            'direction': direction,
                            'profit_pct': trade_result['profit_pct'],
                            'fee': trade_result['fee'],
                            'gross_profit': trade_result['gross_profit'],
                            'hold_periods': trade_result['hold_periods'],
                            'is_win': trade_result['profit_pct'] > 0
                        }
                        
                        results.append(result)
                        total_signals += 1
                        
                        if total_signals % 20 == 0:
                            logger.info(f"  已处理 {total_signals} 个信号...")
                
                except Exception as e:
                    logger.error(f"评分失败 {symbol} @ {current_time}: {e}")
                    continue
            
            logger.info(f"✅ {symbol} 回测完成")
        
        logger.info("=" * 60)
        logger.info(f"回测完成")
        logger.info(f"总信号数：{total_signals}")
        logger.info(f"过滤信号：{rejected_signals + filtered_signals}")
        logger.info(f"  - 质量过滤：{rejected_signals}")
        logger.info(f"  - 频率过滤：{filtered_signals}")
        logger.info("=" * 60)
        
        return results
    
    def prepare_indicators_v6(self, symbol: str, current_idx: Dict[str, int]) -> Optional[Dict[str, Any]]:
        """准备指标数据（v6）"""
        if symbol not in self.data:
            return None
        
        symbol_data = self.data[symbol]
        indicators = {}
        
        for tf in ['1d', '4h', '1h']:
            if tf not in symbol_data:
                continue
            
            klines = symbol_data[tf]
            idx = current_idx.get(tf, 0)
            
            if idx < 30:
                continue
            
            historical_klines = klines[max(0, idx-100):idx+1]
            
            closes = [float(k['close']) for k in historical_klines]
            highs = [float(k['high']) for k in historical_klines]
            lows = [float(k['low']) for k in historical_klines]
            volumes = [float(k['volume']) for k in historical_klines]
            opens = [float(k['open']) for k in historical_klines]
            
            ema21 = self._calculate_ema(closes, 21)
            ema50 = self._calculate_ema(closes, 50)
            rsi14 = self._calculate_rsi(closes, 14)
            atr14 = self._calculate_atr(highs, lows, closes, 14)
            macd_data = self._calculate_macd(closes)
            bb_data = self._calculate_bollinger(closes, 20, 2.0)
            adx = self._calculate_adx(highs, lows, closes, 14)
            
            indicators[tf] = {
                'close': closes,
                'high': highs,
                'low': lows,
                'volume': volumes,
                'open': opens,
                'ema21': ema21,
                'ema50': ema50,
                'rsi14': rsi14,
                'atr14': atr14,
                'macd': macd_data['macd'],
                'macd_signal': macd_data['signal'],
                'macd_hist': macd_data['histogram'],
                'bb_upper': bb_data['upper'],
                'bb_middle': bb_data['middle'],
                'bb_lower': bb_data['lower'],
                'adx': adx
            }
        
        return indicators
    
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
    
    def _calculate_macd(self, prices: List[float]) -> Dict[str, List[float]]:
        """计算 MACD"""
        if len(prices) < 26:
            return {'macd': [0]*len(prices), 'signal': [0]*len(prices), 'histogram': [0]*len(prices)}
        
        ema12 = self._calculate_ema(prices, 12)
        ema26 = self._calculate_ema(prices, 26)
        
        macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        
        signal = self._calculate_ema(macd_line[9:], 9)
        signal = [0]*9 + signal
        
        histogram = [m - s for m, s in zip(macd_line, signal)]
        
        return {'macd': macd_line, 'signal': signal, 'histogram': histogram}
    
    def _calculate_atr(self, highs: List[float], lows: List[float], 
                       closes: List[float], period: int = 14) -> List[float]:
        """计算 ATR"""
        if len(highs) < period + 1:
            return [0.0] * len(highs)
        
        tr = []
        for i in range(1, len(highs)):
            tr1 = highs[i] - lows[i]
            tr2 = abs(highs[i] - closes[i-1])
            tr3 = abs(lows[i] - closes[i-1])
            tr.append(max(tr1, tr2, tr3))
        
        atr = []
        atr.append(sum(tr[:period]) / period)
        
        for i in range(period, len(tr)):
            atr.append((atr[-1] * (period - 1) + tr[i]) / period)
        
        atr = [0.0] + atr
        return atr
    
    def _calculate_bollinger(self, prices: List[float], period: int = 20, 
                             std_dev: float = 2.0) -> Dict[str, List[float]]:
        """计算布林带"""
        if len(prices) < period:
            return {'upper': prices[:], 'middle': prices[:], 'lower': prices[:]}
        
        middle = []
        upper = []
        lower = []
        
        for i in range(len(prices)):
            if i < period - 1:
                middle.append(prices[i])
                upper.append(prices[i])
                lower.append(prices[i])
            else:
                window = prices[i-period+1:i+1]
                sma = sum(window) / period
                variance = sum((x - sma) ** 2 for x in window) / period
                std = math.sqrt(variance)
                
                middle.append(sma)
                upper.append(sma + std_dev * std)
                lower.append(sma - std_dev * std)
        
        return {'upper': upper, 'middle': middle, 'lower': lower}
    
    def _calculate_adx(self, highs: List[float], lows: List[float], 
                       closes: List[float], period: int = 14) -> List[float]:
        """计算 ADX"""
        if len(highs) < period + 1:
            return [0.0] * len(highs)
        
        plus_dm = []
        minus_dm = []
        tr = []
        
        for i in range(1, len(highs)):
            up_move = highs[i] - highs[i-1]
            down_move = lows[i-1] - lows[i]
            
            if up_move > down_move and up_move > 0:
                plus_dm.append(up_move)
            else:
                plus_dm.append(0)
            
            if down_move > up_move and down_move > 0:
                minus_dm.append(down_move)
            else:
                minus_dm.append(0)
            
            tr1 = highs[i] - lows[i]
            tr2 = abs(highs[i] - closes[i-1])
            tr3 = abs(lows[i] - closes[i-1])
            tr.append(max(tr1, tr2, tr3))
        
        plus_di = []
        minus_di = []
        
        for i in range(len(plus_dm)):
            if i < period:
                plus_di.append(0)
                minus_di.append(0)
            else:
                plus_sum = sum(plus_dm[i-period+1:i+1])
                minus_sum = sum(minus_dm[i-period+1:i+1])
                tr_sum = sum(tr[i-period+1:i+1])
                
                plus_di.append(plus_sum / tr_sum * 100 if tr_sum > 0 else 0)
                minus_di.append(minus_sum / tr_sum * 100 if tr_sum > 0 else 0)
        
        adx = []
        for i in range(len(plus_di)):
            if plus_di[i] + minus_di[i] == 0:
                adx.append(0)
            else:
                dx = abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i]) * 100
                if i < period:
                    adx.append(0)
                else:
                    adx_sum = sum(adx[max(0, i-period+1):i])
                    adx.append((adx_sum + dx) / period if i >= period else dx)
        
        adx = [0.0] + adx
        return adx


def main():
    parser = argparse.ArgumentParser(description='v6 夏普比率优化回测器')
    parser.add_argument('--symbols', type=str, default='BTCUSDT,ETHUSDT,BNBUSDT',
                        help='交易对列表')
    parser.add_argument('--output', type=str, default='v6_sharpe',
                        help='输出文件名')
    args = parser.parse_args()
    
    symbols = [s.strip() for s in args.symbols.split(',')]
    
    backtester = RealDataBacktesterV6()
    results = backtester.run_backtest_v6(symbols, args.output)
    
    # 输出统计
    if results:
        total_trades = len(results)
        wins = sum(1 for r in results if r['is_win'])
        losses = total_trades - wins
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0
        
        total_profit = sum(r['profit_pct'] for r in results)
        total_fee = sum(r['fee'] for r in results)
        net_profit = total_profit
        
        avg_profit = total_profit / total_trades if total_trades > 0 else 0
        
        if len(results) > 1:
            profits = [r['profit_pct'] for r in results]
            avg = sum(profits) / len(profits)
            variance = sum((p - avg) ** 2 for p in profits) / len(profits)
            std = math.sqrt(variance)
            sharpe = avg / std if std > 0 else 0
        else:
            sharpe = 0
        
        logger.info("\n" + "=" * 60)
        logger.info("回测统计（v6 夏普比率优化版）")
        logger.info("=" * 60)
        logger.info(f"总交易数：{total_trades}")
        
        if results:
            start = datetime.fromisoformat(results[0]['timestamp'])
            end = datetime.fromisoformat(results[-1]['timestamp'])
            days = (end - start).days + 1
            daily_avg = total_trades / days if days > 0 else 0
            logger.info(f"日均交易数：{daily_avg:.2f}笔/天")
        
        logger.info(f"盈利次数：{wins}")
        logger.info(f"亏损次数：{losses}")
        logger.info(f"胜率：{win_rate:.1f}%")
        logger.info(f"总盈利：{total_profit:.2f}%")
        logger.info(f"总手续费：{total_fee:.2f}%")
        logger.info(f"净利润：{net_profit:.2f}%")
        logger.info(f"平均盈利：{avg_profit:.3f}%")
        logger.info(f"夏普比率：{sharpe:.2f}")
        logger.info("=" * 60)
        logger.info("回测完成！")
    
    # 导出 CSV
    if results:
        output_file = Path(__file__).parent.parent / 'backtest' / f'{args.output}.csv'
        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)
        logger.info(f"✅ CSV 报告已导出：{output_file}")


if __name__ == '__main__':
    main()
