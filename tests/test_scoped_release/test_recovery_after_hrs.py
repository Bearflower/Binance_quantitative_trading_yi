import copy
import json
import pytest
from scripts.ledger_risk_manifest import ReleaseBlocked

def test_json_unicode_startup_is_parsed():
    from scripts.recover_after_hrs import require_startup
    event=json.dumps({'event':'策略初始化完成，开始运行','level':'info'})
    require_startup(event,'hrs-strategy')
    require_startup('2026-11-04T14:10:00Z '+event,'hrs-strategy')
    require_startup(json.dumps({'log':event+'\n','stream':'stdout'}),'hrs-strategy')

@pytest.mark.parametrize('logs',[json.dumps({'event':'not ready','detail':'策略初始化完成，开始运行'}),'策略初始化完成，开始运行',json.dumps({'event':'策略初始化完成，开始运行','level':'error'}),json.dumps({'event':'策略执行失败','level':'error'})])
def test_non_event_or_failed_startup_blocks(logs):
    from scripts.recover_after_hrs import require_startup
    with pytest.raises(ReleaseBlocked):require_startup(logs,'hrs-strategy')

def fixture():
    pos=[dict(symbol='ARIAUSDT',positionSide='BOTH',positionAmt='-42',entryPrice='0.12',markPrice='0.12')]
    stop=dict(symbol='ARIAUSDT',positionSide='BOTH',side='BUY',orderType='STOP_MARKET',triggerPrice='0.13',closePosition=True,reduceOnly=True,algoId=1,algoStatus='NEW')
    tp=dict(symbol='ARIAUSDT',positionSide='BOTH',side='BUY',orderType='TAKE_PROFIT',triggerPrice='0.11',price='0.11',quantity='12',reduceOnly=True,algoId=2,algoStatus='NEW')
    return pos,[stop,tp]

def test_reviewed_rebuild_allows_only_explicit_order_transition():
    from scripts.recover_after_hrs import verify_reviewed_rebuild,exposure
    from scripts.verify_ledger_risk_release import hash_rows
    pos,algos=fixture();old=copy.deepcopy(algos);old[0]['algoId']=99
    approval=dict(symbols=['ARIAUSDT'],before_orders_sha256=hash_rows([]),before_algos_sha256=hash_rows(old),after_orders_sha256=hash_rows([]),after_algos_sha256=hash_rows(algos),reason='HRS startup rebuilt stop and TP',position_exposure=exposure(pos))
    assert verify_reviewed_rebuild(pos,[],old,[],algos,approval)['approved_symbols']==['ARIAUSDT']

@pytest.mark.parametrize('fault',['increase','no_stop','not_reduce','tp_wrong_side','tp_overfill','unreviewed_hash','unrelated_symbol'])
def test_order_transition_failures_block(fault):
    from scripts.recover_after_hrs import verify_reviewed_rebuild,exposure
    from scripts.verify_ledger_risk_release import hash_rows
    pos,algos=fixture();old=copy.deepcopy(algos)
    baseline=exposure(pos)
    if fault=='increase':pos[0]['positionAmt']='-43'
    if fault=='no_stop':algos.pop(0)
    if fault=='not_reduce':algos[1]['reduceOnly']=False
    if fault=='tp_wrong_side':algos[1]['price']='0.14'
    if fault=='tp_overfill':algos[1]['quantity']='43'
    if fault=='unrelated_symbol':algos.append(dict(algos[1],symbol='OTHERUSDT',algoId=3))
    approval=dict(symbols=['ARIAUSDT'],before_orders_sha256=hash_rows([]),before_algos_sha256=hash_rows(old),after_orders_sha256=hash_rows([]),after_algos_sha256=hash_rows(algos),reason='reviewed',position_exposure=baseline)
    if fault=='unreviewed_hash':approval['after_algos_sha256']='wrong'
    with pytest.raises(ReleaseBlocked):verify_reviewed_rebuild(pos,[],old,[],algos,approval)

def test_suffix_never_reactivates_or_recreates_hrs_collector():
    from scripts.recover_after_hrs import execute_after_hrs
    class Backend:
        def __init__(self):self.calls=[]
        def action(self,*args):self.calls.append(args)
    b=Backend();execute_after_hrs(b)
    assert b.calls[0]==('strategy_gate','hrs-strategy')
    assert b.calls[-1]==('success',)
    assert ('up','hrs-strategy') not in b.calls and ('up','execution-facts-collector') not in b.calls
    assert not {x[0] for x in b.calls}&{'activate_policy','migrate','stop','capture'}


def test_later_structured_error_overrides_startup():
    from scripts.recover_after_hrs import require_startup
    logs=json.dumps({'event':'策略初始化完成，开始运行','level':'info'})+'\n'+json.dumps({'event':'failed','level':'error'})
    with pytest.raises(ReleaseBlocked):require_startup(logs,'hrs-strategy')

@pytest.mark.parametrize('failure',['strategy_gate','consumer_gate','final_gate'])
def test_suffix_failure_never_marks_success(failure):
    from scripts.recover_after_hrs import execute_after_hrs
    class Backend:
        def __init__(self):self.calls=[]
        def action(self,*args):
            self.calls.append(args)
            if args[0]==failure:raise ReleaseBlocked('injected')
    b=Backend()
    with pytest.raises(ReleaseBlocked):execute_after_hrs(b)
    assert ('success',) not in b.calls


def test_phase2_failure_boundary_exact():
    from scripts.recover_after_hrs import validate_phase2_state
    from scripts.recover_ledger_risk_release import FAILED_SHA,MANIFEST_DIGEST
    state=dict(sha=FAILED_SHA,manifest_sha256=MANIFEST_DIGEST,status='failed',step='ReleaseBlocked',changed=['execution-facts-collector','hrs-strategy'],no_automatic_rollback=True)
    validate_phase2_state(state)
    for key,value in [('status','in_progress'),('changed',['hrs-strategy']),('sha','other'),('step','success')]:
        with pytest.raises(ReleaseBlocked):validate_phase2_state(dict(state,**{key:value}))


@pytest.fixture
def phase2_files(tmp_path,monkeypatch):
    from scripts import recover_after_hrs as mod
    monkeypatch.setattr(mod,'validate_manifest',lambda value,**kw:value)
    original=tmp_path/'.releases'/mod.RELEASE/mod.FAILED_SHA;previous=original/'manual-recovery';previous.mkdir(parents=True)
    bundle=tmp_path/'bundle';(bundle/'deployment').mkdir(parents=True)
    manifest={'expected_production':{},'policy':{'expected_revision':0}}
    monkeypatch.setattr(mod,'MANIFEST_DIGEST',mod.digest(manifest))
    images={name:'immutable-'+name for name in set(mod.SERVICE_IMAGES.values())}
    state=dict(sha=mod.FAILED_SHA,manifest_sha256=mod.MANIFEST_DIGEST,status='failed',step='ReleaseBlocked',changed=['execution-facts-collector','hrs-strategy'],no_automatic_rollback=True,images=images)
    pos,algos=fixture();from scripts.verify_ledger_risk_release import hash_rows
    review=dict(symbols=['ARIAUSDT'],before_orders_sha256=hash_rows([]),before_algos_sha256=hash_rows(algos),after_orders_sha256=hash_rows([]),after_algos_sha256=hash_rows(algos),reason='reviewed',position_exposure=mod.exposure(pos))
    capture=dict(position_exposure=review['position_exposure'],ordinary_orders_sha256=review['before_orders_sha256'],algo_orders_sha256=review['before_algos_sha256'])
    values={'state.json':{},'manifest.json':manifest,'images.json':{'services':{s:{'image':images[mod.SERVICE_IMAGES[s]]} for s in mod.SERVICES}},'preflight-containers.json':{'hrs-strategy':{}},'preflight-proof.json':capture,'schema-proof.json':{'ok':True},'manual-recovery/state.json':state,'manual-recovery/policy-activation.json':{'active_revision':1},'manual-recovery/policy-proof.json':{'ok':True},'manual-recovery/collector-proof.json':{'ok':True}}
    values.update({s+'-effective.json':{} for s in mod.SERVICES})
    for name,value in values.items():(original/name).write_text(json.dumps(value))
    (bundle/mod.MANIFEST).write_text(json.dumps(manifest));(bundle/'image-digests.json').write_text(json.dumps(images))
    pin=tmp_path/'.deploy_tmp/ledger-risk-active.json';pin.parent.mkdir();pin.write_text(json.dumps(dict(sha=mod.FAILED_SHA,status='activating',overlay=str(original/'images.json'),state=str(original/'state.json'))))
    modules=['recover_after_hrs.py','recover_ledger_risk_release.py','scoped_ledger_risk_release.py','ledger_risk_manifest.py','ledger_risk_runtime_config.py','verify_ledger_risk_release.py']
    code_dir=__import__('pathlib').Path(mod.__file__).parent
    approval=dict(reason='reviewed Unicode gate timeout',artifacts={name:mod.digest((original/name).read_bytes()) for name in values},code={name:mod.digest((code_dir/name).read_bytes()) for name in modules},pin_sha256=mod.digest(pin.read_bytes()),containers={'hrs-strategy':{}},order_review=review,before_orders=[],before_algos=algos)
    return mod,tmp_path,bundle,approval,original

def test_exact_phase2_load_is_validation_only(phase2_files):
    mod,root,bundle,approval,original=phase2_files
    runtime=mod.AfterHRSRuntime.load(root,bundle,approval)
    assert runtime.validating and not runtime.directory.exists()
    assert runtime.changed==['execution-facts-collector','hrs-strategy']

@pytest.mark.parametrize('fault',['artifact','code','pin','state','exposure','before_hash','symbols','inventory','attempted'])
def test_phase2_artifact_and_review_drift_block(phase2_files,fault):
    mod,root,bundle,approval,original=phase2_files
    if fault=='artifact':approval['artifacts']['schema-proof.json']='bad'
    if fault=='code':approval['code']['recover_after_hrs.py']='bad'
    if fault=='pin':approval['pin_sha256']='bad'
    if fault=='state':
        path=original/'manual-recovery/state.json';row=json.loads(path.read_text());row['status']='in_progress';path.write_text(json.dumps(row));approval['artifacts']['manual-recovery/state.json']=mod.digest(path.read_bytes())
    if fault=='exposure':approval['order_review']['position_exposure']=[]
    if fault=='before_hash':approval['order_review']['before_algos_sha256']='other'
    if fault=='symbols':approval['order_review']['symbols']=['OTHER']
    if fault=='inventory':approval['containers']={}
    if fault=='attempted':(original/'after-hrs-recovery').mkdir()
    with pytest.raises(ReleaseBlocked):mod.AfterHRSRuntime.load(root,bundle,approval)

def test_final_transition_does_not_mutate_original_capture(tmp_path,monkeypatch):
    from scripts import recover_after_hrs as mod
    runtime=mod.AfterHRSRuntime.__new__(mod.AfterHRSRuntime);runtime.directory=tmp_path
    original={'position_exposure':['held'],'allocation_sha256':'unchanged','ordinary_orders_sha256':'old','algo_orders_sha256':'old-algo'}
    runtime.capture=original;runtime.approval={'order_review':{'after_orders_sha256':'new','after_algos_sha256':'new-algo'}}
    observed=[]
    def action(self,name,*args):
        observed.append(dict(self.capture));raise ReleaseBlocked('injected final failure')
    monkeypatch.setattr(mod.Runtime,'action',action)
    with pytest.raises(ReleaseBlocked):runtime.action('final_gate')
    assert runtime.capture is original
    assert observed==[dict(original,ordinary_orders_sha256='new',algo_orders_sha256='new-algo')]
    assert json.loads((tmp_path/'reviewed-order-transition.json').read_text())==runtime.approval['order_review']


def gap_fixture():
    from scripts.recover_after_hrs import exposure
    from scripts.verify_ledger_risk_release import hash_rows
    pos,algos=fixture();pos[0]['positionAmt']='-30'
    algos[0].update(algoId=2000001577898163,triggerPrice='0.12977',quantity='0',price='0')
    algos[1].update(algoId=2000001577898176,triggerPrice='0.1068',price='0',quantity='16',orderType='TAKE_PROFIT_MARKET',closePosition=False)
    history=copy.deepcopy(algos)
    history[0].update(algoId=2000001576528510,algoStatus='CANCELED');history[1].update(algoId=2000001576528538,algoStatus='CANCELED')
    review=dict(evidence_mode='missing_old_raw_full_review',missing_old_raw=True,reason='independent signed account-wide protection review',symbols=['ARIAUSDT'],position_exposure=exposure(pos),after_orders_sha256=hash_rows([]),after_algos_sha256=hash_rows(algos),reviewed_position_symbols=['ARIAUSDT'],aria_history=history)
    return pos,algos,review

def test_explicit_evidence_gap_preserves_truth_and_checks_history():
    from scripts.recover_after_hrs import verify_gap_review
    pos,algos,review=gap_fixture()
    proof=verify_gap_review(pos,[],algos,review)
    assert proof['old_raw_comparison_performed'] is False
    assert proof['missing_old_raw'] is True

@pytest.mark.parametrize('fault',['missing_declaration','wrong_history','stop_loosened','tp_quantity','missing_symbol_review','extra_new_order'])
def test_gap_review_is_narrow_not_generic_order_bypass(fault):
    from scripts.recover_after_hrs import verify_gap_review
    from scripts.verify_ledger_risk_release import hash_rows
    pos,algos,review=gap_fixture()
    if fault=='missing_declaration':review['missing_old_raw']=False
    if fault=='wrong_history':review['aria_history'][0]['algoStatus']='NEW'
    if fault=='stop_loosened':algos[0]['triggerPrice']='0.14'
    if fault=='tp_quantity':algos[1]['quantity']='17'
    if fault=='missing_symbol_review':review['reviewed_position_symbols']=[]
    if fault=='extra_new_order':algos.append(dict(algos[1],algoId=3,quantity='1'))
    review['after_algos_sha256']=hash_rows(algos)
    with pytest.raises(ReleaseBlocked):verify_gap_review(pos,[],algos,review)


def test_new_containers_use_service_image_mapping(tmp_path,monkeypatch):
    from scripts import recover_after_hrs as mod
    from scripts.verify_ledger_risk_release import hash_rows
    services=['hrs-strategy','execution-facts-collector']
    monkeypatch.setattr(mod,'SERVICES',services)
    monkeypatch.setattr(mod,'SERVICE_IMAGES',{'hrs-strategy':'hrs','execution-facts-collector':'collector-test'})
    runtime=mod.AfterHRSRuntime.__new__(mod.AfterHRSRuntime)
    runtime.root=tmp_path;runtime.original=tmp_path;runtime.production={'compose_sha256':{},'project':'trading_system'}
    rows={s:dict(Id=s,Image='image-'+s,Config={},HostConfig={},State={'Status':'running','StartedAt':'start','Health':{'Status':'healthy'}}) for s in services}
    runtime.before=copy.deepcopy(rows);runtime.inspect=lambda s:rows[s]
    runtime.approval={'containers':{s:dict(id=s,image_id=rows[s]['Image'],started_at='start',config_sha256=mod.digest({'Config':{},'HostConfig':{}})) for s in services},'order_review':{'evidence_mode':'missing_old_raw_full_review'}}
    runtime.images={'hrs':'ref-hrs','collector-test':'ref-collector'}
    proof=dict(position_exposure=[],allocation_sha256='same',ordinary_orders_sha256=hash_rows([]),algo_orders_sha256=hash_rows([]))
    runtime.capture=proof;runtime.probe=lambda stage:proof;runtime.fresh={}
    runtime.signed_inventory=lambda:dict(positions=[],orders=[],algos=[])
    monkeypatch.setattr(mod,'verify_gap_review',lambda *args:{'reviewed':True})
    runtime.compose=lambda s:['compose',s]
    seen=[];runtime.image_proof=lambda image,**kw:seen.append((image,kw['container']))
    for s in services:(tmp_path/(s+'-effective.json')).write_text('{}')
    def run(args,**kwargs):
        if args[:2]==['docker','ps']:return '\n'.join(services)
        if args[:2]==['docker','image']:return 'image-'+('hrs-strategy' if args[-1]=='ref-hrs' else 'execution-facts-collector')
        if args[:2]==['docker','logs']:return json.dumps({'event':'策略初始化完成，开始运行'})
        if args[0]=='compose':return json.dumps({'services':{args[1]:{}}})
        raise AssertionError(args)
    runtime.run=run
    runtime.validate_live()
    assert seen==[('ref-hrs','hrs-strategy'),('ref-collector','execution-facts-collector')]
