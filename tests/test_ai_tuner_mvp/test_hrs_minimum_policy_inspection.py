import json
import builtins
import runpy
import sys
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ai_tuner.ops import hrs_acceptance as inspector
from ai_tuner.ops.hrs_acceptance import POLICIES,evaluate_policies
from strategies.hrs.risk_budget import FLOOR
from strategies.hrs.margin_budget import HRS_OWNED

def rows():
    now=datetime(2026,11,19,tzinfo=timezone.utc)
    return now,[dict(strategy_id=s,revision=r,mode=m,freeze_allocation=True,allow_add=False,
        effective_at=now,expires_at=now+timedelta(days=20),audited=True,entry_ceiling='26.18',
        reason=json.dumps({'hrs_risk_budget_version':FLOOR,'hrs_margin_scope':HRS_OWNED}) if s=='hrs' else 'old approved reason')
        for s,r,m in [('hrs',8,'reduced'),('btc_eth',5,'capped'),('btc_eth_aggressive',5,'reduced'),('new_coin',5,'capped')]]

def test_daily_inspector_expected_only_hrs_budget_revision_changes():
    assert POLICIES=={'hrs':(8,'reduced'),'btc_eth':(5,'capped'),'btc_eth_aggressive':(5,'reduced'),'new_coin':(5,'capped')}
    now,data=rows();assert evaluate_policies(data,now)==[]

def test_revision_number_alone_does_not_prove_new_budget_mode():
    now,data=rows();data[0]['reason']='old percent reason'
    assert any('预算版本' in x for x in evaluate_policies(data,now))
    data[0]['reason']=json.dumps({'hrs_risk_budget_version':'unknown'})
    assert any('预算版本' in x for x in evaluate_policies(data,now))

def test_other_policy_and_margin_drift_still_alarm():
    now,data=rows();data[1]['revision']=6;data[0]['entry_ceiling']='27'
    issues=evaluate_policies(data,now)
    assert any('btc_eth r6' in x for x in issues) and any('hrs r8' in x for x in issues)


def test_owned_quota_policy_requires_explicit_verified_margin_contract():
    now,data=rows();data[0]["reason"]=json.dumps({"hrs_risk_budget_version":FLOOR})
    assert any("保证金口径" in x for x in evaluate_policies(data,now))
    data[0]["reason"]=json.dumps({"hrs_risk_budget_version":FLOOR,"hrs_margin_scope":"unknown"})
    assert any("保证金口径" in x for x in evaluate_policies(data,now))


@pytest.fixture
def daily_report(monkeypatch, tmp_path):
    data = dict(issues=[], samples=[], positions=[], reservations=0,
                policies=['hrs r8 reduced'], monthly=None, allocation_hash='allocation')
    monkeypatch.setattr(inspector, 'read_journal', lambda since, window: ([], [], [], [], True))
    monkeypatch.setattr(inspector, 'read_shadow', lambda: {'status': 'PREVIEW', 'allocation': {}})
    monkeypatch.setattr(inspector.subprocess, 'run',
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(data)))
    outbox = SimpleNamespace(publish=Mock())
    sender = Mock()
    monkeypatch.setattr(inspector, 'digest_outbox', outbox, raising=False)
    monkeypatch.setattr(inspector, 'send_report', sender)
    state = tmp_path / 'state.json'
    webhook = tmp_path / 'webhook'
    return data, outbox.publish, sender, state, webhook


@pytest.mark.parametrize('direct', [False, True])
@pytest.mark.parametrize('issues', [[], ['政策漂移']])
def test_daily_cli_defaults_to_outbox_and_send_explicitly_selects_direct_delivery(
        monkeypatch, daily_report, direct, issues):
    data, publish, sender, state, webhook = daily_report
    data['issues'] = issues
    monkeypatch.setattr(sys, 'argv', ['hrs_acceptance', '--state', str(state),
                                    '--webhook', str(webhook)] + (['--send'] if direct else []))
    inspector.main()
    assert state.exists()
    if direct:
        publish.assert_not_called()
        sender.assert_called_once()
        assert sender.call_args.args[0] == webhook
        report = sender.call_args.args[1]
    else:
        sender.assert_not_called()
        publish.assert_called_once()
        assert publish.call_args.args[0] == 'hrs'
        assert publish.call_args.kwargs == {
            'status': 'warn' if issues else 'ok',
            'summary': '1 项异常' if issues else '正常',
        }
        report = publish.call_args.args[1]
    assert '【HRS 生产只读巡检】' in report and 'hrs r8 reduced' in report
    assert 'HRS 主动对账' in report and '未记录主动对账' in report


@pytest.mark.parametrize('direct', [False, True])
def test_daily_dry_run_neither_publishes_nor_sends_or_advances_state(
        monkeypatch, daily_report, direct):
    _, publish, sender, state, webhook = daily_report
    monkeypatch.setattr(sys, 'argv', ['hrs_acceptance', '--dry-run', '--state', str(state),
                                    '--webhook', str(webhook)] + (['--send'] if direct else []))
    inspector.main()
    publish.assert_not_called()
    sender.assert_not_called()
    assert not state.exists()


@pytest.mark.parametrize('failure', [None, OSError('outbox unavailable'), ValueError('invalid outbox')])
def test_outbox_unavailable_falls_back_to_mocked_direct_delivery(monkeypatch, failure):
    publish, sender = Mock(side_effect=failure), Mock()
    monkeypatch.setattr(inspector, 'digest_outbox',
                        None if failure is None else SimpleNamespace(publish=publish), raising=False)
    monkeypatch.setattr(inspector, 'send_report', sender)
    inspector.publish_or_send('webhook', 'report', status='warn', summary='1 项异常')
    sender.assert_called_once_with('webhook', 'report')
    if failure is not None:
        publish.assert_called_once_with('hrs', 'report', status='warn', summary='1 项异常')


@pytest.mark.parametrize('available', [False, True])
def test_optional_outbox_import_supports_host_script_and_container_probe(monkeypatch, available):
    original_import = builtins.__import__
    outbox, attempted = SimpleNamespace(publish=Mock()), []
    def optional_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'ai_tuner.ops' and 'digest_outbox' in fromlist:
            attempted.append('package')
            raise ImportError('no package outbox')
        if name == 'digest_outbox':
            attempted.append('script')
            if available:
                return outbox
            raise ImportError('no script outbox')
        return original_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, '__import__', optional_import)
    namespace = runpy.run_path(inspector.__file__, run_name='hrs_acceptance_import_test')
    assert namespace['digest_outbox'] is (outbox if available else None)
    assert attempted == ['package', 'script']
    outbox.publish.assert_not_called()


@pytest.mark.parametrize('flags', [[], ['--send'], ['--dry-run'], ['--dry-run', '--send']])
def test_probe_read_failure_uses_selected_notification_route(monkeypatch, flags):
    publish, sender = Mock(), Mock()
    def fail(args):
        raise RuntimeError('read-only probe failed (exit=1)')
    monkeypatch.setattr(inspector, 'run_daily', fail)
    monkeypatch.setattr(inspector, 'digest_outbox', SimpleNamespace(publish=publish), raising=False)
    monkeypatch.setattr(inspector, 'send_report', sender)
    monkeypatch.setattr(sys, 'argv', ['hrs_acceptance', *flags])
    with pytest.raises(RuntimeError, match='read-only probe failed'):
        inspector.main()
    if '--dry-run' in flags:
        publish.assert_not_called()
        sender.assert_not_called()
    elif '--send' in flags:
        publish.assert_not_called()
        sender.assert_called_once()
        assert '本次证据读取失败' in sender.call_args.args[1]
    else:
        sender.assert_not_called()
        publish.assert_called_once()
        assert publish.call_args.args[0] == 'hrs'
        assert '本次证据读取失败' in publish.call_args.args[1]
        assert publish.call_args.kwargs == {'status': 'error', 'summary': '证据读取失败'}


def test_daily_reconciliation_uses_previous_beijing_day_and_latest_symbol_status():
    from datetime import datetime
    from ai_tuner.ops import hrs_acceptance as inspector

    now = datetime.fromisoformat("2026-11-20T07:31:00+08:00")
    journal = [
        ("2026-11-18T23:59:59+08:00", "OLD", "position_sync", "released"),
        ("2026-11-19T00:00:00+08:00", "WLDUSDT", "full_close", "algo_orders_open"),
        ("2026-11-19T04:00:00+08:00", "WLDUSDT", "position_sync", "released"),
        ("2026-11-19T05:00:00+08:00", "SOLUSDT", "full_close", "exchange_query_failed"),
        ("2026-11-20T00:00:00+08:00", "SOLUSDT", "position_sync", "released"),
    ]
    line, warning = inspector.summarize_hrs_reconciliation(now, journal)
    assert "2026-11-19" in line
    assert "3 次" in line and "释放 1" in line and "待重试 1" in line
    assert "SOLUSDT" in line and "币安查询失败" in line
    assert "WLDUSDT" not in line and "OLD" not in line
    assert warning


def test_journal_reads_previous_day_even_when_incremental_bookmark_is_later(monkeypatch):
    now = datetime(2026, 11, 20, 7, 31, tzinfo=inspector.BEIJING)
    start, end = inspector.previous_trading_day(now)
    event = json.dumps(dict(event="hrs_close_reconciliation", symbol="WLDUSDT",
                            trigger="full_close", outcome="released"))
    entries = [
        json.dumps(dict(__REALTIME_TIMESTAMP=str(int(when.timestamp() * 1_000_000)), MESSAGE=event))
        for when in (start - timedelta(seconds=1), start, end)
    ]

    def journal(command, **kwargs):
        if command[1] == "CONTAINER_NAME=trading_system-hrs":
            assert command[3] == f"@{int(start.timestamp())}"
            return SimpleNamespace(returncode=0, stdout="\n".join(entries))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(inspector.subprocess, "run", journal)
    _, _, _, evidence, available = inspector.read_journal(end + timedelta(hours=7), (start, end))
    assert available
    assert evidence == [(start.isoformat(), "WLDUSDT", "full_close", "released")]


def test_daily_reconciliation_warns_when_prior_day_journal_missing():
    line, warning = inspector.summarize_hrs_reconciliation(
        datetime(2026, 11, 20, 7, 31, tzinfo=inspector.BEIJING), [], journal_has_entries=False
    )
    assert warning and "日志未取到" in line


def test_pending_reconciliation_is_in_0735_outbox_and_raises_warning(monkeypatch, daily_report):
    _, publish, sender, state, webhook = daily_report

    def journal(since, window):
        start, _ = window
        event = (start.isoformat(), "WLDUSDT", "full_close", "algo_orders_open")
        return [], [], [], [event], True

    monkeypatch.setattr(inspector, "read_journal", journal)
    inspector.run_daily(SimpleNamespace(state=state, webhook=webhook, dry_run=False))
    sender.assert_not_called()
    section, report = publish.call_args.args
    assert section == "hrs"
    assert "HRS 主动对账" in report and "WLDUSDT 仍有条件挂单" in report
    assert publish.call_args.kwargs == {"status": "warn", "summary": "1 项异常"}
