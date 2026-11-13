import json
import pytest
from scripts.ledger_risk_manifest import ReleaseBlocked

@pytest.mark.parametrize('prefix',['',json.dumps({'event':'module loaded','level':'info'})+'\n','2026-11-04T15:30:00Z '+json.dumps({'event':'module loaded','level':'info'})+'\n'])
def test_import_marker_with_structured_logs(prefix):
    from scripts.recover_after_data import require_consumer_import
    require_consumer_import(prefix+'canonical-reader-import-ok\n')

@pytest.mark.parametrize('output',['','canonical-reader-import-ok\ntrailing',json.dumps({'event':'canonical-reader-import-ok'}),json.dumps({'event':'failed','level':'error'})+'\ncanonical-reader-import-ok','Traceback (most recent call last)\ncanonical-reader-import-ok','ERROR unexpected failure\ncanonical-reader-import-ok'])
def test_errors_or_embedded_markers_do_not_pass(output):
    from scripts.recover_after_data import require_consumer_import
    with pytest.raises(ReleaseBlocked):require_consumer_import(output)

def test_exact_third_stage_boundary():
    from scripts.recover_after_data import validate_data_state,COMPLETED
    from scripts.recover_ledger_risk_release import FAILED_SHA,MANIFEST_DIGEST
    state=dict(sha=FAILED_SHA,manifest_sha256=MANIFEST_DIGEST,status='failed',step='ReleaseBlocked',changed=COMPLETED,no_automatic_rollback=True)
    validate_data_state(state)
    for key,value in [('status','in_progress'),('changed',COMPLETED[:-1]),('sha','other')]:
        with pytest.raises(ReleaseBlocked):validate_data_state(dict(state,**{key:value}))

def test_suffix_starts_at_consumer_without_replaying_upgrades():
    from scripts.recover_after_data import execute_after_data
    class Backend:
        def __init__(self):self.calls=[]
        def action(self,*args):self.calls.append(args)
    b=Backend();execute_after_data(b)
    assert b.calls==[('consumer_gate','data-backend'),('up','dashboard-api'),('consumer_gate','dashboard-api'),('freeze_gate',),('up','ai-tuner'),('final_gate',),('success',)]


@pytest.mark.parametrize('level',['info','warning','debug'])
def test_actual_plain_structlog_shape(level):
    from scripts.recover_after_data import require_consumer_import
    require_consumer_import('2026-11-04 23:13:57 ['+level.ljust(9)+'] 配置文件不存在，使用默认配置 config_path=/app/dashboard/backend/config.yaml\ncanonical-reader-import-ok')

@pytest.mark.parametrize('prefix',['2026-11-04 23:13:57 [error    ] failed','2026-11-04 23:13:57 [info     ] critical failure',json.dumps({'event':'loaded','level':'unexpected'}),'unrecognized output'])
def test_fail_closed_prefix(prefix):
    from scripts.recover_after_data import require_consumer_import
    with pytest.raises(ReleaseBlocked):require_consumer_import(prefix+'\ncanonical-reader-import-ok')

@pytest.mark.parametrize('fault',[None,'reason','code','artifacts','artifact_hash','state','images','containers'])
def test_load_exact_preceding_stage(tmp_path,monkeypatch,fault):
    from pathlib import Path
    from scripts import recover_after_data as mod
    original=tmp_path/'release';previous=original/'after-meli-recovery';previous.mkdir(parents=True)
    state=dict(sha=mod.FAILED_SHA,manifest_sha256=mod.MANIFEST_DIGEST,status='failed',step='ReleaseBlocked',changed=mod.COMPLETED,no_automatic_rollback=True,images={'hrs':'reviewed'})
    names=['state.json','approval.json','validated-live.json','strategy_gate-proof.json']+[s+'-startup.json' for s in mod.COMPLETED if s.endswith('strategy')]
    for name in names:(previous/name).write_text(json.dumps(state if name=='state.json' else {}))
    def parent_load(cls,root,bundle,approval):
        obj=cls.__new__(cls);obj.original=original;obj.images={'hrs':'reviewed'};obj.before={'all':'containers'};obj.approval={'containers':{'old':'snapshot'}};return obj
    monkeypatch.setattr(mod.AfterMeliRuntime,'load',classmethod(parent_load))
    approval=dict(reason='review exact marker failure',recovery_code_sha256=mod.digest(Path(mod.__file__).read_bytes()),meli_approval={},previous_artifacts={name:mod.digest((previous/name).read_bytes()) for name in names},containers={'all':{}})
    if fault=='reason':approval['reason']=''
    if fault=='code':approval['recovery_code_sha256']='bad'
    if fault=='artifacts':approval['previous_artifacts']={}
    if fault=='artifact_hash':approval['previous_artifacts']['state.json']='bad'
    if fault in ('state','images'):
        state['status' if fault=='state' else 'images']='other';path=previous/'state.json';path.write_text(json.dumps(state));approval['previous_artifacts']['state.json']=mod.digest(path.read_bytes())
    if fault=='containers':approval['containers']={}
    if fault:
        with pytest.raises(ReleaseBlocked):mod.AfterDataRuntime.load(tmp_path,tmp_path,approval)
    else:
        obj=mod.AfterDataRuntime.load(tmp_path,tmp_path,approval)
        assert obj.changed==mod.COMPLETED and obj.upgraded_services()==tuple(mod.COMPLETED)
        assert obj.approval['containers']==approval['containers']

@pytest.mark.parametrize('bad',[False,True])
def test_import_runs_in_exact_target_and_checks_output(bad):
    from scripts import recover_after_data as mod
    obj=mod.AfterDataRuntime.__new__(mod.AfterDataRuntime);obj.inspect=lambda service:{'Id':service+'-id'};calls=[]
    def run(args,**kw):
        calls.append((args,kw));return 'ERROR failure\ncanonical-reader-import-ok' if bad else '2026-11-04 23:13:57 [info     ] configured\ncanonical-reader-import-ok'
    obj.run=run
    if bad:
        with pytest.raises(ReleaseBlocked):obj.consumer_import('data-backend')
    else:assert obj.consumer_import('data-backend')['canonical_reader'] is True
    assert calls[0][0][2]=='data-backend-id' and calls[0][1]['include_stderr'] is True
    compile(mod.CONSUMER_CODE,'consumer-probe','exec')
    with pytest.raises(ReleaseBlocked):obj.consumer_import('unknown')

def test_readonly_prechecks_all_four_strategy_startups(monkeypatch):
    from scripts import recover_after_data as mod
    obj=mod.AfterDataRuntime.__new__(mod.AfterDataRuntime);obj.fresh={};seen=[]
    monkeypatch.setattr(mod.AfterMeliRuntime,'validate_live',lambda self:seen.append('base-economic-image-health'))
    obj.inspect=lambda service:{'Id':service,'State':{'StartedAt':'start'}}
    def run(args,**kwargs):
        service=args[-1];seen.append(service)
        event='策略初始化完成，开始运行' if service in ('hrs-strategy','new-coin-strategy') else '调度器已启动'
        return json.dumps({'event':event,'level':'info'})
    obj.run=run;obj.consumer_import=lambda service:seen.append(service) or {'canonical_reader':True}
    obj.validate_live()
    assert seen==['base-economic-image-health','hrs-strategy','btc-eth-aggressive-strategy','btc-eth-strategy','new-coin-strategy','data-backend']

def test_consumer_action_writes_proof_only_after_success(tmp_path,monkeypatch):
    from scripts import recover_after_data as mod
    obj=mod.AfterDataRuntime.__new__(mod.AfterDataRuntime);obj.directory=tmp_path;seen=[]
    obj.consumer_import=lambda service:{'canonical_reader':True};obj.journal=lambda *args:seen.append(('journal',)+args);obj.probe=lambda *args:seen.append(('probe',)+args);obj.record=lambda *args:seen.append(('record',)+args)
    obj.action('consumer_gate','data-backend')
    assert (tmp_path/'data-backend-import.json').exists()
    assert seen[-1]==('record','in_progress','consumer_gate')
    monkeypatch.setattr(mod.AfterMeliRuntime,'action',lambda self,*args:seen.append(args));obj.action('final_gate');assert seen[-1]==('final_gate',)

@pytest.mark.parametrize('failure',[False,True])
def test_continue_records_separate_audit_failure(tmp_path,monkeypatch,failure):
    from scripts import recover_after_data as mod
    obj=mod.AfterDataRuntime.__new__(mod.AfterDataRuntime);obj.directory=tmp_path/'new';obj.full_approval={};obj.fresh={};obj.validating=True;seen=[];obj.record=lambda *args:seen.append(args)
    def run(runtime):
        assert runtime.validating is False
        if failure:raise ReleaseBlocked('late failure')
    monkeypatch.setattr(mod,'execute_after_data',run)
    if failure:
        with pytest.raises(ReleaseBlocked):obj.continue_release()
        assert seen==[('failed','ReleaseBlocked')]
    else:obj.continue_release()
    assert (obj.directory/'approval.json').exists()

@pytest.mark.parametrize('execute,fail',[(False,False),(True,False),(True,True)])
def test_cli_separates_validation_from_execution(tmp_path,monkeypatch,execute,fail):
    import sys
    from scripts import recover_after_data as mod
    (tmp_path/'.deploy_tmp').mkdir();(tmp_path/'.deploy_tmp/compose_deploy.lock').touch();approval=tmp_path/'approval.json';approval.write_text('{}');seen=[]
    class Fake:
        sha='reviewed'
        def validate_live(self):
            seen.append('validate')
            if fail:raise ReleaseBlocked('drift')
        def continue_release(self):seen.append('execute')
    monkeypatch.setattr(mod.AfterDataRuntime,'load',lambda *args:Fake());monkeypatch.setattr(sys,'argv',['recovery','--root',str(tmp_path),'--bundle',str(tmp_path),'--approval',str(approval)]+(['--execute'] if execute else []))
    assert mod.main()==(2 if fail else 0)
    assert seen==(['validate','execute'] if execute and not fail else ['validate'])
