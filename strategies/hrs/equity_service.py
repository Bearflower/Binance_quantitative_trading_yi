"""Prospective HRS equity collection; missing source facts never authorize entries.

The service owns no exchange credentials or API lifetime. ``refresh`` first uses
ExecutionFactExporter/PostgresExecutionFactsSink/collect_window, with a fresh
exporter so completed old windows cannot hide late events. It then holds exactly
one lease: income session guard -> account policy session lock -> repeatable-read
transaction -> loss lock -> checkpoint/evidence/projection writes. API requests and the
optional recorder happen outside that transaction. The caller owns task creation.

A segment starts once from current DB allocation plus exact current-period HRS
net income. Existing positions require complete fresh fill coverage, exact owners,
active claims and two agreeing account inventories; cached partial sums never
prove ownership. A filled reservation needs authenticated terminal/full-fill proof.
Opening marked equity establishes NAV 1 and makes no pre-activation return claim.
No startup,
deposit or allocation change reseeds a segment. An administrator must install the
allocation mutation journal before a new segment begins. Every later committed
allocation delta is replayed; uncertain commit-time NAV fails closed.
Daily/rolling boundaries use reconstructed inventory plus actual closed historical
mark-price bars when needed, retaining true mark and availability timestamps.
Funding owners are independently reconstructed backwards from verified current
inventory and complete fills; derived labels alone are never human review. Known
held funding deadlines independently observed before settlement require an exact
authoritative receipt or a published zero rate with an actual settlement mark.
Forecasts survive denial/restart; a ready projection expires at the next deadline.
Late pre-activation credits append an audited anchor revision and rebuild saved
valuations, preserving all rolling latches and same-day daily latches.

Environment: TRADE_ACCOUNT_ID; HRS_EQUITY_ALLOW_INCEPTION=true explicitly permits
labelled inception protection; HRS_EQUITY_REFRESH_SECONDS defaults to 30. The
projection always expires by source as_of+120s, regardless of collection duration.
"""
import asyncio
import copy
from dataclasses import asdict
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
import uuid

import structlog

from scripts.collect_execution_facts import collect_window
from scripts.export_execution_facts import ExecutionFactExporter
from shared.accounting.income_integrity import income_account_lock, require_income_integrity
from shared.accounting.report_reader import read_monthly_economic_report,_OWNER_SQL
from shared.execution_facts_sink import PostgresExecutionFactsSink

from .equity_evidence import (
    EvidenceBlocked, allocation_evidence, allocation_event_changes, attributed_incomes, canonical,
    certify_opening_inventory,attribute_funding_from_inventory,historical_boundary_observation,require_funding_publication,certify_reduce_only_lifecycle,
    flat_boundary_observation, number, order_owners, owned_fill_positions, prove_positions,
)
from .equity_reconciliation import ReconciliationSource,rebuild_history,activate_reconciliation,effective_checkpoints
from .loss_gate import GateHistory, SHANGHAI, evaluate_loss_gate
from .loss_gate_repository import LossGateSession, RepositoryConflict
from .risk_ledger import CashFlow, LedgerNotReady, LedgerObservation, PositionMark, initialize_ledger, reconcile_ledger

log = structlog.get_logger()


class _FreshSink:
    """Force a genuine overlap reread; existing sink still owns writes/cursors."""
    def __init__(self, db):
        self.sink = PostgresExecutionFactsSink(db)

    async def load_cursor(self, *args):
        return None

    def __getattr__(self, name):
        return getattr(self.sink, name)


class _SnapshotDB:
    def __init__(self, connection, owners=()):
        self.connection = connection
        self.owners=owners

    async def fetch_all(self, query, *args):
        rows=list(await self.connection.fetch(query,*args))
        if query==_OWNER_SQL:
            rows.extend(row for row in self.owners if row['account_id']==args[0] and str(row['order_id']) in args[1] and row['symbol'] in args[2])
        return rows


class HrsEquityService:
    def __init__(self, db, api, capital_mgr, position_manager, recorder=None):
        self.db, self.api, self.capital_mgr = db, api, capital_mgr
        self.position_manager, self.recorder = position_manager, recorder
        self.account = os.getenv('TRADE_ACCOUNT_ID', 'default')
        self.clock = lambda: datetime.now(timezone.utc)
        self.allow_inception = os.getenv('HRS_EQUITY_ALLOW_INCEPTION', '').lower() == 'true'
        self.refresh_seconds = float(os.getenv('HRS_EQUITY_REFRESH_SECONDS', '30'))
        if not 0 < self.refresh_seconds <= 120:
            raise ValueError('equity_refresh_interval_invalid')
        self._closed = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._anchor_inventory=()
        self._order_source_cache={}
        self._mark_bar_cache={}

    def _now(self):
        value = self.clock()
        if value.tzinfo is None:
            raise EvidenceBlocked('collector_clock_naive')
        return value.astimezone(timezone.utc)

    async def close(self):
        self._closed.set()

    async def run(self):
        while not self._closed.is_set():
            await self.refresh()
            try:
                await asyncio.wait_for(self._closed.wait(), timeout=self.refresh_seconds)
            except asyncio.TimeoutError:
                pass

    async def _capture(self):
        started = self._now()
        inventory = await self.api.get_position()
        independent = await self.api.get_um_account_positions()
        open_orders = await self.api.get_open_orders()
        if not isinstance(open_orders, list):
            raise EvidenceBlocked('open_order_inventory_missing')
        local = self.position_manager.get_all_positions()
        if not isinstance(local, dict):
            raise EvidenceBlocked('local_position_inventory_missing')
        marks = {}
        for symbol in sorted(local):
            marks[symbol] = await self.api._request('GET', '/fapi/v1/premiumIndex',
                                                   params={'symbol': symbol}, signed=False)
        settled={}
        pending=await self.db.fetch_all("SELECT * FROM trading.entry_risk_reservation WHERE account_id=$1 AND strategy_id='hrs' AND state<>'released'",self.account)
        for row in pending:
            if row.get('quantity') is None:
                continue
            order=await self.api.get_order_by_client_id(row['symbol'],row['client_order_id'])
            if (isinstance(order,dict) and order.get('symbol')==row['symbol']
                    and order.get('clientOrderId')==row['client_order_id'] and order.get('status')=='FILLED'
                    and number(order.get('executedQty'),'settled_entry_quantity')==number(row['quantity'],'reservation_quantity')
                    and number(order.get('origQty'),'settled_entry_original_quantity')==number(row['quantity'],'reservation_quantity')):
                settled[row['client_order_id']]={key:order.get(key) for key in ('symbol','orderId','clientOrderId','status','executedQty','origQty','side')}
        captured = self._now()
        if not timedelta(0) <= captured-started <= timedelta(seconds=120):
            raise EvidenceBlocked('inventory_capture_stale')
        return copy.deepcopy(dict(started_at=started, captured_at=captured, inventory=inventory,
                    independent=independent, open_orders=open_orders, local=local, marks=marks,settled_reservations=settled))

    async def _collect(self, start, end, snapshot):
        exporter = ExecutionFactExporter(self.account, self.api, _FreshSink(self.db))
        # Wire income times are inclusive milliseconds; report_reader uses a
        # half-open range. Collect/query [start, as_of_ms+1) to include all
        # authoritative events exactly at the valuation millisecond.
        query_end = end + 1
        result = await collect_window(exporter, self.db, start, query_end, pause_seconds=0)
        # An existing position may have neither an income nor an order inside the
        # short current window. Still collect its exact signed fill stream.
        symbols = {row['symbol'] for row in snapshot['inventory'] if isinstance(row, dict)
                   and row.get('symbol') and number(row.get('positionAmt'), 'position_quantity_missing') != 0}
        for symbol in sorted(symbols):
            # collect_window applies the seven-day API limit. Reuse it for long
            # ranges; explicit symbol-only rereads are bounded the same way here.
            left = start
            while left < query_end:
                right = min(left + 7*24*3600*1000, query_end)
                page = await exporter.export_fills(symbol, left, right)
                if page.errors or not page.page_complete:
                    result['gaps'].append(f'fills:{symbol}:{left}-{right}')
                left = right
        if result['gaps']:
            raise EvidenceBlocked('source_collection_incomplete')
        result['position_symbols_covered']=sorted(symbols)
        return result

    async def _funding_publication_evidence(self,snapshot,start_ms,end_ms):
        """GET-only published rates plus durable, independently observed deadlines.

        A completed income page is deliberately absent from the finality proof.
        Forecasts are archived before ledger evaluation so denial/restart cannot
        erase a deadline. Source availability is retained, never backdated.
        """
        symbols=set(snapshot['local'])
        rows=await self.db.fetch_all("SELECT DISTINCT symbol FROM trading.trade_records WHERE (account_id=$1 OR account_id IS NULL) AND strategy='hrs' AND symbol IS NOT NULL",self.account)
        symbols.update(row['symbol'] for row in rows)
        forecasts=[]
        for symbol,mark in snapshot['marks'].items():
            if isinstance(mark,dict) and mark.get('nextFundingTime') is not None and mark.get('time') is not None:
                forecasts.append(dict(symbol=symbol,source_time=datetime.fromtimestamp(int(mark['time'])/1000,timezone.utc),
                    received_at=snapshot['captured_at'],next_funding_time=datetime.fromtimestamp(int(mark['nextFundingTime'])/1000,timezone.utc),source='authenticated_premium_index'))
        if symbols:
            rows=await self.db.fetch_all("SELECT DISTINCT ON(symbol,next_funding_time) symbol,source_time,received_at,next_funding_time,payload_hash,raw_payload FROM public.ai_tuner_funding_schedule_observations WHERE symbol=ANY($1::text[]) AND next_funding_time >= $2 AND received_at <= $3 ORDER BY symbol,next_funding_time,source_time,received_at",sorted(symbols),datetime.fromtimestamp(start_ms/1000,timezone.utc),snapshot['captured_at'])
            forecasts.extend(dict(row,source='independent_funding_schedule') for row in rows)
        if forecasts:
            # Archive each source observation once; a fresh premium response
            # must not duplicate the whole month of timetable rows every cycle.
            batch=[]
            for forecast in forecasts:
                payload,digest=canonical(dict(forecasts=[forecast]))
                batch.append(dict(evidence_sha256=digest,payload=json.loads(payload)))
            await self.db.execute("INSERT INTO trading.hrs_funding_observation(account_id,evidence_sha256,available_at,payload) SELECT $1,x.evidence_sha256,$2,x.payload FROM jsonb_to_recordset($3::jsonb) AS x(evidence_sha256 text,payload jsonb) ON CONFLICT DO NOTHING",self.account,self._now(),json.dumps(batch))
        archived=await self.db.fetch_all('SELECT payload FROM trading.hrs_funding_observation WHERE account_id=$1 AND available_at<=$2',self.account,self._now())
        for row in archived:
            payload=json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload']
            for item in payload['forecasts']:
                forecasts.append(dict(item,**{key:datetime.fromisoformat(item[key]) for key in ('source_time','received_at','next_funding_time')}))
        unique={canonical(row)[1]:row for row in forecasts}
        published={}
        for symbol in sorted(symbols):
            left=start_ms
            rates={}
            while left<=end_ms:
                right=min(left+7*24*3600*1000-1,end_ms)
                result=await self.api.get_funding_rate_history(symbol,start_time=left,end_time=right,limit=1000)
                if not isinstance(result,list):
                    raise EvidenceBlocked('published_funding_rate_source_missing')
                for row in result:
                    if not isinstance(row,dict) or row.get('symbol',symbol)!=symbol or row.get('fundingTime') is None:
                        raise EvidenceBlocked('published_funding_rate_invalid')
                    at=int(row['fundingTime'])
                    # APIs/mocks may return a superset; only this real interval
                    # participates. Equal identities must still be identical.
                    if not left<=at<=right:
                        continue
                    if at in rates and rates[at]!=row:
                        raise EvidenceBlocked('funding_rate_identity_conflict')
                    rates[at]=copy.deepcopy(row)
                if len(result)>=1000:
                    last=max((int(row['fundingTime']) for row in result),default=left-1)
                    if not left<=last<=right:
                        raise EvidenceBlocked('published_funding_rate_pagination_unproven')
                    left=last+1
                else:
                    left=right+1
            published[symbol]=[rates[key] for key in sorted(rates)]
        snapshot['funding_publication']=dict(forecasts=list(unique.values()),published_rates=published,available_at=self._now())

    async def _authenticate_order_owners(self,snapshot,start_ms,end_ms):
        fills=await self.db.fetch_all('SELECT account_id,symbol,order_id,side FROM trading.execution_fact_fill WHERE account_id=$1 AND trade_time_utc>=$2 AND trade_time_utc<=$3',self.account,datetime.fromtimestamp(start_ms/1000,timezone.utc),datetime.fromtimestamp(end_ms/1000,timezone.utc))
        native=await self.db.fetch_all('SELECT account_id,symbol,order_id,strategy FROM trading.trade_records WHERE account_id=$1 AND order_id IS NOT NULL',self.account)
        known=order_owners(self.account,native)
        missing={(row['symbol'],str(row['order_id'])) for row in fills if known.get((row['symbol'],str(row['order_id']))) is None}
        proofs=[]
        if missing:
            identifiers=sorted({identity for _,identity in missing})
            legacy=await self.db.fetch_all('SELECT id,account_id,symbol,order_id,strategy,side FROM trading.trade_records WHERE order_id=ANY($1::text[]) AND (account_id=$2 OR account_id IS NULL)',identifiers,self.account)
            evidence_table=await self.db.fetch_one("SELECT to_regclass('trading.order_strategy_evidence') AS name")
            linked=[]
            if evidence_table and evidence_table['name']:
                linked=await self.db.fetch_all("SELECT symbol,order_id,strategy,source_kind,evidence FROM trading.order_strategy_evidence WHERE account_id=$1 AND order_id=ANY($2::bigint[])",self.account,[int(item) for item in identifiers])
            retained=[]
            table=await self.db.fetch_one("SELECT to_regclass('btc_eth.condition_orders') AS name")
            if table and table['name']:
                retained=await self.db.fetch_all('SELECT id,strategy_name,account_id,symbol,algo_id,order_id,order_type,status,created_at,updated_at FROM btc_eth.condition_orders WHERE symbol=ANY($1::text[]) AND (account_id=$2 OR account_id IS NULL) ORDER BY created_at DESC NULLS LAST,id DESC',sorted({symbol for symbol,_ in missing}),self.account)
            algo_responses={}
            for symbol,identity in sorted(missing):
                cache=self._order_source_cache.get((symbol,identity))
                candidates=[row for row in legacy if row['symbol']==symbol and str(row['order_id'])==identity]
                owners={row['strategy'] for row in candidates}
                if (cache and cache['source_kind']=='authenticated_exchange_order_and_durable_record'
                        and owners=={cache['strategy']} and {str(row['id']) for row in candidates}==set(cache['source_ids'])
                        and {row['side'] for row in candidates}=={cache['response']['side']}):
                    proofs.append(cache)
                    continue
                expected_sides={row['side'] for row in fills if row['symbol']==symbol and str(row['order_id'])==identity}
                proof=None
                try:
                    if len(owners)==1 and None not in owners:
                        response=await self.api.get_order(symbol,int(identity))
                        if (isinstance(response,dict) and str(response.get('orderId'))==identity and response.get('symbol')==symbol
                                and expected_sides=={response.get('side')} and {row['side'] for row in candidates}==expected_sides):
                            proof=dict(account_id=self.account,symbol=symbol,order_id=identity,strategy=next(iter(owners)),
                                source_kind='authenticated_exchange_order_and_durable_record',source_ids=[str(row['id']) for row in candidates],
                                response={key:response.get(key) for key in ('orderId','symbol','side','status','time','updateTime')},available_at=self._now())
                    if proof is None:
                        for link in linked:
                            if link['symbol']!=symbol or str(link['order_id'])!=identity or link['source_kind']!='pm_algo_link':
                                continue
                            details=json.loads(link['evidence']) if isinstance(link['evidence'],str) else link['evidence']
                            algo=details.get('algo_id')
                            if algo is None:
                                continue
                            condition=await self.db.fetch_all('SELECT id,strategy_name FROM public.condition_orders WHERE symbol=$1 AND algo_id=$2 AND (account_id=$3 OR account_id IS NULL)',symbol,int(algo),self.account)
                            choices={row['strategy_name'] for row in condition}
                            response=await self.api.get_algo_order(algo_id=int(algo))
                            if (len(choices)==1 and choices=={link['strategy']} and isinstance(response,dict)
                                    and str(response.get('algoId'))==str(algo) and str(response.get('actualOrderId'))==identity
                                    and response.get('symbol')==symbol and expected_sides=={response.get('side')}):
                                proof=dict(account_id=self.account,symbol=symbol,order_id=identity,strategy=next(iter(choices)),
                                    source_kind='authenticated_pm_algo_link_and_durable_owner',source_ids=[str(row['id']) for row in condition],
                                    response={key:response.get(key) for key in ('algoId','actualOrderId','symbol','side','algoStatus')},available_at=self._now())
                                break
                except Exception:
                    # A method label, cached digest, API failure or ambiguous link
                    # is never promoted to ownership. The shared reader will deny.
                    proof=None
                if proof is None:
                    # Explicit approved legacy table, independently authenticated
                    # PM child link. No symbol-wide ownership inference and no
                    # order_strategy_evidence backfill is performed.
                    for row in retained:
                        if row['symbol']!=symbol or row['algo_id'] is None:
                            continue
                        group=[item for item in retained if item['symbol']==symbol and item['algo_id']==row['algo_id']]
                        choices={item['strategy_name'] for item in group}
                        if len(choices)!=1 or not choices<={'btc_eth','btc_eth_aggressive','hrs','new_coin'}:
                            continue
                        algo=int(row['algo_id'])
                        if algo not in algo_responses:
                            try:
                                algo_responses[algo]=(await self.api.get_algo_order(algo_id=algo),self._now())
                            except Exception:
                                algo_responses[algo]=(None,self._now())
                        response,available=algo_responses[algo]
                        if (not isinstance(response,dict) or str(response.get('algoId'))!=str(algo)
                                or str(response.get('actualOrderId'))!=identity or response.get('symbol')!=symbol
                                or expected_sides!={response.get('side')} or response.get('algoStatus')!='FINISHED'):
                            continue
                        source_rows=[dict(table='btc_eth.condition_orders',row={key:(value.isoformat() if isinstance(value,datetime) else value) for key,value in dict(item).items()}) for item in group]
                        proof=dict(account_id=self.account,symbol=symbol,order_id=identity,strategy=next(iter(choices)),
                            source_kind='authenticated_retained_pm_condition_owner',source_ids=[str(item['id']) for item in group],source_rows=source_rows,
                            response={key:response.get(key) for key in ('algoId','actualOrderId','symbol','side','algoStatus','quantity')},available_at=available)
                        break
                if proof:
                    self._order_source_cache[(symbol,identity)]=proof
                    proofs.append(proof)
        snapshot['authenticated_order_owners']=proofs
        await self._new_coin_lifecycle(snapshot,start_ms,end_ms,native,proofs)

    async def _new_coin_lifecycle(self,snapshot,start_ms,end_ms,native,proofs):
        """Extend fill proof only; current-allocation income starts unchanged.

        Exact retained new_coin entry identity + signed order establishes its
        owner. Complete fills from its actual creation time and current account
        inventory establish a zero lower bound. Only authenticated reduce-only
        closes of the unique owned position may inherit that owner.
        """
        if not all(key in snapshot for key in ('inventory','independent')):
            return
        owners=order_owners(self.account,native+proofs)
        current_fills=await self.db.fetch_all('SELECT symbol,order_id FROM trading.execution_fact_fill WHERE account_id=$1 AND trade_time_utc>=$2 AND trade_time_utc<=$3',self.account,datetime.fromtimestamp(start_ms/1000,timezone.utc),datetime.fromtimestamp(end_ms/1000,timezone.utc))
        missing={row['symbol'] for row in current_fills if owners.get((row['symbol'],str(row['order_id']))) is None}
        if not missing:
            return
        table=await self.db.fetch_one("SELECT to_regclass('new_coin.orders') AS name")
        if not table or not table['name']:
            return
        sources=await self.db.fetch_all("SELECT id,order_id,symbol,strategy,side,type,quantity,status,created_at FROM new_coin.orders WHERE symbol=ANY($1::text[]) AND strategy='new_coin'",sorted(missing))
        for symbol in sorted({row['symbol'] for row in sources}):
            entries=[]
            for row in sources:
                if row['symbol']!=symbol or row['order_id'] is None or row['side'] not in ('BUY','SELL'):
                    continue
                response=await self.api.get_order(symbol,int(row['order_id']))
                if (not isinstance(response,dict) or str(response.get('orderId'))!=str(row['order_id'])
                        or response.get('symbol')!=symbol or response.get('side')!=row['side'] or response.get('status')!='FILLED'
                        or response.get('reduceOnly') is not False or response.get('time') is None
                        or number(response.get('executedQty'),'legacy_entry_executed')!=number(row['quantity'],'legacy_entry_quantity',positive=True)):
                    continue
                source_row={key:(value.isoformat() if isinstance(value,datetime) else value) for key,value in dict(row).items()}
                entries.append(dict(account_id=self.account,symbol=symbol,order_id=str(row['order_id']),strategy='new_coin',
                    source_kind='authenticated_legacy_new_coin_entry',source_ids=[str(row['id'])],source_rows=[dict(table='new_coin.orders',row=source_row)],
                    response={key:response.get(key) for key in ('orderId','symbol','side','status','reduceOnly','executedQty','origQty','time')},available_at=self._now()))
            if not entries:
                continue
            left=min(int(row['response']['time'])-1 for row in entries)
            if left>end_ms:
                raise EvidenceBlocked('legacy_entry_after_observation')
            bound=left
            exporter=ExecutionFactExporter(self.account,self.api,_FreshSink(self.db))
            coverage=[]
            while left<end_ms+1:
                right=min(left+7*24*3600*1000,end_ms+1)
                result=await exporter.export_fills(symbol,left,right)
                if result.errors or not result.page_complete:
                    raise EvidenceBlocked('legacy_lifecycle_fill_collection_incomplete')
                coverage.append(dict(symbol=symbol,start_ms=left,end_ms=right,page_complete=True))
                left=right
            interval_fills=[dict(row) for row in await self.db.fetch_all('SELECT * FROM trading.execution_fact_fill WHERE account_id=$1 AND symbol=$2 AND trade_time_utc>=$3 AND trade_time_utc<=$4',self.account,symbol,datetime.fromtimestamp(bound/1000,timezone.utc),datetime.fromtimestamp(end_ms/1000,timezone.utc))]
            for entry in entries:
                actual=sum((number(row['qty'],'legacy_entry_fill_quantity',positive=True) for row in interval_fills if str(row['order_id'])==entry['order_id']),Decimal(0))
                if actual!=number(entry['response']['executedQty'],'legacy_entry_executed'):
                    raise EvidenceBlocked('legacy_entry_full_fill_proof_missing')
            known=native+proofs+entries
            signed={}
            availability={}
            choices=order_owners(self.account,known)
            for row in interval_fills:
                if (symbol,str(row['order_id'])) not in choices and row['order_id'] not in signed:
                    signed[row['order_id']]=await self.api.get_order(symbol,int(row['order_id']))
                    availability[row['order_id']]=self._now()
            quantities=[]
            for inventory in (snapshot['inventory'],snapshot['independent']):
                if not isinstance(inventory,list):
                    raise EvidenceBlocked('legacy_current_inventory_missing')
                matches=[row for row in inventory if row.get('symbol')==symbol]
                if len(matches)>1 or any(row.get('positionSide','BOTH')!='BOTH' for row in matches):
                    raise EvidenceBlocked('legacy_current_inventory_ambiguous')
                quantities.append(number(matches[0]['positionAmt'],'legacy_current_quantity') if matches else Decimal(0))
            if quantities[0]!=quantities[1]:
                raise EvidenceBlocked('legacy_current_inventory_conflict')
            inherited=certify_reduce_only_lifecycle(self.account,symbol,quantities[0],interval_fills,known,signed,
                datetime.fromtimestamp(bound/1000,timezone.utc),snapshot['captured_at'],coverage_complete=True)
            for proof in inherited:
                proof['available_at']=availability[int(proof['order_id'])]
                proof['opening_owner_proofs']=entries
                proof['source_coverage']=coverage
            proofs.extend(entries)
            proofs.extend(inherited)
            snapshot.setdefault('extended_fill_coverage',[]).append(dict(symbol=symbol,start_ms=bound,end_ms=end_ms,source_coverage=coverage))
        snapshot['authenticated_order_owners']=proofs

    async def _historical_marks(self,bootstrap,snapshot):
        snapshot['historical_mark_bars']={}
        if bootstrap is None:
            return
        raw=await self.db.fetch_one('SELECT ledger_state FROM trading.hrs_loss_gate_state WHERE account_id=$1',self.account)
        if raw is None:
            return
        ledger=json.loads(raw['ledger_state']) if isinstance(raw['ledger_state'],str) else raw['ledger_state']
        last=datetime.fromisoformat(ledger['as_of'])
        boundary=datetime.combine(last.astimezone(SHANGHAI).date()+timedelta(days=1),time.min,SHANGHAI)
        points=[]
        while boundary<snapshot['captured_at']:
            points.append(boundary)
            boundary+=timedelta(days=1)
        rolling=snapshot['captured_at']-timedelta(days=30)
        if rolling>=bootstrap['inception']:
            points.append(rolling)
        opening=json.loads(bootstrap['opening_evidence']) if isinstance(bootstrap['opening_evidence'],str) else bootstrap['opening_evidence']
        symbols={row['symbol'] for row in opening.get('opening_inventory',()) if row['strategy']=='hrs'}|set(snapshot['local'])
        rows=await self.db.fetch_all("SELECT DISTINCT symbol FROM trading.trade_records WHERE account_id=$1 AND strategy='hrs' AND executed_at>=$2",self.account,bootstrap['inception'].astimezone(SHANGHAI).replace(tzinfo=None))
        symbols.update(row['symbol'] for row in rows if row.get('symbol'))
        for point in points:
            for symbol in sorted(symbols):
                key=f'{symbol}:{int(point.timestamp()*1000)}'
                if key not in self._mark_bar_cache:
                    start=(int(point.timestamp()*1000)//60000)*60000-60000
                    bars=await self.api._request('GET','/fapi/v1/markPriceKlines',params=dict(symbol=symbol,interval='1m',startTime=start,endTime=start+59999,limit=1),signed=False)
                    if not isinstance(bars,list) or len(bars)!=1 or not isinstance(bars[0],list) or len(bars[0])<7:
                        raise EvidenceBlocked('historical_mark_bar_unavailable')
                    if int(bars[0][0])!=start or int(bars[0][6])!=start+59999:
                        raise EvidenceBlocked('historical_mark_bar_identity_conflict')
                    number(bars[0][4],'historical_mark_price',positive=True)
                    self._mark_bar_cache[key]=dict(bar=bars[0],available_at=self._now(),source='/fapi/v1/markPriceKlines')
                snapshot['historical_mark_bars'][key]=self._mark_bar_cache[key]

    @asynccontextmanager
    async def _locked(self):
        async with self.db.pool.acquire() as connection:
            async with income_account_lock(connection, self.account):
                # Session acquisition must precede BEGIN: a waiting SELECT inside
                # repeatable-read freezes an old snapshot before the prior policy
                # holder commits its reservation.
                try:
                    await connection.execute("SELECT set_config('lock_timeout','5000ms',false)")
                    await connection.execute('SELECT pg_advisory_lock(hashtextextended($1,7183))',self.account)
                    async with connection.transaction(isolation='repeatable_read'):
                        key = int.from_bytes(hashlib.sha256(f'hrs-loss-gate:{self.account}'.encode()).digest()[:8], 'big', signed=True)
                        await connection.execute('SELECT pg_advisory_xact_lock($1)', key)
                        await require_income_integrity(connection, self.account)
                        yield connection
                finally:
                    # An interrupted acquisition can still have obtained the
                    # session lock server-side. Always release it before pooling.
                    cleanup=asyncio.create_task(connection.execute(
                        'SELECT pg_advisory_unlock(hashtextextended($1,7183))',self.account))
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise

    async def _read_source(self, connection, start_ms, end_ms, snapshot):
        start = datetime.fromtimestamp(start_ms/1000, timezone.utc)
        end = datetime.fromtimestamp(end_ms/1000, timezone.utc)
        for proof in snapshot.get('authenticated_order_owners',()):
            if proof.get('source_kind')=='authenticated_legacy_new_coin_entry':
                fields=('id','order_id','symbol','strategy','side','quantity')
                expected=[{key:item['row'][key] for key in fields} for item in proof['source_rows'] if item['table']=='new_coin.orders']
                actual=[dict(row) for row in await connection.fetch('SELECT id,order_id,symbol,strategy,side,quantity FROM new_coin.orders WHERE symbol=$1 AND order_id=$2',proof['symbol'],proof['order_id'])]
                if canonical(sorted(expected,key=lambda row:row['id']))[1]!=canonical(sorted(actual,key=lambda row:row['id']))[1] or not expected:
                    raise EvidenceBlocked('authenticated_legacy_entry_source_changed')
            if proof.get('source_kind')!='authenticated_retained_pm_condition_owner':
                continue
            fields=('id','strategy_name','account_id','symbol','algo_id')
            expected=[{key:item['row'][key] for key in fields} for item in proof['source_rows'] if item['table']=='btc_eth.condition_orders']
            actual=[dict(row) for row in await connection.fetch('SELECT id,strategy_name,account_id,symbol,algo_id FROM btc_eth.condition_orders WHERE symbol=$1 AND algo_id=$2 AND (account_id=$3 OR account_id IS NULL)',proof['symbol'],int(proof['response']['algoId']),self.account)]
            if sorted(expected,key=lambda row:row['id'])!=sorted(actual,key=lambda row:row['id']) or not expected:
                raise EvidenceBlocked('authenticated_condition_source_changed')
        report = await read_monthly_economic_report(_SnapshotDB(connection,snapshot.get('authenticated_order_owners',())), self.account, start_ms, end_ms + 1,
                                                   include_reviewed_overrides=False)
        fills = [dict(row) for row in await connection.fetch('''SELECT * FROM trading.execution_fact_fill
            WHERE account_id=$1 AND trade_time_utc >= $2 AND trade_time_utc <= $3''', self.account, start, end)]
        for interval in snapshot.get('extended_fill_coverage',()):
            additional=await connection.fetch('SELECT * FROM trading.execution_fact_fill WHERE account_id=$1 AND symbol=$2 AND trade_time_utc>=$3 AND trade_time_utc<=$4',self.account,interval['symbol'],datetime.fromtimestamp(interval['start_ms']/1000,timezone.utc),end)
            existing={(row['symbol'],row['trade_id']) for row in fills}
            fills.extend(dict(row) for row in additional if (row['symbol'],row['trade_id']) not in existing)
        orders = [dict(row) for row in await connection.fetch('''SELECT account_id,symbol,order_id,strategy,side,status,order_type,
            realized_pnl,executed_at FROM trading.trade_records WHERE account_id=$1 AND order_id IS NOT NULL''', self.account)]
        orders.extend(snapshot.get('authenticated_order_owners',()))
        for order in orders:
            at=order.get('executed_at')
            if isinstance(at,datetime) and at.tzinfo is None:
                order['executed_at']=at.replace(tzinfo=SHANGHAI)
        pending = [dict(row) for row in await connection.fetch("SELECT * FROM trading.entry_risk_reservation WHERE account_id=$1 AND strategy_id='hrs' AND state<>'released'", self.account)]
        return report, fills, orders, pending

    def _entries_clear(self, snapshot, orders, pending, *, bootstrap,fills=()):
        owners = order_owners(self.account, orders)
        for row in snapshot['open_orders']:
            if not isinstance(row, dict) or not row.get('symbol'):
                raise EvidenceBlocked('open_order_identity_missing')
            if row.get('reduceOnly') is True or str(row.get('reduceOnly')).lower() == 'true':
                continue
            choices = owners.get((row['symbol'], str(row.get('orderId'))), set())
            if len(choices) != 1 or None in choices:
                raise EvidenceBlocked('outstanding_entry_owner_unproven')
            if 'hrs' in choices:
                raise EvidenceBlocked('outstanding_hrs_entry')
        for row in pending:
            proof=snapshot.get('settled_reservations',{}).get(row['client_order_id'])
            if proof is None:
                raise EvidenceBlocked('outstanding_hrs_reservation')
            exact=[fill for fill in fills if fill['symbol']==row['symbol'] and str(fill['order_id'])==str(proof['orderId'])]
            if (not exact or sum((number(fill['qty'],'settled_fill_quantity') for fill in exact),Decimal(0))!=number(proof['executedQty'],'settled_entry_quantity')
                    or order_owners(self.account,orders).get((row['symbol'],str(proof['orderId'])))!={'hrs'}):
                raise EvidenceBlocked('settled_reservation_fill_proof_missing')

    async def _allocation_audit(self, connection, bootstrap, as_of):
        control=await connection.fetchrow('SELECT version,installed_at FROM trading.hrs_allocation_audit_control WHERE singleton')
        triggers=await connection.fetchval("""SELECT count(*) FROM pg_trigger
            WHERE tgrelid='public.capital_allocation'::regclass AND tgenabled='O'
            AND ((tgname='hrs_allocation_serialize' AND tgfoid='trading.hrs_serialize_allocation_change()'::regprocedure)
              OR (tgname='hrs_allocation_capture' AND tgfoid='trading.hrs_capture_allocation_event()'::regprocedure)
              OR (tgname='hrs_allocation_no_truncate' AND tgfoid='trading.hrs_reject_allocation_history_change()'::regprocedure))""")
        if not control or control['version']!=1 or triggers!=3 or control['installed_at']>as_of:
            raise EvidenceBlocked('allocation_event_history_missing')
        raw=await connection.fetchval("SELECT COALESCE(jsonb_agg(to_jsonb(a) ORDER BY id),'[]'::jsonb) FROM public.capital_allocation a")
        def decode(value):
            return json.loads(value,parse_float=Decimal) if isinstance(value,str) else value
        current_rows=decode(raw)
        last=await connection.fetchrow('SELECT COALESCE(max(event_id),0) AS id,max(statement_at) AS latest FROM trading.hrs_allocation_event')
        if last['latest'] is not None and last['latest']>as_of:
            raise EvidenceBlocked('allocation_event_after_observation')
        if bootstrap is None:
            # Validates selected identity too; a tied latest active row is not a
            # trustworthy baseline even if LIMIT 1 happened to choose one.
            allocation_event_changes(current_rows,(),current_rows,as_of,as_of)
            return dict(version=1,event_id=last['id'],baseline=current_rows,baseline_json=raw,journal=[],changes=())
        if (bootstrap['allocation_audit_version']!=1 or bootstrap['allocation_audit_event_id'] is None
                or bootstrap['allocation_audit_baseline'] is None or control['installed_at']>bootstrap['inception']):
            raise EvidenceBlocked('allocation_event_history_missing')
        baseline=decode(bootstrap['allocation_audit_baseline'])
        journal=[]
        for row in await connection.fetch('SELECT * FROM trading.hrs_allocation_event WHERE event_id>$1 ORDER BY event_id',bootstrap['allocation_audit_event_id']):
            event=dict(row)
            event['old_row']=decode(event['old_row'])
            event['new_row']=decode(event['new_row'])
            journal.append(event)
        changes=allocation_event_changes(baseline,journal,current_rows,bootstrap['inception'],as_of)
        return dict(version=1,event_id=last['id'],baseline=baseline,journal=journal,changes=changes)

    def _allocation_flows(self,state,changes,events,fills,orders,as_of):
        existing={flow.event_id:flow for flow in state.cash_flows}
        owners=order_owners(self.account,orders)
        flows=[]
        valuation_state=state
        for change in changes:
            identity='allocation:'+change.event_id
            if identity in existing:
                if existing[identity].amount!=change.delta or existing[identity].occurred_at!=change.occurred_at:
                    raise EvidenceBlocked('allocation_event_history_conflict')
                continue
            if change.occurred_at<=valuation_state.as_of:
                raise EvidenceBlocked('late_allocation_requires_reconciliation')
            # Trigger time is a lower bound on commit visibility, not an invented
            # commit timestamp. NAV must be invariant across the uncertainty
            # interval before we can unitize this event without guessing.
            if change.transaction_id is not None:
                economic_change=any(change.not_before<=event.occurred_at<=as_of for event in events)
                exposure_change=any(change.not_before<=row['trade_time_utc']<=as_of
                    and 'hrs' in owners.get((row['symbol'],str(row['order_id'])),set()) for row in fills)
                if economic_change or exposure_change:
                    raise EvidenceBlocked('allocation_effective_nav_unproven')
            try:
                before=flat_boundary_observation(valuation_state,change.occurred_at,self._now(),fills,orders,events,coverage_complete=True,opening_inventory=self._anchor_inventory)
            except EvidenceBlocked:
                raise EvidenceBlocked('allocation_nav_nonflat_unproven') from None
            before_state=reconcile_ledger(valuation_state,before)
            flow=CashFlow(identity,change.delta,change.occurred_at,state.segment_id,
                          before_state.nav,change.evidence_sha256)
            applied=LedgerObservation(before.as_of,state.segment_id,before.incomes,(flow,),(),before.as_of,before.as_of,True,True)
            valuation_state=reconcile_ledger(valuation_state,applied)
            flows.append(flow)
        return tuple(flows)

    def _opening_marks(self,anchor):
        marks=[]
        for raw in anchor.get('opening_hrs_marks',()):
            if raw.get('verified') is not True:
                raise EvidenceBlocked('opening_mark_proof_missing')
            at=raw['marked_at']
            marks.append(PositionMark(raw['position_id'],raw['segment_id'],number(raw['quantity'],'opening_quantity'),
                number(raw['entry_price'],'opening_entry'),number(raw['mark_price'],'opening_mark'),
                datetime.fromisoformat(at) if isinstance(at,str) else at,True))
        return tuple(marks)

    async def _policy_revision(self, connection):
        row = await connection.fetchrow('''SELECT p.revision FROM trading.strategy_risk_policy p
            JOIN trading.strategy_risk_active a USING(account_id,strategy_id,revision)
            WHERE p.account_id=$1 AND p.strategy_id='hrs' AND p.effective_at <= $2 AND p.expires_at > $2''', self.account, self._now())
        if row is None:
            raise EvidenceBlocked('missing_active_equity_policy')
        return row['revision']

    async def _projection(self, connection, revision, result, evidence):
        encoded, digest = canonical(evidence)
        as_of, available = evidence['as_of'], evidence['available_at']
        await connection.execute('''INSERT INTO trading.hrs_equity_evidence
            (account_id,evidence_sha256,segment_id,as_of,available_at,payload) VALUES($1,$2,$3,$4,$5,$6::jsonb)
            ON CONFLICT(account_id,evidence_sha256) DO NOTHING''', self.account, digest,
            evidence.get('segment_id'), as_of, available, encoded)
        expiry = as_of + timedelta(seconds=120) if result['ready'] else as_of
        if result['ready']:
            capture=evidence.get('capture',{})
            held=set(capture.get('local',{}))
            for forecast in capture.get('funding_publication',{}).get('forecasts',()):
                due=forecast['next_funding_time']
                if (forecast['symbol'] in held and forecast['source_time']<=forecast['received_at']<=as_of<due):
                    expiry=min(expiry,due)
        await connection.execute('''INSERT INTO trading.strategy_equity_gate
            (account_id,strategy_id,policy_revision,ready,allowed,as_of,valid_until,reasons,evidence_sha256)
            VALUES($1,'hrs',$2,$3,$4,$5,$6,$7::jsonb,$8)
            ON CONFLICT(account_id,strategy_id) DO UPDATE SET policy_revision=EXCLUDED.policy_revision,
            ready=EXCLUDED.ready,allowed=EXCLUDED.allowed,as_of=EXCLUDED.as_of,valid_until=EXCLUDED.valid_until,
            reasons=EXCLUDED.reasons,evidence_sha256=EXCLUDED.evidence_sha256,updated_at=clock_timestamp()''',
            self.account, revision, result['ready'], result['allowed'], as_of, expiry,
            json.dumps(result['reasons']), digest)
        return dict(result, as_of=as_of, valid_until=expiry, policy_revision=revision, evidence_sha256=digest)

    def _history_start(self,inception,as_of):
        return max(inception,as_of-timedelta(days=30))

    def _history_complete(self, checkpoints, fills, orders, inception):
        if not checkpoints:
            return False
        required=self._history_start(inception,checkpoints[-1].as_of)
        for left, right in zip(checkpoints, checkpoints[1:]):
            if right.as_of<=required:
                continue
            left_at=max(left.as_of,required)
            if right.as_of - left_at <= timedelta(seconds=120):
                continue
            positions = owned_fill_positions(self.account, fills, orders, through=left_at, after=inception,opening_inventory=self._anchor_inventory)
            if any(owner == 'hrs' and qty != 0 for (owner, _), (qty, _) in positions.items()):
                return False
            owners = order_owners(self.account, orders)
            if any(left_at < row['trade_time_utc'] <= right.as_of
                   and 'hrs' in owners.get((row['symbol'], str(row['order_id'])), set()) for row in fills):
                return False
        return True

    async def _commit(self, connection, bootstrap, snapshot, collection, start_ms, end_ms):
        as_of = snapshot['captured_at']
        if not timedelta(0) <= self._now()-as_of < timedelta(seconds=120):
            raise EvidenceBlocked('source_observation_stale')
        report, fills, orders, pending = await self._read_source(connection, start_ms, end_ms, snapshot)
        allocation_row = await connection.fetchrow('''SELECT id,month,entries,created_at FROM public.capital_allocation
            WHERE left(month::text,7)=$1 AND status='active' ORDER BY created_at DESC LIMIT 1''', as_of.astimezone(SHANGHAI).strftime('%Y-%m'))
        allocation = allocation_evidence(dict(allocation_row) if allocation_row else None, as_of)
        revision = await self._policy_revision(connection)
        actual_bootstrap = await connection.fetchrow('SELECT * FROM trading.hrs_equity_segment WHERE account_id=$1 FOR UPDATE', self.account)
        if (bootstrap is None) != (actual_bootstrap is None) or (bootstrap and actual_bootstrap['segment_id'] != bootstrap['segment_id']):
            raise EvidenceBlocked('bootstrap_changed_retry_collection')
        effective_opening_cash=actual_bootstrap['opening_cash'] if actual_bootstrap else None
        opening_revision=None
        if actual_bootstrap:
            anchor=json.loads(actual_bootstrap['opening_evidence']) if isinstance(actual_bootstrap['opening_evidence'],str) else actual_bootstrap['opening_evidence']
            anchor_source_sha256=canonical(anchor)[1]
            if anchor.get('account_flat_anchor_certified') is not True and anchor.get('opening_inventory_certified') is not True:
                raise EvidenceBlocked('opening_inventory_anchor_unproven')
            prior=await connection.fetchrow("SELECT receipt FROM trading.hrs_equity_reconciliation WHERE account_id=$1 AND segment_id=$2 AND receipt ? 'opening_anchor_revision' ORDER BY after_state_version DESC LIMIT 1",self.account,actual_bootstrap['segment_id'])
            if prior:
                receipt=json.loads(prior['receipt']) if isinstance(prior['receipt'],str) else prior['receipt']
                corrected=receipt['opening_anchor_revision']
                anchor=dict(anchor,credited_opening_income=list(corrected['new_credits'].values()))
                effective_opening_cash=number(corrected['new_opening_cash'],'corrected_opening_cash',positive=True)
        audit=await self._allocation_audit(connection,actual_bootstrap,as_of)
        segment = actual_bootstrap['segment_id'] if actual_bootstrap else f'hrs-{uuid.uuid4().hex}'
        session = LossGateSession(connection, self.account, segment)
        stored = await session.load()
        self._entries_clear(snapshot, orders, pending, bootstrap=actual_bootstrap is None,fills=fills)
        if actual_bootstrap is None:
            symbols=[row['symbol'] for row in snapshot['inventory'] if number(row.get('positionAmt'),'position_quantity')!=0]
            claims=[dict(row) for row in await connection.fetch("SELECT id,symbol,strategy FROM trading.position_claims WHERE symbol=ANY($1::text[]) AND claim_state IN ('ACTIVE','PENDING') AND expires_at>clock_timestamp()",symbols)] if symbols else []
            marks,opening_inventory=certify_opening_inventory(self.account,segment,as_of,snapshot['inventory'],snapshot['independent'],
                snapshot['local'],fills,orders,snapshot['marks'],claims,covered_symbols=collection.get('position_symbols_covered',()))
            self._anchor_inventory=opening_inventory
            current_inventory=opening_inventory
        else:
            self._anchor_inventory=tuple(anchor.get('opening_inventory',()))
            marks=prove_positions(self.account,segment,as_of,snapshot['inventory'],snapshot['independent'],snapshot['local'],
                fills,orders,snapshot['marks'],bootstrap=False,inception=actual_bootstrap['inception'],opening_inventory=self._anchor_inventory)
            current=owned_fill_positions(self.account,fills,orders,through=as_of,after=actual_bootstrap['inception'],opening_inventory=self._anchor_inventory)
            current_inventory=tuple(dict(strategy=owner,symbol=symbol,quantity=str(qty),entry_price=str(basis)) for (owner,symbol),(qty,basis) in current.items() if qty!=0)
        report=attribute_funding_from_inventory(report,self.account,as_of,fills,orders,current_inventory,
            coverage_complete=not report.get('coverage_gaps') and not report.get('missing_income_fills') and report.get('account_coverage_complete') is True,available_at=self._now())
        publication=snapshot.get('funding_publication',{})
        funding_proofs=require_funding_publication(self.account,datetime.fromtimestamp(start_ms/1000,timezone.utc),as_of,
            fills,orders,current_inventory,publication.get('forecasts',()),publication.get('published_rates',{}),report)
        all_events=attributed_incomes(report,self.account,segment)
        if actual_bootstrap is None:
            period=datetime.combine(as_of.astimezone(SHANGHAI).date().replace(day=1),time.min,SHANGHAI)
            credited=tuple(event for event in all_events if event.occurred_at>=period)
            events=()
        else:
            old_credits={row['event_id']:row for row in anchor.get('credited_opening_income',())}
            credit_start=datetime.combine(actual_bootstrap['inception'].astimezone(SHANGHAI).date().replace(day=1),time.min,SHANGHAI)
            credited=tuple(event for event in all_events if credit_start<=event.occurred_at<=actual_bootstrap['inception'])
            actual_credits={event.event_id:dict(event_id=event.event_id,kind=event.kind,amount=str(event.amount),occurred_at=event.occurred_at.isoformat()) for event in credited}
            if actual_credits!=old_credits:
                if any(actual_credits.get(key)!=value for key,value in old_credits.items()):
                    raise EvidenceBlocked('opening_income_fact_conflict')
                corrected_cash=effective_opening_cash+sum((number(value['amount'],'opening_credit_amount') for key,value in actual_credits.items() if key not in old_credits),Decimal(0))
                opening_revision=dict(old_credits=old_credits,new_credits=actual_credits,old_opening_cash=str(effective_opening_cash),
                    new_opening_cash=str(corrected_cash),anchor_as_of=actual_bootstrap['inception'],available_at=self._now(),
                    source_report_sha256=canonical(report)[1],opening_evidence_sha256=anchor_source_sha256)
                effective_opening_cash=corrected_cash
            events=tuple(event for event in all_events if event.occurred_at>actual_bootstrap['inception'])
        evidence = dict(account_id=self.account, segment_id=segment, as_of=as_of, available_at=self._now(),
                        capture=snapshot, source_collection=collection, income_report=report,
                        fills=fills, order_owners=orders, allocation=allocation.source,funding_publication_proofs=funding_proofs,
                        allocation_audit=dict(version=audit['version'],through_event_id=audit['event_id'],journal=audit['journal']),
                        reconstructed_boundaries=[])
        if actual_bootstrap is None:
            if stored is not None:
                raise EvidenceBlocked('bootstrap_existing_economics_unreconciled')
            # A fill anywhere in the inventory capture interval defeats a flat
            # point proof even if it roundtripped back to zero before the response.
            if any(snapshot['started_at']<=row['trade_time_utc']<=as_of for row in fills):
                raise EvidenceBlocked('bootstrap_capture_contains_account_fills')
            evidence['account_flat_anchor_certified']=not opening_inventory
            evidence['opening_inventory_certified']=True
            evidence['opening_inventory']=opening_inventory
            evidence['opening_hrs_marks']=[asdict(mark) for mark in marks]
            evidence['source_start_ms']=start_ms
            evidence['credited_opening_income']=[dict(event_id=event.event_id,kind=event.kind,amount=str(event.amount),occurred_at=event.occurred_at.isoformat()) for event in credited]
            evidence['opening_cash_definition']='current_allocation_plus_exact_current_period_hrs_net_income'
            obs = LedgerObservation(as_of, segment, (), (), marks, as_of, snapshot['captured_at'], True, True)
            state = initialize_ledger(segment, allocation.amount+sum((event.amount for event in credited),Decimal(0)), obs)
            await connection.execute('''INSERT INTO trading.hrs_equity_segment
                (account_id,segment_id,inception,opening_cash,opening_allocation_sha256,opening_evidence,
                 allocation_audit_event_id,allocation_audit_baseline,allocation_audit_version)
                VALUES($1,$2,$3,$4,$5,$6::jsonb,$7,$8::jsonb,1)''', self.account, segment, as_of, state.cash_allocation,
                allocation.identity, canonical(evidence)[0],audit['event_id'],audit['baseline_json'])
            gate = evaluate_loss_gate(None, state.checkpoint, GateHistory((state.checkpoint,),as_of,as_of,True),
                                      allow_inception_segment=self.allow_inception)
            stored = await session.commit(state,gate.state,expected_version=None,checkpoint_id=f'{segment}:{end_ms}')
        else:
            if stored is None:
                raise EvidenceBlocked('segment_ledger_missing')
            state = stored.ledger
            if as_of <= state.as_of:
                raise EvidenceBlocked('observation_not_advanced')
            if number(anchor['allocation']['allocated_amount'],'opening_allocation')+sum((change.delta for change in audit['changes']),Decimal(0))!=allocation.amount:
                raise EvidenceBlocked('allocation_event_history_amount_mismatch')
            known={event.event_id for event in state.incomes}
            late=[event for event in events if event.event_id not in known and event.occurred_at<=state.as_of]
            if late or opening_revision is not None:
                existing_flows={flow.event_id for flow in state.cash_flows}
                if any('allocation:'+change.event_id not in existing_flows for change in audit['changes']):
                    raise EvidenceBlocked('reconciliation_allocation_changes_unsettled')
                captures=[]
                for archived in await connection.fetch('SELECT as_of,evidence_sha256,payload FROM trading.hrs_equity_evidence WHERE account_id=$1 AND segment_id=$2 AND as_of<=$3 ORDER BY available_at DESC',self.account,segment,as_of):
                    payload=json.loads(archived['payload']) if isinstance(archived['payload'],str) else archived['payload']
                    if 'capture' in payload and payload.get('result',{}).get('ledger_version') is not None:
                        captures.append(dict(as_of=archived['as_of'],capture=payload['capture'],evidence_sha256=archived['evidence_sha256']))
                source=ReconciliationSource(self.account,segment,state.inception,effective_opening_cash,
                    LedgerObservation(as_of,segment,events,(),marks,as_of,snapshot['captured_at'],True,True),
                    state.cash_flows,await effective_checkpoints(connection,self.account,segment),tuple(captures),
                    tuple(fills),tuple(orders),self._now(),canonical(evidence)[1],opening_positions=self._opening_marks(anchor),opening_inventory=self._anchor_inventory,opening_revision=opening_revision)
                rebuilt=rebuild_history(stored,source,allow_inception_segment=self.allow_inception)
                stored=await activate_reconciliation(connection,self.account,stored,rebuilt)
                gate=rebuilt.gate
                receipt_sha=canonical(rebuilt.receipt)[1]
                result=dict(ready=gate.ready,allowed=gate.allow_new_risk,reasons=list(gate.reasons),
                    window_label=gate.window_label,qualified_30_day=gate.qualified_30_day,ledger_version=stored.version,
                    reconciliation_receipt_sha256=receipt_sha)
                evidence['result']=result
                evidence['reconciliation_receipt_sha256']=receipt_sha
                return await self._projection(connection,revision,result,evidence)
            events_flow=self._allocation_flows(state,audit['changes'],events,fills,orders,as_of)
            if state.cash_allocation+sum((flow.amount for flow in events_flow),Decimal(0))!=effective_opening_cash+sum((change.delta for change in audit['changes']),Decimal(0)):
                raise EvidenceBlocked('allocation_event_history_unrecorded_flow')
            checkpoints = list(await effective_checkpoints(connection,self.account,segment))
            midnight = datetime.combine(state.as_of.astimezone(SHANGHAI).date()+timedelta(days=1),time.min,SHANGHAI)
            while midnight < as_of:
                boundary = historical_boundary_observation(state,midnight,self._now(),fills,orders,events,coverage_complete=True,opening_inventory=self._anchor_inventory,historical_bars=snapshot.get('historical_mark_bars'),account=self.account)
                boundary = LedgerObservation(boundary.as_of,segment,boundary.incomes,
                    tuple(flow for flow in events_flow if flow.occurred_at <= midnight),boundary.positions,midnight,midnight,True,True)
                state = reconcile_ledger(state,boundary)
                history = GateHistory(tuple(checkpoints+[state.checkpoint]),self._history_start(stored.ledger.inception,midnight),midnight,
                                      self._history_complete(checkpoints+[state.checkpoint],fills,orders,state.inception))
                boundary_gate = evaluate_loss_gate(stored.gate,state.checkpoint,history,allow_inception_segment=self.allow_inception)
                stored = await session.commit(state,boundary_gate.state,expected_version=stored.version,
                                              checkpoint_id=f'{segment}:{int(midnight.timestamp()*1000)}')
                checkpoints.append(state.checkpoint)
                evidence['reconstructed_boundaries'].append(dict(effective_at=midnight,available_at=self._now(),source='complete_fill_inventory_and_actual_mark_bar' if boundary.positions else 'complete_flat_fill_income_path'))
                midnight += timedelta(days=1)
            obs = LedgerObservation(as_of,segment,events,events_flow,marks,as_of,snapshot['captured_at'],True,True)
            state = reconcile_ledger(state,obs)
            samples = checkpoints+[state.checkpoint]
            # Exact rolling endpoint may be reconstructed from the genuine initial
            # flat segment, with all already-certified cash flows preserved.
            rolling_start = as_of-timedelta(days=30)
            if rolling_start >= state.inception and not any(cp.as_of==rolling_start for cp in samples):
                base_obs=LedgerObservation(state.inception,segment,(),(),self._opening_marks(anchor),state.inception,state.inception,True,True)
                base=initialize_ledger(segment,effective_opening_cash,base_obs)
                try:
                    historical=historical_boundary_observation(base,rolling_start,self._now(),fills,orders,events,coverage_complete=True,opening_inventory=self._anchor_inventory,historical_bars=snapshot.get('historical_mark_bars'),account=self.account)
                    historical=LedgerObservation(rolling_start,segment,historical.incomes,
                        tuple(flow for flow in state.cash_flows if flow.occurred_at<=rolling_start),historical.positions,rolling_start,rolling_start,True,True)
                    samples.append(reconcile_ledger(base,historical).checkpoint)
                    samples.sort(key=lambda cp:cp.as_of)
                    evidence['reconstructed_boundaries'].append(dict(effective_at=rolling_start,available_at=self._now(),source='complete_fill_inventory_and_actual_mark_bar' if historical.positions else 'complete_flat_fill_income_path'))
                except EvidenceBlocked:
                    pass  # Full qualification stays false; no historical mark is invented.
            history = GateHistory(tuple(samples),self._history_start(state.inception,as_of),as_of,self._history_complete(samples,fills,orders,state.inception))
            gate = evaluate_loss_gate(stored.gate,state.checkpoint,history,allow_inception_segment=self.allow_inception)
            stored=await session.commit(state,gate.state,expected_version=stored.version,checkpoint_id=f'{segment}:{end_ms}')
        result=dict(ready=gate.ready,allowed=gate.allow_new_risk,reasons=list(gate.reasons),
                    window_label=gate.window_label,qualified_30_day=gate.qualified_30_day,ledger_version=stored.version)
        evidence['result']=result
        return await self._projection(connection,revision,result,evidence)

    async def _deny(self, reason):
        now=self._now()
        result=dict(ready=False,allowed=False,reasons=[reason])
        evidence=dict(account_id=self.account,segment_id=None,as_of=now,available_at=now,result=result)
        # Denial must still publish when the income guard itself is unresolved.
        async with self.db.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,7183))',self.account)
                return await self._projection(connection,0,result,evidence)

    async def refresh(self):
        async with self._refresh_lock:
            try:
                if self.db.pool is None:
                    await self.db.connect()
                bootstrap=await self.db.fetch_one('SELECT * FROM trading.hrs_equity_segment WHERE account_id=$1',self.account)
                snapshot=await self._capture()
                end_ms=int(snapshot['captured_at'].timestamp()*1000)
                if bootstrap:
                    saved=json.loads(bootstrap['opening_evidence']) if isinstance(bootstrap['opening_evidence'],str) else bootstrap['opening_evidence']
                    start_ms=saved.get('source_start_ms',int(bootstrap['inception'].timestamp()*1000))
                else:
                    period=datetime.combine(snapshot['captured_at'].astimezone(SHANGHAI).date().replace(day=1),time.min,SHANGHAI)
                    start_ms=int(period.timestamp()*1000)
                    symbols=[row['symbol'] for row in snapshot['inventory'] if number(row.get('positionAmt'),'position_quantity')!=0]
                    if symbols:
                        rows=await self.db.fetch_all("SELECT executed_at FROM trading.trade_records WHERE account_id=$1 AND symbol=ANY($2::text[]) AND order_type IN ('LIMIT','MARKET') AND realized_pnl IS NULL AND status NOT IN ('CANCELED','EXPIRED','CLOSED')",self.account,symbols)
                        for row in rows:
                            at=row['executed_at']
                            if at is None:
                                raise EvidenceBlocked('opening_entry_bound_missing')
                            at=at.replace(tzinfo=SHANGHAI) if at.tzinfo is None else at
                            start_ms=min(start_ms,int(at.timestamp()*1000))
                collection=await self._collect(start_ms,end_ms,snapshot)
                await self._authenticate_order_owners(snapshot,start_ms,end_ms)
                await self._funding_publication_evidence(snapshot,start_ms,end_ms)
                await self._historical_marks(bootstrap,snapshot)
                async with self._locked() as connection:
                    result=await self._commit(connection,bootstrap,snapshot,collection,start_ms,end_ms)
                if self.recorder is not None:
                    await self.recorder.record(self.recorder.run_id,'equity_gate',result,as_of=result['as_of'],available_at=self._now())
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason=str(exc) if isinstance(exc,(LedgerNotReady,RepositoryConflict)) else type(exc).__name__
                log.warning('hrs_equity_not_ready',reason=reason)
                try:
                    return await self._deny(reason)
                except Exception as publication_error:
                    # Missing/stale projection already denies entry. Never return
                    # a cached authorization after failure to publish denial.
                    log.error('hrs_equity_denial_not_persisted',error_type=type(publication_error).__name__)
                    return dict(ready=False,allowed=False,reasons=[reason,'projection_publication_failed'],
                                as_of=None,valid_until=None,policy_revision=0,evidence_sha256=None)
