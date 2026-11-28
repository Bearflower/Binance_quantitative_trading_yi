"""本次窄发布的范围、运行配置和失败回退约束。"""
import copy
import json
from pathlib import Path

import pytest

from scripts import server_inspection_release as release

ROOT = Path(__file__).resolve().parents[2]
SHA = 'f' * 40
IMAGE = 'sha256:' + 'e' * 64


def plan():
    return release.read_plan(ROOT / 'deployment/server-inspection/plan.json')


def old(row):
    return dict(Id=row['old_id'], Name='/' + row['container'], Image=row['old_image'],
        Config=dict(Hostname='old-host', Image=row['old_image'], Env=['ACCOUNT_MODE=classic', 'SECRET=private'],
                    Cmd=['python', 'main.py'], Entrypoint=None, Labels={'existing': 'keep'},
                    Healthcheck={'Test': ['CMD', 'check']}, WorkingDir='/app'),
        HostConfig=dict(Binds=['/data:/app/logs:rw'], Memory=268435456,
                        RestartPolicy={'Name': 'unless-stopped', 'MaximumRetryCount': 0},
                        OomKillDisable=None, ReadonlyRootfs=False),
        Mounts=[{'Type':'bind','Source':'/data','Destination':'/app/logs','RW':True}],
        NetworkSettings={'Networks': {'trading-network-v2': {'Aliases':['canonical'], 'IPAMConfig':None, 'DriverOpts':None}}},
        State={'Running':True,'Status':'running','StartedAt':'2026-11-28T00:00:00Z','Health':{'Status':'healthy'}}, RestartCount=0)


def test_scope_is_exactly_two_pinned_targets():
    doc=plan()
    assert {r['container'] for r in doc['targets']} == {'trading_system-hrs-acceptance','ai-tuner'}
    changed=copy.deepcopy(doc);changed['targets'].append(copy.deepcopy(doc['targets'][0]))
    with pytest.raises(release.ReleaseError): release.validate_plan(changed)
    changed=copy.deepcopy(doc);changed['targets'][0]['base']='ghcr.io/bearflower/trading-hrs:latest'
    with pytest.raises(release.ReleaseError): release.validate_plan(changed)


def test_clone_preserves_runtime_and_only_changes_reviewed_fields():
    for row in plan()['targets']:
        source=old(row);before=copy.deepcopy(source)
        body=release.clone_body(source,row,IMAGE,SHA)
        assert source == before
        expected=copy.deepcopy(source['Config']);expected.pop('Hostname')
        expected['Image']=IMAGE
        expected['Labels'].update({'codex.server-inspection.source':SHA,'org.opencontainers.image.revision':SHA})
        if row['name']=='hrs-acceptance': expected['Env'].append('HRS_ACCEPTANCE_MAX_SOURCE_PAYLOAD_BYTES=8388608')
        assert {k:v for k,v in body.items() if k not in ('HostConfig','NetworkingConfig')} == expected
        expected_host=copy.deepcopy(source['HostConfig']);expected_host['OomKillDisable']=False
        expected_host['RestartPolicy']={'Name':'no','MaximumRetryCount':0}
        assert body['HostConfig']==expected_host
        assert body['NetworkingConfig']['EndpointsConfig']['trading-network-v2']['Aliases']==['canonical']


def test_clone_refuses_unknown_predecessor_and_ambiguous_env():
    row=plan()['targets'][0]
    for mutate in [lambda c:c.update(Id='a'*64),
                   lambda c:c.update(Image='sha256:'+'a'*64),
                   lambda c:c['Config']['Env'].append('SECRET=duplicate'),
                   lambda c:c['HostConfig']['Binds'].append('/code:/app/strategies:ro')]:
        c=old(row);mutate(c)
        with pytest.raises(release.ReleaseError):release.clone_body(c,row,IMAGE,SHA)


def test_protected_check_rejects_same_name_replacement_or_restart():
    p={'untouched':{'id':'a'*64,'started':'2026-11-28T00:00:00Z'}}
    c={'Id':'a'*64,'State':{'Running':True,'StartedAt':'2026-11-28T00:00:00Z'}}
    release.verify_protected(p,lambda n:c)
    for change in ({'Id':'b'*64},{'State':{'Running':True,'StartedAt':'later'}}):
        with pytest.raises(release.ReleaseError):release.verify_protected(p,lambda n:dict(c,**change))


def test_report_probe_requires_complete_read_and_bounded_rss_not_business_pass():
    d={'database_snapshot_coherent':True,'status':'PENDING','source_error_type':None,'rss_kib':90000}
    release.validate_report_probe(d,268435456)
    for change in ({'database_snapshot_coherent':False},{'source_error_type':'ReportEvidenceLimitExceeded'},{'rss_kib':260000}):
        with pytest.raises(release.ReleaseError):release.validate_report_probe(dict(d,**change),268435456)


def test_workflow_is_build_only_no_latest_no_production_access():
    text=(ROOT/'.github/workflows/server-inspection-release.yml').read_text()
    assert 'workflow_dispatch:' in text and 'codex/server-inspection-fix-20261128' in text
    assert 'docker/build-push-action@' in text
    assert ':latest' not in text and 'SERVER_SSH_KEY' not in text
    assert '43.156.242.184' not in text and 'ssh ' not in text


def test_rollback_operates_only_recorded_ids_and_restores_policy(monkeypatch, tmp_path):
    row=plan()['targets'][0];previous=old(row);new_id='e'*64
    state={previous['Id']:copy.deepcopy(previous),new_id:old(row)}
    state[new_id].update(Id=new_id,Image=IMAGE)
    state[previous['Id']]['Name']='/' + row['container']+'-inspection-before-'+SHA[:8]
    state[previous['Id']]['State']['Running']=False
    calls=[]
    def execute(*args,**kwargs):
        calls.append(args)
        action=args[1]
        if action=='stop':state[args[-1]]['State']['Running']=False
        if action=='start':state[args[-1]]['State']['Running']=True
        if action=='rename':state[args[2]]['Name']='/'+args[3]
        return ''
    monkeypatch.setattr(release,'run',execute)
    monkeypatch.setattr(release,'inspect',lambda name:state[name])
    r=release.Release.__new__(release.Release);r.source=SHA
    monkeypatch.setattr(r,'healthy',lambda identifier:state[identifier])
    r.rollback(row,previous,new_id)
    assert state[previous['Id']]['State']['Running'] is True
    assert state[previous['Id']]['Name']=='/'+row['container']
    assert state[new_id]['State']['Running'] is False
    assert all(args[-1] in state or args[1]=='rename' for args in calls)
    assert ('docker','update','--restart=unless-stopped',previous['Id']) in calls


def test_retirement_merge_keeps_existing_entries_and_requires_healthy_successors():
    snapshots=[];previous=[]
    for row in plan()['targets']:
        c=old(row);c['Name']+='-inspection-before-'+SHA[:8]
        c['State'].update(Running=False,Status='exited',ExitCode=137,OOMKilled=False,Error='',FinishedAt='2026-11-28T02:00:00Z')
        c['HostConfig']['RestartPolicy']={'Name':'no','MaximumRetryCount':0}
        previous.append(c);snapshots.append(c)
        successor=old(row);successor['Id']=('c' if row['name']=='ai-tuner' else 'd')*64
        snapshots.append(successor)
    registry={'version':1,'entries':[{'id':'a'*64,'name':'older-retired','successor':'ai-tuner','reason':'existing'}]}
    before=copy.deepcopy(registry)
    merged=release.retirement_registry(registry,previous,plan()['targets'],snapshots,'/proof/activation.json')
    assert registry == before and merged['entries'][:1] == before['entries']
    assert len(merged['entries'])==3
    assert all(e['lifecycle']=='retired' for e in merged['entries'][1:])
    snapshots[-1]['State']['Health']['Status']='unhealthy'
    with pytest.raises(release.ReleaseError):release.retirement_registry(registry,previous,plan()['targets'],snapshots,'/proof/activation.json')


def test_activation_permission_failure_precedes_all_mutation(monkeypatch,tmp_path):
    r=release.Release.__new__(release.Release);r.state=tmp_path;r.source=SHA
    (tmp_path/'staged.json').write_text(json.dumps({'source':SHA,'targets':[]}))
    (tmp_path/'protected.json').write_text('{}')
    calls=[]
    monkeypatch.setattr(release,'run',lambda *args,**kw:calls.append(args))
    monkeypatch.setattr(r,'audit',lambda phase:(_ for _ in ()).throw(release.ReleaseError('audit denied')))
    with pytest.raises(release.ReleaseError,match='audit denied'):r.activate()
    assert calls == [] and not (tmp_path/'activation.json').exists()


def test_second_activation_failure_rolls_back_both_exact_ids_without_registering(monkeypatch,tmp_path):
    r=release.Release.__new__(release.Release)
    r.state=tmp_path;r.source=SHA
    r.rows=[dict(t,source=SHA,deploy_id='00000101',hashes={t['files'][0]:'a'*64}) for t in plan()['targets']]
    rows={};staged=[];calls=[]
    for index,target in enumerate(r.rows):
        before=old(target);rows[before['Id']]=before
        body=release.clone_body(before,target,IMAGE,SHA)
        new_id=str(index+1)*64
        after=copy.deepcopy(before);after.update(Id=new_id,Image=IMAGE,Name='/candidate-'+str(index))
        after['Config']={k:v for k,v in body.items() if k not in ('HostConfig','NetworkingConfig')}
        after['HostConfig']=copy.deepcopy(body['HostConfig'])
        after['State'].update(Running=False,Status='created',OOMKilled=False)
        rows[new_id]=after
        staged.append({'name':target['name'],'old_id':before['Id'],'new_id':new_id,
                       'old_fingerprint':release.fingerprint(before),'new_fingerprint':release.fingerprint(after),'image':IMAGE})
        (tmp_path/(target['name']+'-body.json')).write_text(json.dumps(body))
        (tmp_path/(target['name']+'-before.json')).write_text(json.dumps(before))
    (tmp_path/'staged.json').write_text(json.dumps({'source':SHA,'targets':staged}))
    (tmp_path/'protected.json').write_text('{}')
    def get(name):
        result=rows.get(name) or next(c for c in rows.values() if c['Name']=='/'+name)
        return copy.deepcopy(result)
    failed=False
    def execute(*args,**kwargs):
        nonlocal failed
        calls.append(args)
        action=args[1]
        if action=='update':
            policy=args[2].split('=',1)[1]
            rows[args[3]]['HostConfig']['RestartPolicy']={'Name':policy,'MaximumRetryCount':0}
        elif action=='stop':rows[args[-1]]['State'].update(Running=False,Status='exited')
        elif action=='start':rows[args[-1]]['State'].update(Running=True,Status='running')
        elif action=='rename':rows[args[2]]['Name']='/'+args[3]
        elif action=='exec' and args[2]==staged[1]['new_id'] and not failed:
            failed=True
            raise release.ReleaseError('source verification failed')
        return ''
    monkeypatch.setattr(release,'run',execute)
    monkeypatch.setattr(release,'inspect',get)
    monkeypatch.setattr(r,'audit',lambda phase:calls.append(('audit',phase)))
    monkeypatch.setattr(r,'healthy',get)
    monkeypatch.setattr(r,'fresh_report',lambda *args:None)
    monkeypatch.setattr(r,'finalize',lambda:pytest.fail('failed activation must not register retirements'))
    with pytest.raises(release.ReleaseError,match='source verification failed'):r.activate()
    result=json.loads((tmp_path/'activation.json').read_text())
    assert result['status']=='failed' and result['rollback_failures']==[]
    assert calls[0]==('audit','activation')
    for target,record in zip(r.rows,staged):
        assert get(target['container'])['Id']==target['old_id']
        assert get(target['old_id'])['State']['Running'] is True
        assert get(record['new_id'])['State']['Running'] is False
        assert get(target['old_id'])['HostConfig']['RestartPolicy']['Name']=='unless-stopped'
    rolled_back=[a[-1] for a in calls if a[:2]==('docker','start') and a[-1] in {t['old_id'] for t in r.rows}]
    assert rolled_back==[r.rows[1]['old_id'],r.rows[0]['old_id']]


def test_build_receipts_and_bundle_bind_only_reviewed_sources(monkeypatch,tmp_path):
    monkeypatch.setenv('GITHUB_RUN_ID','12345')
    calls=[]
    monkeypatch.setattr(release,'run',lambda *args,**kw:calls.append(args) or '')
    for target in plan()['targets']:
        destination=tmp_path/'receipts'/target['name']
        release.prepare(target['name'],destination,SHA,'00000101')
        assert sorted(str(p.relative_to(destination/'app')) for p in (destination/'app').rglob('*') if p.is_file()) == sorted(['VERSION',*target['files']])
        assert (destination/'Dockerfile').read_text().startswith('FROM '+target['base']+'\n')
        with pytest.raises(release.ReleaseError):release.finish_build(destination/'receipt.json','ghcr.io/wrong/image@sha256:'+'e'*64)
        release.finish_build(destination/'receipt.json',target['repository']+'@'+IMAGE)
    output=tmp_path/'bundle'
    release.bundle(tmp_path/'receipts',output,SHA)
    packaged=release.Release(output,SHA,project=tmp_path/'project')
    assert {r['container'] for r in packaged.rows}=={'trading_system-hrs-acceptance','ai-tuner'}
    assert all(c[0]=='docker' for c in calls)
    assert all('--network' in c and 'none' in c for c in calls if c[1]=='run')
    (output/'scripts/database_permissions.py').write_text('tampered')
    with pytest.raises(release.ReleaseError,match='checksum'):release.Release(output,SHA,project=tmp_path/'project')


def test_docker_nanosecond_timestamp_is_supported_on_production_python():
    parsed=release.parse_timestamp('2026-11-27T23:25:39.771883711Z')
    assert parsed.isoformat()=='2026-11-27T23:25:39.771883+00:00'
    with pytest.raises(release.ReleaseError):release.parse_timestamp('2026-11-27T23:25:39')


def test_new_branch_workflow_can_build_without_installing_it_on_main():
    import yaml
    doc=yaml.safe_load((ROOT/'.github/workflows/server-inspection-release.yml').read_text())
    triggers=doc.get('on',doc.get(True))
    assert triggers['push']['branches']==['codex/server-inspection-fix-20261128']


def test_active_clone_accepts_docker_update_null_oom_default_only():
    row=plan()['targets'][1];previous=old(row)
    body=release.clone_body(previous,row,IMAGE,SHA)
    current=copy.deepcopy(previous)
    current['Config']={k:v for k,v in body.items() if k not in ('HostConfig','NetworkingConfig')}
    current['HostConfig']=copy.deepcopy(body['HostConfig'])
    current['HostConfig']['RestartPolicy']=previous['HostConfig']['RestartPolicy']
    # Production Docker returns null after updating only the restart policy.
    current['HostConfig']['OomKillDisable']=None
    before=copy.deepcopy(current)
    r=release.Release.__new__(release.Release)
    r.verify_clone(previous,current,body,active=True)
    assert current==before
    for key,value in [('OomKillDisable',True),('Memory',536870912),('ReadonlyRootfs',True)]:
        changed=copy.deepcopy(current);changed['HostConfig'][key]=value
        with pytest.raises(release.ReleaseError,match='host or mount drift'):
            r.verify_clone(previous,changed,body,active=True)



def test_profit_plan_excludes_reporter_and_keeps_nightly_exit_base(monkeypatch):
    for key in ('PLAN_PATH', 'TARGETS', 'BUNDLE_FILES'):
        monkeypatch.setattr(release, key, getattr(release, key))
    release.use_profit_anchor_plan()
    document = release.read_plan(ROOT / release.PLAN_PATH)
    row, = document['targets']
    assert row['name'] == 'ai-tuner' and row['environment'] == {}
    assert row['base'] == row['repository'] + '@' + row['old_image']
    assert 'ai_tuner/reconciler/nightly_exit.py' not in row['files']
    assert 'ai_tuner/monitor/xau_shadow_patrol.py' in row['files']
    for changed in [dict(row, container='trading_system-hrs-acceptance'),
                    dict(row, files=row['files'] + ['shared/binance_api.py'])]:
        with pytest.raises(release.ReleaseError):
            release.validate_plan(dict(version=1, targets=[changed]))
