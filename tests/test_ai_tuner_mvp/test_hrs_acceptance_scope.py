"""The allocation preview has its own window and cannot stand for HRS acceptance."""
import json
import pytest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from ai_tuner.ops import hrs_acceptance as inspector


def blocked():
    return dict(account_id='default', status='BLOCKED_DATA', mode='shadow_only',
                allocation=None, start_ms=1790784000000, end_ms=1793462400000,
                as_of='2026-11-19T10:05:00+08:00',
                reasons=['hrs:capital_unverified', 'hrs:activity_unverified',
                         'income_report_unverified'])


def test_conclusion_names_the_full_account_allocation_scope():
    text = inspector.report_conclusion(0, 0, None, 'BLOCKED_DATA')
    assert '全账户自动分配预览受阻' in text
    assert '影子核验仍受阻' not in text


def test_unchanged_shadow_still_exposes_window_and_exact_pending_evidence(monkeypatch, tmp_path, capsys):
    shadow = blocked()
    state = tmp_path/'state.json'
    state.write_text(json.dumps(dict(shadow_hash=inspector.digest({k:v for k,v in shadow.items() if k!='as_of'}))))
    monkeypatch.setattr(inspector,'read_journal',lambda since,*args:([],[],[],[],True))
    monkeypatch.setattr(inspector,'read_shadow',lambda:shadow)
    result=dict(issues=[],samples=[],positions=[],reservations=0,policies=['hrs r8 reduced'],
                monthly=None,allocation_hash='same')
    monkeypatch.setattr(inspector.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps(result)))
    monkeypatch.setattr(inspector,'send_report',Mock())
    inspector.run_daily(SimpleNamespace(state=state,dry_run=True,send=False,webhook=tmp_path/'unused'))
    text=capsys.readouterr().out
    assert '2026-10-01' in text and '2026-11-01' in text
    for reason in shadow['reasons']:
        assert reason in text
    assert 'BLOCKED_DATA' in text
    assert 'HRS 收益验收' in text
    inspector.send_report.assert_not_called()


def test_shadow_reader_preserves_activation_blocks_and_scope(monkeypatch):
    shadow=dict(blocked(),activation_blocks=['two_holdout_months_unverified'])
    monkeypatch.setattr(inspector.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=json.dumps(shadow)))
    assert inspector.read_shadow()==shadow


def test_malformed_shadow_window_is_reported_as_unknown_not_qualified():
    value=blocked();value['start_ms']=True;value['reasons']=['custom_missing_proof']
    text='\n'.join(inspector.allocation_shadow_lines(value))
    assert '窗口未核实' in text
    assert 'custom_missing_proof' in text and 'BLOCKED_DATA' in text


def test_unknown_status_and_empty_reasons_do_not_turn_into_a_preview():
    text='\n'.join(inspector.allocation_shadow_lines({'status':'unexpected','reasons':None}))
    assert '状态待确认' in text
    assert '分配预览通过' not in text


def test_every_reason_and_activation_block_remains_visible():
    shadow=blocked()
    shadow['reasons']=[f'evidence_gap_{i}' for i in range(20)]
    shadow['activation_blocks']=[f'activation_gap_{i}' for i in range(10)]
    lines=inspector.allocation_shadow_lines(shadow)
    text='\n'.join(lines)
    for reason in shadow['reasons']+shadow['activation_blocks']:
        assert reason in text


def hrs_report(now):
    return dict(report_version='hrs-continuous-acceptance-v1',account_id='default',
                generated_at=now.isoformat(),database_snapshot_coherent=True,
                performance_accepted=False,status='PENDING',
                technical=dict(status='NOT_READY',required_complete_hours=24,
                               consecutive_complete_hours=0,coverage_threshold=.95),
                financial=dict(status='PENDING',required_lifecycles=5,
                               observed_closed_lifecycles=0,engineering_status='PENDING'))


def test_hrs_evidence_has_independent_technical_financial_and_performance_states():
    now=datetime(2026,11,20,1,40,tzinfo=timezone.utc)
    text='\n'.join(inspector.hrs_evidence_lines(hrs_report(now),now))
    assert '连续完整小时 0/24' in text
    assert '自然闭合仓位 0/5' in text
    assert '收益验收未通过' in text
    assert '20' in text and '两个月' in text


def test_hrs_engineering_observation_is_never_a_profitability_approval():
    now=datetime(2026,11,20,1,40,tzinfo=timezone.utc);r=hrs_report(now)
    r['status']='OBSERVATION_READY';r['technical'].update(status='PASS',consecutive_complete_hours=24)
    r['financial'].update(observed_closed_lifecycles=5,engineering_status='READY')
    text='\n'.join(inspector.hrs_evidence_lines(r,now))
    assert '24/24' in text and '5/5' in text
    assert '收益验收未通过' in text


def test_missing_stale_or_incoherent_hrs_report_is_not_a_zero_trade_proof():
    now=datetime(2026,11,20,1,40,tzinfo=timezone.utc)
    rows=[None,{},dict(hrs_report(now),generated_at='2026-11-19T00:00:00+00:00'),
          dict(hrs_report(now),database_snapshot_coherent=False),
          dict(hrs_report(now),account_id='other'),
          dict(hrs_report(now),generated_at='2026-11-21T00:00:00+00:00')]
    for value in rows:
        text='\n'.join(inspector.hrs_evidence_lines(value,now))
        assert '证据待核实' in text
        assert '自然闭合仓位 0/5' not in text


def test_daily_report_shows_hrs_evidence_independently_of_preview(monkeypatch,tmp_path,capsys):
    now=datetime.now(timezone.utc)
    monkeypatch.setattr(inspector,'read_journal',lambda since,*args:([],[],[],[],True))
    monkeypatch.setattr(inspector,'read_shadow',blocked)
    monkeypatch.setattr(inspector,'read_hrs_evidence',lambda:hrs_report(now))
    result=dict(issues=[],samples=[],positions=[],reservations=0,policies=[],monthly=None,allocation_hash='same')
    monkeypatch.setattr(inspector.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps(result)))
    inspector.run_daily(SimpleNamespace(state=tmp_path/'state',dry_run=True,send=False,webhook=tmp_path/'unused'))
    text=capsys.readouterr().out
    assert '全账户自动分配预览：BLOCKED_DATA' in text
    assert 'HRS 独立运行验收' in text and '收益验收未通过' in text


def test_report_freshness_matches_existing_fifteen_minute_reporter():
    from datetime import timedelta
    now=datetime(2026,11,20,1,40,tzinfo=timezone.utc)
    r=hrs_report(now-timedelta(seconds=1000))
    assert '自然闭合仓位 0/5' in '\n'.join(inspector.hrs_evidence_lines(r,now))


def test_report_updated_during_slow_probe_uses_read_time(monkeypatch,tmp_path,capsys):
    from datetime import timedelta
    start=datetime(2026,11,20,1,40,tzinfo=timezone.utc)
    times=iter([start,start+timedelta(minutes=2)])
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):
            return next(times).astimezone(tz)
    monkeypatch.setattr(inspector,'datetime',Clock)
    monkeypatch.setattr(inspector,'read_journal',lambda since,*args:([],[],[],[],True))
    monkeypatch.setattr(inspector,'read_shadow',blocked)
    monkeypatch.setattr(inspector,'read_hrs_evidence',lambda:hrs_report(start+timedelta(minutes=1)))
    result=dict(issues=[],samples=[],positions=[],reservations=0,policies=[],monthly=None,allocation_hash='same')
    monkeypatch.setattr(inspector.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps(result)))
    inspector.run_daily(SimpleNamespace(state=tmp_path/'state',dry_run=True,send=False,webhook=tmp_path/'unused'))
    assert '自然闭合仓位 0/5' in capsys.readouterr().out


def gate_evidence(now,**changes):
    from datetime import timedelta
    value=dict(account_id='default',strategy_id='hrs',ready=True,allowed=True,policy_revision=8,active_revision=8,
               as_of=(now-timedelta(seconds=5)).isoformat(),checked_at=now.isoformat(),
               valid_until=(now+timedelta(seconds=100)).isoformat(),reasons=[])
    return dict(value,**changes)


def test_real_equity_denial_is_visible_independently_of_allocation_preview():
    now=datetime(2026,11,20,9,tzinfo=timezone.utc)
    gate=gate_evidence(now,ready=False,allowed=False,reasons=['daily_history_incomplete','rolling_30_day_history_incomplete'])
    assert inspector.entry_gate_status(gate,now)=='PAUSED'
    text='\n'.join(inspector.hrs_entry_gate_lines(gate,now))
    assert '暂停新增风险' in text
    assert all(reason in text for reason in gate['reasons'])
    headline=inspector.report_conclusion(0,0,None,'BLOCKED_DATA','PAUSED')
    assert 'HRS 权益门禁暂停新增风险' in headline and '门禁与风控核验通过' not in headline


def test_fresh_matching_gate_reports_its_actual_authorization():
    now=datetime(2026,11,20,9,tzinfo=timezone.utc)
    assert inspector.entry_gate_status(gate_evidence(now),now)=='ALLOWED'
    assert '允许新增风险' in '\n'.join(inspector.hrs_entry_gate_lines(gate_evidence(now),now))


@pytest.mark.parametrize('damage',[dict(account_id='foreign'),dict(strategy_id='new_coin'),dict(active_revision=9),
    dict(valid_until='2026-11-19T00:00:00+00:00'),dict(ready=False,allowed=True),dict(allowed='true')])
def test_wrong_stale_or_incoherent_gate_cannot_be_reported_as_allowed(damage):
    now=datetime(2026,11,20,9,tzinfo=timezone.utc)
    gate=gate_evidence(now,**damage)
    assert inspector.entry_gate_status(gate,now)=='UNVERIFIED'
    assert '允许新增风险' not in '\n'.join(inspector.hrs_entry_gate_lines(gate,now))


def test_missing_gate_is_explicitly_unverified():
    now=datetime(2026,11,20,9,tzinfo=timezone.utc)
    assert inspector.entry_gate_status(None,now)=='UNVERIFIED'
    assert '门禁证据未核实' in '\n'.join(inspector.hrs_entry_gate_lines(None,now))
