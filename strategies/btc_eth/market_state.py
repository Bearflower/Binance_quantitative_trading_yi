"""
市场状态识别模块（v6.20 震荡市生存版）

根据 4h K线布林带宽度、ADX、价格变化、日线EMA21斜率、连续K线确认
判断当前市场状态：
- STRONG_TREND（强趋势市）：同时满足5个条件
- RANGING（震荡市）：不满足强趋势条件

核心理念：宁可错过，不可做错。只有真正强趋势才开仓。
"""
import pandas as pd
import numpy as np
from typing import Dict, Tuple, Optional
from enum import Enum


class MarketState(Enum):
    """市场状态枚举（v6.19：仅二态，取消WEAK_TREND）"""
    STRONG_TREND = "STRONG_TREND"   # 强趋势市 - 仅S级开仓
    RANGING = "RANGING"             # 震荡市 - 完全禁止开仓


def _check_consecutive_ema21(df_4h: pd.DataFrame, n: int = 3) -> bool:
    """
    检查最近n根4h K线收盘价是否连续在EMA21同侧

    条件：最近3根K线的收盘价全部 > EMA21（多头）或全部 < EMA21（空头）
    这表明趋势具有持续性，而非短暂的假突破。

    Args:
        df_4h: 4h K线DataFrame（需包含 close 和 MA21 列）
        n: 连续K线数量

    Returns:
        True 如果连续n根K线在EMA21同侧
    """
    if len(df_4h) < n:
        return False

    if 'close' not in df_4h.columns or 'MA21' not in df_4h.columns:
        return False

    closes = df_4h['close'].iloc[-n:]
    ema21s = df_4h['MA21'].iloc[-n:]

    if closes.isna().any() or ema21s.isna().any():
        return False

    # 全部在EMA21上方
    all_above = (closes > ema21s).all()
    # 全部在EMA21下方
    all_below = (closes < ema21s).all()

    return all_above or all_below


def get_market_state(
    indicators_4h: Dict[str, pd.Series],
    close_prices: Optional[pd.Series] = None,
    indicators_1d: Optional[Dict[str, pd.Series]] = None,
    config: Optional[Dict] = None
) -> Tuple[MarketState, str]:
    """
    判断市场状态（v6.19 极端收紧版）

    仅当同时满足以下5个条件时，才判定为 STRONG_TREND：
    1. ADX > 35（而非 v6.18 的 30）
    2. 布林带宽度 > 7%（而非 v6.18 的 6%）
    3. 过去10根4h K线价格变化 > 5%（而非 v6.18 的 3%）
    4. 日线 EMA21 斜率绝对值 > 0.15%（确保日线级别趋势明确）
    5. 连续3根4h K线收盘价在 EMA21 同侧（确保持续性）

    不满足任何条件 → RANGING（震荡市，完全禁止开仓）

    Args:
        indicators_4h: 4h技术指标字典（含 BB_Upper, BB_Middle, BB_Lower, ADX, MA21）
        close_prices: 4h收盘价 Series（用于价格变化和连续检查）
        indicators_1d: 1d技术指标字典（含 MA21，用于日线斜率计算）
        config: 市场状态配置

    Returns:
        (MarketState, 状态描述)
    """
    # 默认阈值（v6.19：极致收紧）
    adx_threshold = 35
    bb_width_threshold = 0.07
    price_change_threshold = 5.0
    daily_slope_threshold = 0.15  # 日线EMA21斜率百分比

    if config:
        adx_threshold = config.get('strong_trend_adx', 35)
        bb_width_threshold = config.get('strong_trend_bb_width', 0.07)
        price_change_threshold = config.get('strong_trend_price_change', 5.0)
        daily_slope_threshold = config.get('strong_trend_daily_slope', 0.15)

    # === 条件1：ADX ===
    if 'ADX' not in indicators_4h:
        return MarketState.RANGING, "ADX缺失，默认震荡市"
    adx = indicators_4h['ADX'].iloc[-1]
    if pd.isna(adx) or adx <= adx_threshold:
        return MarketState.RANGING, f"ADX={adx:.1f} ≤ {adx_threshold}，震荡市"

    # === 条件2：布林带宽度 ===
    if not all(k in indicators_4h for k in ['BB_Upper', 'BB_Middle', 'BB_Lower']):
        return MarketState.RANGING, "布林带缺失，震荡市"
    bb_upper = indicators_4h['BB_Upper'].iloc[-1]
    bb_middle = indicators_4h['BB_Middle'].iloc[-1]
    bb_lower = indicators_4h['BB_Lower'].iloc[-1]
    if pd.isna(bb_upper) or pd.isna(bb_middle) or pd.isna(bb_lower) or bb_middle == 0:
        return MarketState.RANGING, "布林带数据无效，震荡市"
    bb_width = (bb_upper - bb_lower) / bb_middle
    if bb_width <= bb_width_threshold:
        return MarketState.RANGING, f"BB宽度={bb_width:.3f} ≤ {bb_width_threshold}，震荡市"

    # === 条件3：价格变化 ===
    price_change = 0
    if close_prices is not None and len(close_prices) >= 10:
        close_now = close_prices.iloc[-1]
        close_10ago = close_prices.iloc[-10]
        if pd.notna(close_now) and pd.notna(close_10ago) and close_10ago > 0:
            price_change = abs((close_now / close_10ago - 1) * 100)
    if price_change <= price_change_threshold:
        return MarketState.RANGING, f"价格变化={price_change:.1f}% ≤ {price_change_threshold}%，震荡市"

    # === 条件4：日线EMA21斜率 ===
    daily_slope = 0
    if indicators_1d is not None and 'MA21' in indicators_1d:
        ma21_1d = indicators_1d['MA21']
        if len(ma21_1d) >= 2:
            ma21_now = ma21_1d.iloc[-1]
            ma21_prev = ma21_1d.iloc[-2]
            if pd.notna(ma21_now) and pd.notna(ma21_prev) and ma21_prev > 0:
                daily_slope = abs((ma21_now / ma21_prev - 1) * 100)
    if daily_slope <= daily_slope_threshold:
        return MarketState.RANGING, f"日线EMA21斜率={daily_slope:.3f}% ≤ {daily_slope_threshold}%，震荡市"

    # === 条件5：连续3根4h K线在EMA21同侧 ===
    # 需要构造4h DataFrame用于检查
    if close_prices is not None and 'MA21' in indicators_4h:
        df_4h_check = pd.DataFrame({
            'close': close_prices.values,
            'MA21': indicators_4h['MA21'].values
        })
        if not _check_consecutive_ema21(df_4h_check, 3):
            return MarketState.RANGING, "4h K线未连续3根在EMA21同侧，震荡市"
    else:
        return MarketState.RANGING, "缺少4h收盘价或MA21，震荡市"

    # 全部5个条件满足！
    return MarketState.STRONG_TREND, \
        f"强趋势市 (ADX={adx:.1f}, BB={bb_width:.3f}, 价格变化={price_change:.1f}%, 日线斜率={daily_slope:.3f}%, 连续确认)"


def get_market_state_behavior(
    state: MarketState,
    config: Optional[Dict] = None
) -> Dict:
    """
    根据市场状态返回策略行为配置（v6.20 震荡市生存版）

    STRONG_TREND：趋势策略，仅S级，止损1.5×ATR
    RANGING：震荡策略，允许S/A级，止损1.0×ATR（由震荡策略接管）

    行为参数优先从 config.behaviors 读取，未配置时使用默认值。

    Args:
        state: 市场状态
        config: 市场状态配置

    Returns:
        行为配置字典
    """
    if config is None:
        config = {}

    # 读取震荡市策略配置
    ranging_config = config.get('ranging_strategy', {})
    ranging_risk = ranging_config.get('risk', {})

    # 从配置读取行为参数，未配置时使用默认值
    behaviors_config = config.get('behaviors', {})
    trend_cfg = behaviors_config.get('STRONG_TREND', {})
    ranging_cfg = behaviors_config.get('RANGING', {})

    behaviors = {
        MarketState.STRONG_TREND: {
            'can_trade': trend_cfg.get('can_trade', True),
            'strategy_mode': trend_cfg.get('strategy_mode', 'trend'),
            'min_grade': trend_cfg.get('min_grade', 'S'),
            'vol_boost': trend_cfg.get('vol_boost', 0.0),
            'position_ratio_mult': trend_cfg.get('position_ratio_mult', 0.5),
            'stop_loss_atr': trend_cfg.get('stop_loss_atr', 1.5),
            'max_daily_trades': trend_cfg.get('max_daily_trades', 2),
            'ranging_symbol_cooldown_hours': trend_cfg.get('ranging_symbol_cooldown_hours', 72),
        },
        MarketState.RANGING: {
            'can_trade': ranging_cfg.get('can_trade', True),
            'strategy_mode': ranging_cfg.get('strategy_mode', 'ranging'),
            'min_grade': ranging_cfg.get('min_grade', 'A'),
            'vol_boost': ranging_cfg.get('vol_boost', 0.0),
            'position_ratio_mult': ranging_cfg.get('position_ratio_mult', 0.3),
            'stop_loss_atr': ranging_cfg.get('stop_loss_atr', ranging_risk.get('stop_loss_atr', 1.0)),
            'max_daily_trades': ranging_cfg.get('max_daily_trades', 3),
            'ranging_symbol_cooldown_hours': ranging_cfg.get('ranging_symbol_cooldown_hours', 3),
        },
    }

    return behaviors.get(state, behaviors[MarketState.RANGING])


def get_market_state_simple(
    df_4h: pd.DataFrame, 
    df_1d: Optional[pd.DataFrame] = None,
    config: Optional[Dict] = None
) -> MarketState:
    """
    简化版市场状态判断（从 DataFrame 直接计算）
    用于回测场景

    Args:
        df_4h: 4h K线DataFrame（需包含 BB_Upper, BB_Middle, BB_Lower, ADX, MA21, close）
        df_1d: 1d K线DataFrame（需包含 MA21，可选）
        config: 市场状态配置（可选，用于读取阈值）

    Returns:
        MarketState
    """
    # 从配置读取阈值，未配置时使用默认值
    adx_threshold = 35
    bb_width_threshold = 0.07
    price_change_threshold = 5.0
    daily_slope_threshold = 0.15
    if config:
        adx_threshold = config.get('strong_trend_adx', 35)
        bb_width_threshold = config.get('strong_trend_bb_width', 0.07)
        price_change_threshold = config.get('strong_trend_price_change', 5.0)
        daily_slope_threshold = config.get('strong_trend_daily_slope', 0.15)

    # 条件1: ADX
    adx = df_4h['ADX'].iloc[-1]
    if adx <= adx_threshold:
        return MarketState.RANGING

    # 条件2: BB宽度
    bb_width = (df_4h['BB_Upper'].iloc[-1] - df_4h['BB_Lower'].iloc[-1]) / df_4h['BB_Middle'].iloc[-1]
    if bb_width <= bb_width_threshold:
        return MarketState.RANGING

    # 条件3: 价格变化
    price_change = abs((df_4h['close'].iloc[-1] / df_4h['close'].iloc[-10] - 1) * 100)
    if price_change <= price_change_threshold:
        return MarketState.RANGING

    # 条件4: 日线EMA21斜率
    if df_1d is not None and 'MA21' in df_1d.columns and len(df_1d) >= 2:
        daily_slope = abs((df_1d['MA21'].iloc[-1] / df_1d['MA21'].iloc[-2] - 1) * 100)
        if daily_slope <= daily_slope_threshold:
            return MarketState.RANGING
    else:
        return MarketState.RANGING

    # 条件5: 连续3根4h K线在EMA21同侧
    if not _check_consecutive_ema21(df_4h, 3):
        return MarketState.RANGING

    return MarketState.STRONG_TREND