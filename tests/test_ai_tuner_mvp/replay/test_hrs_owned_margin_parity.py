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


def test_independent_reservation_cash_overrides_scan_cash_and_raw_projection_is_verified(tmp_path):
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
            stage['payload']['account_available_balance']='1'
            stage['payload_sha256']=canonical_payload(stage['payload'])[1]
    portal,adapter=owned_fixture(tmp_path,source_transform=receipt)
    assert run_replay(portal,adapter,broker()).final_position is None
    inputs=[json.loads(line) for line in (portal.snapshot_dir/'hrs_decision_inputs.jsonl').read_text().splitlines()]
    for row in inputs:row['risk']['account_available_balance']='448.86'
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
