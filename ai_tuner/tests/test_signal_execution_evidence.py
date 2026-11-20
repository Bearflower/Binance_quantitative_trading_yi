from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from ai_tuner.monitor.signal_execution_evidence import read_signal_execution

START = datetime(2026, 11, 19, tzinfo=timezone.utc)
END = START + timedelta(days=1)


def row(signal_id, status, *, reason=None, order=None, client=None, receipt=None, intent=None):
    return dict(signal_id=signal_id, symbol='ETHUSDT', direction='LONG',
                signal_at=START + timedelta(seconds=4), status=status, reason=reason,
                order_id=order, client_order_id=client, preflight_receipt=receipt,
                intent_client_id=intent)


def order(order_id=98, reduce_only=False):
    return dict(orderId=order_id, clientOrderId='risk_exact', symbol='ETHUSDT',
                positionSide='BOTH', side='BUY', status='FILLED', reduceOnly=reduce_only,
                closePosition=False, origQty='0.012', executedQty='0.012',
                time=int(START.timestamp()*1000)+3000)


def fill(order_id=98):
    return dict(id=180, orderId=order_id, symbol='ETHUSDT', side='BUY', positionSide='BOTH',
                price='1770', qty='0.012', time=int(START.timestamp()*1000)+3500)


async def audit(rows, orders, fills, strategy='btc_eth_aggressive'):
    db = AsyncMock()
    db.fetch_all.return_value = rows
    exchange = AsyncMock()
    exchange._request.side_effect = [orders, fills]
    result = await read_signal_execution(db, exchange, 'default', strategy, START, END)
    assert all(c.args[0] == 'GET' and c.kwargs['signed'] is True
               for c in exchange._request.await_args_list)
    return result


@pytest.mark.asyncio
async def test_known_preflight_and_exact_filled_order_are_verified_together():
    r = await audit([row(1,'cancelled',reason='legacy STOP episode unverified'),
                     row(2,'executed',order='98',client='risk_exact',intent='risk_exact')],
                    [order(), order(99, True)], [fill(), dict(fill(99), id=181)])
    assert r['verified'] is True
    assert [x['outcome'] for x in r['signals']] == ['pre_submit_rejected', 'filled']


@pytest.mark.asyncio
async def test_unmatched_order_and_untrusted_reason_cannot_verify():
    r = await audit([row(1,'cancelled',reason='generic failure')], [order()], [fill()])
    assert r['verified'] is False
    assert r['signals'][0]['outcome'] == 'unverified'


@pytest.mark.asyncio
async def test_unknown_hrs_gate_reports_zero_signed_orders_without_claiming_complete():
    r = await audit([row(1,None)], [], [], 'hrs')
    assert r['verified'] is False
    assert r['signals'][0]['outcome'] == 'no_order_reason_unverified'


@pytest.mark.asyncio
async def test_hrs_preflight_requires_receipt_and_no_conflicting_intent():
    receipt='a'*64
    r = await audit([row(1,'pool_circuit_breaker',receipt=receipt)], [], [], 'hrs')
    assert r['verified'] is True
    r = await audit([row(1,'pool_circuit_breaker',receipt=receipt,intent='risk_exact')], [], [], 'hrs')
    assert r['verified'] is False


@pytest.mark.asyncio
async def test_saturated_history_is_not_empty_evidence():
    r = await audit([row(1,'cancelled',reason='legacy STOP episode unverified')],
                    [order()]*1000, [])
    assert r['verified'] is False
    assert r['signals'][0]['outcome'] == 'unverified'

@pytest.mark.asyncio
async def test_native_hrs_budget_evidence_verifies_without_mutating_old_signal():
    r = await audit([row(1,None,reason='minimum_order_exceeds_risk_budget',receipt='b'*64)],
                    [], [], 'hrs')
    assert r['verified'] is True
    assert r['signals'][0]['reason'] == 'minimum_order_exceeds_risk_budget'
