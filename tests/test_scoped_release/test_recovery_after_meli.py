import copy
import pytest
from scripts.ledger_risk_manifest import ReleaseBlocked

@pytest.fixture
def closure():
    old=dict(symbol='MELIUSDT',orderId=1785285,status='CANCELED',type='LIMIT',side='BUY',positionSide='BOTH',reduceOnly=True,origQty='0.06',executedQty='0',price='1721.03')
    new=dict(symbol='MELIUSDT',orderId=1803995,status='FILLED',type='MARKET',side='BUY',positionSide='BOTH',reduceOnly=True,origQty='0.06',executedQty='0.06',avgPrice='1749.30',clientOrderId='codex-meli-stopclose-20261104-1')
    trades=[dict(symbol='MELIUSDT',id=38450,commission='0.04198320',commissionAsset='USDT',orderId=1803995,side='BUY',positionSide='BOTH',qty='0.06',price='1749.30',realizedPnl='-6.7116')]
    algos=[dict(algoId=i,symbol='MELIUSDT',algoStatus='CANCELED',orderType='TAKE_PROFIT',side='BUY',positionSide='BOTH',reduceOnly=True) for i in (2000001555621787,2000001555621677)]
    algos.append(dict(algoId=2000001555621647,symbol='MELIUSDT',algoStatus='FINISHED',orderType='STOP',actualOrderId='1785285',actualQty='0',quantity='0.06',triggerPrice='1719.31',price='1721.03',side='BUY',positionSide='BOTH',reduceOnly=True))
    return dict(limit_order=old,market_order=new,trades=trades,algo_history=algos)

def test_exact_authorized_close(closure):
    from scripts.recover_after_meli import verify_close_evidence
    assert verify_close_evidence(closure)['closed_quantity']=='0.06'

@pytest.mark.parametrize('field,value',[('orderId',2),('status','NEW'),('type','LIMIT'),('side','SELL'),('reduceOnly',False),('positionSide','SHORT'),('executedQty','0.05'),('avgPrice','1749.31'),('clientOrderId','other')])
def test_other_market_trade_never_authorizes_transition(closure,field,value):
    from scripts.recover_after_meli import verify_close_evidence
    closure['market_order'][field]=value
    with pytest.raises(ReleaseBlocked):verify_close_evidence(closure)

@pytest.mark.parametrize('fault',['filled_limit','nan_quantity','wrong_pnl','duplicate_trade','missing_trade','stale_tp'])
def test_fill_cancel_proof_must_be_complete(closure,fault):
    from scripts.recover_after_meli import verify_close_evidence
    if fault=='filled_limit':closure['limit_order']['executedQty']='0.01'
    if fault=='nan_quantity':closure['trades'][0]['qty']='NaN'
    if fault=='wrong_pnl':closure['trades'][0]['realizedPnl']='0'
    if fault=='duplicate_trade':closure['trades']*=2
    if fault=='missing_trade':closure['trades']=[]
    if fault=='stale_tp':closure['algo_history'][0]['algoStatus']='NEW'
    with pytest.raises(ReleaseBlocked):verify_close_evidence(closure)

def test_only_meli_removed_from_exposure():
    from scripts.recover_after_meli import after_close_exposure
    before=[dict(symbol='ARIAUSDT',positionSide='BOTH',positionAmt='-30',entryPrice='.12'),dict(symbol='MELIUSDT',positionSide='BOTH',positionAmt='-0.06',entryPrice='1637.44')]
    assert after_close_exposure(before)==before[:1]
    for change in ('-0.07','0.06','NaN'):
        bad=copy.deepcopy(before);bad[1]['positionAmt']=change
        with pytest.raises(ReleaseBlocked):after_close_exposure(bad)


def test_remove_only_meli_orders_and_position(closure):
    from scripts.recover_after_meli import verify_post_close_transition
    before=dict(positions=[dict(symbol='MELIUSDT',positionSide='BOTH',positionAmt='-0.06',entryPrice='1637.44')],ordinary=[],algos=[dict(symbol='MELIUSDT',algoId=2000001555621647),dict(symbol='MELIUSDT',algoId=2000001555621787),dict(symbol='MELIUSDT',algoId=2000001555621677)])
    after=dict(positions=[],ordinary=[],algos=[],old_limit=closure['limit_order'],market_close=closure['market_order'],meli_trades=closure['trades'],algo_history={'MELIUSDT':closure['algo_history']})
    result=verify_post_close_transition(before,after)
    assert result['after_exposure']==[]
    assert result['missing_original_raw'] is True
    after['algos']=[dict(symbol='MELIUSDT',algoId=1)]
    with pytest.raises(ReleaseBlocked):verify_post_close_transition(before,after)


@pytest.mark.parametrize('fault',['fee','trade_id','commission_asset','stop_status','stop_order_link','stop_filled'])
def test_exact_history_and_cost_binding(closure,fault):
    from scripts.recover_after_meli import verify_close_evidence
    if fault=='fee':closure['trades'][0]['commission']='0'
    if fault=='trade_id':closure['trades'][0]['id']=2
    if fault=='commission_asset':closure['trades'][0]['commissionAsset']='BNB'
    if fault=='stop_status':closure['algo_history'][-1]['algoStatus']='NEW'
    if fault=='stop_order_link':closure['algo_history'][-1]['actualOrderId']='other'
    if fault=='stop_filled':closure['algo_history'][-1]['actualQty']='.01'
    with pytest.raises(ReleaseBlocked):verify_close_evidence(closure)


@pytest.fixture
def snapshots(closure):
    before=dict(positions=[dict(symbol='MELIUSDT',positionSide='BOTH',positionAmt='-0.06',entryPrice='1637.44')],ordinary=[],algos=[dict(symbol='MELIUSDT',algoId=i) for i in [2000001555621647,2000001555621787,2000001555621677]])
    after=dict(positions=[],ordinary=[],algos=[],old_limit=closure['limit_order'],market_close=closure['market_order'],meli_trades=closure['trades'],algo_history={'MELIUSDT':closure['algo_history']})
    return before,after

@pytest.mark.parametrize('fault',[None,'reason','code','prior_mode','attempted','evidence','origin'])
def test_load_binds_new_code_evidence_and_prior_context(tmp_path,monkeypatch,snapshots,fault):
    import json
    from pathlib import Path
    from scripts import recover_after_meli as mod
    before,after=snapshots;pre=tmp_path/'pre.json';pre.write_text(json.dumps(before));post=tmp_path/'post.json';post.write_text(json.dumps(after))
    monkeypatch.setattr(mod,'POST_EVIDENCE_SHA',mod.digest(post.read_bytes()))
    parent_approval={'signed_evidence_path':str(pre),'order_review':{'evidence_mode':'missing_old_raw_full_review'}}
    def parent_load(cls,root,bundle,approval):
        obj=cls.__new__(cls);obj.original=tmp_path;obj.approval=parent_approval;obj.capture={'position_exposure':mod.exposure(before['positions'])};return obj
    monkeypatch.setattr(mod.AfterHRSRuntime,'load',classmethod(parent_load))
    approval=dict(reason='authorized independent reduce-only close',recovery_code_sha256=mod.digest(Path(mod.__file__).read_bytes()),pre_meli_approval={},signed_post_meli_path=str(post))
    if fault=='reason':approval['reason']=''
    if fault=='code':approval['recovery_code_sha256']='wrong'
    if fault=='prior_mode':parent_approval['order_review']['evidence_mode']='other'
    if fault=='attempted':(tmp_path/'after-meli-recovery').mkdir()
    if fault=='evidence':post.write_text('{}')
    if fault=='origin':
        original_fn=mod.after_close_exposure
        calls=[]
        def changed(value):
            calls.append(True)
            return original_fn(value) if len(calls)==1 else ['different']
        monkeypatch.setattr(mod,'after_close_exposure',changed)
    if fault:
        with pytest.raises(ReleaseBlocked):mod.AfterMeliRuntime.load(tmp_path,tmp_path,approval)
    else:
        result=mod.AfterMeliRuntime.load(tmp_path,tmp_path,approval)
        assert result.close_transition['after_exposure']==[] and not result.directory.exists()

@pytest.mark.parametrize('fault',[None,'allocation','exposure','inventory_position','proof_order','inventory_order','close'])
def test_fresh_live_checks_cannot_expand_exception(snapshots,closure,fault):
    from scripts import recover_after_meli as mod
    before,after=snapshots;obj=mod.AfterMeliRuntime.__new__(mod.AfterMeliRuntime)
    obj.close_transition=mod.verify_post_close_transition(before,after);obj.capture={'allocation_sha256':'original'};obj.fresh={}
    obj.signed_close_evidence=lambda:closure
    proof=dict(position_exposure=[],allocation_sha256='original',ordinary_orders_sha256=mod.hash_rows([]),algo_orders_sha256=mod.hash_rows([]))
    inventory=dict(positions=[],orders=[],algos=[])
    if fault=='allocation':proof['allocation_sha256']='changed'
    if fault=='exposure':proof['position_exposure']=['other']
    if fault=='inventory_position':inventory['positions']=before['positions']
    if fault=='proof_order':proof['algo_orders_sha256']='changed'
    if fault=='inventory_order':inventory['orders']=[{'symbol':'MELIUSDT'}]
    if fault=='close':closure['market_order']['status']='NEW'
    if fault:
        with pytest.raises(ReleaseBlocked):obj.verify_live_economics(proof,inventory)
    else:
        obj.verify_live_economics(proof,inventory)
        assert obj.fresh['signed_close_evidence']==closure

def test_fresh_signed_queries_are_only_reads(closure):
    import json
    from scripts import recover_after_meli as mod
    obj=mod.AfterMeliRuntime.__new__(mod.AfterMeliRuntime);obj.inspect=lambda service:{'Id':'reviewed-hrs'};calls=[]
    def run(args,**kw):calls.append(args);return json.dumps(closure)
    obj.run=run
    assert obj.signed_close_evidence()==closure
    code=calls[0][-1];compile(code,'signed-query','exec')
    assert "'GET'" in code and "'POST'" not in code and 'cancel_order' not in code and 'place_order' not in code
    assert calls[0][:3]==['docker','exec','reviewed-hrs']

@pytest.mark.parametrize('fail',[False,True])
def test_final_endpoint_preserves_original_and_allocation(tmp_path,monkeypatch,snapshots,fail):
    from scripts import recover_after_meli as mod
    obj=mod.AfterMeliRuntime.__new__(mod.AfterMeliRuntime);obj.directory=tmp_path
    obj.close_transition=mod.verify_post_close_transition(*snapshots)
    old={'position_exposure':['MELI originally held'],'ordinary_orders_sha256':'old','algo_orders_sha256':'oldalgo','allocation_sha256':'never change'}
    obj.capture=old;obj.approval={'order_review':{'missing_old_raw':True}};seen=[]
    def action(self,name,*args):
        seen.append(self.capture)
        if fail:raise ReleaseBlocked('later unrelated change')
    monkeypatch.setattr(mod.Runtime,'action',action)
    if fail:
        with pytest.raises(ReleaseBlocked):obj.action('final_gate')
    else:obj.action('final_gate')
    assert obj.capture is old and seen[0]['allocation_sha256']=='never change' and seen[0]['position_exposure']==[]
    assert (tmp_path/'authorized-meli-transition.json').exists()

def test_nonfinal_delegates_without_replay(monkeypatch):
    from scripts import recover_after_meli as mod
    obj=mod.AfterMeliRuntime.__new__(mod.AfterMeliRuntime);seen=[]
    monkeypatch.setattr(mod.AfterHRSRuntime,'action',lambda self,*args:seen.append(args))
    obj.action('strategy_gate','hrs-strategy');assert seen==[('strategy_gate','hrs-strategy')]

@pytest.mark.parametrize('fail',[False,True])
def test_execution_audit_and_failure_pin_semantics(tmp_path,monkeypatch,fail):
    from scripts import recover_after_meli as mod
    obj=mod.AfterMeliRuntime.__new__(mod.AfterMeliRuntime);obj.directory=tmp_path/'audit';obj.full_approval={'reason':'close approved'};obj.fresh={};obj.validating=True
    seen=[];obj.record=lambda *args:seen.append(args)
    def suffix(runtime):
        assert runtime.validating is False
        if fail:raise ReleaseBlocked('late gate')
    monkeypatch.setattr(mod,'execute_after_hrs',suffix)
    if fail:
        with pytest.raises(ReleaseBlocked):obj.continue_release()
        assert seen==[('failed','ReleaseBlocked')]
    else:obj.continue_release()
    assert (obj.directory/'approval.json').exists()

@pytest.mark.parametrize('execute,fail',[(False,False),(True,False),(True,True)])
def test_cli_default_readonly_and_explicit_execution(tmp_path,monkeypatch,execute,fail):
    import json,sys
    from scripts import recover_after_meli as mod
    (tmp_path/'.deploy_tmp').mkdir();(tmp_path/'.deploy_tmp/compose_deploy.lock').touch();approval=tmp_path/'approval.json';approval.write_text('{}')
    calls=[]
    class Fake:
        sha='reviewed'
        def validate_live(self):
            calls.append('validate')
            if fail:raise ReleaseBlocked('drift')
        def continue_release(self):calls.append('execute')
    monkeypatch.setattr(mod.AfterMeliRuntime,'load',lambda *args:Fake())
    monkeypatch.setattr(sys,'argv',['recover','--root',str(tmp_path),'--bundle',str(tmp_path),'--approval',str(approval)]+(['--execute'] if execute else []))
    assert mod.main()==(2 if fail else 0)
    assert calls==(['validate','execute'] if execute and not fail else ['validate'])

@pytest.mark.parametrize('value',[None,True,'NaN','Infinity','nonsense',{},[]])
def test_numeric_evidence_invalid_is_typed(value):
    from scripts.recover_after_meli import number
    with pytest.raises(ReleaseBlocked):number(value)
