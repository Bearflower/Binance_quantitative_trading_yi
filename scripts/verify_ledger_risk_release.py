#!/usr/bin/env python3
"""Release gates: exchange reads only; final may publish verified report metadata. Never place/cancel orders or write facts."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
from decimal import Decimal
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

class GateError(RuntimeError):pass

def positive(value,zero=False):
    try:number=Decimal(str(value))
    except Exception:raise GateError('invalid numeric evidence') from None
    if not number.is_finite() or number<0 or (number==0 and not zero):raise GateError('invalid numeric evidence')
    return number

def true(value):return value is True or value=='true'

def verify_exchange(positions,orders,algos):
    if not all(isinstance(rows,list) for rows in (positions,orders,algos)):raise GateError('unknown exchange inventory')
    if any(not (true(row.get('reduceOnly')) or true(row.get('closePosition'))) for row in orders+algos):
        raise GateError('unreconciled new-risk order exists')
    for pos in positions:
        try:amount=Decimal(str(pos['positionAmt']))
        except Exception:raise GateError('invalid position amount') from None
        positive(abs(amount),zero=True)
        if not amount:continue
        symbol=pos.get('symbol');entry=positive(pos.get('entryPrice'))
        if not symbol:raise GateError('position symbol missing')
        protected=False;covered=Decimal(0);seen=set()
        for order in orders+algos:
            if order.get('symbol')!=symbol or order.get('side')!=('SELL' if amount>0 else 'BUY'):continue
            if order.get('positionSide','BOTH')!=pos.get('positionSide','BOTH'):continue
            if order.get('type',order.get('orderType')) not in ('STOP','STOP_MARKET'):continue
            stop=positive(order.get('stopPrice',order.get('triggerPrice')))
            mark=positive(pos.get('markPrice',entry))
            if (amount>0 and stop>=mark) or (amount<0 and stop<=mark):continue
            identity=str(order.get('orderId',order.get('algoId',json.dumps(order,sort_keys=True))))
            if identity in seen:continue
            seen.add(identity)
            if true(order.get('closePosition')):protected=True
            else:
                qty=positive(order.get('origQty',order.get('quantity')),zero=True)
                filled=positive(order.get('executedQty','0'),zero=True)
                covered+=max(Decimal(0),qty-filled)
        protected=protected or covered>=abs(amount)
        if not protected:raise GateError('confirmed position lacks full protective stop: '+symbol)

def hash_rows(rows):return hashlib.sha256(json.dumps(rows,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()

async def verify_reservations(rows,positions,client):
    """An occupied reservation is valid only while its proven fill is still open.

    Reconciliation deliberately retains full margin until flat. Do not treat a
    protected live position as a stale reservation, or release one here.
    """
    for row in rows:
        if row['state']!='submitting' or row['strategy_id']!='btc_eth_aggressive':
            raise GateError('unresolved entry reservation requires reconciliation')
        symbol=row['symbol'];client_id=row['client_order_id']
        order=await client.get_order_by_client_id(symbol,client_id)
        if not isinstance(order,dict) or order.get('symbol')!=symbol or order.get('clientOrderId')!=client_id:
            raise GateError('entry reservation exchange order unverified')
        if order.get('status') not in ('FILLED','CANCELED','EXPIRED','EXPIRED_IN_MATCH') or true(order.get('reduceOnly')):
            raise GateError('entry reservation order is not terminal fill')
        reserved=positive(row['quantity']);original=positive(order.get('origQty'))
        executed=positive(order.get('executedQty'))
        if reserved!=original or executed>reserved:
            raise GateError('entry reservation order quantity mismatch')
        matching=[p for p in positions if p.get('symbol')==symbol and Decimal(str(p.get('positionAmt','0')))!=0]
        if len(matching)!=1:
            raise GateError('unresolved entry reservation requires reconciliation')
        amount=Decimal(str(matching[0]['positionAmt']))
        if not amount.is_finite() or abs(amount)>executed or order.get('side')!=('BUY' if amount>0 else 'SELL'):
            raise GateError('entry reservation position mismatch')

def verify_reports(rows,account):
    from shared.accounting.published_report import REPORT_VERSION
    result=[]
    for row in rows:
        body=json.loads(row['report']) if isinstance(row['report'],str) else row['report']
        if row['account_id']!=account or row['report_version']!=REPORT_VERSION or row['window_key']!='monthly:'+str(row['start_ms']):raise GateError('publication pointer identity mismatch')
        if any(body.get(key)!=row[key] for key in ('account_id','window_key','report_version','report_id','start_ms','end_ms')) or body.get('account_coverage_complete') is not True:raise GateError('publication body identity/coverage mismatch')
        result.append({key:row[key] for key in ('account_id','window_key','report_version','report_id','start_ms','end_ms')})
    # Read-only evidence does not refresh reports or claim wall-clock freshness.
    return {'status':'stored_verified' if result else 'unavailable','reports':result}

async def publish_report_proof(db,expectations,account):
    from shared.accounting.published_report import read_published_report,REPORT_VERSION
    from dashboard.backend.services.exchange_ledger import ExchangeLedgerDashboardMixin
    class Projection(ExchangeLedgerDashboardMixin):_STRATEGY_NAME_MAP={};_STRATEGY_EMOJI_MAP={}
    proof=[]
    for expected in expectations:
        start,end=expected['start_ms'],expected['end_ms'];key=f'monthly:{start}'
        if expected['account_id']!=account:raise GateError('reviewed report account mismatch')
        report=await read_published_report(db,account,start,end,key,current=False,refresh_seconds=0)
        identity=dict(account_id=account,start_ms=start,end_ms=end,requested_end_ms=end,window_key=key,report_version=REPORT_VERSION)
        if any(report.get(k)!=v for k,v in identity.items()) or not report.get('report_id'):raise GateError('canonical publication identity mismatch')
        if report.get('publication_status')!='published' or report.get('refresh_error') is not None or report.get('account_coverage_complete') is not True:raise GateError('canonical report unavailable/stale/incomplete')
        net=Decimal(str(report['account_components']['net_usdt']))
        if not net.is_finite() or net!=Decimal(expected['net_usdt']):raise GateError('canonical report differs from independently verified exchange net')
        if report.get('strategy_attribution_complete') is not False:raise GateError('historical attribution changed without independent review')
        values={sid:Projection()._strategy_projection(report,sid)['total_pnl'] for sid in report['by_strategy']}
        if any(value is not None for value in values.values()):raise GateError('unknown attribution shown as known strategy result')
        proof.append(dict(**identity,report_id=report['report_id'],net_usdt=str(net),strategy_attribution_complete=False,strategy_values=values,publication_status='published'))
    return proof

async def check(args):
    import asyncpg
    from scripts.export_execution_facts import _build_client_from_env
    from shared.accounting.income_integrity import require_income_integrity
    from shared.entry_risk_policy import RiskPolicyStore,allocation_frozen
    from types import SimpleNamespace
    if os.environ.get('TRADE_ACCOUNT_ID')!=args.account:raise GateError('credential account binding mismatch')
    def env(key):
        value=os.environ.get('DATABASE_'+key) or os.environ.get('DB_'+key)
        if not value:raise GateError('database configuration missing: '+key)
        return value
    pool=await asyncpg.create_pool(host=env('HOST'),port=int(env('PORT')),database=env('NAME'),user=env('USER'),password=env('PASSWORD'),min_size=1,max_size=1)
    client=None
    try:
        reports={'status':'not_checked','reports':[]}
        reservations=[]
        async with pool.acquire() as conn:
            allocations=[dict(r) for r in await conn.fetch('SELECT * FROM public.capital_allocation ORDER BY month')]
            if args.stage not in ('preflight',):
                await require_income_integrity(conn,args.account)
                if args.stage in ('consumer_gate','freeze_gate','final'):
                    rows=await conn.fetch("SELECT r.account_id,r.window_key,r.report_version,r.report_id,r.start_ms,r.end_ms,r.report FROM trading.ledger_report_active a JOIN trading.ledger_report_run r ON r.report_id=a.report_id WHERE a.account_id=$1 AND a.window_key LIKE 'monthly:%' ORDER BY r.start_ms",args.account)
                    reports=verify_reports(rows,args.account)
                required=['ledger_report_run','ledger_report_active','strategy_risk_policy','strategy_risk_active','strategy_risk_audit','entry_risk_reservation','income_ingestion_attempt','income_ingestion_conflict','income_ingestion_attempt_event']
                for table in required:
                    if not await conn.fetchval("SELECT has_table_privilege(current_user,$1,'SELECT')",'trading.'+table):raise GateError('missing runtime table permission')
                for trigger in ['ledger_report_immutable','immutable_risk_policy','immutable_risk_audit','immutable_income','immutable_income_raw','income_attempt_guard','income_attempt_audit','income_conflict_immutable','income_attempt_event_immutable','protect_reserved_entry_claim']:
                    if not await conn.fetchval("SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE tgname=$1 AND tgenabled IN ('O','A') AND n.nspname='trading'",trigger):raise GateError('missing enabled integrity trigger')
                reservations=[dict(r) for r in await conn.fetch("SELECT client_order_id,strategy_id,symbol,state,quantity FROM trading.entry_risk_reservation WHERE account_id=$1 AND state<>'released'",args.account)]
        if args.stage not in ('preflight','schema','pre_activation'):
            store=RiskPolicyStore(SimpleNamespace(pool=pool),args.account)
            for strategy,mode in [('hrs','monitor_only'),('btc_eth_aggressive','reduced' if args.require_reduced else 'monitor_only'),('btc_eth','capped'),('new_coin','capped')]:
                policy=await store.policy(strategy)
                if policy['revision']!=args.revision or policy['mode']!=mode:raise GateError('policy revision/mode drift')
                if not policy['freeze_allocation']:raise GateError('allocation freeze missing')
            if not await allocation_frozen(SimpleNamespace(pool=pool),args.account):raise GateError('allocator not frozen')
        if args.stage=='final':
            expectations=json.loads(args.report_expectations) if args.report_expectations else []
            if len(expectations)!=3:raise GateError('three independently verified monthly expectations required')
            reports={'status':'published_verified','reports':await publish_report_proof(SimpleNamespace(pool=pool),expectations,args.account)}
        client,code=_build_client_from_env()
        if code or client is None:raise GateError('exchange client unavailable')
        positions=await client.get_position();orders=await client.get_open_orders();algos=await client.get_open_algo_orders()
        verify_exchange(positions,orders,algos)
        await verify_reservations(reservations,positions,client)
        info=await client.get_account_info()
        equity=positive(info.get('accountEquity',info.get('totalMarginBalance')))
        return dict(ok=True,stage=args.stage,account=args.account,allocation_sha256=hash_rows(allocations),
            equity=str(equity),publication_evidence=reports,position_exposure=sorted([{k:row.get(k) for k in ('symbol','positionSide','positionAmt','entryPrice')} for row in positions if Decimal(str(row['positionAmt']))!=0],key=lambda row:(row['symbol'],str(row['positionSide']))),positions_sha256=hash_rows(positions),ordinary_orders_sha256=hash_rows(orders),algo_orders_sha256=hash_rows(algos),
            protected_positions=sum(Decimal(str(row['positionAmt']))!=0 for row in positions),read_only=args.stage!='final',writes_report_metadata=args.stage=='final')
    finally:
        if client is not None:await client.close()
        await pool.close()

def main():
    p=argparse.ArgumentParser();p.add_argument('--stage',required=True,choices=['preflight','schema','pre_activation','policy','collector','strategy_gate','consumer_gate','freeze_gate','final']);p.add_argument('--account',required=True);p.add_argument('--revision',type=int,required=True);p.add_argument('--require-reduced',action='store_true');p.add_argument('--report-expectations')
    args=p.parse_args()
    try:print(json.dumps(asyncio.run(asyncio.wait_for(check(args),150)),sort_keys=True));return 0
    except Exception as exc:print(json.dumps({'ok':False,'error':type(exc).__name__,'reason':str(exc)}));return 2
if __name__=='__main__':raise SystemExit(main())
