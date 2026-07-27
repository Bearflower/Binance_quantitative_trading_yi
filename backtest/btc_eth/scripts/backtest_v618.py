"""
v6.18方案回测脚本

核心改进：
1. ATR%范围优化（核心改进）
   - 下限设为1.2%（从0.5%提升，避免低波动开仓）
   - 上限保持9.0%
   - 禁止低波动开仓，避免止损过窄

2. S级门槛提高
   - S级从85分提升至88分
   - 解决S级胜率低于A/B级的问题
   - 提高S级信号质量

3. 时间止损延长
   - 从48小时延长至72小时
   - 给低波动行情更多发展时间

4. 前置过滤器（保持v6.17）
   - ADX ≥ 12
   - ATR%范围：1.2%-9.0%
   - 成交量倍数：S≥1.5，A≥1.5，B/C不要求
   - 同币种冷却期：6小时
   - 每日最大交易数：6笔

5. 趋势一致性（保持v6.16）
   - 日线：close > EMA21（多头）
   - 4小时：EMA21斜率 > 0

6. 评分系统
   - 趋势强度：20分
   - 形态质量：50分
   - 动量背离：30分
   - 等级阈值：S≥88，A≥78，B≥65，C≥55

7. 止盈止损
   - 止损：1.8×ATR
   - TP1：3.5×ATR（平25%）
   - TP2：6.0×ATR（平25%）
   - 吊灯启动：2.0×ATR
   - 吊灯回撤：1.5×ATR
   - 时间止损：72小时未达TP1，平仓50%

预期效果：
- 总交易数：80-120笔/半年
- 胜率：55%-60%
- 净利润：+10%-20%
- 最大回撤：<8%
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
        self.atr = Decimal('0')  # 入场时ATR
        self.atr_percent = 0.0  # 入场时ATR%
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
        self.volume_ratio = 0.0  # 成交量比率


class BacktestEngine:
    """回测引擎 - v6.18方案"""

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

        # v6.18止盈止损参数
        self.stop_loss_atr = Decimal('1.8')  # 止损：1.8×ATR
        self.tp1_atr_multiplier = Decimal('3.5')  # TP1：3.5×ATR
        self.tp2_atr_multiplier = Decimal('6.0')  # TP2：6.0×ATR
        self.trailing_activation_atr = Decimal('2.0')  # 吊灯启动：2.0×ATR
        self.trailing_stop_atr = Decimal('1.5')  # 吊灯回撤：1.5×ATR
        self.time_stop_hours = 72  # 时间止损：72小时（v6.18核心改进）

        # TP1和TP2平仓比例
        self.tp1_close_ratio = Decimal('0.25')  # TP1平仓25%
        self.tp2_close_ratio = Decimal('0.25')  # TP2平仓25%

        # v6.18频率控制参数
        self.max_daily_trades = 6  # 每日最大交易数
        self.symbol_cooldown_hours = 6  # 同币种冷却期
        self.daily_trade_count = {}  # 每日交易计数
        self.symbol_last_trade_time = {}  # 币种最后交易时间

        # v6.18 ATR%阈值配置（核心改进）
        # 下限从0.5%提升至1.2%，避免低波动开仓
        self.atr_min = Decimal('0.012')  # 1.2%（v6.18核心改进）
        self.atr_max = Decimal('0.09')   # 9.0%

        # v6.18 ADX阈值
        self.adx_threshold = 12  # ADX ≥ 12

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
            'opened_positions': 0,
            'atr_low_filtered': 0,  # ATR%过低过滤
            'atr_high_filtered': 0  # ATR%过高过滤
        }

    def load_klines_from_csv(self, interval: str, symbol: str = 'btcusdt') -> pd.DataFrame:
        """
        从CSV文件加载K线数据

        Args:
            interval: 时间周期（1h, 4h, 1d）
            symbol: 交易对（小写）

        Returns:
            K线DataFrame
        """
        script_dir = os.path.dirname(os.path.abspath(__file__))
        filename = os.path.join(script_dir, f"../data/{symbol}_{interval}.csv")
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

    def calculate_ema21_slope(self, df_4h: pd.DataFrame) -> float:
        """
        计算4小时EMA21斜率

        使用最近5根K线的EMA21数据进行线性回归

        Args:
            df_4h: 4小时K线数据

        Returns:
            EMA21斜率
        """
        # 计算EMA21
        ema21_series = self.calculate_ema21(df_4h)

        # 获取最近5根K线的EMA21值
        ema21_recent = ema21_series.iloc[-5:]

        if len(ema21_recent) < 5 or ema21_recent.isna().any():
            return 0.0

        # 使用numpy进行线性回归
        x = np.arange(5)
        y = ema21_recent.values
        slope = np.polyfit(x, y, 1)[0]

        return slope

    def is_trend_aligned_v618(
        self,
        df_4h: pd.DataFrame,
        df_1d: pd.DataFrame,
        direction: str
    ) -> Tuple[bool, str]:
        """
        v6.18趋势一致性过滤（保持v6.16方案A）

        日线要求：close > ema21（多头）/ close < ema21（空头）
        4小时要求：EMA21斜率 > 0（多头）/ 斜率 < 0（空头）

        Args:
            df_4h: 4小时K线数据
            df_1d: 日线K线数据
            direction: 方向（LONG/SHORT）

        Returns:
            (是否趋势一致, 原因说明)
        """
        # 计算EMA21
        ema21_1d = self.calculate_ema21(df_1d)

        # 获取日线最新值
        close_1d = Decimal(str(df_1d['close'].iloc[-1]))
        ema21_1d_value = Decimal(str(ema21_1d.iloc[-1]))

        # 计算4小时EMA21斜率
        slope = self.calculate_ema21_slope(df_4h)

        # 日线趋势判断
        if direction == 'LONG':
            daily_ok = close_1d > ema21_1d_value
            if not daily_ok:
                return False, f"日线趋势不一致（多头需close>ema21，close={close_1d:.2f}, ema21={ema21_1d_value:.2f}）"

            # 4小时趋势判断（斜率 > 0）
            h4_ok = slope > 0
            if not h4_ok:
                return False, f"4小时趋势不一致（多头需EMA21斜率>0，当前斜率={slope:.4f}）"
        else:
            daily_ok = close_1d < ema21_1d_value
            if not daily_ok:
                return False, f"日线趋势不一致（空头需close<ema21，close={close_1d:.2f}, ema21={ema21_1d_value:.2f}）"

            # 4小时趋势判断（斜率 < 0）
            h4_ok = slope < 0
            if not h4_ok:
                return False, f"4小时趋势不一致（空头需EMA21斜率<0，当前斜率={slope:.4f}）"

        return True, f"趋势一致（日线{'多头' if direction == 'LONG' else '空头'}，4h斜率={slope:.4f}）"

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

    def check_cooldown_and_daily_limit(
        self,
        current_time,
        symbol: str
    ) -> Tuple[bool, str]:
        """
        检查冷却期和每日交易限制

        Args:
            current_time: 当前时间
            symbol: 交易对

        Returns:
            (是否通过, 原因说明)
        """
        # 检查同币种冷却期
        if symbol in self.symbol_last_trade_time:
            last_trade_time = self.symbol_last_trade_time[symbol]
            hours_since_last_trade = (current_time - last_trade_time).total_seconds() / 3600
            if hours_since_last_trade < self.symbol_cooldown_hours:
                return False, f"币种{symbol}冷却期未结束（还需{self.symbol_cooldown_hours - hours_since_last_trade:.1f}小时）"

        # 检查每日交易限制
        current_date = current_time.date()
        if current_date in self.daily_trade_count:
            if self.daily_trade_count[current_date] >= self.max_daily_trades:
                return False, f"今日交易次数已达上限（{self.max_daily_trades}笔）"

        return True, "通过"

    def check_grade_a_filters(
        self,
        grade: str,
        rsi_4h: float,
        volume_ratio: float,
        macd_hist: float,
        direction: str
    ) -> Tuple[bool, str]:
        """
        A级信号额外过滤

        Args:
            grade: 信号等级
            rsi_4h: 4小时RSI值
            volume_ratio: 成交量比率
            macd_hist: MACD柱状图值
            direction: 交易方向

        Returns:
            (是否通过, 原因说明)
        """
        if grade != 'A':
            return True, "非A级信号，无需额外过滤"

        # RSI过滤：40-60
        if pd.isna(rsi_4h) or not (40 <= rsi_4h <= 60):
            return False, f"A级信号RSI={rsi_4h:.2f}不在40-60区间"

        # 成交量过滤：≥1.5倍
        if volume_ratio < 1.5:
            return False, f"A级信号成交量不足（{volume_ratio:.2f}倍 < 1.5倍）"

        # MACD柱状图方向一致性
        if pd.isna(macd_hist):
            return False, "MACD柱状图数据缺失"

        if direction == 'LONG' and macd_hist <= 0:
            return False, f"A级信号MACD柱与趋势方向不一致（多头需MACD柱>0，当前={macd_hist:.4f}）"

        if direction == 'SHORT' and macd_hist >= 0:
            return False, f"A级信号MACD柱与趋势方向不一致（空头需MACD柱<0，当前={macd_hist:.4f}）"

        return True, "A级信号过滤通过"

    def get_volume_multiplier(self, grade: str) -> float:
        """
        根据等级获取成交量倍数要求

        v6.18: S≥1.5，A≥1.5，B/C不要求

        Args:
            grade: 信号等级

        Returns:
            成交量倍数
        """
        volume_multipliers = {
            'S': 1.5,
            'A': 1.5,
            'B': 0.0,  # 不要求
            'C': 0.0   # 不要求
        }
        return volume_multipliers.get(grade, 0.0)

    def run_backtest(
        self,
        klines_1h: pd.DataFrame,
        klines_4h: pd.DataFrame,
        klines_1d: pd.DataFrame,
        symbol: str = 'BTCUSDT'
    ) -> Dict:
        """
        运行回测

        Args:
            klines_1h: 1小时K线
            klines_4h: 4小时K线
            klines_1d: 日线K线
            symbol: 交易对

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
                klines_1d.iloc[:i//24+1] if i >= 24 else klines_1d.iloc[:1],
                symbol
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
        klines_1d: pd.DataFrame,
        symbol: str = 'BTCUSDT'
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
            symbol: 交易对
        """
        self.filter_stats['total_signals'] += 1

        # 1. ADX过滤：ADX ≥ 12
        adx = indicators_1h['ADX'].iloc[-1]
        if pd.isna(adx) or adx < self.adx_threshold:
            self.filter_stats['adx_filtered'] += 1
            return

        # 2. ATR%范围过滤（v6.18核心改进：下限1.2%）
        atr = Decimal(str(indicators_1h['ATR'].iloc[-1]))
        if atr == 0:
            return
        atr_percent = float(atr / current_price)

        # 检查ATR%是否在范围内（1.2%-9.0%）
        if atr_percent < self.atr_min:
            self.filter_stats['atr_low_filtered'] += 1
            self.filter_stats['atr_percent_filtered'] += 1
            return

        if atr_percent > self.atr_max:
            self.filter_stats['atr_high_filtered'] += 1
            self.filter_stats['atr_percent_filtered'] += 1
            return

        # 3. 市场状态识别
        if not self.is_market_state_valid(indicators_4h):
            self.filter_stats['market_state_filtered'] += 1
            return

        # 计算评分
        score = self._calculate_score(indicators_1h, indicators_4h, indicators_1d)

        # 4. 评分过滤
        if score < 55:  # C级最低阈值
            self.filter_stats['score_filtered'] += 1
            return

        # 确定等级（v6.18核心改进：S级阈值从85提升至88）
        if score >= 88:
            grade = 'S'
        elif score >= 78:
            grade = 'A'
        elif score >= 65:
            grade = 'B'
        else:
            grade = 'C'

        # 5. 成交量放大过滤（S≥1.5，A≥1.5，B/C不要求）
        volume = indicators_1h['volume'].iloc[-1]
        volume_ma = indicators_1h['Volume_MA'].iloc[-1]
        volume_multiplier = self.get_volume_multiplier(grade)

        if volume_multiplier > 0:  # S级和A级要求成交量
            if pd.isna(volume) or pd.isna(volume_ma) or volume < volume_ma * volume_multiplier:
                self.filter_stats['volume_filtered'] += 1
                return
            volume_ratio = volume / volume_ma
        else:  # B级和C级不要求成交量
            volume_ratio = volume / volume_ma if pd.notna(volume) and pd.notna(volume_ma) else 0.0

        # 确定方向
        direction = self._determine_direction(indicators_1h, indicators_4h)

        # 6. 趋势方向一致性过滤
        trend_aligned, trend_reason = self.is_trend_aligned_v618(klines_4h, klines_1d, direction)
        if not trend_aligned:
            self.filter_stats['trend_alignment_filtered'] += 1
            return

        # 7. 冷却期和每日交易限制检查
        cooldown_ok, cooldown_reason = self.check_cooldown_and_daily_limit(current_time, symbol)
        if not cooldown_ok:
            if '冷却期' in cooldown_reason:
                self.filter_stats['cooldown_filtered'] += 1
            else:
                self.filter_stats['daily_limit_filtered'] += 1
            return

        # 8. A级信号额外过滤
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
        position.atr = atr
        position.atr_percent = atr_percent
        position.score = score
        position.highest_price = current_price
        position.lowest_price = current_price
        position.symbol = symbol
        position.volume_ratio = volume_ratio

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

        # 更新冷却期和每日交易计数
        self.symbol_last_trade_time[symbol] = current_time
        current_date = current_time.date()
        if current_date not in self.daily_trade_count:
            self.daily_trade_count[current_date] = 0
        self.daily_trade_count[current_date] += 1

        logger.debug(
            f"开仓成功",
            time=current_time,
            grade=grade,
            direction=direction,
            entry_price=float(current_price),
            score=score,
            atr_percent=atr_percent,
            volume_ratio=volume_ratio
        )

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
                close_quantity = position.quantity * self.tp1_close_ratio
                pnl = (position.tp1_price - position.entry_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity

                logger.debug(
                    "TP1触发",
                    time=current_time,
                    price=float(position.tp1_price),
                    pnl=float(pnl),
                    close_ratio="25%"
                )

            # 3. TP2检查（平25%）
            elif not position.tp2_hit and current_high >= position.tp2_price:
                position.tp2_hit = True
                close_quantity = position.quantity * self.tp2_close_ratio
                pnl = (position.tp2_price - position.entry_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity

                logger.debug(
                    "TP2触发",
                    time=current_time,
                    price=float(position.tp2_price),
                    pnl=float(pnl),
                    close_ratio="25%"
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

            # 5. 时间止损检查（v6.18核心改进：72小时）
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
                close_quantity = position.quantity * self.tp1_close_ratio
                pnl = (position.entry_price - position.tp1_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity

                logger.debug(
                    "TP1触发",
                    time=current_time,
                    price=float(position.tp1_price),
                    pnl=float(pnl),
                    close_ratio="25%"
                )

            # 3. TP2检查（平25%）
            elif not position.tp2_hit and current_low <= position.tp2_price:
                position.tp2_hit = True
                close_quantity = position.quantity * self.tp2_close_ratio
                pnl = (position.entry_price - position.tp2_price) * close_quantity
                self.current_capital += pnl
                position.quantity -= close_quantity

                logger.debug(
                    "TP2触发",
                    time=current_time,
                    price=float(position.tp2_price),
                    pnl=float(pnl),
                    close_ratio="25%"
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

            # 5. 时间止损检查（v6.18核心改进：72小时）
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
            'holding_hours': (current_time - position.entry_time).total_seconds() / 3600,
            'atr_percent': position.atr_percent,
            'volume_ratio': position.volume_ratio,
            'symbol': position.symbol
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
        计算信号评分（v6.18权重）

        Args:
            indicators_1h: 1小时指标
            indicators_4h: 4小时指标
            indicators_1d: 日线指标

        Returns:
            总评分
        """
        score = 0.0

        # 1. 趋势强度（20分）
        trend_score = self._calculate_trend_strength(indicators_1h, indicators_4h, indicators_1d)
        score += trend_score

        # 2. 形态质量（50分）
        pattern_score = self._calculate_pattern_quality(indicators_1h, indicators_4h)
        score += pattern_score

        # 3. 动量背离（30分）
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
        计算趋势强度评分（满分20分）

        Args:
            indicators_1h: 1小时指标
            indicators_4h: 4小时指标
            indicators_1d: 日线指标

        Returns:
            趋势强度评分
        """
        score = 0.0

        # 1. MA21与MA55关系（8分）
        ma21_1h = indicators_1h['MA21'].iloc[-1]
        ma55_1h = indicators_1h['MA55'].iloc[-1]
        if pd.notna(ma21_1h) and pd.notna(ma55_1h):
            if ma21_1h > ma55_1h:
                score += 8
            elif ma21_1h < ma55_1h:
                score += 8  # 空头趋势也算趋势

        # 2. ADX强度（6分）
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

        # 3. 多时间框架趋势一致性（6分）
        ma21_4h = indicators_4h['MA21'].iloc[-1]
        ma55_4h = indicators_4h['MA55'].iloc[-1]
        ma21_1d = indicators_1d['MA21'].iloc[-1]
        ma55_1d = indicators_1d['MA55'].iloc[-1]

        if pd.notna(ma21_4h) and pd.notna(ma55_4h) and pd.notna(ma21_1d) and pd.notna(ma55_1d):
            # 4小时和日线趋势方向一致
            if (ma21_4h > ma55_4h and ma21_1d > ma55_1d) or \
               (ma21_4h < ma55_4h and ma21_1d < ma55_1d):
                score += 6

        return min(score, 20.0)

    def _calculate_pattern_quality(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame
    ) -> float:
        """
        计算形态质量评分（满分50分）

        Args:
            indicators_1h: 1小时指标
            indicators_4h: 4小时指标

        Returns:
            形态质量评分
        """
        score = 0.0

        # 1. MACD形态（20分）
        macd = indicators_1h['MACD'].iloc[-1]
        macd_signal = indicators_1h['MACD_Signal'].iloc[-1]
        macd_hist = indicators_1h['MACD_Hist'].iloc[-1]

        if pd.notna(macd) and pd.notna(macd_signal):
            if macd > macd_signal and macd_hist > 0:
                score += 20  # 多头形态
            elif macd < macd_signal and macd_hist < 0:
                score += 20  # 空头形态
            elif macd > 0 or macd < 0:
                score += 10  # 中性形态

        # 2. MACD柱状图动能（15分）
        if pd.notna(macd_hist):
            if macd_hist > 0:
                score += 10  # 多头动能
            else:
                score += 10  # 空头动能

            # 柱状图增长趋势
            macd_hist_prev = indicators_1h['MACD_Hist'].iloc[-2] if len(indicators_1h) > 1 else None
            if pd.notna(macd_hist_prev) and abs(macd_hist) > abs(macd_hist_prev):
                score += 5  # 动能增强

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

        return min(score, 50.0)

    def _calculate_momentum_divergence(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame
    ) -> float:
        """
        计算动量背离评分（满分30分）

        Args:
            indicators_1h: 1小时指标
            indicators_4h: 4小时指标

        Returns:
            动量背离评分
        """
        score = 0.0

        # 1. RSI位置（18分）
        rsi_1h = indicators_1h['RSI'].iloc[-1]
        rsi_4h = indicators_4h['RSI'].iloc[-1]

        if pd.notna(rsi_1h):
            if 40 < rsi_1h < 60:
                score += 12  # 中性区域，适合交易
            elif 30 < rsi_1h <= 40 or 60 <= rsi_1h < 70:
                score += 8
            elif rsi_1h <= 30 or rsi_1h >= 70:
                score += 4  # 极端区域，风险较高

        if pd.notna(rsi_4h):
            if 40 < rsi_4h < 60:
                score += 6  # 4小时RSI也在中性区域
            elif 30 < rsi_4h <= 40 or 60 <= rsi_4h < 70:
                score += 3

        # 2. MACD柱状图动能（12分）
        macd_hist = indicators_1h['MACD_Hist'].iloc[-1]
        macd_hist_prev = indicators_1h['MACD_Hist'].iloc[-2] if len(indicators_1h) > 1 else None

        if pd.notna(macd_hist):
            if pd.notna(macd_hist_prev):
                # 柱状图在增长
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
                    'avg_pnl_percent': np.mean([t['pnl_percent'] for t in grade_trades]),
                    'total_pnl': sum(t['pnl'] for t in grade_trades),
                    'avg_atr_percent': np.mean([t['atr_percent'] for t in grade_trades]),
                    'avg_score': np.mean([t['score'] for t in grade_trades])
                }

        # 按平仓原因统计
        close_reason_stats = {}
        for trade in self.trades:
            reason = trade['close_reason']
            if reason not in close_reason_stats:
                close_reason_stats[reason] = {'count': 0, 'total_pnl': 0}
            close_reason_stats[reason]['count'] += 1
            close_reason_stats[reason]['total_pnl'] += trade['pnl']

        # 按ATR%区间统计
        atr_range_stats = {
            'low_volatility': {'count': 0, 'win_count': 0, 'total_pnl': 0},    # ATR% < 1.5%
            'normal_volatility': {'count': 0, 'win_count': 0, 'total_pnl': 0},  # 1.5% <= ATR% <= 5.0%
            'high_volatility': {'count': 0, 'win_count': 0, 'total_pnl': 0}     # ATR% > 5.0%
        }

        for trade in self.trades:
            atr_pct = trade['atr_percent']
            if atr_pct < 0.015:
                key = 'low_volatility'
            elif atr_pct <= 0.05:
                key = 'normal_volatility'
            else:
                key = 'high_volatility'

            atr_range_stats[key]['count'] += 1
            if trade['pnl'] > 0:
                atr_range_stats[key]['win_count'] += 1
            atr_range_stats[key]['total_pnl'] += trade['pnl']

        # 计算月度统计
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
            'atr_range_stats': atr_range_stats,
            'monthly_stats': monthly_stats
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
# v6.18方案回测结果

## 一、方案参数配置

### 核心改进（v6.18）
1. **ATR%范围优化**
   - 下限设为1.2%（从0.5%提升，避免低波动开仓）
   - 上限保持9.0%
   - 禁止低波动开仓，避免止损过窄

2. **S级门槛提高**
   - S级从85分提升至88分
   - 解决S级胜率低于A/B级的问题
   - 提高S级信号质量

3. **时间止损延长**
   - 从48小时延长至72小时
   - 给低波动行情更多发展时间

### 前置过滤器
- ADX ≥ 12
- ATR%范围：1.2%-9.0%
- 成交量倍数：S≥1.5，A≥1.5，B/C不要求
- 同币种冷却期：6小时
- 每日最大交易数：6笔

### 趋势一致性过滤
- 日线：close > ema21（多头）/ close < ema21（空头）
- 4小时：EMA21斜率 > 0（多头）/ 斜率 < 0（空头）

### 评分系统
- 趋势强度：20分
- 形态质量：50分
- 动量背离：30分
- 等级阈值：S≥88，A≥78，B≥65，C≥55

### 止盈止损参数
- 止损：1.8×ATR
- TP1：3.5×ATR（平25%）
- TP2：6.0×ATR（平25%）
- 吊灯启动：2.0×ATR
- 吊灯回撤：1.5×ATR
- 时间止损：72小时未达TP1平仓50%

### 市场状态识别
- 过去5根4h ATR ≥ 过去20根ATR均值×80%

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
- ATR%过滤（总计）：{results['filter_stats']['atr_percent_filtered']}
  - ATR%过低（<1.2%）：{results['filter_stats']['atr_low_filtered']}
  - ATR%过高（>9.0%）：{results['filter_stats']['atr_high_filtered']}
- 趋势一致性过滤：{results['filter_stats']['trend_alignment_filtered']}
- 冷却期过滤：{results['filter_stats']['cooldown_filtered']}
- 每日限额过滤：{results['filter_stats']['daily_limit_filtered']}
- A级RSI过滤：{results['filter_stats']['grade_a_rsi_filtered']}
- A级MACD过滤：{results['filter_stats']['grade_a_macd_filtered']}
- 市场状态过滤：{results['filter_stats']['market_state_filtered']}
- 评分过滤：{results['filter_stats']['score_filtered']}
- 最终开仓：{results['filter_stats']['opened_positions']}
""")

    if 'grade_stats' in results and results['grade_stats']:
        print("""
## 四、按等级统计
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
- 平均ATR%：{stats['avg_atr_percent']*100:.2f}%
- 平均评分：{stats['avg_score']:.1f}
""")

    if 'atr_range_stats' in results and results['atr_range_stats']:
        print("""
## 五、按ATR%区间统计

### 低波动区间（ATR% < 1.5%）
""")
        stats = results['atr_range_stats']['low_volatility']
        if stats['count'] > 0:
            win_rate = stats['win_count'] / stats['count'] * 100
            avg_pnl = stats['total_pnl'] / stats['count']
            print(f"""- 交易次数：{stats['count']}
- 胜率：{win_rate:.2f}%
- 平均盈亏：{avg_pnl:.2f} USDT
- 总盈亏：{stats['total_pnl']:.2f} USDT
""")
        else:
            print("- 无交易记录\n")

        print("### 正常波动区间（1.5% ≤ ATR% ≤ 5.0%）\n")
        stats = results['atr_range_stats']['normal_volatility']
        if stats['count'] > 0:
            win_rate = stats['win_count'] / stats['count'] * 100
            avg_pnl = stats['total_pnl'] / stats['count']
            print(f"""- 交易次数：{stats['count']}
- 胜率：{win_rate:.2f}%
- 平均盈亏：{avg_pnl:.2f} USDT
- 总盈亏：{stats['total_pnl']:.2f} USDT
""")
        else:
            print("- 无交易记录\n")

        print("### 高波动区间（ATR% > 5.0%）\n")
        stats = results['atr_range_stats']['high_volatility']
        if stats['count'] > 0:
            win_rate = stats['win_count'] / stats['count'] * 100
            avg_pnl = stats['total_pnl'] / stats['count']
            print(f"""- 交易次数：{stats['count']}
- 胜率：{win_rate:.2f}%
- 平均盈亏：{avg_pnl:.2f} USDT
- 总盈亏：{stats['total_pnl']:.2f} USDT
""")
        else:
            print("- 无交易记录\n")

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
   - ATR%：{trade['atr_percent']*100:.2f}%
   - 成交量比率：{trade['volume_ratio']:.2f}倍
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
   - ATR%：{trade['atr_percent']*100:.2f}%
   - 成交量比率：{trade['volume_ratio']:.2f}倍
   - 持仓时间：{trade['holding_hours']:.1f}小时
""")

    print("""
## 九、v6.18 vs v6.17 对比

| 指标 | v6.17 | v6.18目标 | v6.18实际 | 变化 |
|------|-------|-----------|-----------|------|
| 总交易数（半年） | 353 | 80-120 | {total} | {change1:+.1f}% |
| 胜率 | 52.7% | 55%-60% | {win_rate:.1f}% | {change3:+.1f}% |
| 净利润 | 5.17% | +10%-20% | {total_return:.2f}% | {change4:+.2f}% |
| 最大回撤 | 14.56% | <8% | {max_dd:.2f}% | {change5:+.2f}% |
| 夏普比率 | -0.09 | ≥0.3 | {sharpe:.2f} | {change6:+.2f} |

### 核心改进效果
1. **ATR%下限提升**：从0.5%提升至1.2%，避免低波动开仓
2. **S级门槛提高**：从85分提升至88分，提高S级信号质量
3. **时间止损延长**：从48小时延长至72小时，给行情更多发展时间
4. **ATR%过滤效果**：通过ATR%过滤的信号占比{atr_pass_rate:.1f}%
""".format(
    total=results['total_trades'],
    win_rate=results['win_rate'],
    total_return=results['total_return'],
    max_dd=results['max_drawdown'],
    sharpe=results['sharpe_ratio'],
    change1=(results['total_trades'] - 353) / 353 * 100 if results['total_trades'] > 0 else 0,
    change3=results['win_rate'] - 52.7,
    change4=results['total_return'] - 5.17,
    change5=results['max_drawdown'] - 14.56,
    change6=results['sharpe_ratio'] - (-0.09),
    atr_pass_rate=(1 - results['filter_stats']['atr_percent_filtered'] / results['filter_stats']['total_signals']) * 100 if results['filter_stats']['total_signals'] > 0 else 0
))


if __name__ == "__main__":
    main()
