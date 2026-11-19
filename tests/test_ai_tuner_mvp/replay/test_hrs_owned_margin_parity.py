"""Original source export to real replay parity for HRS quota and actual cash."""
from decimal import Decimal as D
import pytest
from ai_tuner.replay.engine import run_replay
from strategies.hrs.margin_budget import HRS_OWNED
from .test_hrs_minimum_budget import floor_fixture
from .test_hrs_wiring import broker


def owned_fixture(tmp_path, **kwargs):
    return floor_fixture(tmp_path, margin_scope=HRS_OWNED, allocation='26.18', ceiling='26.18', foreign_margin='64.86', **kwargs)


def test_owned_quota_ignores_proven_foreign_margin_and_expired_entry_claim(tmp_path):
    portal,adapter=owned_fixture(tmp_path)
    result=run_replay(portal,adapter,broker())
    assert result.final_position is not None
    risk=next(e['payload'] for e in result.audit_events if e['type']=='hrs_account_reservation')
    assert D(risk['other_margin'])==D('64.86')
    assert D(risk['remaining_margin'])==D('26.18')
    assert risk['hrs_margin_scope']==HRS_OWNED


def test_owned_quota_preserves_actual_account_cash_ceiling(tmp_path):
    portal,adapter=owned_fixture(tmp_path,cash='1')
    result=run_replay(portal,adapter,broker())
    assert result.final_position is None
    assert any(e['type']=='hrs_reject' and e['payload']['reason']=='minimum_order_exceeds_margin' for e in result.audit_events)


@pytest.mark.parametrize('problem',['cash','source','status','clock','stale_clock','scope','pending','owner','foreign_conflict','scope_change'])
def test_owned_original_sources_fail_closed(tmp_path,problem):
    from strategies.hrs.decision_capture import canonical_payload
    def corrupt(sources):
        starts=[r for r in sources['decision_evidence'] if r['kind']=='scan_start']
        for index,row in enumerate(starts):
            f=row['payload']['financial_context']
            if problem=='cash':f.pop('account_available_balance')
            elif problem=='source':f['account_available_balance_source']='inferred'
            elif problem=='status':f['account_status']='MARGIN_CALL'
            elif problem=='clock':f.pop('account_cash_observed_at')
            elif problem=='stale_clock':f['account_cash_observed_at']='2020-01-01T00:00:00+00:00'
            elif problem=='scope':f.pop('hrs_margin_scope')
            elif problem=='pending':f['account_pending_reservations']=[dict(state='mystery')]
            elif problem=='owner':
                parent=next(e for e in sources['equity_evidence'] if e['payload'].get('account_id')=='test' and e['payload']['as_of']==row['available_at'])
                parent['payload']['order_owners']=[]
                from strategies.hrs.equity_evidence import canonical
                parent['evidence_sha256']=canonical(parent['payload'])[1]
                f['equity_gate']['evidence_sha256']=parent['evidence_sha256']
            elif problem=='foreign_conflict':f['ownership_claims'].append(dict(id=2,symbol='F',strategy='hrs',claim_state='ACTIVE'))
            elif problem=='scope_change' and index:
                import json
                f['entry_policy']['reason']=json.dumps({'hrs_risk_budget_version':'hrs_min_order_budget_v1'})
            row['payload_sha256']=canonical_payload(row['payload'])[1]
    result=owned_fixture(tmp_path,source_transform=corrupt,export_result=True)
    assert result.get('decision')=='BLOCKED_DATA',result


@pytest.mark.parametrize('state,margin,opens',[('reserved','448',False),('submitting','448',False),('released','448',True)])
def test_captured_foreign_pending_retains_cash_until_released(tmp_path,state,margin,opens):
    from strategies.hrs.decision_capture import canonical_payload
    def pending(sources):
        for row in sources['decision_evidence']:
            if row['kind']=='scan_start':
                row['payload']['financial_context']['account_pending_reservations']=[dict(client_order_id='foreign',account_id='test',strategy_id='grid',state=state,margin=margin)]
                row['payload_sha256']=canonical_payload(row['payload'])[1]
    portal,adapter=owned_fixture(tmp_path,source_transform=pending)
    result=run_replay(portal,adapter,broker())
    assert (result.final_position is not None)==opens


def test_owned_quota_retains_modeled_hrs_pending_margin(tmp_path):
    from datetime import timedelta
    from .test_hrs_wiring import AT
    from strategies.hrs.risk_budget import assess_entry,FLOOR
    portal,adapter=owned_fixture(tmp_path)
    ctx=adapter.prepare(portal);ctx.portfolio=broker().view();adapter._bootstrap(ctx)
    now=AT+timedelta(hours=1);raw=portal.hrs_inputs('A',now,decision_at=now)
    decision=assess_entry(budget_version=FLOOR,allocation=D('26.18'),entry_price=D(100),stop_price=D(95),direction='long',existing_open_risk=D(0),leverage=D(2),remaining_margin=D('26.18'),qty_step=D('.001'),min_qty=D('.001'),min_notional=D(5),fee_rate=D('.0005'),pressure_slippage=D('.02'),take_profit_prices=(D(103),D(107)))
    adapter._durable_margin_reservations['P']=dict(margin=D(25),risk=D('.1'),terminal=False)
    assert adapter._reservation_denial('A',decision.quantity,D(100),ctx,now,raw['risk'],D('26.18'),decision=decision)=='minimum_order_exceeds_margin'


@pytest.mark.parametrize('locked_pending',[False,True])
def test_independent_reservation_cash_overrides_scan_cash_and_raw_projection_is_verified(tmp_path,locked_pending):
    import copy,json
    from .test_hrs_replay_review import refreeze,jsonl
    from strategies.hrs.decision_capture import canonical_payload
    def receipt(sources):
        for row in sources['decision_evidence']:
            if row['kind']!='scan_start':continue
            financial=row['payload']['financial_context']
            stage=next((r for r in sources['decision_evidence'] if r['run_id']==row['run_id'] and r['kind']=='entry_stage_inputs'),None)
            if stage is None:continue
            stage['payload'].update(stage='reservation_inventory',slot_scope=financial['runtime_slot_scope'],slot_inputs=copy.deepcopy(financial['hrs_slot_inputs']),
                **{k:financial[k] for k in ('hrs_margin_scope','account_available_balance_source','account_status','account_cash_observed_at')})
            stage['payload']['account_available_balance']='448.86' if locked_pending else '1'
            stage['payload_sha256']=canonical_payload(stage['payload'])[1]
            locked=copy.deepcopy(stage)
            locked['evidence_id']+='-lock'
            pending=[dict(client_order_id='late-foreign',account_id='test',strategy_id='grid',state='reserved',margin='448')] if locked_pending else []
            locked['payload']=dict(stage='reservation_denial',risk_context=dict(financial['equity_gate'],hrs_margin_scope=HRS_OWNED,ownership_claims=copy.deepcopy(financial['ownership_claims']),account_pending_reservations=pending))
            locked['payload_sha256']=canonical_payload(locked['payload'])[1]
            sources['decision_evidence'].append(locked)
    portal,adapter=owned_fixture(tmp_path,source_transform=receipt)
    assert run_replay(portal,adapter,broker()).final_position is None
    inputs=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    for row in inputs:row['risk']['account_available_balance']='999999'
    from ai_tuner.replay.data_portal import DataPortal,compute_manifest_envelope_sha256,compute_manifest_sha256,sha256_file
    path=portal.snapshot_dir/'hrs_decision_inputs.jsonl';path.write_text(jsonl(inputs))
    manifest_path=portal.snapshot_dir/'manifest.json';manifest=json.loads(manifest_path.read_text())
    manifest['files'][path.name]['sha256']=sha256_file(str(path))
    manifest['sha256']=compute_manifest_sha256(manifest['files'])
    manifest['envelope_sha256']=compute_manifest_envelope_sha256(manifest)
    manifest_path.write_text(json.dumps(manifest))
    changed=DataPortal(portal.snapshot_dir,'hrs',portal.as_of,portal.window_start)
    from ai_tuner.replay.models import DataBlocked
    with pytest.raises(DataBlocked,match='margin_raw_source'):
        run_replay(changed,type(adapter)(adapter._state_loader),broker())


@pytest.mark.parametrize('field,value',[('account_available_balance','999999'),('account_cash_observed_at','2026-11-16T23:59:59+00:00')])
def test_bootstrap_margin_projection_requires_original_receipt(tmp_path,field,value):
    import json
    from ai_tuner.replay.data_portal import DataPortal,compute_manifest_envelope_sha256,compute_manifest_sha256,sha256_file
    from ai_tuner.replay.models import DataBlocked
    portal,adapter=owned_fixture(tmp_path)
    path=portal.snapshot_dir/'hrs_replay_state.json';state=json.loads(path.read_text());state[field]=value;path.write_text(json.dumps(state))
    manifest_path=portal.snapshot_dir/'manifest.json';manifest=json.loads(manifest_path.read_text())
    manifest['files'][path.name]['sha256']=sha256_file(str(path));manifest['sha256']=compute_manifest_sha256(manifest['files'])
    manifest['envelope_sha256']=compute_manifest_envelope_sha256(manifest);manifest_path.write_text(json.dumps(manifest))
    changed=DataPortal(portal.snapshot_dir,'hrs',portal.as_of,portal.window_start)
    with pytest.raises(DataBlocked,match='bootstrap_margin_raw_source'):
        run_replay(changed,adapter,broker())


def test_owned_cash_tiny_shortfall_is_not_rounded_up(tmp_path):
    from copy import deepcopy
    from datetime import timedelta
    from .test_hrs_wiring import AT
    portal,adapter=owned_fixture(tmp_path)
    ctx=adapter.prepare(portal);ctx.portfolio=broker().view();adapter._bootstrap(ctx)
    now=AT+timedelta(hours=1);risk=deepcopy(portal.hrs_inputs('A',now,decision_at=now)['risk'])
    risk['account_available_balance']='1000000000000000000002.499999999999999999999999999999999999'
    risk['account_pending_reservations']=[dict(client_order_id='foreign',account_id='test',strategy_id='grid',state='reserved',margin='1000000000000000000000')]
    assert adapter._reservation_denial('A',D('.05'),D(100),ctx,now,risk,D('26.18'))=='entry_ceiling_includes_account_and_durable_reservations'


def test_later_success_receipt_pending_supersedes_scan_pending(tmp_path):
    import copy
    from tests.test_ai_tuner_mvp.test_hrs_evidence_export import envelope
    from strategies.hrs.entry_occupancy import prove_hrs_slot_ownership
    from ai_tuner.replay.models import parse_utc
    def locked(sources):
        for row in list(sources['decision_evidence']):
            if row['kind']!='scan_start' or row['run_id']=='bootstrap':continue
            f=row['payload']['financial_context'];at=parse_utc(row['available_at'])
            parent=next(p for p in sources['equity_evidence'] if p['evidence_sha256']==f['equity_gate']['evidence_sha256'])
            proof=prove_hrs_slot_ownership('test',parent,f['hrs_slot_inputs'],f['ownership_claims'],checked_at=at,margin_scope=HRS_OWNED)
            context=dict(f['equity_gate'],hrs_margin_scope=HRS_OWNED,ownership_claims=f['ownership_claims'],account_pending_reservations=[dict(client_order_id='later-foreign',account_id='test',strategy_id='grid',state='reserved',margin='448.86')])
            sources['decision_evidence'].append(envelope(row['run_id'],'entry_stage_inputs',dict(stage='reservation_success',slot_context=context,budget_context=copy.deepcopy(context),slot_occupancy_proof=proof),at,'A'))
    portal,adapter=owned_fixture(tmp_path,source_transform=locked)
    result=run_replay(portal,adapter,broker())
    assert result.final_position is None
    from .test_hrs_wiring import AT
    from datetime import timedelta
    risk=portal.hrs_inputs('A',AT+timedelta(hours=1),decision_at=AT+timedelta(hours=1))['risk']
    assert risk['account_pending_reservations'][0]['client_order_id']=='later-foreign'
    assert risk['account_pending_source_evidence_id']!=risk['account_occupancy']['slot_source_evidence_id']


def test_new_inventory_cash_without_original_locked_pending_is_blocked(tmp_path):
    import copy
    from strategies.hrs.decision_capture import canonical_payload
    def missing(sources):
        for row in sources['decision_evidence']:
            if row['kind']!='scan_start':continue
            f=row['payload']['financial_context']
            stage=next((s for s in sources['decision_evidence'] if s['run_id']==row['run_id'] and s['kind']=='entry_stage_inputs'),None)
            if stage is None:continue
            stage['payload'].update(stage='reservation_inventory',slot_scope=f['runtime_slot_scope'],slot_inputs=copy.deepcopy(f['hrs_slot_inputs']),
                **{k:f[k] for k in ('hrs_margin_scope','account_available_balance','account_available_balance_source','account_status','account_cash_observed_at')})
            stage['payload_sha256']=canonical_payload(stage['payload'])[1]
    result=owned_fixture(tmp_path,source_transform=missing,export_result=True)
    assert result.get('decision')=='BLOCKED_DATA'
    assert 'hrs_account_pending_locked_source_missing' in result['gaps']


def test_bootstrap_uses_original_cash_and_inventory_completion_clocks(tmp_path):
    from datetime import timedelta
    from .test_hrs_wiring import AT
    from strategies.hrs.decision_capture import canonical_payload
    def clocks(sources):
        row=next(r for r in sources['decision_evidence'] if r['run_id']=='bootstrap' and r['kind']=='scan_start')
        f=row['payload']['financial_context']
        f['account_cash_observed_at']=(AT+timedelta(seconds=1)).isoformat()
        f['hrs_slot_inputs']['observed_at']=(AT+timedelta(seconds=2)).isoformat()
        f['available_at']=row['available_at']=(AT+timedelta(seconds=3)).isoformat()
        f['equity_gate']['checked_at']=(AT+timedelta(seconds=3)).isoformat()
        row['payload_sha256']=canonical_payload(row['payload'])[1]
    portal,adapter=owned_fixture(tmp_path,source_transform=clocks)
    assert run_replay(portal,adapter,broker()).final_position is not None


def reseal_dataset(portal,name,mutate):
    """Change derived facts while retaining every original evidence source."""
    import json
    from ai_tuner.replay.data_portal import DataPortal,compute_manifest_envelope_sha256,compute_manifest_sha256,sha256_file
    path=portal.snapshot_dir/name
    value=([json.loads(line) for line in path.read_text().splitlines()] if name.endswith('.jsonl') else json.loads(path.read_text()))
    mutate(value)
    path.write_text(''.join(json.dumps(row)+'\n' for row in value) if name.endswith('.jsonl') else json.dumps(value))
    manifest_path=portal.snapshot_dir/'manifest.json';manifest=json.loads(manifest_path.read_text())
    manifest['files'][name]['sha256']=sha256_file(str(path));manifest['sha256']=compute_manifest_sha256(manifest['files'])
    manifest['envelope_sha256']=compute_manifest_envelope_sha256(manifest);manifest_path.write_text(json.dumps(manifest))
    return DataPortal(portal.snapshot_dir,'hrs',portal.as_of,portal.window_start)


@pytest.mark.parametrize('target,field',[('bootstrap','allocation'),('bootstrap','account_equity'),('bootstrap','entry_policy'),('risk','allocation'),('risk','entry_policy'),('risk','account_equity_excluding_hrs'),('risk','account_equity_component_source')])
def test_resealed_owned_quota_inputs_remain_bound_to_original_financial_sources(tmp_path,target,field):
    from ai_tuner.replay.models import DataBlocked
    portal,adapter=floor_fixture(tmp_path,margin_scope=HRS_OWNED,allocation='26.18',ceiling='1',foreign_margin='64.86')
    assert run_replay(portal,adapter,broker()).final_position is None
    def mutate(value):
        objects=[value] if target=='bootstrap' else [row['risk'] for row in value]
        for obj in objects:
            if field=='entry_policy':obj[field]['entry_ceiling']='26.18'
            elif field=='account_equity_component_source':obj[field]['model']='fabricated'
            else:obj[field]='999999'
    changed=reseal_dataset(portal,'hrs_replay_state.json' if target=='bootstrap' else 'hrs_decision_inputs.jsonl',mutate)
    with pytest.raises(DataBlocked,match='margin_raw_source'):
        run_replay(changed,type(adapter)(adapter._state_loader),broker())


@pytest.mark.parametrize('identity_change',['keep','drop_bootstrap','wrong_bootstrap','drop_runtime','wrong_runtime'])
def test_derived_legacy_downgrade_cannot_disable_original_owned_source_verification(tmp_path,identity_change):
    from ai_tuner.replay.models import DataBlocked
    portal,adapter=floor_fixture(tmp_path,margin_scope=HRS_OWNED,allocation='100',ceiling='1',cash='1',foreign_margin='64.86')
    assert run_replay(portal,adapter,broker()).final_position is None
    def bootstrap(value):
        value['entry_policy']['reason']='legacy';value['entry_policy']['entry_ceiling']='100'
        value.pop('runtime_slot_scope',None)
        if identity_change=='drop_bootstrap':value.pop('bootstrap_source_evidence_id',None)
        elif identity_change=='wrong_bootstrap':value['bootstrap_source_evidence_id']='not-original-opening'
    portal=reseal_dataset(portal,'hrs_replay_state.json',bootstrap)
    def risk(rows):
        for row in rows:
            row['risk']['entry_policy']['reason']='legacy';row['risk']['entry_policy']['entry_ceiling']='100'
            observed=row['risk']['account_occupancy']['observed_at']
            row['risk']['account_occupancy']=dict(version='hrs_account_occupancy_v1',observed_at=observed,other_positions=[])
            if identity_change=='drop_runtime':row.pop('evidence_ids',None)
            elif identity_change=='wrong_runtime':row['evidence_ids']=['not-original-analysis']
    portal=reseal_dataset(portal,'hrs_decision_inputs.jsonl',risk)
    with pytest.raises(DataBlocked,match='margin_raw_source'):
        run_replay(portal,type(adapter)(adapter._state_loader),broker())


@pytest.mark.parametrize('identity_change',['drop','wrong'])
def test_runtime_original_scope_is_required_before_legacy_occupancy_branch(tmp_path,identity_change):
    from datetime import timedelta
    from .test_hrs_wiring import AT
    from ai_tuner.replay.models import DataBlocked
    portal,_=owned_fixture(tmp_path)
    def risk(rows):
        for row in rows:
            row['risk']['entry_policy']['reason']='legacy'
            observed=row['risk']['account_occupancy']['observed_at']
            row['risk']['account_occupancy']=dict(version='hrs_account_occupancy_v1',observed_at=observed,other_positions=[])
            if identity_change=='drop':row.pop('evidence_ids',None)
            else:row['evidence_ids']=['not-original-analysis']
    portal=reseal_dataset(portal,'hrs_decision_inputs.jsonl',risk)
    with pytest.raises(DataBlocked,match='margin_raw_source'):
        portal.hrs_inputs('A',AT+timedelta(hours=1),decision_at=AT+timedelta(hours=1))


def test_formal_original_evidence_cannot_be_removed_to_gain_legacy_fallback(tmp_path):
    import json
    from ai_tuner.replay.data_portal import DataPortal,compute_manifest_envelope_sha256,compute_manifest_sha256
    from ai_tuner.replay.models import DataBlocked
    portal,adapter=owned_fixture(tmp_path)
    def bootstrap(value):
        value['entry_policy']['reason']='legacy';value.pop('runtime_slot_scope',None)
        value.pop('bootstrap_source_evidence_id',None)
    portal=reseal_dataset(portal,'hrs_replay_state.json',bootstrap)
    path=portal.snapshot_dir/'manifest.json';manifest=json.loads(path.read_text())
    manifest['files'].pop('raw_hrs_evidence.json')
    manifest['sha256']=compute_manifest_sha256(manifest['files']);manifest['envelope_sha256']=compute_manifest_envelope_sha256(manifest)
    path.write_text(json.dumps(manifest));portal=DataPortal(portal.snapshot_dir,'hrs',portal.as_of,portal.window_start)
    with pytest.raises(DataBlocked,match='original_evidence_missing'):
        run_replay(portal,type(adapter)(adapter._state_loader),broker())


@pytest.mark.parametrize('target,change',[('bootstrap','drop'),('bootstrap','other_scan'),('risk','drop'),('risk','other_analysis')])
def test_original_scope_resolution_does_not_trust_derived_source_identity(tmp_path,target,change):
    import json
    from datetime import timedelta
    from .test_hrs_wiring import AT
    from ai_tuner.replay.models import DataBlocked
    portal,adapter=owned_fixture(tmp_path)
    records=json.loads((portal.snapshot_dir/'raw_hrs_evidence.json').read_text())['decision_evidence']
    other=next(row['evidence_id'] for row in records if row['run_id']=='cycle-2' and row['kind']==('scan_start' if target=='bootstrap' else 'analysis'))
    def mutate(value):
        if target=='bootstrap':
            if change=='drop':value.pop('bootstrap_source_evidence_id')
            else:value['bootstrap_source_evidence_id']=other
        else:
            for row in value:
                if change=='drop':row.pop('evidence_ids')
                else:row['evidence_ids']=[other]
    portal=reseal_dataset(portal,'hrs_replay_state.json' if target=='bootstrap' else 'hrs_decision_inputs.jsonl',mutate)
    with pytest.raises(DataBlocked,match='margin_raw_source'):
        if target=='bootstrap':run_replay(portal,adapter,broker())
        else:portal.hrs_inputs('A',AT+timedelta(hours=1),decision_at=AT+timedelta(hours=1))


@pytest.mark.parametrize('field',['risk','account_occupancy'])
def test_original_contract_malformed_derived_risk_is_data_blocked(tmp_path,field):
    from datetime import timedelta
    from .test_hrs_wiring import AT
    from ai_tuner.replay.models import DataBlocked
    portal,_=owned_fixture(tmp_path)
    def corrupt(rows):
        for row in rows:
            if field=='risk':row['risk']=None
            else:row['risk']['account_occupancy']=None
    portal=reseal_dataset(portal,'hrs_decision_inputs.jsonl',corrupt)
    with pytest.raises(DataBlocked):portal.hrs_inputs('A',AT+timedelta(hours=1),decision_at=AT+timedelta(hours=1))
