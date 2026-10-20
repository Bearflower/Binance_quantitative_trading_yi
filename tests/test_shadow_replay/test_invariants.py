"""回放不变量测试：无保护 / 重复平仓 / 数量越界。"""
from decimal import Decimal
import pytest

from backtest.btc_eth.replay.invariants import (
    check_no_unprotected_close, check_no_double_close,
    check_quantity_in_range, check_notional_above_min, run_all_invariants,
)


def test_unprotected_close_detected():
    v = check_no_unprotected_close(None, None, "test")
    assert v is not None
    assert "无持仓但收到平仓" in v


def test_protected_close_ok():
    pos = {"symbol": "XAUUSDT"}
    v = check_no_unprotected_close(pos, None, "test")
    assert v is None


def test_double_close_detected():
    pos = {"symbol": "XAUUSDT"}
    v = check_no_double_close(pos, pos, "test")
    assert v is not None
    assert "double close" in v.lower() or "平仓后持仓仍存在" in v


def test_qty_not_in_step():
    v = check_quantity_in_range(Decimal("4.1234567"), Decimal("0.001"))
    assert v is not None


def test_qty_in_step():
    v = check_quantity_in_range(Decimal("4.123"), Decimal("0.001"))
    assert v is None


def test_min_notional_below():
    v = check_notional_above_min(Decimal("0.001"), Decimal("100"), Decimal("5"))
    # 0.001 * 100 = 0.1 < 5
    assert v is not None


def test_min_notional_ok():
    v = check_notional_above_min(Decimal("1"), Decimal("100"), Decimal("5"))
    assert v is None


def test_run_all_passes_clean():
    violations = run_all_invariants(
        state_after={},
        pos_before={"symbol": "XAUUSDT"},
        pos_after=None,
        quantity=Decimal("1.0"),
        step_size=Decimal("0.001"),
        price=Decimal("100"),
        min_notional=Decimal("5"),
        label="clean",
    )
    assert violations == []


def test_run_all_catches_unprotected_and_double():
    violations = run_all_invariants(
        state_after={},
        pos_before=None,
        pos_after={"symbol": "XAUUSDT"},
        quantity=Decimal("4.1234567"),
        step_size=Decimal("0.001"),
        price=Decimal("100"),
        min_notional=Decimal("5"),
        label="mess",
    )
    # 无持仓 + 平仓后有持仓 + qty 非 step 倍数
    assert len(violations) >= 2


def test_qty_check_skipped_when_step_nonpositive():
    """stepSize <= 0 属非法配置：校验直接跳过（返回 None），不抛除零异常。"""
    assert check_quantity_in_range(Decimal("4.1234567"), Decimal("0")) is None
    assert check_quantity_in_range(Decimal("4.1234567"), Decimal("-0.001")) is None


def test_notional_check_skipped_when_min_notional_nonpositive():
    """minNotional <= 0 表示交易所未配置下限：校验跳过，不做无意义比较。"""
    assert check_notional_above_min(Decimal("0.001"), Decimal("100"), Decimal("0")) is None
    assert check_notional_above_min(Decimal("0.001"), Decimal("100"), Decimal("-5")) is None
