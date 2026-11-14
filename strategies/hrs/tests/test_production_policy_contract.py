from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from shared.entry_risk_policy import entry_allowed
from strategies.hrs.tests.test_execution_review_regressions import executor


@pytest.mark.asyncio
async def test_hrs_submit_requires_current_budget_proof(monkeypatch):
    store = MagicMock()
    store.policy = AsyncMock(return_value={'mode': 'reduced', 'allow_add': False, 'revision': 7})
    monkeypatch.setattr('shared.entry_risk_policy.RiskPolicyStore', lambda db: store)
    target = SimpleNamespace(db=object())
    assert not await entry_allowed(target, 'hrs', stage='submit', quantity=Decimal('1'))
    decision = SimpleNamespace(allowed=True, quantity=Decimal('1'), risk_limit=Decimal('1'),
        worst_loss=Decimal('.5'), policy_revision=7, client_order_id='risk_test')
    assert await entry_allowed(target, 'hrs', stage='submit', risk_decision=decision, quantity=Decimal('1'))
    assert not await entry_allowed(target, 'hrs', stage='submit', risk_decision=decision, quantity=Decimal('2'))
    decision.policy_revision = 6
    assert not await entry_allowed(target, 'hrs', stage='submit', risk_decision=decision, quantity=Decimal('1'))


@pytest.mark.asyncio
async def test_reserved_order_geometry_is_checked_before_post(executor, monkeypatch):
    @asynccontextmanager
    async def submitting(*args):
        yield {'symbol': 'AAVEUSDT', 'quantity': '1', 'price': '100', 'revision': 7}
    store = MagicMock()
    store.submitting = submitting
    monkeypatch.setattr('shared.entry_risk_policy.RiskPolicyStore', lambda db: store)
    decision = SimpleNamespace(client_order_id='risk_test', policy_revision=7)
    executor.binance_api.place_order = AsyncMock(return_value={'orderId': 1})
    with pytest.raises(Exception, match='geometry changed'):
        await executor._submit_reserved_hrs_entry('AAVEUSDT', 'SELL', Decimal('2'), Decimal('100'), decision)
    executor.binance_api.place_order.assert_not_awaited()
    await executor._submit_reserved_hrs_entry('AAVEUSDT', 'SELL', Decimal('1'), Decimal('100'), decision)
    assert executor.binance_api.place_order.await_args.kwargs['client_order_id'] == 'risk_test'
