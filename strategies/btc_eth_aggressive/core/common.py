from __future__ import annotations

"""core.common：指标构建与安全读取。

来源：strategy.py 模块级函数 ``_safe_last``（68-92 行）、classmethod
``_safe_last_klines_value``（2030-2041 行）、实例方法 ``_build_indicators``
（5129-5152 行）。仅将 ``self.risk_config`` 提取为形参，无行为改动。
"""

from typing import Dict, Optional

import pandas as pd
import structlog

from shared.indicators import TechnicalIndicators

logger = structlog.get_logger()


def safe_last(indicators: Dict, timeframe: str, field: str) -> Optional[float]:
    """安全读取某时间框架某指标的最新有效值。

    统一兜底：字段缺失、时间框架缺失、Series 为空、值为 NaN 时返回 None，
    禁止调用方裸用 iloc[-1] 读取指标导致主循环中断。

    Args:
        indicators: 多时间框架指标字典，形如 {timeframe: {field: pd.Series}}
        timeframe: 时间框架（如 '1h' / '4h' / '1d'）
        field: 指标字段名（如 'EMA21' / 'RSI' / 'ATR' / 'BB_Middle' / 'Volume_MA'）

    Returns:
        最新有效值（float）；缺失 / 空 / NaN 返回 None
    """
    timeframe_indicators = indicators.get(timeframe)
    if not isinstance(timeframe_indicators, dict):
        return None
    series = timeframe_indicators.get(field)
    if series is None or len(series) == 0:
        return None
    value = series.iloc[-1]
    if pd.isna(value):
        return None
    return float(value)


def safe_last_klines_value(klines: Dict, timeframe: str, field: str) -> Optional[float]:
    """安全读取指定周期 K 线最新字段值，数据缺失/异常返回 None。"""
    tf_data = klines.get(timeframe)
    if not tf_data:
        return None
    df = pd.DataFrame(tf_data)
    if df.empty or field not in df.columns:
        return None
    raw = df[field].iloc[-1]
    if pd.isna(raw):
        return None
    return float(raw)


def build_indicators(klines: Dict, risk_config: Dict) -> Dict:
    """由多时间框架 K 线构建技术指标。

    从 risk_config 读取长周期 ATR 周期（默认 50），遍历各时间框架将原始 K 线
    转为 DataFrame 并逐列强制数值化后，统一调用 TechnicalIndicators.calculate_all。

    Args:
        klines: 多时间框架 K 线数据 {timeframe: [K线记录]}
        risk_config: 完整风险配置（读取 ranging_strategy.volatility_regime.atr_long_period）

    Returns:
        各时间框架技术指标字典 {timeframe: indicators}
    """
    atr_long_period = risk_config.get('ranging_strategy', {}).get(
        'volatility_regime', {}).get('atr_long_period', 50)
    indicators: Dict = {}
    for timeframe, data in klines.items():
        df = pd.DataFrame(data)
        for col in ('open', 'high', 'low', 'close', 'volume'):
            df[col] = pd.to_numeric(df[col], errors='coerce')
        indicators[timeframe] = TechnicalIndicators.calculate_all(
            df, atr_long_period=atr_long_period)
    return indicators
