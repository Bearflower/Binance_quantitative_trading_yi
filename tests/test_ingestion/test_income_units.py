import asyncio
from decimal import Decimal
from types import SimpleNamespace as NS
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock,patch
import pytest
from shared.income_ingestion import income_observation_digest,_canonical_json,commit_income_observations
from shared.accounting.income_integrity import income_account_lock
from shared.execution_facts_sink import PostgresExecutionFactsSink

@pytest.mark.parametrize('left,right',[
 ({'income':'5.00','tranId':'123456789012345678901234567890123','tradeId':'','time':'1500.0'}, {'income':5,'tranId':123456789012345678901234567890123,'tradeId':None,'time':1500}),
 ({'income':'-0','x':[1,True,None,{'a':2}]},{'income':0,'x':[1.0,True,None,{'a':2.00}]}),
])
def test_semantic_numeric_retry_without_losing_long_identifiers(left,right):
    assert income_observation_digest(left)==income_observation_digest(right)

@pytest.mark.parametrize('body',[{'income':'NaN'},{'time':1.5},{'tradeId':'Infinity'},{'x':float('nan')}])
def test_invalid_wire_numbers_rejected(body):
    with pytest.raises(ValueError):income_observation_digest(body)

def test_unknown_content_retains_json_semantics():
    assert income_observation_digest({'x':True})!=income_observation_digest({'x':1})
    assert income_observation_digest({'x':'1'})!=income_observation_digest({'x':1})

@pytest.mark.asyncio
async def test_sink_rejects_mixed_or_blank_accounts_before_connecting():
    db=NS(connect=AsyncMock());sink=PostgresExecutionFactsSink(db)
    for facts in ([NS(account_id='')],[NS(account_id='a'),NS(account_id='b')]):
        with pytest.raises(ValueError):await sink.upsert_income(facts)
    await sink.upsert_income([]);db.connect.assert_not_called()

@pytest.mark.asyncio
async def test_writer_connects_once_and_uses_one_lease_for_all_transactions():
    connection=NS(execute=AsyncMock());queries=[('INSERT INTO fact VALUES ($1)',(1,))]
    class Pool:
        @asynccontextmanager
        async def acquire(self):yield connection
    db=NS(pool=None,execute_transaction=AsyncMock())
    async def connect():db.pool=Pool()
    db.connect=AsyncMock(side_effect=connect)
    await commit_income_observations(db,'main',queries,[{'value':1}])
    db.connect.assert_awaited_once()
    assert db.execute_transaction.await_count==2
    assert all(call.kwargs['connection'] is connection for call in db.execute_transaction.await_args_list)

@pytest.mark.asyncio
async def test_cancellation_during_unlock_waits_for_cleanup():
    started=asyncio.Event();release=asyncio.Event();done=asyncio.Event()
    async def execute(sql,*args):
        if 'pg_advisory_unlock' in sql:
            started.set();await release.wait();done.set()
    conn=NS(execute=execute)
    async def work():
        async with income_account_lock(conn,'main'):pass
    task=asyncio.create_task(work());await started.wait();task.cancel();release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert done.is_set()

@pytest.mark.asyncio
@pytest.mark.parametrize('initially_connected',[False,True])
async def test_database_existing_transaction_api_connects_and_uses_one_transaction(initially_connected):
    from shared.database import DatabaseManager
    connection=NS(execute=AsyncMock());events=[]
    @asynccontextmanager
    async def transaction():
        events.append('begin')
        try:yield
        except BaseException:events.append('rollback');raise
        else:events.append('commit')
    connection.transaction=transaction
    class Pool:
        @asynccontextmanager
        async def acquire(self):yield connection
    pool=Pool();db=DatabaseManager('localhost',5432,'unit_test','unit','')
    db.pool=pool if initially_connected else None
    async def connect():db.pool=pool
    db.connect=AsyncMock(side_effect=connect)
    assert await db.execute_transaction([('INSERT INTO fact VALUES ($1)',(1,))])
    assert events==['begin','commit']
    assert db.connect.await_count==(0 if initially_connected else 1)
    connection.execute.side_effect=RuntimeError('fail')
    with pytest.raises(RuntimeError):await db.execute_transaction([('INSERT INTO fact VALUES ($1)',(1,))])
    assert events[-2:]==['begin','rollback']
