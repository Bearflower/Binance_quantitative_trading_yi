"""
反事实停牌标记（counterfactual）。

主回放**不截断**路径，持续推进到数据截止时间；
这里独立计算"如果启用 pilot 停机政策"首次触发 15%/25%/30% 的时点与当时仓位，
不改变主路径结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, List, Tuple, Optional, Dict


@dataclass
class CounterfactualTriggers:
    """记录三个亏损红线的首次触发时点。"""
    trigger_15pct: Optional[Tuple[datetime, Decimal, Optional[Dict]]] = None   # (at, equity, open_position)
    trigger_25pct: Optional[Tuple[datetime, Decimal, Optional[Dict]]] = None
    trigger_30pct: Optional[Tuple[datetime, Decimal, Optional[Dict]]] = None

    def as_dict(self) -> Dict[str, Any]:
        def fmt(t):
            if t is None:
                return None
            at, eq, pos = t
            return {
                "triggered_at": at.isoformat(),
                "equity": str(eq),
                "open_position": bool(pos),
            }
        return {
            "15pct": fmt(self.trigger_15pct),
            "25pct": fmt(self.trigger_25pct),
            "30pct": fmt(self.trigger_30pct),
        }


def scan_triggers(
    equity_history: List[Tuple[datetime, Decimal, bool]],
    initial_equity: Decimal,
    halt_lines_pct: Optional[List[int]] = None,
) -> CounterfactualTriggers:
    """遍历 equity_history（阻断 #4：三元组 [at, marked_equity, has_open_position]），
    找首次触发亏损红线的时点。阈值从 halt_lines_pct 参数读取，禁止硬编码。

    Args:
        halt_lines_pct: 亏损红线百分比列表，如 [15, 25, 30]。
                        None 或不足 3 档会 fail-closed 抛 ValueError。

    亏损 = initial_equity - equity（权益从初始 300 往下掉，15% = 亏 45 = equity 255）。
    open_position 现在为真实布尔值——反事实停牌标记
    能同时给出「触发时点当时有没有仓位」。
    """
    # 单一真源校验：halt_lines_pct 必须存在且恰好 3 档
    # （对应当前 CounterfactualTriggers 的三个命名字段）
    if not halt_lines_pct or len(halt_lines_pct) != 3:
        raise ValueError(
            f"counterfactual.halt_lines_pct 必须提供恰好 3 档阈值（当前：{halt_lines_pct!r}）"
        )
    p15, p25, p30 = halt_lines_pct
    if not (0 < p15 < p25 < p30 < 100):
        raise ValueError(
            f"counterfactual.halt_lines_pct 必须严格递增且在 (0, 100) 内（当前：{halt_lines_pct!r}）"
        )

    # 从配置派生出三条亏损线对应的 equity 下限
    def _limit(pct: int) -> Decimal:
        return initial_equity * (Decimal("1") - Decimal(str(pct)) / Decimal("100"))

    limit_15 = _limit(p15)
    limit_25 = _limit(p25)
    limit_30 = _limit(p30)

    triggers = CounterfactualTriggers()
    for item in equity_history:
        # 阻断 #4：equity_history 现在是三元组 (at, marked_equity, has_open_position)
        at, eq, has_pos = item[0], item[1], item[2] if len(item) >= 3 else False
        if triggers.trigger_15pct is None and eq <= limit_15:
            triggers.trigger_15pct = (at, eq, has_pos)
        if triggers.trigger_25pct is None and eq <= limit_25:
            triggers.trigger_25pct = (at, eq, has_pos)
        if triggers.trigger_30pct is None and eq <= limit_30:
            triggers.trigger_30pct = (at, eq, has_pos)
        # 三个都找到则可以提前退出
        if triggers.trigger_15pct and triggers.trigger_25pct and triggers.trigger_30pct:
            break
    return triggers


def pressure_test_final_equity(
    final_equity: Decimal,
    total_notional: Decimal,
    slippage_bps_added: Decimal,
) -> Decimal:
    """在最终 equity 基础上，把新增滑点从 equity 中扣减，得到压力测试后的权益。
    slippage_bps_added 是本次压力测试新增的 bps（基准 2bps，2× 压力即加 2bps = 4bps）。
    total_notional: 整个回放期的累计 notional（用于计算总滑点绝对值）。
    """
    extra_cost = total_notional * slippage_bps_added / Decimal("10000")
    return final_equity - extra_cost
