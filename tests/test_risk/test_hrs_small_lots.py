from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from strategies.hrs.risk_budget import assess_entry
from strategies.hrs.executor import TradingExecutor
from strategies.hrs.risk_budget import RiskDecision


def _decision(*, price='0.1', stop='0.101', tp1='0.095', min_notional='5'):
    return assess_entry(
        allocation=D('52.36'), entry_price=D(price), stop_price=D(stop),
        direction='short', existing_open_risk=D(0), leverage=D(2),
        remaining_margin=D('26.18'), qty_step=D(1), min_qty=D(1),
        min_notional=D(min_notional), fee_rate=D('0.0005'),
        pressure_slippage=D('0.02'),
        take_profit_ratios=(D('0.3'),D('0.4')),
        take_profit_prices=(D(tp1),D('0.09')),
    )


def test_hrs_small_altcoin_uses_one_full_tp_inside_pressure_budget():
    decision=_decision()
    assert decision.allowed
    assert decision.exit_ratios == (D(1),D(0))
    assert decision.quantity * D('0.095') >= D(5)
    assert decision.worst_loss <= decision.risk_limit
    assert decision.risk_limit == D('0.5236')


def test_hrs_too_wide_stop_keeps_budget_gate_closed():
    decision=_decision(stop='0.14')
    assert not decision.allowed
    assert decision.reason in ('minimum_order_exceeds_risk_budget',
                               'take_profit_quantity_unavailable')


@pytest.mark.asyncio
async def test_hrs_single_full_tp_places_stop_and_tp1_only():
    executor = TradingExecutor.__new__(TradingExecutor)
    executor._place_sl_order = AsyncMock(return_value=True)
    executor._place_tp_order = AsyncMock(return_value=True)
    params = {'target1_close_percent': D(1), 'target2_close_percent': D(0)}
    failed = await executor._create_protection_orders(
        'AINUSDT', 'short', 0.1, 0.001, D(53), D('0.00001'), D(1), params)
    assert failed == []
    executor._place_sl_order.assert_awaited_once()
    executor._place_tp_order.assert_awaited_once()
    assert executor._place_tp_order.await_args.args[2] == 'tp1'


def test_hrs_invalid_persisted_exit_ratios_refused():
    with pytest.raises(ValueError):
        TradingExecutor._params_for_exit_ratios({}, ('NaN', '0'))
    with pytest.raises(ValueError):
        TradingExecutor._params_for_exit_ratios({}, ('0.5', '0'))


@pytest.mark.asyncio
async def test_hrs_submit_requires_matching_reduced_policy_and_reservation(monkeypatch):
    from types import SimpleNamespace
    from shared.entry_risk_policy import entry_allowed
    import shared.entry_risk_policy as policy_module

    policy = {'mode': 'reduced', 'allow_add': False, 'revision': 3}
    class FakeStore:
        def __init__(self, db):
            pass
        async def policy(self, strategy):
            return policy
    monkeypatch.setattr(policy_module, 'RiskPolicyStore', FakeStore)
    strategy = SimpleNamespace(db=object())
    decision = RiskDecision(True, 'allowed', quantity=D(53),
                            worst_loss=D('.5'), risk_limit=D('.5236'),
                            exit_ratios=(D(1), D(0)), policy_revision=3,
                            client_order_id='risk_test')
    assert await entry_allowed(strategy, 'hrs', stage='submit',
                               risk_decision=decision, quantity=D(53))
    assert not await entry_allowed(strategy, 'hrs', stage='submit', quantity=D(53))
    assert not await entry_allowed(strategy, 'hrs', stage='submit',
                                   risk_decision=decision, quantity=D(54))
    policy['mode'] = 'monitor_only'
    assert not await entry_allowed(strategy, 'hrs', stage='submit',
                                   risk_decision=decision, quantity=D(53))
