import copy
import pytest
from scripts.ledger_risk_manifest import ReleaseBlocked

def test_recovery_suffix_never_stops_or_migrates():
    from scripts.recover_ledger_risk_release import execute_remaining
    class Backend:
        def __init__(self,fail=None):self.calls=[];self.fail=fail
        def action(self,*args):
            self.calls.append(args)
            if args[0]==self.fail:raise RuntimeError('failure')
    complete=Backend();execute_remaining(complete)
    assert complete.calls[0]==('activate_policy',) and complete.calls[-1]==('success',)
    assert not {row[0] for row in complete.calls}&{'stop','migrate','capture','preflight','pull_and_verify'}
    for failure in ('activate_policy','collector_gate','strategy_gate','final_gate'):
        broken=Backend(failure)
        with pytest.raises(RuntimeError):execute_remaining(broken)
        assert ('success',) not in broken.calls

from scripts import recover_ledger_risk_release as recovery

@pytest.mark.parametrize('key',recovery.BASELINE_KEYS)
def test_live_baseline_change_or_missing_blocks(key):
    before={k:'same' for k in recovery.BASELINE_KEYS}
    for after in (dict(before,**{key:'changed'}),{k:v for k,v in before.items() if k!=key}):
        with pytest.raises(ReleaseBlocked):recovery.same_baseline(before,after)

def test_market_mark_changes_do_not_redefine_exposure():
    before={k:'same' for k in recovery.BASELINE_KEYS}
    recovery.same_baseline(dict(before,positions_sha256='old'),dict(before,positions_sha256='new'))

@pytest.mark.parametrize('field,value',[('sha','other'),('status','complete'),('step','activate_policy'),('changed',['hrs-strategy']),('updated_at',0),('no_automatic_rollback',False)])
def test_failure_is_exact_not_generic_resume(tmp_path,monkeypatch,field,value):
    manifest={'policy':{'expected_revision':0}}
    monkeypatch.setattr(recovery,'MANIFEST_DIGEST',recovery.digest(manifest))
    state=dict(sha=recovery.FAILED_SHA,status='failed',step='ReleaseBlocked',changed=[],manifest_sha256=recovery.MANIFEST_DIGEST,updated_at=recovery.FAILED_AT,no_automatic_rollback=True)
    pin=dict(sha=recovery.FAILED_SHA,status='activating',overlay=str(tmp_path/'images.json'),state=str(tmp_path/'state.json'))
    recovery.validate_failure(state,pin,manifest,tmp_path)
    state[field]=value
    with pytest.raises(ReleaseBlocked):recovery.validate_failure(state,pin,manifest,tmp_path)

@pytest.mark.parametrize('field,value',[('sha','other'),('status','active'),('overlay','other'),('state','other')])
def test_pin_identity_must_remain_original(tmp_path,monkeypatch,field,value):
    manifest={'policy':{'expected_revision':0}}
    monkeypatch.setattr(recovery,'MANIFEST_DIGEST',recovery.digest(manifest))
    state=dict(sha=recovery.FAILED_SHA,status='failed',step='ReleaseBlocked',changed=[],manifest_sha256=recovery.MANIFEST_DIGEST,updated_at=recovery.FAILED_AT,no_automatic_rollback=True)
    pin=dict(sha=recovery.FAILED_SHA,status='activating',overlay=str(tmp_path/'images.json'),state=str(tmp_path/'state.json'))
    pin[field]=value
    with pytest.raises(ReleaseBlocked):recovery.validate_failure(state,pin,manifest,tmp_path)

def test_failure_retains_original_pin_and_state(tmp_path):
    runtime=recovery.RecoveryRuntime.__new__(recovery.RecoveryRuntime)
    runtime.directory=tmp_path/'recovery';runtime.approval={'reason':'reviewed'};runtime.fresh={};runtime.validating=True
    calls=[]
    def action(name,*args):
        calls.append(name)
        raise ReleaseBlocked('CAS conflict')
    runtime.action=action;runtime.record=lambda *args:calls.append(args)
    original=tmp_path/'state.json';original.write_text('original')
    with pytest.raises(ReleaseBlocked):runtime.continue_release()
    assert original.read_text()=='original'
    assert calls==['activate_policy',('failed','ReleaseBlocked')]
    assert (runtime.directory/'approval.json').exists()

def test_validation_probe_is_readonly_and_does_not_write(tmp_path):
    runtime=recovery.RecoveryRuntime.__new__(recovery.RecoveryRuntime)
    runtime.validating=True;runtime.fresh={};runtime.manifest={'policy':{'account':'default','enable_reduced':False}}
    runtime.compose=lambda service:['docker','compose']
    runtime.run=lambda args,**kw:'{"ok":true}'
    runtime.directory=tmp_path/'must-not-exist'
    assert runtime.probe('schema')=={'ok':True}
    assert not runtime.directory.exists()
    with pytest.raises(ReleaseBlocked):runtime.probe('final')

@pytest.mark.parametrize('fault',[None,'identity','status','config','inventory','compose','policy','exposure'])
def test_lock_time_live_validation(tmp_path,monkeypatch,fault):
    import json
    monkeypatch.setattr(recovery,'SERVICES',['hrs-strategy'])
    runtime=recovery.RecoveryRuntime.__new__(recovery.RecoveryRuntime)
    runtime.root=tmp_path;runtime.original=tmp_path
    runtime.production={'project':'trading_system','compose_sha256':{'compose.yml':recovery.digest(b'approved')}}
    (tmp_path/'compose.yml').write_bytes(b'changed' if fault=='compose' else b'approved')
    old={'Id':'id','Image':'image','State':{'StartedAt':'start','Status':'running'},'Config':{'a':1},'HostConfig':{}}
    runtime.before={'hrs-strategy':old};current=copy.deepcopy(old)
    if fault=='identity':current['Id']='other'
    if fault=='status':current['State']['Status']='exited'
    if fault=='config':current['Config']['a']=2
    runtime.inspect=lambda service:current
    runtime.compose=lambda service:['compose']
    (tmp_path/'hrs-strategy-effective.json').write_text('{}')
    runtime.run=lambda args,**kw: ('other' if fault=='inventory' else 'id') if args[0]=='docker' else json.dumps({'services':{'hrs-strategy':{}}})
    runtime.images={'hrs':'new'};runtime.image_proof=lambda image:None
    runtime.sql=lambda sql:'1' if fault=='policy' else '0'
    runtime.capture={k:'same' for k in recovery.BASELINE_KEYS}
    runtime.probe=lambda stage:dict(runtime.capture,position_exposure='changed') if fault=='exposure' else runtime.capture
    if fault:
        with pytest.raises(ReleaseBlocked):runtime.validate_live()
    else:runtime.validate_live()
