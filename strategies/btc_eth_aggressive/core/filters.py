from __future__ import annotations

"""core.filters：趋势过滤、禁止入场（K线 spike 部分）、成交量过滤、
震荡侧过热保护 / 波动熔断（纯计算部分）、量价确认、should_keep_position 纯判定。

来源（strategy.py 行号）：
- _check_trend_filter            1753-1852
- _check_prohibited_conditions   2310-2364  仅提取第 1 项（K 线 spike）
- _check_volume_filter           2366-2422
- _evaluate_overheat             2043-2085  (staticmethod)
- _check_overheat_ban            2087-2095
- _apply_overheat_downgrade      2097-2110
- _check_volume_confirm          2112-2140  (staticmethod)
- _check_volatility_regime       2142-2173  纯计算部分（去掉 symbol 暂停）
- _should_keep_position          5044-5089  纯判定（数据预检 + 三机制）

常量副本 _GRADE_LADDER 来源于 strategy.BTCEthStrategy._GRADE_LADDER（574 行），
避免 core 反向 import strategy 造成循环依赖。
"""

from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import structlog

from .common import safe_last, safe_last_klines_value
from .direction import check_direction_alignment

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# 常量副本：来源 strategy.BTCEthStrategy._GRADE_LADDER（574 行）
# ---------------------------------------------------------------------------
_GRADE_LADDER = ['S', 'A', 'B', 'C']


def check_trend_filter(
    indicators: Dict, klines: Dict, risk_config: Dict
) -> Tuple[bool, str]:
    """趋势过滤器（硬性条件）——纯计算版（去掉未使用的 symbol 形参）。

    多头方向：日线收盘价 > 日线 EMA55；日线 EMA21 斜率 > 0.05%；
    4h 价格回调至 EMA21 附近（≤ 1.5×ATR）；1h 收盘价 > 1h EMA21。
    禁止入场：日线 EMA21 斜率绝对值 < 0.03%；ATR/价格 > 4.5% 或 < 1.0%。

    Returns:
        (是否通过, 方向或失败原因)
    """
    config = risk_config.get('trend_filter', {})
    if not config.get('enabled', True):
        return True, ""

    # 1. 日线趋势判断
    if '1d' not in indicators:
        return False, "日线数据缺失"

    df_1d = pd.DataFrame(klines['1d'])
    close_1d = Decimal(str(df_1d['close'].iloc[-1]))
    ema55_1d = indicators['1d']['EMA55'].iloc[-1]

    # 日线 EMA21 斜率（从配置读取窗口大小）
    slope_window = config.get('ema_slope_window', 5)
    ema21_series = indicators['1d']['MA21'].iloc[-slope_window:]
    if len(ema21_series) >= slope_window:
        x = np.arange(slope_window)
        slope, _ = np.polyfit(x, ema21_series.values, 1)
        slope_pct = slope / ema21_series.iloc[-1]
    else:
        slope_pct = 0

    # 禁止：斜率过平
    if abs(slope_pct) < config['ema_slope_flat']:
        return False, f"日线EMA21斜率过平({slope_pct*100:.2f}%)"

    # 确定方向
    if slope_pct > 0:
        direction = 'LONG'
    else:
        direction = 'SHORT'

    # 做多硬性条件
    if direction == 'LONG':
        if close_1d <= ema55_1d:
            return False, "日线收盘价未站上EMA55"
        if slope_pct < config['ema_slope_min']:
            return False, f"日线EMA21斜率不足({slope_pct*100:.2f}% < 0.05%)"
    else:
        if close_1d >= ema55_1d:
            return False, "日线收盘价未跌破EMA55"
        if slope_pct > -config['ema_slope_min']:
            return False, f"日线EMA21斜率不足({slope_pct*100:.2f}% > -0.05%)"

    # 2. 4h 价格回调检查
    if '4h' in indicators:
        atr_4h = indicators['4h']['ATR'].iloc[-1]
        ema21_4h = indicators['4h']['MA21'].iloc[-1]
        close_4h = Decimal(str(klines['4h'][-1]['close']))

        proximity = abs(close_4h - Decimal(str(ema21_4h)))
        max_proximity = Decimal(str(atr_4h)) * Decimal(str(config['ema21_proximity_atr_mult']))

        if proximity > max_proximity:
            return False, f"4h价格距EMA21过远({float(proximity):.2f} > {float(max_proximity):.2f})"

    # 3. 1h 收盘价检查
    if '1h' in indicators:
        ema21_1h = indicators['1h']['MA21'].iloc[-1]
        close_1h = Decimal(str(klines['1h'][-1]['close']))

        if direction == 'LONG' and close_1h <= Decimal(str(ema21_1h)):
            return False, "1h收盘价未站上EMA21"
        elif direction == 'SHORT' and close_1h >= Decimal(str(ema21_1h)):
            return False, "1h收盘价未跌破EMA21"

    # 4. ATR/价格检查
    if '1h' in indicators:
        atr_1h = Decimal(str(indicators['1h']['ATR'].iloc[-1]))
        atr_ratio = float(atr_1h / close_1h)
        prohibition = risk_config.get('prohibition', {})

        if atr_ratio > prohibition.get('atr_price_max', 0.045):
            return False, f"波动率过高(ATR/价格={atr_ratio*100:.1f}% > 4.5%)"
        if atr_ratio < prohibition.get('atr_price_min', 0.010):
            return False, f"波动率过低(ATR/价格={atr_ratio*100:.1f}% < 1.0%)"

    return True, direction


def check_kline_spike(klines: Dict, prohibition_cfg: Dict) -> Optional[str]:
    """禁止入场第 1 项：6h 内单根 1h K 线涨跌幅超阈值。

    命中返回中文原因字符串（如 '6h内出现单根K线涨跌幅6.1% > 5%'），
    未命中返回 None。仅做纯计算，不发起任何网络请求。
    """
    if '1h' not in klines:
        return None
    df_1h = pd.DataFrame(klines['1h'])
    recent_6 = df_1h.tail(6)
    for _, row in recent_6.iterrows():
        pct = abs((row['close'] - row['open']) / row['open'])
        if pct > prohibition_cfg.get('kline_spike_6h_pct', 0.05):
            return f"6h内出现单根K线涨跌幅{pct*100:.1f}% > 5%"
    return None


def check_volume_filter(
    symbol: str,
    grade: str,
    klines: Dict,
    risk_config: Dict,
    symbol_config: Dict,
    vol_boost: float = 0.0,
) -> Tuple[bool, str]:
    """动态成交量过滤器（v6.17 支持 vol_boost）。

    B/C 级不检查成交量；S/A 级按 symbol_config.vol_ratio_base[grade] 取倍数。
    """
    config = risk_config.get('dynamic_volume', {})
    if not config.get('enabled', True):
        return True, ""

    # B/C 级不检查成交量
    if grade in ('B', 'C'):
        return True, ""

    # 获取币种配置
    symbol_cfg = symbol_config.get(symbol, {})
    vol_ratio = symbol_cfg.get('vol_ratio_base', {})
    required_mult = vol_ratio.get(grade, 0)

    if required_mult == 0:
        return True, ""

    # 应用市场状态成交量加成（v6.17）
    if vol_boost > 0:
        required_mult = required_mult * (1 + vol_boost)
        logger.debug(f"{symbol} 成交量要求提升{vol_boost*100:.0f}%: {required_mult:.2f}x")

    # 计算当前1h成交量 / 过去20h均量
    if '1h' not in klines:
        return True, ""

    df_1h = pd.DataFrame(klines['1h'])
    current_vol = float(df_1h['volume'].iloc[-1])
    avg_vol_20h = float(df_1h['volume'].iloc[-21:-1].mean())

    if pd.isna(avg_vol_20h) or avg_vol_20h == 0:
        return True, ""

    vol_ratio_actual = current_vol / avg_vol_20h

    if vol_ratio_actual < required_mult:
        return False, f"成交量不足({vol_ratio_actual:.1f}x < {required_mult}x)"

    return True, ""


# ---------------------------------------------------------------------------
# 震荡侧：过热保护 / 波动熔断（纯计算）/ 量价确认
# ---------------------------------------------------------------------------

def evaluate_overheat(
    direction: str, indicators: Dict, klines: Dict, cfg: Dict
) -> str:
    """机制②过热档位评估：返回 'ban' / 'downgrade' / 'ok'。"""
    if not cfg:
        return 'ok'
    close_1d = safe_last_klines_value(klines, '1d', 'close')
    if close_1d is None:
        return 'ok'

    fast = safe_last(indicators, '1d', 'EMA21')
    slow = safe_last(indicators, '1d', 'EMA55')
    rsi_4h = safe_last(indicators, '4h', 'RSI')
    if fast is None or slow is None or fast == 0 or slow == 0 or rsi_4h is None:
        return 'ok'

    bias_fast = (close_1d - fast) / fast * 100
    bias_slow = (close_1d - slow) / slow * 100
    downgrade_enabled = cfg.get('downgrade_enabled', True)

    if direction == 'LONG':
        rsi_ban = cfg['rsi_extreme_long']
        rsi_warn = cfg['rsi_warn_long']
        over_fast_ban = bias_fast >= cfg['bias_fast_ban_pct']
        over_slow_ban = bias_slow >= cfg['bias_slow_ban_pct']
        over_fast_down = bias_fast >= cfg['bias_fast_downgrade_pct']
        over_slow_down = bias_slow >= cfg['bias_slow_downgrade_pct']
        rsi_ban_cond = rsi_4h > rsi_ban
        rsi_down_cond = rsi_warn < rsi_4h <= rsi_ban
    else:  # direction == 'SHORT'
        rsi_ban = cfg['rsi_extreme_short']
        rsi_warn = cfg['rsi_warn_short']
        over_fast_ban = bias_fast <= -cfg['bias_fast_ban_pct']
        over_slow_ban = bias_slow <= -cfg['bias_slow_ban_pct']
        over_fast_down = bias_fast <= -cfg['bias_fast_downgrade_pct']
        over_slow_down = bias_slow <= -cfg['bias_slow_downgrade_pct']
        rsi_ban_cond = rsi_4h < rsi_ban
        rsi_down_cond = rsi_ban <= rsi_4h < rsi_warn

    if over_fast_ban or over_slow_ban or rsi_ban_cond:
        return 'ban'
    if over_fast_down or over_slow_down or rsi_down_cond:
        return 'ban' if not downgrade_enabled else 'downgrade'
    return 'ok'


def check_overheat_ban(
    direction: str, indicators: Dict, klines: Dict, ranging_config: Dict
) -> Tuple[bool, str]:
    """机制②：重度过热禁开。乖离率或 RSI 进入极值区时拒绝入场。"""
    cfg = ranging_config.get('overheat_protection', {})
    if not cfg.get('enabled', True):
        return True, ""
    if evaluate_overheat(direction, indicators, klines, cfg) == 'ban':
        return False, "过热禁开：乖离率或RSI进入极值区"
    return True, ""


def apply_overheat_downgrade(
    grade: str,
    direction: str,
    indicators: Dict,
    klines: Dict,
    ranging_config: Dict,
) -> Optional[str]:
    """机制②：轻度过热降级一档（S→A→B→C，C 再降即返回 None 禁开）。"""
    cfg = ranging_config.get('overheat_protection', {})
    if not cfg.get('enabled', True):
        return grade
    if evaluate_overheat(direction, indicators, klines, cfg) != 'downgrade':
        return grade
    idx = _GRADE_LADDER.index(grade) if grade in _GRADE_LADDER else None
    if idx is None or idx + 1 >= len(_GRADE_LADDER):
        return None
    new_grade = _GRADE_LADDER[idx + 1]
    logger.info("轻度过热，信号降一档", before=grade, after=new_grade, direction=direction)
    return new_grade


def check_volume_confirm(
    direction: str, indicators: Dict, klines: Dict, ranging_config: Dict
) -> Tuple[bool, str]:
    """机制③a：量价确认（价格半区校验 + 缩量），数据缺失保守拒绝。"""
    cfg = ranging_config.get('volume_confirm', {})
    if not cfg.get('enabled', True):
        return True, ""
    close_4h = safe_last_klines_value(klines, '4h', 'close')
    if close_4h is None:
        return False, "量价确认拒绝：量价数据缺失"
    volume = safe_last_klines_value(klines, '4h', 'volume')
    if volume is None:
        return False, "量价确认拒绝：量价数据缺失"
    bb_middle = safe_last(indicators, '4h', 'BB_Middle')
    vol_ma = safe_last(indicators, '4h', 'Volume_MA')
    if bb_middle is None or vol_ma is None:
        return False, "量价确认拒绝：量价数据缺失"

    shrink_ratio = cfg.get('shrink_ratio', 1.1)  # 兜底默认值与配置一致（v6.28）
    # v6.28 逻辑修正：价格方向确认由「穿越中轨」改为「半区校验」
    if direction == 'LONG' and not close_4h < bb_middle:
        return False, "量价确认拒绝：收盘价未处于布林带下半区（做多需在下半区）"
    if direction == 'SHORT' and not close_4h > bb_middle:
        return False, "量价确认拒绝：收盘价未处于布林带上半区（做空需在上半区）"
    if not volume < shrink_ratio * vol_ma:
        return False, "量价确认拒绝：成交量未缩量"
    return True, ""


def evaluate_volatility_regime(
    indicators: Dict, ranging_config: Dict
) -> Tuple[bool, str, Optional[float]]:
    """机制③b：波动突变/极端期检测（**纯计算**版）。

    去掉 symbol 与 datetime.now 的「per-symbol 暂停」状态判断——
    那部分留在 strategy.py 的 wrapper 里。本函数只比较 ATR short / ATR_long 比值。

    Returns:
        (ok, reason, ratio)——ratio 为 atr_short/atr_long；未命中时为 None。
    """
    cfg = ranging_config.get('volatility_regime', {})
    if not cfg.get('enabled', True):
        return True, "", None

    atr_short = safe_last(indicators, '4h', 'ATR')
    atr_long = safe_last(indicators, '4h', 'ATR_long')
    if atr_short is None or atr_long is None or atr_long == 0:
        # 纯计算版无法取 symbol 打日志，直接返回
        return True, "", None

    spike_ratio = cfg.get('spike_ratio', 2.0)
    ratio = atr_short / atr_long
    if ratio > spike_ratio:
        return False, f"极端期：ATR短长比={ratio:.2f} 超阈值，暂停震荡反转", ratio
    return True, "", None


def should_keep_position(
    direction: str,
    indicators: Dict,
    klines: Dict,
    risk_config: Dict,
    volatility_check: Optional[
        Callable[[Dict, Dict], Tuple[bool, str, Optional[float]]]
    ] = None,
) -> Tuple[bool, str]:
    """时间平仓复核的**纯同步判定**版。

    原 async 版会调用 strategy 实例的 `_check_volatility_regime`（
    其中含 `symbol_extreme_pause_until` 可变状态 + logger.warning）。本版默认用
    core.filters.evaluate_volatility_regime 替代，是纯函数；实盘 wrapper 可通过
    ``volatility_check`` 注入带 symbol 暂停副作用的检查函数，行为与转发前一致。

    Returns:
        (True, "")=该持有；(False, reason)=任一机制判定不该持有。
    """
    ranging_config = risk_config.get('ranging_strategy', {})
    try:
        # 步骤1：数据可用性预检
        required = [('1d', 'EMA21'), ('1d', 'EMA55'), ('4h', 'RSI'),
                    ('4h', 'ATR'), ('4h', 'ATR_long')]
        for timeframe, field in required:
            if safe_last(indicators, timeframe, field) is None:
                return False, f"复核数据缺失：{timeframe}.{field}，无法评估风险，保守平仓"
        if not klines.get('1d') or not klines.get('4h'):
            return False, "复核K线数据为空，无法评估风险，保守平仓"

        # 步骤2：机制①方向一致性对齐
        ok, reason = check_direction_alignment(direction, indicators, ranging_config)
        if not ok:
            return False, reason

        # 步骤3：机制②重度过热
        ok, reason = check_overheat_ban(direction, indicators, klines, ranging_config)
        if not ok:
            return False, reason

        # 步骤4：机制③波动熔断（默认纯计算版；实盘注入带 symbol 暂停的检查）
        checker = volatility_check or evaluate_volatility_regime
        ok, reason, _ratio = checker(indicators, ranging_config)
        if not ok:
            return False, reason

        return True, ""
    except Exception as e:
        return False, f"复核异常：{e}"
