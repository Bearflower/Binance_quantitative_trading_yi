from decimal import Decimal
import pytest
from shared.entry_fill_evidence import verified_fill

def evidence():
    record = dict(symbol='ARIAUSDT', side='SELL', order_id='42')
    order = dict(symbol='ARIAUSDT', side='SELL', orderId=42, status='FILLED', origQty='39', executedQty='39')
    trades = [dict(symbol='ARIAUSDT',side='SELL',orderId=42,id=i,qty=q,price='0.1715')
              for i,q in [(1,'33'),(2,'6')]]
    return record,order,trades

def test_partial_fills_combine_to_terminal_quantity():
    assert verified_fill(*evidence()) == (Decimal(39), Decimal('0.1715'))

@pytest.mark.parametrize('defect', ['duplicate','other_order','missing_fill','pending','nan'])
def test_incomplete_or_mismatched_evidence_is_rejected(defect):
    r,o,t=evidence()
    if defect=='duplicate': t[1]['id']=1
    if defect=='other_order': t[1]['orderId']=43
    if defect=='missing_fill': t.pop()
    if defect=='pending': o['status']='NEW'
    if defect=='nan': t[0]['price']='NaN'
    with pytest.raises(ValueError): verified_fill(r,o,t)

def test_executed_shadow_source_still_requires_execution_funnel():
    from datetime import datetime, timezone
    from ai_tuner.monitor.reduced_trial import assess_snapshot
    result=assess_snapshot(dict(signals=dict(real_signals=0,shadow_signals=1,executed_signals=1,unresolved_signals=0)),now=datetime.now(timezone.utc),weekly=False)
    assert 'execution_funnel_unverified' in result['reasons']

@pytest.mark.asyncio
async def test_entry_fill_update_is_account_scoped_and_keeps_complete_rows():
    from unittest.mock import AsyncMock
    from shared.trade_logger import TradeLogger
    r,o,t=evidence()
    r['id']=7
    db=AsyncMock()
    db.fetch_all.return_value=[r]
    client=AsyncMock()
    client.get_order.return_value=o
    client.get_user_trades.return_value=t
    logger=TradeLogger(db,'Dashboard采集器',account_id='test-account')
    await logger.reconcile_entry_fills(client)
    sql,*params=db.execute.await_args.args
    assert params[:4]==[Decimal(39),Decimal('0.1715'),7,'test-account']
    assert "account_id=$4" in sql and "(quantity=0 OR price=0)" in sql
    client.get_user_trades.assert_awaited_once_with('ARIAUSDT',order_id='42',limit=1000)

@pytest.mark.asyncio
async def test_entry_fill_mismatch_never_updates_database():
    from unittest.mock import AsyncMock
    from shared.trade_logger import TradeLogger
    r,o,t=evidence()
    r['id']=7
    t.pop()
    db=AsyncMock()
    db.fetch_all.return_value=[r]
    client=AsyncMock()
    client.get_order.return_value=o
    client.get_user_trades.return_value=t
    await TradeLogger(db,'Dashboard采集器',account_id='test-account').reconcile_entry_fills(client)
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_continues_after_page_of_open_orders_and_targets_old_records():
    from unittest.mock import AsyncMock
    from shared.trade_logger import TradeLogger

    record, order, trades = evidence()
    record["id"] = 101
    db, client = AsyncMock(), AsyncMock()
    db.fetch_all.side_effect = [
        [dict(record, id=i) for i in range(1, 101)], [record], [record]
    ]
    logger = TradeLogger(db, "test", account_id="pm")
    client.get_order.return_value = {"status": "NEW"}
    await logger.reconcile_entry_fills(client)
    client.get_user_trades.assert_not_awaited()
    client.get_order.return_value = order
    client.get_user_trades.return_value = trades
    await logger.reconcile_entry_fills(client)
    assert db.fetch_all.await_args.args[-1] == 100
    db.execute.assert_awaited_once()
    preview = await logger.reconcile_entry_fills(client, record_ids=[101], dry_run=True)
    query, account, ids = db.fetch_all.await_args.args
    assert "executed_at" not in query and account == "pm" and ids == [101]
    assert preview[0]["quantity"] == Decimal(39)
    db.execute.assert_awaited_once()
