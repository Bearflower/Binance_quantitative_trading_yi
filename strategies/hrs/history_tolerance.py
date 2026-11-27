"""HRS adapter for an approved finite historical gap, with original economics.

The shared policy loader owns approval/identity validation. This adapter verifies
its pinned HRS state and uses observed income identities without upgrading the
quality of a source report. New events cannot fall through the accepted prefix.
"""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from .equity_evidence import EvidenceBlocked, attributed_incomes, canonical, number
from .loss_gate import GateHistory, evaluate_loss_gate
from .loss_gate_repository import ledger_to_dict
from .risk_ledger import HistoricalIncomeAcceptance, IncomeEvent


def accepted_history(policy, at, *, current_complete_from):
    result = HistoricalIncomeAcceptance(policy.receipt_sha256, policy.accepted_from,
        min(policy.accepted_through, at), current_complete_from)
    result.validate(at)
    return result


def verify_resume_state(policy, stored):
    if stored is None or policy.payload.get('hrs_segment_id') != stored.ledger.segment_id:
        raise EvidenceBlocked('history_acceptance_segment_mismatch')
    if (policy.hrs_daily_limit != Decimal('.03') or policy.hrs_rolling_limit != Decimal('.06')
            or not policy.covers('historical_nav_sampling', policy.accepted_from, policy.accepted_through)):
        raise EvidenceBlocked('history_acceptance_hrs_contract_invalid')
    if stored.gate.history_acceptance_ref == policy.receipt_sha256:
        if stored.gate.history_acceptance_revision != policy.revision:
            raise EvidenceBlocked('history_acceptance_revision_mismatch')
        return
    payload = policy.payload
    gate_dict = stored.gate.to_dict()
    if gate_dict.get('history_acceptance_ref') is None and gate_dict.get('history_acceptance_revision') == 0:
        gate_dict.pop('history_acceptance_ref', None)
        gate_dict.pop('history_acceptance_revision', None)
    if (stored.version != payload.get('hrs_baseline_state_version')
            or canonical(ledger_to_dict(stored.ledger))[1] != payload.get('hrs_baseline_ledger_sha256')
            or payload.get('hrs_baseline_gate_sha256') not in
                {canonical(gate_dict)[1], canonical(stored.gate.to_dict())[1]}):
        raise EvidenceBlocked('history_acceptance_state_changed')


def observed_historical_incomes(report, account, segment, policy):
    """Read proved identities; accept only finite missing coverage, not conflicts."""
    start = datetime.fromtimestamp(report['start_ms']/1000, timezone.utc)
    end = datetime.fromtimestamp(report['end_ms']/1000, timezone.utc)
    if not policy.accepted_from <= start <= end <= policy.accepted_through:
        raise EvidenceBlocked('historical_income_window_not_accepted')
    if report.get('attribution_conflicts') or report.get('unattributed') or report.get('missing_income_fills'):
        raise EvidenceBlocked('historical_income_identity_unproven')
    for gap in report.get('coverage_gaps', ()):
        code = 'pm_native_fill_coverage' if str(gap).startswith('fills:') else 'pm_native_income_coverage'
        if not policy.covers(code, start, end):
            raise EvidenceBlocked('historical_income_gap_not_accepted')
    kinds = {'REALIZED_PNL':'realized_pnl', 'COMMISSION':'fee', 'FUNDING_FEE':'funding'}
    result = {}
    for row in report.get('events', ()):
        if (row.get('account_id') != account or row.get('asset') != 'USDT'
                or row.get('reason') or not row.get('strategy') or row.get('income_type') not in kinds
                or not row.get('tran_id') or not row.get('event_time_utc')):
            raise EvidenceBlocked('historical_income_identity_unproven')
        at = datetime.fromisoformat(row['event_time_utc'])
        if at.tzinfo is None or not start <= at < end:
            raise EvidenceBlocked('historical_income_time_invalid')
        if row['strategy'] != 'hrs':
            continue
        event = IncomeEvent(f"{row['income_type']}:{row['tran_id']}", kinds[row['income_type']],
            number(row['amount'], 'historical_income_amount'), at, segment, True)
        if result.setdefault(event.event_id,event) != event:
            raise EvidenceBlocked('historical_income_identity_conflict')
    return tuple(result.values())


def retained_income_events(state, historical, current, *, opening_credits):
    """Known old credits cannot disappear, change owner, or be silently rebooked."""
    events = {}
    for row in (*historical, *current):
        if events.setdefault(row.event_id, row) != row:
            raise EvidenceBlocked('accepted_history_known_income_changed')
    for old in state.incomes:
        if events.get(old.event_id) != old:
            raise EvidenceBlocked('accepted_history_known_income_changed')
    actual_credits = {row.event_id: row for row in events.values() if row.occurred_at <= state.inception}
    expected = {row['event_id']: row for row in opening_credits}
    if set(actual_credits) != set(expected):
        raise EvidenceBlocked('accepted_history_opening_credit_reconciliation_required')
    for key, event in actual_credits.items():
        prior = expected[key]
        if (event.kind != prior['kind'] or event.amount != Decimal(prior['amount'])
                or event.occurred_at != datetime.fromisoformat(prior['occurred_at'])):
            raise EvidenceBlocked('accepted_history_opening_credit_changed')
    # A late historical fact remains visible but must use the audited history
    # rebuild, not today's return. The gate denies while that is unresolved.
    known = {row.event_id for row in state.incomes}
    if any(state.inception < row.occurred_at <= state.as_of and row.event_id not in known for row in events.values()):
        raise EvidenceBlocked('late_income_requires_reconciliation')
    return tuple(row for row in events.values() if row.occurred_at > state.inception)


def observed_gate(previous, current, samples, *, policy, current_complete, daily_complete,
                  complete_from):
    # The factual flag stays false. Operational permission is a separate input.
    if not current_complete:
        raise EvidenceBlocked('current_nav_history_incomplete')
    proof = accepted_history(policy, current.as_of,
        current_complete_from=min(policy.accepted_through, current.as_of))
    history = GateHistory(tuple(samples), complete_from, current.as_of, False,
                          daily_complete, accepted_history=proof)
    gate = evaluate_loss_gate(previous, current, history)
    return replace(gate, state=replace(gate.state,
        history_acceptance_ref=policy.receipt_sha256, history_acceptance_revision=policy.revision))


class AcceptedAllocationSelector:
    """Economic continuity only; a carried row never authorizes a new month's entry.

    The caller must verify every selected post-acceptance row against the immutable
    approval chain before committing anything. Previously visible month rows may
    not disappear into the carry path, even when their final journal state is empty.
    """
    def __init__(self, policy, baseline, journal):
        self.policy, self.baseline, self.journal = policy, tuple(baseline), tuple(journal)
        self.selected_rows = {}
        self.carried_months = set()
        from .loss_gate import SHANGHAI
        from datetime import timedelta
        anchor_id = policy.payload.get('allocation_anchor_id')
        anchor_rows = [row for row in self.baseline if row.get('id') == anchor_id]
        for event in self.journal:
            anchor_rows.extend(row for row in (event.get('old_row'), event.get('new_row'))
                               if row and row.get('id') == anchor_id)
        months = {str(row.get('month', ''))[:7] for row in anchor_rows}
        if len(months) != 1:
            raise EvidenceBlocked('allocation_anchor_month_unproven')
        try:
            anchor_month = datetime.strptime(months.pop(), '%Y-%m').replace(tzinfo=SHANGHAI)
        except ValueError:
            raise EvidenceBlocked('allocation_anchor_month_unproven') from None
        self.carry_from = (anchor_month + timedelta(days=32)).replace(day=1)

    def __call__(self, rows, at):
        from .equity_evidence import _selected_allocation, allocation_evidence
        from .loss_gate import SHANGHAI
        rows = list(rows)
        month = at.astimezone(SHANGHAI).strftime('%Y-%m')
        current = [row for row in rows if str(row.get('month', ''))[:7] == month]
        if at < self.carry_from:
            proof = _selected_allocation(rows, at)
            if at >= self.policy.accepted_through:
                row = next(row for row in rows if row['id'] == proof.source['id'])
                self.selected_rows[canonical(row)[1]] = dict(row)
            return proof
        if current:
            # Never let a second, draft, malformed or retired row hide behind
            # LIMIT 1, including when the selected HRS amount happens to match.
            if len(current) != 1 or current[0].get('status') != 'active':
                raise EvidenceBlocked('allocation_current_identity_unproven')
            row = current[0]
            proof = _selected_allocation(current, at)
        else:
            known = list(self.baseline)
            for event in self.journal:
                if event['statement_at'] <= at:
                    known.extend(row for row in (event.get('old_row'), event.get('new_row')) if row)
            if any(str(row.get('month', ''))[:7] == month for row in known):
                raise EvidenceBlocked('allocation_published_month_missing')
            older = [row for row in rows if str(row.get('month', ''))[:7] < month]
            if not older:
                raise EvidenceBlocked('allocation_active_gap')
            previous_month = max(str(row.get('month', ''))[:7] for row in older)
            candidates = [row for row in older if str(row.get('month', ''))[:7] == previous_month]
            if len(candidates) != 1 or candidates[0].get('status') != 'active':
                raise EvidenceBlocked('allocation_previous_identity_unproven')
            row = candidates[0]
            # Validate using its actual month and actual timestamp, without
            # inventing either a new allocation row or a midnight cash flow.
            created = row.get('created_at')
            created = datetime.fromisoformat(created) if isinstance(created, str) else created
            if not isinstance(created, datetime):
                raise EvidenceBlocked('allocation_time_missing')
            created = created.replace(tzinfo=SHANGHAI) if created.tzinfo is None else created
            if created > at or created.astimezone(SHANGHAI).strftime('%Y-%m') != previous_month:
                raise EvidenceBlocked('allocation_previous_time_unproven')
            proof = allocation_evidence(dict(row, created_at=created), created)
            self.carried_months.add(month)
        self.selected_rows[canonical(row)[1]] = dict(row)
        return proof

    async def verify(self, connection, account):
        from shared.accounting.allocation_provenance import validate_allocation_chain
        import json
        for row in self.selected_rows.values():
            try:
                native = await connection.fetchrow('SELECT id,entries FROM public.capital_allocation WHERE id=$1', row['id'])
                if native is None:
                    raise ValueError('allocation_selected_row_missing')
                # Journal decoding uses Decimal; retain the native JSON spelling
                # for the approved digest rather than stringifying its numbers.
                entries = json.loads(native['entries'], parse_float=Decimal) if isinstance(native['entries'], str) else native['entries']
                expected = json.loads(row['entries'], parse_float=Decimal) if isinstance(row['entries'], str) else row['entries']
                if entries != expected:
                    raise ValueError('allocation_selected_row_changed')
                await validate_allocation_chain(connection, native, account=account, policy=self.policy)
            except (ValueError, TypeError, KeyError) as exc:
                raise EvidenceBlocked('allocation_approval_unproven:' + str(exc)) from None
