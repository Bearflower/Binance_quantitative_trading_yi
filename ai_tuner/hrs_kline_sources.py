"""Offline closure of a repair receipt against its original public responses."""
from datetime import datetime,timezone

from strategies.hrs.decision_capture import canonical_payload
from strategies.hrs.kline_repair import (HOUR_MS,MAX_HISTORY_HOURS,MAX_REQUESTS,
    _normalized,_decode_response,complete_four_hour_bars,validate_repair_response_scope,
    FOUR_HOUR_AGGREGATION_CONTRACT)
from strategies.hrs.core_decision import validate_klines
from .replay.models import DataBlocked


def _require(ok,reason):
    if not ok:raise DataBlocked(['hrs_kline_repair_'+reason])


def _time(value):
    if isinstance(value,str):value=datetime.fromisoformat(value.replace('Z','+00:00'))
    _require(isinstance(value,datetime) and value.tzinfo is not None and value.utcoffset() is not None,'time_invalid')
    return value.astimezone(timezone.utc)


def _checked(row):
    payload=row['payload']
    _require(canonical_payload(payload)[1]==row['payload_sha256'],'raw_payload_hash_mismatch')
    _require(_time(row['event_time'])<=_time(row['available_at']),'raw_clock_invalid')
    return payload


def verify_kline_repair(repair,analysis,public_rows,config):
    """Rebuild the full original span before applying ordinary scoring windows.

    Uses the same normalization and four-hour aggregation as the live repair.
    Inputs and public receipts remain untouched; no missing body is inferred
    from a receipt hash or a later price. Public rows can be an indexed stream.
    """
    try:
        payload=_checked(repair);scored=_checked(analysis);receipt=payload['receipt']
        _require(repair['kind']=='kline_repair' and analysis['kind'] in ('analysis','lv_rm_analysis')
            and repair['run_id']==analysis['run_id'] and repair['symbol']==analysis['symbol']==receipt['symbol'],'identity_mismatch')
        _require(receipt['version']=='hrs_kline_repair_v1' and receipt['status']=='complete'
            and receipt['hash_algorithm']=='decision_capture.canonical_payload/sha256'
            and receipt.get('aggregation_contract')==FOUR_HOUR_AGGREGATION_CONTRACT,'version_or_status_invalid')
        decision=_time(receipt['decision_at']);started=_time(receipt['started_at']);available=_time(receipt['available_at'])
        _require(decision<=available and started<=available<=_time(repair['available_at'])<=_time(analysis['available_at']),
            'not_available_at_analysis')
        originals=(payload['input_klines_1h'],payload['input_klines_4h'])
        for label,rows in zip(('1h','4h'),originals):
            _require(isinstance(rows,list) and len(rows)==receipt['input_'+label+'_count']
                and canonical_payload(rows)[1]==receipt['input_'+label+'_sha256'],'input_hash_mismatch')
        one,dropped=_normalized(originals[0],HOUR_MS,decision,{})
        four,_=_normalized(originals[1],4*HOUR_MS,decision,{})
        kline=config.get('kline',{});keep=int(kline.get('keep_count',168));four_count=int(kline.get('synthetic_4h_count',50))
        minimum=int(kline.get('min_klines_for_analysis',24))
        _require(keep>0 and four_count>0 and minimum>0,'config_invalid')
        last=int(decision.timestamp()*1000)//HOUR_MS*HOUR_MS-HOUR_MS
        beginning=min([*one,*four]) if one or four else last-(max(keep,four_count*4)-1)*HOUR_MS
        beginning=beginning//(4*HOUR_MS)*(4*HOUR_MS)
        count=(last-beginning)//HOUR_MS+1
        _require(0<count<=MAX_HISTORY_HOURS and receipt['full_start_time']==beginning
            and receipt['full_end_time']==last and receipt['full_count']==count
            and receipt['dropped_unclosed_count']==dropped,'full_span_mismatch')
        requests=receipt['requests'];_require(isinstance(requests,list) and len(requests)<=MAX_REQUESTS,'request_budget_exceeded')
        # Keep only a bounded set of request matches; never materialize unrelated
        # public responses from a whole monthly archive.
        matches=[[] for _ in requests]
        for row in public_rows:
            body=_checked(row)
            if row['kind']!='public_market_response' or row['run_id']!=repair['run_id']:continue
            if body.get('path')!='/fapi/v1/klines':continue
            for index,request in enumerate(requests):
                if (body.get('params')==request['params'] and body['path']==request['path']
                        and _time(request['started_at'])<=_time(row['available_at'])<=_time(request['available_at'])
                        and canonical_payload(body['response'])[1]==request['response_sha256']):
                    _require(not matches[index],'duplicate_source_receipt')
                    matches[index].append(row)
        used=[]
        for request,matching in zip(requests,matches):
            _require(len(matching)==1,'request_source_missing')
            row=matching[0];params=request['params'];request_at=_time(request['available_at'])
            _require(started<=_time(request['started_at'])<=request_at<=available,'request_clock_invalid')
            missing=[t for t in range(beginning,last+1,HOUR_MS) if t not in one]
            begin=params['startTime'];end=params['endTime']-HOUR_MS+1
            _require(request['path']=='/fapi/v1/klines' and params['symbol']==receipt['symbol']
                and params['interval']=='1h' and missing and begin==missing[0]
                and beginning<=begin<=end<=last and begin%HOUR_MS==end%HOUR_MS==0
                and 0<params['limit']<=100 and params['limit']==(end-begin)//HOUR_MS+1
                and all(t in missing for t in range(begin,end+1,HOUR_MS)),'request_scope_invalid')
            raw=row['payload']['response'];decoded=_decode_response(raw,{})
            _require(len(decoded)==request['received_count'],'response_count_mismatch')
            validate_repair_response_scope(decoded,begin,end,{})
            received,_=_normalized(decoded,HOUR_MS,decision,{})
            _require(set(received)==set(range(begin,end+1,HOUR_MS)),'response_scope_incomplete')
            one.update(received);used.append(row['evidence_id'])
        _require(all(t in one for t in range(beginning,last+1,HOUR_MS)),'full_span_incomplete')
        full=[one[t] for t in range(beginning,last+1,HOUR_MS)];derived=complete_four_hour_bars(full)
        validate_klines(full,decision,minimum=minimum)
        validate_klines(derived,decision,interval_ms=4*HOUR_MS,minimum=1)
        outputs=(full[-keep:],derived[-four_count:])
        for label,rows in zip(('1h','4h'),(full,derived)):
            _require(canonical_payload(rows)[1]==receipt['full_'+label+'_sha256'],'full_hash_mismatch')
        _require(len(derived)==receipt['full_4h_count'],'full_count_mismatch')
        for label,rows in zip(('1h','4h'),outputs):
            _require(len(rows)==receipt['output_'+label+'_count']
                and canonical_payload(rows)[1]==receipt['output_'+label+'_sha256']
                and canonical_payload(scored['klines_'+label])[1]==receipt['output_'+label+'_sha256'],'output_hash_mismatch')
        return dict(full_count=count,output_1h_count=len(outputs[0]),output_4h_count=len(outputs[1]),request_source_ids=used)
    except DataBlocked:raise
    except Exception as exc:raise DataBlocked(['hrs_kline_repair_unproven:'+type(exc).__name__]) from exc
