"""
v6.17方案回测脚本 - 动态波动率过滤器版

核心改进（相对于v6.16.6）：
1. 动态ATR过滤器：根据历史波动率分布和ADX趋势强度动态调整最低ATR%阈值
   - 每个币种独立计算历史ATR%分布（过去30天/720小时）
   - 使用历史ATR%的20%分位数作为基准
   - 根据ADX动态调整：强趋势0.6，中等趋势0.7，弱趋势0.8
   - 绝对下限保护：0.3%（BNB: 0.4%，TRX: 0.25%）

前置过滤器（v6.17）：
- ADX ≥ 15（BNB ≥ 18）
- ATR%范围：动态下限-8.5%（替代固定阈值）
- 成交量：ADX-based动态阈值
- 同币种冷却期：4小时
- 每日最大交易数：6笔

评分系统（保持v6.16.6）：
- 趋势强度：20分
- 形态质量：50分
- 动量背离：30分
- 等级阈值：S≥88，A≥75，B≥75，C≥55
- BNB等级阈值：S≥85，A≥80，B≥80，C≥60

仓位配置（保持v6.16.6）：
- S级：35%
- A级：30%（ETH专属20%）
- B级：15%
- C级：8%

止盈止损参数（保持v6.16.6）：
- 止损：2.2×ATR
- TP1：2.5×ATR（平25%）
- TP2：4.0×ATR（平25%）
- 吊灯启动：1.8×ATR
- 吊灯回撤：1.2×ATR
- 时间止损：48小时
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from collections import deque
import structlog
import os
import sys
import yaml

script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
sys.path.insert(0, project_root)

from shared.indicators import TechnicalIndicators
from shared.dynamic_atr_filter import DynamicATRFilter

logger = structlog.get_logger()

ALL_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'TRXUSDT']


class Position:
    """持仓信息类"""

    def __init__(self):
        self.entry_time = None
        self.entry_price = Decimal('0')
        self.direction = None
        self.quantity = Decimal('0')
        self.position_size = Decimal('0')
        self.leverage = 1
        self.grade = 'C'
        self.original_grade = 'C'
        self.atr = Decimal('0')
        self.tp1_price = Decimal('0')
        self.tp2_price = Decimal('0')
        self.stop_loss = Decimal('0')
        self.highest_price = Decimal('0')
        self.lowest_price = Decimal('0')
        self.tp1_hit = False
        self.tp2_hit = False
        self.trailing_activated = False
        self.score = 0.0
        self.symbol = 'BTCUSDT'
        self.position_coefficient = 1.0


class BacktestEngine:
    """回测引擎 - v6.17方案（动态ATR过滤器）"""

    def __init__(self, config: Dict):
        self.config = config
        self.initial_capital = Decimal(str(
            config['strategy']['risk']['frequency_control']['initial_capital_usdt']
        ))
        self.current_capital = self.initial_capital
        self.highest_capital = self.initial_capital
        self.positions: List[Position] = []
        self.trades: List[Dict] = []
        self.scoring_config = config['strategy']['scoring']
        self.binance_config = config['binance']

        self.stop_loss_atr = Decimal('2.2')
        self.tp1_atr_multiplier = Decimal('2.5')
        self.tp2_atr_multiplier = Decimal('4.0')
        self.trailing_activation_atr = Decimal('1.8')
        self.trailing_stop_atr = Decimal('1.2')
        self.time_stop_hours = 48

        self.tp1_close_ratio = Decimal('0.25')
        self.tp2_close_ratio = Decimal('0.25')

        self.max_daily_trades = 6
        self.symbol_cooldown_hours = 4
        self.daily_trade_count = {}
        self.symbol_last_trade_time = {}

        self.grade_thresholds = {
            'S': 88,
            'A': 75,
            'B': 75,
            'C': 55
        }

        self.bnb_grade_thresholds = {
            'S': 85,
            'A': 80,
            'B': 80,
            'C': 60
        }

        self.filter_stats = {
            'total_signals': 0,
            'adx_filtered': 0,
            'volume_filtered': 0,
            'atr_percent_filtered': 0,
            'dynamic_atr_filtered': 0,
            'trend_alignment_filtered': 0,
            'rsi_filtered': 0,
            'score_filtered': 0,
            'cooldown_filtered': 0,
            'daily_limit_filtered': 0,
            'grade_a_rsi_filtered': 0,
            'grade_a_macd_filtered': 0,
            'grade_s_downgraded': 0,
            'volume_position_reduced': 0,
            'low_volatility_filtered': 0,
            'bnb_filtered': 0,
            'opened_positions': 0,
            'symbol_stats': {symbol: {'signals': 0, 'opened': 0} for symbol in ALL_SYMBOLS}
        }

        dynamic_atr_config = config['strategy']['filters'].get('dynamic_atr_filter', {})
        self.dynamic_atr_filter = DynamicATRFilter(dynamic_atr_config)
        self._atr_history_initialized: Dict[str, bool] = {}

    def load_klines_from_csv(self, symbol: str, interval: str) -> pd.DataFrame:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        symbol_lower = symbol.lower().replace('usdt', '')
        filename = os.path.join(script_dir, f"../data/{symbol_lower}usdt_{interval}.csv")

        if not os.path.exists(filename):
            filename = os.path.join(script_dir, f"../data/btcusdt_{interval}.csv")

        df = pd.read_csv(filename)
        df['open_time'] = pd.to_datetime(df['open_time'])
        df.set_index('open_time', inplace=True)
        df.rename(columns={
            'open_price': 'open',
            'high_price': 'high',
            'low_price': 'low',
            'close_price': 'close'
        }, inplace=True)

        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        return df

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        
        high = df['high']
        low = df['low']
        close = df['close']
        
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df['ATR'] = tr.rolling(window=14).mean()
        
        sma_14 = close.rolling(window=14).mean()
        dm_plus = high.diff()
        dm_minus = -low.diff()
        dm_plus[dm_plus < 0] = 0
        dm_minus[dm_minus < 0] = 0
        
        atr_14 = tr.rolling(window=14).mean()
        di_plus = 100 * (dm_plus.rolling(window=14).mean() / atr_14)
        di_minus = 100 * (dm_minus.rolling(window=14).mean() / atr_14)
        dx = 100 * abs(di_plus - di_minus) / (di_plus + di_minus)
        df['ADX'] = dx.rolling(window=14).mean()
        
        df['EMA21'] = close.ewm(span=21, adjust=False).mean()
        df['EMA50'] = close.ewm(span=50, adjust=False).mean()
        df['EMA200'] = close.ewm(span=200, adjust=False).mean()
        
        ema_12 = close.ewm(span=12, adjust=False).mean()
        ema_26 = close.ewm(span=26, adjust=False).mean()
        df['MACD'] = ema_12 - ema_26
        df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']
        
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        df['Volume_MA'] = df['volume'].rolling(window=20).mean()
        
        return df

    def _calculate_score(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame,
        indicators_1d: pd.DataFrame
    ) -> float:
        score = 0.0
        
        trend_score = 0.0
        ema21 = indicators_1h['EMA21'].iloc[-1]
        ema50 = indicators_1h['EMA50'].iloc[-1]
        close = indicators_1h['close'].iloc[-1]
        
        if ema21 > ema50 and close > ema21:
            trend_score = 15.0
        elif ema21 > ema50:
            trend_score = 10.0
        elif close > ema21:
            trend_score = 5.0
        
        adx = indicators_1h['ADX'].iloc[-1]
        if pd.notna(adx):
            if adx >= 25:
                trend_score += 5.0
            elif adx >= 20:
                trend_score += 3.0
            elif adx >= 15:
                trend_score += 2.0
        
        score += trend_score
        
        pattern_score = 40.0
        
        macd_hist = indicators_1h['MACD_Hist'].iloc[-1]
        if pd.notna(macd_hist) and macd_hist > 0:
            pattern_score += 10.0
        
        score += pattern_score
        
        momentum_score = 0.0
        rsi = indicators_1h['RSI'].iloc[-1]
        if pd.notna(rsi):
            if 40 <= rsi <= 60:
                momentum_score = 30.0
            elif 30 <= rsi <= 70:
                momentum_score = 20.0
            else:
                momentum_score = 10.0
        
        score += momentum_score
        
        return min(score, 100.0)

    def _determine_direction(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame
    ) -> str:
        ema21 = indicators_1h['EMA21'].iloc[-1]
        ema50 = indicators_1h['EMA50'].iloc[-1]
        close = indicators_1h['close'].iloc[-1]
        
        if ema21 > ema50 and close > ema21:
            return 'LONG'
        elif ema21 < ema50 and close < ema21:
            return 'SHORT'
        else:
            return 'LONG' if close > ema21 else 'SHORT'

    def is_trend_aligned_v616(
        self,
        klines_4h: pd.DataFrame,
        klines_1d: pd.DataFrame,
        direction: str
    ) -> Tuple[bool, str]:
        if klines_4h is None or klines_1d is None:
            return True, "数据不足，跳过趋势一致性检查"
        
        close_4h = klines_4h['close'].iloc[-1]
        ema21_4h = klines_4h['close'].ewm(span=21, adjust=False).mean().iloc[-1]
        
        close_1d = klines_1d['close'].iloc[-1]
        ema21_1d = klines_1d['close'].ewm(span=21, adjust=False).mean().iloc[-1]
        
        if direction == 'LONG':
            if close_4h > ema21_4h and close_1d > ema21_1d:
                return True, "趋势一致做多"
            else:
                return False, "趋势不一致做多"
        else:
            if close_4h < ema21_4h and close_1d < ema21_1d:
                return True, "趋势一致做空"
            else:
                return False, "趋势不一致做空"

    def is_s_grade_extra_condition(self, indicators_4h: pd.DataFrame) -> bool:
        if indicators_4h is None:
            return False
        
        adx_4h = indicators_4h['ADX'].iloc[-1]
        if pd.isna(adx_4h) or adx_4h < 25:
            return False
        
        macd_hist = indicators_4h['MACD_Hist'].iloc[-1]
        if pd.isna(macd_hist):
            return False
        
        macd_consecutive = 0
        for i in range(min(5, len(indicators_4h))):
            hist = indicators_4h['MACD_Hist'].iloc[-(i+1)]
            if pd.notna(hist) and hist > 0:
                macd_consecutive += 1
            else:
                break
        
        return macd_consecutive >= 3

    def check_volume_filter(
        self,
        symbol: str,
        adx_1d: float,
        volume: float,
        volume_ma: float
    ) -> Tuple[bool, float, str]:
        if pd.isna(volume) or pd.isna(volume_ma) or volume_ma == 0:
            return True, 1.0, "成交量数据不足，跳过检查"
        
        volume_ratio = volume / volume_ma
        
        dynamic_config = self.config['strategy']['filters'].get('dynamic_volume_filter', {})
        
        if not dynamic_config.get('enabled', False):
            threshold = 1.0
        else:
            if adx_1d is None or pd.isna(adx_1d):
                threshold = dynamic_config.get('fallback_ratio', 1.0)
            else:
                thresholds = dynamic_config.get('adx_thresholds', {})
                ratios = dynamic_config.get('base_ratios', {})
                
                if adx_1d > thresholds.get('strong_trend', 25):
                    threshold = ratios.get('strong', 0.5)
                elif adx_1d > thresholds.get('medium_trend', 20):
                    threshold = ratios.get('medium', 0.65)
                elif adx_1d >= thresholds.get('weak_trend', 15):
                    threshold = ratios.get('weak', 0.8)
                else:
                    threshold = ratios.get('none', 1.0)
        
        if volume_ratio >= threshold:
            return True, 1.0, f"成交量达标（{volume_ratio:.2f}倍 ≥ {threshold}倍，ADX_1d={adx_1d:.1f}）"
        elif volume_ratio >= threshold * 0.8:
            return True, 0.5, f"成交量接近阈值，仓位减半（{volume_ratio:.2f}倍，阈值={threshold}倍，ADX_1d={adx_1d:.1f}）"
        else:
            return False, 0.0, f"成交量不足（{volume_ratio:.2f}倍 < {threshold * 0.8}倍，ADX_1d={adx_1d:.1f}）"

    def check_low_volatility_filter(
        self,
        atr_percent: float,
        grade: str,
        score: float
    ) -> Tuple[bool, str]:
        if atr_percent < 0.5:
            if grade == 'C':
                return False, "低波动市场拒绝C级信号"
            elif grade == 'B' and score < 75:
                return False, f"低波动市场B级信号评分不足（{score}分 < 75分）"
        
        return True, "通过"

    def check_bnb_filter(
        self,
        symbol: str,
        score: float,
        atr_percent: float,
        adx: float
    ) -> Tuple[bool, str]:
        if symbol != 'BNBUSDT':
            return True, "非BNB币种，跳过特殊过滤"
        
        if score < 60:
            return False, f"BNB评分不足（{score}分 < 60分）"
        if 60 <= score < 80:
            return False, f"BNB评分不足（{score}分 < 80分，需A级以上）"
        
        if atr_percent < 0.4:
            return False, f"BNB波动不足（ATR%={atr_percent:.2f}% < 0.4%）"
        
        if adx < 18:
            return False, f"BNB趋势不强（ADX={adx:.1f} < 18）"
        
        return True, "通过BNB特殊过滤"

    def _initialize_dynamic_atr_history(
        self,
        symbol: str,
        klines_1h: pd.DataFrame
    ) -> None:
        if self._atr_history_initialized.get(symbol, False):
            return
        
        try:
            if len(klines_1h) < 50:
                logger.warning(
                    f"{symbol} K线数据不足，无法初始化ATR历史",
                    symbol=symbol,
                    kline_count=len(klines_1h)
                )
                return
            
            high = klines_1h['high']
            low = klines_1h['low']
            close = klines_1h['close']
            
            tr1 = high - low
            tr2 = abs(high - close.shift(1))
            tr3 = abs(low - close.shift(1))
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            
            atr_period = 14
            atr = tr.rolling(window=atr_period).mean()
            
            atr_values = []
            close_prices = []
            
            for i in range(atr_period, len(klines_1h)):
                if pd.notna(atr.iloc[i]) and pd.notna(close.iloc[i]) and close.iloc[i] > 0:
                    atr_values.append(float(atr.iloc[i]))
                    close_prices.append(float(close.iloc[i]))
            
            if atr_values and close_prices:
                count = self.dynamic_atr_filter.initialize_history(
                    symbol=symbol,
                    atr_values=atr_values,
                    close_prices=close_prices
                )
                
                self.dynamic_atr_filter.log_statistics(symbol)
                
                logger.info(
                    f"{symbol} 动态ATR过滤器历史数据初始化完成",
                    symbol=symbol,
                    history_count=count
                )
            
            self._atr_history_initialized[symbol] = True
            
        except Exception as e:
            logger.error(
                f"{symbol} 初始化动态ATR历史数据失败",
                symbol=symbol,
                error=str(e)
            )

    def run_backtest(
        self,
        symbols: List[str],
        klines_dict: Dict[str, Dict[str, pd.DataFrame]]
    ) -> Dict:
        all_timestamps = set()
        for symbol in symbols:
            if symbol in klines_dict and '1h' in klines_dict[symbol]:
                all_timestamps.update(klines_dict[symbol]['1h'].index)
        
        sorted_timestamps = sorted(list(all_timestamps))
        
        for symbol in symbols:
            if symbol in klines_dict and '1h' in klines_dict[symbol]:
                self._initialize_dynamic_atr_history(symbol, klines_dict[symbol]['1h'])
        
        for timestamp in sorted_timestamps:
            for symbol in symbols:
                if symbol not in klines_dict:
                    continue
                
                klines_1h = klines_dict[symbol].get('1h')
                klines_4h = klines_dict[symbol].get('4h')
                klines_1d = klines_dict[symbol].get('1d')
                
                if klines_1h is None or timestamp not in klines_1h.index:
                    continue
                
                current_idx = klines_1h.index.get_loc(timestamp)
                if current_idx < 200:
                    continue
                
                current_price = Decimal(str(klines_1h['close'].iloc[current_idx]))
                
                indicators_1h = self.calculate_indicators(klines_1h.iloc[:current_idx+1])
                indicators_4h = self.calculate_indicators(klines_4h.iloc[:current_idx+1]) if klines_4h is not None else None
                indicators_1d = self.calculate_indicators(klines_1d.iloc[:current_idx+1]) if klines_1d is not None else None
                
                atr = Decimal(str(indicators_1h['ATR'].iloc[-1]))
                close = float(klines_1h['close'].iloc[-1])
                if close > 0 and atr > 0:
                    self.dynamic_atr_filter.update_history(symbol, float(atr), close)
                
                self._check_and_open_position(
                    symbol=symbol,
                    current_time=timestamp,
                    current_price=current_price,
                    indicators_1h=indicators_1h,
                    indicators_4h=indicators_4h,
                    indicators_1d=indicators_1d,
                    klines_4h=klines_4h,
                    klines_1d=klines_1d
                )
                
                self._check_and_close_positions(
                    symbol=symbol,
                    current_time=timestamp,
                    current_price=current_price,
                    indicators_1h=indicators_1h
                )
        
        for symbol in symbols:
            if symbol in klines_dict and '1h' in klines_dict[symbol]:
                last_price = Decimal(str(klines_dict[symbol]['1h']['close'].iloc[-1]))
                last_time = klines_dict[symbol]['1h'].index[-1]
                for position in self.positions[:]:
                    if position.symbol == symbol:
                        self._force_close_position(position, last_time, last_price)
        
        return self._calculate_results()

    def _check_and_open_position(
        self,
        symbol: str,
        current_time,
        current_price: Decimal,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame,
        indicators_1d: pd.DataFrame,
        klines_4h: pd.DataFrame,
        klines_1d: pd.DataFrame
    ):
        self.filter_stats['total_signals'] += 1
        self.filter_stats['symbol_stats'][symbol]['signals'] += 1
        
        adx = indicators_1h['ADX'].iloc[-1]
        min_adx = 18 if symbol == 'BNBUSDT' else 15
        if pd.isna(adx) or adx < min_adx:
            self.filter_stats['adx_filtered'] += 1
            return
        
        atr = Decimal(str(indicators_1h['ATR'].iloc[-1]))
        if atr == 0:
            return
        atr_percent = float(atr / current_price * 100)
        
        adx_1d = indicators_1d['ADX'].iloc[-1] if indicators_1d is not None and 'ADX' in indicators_1d.columns else None
        adx_for_atr = float(adx_1d) if pd.notna(adx_1d) else 15.0
        
        should_filter, filter_reason = self.dynamic_atr_filter.should_filter(
            symbol=symbol,
            current_atr_percent=atr_percent,
            adx=adx_for_atr
        )
        
        if should_filter:
            self.filter_stats['dynamic_atr_filtered'] += 1
            logger.debug(
                f"动态ATR过滤",
                symbol=symbol,
                time=current_time,
                atr_percent=atr_percent,
                reason=filter_reason
            )
            return
        
        if atr_percent > 8.5:
            self.filter_stats['atr_percent_filtered'] += 1
            return
        
        score = self._calculate_score(indicators_1h, indicators_4h, indicators_1d)
        
        min_score = 60 if symbol == 'BNBUSDT' else 55
        if score < min_score:
            self.filter_stats['score_filtered'] += 1
            return
        
        thresholds = self.bnb_grade_thresholds if symbol == 'BNBUSDT' else self.grade_thresholds
        
        if score >= thresholds['S']:
            grade = 'S'
        elif score >= thresholds['A']:
            grade = 'A'
        elif score >= thresholds['B']:
            grade = 'B'
        else:
            grade = 'C'
        
        original_grade = grade
        
        if grade == 'S' and not self.is_s_grade_extra_condition(indicators_4h):
            grade = 'A'
            self.filter_stats['grade_s_downgraded'] += 1
        
        low_vol_ok, low_vol_reason = self.check_low_volatility_filter(atr_percent, grade, score)
        if not low_vol_ok:
            self.filter_stats['low_volatility_filtered'] += 1
            return
        
        bnb_ok, bnb_reason = self.check_bnb_filter(symbol, score, atr_percent, adx)
        if not bnb_ok:
            self.filter_stats['bnb_filtered'] += 1
            return
        
        volume = indicators_1h['volume'].iloc[-1]
        volume_ma = indicators_1h['Volume_MA'].iloc[-1]
        
        volume_pass, position_coefficient, volume_reason = self.check_volume_filter(
            symbol, adx_1d, volume, volume_ma
        )
        
        if not volume_pass:
            self.filter_stats['volume_filtered'] += 1
            return
        
        if position_coefficient < 1.0:
            self.filter_stats['volume_position_reduced'] += 1
        
        volume_ratio = volume / volume_ma if pd.notna(volume) and pd.notna(volume_ma) else 0.0
        
        direction = self._determine_direction(indicators_1h, indicators_4h)
        
        trend_aligned, trend_reason = self.is_trend_aligned_v616(klines_4h, klines_1d, direction)
        if not trend_aligned:
            self.filter_stats['trend_alignment_filtered'] += 1
            return
        
        cooldown_ok, cooldown_reason = self.check_cooldown_and_daily_limit(current_time, symbol)
        if not cooldown_ok:
            if '冷却期' in cooldown_reason:
                self.filter_stats['cooldown_filtered'] += 1
            else:
                self.filter_stats['daily_limit_filtered'] += 1
            return
        
        rsi_4h = indicators_4h['RSI'].iloc[-1] if indicators_4h is not None else 50
        macd_hist = indicators_1h['MACD_Hist'].iloc[-1]
        grade_a_ok, grade_a_reason = self.check_grade_a_filters(grade, rsi_4h, volume_ratio, macd_hist, direction)
        if not grade_a_ok:
            if 'RSI' in grade_a_reason:
                self.filter_stats['grade_a_rsi_filtered'] += 1
            elif 'MACD' in grade_a_reason:
                self.filter_stats['grade_a_macd_filtered'] += 1
            else:
                self.filter_stats['volume_filtered'] += 1
            return
        
        eth_override = self.binance_config.get('eth_position_override', {})
        if symbol == 'ETHUSDT' and grade in eth_override:
            position_ratio = Decimal(str(eth_override[grade]))
        else:
            position_ratio = Decimal(str(self.binance_config['position_ratio'][grade]))
        position_ratio *= Decimal(str(position_coefficient))
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
        position.original_grade = original_grade
        position.atr = atr
        position.score = score
        position.highest_price = current_price
        position.lowest_price = current_price
        position.symbol = symbol
        position.position_coefficient = position_coefficient
        
        if direction == 'LONG':
            position.stop_loss = current_price - atr * self.stop_loss_atr
            position.tp1_price = current_price + atr * self.tp1_atr_multiplier
            position.tp2_price = current_price + atr * self.tp2_atr_multiplier
        else:
            position.stop_loss = current_price + atr * self.stop_loss_atr
            position.tp1_price = current_price - atr * self.tp1_atr_multiplier
            position.tp2_price = current_price - atr * self.tp2_atr_multiplier
        
        self.positions.append(position)
        
        today = current_time.date().isoformat()
        if today not in self.daily_trade_count:
            self.daily_trade_count[today] = 0
        self.daily_trade_count[today] += 1
        self.symbol_last_trade_time[symbol] = current_time
        
        self.filter_stats['opened_positions'] += 1
        self.filter_stats['symbol_stats'][symbol]['opened'] += 1
        
        logger.info(
            f"开仓信号",
            symbol=symbol,
            time=current_time,
            direction=direction,
            grade=grade,
            score=score,
            price=float(current_price),
            atr_percent=f"{atr_percent:.2f}%",
            position_ratio=f"{float(position_ratio)*100:.1f}%"
        )

    def check_grade_a_filters(
        self,
        grade: str,
        rsi_4h: float,
        volume_ratio: float,
        macd_hist: float,
        direction: str
    ) -> Tuple[bool, str]:
        if grade != 'A':
            return True, "非A级信号，跳过A级过滤"
        
        if pd.notna(rsi_4h):
            if direction == 'LONG' and rsi_4h > 70:
                return False, f"A级做多RSI过高（RSI_4h={rsi_4h:.1f} > 70）"
            elif direction == 'SHORT' and rsi_4h < 30:
                return False, f"A级做空RSI过低（RSI_4h={rsi_4h:.1f} < 30）"
        
        if pd.notna(macd_hist):
            if direction == 'LONG' and macd_hist < 0:
                return False, f"A级做多MACD柱为负（MACD_Hist={macd_hist:.4f}）"
            elif direction == 'SHORT' and macd_hist > 0:
                return False, f"A级做空MACD柱为正（MACD_Hist={macd_hist:.4f}）"
        
        return True, "A级信号通过额外过滤"

    def check_cooldown_and_daily_limit(
        self,
        current_time,
        symbol: str
    ) -> Tuple[bool, str]:
        today = current_time.date().isoformat()
        if today in self.daily_trade_count and self.daily_trade_count[today] >= self.max_daily_trades:
            return False, f"已达每日最大交易数{self.max_daily_trades}笔"
        
        if symbol in self.symbol_last_trade_time:
            last_trade_time = self.symbol_last_trade_time[symbol]
            cooldown_delta = timedelta(hours=self.symbol_cooldown_hours)
            if current_time - last_trade_time < cooldown_delta:
                remaining = cooldown_delta - (current_time - last_trade_time)
                return False, f"{symbol}冷却期中，剩余{remaining.seconds // 3600}小时"
        
        return True, "通过冷却期和每日限制检查"

    def _check_and_close_positions(
        self,
        symbol: str,
        current_time,
        current_price: Decimal,
        indicators_1h: pd.DataFrame
    ):
        for position in self.positions[:]:
            if position.symbol != symbol:
                continue
            
            atr = position.atr
            
            if position.direction == 'LONG':
                position.highest_price = max(position.highest_price, current_price)
                
                profit_atr = (current_price - position.entry_price) / atr
                if profit_atr >= self.trailing_activation_atr:
                    position.trailing_activated = True
                
                if position.trailing_activated:
                    trailing_stop = position.highest_price - atr * self.trailing_stop_atr
                    if current_price <= trailing_stop:
                        self._close_position(position, current_time, current_price, "吊灯止损")
                        continue
                
                if not position.tp1_hit and current_price >= position.tp1_price:
                    self._partial_close_position(position, current_time, current_price, "TP1")
                
                if not position.tp2_hit and current_price >= position.tp2_price:
                    self._partial_close_position(position, current_time, current_price, "TP2")
                
                if current_price <= position.stop_loss:
                    self._close_position(position, current_time, current_price, "止损")
                    continue
            
            else:
                position.lowest_price = min(position.lowest_price, current_price)
                
                profit_atr = (position.entry_price - current_price) / atr
                if profit_atr >= self.trailing_activation_atr:
                    position.trailing_activated = True
                
                if position.trailing_activated:
                    trailing_stop = position.lowest_price + atr * self.trailing_stop_atr
                    if current_price >= trailing_stop:
                        self._close_position(position, current_time, current_price, "吊灯止损")
                        continue
                
                if not position.tp1_hit and current_price <= position.tp1_price:
                    self._partial_close_position(position, current_time, current_price, "TP1")
                
                if not position.tp2_hit and current_price <= position.tp2_price:
                    self._partial_close_position(position, current_time, current_price, "TP2")
                
                if current_price >= position.stop_loss:
                    self._close_position(position, current_time, current_price, "止损")
                    continue
            
            holding_hours = (current_time - position.entry_time).total_seconds() / 3600
            if holding_hours >= self.time_stop_hours and not position.tp1_hit:
                self._partial_close_position(position, current_time, current_price, "时间止损")

    def _partial_close_position(
        self,
        position: Position,
        current_time,
        current_price: Decimal,
        reason: str
    ):
        if reason == "TP1":
            close_ratio = self.tp1_close_ratio
            position.tp1_hit = True
        elif reason == "TP2":
            close_ratio = self.tp2_close_ratio
            position.tp2_hit = True
        else:
            close_ratio = Decimal('0.5')
        
        close_quantity = position.quantity * close_ratio
        pnl = self._calculate_pnl(position, current_price, close_quantity)
        
        self.current_capital += pnl
        
        position.quantity -= close_quantity
        
        trade_record = {
            'symbol': position.symbol,
            'entry_time': position.entry_time,
            'exit_time': current_time,
            'entry_price': float(position.entry_price),
            'exit_price': float(current_price),
            'direction': position.direction,
            'grade': position.grade,
            'score': position.score,
            'pnl': float(pnl),
            'exit_reason': reason,
            'quantity': float(close_quantity)
        }
        self.trades.append(trade_record)
        
        logger.info(
            f"部分平仓",
            symbol=position.symbol,
            time=current_time,
            reason=reason,
            price=float(current_price),
            pnl=float(pnl),
            remaining_quantity=float(position.quantity)
        )

    def _close_position(
        self,
        position: Position,
        current_time,
        current_price: Decimal,
        reason: str
    ):
        pnl = self._calculate_pnl(position, current_price, position.quantity)
        
        self.current_capital += pnl
        
        if self.current_capital > self.highest_capital:
            self.highest_capital = self.current_capital
        
        trade_record = {
            'symbol': position.symbol,
            'entry_time': position.entry_time,
            'exit_time': current_time,
            'entry_price': float(position.entry_price),
            'exit_price': float(current_price),
            'direction': position.direction,
            'grade': position.grade,
            'score': position.score,
            'pnl': float(pnl),
            'exit_reason': reason,
            'quantity': float(position.quantity)
        }
        self.trades.append(trade_record)
        
        self.positions.remove(position)
        
        logger.info(
            f"平仓",
            symbol=position.symbol,
            time=current_time,
            reason=reason,
            price=float(current_price),
            pnl=float(pnl),
            grade=position.grade
        )

    def _force_close_position(
        self,
        position: Position,
        current_time,
        current_price: Decimal
    ):
        self._close_position(position, current_time, current_price, "回测结束强制平仓")

    def _calculate_pnl(
        self,
        position: Position,
        current_price: Decimal,
        quantity: Decimal
    ) -> Decimal:
        if position.direction == 'LONG':
            pnl = (current_price - position.entry_price) * quantity
        else:
            pnl = (position.entry_price - current_price) * quantity
        
        return pnl * position.leverage

    def _calculate_results(self) -> Dict:
        if not self.trades:
            return {
                'total_return': 0,
                'total_trades': 0,
                'win_rate': 0,
                'max_drawdown': 0,
                'sharpe_ratio': 0,
                'trades': [],
                'filter_stats': self.filter_stats,
                'grade_stats': {},
                'symbol_stats': {},
                'final_capital': float(self.current_capital),
                'total_pnl': 0.0
            }
        
        total_pnl = sum(t['pnl'] for t in self.trades)
        total_return = (total_pnl / float(self.initial_capital)) * 100
        
        winning_trades = [t for t in self.trades if t['pnl'] > 0]
        losing_trades = [t for t in self.trades if t['pnl'] < 0]
        win_rate = len(winning_trades) / len(self.trades) * 100 if self.trades else 0
        
        capital_curve = [float(self.initial_capital)]
        for trade in sorted(self.trades, key=lambda x: x['exit_time']):
            capital_curve.append(capital_curve[-1] + trade['pnl'])
        
        peak = capital_curve[0]
        max_drawdown = 0
        for capital in capital_curve:
            if capital > peak:
                peak = capital
            drawdown = (peak - capital) / peak * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        
        returns = []
        for i in range(1, len(capital_curve)):
            ret = (capital_curve[i] - capital_curve[i-1]) / capital_curve[i-1]
            returns.append(ret)
        
        if returns:
            avg_return = np.mean(returns)
            std_return = np.std(returns)
            sharpe_ratio = (avg_return / std_return * np.sqrt(252)) if std_return > 0 else 0
        else:
            sharpe_ratio = 0
        
        grade_stats = {}
        for trade in self.trades:
            grade = trade['grade']
            if grade not in grade_stats:
                grade_stats[grade] = {'count': 0, 'pnl': 0, 'wins': 0}
            grade_stats[grade]['count'] += 1
            grade_stats[grade]['pnl'] += trade['pnl']
            if trade['pnl'] > 0:
                grade_stats[grade]['wins'] += 1
        
        for grade in grade_stats:
            grade_stats[grade]['win_rate'] = (
                grade_stats[grade]['wins'] / grade_stats[grade]['count'] * 100
                if grade_stats[grade]['count'] > 0 else 0
            )
        
        symbol_stats = {}
        for trade in self.trades:
            symbol = trade['symbol']
            if symbol not in symbol_stats:
                symbol_stats[symbol] = {'count': 0, 'pnl': 0, 'wins': 0}
            symbol_stats[symbol]['count'] += 1
            symbol_stats[symbol]['pnl'] += trade['pnl']
            if trade['pnl'] > 0:
                symbol_stats[symbol]['wins'] += 1
        
        for symbol in symbol_stats:
            symbol_stats[symbol]['win_rate'] = (
                symbol_stats[symbol]['wins'] / symbol_stats[symbol]['count'] * 100
                if symbol_stats[symbol]['count'] > 0 else 0
            )
        
        return {
            'total_return': total_return,
            'total_trades': len(self.trades),
            'win_rate': win_rate,
            'max_drawdown': max_drawdown,
            'sharpe_ratio': sharpe_ratio,
            'trades': self.trades,
            'filter_stats': self.filter_stats,
            'grade_stats': grade_stats,
            'symbol_stats': symbol_stats,
            'final_capital': float(self.current_capital),
            'total_pnl': float(total_pnl)
        }


def main():
    config_path = os.path.join(project_root, "strategies/btc_eth/config.yaml")
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    engine = BacktestEngine(config)
    
    symbols = ALL_SYMBOLS
    
    klines_dict = {}
    for symbol in symbols:
        klines_dict[symbol] = {}
        for interval in ['1h', '4h', '1d']:
            try:
                df = engine.load_klines_from_csv(symbol, interval)
                klines_dict[symbol][interval] = df
                logger.info(f"加载 {symbol} {interval} 数据: {len(df)} 条")
            except Exception as e:
                logger.warning(f"加载 {symbol} {interval} 数据失败: {e}")
    
    results = engine.run_backtest(symbols, klines_dict)
    
    print("\n" + "="*60)
    print("v6.17 回测结果（动态ATR过滤器）")
    print("="*60)
    print(f"总收益率: {results['total_return']:.2f}%")
    print(f"总交易次数: {results['total_trades']}")
    print(f"胜率: {results['win_rate']:.2f}%")
    print(f"最大回撤: {results['max_drawdown']:.2f}%")
    print(f"夏普比率: {results['sharpe_ratio']:.2f}")
    print(f"最终资金: {results['final_capital']:.2f} USDT")
    print(f"总盈亏: {results['total_pnl']:.2f} USDT")
    
    print("\n" + "-"*60)
    print("过滤器统计:")
    print(f"- 总信号数: {results['filter_stats']['total_signals']}")
    print(f"- ADX过滤: {results['filter_stats']['adx_filtered']}")
    print(f"- 评分过滤: {results['filter_stats']['score_filtered']}")
    print(f"- 动态ATR过滤: {results['filter_stats']['dynamic_atr_filtered']}")
    print(f"- ATR%上限过滤: {results['filter_stats']['atr_percent_filtered']}")
    print(f"- 成交量过滤: {results['filter_stats']['volume_filtered']}")
    print(f"- 低波动过滤: {results['filter_stats']['low_volatility_filtered']}")
    print(f"- BNB特殊过滤: {results['filter_stats']['bnb_filtered']}")
    print(f"- 趋势一致性过滤: {results['filter_stats']['trend_alignment_filtered']}")
    print(f"- 冷却期过滤: {results['filter_stats']['cooldown_filtered']}")
    print(f"- 每日限制过滤: {results['filter_stats']['daily_limit_filtered']}")
    print(f"- 开仓数: {results['filter_stats']['opened_positions']}")
    
    if 'grade_stats' in results:
        print("\n" + "-"*60)
        print("等级统计:")
        for grade, stats in sorted(results['grade_stats'].items()):
            print(f"- {grade}级: {stats['count']}笔, 胜率{stats['win_rate']:.1f}%, 盈亏{stats['pnl']:.2f}U")
    
    if 'symbol_stats' in results:
        print("\n" + "-"*60)
        print("币种统计:")
        for symbol, stats in sorted(results['symbol_stats'].items()):
            print(f"- {symbol}: {stats['count']}笔, 胜率{stats['win_rate']:.1f}%, 盈亏{stats['pnl']:.2f}U")
    
    print("\n" + "="*60)
    
    for symbol in symbols:
        engine.dynamic_atr_filter.log_statistics(symbol)


if __name__ == "__main__":
    main()
