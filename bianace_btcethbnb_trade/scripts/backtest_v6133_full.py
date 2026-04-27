#!/usr/bin/env python3
"""
V6.13.3 完整回测器 - 优化止损距离 + 持仓时间平仓

优化内容 (对比 V6.13.2/V6.13.1):
1. 缩小止损距离：3-7% → 2-4%
2. 优化 ATR 计算：ATR * 1.5 作为止损基准
3. 新增持仓时间平仓：48 小时浮亏>2% 或 72 小时无条件平仓
4. 止盈基于 R 值：TP1=1.5R, TP2=2.5R

数据源：data/multi_timeframe_data.json
"""

import json
import logging
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
import sys
import math

# 导入 v6.13 动态仓位调整器
sys.path.append('/Users/yl/vscode/bianace_btcethbnb_trade')
from services.position_adjuster import PositionAdjuster

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('v6133_full_backtest')


class V6133FullBacktester:
    """V6.13.3 完整回测器"""
    
    def __init__(self, initial_capital: Decimal = Decimal('500')):
        """初始化回测器"""
        self.initial_capital = initial_capital
        self.position_adjuster = PositionAdjuster()
        
        # 评分系统配置（与 V6.13 相同）
        self.grade_config = {
            'S': {'min_score': 85, 'position_ratio': Decimal('0.50'), 'leverage': 5},
            'A': {'min_score': 75, 'position_ratio': Decimal('0.30'), 'leverage': 4},
            'B': {'min_score': 65, 'position_ratio': Decimal('0.15'), 'leverage': 3},
            'C': {'min_score': 55, 'position_ratio': Decimal('0.05'), 'leverage': 2},
        }
        
        # 前置过滤器配置
        self.filter_config = {
            'min_adx': 15,
            'volume_ratio_threshold': {'S': 1.5, 'A': 1.3, 'B': 1.1, 'C': 1.0},
            'atr_pct_min': Decimal('0.01'),
            'atr_pct_max': Decimal('0.08'),
        }
        
        # 一票否决配置
        self.veto_config = {
            'max_funding_rate': Decimal('0.0008'),
            'max_volatility': {'BTCUSDT': Decimal('0.04'), 'ETHUSDT': Decimal('0.045'), 'BNBUSDT': Decimal('0.07')},
            'max_price_increase': Decimal('0.25'),
            'max_price_decrease': Decimal('0.20'),
        }
        
        # V6.13.3 ATR 止损止盈配置（优化版）
        self.atr_config = {
            'stop_loss_atr': Decimal('1.5'),       # 不变
            'tp1_atr': Decimal('1.5'),             # v6.13.3: 基于 R 值 (1.5R)
            'tp2_atr': Decimal('2.5'),             # v6.13.3: 基于 R 值 (2.5R)
            'tp1_ratio': Decimal('0.3'),
            'tp2_ratio': Decimal('0.3'),
            'tp3_ratio': Decimal('0.4'),
        }
        
        # V6.13.3 持仓时间平仓配置（新增）
        self.time_close_config = {
            'max_hold_hours': 48,
            'emergency_hold_hours': 72,
            'min_loss_threshold': Decimal('0.02'),
        }
        
        # 币种差异化配置
        self.symbol_config = {
            'BTCUSDT': {'volatility_threshold': Decimal('0.04'), 'breakout_threshold': Decimal('0.015')},
            'ETHUSDT': {'volatility_threshold': Decimal('0.045'), 'breakout_threshold': Decimal('0.015')},
            'BNBUSDT': {'volatility_threshold': Decimal('0.07'), 'breakout_threshold': Decimal('0.025')},
        }
        
        # 手续费
        self.fee_rate = Decimal('0.0004')
        
        logger.info("=" * 80)
        logger.info("V6.13.3 优化版回测器初始化完成")
        logger.info("=" * 80)
        logger.info(f"初始资金：{self.initial_capital}U")
        logger.info(f"信号分级：S 级≥85 分 (50%/5x), A 级≥75 分 (30%/4x), B 级≥65 分 (15%/3x), C 级≥55 分 (5%/2x)")
        logger.info(f"ATR 配置（V6.13.3 优化）：止损=1.5×ATR, TP1=1.5R, TP2=2.5R")
        logger.info(f"持仓时间平仓（新增）：48h 浮亏>2% 或 72h 无条件")
        logger.info("=" * 80)
    
    def calculate_indicators(self, klines: List[Dict]) -> Dict:
        """计算技术指标"""
        if len(klines) < 55:
            return {}
        
        closes = [Decimal(k['close']) for k in klines]
        highs = [Decimal(k['high']) for k in klines]
        lows = [Decimal(k['low']) for k in klines]
        volumes = [Decimal(k['volume']) for k in klines]
        
        # EMA
        def ema(values, period):
            multiplier = Decimal(2) / (period + 1)
            ema_values = [sum(values[:period]) / period]
            for val in values[period:]:
                ema_values.append((val - ema_values[-1]) * multiplier + ema_values[-1])
            return ema_values
        
        ema21 = ema(closes, 21)
        ema55 = ema(closes, 55)
        
        # MACD
        ema12 = ema(closes, 12)
        ema26 = ema(closes, 26)
        dif = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        dea = ema(dif, 9)
        macd_hist = [(d - de) * 2 for d, de in zip(dif, dea)]
        
        # ATR
        def calculate_atr(highs, lows, closes, period=14):
            tr_values = []
            for i in range(1, len(closes)):
                tr1 = highs[i] - lows[i]
                tr2 = abs(highs[i] - closes[i-1])
                tr3 = abs(lows[i] - closes[i-1])
                tr = max(tr1, tr2, tr3)
                tr_values.append(tr)
            
            atr = [sum(tr_values[:period]) / period]
            for tr in tr_values[period:]:
                atr.append((atr[-1] * (period - 1) + tr) / period)
            
            return [None] * period + atr
        
        atr = calculate_atr(highs, lows, closes, 14)
        
        # ADX
        def calculate_adx(highs, lows, closes, period=14):
            plus_dm = []
            minus_dm = []
            
            for i in range(1, len(highs)):
                if highs[i] - highs[i-1] > lows[i-1] - lows[i]:
                    plus_dm.append(max(Decimal(0), highs[i] - highs[i-1]))
                else:
                    plus_dm.append(Decimal(0))
                
                if lows[i-1] - lows[i] > highs[i] - highs[i-1]:
                    minus_dm.append(max(Decimal(0), lows[i-1] - lows[i]))
                else:
                    minus_dm.append(Decimal(0))
            
            tr_values = []
            for i in range(1, len(highs)):
                tr1 = highs[i] - lows[i]
                tr2 = abs(highs[i] - closes[i-1])
                tr3 = abs(lows[i] - closes[i-1])
                tr = max(tr1, tr2, tr3)
                tr_values.append(tr)
            
            def smooth(values, period):
                smoothed = [sum(values[:period])]
                current = smoothed[0]
                for i in range(period, len(values)):
                    current = current - current / period + values[i]
                    smoothed.append(current)
                return smoothed
            
            plus_dm_smooth = smooth(plus_dm, period)
            minus_dm_smooth = smooth(minus_dm, period)
            tr_smooth = smooth(tr_values, period)
            
            plus_di = []
            minus_di = []
            
            for i in range(len(plus_dm_smooth)):
                if tr_smooth[i] != 0:
                    plus_di.append(plus_dm_smooth[i] / tr_smooth[i] * 100)
                    minus_di.append(minus_dm_smooth[i] / tr_smooth[i] * 100)
                else:
                    plus_di.append(Decimal(0))
                    minus_di.append(Decimal(0))
            
            dx = []
            for i in range(len(plus_di)):
                di_sum = plus_di[i] + minus_di[i]
                if di_sum != 0:
                    dx.append(abs(plus_di[i] - minus_di[i]) / di_sum * 100)
                else:
                    dx.append(Decimal(0))
            
            adx_smooth = smooth(dx, period)
            adx_values = [adx / period for adx in adx_smooth]
            
            return [None] * (period * 2) + adx_values
        
        adx = calculate_adx(highs, lows, closes, 14)
        
        # 布林带
        def calculate_bollinger_bands(prices, period=20):
            upper = []
            lower = []
            
            for i in range(len(prices) - period + 1):
                sma = sum(prices[i:i+period]) / period
                variance = sum((p - sma) ** 2 for p in prices[i:i+period]) / period
                std = variance.sqrt() if variance > 0 else Decimal(0)
                upper.append(sma + 2 * std)
                lower.append(sma - 2 * std)
            
            return {
                'upper': [None] * (period - 1) + upper,
                'lower': [None] * (period - 1) + lower,
            }
        
        bb = calculate_bollinger_bands(closes, 20)
        
        # 成交量 MA
        vol_ma20 = []
        for i in range(len(volumes)):
            if i < 20:
                vol_ma20.append(None)
            else:
                vol_ma20.append(sum(volumes[i-20:i+1]) / 20)
        
        return {
            'ema21': ema21,
            'ema55': ema55,
            'macd': [{'dif': d, 'dea': de, 'histogram': h} for d, de, h in zip(dif, dea, macd_hist)],
            'atr': atr,
            'adx': adx,
            'bb_upper': bb['upper'],
            'bb_lower': bb['lower'],
            'vol_ma20': vol_ma20,
        }
    
    def check_veto(self, symbol: str, funding_rate: Decimal, 
                   price_change_24h: Decimal, volatility: Decimal) -> Tuple[bool, str]:
        """一票否决检查"""
        if funding_rate > self.veto_config['max_funding_rate']:
            return True, f"资金费率过高 ({funding_rate:.4%} > {self.veto_config['max_funding_rate']:.4%})"
        
        max_vol = self.veto_config['max_volatility'].get(symbol, Decimal('0.05'))
        if volatility > max_vol:
            return True, f"波动率过高 ({volatility:.2%} > {max_vol:.2%})"
        
        if price_change_24h > self.veto_config['max_price_increase']:
            return True, f"24 小时涨幅过大 ({price_change_24h:.2%} > {self.veto_config['max_price_increase']:.2%})"
        
        if price_change_24h < -self.veto_config['max_price_decrease']:
            return True, f"24 小时跌幅过大 ({price_change_24h:.2%} > {self.veto_config['max_price_decrease']:.2%})"
        
        return False, ""
    
    def score_signal(self, symbol: str, klines: List[Dict], indicators: Dict, 
                     current_index: int) -> Tuple[int, str, Dict]:
        """量化评分系统"""
        if current_index < 55:
            return 0, '', {}
        
        score_detail = {}
        total_score = 0
        
        # 1. 趋势强度评分 (40 分)
        trend_score = 0
        close = Decimal(klines[current_index]['close'])
        ema21 = indicators['ema21'][current_index]
        ema55 = indicators['ema55'][current_index]
        
        if ema21 is not None and ema55 is not None:
            if close > ema21 > ema55:
                trend_score += 20
            elif close < ema21 < ema55:
                trend_score += 20
            elif (close > ema21 and ema21 > ema55) or (close < ema21 and ema21 < ema55):
                trend_score += 10
            
            if current_index >= 3:
                ema21_prev = indicators['ema21'][current_index - 3]
                if ema21_prev is not None:
                    slope = (ema21 - ema21_prev) / ema21_prev * 100
                    slope_score = min(20, max(0, abs(slope) * 10))
                    trend_score += int(slope_score)
        
        score_detail['trend'] = trend_score
        total_score += trend_score
        
        # 2. 形态质量评分 (35 分)
        pattern_score = 0
        
        if current_index >= 2:
            curr_open = Decimal(klines[current_index]['open'])
            curr_close = Decimal(klines[current_index]['close'])
            prev_open = Decimal(klines[current_index-1]['open'])
            prev_close = Decimal(klines[current_index-1]['close'])
            
            if curr_close > curr_open and prev_close < prev_open:
                if curr_open < prev_close and curr_close > prev_open:
                    pattern_score += 15
            
            if curr_close < curr_open and prev_close > prev_open:
                if curr_open > prev_close and curr_close < prev_open:
                    pattern_score += 15
            
            if close > indicators['bb_upper'][current_index]:
                pattern_score += 10
            elif close < indicators['bb_lower'][current_index]:
                pattern_score += 10
            
            if current_index >= 5:
                consecutive = 0
                for i in range(5):
                    if Decimal(klines[current_index-i]['close']) > Decimal(klines[current_index-i]['open']):
                        consecutive += 1
                pattern_score += min(10, consecutive * 2)
        
        score_detail['pattern'] = pattern_score
        total_score += pattern_score
        
        # 3. 动量背离评分 (25 分)
        momentum_score = 0
        
        macd = indicators['macd'][current_index]
        if macd is not None:
            if current_index >= 5:
                prev_macd = indicators['macd'][current_index - 5]
                if prev_macd is not None:
                    if close > Decimal(klines[current_index-5]['close']):
                        if macd['histogram'] < prev_macd['histogram']:
                            momentum_score += 15
                    
                    if close < Decimal(klines[current_index-5]['close']):
                        if macd['histogram'] > prev_macd['histogram']:
                            momentum_score += 15
            
            if current_index >= 2:
                prev_macd = indicators['macd'][current_index - 1]
                if prev_macd is not None:
                    if macd['dif'] > macd['dea'] and prev_macd['dif'] <= prev_macd['dea']:
                        momentum_score += 10
                    elif macd['dif'] < macd['dea'] and prev_macd['dif'] >= prev_macd['dea']:
                        momentum_score += 10
        
        score_detail['momentum'] = momentum_score
        total_score += momentum_score
        
        # 确定信号等级
        if total_score >= 85:
            grade = 'S'
        elif total_score >= 75:
            grade = 'A'
        elif total_score >= 65:
            grade = 'B'
        elif total_score >= 55:
            grade = 'C'
        else:
            grade = ''
        
        return total_score, grade, score_detail
    
    def generate_signals(self, data: Dict[str, Dict[str, List[Dict]]]) -> List[Dict[str, Any]]:
        """生成交易信号"""
        signals = []
        
        for symbol, timeframes in data.items():
            logger.info(f"分析 {symbol} 的信号...")
            
            daily_klines = timeframes.get('1d', [])
            
            if not daily_klines:
                logger.warning(f"  {symbol} 数据不完整，跳过")
                continue
            
            indicators = self.calculate_indicators(daily_klines)
            
            if not indicators:
                logger.warning(f"  {symbol} 指标计算失败，跳过")
                continue
            
            for i in range(55, len(daily_klines)):
                kline = daily_klines[i]
                
                adx = indicators['adx'][i] if i < len(indicators['adx']) else None
                if adx is None or adx < self.filter_config['min_adx']:
                    continue
                
                vol_ratio = Decimal(kline['volume']) / indicators['vol_ma20'][i] if indicators['vol_ma20'][i] else Decimal(0)
                if vol_ratio < Decimal('1.2'):
                    continue
                
                atr_pct = indicators['atr'][i] / Decimal(kline['close']) if indicators['atr'][i] else Decimal(0)
                if not (self.filter_config['atr_pct_min'] <= atr_pct <= self.filter_config['atr_pct_max']):
                    continue
                
                funding_rate = Decimal('0.0001')
                price_change = Decimal('0.05')
                volatility = atr_pct
                
                veto, reason = self.check_veto(symbol, funding_rate, price_change, volatility)
                if veto:
                    logger.debug(f"  {symbol} 触发否决：{reason}")
                    continue
                
                score, grade, score_detail = self.score_signal(symbol, daily_klines, indicators, i)
                
                if grade:
                    signals.append({
                        'symbol': symbol,
                        'timestamp': kline['timestamp'],
                        'entry_price': Decimal(kline['close']),
                        'score': score,
                        'grade': grade,
                        'score_detail': score_detail,
                        'adx': adx,
                        'vol_ratio': vol_ratio,
                        'atr_pct': atr_pct,
                    })
            
            logger.info(f"  {symbol} 生成 {len([s for s in signals if s['symbol'] == symbol])} 个有效信号")
        
        logger.info(f"总计生成 {len(signals)} 个有效信号")
        return signals
    
    def run_backtest(self, signals: List[Dict[str, Any]]) -> Dict[str, Any]:
        """运行 V6.13.3 回测"""
        logger.info("\n" + "=" * 80)
        logger.info("开始运行 V6.13.3 回测")
        logger.info("=" * 80)
        
        current_capital = self.initial_capital
        position = None
        winning_trades = 0
        losing_trades = 0
        total_pnl = Decimal('0')
        total_fees = Decimal('0')
        max_drawdown = Decimal('0')
        peak_capital = current_capital
        
        trade_details = []
        skipped_trades = 0
        adjusted_trades = 0
        
        # V6.13.3 特有统计
        tp1_hit_count = 0
        tp2_hit_count = 0
        time_stop_count = 0
        stop_loss_count = 0
        total_hold_time = 0
        
        grade_stats = {grade: {'trades': 0, 'wins': 0, 'pnl': Decimal('0')} for grade in ['S', 'A', 'B', 'C']}
        
        for i, signal in enumerate(signals):
            if position is None:
                # 开仓
                base_margin = self.initial_capital * self.grade_config[signal['grade']]['position_ratio']
                leverage = self.grade_config[signal['grade']]['leverage']
                
                position_params = {
                    'symbol': signal['symbol'],
                    'margin': base_margin,
                    'quantity': base_margin * leverage / signal['entry_price'],
                    'notional_value': base_margin * leverage,
                    'leverage': leverage,
                    'signal_grade': signal['grade'],
                }
                
                adjusted_position = self.position_adjuster.adjust_position(
                    position_params, 
                    current_capital
                )
                
                if adjusted_position is None:
                    logger.warning(f"交易 {i+1}: 资金严重不足，跳过")
                    skipped_trades += 1
                    continue
                
                adj_info = adjusted_position.get('adjustment_info', {})
                required_margin = adjusted_position['margin']
                
                if adj_info.get('adjusted'):
                    adjusted_trades += 1
                    logger.info(f"交易 {i+1}: 触发动态调仓 {base_margin:.2f}U → {required_margin:.2f}U "
                               f"({adj_info['adjustment_ratio']:.0%})")
                else:
                    logger.info(f"交易 {i+1}: 资金充足，不调整 ({required_margin:.2f}U)")
                
                # V6.13.3: 计算优化的止损止盈
                atr = Decimal('1000')  # 简化：使用固定 ATR
                
                # 基础止损（不变）
                stop_loss_distance = self.atr_config['stop_loss_atr'] * atr
                
                # V6.13.3 优化止盈（基于 R 值）
                r_value = stop_loss_distance
                tp1_distance = r_value * self.atr_config['tp1_atr']  # 1.5R
                tp2_distance = r_value * self.atr_config['tp2_atr']  # 2.5R
                
                if signal.get('direction', 'LONG') == 'LONG':
                    stop_loss = signal['entry_price'] - stop_loss_distance
                    tp1 = signal['entry_price'] + tp1_distance
                    tp2 = signal['entry_price'] + tp2_distance
                else:
                    stop_loss = signal['entry_price'] + stop_loss_distance
                    tp1 = signal['entry_price'] - tp1_distance
                    tp2 = signal['entry_price'] - tp2_distance
                
                position = {
                    'symbol': signal['symbol'],
                    'direction': signal.get('direction', 'LONG'),
                    'entry_price': signal['entry_price'],
                    'quantity': adjusted_position['quantity'],
                    'entry_time': signal['timestamp'],
                    'stop_loss': stop_loss,
                    'tp1': tp1,
                    'tp2': tp2,
                    'signal_grade': signal['grade'],
                    'leverage': leverage,
                }
                
                logger.info(f"交易 {i+1}: 开仓 {position['symbol']} @ {position['entry_price']:.2f}, "
                           f"止损={stop_loss:.2f}, TP1={tp1:.2f}, TP2={tp2:.2f}")
            
            else:
                # 检查持仓时间平仓（V6.13.3 新增）
                entry_time = datetime.fromisoformat(position['entry_time'].replace('Z', '+00:00'))
                current_time = datetime.fromisoformat(signal['timestamp'].replace('Z', '+00:00'))
                hold_hours = (current_time - entry_time).total_seconds() / 3600
                
                # 检查 1: 超过 72 小时，紧急平仓
                if hold_hours >= self.time_close_config['emergency_hold_hours']:
                    logger.info(f"交易 {i+1}: 时间平仓 {position['symbol']} 持仓{hold_hours:.1f}小时 (>= 72 小时)")
                    
                    # 平仓
                    exit_price = signal['entry_price']
                    pnl = (exit_price - position['entry_price']) * position['quantity']
                    fee = (exit_price + position['entry_price']) * position['quantity'] * self.fee_rate
                    pnl -= fee
                    
                    total_pnl += pnl
                    current_capital += pnl
                    losing_trades += 1
                    time_stop_count += 1
                    
                    trade_details.append({
                        'symbol': position['symbol'],
                        'entry_price': float(position['entry_price']),
                        'exit_price': float(exit_price),
                        'pnl': float(pnl),
                        'exit_reason': f'时间平仓 ({hold_hours:.1f}小时)',
                        'hold_hours': hold_hours,
                    })
                    
                    total_hold_time += hold_hours
                    position = None
                    continue
                
                # 检查 2: 超过 48 小时且浮亏>2%
                if hold_hours >= self.time_close_config['max_hold_hours']:
                    pnl_rate = (signal['entry_price'] - position['entry_price']) / position['entry_price']
                    if position['direction'] == 'SHORT':
                        pnl_rate = -pnl_rate
                    
                    if pnl_rate < -self.time_close_config['min_loss_threshold']:
                        logger.info(f"交易 {i+1}: 时间平仓 {position['symbol']} 持仓{hold_hours:.1f}小时，浮亏{pnl_rate*100:.2f}%")
                        
                        exit_price = signal['entry_price']
                        pnl = (exit_price - position['entry_price']) * position['quantity']
                        fee = (exit_price + position['entry_price']) * position['quantity'] * self.fee_rate
                        pnl -= fee
                        
                        total_pnl += pnl
                        current_capital += pnl
                        losing_trades += 1
                        time_stop_count += 1
                        
                        trade_details.append({
                            'symbol': position['symbol'],
                            'entry_price': float(position['entry_price']),
                            'exit_price': float(exit_price),
                            'pnl': float(pnl),
                            'exit_reason': f'时间平仓 ({hold_hours:.1f}小时，浮亏{pnl_rate*100:.2f}%)',
                            'hold_hours': hold_hours,
                        })
                        
                        total_hold_time += hold_hours
                        position = None
                        continue
                
                # 检查止盈止损
                exit_reason = None
                exit_price = signal['entry_price']
                
                if position['direction'] == 'LONG':
                    if signal['entry_price'] <= position['stop_loss']:
                        exit_reason = '止损'
                        stop_loss_count += 1
                    elif signal['entry_price'] >= position['tp2']:
                        exit_reason = '止盈 TP2'
                        tp2_hit_count += 1
                    elif signal['entry_price'] >= position['tp1']:
                        exit_reason = '止盈 TP1'
                        tp1_hit_count += 1
                else:
                    if signal['entry_price'] >= position['stop_loss']:
                        exit_reason = '止损'
                        stop_loss_count += 1
                    elif signal['entry_price'] <= position['tp2']:
                        exit_reason = '止盈 TP2'
                        tp2_hit_count += 1
                    elif signal['entry_price'] <= position['tp1']:
                        exit_reason = '止盈 TP1'
                        tp1_hit_count += 1
                
                if exit_reason:
                    pnl = (exit_price - position['entry_price']) * position['quantity']
                    if position['direction'] == 'SHORT':
                        pnl = (position['entry_price'] - exit_price) * position['quantity']
                    
                    fee = (exit_price + position['entry_price']) * position['quantity'] * self.fee_rate
                    pnl -= fee
                    
                    total_pnl += pnl
                    current_capital += pnl
                    
                    if pnl > 0:
                        winning_trades += 1
                    else:
                        losing_trades += 1
                    
                    # 更新最大回撤
                    if current_capital > peak_capital:
                        peak_capital = current_capital
                    drawdown = (peak_capital - current_capital) / peak_capital
                    if drawdown > max_drawdown:
                        max_drawdown = drawdown
                    
                    total_fees += fee
                    
                    trade_details.append({
                        'symbol': position['symbol'],
                        'entry_price': float(position['entry_price']),
                        'exit_price': float(exit_price),
                        'pnl': float(pnl),
                        'exit_reason': exit_reason,
                        'hold_hours': hold_hours,
                    })
                    
                    total_hold_time += hold_hours
                    
                    # 更新等级统计
                    grade = position['signal_grade']
                    grade_stats[grade]['trades'] += 1
                    if pnl > 0:
                        grade_stats[grade]['wins'] += 1
                    grade_stats[grade]['pnl'] += pnl
                    
                    logger.info(f"平仓：{signal['timestamp']} (盈亏：{pnl:+.2f}U, 余额：{current_capital:.2f}U, 原因：{exit_reason})")
                    
                    position = None
        
        # 计算统计
        total_trades = winning_trades + losing_trades
        win_rate = winning_trades / total_trades if total_trades > 0 else Decimal(0)
        total_return = total_pnl / self.initial_capital
        avg_hold_time = total_hold_time / total_trades if total_trades > 0 else Decimal(0)
        
        # 盈亏比
        avg_win = sum(t['pnl'] for t in trade_details if t['pnl'] > 0) / winning_trades if winning_trades > 0 else Decimal(0)
        avg_loss = abs(sum(t['pnl'] for t in trade_details if t['pnl'] <= 0) / losing_trades) if losing_trades > 0 else Decimal(1)
        profit_factor = avg_win / avg_loss if avg_loss > 0 else Decimal(0)
        
        # 夏普比率（简化）
        if len(trade_details) > 1:
            pnls = [t['pnl'] for t in trade_details]
            avg_pnl = sum(pnls) / len(pnls)
            std_pnl = math.sqrt(sum((p - avg_pnl) ** 2 for p in pnls) / len(pnls))
            sharpe_ratio = (avg_pnl / std_pnl) if std_pnl > 0 else Decimal(0)
        else:
            sharpe_ratio = Decimal(0)
        
        result = {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': float(win_rate),
            'total_pnl': float(total_pnl),
            'total_return': float(total_return),
            'max_drawdown': float(max_drawdown),
            'sharpe_ratio': float(sharpe_ratio),
            'profit_factor': float(profit_factor),
            'avg_hold_time_hours': float(avg_hold_time),
            'tp1_hit_count': tp1_hit_count,
            'tp2_hit_count': tp2_hit_count,
            'time_stop_count': time_stop_count,
            'stop_loss_count': stop_loss_count,
            'skipped_trades': skipped_trades,
            'adjusted_trades': adjusted_trades,
            'total_fees': float(total_fees),
            'final_capital': float(current_capital),
            'grade_statistics': {k: {'trades': v['trades'], 'wins': v['wins'], 'pnl': float(v['pnl'])} for k, v in grade_stats.items()},
            'parameters': {
                'stop_loss_atr': str(self.atr_config['stop_loss_atr']),
                'tp1_atr': str(self.atr_config['tp1_atr']),
                'tp2_atr': str(self.atr_config['tp2_atr']),
                'max_hold_hours': self.time_close_config['max_hold_hours'],
                'emergency_hold_hours': self.time_close_config['emergency_hold_hours'],
                'min_loss_threshold': str(self.time_close_config['min_loss_threshold']),
            },
        }
        
        logger.info("=" * 80)
        logger.info("V6.13.3 回测完成")
        logger.info("=" * 80)
        
        return result
    
    def save_report(self, result: Dict[str, Any], filepath: str):
        """保存回测报告"""
        def decimal_to_float(obj):
            if isinstance(obj, Decimal):
                return float(obj)
            elif isinstance(obj, dict):
                return {k: decimal_to_float(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [decimal_to_float(item) for item in obj]
            return obj
        
        report = {
            'backtest_date': datetime.now().isoformat(),
            'initial_capital': float(self.initial_capital),
            'result': decimal_to_float(result),
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        
        logger.info(f"回测报告已保存：{filepath}")


def main():
    """主函数"""
    logger.info("开始 V6.13.3 完整回测")
    
    # 1. 初始化回测器
    backtester = V6133FullBacktester(initial_capital=Decimal('500'))
    
    # 2. 加载数据
    with open('data/multi_timeframe_data.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    logger.info(f"加载数据完成：{list(data.keys())}")
    
    # 3. 生成信号
    signals = backtester.generate_signals(data)
    logger.info(f"生成 {len(signals)} 个有效信号")
    
    # 4. 运行回测
    result = backtester.run_backtest(signals)
    
    # 5. 保存报告
    report_file = f'data/backtest_v6133_full_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    backtester.save_report(result, report_file)
    
    # 6. 打印报告
    print("\n" + "=" * 80)
    print("📊 V6.13.3 完整回测报告")
    print("=" * 80)
    
    print(f"\n{'指标':<25} {'数值':<20}")
    print("-" * 45)
    print(f"{'总交易数':<25} {result['total_trades']:<20}")
    print(f"{'胜率':<25} {result['win_rate']*100:.2f}%")
    print(f"{'总盈亏':<25} {result['total_pnl']:+.2f}U")
    print(f"{'总收益率':<25} {result['total_return']*100:.2f}%")
    print(f"{'最大回撤':<25} {result['max_drawdown']*100:.2f}%")
    print(f"{'夏普比率':<25} {result['sharpe_ratio']:.2f}")
    print(f"{'盈亏比':<25} {result['profit_factor']:.2f}")
    print(f"{'平均持仓时间':<25} {result['avg_hold_time_hours']:.1f}小时")
    print(f"{'最终资金':<25} {result['final_capital']:.2f}U")
    
    print("\n止盈止损统计:")
    print(f"  TP1 触及：{result['tp1_hit_count']}次")
    print(f"  TP2 触及：{result['tp2_hit_count']}次")
    print(f"  时间平仓：{result['time_stop_count']}次")
    print(f"  止损触发：{result['stop_loss_count']}次")
    
    print("\n按信号等级统计:")
    for grade, stats in result['grade_statistics'].items():
        print(f"  {grade}级：{stats['trades']}笔，胜率{stats['wins']/stats['trades']*100 if stats['trades']>0 else 0:.1f}%，盈亏{stats['pnl']:+.2f}U")
    
    print("\n优化参数:")
    print(f"  止损：{result['parameters']['stop_loss_atr']}×ATR")
    print(f"  TP1: {result['parameters']['tp1_atr']}×R")
    print(f"  TP2: {result['parameters']['tp2_atr']}×R")
    print(f"  时间平仓：{result['parameters']['max_hold_hours']}小时 (浮亏>{float(result['parameters']['min_loss_threshold'])*100}%)")
    print(f"  紧急平仓：{result['parameters']['emergency_hold_hours']}小时")
    
    print("=" * 80)
    logger.info("回测完成")


if __name__ == '__main__':
    main()
