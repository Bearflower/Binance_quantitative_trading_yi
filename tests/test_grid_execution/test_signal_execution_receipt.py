"""Pushes must describe actual execution evidence, never advisory cooldowns."""
import json
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from strategies.grid.execution.main import ExecutorService
from strategies.grid.execution.planner import ExchangeFilters, PlanLimitExceeded
from strategies.grid.execution.store import DecisionStore, migrate


def decision():
    return dict(decision_id="d1", symbol="ETHUSDT", state="WEAK_TREND",
                calculated_at_ms=1795694700831, executable=True,
                last_closed_kline_ms={"15m": 1795694400000, "1h": 1795694400000,
                                      "4h": 1795694400000}, margin_used="17",
                grid_params=dict(lower_boundary="1595.053669457970700",
                    upper_boundary="1804.326330542029300", grid_count=10,
                    grid_mode="ARITHMETIC", stop_loss_low="1553.199137241158980",
                    stop_loss_high="1846.180862758841020", profit_rate="0.0123124",
                    grid_spacing="20.927266108405860"),
                feasibility=dict(feasible=True, grid_count=10,
                                 qty_per_grid="0.010001823861998364", binding="STRUCT"))


def service():
    s = ExecutorService.__new__(ExecutorService)
    s.settings = SimpleNamespace(max_margin_usdt=D(240), max_notional_usdt=D(2400),
                                 max_open_orders=13, expected_leverage=5)
    s.exchange = SimpleNamespace(
        verify_account=AsyncMock(return_value=ExchangeFilters(D('.01'), D('.001'), D('.001'), D(20))),
        ticker_price=AsyncMock(return_value=D('1699.69')),
        account_info=AsyncMock(return_value=dict(totalAvailableBalance=D('286.49879946'),
            positionInitialMargin=D(0), openOrderInitialMargin=D(0))))
    return s


@pytest.mark.asyncio
async def test_actual_rejected_plan_retains_exact_budget_and_reason_for_push():
    s = service()
    with pytest.raises(PlanLimitExceeded, match="order notional below minimum"):
        await s._plan(decision())
    r = s._plan_receipt
    assert r['decision_id'] == 'd1'
    assert r['budget'] == '17' and r['leverage'] == 5
    assert r['status'] == 'REJECTED'
    assert r['reason'] == 'order notional below minimum'
    assert r['min_notional'] == '20'
    assert r['quantity'] == '0.004'
    assert D(r['order_notional']) < D(20)


@pytest.mark.asyncio
async def test_passed_plan_receipt_uses_exact_submitted_quantity_and_order_count():
    s = service()
    d = decision()
    d['margin_used'] = '100'
    d['feasibility']['qty_per_grid'] = '0.1'
    p = await s._plan(d)
    assert s._plan_receipt['quantity'] == str(p.orders[0].quantity)
    assert s._plan_receipt['order_count'] == len(p.orders)
    assert s._plan_receipt['status'] == 'PASSED'


def test_live_message_omits_config_margin_and_advisory_execution_claims():
    from strategies.grid.execution.signal_receipt import render_receipt
    r = dict(decision_id='d1', updated_at_ms=1795694705000, state='FLAT',
        processed=True, account=dict(available='286.49879946', position_margin='0E-8',
            order_margin='0E-8', position='0', open_orders=0, open_stops=0),
        sizing=dict(status='REJECTED', reason='order notional below minimum',
                    budget='17', leverage=5, max_margin='240', min_notional='20'),
        reduction=None, risk='ALLOW')
    text = render_receipt(r, 'd1', now_ms=1795694706000)
    for expected in ('286.4988', '持仓初始保证金: 0', '5x', '17 USDT', '20 USDT', '未通过'):
        assert expected in text
    assert '500' not in text and '上次减码' not in text and '剩余 4.0h' not in text
    assert '0E-8' not in text


@pytest.mark.parametrize('mismatch,age', [(True, 1000), (False, 60001), (False, -1)])
def test_stale_or_other_decision_receipt_never_claims_feasible(mismatch, age):
    from strategies.grid.execution.signal_receipt import render_receipt
    r = dict(decision_id='other' if mismatch else 'd1', updated_at_ms=100000,
             sizing=dict(status='PASSED'))
    text = render_receipt(r, 'd1', now_ms=100000 + age)
    assert '待确认' in text and '校验通过' not in text


def test_reduction_uses_durable_fill_not_order_intent_and_survives_restart(tmp_path):
    from strategies.grid.execution.signal_receipt import last_reduction
    path = tmp_path / 'execution.sqlite'
    migrate(path)
    store = DecisionStore(path)
    epoch = store.create_epoch('d1')
    for client, role in [('eg-exit', 'EXIT'), ('eg-entry', 'ENTRY')]:
        store.conn.execute('''INSERT INTO order_intent
            (client_order_id,epoch_id,level_index,role,side,price,quantity,status,created_at_ms)
            VALUES(?,?,0,?,'SELL','1700','0.1','NEW',1)''', (client, epoch, role))
    assert last_reduction(store) is None
    store.record_fill('eg-entry', '1', '1', '.1', '1700')
    assert last_reduction(store) is None
    store.record_fill('eg-exit', '2', '2', '.04', '1700')
    store.conn.execute('UPDATE fill SET created_at_ms=1795694705000 WHERE trade_id="2"')
    store.close()
    store = DecisionStore(path)
    r = last_reduction(store)
    assert r['quantity'] == '.04' and r['trade_id'] == '2'
    from strategies.grid.execution.signal_receipt import render_reduction
    assert '20:05:05' in render_reduction(r)
    assert '入账' in render_reduction(r)
    store.close()


def test_live_advice_never_labels_config_as_account_or_records_a_reduction():
    from strategies.grid.margin_advisor import MarginAdvisor
    advisor = MarginAdvisor.__new__(MarginAdvisor)
    advisor.live_reporting = True
    advisor.base_margin = D(500)
    advice = SimpleNamespace(suggested_margin=D(17))
    text = advisor.format_section(advice)
    assert '策略建议预算: 17 USDT' in text
    assert '当前保证金' not in text and '上次减码' not in text
    assert '150' not in text and '冷却' not in text


@pytest.mark.asyncio
async def test_classic_margin_snapshot_keeps_position_and_order_margin_distinct():
    from strategies.grid.execution.exchange import ClassicGridExchange
    client = AsyncMock()
    client.use_unified_account = False
    client._request.side_effect = [dict(canTrade=True, multiAssetsMargin=False, dualSidePosition=False),
        dict(totalMarginBalance='300', availableBalance='250',
             totalPositionInitialMargin='20', totalOpenOrderInitialMargin='30')]
    actual = await ClassicGridExchange(client).account_info()
    assert actual['positionInitialMargin'] == D(20)
    assert actual['openOrderInitialMargin'] == D(30)


@pytest.mark.asyncio
async def test_receipt_cross_process_handoff_and_unknown_account(tmp_path):
    from strategies.grid.execution.signal_receipt import SignalReceiptWriter, read_receipt
    from strategies.grid.execution.risk import RiskStatus
    path = tmp_path / 'handoff.sqlite'
    migrate(path)
    s = service()
    s.store = DecisionStore(path)
    d = decision()
    d['decision_id'] = s.store.publish(d, 'hash')
    s._risk_result = SimpleNamespace(status=RiskStatus.ALLOW)
    s.stream = SimpleNamespace(ready=True)
    s.exchange.position = AsyncMock(return_value=[dict(positionAmt='0')])
    s.exchange.open_orders = AsyncMock(return_value=[])
    s.exchange.open_stops = AsyncMock(return_value=[])
    with pytest.raises(PlanLimitExceeded):
        await s._plan(d)
    s.store.acknowledge_after_action(d['decision_id'], None)
    await SignalReceiptWriter().capture(s)
    reader = DecisionStore(path)
    r = read_receipt(reader)
    assert r['processed'] is True and r['sizing']['status'] == 'REJECTED'
    assert r['account']['position_margin'] == '0'
    s.exchange.account_info.side_effect = RuntimeError('unavailable')
    await SignalReceiptWriter().capture(s)
    r = read_receipt(reader)
    assert r['account'] is None
    assert r['account_error'] == 'RuntimeError'
    reader.close()
    s.store.close()


@pytest.mark.asyncio
async def test_actual_message_replaces_legacy_calculation_after_matching_receipt(tmp_path, monkeypatch):
    from tests.test_strategies.test_grid_capital_linkage import make_bot, make_analysis, make_advice
    from strategies.grid.execution.contracts import decode_grid_params, decode_feasibility
    from strategies.grid.execution.signal_receipt import RECEIPT_KEY, PENDING_TEXT
    from strategies.grid.signal_bot import GridSignal
    from datetime import datetime
    path = tmp_path / 'push.sqlite'
    migrate(path)
    monkeypatch.setenv('GRID_EXECUTION_HANDOFF_DB', str(path))
    bot = make_bot()
    d = decision()
    message = bot._generate_signal_message('ETHUSDT', make_analysis(),
        decode_grid_params(d['grid_params']), decode_feasibility(d['feasibility']), D(17), make_advice(D(17)))
    assert '10x' not in message and '当前保证金: 500' not in message
    assert '可开' not in message and '建议减少' not in message
    assert PENDING_TEXT in message
    signal = GridSignal('ETHUSDT', make_analysis(), None, datetime.now(), message, None, '')
    r = dict(decision_id='d1', updated_at_ms=1795694705000, state='FLAT', processed=True,
             account=None, sizing=dict(status='REJECTED',reason='order notional below minimum',
                    leverage=5, budget='17', max_margin='240', min_notional='20'))
    bot._decision_store.conn.execute('INSERT INTO schema_meta VALUES(?,?)', (RECEIPT_KEY, json.dumps(r)))
    monkeypatch.setattr('strategies.grid.signal_bot._now_ms', lambda: 1795694706000)
    await bot._attach_execution_receipt(signal, 'd1')
    assert '5x' in signal.message and '未通过' in signal.message
    assert PENDING_TEXT not in signal.message
    bot._decision_store.close()
