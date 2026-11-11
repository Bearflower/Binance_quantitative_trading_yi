from decimal import Decimal

import pytest

from strategies.grid.execution.planner import ExchangeFilters, HardLimits, build_plan
from strategies.grid.grid_calculator import CapitalFeasibility, DynamicGridParams, GridMode


def _params(mode=GridMode.ARITHMETIC):
    return DynamicGridParams(Decimal("1900"), Decimal("2100"), 4, mode,
                             Decimal("1850"), Decimal("2150"),
                             profit_rate=Decimal("0.02"), grid_spacing=Decimal("50"))


def _feasibility(qty=Decimal("0.02")):
    return CapitalFeasibility(True, 4, qty, "STRUCT")


def _filters():
    return ExchangeFilters(Decimal("0.01"), Decimal("0.001"),
                           Decimal("0.01"), Decimal("5"))


def _limits():
    return HardLimits(Decimal("100"), Decimal("500"), 8, 10)


def test_arithmetic_plan_has_no_crossing_orders():
    plan = build_plan(_params(), _feasibility(), Decimal("2000"), _filters(), _limits())
    assert [(x.side, x.price) for x in plan.orders] == [
        ("BUY", Decimal("1900")), ("BUY", Decimal("1950")),
        ("SELL", Decimal("2050")), ("SELL", Decimal("2100"))]
    assert all(x.quantity == Decimal("0.02") for x in plan.orders)


def test_rejects_whole_plan_exposure_and_bad_units():
    with pytest.raises(ValueError, match="notional"):
        build_plan(_params(), _feasibility(), Decimal("2000"), _filters(),
                   HardLimits(Decimal("100"), Decimal("50"), 8, 10))
    with pytest.raises(ValueError, match="quantity"):
        build_plan(_params(), _feasibility(Decimal("0.001")), Decimal("2000"),
                   _filters(), _limits())


def test_geometric_grid_is_strictly_increasing():
    plan = build_plan(_params(GridMode.GEOMETRIC), _feasibility(),
                      Decimal("2000"), _filters(), _limits())
    prices = [x.price for x in plan.orders]
    assert prices == sorted(set(prices))
