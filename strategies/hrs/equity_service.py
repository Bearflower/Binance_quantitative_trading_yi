"""Prospective HRS equity collection; missing source facts never authorize entries.

The service owns no exchange credentials or API lifetime. ``refresh`` first uses
ExecutionFactExporter/PostgresExecutionFactsSink/collect_window, with a fresh
exporter so completed old windows cannot hide late events. It then holds exactly
one lease: income session guard -> repeatable-read transaction -> account policy
lock -> loss lock -> checkpoint/evidence/projection writes. API requests and the
optional recorder happen outside that transaction. The caller owns task creation.

A segment starts once from current DB allocation and genuinely flat HRS inventory,
with no unresolved HRS entry. No startup, deposit or allocation change reseeds it.
Daily and rolling boundary NAVs are reconstructed only when the complete fill path
proves HRS flat there; nonflat historical marks are deliberately not synthesized.
Derived attribution is not silently promoted to human review; unsupported funding
or missing exact orders remain a concrete readiness barrier.

Environment: TRADE_ACCOUNT_ID; HRS_EQUITY_ALLOW_INCEPTION=true explicitly permits
labelled inception protection; HRS_EQUITY_REFRESH_SECONDS defaults to 30. The
projection always expires by source as_of+120s, regardless of collection duration.
"""
import asyncio
import copy
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
from shared.accounting.report_reader import read_monthly_economic_report
from shared.execution_facts_sink import PostgresExecutionFactsSink

from .equity_evidence import (
    EvidenceBlocked, allocation_evidence, attributed_incomes, canonical,
    flat_boundary_observation, number, order_owners, owned_fill_positions, prove_positions,
)
from .loss_gate import GateHistory, SHANGHAI, evaluate_loss_gate
from .loss_gate_repository import LossGateSession, RepositoryConflict
from .risk_ledger import CashFlow, LedgerNotReady, LedgerObservation, initialize_ledger, reconcile_ledger

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
    def __init__(self, connection):
        self.connection = connection

    async def fetch_all(self, query, *args):
        return await self.connection.fetch(query, *args)


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

    def _now(self):
        value = self.clock()
        if value.tzinfo is None:
            raise EvidenceBlocked('collector_clock_naive')
        return datetime.fromtimestamp(int(value.timestamp()*1000)/1000, timezone.utc)

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
        captured = self._now()
        if not timedelta(0) <= captured-started <= timedelta(seconds=120):
            raise EvidenceBlocked('inventory_capture_stale')
        return copy.deepcopy(dict(started_at=started, captured_at=captured, inventory=inventory,
                    independent=independent, open_orders=open_orders, local=local, marks=marks))

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
        return result

    @asynccontextmanager
    async def _locked(self):
        async with self.db.pool.acquire() as connection:
            async with income_account_lock(connection, self.account):
                async with connection.transaction(isolation='repeatable_read'):
                    await connection.execute("SELECT set_config('lock_timeout','5000ms',true)")
                    await connection.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,7183))', self.account)
                    key = int.from_bytes(hashlib.sha256(f'hrs-loss-gate:{self.account}'.encode()).digest()[:8], 'big', signed=True)
                    await connection.execute('SELECT pg_advisory_xact_lock($1)', key)
                    await require_income_integrity(connection, self.account)
                    yield connection

    async def _read_source(self, connection, start_ms, end_ms, snapshot):
        start = datetime.fromtimestamp(start_ms/1000, timezone.utc)
        end = datetime.fromtimestamp(end_ms/1000, timezone.utc)
        report = await read_monthly_economic_report(_SnapshotDB(connection), self.account, start_ms, end_ms + 1,
                                                   include_reviewed_overrides=True)
        nonzero_symbols = [row['symbol'] for row in snapshot['inventory'] if isinstance(row, dict)
                           and number(row.get('positionAmt'), 'position_quantity_missing') != 0]
        fills = [dict(row) for row in await connection.fetch('''SELECT * FROM trading.execution_fact_fill
            WHERE account_id=$1 AND trade_time_utc <= $3
            AND (trade_time_utc >= $2 OR symbol=ANY($4::text[]))''', self.account, start, end, nonzero_symbols)]
        orders = [dict(row) for row in await connection.fetch('''SELECT account_id,symbol,order_id,strategy,side,status,order_type,
            realized_pnl,executed_at FROM trading.trade_records WHERE account_id=$1 AND order_id IS NOT NULL''', self.account)]
        pending = await connection.fetchval("SELECT count(*) FROM trading.entry_risk_reservation WHERE account_id=$1 AND strategy_id='hrs' AND state<>'released'", self.account)
        return report, fills, orders, pending

    def _entries_clear(self, snapshot, orders, pending, *, bootstrap):
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
        if pending:
            raise EvidenceBlocked('outstanding_hrs_reservation')
        if bootstrap:
            for row in orders:
                if (row['strategy'] == 'hrs' and row.get('order_type') in ('LIMIT', 'MARKET')
                        and row.get('realized_pnl') is None and row.get('status') not in ('CANCELED', 'EXPIRED', 'CLOSED')):
                    raise EvidenceBlocked('bootstrap_open_hrs_identity')

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
        await connection.execute('''INSERT INTO trading.strategy_equity_gate
            (account_id,strategy_id,policy_revision,ready,allowed,as_of,valid_until,reasons,evidence_sha256)
            VALUES($1,'hrs',$2,$3,$4,$5,$6,$7::jsonb,$8)
            ON CONFLICT(account_id,strategy_id) DO UPDATE SET policy_revision=EXCLUDED.policy_revision,
            ready=EXCLUDED.ready,allowed=EXCLUDED.allowed,as_of=EXCLUDED.as_of,valid_until=EXCLUDED.valid_until,
            reasons=EXCLUDED.reasons,evidence_sha256=EXCLUDED.evidence_sha256,updated_at=clock_timestamp()''',
            self.account, revision, result['ready'], result['allowed'], as_of, expiry,
            json.dumps(result['reasons']), digest)
        return dict(result, as_of=as_of, valid_until=expiry, policy_revision=revision, evidence_sha256=digest)

    def _history_complete(self, checkpoints, fills, orders, inception):
        for left, right in zip(checkpoints, checkpoints[1:]):
            if right.as_of - left.as_of <= timedelta(seconds=120):
                continue
            positions = owned_fill_positions(self.account, fills, orders, through=left.as_of, after=inception)
            if any(owner == 'hrs' and qty != 0 for (owner, _), (qty, _) in positions.items()):
                return False
            owners = order_owners(self.account, orders)
            if any(left.as_of < row['trade_time_utc'] <= right.as_of
                   and 'hrs' in owners.get((row['symbol'], str(row['order_id'])), set()) for row in fills):
                return False
        return True

    async def _commit(self, connection, bootstrap, snapshot, collection, start_ms, end_ms):
        as_of = datetime.fromtimestamp(end_ms/1000, timezone.utc)
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
        segment = actual_bootstrap['segment_id'] if actual_bootstrap else f'hrs-{uuid.uuid4().hex}'
        session = LossGateSession(connection, self.account, segment)
        stored = await session.load()
        self._entries_clear(snapshot, orders, pending, bootstrap=actual_bootstrap is None)
        events = attributed_incomes(report, self.account, segment)
        marks = prove_positions(self.account, segment, as_of, snapshot['inventory'], snapshot['independent'],
                                snapshot['local'], fills, orders, snapshot['marks'], bootstrap=actual_bootstrap is None,
                                inception=actual_bootstrap['inception'] if actual_bootstrap else None)
        evidence = dict(account_id=self.account, segment_id=segment, as_of=as_of, available_at=self._now(),
                        capture=snapshot, source_collection=collection, income_report=report,
                        fills=fills, order_owners=orders, allocation=allocation.source, reconstructed_boundaries=[])
        if actual_bootstrap is None:
            if stored is not None or events:
                raise EvidenceBlocked('bootstrap_existing_economics_unreconciled')
            # A fill anywhere in the inventory capture interval defeats a flat
            # point proof even if it roundtripped back to zero before the response.
            owners = order_owners(self.account, orders)
            if any('hrs' in owners.get((row['symbol'],str(row['order_id'])),set()) for row in fills):
                raise EvidenceBlocked('bootstrap_capture_contains_hrs_fills')
            obs = LedgerObservation(as_of, segment, (), (), marks, as_of, snapshot['captured_at'], True, True)
            state = initialize_ledger(segment, allocation.amount, obs)
            await connection.execute('''INSERT INTO trading.hrs_equity_segment
                (account_id,segment_id,inception,opening_cash,opening_allocation_sha256,opening_evidence)
                VALUES($1,$2,$3,$4,$5,$6::jsonb)''', self.account, segment, as_of, allocation.amount,
                allocation.identity, canonical(evidence)[0])
            gate = evaluate_loss_gate(None, state.checkpoint, GateHistory((state.checkpoint,),as_of,as_of,True),
                                      allow_inception_segment=self.allow_inception)
            stored = await session.commit(state,gate.state,expected_version=None,checkpoint_id=f'{segment}:{end_ms}')
        else:
            if stored is None:
                raise EvidenceBlocked('segment_ledger_missing')
            state = stored.ledger
            if as_of <= state.as_of:
                raise EvidenceBlocked('observation_not_advanced')
            if allocation.amount != state.cash_allocation:
                # Flow valuation requires an actual effective-time flat checkpoint;
                # never treat a late administrative allocation as today's income.
                if not state.as_of < allocation.effective_at <= as_of:
                    raise EvidenceBlocked('late_allocation_requires_reconciliation')
                before = flat_boundary_observation(state,allocation.effective_at,self._now(),fills,orders,events,coverage_complete=True)
                before_state = reconcile_ledger(state,before)
                flow = CashFlow(f'allocation:{allocation.identity}',allocation.amount-state.cash_allocation,
                                allocation.effective_at,segment,before_state.nav,canonical(allocation.source)[1])
                events_flow = (flow,)
            else:
                events_flow = ()
            checkpoints = list(await session.checkpoints())
            midnight = datetime.combine(state.as_of.astimezone(SHANGHAI).date()+timedelta(days=1),time.min,SHANGHAI)
            while midnight < as_of:
                boundary = flat_boundary_observation(state,midnight,self._now(),fills,orders,events,coverage_complete=True)
                boundary = LedgerObservation(boundary.as_of,segment,boundary.incomes,
                    tuple(flow for flow in events_flow if flow.occurred_at <= midnight),(),midnight,midnight,True,True)
                state = reconcile_ledger(state,boundary)
                history = GateHistory(tuple(checkpoints+[state.checkpoint]),stored.ledger.inception,midnight,
                                      self._history_complete(checkpoints+[state.checkpoint],fills,orders,state.inception))
                boundary_gate = evaluate_loss_gate(stored.gate,state.checkpoint,history,allow_inception_segment=self.allow_inception)
                stored = await session.commit(state,boundary_gate.state,expected_version=stored.version,
                                              checkpoint_id=f'{segment}:{int(midnight.timestamp()*1000)}')
                checkpoints.append(state.checkpoint)
                evidence['reconstructed_boundaries'].append(dict(effective_at=midnight,available_at=self._now(),source='complete_flat_fill_income_path'))
                midnight += timedelta(days=1)
            obs = LedgerObservation(as_of,segment,events,events_flow,marks,as_of,snapshot['captured_at'],True,True)
            state = reconcile_ledger(state,obs)
            samples = checkpoints+[state.checkpoint]
            # Exact rolling endpoint may be reconstructed from the genuine initial
            # flat segment, with all already-certified cash flows preserved.
            rolling_start = as_of-timedelta(days=30)
            if rolling_start >= state.inception and not any(cp.as_of==rolling_start for cp in samples):
                base_obs=LedgerObservation(state.inception,segment,(),(),(),state.inception,state.inception,True,True)
                base=initialize_ledger(segment,actual_bootstrap['opening_cash'],base_obs)
                try:
                    historical=flat_boundary_observation(base,rolling_start,self._now(),fills,orders,events,coverage_complete=True)
                    historical=LedgerObservation(rolling_start,segment,historical.incomes,
                        tuple(flow for flow in state.cash_flows if flow.occurred_at<=rolling_start),(),rolling_start,rolling_start,True,True)
                    samples.append(reconcile_ledger(base,historical).checkpoint)
                    samples.sort(key=lambda cp:cp.as_of)
                    evidence['reconstructed_boundaries'].append(dict(effective_at=rolling_start,available_at=self._now(),source='complete_flat_fill_income_path'))
                except EvidenceBlocked:
                    pass  # Full qualification stays false; no historical mark is invented.
            history = GateHistory(tuple(samples),state.inception,as_of,self._history_complete(samples,fills,orders,state.inception))
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
                start_ms=int(bootstrap['inception'].timestamp()*1000) if bootstrap else min(int(snapshot['started_at'].timestamp()*1000),end_ms-1)
                collection=await self._collect(start_ms,end_ms,snapshot)
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
