"""将已计算特征映射到六类市场状态及 STRESS。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Mapping

from ai_tuner.replay.models import DataBlocked, ZERO

from .features import RegimeFeatures


REQUIRED_THRESHOLDS = (
    "trend_return_pct",
    "high_volatility_atr_pct",
    "stress_atr_pct",
)


def classify_regime(features: RegimeFeatures, config: Mapping[str, object]) -> str:
    """所有阈值由配置显式注入，缺失或非法一律阻断。"""
    values = {}
    for key in REQUIRED_THRESHOLDS:
        raw = config.get(key)
        if raw is None:
            raise DataBlocked(["regime_threshold_missing:{0}".format(key)])
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            raise DataBlocked(["regime_threshold_invalid:{0}".format(key)])
        if not value.is_finite() or value <= ZERO:
            raise DataBlocked(["regime_threshold_invalid:{0}".format(key)])
        values[key] = value
    if values["stress_atr_pct"] <= values["high_volatility_atr_pct"]:
        raise DataBlocked(["regime_stress_threshold_not_above_high_volatility"])

    if features.atr_pct >= values["stress_atr_pct"]:
        return "STRESS"
    if features.signed_return_pct >= values["trend_return_pct"]:
        direction = "up_trend"
    elif features.signed_return_pct <= -values["trend_return_pct"]:
        direction = "down_trend"
    else:
        direction = "ranging"
    volatility = (
        "high_vol" if features.atr_pct >= values["high_volatility_atr_pct"]
        else "normal"
    )
    return "{0}_{1}".format(direction, volatility)
