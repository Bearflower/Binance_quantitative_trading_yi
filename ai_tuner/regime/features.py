"""仅从截至 as_of 已收盘 K 线计算市场状态特征。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from ai_tuner.replay.models import Bar, DataBlocked, FutureDataAccess, ZERO


@dataclass(frozen=True)
class RegimeFeatures:
    as_of: datetime
    signed_return_pct: Decimal
    atr_pct: Decimal
    bar_count: int


def extract_features(
    bars: Sequence[Bar], as_of: datetime, *, min_segment_length: int
) -> RegimeFeatures:
    """计算区间收益和平均真实波幅百分比；未来数据直接报错。"""
    if min_segment_length < 2:
        raise ValueError("min_segment_length 必须 >= 2")
    for bar in bars:
        if bar.close_time > as_of:
            raise FutureDataAccess("regime.extract_features", as_of, "close_time", bar.close_time)
    if len({(bar.symbol, bar.interval) for bar in bars}) > 1:
        raise DataBlocked(["regime_mixed_symbol_or_interval"])
    ordered = sorted(bars, key=lambda bar: bar.close_time)
    if len(ordered) < min_segment_length:
        raise DataBlocked(["regime_closed_bars_insufficient"])
    segment = ordered[-min_segment_length:]
    first = segment[0].close
    last = segment[-1].close
    if first <= ZERO or last <= ZERO:
        raise DataBlocked(["regime_non_positive_price"])
    signed_return_pct = (last / first - Decimal("1")) * Decimal("100")
    ranges = []
    for index, bar in enumerate(segment):
        previous_close = segment[index - 1].close if index else bar.open
        ranges.append(max(
            bar.high - bar.low,
            abs(bar.high - previous_close),
            abs(bar.low - previous_close),
        ))
    atr_pct = sum(ranges, ZERO) / Decimal(len(ranges)) / last * Decimal("100")
    return RegimeFeatures(as_of, signed_return_pct, atr_pct, len(segment))
