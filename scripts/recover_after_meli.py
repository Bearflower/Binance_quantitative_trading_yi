#!/usr/bin/env python3
"""Bound recovery after the separately authorized MELI reduce-only close."""
import json
import sys
from pathlib import Path
from decimal import Decimal,InvalidOperation
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.recover_after_hrs import require,exposure,verify_current_protection,AfterHRSRuntime,execute_after_hrs
from scripts.scoped_ledger_risk_release import Runtime,atomic_json
from scripts.ledger_risk_manifest import ReleaseBlocked,digest
from scripts.verify_ledger_risk_release import hash_rows,true

MELI='MELIUSDT'
POST_EVIDENCE_SHA='0dbad1540bdd398fcccf19453603c2215bcf1d0735360ff5f19592067bef06cb'

def number(value):
    try:
        require(not isinstance(value,bool),'boolean numeric evidence')
        result=Decimal(str(value));require(result.is_finite(),'nonfinite evidence');return result
    except (InvalidOperation,ValueError,TypeError) as exc:raise ReleaseBlocked('invalid numeric evidence') from exc

def after_close_exposure(before):
    meli=[row for row in before if row.get('symbol')==MELI]
    require(len(meli)==1 and meli[0].get('positionSide')=='BOTH' and number(meli[0].get('positionAmt'))==Decimal('-.06'),'original MELI exposure differs from authorized close')
    return [row for row in before if row.get('symbol')!=MELI]

def verify_close_evidence(evidence):
    old=evidence['limit_order'];new=evidence['market_order'];trades=evidence['trades'];history=evidence['algo_history']
    for row in (old,new):
        require(row.get('symbol')==MELI and row.get('side')=='BUY' and row.get('positionSide')=='BOTH' and true(row.get('reduceOnly')) and number(row.get('origQty'))==Decimal('.06'),'MELI close identity/side/size differs')
    require(old.get('orderId')==1785285 and old.get('status')=='CANCELED' and old.get('type')=='LIMIT' and number(old.get('executedQty'))==0 and number(old.get('price'))==Decimal('1721.03'),'original triggered limit not proven canceled unfilled')
    require(new.get('orderId')==1803995 and new.get('clientOrderId')=='codex-meli-stopclose-20261104-1' and new.get('status')=='FILLED' and new.get('type')=='MARKET' and number(new.get('executedQty'))==Decimal('.06') and number(new.get('avgPrice'))==Decimal('1749.30'),'authorized market close not proven fully filled')
    require(isinstance(trades,list) and bool(trades),'close fill evidence missing')
    ids=set();qty=Decimal(0);pnl=Decimal(0);fee=Decimal(0)
    for row in trades:
        require(row.get('id') is not None and row['id'] not in ids,'missing/duplicate trade ID');ids.add(row['id'])
        require(row.get('symbol')==MELI and row.get('orderId')==1803995 and row.get('side')=='BUY' and row.get('positionSide')=='BOTH','unrelated trade evidence')
        q=number(row.get('qty'));require(q>0 and number(row.get('price'))==Decimal('1749.30'),'close trade price/quantity differs')
        require(row.get('commissionAsset')=='USDT','unexpected commission asset')
        qty+=q;pnl+=number(row.get('realizedPnl'));fee+=number(row.get('commission'))
    require(qty==Decimal('.06') and pnl==Decimal('-6.7116'),'close trade quantity/PnL mismatch')
    require(ids=={38450} and fee==Decimal('0.04198320'),'signed trade ID/commission differs')
    stop=[row for row in history if row.get('algoId')==2000001555621647]
    require(len(stop)==1,'triggered stop history missing/duplicate')
    row=stop[0]
    require(row.get('algoStatus')=='FINISHED' and str(row.get('actualOrderId'))=='1785285' and row.get('symbol')==MELI and row.get('orderType')=='STOP' and row.get('side')=='BUY' and row.get('positionSide')=='BOTH' and true(row.get('reduceOnly')) and number(row.get('actualQty'))==0 and number(row.get('quantity'))==Decimal('.06') and number(row.get('triggerPrice'))==Decimal('1719.31') and number(row.get('price'))==Decimal('1721.03'),'triggered stop-to-limit chain differs')
    for identity in (2000001555621787,2000001555621677):
        matches=[row for row in history if row.get('algoId')==identity]
        require(len(matches)==1,'canceled TP evidence missing/duplicate')
        row=matches[0]
        require(row.get('symbol')==MELI and row.get('algoStatus')=='CANCELED' and row.get('orderType')=='TAKE_PROFIT' and row.get('side')=='BUY' and row.get('positionSide')=='BOTH' and true(row.get('reduceOnly')),'MELI TP cancellation unproven')
    return dict(closed_quantity='0.06',close_order_id=1803995,canceled_limit_id=1785285,fill_price='1749.30',realized_pnl='-6.7116',trade_ids=sorted(ids),commission_usdt='0.04198320',canceled_tp_ids=[2000001555621787,2000001555621677])


def close_evidence(snapshot):
    return dict(limit_order=snapshot['old_limit'],market_order=snapshot['market_close'],trades=[row for row in snapshot['meli_trades'] if row.get('orderId')==1803995],algo_history=snapshot['algo_history'][MELI])

def verify_post_close_transition(before,after):
    close=verify_close_evidence(close_evidence(after))
    expected=after_close_exposure(exposure(before['positions']))
    require(exposure(after['positions'])==expected,'unauthorized exposure change outside exact MELI close')
    for kind in ('ordinary','algos'):
        require(all(row.get('symbol')!=MELI for row in after[kind]),'MELI still has open orders')
        require([row for row in before[kind] if row.get('symbol')!=MELI]==after[kind],'other held-symbol orders changed')
    require({row.get('algoId') for row in before['algos'] if row.get('symbol')==MELI}=={2000001555621647,2000001555621787,2000001555621677},'unreviewed pre-close MELI order inventory')
    verify_current_protection(after['positions'],after['ordinary'],after['algos'],sorted({p['symbol'] for p in expected}))
    return dict(missing_original_raw=True,original_raw_comparison_performed=False,post_hrs_to_post_meli_other_orders_compared=True,close=close,after_exposure=expected,after_orders_sha256=hash_rows(after['ordinary']),after_algos_sha256=hash_rows(after['algos']))

class AfterMeliRuntime(AfterHRSRuntime):
    AUDIT_DIRECTORY='after-meli-recovery'
    @classmethod
    def load(cls,root,bundle,approval):
        require(isinstance(approval.get('reason'),str) and bool(approval['reason'].strip()),'MELI recovery reason required')
        require(digest(Path(__file__).read_bytes())==approval.get('recovery_code_sha256'),'MELI recovery code review mismatch')
        self=super().load(root,bundle,approval['pre_meli_approval'])
        require(self.approval['order_review'].get('evidence_mode')=='missing_old_raw_full_review','prior evidence gap must remain explicit')
        self.full_approval=approval
        self.directory=self.original/self.AUDIT_DIRECTORY;self.state_file=self.directory/'state.json'
        require(not self.directory.exists(),'MELI recovery already attempted')
        path=Path(approval['signed_post_meli_path'])
        require(not path.is_symlink() and digest(path.read_bytes())==POST_EVIDENCE_SHA,'independent signed close evidence mismatch')
        self.post=json.loads(path.read_text())
        pre=json.loads(Path(self.approval['signed_evidence_path']).read_text())
        self.close_transition=verify_post_close_transition(pre,self.post)
        require(after_close_exposure(self.capture['position_exposure'])==self.close_transition['after_exposure'],'authorized transition does not originate at original exposure')
        return self

    def signed_close_evidence(self):
        code="import sys,asyncio,json;sys.path.insert(0,'/app');from scripts.export_execution_facts import _build_client_from_env\nasync def main():\n c,error=_build_client_from_env()\n if error or c is None or not c.use_unified_account:raise RuntimeError('PM client unavailable')\n try:print(json.dumps({'limit_order':await c.get_order('MELIUSDT',1785285),'market_order':await c.get_order('MELIUSDT',1803995),'trades':await c.get_user_trades('MELIUSDT',order_id=1803995,limit=100),'algo_history':await c._request('GET','/papi/v1/um/algo/allAlgoOrders',{'symbol':'MELIUSDT','limit':100},signed=True)},sort_keys=True,default=str))\n finally:await c.close()\nasyncio.run(main())"
        row=self.inspect('hrs-strategy')
        return json.loads(self.run(['docker','exec',row['Id'],'python','-c',code],timeout=180).splitlines()[-1])

    def verify_live_economics(self,proof,inventory):
        expected=self.close_transition
        require(proof['allocation_sha256']==self.capture['allocation_sha256'],'original holdings allocation changed')
        require(proof['position_exposure']==expected['after_exposure'] and exposure(inventory['positions'])==expected['after_exposure'],'exposure differs from authorized post-close endpoint')
        require(proof['ordinary_orders_sha256']==expected['after_orders_sha256']==hash_rows(inventory['orders']) and proof['algo_orders_sha256']==expected['after_algos_sha256']==hash_rows(inventory['algos']),'unreviewed current order change')
        require(all(row.get('symbol')!=MELI for row in inventory['orders']+inventory['algos']),'MELI open orders remain')
        verify_current_protection(inventory['positions'],inventory['orders'],inventory['algos'],sorted({p['symbol'] for p in expected['after_exposure']}))
        current=self.signed_close_evidence();require(verify_close_evidence(current)==expected['close'],'fresh signed close evidence changed')
        self.fresh.update(signed_inventory=inventory,signed_close_evidence=current,approved_transition=expected)

    def action(self,name,*args):
        if name=='final_gate':
            original=self.capture;endpoint=self.close_transition
            atomic_json(self.directory/'authorized-meli-transition.json',dict(original_exposure=original['position_exposure'],original_orders_sha256=original['ordinary_orders_sha256'],original_algos_sha256=original['algo_orders_sha256'],prior_hrs_review=self.approval['order_review'],transition=endpoint))
            self.capture=dict(original,position_exposure=endpoint['after_exposure'],ordinary_orders_sha256=endpoint['after_orders_sha256'],algo_orders_sha256=endpoint['after_algos_sha256'])
            try:return Runtime.action(self,name,*args)
            finally:self.capture=original
        return super().action(name,*args)

    def continue_release(self):
        self.directory.mkdir(mode=0o700)
        atomic_json(self.directory/'approval.json',self.full_approval);atomic_json(self.directory/'validated-live.json',self.fresh)
        self.validating=False
        try:execute_after_hrs(self)
        except Exception as exc:self.record('failed',type(exc).__name__);raise

def main():
    import argparse,fcntl
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('--bundle',required=True);p.add_argument('--approval',required=True);p.add_argument('--execute',action='store_true');a=p.parse_args()
    try:
        with open(Path(a.root)/'.deploy_tmp/compose_deploy.lock','r+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            runtime=AfterMeliRuntime.load(a.root,a.bundle,json.loads(Path(a.approval).read_text()));runtime.validate_live()
            if a.execute:runtime.continue_release()
            print(json.dumps({'ok':True,'executed':a.execute,'sha':runtime.sha}))
        return 0
    except Exception as exc:print('MELI recovery blocked: '+type(exc).__name__,file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
