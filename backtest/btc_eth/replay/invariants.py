"""
回放不变量校验（invariants）。

引擎在每次开仓 / 平仓后、期终前调用，确保结果合法。
返回 ("OK", None) 或 ("VIOLATION", violation_dict)。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Any, List, Optional


def check_no_unprotected_close(
    pos_before: Optional[Dict[str, Any]],
    pos_after: Optional[Dict[str, Any]],
    label: str,
) -> Optional[str]:
    """平仓前若 open_position 为 None 又收到平仓事件——属于无保护状态。"""
    if pos_before is None and pos_after is None:
        return f"[{label}] 无持仓但收到平仓事件"
    return None


def check_no_double_close(
    pos_before: Optional[Dict[str, Any]],
    pos_after: Optional[Dict[str, Any]],
    label: str,
) -> Optional[str]:
    """平仓后 open_position 不应仍存在。"""
    if pos_after is not None:
        return f"[{label}] 平仓后持仓仍存在（可能 double close）"
    return None


def check_quantity_in_range(quantity: Decimal, step_size: Decimal) -> Optional[str]:
    """撮合后 quantity 必须是 step_size 的整数倍（允许极小 eps）。"""
    if step_size <= 0:
        return None
    remainder = (quantity / step_size) - (quantity / step_size).to_integral_value()
    eps = Decimal("1e-6")
    if abs(remainder) > eps:
        return f"quantity={quantity} 不是 stepSize={step_size} 的整数倍"
    return None


def check_notional_above_min(quantity: Decimal, price: Decimal, min_notional: Decimal) -> Optional[str]:
    notional = quantity * price
    if min_notional <= 0:
        return None
    # 撮合阶段已保证 >= minNotional；这里只做安全兜底
    if notional < min_notional - Decimal("1e-8"):
        return f"notional={notional} 低于 minNotional={min_notional}"
    return None


def run_all_invariants(
    state_after: Dict[str, Any],   # 保留以匹配历史签名：由 engine 直接检查 pos_after
    pos_before: Optional[Dict[str, Any]],
    pos_after: Optional[Dict[str, Any]],
    quantity: Optional[Decimal] = None,
    step_size: Optional[Decimal] = None,
    price: Optional[Decimal] = None,
    min_notional: Optional[Decimal] = None,
    label: str = "",
) -> List[str]:
    """一次性跑全部不变量，返回 violations 列表（空列表=通过）。"""
    violations: List[str] = []
    # 两个检查独立：
    # - pos_before 是 None 就收到平仓 → 无保护状态
    # - pos_after 还在（平仓后仍剩持仓） → double close
    if pos_before is None:
        v = check_no_unprotected_close(pos_before, pos_after, label)
        if v:
            violations.append(v)
    if pos_after is not None:
        v = check_no_double_close(pos_before, pos_after, label)
        if v:
            violations.append(v)
    if quantity is not None and step_size is not None:
        v = check_quantity_in_range(quantity, step_size)
        if v:
            violations.append(v)
    if quantity is not None and price is not None and min_notional is not None:
        v = check_notional_above_min(quantity, price, min_notional)
        if v:
            violations.append(v)
    return [v for v in violations if v]
