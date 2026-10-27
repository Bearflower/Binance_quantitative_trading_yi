from __future__ import annotations

"""core.scoring：评分三件套、形态检测、综合评分与等级判定。

来源（strategy.py 行号）：
- _calculate_score             1374-1434
- _calculate_trend_strength_score 1436-1495
- _calculate_pattern_quality_score 1497-1551
- _detect_bullish_engulfing    1553-1584
- _detect_bearish_engulfing    1586-1617
- _detect_breakout_pullback    1619-1657
- _detect_divergence           1659-1702
- _calculate_momentum_divergence_score 1704-1751
- _determine_grade             2225-2251

所有方法从 strategy 实例方法改为模块级函数：self.scoring_config → scoring_cfg、
self.symbol_config → symbol_config、self.risk_config → risk_config、
self._xxx(...) → 同模块函数 xxx(...)。logger.debug/info 原样保留。
"""

from typing import Any, Dict, Optional

import pandas as pd
import structlog

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# 评分三件套
# ---------------------------------------------------------------------------

def trend_strength_score(indicators: Dict, scoring_cfg: Dict) -> float:
    """计算趋势强度评分（0-100）。

    评分标准：
    - 1h和4h时间框架的MA21 > MA55（上升趋势）：基础分
    - 1h和4h时间框架的MA21 < MA55（下降趋势）：基础分（略低）
    - MACD在零轴上方：加分
    - 多时间框架趋势一致：加分
    - ADX > 阈值（强趋势）：加分
    """
    score = float(scoring_cfg['trend_strength']['base_score'])

    # 检查1h和4h的MA趋势
    if '1h' in indicators and '4h' in indicators:
        ma21_1h = indicators['1h']['MA21'].iloc[-1]
        ma55_1h = indicators['1h']['MA55'].iloc[-1]
        ma21_4h = indicators['4h']['MA21'].iloc[-1]
        ma55_4h = indicators['4h']['MA55'].iloc[-1]

        # 1h趋势
        trend_1h = ma21_1h > ma55_1h
        # 4h趋势
        trend_4h = ma21_4h > ma55_4h

        # 趋势一致加分
        if trend_1h == trend_4h:
            score += scoring_cfg['trend_strength']['consistency_bonus']

        # 上升趋势加分
        if trend_1h and trend_4h:
            score += scoring_cfg['trend_strength']['dual_uptrend_bonus']
        elif not trend_1h and not trend_4h:
            # 下降趋势也有一定分数（保持原逻辑，无额外加分）
            pass

    # 检查MACD
    if '1h' in indicators:
        macd = indicators['1h']['MACD'].iloc[-1]
        if macd > 0:
            score += scoring_cfg['trend_strength']['macd_positive_bonus']
        else:
            score += scoring_cfg['trend_strength']['macd_negative_penalty']

    # 检查ADX趋势强度
    if '1h' in indicators:
        adx = indicators['1h']['ADX'].iloc[-1]
        if pd.notna(adx):
            if adx > scoring_cfg['trend_strength']['adx_strong_threshold']:
                score += scoring_cfg['trend_strength']['adx_strong_bonus']
            elif adx >= scoring_cfg['trend_strength']['adx_medium_threshold']:
                score += scoring_cfg['trend_strength']['adx_medium_bonus']

    return max(0, min(100, score))


def pattern_quality_score(
    indicators: Dict, klines: Dict, scoring_cfg: Dict
) -> float:
    """计算形态质量评分（0-100）。

    识别的形态：
    - 阳包阴：看涨形态，加分
    - 阴包阳：看跌形态，加分
    - 突破回踩：趋势延续形态，加分
    - 背离：反转信号，加分
    """
    score = float(scoring_cfg['pattern_quality']['base_score'])

    if '1h' not in klines:
        return score

    # 获取最近几根K线
    recent_klines = klines['1h'][-5:]
    if len(recent_klines) < 3:
        return score

    # 转换为DataFrame方便计算
    df = pd.DataFrame(recent_klines)
    df['open'] = pd.to_numeric(df['open'])
    df['high'] = pd.to_numeric(df['high'])
    df['low'] = pd.to_numeric(df['low'])
    df['close'] = pd.to_numeric(df['close'])

    # 检测阳包阴形态（看涨）
    if detect_bullish_engulfing(df):
        score += scoring_cfg['pattern_quality']['bullish_engulfing_bonus']
        logger.debug("检测到阳包阴形态")

    # 检测阴包阳形态（看跌）
    if detect_bearish_engulfing(df):
        score += scoring_cfg['pattern_quality']['bearish_engulfing_bonus']
        logger.debug("检测到阴包阳形态")

    # 检测突破回踩形态
    if detect_breakout_pullback(df, indicators, scoring_cfg):
        score += scoring_cfg['pattern_quality']['breakout_pullback_bonus']
        logger.debug("检测到突破回踩形态")

    # 检测背离形态
    if detect_divergence(indicators, klines):
        score += scoring_cfg['pattern_quality']['divergence_bonus']
        logger.debug("检测到背离形态")

    return max(0, min(100, score))


def momentum_divergence_score(
    indicators: Dict, klines: Dict, scoring_cfg: Dict
) -> float:
    """计算动量背离评分（0-100）。

    评分标准：
    - RSI在30-70之间（正常区间）：基础分
    - RSI < 30（超卖）：加分（买入机会）
    - RSI > 70（超买）：扣分（风险较高）
    - MACD柱状图为正：加分
    - MACD柱状图为负：扣分
    - 存在背离：加分
    """
    score = float(scoring_cfg['momentum_divergence']['base_score'])

    if '1h' in indicators:
        rsi = indicators['1h']['RSI'].iloc[-1]

        if pd.notna(rsi):
            if rsi < scoring_cfg['momentum_divergence']['rsi_oversold']:
                # 超卖，买入机会
                score += scoring_cfg['momentum_divergence']['rsi_oversold_bonus']
            elif rsi > scoring_cfg['momentum_divergence']['rsi_overbought']:
                # 超买，风险较高
                score += scoring_cfg['momentum_divergence']['rsi_overbought_penalty']
            else:
                # 正常区间
                score += scoring_cfg['momentum_divergence']['rsi_normal_bonus']

        # MACD柱状图
        macd_hist = indicators['1h']['MACD_Hist'].iloc[-1]
        if pd.notna(macd_hist):
            if macd_hist > 0:
                score += scoring_cfg['momentum_divergence']['macd_hist_positive_bonus']
            else:
                score += scoring_cfg['momentum_divergence']['macd_hist_negative_penalty']

        # 检测背离加分
        if detect_divergence(indicators, klines):
            score += scoring_cfg['momentum_divergence']['divergence_bonus']

    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# 形态检测
# ---------------------------------------------------------------------------

def detect_bullish_engulfing(df: pd.DataFrame) -> bool:
    """检测阳包阴形态：前一根阴线、当前阳线、当前实体完全包住前一根。"""
    if len(df) < 2:
        return False

    prev = df.iloc[-2]
    curr = df.iloc[-1]

    # 前一根是阴线
    prev_is_bearish = prev['close'] < prev['open']
    # 当前是阳线
    curr_is_bullish = curr['close'] > curr['open']
    # 当前实体包含前一根实体
    curr_engulfs = (
        curr['open'] <= prev['close'] and
        curr['close'] >= prev['open']
    )

    return prev_is_bearish and curr_is_bullish and curr_engulfs


def detect_bearish_engulfing(df: pd.DataFrame) -> bool:
    """检测阴包阳形态：前一根阳线、当前阴线、当前实体完全包住前一根。"""
    if len(df) < 2:
        return False

    prev = df.iloc[-2]
    curr = df.iloc[-1]

    # 前一根是阳线
    prev_is_bullish = prev['close'] > prev['open']
    # 当前是阴线
    curr_is_bearish = curr['close'] < curr['open']
    # 当前实体包含前一根实体
    curr_engulfs = (
        curr['open'] >= prev['close'] and
        curr['close'] <= prev['open']
    )

    return prev_is_bullish and curr_is_bearish and curr_engulfs


def detect_breakout_pullback(
    df: pd.DataFrame, indicators: Dict, scoring_cfg: Dict
) -> bool:
    """检测突破回踩形态：价格突破MA21、回踩至MA21附近、MA21上升。"""
    if '1h' not in indicators:
        return False

    if len(df) < 5:
        return False

    ma21 = indicators['1h']['MA21'].iloc[-1]
    current_close = df.iloc[-1]['close']

    # 检查是否在MA21附近（从配置读取阈值）
    proximity_pct = scoring_cfg['breakout_pullback']['proximity_pct']
    distance_ratio = abs(current_close - ma21) / ma21
    near_ma21 = distance_ratio < proximity_pct

    # 检查MA21是否上升
    ma21_prev = indicators['1h']['MA21'].iloc[-5]
    ma21_rising = ma21 > ma21_prev

    # 检查之前是否突破MA21
    prev_closes = df.iloc[-5:-1]['close']
    broke_above = any(close > ma21_prev for close in prev_closes)

    return near_ma21 and ma21_rising and broke_above


def detect_divergence(indicators: Dict, klines: Dict) -> bool:
    """检测背离形态：价格创新高/低但RSI未创新高/低（顶/底背离）。"""
    if '1h' not in indicators or '1h' not in klines:
        return False

    # 获取最近20根K线
    closes = [float(k['close']) for k in klines['1h'][-20:]]
    rsi_values = indicators['1h']['RSI'].iloc[-20:].values

    if len(closes) < 20 or len(rsi_values) < 20:
        return False

    # 检测顶背离
    price_high = max(closes[-10:])
    price_prev_high = max(closes[-20:-10])
    rsi_high = max(rsi_values[-10:])
    rsi_prev_high = max(rsi_values[-20:-10])

    if price_high > price_prev_high and rsi_high < rsi_prev_high:
        return True

    # 检测底背离
    price_low = min(closes[-10:])
    price_prev_low = min(closes[-20:-10])
    rsi_low = min(rsi_values[-10:])
    rsi_prev_low = min(rsi_values[-20:-10])

    if price_low < price_prev_low and rsi_low > rsi_prev_low:
        return True

    return False


# ---------------------------------------------------------------------------
# 综合评分 + 等级
# ---------------------------------------------------------------------------

def calculate_score(
    indicators: Dict,
    klines: Dict,
    scoring_cfg: Dict,
    risk_config: Dict,
    market_state: str,
) -> float:
    """计算综合评分（0-100）。

    评分体系：
    - 趋势强度（趋势市25%/震荡市15%）
    - 形态质量（趋势市50%/震荡市60%）
    - 动量背离（趋势市25%/震荡市25%）

    震荡市权重从 risk_config.ranging_strategy.scoring_weights 读取，
    否则回退 scoring_cfg.weights。
    """
    score = 0.0

    # 震荡市使用不同权重（形态权重提高，趋势权重降低）
    if market_state == 'RANGING':
        ranging_config = risk_config.get('ranging_strategy', {})
        weights = ranging_config.get('scoring_weights', {
            'trend_strength': 0.15,
            'pattern_quality': 0.60,
            'momentum_divergence': 0.25,
        })
    else:
        weights = scoring_cfg['weights']

    # 趋势强度评分
    trend_score = trend_strength_score(indicators, scoring_cfg)
    score += trend_score * weights['trend_strength']

    # 形态质量评分
    pattern_score = pattern_quality_score(indicators, klines, scoring_cfg)
    score += pattern_score * weights['pattern_quality']

    # 动量背离评分
    momentum_score = momentum_divergence_score(indicators, klines, scoring_cfg)
    score += momentum_score * weights['momentum_divergence']

    # A级额外加分：4h RSI 在 35~65 之间 +2 分（v6.16.10）
    if '4h' in indicators:
        rsi_4h = indicators['4h']['RSI'].iloc[-1]
        bonus_config = scoring_cfg.get('a_level_bonus', {})
        if pd.notna(rsi_4h):
            rsi_low = bonus_config.get('rsi_low', 35)
            rsi_high = bonus_config.get('rsi_high', 65)
            if rsi_low <= rsi_4h <= rsi_high:
                score += bonus_config.get('bonus', 2)

    logger.debug(
        "评分计算完成",
        trend_strength_score=trend_score,
        pattern_quality_score=pattern_score,
        momentum_divergence_score=momentum_score,
        total_score=score
    )

    return round(score, 2)


def determine_grade(
    score: float,
    scoring_cfg: Dict,
    symbol_config: Dict,
    symbol: Optional[str] = None,
) -> str:
    """确定信号等级（S/A/B/C），支持币种差异化S级阈值。"""
    thresholds = scoring_cfg['grade_thresholds']

    # 币种差异化 S 级阈值（v6.16.10）
    if symbol and symbol in symbol_config:
        s_threshold = symbol_config[symbol].get('s_min_score', thresholds['S'])
    else:
        s_threshold = thresholds['S']

    if score >= s_threshold:
        return 'S'
    elif score >= thresholds['A']:
        return 'A'
    elif score >= thresholds['B']:
        return 'B'
    else:
        return 'C'
