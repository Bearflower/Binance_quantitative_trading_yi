#!/usr/bin/env python3
"""
v6.16.1方案回测脚本 - 多币种版本

基于v6.16.1原版参数，支持多币种回测

核心改进（相对于v6.16）：
1. ATR%范围放宽：上限从7.0%放宽至8.5%
2. S级额外过滤：
   - 条件1：4h ADX > 25
   - 条件2：MACD柱状线连续3根放大
   - 不满足条件时降级为A级
3. 成交量倍数降低：
   - S级：从1.5降至1.4
   - A级：从1.5降至1.3
4. 冷却期缩短：从6小时缩短至4小时

前置过滤器：
- ADX ≥ 12
- ATR%范围：1.0%-8.5%
- 成交量倍数：S≥1.4，A≥1.3，B/C不要求
- 同币种冷却期：4小时
- 每日最大交易数：6笔

评分系统：
- 趋势强度：20分
- 形态质量：50分
- 动量背离：30分
- 等级阈值：S≥85，A≥78，B≥65，C≥55

A级信号额外过滤：
- RSI 40-60
- 成交量≥1.3倍
- MACD柱与趋势方向一致

止盈止损参数：
- 止损：1.8×ATR
- TP1：3.5×ATR（平25%）
- TP2：6.0×ATR（平25%）
- 吊灯启动：2.0×ATR
- 吊灯回撤：1.5×ATR
- 时间止损：48小时未达TP1，平仓50%
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
        self.entry_time = None  # 入场时间
        self.entry_price = Decimal('0')  # 入场价格
        self.direction = None  # 方向：LONG/SHORT
        self.quantity = Decimal('0')  # 持仓数量
        self.position_size = Decimal('0')  # 仓位大小（USDT）
        self.leverage = 1  # 杠杆倍数
        self.grade = 'C'  # 信号等级
        self.original_grade = 'C'  # 原始等级（降级前）
        self.atr = Decimal('0')  # 入场时ATR
        self.tp1_price = Decimal('0')  # 第一止盈价格
        self.tp2_price = Decimal('0')  # 第二止盈价格
        self.stop_loss = Decimal('0')  # 止损价格
        self.highest_price = Decimal('0')  # 最高价格（用于吊灯止损）
        self.lowest_price = Decimal('0')  # 最低价格（用于吊灯止损）
        self.tp1_hit = False  # 是否触及TP1
        self.tp2_hit = False  # 是否触及TP2
        self.trailing_activated = False  # 吊灯止损是否启动
        self.score = 0.0  # 信号评分
        self.symbol = 'BTCUSDT'  # 交易对


class BacktestEngine:
    """回测引擎 - v6.16.1方案多币种版本"""

    def __init__(self, config: Dict):
        """
        初始化回测引擎

        Args:
            config: 配置字典
        """
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

        # v6.16.1止盈止损参数
        self.stop_loss_atr = Decimal('1.8')  # 止损：1.8×ATR
        self.tp1_atr_multiplier = Decimal('3.5')  # TP1：3.5×ATR
        self.tp2_atr_multiplier = Decimal('6.0')  # TP2：6.0×ATR
        self.trailing_activation_atr = Decimal('2.0')  # 吊灯启动：2.0×ATR
        self.trailing_stop_atr = Decimal('1.5')  # 吊灯回撤：1.5×ATR
        self.time_stop_hours = 48  # 时间止损：48小时

        # TP1和TP2平仓比例
        self.tp1_close_ratio = Decimal('0.25')  # TP1平仓25%
        self.tp2_close_ratio = Decimal('0.25')  # TP2平仓25%

        # v6.16.1频率控制参数
        self.max_daily_trades = 6  # 每日最大交易数
        self.symbol_cooldown_hours = 4  # 同币种冷却期
        self.daily_trade_count = {}  # 每日交易计数
        self.symbol_last_trade_time = {}  # 币种最后交易时间

        # 统计信息
        self.filter_stats = {
            'total_signals': 0,
            'adx_filtered': 0,
            'volume_filtered': 0,
            'atr_percent_filtered': 0,
            'trend_alignment_filtered': 0,
            'rsi_filtered': 0,
            'market_state_filtered': 0,
            'score_filtered': 0,
            'cooldown_filtered': 0,
            'daily_limit_filtered': 0,
            'grade_a_rsi_filtered': 0,
            'grade_a_macd_filtered': 0,
            'grade_s_downgraded': 0,
            'opened_positions': 0,
            'symbol_stats': {}
        }

    def load_klines_from_csv(self, symbol: str, interval: str) -> pd.DataFrame:
        """
        从CSV文件加载K线数据

        Args:
            symbol: 交易对
            interval: 时间周期（1h, 4h, 1d）

        Returns:
            K线DataFrame
        """
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
                return False, f"日线趋势不一致"
            h4_ok = slope > 0
            if not h4_ok:
                return False, f"4小时趋势不一致"
        else:
            daily_ok = close_1d < ema21_1d_value
            if not daily_ok:
                return False, f"日线趋势不一致"
            h4_ok = slope < 0
            if not h4_ok:
                return False, f"4小时趋势不一致"

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
                return False, f"币种{symbol}冷却期未结束"

        current_date = current_time.date()
        if current_date in self.daily_trade_count:
            if self.daily_trade_count[current_date] >= self.max_daily_trades:
                return False, f"今日交易次数已达上限"

        return True, "通过"

    def is_s_grade_extra_condition(self, indicators_4h: pd.DataFrame) -> bool:
        """v6.16.1 S级额外过滤条件"""
        adx = indicators_4h['ADX'].iloc[-1]
        if pd.notna(adx) and adx > 25:
            return True

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
        """A级信号额外过滤"""
        if grade != 'A':
            return True, "非A级信号"

        if pd.isna(rsi_4h) or not (40 <= rsi_4h <= 60):
            return False, f"A级信号RSI不在40-60区间"

        if volume_ratio < 1.3:
            return False, f"A级信号成交量不足"

        if pd.isna(macd_hist):
            return False, "MACD柱状图数据缺失"

        if direction == 'LONG' and macd_hist <= 0:
            return False, f"A级信号MACD柱与趋势方向不一致"

        if direction == 'SHORT' and macd_hist >= 0:
            return False, f"A级信号MACD柱与趋势方向不一致"

        return True, "A级信号过滤通过"

    def get_volume_multiplier(self, grade: str) -> float:
        """根据等级获取成交量倍数要求"""
        volume_multipliers = {
            'S': 1.4,
            'A': 1.3,
            'B': 0.0,
            'C': 0.0
        }
        return volume_multipliers.get(grade, 0.0)

    def run_backtest(
        self,
        symbols: List[str],
        klines_dict: Dict[str, Dict[str, pd.DataFrame]]
    ) -> Dict:
        """运行多币种回测"""
        # 初始化币种统计
        for symbol in symbols:
            self.filter_stats['symbol_stats'][symbol] = {'signals': 0, 'opened': 0}

        # 按时间顺序遍历所有币种
        all_timestamps = set()
        for symbol in symbols:
            if symbol in klines_dict and '1h' in klines_dict[symbol]:
                all_timestamps.update(klines_dict[symbol]['1h'].index)

        sorted_timestamps = sorted(list(all_timestamps))

        # 主回测循环
        for timestamp in sorted_timestamps:
            for symbol in symbols:
                if symbol not in klines_dict:
                    continue

                if timestamp not in klines_dict[symbol]['1h'].index:
                    continue

                idx = klines_dict[symbol]['1h'].index.get_loc(timestamp)

                if idx < 100:
                    continue

                current_price = Decimal(str(klines_dict[symbol]['1h']['close'].iloc[idx]))
                current_high = Decimal(str(klines_dict[symbol]['1h']['high'].iloc[idx]))
                current_low = Decimal(str(klines_dict[symbol]['1h']['low'].iloc[idx]))

                # 计算指标
                indicators_1h = pd.DataFrame(TechnicalIndicators.calculate_all(
                    klines_dict[symbol]['1h'].iloc[:idx+1]
                ))
                indicators_4h = pd.DataFrame(TechnicalIndicators.calculate_all(
                    klines_dict[symbol]['4h'].iloc[:idx//4+1]
                ))
                indicators_1d = pd.DataFrame(TechnicalIndicators.calculate_all(
                    klines_dict[symbol]['1d'].iloc[:idx//24+1]
                ))

                indicators_1h['volume'] = klines_dict[symbol]['1h']['volume'].iloc[:idx+1].values

                # 检查并平仓现有持仓
                for position in self.positions[:]:
                    if position.symbol == symbol:
                        self._check_and_close_position(
                            position,
                            timestamp,
                            current_price,
                            current_high,
                            current_low
                        )

                # 尝试开新仓
                self._check_and_open_position(
                    symbol,
                    timestamp,
                    current_price,
                    indicators_1h,
                    indicators_4h,
                    indicators_1d,
                    klines_dict[symbol]['4h'].iloc[:idx//4+1],
                    klines_dict[symbol]['1d'].iloc[:idx//24+1]
                )

        # 强制平仓所有剩余持仓
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
        """检查并开仓"""
        self.filter_stats['total_signals'] += 1
        self.filter_stats['symbol_stats'][symbol]['signals'] += 1

        # 1. ADX过滤：ADX ≥ 12
        adx = indicators_1h['ADX'].iloc[-1]
        if pd.isna(adx) or adx < 12:
            self.filter_stats['adx_filtered'] += 1
            return

        # 2. ATR%范围过滤：1.0%-8.5%
        atr = Decimal(str(indicators_1h['ATR'].iloc[-1]))
        if atr == 0:
            return
        atr_percent = float(atr / current_price * 100)
        if atr_percent < 1.0 or atr_percent > 8.5:
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

        # 6. 成交量放大过滤
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

        # 7. 趋势方向一致性过滤
        trend_aligned, trend_reason = self.is_trend_aligned_v616(klines_4h, klines_1d, direction)
        if not trend_aligned:
            self.filter_stats['trend_alignment_filtered'] += 1
            return

        # 8. 冷却期和每日交易限制检查
        cooldown_ok, cooldown_reason = self.check_cooldown_and_daily_limit(current_time, symbol)
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
        position.symbol = symbol

        # 设置止盈止损价格
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
        self.filter_stats['symbol_stats'][symbol]['opened'] += 1

        # 更新冷却期和每日交易计数
        self.symbol_last_trade_time[symbol] = current_time
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
            'symbol': position.symbol,
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
        score += self._calculate_trend_strength(indicators_1h, indicators_4h, indicators_1d)
        score += self._calculate_pattern_quality(indicators_1h, indicators_4h)
        score += self._calculate_momentum_divergence(indicators_1h, indicators_4h)
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
                'filter_stats': self.filter_stats
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

        symbol_stats = {}
        for symbol in self.filter_stats['symbol_stats'].keys():
            symbol_trades = [t for t in self.trades if t['symbol'] == symbol]
            if symbol_trades:
                symbol_stats[symbol] = {
                    'count': len(symbol_trades),
                    'win_rate': sum(1 for t in symbol_trades if t['pnl'] > 0) / len(symbol_trades) * 100,
                    'avg_pnl_percent': np.mean([t['pnl_percent'] for t in symbol_trades]),
                    'total_pnl': sum(t['pnl'] for t in symbol_trades)
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

        s_downgraded_trades = [t for t in self.trades if t['original_grade'] == 'S' and t['grade'] == 'A']

        return {
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
            'symbol_stats': symbol_stats,
            'close_reason_stats': close_reason_stats,
            'monthly_stats': monthly_stats,
            's_downgraded_count': len(s_downgraded_trades)
        }


def main():
    """主函数"""
    # 加载配置
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
    config_path = os.path.join(project_root, 'strategies/btc_eth/config.yaml')

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # 创建回测引擎
    engine = BacktestEngine(config)

    # 定义回测币种
    symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'TRXUSDT']

    # 最小数据量要求
    MIN_KLINES = 100

    # 加载所有币种数据
    klines_dict = {}
    for symbol in symbols:
        try:
            klines_1h = engine.load_klines_from_csv(symbol, '1h')
            klines_4h = engine.load_klines_from_csv(symbol, '4h')
            klines_1d = engine.load_klines_from_csv(symbol, '1d')

            if len(klines_1h) < MIN_KLINES:
                print(f"警告：{symbol} 1h数据量不足（{len(klines_1h)}条 < {MIN_KLINES}条），跳过该币种")
                continue

            if len(klines_4h) < MIN_KLINES // 4:
                print(f"警告：{symbol} 4h数据量不足（{len(klines_4h)}条 < {MIN_KLINES // 4}条），跳过该币种")
                continue

            if len(klines_1d) < MIN_KLINES // 24:
                print(f"警告：{symbol} 1d数据量不足（{len(klines_1d)}条 < {MIN_KLINES // 24}条），跳过该币种")
                continue

            klines_dict[symbol] = {
                '1h': klines_1h,
                '4h': klines_4h,
                '1d': klines_1d
            }
            print(f"成功加载 {symbol} 数据（1h: {len(klines_1h)}条, 4h: {len(klines_4h)}条, 1d: {len(klines_1d)}条）")
        except Exception as e:
            print(f"警告：无法加载 {symbol} 数据，跳过该币种: {e}")

    # 如果没有加载到任何数据，使用BTC数据
    if not klines_dict:
        print("未找到多币种数据，使用BTC数据进行回测")
        symbols = ['BTCUSDT']
        klines_dict['BTCUSDT'] = {
            '1h': engine.load_klines_from_csv('BTCUSDT', '1h'),
            '4h': engine.load_klines_from_csv('BTCUSDT', '4h'),
            '1d': engine.load_klines_from_csv('BTCUSDT', '1d')
        }

    # 运行回测
    results = engine.run_backtest(list(klines_dict.keys()), klines_dict)

    # 输出报告
    print_report(results, list(klines_dict.keys()))


def print_report(results: Dict, symbols: List[str]):
    """打印回测报告"""
    print(f"""
# v6.16.1方案回测结果 - 多币种版本

## 一、方案参数配置

### 核心改进（相对于v6.16）
1. **ATR%范围放宽**
   - 上限从7.0%放宽至8.5%
   - 下限保持1.0%

2. **S级额外过滤**
   - 条件1：4h ADX > 25
   - 条件2：MACD柱状线连续3根放大
   - 不满足条件时降级为A级

3. **成交量倍数降低**
   - S级：从1.5降至1.4
   - A级：从1.5降至1.3

4. **冷却期缩短**
   - 从6小时缩短至4小时

### 前置过滤器
- ADX ≥ 12
- ATR%范围：1.0%-8.5%
- 成交量倍数：S≥1.4，A≥1.3，B/C不要求
- 同币种冷却期：4小时
- 每日最大交易数：6笔

### 评分系统
- 趋势强度：20分
- 形态质量：50分
- 动量背离：30分
- 等级阈值：S≥85，A≥78，B≥65，C≥55

### A级信号额外过滤
- RSI 40-60
- 成交量≥1.3倍
- MACD柱与趋势方向一致

### 止盈止损参数
- 止损：1.8×ATR
- TP1：3.5×ATR（平25%）
- TP2：6.0×ATR（平25%）
- 吊灯启动：2.0×ATR
- 吊灯回撤：1.5×ATR
- 时间止损：48小时未达TP1平仓50%

### 市场状态识别
- 过去5根4h ATR ≥ 过去20根ATR均值×80%

### 回测币种
- {', '.join(symbols)}

## 二、回测结果

### 资金情况
- 初始资金：{results['initial_capital']:.2f} USDT
- 最终资金：{results['final_capital']:.2f} USDT
- 总收益率：{results['total_return']:.2f}%
- 最大回撤：{results['max_drawdown']:.2f}%
- 夏普比率：{results['sharpe_ratio']:.2f}

### 交易统计
- 总交易次数：{results['total_trades']}
- 盈利次数：{results['win_trades']}
- 亏损次数：{results['loss_trades']}
- 胜率：{results['win_rate']:.2f}%
- 平均盈亏：{results['avg_pnl_percent']:.2f}%

## 三、过滤器效果统计

- 总信号数：{results['filter_stats']['total_signals']}
- ADX过滤：{results['filter_stats']['adx_filtered']}
- 成交量过滤：{results['filter_stats']['volume_filtered']}
- ATR%过滤：{results['filter_stats']['atr_percent_filtered']}
- 趋势一致性过滤：{results['filter_stats']['trend_alignment_filtered']}
- 冷却期过滤：{results['filter_stats']['cooldown_filtered']}
- 每日限额过滤：{results['filter_stats']['daily_limit_filtered']}
- A级RSI过滤：{results['filter_stats']['grade_a_rsi_filtered']}
- A级MACD过滤：{results['filter_stats']['grade_a_macd_filtered']}
- 市场状态过滤：{results['filter_stats']['market_state_filtered']}
- 评分过滤：{results['filter_stats']['score_filtered']}
- S级降级为A级：{results['filter_stats']['grade_s_downgraded']}
- 最终开仓：{results['filter_stats']['opened_positions']}
""")

    # 按币种统计
    if 'symbol_stats' in results and results['symbol_stats']:
        print("""
## 四、按币种统计
""")
        for symbol in symbols:
            if symbol in results['symbol_stats']:
                stats = results['symbol_stats'][symbol]
                print(f"""
### {symbol}
- 交易次数：{stats['count']}
- 胜率：{stats['win_rate']:.2f}%
- 平均盈亏：{stats['avg_pnl_percent']:.2f}%
- 总盈亏：{stats['total_pnl']:.2f} USDT
""")

    if 'grade_stats' in results and results['grade_stats']:
        print("""
## 五、按等级统计
""")
        for grade in ['S', 'A', 'B', 'C']:
            if grade in results['grade_stats']:
                stats = results['grade_stats'][grade]
                print(f"""
### {grade}级信号
- 交易次数：{stats['count']}
- 胜率：{stats['win_rate']:.2f}%
- 平均盈亏：{stats['avg_pnl_percent']:.2f}%
- 总盈亏：{stats['total_pnl']:.2f} USDT
""")

    if 'close_reason_stats' in results and results['close_reason_stats']:
        print("""
## 六、按平仓原因统计
""")
        for reason, stats in results['close_reason_stats'].items():
            avg_pnl = stats['total_pnl'] / stats['count']
            print(f"- {reason}：{stats['count']}次，平均盈亏：{avg_pnl:.2f} USDT")

    if 'monthly_stats' in results and results['monthly_stats']:
        print("""
## 七、月度统计
""")
        for month, stats in sorted(results['monthly_stats'].items()):
            win_rate = stats['win_count'] / stats['count'] * 100 if stats['count'] > 0 else 0
            print(f"- {month}：{stats['count']}笔，胜率：{win_rate:.2f}%，盈亏：{stats['total_pnl']:.2f} USDT")

    print("""
## 八、详细交易记录
""")

    trades = results['trades']
    if trades:
        print("### 前10笔交易")
        for i, trade in enumerate(trades[:10], 1):
            grade_info = f"{trade['grade']}级"
            if trade.get('original_grade') and trade['original_grade'] != trade['grade']:
                grade_info = f"{trade['grade']}级（原{trade['original_grade']}级降级）"
            print(f"""
{i}. {trade['symbol']} {trade['direction']} {grade_info}
   - 入场：{trade['entry_time']} @ {trade['entry_price']:.2f}
   - 出场：{trade['exit_time']} @ {trade['exit_price']:.2f}
   - 盈亏：{trade['pnl']:.2f} USDT ({trade['pnl_percent']:.2f}%)
   - 原因：{trade['close_reason']}
   - 评分：{trade['score']:.1f}
   - 持仓时间：{trade['holding_hours']:.1f}小时
""")

        if len(trades) > 10:
            print(f"\n... 省略中间 {len(trades) - 20} 笔交易 ...\n")

            print("### 后10笔交易")
            for i, trade in enumerate(trades[-10:], len(trades) - 9):
                grade_info = f"{trade['grade']}级"
                if trade.get('original_grade') and trade['original_grade'] != trade['grade']:
                    grade_info = f"{trade['grade']}级（原{trade['original_grade']}级降级）"
                print(f"""
{i}. {trade['symbol']} {trade['direction']} {grade_info}
   - 入场：{trade['entry_time']} @ {trade['entry_price']:.2f}
   - 出场：{trade['exit_time']} @ {trade['exit_price']:.2f}
   - 盈亏：{trade['pnl']:.2f} USDT ({trade['pnl_percent']:.2f}%)
   - 原因：{trade['close_reason']}
   - 评分：{trade['score']:.1f}
   - 持仓时间：{trade['holding_hours']:.1f}小时
""")


if __name__ == "__main__":
    main()
