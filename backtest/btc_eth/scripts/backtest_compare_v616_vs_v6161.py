"""
v6.16 vs v6.16.1 对比回测脚本

用于对比两个版本的回测结果差异
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
import structlog
import os
import sys
import yaml

# 添加项目根目录到系统路径
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
sys.path.insert(0, project_root)

from shared.indicators import TechnicalIndicators

logger = structlog.get_logger()


class Position:
    """持仓信息类"""

    def __init__(self):
        """初始化持仓"""
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


class BacktestEngineBase:
    """回测引擎基类"""

    def __init__(self, config: Dict, version: str):
        """
        初始化回测引擎

        Args:
            config: 配置字典
            version: 版本号（v6.16 或 v6.16.1）
        """
        self.config = config
        self.version = version
        self.initial_capital = Decimal(str(
            config['strategy']['risk']['frequency_control']['initial_capital_usdt']
        ))
        self.current_capital = self.initial_capital
        self.highest_capital = self.initial_capital
        self.positions: List[Position] = []
        self.trades: List[Dict] = []
        self.binance_config = config['binance']

        # 止盈止损参数（两个版本相同）
        self.stop_loss_atr = Decimal('1.8')
        self.tp1_atr_multiplier = Decimal('3.5')
        self.tp2_atr_multiplier = Decimal('6.0')
        self.trailing_activation_atr = Decimal('2.0')
        self.trailing_stop_atr = Decimal('1.5')
        self.time_stop_hours = 48
        self.tp1_close_ratio = Decimal('0.25')
        self.tp2_close_ratio = Decimal('0.25')

        # 频率控制参数（子类覆盖）
        self.max_daily_trades = 6
        self.symbol_cooldown_hours = 6  # v6.16默认，v6.16.1会覆盖
        self.daily_trade_count = {}
        self.symbol_last_trade_time = {}

        # 统计信息
        self.filter_stats = {
            'total_signals': 0,
            'adx_filtered': 0,
            'volume_filtered': 0,
            'atr_percent_filtered': 0,
            'trend_alignment_filtered': 0,
            'market_state_filtered': 0,
            'score_filtered': 0,
            'cooldown_filtered': 0,
            'daily_limit_filtered': 0,
            'grade_a_rsi_filtered': 0,
            'grade_a_macd_filtered': 0,
            'grade_s_downgraded': 0,
            'opened_positions': 0
        }

    def load_klines_from_csv(self, interval: str) -> pd.DataFrame:
        """从CSV文件加载K线数据"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
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

    def calculate_ema21(self, data: pd.DataFrame) -> pd.Series:
        """计算EMA21"""
        return data['close'].ewm(span=21, adjust=False).mean()

    def calculate_ema21_slope(self, df_4h: pd.DataFrame) -> float:
        """计算4小时EMA21斜率"""
        ema21_series = self.calculate_ema21(df_4h)
        ema21_recent = ema21_series.iloc[-5:]

        if len(ema21_recent) < 5 or ema21_recent.isna().any():
            return 0.0

        x = np.arange(5)
        y = ema21_recent.values
        slope = np.polyfit(x, y, 1)[0]

        return slope

    def is_trend_aligned_v616(
        self,
        df_4h: pd.DataFrame,
        df_1d: pd.DataFrame,
        direction: str
    ) -> Tuple[bool, str]:
        """v6.16趋势一致性过滤"""
        ema21_1d = self.calculate_ema21(df_1d)
        close_1d = Decimal(str(df_1d['close'].iloc[-1]))
        ema21_1d_value = Decimal(str(ema21_1d.iloc[-1]))
        slope = self.calculate_ema21_slope(df_4h)

        if direction == 'LONG':
            daily_ok = close_1d > ema21_1d_value
            if not daily_ok:
                return False, "日线趋势不一致"
            h4_ok = slope > 0
            if not h4_ok:
                return False, "4小时趋势不一致"
        else:
            daily_ok = close_1d < ema21_1d_value
            if not daily_ok:
                return False, "日线趋势不一致"
            h4_ok = slope < 0
            if not h4_ok:
                return False, "4小时趋势不一致"

        return True, "趋势一致"

    def is_market_state_valid(self, indicators_4h: pd.DataFrame) -> bool:
        """判断市场状态是否适合交易"""
        atr_series = indicators_4h['ATR'].iloc[-20:]

        if len(atr_series) < 20 or atr_series.isna().any():
            return False

        atr_5_mean = Decimal(str(atr_series.iloc[-5:].mean()))
        atr_20_mean = Decimal(str(atr_series.iloc[-20:].mean()))

        return atr_5_mean >= atr_20_mean * Decimal('0.8')

    def check_cooldown_and_daily_limit(
        self,
        current_time,
        symbol: str
    ) -> Tuple[bool, str]:
        """检查冷却期和每日交易限制"""
        if symbol in self.symbol_last_trade_time:
            last_trade_time = self.symbol_last_trade_time[symbol]
            hours_since_last_trade = (current_time - last_trade_time).total_seconds() / 3600
            if hours_since_last_trade < self.symbol_cooldown_hours:
                return False, "冷却期未结束"

        current_date = current_time.date()
        if current_date in self.daily_trade_count:
            if self.daily_trade_count[current_date] >= self.max_daily_trades:
                return False, "今日交易次数已达上限"

        return True, "通过"

    def get_atr_percent_range(self) -> Tuple[float, float]:
        """获取ATR%范围（子类覆盖）"""
        return (1.0, 7.0)  # v6.16默认

    def get_volume_multiplier(self, grade: str) -> float:
        """获取成交量倍数（子类覆盖）"""
        volume_multipliers = {
            'S': 1.5,
            'A': 1.5,
            'B': 0.0,
            'C': 0.0
        }
        return volume_multipliers.get(grade, 0.0)

    def is_s_grade_extra_condition(self, indicators_4h: pd.DataFrame) -> bool:
        """S级额外过滤条件（子类覆盖）"""
        return True  # v6.16默认无额外过滤

    def check_grade_a_filters(
        self,
        grade: str,
        rsi_4h: float,
        volume_ratio: float,
        macd_hist: float,
        direction: str
    ) -> Tuple[bool, str]:
        """A级信号额外过滤"""
        if grade != 'A':
            return True, "非A级信号"

        if pd.isna(rsi_4h) or not (40 <= rsi_4h <= 60):
            return False, "RSI不在40-60区间"

        if volume_ratio < 1.5:  # v6.16默认
            return False, "成交量不足"

        if pd.isna(macd_hist):
            return False, "MACD数据缺失"

        if direction == 'LONG' and macd_hist <= 0:
            return False, "MACD柱与趋势方向不一致"

        if direction == 'SHORT' and macd_hist >= 0:
            return False, "MACD柱与趋势方向不一致"

        return True, "A级信号过滤通过"

    def run_backtest(
        self,
        klines_1h: pd.DataFrame,
        klines_4h: pd.DataFrame,
        klines_1d: pd.DataFrame
    ) -> Dict:
        """运行回测"""
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
                self._check_and_close_position(
                    position,
                    current_time,
                    current_price,
                    current_high,
                    current_low
                )

            self._check_and_open_position(
                current_time,
                current_price,
                indicators_1h.iloc[:i+1],
                indicators_4h.iloc[:i+1],
                indicators_1d.iloc[:i+1],
                klines_4h.iloc[:i//4+1] if i >= 4 else klines_4h.iloc[:1],
                klines_1d.iloc[:i//24+1] if i >= 24 else klines_1d.iloc[:1]
            )

        for position in self.positions[:]:
            self._force_close_position(
                position,
                klines_1h.index[-1],
                klines_1h['close'].iloc[-1]
            )

        return self._calculate_results()

    def _check_and_open_position(
        self,
        current_time,
        current_price: Decimal,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame,
        indicators_1d: pd.DataFrame,
        klines_4h: pd.DataFrame,
        klines_1d: pd.DataFrame
    ):
        """检查并开仓"""
        self.filter_stats['total_signals'] += 1

        # 1. ADX过滤
        adx = indicators_1h['ADX'].iloc[-1]
        if pd.isna(adx) or adx < 12:
            self.filter_stats['adx_filtered'] += 1
            return

        # 2. ATR%范围过滤
        atr = Decimal(str(indicators_1h['ATR'].iloc[-1]))
        if atr == 0:
            return
        atr_percent = float(atr / current_price * 100)
        atr_min, atr_max = self.get_atr_percent_range()
        if atr_percent < atr_min or atr_percent > atr_max:
            self.filter_stats['atr_percent_filtered'] += 1
            return

        # 3. 市场状态识别
        if not self.is_market_state_valid(indicators_4h):
            self.filter_stats['market_state_filtered'] += 1
            return

        # 计算评分
        score = self._calculate_score(indicators_1h, indicators_4h, indicators_1d)

        # 4. 评分过滤
        if score < 55:
            self.filter_stats['score_filtered'] += 1
            return

        # 确定等级
        if score >= 85:
            grade = 'S'
        elif score >= 78:
            grade = 'A'
        elif score >= 65:
            grade = 'B'
        else:
            grade = 'C'

        original_grade = grade

        # 5. S级额外过滤
        if grade == 'S' and not self.is_s_grade_extra_condition(indicators_4h):
            grade = 'A'
            self.filter_stats['grade_s_downgraded'] += 1

        # 6. 成交量过滤
        volume = indicators_1h['volume'].iloc[-1]
        volume_ma = indicators_1h['Volume_MA'].iloc[-1]
        volume_multiplier = self.get_volume_multiplier(grade)

        if volume_multiplier > 0:
            if pd.isna(volume) or pd.isna(volume_ma) or volume < volume_ma * volume_multiplier:
                self.filter_stats['volume_filtered'] += 1
                return
            volume_ratio = volume / volume_ma
        else:
            volume_ratio = volume / volume_ma if pd.notna(volume) and pd.notna(volume_ma) else 0.0

        # 确定方向
        direction = self._determine_direction(indicators_1h, indicators_4h)

        # 7. 趋势一致性过滤
        trend_aligned, _ = self.is_trend_aligned_v616(klines_4h, klines_1d, direction)
        if not trend_aligned:
            self.filter_stats['trend_alignment_filtered'] += 1
            return

        # 8. 冷却期和每日限制检查
        cooldown_ok, cooldown_reason = self.check_cooldown_and_daily_limit(current_time, 'BTCUSDT')
        if not cooldown_ok:
            if '冷却期' in cooldown_reason:
                self.filter_stats['cooldown_filtered'] += 1
            else:
                self.filter_stats['daily_limit_filtered'] += 1
            return

        # 9. A级信号额外过滤
        rsi_4h = indicators_4h['RSI'].iloc[-1]
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

        # 计算仓位
        position_ratio = Decimal(str(self.binance_config['position_ratio'][grade]))
        leverage = self.binance_config['leverage'][grade]
        position_size = self.current_capital * position_ratio
        quantity = position_size / current_price

        # 创建持仓
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
        position.symbol = 'BTCUSDT'

        if direction == 'LONG':
            position.stop_loss = current_price - atr * self.stop_loss_atr
            position.tp1_price = current_price + atr * self.tp1_atr_multiplier
            position.tp2_price = current_price + atr * self.tp2_atr_multiplier
        else:
            position.stop_loss = current_price + atr * self.stop_loss_atr
            position.tp1_price = current_price - atr * self.tp1_atr_multiplier
            position.tp2_price = current_price - atr * self.tp2_atr_multiplier

        self.positions.append(position)
        self.filter_stats['opened_positions'] += 1

        self.symbol_last_trade_time['BTCUSDT'] = current_time
        current_date = current_time.date()
        if current_date not in self.daily_trade_count:
            self.daily_trade_count[current_date] = 0
        self.daily_trade_count[current_date] += 1

    def _check_and_close_position(
        self,
        position: Position,
        current_time,
        current_price: Decimal,
        current_high: Decimal,
        current_low: Decimal
    ):
        """检查并平仓"""
        if not position:
            return

        close_reason = None
        close_price = None

        if position.direction == 'LONG':
            if current_low <= position.stop_loss:
                close_reason = "止损"
                close_price = position.stop_loss
            elif not position.tp1_hit and current_high >= position.tp1_price:
                position.tp1_hit = True
                close_quantity = position.quantity * self.tp1_close_ratio
                pnl = (position.tp1_price - position.entry_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity
            elif not position.tp2_hit and current_high >= position.tp2_price:
                position.tp2_hit = True
                close_quantity = position.quantity * self.tp2_close_ratio
                pnl = (position.tp2_price - position.entry_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity

            if not close_reason:
                position.highest_price = max(position.highest_price, current_high)
                profit_atr = (position.highest_price - position.entry_price) / position.atr
                if profit_atr >= self.trailing_activation_atr:
                    position.trailing_activated = True

                if position.trailing_activated:
                    trailing_stop = position.highest_price - position.atr * self.trailing_stop_atr
                    if current_low <= trailing_stop:
                        close_reason = "吊灯止损"
                        close_price = trailing_stop

            if not close_reason:
                holding_hours = (current_time - position.entry_time).total_seconds() / 3600
                if holding_hours >= self.time_stop_hours and not position.tp1_hit:
                    close_quantity = position.quantity * Decimal('0.5')
                    pnl = (current_price - position.entry_price) * close_quantity
                    self.current_capital += pnl
                    position.quantity -= close_quantity
                    close_reason = "时间止损"
                    close_price = current_price

        else:
            if current_high >= position.stop_loss:
                close_reason = "止损"
                close_price = position.stop_loss
            elif not position.tp1_hit and current_low <= position.tp1_price:
                position.tp1_hit = True
                close_quantity = position.quantity * self.tp1_close_ratio
                pnl = (position.entry_price - position.tp1_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity
            elif not position.tp2_hit and current_low <= position.tp2_price:
                position.tp2_hit = True
                close_quantity = position.quantity * self.tp2_close_ratio
                pnl = (position.entry_price - position.tp2_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity

            if not close_reason:
                position.lowest_price = min(position.lowest_price, current_low)
                profit_atr = (position.entry_price - position.lowest_price) / position.atr
                if profit_atr >= self.trailing_activation_atr:
                    position.trailing_activated = True

                if position.trailing_activated:
                    trailing_stop = position.lowest_price + position.atr * self.trailing_stop_atr
                    if current_high >= trailing_stop:
                        close_reason = "吊灯止损"
                        close_price = trailing_stop

            if not close_reason:
                holding_hours = (current_time - position.entry_time).total_seconds() / 3600
                if holding_hours >= self.time_stop_hours and not position.tp1_hit:
                    close_quantity = position.quantity * Decimal('0.5')
                    pnl = (position.entry_price - current_price) * close_quantity
                    self.current_capital += pnl
                    position.quantity -= close_quantity
                    close_reason = "时间止损"
                    close_price = current_price

        if close_reason and position.quantity > 0:
            self._close_position(position, current_time, close_price, close_reason)

    def _close_position(
        self,
        position: Position,
        current_time,
        close_price: Decimal,
        reason: str
    ):
        """平仓"""
        if not position or position.quantity <= 0:
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
            'original_grade': position.original_grade,
            'position_size': float(position.position_size),
            'leverage': position.leverage,
            'pnl': float(pnl),
            'pnl_percent': float(pnl / position.position_size * 100),
            'close_reason': reason,
            'score': position.score,
            'holding_hours': (current_time - position.entry_time).total_seconds() / 3600
        })

        if position in self.positions:
            self.positions.remove(position)

    def _force_close_position(self, position: Position, current_time, close_price):
        """强制平仓"""
        if position:
            self._close_position(position, current_time, Decimal(str(close_price)), "回测结束")

    def _calculate_score(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame,
        indicators_1d: pd.DataFrame
    ) -> float:
        """计算信号评分"""
        score = 0.0

        # 趋势强度（20分）
        trend_score = self._calculate_trend_strength(indicators_1h, indicators_4h, indicators_1d)
        score += trend_score

        # 形态质量（50分）
        pattern_score = self._calculate_pattern_quality(indicators_1h, indicators_4h)
        score += pattern_score

        # 动量背离（30分）
        momentum_score = self._calculate_momentum_divergence(indicators_1h, indicators_4h)
        score += momentum_score

        return score

    def _calculate_trend_strength(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame,
        indicators_1d: pd.DataFrame
    ) -> float:
        """计算趋势强度评分"""
        score = 0.0

        ma21_1h = indicators_1h['MA21'].iloc[-1]
        ma55_1h = indicators_1h['MA55'].iloc[-1]
        if pd.notna(ma21_1h) and pd.notna(ma55_1h):
            if ma21_1h > ma55_1h or ma21_1h < ma55_1h:
                score += 8

        adx = indicators_1h['ADX'].iloc[-1]
        if pd.notna(adx):
            if adx >= 25:
                score += 6
            elif adx >= 20:
                score += 4
            elif adx >= 15:
                score += 3
            elif adx >= 12:
                score += 2

        ma21_4h = indicators_4h['MA21'].iloc[-1]
        ma55_4h = indicators_4h['MA55'].iloc[-1]
        ma21_1d = indicators_1d['MA21'].iloc[-1]
        ma55_1d = indicators_1d['MA55'].iloc[-1]

        if pd.notna(ma21_4h) and pd.notna(ma55_4h) and pd.notna(ma21_1d) and pd.notna(ma55_1d):
            if (ma21_4h > ma55_4h and ma21_1d > ma55_1d) or \
               (ma21_4h < ma55_4h and ma21_1d < ma55_1d):
                score += 6

        return min(score, 20.0)

    def _calculate_pattern_quality(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame
    ) -> float:
        """计算形态质量评分"""
        score = 0.0

        macd = indicators_1h['MACD'].iloc[-1]
        macd_signal = indicators_1h['MACD_Signal'].iloc[-1]
        macd_hist = indicators_1h['MACD_Hist'].iloc[-1]

        if pd.notna(macd) and pd.notna(macd_signal):
            if macd > macd_signal and macd_hist > 0:
                score += 20
            elif macd < macd_signal and macd_hist < 0:
                score += 20
            elif macd > 0 or macd < 0:
                score += 10

        if pd.notna(macd_hist):
            if macd_hist > 0 or macd_hist < 0:
                score += 10

            macd_hist_prev = indicators_1h['MACD_Hist'].iloc[-2] if len(indicators_1h) > 1 else None
            if pd.notna(macd_hist_prev) and abs(macd_hist) > abs(macd_hist_prev):
                score += 5

        volume = indicators_1h['volume'].iloc[-1] if 'volume' in indicators_1h.columns else None
        volume_ma = indicators_1h['Volume_MA'].iloc[-1]

        if pd.notna(volume) and pd.notna(volume_ma):
            volume_ratio = volume / volume_ma
            if volume_ratio >= 1.5:
                score += 15
            elif volume_ratio >= 1.2:
                score += 10
            elif volume_ratio >= 1.0:
                score += 5

        return min(score, 50.0)

    def _calculate_momentum_divergence(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame
    ) -> float:
        """计算动量背离评分"""
        score = 0.0

        rsi_1h = indicators_1h['RSI'].iloc[-1]
        rsi_4h = indicators_4h['RSI'].iloc[-1]

        if pd.notna(rsi_1h):
            if 40 < rsi_1h < 60:
                score += 12
            elif 30 < rsi_1h <= 40 or 60 <= rsi_1h < 70:
                score += 8
            elif rsi_1h <= 30 or rsi_1h >= 70:
                score += 4

        if pd.notna(rsi_4h):
            if 40 < rsi_4h < 60:
                score += 6
            elif 30 < rsi_4h <= 40 or 60 <= rsi_4h < 70:
                score += 3

        macd_hist = indicators_1h['MACD_Hist'].iloc[-1]
        macd_hist_prev = indicators_1h['MACD_Hist'].iloc[-2] if len(indicators_1h) > 1 else None

        if pd.notna(macd_hist):
            if pd.notna(macd_hist_prev):
                if macd_hist > macd_hist_prev:
                    score += 12
                else:
                    score += 6
            else:
                score += 6

        return min(score, 30.0)

    def _determine_direction(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame
    ) -> str:
        """确定交易方向"""
        long_votes = 0
        short_votes = 0

        ma21_1h = indicators_1h['MA21'].iloc[-1]
        ma55_1h = indicators_1h['MA55'].iloc[-1]
        if pd.notna(ma21_1h) and pd.notna(ma55_1h):
            if ma21_1h > ma55_1h:
                long_votes += 1
            else:
                short_votes += 1

        macd = indicators_1h['MACD'].iloc[-1]
        macd_signal = indicators_1h['MACD_Signal'].iloc[-1]
        if pd.notna(macd) and pd.notna(macd_signal):
            if macd > macd_signal:
                long_votes += 1
            else:
                short_votes += 1

        ma21_4h = indicators_4h['MA21'].iloc[-1]
        ma55_4h = indicators_4h['MA55'].iloc[-1]
        if pd.notna(ma21_4h) and pd.notna(ma55_4h):
            if ma21_4h > ma55_4h:
                long_votes += 1
            else:
                short_votes += 1

        return 'LONG' if long_votes > short_votes else 'SHORT'

    def _calculate_results(self) -> Dict:
        """计算回测结果"""
        if not self.trades:
            return {
                'version': self.version,
                'initial_capital': float(self.initial_capital),
                'final_capital': float(self.current_capital),
                'total_return': 0.0,
                'total_trades': 0,
                'win_trades': 0,
                'loss_trades': 0,
                'win_rate': 0.0,
                'avg_pnl_percent': 0.0,
                'max_drawdown': 0.0,
                'sharpe_ratio': 0.0,
                'trades': [],
                'filter_stats': self.filter_stats,
                'grade_stats': {},
                'close_reason_stats': {},
                'monthly_stats': {}
            }

        total_trades = len(self.trades)
        win_trades = sum(1 for t in self.trades if t['pnl'] > 0)
        loss_trades = sum(1 for t in self.trades if t['pnl'] <= 0)
        win_rate = win_trades / total_trades * 100 if total_trades > 0 else 0
        avg_pnl_percent = np.mean([t['pnl_percent'] for t in self.trades])

        capital_curve = [float(self.initial_capital)]
        for trade in self.trades:
            capital_curve.append(capital_curve[-1] + trade['pnl'])

        max_drawdown = 0.0
        peak = capital_curve[0]
        for capital in capital_curve:
            if capital > peak:
                peak = capital
            drawdown = (peak - capital) / peak * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        returns = [t['pnl_percent'] for t in self.trades]
        sharpe_ratio = np.mean(returns) / np.std(returns) if np.std(returns) > 0 else 0

        grade_stats = {}
        for grade in ['S', 'A', 'B', 'C']:
            grade_trades = [t for t in self.trades if t['grade'] == grade]
            if grade_trades:
                grade_stats[grade] = {
                    'count': len(grade_trades),
                    'win_rate': sum(1 for t in grade_trades if t['pnl'] > 0) / len(grade_trades) * 100,
                    'avg_pnl_percent': np.mean([t['pnl_percent'] for t in grade_trades]),
                    'total_pnl': sum(t['pnl'] for t in grade_trades)
                }

        close_reason_stats = {}
        for trade in self.trades:
            reason = trade['close_reason']
            if reason not in close_reason_stats:
                close_reason_stats[reason] = {'count': 0, 'total_pnl': 0}
            close_reason_stats[reason]['count'] += 1
            close_reason_stats[reason]['total_pnl'] += trade['pnl']

        monthly_stats = {}
        for trade in self.trades:
            month = trade['entry_time'].strftime('%Y-%m')
            if month not in monthly_stats:
                monthly_stats[month] = {
                    'count': 0,
                    'win_count': 0,
                    'total_pnl': 0
                }
            monthly_stats[month]['count'] += 1
            if trade['pnl'] > 0:
                monthly_stats[month]['win_count'] += 1
            monthly_stats[month]['total_pnl'] += trade['pnl']

        return {
            'version': self.version,
            'initial_capital': float(self.initial_capital),
            'final_capital': float(self.current_capital),
            'total_return': float((self.current_capital - self.initial_capital) / self.initial_capital * 100),
            'total_trades': total_trades,
            'win_trades': win_trades,
            'loss_trades': loss_trades,
            'win_rate': win_rate,
            'avg_pnl_percent': avg_pnl_percent,
            'max_drawdown': max_drawdown,
            'sharpe_ratio': sharpe_ratio,
            'trades': self.trades,
            'filter_stats': self.filter_stats,
            'grade_stats': grade_stats,
            'close_reason_stats': close_reason_stats,
            'monthly_stats': monthly_stats
        }


class BacktestEngineV616(BacktestEngineBase):
    """v6.16回测引擎"""

    def __init__(self, config: Dict):
        super().__init__(config, 'v6.16')
        self.symbol_cooldown_hours = 6  # v6.16: 6小时冷却期

    def get_atr_percent_range(self) -> Tuple[float, float]:
        """v6.16: ATR%范围1.0%-7.0%"""
        return (1.0, 7.0)

    def get_volume_multiplier(self, grade: str) -> float:
        """v6.16: S≥1.5，A≥1.5"""
        volume_multipliers = {
            'S': 1.5,
            'A': 1.5,
            'B': 0.0,
            'C': 0.0
        }
        return volume_multipliers.get(grade, 0.0)

    def is_s_grade_extra_condition(self, indicators_4h: pd.DataFrame) -> bool:
        """v6.16: 无S级额外过滤"""
        return True

    def check_grade_a_filters(
        self,
        grade: str,
        rsi_4h: float,
        volume_ratio: float,
        macd_hist: float,
        direction: str
    ) -> Tuple[bool, str]:
        """v6.16: A级过滤，成交量≥1.5"""
        if grade != 'A':
            return True, "非A级信号"

        if pd.isna(rsi_4h) or not (40 <= rsi_4h <= 60):
            return False, "RSI不在40-60区间"

        if volume_ratio < 1.5:
            return False, "成交量不足"

        if pd.isna(macd_hist):
            return False, "MACD数据缺失"

        if direction == 'LONG' and macd_hist <= 0:
            return False, "MACD柱与趋势方向不一致"

        if direction == 'SHORT' and macd_hist >= 0:
            return False, "MACD柱与趋势方向不一致"

        return True, "A级信号过滤通过"


class BacktestEngineV6161(BacktestEngineBase):
    """v6.16.1回测引擎"""

    def __init__(self, config: Dict):
        super().__init__(config, 'v6.16.1')
        self.symbol_cooldown_hours = 4  # v6.16.1: 4小时冷却期

    def get_atr_percent_range(self) -> Tuple[float, float]:
        """v6.16.1: ATR%范围1.0%-8.5%"""
        return (1.0, 8.5)

    def get_volume_multiplier(self, grade: str) -> float:
        """v6.16.1: S≥1.4，A≥1.3"""
        volume_multipliers = {
            'S': 1.4,
            'A': 1.3,
            'B': 0.0,
            'C': 0.0
        }
        return volume_multipliers.get(grade, 0.0)

    def is_s_grade_extra_condition(self, indicators_4h: pd.DataFrame) -> bool:
        """v6.16.1: S级额外过滤"""
        # 条件1: 4h ADX > 25
        adx = indicators_4h['ADX'].iloc[-1]
        if pd.notna(adx) and adx > 25:
            return True

        # 条件2: MACD柱状线连续3根放大
        macd_hist = indicators_4h['MACD_Hist'].iloc[-3:]
        if len(macd_hist) >= 3 and not macd_hist.isna().any():
            if abs(macd_hist.iloc[-1]) > abs(macd_hist.iloc[-2]) > abs(macd_hist.iloc[-3]):
                return True

        return False

    def check_grade_a_filters(
        self,
        grade: str,
        rsi_4h: float,
        volume_ratio: float,
        macd_hist: float,
        direction: str
    ) -> Tuple[bool, str]:
        """v6.16.1: A级过滤，成交量≥1.3"""
        if grade != 'A':
            return True, "非A级信号"

        if pd.isna(rsi_4h) or not (40 <= rsi_4h <= 60):
            return False, "RSI不在40-60区间"

        if volume_ratio < 1.3:
            return False, "成交量不足"

        if pd.isna(macd_hist):
            return False, "MACD数据缺失"

        if direction == 'LONG' and macd_hist <= 0:
            return False, "MACD柱与趋势方向不一致"

        if direction == 'SHORT' and macd_hist >= 0:
            return False, "MACD柱与趋势方向不一致"

        return True, "A级信号过滤通过"


def print_comparison_report(results_v616: Dict, results_v6161: Dict):
    """打印对比报告"""
    print(f"""
# v6.16 vs v6.16.1 回测对比报告

## 一、核心参数差异对比

| 参数 | v6.16 | v6.16.1 | 变化 |
|------|-------|---------|------|
| ATR%上限 | 7.0% | 8.5% | 放宽1.5% |
| 成交量倍数(S级) | 1.5 | 1.4 | 降低0.1 |
| 成交量倍数(A级) | 1.5 | 1.3 | 降低0.2 |
| 冷却期 | 6小时 | 4小时 | 缩短2小时 |
| S级额外过滤 | 无 | ADX>25或MACD连续放大 | 新增 |

## 二、整体表现对比

### 资金情况
| 指标 | v6.16 | v6.16.1 | 差异 |
|------|-------|---------|------|
| 初始资金 | {results_v616['initial_capital']:.2f} USDT | {results_v6161['initial_capital']:.2f} USDT | - |
| 最终资金 | {results_v616['final_capital']:.2f} USDT | {results_v6161['final_capital']:.2f} USDT | {results_v6161['final_capital'] - results_v616['final_capital']:+.2f} USDT |
| 总收益率 | {results_v616['total_return']:.2f}% | {results_v6161['total_return']:.2f}% | {results_v6161['total_return'] - results_v616['total_return']:+.2f}% |
| 最大回撤 | {results_v616['max_drawdown']:.2f}% | {results_v6161['max_drawdown']:.2f}% | {results_v6161['max_drawdown'] - results_v616['max_drawdown']:+.2f}% |
| 夏普比率 | {results_v616['sharpe_ratio']:.2f} | {results_v6161['sharpe_ratio']:.2f} | {results_v6161['sharpe_ratio'] - results_v616['sharpe_ratio']:+.2f} |

### 交易统计
| 指标 | v6.16 | v6.16.1 | 差异 |
|------|-------|---------|------|
| 总交易次数 | {results_v616['total_trades']} | {results_v6161['total_trades']} | {results_v6161['total_trades'] - results_v616['total_trades']:+d} |
| 盈利次数 | {results_v616['win_trades']} | {results_v6161['win_trades']} | {results_v6161['win_trades'] - results_v616['win_trades']:+d} |
| 亏损次数 | {results_v616['loss_trades']} | {results_v6161['loss_trades']} | {results_v6161['loss_trades'] - results_v616['loss_trades']:+d} |
| 胜率 | {results_v616['win_rate']:.2f}% | {results_v6161['win_rate']:.2f}% | {results_v6161['win_rate'] - results_v616['win_rate']:+.2f}% |
| 平均盈亏 | {results_v616['avg_pnl_percent']:.2f}% | {results_v6161['avg_pnl_percent']:.2f}% | {results_v6161['avg_pnl_percent'] - results_v616['avg_pnl_percent']:+.2f}% |

## 三、过滤器效果对比

| 过滤器 | v6.16 | v6.16.1 | 差异 |
|--------|-------|---------|------|
| 总信号数 | {results_v616['filter_stats']['total_signals']} | {results_v6161['filter_stats']['total_signals']} | {results_v6161['filter_stats']['total_signals'] - results_v616['filter_stats']['total_signals']:+d} |
| ADX过滤 | {results_v616['filter_stats']['adx_filtered']} | {results_v6161['filter_stats']['adx_filtered']} | {results_v6161['filter_stats']['adx_filtered'] - results_v616['filter_stats']['adx_filtered']:+d} |
| 成交量过滤 | {results_v616['filter_stats']['volume_filtered']} | {results_v6161['filter_stats']['volume_filtered']} | {results_v6161['filter_stats']['volume_filtered'] - results_v616['filter_stats']['volume_filtered']:+d} |
| ATR%过滤 | {results_v616['filter_stats']['atr_percent_filtered']} | {results_v6161['filter_stats']['atr_percent_filtered']} | {results_v6161['filter_stats']['atr_percent_filtered'] - results_v616['filter_stats']['atr_percent_filtered']:+d} |
| 趋势一致性过滤 | {results_v616['filter_stats']['trend_alignment_filtered']} | {results_v6161['filter_stats']['trend_alignment_filtered']} | {results_v6161['filter_stats']['trend_alignment_filtered'] - results_v616['filter_stats']['trend_alignment_filtered']:+d} |
| 冷却期过滤 | {results_v616['filter_stats']['cooldown_filtered']} | {results_v6161['filter_stats']['cooldown_filtered']} | {results_v6161['filter_stats']['cooldown_filtered'] - results_v616['filter_stats']['cooldown_filtered']:+d} |
| 每日限额过滤 | {results_v616['filter_stats']['daily_limit_filtered']} | {results_v6161['filter_stats']['daily_limit_filtered']} | {results_v6161['filter_stats']['daily_limit_filtered'] - results_v616['filter_stats']['daily_limit_filtered']:+d} |
| A级RSI过滤 | {results_v616['filter_stats']['grade_a_rsi_filtered']} | {results_v6161['filter_stats']['grade_a_rsi_filtered']} | {results_v6161['filter_stats']['grade_a_rsi_filtered'] - results_v616['filter_stats']['grade_a_rsi_filtered']:+d} |
| A级MACD过滤 | {results_v616['filter_stats']['grade_a_macd_filtered']} | {results_v6161['filter_stats']['grade_a_macd_filtered']} | {results_v6161['filter_stats']['grade_a_macd_filtered'] - results_v616['filter_stats']['grade_a_macd_filtered']:+d} |
| 市场状态过滤 | {results_v616['filter_stats']['market_state_filtered']} | {results_v6161['filter_stats']['market_state_filtered']} | {results_v6161['filter_stats']['market_state_filtered'] - results_v616['filter_stats']['market_state_filtered']:+d} |
| 评分过滤 | {results_v616['filter_stats']['score_filtered']} | {results_v6161['filter_stats']['score_filtered']} | {results_v6161['filter_stats']['score_filtered'] - results_v616['filter_stats']['score_filtered']:+d} |
| S级降级 | {results_v616['filter_stats']['grade_s_downgraded']} | {results_v6161['filter_stats']['grade_s_downgraded']} | {results_v6161['filter_stats']['grade_s_downgraded'] - results_v616['filter_stats']['grade_s_downgraded']:+d} |
| 最终开仓 | {results_v616['filter_stats']['opened_positions']} | {results_v6161['filter_stats']['opened_positions']} | {results_v6161['filter_stats']['opened_positions'] - results_v616['filter_stats']['opened_positions']:+d} |
""")

    # 按等级统计对比
    print("""
## 四、按等级统计对比
""")
    for grade in ['S', 'A', 'B', 'C']:
        v616_stats = results_v616['grade_stats'].get(grade, {})
        v6161_stats = results_v6161['grade_stats'].get(grade, {})

        if v616_stats or v6161_stats:
            print(f"""
### {grade}级信号
| 指标 | v6.16 | v6.16.1 | 差异 |
|------|-------|---------|------|
| 交易次数 | {v616_stats.get('count', 0)} | {v6161_stats.get('count', 0)} | {v6161_stats.get('count', 0) - v616_stats.get('count', 0):+d} |
| 胜率 | {v616_stats.get('win_rate', 0):.2f}% | {v6161_stats.get('win_rate', 0):.2f}% | {v6161_stats.get('win_rate', 0) - v616_stats.get('win_rate', 0):+.2f}% |
| 平均盈亏 | {v616_stats.get('avg_pnl_percent', 0):.2f}% | {v6161_stats.get('avg_pnl_percent', 0):.2f}% | {v6161_stats.get('avg_pnl_percent', 0) - v616_stats.get('avg_pnl_percent', 0):+.2f}% |
| 总盈亏 | {v616_stats.get('total_pnl', 0):.2f} USDT | {v6161_stats.get('total_pnl', 0):.2f} USDT | {v6161_stats.get('total_pnl', 0) - v616_stats.get('total_pnl', 0):+.2f} USDT |
""")

    # 平仓原因对比
    print("""
## 五、按平仓原因对比
""")
    all_reasons = set(results_v616['close_reason_stats'].keys()) | set(results_v6161['close_reason_stats'].keys())
    print("| 平仓原因 | v6.16次数 | v6.16盈亏 | v6.16.1次数 | v6.16.1盈亏 |")
    print("|----------|-----------|-----------|-------------|-------------|")
    for reason in sorted(all_reasons):
        v616_stats = results_v616['close_reason_stats'].get(reason, {'count': 0, 'total_pnl': 0})
        v6161_stats = results_v6161['close_reason_stats'].get(reason, {'count': 0, 'total_pnl': 0})
        print(f"| {reason} | {v616_stats['count']} | {v616_stats['total_pnl']:.2f} USDT | {v6161_stats['count']} | {v6161_stats['total_pnl']:.2f} USDT |")

    # 月度统计对比
    print("""
## 六、月度统计对比
""")
    all_months = set(results_v616['monthly_stats'].keys()) | set(results_v6161['monthly_stats'].keys())
    print("| 月份 | v6.16笔数 | v6.16胜率 | v6.16盈亏 | v6.16.1笔数 | v6.16.1胜率 | v6.16.1盈亏 |")
    print("|------|-----------|-----------|-----------|-------------|-------------|-------------|")
    for month in sorted(all_months):
        v616_stats = results_v616['monthly_stats'].get(month, {'count': 0, 'win_count': 0, 'total_pnl': 0})
        v6161_stats = results_v6161['monthly_stats'].get(month, {'count': 0, 'win_count': 0, 'total_pnl': 0})

        v616_win_rate = v616_stats['win_count'] / v616_stats['count'] * 100 if v616_stats['count'] > 0 else 0
        v6161_win_rate = v6161_stats['win_count'] / v6161_stats['count'] * 100 if v6161_stats['count'] > 0 else 0

        print(f"| {month} | {v616_stats['count']} | {v616_win_rate:.2f}% | {v616_stats['total_pnl']:.2f} USDT | {v6161_stats['count']} | {v6161_win_rate:.2f}% | {v6161_stats['total_pnl']:.2f} USDT |")

    print("""
## 七、关键发现与建议

### 改进效果分析
1. **ATR%范围放宽**：上限从7.0%放宽至8.5%，预计会捕获更多高波动机会
2. **S级额外过滤**：新增ADX>25或MACD连续放大条件，提高S级信号质量
3. **成交量要求降低**：S级从1.5降至1.4，A级从1.5降至1.3，增加交易机会
4. **冷却期缩短**：从6小时缩短至4小时，提高资金利用率

### 风险提示
- ATR%上限放宽可能增加高波动风险
- 冷却期缩短可能导致过度交易
- S级降级机制可能改变等级分布

### 建议
根据回测结果，评估是否需要进一步调整参数。
""")


def main():
    """主函数"""
    # 加载配置
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
    config_path = os.path.join(project_root, 'strategies/btc_eth/config.yaml')

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # 创建两个版本的回测引擎
    engine_v616 = BacktestEngineV616(config)
    engine_v6161 = BacktestEngineV6161(config)

    # 加载数据
    klines_1h = engine_v616.load_klines_from_csv('1h')
    klines_4h = engine_v616.load_klines_from_csv('4h')
    klines_1d = engine_v616.load_klines_from_csv('1d')

    # 运行v6.16回测
    print("正在运行v6.16回测...")
    results_v616 = engine_v616.run_backtest(klines_1h, klines_4h, klines_1d)

    # 运行v6.16.1回测
    print("正在运行v6.16.1回测...")
    results_v6161 = engine_v6161.run_backtest(klines_1h, klines_4h, klines_1d)

    # 打印对比报告
    print_comparison_report(results_v616, results_v6161)


if __name__ == "__main__":
    main()
