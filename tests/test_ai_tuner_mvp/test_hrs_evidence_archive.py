"""Offline receipt archives preserve original hashes and bounded streaming reads."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from strategies.hrs.decision_capture import canonical_payload

AT = datetime(2026, 11, 17, tzinfo=timezone.utc)


def receipt(index, payload=None, *, at=None, run='cycle'):
    payload = {'value': 1} if payload is None else payload
    body, digest = canonical_payload(payload)
    instant = at or AT + timedelta(seconds=index)
    return dict(evidence_id=f'row-{index}', run_id=run, kind='scan_start', symbol='BTCUSDT',
        event_time=instant.isoformat(), available_at=(instant+timedelta(microseconds=17)).isoformat(),
        payload_sha256=digest, payload=json.loads(body), extra_envelope_field={'retained': True})


def declaration(count, **extra):
    return dict(expected_count=count, query_scope={'table':'hrs.hrs_decision_evidence','predicate':'bounded_fixture'},
        account_id='default', window_start=AT.isoformat(), window_end=(AT+timedelta(days=2)).isoformat(), **extra)


def test_original_envelopes_roundtrip_and_cross_shard_earliest_is_not_skipped(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter, EvidenceArchive, ArchiveLimits
    original = [receipt(30), receipt(10), receipt(20)]
    with EvidenceArchiveWriter(tmp_path/'archive', source_metadata={'decision_evidence':declaration(3)},
                               limits=ArchiveLimits(shard_receipts=1)) as writer:
        for row in original:writer.write_row('decision_evidence',row)
        descriptor=writer.finish()
    assert Path(descriptor['path']).name=='hrs_evidence_archive_inventory.json'
    with EvidenceArchive.open(descriptor['path'],expected_manifest_sha256=descriptor['manifest_sha256']) as archive:
        assert archive.counts=={'decision_evidence':3}
        assert list(archive.iter_records('decision_evidence'))==[original[1],original[2],original[0]]
        assert archive.get_record('decision_evidence','row-20')==original[2]
        assert list(archive.iter_runs())==['cycle']
        assert list(archive.iter_records('decision_evidence',event_start=AT+timedelta(seconds=15),event_end=AT+timedelta(seconds=25)))==[original[2]]
        assert all(path.is_file() and path.suffix!='.sqlite' for path in archive.files())
        assert len({path.name for path in archive.files()})==len(list(archive.files()))


def test_late_declaration_count_mismatch_and_conflicting_identity_never_seal(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter, ArchiveError
    with EvidenceArchiveWriter(tmp_path/'count',source_metadata={}) as writer:
        writer.declare_source('decision_evidence',declaration(2))
        writer.write_row('decision_evidence',receipt(1))
        with pytest.raises(ArchiveError,match='count'):writer.finish()
    assert not (tmp_path/'count'/'hrs_evidence_archive_inventory.json').exists()
    with EvidenceArchiveWriter(tmp_path/'conflict',source_metadata={'decision_evidence':declaration(2)}) as writer:
        writer.write_row('decision_evidence',receipt(1))
        with pytest.raises(ArchiveError,match='identity'):writer.write_row('decision_evidence',receipt(1,{'value':2}))
        with pytest.raises(ArchiveError):writer.finish()
    assert not (tmp_path/'conflict'/'hrs_evidence_archive_inventory.json').exists()


def test_same_body_different_receipt_clocks_remains_distinct_and_hash_exact(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter, EvidenceArchive
    rows=[receipt(1,{'symbols':['BTCUSDT','ETHUSDT'],'bars':[{'open':'1','close':'2'}]}),
          receipt(2,{'symbols':['BTCUSDT','ETHUSDT'],'bars':[{'open':'1','close':'2'}]})]
    with EvidenceArchiveWriter(tmp_path/'archive',source_metadata={'decision_evidence':declaration(2)}) as writer:
        for row in rows:writer.write_row('decision_evidence',row)
        result=writer.finish()
    with EvidenceArchive.open(result['path']) as archive:
        rebuilt=list(archive.iter_records('decision_evidence'))
        assert rebuilt==rows
        assert [canonical_payload(row)[1] for row in rebuilt]==[canonical_payload(row)[1] for row in rows]
        assert rebuilt[0]['available_at']!=rebuilt[1]['available_at']


def test_more_than_legacy_receipt_cap_streams_without_flattening(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter, EvidenceArchive
    count=50_001
    with EvidenceArchiveWriter(tmp_path/'many',source_metadata={'decision_evidence':declaration(count)}) as writer:
        for index in range(count):writer.write_row('decision_evidence',receipt(index))
        result=writer.finish()
    with EvidenceArchive.open(result['path']) as archive:
        assert sum(1 for _ in archive.iter_records('decision_evidence'))==count
        parts=[entry for entry in archive.manifest['files'] if entry['kind']=='receipts']
        assert len(parts)==2 and max(p['row_count'] for p in parts)<=50_000
        assert archive.get_record('decision_evidence','row-50000')['payload']=={'value':1}


def test_more_than_legacy_logical_byte_cap_uses_recursive_cas_and_bounded_shards(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter, EvidenceArchive
    payload={'symbols':[f'ASSET{i:05d}_'+('x'*120) for i in range(16_000)],'bars':[{'open':'1','close':'2'}]*20}
    count=35
    with EvidenceArchiveWriter(tmp_path/'large',source_metadata={'decision_evidence':declaration(count)}) as writer:
        for index in range(count):writer.write_row('decision_evidence',receipt(index,payload))
        result=writer.finish()
    assert result['logical_bytes']>64*1024*1024
    assert result['physical_bytes']<result['logical_bytes']//4
    with EvidenceArchive.open(result['path']) as archive:
        parts=[entry for entry in archive.manifest['files'] if entry['kind']=='receipts']
        assert len(parts)>1 and max(p['logical_bytes'] for p in parts)<=64*1024*1024
        assert archive.get_record('decision_evidence','row-34')['payload']==payload


@pytest.mark.parametrize('setting,value',[('max_receipts',1),('max_logical_bytes',1),('max_physical_bytes',1024),('max_shards',1)])
def test_hard_budgets_refuse_seal(tmp_path,setting,value):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter, ArchiveLimits, ArchiveError
    target=tmp_path/setting
    with pytest.raises(ArchiveError,match='budget|limit'):
        with EvidenceArchiveWriter(target,source_metadata={'decision_evidence':declaration(2)},limits=ArchiveLimits(**{setting:value},shard_receipts=1)) as writer:
            writer.write_row('decision_evidence',receipt(1))
            writer.write_row('decision_evidence',receipt(2))
            writer.finish()
    assert not (target/'hrs_evidence_archive_inventory.json').exists()


def _small_archive(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter, ArchiveLimits
    with EvidenceArchiveWriter(tmp_path/'archive',source_metadata={'decision_evidence':declaration(2)},limits=ArchiveLimits(shard_receipts=1)) as writer:
        writer.write_row('decision_evidence',receipt(1));writer.write_row('decision_evidence',receipt(2))
        return writer.finish()


@pytest.mark.parametrize('damage',['missing','file_tamper','count','first_key','duplicate_receipt','cas_body'])
def test_inventory_shard_and_cas_closure_reject_damage(tmp_path,damage):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchive, ArchiveError
    result=_small_archive(tmp_path)
    inventory=Path(result['path']);manifest=json.loads(inventory.read_text())
    shards=[entry for entry in manifest['files'] if entry['kind']=='receipts']
    if damage=='missing':(inventory.parent/shards[0]['path']).unlink()
    elif damage=='file_tamper':
        path=inventory.parent/shards[0]['path'];path.write_bytes(path.read_bytes()+b' ')
    elif damage=='count':manifest['source_metadata']['decision_evidence']['expected_count']+=1
    elif damage=='first_key':manifest['families']['decision_evidence']['first_key']=['wrong']
    elif damage=='duplicate_receipt':
        first=inventory.parent/shards[0]['path'];second=inventory.parent/shards[1]['path']
        second.write_bytes(first.read_bytes());shards[1]['sha256']=hashlib.sha256(second.read_bytes()).hexdigest();shards[1]['physical_bytes']=second.stat().st_size
    elif damage=='cas_body':
        entry=next(e for e in manifest['files'] if e['kind']=='objects')
        path=inventory.parent/entry['path'];lines=path.read_text().splitlines()
        for index,line in enumerate(lines):
            value=json.loads(line)
            if value['n'][0]=='d' and value['n'][1] and value['n'][1][0][1]==[0,True]:
                value['n'][1][0][1]=[0,False];lines[index]=json.dumps(value,separators=(',',':'),sort_keys=True);break
        else:raise AssertionError('expected shared boolean subtree')
        path.write_text('\n'.join(lines)+'\n');entry['sha256']=hashlib.sha256(path.read_bytes()).hexdigest();entry['physical_bytes']=path.stat().st_size
    inventory.write_text(json.dumps(manifest,separators=(',',':'),sort_keys=True))
    with pytest.raises(ArchiveError):EvidenceArchive.open(inventory)


def test_legacy_payload_hashes_and_equity_identity_are_preserved(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter, EvidenceArchive
    from strategies.hrs.equity_evidence import canonical
    body={'as_of':AT.isoformat(),'available_at':AT.isoformat(),'reason':'来源'}
    row=dict(evidence_sha256=canonical(body)[1],as_of=AT.isoformat(),available_at=AT.isoformat(),payload=body)
    bars=[{'open_time':AT.isoformat(),'close':'2'}]
    batch=dict(symbol='BTCUSDT',interval='1m',source='postgres_klines',rows=bars,payload_sha256=canonical_payload(bars)[1])
    meta={'equity_evidence':declaration(1),'market_bars':declaration(1,expected_item_count=1,item_field='rows')}
    with EvidenceArchiveWriter(tmp_path/'legacy',source_metadata=meta) as writer:
        writer.write_row('equity_evidence',row);writer.write_row('market_bars',batch);result=writer.finish()
    with EvidenceArchive.open(result['path']) as archive:
        assert archive.get_record('equity_evidence',row['evidence_sha256'])==row
        assert list(archive.iter_records('market_bars'))==[batch]
        assert canonical(archive.get_record('equity_evidence',row['evidence_sha256'])['payload'])[1]==row['evidence_sha256']


def test_invalid_payload_hash_and_nested_source_count_never_seal(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter, ArchiveError
    row=receipt(1);row['payload']['value']=9
    with EvidenceArchiveWriter(tmp_path/'hash',source_metadata={'decision_evidence':declaration(1)}) as writer:
        with pytest.raises(ArchiveError,match='hash'):writer.write_row('decision_evidence',row)
        with pytest.raises(ArchiveError):writer.finish()
    batch=dict(symbol='BTCUSDT',interval='1m',source='postgres_klines',rows=[{'open_time':AT.isoformat()}])
    with EvidenceArchiveWriter(tmp_path/'count',source_metadata={'market_bars':declaration(1,expected_item_count=2,item_field='rows')}) as writer:
        writer.write_row('market_bars',batch)
        with pytest.raises(ArchiveError,match='count'):writer.finish()


def test_same_bar_batch_at_different_receipt_times_is_not_collapsed(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter, EvidenceArchive
    bars=[{'open_time':AT.isoformat(),'close':'2'}]
    common=dict(symbol='BTCUSDT',interval='1m',source='postgres_klines',rows=bars,payload_sha256=canonical_payload(bars)[1])
    rows=[dict(common,available_at=(AT+timedelta(minutes=i)).isoformat()) for i in (1,2)]
    with EvidenceArchiveWriter(tmp_path/'bars',source_metadata={'market_bars':declaration(2,expected_item_count=2,item_field='rows')}) as writer:
        for row in rows:writer.write_row('market_bars',row)
        result=writer.finish()
    with EvidenceArchive.open(result['path']) as archive:
        assert list(archive.iter_records('market_bars',path='1m'))==rows


def test_preseal_file_tamper_and_unreachable_objects_refuse_closure(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter, EvidenceArchive, ArchiveError
    with EvidenceArchiveWriter(tmp_path/'preseal',source_metadata={'decision_evidence':declaration(1)}) as writer:
        writer.write_row('decision_evidence',receipt(1))
        writer._pack.flush()  # Inject a disk fault after bytes reached the file.
        path=next((tmp_path/'preseal').glob('hrs_archive_objects_*'))
        raw=bytearray(path.read_bytes());raw[0]=ord('[');path.write_bytes(raw)
        with pytest.raises(ArchiveError,match='changed_before_seal'):writer.finish()
    result=_small_archive(tmp_path)
    inventory=Path(result['path']);manifest=json.loads(inventory.read_text())
    entry=next(e for e in manifest['files'] if e['kind']=='objects')
    path=inventory.parent/entry['path'];value='unreachable'
    encoded=canonical_payload(dict(h=canonical_payload(value)[1],b=len(canonical_payload(value)[0].encode()),n=['s',value]))[0].encode()+b'\n'
    path.write_bytes(path.read_bytes()+encoded)
    entry['row_count']+=1;entry['physical_bytes']+=len(encoded);entry['last_key']=canonical_payload(value)[1]
    entry['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    manifest['object_count']+=1;manifest['physical_bytes']+=len(encoded)
    inventory.write_text(json.dumps(manifest,separators=(',',':'),sort_keys=True))
    with pytest.raises(ArchiveError,match='unreachable'):EvidenceArchive.open(inventory)


def test_limits_cannot_raise_production_caps_and_expected_inventory_hash_is_bound(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchive, ArchiveLimits, ArchiveError, MAX_RECEIPTS
    with pytest.raises(ArchiveError,match='limit'):ArchiveLimits(max_receipts=MAX_RECEIPTS+1)
    result=_small_archive(tmp_path)
    with pytest.raises(ArchiveError,match='inventory_hash'):EvidenceArchive.open(result['path'],expected_manifest_sha256='0'*64)


def test_reader_rejects_cas_record_changed_after_open_even_when_body_is_unchanged(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchive, ArchiveError
    result=_small_archive(tmp_path)
    with EvidenceArchive.open(result['path']) as archive:
        entry=next(e for e in archive.manifest['files'] if e['kind']=='objects')
        path=archive.root/entry['path'];lines=path.read_text().splitlines()
        original=json.loads(lines[0]);original['b']+=1 if original['b']%10!=9 else -1
        replacement=json.dumps(original,sort_keys=True,separators=(',',':'))
        assert len(replacement)==len(lines[0])
        lines[0]=replacement;path.write_text('\n'.join(lines)+'\n')
        with pytest.raises(ArchiveError):archive.get_record('decision_evidence','row-1')


def test_appended_forecast_list_reuses_complete_merkle_prefix_blocks(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter, EvidenceArchive
    prefix=[{'source_time':AT.isoformat(),'symbol':f'SYM{i}','value':i} for i in range(1024)]
    rows=[receipt(1,{'forecasts':prefix}),receipt(2,{'forecasts':prefix+[{'source_time':AT.isoformat(),'symbol':'NEW','value':1024}]})]
    with EvidenceArchiveWriter(tmp_path/'prefix',source_metadata={'decision_evidence':declaration(2)}) as writer:
        for row in rows:writer.write_row('decision_evidence',row)
        result=writer.finish()
    with EvidenceArchive.open(result['path']) as archive:
        nodes=[]
        for entry in archive.manifest['files']:
            if entry['kind']=='objects':
                nodes.extend(json.loads(line)['n'] for line in (archive.root/entry['path']).read_text().splitlines())
        roots={node[1]['length']:node[1]['chunks'] for node in nodes if node[0]=='L'}
        assert roots[1025][:4]==roots[1024]  # Only the tail and small top node change.
        assert list(archive.iter_records('decision_evidence'))==rows


def test_local_disk_margin_blocks_before_any_inventory_is_sealed(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import ai_tuner.replay.hrs_evidence_archive as module
    monkeypatch.setattr(module.shutil,'disk_usage',lambda path:SimpleNamespace(free=module.MIN_FREE_DISK_BYTES-1))
    with pytest.raises(module.ArchiveError,match='disk'):
        with module.EvidenceArchiveWriter(tmp_path/'lowdisk',source_metadata={'decision_evidence':declaration(1)}) as writer:
            writer.write_row('decision_evidence',receipt(1));writer.finish()
    assert not (tmp_path/'lowdisk'/module.INVENTORY_NAME).exists()


def test_available_order_is_streamed_independently_of_event_shard_order(tmp_path):
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter, EvidenceArchive, ArchiveLimits
    rows=[receipt(1),receipt(2),receipt(3)]
    rows[0]['available_at']=(AT+timedelta(seconds=30)).isoformat()
    rows[1]['available_at']=(AT+timedelta(seconds=10)).isoformat()
    rows[2]['available_at']=(AT+timedelta(seconds=10)).isoformat()
    with EvidenceArchiveWriter(tmp_path/'available',source_metadata={'decision_evidence':declaration(3)},
                               limits=ArchiveLimits(shard_receipts=1)) as writer:
        for row in reversed(rows):writer.write_row('decision_evidence',row)
        result=writer.finish()
    with EvidenceArchive.open(result['path']) as archive:
        assert list(archive.iter_records('decision_evidence'))==rows
        assert list(archive.iter_records('decision_evidence',order_by_available=True))==[rows[1],rows[2],rows[0]]
        assert list(archive.iter_records('decision_evidence',order_by_available=True,
            available_before=AT+timedelta(seconds=10)))==rows[1:]
