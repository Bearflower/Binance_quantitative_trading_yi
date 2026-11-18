"""Convert retained raw HRS evidence into the existing sealed replay format.

No exchange calls, credentials, production writes, chosen-action replay, or source
attestation. A seal proves local integrity only. Gaps produce a reviewable bundle
without a replay manifest. Source and decision availability are never backdated.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import tempfile
from contextlib import ExitStack
import sqlite3
import shutil

from strategies.hrs.decision_capture import canonical_payload
from strategies.hrs.equity_evidence import canonical as equity_canonical, prove_positions, owned_fill_positions
from strategies.hrs.risk_ledger import LedgerNotReady
from shared.config_loader import compute_config_hash
from .replay.data_portal import DataPortal, sha256_file
from .replay.funding_history import attested_funding_schedule
from .replay.models import DataBlocked, FutureDataAccess
from .replay.snapshot_sealer import seal_snapshot, _has_symlink_ancestor, _repo_destination_is_ignored

VERSION='hrs-raw-evidence-export-v1'
TABLES=('hrs_positions','hrs_blacklist','hrs_active_symbols')
HASHES=('base_config_hash','params_manifest_hash','overlay_hash')
MAX_ROWS=50_000
MAX_BYTES=64*1024*1024
MAX_EXECUTION_BARS=3_000_000
MAX_DERIVED_EVENTS=4_000_000


class _ArchiveFamily:
    """Restartable indexed stream; never a reconstructed whole-window list."""
    _hrs_verified_archive_family=True
    def __init__(self,sources,family):self.sources,self.family=sources,family
    def __len__(self):return self.sources.archive.counts.get(self.family,0)
    def __iter__(self):return self.select()
    def select(self,**filters):
        if filters.get('event_end') is not None:
            filters['event_end']=_time(filters['event_end'])+timedelta(microseconds=1)
        for row in self.sources.archive.iter_records(self.family,**filters):
            if self.family=='decision_evidence':
                row=dict(row);row['payload']=_decoded(row['payload'])
                _require(canonical_payload(row['payload'])[1]==row['payload_sha256'],'decision_payload_hash_mismatch')
                row['event_time']=_time(row['event_time']);row['available_at']=_time(row['available_at'])
                _require(row['event_time']<=row['available_at'],'decision_availability_before_event')
            yield row
    def runs(self):
        for run in self.sources.archive.iter_runs():
            if next(self.select(kind='scan_start',run_id=run),None) is not None:
                yield run,_RunRows(self,run)
    def for_symbol(self,symbol,*,available_before=None,order_by_available=False):
        return self.select(symbol=symbol,available_before=available_before,order_by_available=order_by_available)


class _RunRows:
    def __init__(self,records,run):self.records,self.run=records,run
    def __iter__(self):return self.select()
    def select(self,**filters):return self.records.select(run_id=self.run,**filters)


def _run_rows(rows,*,kind=None,symbol=None,available_before=None):
    if isinstance(rows,_RunRows):return rows.select(kind=kind,symbol=symbol,available_before=available_before)
    return (row for row in rows if (kind is None or row['kind']==kind) and
        (symbol is None or row['symbol']==symbol) and (available_before is None or row['available_at']<=available_before))


class _ArchiveSources:
    def __init__(self,source):
        from .replay.hrs_evidence_archive import EvidenceArchive,ArchiveError
        _require(len(canonical_payload(source)[0].encode())<=MAX_BYTES,'archive_descriptor_byte_limit')
        _require(not any(name in source for name in ('decision_evidence','equity_evidence','equity_checkpoints','funding_observations','hrs_funding_observations','market_bars','cash_flows')),
            'archive_uninventoried_source_family')
        self.metadata=dict(source);descriptor=source['evidence_archive']
        try:
            self.archive=EvidenceArchive.open(descriptor['path'],expected_manifest_sha256=descriptor.get('manifest_sha256'))
            self.forecast_closure=verify_forecast_source_closure(self)
        except ArchiveError as exc:
            if getattr(self,'archive',None) is not None:self.archive.close()
            raise DataBlocked(['evidence_archive_invalid:'+str(exc)]) from exc
        except Exception:
            if getattr(self,'archive',None) is not None:self.archive.close()
            raise
    def __enter__(self):return self
    def __exit__(self,*args):self.archive.close()
    def get(self,key,default=None):
        if key in self.archive.counts:return _ArchiveFamily(self,key)
        return self.metadata.get(key,default)
    def items(self):return self.metadata.items()
    def copy_to(self,staging):
        target=staging/'hrs_evidence_archive';target.mkdir()
        for source in self.archive.files():
            relative=source.relative_to(self.archive.root)
            destination=target/relative;destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,destination)
        descriptor=dict(self.archive.descriptor())
        descriptor['path']=(target/self.archive.inventory_path.relative_to(self.archive.root)).relative_to(staging).as_posix()
        return dict(self.metadata,version='hrs-archive-sources-v2',evidence_archive=descriptor)


def snapshot_evidence_sources(portal):
    """Resolve only manifest-bound archive files; cache belongs to this portal."""
    existing=getattr(portal,'_hrs_archive_sources_cache',None)
    if existing is not None:return existing
    portal._require_dataset('raw_hrs_evidence.json')
    raw=portal._read_json('raw_hrs_evidence.json')
    if not raw.get('evidence_archive'):return raw
    descriptor=dict(raw['evidence_archive']);relative=Path(descriptor['path'])
    _require(not relative.is_absolute() and '..' not in relative.parts,'archive_descriptor_path_escape')
    portal._require_dataset(relative.as_posix())
    descriptor['path']=str(portal.snapshot_dir/relative)
    sources=_ArchiveSources(dict(raw,evidence_archive=descriptor))
    try:
        for path in sources.archive.files():
            portal._require_dataset(path.relative_to(portal.snapshot_dir).as_posix())
    except Exception:
        sources.__exit__(None,None,None);raise
    portal._hrs_archive_sources_cache=sources
    return sources


class _InputIndex:
    def __init__(self,path):
        self.temp=tempfile.TemporaryDirectory(prefix='hrs-input-index-')
        self.db=sqlite3.connect(Path(self.temp.name)/'inputs.sqlite')
        self.db.execute('CREATE TABLE inputs(symbol TEXT, decided TEXT, body TEXT, PRIMARY KEY(symbol,decided))')
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                if not line.strip():continue
                row=json.loads(line)
                try:self.db.execute('INSERT INTO inputs VALUES(?,?,?)',(row['symbol'],_iso(row['decision_at']),line))
                except sqlite3.IntegrityError as exc:raise DataBlocked(['hrs_decision_inputs_duplicate']) from exc
        self.db.commit()
    def __iter__(self):
        for body, in self.db.execute('SELECT body FROM inputs ORDER BY decided,symbol'):yield json.loads(body)
    def get(self,symbol,at):
        result=self.db.execute('SELECT body FROM inputs WHERE symbol=? AND decided=?',(symbol,_iso(at))).fetchone()
        return json.loads(result[0]) if result else None
    def close(self):self.db.close();self.temp.cleanup()


def indexed_hrs_inputs(portal):
    index=getattr(portal,'_hrs_input_index',None)
    if index is None:
        portal._require_dataset('hrs_decision_inputs.jsonl')
        index=portal._hrs_input_index=_InputIndex(portal.snapshot_dir/'hrs_decision_inputs.jsonl')
    return index


def close_portal_indexes(portal):
    index=getattr(portal,'_hrs_input_index',None)
    if index is not None:index.close();portal._hrs_input_index=None
    source=getattr(portal,'_hrs_archive_sources_cache',None)
    if source is not None and not getattr(portal,'_hrs_archive_shared',False):
        source.__exit__(None,None,None);portal._hrs_archive_sources_cache=None


class _EquityView:
    def __init__(self,sources):
        self.sources=sources;self.embedded={}
        for row in sources.get('decision_evidence').select(kind='scan_start'):
            for index,archive in enumerate(row['payload'].get('financial_context',{}).get('equity_source_archives',[])):
                payload=_decoded(archive['payload'])
                self.embedded.setdefault(archive['evidence_sha256'],(row['evidence_id'],index,_time(payload['as_of'])))
    def _checked(self,row):
        payload=_decoded(row['payload']);digest=row['evidence_sha256']
        _require(equity_canonical(payload)[1]==digest,'equity_payload_hash_mismatch')
        return dict(payload=payload,sha256=digest)
    def get(self,digest):
        row=self.sources.archive.get_record('equity_evidence',digest)
        if row is None and digest in self.embedded:
            identity,index,_at=self.embedded[digest]
            parent=self.sources.archive.get_record('decision_evidence',identity)
            row=_decoded(parent['payload'])['financial_context']['equity_source_archives'][index]
        return self._checked(row) if row is not None else None
    def select(self,after=None,before=None):
        seen=set()
        for row in self.sources.archive.iter_records('equity_evidence',event_start=after,event_end=before+timedelta(microseconds=1) if before is not None else None):
            seen.add(row['evidence_sha256']);yield self._checked(row)
        for digest,(_identity,_index,at) in self.embedded.items():
            if digest not in seen and (after is None or at>=after) and (before is None or at<=before):yield self.get(digest)
    def __iter__(self):return self.select()


def _decision_rows(records,**filters):
    if isinstance(records,_ArchiveFamily):return records.select(**filters)
    return (r for r in records if
        (filters.get('kind') is None or r['kind']==filters['kind']) and
        (filters.get('event_start') is None or r['event_time']>=filters['event_start']) and
        (filters.get('event_end') is None or r['event_time']<=filters['event_end']) and
        (filters.get('available_before') is None or r['available_at']<=filters['available_before']) and
        (filters.get('path') is None or r['payload'].get('path')==filters['path']))


def _equity_rows(equities,after=None,before=None):
    if isinstance(equities,_EquityView):return equities.select(after,before)
    return (e for e in equities if (after is None or _time(e['payload']['as_of'])>=after)
        and (before is None or _time(e['payload']['as_of'])<=before))


def verify_forecast_source_closure(sources,records=None):
    from .hrs_forecast_sources import FAMILY,verify_parent_closure
    def parents():
        yield from sources.get('equity_evidence',())
        decisions=records if records is not None else sources.get('decision_evidence',())
        for row in _decision_rows(decisions,kind='scan_start'):
            yield from row['payload'].get('financial_context',{}).get('equity_source_archives',())
    if isinstance(sources,_ArchiveSources):
        present=FAMILY in sources.archive.counts
        metadata=sources.archive.source_metadata.get(FAMILY,{})
        inventory=metadata.get('prefix_inventory')
        if inventory is not None:
            _require(inventory.get('account_id')==metadata.get('account_id'),'funding_forecasts_inventory_account_mismatch')
        def count(cutoff):
            if not present:return 0
            return sources.archive.count_records(FAMILY,available_before=cutoff)
        def rows(cutoff):
            return sources.archive.iter_records(FAMILY,available_before=cutoff,order_by_available=True) if present else iter(())
    else:
        supplied=sources.get(FAMILY)
        _require(supplied is None or isinstance(supplied,list),'funding_forecasts_source_shape_invalid')
        retained=supplied or []
        inventory=sources.get('hrs_funding_observations_inventory') if supplied is not None else None
        def count(cutoff):return sum(1 for row in retained if cutoff is None or _time(row['available_at'])<=cutoff)
        def rows(cutoff):return iter(sorted((row for row in retained if _time(row['available_at'])<=cutoff),key=lambda row:(_time(row['available_at']),row['evidence_sha256'])))
    return verify_parent_closure(parents(),source_rows=rows,source_count=count,inventory=inventory)


def _time(value):
    value=datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
    if not isinstance(value,datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('aware_timestamp_required')
    return value.astimezone(timezone.utc)


def _iso(value):
    return _time(value).isoformat().replace('+00:00','Z')


def _decoded(value):
    return json.loads(value) if isinstance(value,str) else copy.deepcopy(value)


def _write(path,value,*,jsonl=False):
    path.parent.mkdir(parents=True,exist_ok=True)
    rows=value if jsonl else [value]
    with path.open('w',encoding='utf-8') as handle:
        for row in rows:handle.write(canonical_payload(row)[0]+'\n')


class _RowSpool:
    def __init__(self,path):self.path,self.count=path,0;self.handle=path.open('w',encoding='utf-8')
    def append(self,row):self.handle.write(canonical_payload(row)[0]+'\n');self.count+=1
    def __len__(self):return self.count
    def __iter__(self):
        self.handle.flush()
        with self.path.open(encoding='utf-8') as rows:
            for line in rows:yield json.loads(line)
    def close(self):self.handle.close()


class _BarSpool:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.execute('CREATE TABLE bars(symbol TEXT, interval TEXT, opened TEXT, body TEXT, PRIMARY KEY(symbol,interval,opened))')
    def put(self,row):
        key=(row['symbol'],row['interval'],row['open_time'])
        prior=self.db.execute('SELECT body FROM bars WHERE symbol=? AND interval=? AND opened=?',key).fetchone()
        if prior:
            old=json.loads(prior[0]);_require(all(Decimal(str(old[k]))==Decimal(str(row[k])) for k in ('open','high','low','close','volume')),'kline_source_conflict:'+row['symbol']+':'+row['interval'])
        else:self.db.execute('INSERT INTO bars VALUES(?,?,?,?)',(*key,canonical_payload(row)[0]))
    def __contains__(self,key):return self.db.execute('SELECT 1 FROM bars WHERE symbol=? AND interval=? LIMIT 1',key).fetchone() is not None
    def items(self):
        for symbol,interval in self.db.execute('SELECT DISTINCT symbol,interval FROM bars ORDER BY symbol,interval'):
            def rows(s=symbol,i=interval):
                for body, in self.db.execute('SELECT body FROM bars WHERE symbol=? AND interval=? ORDER BY opened',(s,i)):yield json.loads(body)
            yield (symbol,interval),rows()
    def count(self,interval=None,*,after=None,through=None):
        clauses=[];args=[]
        if interval is not None:clauses.append('interval=?');args.append(interval)
        if after is not None:clauses.append("json_extract(body,'$.close_time')>?");args.append(_time(after).isoformat())
        if through is not None:clauses.append("json_extract(body,'$.close_time')<=?");args.append(_time(through).isoformat())
        return self.db.execute('SELECT count(*) FROM bars'+(' WHERE '+' AND '.join(clauses) if clauses else ''),args).fetchone()[0]
    def close(self):self.db.close()


def _require(condition,gap):
    if not condition:
        raise DataBlocked([gap])


def _records(rows):
    if isinstance(rows,_ArchiveFamily):return rows
    _require(isinstance(rows,list) and bool(rows),'decision_evidence_missing')
    _require(len(rows)<=MAX_ROWS,'decision_evidence_row_limit')
    result=[];seen={}
    for source in rows:
        row=dict(source);row['payload']=_decoded(row['payload'])
        _require(canonical_payload(row['payload'])[1]==row['payload_sha256'],'decision_payload_hash_mismatch:'+str(row.get('evidence_id')))
        row['event_time']=_time(row['event_time']);row['available_at']=_time(row['available_at'])
        _require(row['event_time']<=row['available_at'],'decision_availability_before_event')
        identity=row['evidence_id']
        _require(identity not in seen or seen[identity]==row,'decision_evidence_identity_conflict')
        if identity not in seen:
            result.append(row);seen[identity]=row
    return sorted(result,key=lambda row:(row['available_at'],row['event_time'],row['evidence_id']))


def _equity(rows):
    _require(isinstance(rows,list) and bool(rows),'equity_source_evidence_missing')
    result=[]
    for row in rows:
        payload=_decoded(row['payload'])
        _require(equity_canonical(payload)[1]==row['evidence_sha256'],'equity_payload_hash_mismatch')
        result.append(dict(payload=payload,sha256=row['evidence_sha256']))
    return result


def _scan_delay(policy):
    _require(isinstance(policy,dict) and policy.get('version')=='first_scan_start_v1'
        and type(policy.get('max_delay_seconds')) is int and 0<policy['max_delay_seconds']<=3600,
        'scan_boundary_policy_invalid')
    return policy['max_delay_seconds']


def resolve_scan_start(records,registered_start,window_end,policy):
    """Select by time alone: an unusable first scan must never be skipped."""
    start,end=_time(registered_start),_time(window_end)
    delay=_scan_delay(policy)
    rows=_records(records)
    candidates=[r for r in _decision_rows(rows,kind='scan_start',event_start=start,event_end=start+timedelta(seconds=delay)) if
        start<=r['event_time']<=start+timedelta(seconds=delay) and r['event_time']<end]
    _require(candidates,'first_scan_boundary_missing')
    first=min(r['event_time'] for r in candidates)
    selected=[r for r in candidates if r['event_time']==first]
    _require(len(selected)==1,'first_scan_boundary_ambiguous')
    row=selected[0]
    endings=list(rows.select(kind='scan_end',run_id=row['run_id'])) if isinstance(rows,_ArchiveFamily) else [r for r in rows if r['run_id']==row['run_id'] and r['kind']=='scan_end']
    _require(len(endings)==1 and endings[0]['payload'].get('status')=='complete',
        'first_scan_boundary_incomplete')
    return dict(policy=dict(policy),registered_start=_iso(start),actual_start=_iso(first),end=_iso(end),
        delay_seconds=(first-start).total_seconds(),full_registered_span=first==start,
        selected_evidence_id=row['evidence_id'],selected_payload_sha256=row['payload_sha256'],
        candidate_inventory=[dict(evidence_id=r['evidence_id'],event_time=_iso(r['event_time']),
            payload_sha256=r['payload_sha256']) for r in sorted(candidates,key=lambda r:(r['event_time'],r['evidence_id']))])


def _config(snapshot):
    config=snapshot['config'];hashes=snapshot['config_hashes']
    _require(set(hashes)==set(HASHES) and all(re.fullmatch('[0-9a-f]{64}',str(hashes[k])) for k in HASHES),'config_identity_missing_or_invalid')
    metadata=snapshot.get('loaded_config_metadata')
    if metadata is not None:
        _require(metadata.get('config_hash')==compute_config_hash(config),'effective_config_metadata_mismatch')
    evidence=snapshot.get('configuration_evidence')
    if evidence is not None:
        _require(evidence['config_hashes']==hashes,'configuration_evidence_identity_mismatch')
    return config,hashes


def _closed_bars(source,records,as_of,*,spool=None):
    grouped={}
    def add(symbol,interval,row):
        _require(isinstance(symbol,str) and re.fullmatch('[A-Z0-9]{1,32}',symbol) and interval in ('1m','1h','4h'),'market_bar_identity_invalid')
        minutes={'1m':1,'1h':60,'4h':240}[interval]
        if isinstance(row,list):
            opened=datetime.fromtimestamp(int(row[0])/1000,timezone.utc)
            data=dict(open_time=opened,close_time=opened+timedelta(minutes=minutes),open=row[1],high=row[2],low=row[3],close=row[4],volume=row[5],quote_volume=row[7] if len(row)>7 else None,trades=row[8] if len(row)>8 else None)
        else:
            data=dict(row)
            opened=data['open_time']
            opened=datetime.fromtimestamp(opened/1000,timezone.utc) if isinstance(opened,(int,float)) else _time(opened)
            data['open_time']=opened;data['close_time']=opened+timedelta(minutes=minutes)
            for short,long in [('open','open_price'),('high','high_price'),('low','low_price'),('close','close_price')]:
                if short not in data:data[short]=data[long]
            data.setdefault('trades',data.get('trade_count'));data.setdefault('quote_volume',None)
        if data['close_time']>as_of:return
        values=[Decimal(str(data[k])) for k in ('open','high','low','close','volume')]
        _require(all(v.is_finite() for v in values) and min(values[:4])>0 and values[4]>=0
            and values[1]>=max(values[0],values[2],values[3]) and values[2]<=min(values[0],values[1],values[3]),'kline_ohlcv_invalid:'+symbol+':'+interval)
        normalized={k:data[k] for k in ('open_time','close_time','open','high','low','close','volume','quote_volume','trades')}
        normalized.update(symbol=symbol,interval=interval)
        normalized=json.loads(canonical_payload(normalized)[0])
        if spool is not None:
            spool.put(normalized);return
        key=(symbol,interval);instant=normalized['open_time']
        prior=grouped.setdefault(key,{}).setdefault(instant,normalized)
        # Numeric representation may differ across raw providers; actual OHLCV may not.
        _require(all(Decimal(str(prior[k]))==Decimal(str(normalized[k])) for k in ('open','high','low','close','volume')),'kline_source_conflict:'+symbol+':'+interval)
    for batch in source.get('market_bars',[]):
        _require(batch.get('source') in ('postgres_klines','public_klines'),'market_bar_source_unknown')
        _require(canonical_payload(batch['rows'])[1]==batch['payload_sha256'],'market_bar_payload_hash_mismatch')
        for row in batch['rows']:add(batch['symbol'],batch['interval'],row)
    relevant=(row for kind in ('analysis','lv_rm_analysis','standard_candidate_outcome','lv_rm_candidate_outcome','public_market_response')
        for row in _decision_rows(records,kind=kind)) if isinstance(records,_ArchiveFamily) else records
    for row in relevant:
        if row['kind'] in ('analysis','lv_rm_analysis','standard_candidate_outcome','lv_rm_candidate_outcome'):
            for interval in ('1h','4h'):
                for bar in row['payload'].get('klines_'+interval,[]):add(row['symbol'],interval,bar)
        if row['kind']=='public_market_response' and row['payload']['path']=='/fapi/v1/klines':
            params=row['payload']['params'];interval=params.get('interval')
            if interval in ('1m','1h','4h'):
                for bar in row['payload']['response']:add(params['symbol'],interval,bar)
    return spool if spool is not None else {key:[rows[k] for k in sorted(rows)] for key,rows in grouped.items()}


def _flat_unchanged_proof(equities,state_at,boundary,records):
    anchors=[e for e in _equity_rows(equities,state_at,state_at) if e['payload'].get('capture',{}).get('local')=={} and e['payload'].get('source_collection',{}).get('complete') is True]
    proofs=(e for e in _equity_rows(equities,boundary) if e['payload'].get('source_collection',{}).get('complete') is True and e['payload'].get('capture',{}).get('local')=={})
    proof=min(proofs,key=lambda e:_time(e['payload']['available_at']),default=None)
    _require(anchors and proof,'bootstrap_ledger_boundary_unproven')
    p=proof['payload']
    for source in (anchors[0]['payload'],p):
        capture=source['capture']
        _require(source['income_report'].get('id_match_and_coverage_complete') is True and not source['income_report'].get('coverage_gaps'),'bootstrap_bridge_income_incomplete')
        fills=[dict(f,trade_time_utc=_time(f['trade_time_utc'])) for f in source['fills']]
        try:
            prove_positions(source['account_id'],source['segment_id'],_time(source['as_of']),capture['inventory'],capture['independent'],{},fills,source['order_owners'],{},bootstrap=False)
        except LedgerNotReady as exc:
            raise DataBlocked(['bootstrap_bridge_owner_unproven:'+str(exc)]) from exc
    owners={}
    for row in p['order_owners']:owners.setdefault((row['symbol'],str(row['order_id'])),set()).add(row['strategy'])
    _require(not any(state_at<_time(f['trade_time_utc'])<=boundary and 'hrs' in owners.get((f['symbol'],str(f['order_id'])),set()) for f in p['fills']),'bootstrap_bridge_hrs_fills')
    _require(not any(state_at<_time(e['event_time_utc'])<=boundary and e.get('strategy')=='hrs' for e in p['income_report']['events']),'bootstrap_bridge_hrs_income')
    _require(not any(state_at<r['event_time']<=boundary for kind in ('entry_budget_reserved','order_execution_result','order_execution_failure') for r in _decision_rows(records,kind=kind,event_start=state_at,event_end=boundary)),'bootstrap_bridge_risk_intent')
    _require(not any(state_at<_time(event['statement_at'])<=boundary for event in p.get('allocation_audit',{}).get('journal',[])),'bootstrap_bridge_allocation_changes_unsettled')
    return proof,anchors[0]


def _bootstrap(snapshot,start,equities,checkpoints,records,execution_model,monitoring_model,allow_inception):
    config,hashes=_config(snapshot)
    financial=snapshot['financial_context']
    _require(financial.get('database_snapshot_coherent') is True and financial.get('replay_bootstrap_ready') is True,'bootstrap_database_snapshot_unproven')
    available=max(_time(financial['available_at']),_time(snapshot['_available_at']))
    tables={name:dict(as_of=_iso(start),complete=True,rows=financial['state_tables'][name]['rows']) for name in TABLES}
    for name in TABLES:
        _require(financial['state_tables'][name].get('database_snapshot_coherent') is True,'bootstrap_state_table_incoherent:'+name)
    _require(not tables['hrs_positions']['rows'] and not snapshot.get('positions'),'nonflat_opening_cost_mae_funding_proof_missing')
    _require(_time(financial['entry_policy']['effective_at'])<_time(financial['entry_policy']['expires_at']),'entry_policy_timebounds_invalid')
    raw=financial['ledger_state'];ledger=_decoded(raw['ledger_state']);gate=_decoded(raw['gate_state'])
    state_at=_time(ledger['as_of']);bridge=None
    _require(state_at<=start,'bootstrap_ledger_from_future')
    if state_at<start:
        proof,anchor=_flat_unchanged_proof(equities,state_at,start,records)
        _require(isinstance(financial.get('reservations'),list) and not any(r.get('state')!='released' for r in financial['reservations']),'bootstrap_bridge_reservation_inventory_missing')
        available=max(available,_time(proof['payload']['available_at']),_time(anchor['payload']['available_at']))
        bridge=dict(version='proven_flat_unchanged_v1',from_as_of=_iso(state_at),economic_as_of=_iso(start),available_at=_iso(available),source_sha256=[anchor['sha256'],proof['sha256']])
    history=[dict(as_of=_iso(_time(cp['as_of'])),equity=str(cp['equity']),units=str(cp['units']),nav=str(cp['nav'])) for cp in checkpoints if _time(cp['as_of'])<=start and _time(cp.get('created_at',cp['as_of']))<=available]
    _require(history,'bootstrap_checkpoint_history_missing')
    history=[cp for cp in history if _time(cp['as_of'])!=start]+[dict(as_of=_iso(start),equity=ledger['equity'],units=ledger['units'],nav=ledger['nav'])]
    risk=snapshot['runtime']['risk_manager']
    runtime={key:risk[key] for key in ('pause_until','drawdown_pause_until','consecutive_losses','daily_open_count','stop_loss_monitor','circuit_breakers')}
    runtime.update(signal_timestamps=snapshot['runtime']['signal_timestamps'],daily_reset_date=snapshot['runtime']['daily_reset_date'],total_pnl=snapshot['runtime']['total_pnl'],pending_timeout_entries=financial['pending_timeout_entries'])
    cooldown=snapshot['market_circuit_breaker_state']
    offset=timedelta(seconds=cooldown['process_utc_offset_seconds'])
    runtime['market_cooldowns']={symbol:_iso(datetime.fromisoformat(at).replace(tzinfo=timezone(offset))) for symbol,at in cooldown['cooldown_until_local'].items()}
    result=dict(as_of=_iso(start),complete=True,margin_reservations=financial.get('reservations',[]),config=config,config_hashes=hashes,allocation=financial['allocation'],account_equity=financial['account_equity'],entry_policy=financial['entry_policy'],market_circuit_breaker_config=snapshot['market_circuit_breaker_config'],runtime=runtime,
        ledger=dict(segment_id=ledger['segment_id'],units=ledger['units'],history_complete_from=ledger['inception'],history=history,allow_inception_segment=allow_inception,gate_state=gate),bootstrap_available_at=_iso(available),bootstrap_bridge=bridge,execution_model=execution_model,monitoring_model=monitoring_model)
    if financial.get('runtime_slot_scope') is not None:
        from strategies.hrs.entry_occupancy import SLOT_SCOPE
        _require(financial['runtime_slot_scope']==SLOT_SCOPE,'hrs_slot_scope_unknown')
        result['runtime_slot_scope']=SLOT_SCOPE
    return result,tables,available


def _account_occupancy(inventory,snapshot,financial,observed,equities,*,slot_scope=None,
        slot_inputs=None,checked_at=None,source_evidence_id=None,claims_evidence_id=None):
    """Observed external holdings; HRS counterfactual holdings are modeled separately."""
    _require(isinstance(inventory,list),'account_occupancy_inventory_missing')
    if slot_scope is not None:
        from strategies.hrs.entry_occupancy import SLOT_SCOPE,prove_hrs_slot_ownership,HrsOccupancyDenied
        _require(slot_scope==SLOT_SCOPE,'hrs_slot_scope_unknown')
        _require(isinstance(slot_inputs,dict) and slot_inputs.get('inventory')==inventory
            and _time(slot_inputs['observed_at'])==observed,'hrs_slot_inputs_missing_or_conflicting')
        _require(bool(source_evidence_id) and bool(claims_evidence_id),'hrs_slot_raw_source_missing')
        checked_at=checked_at or observed
        sources=[e for e in _equity_rows(equities,checked_at-timedelta(seconds=120),observed)
            if _time(e['payload']['available_at'])<=checked_at]
        _require(sources,'hrs_slot_raw_parent_missing')
        source=max(sources,key=lambda e:(_time(e['payload']['as_of']),e['sha256']))
        account=financial.get('entry_policy',{}).get('account_id')
        _require(isinstance(account,str) and bool(account) and source['payload'].get('account_id')==account,
            'hrs_slot_account_mismatch')
        try:
            proof=prove_hrs_slot_ownership(account,
                dict(evidence_sha256=source['sha256'],payload=source['payload']),slot_inputs,
                financial['claims'],checked_at=checked_at)
        except HrsOccupancyDenied as exc:raise DataBlocked([exc.reason_code]) from exc
        others=[dict(row,strategy_id=proof['owners'][row['symbol']],
            ownership_evidence_sha256=source['sha256'],owner_source_as_of=source['payload']['as_of'])
            for row in inventory if Decimal(str(row['positionAmt']))!=0 and proof['owners'][row['symbol']]!='hrs']
        return dict(version='hrs_account_occupancy_v2',observed_at=_iso(observed),other_positions=others,inventory=copy.deepcopy(inventory),
            slot_scope=SLOT_SCOPE,slot_ownership_proof=proof,slot_source_evidence_id=source_evidence_id,
            claims_source_evidence_id=claims_evidence_id)
    others=[p for p in inventory if Decimal(str(p['positionAmt']))!=0 and p['symbol'] not in snapshot.get('positions',{})]
    result=[]
    if others:
        sources=[e for e in _equity_rows(equities,observed-timedelta(seconds=120),observed) if e['payload'].get('source_collection',{}).get('complete') is True
            and e['payload'].get('income_report',{}).get('id_match_and_coverage_complete') is True
            and 'fills' in e['payload'] and 'order_owners' in e['payload']
            and timedelta(0)<=observed-_time(e['payload']['as_of'])<=timedelta(seconds=120)
            and _time(e['payload']['available_at'])<=observed]
        _require(sources,'hrs_exogenous_inventory_owner_unproven')
        source=max(sources,key=lambda e:_time(e['payload']['as_of']));p=source['payload']
        fills=[dict(row,trade_time_utc=_time(row['trade_time_utc'])) for row in p['fills']]
        owned=owned_fill_positions(p['account_id'],fills,p['order_owners'],through=_time(p['as_of']))
        for row in others:
            choices=[owner for (owner,symbol),(qty,basis) in owned.items() if symbol==row['symbol'] and qty==Decimal(str(row['positionAmt'])) and basis==Decimal(str(row['entryPrice']))]
            claims={c['strategy'] for c in financial['claims'] if c['symbol']==row['symbol'] and c.get('claim_state') in ('ACTIVE','PENDING') and (not c.get('expires_at') or _time(c['expires_at'])>observed)}
            _require(len(choices)==1 and choices[0]!='hrs' and claims=={choices[0]},'hrs_exogenous_inventory_owner_unproven:'+row['symbol'])
            result.append(dict(row,strategy_id=choices[0],ownership_evidence_sha256=source['sha256'],owner_source_as_of=p['as_of']))
    return dict(version='hrs_account_occupancy_v1',observed_at=_iso(observed),other_positions=result)


def _raw_decision(sources,identity,checked_at):
    records=_records(sources.get('decision_evidence'))
    if isinstance(records,_ArchiveFamily):
        row=sources.archive.get_record('decision_evidence',identity)
        rows=_records([row]) if row is not None else []
    else:rows=[row for row in records if row['evidence_id']==identity]
    _require(len(rows)==1,'hrs_raw_source_missing')
    row=rows[0];_require(row['available_at']<=checked_at,'hrs_raw_source_unavailable')
    return row


def verify_runtime_slot_occupancy(portal,raw,checked_at):
    """Reproduce new policy proof from original manifest-bound source receipts."""
    occupancy=raw.get('risk',{}).get('account_occupancy',{})
    if occupancy.get('version')=='hrs_account_occupancy_v1' and 'slot_scope' not in occupancy:return
    _require(occupancy.get('version')=='hrs_account_occupancy_v2','hrs_slot_occupancy_version_unknown')
    sources=snapshot_evidence_sources(portal);records=_records(sources.get('decision_evidence'))
    slot=_raw_decision(sources,occupancy['slot_source_evidence_id'],checked_at)
    claim=_raw_decision(sources,occupancy['claims_source_evidence_id'],checked_at)
    analysis=_raw_decision(sources,raw['evidence_ids'][0],checked_at)
    _require(analysis['kind'] in ('analysis','lv_rm_analysis') and analysis['symbol']==raw['symbol']
        and analysis['event_time']==_time(raw['decision_at'])
        and slot['run_id']==claim['run_id']==analysis['run_id']
        and slot.get('symbol') in (None,raw['symbol']) and claim.get('symbol') in (None,raw['symbol']),
        'hrs_slot_raw_cycle_mismatch')
    if isinstance(records,_ArchiveFamily):starts=list(records.select(kind='scan_start',run_id=analysis['run_id']))
    else:starts=[row for row in records if row['kind']=='scan_start' and row['run_id']==analysis['run_id']]
    _require(len(starts)==1,'hrs_slot_raw_cycle_mismatch')
    financial=starts[0]['payload']['financial_context']
    if slot['kind']=='scan_start':
        scope=financial.get('runtime_slot_scope');inputs=financial.get('hrs_slot_inputs')
    else:
        _require(slot['kind']=='entry_stage_inputs' and slot['payload'].get('stage')=='reservation_inventory','hrs_slot_raw_source_kind_invalid')
        scope=slot['payload'].get('slot_scope');inputs=slot['payload'].get('slot_inputs')
    if claim['kind']=='scan_start':claims=claim['payload']['financial_context']['claims']
    else:
        _require(claim['kind']=='entry_stage_inputs' and 'claims' in claim['payload'],'hrs_slot_claim_source_invalid')
        claims=claim['payload']['claims']
    digest=occupancy['slot_ownership_proof']['equity_evidence_sha256']
    if isinstance(sources,_ArchiveSources):
        view=getattr(portal,'_hrs_slot_equity_view',None)
        if view is None or view.sources is not sources:portal._hrs_slot_equity_view=view=_EquityView(sources)
        parent=view.get(digest)
    else:
        rows=list(sources.get('equity_evidence') or [])
        rows.extend(parent for row in records if row['kind']=='scan_start'
            for parent in row['payload'].get('financial_context',{}).get('equity_source_archives',[]))
        parents=[row for row in _equity(rows) if row['sha256']==digest]
        _require(bool(parents),'hrs_slot_raw_parent_missing');parent=parents[0]
    _require(parent is not None,'hrs_slot_raw_parent_missing')
    expected=_account_occupancy(inputs['inventory'],{},dict(financial,claims=claims),_time(inputs['observed_at']),[parent],
        slot_scope=scope,slot_inputs=inputs,checked_at=checked_at,
        source_evidence_id=slot['evidence_id'],claims_evidence_id=claim['evidence_id'])
    _require(equity_canonical(expected)[1]==equity_canonical(occupancy)[1],'hrs_slot_raw_proof_mismatch')


def verify_runtime_kline_repair(portal,raw,checked_at):
    from .hrs_kline_sources import verify_kline_repair
    sources=snapshot_evidence_sources(portal)
    analysis=_raw_decision(sources,raw['evidence_ids'][0],checked_at)
    records=_records(sources.get('decision_evidence'))
    if isinstance(records,_ArchiveFamily):
        repairs=list(records.select(kind='kline_repair',run_id=analysis['run_id'],symbol=raw['symbol'],available_before=analysis['available_at']))
        public=records.select(kind='public_market_response',run_id=analysis['run_id'],symbol=raw['symbol'],available_before=checked_at)
    else:
        repairs=[row for row in records if row['kind']=='kline_repair' and row['run_id']==analysis['run_id']
            and row.get('symbol')==raw['symbol'] and row['available_at']<=analysis['available_at']]
        public=(row for row in records if row['kind']=='public_market_response' and row['run_id']==analysis['run_id'] and row.get('symbol')==raw['symbol'])
    if not repairs:
        _require(not raw.get('kline_repair_evidence_id'),'hrs_kline_repair_source_missing')
        return
    repair=max(repairs,key=lambda row:(row['available_at'],row['evidence_id']))
    _require(repair['evidence_id']==raw.get('kline_repair_evidence_id')
        and repair['payload_sha256']==raw.get('kline_repair_payload_sha256'),'hrs_kline_repair_identity_mismatch')
    summary=verify_kline_repair(repair,analysis,public,portal.hrs_replay_state(portal.window_start)['config'])
    _require(summary['request_source_ids']==raw['kline_repair_source_ids'],'hrs_kline_repair_source_inventory_mismatch')


def _cycles(records,bootstrap,bootstrap_available,start,end,equities,*,spools=None):
    if isinstance(records,_ArchiveFamily):groups=records.runs()
    else:
        grouped={}
        for row in records:grouped.setdefault(row['run_id'],[]).append(row)
        groups=sorted(grouped.items(),key=lambda pair:min(r['event_time'] for r in pair[1]))
    inputs,calendar,used=spools if spools is not None else ([],[],[])
    sequence=0
    for run,rows in groups:
        starts=list(_run_rows(rows,kind='scan_start'));ends=list(_run_rows(rows,kind='scan_end'))
        if not starts:continue
        if all(r['available_at']<=start for r in (*starts,*ends)) and max(r['available_at'] for r in rows)<=start:continue
        if min(r['event_time'] for r in starts)>=end:continue
        _require(len(starts)==1 and len(ends)==1 and ends[0]['payload'].get('status')=='complete','scan_incomplete:'+run)
        if starts[0]['event_time']>end:continue
        snapshot=starts[0]['payload'];final=ends[0]['payload']['state']
        config,hashes=_config(snapshot)
        _require(hashes==bootstrap['config_hashes'] and config==bootstrap['config'],'config_change_requires_partition:'+run)
        _require(snapshot['financial_context'].get('runtime_slot_scope')==bootstrap.get('runtime_slot_scope'),
            'slot_policy_change_requires_partition:'+run)
        scopes=sorted((r for kind in ('standard_scan_scope','lv_rm_scan_scope') for r in _run_rows(rows,kind=kind)),key=lambda r:(r['available_at'],r['event_time'],r['evidence_id']))
        symbols=list(dict.fromkeys(s for r in scopes for s in r['payload']['symbols']))
        if not symbols:
            denials=list(_run_rows(rows,kind='scan_denial'))
            _require(denials,'scan_scope_missing:'+run)
            used.append(dict(run_id=run,scope=[],denial_evidence=[r['evidence_id'] for r in denials]))
            continue
        batches=list(_run_rows(rows,kind='execution_batch'))
        _require(len(batches)==1,'execution_batch_missing_or_duplicate:'+run)
        batch=batches[0];released=batch['available_at']
        _require(released<=end,'execution_batch_after_window:'+run)
        _require(released>=bootstrap_available,'bootstrap_unavailable_at_decision:'+run)
        scan=copy.deepcopy(final['candidate_scan'])
        _require(all(k in scan for k in ('as_of','short_candidates','long_candidates','dynamic_thresholds')),'candidate_snapshot_missing:'+run)
        scan['complete']=True;scan['lv_rm_symbols']=list(dict.fromkeys(s for r in scopes if r['kind']=='lv_rm_scan_scope' for s in r['payload']['symbols']))
        chosen=list(dict.fromkeys(s['symbol'] for s in batch['payload']['ordered_signals']))
        _require(set(chosen)<=set(symbols),'execution_batch_outside_scope:'+run)
        # Score-only vetoes release with the score batch. Surviving entries retain
        # the actual controller order and their later pre-entry input availability.
        order=[s for s in symbols if s not in chosen]+chosen
        financial=snapshot['financial_context']
        _require(isinstance(financial.get('exchange_positions'),list) and isinstance(financial.get('reservations'),list),'account_occupancy_inventory_missing:'+run)
        for reservation in financial['reservations']:
            if reservation.get('state')=='released':continue
            _require(reservation.get('created_at') is not None,'hrs_retained_reservation_source_time_missing:'+run)
            if _time(reservation['created_at'])>start:
                _require(any(r['payload'].get('decision',{}).get('client_order_id')==reservation.get('client_order_id') for r in _decision_rows(records,kind='entry_budget_reserved')),'hrs_retained_reservation_origin_unproven:'+run)
        policy=financial['entry_policy']
        _require(_time(policy['effective_at'])<_time(policy['expires_at']),'entry_policy_timebounds_invalid:'+run)
        raw_ledger=_decoded(financial['ledger_state']['ledger_state'])
        component=financial.get('hrs_marked_equity_observation')
        component_source=None
        if component is not None:
            hrs_equity=Decimal(str(component['value']))
            source=[equities.get(component['evidence_sha256'])] if isinstance(equities,_EquityView) else [e for e in equities if e['sha256']==component['evidence_sha256']]
            source=[e for e in source if e is not None]
            age=(_time(financial['available_at'])-_time(component['as_of'])).total_seconds()
            _require(len(source)>=1 and component['ledger_version']==financial['ledger_state']['version']
                and source[0]['payload'].get('result',{}).get('ledger_version')==component['ledger_version']
                and _time(component['as_of'])==_time(raw_ledger['as_of'])==_time(source[0]['payload']['as_of'])
                and hrs_equity==Decimal(str(raw_ledger['equity'])) and 0<=age<=120
                and _time(component['available_at'])==_time(source[0]['payload']['available_at'])<=_time(financial['available_at']),
                'account_equity_component_observation_unproven:'+run)
            component_source=dict(component,model='ledger_sample_120s_v1',source_age_seconds=age,mark_synchrony=False)
        elif 'hrs_marked_equity' in financial:
            hrs_equity=Decimal(str(financial['hrs_marked_equity']))
            component_source=dict(model='captured_component_v1',as_of=financial['available_at'],available_at=financial['available_at'],source_age_seconds=0,mark_synchrony=False)
        else:
            _require(not snapshot.get('positions') and not financial['state_tables']['hrs_positions']['rows'],'nonflat_account_equity_component_unproven:'+run)
            ledger_at=_time(raw_ledger['as_of']);financial_at=_time(financial['available_at'])
            _require(ledger_at<=financial_at,'financial_ledger_from_future:'+run)
            if ledger_at<financial_at:
                proof,_anchor=_flat_unchanged_proof(equities,ledger_at,financial_at,records)
                _require(_time(proof['payload']['available_at'])<=released,'account_equity_component_proof_unavailable:'+run)
            hrs_equity=Decimal(str(raw_ledger['equity']))
        for symbol in order:
            analyses=[r for kind in ('analysis','lv_rm_analysis') for r in _run_rows(rows,kind=kind,symbol=symbol)]
            _require(len(analyses)==1,'raw_analysis_missing_or_duplicate:'+run+':'+symbol)
            analysis=analyses[0];decided=analysis['event_time'];payload=analysis['payload']
            _require(payload.get('klines_1h') and payload.get('klines_4h'),'analysis_bar_history_missing:'+symbol)
            providers=sorted(_run_rows(rows,kind='market_input',symbol=symbol,available_before=analysis['available_at']),key=lambda r:(r['available_at'],r['event_time'],r['evidence_id']))
            values={r['payload']['operation']:r for r in providers}
            operations={'oi_usd':'get_oi_usd','funding_rate':'get_funding_rate','volume_24h':'get_24h_volume','cap':'get_market_cap_provenance'}
            _require(all(op in values for op in operations.values()),'market_input_missing:'+run+':'+symbol)
            cap=values[operations['cap']]['payload']['result']
            cap_at=cap['as_of'];cap_at=datetime.fromtimestamp(cap_at,timezone.utc) if isinstance(cap_at,(int,float)) else _time(cap_at)
            expiry=_time(cap['expires_at']) if cap.get('expires_at') else cap_at+timedelta(seconds=config.get('market_cap',{}).get('cache_ttl_seconds',3600))
            if cap['source'] not in ('coingecko','coinmarketcap','cache_hit'):
                expiry=values[operations['cap']]['available_at']+timedelta(seconds=config.get('market_cap',{}).get('cache_ttl_seconds',3600))
            available=max(analysis['available_at'],*(r['available_at'] for r in values.values()),_time(financial['available_at']))
            repair_refs={}
            repairs=list(_run_rows(rows,kind='kline_repair',symbol=symbol,available_before=analysis['available_at']))
            if repairs:
                from .hrs_kline_sources import verify_kline_repair
                repair=max(repairs,key=lambda row:(row['available_at'],row['evidence_id']))
                proof=verify_kline_repair(repair,analysis,
                    _run_rows(rows,kind='public_market_response',symbol=symbol,available_before=analysis['available_at']),config)
                repair_refs=dict(kline_repair_evidence_id=repair['evidence_id'],kline_repair_payload_sha256=repair['payload_sha256'],
                    kline_repair_source_ids=proof['request_source_ids'])
            market={key:values[op]['payload']['result'] for key,op in operations.items() if key!='cap'}
            market.update(entry_price=payload['klines_1h'][-1]['close'],market_cap=cap['value'],has_market_cap=cap.get('is_proxy') is False and cap['source'] in ('coingecko','coinmarketcap','cache_hit') and Decimal(str(cap['value']))>0,
                market_cap_source=cap['source'],source_observed_at=_iso(max(r['available_at'] for r in values.values())),expires_at=_iso(expiry),provenance=cap)
            if market['has_market_cap']:
                _require(cap.get('provider') and cap.get('provider_asset_id'),'market_cap_provider_identity_missing:'+symbol)
            risk=dict(allocation=financial['allocation'],entry_policy=financial['entry_policy'],account_equity_excluding_hrs=str(Decimal(str(financial['account_equity']))-hrs_equity),account_equity_component_source=component_source,baseline_margin_reservations=financial['reservations'])
            stages=sorted(_run_rows(rows,kind='entry_stage_inputs',symbol=symbol),key=lambda r:(r['available_at'],r['event_time'],r['evidence_id']))
            facts={}
            for stage in stages:
                available=max(available,stage['available_at'])
                facts.update({k:v for k,v in stage['payload'].items() if k!='stage'})
            if 'exchange_positions' in facts:
                positions=facts['exchange_positions']
                _require(isinstance(positions,list),'entry_stage_inventory_missing:'+symbol)
                risk['exchange_position_clear']=not any(p['symbol']==symbol and Decimal(str(p['positionAmt']))!=0 for p in positions)
            if 'claims' in facts:
                claims=facts['claims'];_require(isinstance(claims,list),'entry_claim_inventory_missing:'+symbol)
                risk['claims_clear']=not any(c['symbol']==symbol and c.get('strategy')!='hrs' and c.get('claim_state') in ('ACTIVE','PENDING') and
                    (not c.get('expires_at') or _time(c['expires_at'])>available) for c in claims)
            for key in ('pool_index','pool_index_12h'):
                if key in facts:risk[key]=facts[key]
                elif bootstrap['market_circuit_breaker_config'] is None:risk[key]=None
                else:raise DataBlocked(['market_guard_index_inputs_missing:'+symbol])
            for key in ('pre_entry_oi_usd','pre_entry_funding_rate'):
                if key in facts:market[key]=facts[key]
            inventory_stage=next((r for r in reversed(stages) if r['payload'].get('stage')=='reservation_inventory'),None)
            occupancy_at=inventory_stage['available_at'] if inventory_stage else _time(financial['available_at'])
            inventory=inventory_stage['payload']['exchange_positions'] if inventory_stage else financial['exchange_positions']
            event_available=max(available,released)
            scope=financial.get('runtime_slot_scope')
            if scope is not None:
                slot_source=inventory_stage or starts[0]
                if inventory_stage:
                    _require(inventory_stage['payload'].get('slot_scope')==scope,'hrs_slot_stage_scope_mismatch')
                    slot_inputs=inventory_stage['payload'].get('slot_inputs')
                else:slot_inputs=financial.get('hrs_slot_inputs')
                _require(isinstance(slot_inputs,dict),'hrs_slot_inputs_missing')
                inventory=slot_inputs['inventory'];occupancy_at=_time(slot_inputs['observed_at'])
                claim_stage=next((r for r in reversed(stages) if 'claims' in r['payload']),None)
                claims=claim_stage['payload']['claims'] if claim_stage else financial['claims']
                occupancy_financial=dict(financial,claims=claims)
                risk['account_occupancy']=_account_occupancy(inventory,snapshot,occupancy_financial,occupancy_at,equities,
                    slot_scope=scope,slot_inputs=slot_inputs,checked_at=event_available,
                    source_evidence_id=slot_source['evidence_id'],claims_evidence_id=(claim_stage or starts[0])['evidence_id'])
            else:risk['account_occupancy']=_account_occupancy(inventory,snapshot,financial,occupancy_at,equities)
            _require(event_available<=end,'decision_inputs_after_window:'+run+':'+symbol)
            history=(dict(analysis_evidence_id=analysis['evidence_id'],analysis_payload_sha256=analysis['payload_sha256'])
                if isinstance(records,_ArchiveFamily) else dict(analysis_klines_1h=payload['klines_1h'],analysis_klines_4h=payload['klines_4h']))
            inputs.append(dict(symbol=symbol,decision_at=_iso(decided),score_available_at=_iso(analysis['available_at']),observed_at=_iso(available),available_at=_iso(available),complete=True,candidate_scan=scan,market=market,risk=risk,config_hashes=hashes,**history,**repair_refs,
                evidence_ids=[r['evidence_id'] for r in [analysis,*values.values(),*stages]]))
            calendar.append(dict(symbol=symbol,decision_at=_iso(decided),available_at=_iso(event_available),batch_available_at=_iso(released),batch_id=run,sequence=sequence))
            sequence+=1
        used.append(dict(run_id=run,scope=symbols,batch_evidence=batch['evidence_id']))
    _require(inputs,'decision_calendar_empty_or_history_unavailable')
    return inputs,calendar,used


def _funding(records,source,symbols,start,end):
    observations=source.get('funding_observations',[])
    if isinstance(observations,_ArchiveFamily):
        evidence=dict(source='prospective_exchange_forecast',complete=True,observations_archive_family='funding_observations')
        schedule=attested_funding_schedule(dict(source='prospective_exchange_forecast',complete=True,observations=observations),symbols,start,end)
    else:
        evidence=dict(source='prospective_exchange_forecast',complete=True,observations=observations)
        schedule=attested_funding_schedule(evidence,symbols,start,end)
    published={}
    for row in _decision_rows(records,kind='public_market_response',path='/fapi/v1/fundingRate'):
        payload=row['payload']
        if row['kind']!='public_market_response' or payload['path']!='/fapi/v1/fundingRate':continue
        response=payload['response']
        for fact in response if isinstance(response,list) else [response]:
            at=datetime.fromtimestamp(int(fact['fundingTime'])/1000,timezone.utc);symbol=fact['symbol']
            if symbol not in schedule or at not in schedule[symbol]:continue
            if row['available_at']>end:continue
            _require(fact.get('markPrice') is not None,'funding_settlement_mark_missing:'+symbol)
            selected=dict(symbol=symbol,funding_time=_iso(at),mark_price=str(fact['markPrice']),mark_time=_iso(at),mark_source='binance_um_funding_rate')
            result=dict(selected,funding_rate=str(fact['fundingRate']),mark_available_at=_iso(row['available_at']),mark_payload_hash=hashlib.sha256(json.dumps(selected,sort_keys=True,separators=(',',':')).encode()).hexdigest())
            key=(symbol,at)
            if key not in published or _time(published[key]['mark_available_at'])>row['available_at']:published[key]=result
            else:_require(all(published[key][k]==result[k] for k in ('funding_rate','mark_price')),'funding_publication_conflict:'+symbol)
    for symbol,times in schedule.items():
        for at in times:_require((symbol,at) in published,'funding_settlement_publication_missing:'+symbol+':'+_iso(at))
    return evidence,[published[key] for key in sorted(published)]


def _cash_flows(source,records,terminal,start,end,segment):
    retained={}
    for record in _decision_rows(records,kind='scan_start'):
        if record['kind']!='scan_start':continue
        raw=record['payload'].get('financial_context',{}).get('ledger_state')
        if not raw:continue
        for flow in _decoded(raw['ledger_state']).get('cash_flows',[]):
            identity=flow['event_id']
            retained.setdefault(identity,[]).append((flow,record['available_at']))
    result=[];seen=set()
    for row in source.get('cash_flows',[]):
        at=_time(row['occurred_at'])
        if not start<at<=end:continue
        _require(row.get('verified') is True and row['segment_id']==segment and row['event_id'] not in seen,'cashflow_identity_or_verification_invalid')
        proofs=[(flow,available) for flow,available in retained.get(row['event_id'],[]) if
            Decimal(str(flow['amount']))==Decimal(str(row['amount'])) and _time(flow['occurred_at'])==at and flow['segment_id']==segment]
        _require(proofs,'cashflow_ledger_proof_missing:'+row['event_id'])
        observed=max(_time(row['observed_at']),min(available for _flow,available in proofs))
        _require(observed<=at,'cashflow_publication_or_verification_unproven')
        result.append(dict(row,observed_at=_iso(observed)));seen.add(row['event_id'])
    actual={identity for identity,proofs in retained.items() if any(start<_time(flow['occurred_at'])<=end for flow,_available in proofs)}
    _require(seen==actual,'cashflow_inventory_missing_or_unexpected')
    changes=[event for event in terminal['allocation_audit'].get('journal',[]) if start<_time(event['statement_at'])<=end]
    _require(not changes or result,'allocation_cashflow_mapping_unproven')
    return result


def export_evidence_bundle(sources,output_root,*,window_start,as_of,execution_model=None,monitoring_model=None,allow_inception_segment=False,start_policy=None,end_policy=None):
    """Read raw facts, validate actual adapter/engine contract, then seal once.

    Sources retain the SQL row envelopes and original hashes. The exporter never
    takes recorded signals as entry instructions. Execution models are explicit
    research assumptions; they do not manufacture market or strategy evidence.
    """
    output=Path(output_root);start,end=_time(window_start),_time(as_of)
    _require(start<end and end-start<=timedelta(days=90),'invalid_or_excessive_window')
    _require(not _has_symlink_ancestor(output) and _repo_destination_is_ignored(output/'gap_bundle'),'output_not_private_or_symlink')
    cleanup=ExitStack()
    try:
        if isinstance(sources,dict) and sources.get('evidence_archive'):
            sources=cleanup.enter_context(_ArchiveSources(sources))
        archived=isinstance(sources,_ArchiveSources)
        if not archived:_require(len(canonical_payload(sources)[0].encode())<=MAX_BYTES,'source_byte_limit')
        missing=[]
        for name,gap in (('decision_evidence','decision_evidence_missing'),('funding_observations','funding_schedule_observations_missing'),('equity_checkpoints','bootstrap_checkpoint_history_missing')):
            if not sources.get(name):missing.append(gap)
        if missing:raise DataBlocked(missing)
        records=_records(sources.get('decision_evidence'))
        if not archived:verify_forecast_source_closure(sources,records)
        boundary=None
        end_boundary=None
        if end_policy is not None:
            end_boundary=resolve_scan_start(records,end,end+timedelta(hours=1,microseconds=1),end_policy)
            end=_time(end_boundary['actual_start'])
        if start_policy is not None:
            boundary=resolve_scan_start(records,start,_time(as_of),start_policy)
            start=_time(boundary['actual_start'])
        if archived:equities=_EquityView(sources)
        else:
            equity_rows=list(sources.get('equity_evidence') or [])
            equity_rows.extend(archive for row in records if row['kind']=='scan_start'
                for archive in row['payload'].get('financial_context',{}).get('equity_source_archives',[]))
            equities=_equity(equity_rows)
        openings=[r for r in _decision_rows(records,kind='scan_start',event_start=start,event_end=start) if r['event_time']==start]
        _require(len(openings)==1,'bootstrap_capture_boundary_missing_or_duplicate')
        opening=copy.deepcopy(openings[0]['payload']);opening['_available_at']=openings[0]['available_at']
        bootstrap,tables,available=_bootstrap(opening,start,equities,sources.get('equity_checkpoints',[]),records,execution_model,monitoring_model,allow_inception_segment)
        scratch=Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix='hrs-conversion-spools-')))
        spools=None
        if archived:
            spools=tuple(_RowSpool(scratch/name) for name in ('inputs.jsonl','calendar.jsonl','coverage.jsonl'))
            for spool in spools:cleanup.callback(spool.close)
        inputs,calendar,coverage=_cycles(records,bootstrap,available,start,end,equities,spools=spools)
        symbols=tuple(sorted({r['symbol'] for r in inputs}))
        bar_spool=_BarSpool(scratch/'bars.sqlite') if archived else None
        if bar_spool is not None:cleanup.callback(bar_spool.close)
        bars=_closed_bars(sources,records,end,spool=bar_spool)
        for symbol in symbols:
            for interval in ('1h','4h','1m'):
                _require((symbol,interval) in bars,'market_bars_missing:'+symbol+':'+interval)
        forecasts,funding=_funding(records,sources,symbols,start,end)
        complete=(e for e in _equity_rows(equities,end) if e['payload'].get('source_collection',{}).get('complete') is True and e['payload'].get('income_report',{}).get('id_match_and_coverage_complete') is True)
        terminal_source=max(complete,key=lambda e:_time(e['payload']['as_of']),default=None)
        _require(terminal_source is not None,'cashflow_and_income_inventory_through_end_unproven')
        terminal=terminal_source['payload']
        _require(terminal.get('allocation_audit',{}).get('version')==1,'allocation_journal_inventory_missing')
        flows=_cash_flows(sources,records,terminal,start,end,bootstrap['ledger']['segment_id'])
        exchange_row=max(_decision_rows(records,kind='public_market_response',path='/fapi/v1/exchangeInfo',available_before=available),key=lambda r:r['available_at'],default=None)
        _require(exchange_row is not None,'exchange_info_at_bootstrap_missing')
        exchange=exchange_row['payload']['response']
        with tempfile.TemporaryDirectory(prefix='hrs-evidence-conversion-') as temp:
            staging=Path(temp).resolve()
            execution_count=bars.count('1m',after=start,through=end) if archived else sum(1 for (symbol,interval),rows in bars.items() if interval=='1m' for row in rows if start<_time(row['close_time'])<=end)
            bar_events=bars.count(after=start,through=end) if archived else sum(1 for rows in bars.values() for row in rows if start<_time(row['close_time'])<=end)
            from .replay.clock import collect_daily_mark_events
            event_count=bar_events+len(calendar)+len(flows)+3*len(funding)+len(collect_daily_mark_events(start,end))
            _require(execution_count<=MAX_EXECUTION_BARS,'execution_bar_budget_exceeded:'+str(execution_count))
            _require(event_count<=MAX_DERIVED_EVENTS,'derived_event_budget_exceeded:'+str(event_count))
            raw_sources=sources.copy_to(staging) if archived else sources
            ages=sorted(row['risk']['account_equity_component_source']['source_age_seconds'] for row in inputs
                if row['risk'].get('account_equity_component_source',{} ) is not None
                and row['risk'].get('account_equity_component_source',{}).get('model')=='ledger_sample_120s_v1')
            distribution=dict(count=len(ages),min_seconds=ages[0] if ages else None,
                median_seconds=ages[len(ages)//2] if ages else None,
                p95_seconds=ages[max(0,(len(ages)*95+99)//100-1)] if ages else None,
                max_seconds=ages[-1] if ages else None)
            files={'hrs_replay_state.json':bootstrap,'hrs_state_tables.json':tables,
                'config_hashes.json':dict(strategy_id='hrs',as_of=_iso(end),**bootstrap['config_hashes']),
                'universe_slices.json':[dict(strategy_id='hrs',valid_from=_iso(start),valid_to=_iso(end+timedelta(microseconds=1)),observed_at=_iso(start),symbols=symbols,intervals=['1h','4h'],source='decision_evidence',config_hash=compute_config_hash(bootstrap['config']))],
                'exchange_info.json':exchange,'funding_schedule_evidence.json':forecasts,
                'hrs_export_receipt.json':dict(version=VERSION,boundary_resolution=boundary,end_boundary_resolution=end_boundary,qualification_available_at=terminal['available_at'],source_age_distribution=distribution,execution_budget=dict(one_minute_bars=execution_count,total_derived_events=event_count,max_execution_bars=MAX_EXECUTION_BARS,max_derived_events=MAX_DERIVED_EVENTS),mark_synchrony=False,exact_exchange_parity=False,qualification_limitations=['ledger_sample_120s_v1 preserves asynchronous source points; optimistic differences alone do not establish performance qualification.','The replay engine materializes derived bars/events; declared count budgets do not establish constant RAM for arbitrary universes.'],bootstrap_available_at=_iso(available),capture_coverage=list(coverage),source_attested=False,
                    decision_payload_hashes=raw_sources['evidence_archive'] if archived else {r['evidence_id']:r['payload_sha256'] for r in records},equity_payload_hashes=None if archived else [e['sha256'] for e in equities]),
                'raw_hrs_evidence.json':raw_sources}
            for name,value in files.items():_write(staging/name,value)
            for name,value in [('hrs_decision_inputs.jsonl',inputs),('hrs_decision_calendar.jsonl',calendar),('hrs_cash_flows.jsonl',flows),('funding_rates.jsonl',funding),('trade_signals.jsonl',[]),('market_snapshots.jsonl',[])]:_write(staging/name,value,jsonl=True)
            for (symbol,interval),rows in bars.items():_write(staging/f'klines/{symbol}_{interval}.jsonl',rows,jsonl=True)
            # The temporary seal and real engine preflight precede publication.
            # A missing execution fact must never leave an apparently usable
            # manifest in the requested destination.
            provisional=seal_snapshot(staging,staging/'validation','hrs',end,start,end)
            _require(provisional.get('status')=='SEALED','provisional_seal_failed:'+str(provisional.get('gaps')))
            _preflight(provisional['snapshot_dir'],start,end,tables)
            result=seal_snapshot(provisional['snapshot_dir'],output,'hrs',end,start,end)
        if result.get('status')!='SEALED':
            raise DataBlocked(result.get('gaps',['snapshot_seal_failed']))
        return dict(result,version=VERSION,performance_qualified=False)
    except (DataBlocked,FutureDataAccess,LedgerNotReady,KeyError,TypeError,ValueError,ArithmeticError,IndexError) as error:
        gaps=list(error.gaps) if isinstance(error,DataBlocked) else ['raw_evidence_invalid:'+str(error)]
        output.mkdir(parents=True,exist_ok=True)
        path=output/('hrs_export_gaps_'+start.strftime('%Y%m%dT%H%M%SZ')+'_'+end.strftime('%Y%m%dT%H%M%SZ')+'.json')
        result=dict(decision='BLOCKED_DATA',version=VERSION,source_provenance='unverified',source_attested=False,window_start=_iso(start),as_of=_iso(end),gaps=gaps,gap_bundle=str(path),performance_qualified=False,source_inventory={key:len(value) for key,value in sources.items() if isinstance(value,list)},source_artifact=sources.get('source_artifact'))
        _write(path,result)
        return result
    finally:
        cleanup.close()


def _preflight(directory,start,end,tables):
    from .replay.adapters.hrs import HrsAdapter
    from .replay.broker import VirtualBroker,SymbolFilters
    from .replay.engine import run_replay
    portal=DataPortal(directory,'hrs',end,start)
    initial=portal.hrs_replay_state(start);config=initial['config']
    filters={symbol:SymbolFilters(**{key:value for key,value in portal.symbol_filters(symbol).items() if key in ('tick_size','step_size','min_notional')}) for symbol in portal.universe().symbols}
    broker=VirtualBroker(filters,Decimal(0),Decimal(0),Decimal(0),Decimal(0),Decimal(str(initial['ledger']['history'][-1]['equity'])),Decimal(str(config.get('trading',{}).get('leverage',2))),Decimal(1))
    run_replay(portal,HrsAdapter(lambda at:tables),broker)


def _market_inventory(directory):
    root=Path(directory)
    _require(root.is_dir() and not _has_symlink_ancestor(root),'raw_market_export_path_invalid')
    manifest=json.loads((root/'raw_manifest.json').read_text())
    _require(manifest.get('strategy_id')=='hrs','raw_market_export_strategy_mismatch')
    selected={name:query for name,query in manifest['queries'].items()
        if re.fullmatch(r'btc_eth\.kline_([a-z0-9]{1,32})_(1m|1h|4h)',name)}
    return root,selected


def iter_market_export(directory):
    """Read only kline rows from the existing raw_export hash inventory.

    Its BLOCKED_DATA label is expected: raw_export alone never qualified HRS.
    Timestamp bindings were applied by that exporter; ambiguous naive rows still
    fail this converter. Existing historical-revision limitations remain visible.
    """
    root,queries=_market_inventory(directory)
    for name,query in queries.items():
        match=re.fullmatch(r'btc_eth\.kline_([a-z0-9]{1,32})_(1m|1h|4h)',name)
        if not match:continue
        relative=Path(query['file'])
        _require(not relative.is_absolute() and '..' not in relative.parts and len(relative.parts)==1,'raw_market_export_path_escape')
        path=root/relative
        _require(not _has_symlink_ancestor(path) and path.is_file() and sha256_file(str(path))==query['sha256'],'raw_market_export_hash_mismatch:'+name)
        rows=[json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        _require(len(rows)==query['row_count'] and len(rows)<=MAX_ROWS,'raw_market_export_row_count_mismatch')
        yield dict(symbol=match[1].upper(),interval=match[2],source='postgres_klines',rows=rows,
            payload_sha256=canonical_payload(rows)[1],raw_export_query=query)


def read_market_export(directory):
    return list(iter_market_export(directory))


def _archive_market_source(writer,directory,start,end):
    _root,queries=_market_inventory(directory)
    # Rows are independently counted by raw_export's retained query inventory.
    writer.declare_source('market_bars',dict(expected_count=len(queries),
        expected_item_count=sum(q['row_count'] for q in queries.values()),item_field='rows',
        query_scope=dict(source='hash_bound_raw_export',queries=queries),account_id=None,
        window_start=min((q.get('min_time',q.get('window_start',_iso(start-timedelta(days=31)))) for q in queries.values()),default=_iso(start)),
        window_end=max((q.get('max_time',q.get('as_of',_iso(end))) for q in queries.values()),default=_iso(end))))
    for batch in iter_market_export(directory):writer.write_row('market_bars',batch)


async def _stream_market_tables(connection,writer,pairs,start,end,timezones):
    """Reuse raw_export's guarded identifiers and explicit naive-time bindings.

    The archive counts source items independently, then emits <=1000-bar
    receipts. Neither the legacy 64-pair nor whole-export byte cap is reused.
    """
    from .replay.raw_export import _query_specs,_zone_for,_select_sql_and_args,_canonical_rows,_as_utc,SQL_VERSION
    pairs=tuple(pairs)
    _require(0<len(pairs)<=192 and all(interval in ('1m','1h','4h') for _,interval in pairs),'archive_market_pairs_invalid')
    lower=start-timedelta(hours=240)
    specs=[spec for spec in _query_specs('hrs',pairs) if spec[0]=='btc_eth']
    prepared=[];expected=items=0
    first,last=lower,end
    for schema,table,time_col,columns,cutoffs,sorting in specs:
        name=schema+'.'+table
        meta=await connection.fetch('SELECT column_name,data_type FROM information_schema.columns WHERE table_schema=$1 AND table_name=$2',schema,table)
        types={r['column_name']:r['data_type'] for r in meta}
        _require(set(columns)<=set(types),'archive_market_columns_missing:'+name)
        zones={column:_zone_for(name,column,types[column],timezones) for column in (time_col,*cutoffs)}
        sql,args=_select_sql_and_args(name,time_col,columns,cutoffs,sorting,types,zones,lower,end)
        sql=re.sub(r'\s+LIMIT\s+\$\d+\s*$','',sql,flags=re.IGNORECASE);args=args[:-1]
        inventory=await connection.fetchrow('SELECT count(*) AS expected_count,min('+time_col+') AS first_event,max('+time_col+') AS last_event FROM ('+sql+') AS source_inventory',*args)
        count=inventory['expected_count']
        _require(count>0,'archive_market_source_empty:'+name)
        first=min(first,_as_utc(inventory['first_event'],zones[time_col]));last=max(last,_as_utc(inventory['last_event'],zones[time_col]))
        expected+=(count+999)//1000;items+=count
        prepared.append((name,time_col,columns,cutoffs,sorting,zones,sql,args,count))
    writer.declare_source('market_bars',dict(expected_count=expected,expected_item_count=items,item_field='rows',
        query_scope=dict(sql_version=SQL_VERSION,queries=[dict(table=p[0],sql=p[6],arguments=p[7],expected_items=p[8]) for p in prepared],timezone_bindings=timezones),
        account_id=None,window_start=_iso(first),window_end=_iso(last)))
    for name,time_col,columns,cutoffs,sorting,zones,sql,args,count in prepared:
        symbol,interval=re.fullmatch(r'btc_eth\.kline_([a-z0-9]+)_(1m|1h|4h)',name).groups()
        buffer=[]
        def emit():
            rows=_canonical_rows(buffer,time_col,cutoffs,sorting,lower,end,zones,name)
            writer.write_row('market_bars',dict(symbol=symbol.upper(),interval=interval,source='postgres_klines',rows=rows,
                payload_sha256=canonical_payload(rows)[1],source_query=name))
        async for record in connection.cursor(sql,*args,prefetch=128):
            buffer.append({column:record[column] for column in columns})
            if len(buffer)==1000:emit();buffer=[]
        if buffer:emit()


def merge_market_archive(sources,directory,archive_root,*,window_start,as_of):
    from .replay.hrs_evidence_archive import EvidenceArchiveWriter
    with _ArchiveSources(sources) as original:
        _require('market_bars' not in original.archive.counts,'archive_market_source_already_present')
        with EvidenceArchiveWriter(archive_root,source_metadata={}) as writer:
            for family,metadata in original.archive.source_metadata.items():
                writer.declare_source(family,metadata)
                for row in original.archive.iter_records(family):writer.write_row(family,row)
            _archive_market_source(writer,directory,_time(window_start),_time(as_of)+timedelta(hours=1))
            return dict(sources,evidence_archive=writer.finish())


async def read_evidence_sources(connection,account_id,*,window_start,as_of,end_policy=None,archive_root=None,raw_market_export=None,market_pairs=None,timezone_bindings=None):
    """Bounded extraction on an acquired SELECT-only connection, one snapshot.

    No secret lookup, connection ownership, DDL or writes. The caller supplies an
    already authorized read-only connection. Market rows use read_market_export
    or captured public klines; arbitrary SQL/table names are never interpolated.
    """
    start,end=_time(window_start),_time(as_of)
    nominal_end=end
    _require(start<end and end-start<=timedelta(days=90),'invalid_or_excessive_window')
    if end_policy is not None:
        end+=timedelta(seconds=_scan_delay(end_policy))
    limit=MAX_ROWS+1
    specs=[('decision_evidence',"""SELECT evidence_id,run_id,kind,symbol,event_time,available_at,payload_sha256,payload
        FROM hrs.hrs_decision_evidence WHERE run_id IN (
          SELECT run_id FROM hrs.hrs_decision_evidence WHERE kind='scan_start'
          AND event_time >= $1 AND event_time <= $2
          AND payload->'financial_context'->'entry_policy'->>'account_id'=$3)
        OR (kind='public_market_response' AND event_time >= $1 AND event_time <= $2)
        ORDER BY available_at,evidence_id LIMIT $4""",(start,end,account_id,limit)),
        ('equity_evidence',"""SELECT account_id,evidence_sha256,segment_id,as_of,available_at,payload
        FROM trading.hrs_equity_evidence WHERE account_id=$1 AND as_of >= $2 AND as_of <= $3
        ORDER BY available_at,evidence_sha256 LIMIT $4""",(account_id,start-timedelta(days=31),end,limit)),
        ('equity_checkpoints',"""SELECT segment_id,as_of,equity,units,nav,created_at FROM trading.hrs_risk_equity_checkpoints
        WHERE account_id=$1 AND as_of >= $2 AND as_of <= $3 ORDER BY as_of LIMIT $4""",(account_id,start-timedelta(days=31),end,limit)),
        ('funding_observations',"""SELECT symbol,source_time,received_at,next_funding_time,funding_interval_hours,payload_hash
        FROM public.ai_tuner_funding_schedule_observations WHERE received_at >= $1 AND received_at <= $2
        ORDER BY received_at,symbol LIMIT $3""",(start-timedelta(seconds=420),end,limit))]
    result={'source_query_version':VERSION,'source_account_id':account_id,'source_window_start':_iso(start),'source_as_of':_iso(end)}
    estimated=0;referenced=set();ledger_points=set();scanned=set();forecast_cutoffs={}
    with ExitStack() as cleanup:
        writer=None
        if archive_root is not None:
            from .replay.hrs_evidence_archive import EvidenceArchiveWriter
            writer=cleanup.enter_context(EvidenceArchiveWriter(archive_root,source_metadata={}))
        async with connection.transaction(readonly=True,isolation='repeatable_read'):
            for name,query,args in specs:
                if name=='equity_evidence':
                    query='''SELECT account_id,evidence_sha256,segment_id,as_of,available_at,payload
                        FROM trading.hrs_equity_evidence WHERE account_id=$1 AND
                        (evidence_sha256=ANY($2::text[]) OR as_of=ANY($3::timestamptz[])
                         OR (as_of >= $4 AND as_of <= $5)) ORDER BY available_at,evidence_sha256 LIMIT $6'''
                    args=(account_id,sorted(referenced),sorted(ledger_points),nominal_end,end+timedelta(seconds=120),limit)
                elif name=='funding_observations':
                    query='''SELECT symbol,source_time,received_at,next_funding_time,funding_interval_hours,payload_hash
                        FROM public.ai_tuner_funding_schedule_observations WHERE received_at >= $1 AND received_at <= $2
                        AND symbol=ANY($3::text[]) ORDER BY received_at,symbol LIMIT $4'''
                    args=(start-timedelta(seconds=420),end,sorted(scanned),limit)
                if writer is not None:
                    streamed_query=re.sub(r'\s+LIMIT\s+\$\d+\s*$','',query,flags=re.IGNORECASE)
                    streamed_args=args[:-1]
                    event_column={'decision_evidence':'event_time','equity_evidence':'as_of','equity_checkpoints':'as_of','funding_observations':'source_time'}[name]
                    inventory=await connection.fetchrow('SELECT count(*) AS expected_count,min('+event_column+') AS first_event,max('+event_column+') AS last_event FROM ('+streamed_query+') AS source_inventory',*streamed_args)
                    writer.declare_source(name,dict(expected_count=inventory['expected_count'],query_scope=dict(sql=streamed_query,arguments=streamed_args),
                        account_id=account_id,window_start=_iso(inventory['first_event'] or start),window_end=_iso(inventory['last_event'] or end),
                        requested_window_start=_iso(start),requested_window_end=_iso(end)))
                    async for source_row in connection.cursor(streamed_query,*streamed_args,prefetch=128):
                        row=dict(source_row)
                        if name in ('decision_evidence','equity_evidence'):
                            row['payload']=_decoded(row['payload'])
                        writer.write_row(name,row)
                        if name=='decision_evidence':
                            _source_references(row,referenced,ledger_points,scanned,forecast_cutoffs,account_id)
                        elif name=='equity_evidence':
                            _source_forecast_ref(row,forecast_cutoffs,account_id)
                    continue
                rows=[dict(row) for row in await connection.fetch(query,*args)]
                _require(len(rows)<=MAX_ROWS,'source_row_limit:'+name)
                # The same canonical encoder used by the recorder preserves values
                # and awareness while preventing credentials from entering an export.
                encoded=canonical_payload(rows)[0]
                estimated+=len(encoded.encode())
                _require(estimated<=MAX_BYTES,'source_byte_limit')
                result[name]=json.loads(encoded)
                if name=='decision_evidence':
                    for row in rows:
                        _source_references(row,referenced,ledger_points,scanned,forecast_cutoffs,account_id)
                elif name=='equity_evidence':
                    for row in rows:_source_forecast_ref(row,forecast_cutoffs,account_id)
            if forecast_cutoffs:
                from .hrs_forecast_sources import FAMILY,INVENTORY_VERSION
                from strategies.hrs.funding_reference import ROWS_SQL
                cutoff=forecast_cutoffs[account_id]
                inventory=await connection.fetchrow('SELECT count(*) AS expected_count,min(available_at) AS first_event,max(available_at) AS last_event FROM ('+ROWS_SQL+') AS source_inventory',account_id,cutoff)
                prefix=dict(version=INVENTORY_VERSION,account_id=account_id,available_through=cutoff.isoformat(),expected_count=inventory['expected_count'])
                if writer is not None:
                    writer.declare_source(FAMILY,dict(expected_count=inventory['expected_count'],
                        account_id=account_id,query_scope=dict(sql=ROWS_SQL,arguments=[account_id,cutoff]),
                        window_start=_iso(inventory['first_event'] or cutoff),window_end=_iso(inventory['last_event'] or cutoff),prefix_inventory=prefix))
                    async for original in connection.cursor(ROWS_SQL,account_id,cutoff,prefetch=128):
                        row=dict(original);row['payload']=_decoded(row['payload']);writer.write_row(FAMILY,row)
                else:
                    rows=[dict(row) for row in await connection.fetch(ROWS_SQL+' LIMIT $3',account_id,cutoff,limit)]
                    _require(len(rows)<=MAX_ROWS,'source_row_limit:'+FAMILY)
                    encoded=canonical_payload(rows)[0];estimated+=len(encoded.encode())
                    _require(estimated<=MAX_BYTES,'source_byte_limit')
                    result[FAMILY]=json.loads(encoded);result[FAMILY+'_inventory']=prefix
            if writer is not None:
                if market_pairs is not None:
                    _require(raw_market_export is None,'archive_market_source_duplicate')
                    pairs=tuple((symbol,interval) for symbol in sorted(scanned) for interval in ('1m','1h','4h')) if market_pairs=='scanned' else market_pairs
                    await _stream_market_tables(connection,writer,pairs,start,end,timezone_bindings or {})
                if raw_market_export is not None:_archive_market_source(writer,raw_market_export,start,end)
        if writer is not None:
            result['evidence_archive']=writer.finish()
        elif raw_market_export is not None:result['market_bars']=read_market_export(raw_market_export)
    return result


def _source_forecast_ref(parent,cutoffs,account_id):
    if 'payload' not in parent:return
    from .hrs_forecast_sources import parent_reference
    found=parent_reference(parent,account_id=account_id)
    if found is not None:
        account,cutoff,_ref=found;cutoffs[account]=max(cutoffs.get(account,cutoff),cutoff)


def _source_references(row,referenced,ledger_points,scanned,forecast_cutoffs=None,account_id=None):
    payload=_decoded(row['payload'])
    _require(row.get('payload_sha256')==canonical_payload(payload)[1],'decision_payload_hash_mismatch')
    if row['kind'] in ('standard_scan_scope','lv_rm_scan_scope'):
        scanned.update(payload['symbols'])
    financials=[payload.get('financial_context',{})]
    if row['kind']=='scan_end':financials.append(payload.get('state',{}).get('financial_context',{}))
    for financial in financials:
        referenced.update(r['evidence_sha256'] for r in financial.get('equity_source_archives',[]))
        if forecast_cutoffs is not None:
            for parent in financial.get('equity_source_archives',[]):_source_forecast_ref(parent,forecast_cutoffs,account_id)
        component=financial.get('hrs_marked_equity_observation')
        if component:referenced.add(component['evidence_sha256'])
        raw=financial.get('ledger_state')
        if raw:ledger_points.add(_time(_decoded(raw['ledger_state'])['as_of']))
        for reservation in financial.get('reservations',[]):
            geometry=_decoded(reservation.get('geometry') or {})
            proof=geometry.get('slot_occupancy_proof')
            if proof:referenced.add(proof['equity_evidence_sha256'])
    if row['kind']=='entry_stage_inputs' and payload.get('stage')=='reservation_denial':
        context=payload.get('risk_context') or {}
        if context.get('evidence_sha256'):referenced.add(context['evidence_sha256'])


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources',type=Path,help='Raw captured source bundle; absent history emits BLOCKED_DATA')
    parser.add_argument('--raw-market-export',type=Path,help='Existing replay.raw_export output containing actual 1m/1h/4h rows')
    parser.add_argument('--output-root',type=Path,required=True)
    parser.add_argument('--window-start',required=True)
    parser.add_argument('--as-of',required=True)
    parser.add_argument('--participation-rate',help='Explicit hrs_1m_volume_v1 base-volume participation assumption')
    parser.add_argument('--monitor-cadence-seconds',type=int)
    parser.add_argument('--monitor-anchor')
    parser.add_argument('--allow-inception-segment',action='store_true')
    parser.add_argument('--first-scan-max-delay-seconds',type=int,help='Preregistered first-scan lower-bound policy, at most 3600 seconds')
    parser.add_argument('--end-first-scan-max-delay-seconds',type=int,help='Preregistered terminal scan lower-bound policy; requires source facts through that actual scan')
    args=parser.parse_args(argv)
    if args.sources and args.sources.stat().st_size>MAX_BYTES:parser.error('source_byte_limit')
    sources=json.loads(args.sources.read_text()) if args.sources else {}
    if sources.get('version')=='hrs-evidence-archive-v2':
        sources=dict(evidence_archive=dict(path=str(args.sources.resolve()),manifest_sha256=sha256_file(str(args.sources))))
    elif sources.get('evidence_archive') and args.sources:
        descriptor=sources['evidence_archive']
        if not Path(descriptor['path']).is_absolute():
            descriptor['path']=str((args.sources.parent/descriptor['path']).resolve())
    if args.sources:sources['source_artifact']=dict(path=str(args.sources),sha256=sha256_file(str(args.sources)))
    if args.raw_market_export and not sources.get('evidence_archive'):
        sources.setdefault('market_bars',[]).extend(read_market_export(args.raw_market_export))
    execution=(dict(version='hrs_1m_volume_v1',participation_rate=args.participation_rate,timeout_minutes=15) if args.participation_rate else None)
    monitor=(dict(version='hrs_cycle_monitor_v1',cadence_seconds=args.monitor_cadence_seconds,anchor_at=args.monitor_anchor) if args.monitor_cadence_seconds and args.monitor_anchor else None)
    policy=(dict(version='first_scan_start_v1',max_delay_seconds=args.first_scan_max_delay_seconds)
        if args.first_scan_max_delay_seconds is not None else None)
    end_policy=(dict(version='first_scan_start_v1',max_delay_seconds=args.end_first_scan_max_delay_seconds)
        if args.end_first_scan_max_delay_seconds is not None else None)
    with ExitStack() as cleanup:
        if args.raw_market_export and sources.get('evidence_archive'):
            combined=Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix='hrs-merged-archive-'))).resolve()/'archive'
            sources=merge_market_archive(sources,args.raw_market_export,combined,window_start=args.window_start,as_of=args.as_of)
        result=export_evidence_bundle(sources,args.output_root,window_start=_time(args.window_start),as_of=_time(args.as_of),execution_model=execution,monitoring_model=monitor,allow_inception_segment=args.allow_inception_segment,start_policy=policy,end_policy=end_policy)
    print(canonical_payload(result)[0])
    return 0 if result.get('status')=='SEALED' else 2


if __name__=='__main__':
    raise SystemExit(main())
