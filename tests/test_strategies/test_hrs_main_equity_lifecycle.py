import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from strategies.hrs import main as hrs_main


@pytest.fixture(autouse=True)
def inert_funding_inventory(monkeypatch):
    async def idle():await asyncio.Event().wait()
    inventory=SimpleNamespace(collect_once=AsyncMock(),run=idle,close=AsyncMock())
    monkeypatch.setattr(hrs_main,'FundingInventoryCollector',MagicMock(return_value=inventory),raising=False)


def strategy(run):
    return SimpleNamespace(run=run,capital_mgr=object(),position_manager=object(),decision_recorder=object())


@pytest.mark.asyncio
async def test_enabled_gate_requires_shared_entry_guard_before_strategy_runs(monkeypatch):
    monkeypatch.setenv('HRS_EQUITY_GATE_ENABLED','1')
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES','btc_eth')
    factory=MagicMock()
    monkeypatch.setattr(hrs_main,'HrsEquityService',factory,raising=False)
    item=strategy(AsyncMock())
    with pytest.raises(ValueError,match='ENTRY_EQUITY_GATE_STRATEGIES'):
        await hrs_main.run_with_equity_service(item,object(),object())
    factory.assert_not_called()
    item.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_gate_runs_existing_strategy_without_collector(monkeypatch):
    monkeypatch.delenv('HRS_EQUITY_GATE_ENABLED',raising=False)
    factory=MagicMock()
    monkeypatch.setattr(hrs_main,'HrsEquityService',factory,raising=False)
    item=strategy(AsyncMock(return_value='done'))
    assert await hrs_main.run_with_equity_service(item,object(),object())=='done'
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_initial_not_ready_refresh_precedes_run_and_does_not_stop_exits(monkeypatch):
    monkeypatch.setenv('HRS_EQUITY_GATE_ENABLED','true')
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES','hrs,btc_eth')
    events=[]
    async def refresh():
        events.append('refresh')
        return dict(ready=False,allowed=False,reasons=['daily_origin_missing'])
    async def collector():
        try:
            events.append('collector_run')
            await asyncio.Event().wait()
        finally:
            events.append('collector_stopped')
    service=SimpleNamespace(refresh=AsyncMock(side_effect=refresh),run=collector,close=AsyncMock(side_effect=lambda:events.append('close')))
    async def trading():
        assert 'refresh' in events
        events.append('strategy_run')
        return 'finished'
    item=strategy(trading)
    factory=MagicMock(return_value=service)
    monkeypatch.setattr(hrs_main,'HrsEquityService',factory,raising=False)
    db,api=object(),object()
    assert await hrs_main.run_with_equity_service(item,db,api)=='finished'
    factory.assert_called_once_with(db,api,item.capital_mgr,item.position_manager,recorder=item.decision_recorder)
    assert events.index('refresh')<events.index('strategy_run')
    assert events.index('close')<events.index('collector_stopped')
    assert item.equity_service is None


@pytest.mark.asyncio
async def test_cancelled_strategy_cleans_up_collector_before_returning(monkeypatch):
    monkeypatch.setenv('HRS_EQUITY_GATE_ENABLED','1')
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES','hrs')
    entered=asyncio.Event()
    stopped=asyncio.Event()
    async def collector():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    async def trading():
        entered.set()
        await asyncio.Event().wait()
    service=SimpleNamespace(refresh=AsyncMock(return_value=dict(ready=False,allowed=False,reasons=[])),run=collector,close=AsyncMock())
    monkeypatch.setattr(hrs_main,'HrsEquityService',MagicMock(return_value=service),raising=False)
    item=strategy(trading)
    task=asyncio.create_task(hrs_main.run_with_equity_service(item,object(),object()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    service.close.assert_awaited_once()
    assert stopped.is_set()
    assert item.equity_service is None


@pytest.mark.asyncio
async def test_unexpected_collector_exit_is_supervised_and_strategy_is_cancelled(monkeypatch):
    monkeypatch.setenv('HRS_EQUITY_GATE_ENABLED','1')
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES','hrs')
    stopped=asyncio.Event()
    async def failed():
        raise RuntimeError('collector failed')
    async def trading():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    service=SimpleNamespace(refresh=AsyncMock(return_value=dict(ready=False,allowed=False,reasons=[])),run=failed,close=AsyncMock())
    monkeypatch.setattr(hrs_main,'HrsEquityService',MagicMock(return_value=service),raising=False)
    with pytest.raises(RuntimeError,match='equity collector stopped'):
        await asyncio.wait_for(hrs_main.run_with_equity_service(strategy(trading),object(),object()),timeout=1)
    assert stopped.is_set()
    service.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_refreshes_then_closes_collector_before_database_disconnect(monkeypatch):
    for name in ('BINANCE_API_KEY','BINANCE_API_SECRET','KLINE_SERVICE_URL','NOTIFICATION_SERVICE_URL',
                 'DATABASE_HOST','DATABASE_PORT','DATABASE_NAME','DATABASE_USER','DATABASE_PASSWORD'):
        monkeypatch.setenv(name,'5432' if name=='DATABASE_PORT' else 'test')
    monkeypatch.delenv('AI_TUNER_SNAPSHOT_DB_USER',raising=False)
    monkeypatch.setenv('HRS_EQUITY_GATE_ENABLED','1')
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES','hrs')
    events=[]
    def client():
        obj=MagicMock()
        obj.__aenter__=AsyncMock(return_value=obj)
        obj.__aexit__=AsyncMock(return_value=None)
        return obj
    api,kline,notification=client(),client(),client()
    db=SimpleNamespace(connect=AsyncMock(),disconnect=AsyncMock(side_effect=lambda:events.append('db_disconnect')))
    item=strategy(AsyncMock(side_effect=lambda:events.append('strategy_run')))
    for method in ('set_binance_client','set_kline_service','set_notification_client','set_database','initialize'):
        setattr(item,method,AsyncMock())
    async def collector():
        try:
            await asyncio.Event().wait()
        finally:
            events.append('collector_stopped')
    service=SimpleNamespace(refresh=AsyncMock(side_effect=lambda:(events.append('refresh') or dict(ready=False,allowed=False,reasons=[]))),
                            run=collector,close=AsyncMock(side_effect=lambda:events.append('collector_close')))
    monkeypatch.setattr(hrs_main,'BinanceClient',MagicMock(return_value=api))
    monkeypatch.setattr(hrs_main,'KLineService',MagicMock(return_value=kline))
    monkeypatch.setattr(hrs_main,'NotificationClient',MagicMock(return_value=notification))
    monkeypatch.setattr(hrs_main,'DatabaseManager',MagicMock(return_value=db))
    monkeypatch.setattr(hrs_main,'HRSStrategy',MagicMock(return_value=item))
    monkeypatch.setattr(hrs_main,'HrsEquityService',MagicMock(return_value=service),raising=False)
    monkeypatch.setattr(hrs_main,'TradeLogger',MagicMock(return_value=SimpleNamespace(ensure_table_exists=AsyncMock())))
    monkeypatch.setattr(hrs_main.condition_orders,'ensure_table',AsyncMock())
    monkeypatch.setattr(hrs_main,'load_strategy_config_with_metadata',lambda _:({'notification':{'enabled':False}},{}))
    monkeypatch.setattr(hrs_main,'record_loaded_version',lambda *a:None)
    monkeypatch.setattr(hrs_main,'setup_logging',lambda **kw:None)
    await hrs_main.main()
    assert events.index('refresh')<events.index('strategy_run')
    assert events.index('collector_stopped')<events.index('db_disconnect')


@pytest.mark.asyncio
async def test_funding_inventory_receipt_precedes_scoring_and_background_is_joined(monkeypatch):
    monkeypatch.setenv('HRS_EQUITY_GATE_ENABLED','1')
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES','hrs')
    events=[]
    async def idle(name):
        try:
            events.append(name+'_started')
            await asyncio.Event().wait()
        finally:events.append(name+'_stopped')
    inventory=SimpleNamespace(collect_once=AsyncMock(side_effect=lambda:events.append('funding_receipt')),
                              run=lambda:idle('funding'),close=AsyncMock())
    factory=MagicMock(return_value=inventory)
    monkeypatch.setattr(hrs_main,'FundingInventoryCollector',factory,raising=False)
    service=SimpleNamespace(refresh=AsyncMock(side_effect=lambda:events.append('equity_refresh') or dict(ready=True,allowed=True,reasons=[])),
                            run=lambda:idle('equity'),close=AsyncMock())
    monkeypatch.setattr(hrs_main,'HrsEquityService',MagicMock(return_value=service))
    async def trading():
        assert events.index('funding_receipt')<events.index('equity_refresh')
        await asyncio.sleep(0)
        return 'natural_run'
    item=strategy(trading);api=object()
    assert await hrs_main.run_with_equity_service(item,object(),api)=='natural_run'
    factory.assert_called_once_with(api,item.decision_recorder,account_id='default')
    assert events.index('funding_stopped')<len(events)
    assert events.index('equity_stopped')<len(events)


@pytest.mark.asyncio
async def test_unexpected_funding_inventory_exit_cancels_strategy_and_equity_collector(monkeypatch):
    monkeypatch.setenv('HRS_EQUITY_GATE_ENABLED','1')
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES','hrs')
    stopped=asyncio.Event()
    async def idle():await asyncio.Event().wait()
    async def failed():raise RuntimeError('unexpected funding collector exit')
    inventory=SimpleNamespace(collect_once=AsyncMock(),run=failed,close=AsyncMock())
    monkeypatch.setattr(hrs_main,'FundingInventoryCollector',MagicMock(return_value=inventory),raising=False)
    service=SimpleNamespace(refresh=AsyncMock(return_value=dict(ready=True,allowed=True,reasons=[])),run=idle,close=AsyncMock())
    monkeypatch.setattr(hrs_main,'HrsEquityService',MagicMock(return_value=service))
    async def trading():
        try:await idle()
        finally:stopped.set()
    with pytest.raises(RuntimeError,match='funding inventory collector stopped'):
        await asyncio.wait_for(hrs_main.run_with_equity_service(strategy(trading),object(),object()),timeout=1)
    assert stopped.is_set()
    service.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_funding_shutdown_failure_still_joins_all_children(monkeypatch):
    monkeypatch.setenv('HRS_EQUITY_GATE_ENABLED','1')
    monkeypatch.setenv('ENTRY_EQUITY_GATE_STRATEGIES','hrs')
    stopped=[]
    async def idle(name):
        try:await asyncio.Event().wait()
        finally:stopped.append(name)
    inventory=SimpleNamespace(collect_once=AsyncMock(),run=lambda:idle('funding'),
                              close=AsyncMock(side_effect=RuntimeError('funding close failed')))
    monkeypatch.setattr(hrs_main,'FundingInventoryCollector',MagicMock(return_value=inventory))
    service=SimpleNamespace(refresh=AsyncMock(return_value=dict(ready=True,allowed=True,reasons=[])),
                            run=lambda:idle('equity'),close=AsyncMock())
    monkeypatch.setattr(hrs_main,'HrsEquityService',MagicMock(return_value=service))
    item=strategy(AsyncMock(return_value='done'))
    with pytest.raises(RuntimeError,match='funding close failed'):
        await asyncio.wait_for(hrs_main.run_with_equity_service(item,object(),object()),timeout=1)
    assert set(stopped)=={'funding','equity'}
    assert item.equity_service is None
