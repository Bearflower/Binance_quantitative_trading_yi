"""Repair receipts retain the actual source bodies and later availability."""
import asyncio
import copy
from datetime import datetime,timedelta,timezone

import pytest

from ai_tuner.replay.models import DataBlocked
from strategies.hrs.decision_capture import canonical_payload
from tests.test_ai_tuner_mvp.test_hrs_evidence_export import envelope


def fixture():
    from strategies.hrs.kline_repair import prepare_klines
    start=datetime(2026,11,17,tzinfo=timezone.utc)
    bars=[dict(open_time=int((start+timedelta(hours=i)).timestamp()*1000),
        open='100',high='102',low='99',close='101',volume='10') for i in range(8)]
    missing=bars[3];original=[row for i,row in enumerate(bars) if i!=3];public=[]
    body=[[missing['open_time'],'100','102','99','101','10',missing['open_time']+3599999,'1000',1,'5','500','0']]
    class Client:
        async def _request(self,method,path,params,signed):
            now=datetime.now(timezone.utc)
            public.append(envelope('run','public_market_response',dict(path=path,params=params,response=copy.deepcopy(body)),now,'A'))
            return copy.deepcopy(body)
    config={'kline':dict(keep_count=4,synthetic_4h_count=2,min_klines_for_analysis=1)}
    prepared=asyncio.run(prepare_klines(Client(),'A',original,[],decision_at=start+timedelta(hours=8),
        keep_count=4,four_hour_count=2,minimum=1,page_limit=100))
    at=prepared.available_at+timedelta(microseconds=1)
    repair=envelope('run','kline_repair',dict(receipt=prepared.receipt,input_klines_1h=original,input_klines_4h=[]),at,'A')
    analysis=envelope('run','analysis',dict(klines_1h=prepared.klines_1h,klines_4h=prepared.klines_4h),at+timedelta(microseconds=1),'A')
    return repair,analysis,public,config


def test_repair_receipt_reconstructs_original_full_span_before_final_window():
    from ai_tuner.hrs_kline_sources import verify_kline_repair
    repair,analysis,public,config=fixture()
    summary=verify_kline_repair(repair,analysis,public,config)
    assert summary['full_count']==8 and summary['output_1h_count']==4
    assert summary['request_source_ids']==[public[0]['evidence_id']]
    assert analysis['event_time']>repair['payload']['receipt']['available_at']


@pytest.mark.parametrize('damage',['missing','params','response','input','output','unavailable','full_hash','version','aggregation_missing','aggregation_unknown'])
def test_missing_changed_or_future_repair_facts_fail_closed(damage):
    from ai_tuner.hrs_kline_sources import verify_kline_repair
    repair,analysis,public,config=fixture()
    if damage=='missing':public=[]
    elif damage=='params':public[0]['payload']['params']['symbol']='B'
    elif damage=='response':public[0]['payload']['response'][0][1]='999'
    elif damage=='input':repair['payload']['input_klines_1h'][0]['open']='999'
    elif damage=='output':analysis['payload']['klines_1h'][-1]['close']='99'
    elif damage=='unavailable':repair['payload']['receipt']['available_at']=datetime.now(timezone.utc)+timedelta(hours=1)
    elif damage=='full_hash':repair['payload']['receipt']['full_1h_sha256']='0'*64
    elif damage=='version':repair['payload']['receipt']['version']='unknown'
    elif damage=='aggregation_missing':repair['payload']['receipt'].pop('aggregation_contract')
    elif damage=='aggregation_unknown':repair['payload']['receipt']['aggregation_contract']='unknown'
    # An internally consistent new envelope must not bypass the source closure.
    for row in [repair,analysis,*public]:row['payload_sha256']=canonical_payload(row['payload'])[1]
    with pytest.raises(DataBlocked,match='kline_repair'):
        verify_kline_repair(repair,analysis,public,config)


def test_runtime_cannot_strip_repair_reference_from_derived_inputs():
    from types import SimpleNamespace
    from ai_tuner.hrs_evidence_export import verify_runtime_kline_repair
    repair,analysis,public,config=fixture()
    portal=SimpleNamespace(_hrs_archive_sources_cache=dict(decision_evidence=[repair,analysis,*public]),
        window_start=datetime(2026,11,17,tzinfo=timezone.utc),hrs_replay_state=lambda at:dict(config=config))
    raw=dict(symbol='A',evidence_ids=[analysis['evidence_id']],decision_at=analysis['event_time'])
    complete=dict(raw,kline_repair_evidence_id=repair['evidence_id'],kline_repair_payload_sha256=repair['payload_sha256'],
        kline_repair_source_ids=[row['evidence_id'] for row in public])
    verify_runtime_kline_repair(portal,complete,datetime.now(timezone.utc))
    with pytest.raises(DataBlocked,match='kline_repair'):
        verify_runtime_kline_repair(portal,raw,datetime.now(timezone.utc))


def test_repair_get_must_reject_future_extra_before_normalization_drops_it():
    from ai_tuner.hrs_kline_sources import verify_kline_repair
    repair,analysis,public,config=fixture()
    extra=copy.deepcopy(public[0]['payload']['response'][0])
    extra[0]+=365*86400*1000;extra[6]+=365*86400*1000
    public[0]['payload']['response'].append(extra)
    request=repair['payload']['receipt']['requests'][0]
    request['response_sha256']=canonical_payload(public[0]['payload']['response'])[1]
    request['received_count']=2
    for row in [repair,*public]:row['payload_sha256']=canonical_payload(row['payload'])[1]
    with pytest.raises(DataBlocked,match='kline_repair'):
        verify_kline_repair(repair,analysis,public,config)
