"""
v6.16.8方案回测脚本 - 动态ATR + 动态成交量 + 币种差异化

核心改进（相对于v6.16.7）：
1. 币种差异化配置（SYMBOL_CONFIG）：
   - 每个币种独立的ADX最小值、ATR绝对下限、ATR分位数、成交量倍数、评分阈值
   - BTC/ETH: ADX≥12, ATR≥0.5%, 成交量S级1.2倍, S级评分≥82
   - BNB/XRP: ADX≥15, ATR≥0.4%, 成交量S级1.0倍, S级评分≥80
   - SOL: ADX≥22, ATR≥0.6%, 成交量S级1.8倍, S级评分≥88, 每日最大1次
   - TRX: ADX≥15, ATR≥0.3%, 成交量S级1.0倍, S级评分≥78

2. 动态ATR过滤器（v6.16.8优化）：
   - 基于历史ATR%的币种差异化分位数（BTC/ETH: 30%, SOL: 35%, TRX: 25%）
   - ADX调节系数（强趋势时放宽）
   - 币种绝对下限保护

3. 动态成交量过滤器（v6.16.8新增）：
   - 基于过去20小时均量计算
   - 币种差异化基础倍数（SOL S级: 1.8倍, A级: 1.5倍）
   - ADX调节（强趋势时降低20%要求）

前置过滤器（v6.16.8）：
- ADX ≥ 币种差异化最小值
- ATR%范围：动态下限（币种差异化）-8.5%
- 成交量：币种差异化动态阈值
- 同币种冷却期：4小时
- 每日最大交易数：币种差异化配置

评分系统：
- 趋势强度：20分
- 形态质量：50分
- 动量背离：30分
- 等级阈值：币种差异化配置

仓位配置：
- S级：币种差异化配置（SOL: 25%, 其他: 50%）
- A级：30%（ETH: 20%, SOL: 15%）
- B级：15%
- C级：8%

止盈止损参数：
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
from shared.dynamic_atr_filter import DynamicATRFilter, DynamicVolumeFilter

logger = structlog.get_logger()

ALL_SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'TRXUSDT']

# 币种差异化配置（v6.16.8完整配置表）
SYMBOL_CONFIG = {
    'BTCUSDT': {
        'adx_min': 12,
        'vol_ratio_base': {'S': 1.2, 'A': 1.0, 'B': 0.0, 'C': 0.0},
        'atr_abs_min': 0.005,   # 0.5%
        'atr_percentile': 30,
        'atr_factor_strong': 0.7,
        's_min_score': 82,
        'max_daily_trades': 2,
        'position_ratio_s': 0.5
    },
    'ETHUSDT': {
        'adx_min': 12,
        'vol_ratio_base': {'S': 1.2, 'A': 1.0, 'B': 0.0, 'C': 0.0},
        'atr_abs_min': 0.005,
        'atr_percentile': 30,
        'atr_factor_strong': 0.7,
        's_min_score': 82,
        'max_daily_trades': 2,
        'position_ratio_s': 0.5
    },
    'BNBUSDT': {
        'adx_min': 15,
        'vol_ratio_base': {'S': 1.0, 'A': 1.0, 'B': 0.0, 'C': 0.0},
        'atr_abs_min': 0.004,
        'atr_percentile': 30,
        'atr_factor_strong': 0.8,
        's_min_score': 80,
        'max_daily_trades': 2,
        'position_ratio_s': 0.5
    },
    'SOLUSDT': {
        'adx_min': 22,
        'vol_ratio_base': {'S': 1.8, 'A': 1.5, 'B': 0.0, 'C': 0.0},
        'atr_abs_min': 0.006,
        'atr_percentile': 35,
        'atr_factor_strong': 0.9,
        's_min_score': 88,
        'max_daily_trades': 1,
        'position_ratio_s': 0.25
    },
    'XRPUSDT': {
        'adx_min': 15,
        'vol_ratio_base': {'S': 1.0, 'A': 1.0, 'B': 0.0, 'C': 0.0},
        'atr_abs_min': 0.004,
        'atr_percentile': 30,
        'atr_factor_strong': 0.8,
        's_min_score': 80,
        'max_daily_trades': 2,
        'position_ratio_s': 0.5
    },
    'TRXUSDT': {
        'adx_min': 15,
        'vol_ratio_base': {'S': 1.0, 'A': 1.0, 'B': 0.0, 'C': 0.0},
        'atr_abs_min': 0.003,
        'atr_percentile': 25,
        'atr_factor_strong': 0.7,
        's_min_score': 78,
        'max_daily_trades': 2,
        'position_ratio_s': 0.5
    }
}


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
    """回测引擎 - v6.16.8方案（动态ATR + 动态成交量 + 币种差异化）"""

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

        # 币种冷却期配置
        self.symbol_cooldown_hours = 4
        self.daily_trade_count = {}
        self.symbol_last_trade_time = {}

        # 币种累计亏损跟踪
        self.symbol_loss_tracker = {}

        # 标准等级阈值
        self.grade_thresholds = {
            'S': 85,
            'A': 75,
            'B': 75,
            'C': 55
        }

        # 过滤器统计
        self.filter_stats = {
            'total_signals': 0,
            'adx_filtered': 0,
            'volume_filtered': 0,
            'dynamic_volume_filtered': 0,
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
            'symbol_loss_suspended': 0,
            'concurrent_position_filtered': 0,
            's_grade_extra_filtered': 0,
            'opened_positions': 0,
            'symbol_stats': {symbol: {'signals': 0, 'opened': 0} for symbol in ALL_SYMBOLS}
        }

        # 初始化动态ATR过滤器（使用默认配置）
        dynamic_atr_config = {
            'enabled': True,
            'lookback_hours': 720,
            'percentile': 0.35,
            'min_history_count': 100,
            'absolute_min_atr_percent': 0.6,
            'adx_coefficients': {
                'strong_trend': 25,
                'medium_trend': 20,
                'strong_coefficient': 0.8,
                'medium_coefficient': 0.9,
                'weak_coefficient': 1.0
            },
            'symbol_overrides': {}
        }
        self.dynamic_atr_filter = DynamicATRFilter(dynamic_atr_config)
        self._atr_history_initialized: Dict[str, bool] = {}

        # 初始化动态成交量过滤器（每个币种独立）
        self.dynamic_volume_filters: Dict[str, DynamicVolumeFilter] = {}
        for symbol, symbol_cfg in SYMBOL_CONFIG.items():
            self.dynamic_volume_filters[symbol] = DynamicVolumeFilter(symbol_cfg)

        # 成交量历史数据
        self._volume_history: Dict[str, deque] = {}

    def get_symbol_config(self, symbol: str) -> Dict:
        """获取币种特定配置"""
        return SYMBOL_CONFIG.get(symbol, SYMBOL_CONFIG['BTCUSDT'])

    def load_klines_from_csv(self, symbol: str, interval: str) -> pd.DataFrame:
        """从CSV文件加载K线数据"""
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
        """计算技术指标"""
        df = df.copy()

        high = df['high']
        low = df['low']
        close = df['close']

        # ATR计算
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df['ATR'] = tr.rolling(window=14).mean()

        # ADX计算
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

        # EMA计算
        df['EMA21'] = close.ewm(span=21, adjust=False).mean()
        df['EMA50'] = close.ewm(span=50, adjust=False).mean()
        df['EMA200'] = close.ewm(span=200, adjust=False).mean()

        # MACD计算
        ema_12 = close.ewm(span=12, adjust=False).mean()
        ema_26 = close.ewm(span=26, adjust=False).mean()
        df['MACD'] = ema_12 - ema_26
        df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']

        # RSI计算
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))

        # 成交量MA
        df['Volume_MA'] = df['volume'].rolling(window=20).mean()

        return df

    def _calculate_score(
        self,
        indicators_1h: pd.DataFrame,
        indicators_4h: pd.DataFrame,
        indicators_1d: pd.DataFrame
    ) -> float:
        """计算综合评分"""
        score = 0.0

        # 趋势强度评分（20分）
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

        # 形态质量评分（50分）
        pattern_score = 40.0

        macd_hist = indicators_1h['MACD_Hist'].iloc[-1]
        if pd.notna(macd_hist) and macd_hist > 0:
            pattern_score += 10.0

        score += pattern_score

        # 动量背离评分（30分）
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
        """确定交易方向"""
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
        """检查趋势一致性"""
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

    def check_symbol_loss_suspension(
        self,
        symbol: str,
        current_time
    ) -> Tuple[bool, str]:
        """检查币种累计亏损暂停状态"""
        if symbol not in self.symbol_loss_tracker:
            return True, "币种无累计亏损记录"

        tracker = self.symbol_loss_tracker[symbol]

        if tracker.get('suspended_until'):
            if current_time < tracker['suspended_until']:
                remaining = tracker['suspended_until'] - current_time
                remaining_hours = remaining.total_seconds() / 3600
                return False, f"币种暂停交易中（累计亏损{tracker['cumulative_loss']:.2f}U，剩余{remaining_hours:.1f}小时）"
            else:
                tracker['cumulative_loss'] = 0.0
                tracker['suspended_until'] = None
                logger.info(
                    f"币种暂停期结束，重置累计亏损",
                    symbol=symbol,
                    time=current_time
                )

        return True, "币种交易正常"

    def update_symbol_loss(
        self,
        symbol: str,
        pnl: float,
        current_time
    ):
        """更新币种累计亏损"""
        if symbol not in self.symbol_loss_tracker:
            self.symbol_loss_tracker[symbol] = {
                'cumulative_loss': 0.0,
                'suspended_until': None
            }

        tracker = self.symbol_loss_tracker[symbol]

        if pnl < 0:
            tracker['cumulative_loss'] += abs(pnl)

            if tracker['cumulative_loss'] >= 25.0:
                tracker['suspended_until'] = current_time + timedelta(hours=24)
                logger.warning(
                    f"币种累计亏损超过25U，暂停交易24小时",
                    symbol=symbol,
                    cumulative_loss=tracker['cumulative_loss'],
                    suspended_until=tracker['suspended_until']
                )
        else:
            tracker['cumulative_loss'] = 0.0

    def check_concurrent_position(
        self,
        symbol: str
    ) -> Tuple[bool, str]:
        """检查同时持仓限制"""
        symbol_positions = [p for p in self.positions if p.symbol == symbol]
        if len(symbol_positions) >= 1:
            return False, f"{symbol}已有持仓，不允许同时开多个仓位"
        return True, "无并发持仓"

    def _initialize_dynamic_atr_history(
        self,
        symbol: str,
        klines_1h: pd.DataFrame
    ) -> None:
        """初始化动态ATR历史数据"""
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

    def _initialize_volume_history(
        self,
        symbol: str,
        klines_1h: pd.DataFrame
    ) -> None:
        """初始化成交量历史数据"""
        if symbol not in self._volume_history:
            self._volume_history[symbol] = deque(maxlen=100)

            # 初始化过去100小时的成交量
            for i in range(min(100, len(klines_1h))):
                volume = klines_1h['volume'].iloc[-(i+1)]
                if pd.notna(volume):
                    self._volume_history[symbol].append(float(volume))

    def run_backtest(
        self,
        symbols: List[str],
        klines_dict: Dict[str, Dict[str, pd.DataFrame]]
    ) -> Dict:
        """运行回测"""
        all_timestamps = set()
        for symbol in symbols:
            if symbol in klines_dict and '1h' in klines_dict[symbol]:
                all_timestamps.update(klines_dict[symbol]['1h'].index)

        sorted_timestamps = sorted(list(all_timestamps))

        # 初始化动态ATR历史
        for symbol in symbols:
            if symbol in klines_dict and '1h' in klines_dict[symbol]:
                self._initialize_dynamic_atr_history(symbol, klines_dict[symbol]['1h'])
                self._initialize_volume_history(symbol, klines_dict[symbol]['1h'])

        # 主回测循环
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

                # 更新成交量历史
                volume = klines_1h['volume'].iloc[-1]
                if pd.notna(volume) and symbol in self._volume_history:
                    self._volume_history[symbol].append(float(volume))

                # 检查开仓
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

                # 检查平仓
                self._check_and_close_positions(
                    symbol=symbol,
                    current_time=timestamp,
                    current_price=current_price,
                    indicators_1h=indicators_1h
                )

        # 强制平仓所有持仓
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

        # 获取币种配置
        symbol_config = self.get_symbol_config(symbol)

        # 1. ADX过滤器（币种差异化）
        adx = indicators_1h['ADX'].iloc[-1]
        min_adx = symbol_config.get('adx_min', 15)
        if pd.isna(adx) or adx < min_adx:
            self.filter_stats['adx_filtered'] += 1
            return

        # 2. 动态ATR过滤器（币种差异化）
        atr = Decimal(str(indicators_1h['ATR'].iloc[-1]))
        if atr == 0:
            return
        atr_percent = float(atr / current_price * 100)

        adx_1d = indicators_1d['ADX'].iloc[-1] if indicators_1d is not None and 'ADX' in indicators_1d.columns else None
        adx_for_atr = float(adx_1d) if pd.notna(adx_1d) else 15.0

        # 使用币种差异化配置更新动态ATR过滤器
        atr_filter_config = {
            'enabled': True,
            'lookback_hours': 720,
            'percentile': symbol_config.get('atr_percentile', 30) / 100.0,
            'min_history_count': 100,
            'absolute_min_atr_percent': symbol_config.get('atr_abs_min', 0.005) * 100,
            'adx_coefficients': {
                'strong_trend': 25,
                'medium_trend': 20,
                'strong_coefficient': symbol_config.get('atr_factor_strong', 0.7),
                'medium_coefficient': 0.9,
                'weak_coefficient': 1.0
            },
            'symbol_overrides': {}
        }

        # 临时更新过滤器配置
        self.dynamic_atr_filter.low_percentile = atr_filter_config['percentile']
        self.dynamic_atr_filter.absolute_min = atr_filter_config['absolute_min_atr_percent']
        self.dynamic_atr_filter.strong_coefficient = atr_filter_config['adx_coefficients']['strong_coefficient']

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

        # 3. ATR%上限过滤
        if atr_percent > 8.5:
            self.filter_stats['atr_percent_filtered'] += 1
            return

        # 4. 评分计算
        score = self._calculate_score(indicators_1h, indicators_4h, indicators_1d)

        # 获取币种特定等级阈值
        s_min_score = symbol_config.get('s_min_score', 85)
        thresholds = {
            'S': s_min_score,
            'A': 75,
            'B': 75,
            'C': 55
        }

        min_score = thresholds.get('C', 55)
        if score < min_score:
            self.filter_stats['score_filtered'] += 1
            return

        # 5. 确定等级
        if score >= thresholds['S']:
            grade = 'S'
        elif score >= thresholds['A']:
            grade = 'A'
        elif score >= thresholds['B']:
            grade = 'B'
        else:
            grade = 'C'

        original_grade = grade

        # 6. 动态成交量过滤器（v6.16.8新增）
        volume = indicators_1h['volume'].iloc[-1]
        volume_history = list(self._volume_history.get(symbol, []))

        volume_filter = self.dynamic_volume_filters.get(symbol)
        if volume_filter:
            volume_pass, position_coefficient, volume_reason = volume_filter.check_with_position_adjustment(
                current_volume=float(volume),
                volume_history_1h=volume_history,
                adx_1d=adx_for_atr,
                grade=grade
            )

            if not volume_pass:
                self.filter_stats['dynamic_volume_filtered'] += 1
                logger.debug(
                    f"动态成交量过滤",
                    symbol=symbol,
                    time=current_time,
                    grade=grade,
                    reason=volume_reason
                )
                return

            if position_coefficient < 1.0:
                self.filter_stats['volume_position_reduced'] += 1
        else:
            position_coefficient = 1.0

        # 7. 趋势一致性
        direction = self._determine_direction(indicators_1h, indicators_4h)
        trend_aligned, trend_reason = self.is_trend_aligned_v616(klines_4h, klines_1d, direction)
        if not trend_aligned:
            self.filter_stats['trend_alignment_filtered'] += 1
            return

        # 8. 币种累计亏损暂停检查
        loss_ok, loss_reason = self.check_symbol_loss_suspension(symbol, current_time)
        if not loss_ok:
            self.filter_stats['symbol_loss_suspended'] += 1
            logger.debug(
                f"币种累计亏损暂停",
                symbol=symbol,
                time=current_time,
                reason=loss_reason
            )
            return

        # 9. 同时持仓限制
        concurrent_ok, concurrent_reason = self.check_concurrent_position(symbol)
        if not concurrent_ok:
            self.filter_stats['concurrent_position_filtered'] += 1
            return

        # 10. 冷却期和每日限制检查（币种差异化）
        cooldown_ok, cooldown_reason = self.check_cooldown_and_daily_limit(current_time, symbol)
        if not cooldown_ok:
            if '冷却期' in cooldown_reason:
                self.filter_stats['cooldown_filtered'] += 1
            else:
                self.filter_stats['daily_limit_filtered'] += 1
            return

        # 11. A级额外过滤
        rsi_4h = indicators_4h['RSI'].iloc[-1] if indicators_4h is not None else 50
        macd_hist = indicators_1h['MACD_Hist'].iloc[-1]
        grade_a_ok, grade_a_reason = self.check_grade_a_filters(grade, rsi_4h, 0, macd_hist, direction)
        if not grade_a_ok:
            if 'RSI' in grade_a_reason:
                self.filter_stats['grade_a_rsi_filtered'] += 1
            elif 'MACD' in grade_a_reason:
                self.filter_stats['grade_a_macd_filtered'] += 1
            return

        # 12. 计算仓位（币种差异化）
        if grade == 'S':
            position_ratio = Decimal(str(symbol_config.get('position_ratio_s', 0.5)))
        else:
            position_ratios = self.binance_config['position_ratio']
            position_ratio = Decimal(str(position_ratios.get(grade, 0.08)))

        position_ratio *= Decimal(str(position_coefficient))

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
        position.position_coefficient = position_coefficient

        # 设置止盈止损
        if direction == 'LONG':
            position.stop_loss = current_price - atr * self.stop_loss_atr
            position.tp1_price = current_price + atr * self.tp1_atr_multiplier
            position.tp2_price = current_price + atr * self.tp2_atr_multiplier
        else:
            position.stop_loss = current_price + atr * self.stop_loss_atr
            position.tp1_price = current_price - atr * self.tp1_atr_multiplier
            position.tp2_price = current_price - atr * self.tp2_atr_multiplier

        self.positions.append(position)

        # 更新交易计数
        today = current_time.date().isoformat()
        if today not in self.daily_trade_count:
            self.daily_trade_count[today] = {}
        if symbol not in self.daily_trade_count[today]:
            self.daily_trade_count[today][symbol] = 0
        self.daily_trade_count[today][symbol] += 1
        self.symbol_last_trade_time[symbol] = current_time

        self.filter_stats['opened_positions'] += 1
        self.filter_stats['symbol_stats'][symbol]['opened'] += 1

        logger.info(
            f"开仓信号",
            symbol=symbol,
            time=current_time,
            direction=direction,
            grade=grade,
            original_grade=original_grade,
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
        """A级信号额外过滤"""
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
        """检查冷却期和每日限制（币种差异化）"""
        symbol_config = self.get_symbol_config(symbol)
        max_daily_trades = symbol_config.get('max_daily_trades', 2)

        # 检查每日限制（按币种统计）
        today = current_time.date().isoformat()
        if today in self.daily_trade_count and symbol in self.daily_trade_count[today]:
            if self.daily_trade_count[today][symbol] >= max_daily_trades:
                return False, f"{symbol}已达每日最大交易数{max_daily_trades}笔"

        # 检查冷却期
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
        """检查并平仓"""
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
        """部分平仓"""
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

        # 更新币种累计亏损
        self.update_symbol_loss(position.symbol, float(pnl), current_time)

        position.quantity -= close_quantity

        trade_record = {
            'symbol': position.symbol,
            'entry_time': position.entry_time,
            'exit_time': current_time,
            'entry_price': float(position.entry_price),
            'exit_price': float(current_price),
            'direction': position.direction,
            'grade': position.grade,
            'original_grade': position.original_grade,
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
        """完全平仓"""
        pnl = self._calculate_pnl(position, current_price, position.quantity)

        self.current_capital += pnl

        # 更新币种累计亏损
        self.update_symbol_loss(position.symbol, float(pnl), current_time)

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
            'original_grade': position.original_grade,
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
            grade=position.grade,
            original_grade=position.original_grade
        )

    def _force_close_position(
        self,
        position: Position,
        current_time,
        current_price: Decimal
    ):
        """强制平仓"""
        self._close_position(position, current_time, current_price, "回测结束强制平仓")

    def _calculate_pnl(
        self,
        position: Position,
        current_price: Decimal,
        quantity: Decimal
    ) -> Decimal:
        """计算盈亏"""
        if position.direction == 'LONG':
            pnl = (current_price - position.entry_price) * quantity
        else:
            pnl = (position.entry_price - current_price) * quantity

        return pnl * position.leverage

    def _calculate_results(self) -> Dict:
        """计算回测结果"""
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
                'total_pnl': 0.0,
                'symbol_loss_tracker': self.symbol_loss_tracker
            }

        total_pnl = sum(t['pnl'] for t in self.trades)
        total_return = (total_pnl / float(self.initial_capital)) * 100

        winning_trades = [t for t in self.trades if t['pnl'] > 0]
        losing_trades = [t for t in self.trades if t['pnl'] < 0]
        win_rate = len(winning_trades) / len(self.trades) * 100 if self.trades else 0

        # 计算资金曲线和最大回撤
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

        # 计算夏普比率
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

        # 等级统计
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

        # 币种统计
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
            'total_pnl': float(total_pnl),
            'symbol_loss_tracker': self.symbol_loss_tracker
        }


def main():
    """主函数"""
    config_path = os.path.join(project_root, "strategies/btc_eth/config.yaml")
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    engine = BacktestEngine(config)

    symbols = ALL_SYMBOLS

    # 加载K线数据
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

    # 运行回测
    results = engine.run_backtest(symbols, klines_dict)

    # 打印结果
    print("\n" + "="*60)
    print("v6.16.8 回测结果（动态ATR + 动态成交量 + 币种差异化）")
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
    print(f"- 动态成交量过滤: {results['filter_stats']['dynamic_volume_filtered']}")
    print(f"- ATR%上限过滤: {results['filter_stats']['atr_percent_filtered']}")
    print(f"- 趋势一致性过滤: {results['filter_stats']['trend_alignment_filtered']}")
    print(f"- 冷却期过滤: {results['filter_stats']['cooldown_filtered']}")
    print(f"- 每日限制过滤: {results['filter_stats']['daily_limit_filtered']}")
    print(f"- 币种累计亏损暂停: {results['filter_stats']['symbol_loss_suspended']}")
    print(f"- 同时持仓过滤: {results['filter_stats']['concurrent_position_filtered']}")
    print(f"- S级降级: {results['filter_stats']['grade_s_downgraded']}")
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

    # 打印币种累计亏损情况
    if results.get('symbol_loss_tracker'):
        print("\n" + "-"*60)
        print("币种累计亏损跟踪:")
        for symbol, tracker in results['symbol_loss_tracker'].items():
            status = "暂停中" if tracker.get('suspended_until') else "正常"
            print(f"- {symbol}: 累计亏损{tracker['cumulative_loss']:.2f}U, 状态: {status}")

    print("\n" + "="*60)

    # 打印动态ATR过滤器统计
    for symbol in symbols:
        engine.dynamic_atr_filter.log_statistics(symbol)


if __name__ == "__main__":
    main()
