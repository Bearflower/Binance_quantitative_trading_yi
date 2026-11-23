"""Synthetic local authority tests; no production key or account facts."""
import copy
import hashlib
import importlib
import json
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from ai_tuner import hrs_exit_experiment as experiment
from ai_tuner.replay.models import DataBlocked
from .test_hrs_exit_partition_guard import partition_registration, archive_bundle, guard


def freezer():
    return importlib.import_module('ai_tuner.hrs_exit_selection_freeze')


def test_authority_module_is_available():
    assert importlib.util.find_spec('ai_tuner.hrs_exit_selection_freeze') is not None


def setup_authority(tmp_path, monkeypatch):
    f=freezer(); r,path=partition_registration(tmp_path)
    start=experiment._time(r['windows'][0]['start'])
    monkeypatch.setattr(f,'_now',lambda:start-timedelta(days=1))
    state=tmp_path/'authority'
    pin=f.initialize_authority(path,state,expected_registration_sha256=experiment.fingerprint(r))
    monkeypatch.setattr(f,'_now',lambda:experiment._time(r['windows'][-1]['end'])+timedelta(days=1))
    return f,r,path,state,pin


def qualified_report(r):
    engine=experiment.engine_identity(); wid=r['windows'][0]['window_id']
    boundary=dict(actual_start=r['windows'][0]['start'],actual_end=r['windows'][0]['end'],
        start_resolution=dict(selected_evidence_id='start',selected_payload_sha256='1'*64),
        end_resolution=dict(selected_evidence_id='shared',selected_payload_sha256='2'*64))
    sources={wid:dict(manifest_file_sha256='a'*64,boundary=boundary)}
    rows=[]
    for c in r['candidates']:
        tiers={tier:dict(blocked_reasons=[],future_data_accesses=0,audit_sha256='a'*64,source_manifest_sha256='b'*64,
            nav=dict(status='AVAILABLE',nav_return='0.1',max_drawdown='0.1'),lifecycles=dict(status='AVAILABLE',blocked_reasons=[],lifecycle_r=['1']*20,
            net_pnl_per_entry_risk='1',worst_lifecycle_r='-1',without_best_lifecycle_net_pnl='2',longest_losing_streak=1))
            for tier in experiment.TIERS}
        rows.append(dict(candidate_id=c['candidate_id'],mode=c['mode'],window_id=wid,status='AVAILABLE',blocked_reasons=[],
            candidate_sha256=experiment.fingerprint(c),is_baseline=not c['exit_parameters'],engine_identity=engine,tiers=tiers,window_boundary=boundary))
    guard_hash=hashlib.sha256(Path(guard().__file__).read_bytes()).hexdigest()
    sidecar=dict(guard_sha256=guard_hash,window_sources=sources)
    sidecar['sha256']=experiment.fingerprint(sidecar)
    return dict(stage='research',status='AVAILABLE',guard_sha256=guard_hash,blocked_reasons=[],registration_sha256=experiment.fingerprint(r),engine_identity=engine,
        engine_runs=len(rows)*3,evaluations=rows,window_sources=sources,window_boundaries={wid:boundary},
        partition_guard=sidecar,source_attested=False,performance_accepted=False,official_run_ready=False)


def produce(f,r,path,state,pin,monkeypatch,report=None):
    report=report or qualified_report(r)
    def fresh(*args,**kwargs):
        assert kwargs['execute'] is True and kwargs['stage']=='research'
        assert 'selection' not in kwargs and 'research_report' not in kwargs
        return copy.deepcopy(report)
    monkeypatch.setattr(guard(),'run_guard',fresh)
    return f.freeze_research(path,state,expected_anchor_sha256=pin,expected_registration_sha256=experiment.fingerprint(r),expected_snapshot_manifest_sha256='a'*64)


def test_owner_only_prestart_authority_no_rekey_and_public_verifier(tmp_path,monkeypatch):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch)
    assert state.stat().st_mode&0o777==0o700
    assert (state/'private.key').stat().st_mode&0o777==0o600
    assert len((state/'private.key').read_bytes())==32
    with pytest.raises((DataBlocked,FileExistsError)): f.initialize_authority(path,state,expected_registration_sha256=experiment.fingerprint(r))
    result=produce(f,r,path,state,pin,monkeypatch)
    secret=(state/'private.key').read_bytes()
    assert secret.hex() not in json.dumps(result)
    (state/'private.key').unlink()
    with f.authenticated_holdout(path,state,expected_anchor_sha256=pin,expected_registration_sha256=experiment.fingerprint(r),expected_holdout_manifest_sha256={w['window_id']:'c'*64 for w in r['windows'] if w['role']=='holdout'}) as verified:
        assert verified['selection']['candidate_by_mode']=={c['mode']:c['candidate_id'] for c in r['candidates'] if not c['exit_parameters']}
    assert (state/'first-access.json').is_file()


@pytest.mark.parametrize('damage',['signature','report','anchor','copy','code','keymode','keylink','partial','marker'])
def test_forgery_and_partial_state_fail_closed(tmp_path,monkeypatch,damage):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch)
    if damage in ('keymode','keylink','partial','marker'):
        if damage=='keymode': (state/'private.key').chmod(0o644)
        if damage=='keylink':
            (state/'private.key').rename(state/'other');(state/'private.key').symlink_to(state/'other')
        if damage=='partial': (state/'research.json').write_text('{}')
        if damage=='marker': (state/'first-access.json').write_text('{}')
        with pytest.raises(DataBlocked): produce(f,r,path,state,pin,monkeypatch)
        return
    produce(f,r,path,state,pin,monkeypatch)
    if damage=='signature':
        receipt=json.loads((state/'freeze.json').read_text());receipt['signature']='00'*64
        (state/'freeze.json').write_text(json.dumps(receipt));(state/'freeze.json').chmod(0o600)
    if damage=='report': (state/'research.json').write_bytes((state/'research.json').read_bytes()+b' ')
    if damage=='anchor': pin='0'*64
    if damage=='copy':
        shutil.copytree(state,tmp_path/'copy');state=tmp_path/'copy'
    if damage=='code': monkeypatch.setattr(f,'_source_identity',lambda:{'changed':True})
    with pytest.raises(DataBlocked):
        with f.authenticated_holdout(path,state,expected_anchor_sha256=pin,expected_registration_sha256=experiment.fingerprint(r),expected_holdout_manifest_sha256={w['window_id']:'c'*64 for w in r['windows'] if w['role']=='holdout'}): pytest.fail('unauthenticated access')
    assert not (state/'first-access.json').exists()


def test_fresh_complete_research_required_and_no_caller_report_or_time(tmp_path,monkeypatch):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch)
    with pytest.raises(TypeError): f.freeze_research(path,state,expected_anchor_sha256=pin,expected_registration_sha256=experiment.fingerprint(r),research_report=qualified_report(r),frozen_at='2000')
    bad=qualified_report(r);bad['evaluations']=[]
    with pytest.raises(DataBlocked): produce(f,r,path,state,pin,monkeypatch,bad)
    assert not (state/'freeze.json').exists()


def test_objective_all_tiers_and_strict_improvement_baseline_fallback(tmp_path,monkeypatch):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch);report=qualified_report(r)
    assert f.select_candidates(r,report)=={c['mode']:c['candidate_id'] for c in r['candidates'] if not c['exit_parameters']}
    candidate=next(row for row in report['evaluations'] if not row['is_baseline'])
    for tier in candidate['tiers'].values(): tier['nav']['max_drawdown']='0.05'
    assert f.select_candidates(r,report)[candidate['mode']]==candidate['candidate_id']
    candidate['tiers']['2.0x']['lifecycles']['worst_lifecycle_r']='-2'
    assert f.select_candidates(r,report)[candidate['mode']]!=candidate['candidate_id']


def test_original_guard_replay_min20_still_blocks_signing(tmp_path,monkeypatch):
    f=freezer();r,path,directory=archive_bundle(tmp_path)
    monkeypatch.setattr(f,'_now',lambda:experiment._time(r['windows'][0]['start'])-timedelta(days=1))
    state=tmp_path/'authority';pin=f.initialize_authority(path,state,expected_registration_sha256=experiment.fingerprint(r))
    monkeypatch.setattr(f,'_now',guard()._observed_now)
    with pytest.raises(DataBlocked,match='research_not_available'):
        f.freeze_research(path,state,expected_anchor_sha256=pin,expected_registration_sha256=experiment.fingerprint(r),
            expected_snapshot_manifest_sha256=hashlib.sha256((directory/'manifest.json').read_bytes()).hexdigest())
    assert not (state/'freeze.json').exists()


def holdout_pins(r):
    return {w['window_id']:'c'*64 for w in r['windows'] if w['role']=='holdout'}


def test_guard_authenticates_before_holdout_and_requires_both_external_pins(tmp_path,monkeypatch):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch);original=guard().run_guard
    produce(f,r,path,state,pin,monkeypatch);monkeypatch.setattr(guard(),'run_guard',original)
    monkeypatch.setattr(guard(),'_window_source',lambda *a,**k:pytest.fail('holdout opened before pins'))
    result=original(path,expected_registration_sha256=experiment.fingerprint(r),stage='holdout',execute=True,
        authority_dir=state,expected_anchor_sha256=pin)
    assert result['engine_runs']==0 and 'holdout_external_manifest_pins_required' in result['blocked_reasons']
    assert not (state/'first-access.json').exists()


@pytest.mark.parametrize('boundary_damage',[None,'timestamp','evidence','payload'])
def test_authenticated_baseline_holdout_uses_original_comparison_no_special_win(tmp_path,monkeypatch,boundary_damage):
    f=freezer();r,path=partition_registration(tmp_path);state=tmp_path/'authority';original=guard().run_guard
    policy=dict(version='first_scan_start_v1',max_delay_seconds=3600)
    for w in r['windows']:
        w.update(start_policy=policy,end_policy=policy)
        if w['role']=='holdout':
            w.update(snapshot_dir=str(tmp_path/w['window_id']),observation_model='scan_aligned_month_v1',minimum_duration_seconds=28*86400)
    path.write_text(json.dumps(r))
    called=[]
    def evaluate(reg,c,w,d,on_run,**kwargs):
        assert (state/'first-access.json').exists()
        called.append((c['candidate_id'],w['window_id']))
        row=copy.deepcopy(next(x for x in report['evaluations'] if x['candidate_id']==c['candidate_id']))
        first=w['window_id']=='month1'
        row['window_id']=w['window_id'];row['window_boundary']=dict(actual_start=w['start'],actual_end=w['end'],
            start_resolution=dict(selected_evidence_id='shared' if first else 'next',selected_payload_sha256='2'*64),
            end_resolution=dict(selected_evidence_id='next' if first else 'last',selected_payload_sha256='2'*64))
        if first and boundary_damage:
            if boundary_damage=='timestamp':row['window_boundary']['actual_start']=(experiment._time(w['start'])+timedelta(minutes=1)).isoformat()
            if boundary_damage=='evidence':row['window_boundary']['start_resolution']['selected_evidence_id']='forged'
            if boundary_damage=='payload':row['window_boundary']['start_resolution']['selected_payload_sha256']='f'*64
        for _ in experiment.TIERS:on_run()
        return row
    monkeypatch.setattr(experiment,'_run_candidate',evaluate)
    monkeypatch.setattr(f,'_now',lambda:experiment._time(r['windows'][0]['start'])-timedelta(days=1))
    pin=f.initialize_authority(path,state,expected_registration_sha256=experiment.fingerprint(r))
    monkeypatch.setattr(f,'_now',lambda:experiment._time(r['windows'][-1]['end'])+timedelta(days=1))
    report=qualified_report(r);produce(f,r,path,state,pin,monkeypatch,report);monkeypatch.setattr(guard(),'run_guard',original)
    monkeypatch.setattr(guard(),'_window_source',lambda w,*a,**k:({'boundary':{},'manifest_file_sha256':'c'*64},{}))
    monkeypatch.setattr(guard(),'_observed_now',f._now)
    monkeypatch.setattr(guard(),'sha256_file',lambda p:'c'*64 if str(p).endswith('manifest.json') else hashlib.sha256(Path(p).read_bytes()).hexdigest())
    result=original(path,expected_registration_sha256=experiment.fingerprint(r),stage='holdout',execute=True,
        authority_dir=state,expected_anchor_sha256=pin,expected_snapshot_manifest_sha256_by_window=holdout_pins(r))
    assert len(called)==2 and all(identity=='baseline' for identity,w in called),result
    if boundary_damage:
        assert result['status']=='BLOCKED_DATA' and any('shared_scan_boundary_mismatch' in gap for gap in result['blocked_reasons']),result
    else:
        assert result['status']=='AVAILABLE',result
        assert result['holdout_comparison_passed'] is False
    assert result['performance_accepted'] is False and result['source_attested'] is False
    marker=(state/'first-access.json').read_bytes()
    with f.authenticated_holdout(path,state,expected_anchor_sha256=pin,expected_registration_sha256=experiment.fingerprint(r),expected_holdout_manifest_sha256=holdout_pins(r)):pass
    assert (state/'first-access.json').read_bytes()==marker
    changed=holdout_pins(r);changed[next(iter(changed))]='d'*64
    with pytest.raises(DataBlocked,match='first_access_marker_mismatch'):
        with f.authenticated_holdout(path,state,expected_anchor_sha256=pin,expected_registration_sha256=experiment.fingerprint(r),expected_holdout_manifest_sha256=changed):pass


def test_late_initialization_cannot_create_directory(tmp_path,monkeypatch):
    f=freezer();r,path=partition_registration(tmp_path)
    monkeypatch.setattr(f,'_now',lambda:experiment._time(r['windows'][0]['start']))
    with pytest.raises(DataBlocked,match='after_research_start'):
        f.initialize_authority(path,tmp_path/'late',expected_registration_sha256=experiment.fingerprint(r))
    assert not (tmp_path/'late').exists()


def test_refreeze_never_replaces_receipt_before_or_after_access(tmp_path,monkeypatch):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch)
    produce(f,r,path,state,pin,monkeypatch);original=(state/'freeze.json').read_bytes()
    for access in (False,True):
        if access:
            with f.authenticated_holdout(path,state,expected_anchor_sha256=pin,expected_registration_sha256=experiment.fingerprint(r),expected_holdout_manifest_sha256=holdout_pins(r)):pass
        with pytest.raises(DataBlocked,match='already_exists'):produce(f,r,path,state,pin,monkeypatch)
        assert (state/'freeze.json').read_bytes()==original


def test_crashed_publication_leaves_blocking_partial_state(tmp_path,monkeypatch):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch);publish=f._publish
    def crash(path,data):
        if path.name=='freeze.json':raise OSError('synthetic publication crash')
        return publish(path,data)
    monkeypatch.setattr(f,'_publish',crash)
    with pytest.raises(DataBlocked):produce(f,r,path,state,pin,monkeypatch)
    assert (state/'research.json').exists() and not (state/'freeze.json').exists()
    monkeypatch.setattr(f,'_publish',publish)
    with pytest.raises(DataBlocked,match='already_exists'):produce(f,r,path,state,pin,monkeypatch)
    with pytest.raises(DataBlocked):
        with f.authenticated_holdout(path,state,expected_anchor_sha256=pin,expected_registration_sha256=experiment.fingerprint(r),expected_holdout_manifest_sha256=holdout_pins(r)):pytest.fail('partial unlock')


@pytest.mark.parametrize('metric',['nav_return','net_pnl_per_entry_risk','max_drawdown','longest_losing_streak','worst_lifecycle_r','without_best_lifecycle_net_pnl'])
def test_every_registered_economic_metric_is_a_no_worse_constraint(tmp_path,monkeypatch,metric):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch);report=qualified_report(r)
    candidate=next(x for x in report['evaluations'] if not x['is_baseline'])
    for tier in candidate['tiers'].values():tier['nav']['max_drawdown']='0.05'
    target=candidate['tiers']['1.5x']
    if metric=='nav_return':target['nav'][metric]='0'
    elif metric=='max_drawdown':target['nav'][metric]='0.2'
    elif metric=='longest_losing_streak':target['lifecycles'][metric]=2
    else:target['lifecycles'][metric]='-3'
    assert f.select_candidates(r,report)[candidate['mode']]=='baseline'


def test_strict_improvement_in_one_tier_suffices_and_ties_use_stable_id(tmp_path,monkeypatch):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch);report=qualified_report(r)
    row=next(x for x in report['evaluations'] if not x['is_baseline'])
    row['tiers']['2.0x']['lifecycles']['longest_losing_streak']=0
    assert f.select_candidates(r,report)[row['mode']]==row['candidate_id']
    other=copy.deepcopy(row);other['candidate_id']='aaa';report['evaluations'].append(other)
    assert f.select_candidates(r,report)[row['mode']]=='aaa'


def test_fixed_lock_serializes_signing_and_first_holdout_access(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch)
    entered=Event();release=Event();access_started=Event();access_finished=Event()
    def fresh(*args,**kwargs):
        entered.set();assert release.wait(5)
        return qualified_report(r)
    monkeypatch.setattr(guard(),'run_guard',fresh)
    def sign():
        return f.freeze_research(path,state,expected_anchor_sha256=pin,expected_registration_sha256=experiment.fingerprint(r),expected_snapshot_manifest_sha256='a'*64)
    def access():
        access_started.set()
        with f.authenticated_holdout(path,state,expected_anchor_sha256=pin,expected_registration_sha256=experiment.fingerprint(r),expected_holdout_manifest_sha256=holdout_pins(r)):
            access_finished.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        signing=pool.submit(sign);assert entered.wait(5)
        verification=pool.submit(access);assert access_started.wait(5)
        assert not access_finished.wait(.1) and not (state/'first-access.json').exists()
        release.set();signing.result(timeout=5);verification.result(timeout=5)
    assert access_finished.is_set()


def test_future_research_producer_is_zero_run_blocked_and_never_signed(tmp_path,monkeypatch):
    f=freezer();r,path=partition_registration(tmp_path)
    r['windows'][0].update(start='2099-09-01T00:00:00+08:00',end='2099-10-01T00:00:00+08:00')
    r['windows'][1].update(start='2099-10-01T00:00:00+08:00',end='2099-11-01T00:00:00+08:00')
    r['windows'][2].update(start='2099-11-01T00:00:00+08:00',end='2099-12-01T00:00:00+08:00')
    path.write_text(json.dumps(r));state=tmp_path/'future'
    pin=f.initialize_authority(path,state,expected_registration_sha256=experiment.fingerprint(r))
    original=guard().run_guard;results=[]
    def observe(*a,**k):
        result=original(*a,**k);results.append(result);return result
    monkeypatch.setattr(guard(),'run_guard',observe)
    with pytest.raises(DataBlocked,match='research_not_available'):
        f.freeze_research(path,state,expected_anchor_sha256=pin,expected_registration_sha256=experiment.fingerprint(r),expected_snapshot_manifest_sha256='a'*64)
    assert results[0]['engine_runs']==0 and 'research_window_not_elapsed' in results[0]['blocked_reasons']
    assert not (state/'freeze.json').exists() and not (state/'first-access.json').exists()


@pytest.mark.parametrize('damage',['nav_unavailable','lifecycle_unavailable','sidecar_changed','guard_code_hash','report_sources_changed'])
def test_full_research_qualification_rejects_inconsistent_guard_or_metrics(tmp_path,monkeypatch,damage):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch);report=qualified_report(r)
    tier=report['evaluations'][0]['tiers']['baseline']
    if damage=='nav_unavailable':tier['nav']['status']='BLOCKED_DATA'
    if damage=='lifecycle_unavailable':tier['lifecycles']['status']='BLOCKED_DATA'
    if damage=='sidecar_changed':report['partition_guard']['sha256']='f'*64
    if damage=='guard_code_hash':report['guard_sha256']='f'*64
    if damage=='report_sources_changed':report['window_sources']={}
    with pytest.raises(DataBlocked):produce(f,r,path,state,pin,monkeypatch,report)
    assert not (state/'freeze.json').exists()


def test_holdout_output_cannot_overwrite_fixed_authority_even_when_authentication_fails(tmp_path,monkeypatch):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch);original=guard().run_guard
    produce(f,r,path,state,pin,monkeypatch);monkeypatch.setattr(guard(),'run_guard',original)
    monkeypatch.setattr(guard(),'_window_source',lambda *a,**k:pytest.fail('output collision touched holdout'))
    original_receipt=(state/'freeze.json').read_bytes()
    result=original(path,expected_registration_sha256=experiment.fingerprint(r),stage='holdout',execute=True,
        authority_dir=state,expected_anchor_sha256='0'*64,expected_snapshot_manifest_sha256_by_window=holdout_pins(r),output=state/'freeze.json')
    assert (state/'freeze.json').read_bytes()==original_receipt
    assert result['engine_runs']==0 and 'authority_output_collision' in result['blocked_reasons']


@pytest.mark.parametrize('target_kind',['direct','parent_alias','report_temporary_alias','sidecar_alias','sidecar_temporary_alias'])
def test_research_report_and_sidecar_never_overwrite_authority(tmp_path,monkeypatch,target_kind):
    r,path=partition_registration(tmp_path)
    r['windows'][0].update(start='2099-09-01T00:00:00+08:00',end='2099-10-01T00:00:00+08:00')
    r['windows'][1].update(start='2099-10-01T00:00:00+08:00',end='2099-11-01T00:00:00+08:00')
    r['windows'][2].update(start='2099-11-01T00:00:00+08:00',end='2099-12-01T00:00:00+08:00')
    path.write_text(json.dumps(r))
    state=tmp_path/'synthetic-authority';state.mkdir(mode=0o700)
    anchor=state/'anchor.json';anchor.write_bytes(b'synthetic protected anchor');anchor.chmod(0o600)
    before=anchor.read_bytes();output=tmp_path/'report.json'
    if target_kind=='direct':output=anchor
    if target_kind=='parent_alias':
        alias=tmp_path/'authority-alias';alias.symlink_to(state,target_is_directory=True);output=alias/'anchor.json'
    if target_kind=='report_temporary_alias':Path(str(output)+'.tmp').symlink_to(anchor)
    if target_kind=='sidecar_alias':Path(str(output)+'.partition.json').symlink_to(anchor)
    if target_kind=='sidecar_temporary_alias':Path(str(output)+'.partition.json.tmp').symlink_to(anchor)
    monkeypatch.setattr(guard(),'_window_source',lambda *a,**k:pytest.fail('future research opened input'))
    result=guard().run_guard(path,expected_registration_sha256=experiment.fingerprint(r),execute=True,
        stage='research',authority_dir=state,output=output)
    assert anchor.read_bytes()==before
    assert result['engine_runs']==0 and result['status']=='BLOCKED_DATA'
    assert 'authority_output_collision' in result['blocked_reasons']
    assert not (state/'first-access.json').exists()


@pytest.mark.parametrize('entry',['research.json','freeze.json','first-access.json'])
def test_dangling_prior_state_blocks_producer_before_research(tmp_path,monkeypatch,entry):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch)
    (state/entry).symlink_to(state/'never-created')
    calls=[]
    def fresh(*a,**k):calls.append(True);return qualified_report(r)
    monkeypatch.setattr(guard(),'run_guard',fresh)
    with pytest.raises(DataBlocked):
        f.freeze_research(path,state,expected_anchor_sha256=pin,expected_registration_sha256=experiment.fingerprint(r),expected_snapshot_manifest_sha256='a'*64)
    assert calls==[]
    assert not (state/'freeze.json').is_file()
    assert (state/entry).is_symlink()


def test_unauthenticated_holdout_never_resolves_or_stats_output_targets(tmp_path,monkeypatch):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch)
    forbidden=tmp_path/'unopened-holdout'
    for method in ('resolve','stat','lstat'):
        original=getattr(Path,method)
        def checked(self,*args,_original=original,**kwargs):
            assert self!=forbidden and forbidden not in self.parents,'unauthenticated holdout path inspected'
            return _original(self,*args,**kwargs)
        monkeypatch.setattr(Path,method,checked)
    monkeypatch.setattr(guard(),'_window_source',lambda *a,**k:pytest.fail('unauthenticated holdout opened'))
    result=guard().run_guard(path,expected_registration_sha256=experiment.fingerprint(r),execute=True,stage='holdout',
        authority_dir=state,expected_anchor_sha256='0'*64,output=forbidden/'report.json')
    assert result['status']=='BLOCKED_DATA' and result['engine_runs']==0
    assert result['blocked_reasons']==['authority_external_pin_mismatch']


@pytest.mark.parametrize('suffix',['.tmp','.partition.json.tmp'])
def test_authenticated_holdout_temporary_output_cannot_overwrite_private_key(tmp_path,monkeypatch,suffix):
    f,r,path,state,pin=setup_authority(tmp_path,monkeypatch);original=guard().run_guard
    produce(f,r,path,state,pin,monkeypatch);monkeypatch.setattr(guard(),'run_guard',original)
    output=tmp_path/'report.json';Path(str(output)+suffix).symlink_to(state/'private.key')
    before=(state/'private.key').read_bytes()
    monkeypatch.setattr(guard(),'_window_source',lambda *a,**k:pytest.fail('output collision opened holdout'))
    result=original(path,expected_registration_sha256=experiment.fingerprint(r),execute=True,stage='holdout',
        authority_dir=state,expected_anchor_sha256=pin,expected_snapshot_manifest_sha256_by_window=holdout_pins(r),output=output)
    assert (state/'private.key').read_bytes()==before
    assert result['status']=='BLOCKED_DATA' and result['engine_runs']==0
    assert result['blocked_reasons']==['authority_output_collision']


def test_missing_holdout_authority_blocks_before_optional_deployment_file_access(tmp_path,monkeypatch):
    r,path=partition_registration(tmp_path);forbidden=tmp_path/'unopened-holdout-manifest.json'
    original_hash=guard().sha256_file;original_read=guard()._read_json
    def checked_hash(filename):
        assert Path(filename)!=forbidden,'unauthenticated holdout deployment fact read'
        return original_hash(filename)
    def checked_read(filename):
        assert Path(filename)!=forbidden,'unauthenticated holdout deployment fact read'
        return original_read(filename)
    monkeypatch.setattr(guard(),'sha256_file',checked_hash);monkeypatch.setattr(guard(),'_read_json',checked_read)
    monkeypatch.setattr(guard(),'_window_source',lambda *a,**k:pytest.fail('unauthenticated holdout source read'))
    result=guard().run_guard(path,expected_registration_sha256=experiment.fingerprint(r),execute=True,stage='holdout',
        deployment_facts=forbidden,expected_deployment_facts_sha256='0'*64)
    assert result['status']=='BLOCKED_DATA' and result['engine_runs']==0
    assert 'authenticated_selection_authority_required' in result['blocked_reasons']
