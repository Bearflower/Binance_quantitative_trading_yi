"""AI Tuner evidence collection and shadow review; never activates allocations."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from ai_tuner.allocation.activity_evidence import (
    collect_entry_activity, collect_flat_anchors, read_activity_evidence,
)
from ai_tuner.monitor.trial_risk_evidence import account_initial_margin
from ai_tuner.allocation.capital_evidence import read_capital_evidence
from ai_tuner.allocation.shadow_allocation import preview_monthly_allocation
from ai_tuner.monitor.account_risk import collect_account_risk
from ai_tuner.monitor.reduced_trial import BEIJING, NotificationState
from shared.accounting.pm_equity_bridge import observe_pm_equity
from shared.accounting.published_report import read_published_report


def atomic_report(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('unsafe evidence report path')
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(value, handle, sort_keys=True, ensure_ascii=False, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class EvidenceJobs:
    def __init__(self, db, pm, wallet, messenger, config, state_dir: Path):
        self.db, self.pm, self.wallet, self.messenger = db, pm, wallet, messenger
        self.config, self.state_dir = config, Path(state_dir)
        self.budget = dict(config['account_risk']['budget'])
        if self.budget.get('account_id') != 'default':
            raise ValueError('reviewed budget covers only default PM')

    def latest_risk(self):
        risk = {}
        try:
            risk = json.loads((self.state_dir / 'account-risk-latest.json').read_text())
            if (not isinstance(risk, dict) or risk.get('account_id') != self.budget['account_id']
                    or risk.get('budget_id') != self.budget['budget_id']):
                raise ValueError('risk identity mismatch')
            age = int(time.time()*1000) - risk['as_of_ms']
            if age < 0 or age > self.budget.get('max_snapshot_age_ms', 120000):
                raise ValueError('risk snapshot stale')
            return risk
        except (OSError, ValueError, KeyError, TypeError):
            latched = (isinstance(risk, dict) and risk.get('account_id') == self.budget['account_id']
                       and risk.get('budget_id') == self.budget['budget_id']
                       and (risk.get('known_breach') or risk.get('status') == 'SAFETY_REVIEW'))
            return {'status': 'SAFETY_REVIEW' if latched else 'BLOCKED_DATA',
                    'known_breach': risk.get('known_breach') if latched else None, 'account_id': self.budget['account_id'],
                    'budget_id': self.budget['budget_id'], 'limit_ratio': '0.05',
                    'max_drawdown_ratio': None, 'current_drawdown_ratio': None,
                    'reasons': ['account_risk_latest_unverified']}

    async def sample_risk(self):
        pin_path = self.state_dir / 'account-risk-baseline-pin.json'
        budget = dict(self.budget)
        try:
            pin = json.loads(pin_path.read_text())
        except FileNotFoundError:
            pin = None
        if pin is not None:
            if (not isinstance(pin, dict) or pin.get('account_id') != budget['account_id']
                    or pin.get('account_identity') != budget['account_identity']):
                raise ValueError('persisted risk baseline account mismatch')
            budget['expected_baseline_time_ms'] = pin['exchange_time_ms']
        result = await collect_account_risk(self.db, self.pm, self.wallet, budget)
        if pin is None and result.get('baseline'):
            self.state_dir.mkdir(parents=True, exist_ok=True)
            # Create once; daily jobs, failures and budget renewals never replace it.
            with pin_path.open('x', encoding='utf-8') as handle:
                json.dump(result['baseline'], handle, sort_keys=True, default=str)
                handle.flush()
                os.fsync(handle.fileno())
        # Immutable detailed facts live in PostgreSQL; this pointer is a report cache.
        summary = {key: value for key, value in result.items() if key != 'evidence'}
        summary['exchange_account_initial_margin_usd'] = account_initial_margin(
            result.get('evidence'), result.get('as_of_ms'))
        atomic_report(self.state_dir / 'account-risk-latest.json', summary)
        signature = json.dumps([summary['status'], sorted({str(r).split(':')[0]
                                 for r in summary.get('reasons', [])})], sort_keys=True)
        state = NotificationState(self.state_dir / 'account-risk-notified.json')
        if summary['status'] != 'VERIFIED' and state.changed(signature):
            delivered = await self.messenger.send_alert(
                '【AI Tuner 主账户5%回撤预算】',
                f"范围: default PM；网格另评\n结论: {summary['status']}\n"
                f"最大采样回撤: {summary.get('max_drawdown_ratio')}；预算: 0.05\n"
                f"证据问题: {', '.join(summary.get('reasons', []))}\n"
                '只读考核；自动分配继续冻结，需复审处理。', level='error')
            if delivered:
                state.mark_delivered(signature, datetime.now(BEIJING))
        elif summary['status'] == 'VERIFIED' and state.changed(signature):
            state.mark_delivered(signature, datetime.now(BEIJING))
        return summary

    async def sample_equity_valuation(self):
        sampled_at = datetime.now(BEIJING)
        try:
            result = await asyncio.wait_for(observe_pm_equity(self.pm, self.wallet), timeout=5)
        except Exception as exc:
            result = {'status': 'BLOCKED_DATA',
                      'reasons': ['valuation_request_failed:' + type(exc).__name__]}
        sample = dict(result, method='pm-usdt-equity-bridge-v1', sampled_at=sampled_at.isoformat(),
                      account_scope=self.budget['account_id'],
                      account_identity=self.budget['account_identity'])
        body = json.dumps(sample, sort_keys=True).encode()
        directory = self.state_dir / 'pm-equity' / sampled_at.date().isoformat()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = directory / f'{sampled_at:%H%M%S%f}-{hashlib.sha256(body).hexdigest()[:16]}.json'
        # Readers see only complete records. A new observation never replaces a prior one.
        with tempfile.NamedTemporaryFile(dir=directory) as temporary:
            temporary.write(body)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.link(temporary.name, target)
        return sample

    async def sample_activity(self):
        result = await collect_entry_activity(self.db, self.pm, self.budget['account_id'],
                                              max_requests=20)
        try:
            result['flat_anchors_observed'] = await collect_flat_anchors(
                self.db, self.pm, self.budget['account_id'])
        except Exception as exc:
            result['flat_anchors_observed'] = None
            result['flat_anchor_error'] = type(exc).__name__
        atomic_report(self.state_dir / 'entry-activity-latest.json', result)
        return result

    async def run_allocation_shadow(self, *, now=None):
        now = now or datetime.now(BEIJING)
        local = now.astimezone(BEIJING)
        right = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        left = (right - timedelta(days=1)).replace(day=1)
        start, end = int(left.timestamp()*1000), int(right.timestamp()*1000)
        account = self.budget['account_id']
        cfg = self.config['capital_allocation']
        strategies = tuple(cfg['participating_strategies'])
        risk = self.latest_risk()
        try:
            report = await read_published_report(self.db, account, start, end,
                                                 f'monthly:{start}', current=False, include_derived_attribution=True)
            capital = await read_capital_evidence(self.db, account, start, end, strategies)
            activity = await read_activity_evidence(self.db, account, start, end, strategies)
            # Actual signed PM economic equity, never the stale configured 1000 USDT.
            pool = risk.get('current_equity_usd')
            result = preview_monthly_allocation(
                report=report, capital=capital, activity=activity, account_id=account,
                start_ms=start, end_ms=end, strategy_ids=strategies,
                total_capital=pool, reserve_ratio=cfg['reserve_ratio'],
                rank_ratios=cfg['rank_ratios'], effective_month=right.strftime('%Y-%m'))
        except Exception as exc:
            result = {'status': 'BLOCKED_DATA', 'allocation': None, 'mode': 'shadow_only',
                      'reasons': ['allocation_evidence_unavailable:' + type(exc).__name__]}
        result.update(account_id=account, start_ms=start, end_ms=end, risk=risk,
                      as_of=now.isoformat(), live_activation_eligible=False,
                      activation_blocks=['operator_shadow_approval_required',
                                         'two_holdout_months_unverified',
                                         'profitability_thresholds_unfrozen'])
        if risk['status'] != 'VERIFIED':
            result['activation_blocks'].append('account_risk_unverified_or_budget_breached')
        atomic_report(self.state_dir / 'allocation-shadow-latest.json', result)
        digest = hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()
        atomic_report(self.state_dir / 'reports' / (digest + '.json'), result)
        state = NotificationState(self.state_dir / 'allocation-shadow-notified.json')
        signature = json.dumps([result['status'], result.get('report_id'),
                                result.get('reasons'), result['activation_blocks']], sort_keys=True)
        if state.changed(signature):
            delivered = await self.messenger.send_alert(
                '【AI Tuner 自动分配影子核验】',
                f"范围: default PM；窗口: {left.isoformat()} 至 {right.isoformat()}\n"
                f"核验: {result['status']}；主账户风险: {risk['status']}\n"
                f"数据问题: {', '.join(result.get('reasons', [])) or '无'}\n"
                f"恢复前待办: {', '.join(result['activation_blocks'])}\n"
                '仅生成影子结果，实盘分配保持冻结。', level='warning')
            if delivered:
                state.mark_delivered(signature, now)
        return result


def register_evidence_jobs(scheduler, jobs):
    for name, callback in (('account_risk', jobs.sample_risk),
                           ('entry_activity', jobs.sample_activity)):
        scheduler.add_job(callback, 'interval', minutes=1, id=name, max_instances=1,
                          replace_existing=True, coalesce=True, misfire_grace_time=30)
    scheduler.add_job(jobs.sample_equity_valuation, 'interval', minutes=5,
                      id='pm_equity_valuation', max_instances=1, replace_existing=True,
                      coalesce=True, misfire_grace_time=60, next_run_time=datetime.now(BEIJING))
    scheduler.add_job(jobs.run_allocation_shadow, 'cron', hour=10, minute=5,
                      timezone='Asia/Shanghai', id='allocation_shadow', max_instances=1,
                      replace_existing=True, coalesce=True, misfire_grace_time=3600)
