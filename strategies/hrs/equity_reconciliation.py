"""Append-only late-income reconciliation, activated under the equity service lock.

Historical checkpoints and event rows remain immutable. Rebuilt valuations are a
new version with a receipt, never an earlier availability claim. Flat points are
proved from complete signed fills; nonflat points need their real saved capture.
No current mark is borrowed for a historical point.
"""
from dataclasses import asdict,dataclass,replace
from datetime import datetime,timedelta,time
from decimal import Decimal
import json

from .equity_evidence import EvidenceBlocked,canonical,order_key,order_owners,owned_fill_positions,prove_positions,historical_boundary_observation
from .loss_gate import GateHistory,GateEvaluation,SHANGHAI,evaluate_loss_gate
from .loss_gate_repository import StoredRiskState,RepositoryConflict,ledger_to_dict,ledger_from_dict
from .risk_ledger import LedgerObservation,CashFlow,EquityCheckpoint,initialize_ledger,reconcile_ledger,decimal_value


@dataclass(frozen=True)
class ReconciliationSource:
    account:str
    segment_id:str
    inception:datetime
    opening_cash:Decimal
    current:LedgerObservation
    cash_flows:tuple
    checkpoints:tuple
    captures:tuple
    fills:tuple
    orders:tuple
    available_at:datetime
    source_sha256:str
    opening_positions:tuple=()
    opening_inventory:tuple=()
    opening_revision:object=None


@dataclass(frozen=True)
class RebuiltHistory:
    ledger:object
    gate:GateEvaluation
    checkpoints:tuple
    receipt:dict


def _positions_at(source,at):
    positions=owned_fill_positions(source.account,source.fills,source.orders,through=at,after=source.inception,opening_inventory=source.opening_inventory)
    if at==source.inception:
        return source.opening_positions,'certified_opening',None
    if at==source.current.as_of:
        return source.current.positions,'current_capture',source.source_sha256
    if not any(owner=='hrs' and qty!=0 for (owner,_),(qty,_) in positions.items()):
        return (),'complete_flat_fill_path',None
    for archived in source.captures:
        if archived['as_of']!=at:
            continue
        capture=archived['capture']
        try:
            marks=prove_positions(source.account,source.segment_id,at,capture['inventory'],capture['independent'],
                capture['local'],source.fills,source.orders,capture['marks'],bootstrap=False,inception=source.inception,opening_inventory=source.opening_inventory)
        except Exception:
            continue
        return marks,'saved_capture',archived['evidence_sha256']
    bars={}
    for archived in source.captures:
        bars.update(archived['capture'].get('historical_mark_bars',{}))
    if bars:
        initial=LedgerObservation(source.inception,source.segment_id,(),(),source.opening_positions,source.inception,source.inception,True,True)
        base=initialize_ledger(source.segment_id,source.opening_cash,initial)
        try:
            observed=historical_boundary_observation(base,at,source.available_at,source.fills,source.orders,source.current.incomes,
                coverage_complete=True,opening_inventory=source.opening_inventory,historical_bars=bars,account=source.account)
            return observed.positions,'saved_historical_mark_bar',None
        except EvidenceBlocked:
            pass
    raise EvidenceBlocked('reconciliation_historical_mark_missing')


def rebuild_history(previous,source,*,allow_inception_segment=False):
    if (source.segment_id!=previous.ledger.segment_id or source.inception!=previous.ledger.inception
            or source.available_at<source.current.as_of or source.current.as_of<=previous.ledger.as_of
            or source.current.income_complete is not True or source.current.positions_complete is not True):
        raise EvidenceBlocked('reconciliation_source_invalid')
    if source.opening_revision is not None:
        revision=source.opening_revision
        old_credits=revision['old_credits']
        new_credits=revision['new_credits']
        if any(new_credits.get(key)!=value for key,value in old_credits.items()):
            raise EvidenceBlocked('reconciliation_opening_credit_conflict')
        additions=[value for key,value in new_credits.items() if key not in old_credits]
        previous_cash=previous.ledger.cash_allocation-sum((flow.amount for flow in previous.ledger.cash_flows),Decimal(0))
        if (not additions or Decimal(revision['old_opening_cash'])!=previous_cash
                or Decimal(revision['new_opening_cash'])!=source.opening_cash
                or source.opening_cash!=previous_cash+sum((decimal_value(Decimal(credit['amount']),'opening_credit') for credit in additions),Decimal(0))
                or any(datetime.fromisoformat(value['occurred_at'])>source.inception for value in additions)):
            raise EvidenceBlocked('reconciliation_opening_revision_invalid')
    incoming={event.event_id:event for event in source.current.incomes}
    old={event.event_id:event for event in previous.ledger.incomes}
    if any(incoming.get(key)!=event for key,event in old.items()):
        raise EvidenceBlocked('reconciliation_income_fact_conflict')
    added=[event for key,event in incoming.items() if key not in old]
    if any(event.occurred_at<=source.inception for event in added):
        raise EvidenceBlocked('reconciliation_income_predates_anchor')
    flows={}
    for flow in source.cash_flows:
        if flow.event_id in flows and flow!=flows[flow.event_id]:
            raise EvidenceBlocked('reconciliation_cashflow_source_conflict')
        flows[flow.event_id]=flow
    for flow in previous.ledger.cash_flows:
        other=flows.get(flow.event_id)
        if other is None or (other.amount,other.occurred_at,other.segment_id)!=(flow.amount,flow.occurred_at,flow.segment_id):
            raise EvidenceBlocked('reconciliation_cashflow_source_conflict')
    times={cp.as_of for cp in source.checkpoints}|{source.inception,source.current.as_of}|{flow.occurred_at for flow in flows.values()}
    boundary=datetime.combine(source.inception.astimezone(SHANGHAI).date()+timedelta(days=1),time.min,SHANGHAI)
    while boundary<=source.current.as_of:
        times.add(boundary)
        boundary+=timedelta(days=1)
    # Exact flat income points make realized jumps visible in the rebuilt peak.
    for event in incoming.values():
        positions=owned_fill_positions(source.account,source.fills,source.orders,through=event.occurred_at,after=source.inception,opening_inventory=source.opening_inventory)
        if not any(owner=='hrs' and qty!=0 for (owner,_),(qty,_) in positions.items()):
            times.add(event.occurred_at)
    times=sorted(times)
    if times[0]!=source.inception or times[-1]!=source.current.as_of:
        raise EvidenceBlocked('reconciliation_checkpoint_range_invalid')
    opening=LedgerObservation(source.inception,source.segment_id,(),(),source.opening_positions,
                              source.inception,source.inception,True,True)
    state=initialize_ledger(source.segment_id,source.opening_cash,opening)
    samples=[state.checkpoint]
    gate=evaluate_loss_gate(None,state.checkpoint,GateHistory(tuple(samples),source.inception,state.as_of,True),allow_inception_segment=allow_inception_segment)
    valuations=[]
    proof=[]
    for at in times[1:]:
        marks,kind,reference=_positions_at(source,at)
        required=max(source.inception,source.current.as_of-timedelta(days=30))
        left_at=max(state.as_of,required)
        if at>required and at-left_at>timedelta(seconds=120):
            held=owned_fill_positions(source.account,source.fills,source.orders,through=left_at,after=source.inception,opening_inventory=source.opening_inventory)
            if any(owner=='hrs' and qty!=0 for (owner,_),(qty,_) in held.items()):
                raise EvidenceBlocked('reconciliation_mark_coverage_gap')
            owners=order_owners(source.account,source.orders)
            if any(left_at<row['trade_time_utc']<=at and 'hrs' in owners.get(order_key(source.account,row),set()) for row in source.fills):
                raise EvidenceBlocked('reconciliation_mark_coverage_gap')
        incomes=tuple(event for event in incoming.values() if event.occurred_at<=at)
        before=LedgerObservation(at,source.segment_id,incomes,(),marks,at,at,True,True)
        preflow=reconcile_ledger(state,before)
        current_flows=[]
        for flow in flows.values():
            if flow.occurred_at!=at:
                continue
            updated=replace(flow,nav_before=preflow.nav,valuation_evidence='reconciliation:'+source.source_sha256)
            current_flows.append(updated)
            valuations.append(dict(event_id=flow.event_id,amount=str(flow.amount),occurred_at=at,
                                   old_nav_before=str(flow.nav_before),new_nav_before=str(preflow.nav)))
        state=reconcile_ledger(state,replace(before,cash_flows=tuple(current_flows)))
        samples.append(state.checkpoint)
        gate=evaluate_loss_gate(gate.state,state.checkpoint,GateHistory(tuple(samples),max(source.inception,at-timedelta(days=30)),at,True),allow_inception_segment=allow_inception_segment)
        proof.append(dict(as_of=at,nav=str(state.nav),source_kind=kind,evidence_sha256=reference))
    daily=gate.state.daily_latched or (previous.gate.daily_latched and previous.gate.day==gate.state.day)
    rolling=gate.state.rolling_latched or previous.gate.rolling_latched
    state_gate=replace(gate.state,daily_latched=daily,rolling_latched=rolling,
        rolling_latch_scope=previous.gate.rolling_latch_scope if previous.gate.rolling_latched else gate.state.rolling_latch_scope,
        revision=max(gate.state.revision,previous.gate.revision)+1)
    reasons=list(gate.reasons)
    if daily and 'daily_loss_latched' not in reasons:
        reasons.append('daily_loss_latched')
    if rolling and not any(reason.endswith('_drawdown_latched') for reason in reasons):
        reasons.append(f'{state_gate.rolling_latch_scope or "rolling_30_day"}_drawdown_latched')
    gate=replace(gate,state=state_gate,allow_new_risk=gate.ready and not daily and not rolling,reasons=tuple(reasons))
    receipt=dict(kind='late_income_reconciliation_v1',available_at=source.available_at,as_of=source.current.as_of,
        source_evidence_sha256=source.source_sha256,new_income_ids=sorted(event.event_id for event in added),
        before_ledger_sha256=canonical(ledger_to_dict(previous.ledger))[1],after_ledger_sha256=canonical(ledger_to_dict(state))[1],
        before_gate_sha256=canonical(previous.gate.to_dict())[1],after_gate_sha256=canonical(state_gate.to_dict())[1],
        checkpoints=proof,flow_valuations=valuations)
    if source.opening_revision is not None:
        receipt['opening_anchor_revision']=source.opening_revision
        receipt['kind']='opening_anchor_and_late_income_reconciliation_v1'
    return RebuiltHistory(state,gate,tuple(samples),receipt)


def _decode(value):
    return json.loads(value) if isinstance(value,str) else value


def _checkpoint_dict(cp):
    return dict(segment_id=cp.segment_id,as_of=cp.as_of.isoformat(),equity=str(cp.equity),units=str(cp.units),nav=str(cp.nav))


async def effective_checkpoints(connection,account,segment,*,known_at=None):
    rows=await connection.fetch('SELECT as_of,equity,units,nav FROM trading.hrs_risk_equity_checkpoints WHERE account_id=$1 AND segment_id=$2 AND ($3::timestamptz IS NULL OR created_at<=$3) ORDER BY as_of',account,segment,known_at)
    result={row['as_of']:EquityCheckpoint(segment,row['as_of'],row['equity'],row['units'],row['nav']) for row in rows}
    revision=await connection.fetchrow('SELECT checkpoint_series FROM trading.hrs_equity_reconciliation WHERE account_id=$1 AND segment_id=$2 AND ($3::timestamptz IS NULL OR available_at<=$3) ORDER BY after_state_version DESC LIMIT 1',account,segment,known_at)
    if revision:
        for raw in _decode(revision['checkpoint_series']):
            cp=EquityCheckpoint(segment,datetime.fromisoformat(raw['as_of']),Decimal(raw['equity']),Decimal(raw['units']),Decimal(raw['nav']))
            result[cp.as_of]=cp
    return tuple(result[key] for key in sorted(result))


async def activate_reconciliation(connection,account,previous,rebuilt):
    """Caller holds income/policy/loss locks; all state, receipt and proof commit once."""
    row=await connection.fetchrow('SELECT version,segment_id FROM trading.hrs_loss_gate_state WHERE account_id=$1 FOR UPDATE',account)
    if row is None or row['version']!=previous.version or row['segment_id']!=rebuilt.ledger.segment_id:
        raise RepositoryConflict('reconciliation_version_conflict')
    if (rebuilt.ledger.as_of<=previous.ledger.as_of or rebuilt.receipt['available_at']<rebuilt.ledger.as_of
            or (previous.gate.rolling_latched and not rebuilt.gate.state.rolling_latched)
            or (previous.gate.daily_latched and previous.gate.day==rebuilt.gate.state.day and not rebuilt.gate.state.daily_latched)):
        raise EvidenceBlocked('reconciliation_latch_or_time_conflict')
    for field in ('equity','units','nav'):
        decimal_value(getattr(rebuilt.ledger,field),field,positive=True)
    if rebuilt.ledger.equity/rebuilt.ledger.units!=rebuilt.ledger.nav:
        raise EvidenceBlocked('reconciliation_invalid_nav')
    before=dict(ledger=ledger_to_dict(previous.ledger),gate=previous.gate.to_dict())
    after=dict(ledger=ledger_to_dict(rebuilt.ledger),gate=rebuilt.gate.state.to_dict())
    receipt,digest=canonical(rebuilt.receipt)
    for event in rebuilt.ledger.incomes:
        payload=canonical(asdict(event))[0]
        existing=await connection.fetchrow("SELECT payload FROM trading.hrs_risk_ledger_events WHERE account_id=$1 AND event_kind='income' AND event_id=$2",account,event.event_id)
        if existing:
            if _decode(existing['payload'])!=json.loads(payload):
                raise RepositoryConflict('reconciliation_income_fact_conflict')
        else:
            await connection.execute("INSERT INTO trading.hrs_risk_ledger_events(account_id,segment_id,event_kind,event_id,occurred_at,payload) VALUES($1,$2,'income',$3,$4,$5::jsonb)",account,rebuilt.ledger.segment_id,event.event_id,event.occurred_at,payload)
    await connection.execute('''INSERT INTO trading.hrs_equity_reconciliation
        (account_id,segment_id,before_state_version,after_state_version,as_of,available_at,receipt_sha256,before_state,after_state,checkpoint_series,receipt)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10::jsonb,$11::jsonb)''',account,rebuilt.ledger.segment_id,
        previous.version,previous.version+1,rebuilt.ledger.as_of,rebuilt.receipt['available_at'],digest,
        canonical(before)[0],canonical(after)[0],json.dumps([_checkpoint_dict(cp) for cp in rebuilt.checkpoints]),receipt)
    state=rebuilt.ledger
    await connection.execute('''INSERT INTO trading.hrs_risk_equity_checkpoints(account_id,segment_id,checkpoint_id,as_of,equity,units,nav,payload)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb)''',account,state.segment_id,'reconciliation:'+digest,state.as_of,state.equity,state.units,state.nav,canonical(after)[0])
    changed=await connection.execute('''UPDATE trading.hrs_loss_gate_state SET version=version+1,ledger_state=$3::jsonb,gate_state=$4::jsonb,updated_at=clock_timestamp()
        WHERE account_id=$1 AND version=$2''',account,previous.version,canonical(after['ledger'])[0],canonical(after['gate'])[0])
    if changed!='UPDATE 1':
        raise RepositoryConflict('reconciliation_version_conflict')
    return StoredRiskState(previous.version+1,state,rebuilt.gate.state)
