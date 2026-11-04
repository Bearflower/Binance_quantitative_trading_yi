import pytest

def test_exact_order_and_failure_never_runs_later_steps():
    from scripts.scoped_ledger_risk_release import execute_release
    class Backend:
        def __init__(self,fail=None):self.calls=[];self.fail=fail
        def action(self,name,*args):
            self.calls.append((name,*args))
            if name==self.fail:raise RuntimeError(name)
    from scripts.ledger_risk_manifest import SERVICES,MIGRATIONS
    for failure in [None,'preflight','migrate','activate_policy','collector_gate','strategy_gate','final_gate']:
        b=Backend(failure)
        if failure:
            with pytest.raises(RuntimeError):execute_release(b)
            assert not any(row[0]=='success' for row in b.calls)
            assert not any(row[0]=='rollback' for row in b.calls)
        else:
            execute_release(b)
            assert [r[1] for r in b.calls if r[0]=='up']==['execution-facts-collector','hrs-strategy','btc-eth-aggressive-strategy','btc-eth-strategy','new-coin-strategy','data-backend','dashboard-api','ai-tuner']
            assert [r[1] for r in b.calls if r[0]=='stop']==['ai-tuner','execution-facts-collector']
            assert [r[1] for r in b.calls if r[0]=='migrate']==MIGRATIONS
            assert b.calls.index(('activate_policy',))<b.calls.index(('up','execution-facts-collector'))
            assert b.calls[-1]==('success',)

def test_real_commands_scope_and_migration_hash(tmp_path):
    import json
    from scripts.scoped_ledger_risk_release import Runtime
    from scripts.ledger_risk_manifest import SERVICES,SERVICE_IMAGES,COMPOSE,MIGRATIONS,digest,ReleaseBlocked
    r=object.__new__(Runtime);r.root=tmp_path;r.bundle=tmp_path/'bundle';r.directory=tmp_path/'evidence';r.directory.mkdir()
    r.overlay=r.directory/'images.json';r.changed=[];r.images={v:'registry/'+v for v in SERVICE_IMAGES.values()}
    r.production={'project':'trading_system','stacks':{s:COMPOSE for s in SERVICES}}
    r.manifest={'migrations':[{'path':MIGRATIONS[0],'sha256':digest(b'SELECT 1;')}]}
    path=r.bundle/MIGRATIONS[0];path.parent.mkdir(parents=True);path.write_text('SELECT 1;')
    calls=[]
    def run(args,**kwargs):
        calls.append((args,kwargs));return 'sha256:expected'
    r.run=run;r.record=lambda *a:None;r.health=lambda *a:None;r.image_proof=lambda *a,**k:None;r.inspect=lambda s:{'Image':'sha256:expected','Id':'running'}
    r.action('migrate',MIGRATIONS[0])
    assert calls[-1][0][:4]==['docker','exec','-i','trading_system-postgres']
    assert calls[-1][1]['input']=='SELECT 1;'
    for service in SERVICES:
        r.action('up',service)
        command=calls[-2][0]
        assert command[-4:]==['up','-d','--no-deps',service]
        assert command[command.index('-p')+1]=='trading_system'
        assert sum(x=='-f' for x in command)==4
    path.write_text('SELECT unsafe;')
    before=len(calls)
    with pytest.raises(ReleaseBlocked):r.action('migrate',MIGRATIONS[0])
    assert len(calls)==before


def test_full_runtime_guard_precedes_secrets_and_mutations():
    from pathlib import Path
    source=Path('scripts/remote_deploy_runtime.sh').read_text()
    scoped=source.index('ledger-risk-${COMMIT_SHA}')
    pins=source.index('ledger-risk-active.json')
    assert scoped<pins
    assert 'exit $?' in source[scoped:pins]
    assert pins<source.index('mkdir -p .secrets')

def test_every_failure_stops_exactly_at_failed_step():
    from scripts.scoped_ledger_risk_release import execute_release
    class Backend:
        def __init__(self,stop=None):self.calls=[];self.stop=stop
        def action(self,*args):
            self.calls.append(args)
            if len(self.calls)==self.stop:raise RuntimeError('injected failure')
    complete=Backend();execute_release(complete)
    for index in range(1,len(complete.calls)+1):
        broken=Backend(index)
        with pytest.raises(RuntimeError):execute_release(broken)
        assert broken.calls==complete.calls[:index]
        assert not any('rollback' in str(call) for call in broken.calls)


def test_partial_or_completed_state_never_replays(tmp_path,monkeypatch):
    import json
    import scripts.scoped_ledger_risk_release as module
    from scripts.ledger_risk_manifest import IMAGES,ReleaseBlocked,RELEASE,digest
    bundle=tmp_path/'bundle';bundle.mkdir();(bundle/'release-sha').write_text('a'*40)
    checksums={}
    for name in ('scripts/scoped_ledger_risk_release.py','scripts/ledger_risk_manifest.py','scripts/ledger_risk_runtime_config.py'):
        path=bundle/name;path.parent.mkdir(exist_ok=True);path.write_text('reviewed')
        checksums[name]=digest(path.read_bytes())
    manifest={'source_checksums':checksums,'expected_production':{}}
    monkeypatch.setattr(module,'validate_manifest',lambda m,**kw:m)
    images={name:'ghcr.io/bearflower/trading-'+name+'@sha256:'+'b'*64 for name in IMAGES}
    state=tmp_path/'.releases'/RELEASE/('a'*40)/'state.json';state.parent.mkdir(parents=True)
    for status in ('failed','complete'):
        state.write_text(json.dumps({'status':status}))
        with pytest.raises(ReleaseBlocked,match='never blindly replay'):module.Runtime(tmp_path,bundle,'a'*40,manifest,images)
    state.unlink();pin=tmp_path/'.deploy_tmp/ledger-risk-active.json';pin.parent.mkdir();pin.write_text('{}')
    with pytest.raises(ReleaseBlocked,match='pinned'):module.Runtime(tmp_path,bundle,'a'*40,manifest,images)

def test_image_probe_handles_dashboard_workdir_without_pythonpath(tmp_path):
    import json,os,subprocess,sys
    from scripts.scoped_ledger_risk_release import image_probe_code
    root=tmp_path/'app';workdir=root/'dashboard/backend';workdir.mkdir(parents=True)
    for name in ('shared/__init__.py','shared/accounting/__init__.py','shared/accounting/published_report.py','shared/entry_risk_policy.py'):
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('')
    (root/'VERSION').write_text('GIT_SHA='+'a'*40)
    env=dict(os.environ);env.pop('PYTHONPATH',None)
    result=subprocess.run([sys.executable,'-c',image_probe_code(str(root))],cwd=workdir,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['version']=='GIT_SHA='+'a'*40

@pytest.mark.parametrize('drift',[False,True])
def test_pull_stage_checks_default_behavior_before_any_stop(tmp_path,drift):
    import copy,json
    from scripts.scoped_ledger_risk_release import Runtime,execute_release
    from scripts.ledger_risk_manifest import SERVICE_IMAGES,IMAGES,SERVICES,ReleaseBlocked
    r=object.__new__(Runtime);r.directory=tmp_path;r.images={key:'registry/'+key for key in IMAGES}
    r.before={service:{'Image':'sha256:old'} for service in SERVICES}
    for service in SERVICES:(tmp_path/(service+'-effective.json')).write_text(json.dumps({'image':r.images[SERVICE_IMAGES[service]]}))
    commands=[]
    def run(args,**kwargs):
        commands.append(args)
        if args[:2]==['docker','pull']:return ''
        assert args[:3]==['docker','image','inspect']
        config={'Cmd':['python','main.py'],'Env':['PATH=/bin'],'User':''}
        if drift and args[-1]==r.images['hrs']:config['Cmd']=['unexpected-startup']
        return json.dumps([{'Config':config}])
    r.run=run;r.image_proof=lambda *a:None;r.record=lambda *a:None
    if not drift:
        r.action('pull_and_verify')
        assert len(json.loads((tmp_path/'image-default-transitions.json').read_text()))==8
        assert len([c for c in commands if c[:2]==['docker','pull']])==7
    else:
        steps=[]
        class Backend:
            def action(self,name,*args):
                steps.append(name)
                if name=='pull_and_verify':r.action(name)
        with pytest.raises(ReleaseBlocked,match='effective defaults Cmd'):execute_release(Backend())
        assert steps==['preflight','pull_and_verify']
        assert not (tmp_path/'image-default-transitions.json').exists()
