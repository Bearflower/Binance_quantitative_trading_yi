from decimal import Decimal as D
from dataclasses import replace
import pytest
from strategies.grid.execution.planner import ExchangeFilters, HardLimits, PlanLimitExceeded
from strategies.grid.grid_calculator import DynamicGridParams, GridMode


def inputs():
    return (DynamicGridParams(D('1595.0536694579707'), D('1804.3263305420293'), 10,
            GridMode.ARITHMETIC, D('1553.20'), D('1846.18')),
            ExchangeFilters(D('.01'),D('.001'),D('.001'),D('20')),
            HardLimits(D('240'),D('2400'),13,5))


def solve(advice='17', available='286.49879946', **kw):
    from strategies.grid.execution.sizing import size_grid
    p,f,h=inputs()
    return size_grid(p,D('1699.69'),f,h,advisory_margin=D(advice),
                     available_margin=D(available),min_grid_count=4,**kw)


def test_positive_budget_tops_up_only_minimum_grid_and_reserves_every_level():
    plan=solve()
    assert len(plan.levels)==5
    assert plan.sizing['budget']=='22.10'
    assert plan.sizing['budget_source']=='minimum_viable_grid'
    assert plan.orders[0].quantity==D('.013')
    assert all(o.price*o.quantity >= 20 for o in plan.orders)
    assert sum(plan.levels)*D('.013')/5 <= D('22.10')
    assert plan.sizing['grid_params']['grid_count']==4


def test_enough_advice_uses_largest_feasible_count_without_old_quantity_cap():
    plan=solve('49')
    assert len(plan.levels)==11
    assert plan.orders[0].quantity==D('.013')
    assert plan.sizing['budget_source']=='advisory'


def test_reduces_count_before_any_budget_topup():
    plan=solve('30')
    assert len(plan.levels)==6
    assert plan.sizing['budget']=='30'
    assert plan.sizing['budget_source']=='advisory'


@pytest.mark.parametrize('advice,available', [('0','286'),('-1','286'),('NaN','286'),('17','22'),('17','NaN')])
def test_never_tops_up_missing_zero_or_unfunded_budget(advice,available):
    with pytest.raises(ValueError):solve(advice,available)


def test_hard_margin_notional_and_order_caps_remain_binding():
    plan=solve('500')
    assert plan.max_margin_usdt <= 240
    assert plan.max_notional_usdt <= 2400
    assert len(plan.orders)<=13
