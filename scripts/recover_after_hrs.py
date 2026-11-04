#!/usr/bin/env python3
"""Exact post-HRS recovery. Defaults to validation; explicit execute resumes only the remaining services."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from datetime import datetime
from decimal import Decimal
from scripts.ledger_risk_manifest import ReleaseBlocked
from scripts.verify_ledger_risk_release import verify_exchange, hash_rows, positive, true, GateError

def require(condition,reason):
    if not condition:raise ReleaseBlocked(reason)

def log_events(logs):
    """Decode structured events, including docker JSON envelope/timestamp prefix."""
    for line in logs.splitlines():
        line=line.strip()
        if not line:continue
        if 'Traceback (most recent call last)' in line:raise ReleaseBlocked('startup traceback')
        if not line.startswith('{'):
            prefix,sep,rest=line.partition(' ')
            if not sep:continue
            try:datetime.fromisoformat(prefix.replace('Z','+00:00'))
            except ValueError:continue
            line=rest.lstrip()
        try:row=json.loads(line)
        except (ValueError,TypeError):continue
        if not isinstance(row,dict):continue
        if isinstance(row.get('log'),str):
            yield from log_events(row['log'])
        elif isinstance(row.get('event'),str):yield row

def require_startup(logs,service):
    markers={'hrs-strategy':'策略初始化完成，开始运行','new-coin-strategy':'策略初始化完成，开始运行','btc-eth-strategy':'调度器已启动','btc-eth-aggressive-strategy':'调度器已启动'}
    require(service in markers,'unknown startup service')
    ready=False
    for event in log_events(logs):
        if event.get('level','').lower() in ('error','critical','fatal') or event['event']=='策略执行失败':raise ReleaseBlocked('structured startup failure')
        if event['event']==markers[service]:ready=True
    if not ready:raise StartupPending('structured startup event missing')

def exposure(positions):
    return sorted([{k:row.get(k) for k in ('symbol','positionSide','positionAmt','entryPrice')} for row in positions if Decimal(str(row['positionAmt']))!=0],key=lambda row:(row['symbol'],str(row['positionSide'])))

def verify_reviewed_rebuild(positions,before_orders,before_algos,orders,algos,review):
    """Accept only explicitly hash-reviewed stop/TP replacements, never new risk."""
    require(isinstance(review.get('reason'),str) and bool(review['reason'].strip()),'order review reason missing')
    symbols=review.get('symbols')
    require(isinstance(symbols,list) and bool(symbols) and len(set(symbols))==len(symbols),'reviewed symbols required')
    require(exposure(positions)==review.get('position_exposure'),'signed exposure changed')
    for key,rows in [('before_orders_sha256',before_orders),('before_algos_sha256',before_algos),('after_orders_sha256',orders),('after_algos_sha256',algos)]:
        require(hash_rows(rows)==review.get(key),'unreviewed order identity transition')
    for old,new in ((before_orders,orders),(before_algos,algos)):
        require([r for r in old if r.get('symbol') not in symbols]==[r for r in new if r.get('symbol') not in symbols],'unrelated orders changed')
    verify_current_protection(positions,orders,algos,symbols)
    return {'approved_symbols':symbols,'reason':review['reason'],'before_orders_sha256':review['before_orders_sha256'],'before_algos_sha256':review['before_algos_sha256'],'after_orders_sha256':review['after_orders_sha256'],'after_algos_sha256':review['after_algos_sha256']}

def verify_current_protection(positions,orders,algos,symbols):
    try:
        verify_exchange(positions,orders,algos)
        by_position={(p['symbol'],p.get('positionSide','BOTH')):p for p in positions if Decimal(str(p['positionAmt']))!=0}
        require(set(symbols)<={k[0] for k in by_position},'review symbol has no held position')
        totals={};identities=set()
        for row in orders+algos:
            if row.get('symbol') not in symbols:continue
            key=(row['symbol'],row.get('positionSide','BOTH'));p=by_position.get(key)
            require(p is not None,'order does not match a held position')
            amount=Decimal(str(p['positionAmt']));entry=positive(p['entryPrice'])
            require(row.get('side')==('SELL' if amount>0 else 'BUY'),'protection side mismatch')
            require(true(row.get('reduceOnly')) or true(row.get('closePosition')),'non-reducing order')
            require(row.get('status',row.get('algoStatus'))=='NEW','order not confirmed NEW')
            identity=(row.get('orderId'),row.get('algoId'))
            require(identity!=(None,None) and identity not in identities,'missing/duplicate order identity');identities.add(identity)
            kind=row.get('type',row.get('orderType'))
            require(kind in ('STOP','STOP_MARKET','TAKE_PROFIT','TAKE_PROFIT_MARKET'),'unexpected protection order type')
            if kind.startswith('TAKE_PROFIT'):
                require(not true(row.get('closePosition')),'unbounded TP quantity')
                trigger=positive(row.get('stopPrice',row.get('triggerPrice')))
                price=positive(row.get('price')) if kind=='TAKE_PROFIT' else trigger
                require(all(v>entry if amount>0 else v<entry for v in (trigger,price)),'TP on loss side')
                remaining=positive(row.get('origQty',row.get('quantity')))-positive(row.get('executedQty','0'),zero=True)
                require(remaining>0,'invalid remaining TP quantity')
                totals[key]=totals.get(key,Decimal(0))+remaining
                require(totals[key]<=abs(amount),'TP quantities exceed held exposure')
    except (GateError,KeyError,ValueError,TypeError,ArithmeticError) as exc:
        raise ReleaseBlocked('invalid reviewed protection evidence') from exc


def verify_gap_review(positions,orders,algos,review):
    require(review.get('evidence_mode')=='missing_old_raw_full_review' and review.get('missing_old_raw') is True,'old raw evidence gap must be explicit')
    require(isinstance(review.get('reason'),str) and bool(review['reason'].strip()),'independent protection review required')
    require(exposure(positions)==review.get('position_exposure'),'original signed exposure changed')
    require(not orders and hash_rows(orders)==review.get('after_orders_sha256') and hash_rows(algos)==review.get('after_algos_sha256'),'current full order review drift')
    symbols=sorted({row['symbol'] for row in exposure(positions)})
    require(review.get('reviewed_position_symbols')==symbols,'all held symbols require explicit current protection review')
    verify_current_protection(positions,orders,algos,symbols)
    pairs=[(2000001576528510,2000001577898163,'STOP_MARKET',True,'0.12977','0'),(2000001576528538,2000001577898176,'TAKE_PROFIT_MARKET',False,'0.1068','16')]
    history=review.get('aria_history',[])
    require({r.get('algoId') for r in algos if r.get('symbol')=='ARIAUSDT'}=={p[1] for p in pairs},'ARIA order membership changed')
    for old_id,new_id,kind,close,trigger,quantity in pairs:
        old=[r for r in history if r.get('algoId')==old_id];new=[r for r in algos if r.get('algoId')==new_id]
        require(len(old)==len(new)==1,'historical order transition identity missing')
        for row,status in ((old[0],'CANCELED'),(new[0],'NEW')):
            require(row.get('algoStatus')==status and row.get('symbol')=='ARIAUSDT' and row.get('side')=='BUY' and row.get('positionSide')=='BOTH' and row.get('orderType')==kind and true(row.get('reduceOnly')) and row.get('closePosition') is close,'historical order transition semantics changed')
            require(positive(row.get('triggerPrice'))==Decimal(trigger) and positive(row.get('quantity'),zero=True)==Decimal(quantity),'historical protection price/quantity changed')
            require(positive(row.get('price','0'),zero=True)==0,'unexpected market-order limit price')
        for field in ('workingType','timeInForce','priceProtect','priceMatch','selfTradePreventionMode'):
            require(old[0].get(field)==new[0].get(field),'historical protection execution settings changed')
    return dict(missing_old_raw=True,old_raw_comparison_performed=False,reviewed_current_position_symbols=symbols,aria_equivalent_replacement_pairs=[list(p[:2]) for p in pairs],reason=review['reason'])


def execute_after_hrs(backend):
    """Only call after bound state, policy, collector and exchange review gates."""
    backend.action('strategy_gate','hrs-strategy')
    for service in ('btc-eth-aggressive-strategy','btc-eth-strategy','new-coin-strategy'):
        backend.action('up',service);backend.action('strategy_gate',service)
    for service in ('data-backend','dashboard-api'):
        backend.action('up',service);backend.action('consumer_gate',service)
    backend.action('freeze_gate');backend.action('up','ai-tuner')
    backend.action('final_gate');backend.action('success')


from scripts.scoped_ledger_risk_release import Runtime,atomic_json
from scripts.recover_ledger_risk_release import FAILED_SHA,MANIFEST_DIGEST
from scripts.ledger_risk_manifest import RELEASE,MANIFEST,SERVICES,SERVICE_IMAGES,digest,validate_manifest

def validate_phase2_state(state):
    require(all(state.get(k)==v for k,v in dict(sha=FAILED_SHA,manifest_sha256=MANIFEST_DIGEST,status='failed',step='ReleaseBlocked',changed=['execution-facts-collector','hrs-strategy'],no_automatic_rollback=True).items()),'not the exact post-HRS failure boundary')

class StartupPending(ReleaseBlocked):pass

class AfterHRSRuntime(Runtime):
    """Separate audit directory; existing successful activation is never replayed."""
    @classmethod
    def load(cls,root,bundle,approval):
        self=cls.__new__(cls);self.root=Path(root).resolve();self.bundle=Path(bundle).resolve();self.sha=FAILED_SHA
        self.original=self.root/'.releases'/RELEASE/FAILED_SHA;self.previous=self.original/'manual-recovery'
        self.directory=self.original/'after-hrs-recovery';self.state_file=self.directory/'state.json'
        self.pin_file=self.root/'.deploy_tmp/ledger-risk-active.json';self.overlay=self.original/'images.json'
        require(not self.directory.exists(),'second-stage recovery already attempted')
        self.approval=approval;require(bool(approval.get('reason')),'review reason required')
        required=['state.json','manifest.json','images.json','preflight-containers.json','preflight-proof.json','schema-proof.json','manual-recovery/state.json','manual-recovery/policy-activation.json','manual-recovery/policy-proof.json','manual-recovery/collector-proof.json']+[service+'-effective.json' for service in SERVICES]
        require(set(approval.get('artifacts',{}))==set(required),'complete original/stage-one artifact review required')
        for name in required:
            path=self.original/name
            require(not path.is_symlink() and digest(path.read_bytes())==approval['artifacts'][name],'artifact drift: '+name)
        modules=['recover_after_hrs.py','recover_ledger_risk_release.py','scoped_ledger_risk_release.py','ledger_risk_manifest.py','ledger_risk_runtime_config.py','verify_ledger_risk_release.py']
        require(set(approval.get('code',{}))==set(modules),'complete recovery code review required')
        for name in modules:require(digest((Path(__file__).parent/name).read_bytes())==approval['code'][name],'recovery code drift')
        pin=json.loads(self.pin_file.read_text())
        require(digest(self.pin_file.read_bytes())==approval.get('pin_sha256') and pin.get('sha')==FAILED_SHA and pin.get('status')=='activating' and pin.get('overlay')==str(self.overlay),'pin drift')
        state=json.loads((self.previous/'state.json').read_text());validate_phase2_state(state)
        self.manifest=json.loads((self.bundle/MANIFEST).read_text());validate_manifest(self.manifest,activation=True)
        require(digest(self.manifest)==MANIFEST_DIGEST,'original manifest drift')
        self.production=self.manifest['expected_production'];self.images=state['images']
        require(self.images==json.loads((self.bundle/'image-digests.json').read_text()),'image map drift')
        require(json.loads(self.overlay.read_text())=={'services':{s:{'image':self.images[SERVICE_IMAGES[s]]} for s in SERVICES}},'overlay drift')
        self.before=json.loads((self.original/'preflight-containers.json').read_text())
        self.capture=json.loads((self.original/'preflight-proof.json').read_text())
        self.changed=list(state['changed']);self.validating=True;self.fresh={}
        require(set(approval.get('containers',{}))==set(self.before),'complete reviewed current container inventory required')
        review=approval['order_review']
        require(review.get('symbols')==['ARIAUSDT'],'only reviewed HRS ARIA protection replacement allowed')
        require(review.get('position_exposure')==self.capture['position_exposure'],'review attempts to replace original exposure')
        require(review.get('before_orders_sha256')==self.capture['ordinary_orders_sha256'] and review.get('before_algos_sha256')==self.capture['algo_orders_sha256'],'review not bound to original orders')
        if review.get('evidence_mode')=='missing_old_raw_full_review':
            evidence_path=Path(approval['signed_evidence_path'])
            expected='d1ebf9dacd301b707c03b506b939183d3d00b737b017fc32a312ffc4c07bf4ff'
            require(digest(evidence_path.read_bytes())==expected,'independently acquired signed review evidence mismatch')
            evidence=json.loads(evidence_path.read_text())
            require(review.get('aria_history')==evidence['algo_history']['ARIAUSDT'],'historical review rows changed')
            require(review.get('after_orders_sha256')==hash_rows(evidence['ordinary']) and review.get('after_algos_sha256')==hash_rows(evidence['algos']),'review not bound to signed evidence')
            verify_gap_review(evidence['positions'],evidence['ordinary'],evidence['algos'],review)
        return self

    def probe(self,stage):
        if not self.validating:return super().probe(stage)
        require(stage=='policy','validation may only read current policy/exchange')
        p=self.manifest['policy'];args=self.compose('hrs-strategy')+['run','--rm','--no-deps','--entrypoint','python','hrs-strategy','/app/scripts/verify_ledger_risk_release.py','--stage','policy','--account',p['account'],'--revision',str(p['expected_revision']+1)]
        if p['enable_reduced']:args+=['--require-reduced']
        result=json.loads(self.run(args,timeout=180).splitlines()[-1]);require(result.get('ok') is True,'current policy gate failed')
        self.fresh['policy']=result;return result

    def signed_inventory(self):
        code="import sys,asyncio,json;sys.path.insert(0,'/app');from scripts.export_execution_facts import _build_client_from_env\nasync def main():\n c,error=_build_client_from_env()\n if error or c is None:raise RuntimeError('client unavailable')\n try:print(json.dumps({'positions':await c.get_position(),'orders':await c.get_open_orders(),'algos':await c.get_open_algo_orders()},sort_keys=True,default=str))\n finally:await c.close()\nasyncio.run(main())"
        row=self.inspect('hrs-strategy')
        return json.loads(self.run(['docker','exec',row['Id'],'python','-c',code],timeout=180).splitlines()[-1])

    def upgraded_services(self):
        return ('hrs-strategy','execution-facts-collector')

    def validate_live(self):
        for path,sha in self.production['compose_sha256'].items():require(digest((self.root/path).read_bytes())==sha,'Compose drift')
        expected=self.approval['containers']
        ids=set(self.run(['docker','ps','-a','--no-trunc','--filter','label=com.docker.compose.project='+self.production['project'],'--format','{{.ID}}']).splitlines())
        require(ids=={r['id'] for r in expected.values()},'project inventory drift')
        for service,approved in expected.items():
            row=self.inspect(service)
            require((row['Id'],row['Image'],row['State']['StartedAt'])==(approved['id'],approved['image_id'],approved['started_at']),'reviewed current identity drift')
            require(digest({'Config':row['Config'],'HostConfig':row['HostConfig']})==approved['config_sha256'],'reviewed current settings drift')
            old=self.before[service]
            if service not in self.upgraded_services():
                require((row['Id'],row['Image'],row['State']['StartedAt'])==(old['Id'],old['Image'],old['State']['StartedAt']),'unexpected preceding rollout')
            require(row['State']['Status']==('exited' if service=='ai-tuner' else old['State']['Status']),'unexpected lifecycle')
            if service in self.upgraded_services():
                require(row['State'].get('Health',{}).get('Status')=='healthy','new HRS/collector unhealthy')
                require(row['Image']==self.run(['docker','image','inspect','--format','{{.Id}}',self.images[SERVICE_IMAGES[service]]]),'new HRS/collector wrong image')
                self.image_proof(self.images[SERVICE_IMAGES[service]],container=row['Id'])
        for service in SERVICES:
            effective=json.loads(self.run(self.compose(service)+['config','--format','json']))['services'][service]
            require(effective==json.loads((self.original/(service+'-effective.json')).read_text()),'effective configuration drift')
        proof=self.probe('policy')
        inventory=self.signed_inventory()
        self.verify_live_economics(proof,inventory)
        row=self.inspect('hrs-strategy');logs=self.run(['docker','logs','--since',row['State']['StartedAt'],row['Id']],include_stderr=True)
        require_startup(logs,'hrs-strategy');self.fresh['hrs_startup_logs']=logs

    def verify_live_economics(self,proof,inventory):
        require(proof['position_exposure']==self.capture['position_exposure'] and proof['allocation_sha256']==self.capture['allocation_sha256'],'original economic baseline changed')
        review=self.approval['order_review']
        require(hash_rows(inventory['orders'])==proof['ordinary_orders_sha256'] and hash_rows(inventory['algos'])==proof['algo_orders_sha256'],'exchange inventory changed during verification')
        transition=verify_gap_review(inventory['positions'],inventory['orders'],inventory['algos'],review) if review.get('evidence_mode')=='missing_old_raw_full_review' else verify_reviewed_rebuild(inventory['positions'],self.approval['before_orders'],self.approval['before_algos'],inventory['orders'],inventory['algos'],review)
        self.fresh['signed_inventory']=inventory;self.fresh['approved_transition']=transition

    def action(self,name,*args):
        if name=='strategy_gate':
            service=args[0];row=self.inspect(service)
            import time
            deadline=time.monotonic()+300
            while True:
                logs=self.run(['docker','logs','--since',row['State']['StartedAt'],row['Id']],include_stderr=True)
                try:require_startup(logs,service);break
                except StartupPending:
                    if time.monotonic()>=deadline:raise
                    time.sleep(3)
            atomic_json(self.directory/(service+'-startup.json'),{'container_id':row['Id'],'logs':logs})
            self.probe('strategy_gate');self.record('in_progress','strategy_gate');return
        if name=='final_gate':
            # Explicit endpoint of approved transition, with original economic baseline.
            # Persist chain and never rewrite original capture/proofs.
            original=self.capture;review=self.approval['order_review']
            atomic_json(self.directory/'reviewed-order-transition.json',review)
            self.capture=dict(original,ordinary_orders_sha256=review['after_orders_sha256'],algo_orders_sha256=review['after_algos_sha256'])
            try:return super().action(name,*args)
            finally:self.capture=original
        return super().action(name,*args)

    def continue_release(self):
        self.directory.mkdir(mode=0o700);atomic_json(self.directory/'approval.json',self.approval);atomic_json(self.directory/'validated-live.json',self.fresh)
        self.validating=False
        try:execute_after_hrs(self)
        except Exception as exc:self.record('failed',type(exc).__name__);raise

def main():
    import argparse,fcntl
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--bundle',required=True);p.add_argument('--approval',required=True);p.add_argument('--execute',action='store_true');a=p.parse_args()
    try:
        with open(Path(a.root)/'.deploy_tmp/compose_deploy.lock','r+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            runtime=AfterHRSRuntime.load(a.root,a.bundle,json.loads(Path(a.approval).read_text()));runtime.validate_live()
            if a.execute:runtime.continue_release()
            print(json.dumps({'ok':True,'executed':a.execute,'sha':FAILED_SHA}))
        return 0
    except Exception as exc:print('Second-stage recovery blocked: '+type(exc).__name__,file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
