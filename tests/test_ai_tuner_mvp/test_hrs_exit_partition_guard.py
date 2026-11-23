"""Synthetic partition facts only; seals never establish source authenticity."""
import copy
import hashlib
import importlib
import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from ai_tuner import hrs_exit_experiment as experiment
from strategies.hrs.decision_capture import canonical_payload
from shared.config_loader import compute_config_hash
from .test_hrs_exit_experiment import registration
from .test_hrs_evidence_export import captured_sources, envelope, AT

ROOT = Path(__file__).resolve().parents[2]


def guard():
    return importlib.import_module('ai_tuner.hrs_exit_partition_guard')


def write_json(path, value):
    path.write_text(json.dumps(value))


def partition_registration(tmp_path):
    r = registration()
    paths = set(experiment.engine_identity()['source_files']) | {'services/market_cap/assets.json'}
    for path in sorted((ROOT/'strategies/hrs').glob('*.py')) + sorted((ROOT/'shared').glob('*.py')):
        if len(paths) == 60:
            break
        paths.add(path.relative_to(ROOT).as_posix())
    assert len(paths) == 60
    files = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sorted(paths)}
    r['prospective_runtime_partition'] = dict(expected_release_sha='e'*40, source_files=files,
        source_files_sha256=experiment.fingerprint(files), expected_market_cap_asset_count=len(json.loads((ROOT/'services/market_cap/assets.json').read_text())),
        market_cap_assets_sha256=files['services/market_cap/assets.json'], expected_runtime_loaded_config_hash=compute_config_hash({}))
    path = tmp_path/'registration.json'
    write_json(path, r)
    return r, path


def archive_bundle(tmp_path, mutate=None):
    """Use the real exporter and real archive verifier, with synthetic captures."""
    from ai_tuner.hrs_evidence_export import export_evidence_bundle
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchiveWriter
    sources, bootstrap = captured_sources(tmp_path)
    for row in sources['decision_evidence']:
        state = row['payload'] if row['kind'] == 'scan_start' else row['payload'].get('state')
        if state is not None:
            state['release_sha'] = 'e'*40
            row['payload_sha256'] = canonical_payload(row['payload'])[1]
    if mutate:
        mutate(sources)
    with EvidenceArchiveWriter(tmp_path/'archive', source_metadata={}) as writer:
        for family, rows in sources.items():
            writer.declare_source(family, dict(expected_count=len(rows), query_scope='independent synthetic cardinality',
                account_id='test', window_start=(AT-timedelta(days=31)).isoformat(), window_end=(AT+timedelta(days=3)).isoformat()))
            for row in rows:
                writer.write_row(family, row)
        descriptor = writer.finish()
    policy = dict(version='first_scan_start_v1', max_delay_seconds=3600)
    result = export_evidence_bundle(dict(evidence_archive=descriptor), tmp_path/'sealed',
        window_start=AT-timedelta(minutes=21), as_of=AT+timedelta(hours=1, minutes=45),
        start_policy=policy, end_policy=policy, execution_model=bootstrap['execution_model'],
        monitoring_model=bootstrap['monitoring_model'], allow_inception_segment=True)
    assert result.get('status') == 'SEALED', result
    r, path = partition_registration(tmp_path)
    r['baseline'].update(config=bootstrap['config'], effective_config_sha256=experiment.fingerprint(bootstrap['config']), config_hashes=bootstrap['config_hashes'])
    r['prospective_runtime_partition']['expected_runtime_loaded_config_hash'] = compute_config_hash(bootstrap['config'])
    r['windows'][0].update(start=(AT-timedelta(minutes=21)).isoformat(), end=(AT+timedelta(hours=1, minutes=45)).isoformat(),
        start_policy=policy, end_policy=policy, snapshot_dir=result['snapshot_dir'])
    r['windows'][1].update(start='2026-12-01T00:00:00+08:00', end='2027-01-01T00:00:00+08:00')
    r['windows'][2].update(start='2027-01-01T00:00:00+08:00', end='2027-02-01T00:00:00+08:00')
    write_json(path, r)
    return r, path, Path(result['snapshot_dir'])


def check(path, r, **kwargs):
    directory=r['windows'][0].get('snapshot_dir')
    if directory and 'expected_snapshot_manifest_sha256' not in kwargs:
        kwargs['expected_snapshot_manifest_sha256']=hashlib.sha256((Path(directory)/'manifest.json').read_bytes()).hexdigest()
    return guard().run_guard(path, expected_registration_sha256=experiment.fingerprint(r), **kwargs)


def test_registration_requires_external_pin_even_if_forgery_is_self_consistent(tmp_path):
    r, path = partition_registration(tmp_path)
    pin = experiment.fingerprint(r)
    r['prospective_runtime_partition']['expected_release_sha'] = 'f'*40
    write_json(path, r)
    result = guard().run_guard(path, expected_registration_sha256=pin)
    assert result['blocked_reasons'] == ['registration_external_pin_mismatch']
    assert result['engine_runs'] == 0


@pytest.mark.parametrize('damage', ['missing', 'changed', 'traversal', 'digest', 'release', 'count', 'registry'])
def test_partition_fails_closed_before_data_access(tmp_path, damage):
    r, path = partition_registration(tmp_path)
    p = r['prospective_runtime_partition']
    if damage == 'missing': p['source_files']['missing.py'] = p['source_files'].pop(next(iter(p['source_files'])))
    if damage == 'changed': p['source_files'][next(iter(p['source_files']))] = '0'*64
    if damage == 'traversal': p['source_files']['../escape.py'] = p['source_files'].pop(next(iter(p['source_files'])))
    if damage == 'release': p.pop('expected_release_sha')
    if damage == 'count': p['source_files'].pop(next(iter(p['source_files'])))
    if damage == 'registry': p['expected_market_cap_asset_count'] += 1
    if damage != 'digest': p['source_files_sha256'] = experiment.fingerprint(p['source_files'])
    else: p['source_files_sha256'] = '0'*64
    write_json(path, r)
    result = check(path, r)
    assert result['engine_runs'] == 0 and result['status'] == 'BLOCKED_DATA'
    assert result['blocked_reasons'] and not result['window_sources']


def test_alternate_prefix_cannot_verify_one_copy_and_execute_another(tmp_path):
    r, path = partition_registration(tmp_path)
    alternate = tmp_path/'alternate'
    for name in r['prospective_runtime_partition']['source_files']:
        target = alternate/name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT/name, target)
    result = check(path, r, source_root=alternate)
    assert any('import_source_path_mismatch' in reason for reason in result['blocked_reasons'])
    assert result['engine_runs'] == 0


def test_future_window_cannot_be_qualified_by_future_input_or_now_override(tmp_path):
    r, path = partition_registration(tmp_path)
    r['windows'][0].update(start='2099-09-01T00:00:00+08:00', end='2099-10-01T00:00:00+08:00')
    r['windows'][1].update(start='2099-10-01T00:00:00+08:00', end='2099-11-01T00:00:00+08:00')
    r['windows'][2].update(start='2099-11-01T00:00:00+08:00', end='2099-12-01T00:00:00+08:00')
    write_json(path, r)
    result = check(path, r, execute=True)
    assert 'research_window_not_elapsed' in result['blocked_reasons']
    assert result['engine_runs'] == 0
    with pytest.raises(TypeError): check(path, r, now='2100-01-01')


def test_verified_archive_binds_boundaries_and_never_claims_attestation(tmp_path):
    r, path, directory = archive_bundle(tmp_path)
    result = check(path, r)
    assert result['status'] == 'CHECKED_LOCAL', result
    assert result['engine_runs'] == 0 and result['source_attested'] is False
    assert result['deployment_facts_status'] == 'UNVERIFIED'
    assert result['official_run_ready'] is False
    source = result['window_sources']['research']
    assert source['envelope_sha256'] == json.loads((directory/'manifest.json').read_text())['envelope_sha256']
    assert source['boundary']['start_resolution']['selected_evidence_id']
    assert result['guard_sha256'] and result['engine_identity']['sha256']


@pytest.mark.parametrize('damage', ['opening_missing', 'opening_old', 'middle_old', 'terminal_missing', 'end_old'])
def test_missing_or_mixed_release_never_skips_first_scan(tmp_path, damage):
    def mutate(sources):
        for row in sources['decision_evidence']:
            state = row['payload'] if row['kind']=='scan_start' else row['payload'].get('state')
            if state is None: continue
            match = ((damage.startswith('opening') and row['run_id']=='bootstrap' and row['kind']=='scan_start') or
                (damage=='middle_old' and row['run_id']=='cycle-0' and row['kind']=='scan_start') or
                (damage=='terminal_missing' and row['run_id']=='cycle-1' and row['kind']=='scan_start') or
                (damage=='end_old' and row['run_id']=='cycle-0' and row['kind']=='scan_end'))
            if match:
                state['release_sha'] = None if 'missing' in damage else 'f'*40
                row['payload_sha256'] = canonical_payload(row['payload'])[1]
    r, path, _ = archive_bundle(tmp_path, mutate)
    result = check(path, r, execute=True)
    assert result['status'] == 'BLOCKED_DATA' and result['engine_runs'] == 0, result
    assert any('scan_release_mismatch' in reason for reason in result['blocked_reasons'])


def test_old_warmup_release_outside_trading_range_is_retained(tmp_path):
    def mutate(sources):
        at = AT-timedelta(days=1)
        sources['decision_evidence'] += [envelope('old','scan_start',dict(release_sha='f'*40), at),
            envelope('old','scan_end',dict(status='complete',state=dict(release_sha='f'*40)), at)]
    r, path, _ = archive_bundle(tmp_path, mutate)
    assert check(path, r)['status'] == 'CHECKED_LOCAL'


@pytest.mark.parametrize('damage', ['file', 'count', 'clock', 'prefix'])
def test_whole_original_archive_is_verified_including_out_of_window_receipts(tmp_path, damage):
    r, path, directory = archive_bundle(tmp_path)
    manifest = json.loads((directory/'manifest.json').read_text())
    raw = json.loads((directory/'raw_hrs_evidence.json').read_text())
    inventory = directory/raw['evidence_archive']['path']
    archive = json.loads(inventory.read_text())
    if damage=='file':
        target = inventory.parent/archive['files'][0]['path']
        target.write_bytes(target.read_bytes()+b' ')
    elif damage=='count':
        archive['source_metadata']['decision_evidence']['expected_count'] += 1
        write_json(inventory, archive)
    elif damage=='prefix':
        entry = next(e for e in archive['files'] if e['kind']=='receipts')
        target = inventory.parent/entry['path']
        target.write_bytes(target.read_bytes().splitlines(keepends=True)[0])
    else:
        raw['evidence_archive']['path'] = '../outside/inventory.json'
        write_json(directory/'raw_hrs_evidence.json', raw)
    # Deliberately reseal dataset hashes/envelope, so inner archive checks matter.
    from ai_tuner.replay.data_portal import compute_manifest_sha256, compute_manifest_envelope_sha256
    if damage in ('file','count','prefix'):
        raw['evidence_archive']['manifest_sha256'] = hashlib.sha256(inventory.read_bytes()).hexdigest()
        write_json(directory/'raw_hrs_evidence.json', raw)
    for name, entry in manifest['files'].items():
        entry['sha256'] = hashlib.sha256((directory/name).read_bytes()).hexdigest()
    manifest['sha256'] = compute_manifest_sha256(manifest['files'])
    manifest['envelope_sha256'] = compute_manifest_envelope_sha256(manifest)
    write_json(directory/'manifest.json', manifest)
    result = check(path, r, execute=True)
    assert result['status']=='BLOCKED_DATA' and result['engine_runs']==0, result


def test_future_original_availability_is_rejected(tmp_path):
    def mutate(sources):
        sources['decision_evidence'].append(envelope('late','public_market_response',dict(path='/synthetic', response=[]),AT))
        sources['decision_evidence'][-1]['available_at'] = '2099-01-01T00:00:00+00:00'
    r, path, _ = archive_bundle(tmp_path, mutate)
    result = check(path, r)
    assert any('source_available_after_observation' in reason for reason in result['blocked_reasons'])


def test_input_swap_after_preflight_blocks_before_engine(tmp_path, monkeypatch):
    r, path, directory = archive_bundle(tmp_path)
    original = experiment._run_candidate
    def swapped(*args, **kwargs):
        target = directory/'raw_hrs_evidence.json'
        target.write_bytes(target.read_bytes()+b' ')
        return original(*args, **kwargs)
    monkeypatch.setattr(experiment, '_run_candidate', swapped)
    result = check(path, r, execute=True)
    assert result['status']=='BLOCKED_DATA' and result['engine_runs']==0, result


def test_tiny_available_research_and_caller_freeze_never_access_holdout(tmp_path, monkeypatch):
    r, path = partition_registration(tmp_path)
    research = dict(stage='research', status='AVAILABLE', registration_sha256=experiment.fingerprint(r),engine_identity=experiment.engine_identity())
    selection = dict(registration_sha256=experiment.fingerprint(r),research_report_sha256=experiment.fingerprint(research),
        candidate_by_mode={'standard':'floor-4'},frozen_at='2026-01-01T00:00:00+00:00')
    def forbidden(*a, **k): raise AssertionError('holdout data touched')
    monkeypatch.setattr(guard(), '_window_source', forbidden)
    result = check(path, r, stage='holdout', research_report=research,selection=selection)
    assert result['engine_runs']==0 and result['status']=='BLOCKED_DATA'
    assert 'authenticated_selection_authority_required' in result['blocked_reasons']
    assert result['blocked_reasons']==['authenticated_selection_authority_required']


def test_guarded_research_keeps_original_all_cost_tiers_and_minimum_samples(tmp_path):
    r, path, _ = archive_bundle(tmp_path)
    result = check(path, r, execute=True, output=tmp_path/'report.json')
    assert result['engine_runs']==6, result
    assert len(result['evaluations'])==2
    assert result['status']=='BLOCKED_DATA'  # Synthetic fixture lacks 20 closed lifecycles.
    assert all(set(row['tiers'])==set(experiment.TIERS) for row in result['evaluations'])
    assert result['blocked_reasons'] and result['performance_accepted'] is False
    assert json.loads((tmp_path/'report.json.partition.json').read_text())==result['partition_guard']


def test_swap_after_portal_read_before_engine_is_rejected(tmp_path, monkeypatch):
    r, path, directory = archive_bundle(tmp_path)
    original = experiment._registered_margin_validation
    def swapped(*args, **kwargs):
        result = original(*args, **kwargs)
        target = directory/'raw_hrs_evidence.json'
        target.write_bytes(target.read_bytes()+b' ')
        return result
    monkeypatch.setattr(experiment, '_registered_margin_validation', swapped)
    result = check(path, r, execute=True)
    assert result['engine_runs']==0 and result['status']=='BLOCKED_DATA', result
    assert any('snapshot_changed_after_check' in reason for reason in result['blocked_reasons'])


def test_mutation_during_replay_invalidates_result_and_keeps_actual_run_count(tmp_path, monkeypatch):
    r, path, directory = archive_bundle(tmp_path)
    original = experiment.run_replay
    def swapped(*args, **kwargs):
        result = original(*args, **kwargs)
        target = directory/'raw_hrs_evidence.json'
        target.write_bytes(target.read_bytes()+b' ')
        return result
    monkeypatch.setattr(experiment, 'run_replay', swapped)
    result = check(path, r, execute=True)
    assert result['engine_runs']==1 and result['status']=='BLOCKED_DATA', result
    assert not result['evaluations']


def test_future_end_scan_cannot_be_replaced_with_earlier_manifest_boundary(tmp_path):
    r, path, _ = archive_bundle(tmp_path)
    r['windows'][0]['end']='2099-01-01T00:00:00+00:00'
    r['windows'][1].update(start='2099-02-01T00:00:00+08:00',end='2099-03-01T00:00:00+08:00')
    r['windows'][2].update(start='2099-03-01T00:00:00+08:00',end='2099-04-01T00:00:00+08:00')
    write_json(path,r)
    result = check(path,r,execute=True)
    assert result['engine_runs']==0 and 'research_window_not_elapsed' in result['blocked_reasons']


def test_runtime_loaded_config_is_checked_in_terminal_state(tmp_path):
    def mutate(sources):
        for row in sources['decision_evidence']:
            if row['run_id']=='cycle-1' and row['kind']=='scan_end':
                row['payload']['state'] = copy.deepcopy(row['payload']['state'])
                row['payload']['state']['config']['synthetic_changed'] = True
                row['payload_sha256'] = canonical_payload(row['payload'])[1]
    r,path,_=archive_bundle(tmp_path,mutate)
    result=check(path,r)
    assert any('scan_loaded_config_mismatch' in reason for reason in result['blocked_reasons'])


def test_pinned_deployment_observation_binds_no_historical_or_production_authority(tmp_path):
    r,path,_=archive_bundle(tmp_path)
    p=r['prospective_runtime_partition']
    receipt=dict(registration_sha256=experiment.fingerprint(r),release_sha=p['expected_release_sha'],
        source_files=p['source_files'],source_files_sha256=p['source_files_sha256'],observed_at=AT.isoformat(),
        source_attested=True,proves_historical_continuity=True)
    facts=tmp_path/'observation.json';write_json(facts,receipt)
    result=check(path,r,deployment_facts=facts,expected_deployment_facts_sha256=hashlib.sha256(facts.read_bytes()).hexdigest())
    assert result['status']=='CHECKED_LOCAL',result
    assert result['deployment_facts_status']=='BOUND_OBSERVATION_UNVERIFIED_CONTINUITY'
    assert result['deployment_facts']['proves_historical_continuity'] is False
    assert result['source_attested'] is False and result['official_run_ready'] is False
    assert check(path,r,deployment_facts=facts,expected_deployment_facts_sha256='0'*64)['status']=='BLOCKED_DATA'


def test_fixed_first_scan_policies_are_mandatory(tmp_path):
    r,path,directory=archive_bundle(tmp_path)
    # A self-consistent re-seal of a derived bootstrap must not replace the
    # original boundary state. This is a check-only requirement as well.
    raw=json.loads((directory/'raw_hrs_evidence.json').read_text())
    assert raw['evidence_archive']
    from ai_tuner.replay.hrs_evidence_archive import EvidenceArchive
    with EvidenceArchive.open(directory/raw['evidence_archive']['path']) as archive:
        original=list(archive.iter_records('decision_evidence',kind='scan_start'))
    assert original[0]['payload']['positions']=={}
    # Exercise explicit policy requirement, not an unregistered derived start.
    r['windows'][0].pop('start_policy')
    write_json(path,r)
    result=check(path,r)
    assert 'partition_fixed_scan_policies_required' in result['blocked_reasons']


def test_nonflat_terminal_original_scan_stays_blocked(tmp_path):
    def mutate(sources):
        for row in sources['decision_evidence']:
            if row['run_id']=='cycle-1' and row['kind']=='scan_start':
                row['payload']=copy.deepcopy(row['payload'])
                row['payload']['positions']={'A':dict(remaining_quantity='1')}
                row['payload_sha256']=canonical_payload(row['payload'])[1]
    r,path,_=archive_bundle(tmp_path,mutate)
    result=check(path,r,execute=True)
    assert result['engine_runs']==0 and result['status']=='BLOCKED_DATA',result
    assert any('partition_boundary_nonflat' in reason for reason in result['blocked_reasons'])


def test_input_swap_during_archive_verification_cannot_publish_checked_receipt(tmp_path,monkeypatch):
    r,path,directory=archive_bundle(tmp_path)
    original=experiment._window_portal
    def swapped(*args,**kwargs):
        result=original(*args,**kwargs)
        target=directory/'hrs_cash_flows.jsonl'
        target.write_bytes(target.read_bytes()+b' ')
        return result
    monkeypatch.setattr(experiment,'_window_portal',swapped)
    result=check(path,r)
    assert result['status']=='BLOCKED_DATA' and result['engine_runs']==0,result
    assert any('snapshot_changed_during_check' in reason for reason in result['blocked_reasons'])


@pytest.mark.parametrize('field',['as_of','observed_at','source_time'])
def test_numeric_market_source_clocks_are_seconds_and_future_is_blocked(field):
    from ai_tuner.replay.models import DataBlocked
    module=guard();now=module._observed_now()
    module._clock_check({field:now.timestamp()-60},now)
    with pytest.raises(DataBlocked,match='source_available_after_observation'):
        module._clock_check({field:now.timestamp()+3600},now)


def test_numeric_kline_clocks_are_explicit_milliseconds():
    from ai_tuner.replay.models import DataBlocked
    module=guard();now=module._observed_now()
    module._clock_check({'close_time':(now.timestamp()-60)*1000},now)
    with pytest.raises(DataBlocked,match='source_available_after_observation'):
        module._clock_check({'close_time':(now.timestamp()+60)*1000},now)
    with pytest.raises(DataBlocked,match='source_numeric_clock_unit_unknown'):
        module._clock_check({'available_at':now.timestamp()},now)


@pytest.mark.parametrize('late_kind',['scan_denial','protection_order_observation'])
def test_older_run_release_uses_exact_exporter_activity_predicate(tmp_path,late_kind):
    def mutate(sources):
        state=copy.deepcopy(sources['decision_evidence'][0]['payload'])
        state['release_sha']='f'*40
        old=AT-timedelta(minutes=30)
        late=envelope('old',late_kind,dict(reason='synthetic old-cycle observation'),old)
        late['available_at']=(AT+timedelta(minutes=1)).isoformat()
        sources['decision_evidence'] += [envelope('old','scan_start',state,old),
            envelope('old','scan_end',dict(status='complete',state=state),old),
            envelope('old','scan_denial',dict(reason='warmup'),old),late]
    r,path,_=archive_bundle(tmp_path,mutate)
    result=check(path,r,execute=False)
    if late_kind=='scan_denial':
        assert result['status']=='BLOCKED_DATA' and result['engine_runs']==0,result
        assert any('scan_release_mismatch' in reason for reason in result['blocked_reasons'])
    else:
        assert result['status']=='CHECKED_LOCAL',result


def test_standalone_public_run_without_scan_is_retained_and_allowed(tmp_path):
    def mutate(sources):
        sources['decision_evidence'].append(envelope('public-only','public_market_response',
            dict(path='/synthetic',response=[]),AT))
    r,path,_=archive_bundle(tmp_path,mutate)
    assert check(path,r)['status']=='CHECKED_LOCAL'


@pytest.mark.parametrize('kind',['market_cap_batch','market_input'])
def test_hub_quote_availability_seconds_need_explicit_quote_context(kind):
    from ai_tuner.replay.models import DataBlocked
    module=guard();now=module._observed_now()
    quote=dict(value=1,source='cache_hit',as_of=now.timestamp()-120,observed_at=now.timestamp()-120,
        available_at=now.timestamp()-60,is_proxy=False,missing_reason=None)
    payload=(dict(quotes={'A':quote}) if kind=='market_cap_batch'
        else dict(operation='get_market_cap_provenance',result=quote))
    row=dict(kind=kind,payload=payload)
    module._clock_check(row,now)
    quote['available_at']=now.timestamp()+60
    with pytest.raises(DataBlocked,match='source_available_after_observation'):
        module._clock_check(row,now)
    with pytest.raises(DataBlocked,match='source_numeric_clock_unit_unknown'):
        module._clock_check(dict(metadata=dict(available_at=now.timestamp()-60)),now)


@pytest.mark.parametrize('field',['requested_at','captured_at'])
def test_original_request_and_capture_clocks_are_observations(field):
    from ai_tuner.replay.models import DataBlocked
    module=guard();now=module._observed_now()
    with pytest.raises(DataBlocked,match='source_available_after_observation'):
        module._clock_check({field:(now+timedelta(minutes=1)).isoformat()},now)
    module._clock_check({field:(now-timedelta(minutes=1)).isoformat(),
        'expires_at':(now+timedelta(days=1)).isoformat(),'next_funding_time':(now+timedelta(days=1)).isoformat()},now)


def test_external_snapshot_manifest_pin_required_before_local_or_research_access(tmp_path):
    r,path,_=archive_bundle(tmp_path)
    result=check(path,r,execute=True,expected_snapshot_manifest_sha256=None)
    assert result['engine_runs']==0 and result['status']=='BLOCKED_DATA',result
    assert 'snapshot_external_pin_required' in result['blocked_reasons']


def test_fully_self_consistent_manifest_reseal_cannot_replace_external_pin(tmp_path):
    from ai_tuner.replay.data_portal import compute_manifest_sha256,compute_manifest_envelope_sha256
    r,path,directory=archive_bundle(tmp_path)
    manifest_path=directory/'manifest.json'
    pin=hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    manifest=json.loads(manifest_path.read_text())
    target=directory/'raw_hrs_evidence.json'
    raw=json.loads(target.read_text());raw['synthetic_untrusted_annotation']='self-consistent replacement'
    write_json(target,raw)
    manifest['files']['raw_hrs_evidence.json']['sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
    manifest['sha256']=compute_manifest_sha256(manifest['files'])
    manifest['envelope_sha256']=compute_manifest_envelope_sha256(manifest)
    write_json(manifest_path,manifest)
    result=check(path,r,execute=True,expected_snapshot_manifest_sha256=pin)
    assert result['engine_runs']==0 and result['status']=='BLOCKED_DATA',result
    assert 'snapshot_external_pin_mismatch' in result['blocked_reasons']


def test_sidecar_retains_external_snapshot_pin(tmp_path):
    r,path,directory=archive_bundle(tmp_path)
    pin=hashlib.sha256((directory/'manifest.json').read_bytes()).hexdigest()
    result=check(path,r,expected_snapshot_manifest_sha256=pin)
    assert result['status']=='CHECKED_LOCAL',result
    assert result['partition_guard']['window_sources']['research']['manifest_file_sha256']==pin


@pytest.mark.parametrize('future',[False,True])
def test_archive_json_string_payload_preserves_observation_clock_checks(tmp_path,future):
    def mutate(sources):
        payload=dict(path='/synthetic',response=[],captured_at='2099-01-01T00:00:00+00:00' if future else AT.isoformat())
        row=envelope('string-body','public_market_response',payload,AT)
        row['payload']=json.dumps(payload)
        sources['decision_evidence'].append(row)
    r,path,_=archive_bundle(tmp_path,mutate)
    result=check(path,r)
    if future:
        assert result['status']=='BLOCKED_DATA' and result['engine_runs']==0,result
        assert 'source_available_after_observation:captured_at' in result['blocked_reasons']
    else:
        assert result['status']=='CHECKED_LOCAL',result


def test_unknown_text_is_not_interpreted_as_encoded_source_body():
    module=guard()
    module._clock_check(dict(notes=json.dumps(dict(captured_at='2099-01-01T00:00:00+00:00'))),module._observed_now())


def test_encoded_receipt_observation_clock_is_checked():
    from ai_tuner.replay.models import DataBlocked
    module=guard()
    with pytest.raises(DataBlocked,match='source_available_after_observation:captured_at'):
        module._clock_check(dict(receipt=json.dumps(dict(captured_at='2099-01-01T00:00:00+00:00'))),module._observed_now())


def test_original_checkpoint_created_at_is_its_availability_clock(tmp_path):
    def mutate(sources):
        checkpoint=copy.deepcopy(sources['equity_checkpoints'][-1])
        checkpoint.update(as_of=(AT+timedelta(minutes=30)).isoformat(),created_at='2099-01-01T00:00:00+00:00')
        sources['equity_checkpoints'].append(checkpoint)
    r,path,_=archive_bundle(tmp_path,mutate)
    result=check(path,r)
    assert result['status']=='BLOCKED_DATA' and result['engine_runs']==0,result
    assert 'source_available_after_observation:created_at' in result['blocked_reasons']


def test_json_string_market_quote_body_keeps_seconds_context():
    from ai_tuner.replay.models import DataBlocked
    module=guard();now=module._observed_now()
    payload=dict(operation='get_market_cap_provenance',result=dict(as_of=now.timestamp()-120,
        observed_at=now.timestamp()-120,available_at=now.timestamp()-60))
    module._clock_check(dict(kind='market_input',payload=json.dumps(payload)),now)
    payload['result']['available_at']=now.timestamp()+60
    with pytest.raises(DataBlocked,match='source_available_after_observation:available_at'):
        module._clock_check(dict(kind='market_input',payload=json.dumps(payload)),now)


def test_checkpoint_numeric_created_at_has_no_implicit_unit():
    from ai_tuner.replay.models import DataBlocked
    module=guard();now=module._observed_now()
    with pytest.raises(DataBlocked,match='source_numeric_clock_unit_unknown:created_at'):
        module._clock_check(dict(as_of=(now-timedelta(minutes=1)).isoformat(),created_at=now.timestamp()),
            now,family='equity_checkpoints')
