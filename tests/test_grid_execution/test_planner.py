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


def test_stop_prices_round_outward_to_exchange_tick_before_opening():
    from dataclasses import replace
    params = replace(_params(), stop_loss_low=Decimal('1850.0091'),
                     stop_loss_high=Decimal('2150.0001'))
    plan = build_plan(params, _feasibility(), Decimal('2000'), _filters(), _limits())
    assert plan.stop_loss_low == Decimal('1850.00')
    assert plan.stop_loss_high == Decimal('2150.01')



def test_arithmetic_plan_compares_rounding_with_same_interval():
    from dataclasses import replace
    params = replace(_params(), lower_boundary=Decimal("1700"),
                     upper_boundary=Decimal("2300"),
                     stop_loss_low=Decimal("1650"), stop_loss_high=Decimal("2350"),
                     grid_spacing=Decimal("150"), profit_rate=Decimal("150") / Decimal("1800"))
    plan = build_plan(params, _feasibility(), Decimal("1800"), _filters(), _limits())
    assert plan.levels == tuple(map(Decimal, ("1700", "1850", "2000", "2150", "2300")))


def test_rounding_loss_is_a_rejected_plan():
    from dataclasses import replace
    from strategies.grid.execution.planner import PlanLimitExceeded
    params = replace(_params(), lower_boundary=Decimal("1900.9"),
                     upper_boundary=Decimal("1905.7"),
                     grid_spacing=Decimal("1.2"), profit_rate=Decimal("1.2") / Decimal("1903"))
    filters = replace(_filters(), tick_size=Decimal("1"))
    with pytest.raises(PlanLimitExceeded, match="rounded profit"):
        build_plan(params, _feasibility(), Decimal("1903"), filters, _limits())


@pytest.mark.parametrize("budget", [Decimal("240"), Decimal("80")])
def test_execution_quantity_uses_budget_and_actual_leverage(budget):
    params = _params()
    feasibility = _feasibility(Decimal("0.5"))
    limits = HardLimits(Decimal("240"), Decimal("2400"), 7, 5)
    plan = build_plan(params, feasibility, Decimal("2000"), _filters(), limits,
                      margin_budget_usdt=budget)
    assert all(order.quantity == (budget * 5 / sum(plan.levels) // Decimal(".001"))
               * Decimal(".001") for order in plan.orders)
    assert plan.max_margin_usdt <= budget
    assert plan.max_notional_usdt <= budget * 5
    assert plan.orders[0].quantity < feasibility.qty_per_grid
    assert params.grid_count == 4


def test_small_budget_skips_plan_without_invalid_exchange_order():
    from strategies.grid.execution.planner import PlanLimitExceeded
    with pytest.raises(PlanLimitExceeded, match="minimum"):
        build_plan(_params(), _feasibility(), Decimal("2000"), _filters(), _limits(),
                   margin_budget_usdt=Decimal(".1"))
