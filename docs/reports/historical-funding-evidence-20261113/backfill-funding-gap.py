import asyncio,asyncpg,os,time,json
from scripts.export_execution_facts import ExecutionFactExporter,_maybe_open_sink,_maybe_close_sink,_build_client_from_env
from scripts.reconcile_income_attribution import verified_position_at_cutoff
from datetime import datetime,timezone
async def main():
 os.environ['EXECUTION_FACTS_DB_WRITE']='true'
 sink,db=await _maybe_open_sink()
 client,code=_build_client_from_env()
 try:
  exporter=ExecutionFactExporter(account_id='default',api_client=client,sink=sink,page_limit=1000)
  async with db.pool.acquire() as c:
   rows=await c.fetch("""select symbol,max(covered_until_ms) cutoff from trading.execution_fact_cursor
    where account_id='default' and source='userTrades' and page_complete and request_status='ok'
    and symbol in(select distinct symbol from trading.execution_fact_income where account_id='default' and income_type='FUNDING_FEE')
    group by symbol""")
  stop=int(time.time()*1000)-10000
  for r in rows:
   start=r['cutoff'];ok=True
   while start<stop:
    end=min(stop,start+6*86400000)
    result=await exporter.export_fills(r['symbol'],start,end)
    if result.errors or not result.page_complete:
     print('GAP',r['symbol'],str(result.errors),flush=True);ok=False;break
    start=end
   if not ok:continue
   qty,reason,proof=await verified_position_at_cutoff(client,r['symbol'],stop)
   if reason or qty!=0:
    print('POSITION_PENDING',r['symbol'],reason,str(qty),flush=True);continue
   async with db.pool.acquire() as c:
    await c.execute("""insert into trading.position_flat_baseline(account_id,symbol,flat_at,exchange_qty,evidence)
     values('default',$1,$2,0,$3::jsonb) on conflict do nothing""",r['symbol'],datetime.fromtimestamp(stop/1000,timezone.utc),json.dumps(proof))
   print('VERIFIED_FLAT',r['symbol'],flush=True)
 finally:await client.close();await _maybe_close_sink(db)
asyncio.run(main())
