#!/usr/bin/env python3
"""
v6.20 震荡市生存版 全币种回测脚本
1. 使用已有数据（从币安API下载）进行回测
2. 使用 v6.20 策略评分逻辑 + 市场状态识别（5条件判定）
3. STRONG_TREND时趋势策略S级开仓，RANGING时震荡策略接管
4. 趋势策略止损1.5×ATR，震荡策略止损1.0×ATR
5. 生成回测报告
"""
import asyncio
import aiohttp
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple
import structlog
import os
import sys
import yaml

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../..'))

from shared.indicators import TechnicalIndicators
from strategies.btc_eth.market_state import (
    get_market_state,
    get_market_state_behavior,
    MarketState
)

logger = structlog.get_logger()

# ============================================================
# 配置
# ============================================================
SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'TRXUSDT']
INTERVALS = ['1h', '4h', '1d']
BACKTEST_MONTHS = 3  # 回测近 3 个月
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'reports')

# 加载 v6.19 策略配置
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '../../..', 'strategies', 'btc_eth', 'config.yaml')
with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
    STRATEGY_CONFIG = yaml.safe_load(f)


class KlineDownloader:
    """K 线数据下载器（从币安 API）"""

    BASE_URL = "https://fapi.binance.com"

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def download_klines(
        self, symbol: str, interval: str, start_time: int, end_time: int
    ) -> list:
        """下载 K 线数据"""
        all_klines = []
        current_start = start_time

        interval_ms = {
            '1h': 60 * 60 * 1000,
            '4h': 4 * 60 * 60 * 1000,
            '1d': 24 * 60 * 60 * 1000,
        }

        ms_per_request = interval_ms[interval] * 1000  # 每次请求 1000 根

        while current_start < end_time:
            current_end = min(current_start + ms_per_request, end_time)
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": current_start,
                "endTime": current_end,
                "limit": 1000,
            }

            try:
                async with self.session.get(
                    f"{self.BASE_URL}/fapi/v1/klines", params=params
                ) as response:
                    if response.status != 200:
                        logger.error(f"请求失败: {response.status}")
                        break
                    data = await response.json()
                    if not data:
                        break
                    all_klines.extend(data)
                    current_start = data[-1][0] + interval_ms[interval]
                    await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"下载失败: {e}")
                break

        logger.info(f"{symbol} {interval}: 下载 {len(all_klines)} 根 K 线")
        return all_klines

    def save_to_csv(self, klines: list, symbol: str, interval: str):
        """保存到 CSV"""
        if not klines:
            return

        df = pd.DataFrame(klines, columns=[
            'open_time', 'open_price', 'high_price', 'low_price',
            'close_price', 'volume', 'close_time', 'quote_volume',
            'trades', 'taker_buy_base', 'taker_buy_quote', 'ignore',
        ])
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        df = df[['open_time', 'open_price', 'high_price', 'low_price', 'close_price', 'volume']]

        os.makedirs(DATA_DIR, exist_ok=True)
        filename = f"{symbol.lower()}_{interval}.csv"
        filepath = os.path.join(DATA_DIR, filename)
        df.to_csv(filepath, index=False)
        logger.info(f"已保存: {filepath} ({len(df)} 行)")


class V620BacktestEngine:
    """v6.20 震荡市生存版 策略回测引擎"""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.config = STRATEGY_CONFIG
        self.scoring_config = self.config['strategy']['scoring']
        self.risk_config = self.config['strategy']['risk']
        self.binance_config = self.config['binance']
        self.symbol_config = self.config.get('strategy', {}).get('symbol_config', {}).get(symbol, {})

        self.initial_capital = Decimal(str(
            self.risk_config.get('frequency_control', {}).get('initial_capital_usdt', 500)
        ))
        self.current_capital = self.initial_capital
        self.trades: List[Dict] = []
        self.equity_curve: List[Dict] = []

        # 每日交易计数
        self.daily_trades: Dict[str, int] = {}
        # 止损跟踪
        self.stop_loss_price: Optional[Decimal] = None
        # 吊灯止损
        self.trailing_stop_price: Optional[Decimal] = None
        self.trailing_stop_active = False
        # 持仓状态
        self.in_position = False
        self.position_direction: Optional[str] = None
        self.position_entry_price: Optional[Decimal] = None
        self.position_entry_time: Optional[datetime] = None
        self.position_grade: Optional[str] = None
        self.position_highest: Optional[Decimal] = None
        self.position_lowest: Optional[Decimal] = None

    def load_klines(self, interval: str) -> pd.DataFrame:
        """从 CSV 加载 K 线数据"""
        filename = f"{self.symbol.lower()}_{interval}.csv"
        filepath = os.path.join(DATA_DIR, filename)
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"数据文件不存在: {filepath}")

        df = pd.read_csv(filepath)
        df['open_time'] = pd.to_datetime(df['open_time'])
        df.set_index('open_time', inplace=True)
        df.rename(columns={
            'open_price': 'open',
            'high_price': 'high',
            'low_price': 'low',
            'close_price': 'close',
        }, inplace=True)
        return df

    def calculate_signals(self, klines_1h: pd.DataFrame, klines_4h: pd.DataFrame,
                          klines_1d: pd.DataFrame) -> List[Dict]:
        """计算交易信号"""
        signals = []

        # 计算技术指标（使用项目共享模块）
        indicators_1h = TechnicalIndicators.calculate_all(klines_1h)
        indicators_4h = TechnicalIndicators.calculate_all(klines_4h)
        indicators_1d = TechnicalIndicators.calculate_all(klines_1d)

        # 指标已对齐各自 K 线的索引，无需额外处理

        # 计算 ATR
        indicators_1h['ATR'] = self._calculate_atr(klines_1h, period=14)
        indicators_4h['ATR'] = self._calculate_atr(klines_4h, period=14)

        # 市场状态配置（v6.20）
        market_state_config = self.risk_config.get('market_state', {})
        market_state_enabled = market_state_config.get('enabled', True)

        # 从第 100 根 K 线开始（确保有足够历史数据）
        start_idx = 100
        for i in range(start_idx, len(klines_1h)):
            current_time = klines_1h.index[i]
            current_price = Decimal(str(klines_1h['close'].iloc[i]))

            # 检查频率控制
            if not self._check_frequency_control(current_time):
                continue

            # 市场状态检查（v6.20 震荡市生存版：策略分流）
            market_behavior = {
                'can_trade': False, 'strategy_mode': 'trend',
                'min_grade': 'S', 'vol_boost': 0.0,
                'position_ratio_mult': 0.0, 'stop_loss_atr': 1.5
            }
            market_state = None
            if market_state_enabled:
                # 找到对应的 4h 和 1d 索引
                try:
                    i_4h_arr = klines_4h.index.get_indexer([current_time], method='pad')
                    i_4h = i_4h_arr[0] if len(i_4h_arr) > 0 else -1
                    if i_4h >= 100:  # 确保有足够的 4h 数据
                        # 构造 4h 指标切片（含 MA21 用于连续检查）
                        indicators_4h_slice = {
                            key: indicators_4h[key].iloc[:i_4h + 1] if key in indicators_4h else pd.Series()
                            for key in ['BB_Upper', 'BB_Middle', 'BB_Lower', 'ADX', 'MA21']
                        }
                        close_4h = klines_4h['close'].iloc[:i_4h + 1]
                        # 构造1d指标切片用于日线EMA21斜率计算
                        indicators_1d_slice = None
                        try:
                            i_1d_arr = klines_1d.index.get_indexer([current_time], method='pad')
                            i_1d = i_1d_arr[0] if len(i_1d_arr) > 0 else -1
                            if i_1d >= 2:
                                indicators_1d_slice = {
                                    key: indicators_1d[key].iloc[:i_1d + 1] if key in indicators_1d else pd.Series()
                                    for key in ['MA21']
                                }
                        except (KeyError, IndexError):
                            pass
                        
                        market_state, state_desc = get_market_state(
                            indicators_4h_slice, close_prices=close_4h,
                            indicators_1d=indicators_1d_slice, config=market_state_config
                        )
                        market_behavior = get_market_state_behavior(market_state, market_state_config)
                except (KeyError, IndexError):
                    pass  # 无法获取数据，保持默认行为
            
            # v6.20：根据策略模式分流
            strategy_mode = market_behavior.get('strategy_mode', 'trend')
            
            if strategy_mode == 'ranging':
                # 震荡市策略：生成震荡信号
                # 先获取4h指标的时间对齐切片
                try:
                    i_4h_arr = klines_4h.index.get_indexer([current_time], method='pad')
                    i_4h = i_4h_arr[0] if len(i_4h_arr) > 0 else -1
                except (KeyError, IndexError):
                    i_4h = -1
                if i_4h < 100:
                    continue
                
                ranging_signal = self._calculate_ranging_signal(
                    indicators_1h, indicators_4h, indicators_1d, 
                    i, klines_1h, klines_4h, current_time, current_price, 
                    market_behavior, i_4h
                )
                if ranging_signal:
                    # 震荡市频率计数：在信号生成时立即更新（避免同一根K线产生多个信号）
                    ranging_date_key = current_time.strftime('%Y-%m-%d')
                    ranging_key = f"ranging_{ranging_date_key}"
                    self.daily_trades[ranging_key] = self.daily_trades.get(ranging_key, 0) + 1
                    signals.append(ranging_signal)
                continue  # 震荡市处理完毕，跳过趋势逻辑
            
            # 原有趋势策略逻辑（仅 STRONG_TREND 时执行）
            if not market_behavior.get('can_trade', False):
                continue

            # 评分
            score = self._calculate_score(
                indicators_1h, indicators_4h, indicators_1d, i, klines_1h
            )

            # 判断等级（使用配置中的等级阈值）
            thresholds = self.scoring_config.get('grade_thresholds', {})
            s_min = self.symbol_config.get('s_min_score', thresholds.get('S', 90))
            if score >= s_min:
                grade = 'S'
            elif score >= thresholds.get('A', 75):
                grade = 'A'
            elif score >= thresholds.get('B', 65):
                grade = 'B'
            elif score >= thresholds.get('C', 55):
                grade = 'C'
            else:
                continue

            # 市场状态等级过滤（v6.19）
            grade_order = {'S': 0, 'A': 1, 'B': 2, 'C': 3}
            min_grade = market_behavior.get('min_grade', 'C')
            if grade_order.get(grade, 3) > grade_order.get(min_grade, 3):
                continue

            # 判断方向
            direction = self._determine_direction(indicators_1h, indicators_4h, i)

            # 计算 ATR
            atr = Decimal(str(indicators_1h['ATR'].iloc[i])) if i < len(indicators_1h['ATR']) else Decimal('0')

            # 仓位比例（从配置读取）
            position_ratio = Decimal(str(self.binance_config.get('position_ratio', {}).get(
                grade, 0.10
            )))

            # 杠杆（从配置读取）
            leverage = self.binance_config.get('leverage', {}).get(grade, 2)

            signal = {
                'timestamp': current_time,
                'price': current_price,
                'direction': direction,
                'grade': grade,
                'score': score,
                'atr': atr,
                'position_ratio': position_ratio,
                'leverage': leverage,
                'market_state': market_state.value if market_state else 'RANGING',
                'stop_loss_atr': market_behavior.get('stop_loss_atr', 1.8),
                'position_ratio_mult': market_behavior.get('position_ratio_mult', 1.0),
            }
            signals.append(signal)

        return signals

    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """计算 ATR"""
        high = df['high']
        low = df['low']
        close = df['close'].shift(1)

        tr1 = high - low
        tr2 = (high - close).abs()
        tr3 = (low - close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        return tr.rolling(window=period).mean()

    def _check_frequency_control(self, current_time: datetime) -> bool:
        """频率控制：冷却期 + 每日最大交易数（v6.19：极致低频）"""
        fc = self.risk_config.get('frequency_control', {})

        # 冷却期检查（v6.19: 72 小时）
        if self.trades:
            last_trade_time = self.trades[-1]['timestamp']
            cooldown_hours = fc.get('symbol_cooldown_hours', 72)
            if (current_time - last_trade_time).total_seconds() < cooldown_hours * 3600:
                return False

        # 每日最大交易数（v6.19: 1 笔）
        date_key = current_time.strftime('%Y-%m-%d')
        max_daily = fc.get('max_daily_total_trades', 1)
        if self.daily_trades.get(date_key, 0) >= max_daily:
            return False

        # 币种每日最大交易数
        symbol_max_daily = self.symbol_config.get('max_daily_trades', max_daily)
        symbol_date_key = f"{self.symbol}_{date_key}"
        if self.daily_trades.get(symbol_date_key, 0) >= symbol_max_daily:
            return False

        return True

    def _calculate_score(self, indicators_1h, indicators_4h, indicators_1d,
                         idx: int, klines_1h: pd.DataFrame) -> float:
        """v6.19 评分系统: 趋势 25% + 形态 50% + 动量 25%"""
        score = 0.0

        # ====== 趋势评分 (v6.19: 25%) ======
        weights = self.scoring_config.get('weights', {})
        trend_weight = weights.get('trend_strength', 0.25)
        trend_score = 0.0

        # EMA21/EMA55 多时间框架
        for indicators, name in [(indicators_1h, '1h'), (indicators_4h, '4h')]:
            if idx >= len(indicators.get('MA21', pd.Series(dtype=float))):
                continue
            ma21 = indicators['MA21'].iloc[idx] if 'MA21' in indicators else None
            ma55 = indicators['MA55'].iloc[idx] if 'MA55' in indicators else None
            if ma21 is not None and ma55 is not None and pd.notna(ma21) and pd.notna(ma55):
                if ma21 > ma55:
                    trend_score += 50

        # MACD 柱状图
        if 'MACD' in indicators_1h and idx < len(indicators_1h['MACD']):
            macd = indicators_1h['MACD'].iloc[idx]
            if pd.notna(macd) and macd > 0:
                trend_score += 30

        # ADX 趋势强度
        if 'ADX' in indicators_1h and idx < len(indicators_1h['ADX']):
            adx = indicators_1h['ADX'].iloc[idx]
            if pd.notna(adx) and adx > 25:
                trend_score += 20

        score += trend_score * trend_weight

        # ====== 形态评分 (v6.19: 50%) ======
        pattern_weight = weights.get('pattern_quality', 0.50)
        pattern_score = 0.0

        # EMA21 斜率
        if idx >= 5 and 'MA21' in indicators_1h:
            ema_slice = indicators_1h['MA21'].iloc[max(0, idx-5):idx+1]
            if len(ema_slice) >= 5 and ema_slice.notna().all():
                slope = ema_slice.iloc[-1] - ema_slice.iloc[0]
                if slope > 0:
                    pattern_score += 40

        # 价格与 EMA21 的关系
        if 'MA21' in indicators_1h and idx < len(indicators_1h['MA21']):
            price = klines_1h['close'].iloc[idx]
            ma21_val = indicators_1h['MA21'].iloc[idx]
            if pd.notna(ma21_val) and price > ma21_val:
                pattern_score += 30

        # 成交量确认
        if idx >= 20:
            recent_vol = klines_1h['volume'].iloc[max(0, idx-20):idx+1]
            avg_vol = recent_vol.mean()
            current_vol = klines_1h['volume'].iloc[idx]
            if current_vol > avg_vol * 1.5:
                pattern_score += 30

        score += pattern_score * pattern_weight

        # ====== 动量评分 (v6.19: 25%) ======
        momentum_weight = weights.get('momentum_divergence', 0.25)
        momentum_score = 0.0

        # RSI 评分
        if 'RSI' in indicators_1h and idx < len(indicators_1h['RSI']):
            rsi = indicators_1h['RSI'].iloc[idx]
            if pd.notna(rsi):
                if 40 < rsi < 60:
                    momentum_score += 50
                elif 30 < rsi <= 40:
                    momentum_score += 40
                elif 60 <= rsi < 70:
                    momentum_score += 30

        # 价格动量
        if idx >= 3:
            price_momentum = (klines_1h['close'].iloc[idx] - klines_1h['close'].iloc[idx-3]) / klines_1h['close'].iloc[idx-3]
            if price_momentum > 0:
                momentum_score += 30

        # A 级额外加分：RSI 在 40-60 且 ADX > 20
        if 'RSI' in indicators_1h and 'ADX' in indicators_1h:
            if idx < len(indicators_1h['RSI']) and idx < len(indicators_1h['ADX']):
                rsi = indicators_1h['RSI'].iloc[idx]
                adx = indicators_1h['ADX'].iloc[idx]
                if pd.notna(rsi) and pd.notna(adx):
                    if 40 < rsi < 60 and adx > 20:
                        momentum_score += 20

        score += momentum_score * momentum_weight

        return min(score, 100.0)

    def _determine_direction(self, indicators_1h, indicators_4h, idx: int) -> str:
        """判断方向：多时间框架投票"""
        long_votes = 0
        short_votes = 0

        for indicators in [indicators_1h, indicators_4h]:
            if idx >= len(indicators.get('MA21', pd.Series())):
                continue
            ma21 = indicators['MA21'].iloc[idx] if 'MA21' in indicators else None
            ma55 = indicators['MA55'].iloc[idx] if 'MA55' in indicators else None
            if ma21 is not None and ma55 is not None and pd.notna(ma21) and pd.notna(ma55):
                if ma21 > ma55:
                    long_votes += 1
                else:
                    short_votes += 1

            if 'MACD' in indicators and idx < len(indicators['MACD']):
                macd = indicators['MACD'].iloc[idx]
                if pd.notna(macd):
                    if macd > 0:
                        long_votes += 1
                    else:
                        short_votes += 1

        return 'LONG' if long_votes > short_votes else 'SHORT'

    def _calculate_ranging_signal(
        self, indicators_1h, indicators_4h, indicators_1d,
        idx: int, klines_1h: pd.DataFrame, klines_4h: pd.DataFrame,
        current_time: datetime, current_price: Decimal,
        market_behavior: Dict, i_4h: int = -1
    ) -> Optional[Dict]:
        """
        v6.20 震荡市信号生成
        
        检查震荡市入场条件（布林带触轨/RSI超买超卖/反转K线形态），
        至少满足1个条件即可生成信号。
        
        Args:
            indicators_1h: 1h技术指标
            indicators_4h: 4h技术指标
            indicators_1d: 1d技术指标
            idx: 当前1h索引
            klines_1h: 1h K线数据
            klines_4h: 4h K线数据
            current_time: 当前时间
            current_price: 当前价格
            market_behavior: 市场状态行为配置
            i_4h: 当前时间对应的4h K线索引（用于时间对齐）
        
        Returns:
            信号字典或None
        """
        ranging_config = self.risk_config.get('ranging_strategy', {})
        if not ranging_config.get('enabled', True):
            return None
        
        # 1. 震荡市频率控制
        if not self._check_ranging_frequency(current_time):
            return None
        
        # 2. 检查入场条件（使用时间对齐的4h指标切片）
        entry_config = ranging_config.get('entry_conditions', {})
        conditions_met = []
        
        # 使用时间对齐的4h指标切片（取[:i_4h+1]）
        if i_4h >= 0:
            indicators_4h_slice = {
                key: indicators_4h[key].iloc[:i_4h + 1] if key in indicators_4h else pd.Series()
                for key in ['BB_Upper', 'BB_Middle', 'BB_Lower', 'RSI']
            }
            klines_4h_slice = klines_4h.iloc[:i_4h + 1]
        else:
            indicators_4h_slice = indicators_4h
            klines_4h_slice = klines_4h
        
        # 条件A：布林带触轨
        if entry_config.get('bb_touch', True):
            bb_result = self._check_bb_touch_backtest(
                indicators_4h_slice, klines_4h_slice, entry_config
            )
            if bb_result:
                conditions_met.append(('布林带触轨', bb_result))
        
        # 条件B：RSI超买超卖
        if entry_config.get('rsi_extreme', True):
            rsi_result = self._check_rsi_extreme_backtest(
                indicators_4h_slice, entry_config
            )
            if rsi_result:
                conditions_met.append(('RSI极端', rsi_result))
        
        # 条件C：反转K线形态
        if entry_config.get('reversal_pattern', True):
            pattern_result = self._check_reversal_pattern_backtest(
                klines_4h_slice
            )
            if pattern_result:
                conditions_met.append(('反转K线形态', pattern_result))
        
        if not conditions_met:
            return None
        
        # v6.20.1：震荡市至少需要2个入场条件（提高信号质量）
        if len(conditions_met) < 2:
            return None
        
        # 3. 评分
        score = self._calculate_ranging_score(
            indicators_1h, indicators_4h, idx, klines_1h
        )
        
        # 4. 等级判定
        thresholds = self.scoring_config.get('grade_thresholds', {})
        s_min = self.symbol_config.get('s_min_score', thresholds.get('S', 90))
        if score >= s_min:
            grade = 'S'
        elif score >= thresholds.get('A', 75):
            grade = 'A'
        elif score >= thresholds.get('B', 65):
            grade = 'B'
        elif score >= thresholds.get('C', 55):
            grade = 'C'
        else:
            return None
        
        # 震荡市仅允许S/A级
        allowed_grades = ranging_config.get('allowed_grades', ['S', 'A'])
        if grade not in allowed_grades:
            return None
        
        # 5. 确定方向
        direction = self._determine_ranging_direction_backtest(
            conditions_met, indicators_1h, indicators_4h, idx
        )
        if not direction:
            return None
        
        # 6. ATR
        atr = Decimal(str(indicators_1h['ATR'].iloc[idx])) if idx < len(indicators_1h['ATR']) else Decimal('0')
        if atr == Decimal('0'):
            return None
        
        ranging_risk = ranging_config.get('risk', {})
        stop_loss_atr = Decimal(str(ranging_risk.get('stop_loss_atr', 2.0)))
        take_profit_atr = Decimal(str(ranging_risk.get('take_profit_atr', 4.0)))
        
        signal = {
            'timestamp': current_time,
            'price': current_price,
            'direction': direction,
            'grade': grade,
            'score': score,
            'atr': atr,
            'position_ratio': Decimal(str(self.binance_config.get('position_ratio', {}).get(grade, 0.10))),
            'leverage': self.binance_config.get('leverage', {}).get(grade, 2),
            'market_state': 'RANGING',
            'strategy_mode': 'ranging',
            'stop_loss_atr': float(stop_loss_atr),
            'take_profit_atr': float(take_profit_atr),
            'position_ratio_mult': market_behavior.get('position_ratio_mult', 1.0),
            'entry_conditions': [c[0] for c in conditions_met],
            'exit_conditions': ranging_config.get('exit_conditions', {}),
        }
        
        return signal
    
    def _check_ranging_frequency(self, current_time: datetime) -> bool:
        """震荡市频率控制（独立于趋势策略计数）"""
        ranging_config = self.risk_config.get('ranging_strategy', {})
        fc = ranging_config.get('frequency_control', {})
        
        max_daily = fc.get('max_daily_trades', 3)
        cooldown_hours = fc.get('symbol_cooldown_hours', 24)
        
        date_key = current_time.strftime('%Y-%m-%d')
        ranging_key = f"ranging_{date_key}"
        
        # 每日最大交易数
        if self.daily_trades.get(ranging_key, 0) >= max_daily:
            return False
        
        # 冷却期检查（仅检查震荡市交易）
        ranging_trades = [t for t in self.trades if t.get('strategy_mode') == 'ranging']
        if ranging_trades:
            last_ranging_time = ranging_trades[-1]['entry_time']
            if (current_time - last_ranging_time).total_seconds() < cooldown_hours * 3600:
                return False
        
        return True
    
    def _check_bb_touch_backtest(self, indicators_4h_slice, klines_4h_slice, config: Dict) -> Optional[str]:
        """回测版布林带触轨检查（使用时间对齐的4h切片）"""
        bb_upper = indicators_4h_slice['BB_Upper'].iloc[-1] if 'BB_Upper' in indicators_4h_slice else None
        bb_lower = indicators_4h_slice['BB_Lower'].iloc[-1] if 'BB_Lower' in indicators_4h_slice else None
        
        if bb_upper is None or bb_lower is None:
            return None
        
        current_price = klines_4h_slice['close'].iloc[-1]
        threshold = config.get('bb_touch_threshold', 0.02)
        
        if current_price <= bb_lower * (1 + threshold):
            return 'LONG'
        if current_price >= bb_upper * (1 - threshold):
            return 'SHORT'
        
        return None
    
    def _check_rsi_extreme_backtest(self, indicators_4h_slice, config: Dict) -> Optional[str]:
        """回测版RSI超买超卖检查（使用时间对齐的4h切片）"""
        if 'RSI' not in indicators_4h_slice:
            return None
        
        rsi = indicators_4h_slice['RSI'].iloc[-1]
        if pd.isna(rsi):
            return None
        
        if rsi <= config.get('rsi_oversold', 30):
            return 'LONG'
        if rsi >= config.get('rsi_overbought', 70):
            return 'SHORT'
        
        return None
    
    def _check_reversal_pattern_backtest(self, klines_4h_slice) -> Optional[str]:
        """回测版反转K线形态检查（使用时间对齐的4h切片）"""
        if len(klines_4h_slice) < 3:
            return None
        
        # 取最近2根4h K线
        recent = klines_4h_slice.iloc[-2:]
        
        prev = recent.iloc[0]
        curr = recent.iloc[1]
        
        # 看涨吞没
        if prev['close'] < prev['open'] and curr['close'] > curr['open']:
            if curr['open'] <= prev['close'] and curr['close'] >= prev['open']:
                return 'LONG'
        
        # 看跌吞没
        if prev['close'] > prev['open'] and curr['close'] < curr['open']:
            if curr['open'] >= prev['close'] and curr['close'] <= prev['open']:
                return 'SHORT'
        
        return None
    
    def _calculate_ranging_score(self, indicators_1h, indicators_4h,
                                  idx: int, klines_1h: pd.DataFrame) -> float:
        """震荡市评分（趋势15%/形态60%/动量25%）"""
        ranging_config = self.risk_config.get('ranging_strategy', {})
        weights = ranging_config.get('scoring_weights', {
            'trend_strength': 0.15, 'pattern_quality': 0.60, 'momentum_divergence': 0.25,
        })
        
        score = 0.0
        
        # 趋势评分（低权重）
        trend_score = 0.0
        for indicators in [indicators_1h, indicators_4h]:
            if idx >= len(indicators.get('MA21', pd.Series())):
                continue
            ma21 = indicators['MA21'].iloc[idx] if 'MA21' in indicators else None
            ma55 = indicators['MA55'].iloc[idx] if 'MA55' in indicators else None
            if ma21 is not None and ma55 is not None and pd.notna(ma21) and pd.notna(ma55):
                if ma21 > ma55:
                    trend_score += 50
        score += trend_score * weights['trend_strength']
        
        # 形态评分（高权重）
        pattern_score = 0.0
        if idx >= 5 and 'MA21' in indicators_1h:
            ema_slice = indicators_1h['MA21'].iloc[max(0, idx-5):idx+1]
            if len(ema_slice) >= 5 and ema_slice.notna().all():
                slope = ema_slice.iloc[-1] - ema_slice.iloc[0]
                if slope > 0:
                    pattern_score += 40
        if 'MA21' in indicators_1h and idx < len(indicators_1h['MA21']):
            price = klines_1h['close'].iloc[idx]
            ma21_val = indicators_1h['MA21'].iloc[idx]
            if pd.notna(ma21_val) and price > ma21_val:
                pattern_score += 30
        if idx >= 20:
            recent_vol = klines_1h['volume'].iloc[max(0, idx-20):idx+1]
            avg_vol = recent_vol.mean()
            current_vol = klines_1h['volume'].iloc[idx]
            if current_vol > avg_vol * 1.5:
                pattern_score += 30
        score += pattern_score * weights['pattern_quality']
        
        # 动量评分
        momentum_score = 0.0
        if 'RSI' in indicators_1h and idx < len(indicators_1h['RSI']):
            rsi = indicators_1h['RSI'].iloc[idx]
            if pd.notna(rsi):
                if 40 < rsi < 60:
                    momentum_score += 50
                elif 30 < rsi <= 40:
                    momentum_score += 40
                elif 60 <= rsi < 70:
                    momentum_score += 30
        if idx >= 3:
            price_momentum = (klines_1h['close'].iloc[idx] - klines_1h['close'].iloc[idx-3]) / klines_1h['close'].iloc[idx-3]
            if price_momentum > 0:
                momentum_score += 30
        score += momentum_score * weights['momentum_divergence']
        
        return min(score, 100.0)
    
    def _determine_ranging_direction_backtest(
        self, conditions_met: list, indicators_1h, indicators_4h, idx: int
    ) -> Optional[str]:
        """回测版震荡市方向判定（v6.20.2：仅强趋势时拒绝反向信号）"""
        directions = [c[1] for c in conditions_met if c[1] in ('LONG', 'SHORT')]
        if not directions:
            return None
        
        long_count = directions.count('LONG')
        short_count = directions.count('SHORT')
        
        # 短线趋势过滤：检查1h EMA21斜率，仅在强趋势时拒绝反向信号
        # 强趋势定义：5根1h K线内EMA21斜率 > 阈值（ATR归一化）
        trend_bias = None
        trend_is_strong = False
        if idx >= 5 and 'MA21' in indicators_1h and 'ATR' in indicators_1h:
            ema_slice = indicators_1h['MA21'].iloc[max(0, idx-5):idx+1]
            if len(ema_slice) >= 5 and ema_slice.notna().all():
                slope = ema_slice.iloc[-1] - ema_slice.iloc[0]
                avg_ema = ema_slice.mean()
                atr_val = indicators_1h['ATR'].iloc[idx]
                # 斜率强度 = 斜率 / ATR（ATR归一化，跨币种可比）
                if pd.notna(atr_val) and atr_val > 0 and avg_ema > 0:
                    slope_strength = abs(slope) / atr_val
                    # 强趋势阈值：斜率 > 0.8×ATR（从配置读取）
                    ranging_config = self.risk_config.get('ranging_strategy', {})
                    strong_slope_threshold = ranging_config.get('trend_filter', {}).get('strong_slope_atr_mult', 0.8)
                    if slope_strength > strong_slope_threshold:
                        trend_is_strong = True
                        if slope > 0:
                            trend_bias = 'LONG'
                        elif slope < 0:
                            trend_bias = 'SHORT'
        
        if long_count > short_count:
            direction = 'LONG'
        elif short_count > long_count:
            direction = 'SHORT'
        else:
            direction = self._determine_direction(indicators_1h, indicators_4h, idx)
        
        # 仅在强趋势时拒绝反向信号（平缓趋势允许双向交易）
        if trend_is_strong and trend_bias and direction != trend_bias:
            return None
        
        return direction

    def simulate_trades(self, signals: List[Dict], klines_1h: pd.DataFrame, klines_4h: pd.DataFrame = None) -> Dict:
        """模拟交易执行（v6.19：止损 1.5×ATR）"""
        # 预先计算4h指标（震荡市退出需要）
        indicators_4h = None
        if klines_4h is not None:
            indicators_4h = TechnicalIndicators.calculate_all(klines_4h)
        
        for signal in signals:
            entry_time = signal['timestamp']
            entry_price = signal['price']
            direction = signal['direction']
            grade = signal['grade']
            atr = signal['atr']
            position_ratio = signal['position_ratio']
            leverage = signal['leverage']
            # v6.18: 动态止损（根据市场状态）
            stop_loss_atr = signal.get('stop_loss_atr', 1.8)
            position_ratio_mult = signal.get('position_ratio_mult', 1.0)

            # v6.20：震荡策略使用不同的退出逻辑
            if signal.get('strategy_mode') == 'ranging':
                if atr == Decimal('0'):
                    continue
                
                try:
                    entry_idx = klines_1h.index.get_loc(entry_time)
                except KeyError:
                    continue
                
                # 计算仓位（震荡策略，v6.20.2：修复entry_price归一化）
                pm_config = self.risk_config.get('position_management', {})
                risk_amount = Decimal(str(pm_config.get('volatility_target_risk', 10)))
                min_risk = Decimal(str(pm_config.get('volatility_target_min', 5)))
                max_risk = Decimal(str(pm_config.get('volatility_target_max', 15)))
                
                stop_loss_mult = Decimal(str(stop_loss_atr))
                stop_loss_pct = atr * stop_loss_mult / entry_price  # 止损百分比
                if stop_loss_pct == Decimal('0'):
                    continue
                position_size = risk_amount / stop_loss_pct  # USDT名义价值
                position_size = max(min_risk, min(max_risk, position_size))
                position_size = position_size * Decimal(str(position_ratio_mult))
                # v6.20.2：按币种差异化震荡市仓位乘数
                ranging_symbol_mult = Decimal(str(self.symbol_config.get('ranging_position_mult', 1.0)))
                position_size = position_size * ranging_symbol_mult
                
                exit_result = self._simulate_ranging_exit(
                    signal, klines_1h, entry_idx, position_size, klines_4h, indicators_4h
                )
                if exit_result:
                    exit_time, exit_price, exit_reason, pnl = exit_result
                    self.current_capital += pnl
                    trade = {
                        'symbol': self.symbol,
                        'entry_time': entry_time,
                        'exit_time': exit_time,
                        'direction': direction,
                        'grade': grade,
                        'score': signal['score'],
                        'entry_price': float(entry_price),
                        'exit_price': float(exit_price),
                        'position_size': float(position_size),
                        'leverage': leverage,
                        'pnl': float(pnl),
                        'exit_reason': exit_reason,
                        'strategy_mode': 'ranging',
                    }
                    self.trades.append(trade)
                    self.equity_curve.append({'timestamp': entry_time, 'equity': float(self.current_capital)})
                continue  # 震荡策略处理完毕

            if atr == Decimal('0'):
                continue

            # 计算仓位（波动率目标：从配置读取风险参数，v6.20.2：修复entry_price归一化）
            pm_config = self.risk_config.get('position_management', {})
            risk_amount = Decimal(str(pm_config.get('volatility_target_risk', 10)))
            min_risk = Decimal(str(pm_config.get('volatility_target_min', 5)))
            max_risk = Decimal(str(pm_config.get('volatility_target_max', 15)))
            
            stop_loss_mult = Decimal(str(stop_loss_atr))
            stop_loss_pct = atr * stop_loss_mult / entry_price  # 止损百分比
            if stop_loss_pct == Decimal('0'):
                continue
            position_size = risk_amount / stop_loss_pct  # USDT名义价值

            # 限制在 [min_risk, max_risk] 范围内
            position_size = max(min_risk, min(max_risk, position_size))

            # 应用市场状态仓位乘数（v6.18）
            position_size = position_size * Decimal(str(position_ratio_mult))

            # 保证金 = 仓位 / 杠杆
            margin = position_size / Decimal(str(leverage))

            # 总保证金限制
            if margin > self.current_capital * Decimal('0.3'):
                continue

            # 查找入场后的 K 线数据
            try:
                entry_idx = klines_1h.index.get_loc(entry_time)
            except KeyError:
                continue

            # 止盈止损（从配置读取，v6.18：止损使用动态值）
            partial_config = self.risk_config.get('partial_take_profit', {})
            chandelier_config = self.risk_config.get('chandelier_stop', {})
            time_stop_config = self.risk_config.get('time_stop', {})
            extreme_config = self.risk_config.get('extreme_market', {})
            
            sl_mult = Decimal(str(stop_loss_atr))
            tp1_mult = Decimal(str(partial_config.get('tp1_atr_multiplier', 4.0)))
            tp2_mult = Decimal(str(partial_config.get('tp2_atr_multiplier', 6.0)))
            tp1_ratio = Decimal(str(partial_config.get('tp1_close_ratio', 0.25)))
            chandelier_activation = Decimal(str(chandelier_config.get('activation_atr', 2.5)))
            chandelier_trailing = Decimal(str(chandelier_config.get('trailing_atr', 1.5)))
            max_holding_hours = time_stop_config.get('max_holding_hours', 72)
            reverse_pct = Decimal(str(extreme_config.get('reverse_pct', 0.05)))
            
            stop_loss = entry_price - atr * sl_mult if direction == 'LONG' else entry_price + atr * sl_mult
            tp1 = entry_price + atr * tp1_mult if direction == 'LONG' else entry_price - atr * tp1_mult
            tp2 = entry_price + atr * tp2_mult if direction == 'LONG' else entry_price - atr * tp2_mult
            trailing_stop = Decimal('0')

            # 模拟持仓
            exit_time = None
            exit_price = None
            exit_reason = ''
            highest = entry_price
            lowest = entry_price
            trailing_active = False

            for j in range(entry_idx + 1, min(entry_idx + max_holding_hours * 24, len(klines_1h))):  # 从配置读取最大持仓时间
                bar_high = Decimal(str(klines_1h['high'].iloc[j]))
                bar_low = Decimal(str(klines_1h['low'].iloc[j]))
                bar_close = Decimal(str(klines_1h['close'].iloc[j]))
                bar_time = klines_1h.index[j]

                if direction == 'LONG':
                    highest = max(highest, bar_high)
                    lowest = min(lowest, bar_low)

                    # 止损检查
                    if bar_low <= stop_loss:
                        exit_price = stop_loss
                        exit_time = bar_time
                        exit_reason = '止损'
                        break

                    # TP1（平仓比例从配置读取）
                    if bar_high >= tp1 and not trailing_active:
                        trailing_active = True
                        trailing_stop = highest - atr * chandelier_activation
                        # 记录部分止盈
                        profit_tp1 = position_size * tp1_ratio * (tp1 - entry_price) / entry_price
                        self.current_capital += profit_tp1
                        position_size *= (Decimal('1') - tp1_ratio)

                    # TP2
                    if bar_high >= tp2:
                        exit_price = tp2
                        exit_time = bar_time
                        exit_reason = 'TP2'
                        break

                    # 吊灯止损（从配置读取回撤倍数）
                    if trailing_active:
                        trailing_stop = max(trailing_stop, highest - atr * chandelier_trailing)
                        if bar_low <= trailing_stop:
                            exit_price = trailing_stop
                            exit_time = bar_time
                            exit_reason = '吊灯止损'
                            break

                    # 极端行情（从配置读取反向比例）
                    if bar_low <= entry_price * (Decimal('1') - reverse_pct):
                        exit_price = entry_price * (Decimal('1') - reverse_pct)
                        exit_time = bar_time
                        exit_reason = '极端行情'
                        break

                else:  # SHORT
                    highest = min(highest, bar_high)  # 最短最高
                    lowest = max(lowest, bar_low)  # 最短最低

                    if bar_high >= stop_loss:
                        exit_price = stop_loss
                        exit_time = bar_time
                        exit_reason = '止损'
                        break

                    if bar_low <= tp1 and not trailing_active:
                        trailing_active = True
                        trailing_stop = lowest + atr * chandelier_activation
                        profit_tp1 = position_size * tp1_ratio * (entry_price - tp1) / entry_price
                        self.current_capital += profit_tp1
                        position_size *= (Decimal('1') - tp1_ratio)

                    if bar_low <= tp2:
                        exit_price = tp2
                        exit_time = bar_time
                        exit_reason = 'TP2'
                        break

                    if trailing_active:
                        trailing_stop = min(trailing_stop, lowest + atr * chandelier_trailing)
                        if bar_high >= trailing_stop:
                            exit_price = trailing_stop
                            exit_time = bar_time
                            exit_reason = '吊灯止损'
                            break

                    if bar_high >= entry_price * (Decimal('1') + reverse_pct):
                        exit_price = entry_price * (Decimal('1') + reverse_pct)
                        exit_time = bar_time
                        exit_reason = '极端行情'
                        break

                # 时间止损（从配置读取最大持仓时间）
                if j == entry_idx + max_holding_hours * 24 - 1:
                    exit_price = bar_close
                    exit_time = bar_time
                    exit_reason = '时间止损'
                    break

            # 如果没有自然退出，最大持仓时间后强制平仓（从配置读取）
            if exit_price is None:
                exit_idx = min(entry_idx + max_holding_hours * 24, len(klines_1h) - 1)
                exit_price = Decimal(str(klines_1h['close'].iloc[exit_idx]))
                exit_time = klines_1h.index[exit_idx]
                exit_reason = '时间止损'

            # 计算盈亏
            if direction == 'LONG':
                pnl = position_size * (exit_price - entry_price) / entry_price
            else:
                pnl = position_size * (entry_price - exit_price) / entry_price

            self.current_capital += pnl

            trade = {
                'symbol': self.symbol,
                'entry_time': entry_time,
                'exit_time': exit_time,
                'direction': direction,
                'grade': grade,
                'score': signal['score'],
                'entry_price': float(entry_price),
                'exit_price': float(exit_price),
                'position_size': float(position_size),
                'leverage': leverage,
                'pnl': float(pnl),
                'exit_reason': exit_reason,
            }
            self.trades.append(trade)

            # 更新每日交易计数
            date_key = entry_time.strftime('%Y-%m-%d')
            self.daily_trades[date_key] = self.daily_trades.get(date_key, 0) + 1
            symbol_date_key = f"{self.symbol}_{date_key}"
            self.daily_trades[symbol_date_key] = self.daily_trades.get(symbol_date_key, 0) + 1

            # 记录权益曲线
            self.equity_curve.append({
                'timestamp': entry_time,
                'equity': float(self.current_capital),
            })

        return {
            'symbol': self.symbol,
            'initial_capital': float(self.initial_capital),
            'final_capital': float(self.current_capital),
            'total_trades': len(self.trades),
            'trades': self.trades,
            'equity_curve': self.equity_curve,
        }

    def _simulate_ranging_exit(
        self, signal: Dict, klines_1h: pd.DataFrame, entry_idx: int, position_size: Decimal,
        klines_4h: pd.DataFrame = None, indicators_4h = None
    ) -> Optional[Tuple]:
        """
        震荡市退出模拟
        
        退出条件（按优先级）：
        1. 止损（stop_loss_atr × ATR）
        2. 止盈（take_profit_atr × ATR）
        3. 触及布林带中轨（bb_mid_touch）
        4. RSI回归中性（rsi_neutral）
        5. 时间止损（time_stop_hours）
        
        Args:
            signal: 交易信号
            klines_1h: 1h K线数据
            entry_idx: 入场索引
            position_size: 仓位大小
            klines_4h: 4h K线数据（用于BB中轨/RSI检查）
            indicators_4h: 4h技术指标
        
        Returns:
            (退出时间, 退出价格, 退出原因, 盈亏) 或 None
        """
        entry_price = signal['price']
        direction = signal['direction']
        atr = signal['atr']
        exit_conditions = signal.get('exit_conditions', {})
        
        stop_loss_atr = Decimal(str(signal.get('stop_loss_atr', 2.0)))
        take_profit_atr = Decimal(str(signal.get('take_profit_atr', 4.0)))
        time_stop_hours = exit_conditions.get('time_stop_hours', 24)
        bb_mid_enabled = exit_conditions.get('bb_mid_touch', True)
        bb_mid_min_distance_atr = Decimal(str(exit_conditions.get('bb_mid_min_distance_atr', 0.5)))
        rsi_neutral_long = exit_conditions.get('rsi_neutral_long', 55)
        rsi_neutral_short = exit_conditions.get('rsi_neutral_short', 45)
        
        # 计算止损止盈价格
        if direction == 'LONG':
            stop_loss = entry_price - atr * stop_loss_atr
            take_profit = entry_price + atr * take_profit_atr
        else:
            stop_loss = entry_price + atr * stop_loss_atr
            take_profit = entry_price - atr * take_profit_atr
        
        # 模拟持仓（最多24h * 1h K线）
        max_bars = time_stop_hours  # 24h = 24根1h K线
        for j in range(entry_idx + 1, min(entry_idx + max_bars + 1, len(klines_1h))):
            bar_high = Decimal(str(klines_1h['high'].iloc[j]))
            bar_low = Decimal(str(klines_1h['low'].iloc[j]))
            bar_close = Decimal(str(klines_1h['close'].iloc[j]))
            bar_time = klines_1h.index[j]
            
            # 获取对应4h指标（用于BB中轨和RSI检查）
            bb_mid = None
            rsi_4h = None
            if klines_4h is not None and indicators_4h is not None:
                try:
                    i_4h_arr = klines_4h.index.get_indexer([bar_time], method='pad')
                    i_4h = i_4h_arr[0] if len(i_4h_arr) > 0 else -1
                    if i_4h >= 0:
                        bb_mid_series = indicators_4h.get('BB_Middle', pd.Series())
                        rsi_series = indicators_4h.get('RSI', pd.Series())
                        if i_4h < len(bb_mid_series):
                            bb_mid = Decimal(str(bb_mid_series.iloc[i_4h])) if pd.notna(bb_mid_series.iloc[i_4h]) else None
                        if i_4h < len(rsi_series):
                            rsi_4h = float(rsi_series.iloc[i_4h]) if pd.notna(rsi_series.iloc[i_4h]) else None
                except (KeyError, IndexError):
                    pass
            
            if direction == 'LONG':
                # 止损
                if bar_low <= stop_loss:
                    pnl = position_size * (stop_loss - entry_price) / entry_price
                    return (bar_time, stop_loss, '震荡止损', pnl)
                # 止盈
                if bar_high >= take_profit:
                    pnl = position_size * (take_profit - entry_price) / entry_price
                    return (bar_time, take_profit, '震荡止盈', pnl)
                # BB中轨触及退出（价格从下方向上涨到中轨，且距离足够）
                if bb_mid_enabled and bb_mid is not None and bar_high >= bb_mid:
                    bb_distance = bb_mid - entry_price
                    if bb_distance >= atr * bb_mid_min_distance_atr:
                        pnl = position_size * bb_distance / entry_price
                        return (bar_time, bb_mid, '震荡BB中轨', pnl)
                # RSI回归中性退出
                if rsi_4h is not None and rsi_4h >= rsi_neutral_long:
                    pnl = position_size * (bar_close - entry_price) / entry_price
                    return (bar_time, bar_close, '震荡RSI中性', pnl)
            else:
                # 止损
                if bar_high >= stop_loss:
                    pnl = position_size * (entry_price - stop_loss) / entry_price
                    return (bar_time, stop_loss, '震荡止损', pnl)
                # 止盈
                if bar_low <= take_profit:
                    pnl = position_size * (entry_price - take_profit) / entry_price
                    return (bar_time, take_profit, '震荡止盈', pnl)
                # BB中轨触及退出（价格从上方下跌到中轨，且距离足够）
                if bb_mid_enabled and bb_mid is not None and bar_low <= bb_mid:
                    bb_distance = entry_price - bb_mid
                    if bb_distance >= atr * bb_mid_min_distance_atr:
                        pnl = position_size * bb_distance / entry_price
                        return (bar_time, bb_mid, '震荡BB中轨', pnl)
                # RSI回归中性退出
                if rsi_4h is not None and rsi_4h <= rsi_neutral_short:
                    pnl = position_size * (entry_price - bar_close) / entry_price
                    return (bar_time, bar_close, '震荡RSI中性', pnl)
            
            # 时间止损
            if j == entry_idx + max_bars:
                pnl = position_size * (bar_close - entry_price) / entry_price if direction == 'LONG' else position_size * (entry_price - bar_close) / entry_price
                return (bar_time, bar_close, '震荡时间止损', pnl)
        
        # 强制平仓
        last_idx = min(entry_idx + max_bars, len(klines_1h) - 1)
        last_price = Decimal(str(klines_1h['close'].iloc[last_idx]))
        last_time = klines_1h.index[last_idx]
        pnl = position_size * (last_price - entry_price) / entry_price if direction == 'LONG' else position_size * (entry_price - last_price) / entry_price
        return (last_time, last_price, '震荡时间止损', pnl)

    def generate_report(self, results: Dict) -> str:
        """生成回测报告"""
        trades = results['trades']

        if not trades:
            return f"# {self.symbol} 回测报告\n\n无交易信号。"

        # 统计
        winning = [t for t in trades if t['pnl'] > 0]
        losing = [t for t in trades if t['pnl'] <= 0]
        win_rate = len(winning) / len(trades) * 100 if trades else 0

        total_pnl = sum(t['pnl'] for t in trades)
        avg_win = sum(t['pnl'] for t in winning) / len(winning) if winning else 0
        avg_loss = sum(t['pnl'] for t in losing) / len(losing) if losing else 0

        # 最大回撤
        equity_values = [e['equity'] for e in results['equity_curve']]
        max_drawdown = 0.0
        if equity_values:
            peak = equity_values[0]
            max_drawdown = 0
            for v in equity_values:
                peak = max(peak, v)
                drawdown = (peak - v) / peak * 100
                max_drawdown = max(max_drawdown, drawdown)

        # 按等级统计
        grade_stats = {}
        for grade in ['S', 'A', 'B', 'C']:
            grade_trades = [t for t in trades if t['grade'] == grade]
            if grade_trades:
                grade_pnl = sum(t['pnl'] for t in grade_trades)
                grade_win = [t for t in grade_trades if t['pnl'] > 0]
                grade_stats[grade] = {
                    'count': len(grade_trades),
                    'pnl': grade_pnl,
                    'win_rate': len(grade_win) / len(grade_trades) * 100,
                }

        # 按退出原因统计
        exit_reason_stats = {}
        for t in trades:
            reason = t['exit_reason']
            if reason not in exit_reason_stats:
                exit_reason_stats[reason] = {'count': 0, 'pnl': 0}
            exit_reason_stats[reason]['count'] += 1
            exit_reason_stats[reason]['pnl'] += t['pnl']

        report = f"""# {self.symbol} v6.20 回测报告

## 概览

| 指标 | 数值 |
|------|------|
| 回测时间 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |
| 初始资金 | {results['initial_capital']:.2f} USDT |
| 最终资金 | {results['final_capital']:.2f} USDT |
| 总收益率 | **{((results['final_capital'] - results['initial_capital']) / results['initial_capital'] * 100):.2f}%** |
| 总交易次数 | {results['total_trades']} |
| 胜率 | {win_rate:.1f}% |
| 平均盈利 | {avg_win:.2f} USDT |
| 平均亏损 | {avg_loss:.2f} USDT |
| 总盈亏 | {total_pnl:.2f} USDT |
| 最大回撤 | {max_drawdown:.1f}% |

## 按策略模式统计

| 策略模式 | 交易数 | 总盈亏 | 胜率 |
|----------|--------|--------|------|
"""
        # 按策略模式统计（v6.20 新增）
        trend_trades = [t for t in trades if t.get('strategy_mode') != 'ranging']
        ranging_trades = [t for t in trades if t.get('strategy_mode') == 'ranging']
        for mode_name, mode_trades in [('趋势策略', trend_trades), ('震荡策略', ranging_trades)]:
            if mode_trades:
                mode_pnl = sum(t['pnl'] for t in mode_trades)
                mode_win = [t for t in mode_trades if t['pnl'] > 0]
                mode_wr = len(mode_win) / len(mode_trades) * 100
                report += f"| {mode_name} | {len(mode_trades)} | {mode_pnl:.2f} USDT | {mode_wr:.1f}% |\n"
            else:
                report += f"| {mode_name} | 0 | 0.00 USDT | 0.0% |\n"

        report += f"""
## 按等级统计

| 等级 | 交易数 | 总盈亏 | 胜率 |
|------|--------|--------|------|
"""
        for grade in ['S', 'A', 'B', 'C']:
            if grade in grade_stats:
                s = grade_stats[grade]
                report += f"| {grade} | {s['count']} | {s['pnl']:.2f} USDT | {s['win_rate']:.1f}% |\n"

        report += f"""
## 按退出原因统计

| 退出原因 | 交易数 | 总盈亏 |
|----------|--------|--------|
"""
        for reason, s in sorted(exit_reason_stats.items(), key=lambda x: x[1]['count'], reverse=True):
            report += f"| {reason} | {s['count']} | {s['pnl']:.2f} USDT |\n"

        report += f"""
## 方向统计

| 方向 | 交易数 | 总盈亏 |
|------|--------|--------|
"""
        long_trades = [t for t in trades if t['direction'] == 'LONG']
        short_trades = [t for t in trades if t['direction'] == 'SHORT']
        report += f"| 做多 | {len(long_trades)} | {sum(t['pnl'] for t in long_trades):.2f} USDT |\n"
        report += f"| 做空 | {len(short_trades)} | {sum(t['pnl'] for t in short_trades):.2f} USDT |\n"

        # 前 5 笔盈利交易
        sorted_trades = sorted(trades, key=lambda t: t['pnl'], reverse=True)
        report += f"""
## 前 5 笔盈利交易

| 时间 | 方向 | 等级 | 入场价 | 退出价 | 盈亏 | 退出原因 |
|------|------|------|--------|--------|------|----------|
"""
        for t in sorted_trades[:5]:
            report += f"| {t['entry_time'].strftime('%m-%d %H:%M')} | {t['direction']} | {t['grade']} | {t['entry_price']:.2f} | {t['exit_price']:.2f} | {t['pnl']:.2f} | {t['exit_reason']} |\n"

        return report


async def main():
    """主函数"""
    print("=" * 60)
    print("v6.20 震荡市生存版 全币种回测")
    print("=" * 60)

    # 步骤 1: 使用已有数据（从服务器数据库导出）
    end_time = datetime.now()
    start_time = end_time - timedelta(days=BACKTEST_MONTHS * 30)

    print(f"\n📊 使用已有数据开始回测...")

    # 步骤 2: 回测
    all_results = []
    os.makedirs(REPORT_DIR, exist_ok=True)

    for symbol in SYMBOLS:
        print(f"\n  回测 {symbol}...")
        try:
            engine = V620BacktestEngine(symbol)
            klines_1h = engine.load_klines('1h')
            klines_4h = engine.load_klines('4h')
            klines_1d = engine.load_klines('1d')

            # 对齐时间范围
            start = max(klines_1h.index[0], klines_4h.index[0], klines_1d.index[0])
            end = min(klines_1h.index[-1], klines_4h.index[-1], klines_1d.index[-1])
            klines_1h = klines_1h[(klines_1h.index >= start) & (klines_1h.index <= end)]
            klines_4h = klines_4h[(klines_4h.index >= start) & (klines_4h.index <= end)]
            klines_1d = klines_1d[(klines_1d.index >= start) & (klines_1d.index <= end)]

            print(f"    数据范围: {start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}")
            print(f"    1h: {len(klines_1h)} 根, 4h: {len(klines_4h)} 根, 1d: {len(klines_1d)} 根")

            signals = engine.calculate_signals(klines_1h, klines_4h, klines_1d)
            print(f"    信号数: {len(signals)}")

            results = engine.simulate_trades(signals, klines_1h, klines_4h)
            all_results.append(results)

            # 生成报告
            report = engine.generate_report(results)
            report_path = os.path.join(REPORT_DIR, f"backtest_v620_{symbol}.md")
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write(report)
            print(f"    报告: {report_path}")

        except FileNotFoundError as e:
            print(f"    ⚠️ 跳过: {e}")
        except Exception as e:
            print(f"    ❌ 错误: {e}")
            import traceback
            traceback.print_exc()

    # 步骤 3: 汇总报告
    print("\n\n" + "=" * 60)
    print("📊 汇总结果")
    print("=" * 60)

    total_pnl = 0
    total_trades = 0
    summary = "# v6.20 震荡市生存版 全币种回测汇总报告\n\n"
    summary += f"回测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    summary += f"回测周期: {start_time.strftime('%Y-%m-%d')} ~ {end_time.strftime('%Y-%m-%d')}\n\n"
    summary += "| 币种 | 初始资金 | 最终资金 | 收益率 | 交易数 |\n"
    summary += "|------|----------|----------|--------|--------|\n"

    for r in all_results:
        pnl_pct = (r['final_capital'] - r['initial_capital']) / r['initial_capital'] * 100
        total_pnl += r['final_capital'] - r['initial_capital']
        total_trades += r['total_trades']
        summary += f"| {r['symbol']} | {r['initial_capital']:.2f} | {r['final_capital']:.2f} | {pnl_pct:+.2f}% | {r['total_trades']} |\n"
        print(f"  {r['symbol']}: {r['initial_capital']:.2f} → {r['final_capital']:.2f} ({pnl_pct:+.2f}%), {r['total_trades']} 笔")

    total_initial = sum(r['initial_capital'] for r in all_results)
    total_final = sum(r['final_capital'] for r in all_results)
    if total_initial > 0:
        total_pnl_pct = (total_final - total_initial) / total_initial * 100
    else:
        total_pnl_pct = 0

    summary += f"| **合计** | **{total_initial:.2f}** | **{total_final:.2f}** | **{total_pnl_pct:+.2f}%** | **{total_trades}** |\n"

    print(f"\n  合计: {total_initial:.2f} → {total_final:.2f} ({total_pnl_pct:+.2f}%), {total_trades} 笔")

    summary_path = os.path.join(REPORT_DIR, "backtest_v620_summary.md")
    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write(summary)
    print(f"\n  汇总报告: {summary_path}")


if __name__ == "__main__":
    asyncio.run(main())