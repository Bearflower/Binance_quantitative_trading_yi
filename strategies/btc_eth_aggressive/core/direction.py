from __future__ import annotations

"""core.direction：方向判定、震荡三条件投票、方向对齐校验。

来源（strategy.py 行号）：
- _determine_direction         2267-2308
- _bb_touch_votes              1854-1893  (staticmethod)
- _rsi_extreme_votes           1895-1919  (staticmethod)
- _reversal_pattern_votes      1921-1952  (staticmethod)
- _vote_ranging_direction      1954-2000
- _check_direction_alignment   2002-2027  (staticmethod)

内部共用 safe_last（从 core.common 导入）替代 strategy 模块级函数。
所有 self._xxx 方法调用改为同模块函数 xxx()；self.timeframes 改为形参 timeframes。
"""

from typing import Dict, List, Optional, Tuple

import pandas as pd
import structlog

from .common import safe_last, safe_last_klines_value

logger = structlog.get_logger()


def determine_direction(indicators: Dict, timeframes: List[str]) -> str:
    """确定交易方向：多时间框架 MA21 vs MA55 + MACD 投票。"""
    long_votes = 0
    short_votes = 0

    # 检查各时间框架（从配置读取，跳过日线等不适合方向判断的时间框架）
    direction_timeframes = [tf for tf in timeframes if tf in ('1h', '4h')]
    for timeframe in direction_timeframes:
        if timeframe in indicators:
            ma21 = indicators[timeframe]['MA21'].iloc[-1]
            ma55 = indicators[timeframe]['MA55'].iloc[-1]
            macd = indicators[timeframe]['MACD'].iloc[-1]

            if pd.notna(ma21) and pd.notna(ma55):
                if ma21 > ma55:
                    long_votes += 1
                else:
                    short_votes += 1

            if pd.notna(macd):
                if macd > 0:
                    long_votes += 1
                else:
                    short_votes += 1

    # 根据投票结果决定方向
    if long_votes > short_votes:
        return 'LONG'
    else:
        return 'SHORT'


def bb_touch_votes(
    df_4h: pd.DataFrame, indicators: Dict, entry_conditions: Dict
) -> Tuple[int, int, List[str], float, float]:
    """BB半区投票：价格位于布林中轨下方投多、上方投空。

    返回 (多票增量, 空票增量, 命中描述列表, 距下轨比例, 距上轨比例)。
    禁用或数据缺失时返回 0 票，诊断比例取默认 1.0。
    """
    if not entry_conditions.get('bb_touch', True):
        return 0, 0, [], 1.0, 1.0
    if df_4h.empty or 'close' not in df_4h.columns:
        return 0, 0, [], 1.0, 1.0
    close_4h_raw = df_4h['close'].iloc[-1]
    if pd.isna(close_4h_raw):
        return 0, 0, [], 1.0, 1.0
    close_4h = float(close_4h_raw)
    bb_upper = safe_last(indicators, '4h', 'BB_Upper')
    bb_middle = safe_last(indicators, '4h', 'BB_Middle')
    bb_lower = safe_last(indicators, '4h', 'BB_Lower')
    if bb_upper is None or bb_middle is None or bb_lower is None:
        return 0, 0, [], 1.0, 1.0
    bb_range = bb_upper - bb_lower
    if bb_range <= 0:
        return 0, 0, [], 1.0, 1.0
    dist_to_lower = (close_4h - bb_lower) / bb_range
    dist_to_upper = (bb_upper - close_4h) / bb_range
    long_delta = 0
    short_delta = 0
    conds: List[str] = []
    if close_4h < bb_middle:
        long_delta += 1
        conds.append(f"BB下半区(距下轨{dist_to_lower*100:.1f}%)")
    elif close_4h > bb_middle:
        short_delta += 1
        conds.append(f"BB上半区(距上轨{dist_to_upper*100:.1f}%)")
    return long_delta, short_delta, conds, dist_to_lower, dist_to_upper


def rsi_extreme_votes(
    indicators: Dict, entry_conditions: Dict
) -> Tuple[int, int, List[str], float]:
    """RSI极端投票：超卖投多、超买投空。

    返回 (多票增量, 空票增量, 命中描述列表, RSI 诊断值)。
    禁用或数据缺失时返回 0 票，诊断值取默认 0.0。
    """
    if not entry_conditions.get('rsi_extreme', True):
        return 0, 0, [], 0.0
    rsi = safe_last(indicators, '4h', 'RSI')
    if rsi is None:
        return 0, 0, [], 0.0
    oversold = entry_conditions.get('rsi_oversold', 20)
    overbought = entry_conditions.get('rsi_overbought', 80)
    long_delta = 0
    short_delta = 0
    conds: List[str] = []
    if rsi < oversold:
        long_delta += 1
        conds.append(f"RSI超卖({rsi:.1f}<{oversold})")
    if rsi > overbought:
        short_delta += 1
        conds.append(f"RSI超买({rsi:.1f}>{overbought})")
    return long_delta, short_delta, conds, rsi


def reversal_pattern_votes(
    df_4h: pd.DataFrame, entry_conditions: Dict
) -> Tuple[int, int, List[str]]:
    """反转K线形态投票：看涨吞没投多、看跌吞没投空。"""
    if not entry_conditions.get('reversal_pattern', True):
        return 0, 0, []
    if len(df_4h) < 2:
        return 0, 0, []
    prev_open_raw = df_4h['open'].iloc[-2]
    prev_close_raw = df_4h['close'].iloc[-2]
    curr_open_raw = df_4h['open'].iloc[-1]
    curr_close_raw = df_4h['close'].iloc[-1]
    if any(pd.isna(v) for v in (prev_open_raw, prev_close_raw, curr_open_raw, curr_close_raw)):
        return 0, 0, []
    prev_open = float(prev_open_raw)
    prev_close = float(prev_close_raw)
    curr_open = float(curr_open_raw)
    curr_close = float(curr_close_raw)
    long_delta = 0
    short_delta = 0
    conds: List[str] = []
    # 看涨吞没：前阴后阳，后实体包住前实体
    if (prev_close < prev_open and curr_close > curr_open and
            curr_open <= prev_close and curr_close >= prev_open):
        long_delta += 1
        conds.append("看涨吞没")
    # 看跌吞没：前阳后阴，后实体包住前实体
    if (prev_close > prev_open and curr_close < curr_open and
            curr_open >= prev_close and curr_close <= prev_open):
        short_delta += 1
        conds.append("看跌吞没")
    return long_delta, short_delta, conds


def vote_ranging_direction(
    indicators: Dict, klines: Dict, entry_conditions: Dict
) -> Tuple[Optional[str], str, List[str]]:
    """三条件投票得出震荡反转候选方向。

    依次汇总 BB 触轨、RSI 极端、反转 K 线形态三项投票，多数方向获胜；
    无票或平票返回 None。
    """
    df_4h = pd.DataFrame(klines['4h'])
    long_votes = 0
    short_votes = 0
    conditions_met: List[str] = []

    ld, sd, conds, dist_to_lower, dist_to_upper = bb_touch_votes(
        df_4h, indicators, entry_conditions)
    long_votes += ld
    short_votes += sd
    conditions_met.extend(conds)

    ld, sd, conds, rsi = rsi_extreme_votes(indicators, entry_conditions)
    long_votes += ld
    short_votes += sd
    conditions_met.extend(conds)

    ld, sd, conds = reversal_pattern_votes(df_4h, entry_conditions)
    long_votes += ld
    short_votes += sd
    conditions_met.extend(conds)

    if long_votes == 0 and short_votes == 0:
        bb_info = (
            f"BB距下轨{dist_to_lower*100:.1f}%/距上轨{dist_to_upper*100:.1f}%"
            if entry_conditions.get('bb_touch', True) else "BB=禁用"
        )
        rsi_info = (
            f"RSI={rsi:.1f}"
            if entry_conditions.get('rsi_extreme', True) else "RSI=禁用"
        )
        return None, f"无震荡入场条件满足({bb_info}, {rsi_info})", conditions_met

    if long_votes > short_votes:
        direction = 'LONG'
    elif short_votes > long_votes:
        direction = 'SHORT'
    else:
        return None, f"方向不一致(long={long_votes}, short={short_votes})", conditions_met

    return direction, "", conditions_met


def check_direction_alignment(
    direction: str, indicators: Dict, ranging_config: Dict
) -> Tuple[bool, str]:
    """机制①：方向一致性对齐。

    校验日线均线排列与候选方向是否一致：多头排列禁止做空、空头排列禁止做多；
    均线缺失/NaN/粘合按 CP-2 保守拒绝（fallback_on_missing=false）。
    """
    config = ranging_config.get('direction_alignment', {})
    if not config.get('enabled', True):
        return True, ""
    fast = safe_last(indicators, '1d', config.get('fast_ma', 'EMA21'))
    slow = safe_last(indicators, '1d', config.get('slow_ma', 'EMA55'))
    if fast is None or slow is None or slow == 0:
        if config.get('fallback_on_missing', False):
            logger.warning("均线数据缺失，放行方向护栏")
            return True, ""
        return False, "方向一致性拒绝：日线均线数据缺失"
    stick_threshold = config.get('stick_threshold_pct', 0.3)
    if abs(fast - slow) / slow * 100 <= stick_threshold:
        return False, "方向一致性拒绝：日线均线粘合"
    if fast > slow and direction == 'SHORT':
        return False, "方向一致性拒绝：多头排列禁止做空"
    if fast < slow and direction == 'LONG':
        return False, "方向一致性拒绝：空头排列禁止做多"
    return True, ""
