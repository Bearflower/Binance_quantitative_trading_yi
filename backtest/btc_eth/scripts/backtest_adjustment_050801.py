"""
调整方案050801回测脚本
基于方案2的优化版本

止盈止损参数：
- 止损：2.0×ATR
- TP1：3.5×ATR（平25%）
- TP2：5.5×ATR（平25%）
- 吊灯启动：3.0×ATR
- 吊灯回撤：2.0×ATR
- 时间止损：72小时未达TP1平仓50%

前置过滤器：
- ADX ≥ 15
- 成交量放大 ≥ 1.2倍
- ATR%范围：1.5%-6.0%
- 趋势确认：4小时EMA21与日线EMA21方向一致
- RSI过滤：多头时RSI>40，空头时RSI<60

评分系统：
- 趋势强度：30分
- 形态质量：45分
- 动量背离：25分
- 等级阈值：S≥80, A≥70, B≥60, C≥50

市场状态识别：
- 当过去5根4小时K线的ATR低于过去20根ATR均值的80%时，禁止开仓
"""
import pandas as pd
import numpy as np
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional
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


class BacktestEngine:
    """回测引擎"""

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

        # 止盈止损参数（新参数）
        self.stop_loss_atr = Decimal('2.0')  # 止损：2.0×ATR
        self.tp1_atr_multiplier = Decimal('3.5')  # TP1：3.5×ATR
        self.tp2_atr_multiplier = Decimal('5.5')  # TP2：5.5×ATR
        self.trailing_activation_atr = Decimal('3.0')  # 吊灯启动：3.0×ATR
        self.trailing_stop_atr = Decimal('2.0')  # 吊灯回撤：2.0×ATR
        self.time_stop_hours = 72  # 时间止损：72小时

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
            'opened_positions': 0
        }

    def load_klines_from_csv(self, interval: str) -> pd.DataFrame:
        """
        从CSV文件加载K线数据

        Args:
            interval: 时间周期（1h, 4h, 1d）

        Returns:
            K线DataFrame
        """
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

        # 转换数值类型
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        return df

    def calculate_ema21(self, data: pd.DataFrame) -> pd.Series:
        """
        计算EMA21

        Args:
            data: K线数据

        Returns:
            EMA21序列
        """
        return data['close'].ewm(span=21, adjust=False).mean()

    def is_trend_aligned(
        self,
        df_4h: pd.DataFrame,
        df_1d: pd.DataFrame,
        direction: str
    ) -> bool:
        """
        判断4小时和日线趋势方向是否一致

        Args:
            df_4h: 4小时K线数据
            df_1d: 日线K线数据
            direction: 方向（LONG/SHORT）

        Returns:
            是否趋势一致
        """
        # 计算EMA21
        ema21_4h = self.calculate_ema21(df_4h)
        ema21_1d = self.calculate_ema21(df_1d)

        # 获取最新值
        close_4h = Decimal(str(df_4h['close'].iloc[-1]))
        ema21_4h_value = Decimal(str(ema21_4h.iloc[-1]))
        close_1d = Decimal(str(df_1d['close'].iloc[-1]))
        ema21_1d_value = Decimal(str(ema21_1d.iloc[-1]))

        # 判断方向一致性
        if direction == 'LONG':
            # 多头：价格都在EMA21上方
            return (close_4h > ema21_4h_value) and (close_1d > ema21_1d_value)
        else:
            # 空头：价格都在EMA21下方
            return (close_4h < ema21_4h_value) and (close_1d < ema21_1d_value)

    def is_market_state_valid(self, indicators_4h: pd.DataFrame) -> bool:
        """
        判断市场状态是否适合交易

        Args:
            indicators_4h: 4小时指标数据

        Returns:
            是否适合交易
        """
        # 获取最近20根K线的ATR
        atr_series = indicators_4h['ATR'].iloc[-20:]

        if len(atr_series) < 20 or atr_series.isna().any():
            return False

        # 计算过去5根ATR均值和过去20根ATR均值
        atr_5_mean = Decimal(str(atr_series.iloc[-5:].mean()))
        atr_20_mean = Decimal(str(atr_series.iloc[-20:].mean()))

        # 如果过去5根ATR低于过去20根ATR均值的80%，判定为震荡市，禁止开仓
        return atr_5_mean >= atr_20_mean * Decimal('0.8')

    def check_rsi_filter(
        self,
        rsi: float,
        direction: str
    ) -> bool:
        """
        检查RSI过滤条件

        Args:
            rsi: RSI值
            direction: 方向（LONG/SHORT）

        Returns:
            是否通过过滤
        """
        if pd.isna(rsi):
            return False

        if direction == 'LONG':
            # 多头时RSI > 40
            return rsi > 40
        else:
            # 空头时RSI < 60
            return rsi < 60

    def run_backtest(
        self,
        klines_1h: pd.DataFrame,
        klines_4h: pd.DataFrame,
        klines_1d: pd.DataFrame
    ) -> Dict:
        """
        运行回测

        Args:
            klines_1h: 1小时K线
            klines_4h: 4小时K线
            klines_1d: 日线K线

        Returns:
            回测结果字典
        """
        # 计算技术指标
        indicators_1h = pd.DataFrame(TechnicalIndicators.calculate_all(klines_1h))
        indicators_4h = pd.DataFrame(TechnicalIndicators.calculate_all(klines_4h))
        indicators_1d = pd.DataFrame(TechnicalIndicators.calculate_all(klines_1d))

        # 添加成交量数据
        indicators_1h['volume'] = klines_1h['volume'].values

        # 主回测循环
        for i in range(100, len(klines_1h)):
            current_time = klines_1h.index[i]
            current_price = Decimal(str(klines_1h['close'].iloc[i]))
            current_high = Decimal(str(klines_1h['high'].iloc[i]))
            current_low = Decimal(str(klines_1h['low'].iloc[i]))

            # 检查并平仓现有持仓
            for position in self.positions[:]:
                self._check_and_close_position(
                    position,
                    current_time,
                    current_price,
                    current_high,
                    current_low
                )

            # 尝试开新仓
            self._check_and_open_position(
                current_time,
                current_price,
                indicators_1h.iloc[:i+1],
                indicators_4h.iloc[:i+1],
                indicators_1d.iloc[:i+1],
                klines_4h.iloc[:i//4+1] if i >= 4 else klines_4h.iloc[:1],
                klines_1d.iloc[:i//24+1] if i >= 24 else klines_1d.iloc[:1]
            )

        # 强制平仓所有剩余持仓
        for position in self.positions[:]:
            self._force_close_position(
                position,
                klines_1h.index[-1],
                klines_1h['close'].iloc[-1]
            )

        # 计算回测指标
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
        """
        检查并开仓

        Args:
            current_time: 当前时间
            current_price: 当前价格
            indicators_1h: 1小时指标
            indicators_4h: 4小时指标
            indicators_1d: 日线指标
            klines_4h: 4小时K线
            klines_1d: 日线K线
        """
        self.filter_stats['total_signals'] += 1

        # 1. ADX过滤：ADX ≥ 15
        adx = indicators_1h['ADX'].iloc[-1]
        if pd.isna(adx) or adx < 15:
            self.filter_stats['adx_filtered'] += 1
            return

        # 2. 成交量放大过滤：≥ 1.2倍
        volume = indicators_1h['volume'].iloc[-1]
        volume_ma = indicators_1h['Volume_MA'].iloc[-1]
        if pd.isna(volume) or pd.isna(volume_ma) or volume < volume_ma * 1.2:
            self.filter_stats['volume_filtered'] += 1
            return

        # 3. ATR%范围过滤：1.5%-6.0%
        atr = Decimal(str(indicators_1h['ATR'].iloc[-1]))
        if atr == 0:
            return
        atr_percent = float(atr / current_price * 100)
        if atr_percent < 1.5 or atr_percent > 6.0:
            self.filter_stats['atr_percent_filtered'] += 1
            return

        # 4. 市场状态识别
        if not self.is_market_state_valid(indicators_4h):
            self.filter_stats['market_state_filtered'] += 1
            return

        # 计算评分
        score = self._calculate_score(indicators_1h, indicators_4h, indicators_1d)

        # 5. 评分过滤
        if score < 50:  # C级最低阈值
            self.filter_stats['score_filtered'] += 1
            return

        # 确定方向
        direction = self._determine_direction(indicators_1h, indicators_4h)

        # 6. 趋势方向一致性过滤
        if not self.is_trend_aligned(klines_4h, klines_1d, direction):
            self.filter_stats['trend_alignment_filtered'] += 1
            return

        # 7. RSI过滤
        rsi = indicators_4h['RSI'].iloc[-1]
        if not self.check_rsi_filter(rsi, direction):
            self.filter_stats['rsi_filtered'] += 1
            return

        # 确定等级
        if score >= 80:
            grade = 'S'
        elif score >= 70:
            grade = 'A'
        elif score >= 60:
            grade = 'B'
        else:
            grade = 'C'

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
        position.atr = atr
        position.score = score
        position.highest_price = current_price
        position.lowest_price = current_price

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

    def _check_and_close_position(
        self,
        position: Position,
        current_time,
        current_price: Decimal,
        current_high: Decimal,
        current_low: Decimal
    ):
        """
        检查并平仓

        Args:
            position: 持仓对象
            current_time: 当前时间
            current_price: 当前价格
            current_high: 当前最高价
            current_low: 当前最低价
        """
        if not position:
            return

        close_reason = None
        close_price = None

        if position.direction == 'LONG':
            # 多头持仓检查

            # 1. 止损检查
            if current_low <= position.stop_loss:
                close_reason = "止损"
                close_price = position.stop_loss

            # 2. TP1检查（平25%）
            elif not position.tp1_hit and current_high >= position.tp1_price:
                position.tp1_hit = True
                close_quantity = position.quantity * Decimal('0.25')
                pnl = (position.tp1_price - position.entry_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity

                logger.debug(
                    "TP1触发",
                    time=current_time,
                    price=float(position.tp1_price),
                    pnl=float(pnl)
                )

            # 3. TP2检查（平25%）
            elif not position.tp2_hit and current_high >= position.tp2_price:
                position.tp2_hit = True
                close_quantity = position.quantity * Decimal('0.5')  # 剩余仓位的50%
                pnl = (position.tp2_price - position.entry_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity

                logger.debug(
                    "TP2触发",
                    time=current_time,
                    price=float(position.tp2_price),
                    pnl=float(pnl)
                )

            # 4. 吊灯止损检查
            if not close_reason:
                # 更新最高价
                position.highest_price = max(position.highest_price, current_high)

                # 检查是否启动吊灯止损
                profit_atr = (position.highest_price - position.entry_price) / position.atr
                if profit_atr >= self.trailing_activation_atr:
                    position.trailing_activated = True

                # 如果吊灯止损已启动，检查回撤
                if position.trailing_activated:
                    trailing_stop = position.highest_price - position.atr * self.trailing_stop_atr
                    if current_low <= trailing_stop:
                        close_reason = "吊灯止损"
                        close_price = trailing_stop

            # 5. 时间止损检查
            if not close_reason:
                holding_hours = (current_time - position.entry_time).total_seconds() / 3600
                if holding_hours >= self.time_stop_hours and not position.tp1_hit:
                    # 72小时未达TP1，平仓50%
                    close_quantity = position.quantity * Decimal('0.5')
                    pnl = (current_price - position.entry_price) * close_quantity
                    self.current_capital += pnl
                    position.quantity -= close_quantity
                    close_reason = "时间止损"
                    close_price = current_price

        else:
            # 空头持仓检查

            # 1. 止损检查
            if current_high >= position.stop_loss:
                close_reason = "止损"
                close_price = position.stop_loss

            # 2. TP1检查（平25%）
            elif not position.tp1_hit and current_low <= position.tp1_price:
                position.tp1_hit = True
                close_quantity = position.quantity * Decimal('0.25')
                pnl = (position.entry_price - position.tp1_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity

                logger.debug(
                    "TP1触发",
                    time=current_time,
                    price=float(position.tp1_price),
                    pnl=float(pnl)
                )

            # 3. TP2检查（平25%）
            elif not position.tp2_hit and current_low <= position.tp2_price:
                position.tp2_hit = True
                close_quantity = position.quantity * Decimal('0.5')
                pnl = (position.entry_price - position.tp2_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity

                logger.debug(
                    "TP2触发",
                    time=current_time,
                    price=float(position.tp2_price),
                    pnl=float(pnl)
                )

            # 4. 吊灯止损检查
            if not close_reason:
                # 更新最低价
                position.lowest_price = min(position.lowest_price, current_low)

                # 检查是否启动吊灯止损
                profit_atr = (position.entry_price - position.lowest_price) / position.atr
                if profit_atr >= self.trailing_activation_atr:
                    position.trailing_activated = True

                # 如果吊灯止损已启动，检查回撤
                if position.trailing_activated:
                    trailing_stop = position.lowest_price + position.atr * self.trailing_stop_atr
                    if current_high >= trailing_stop:
                        close_reason = "吊灯止损"
                        close_price = trailing_stop

            # 5. 时间止损检查
            if not close_reason:
                holding_hours = (current_time - position.entry_time).total_seconds() / 3600
                if holding_hours >= self.time_stop_hours and not position.tp1_hit:
                    # 72小时未达TP1，平仓50%
                    close_quantity = position.quantity * Decimal('0.5')
                    pnl = (position.entry_price - current_price) * close_quantity
                    self.current_capital += pnl
                    position.quantity -= close_quantity
                    close_reason = "时间止损"
                    close_price = current_price

        # 执行平仓
        if close_reason and position.quantity > 0:
            self._close_position(position, current_time, close_price, close_reason)

    def _close_position(
        self,
        position: Position,
        current_time,
        close_price: Decimal,
        reason: str
    ):
        """
        平仓

        Args:
            position: 持仓对象
            current_time: 当前时间
            close_price: 平仓价格
            reason: 平仓原因
        """
        if not position or position.quantity <= 0:
            return

        # 计算盈亏
        if position.direction == 'LONG':
            pnl = (close_price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - close_price) * position.quantity

        self.current_capital += pnl

        # 记录交易
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
            'close_reason': reason,
            'score': position.score,
            'holding_hours': (current_time - position.entry_time).total_seconds() / 3600
        })

        # 移除持仓
        if position in self.positions:
            self.positions.remove(position)

    def _force_close_position(self, position: Position, current_time, close_price):
        """
        强制平仓（回测结束时）

        Args:
            position: 持仓对象
            current_time: 当前时间
            close_price: 平仓价格
        """
        if position:
            self._close_position(position, current_time, Decimal(str(close_price)), "回测结束")

    def _calculate_score(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame,
        indicators_1d: pd.DataFrame
    ) -> float:
        """
        计算信号评分

        Args:
            indicators_1h: 1小时指标
            indicators_4h: 4小时指标
            indicators_1d: 日线指标

        Returns:
            总评分
        """
        score = 0.0

        # 1. 趋势强度（30分）
        trend_score = self._calculate_trend_strength(indicators_1h, indicators_4h, indicators_1d)
        score += trend_score

        # 2. 形态质量（45分）
        pattern_score = self._calculate_pattern_quality(indicators_1h, indicators_4h)
        score += pattern_score

        # 3. 动量背离（25分）
        momentum_score = self._calculate_momentum_divergence(indicators_1h, indicators_4h)
        score += momentum_score

        return score

    def _calculate_trend_strength(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame,
        indicators_1d: pd.DataFrame
    ) -> float:
        """
        计算趋势强度评分（满分30分）

        Args:
            indicators_1h: 1小时指标
            indicators_4h: 4小时指标
            indicators_1d: 日线指标

        Returns:
            趋势强度评分
        """
        score = 0.0

        # 1. MA21与MA55关系（10分）
        ma21_1h = indicators_1h['MA21'].iloc[-1]
        ma55_1h = indicators_1h['MA55'].iloc[-1]
        if pd.notna(ma21_1h) and pd.notna(ma55_1h):
            if ma21_1h > ma55_1h:
                score += 10
            elif ma21_1h < ma55_1h:
                score += 10  # 空头趋势也算趋势

        # 2. ADX强度（10分）
        adx = indicators_1h['ADX'].iloc[-1]
        if pd.notna(adx):
            if adx >= 25:
                score += 10
            elif adx >= 20:
                score += 7
            elif adx >= 15:
                score += 5

        # 3. 多时间框架趋势一致性（10分）
        ma21_4h = indicators_4h['MA21'].iloc[-1]
        ma55_4h = indicators_4h['MA55'].iloc[-1]
        ma21_1d = indicators_1d['MA21'].iloc[-1]
        ma55_1d = indicators_1d['MA55'].iloc[-1]

        if pd.notna(ma21_4h) and pd.notna(ma55_4h) and pd.notna(ma21_1d) and pd.notna(ma55_1d):
            # 4小时和日线趋势方向一致
            if (ma21_4h > ma55_4h and ma21_1d > ma55_1d) or \
               (ma21_4h < ma55_4h and ma21_1d < ma55_1d):
                score += 10

        return min(score, 30.0)

    def _calculate_pattern_quality(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame
    ) -> float:
        """
        计算形态质量评分（满分45分）

        Args:
            indicators_1h: 1小时指标
            indicators_4h: 4小时指标

        Returns:
            形态质量评分
        """
        score = 0.0

        # 1. MACD形态（15分）
        macd = indicators_1h['MACD'].iloc[-1]
        macd_signal = indicators_1h['MACD_Signal'].iloc[-1]
        macd_hist = indicators_1h['MACD_Hist'].iloc[-1]

        if pd.notna(macd) and pd.notna(macd_signal):
            if macd > macd_signal and macd_hist > 0:
                score += 15  # 多头形态
            elif macd < macd_signal and macd_hist < 0:
                score += 15  # 空头形态
            elif macd > 0 or macd < 0:
                score += 8  # 中性形态

        # 2. 布林带位置（15分）
        close = indicators_1h['BB_Middle'].index[-1]  # 这里需要从原始数据获取close
        bb_upper = indicators_1h['BB_Upper'].iloc[-1]
        bb_middle = indicators_1h['BB_Middle'].iloc[-1]
        bb_lower = indicators_1h['BB_Lower'].iloc[-1]

        # 简化处理：根据MACD柱状图判断
        if pd.notna(macd_hist):
            if macd_hist > 0:
                score += 10  # 多头动能
            else:
                score += 10  # 空头动能

        # 3. 成交量确认（15分）
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

        return min(score, 45.0)

    def _calculate_momentum_divergence(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame
    ) -> float:
        """
        计算动量背离评分（满分25分）

        Args:
            indicators_1h: 1小时指标
            indicators_4h: 4小时指标

        Returns:
            动量背离评分
        """
        score = 0.0

        # 1. RSI位置（15分）
        rsi_1h = indicators_1h['RSI'].iloc[-1]
        rsi_4h = indicators_4h['RSI'].iloc[-1]

        if pd.notna(rsi_1h):
            if 40 < rsi_1h < 60:
                score += 10  # 中性区域，适合交易
            elif 30 < rsi_1h <= 40 or 60 <= rsi_1h < 70:
                score += 8
            elif rsi_1h <= 30 or rsi_1h >= 70:
                score += 5  # 极端区域，风险较高

        if pd.notna(rsi_4h):
            if 40 < rsi_4h < 60:
                score += 5  # 4小时RSI也在中性区域
            elif 30 < rsi_4h <= 40 or 60 <= rsi_4h < 70:
                score += 3

        # 2. MACD柱状图动能（10分）
        macd_hist = indicators_1h['MACD_Hist'].iloc[-1]
        macd_hist_prev = indicators_1h['MACD_Hist'].iloc[-2] if len(indicators_1h) > 1 else None

        if pd.notna(macd_hist):
            if pd.notna(macd_hist_prev):
                # 柱状图在增长
                if macd_hist > macd_hist_prev:
                    score += 10
                else:
                    score += 5
            else:
                score += 5

        return min(score, 25.0)

    def _determine_direction(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame
    ) -> str:
        """
        确定交易方向

        Args:
            indicators_1h: 1小时指标
            indicators_4h: 4小时指标

        Returns:
            方向（LONG/SHORT）
        """
        long_votes = 0
        short_votes = 0

        # 1. MA21与MA55关系
        ma21_1h = indicators_1h['MA21'].iloc[-1]
        ma55_1h = indicators_1h['MA55'].iloc[-1]
        if pd.notna(ma21_1h) and pd.notna(ma55_1h):
            if ma21_1h > ma55_1h:
                long_votes += 1
            else:
                short_votes += 1

        # 2. MACD方向
        macd = indicators_1h['MACD'].iloc[-1]
        macd_signal = indicators_1h['MACD_Signal'].iloc[-1]
        if pd.notna(macd) and pd.notna(macd_signal):
            if macd > macd_signal:
                long_votes += 1
            else:
                short_votes += 1

        # 3. 4小时趋势
        ma21_4h = indicators_4h['MA21'].iloc[-1]
        ma55_4h = indicators_4h['MA55'].iloc[-1]
        if pd.notna(ma21_4h) and pd.notna(ma55_4h):
            if ma21_4h > ma55_4h:
                long_votes += 1
            else:
                short_votes += 1

        return 'LONG' if long_votes > short_votes else 'SHORT'

    def _calculate_results(self) -> Dict:
        """
        计算回测结果

        Returns:
            回测结果字典
        """
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

        # 计算基本统计
        total_trades = len(self.trades)
        win_trades = sum(1 for t in self.trades if t['pnl'] > 0)
        loss_trades = sum(1 for t in self.trades if t['pnl'] <= 0)
        win_rate = win_trades / total_trades * 100 if total_trades > 0 else 0

        # 计算平均盈亏
        avg_pnl_percent = np.mean([t['pnl_percent'] for t in self.trades])

        # 计算最大回撤
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

        # 计算夏普比率（简化版）
        returns = [t['pnl_percent'] for t in self.trades]
        sharpe_ratio = np.mean(returns) / np.std(returns) if np.std(returns) > 0 else 0

        # 按等级统计
        grade_stats = {}
        for grade in ['S', 'A', 'B', 'C']:
            grade_trades = [t for t in self.trades if t['grade'] == grade]
            if grade_trades:
                grade_stats[grade] = {
                    'count': len(grade_trades),
                    'win_rate': sum(1 for t in grade_trades if t['pnl'] > 0) / len(grade_trades) * 100,
                    'avg_pnl_percent': np.mean([t['pnl_percent'] for t in grade_trades])
                }

        # 按平仓原因统计
        close_reason_stats = {}
        for trade in self.trades:
            reason = trade['close_reason']
            if reason not in close_reason_stats:
                close_reason_stats[reason] = {'count': 0, 'total_pnl': 0}
            close_reason_stats[reason]['count'] += 1
            close_reason_stats[reason]['total_pnl'] += trade['pnl']

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
            'close_reason_stats': close_reason_stats
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

    # 加载数据
    klines_1h = engine.load_klines_from_csv('1h')
    klines_4h = engine.load_klines_from_csv('4h')
    klines_1d = engine.load_klines_from_csv('1d')

    # 运行回测
    results = engine.run_backtest(klines_1h, klines_4h, klines_1d)

    # 输出报告
    print_report(results)


def print_report(results: Dict):
    """
    打印回测报告

    Args:
        results: 回测结果
    """
    print(f"""
# 调整方案050801回测结果

## 一、过滤参数配置

### 止盈止损参数
- 止损：2.0×ATR
- TP1：3.5×ATR（平25%）
- TP2：5.5×ATR（平25%）
- 吊灯启动：3.0×ATR
- 吊灯回撤：2.0×ATR
- 时间止损：72小时未达TP1平仓50%

### 前置过滤器
- ADX趋势强度：≥ 15
- 成交量放大：≥ 1.2倍
- ATR%范围：1.5%-6.0%
- 趋势确认：4小时EMA21与日线EMA21方向一致
- RSI过滤：多头时RSI>40，空头时RSI<60

### 评分系统
- 趋势强度：30分
- 形态质量：45分
- 动量背离：25分
- 等级阈值：S≥80, A≥70, B≥60, C≥50

### 市场状态识别
- 过去5根4小时ATR < 过去20根ATR均值×80%时禁止开仓

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
- RSI过滤：{results['filter_stats']['rsi_filtered']}
- 市场状态过滤：{results['filter_stats']['market_state_filtered']}
- 评分过滤：{results['filter_stats']['score_filtered']}
- 最终开仓：{results['filter_stats']['opened_positions']}

## 四、按等级统计
""")

    if 'grade_stats' in results:
        for grade, stats in results['grade_stats'].items():
            print(f"""
### {grade}级信号
- 交易次数：{stats['count']}
- 胜率：{stats['win_rate']:.2f}%
- 平均盈亏：{stats['avg_pnl_percent']:.2f}%
""")

    print("""
## 五、按平仓原因统计
""")

    if 'close_reason_stats' in results:
        for reason, stats in results['close_reason_stats'].items():
            avg_pnl = stats['total_pnl'] / stats['count']
            print(f"- {reason}：{stats['count']}次，平均盈亏：{avg_pnl:.2f} USDT")

    print("""
## 六、详细交易记录
""")

    # 显示前10笔和后10笔交易
    trades = results['trades']
    if trades:
        print("### 前10笔交易")
        for i, trade in enumerate(trades[:10], 1):
            print(f"""
{i}. {trade['direction']} {trade['grade']}级
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
                print(f"""
{i}. {trade['direction']} {trade['grade']}级
   - 入场：{trade['entry_time']} @ {trade['entry_price']:.2f}
   - 出场：{trade['exit_time']} @ {trade['exit_price']:.2f}
   - 盈亏：{trade['pnl']:.2f} USDT ({trade['pnl_percent']:.2f}%)
   - 原因：{trade['close_reason']}
   - 评分：{trade['score']:.1f}
   - 持仓时间：{trade['holding_hours']:.1f}小时
""")


if __name__ == "__main__":
    main()
