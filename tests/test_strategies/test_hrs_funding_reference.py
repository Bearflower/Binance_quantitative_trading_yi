"""Forecast references retain every original source receipt, without copying prefixes."""
import copy
import json
from datetime import datetime,timedelta,timezone

import pytest

from strategies.hrs.equity_evidence import EvidenceBlocked,canonical

T=datetime(2026,11,18,tzinfo=timezone.utc)


def source(at,*,received=None,account='test',forecast=None):
    forecast=forecast or dict(symbol='WLDUSDT',source_time=T,received_at=received or T,
        next_funding_time=T+timedelta(hours=1),source='authenticated_premium_index')
    payload=dict(forecasts=[forecast])
    encoded,digest=canonical(payload)
    return dict(account_id=account,evidence_sha256=digest,available_at=at,payload=json.loads(encoded))


def build(rows,cutoff=T+timedelta(seconds=30)):
    from strategies.hrs.funding_reference import build_forecasts_ref
    return build_forecasts_ref('test',cutoff,rows,expected_count=len(rows))


def test_reference_retains_same_body_different_receipt_clocks_and_constant_size():
    from strategies.hrs.funding_reference import resolve_forecasts_ref
    rows=[source(T),source(T+timedelta(seconds=1),received=T+timedelta(seconds=1))]
    ref,forecasts=build(rows)
    assert ref['table']=='trading.hrs_funding_observation' and ref['row_count']==ref['forecast_count']==2
    assert len(forecasts)==2 and forecasts[0]['received_at']!=forecasts[1]['received_at']
    assert resolve_forecasts_ref(ref,rows,account_id='test',expected_count=2)==forecasts
    many=[source(T+timedelta(seconds=i),received=T+timedelta(seconds=i)) for i in range(1000)]
    larger,_=build(many,T+timedelta(seconds=1000))
    assert len(canonical(larger)[0])<len(canonical(ref)[0])+30
    assert 'forecasts' not in larger


@pytest.mark.parametrize('damage',['missing','extra','payload','identity','account','future','count','digest','first','version','table','contract','duplicate','order'])
def test_ref_closure_rejects_missing_extra_conflicting_and_unknown_sources(damage):
    from strategies.hrs.funding_reference import resolve_forecasts_ref
    rows=[source(T),source(T+timedelta(seconds=1),received=T+timedelta(seconds=1))]
    ref,_=build(rows)
    expected=2
    if damage=='missing':rows.pop()
    elif damage=='extra':rows.append(source(T+timedelta(seconds=2),received=T+timedelta(seconds=2)));expected=3
    elif damage=='payload':rows[0]['payload']['forecasts'][0]['symbol']='OTHER'
    elif damage=='identity':rows[0]['evidence_sha256']='0'*64
    elif damage=='account':rows[0]['account_id']='another'
    elif damage=='future':rows[0]['available_at']=T+timedelta(minutes=1)
    elif damage=='count':expected=3
    elif damage=='digest':ref['ordered_sha256']='0'*64
    elif damage=='first':ref['first_key']=ref['last_key']
    elif damage=='version':ref['version']='future-v2'
    elif damage=='table':ref['table']='public.some_other_table'
    elif damage=='contract':ref['canonical_contract']='future'
    elif damage=='duplicate':rows=[rows[0],rows[0]]
    elif damage=='order':rows.reverse()
    with pytest.raises(EvidenceBlocked):resolve_forecasts_ref(ref,rows,account_id='test',expected_count=expected)


def test_raw_parent_hash_is_checked_before_expansion_and_original_is_unchanged():
    from strategies.hrs.funding_reference import resolve_evidence_forecasts
    rows=[source(T)]
    ref,forecasts=build(rows)
    payload=dict(account_id='test',available_at=T+timedelta(seconds=30),capture=dict(funding_publication=dict(forecasts_ref=ref)))
    encoded,digest=canonical(payload)
    parent=dict(account_id='test',evidence_sha256=digest,payload=json.loads(encoded))
    original=copy.deepcopy(parent)
    assert resolve_evidence_forecasts(parent,rows,account_id='test',expected_count=1)==forecasts
    assert parent==original and canonical(parent['payload'])[1]==digest
    parent['payload']['capture']['funding_publication']['forecasts_ref']['row_count']=0
    with pytest.raises(EvidenceBlocked,match='parent_hash'):resolve_evidence_forecasts(parent,rows,account_id='test',expected_count=1)


def test_legacy_inline_hash_and_clocks_survive_and_missing_reference_is_not_empty():
    from strategies.hrs.funding_reference import resolve_evidence_forecasts
    forecasts=[source(T)['payload']['forecasts'][0]]
    payload=dict(account_id='test',capture=dict(funding_publication=dict(forecasts=forecasts)))
    encoded,digest=canonical(payload)
    parent=dict(account_id='test',payload=json.loads(encoded),evidence_sha256=digest)
    resolved=resolve_evidence_forecasts(parent,[],account_id='test',expected_count=0)
    assert resolved==forecasts
    resolved[0]['symbol']='MUTATED_DERIVED_VIEW'
    assert parent['payload']['capture']['funding_publication']['forecasts']==forecasts
    assert canonical(parent['payload'])[1]==digest
    payload['capture']['funding_publication']={}
    encoded,digest=canonical(payload)
    parent.update(payload=json.loads(encoded),evidence_sha256=digest)
    with pytest.raises(EvidenceBlocked,match='missing'):resolve_evidence_forecasts(parent,[],account_id='test',expected_count=0)


def test_integrity_only_mode_streams_receipts_without_collecting_forecast_lists():
    from strategies.hrs.funding_reference import resolve_evidence_forecasts
    rows=[source(T)]
    ref,_=build(rows)
    payload=dict(account_id='test',available_at=T+timedelta(seconds=30),capture=dict(funding_publication=dict(forecasts_ref=ref)))
    encoded,digest=canonical(payload)
    parent=dict(account_id='test',payload=json.loads(encoded),evidence_sha256=digest)
    summary=resolve_evidence_forecasts(parent,(row for row in rows),account_id='test',expected_count=1,collect=False)
    assert summary==dict(row_count=1,forecast_count=1,ordered_sha256=ref['ordered_sha256'])


def test_reference_cannot_claim_source_available_after_original_parent_receipt():
    from strategies.hrs.funding_reference import resolve_evidence_forecasts
    rows=[source(T)]
    ref,_=build(rows)
    payload=dict(account_id='test',available_at=T,capture=dict(funding_publication=dict(forecasts_ref=ref)))
    encoded,digest=canonical(payload)
    parent=dict(account_id='test',payload=json.loads(encoded),evidence_sha256=digest)
    with pytest.raises(EvidenceBlocked,match='parent_availability'):
        resolve_evidence_forecasts(parent,rows,account_id='test',expected_count=1)


def test_batch_prefix_verification_reads_each_source_once_for_many_parent_cutoffs():
    from strategies.hrs.funding_reference import verify_forecasts_prefixes,ForecastPrefixState
    rows=[source(T+timedelta(seconds=i),received=T+timedelta(seconds=i)) for i in range(30)]
    refs=[build(rows[:i],T+timedelta(seconds=i-1))[0] for i in range(1,31)]
    reads=[]
    def once():
        for row in rows:
            reads.append(row['evidence_sha256']);yield row
    summaries=verify_forecasts_prefixes(list(reversed(refs))+[refs[0]],once(),account_id='test',expected_count=30)
    assert len(reads)==30
    assert summaries['verified_prefix_count']==31 and summaries['source_row_count']==30
    state=ForecastPrefixState('test',collect=False)
    for row in rows:state.add(row)
    assert state.reference(T+timedelta(seconds=29),expected_count=30)==refs[-1]
    assert state.forecasts==[]


@pytest.mark.parametrize('damage',['prefix_digest','prefix_count','missing','extra','late_cutoff','unsorted','last_key','null'])
def test_incremental_batch_checks_every_prefix_not_only_latest(damage):
    from strategies.hrs.funding_reference import verify_forecasts_prefixes
    rows=[source(T+timedelta(seconds=i),received=T+timedelta(seconds=i)) for i in range(3)]
    refs=[build(rows[:i],T+timedelta(seconds=i-1))[0] for i in range(1,4)]
    if damage=='prefix_digest':refs[0]['ordered_sha256']='0'*64
    elif damage=='prefix_count':refs[0]['row_count']=0
    elif damage=='last_key':refs[0]['last_key']=refs[-1]['last_key']
    elif damage=='missing':rows.pop(0)
    elif damage=='extra':rows.append(source(T+timedelta(seconds=3),received=T+timedelta(seconds=3)))
    elif damage=='late_cutoff':refs[-1]['available_through']=(T+timedelta(seconds=1)).isoformat()
    elif damage=='unsorted':rows.reverse()
    elif damage=='null':rows.append(None)
    with pytest.raises(EvidenceBlocked):verify_forecasts_prefixes(refs,iter(rows),account_id='test',expected_count=3)
