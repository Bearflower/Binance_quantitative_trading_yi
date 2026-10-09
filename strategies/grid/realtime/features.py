"""特征层：整秒采样、滚动涨跌幅、方向效率（纯计算，无 I/O / 系统时钟）。

口径权威：需求 §5.2/§5.3、计划 §2.2。
- 整秒价 p(t)=不晚于 t.000 的最后一笔成交；锚距 sample_ms-anchor_time_ms >
  features.max_anchor_gap_seconds（研究种子 2s）→ 该点价格记 None（O-C3，
  与审计脚本 price() 的 bisect_right+2000ms 规则同口径）。
- r_w(t)=p(t)/p(t-w)-1，w∈{60,180,300}；p(t) 或 p(t-w) 失效 → None。
- E_w(stride)=|q_last-q_first|/Σ|q_i-q_(i-1)|，q 为窗口内每 stride 秒抽点
  （stride=1→301 点，3→101，5→61）；任一点失效 → None；分母 0 → 0。
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Sequence

MS_PER_SECOND = 1000


@dataclass(frozen=True)
class AggTrade:
    """单笔聚合成交（字段口径见 services/aggtrade_collector/models.py）。"""

    agg_trade_id: int
    price: Decimal
    quantity: Decimal
    trade_time_ms: int
    is_buyer_maker: int  # 0=买方主动吃单，1=卖方主动（买方挂单）


@dataclass(frozen=True)
class SecondSample:
    """整秒采样点；price=None 表示无锚点或锚距超限（O-C3）。"""

    sample_ms: int
    price: Optional[Decimal]
    anchor_time_ms: Optional[int]
    anchor_trade_id: Optional[int]


@dataclass(frozen=True)
class FeatureSlice:
    """某决策点特征；r/e 按窗口键，None=特征不可用（不填零）。"""

    price: Optional[Decimal]
    r: Dict[int, Optional[Decimal]]
    e: Dict[int, Optional[Decimal]]


PriceLookup = Callable[[int], Optional[Decimal]]


def sample_from_trades(times: Sequence[int], ids: Sequence[int],
                       prices: Sequence[Decimal], target_ms: int,
                       max_gap_ms: int) -> SecondSample:
    """按 (trade_time_ms, agg_trade_id) 有序的成交流，对整秒点 target 采样。

    取不晚于 target 的最后一笔（bisect_right，同毫秒取 ID 序末笔）；
    无锚点或锚距 > max_gap_ms 时价格记 None，锚点元信息保留（O-C3）。
    """
    i = bisect.bisect_right(times, target_ms) - 1
    if i < 0:
        return SecondSample(target_ms, None, None, None)
    gap_ok = target_ms - times[i] <= max_gap_ms
    price = prices[i] if gap_ok else None
    return SecondSample(target_ms, price, times[i], ids[i])


def efficiency(q: Sequence[Optional[Decimal]]) -> Optional[Decimal]:
    """方向效率：|末-首|/Σ相邻绝对差；任一点 None → None；分母 0 → 0。"""
    if any(v is None for v in q):
        return None
    denominator = Decimal(0)
    for a, b in zip(q, q[1:]):
        denominator += abs(b - a)  # type: ignore[operator]
    if denominator == 0:
        return Decimal(0)
    return abs(q[-1] - q[0]) / denominator  # type: ignore[operator]


def resampled_q(price_of: PriceLookup, now_ms: int, window_seconds: int,
                 stride_seconds: int) -> List[Optional[Decimal]]:
    """窗口 [now-w, now] 内每 stride 秒抽点（首末点对齐窗口边界）。"""
    start_ms = now_ms - window_seconds * MS_PER_SECOND
    steps = window_seconds // stride_seconds
    return [price_of(start_ms + k * stride_seconds * MS_PER_SECOND)
            for k in range(steps + 1)]


def compute_features(price_of: PriceLookup, now_ms: int,
                     windows_seconds: Sequence[int], stride_seconds: int,
                     e_windows_seconds: Sequence[int]) -> FeatureSlice:
    """计算决策点 now 的滚动涨跌幅 r_w 与效率 e_w（任一锚点失效即 None）。"""
    price = price_of(now_ms)
    returns: Dict[int, Optional[Decimal]] = {}
    for w in windows_seconds:
        ref = price_of(now_ms - w * MS_PER_SECOND)
        returns[w] = price / ref - 1 if price is not None and ref is not None else None
    efficiencies: Dict[int, Optional[Decimal]] = {}
    for w in e_windows_seconds:
        q = resampled_q(price_of, now_ms, w, stride_seconds)
        efficiencies[w] = efficiency(q)
    return FeatureSlice(price, returns, efficiencies)
