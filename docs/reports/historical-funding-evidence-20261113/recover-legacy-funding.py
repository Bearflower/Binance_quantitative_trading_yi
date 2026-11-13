import asyncio,asyncpg,os,json,time
from decimal import Decimal
from scripts.reconcile_income_attribution import verified_position_at_cutoff
from scripts.export_execution_facts import _build_client_from_env
async def main():
 c=await asyncpg.connect(host='postgres',database='trading_platform',user='trading_user',password=os.environ['DATABASE_PASSWORD'])
 rows=await c.fetch("""
 select distinct f.account_id,f.symbol,f.order_id,r.strategy,t.id
 from trading.execution_fact_income i
 join trading.reviewed_income_attribution r on (r.account_id,r.income_type,r.tran_id)=(i.account_id,i.income_type,i.tran_id)
 and (r.symbol,r.income,r.asset,r.event_time_utc)=(i.symbol,i.income,i.asset,i.event_time_utc)
 join trading.execution_fact_fill f on (f.account_id,f.symbol,f.trade_id)=(i.account_id,i.symbol,i.trade_id)
 join trading.trade_records t on t.order_id=f.order_id::text and t.symbol=f.symbol and t.strategy=r.strategy
 where i.account_id='default' and t.account_id is null and i.income_type in ('REALIZED_PNL','COMMISSION')
 and not exists(select 1 from trading.trade_records x where x.order_id=f.order_id::text and x.symbol=f.symbol and x.strategy<>r.strategy)
 """)
 count=0
 for r in rows:
  proof=json.dumps(dict(source_kind='trade_record',source_ids=[str(r['id'])],order_id=str(r['order_id']),account_proof='reviewed_income_exact_exchange_fill'))
  result=await c.execute("""insert into trading.order_strategy_evidence(account_id,symbol,order_id,strategy,source_kind,source_id,evidence)
   values($1,$2,$3,$4,'trade_record',$5,$6::jsonb) on conflict do nothing""",r['account_id'],r['symbol'],r['order_id'],r['strategy'],str(r['id']),proof)
  count+=result.endswith('1')
 print('legacy_orders',count,flush=True)
 curs=await c.fetch("""select symbol,max(covered_until_ms) cutoff from trading.execution_fact_cursor
 where account_id='default' and source='userTrades' and request_status='ok' and page_complete group by symbol
 having max(covered_until_ms)>$1""",int(time.time()*1000)-6*86400000)
 client,code=_build_client_from_env()
 try:
  for r in curs:
   qty,reason,proof=await verified_position_at_cutoff(client,r['symbol'],r['cutoff'])
   if qty==0 and not reason:
    stamp=__import__('datetime').datetime.fromtimestamp(r['cutoff']/1000,__import__('datetime').timezone.utc)
    result=await c.execute("""insert into trading.position_flat_baseline(account_id,symbol,flat_at,exchange_qty,evidence)
     values('default',$1,$2,0,$3::jsonb) on conflict do nothing""",r['symbol'],stamp,json.dumps(proof))
    print('anchor',r['symbol'],result,flush=True)
   else:print('pending',r['symbol'],reason,str(qty),flush=True)
 finally:await client.close();await c.close()
asyncio.run(main())
