"""真实隔离 PostgreSQL 验证模式切点、同号订单和事务内重复检查。"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from ai_tuner.reconciler import nightly_exit as mod


@pytest.fixture(scope='module')
def postgres():
    if not all(shutil.which(x) for x in ('initdb','pg_ctl')):
        pytest.skip('需要本地隔离 PostgreSQL')
    with tempfile.TemporaryDirectory(prefix='nightly-epoch-') as tmp:
        data=Path(tmp)/'data'
        subprocess.run(['initdb','-D',str(data),'-U','exit_test_admin','-A','trust','--no-locale','-E','UTF8'],check=True,capture_output=True)
        subprocess.run(['pg_ctl','-D',str(data),'-l',str(Path(tmp)/'log'),'-o',f"-F -k {tmp} -h '' -p 55483",'-w','start'],check=True,capture_output=True)
        try:
            yield dict(host=tmp,port=55483,user='exit_test_admin',database='postgres')
        finally:
            subprocess.run(['pg_ctl','-D',str(data),'-m','immediate','-w','stop'],check=True,capture_output=True)


@pytest.mark.asyncio
async def test_boundary_same_order_id_and_repeated_repair(postgres):
    conn=await asyncpg.connect(**postgres)
    pool=await asyncpg.create_pool(**postgres,min_size=1,max_size=3)
    boundary=datetime.now(timezone.utc).replace(microsecond=0)-timedelta(hours=1)
    cutoff=int(boundary.timestamp()*1000)
    local=boundary.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    before=boundary-timedelta(milliseconds=1)
    before_local=local-timedelta(milliseconds=1)
    try:
        await conn.execute('''CREATE SCHEMA trading;
            CREATE TABLE trading.execution_fact_fill(account_id text,symbol text,trade_id bigint,order_id bigint,side text,
                qty numeric,price numeric,commission numeric,commission_asset text,realized_pnl numeric,trade_time_utc timestamptz);
            CREATE TABLE trading.execution_fact_income(account_id text,symbol text,trade_id bigint,income_type text,
                income numeric,asset text,event_time_utc timestamptz);
            CREATE TABLE trading.trade_records(account_id text,strategy text,symbol text,order_id text,algo_id text,side text,
                order_type text,status text,close_reason text,quantity numeric,price numeric,commission numeric,
                realized_pnl numeric,executed_at timestamp,pnl_source text);
            CREATE TABLE public.pnl_state(account_id text,last_reconciled_ts bigint);
            CREATE TABLE public.pnl_state_gaps(account_id text,event_ms bigint);''')
        await conn.execute("INSERT INTO public.pnl_state VALUES ('default',$1),($2,$1)",cutoff-86400000,f'classic:{cutoff}:default')
        for tid,oid,stamp in [(1,123,boundary),(2,777,before),(3,123,before)]:
            await conn.execute("INSERT INTO trading.execution_fact_fill VALUES ('default','ETHUSDT',$1,$2,'BUY',3,2,.01,'USDT',.5,$3)",tid,oid,stamp)
            await conn.execute("INSERT INTO trading.execution_fact_income VALUES ('default','ETHUSDT',$1,'REALIZED_PNL',.5,'USDT',$2)",tid,stamp)
        # 相同子订单号与父条件单号在两个模式分别出现，旧模式不能屏蔽当前缺口或污染归属。
        await conn.execute("""INSERT INTO trading.trade_records(account_id,strategy,symbol,order_id,algo_id,side,order_type,executed_at)
            VALUES ('default','old','ETHUSDT','123',NULL,'BUY','EXCHANGE_FILL',$1),
                   ('default','old','ETHUSDT',NULL,'1','BUY','CONDITIONAL_ORDER',$1),
                   ('default','hrs','ETHUSDT',NULL,'1','BUY','CONDITIONAL_ORDER',$2)""",before_local,local)
        rec=SimpleNamespace(account_id='default',pm=False,cutover=cutoff,excluded_strategies=[],
            state_schema='public',state_table='pnl_state',_ensure_state_table=AsyncMock(),
            db=SimpleNamespace(fetch_all=pool.fetch,pool=pool),
            binance=SimpleNamespace(use_unified_account=False,
                get_algo_order=AsyncMock(return_value=dict(actualOrderId='123',symbol='ETHUSDT',side='BUY',algoStatus='FINISHED',orderType='TAKE_PROFIT_MARKET')),
                get_order=AsyncMock(return_value=dict(symbol='ETHUSDT',orderId=123,side='BUY',status='FILLED',executedQty='3'))))
        with patch.object(mod,'log') as logger:
            await mod.nightly_exit_repair(rec)
        logger.info.assert_called_with('凌晨成交补录完成',inserted=1,unresolved=0,historical_pending=1)
        rows=await conn.fetch("SELECT strategy,quantity,realized_pnl FROM trading.trade_records WHERE order_id='123' AND executed_at >= $1",local)
        assert len(rows)==1 and rows[0]['strategy']=='hrs' and rows[0]['quantity']==3 and rows[0]['realized_pnl']==.5
        gap=dict(symbol='ETHUSDT',order_id=123,side='BUY')
        # 再次直接调用也必须在事务内幂等；历史同号行不会使首次补录被错误跳过。
        assert await mod.repair_order(rec,gap) is False
        assert await conn.fetchval("SELECT count(*) FROM trading.trade_records WHERE order_id='123'")==2
        calls=rec.binance.get_algo_order.await_args_list
        assert len(calls)==2 and all(c.args==(1,) for c in calls)
        # 两个独立数据库连接并发处理同一新模式订单，只能插入一次。
        await conn.execute("DELETE FROM trading.trade_records WHERE order_id='123' AND executed_at >= $1",local)
        results=await asyncio.gather(mod.repair_order(rec,gap),mod.repair_order(rec,gap))
        assert sorted(results)==[False,True]
        assert await conn.fetchval("SELECT count(*) FROM trading.trade_records WHERE order_id='123'")==2
        # 时间未知的旧记录不能被当成另一模式忽略，仍保留原有禁止重复插入的保护。
        await conn.execute("UPDATE trading.trade_records SET executed_at=NULL WHERE order_id='123' AND executed_at >= $1",local)
        assert await mod.repair_order(rec,gap) is False
        assert await conn.fetchval("SELECT count(*) FROM trading.trade_records WHERE order_id='123'")==2
    finally:
        await pool.close()
        await conn.close()
