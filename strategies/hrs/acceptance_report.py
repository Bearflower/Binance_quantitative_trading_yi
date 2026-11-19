"""Read-only, prospective HRS acceptance evidence; never a performance approval.

Call ``await build_report(db)`` after a captured cycle, then ``write_report_atomic``.
The last 24 *closed* Shanghai hourly slots must each be complete; current-hour
observations cannot repair a missing slot. Cap coverage is the explicitly captured
requested scan batch, never an extrapolation to the full raw exchange universe.
Financial samples are the first five actual flat-to-flat fill lifecycles since a
certified opening inventory. A partial TP is a leg, not a second trade. Unknown
ownership, incomplete income collection, or absent samples remain pending/null.

Optional operational process: ``python -m strategies.hrs.acceptance_report`` once,
or ``--interval 300`` continuously, with DATABASE_URL and TRADE_ACCOUNT_ID set.
No schema installation, exchange calls, orders, or external messages occur here.
"""
from __future__ import annotations

import argparse
import hashlib
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

from shared.accounting.income_integrity import income_account_lock, require_income_integrity
from shared.accounting.report_reader import read_monthly_economic_report
from .decision_capture import canonical_payload
from .equity_evidence import (
    attribute_funding_from_inventory, canonical, owned_fill_positions, prove_positions,
)
from .funding_reference import COUNT_SQL,ROWS_SQL,validate_forecasts_ref,resolve_evidence_forecasts

UTC = timezone.utc
CST = timezone(timedelta(hours=8))
MODES = ('standard', 'emm', 'semi_emm', 'lv_rm')
REAL_CAP_SOURCES = {'coingecko', 'coinmarketcap', 'cache_hit', 'coingecko_cache'}
DEFAULT_OUTPUT = '/app/logs/hrs-acceptance.json'
# Large public API bodies are unnecessary here; retain every decision/failure kind.
EXCLUDED_KINDS = ['public_market_response', 'market_input']
# Independent equity refresh and the daily candidate timer share startup run
# context; their observations are inputs/heartbeats, not trading scan cycles.
BACKGROUND_KINDS = frozenset({'equity_gate', 'market_cap_batch',
    'public_market_response', 'market_input', 'public_market_failure', 'market_input_failure'})


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('timestamp_unproven')
    return value.astimezone(UTC)


def _audit_time(value):
    # MarketData records the metadata observation as time.time(); captures and
    # financial source timestamps use aware ISO datetimes.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC)
    return _time(value)


def _object(value):
    result = json.loads(value) if isinstance(value, str) else value
    if not isinstance(result, dict):
        raise ValueError('object_unproven')
    return result


def _number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('nonfinite_number')
    return result


def _mode(signal):
    value = signal.get('entry_mode')
    return value if value in MODES else 'unknown'


def _funnel():
    return dict(scanned_symbols=0, evaluated_directions=0, eligible=0,
        eligibility_unknown=0, shadow_evaluations=0, trend_denials=0,
        scoring_denials=0, data_failures=0, signals_reaching_execution=0,
        budget_denials=0, execution_denials=0, submitted_orders=0,
        exchange_fills=0, denial_reasons={})


def _batch(payload, at):
    reasons = []
    requested = payload.get('requested_symbols')
    quotes = payload.get('quotes')
    valid_request = (isinstance(requested, list) and bool(requested)
        and all(isinstance(s, str) and s for s in requested)
        and len(set(requested)) == len(requested)
        and payload.get('coverage_scope') == 'requested_scan_batch')
    inventory = valid_request and isinstance(quotes, dict) and set(quotes) == set(requested)
    if not inventory:
        reasons.append('quote_inventory_incomplete')
    audit = payload.get('universe_audit') or {}
    if not isinstance(audit, dict):
        audit = {}
    classifications = audit.get('rows')
    raw_proven = False
    try:
        captured = _time(payload.get('captured_at'))
        raw_proven = (isinstance(classifications, dict) and bool(classifications)
            and type(audit.get('raw_count')) is int and audit['raw_count'] == len(classifications)
            and dict(Counter(classifications.values())) == audit.get('counts')
            and audit.get('eligible_count') == sum(v == 'crypto' for v in classifications.values())
            and _audit_time(audit.get('observed_at')) <= captured <= at)
    except (ValueError, TypeError, KeyError, OverflowError):
        captured = at
    if not raw_proven:
        reasons.append('raw_universe_unproven')
    elif valid_request and any(classifications.get(s) != 'crypto' for s in requested):
        reasons.append('requested_crypto_eligibility_unproven')
    failures = Counter()
    fresh = 0
    for symbol in requested if valid_request else ():
        quote = quotes.get(symbol) if isinstance(quotes, dict) else None
        try:
            if not isinstance(quote, dict):
                raise ValueError('quote_missing')
            if quote.get('missing_reason'):
                raise ValueError(str(quote['missing_reason']))
            if not (quote.get('source') in REAL_CAP_SOURCES
                and quote.get('provider') in ('coingecko', 'coinmarketcap')
                and quote.get('provider_asset_id') and quote.get('is_proxy') is False
                and _number(quote.get('value')) > 0
                and 0 <= captured.timestamp()-float(_number(quote.get('as_of'))) <= 3600
                and float(_number(quote.get('as_of'))) <= float(_number(quote.get('available_at'))) <= captured.timestamp()):
                raise ValueError('not_fresh_real_provider_quote')
            fresh += 1
        except (ValueError, TypeError, ArithmeticError) as exc:
            failures[str(exc) if isinstance(exc, ValueError) else 'invalid_quote'] += 1
    coverage = fresh/len(requested) if inventory else None
    if coverage is not None and Decimal(fresh)/Decimal(len(requested)) < Decimal('.95'):
        reasons.append('fresh_coverage_below_95_percent')
    return dict(coverage_scope=payload.get('coverage_scope'),
        requested_count=len(requested) if isinstance(requested, list) else None,
        quote_inventory_complete=bool(inventory), fresh_count=fresh,
        fresh_coverage=coverage, failures=dict(failures),
        raw_universe_count=audit.get('raw_count') if type(audit.get('raw_count')) is int and audit['raw_count'] >= 0 else None,
        raw_universe_inventory_proven=bool(raw_proven),
        metadata_eligible_count=audit.get('eligible_count') if raw_proven else None,
        raw_classifications=audit.get('counts') if raw_proven else None,
        raw_universe_observed_at=_audit_time(audit['observed_at']).isoformat() if raw_proven else None,
        full_ticker_cap_coverage=None, reasons=reasons)


def summarize_evidence(rows, *, as_of, account_id, fills=(), orders=()):
    """Fail closed on an unexpected schema without breaking cycle maintenance."""
    try:
        return _summarize_evidence(rows, as_of=as_of, account_id=account_id, fills=fills, orders=orders)
    except Exception as exc:
        report = _summarize_evidence([], as_of=as_of, account_id=account_id)
        report['source_error_type'] = type(exc).__name__
        return report


def _summarize_evidence(rows, *, as_of, account_id, fills=(), orders=()):
    """Pure summary; only persisted actual events qualify a closed hourly slot."""
    now = _time(as_of)
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end-timedelta(hours=24)
    runs, seen, bad_hours = defaultdict(list), {}, defaultdict(set)
    for raw in rows:
        try:
            row = dict(raw)
            at, available = _time(row['event_time']), _time(row['available_at'])
            if available > now or at >= end or at < start:
                continue
            hour = at.replace(minute=0, second=0, microsecond=0)
            payload = _object(row['payload'])
            _, digest = canonical_payload(payload)
            if digest != row.get('payload_sha256'):
                bad_hours[hour].add('payload_hash_mismatch')
            identity = row['evidence_id']
            fingerprint = (row['run_id'], row['kind'], at, available, digest)
            if identity in seen:
                if seen[identity] != fingerprint:
                    bad_hours[hour].add('conflicting_evidence_identity')
                continue
            seen[identity] = fingerprint
            if available < at:
                bad_hours[hour].add('noncausal_evidence')
            row.update(payload=payload, event_time=at, available_at=available)
            runs[row['run_id']].append(row)
        except (KeyError, ValueError, TypeError):
            # An unplaceable record cannot improve any slot's evidence quality.
            for i in range(24):
                bad_hours[start+timedelta(hours=i)].add('invalid_evidence_envelope')
    slots, slot_runs = [], defaultdict(list)
    for run, records in runs.items():
        starts = [r for r in records if r['kind'] == 'scan_start']
        if not starts and all(r['kind'] in BACKGROUND_KINDS for r in records):
            continue
        if len(starts) == 1:
            source_account = starts[0]['payload'].get('financial_context', {}).get('entry_policy', {}).get('account_id')
            if source_account and source_account != account_id:
                continue
        anchor = min(r['event_time'] for r in records)
        hour = anchor.replace(minute=0, second=0, microsecond=0)
        if len(starts) != 1:
            bad_hours[hour].add('scan_start_unproven')
        else:
            hour = starts[0]['event_time'].replace(minute=0, second=0, microsecond=0)
        slot_runs[hour].append(records)
    order_modes = {}
    fill_seen = set()
    for index in range(24):
        hour = start+timedelta(hours=index)
        reasons = set(bad_hours[hour])
        grouped = slot_runs[hour]
        batches, counts = [], {m: _funnel() for m in (*MODES, 'unknown')}
        scanned = {m: set() for m in counts}
        submitted = {m: set() for m in counts}
        if not grouped:
            reasons.add('hour_has_no_complete_scan')
        for records in grouped:
            records.sort(key=lambda r: (r['event_time'], r['kind'] != 'signal_pre_execution'))
            starts = [r for r in records if r['kind'] == 'scan_start']
            ends = [r for r in records if r['kind'] == 'scan_end']
            if len(starts) != 1 or len(ends) != 1 or ends[0]['payload'].get('status') != 'complete':
                reasons.add('scan_incomplete')
            elif not (hour <= starts[0]['event_time'] <= ends[0]['event_time'] < hour+timedelta(hours=1)):
                reasons.add('scan_crosses_hour_or_noncausal')
            if len(starts) != 1 or starts[0]['payload'].get('financial_context', {}).get('entry_policy', {}).get('account_id') != account_id:
                reasons.add('scan_account_unproven')
            scopes, outcomes, analyzed, scored = (defaultdict(set) for _ in range(4))
            latest_signal = {}
            has_denial = any(r['kind'] == 'scan_denial' for r in records)
            for row in records:
                kind, p, symbol = row['kind'], row['payload'], row.get('symbol')
                if kind == 'market_cap_batch':
                    batch = _batch(p, row['event_time'])
                    batches.append(batch)
                    reasons.update(batch['reasons'])
                elif kind in ('standard_scan_scope', 'lv_rm_scan_scope'):
                    mode = 'lv_rm' if kind.startswith('lv_rm') else 'standard'
                    symbols = p.get('symbols')
                    if not isinstance(symbols, list) or len(set(symbols)) != len(symbols):
                        reasons.add('scan_scope_unproven')
                    else:
                        scopes[mode].update(symbols)
                        scanned[mode].update(symbols)
                elif kind.endswith('_candidate_outcome'):
                    mode = 'lv_rm' if kind.startswith('lv_rm') else 'standard'
                    outcomes[mode].add(symbol)
                    reason = p.get('reason')
                    if not reason:
                        reasons.add('candidate_terminal_reason_unproven')
                    if reason != 'scored':
                        bucket = counts[mode]
                        bucket['denial_reasons'][reason or 'unknown'] = bucket['denial_reasons'].get(reason or 'unknown', 0)+1
                        if any(s in str(reason) for s in ('insufficient', 'missing', 'error', 'unavailable', 'skip')):
                            bucket['data_failures'] += 1
                    else:
                        scored[mode].add(symbol)
                elif kind in ('analysis', 'lv_rm_analysis'):
                    analyzed['lv_rm' if kind == 'lv_rm_analysis' else 'standard'].add(symbol)
                    directions = ([(p.get('result') or {}).get(d) for d in ('short', 'long')]
                                  if kind == 'analysis' else
                                  [dict(score_result=p.get(d+'_score'), should_entry=p.get(d+'_should_entry')) for d in ('short', 'long')])
                    for data in directions:
                        if not isinstance(data, dict):
                            continue
                        score = data.get('score_result') or data.get('_scoring_result') or {}
                        mode = 'lv_rm' if kind == 'lv_rm_analysis' else _mode(score)
                        bucket = counts[mode]
                        scanned[mode].add(symbol)
                        bucket['evaluated_directions'] += 1
                        if type(data.get('should_entry')) is bool:
                            bucket['eligible'] += int(data['should_entry'])
                        else:
                            bucket['eligibility_unknown'] += 1
                        bucket['trend_denials'] += int(score.get('trend_filter_passed') is False)
                        bucket['scoring_denials'] += int(data.get('should_entry') is False and score.get('trend_filter_passed') is not False)
                        for shadow in score.get('shadows', []):
                            shadow_mode = _mode(shadow)
                            counts[shadow_mode]['shadow_evaluations'] += 1
                            counts[shadow_mode]['eligibility_unknown'] += 1
                            scanned[shadow_mode].add(symbol)
                elif kind == 'signal_pre_execution':
                    latest_signal[symbol] = _mode(p)
                    counts[_mode(p)]['signals_reaching_execution'] += 1
                elif kind == 'signal_execution_outcome' and p.get('result') is False:
                    signal = p.get('signal') or {}
                    bucket = counts[_mode(signal)]
                    reason = signal.get('_audit_denial') or signal.get('_audit_stage') or 'execution_denial_unclassified'
                    bucket['execution_denials'] += 1
                    bucket['budget_denials'] += int(signal.get('_audit_stage') == 'entry_risk_budget' and bool(signal.get('_audit_denial')))
                    bucket['denial_reasons'][reason] = bucket['denial_reasons'].get(reason, 0)+1
                elif kind == 'order_execution_result':
                    order = (p.get('outcome') or {}).get('order') or {}
                    if order.get('orderId') is not None:
                        mode = latest_signal.get(symbol, 'unknown')
                        key = (symbol, str(order['orderId']))
                        submitted[mode].add(key)
                        prior = order_modes.get(key)
                        order_modes[key] = mode if prior in (None, mode) else 'unknown'
                elif kind in ('market_input_failure', 'public_market_failure'):
                    counts['unknown']['data_failures'] += 1
                elif kind == 'scan_denial':
                    reason = p.get('reason') or 'unclassified_scan_denial'
                    counts['unknown']['denial_reasons'][reason] = counts['unknown']['denial_reasons'].get(reason, 0)+1
            if not scopes and not has_denial:
                reasons.add('scan_scope_missing')
            for mode, symbols in scopes.items():
                if symbols != outcomes[mode]:
                    reasons.add('candidate_terminal_inventory_incomplete')
                if not scored[mode] <= analyzed[mode]:
                    reasons.add('scored_analysis_missing')
        if not batches:
            reasons.add('market_cap_batch_missing')
        for mode in counts:
            counts[mode]['scanned_symbols'] = len(scanned[mode])
            counts[mode]['submitted_orders'] = len(submitted[mode])
        slots.append(dict(start_cst=hour.astimezone(CST).isoformat(),
            end_cst=(hour+timedelta(hours=1)).astimezone(CST).isoformat(),
            status='COMPLETE' if not reasons else 'INCOMPLETE', reasons=sorted(reasons),
            scan_count=len(grouped), market_cap_batches=batches, modes=counts))
    owner_map = _owners(orders, account_id)
    for raw in fills:
        fill = dict(raw)
        if fill.get('account_id') != account_id:
            continue
        at = _time(fill['trade_time_utc'])
        if not start <= at < end:
            continue
        key = (fill['symbol'], str(fill['order_id']))
        # Captured exchange order responses prove new-entry ownership; other
        # fills require the independent exact order owner (e.g. protection legs).
        if key not in order_modes and owner_map.get(key) != {'hrs'}:
            continue
        identity = (fill['symbol'], str(fill['trade_id']))
        if identity in fill_seen:
            continue
        fill_seen.add(identity)
        mode = order_modes.get(key, 'unknown')
        slots[int((at-start).total_seconds()//3600)]['modes'][mode]['exchange_fills'] += 1
    streak = 0
    for slot in reversed(slots):
        if slot['status'] != 'COMPLETE':
            break
        streak += 1
    return dict(status='PASS' if streak == 24 else 'NOT_READY',
        required_complete_hours=24, consecutive_complete_hours=streak,
        coverage_threshold=.95, cap_max_age_seconds=3600,
        coverage_caliber='captured_requested_scan_batch_only', hourly_slots=slots)


def _owners(orders, account):
    result = defaultdict(set)
    for row in orders:
        if row.get('account_id') == account and row.get('order_id') is not None and row.get('strategy'):
            result[(str(row['symbol']), str(row['order_id']))].add(row['strategy'])
    return result


def _actual_true(value):
    return value is True or value in ('true', 'TRUE')


def _apply_verified_funding(economic, archive, *, account_id, inception, through,
                            fills, orders, opening_inventory,funding_source_rows=None,funding_source_count=None):
    """Reuse the actual service's inventory proof, rechecking current source facts.

    An archive hash alone cannot bless changed/late facts. Its exact post-anchor
    fill inventory must still match, and both independent account inventories
    must reconcile against those fills before the shared event-time proof runs.
    """
    if not archive or not any(e.get('reason') == 'funding_owner_unproven' for e in economic.get('events', ())):
        return economic
    try:
        payload = _object(archive['payload'])
        if (canonical(payload)[1] != archive['evidence_sha256']
                or payload.get('account_id') != account_id
                or _time(payload['as_of']) != through
                or not payload.get('result', {}).get('ledger_version')
                or payload.get('source_collection', {}).get('gaps') != []):
            return economic
        if 'forecasts_ref' in payload.get('capture',{}).get('funding_publication',{}):
            resolve_evidence_forecasts(archive,funding_source_rows if funding_source_rows is not None else (),
                account_id=account_id,expected_count=funding_source_count,collect=False)
        fields = ('account_id','symbol','trade_id','order_id','side','qty','price',
                  'commission','commission_asset','realized_pnl')
        numeric = {'trade_id','order_id','qty','price','commission','realized_pnl'}
        def fingerprint(source):
            result = {}
            for row in source:
                at = _time(row['trade_time_utc'])
                if row['account_id'] != account_id or not inception < at <= through:
                    continue
                values = tuple(_number(row[k]) if k in numeric and row.get(k) is not None
                               else row.get(k) for k in fields)+(at,)
                key = (row['symbol'], str(row['trade_id']))
                if key in result and result[key] != values:
                    raise ValueError('conflicting_archive_fill')
                result[key] = values
            return result
        if fingerprint(payload['fills']) != fingerprint(fills):
            return economic
        capture = payload['capture']
        if _time(capture['captured_at']) != through:
            return economic
        prove_positions(account_id, payload['segment_id'], through, capture['inventory'],
            capture['independent'], capture['local'], fills, orders, capture['marks'],
            bootstrap=False, inception=inception, opening_inventory=opening_inventory)
        positions = owned_fill_positions(account_id, fills, orders, through=through,
                                          after=inception, opening_inventory=opening_inventory)
        current = [dict(strategy=owner, symbol=symbol, quantity=str(qty), entry_price=str(basis))
            for (owner, symbol), (qty, basis) in positions.items() if qty != 0]
        attributed = attribute_funding_from_inventory(economic, account_id, through,
            fills, orders, current, coverage_complete=economic.get('account_coverage_complete') is True
            and not economic.get('coverage_gaps'), available_at=through)
        if attributed is not economic:
            attributed['funding_capture_evidence_sha256'] = archive['evidence_sha256']
            attributed.pop('report_id', None)
            attributed['report_id'] = canonical(attributed)[1]
        return attributed
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return economic


def _floor_budget_provenance(lifecycle, payload, decision, records, budget_at, account_id, archives):
    """Reproduce the committed lock decision from immutable source receipts."""
    from shared.entry_risk_policy import (EntryDenied, _hrs_budget_json,
        _validate_hrs_floor_decision, _validate_hrs_floor_context,
        _bind_hrs_floor_local_to_parent, _bind_hrs_floor_pending_to_rows, _validate_hrs_floor_finance,
        _validate_hrs_financial_receipt)
    from .risk_budget import FLOOR, policy_budget_version, calculate_open_position_risk
    from .entry_occupancy import prove_hrs_slot_ownership, HrsOccupancyDenied
    try:
        client = decision['client_order_id']
        stages = [(p, at) for kind, p, at in records if kind == 'entry_stage_inputs'
            and p.get('stage') == 'reservation_success' and p.get('client_order_id') == client
            and p.get('_capture_run_id') == payload.get('_capture_run_id') and at <= budget_at]
        if len(stages) != 1:
            raise ValueError('committed reservation capture missing or ambiguous')
        stage, at = stages[0]
        context, slot, proof = (stage[k] for k in ('budget_context','slot_context','slot_occupancy_proof'))
        expected = _validate_hrs_floor_decision(decision, decision['budget_inputs'])
        captured = _hrs_budget_json(stage['budget_decision'])
        if any(captured[k] != decision[k] for k in captured if k != 'client_order_id'):
            raise ValueError('committed reservation differs from order budget')
        geometry = dict(hrs_risk_budget_version=FLOOR,hrs_budget_source='hrs_live_assessor_v1',
            hrs_budget_decision=captured,hrs_budget_inputs=decision['budget_inputs'],
            hrs_budget_context=context,slot_context=slot,slot_occupancy_proof=proof)
        _validate_hrs_floor_context(geometry,expected,dict(account_id=account_id,revision=decision['policy_revision']))
        policy = context['locked_policy']
        if (policy_budget_version(policy) != FLOOR or policy['account_id'] != account_id
                or policy['strategy_id'] != 'hrs' or policy['revision'] != decision['policy_revision']
                or policy['mode'] != 'reduced' or policy['allow_add'] is not False
                or not _time(policy['effective_at']) <= _time(context['checked_at']) < _time(policy['expires_at'])
                or not _time(context['checked_at']) <= at <= budget_at):
            raise ValueError('locked policy does not authorize the captured mode')
        inputs = expected.budget_inputs
        if (inputs['entry_price'] != _number(payload['entry_price'])
                or inputs['stop_price'] != _number(payload['stop_price'])
                or inputs['take_profit_prices'] != tuple(_number(p) for p in payload['take_profit_prices'])
                or inputs['direction'] != ('long' if lifecycle['fill_legs'][0]['side']=='BUY' else 'short')
                or inputs['allocation'] != _number(decision['risk_allocation'])
                or inputs['allocation'] != _number(context['full_allocation'])
                or inputs['fee_rate'] < Decimal('.0005') or inputs['pressure_slippage'] < Decimal('.02')
                or inputs['single_risk_ratio'] > Decimal('.01') or inputs['portfolio_risk_ratio'] > Decimal('.03')):
            raise ValueError('order geometry differs from assessor contract')
        parents = [a for a in archives if a.get('evidence_sha256') == context['evidence_sha256']]
        if len(parents) != 1:
            raise ValueError('original raw equity parent missing or ambiguous')
        parent = _bind_hrs_floor_local_to_parent(parents[0]['payload'],context['local_positions'],account_id,context['evidence_sha256'])
        receipt=context['financial_receipt']
        expected_client='risk_'+hashlib.sha256(f"{account_id}:hrs:{context['signal_id']}".encode()).hexdigest()[:30]
        if client!=expected_client or context['symbol']!=lifecycle['symbol']:
            raise ValueError('original financial receipt intent mismatch')
        financial_rows=[p for kind,p,_ in records if kind=='entry_stage_inputs'
            and p.get('stage')=='reservation_inventory'
            and p['_capture_envelope'].get('evidence_id')==receipt['evidence_id']]
        if len(financial_rows)!=1 or receipt['run_id']!=payload['_capture_run_id']:
            raise ValueError('original financial receipt missing or ambiguous')
        source=financial_rows[0]
        financial=_validate_hrs_financial_receipt(receipt,
            dict(source['_capture_envelope'],payload={k:v for k,v in source.items() if not k.startswith('_capture_')}),
            account=account_id,symbol=lifecycle['symbol'],signal_id=context['signal_id'],
            checked_at=_time(context['checked_at']))
        reproduced = prove_hrs_slot_ownership(account_id,
            dict(payload=parent,evidence_sha256=context['evidence_sha256']),
            financial['slot_inputs'],
            context['ownership_claims'],checked_at=_time(context['checked_at']))
        if reproduced != proof or any(slot[k] != proof[k] for k in ('account_symbols','hrs_symbols')):
            raise ValueError('original owner proof differs from committed reservation')
        # The immutable reservation_success receipt is the historical pending source.
        # No current database rows can replace that original account-lock inventory.
        pending = context['pending_reservations']
        _bind_hrs_floor_pending_to_rows(context,slot,pending,account=account_id,client_id=client)
        for row in pending:
            quantity, price, leverage = (_number(row[k]) for k in ('quantity','price','leverage'))
            margin, pressure = (_number(row[k]) for k in ('margin','pressure_risk'))
            if (quantity <= 0 or leverage <= 0 or price <= 0 or margin != quantity*price/leverage
                    or pressure <= 0 or _number(row['original_quantity']) < quantity
                    or row['original_pressure_risk'] is not None and _number(row['original_pressure_risk']) < pressure):
                raise ValueError('pending financial source invalid')
        local_risk = calculate_open_position_risk(context['local_positions'],fee_rate=inputs['fee_rate'],pressure_slippage=inputs['pressure_slippage'])
        _validate_hrs_floor_finance(context,slot,financial,pending,_number(context['full_allocation']),policy,expected)
        if (local_risk is None or local_risk != _number(context['planned_budget_inputs']['existing_open_risk'])
                or _number(context['account_margin']) != _number(proof['account_margin'])
                or _number(decision['effective_allocation']) != _number(context['snapshot_effective_allocation'])
                or _number(decision['effective_allocation']) > _number(context['planned_budget_inputs']['allocation'])*_number(policy['multiplier'])):
            raise ValueError('locked original risk or margin source invalid')
        return expected
    except (KeyError,ValueError,TypeError,AttributeError,ArithmeticError,EntryDenied,HrsOccupancyDenied) as exc:
        raise ValueError('minimum_order_budget_provenance_unproven') from exc


def _engineering_checks(lifecycle, evidence, through, account_id, *, budget_source_archives=()):
    """Join source identities; request fields never fill missing exchange fields."""
    checks = {'budget': {'status': 'NOT_CAPTURED'},
              'protection_quantity_and_price': {'status': 'NOT_CAPTURED'},
              'exit_role_reconciliation': {'status': 'NOT_CAPTURED'}}
    records, invalid = [], False
    contexts = defaultdict(set)
    policies = defaultdict(list)
    for raw in evidence:
        if raw.get('kind') != 'scan_start':
            continue
        try:
            payload = _object(raw['payload'])
            valid = (canonical_payload(payload)[1] == raw.get('payload_sha256')
                     and _time(raw['event_time']) <= _time(raw['available_at']) <= through)
            contexts[raw.get('run_id')].add(payload.get('financial_context', {}).get('entry_policy', {}).get('account_id') if valid else None)
            if valid:policies[raw.get('run_id')].append(payload.get('financial_context', {}).get('entry_policy', {}))
        except (ValueError, TypeError, KeyError):
            contexts[raw.get('run_id')].add(None)
    for raw in evidence:
        if raw.get('symbol') != lifecycle['symbol']:
            continue
        if contexts[raw.get('run_id')] != {account_id}:
            continue
        try:
            p = _object(raw['payload'])
            at, available = _time(raw['event_time']), _time(raw['available_at'])
            if canonical_payload(p)[1] != raw.get('payload_sha256') or not at <= available <= through:
                raise ValueError('capture_integrity_unproven')
            envelope={key:raw.get(key) for key in ('evidence_id','run_id','kind','symbol','payload_sha256','recorded_at')}
            envelope.update(event_time=at,available_at=available)
            if envelope['recorded_at'] is not None:envelope['recorded_at']=_time(envelope['recorded_at'])
            records.append((raw['kind'], dict(p,_capture_run_id=raw.get('run_id'),_capture_envelope=envelope), at))
        except (ValueError, TypeError, KeyError):
            invalid = True
    if invalid:
        return {key: {'status': 'PENDING', 'reason': 'capture_integrity_unproven'} for key in checks}
    first_order = lifecycle['order_ids'][0]
    entries = [p['outcome']['order'] for kind, p, _ in records
        if kind == 'order_execution_result' and isinstance(p.get('outcome'), dict)
        and isinstance(p['outcome'].get('order'), dict)
        and str(p['outcome']['order'].get('orderId')) == first_order
        and p['outcome']['order'].get('symbol') == lifecycle['symbol']]
    clients = {row.get('clientOrderId') for row in entries if row.get('clientOrderId')}
    if len(clients) != 1:
        return checks
    client = next(iter(clients))
    budgets = [(p, at) for kind, p, at in records if kind == 'entry_budget_reserved'
        and (p.get('decision') or {}).get('client_order_id') == client
        and at <= _time(lifecycle['opened_at'])]
    if len(budgets) != 1:
        return checks
    p, at = budgets[0]
    d = p['decision']
    if d.get('risk_allocation') is None:
        checks['budget'] = dict(status='PENDING', reason='risk_allocation_not_captured')
        return checks
    try:
        first_side = lifecycle['fill_legs'][0]['side']
        entry_legs = []
        first_exit = _time(lifecycle['closed_at'])
        for leg in lifecycle['fill_legs']:
            if leg['side'] != first_side:
                first_exit = _time(leg['at'])
                break
            entry_legs.append(leg)
        if any(leg['side'] == first_side for leg in lifecycle['fill_legs'][len(entry_legs):]):
            raise ValueError('entry_added_after_reduction')
        original = sum((_number(leg['quantity']) for leg in entry_legs), Decimal(0))
        planned = _number(d['quantity'])
        allocation, loss, limit = (_number(d['effective_allocation']),
            _number(d.get('worst_loss', d.get('stressed_loss'))), _number(d['risk_limit']))
        risk_allocation = _number(d['risk_allocation'])
        stop, entry = _number(p['stop_price']), _number(p['entry_price'])
        ratios = tuple(_number(r) for r in d['exit_ratios'])
        targets = tuple(_number(r) for r in p['take_profit_prices'])
        from .risk_budget import STRICT, FLOOR, policy_budget_version
        version = d.get('budget_version', STRICT)
        if (version not in (STRICT, FLOOR) or not policies[p['_capture_run_id']]
                or any(policy_budget_version(policy) != version for policy in policies[p['_capture_run_id']])):
            raise ValueError('unknown_budget_version')
        if version == FLOOR:
            _floor_budget_provenance(lifecycle,p,d,records,at,account_id,budget_source_archives)
        if not (d.get('allowed') is True and type(d.get('policy_revision')) is int and d['policy_revision'] > 0
            and allocation > 0 and risk_allocation > 0 and 0 < original <= planned
            and 0 < loss <= limit and (version == FLOOR or limit <= risk_allocation*Decimal('.01'))
            and entry > 0 and stop > 0 and (stop < entry if first_side == 'BUY' else stop > entry)
            and len(ratios) == len(targets) == 2 and ratios[0] > 0 and ratios[1] >= 0
            and (sum(ratios) < 1 or ratios == (Decimal(1), Decimal(0)))
            and all(price > entry if first_side == 'BUY' else price < entry for price in targets)
            and all(_number(leg.get('price')) > 0 for leg in lifecycle['fill_legs'])):
            raise ValueError('budget_or_fill_geometry_unproven')
        checks['budget'] = dict(status='RECONCILED', client_order_id=client,
            policy_revision=d['policy_revision'], reserved_quantity=str(planned),
            actual_original_quantity=str(original), worst_loss=str(loss), risk_limit=str(limit),
            effective_allocation=str(allocation), risk_allocation=str(risk_allocation), budget_version=version, entry_mode=p.get('entry_mode'),
            entry_price=str(entry), stop_price=str(stop), exit_ratios=[str(r) for r in ratios])
    except (ValueError, KeyError, TypeError, ArithmeticError):
        checks['budget'] = dict(status='PENDING', reason='budget_or_fill_geometry_unproven')
        return checks
    required = {'sl': (original, stop), 'tp1': (original*ratios[0], targets[0])}
    if ratios[1] > 0:
        required['tp2'] = (original*ratios[1], targets[1])
    proven = {}
    position = f"hrs:{lifecycle['symbol']}:{first_order}"
    for kind, payload, at in records:
        if kind != 'protection_order_ack' or payload.get('position_id') != position or not _time(lifecycle['opened_at']) <= at < first_exit:
            continue
        role, request, response = payload.get('role'), payload.get('request') or {}, payload.get('response') or {}
        if role not in required:
            continue
        try:
            max_quantity, expected_price = required[role]
            actual_price = _number(response.get('triggerPrice', response.get('stopPrice')))
            close_all = _actual_true(response.get('closePosition'))
            actual_qty = None if close_all else _number(response.get('quantity', response.get('origQty')))
            client_ack = response.get('clientAlgoId', response.get('clientOrderId'))
            if not (response.get('symbol') == lifecycle['symbol'] == request.get('symbol')
                and response.get('side') == request.get('side') == ('SELL' if first_side == 'BUY' else 'BUY')
                and response.get('algoId', response.get('orderId')) is not None
                and client_ack and client_ack == request.get('client_id')
                and actual_price == expected_price == _number(request.get('stop_price'))
                and (close_all and role == 'sl' and request.get('close_position') is True
                     or not close_all and _actual_true(response.get('reduceOnly'))
                     and request.get('reduce_only') is True and 0 < actual_qty <= max_quantity
                     and actual_qty == _number(request.get('quantity'))
                     and (role != 'sl' or actual_qty == original))
                and response.get('status', response.get('algoStatus')) not in ('CANCELED','REJECTED','EXPIRED')):
                continue
            proven[role] = dict(exchange_order_id=str(response.get('algoId', response.get('orderId'))),
                actual_quantity=str(actual_qty) if actual_qty is not None else None,
                close_position=close_all, actual_stop_price=str(actual_price))
            child = response.get('actualOrderId')
            if child is None and response.get('algoId') is None:
                child = response.get('orderId')
            if child is not None and _number(child) > 0:
                proven[role]['actual_fill_order_id'] = str(child)
        except (ValueError, KeyError, TypeError, ArithmeticError):
            continue
    checks['protection_quantity_and_price'] = dict(
        status='RECONCILED' if set(proven) == set(required) else 'PENDING',
        required_roles=sorted(required), actual_acknowledgments=proven,
        missing_actual_acknowledgments=sorted(set(required)-set(proven)))
    fill_roles, consumed, unknown = {}, defaultdict(Decimal), []
    for leg in lifecycle['fill_legs'][len(entry_legs):]:
        roles = [role for role, proof in proven.items() if proof.get('actual_fill_order_id') == leg['order_id']]
        if len(roles) != 1:
            unknown.append(leg['trade_id'])
            continue
        role = roles[0]
        consumed[role] += _number(leg['quantity'])
        limit = original if proven[role]['close_position'] else _number(proven[role]['actual_quantity'])
        if consumed[role] > limit:
            unknown.append(leg['trade_id'])
        else:
            fill_roles[leg['trade_id']] = role
    checks['exit_role_reconciliation'] = dict(status='RECONCILED' if not unknown else 'PENDING',
        actual_fill_roles=fill_roles, unlinked_exit_trade_ids=unknown,
        caliber='actual_exchange_conditional_child_order_identity_only')
    return checks


def reconcile_lifecycles(fills, orders, incomes, economic, *, account_id,
        inception, through, anchor_certified=False, opening_inventory=(), decision_evidence=(), budget_source_archives=()):
    """Observe actual signed fill legs, then attach uniquely reconciled incomes.

    Opening positions are tracked to flat but excluded: their entry predates this
    observation segment. Cross-zero fills and uncertain ownership are unsupported,
    and conservatively leave all five financial samples pending.
    """
    reasons = set()
    if not anchor_certified:
        reasons.add('opening_inventory_unproven')
    if economic.get('id_match_and_coverage_complete') is not True:
        reasons.add('income_or_fill_reconciliation_incomplete')
    owners = _owners(orders, account_id)
    quantities, active, closed, unique = defaultdict(Decimal), {}, [], {}
    for row in opening_inventory:
        if row.get('strategy') == 'hrs':
            quantities[row['symbol']] += _number(row['quantity'])
    for raw in fills:
        row = dict(raw)
        if row.get('account_id') != account_id:
            continue
        at = _time(row['trade_time_utc'])
        if not inception < at <= through:
            continue
        key = (row['symbol'], str(row['trade_id']))
        if key in unique and unique[key] != row:
            reasons.add('conflicting_fill_identity')
        unique[key] = row
    for row in sorted(unique.values(), key=lambda r: (_time(r['trade_time_utc']), str(r['symbol']), int(r['trade_id']))):
        symbol, at = row['symbol'], _time(row['trade_time_utc'])
        ownership = owners.get((symbol, str(row['order_id'])), set())
        if len(ownership) != 1:
            reasons.add('fill_owner_unproven')
            continue
        if ownership != {'hrs'}:
            continue
        if row.get('commission') is None or row.get('realized_pnl') is None:
            reasons.add('fill_fee_or_realized_amount_missing')
        elif _number(row['commission']) != 0 and row.get('commission_asset') != 'USDT':
            reasons.add('non_usdt_fee_not_reconciled')
        qty = _number(row.get('qty'))
        if qty <= 0 or row.get('side') not in ('BUY', 'SELL'):
            reasons.add('fill_quantity_or_side_invalid')
            continue
        signed = qty if row['side'] == 'BUY' else -qty
        before, after = quantities[symbol], quantities[symbol]+signed
        if before*after < 0:
            reasons.add('cross_zero_fill_unsupported')
        if before == 0:
            active[symbol] = dict(symbol=symbol, opened_at=at.isoformat(), fill_ids=[], order_ids=[], fill_count=0, fill_legs=[])
        lifecycle = active.get(symbol)
        if lifecycle is not None:
            lifecycle['fill_ids'].append(str(row['trade_id']))
            lifecycle['order_ids'].append(str(row['order_id']))
            lifecycle['fill_count'] += 1
            lifecycle['fill_legs'].append(dict(trade_id=str(row['trade_id']), order_id=str(row['order_id']),
                side=row['side'], quantity=str(qty), at=at.isoformat(),
                price=str(row['price']) if row.get('price') is not None else None,
                commission=str(row['commission']) if row.get('commission') is not None else None,
                commission_asset=row.get('commission_asset'),
                realized_pnl=str(row['realized_pnl']) if row.get('realized_pnl') is not None else None))
        quantities[symbol] = after
        if after == 0 and lifecycle is not None:
            lifecycle['closed_at'] = at.isoformat()
            closed.append(active.pop(symbol))
    closed.sort(key=lambda s: (s['closed_at'], s['symbol'], s['fill_ids'][0]))
    income_map = {}
    for row in incomes:
        if row.get('account_id') != account_id:
            continue
        key = (str(row['income_type']), str(row['tran_id']))
        if key in income_map and income_map[key] != row:
            reasons.add('conflicting_income_identity')
        income_map[key] = row
    totals = [dict(realized_pnl_usdt=Decimal(0), commission_usdt=Decimal(0), funding_usdt=Decimal(0)) for _ in closed]
    mapped_events = set()
    for event in economic.get('events', []):
        if event.get('strategy') != 'hrs':
            continue
        identity = (str(event['income_type']), str(event['tran_id']))
        source = income_map.get(identity)
        if (source is None or event.get('account_id') != account_id or source.get('asset') != 'USDT'
            or source.get('symbol') != event.get('symbol') or _number(source.get('income')) != _number(event.get('amount'))
            or not inception < _time(source.get('event_time_utc')) <= through
            or _time(source.get('event_time_utc')) != _time(event.get('event_time_utc'))):
            reasons.add('reconciled_income_source_missing_or_mismatched')
            continue
        if identity in mapped_events:
            reasons.add('duplicate_reconciled_income_identity')
            continue
        mapped_events.add(identity)
        matches = []
        for index, lifecycle in enumerate(closed):
            if lifecycle['symbol'] != source['symbol']:
                continue
            if source['income_type'] in ('REALIZED_PNL', 'COMMISSION'):
                matched = str(source.get('trade_id')) in lifecycle['fill_ids']
            else:
                at = _time(source['event_time_utc'])
                matched = _time(lifecycle['opened_at']) < at < _time(lifecycle['closed_at'])
                if at in (_time(lifecycle['opened_at']), _time(lifecycle['closed_at'])):
                    reasons.add('funding_at_lifecycle_boundary_unproven')
            if matched:
                matches.append(index)
        if len(matches) > 1:
            reasons.add('income_lifecycle_ambiguous')
        elif matches:
            component = {'REALIZED_PNL': 'realized_pnl_usdt', 'COMMISSION': 'commission_usdt', 'FUNDING_FEE': 'funding_usdt'}.get(source['income_type'])
            if component is None:
                reasons.add('unsupported_lifecycle_income_type')
            else:
                totals[matches[0]][component] += _number(source['income'])
    samples = []
    for index, lifecycle in enumerate(closed[:5]):
        checks = _engineering_checks(lifecycle, decision_evidence, through, account_id, budget_source_archives=budget_source_archives)
        checks.update(exchange_fill_lifecycle={'status': 'RECONCILED' if not reasons else 'PENDING'},
                      fees_and_funding={'status': 'RECONCILED' if not reasons else 'PENDING'})
        engineering_ready = all(v['status'] == 'RECONCILED' for v in checks.values())
        samples.append(dict(**lifecycle, status='RECONCILED' if not reasons else 'PENDING',
            net_income_usdt=str(sum(totals[index].values())) if not reasons else None,
            **{k: str(v) if not reasons else None for k, v in totals[index].items()},
            engineering_status='READY' if engineering_ready else 'PENDING', engineering_checks=checks,
            income_report_id=economic.get('report_id')))
    while len(samples) < 5:
        samples.append(dict(status='PENDING', reason='awaiting_natural_closed_lifecycle', net_income_usdt=None))
    return dict(status='READY' if len(closed) >= 5 and not reasons else 'PENDING',
        observed_closed_lifecycles=len(closed), required_lifecycles=5, samples=samples,
        reasons=sorted(reasons), income_report_id=economic.get('report_id'),
        observation_caliber='prospective_actual_exchange_flat_to_flat_fills',
        engineering_status='READY' if len(closed) >= 5 and all(s.get('engineering_status') == 'READY' for s in samples) else 'PENDING',
        performance_accepted=False)


class _Reader:
    def __init__(self, connection):
        self.connection = connection

    async def fetch_all(self, sql, *args):
        return await self.connection.fetch(sql, *args)


def _compact_record(raw):
    """Validate the entire immutable source before discarding bulky raw bars."""
    row = dict(raw)
    payload = _object(row['payload'])
    valid = canonical_payload(payload)[1] == row['payload_sha256']
    row['source_payload_sha256'] = row['payload_sha256']
    row['payload'] = {k: v for k, v in payload.items() if k not in ('klines_1h', 'klines_4h')}
    row['payload_sha256'] = canonical_payload(row['payload'])[1] if valid else 'source_payload_hash_mismatch'
    return row


async def _read_evidence(connection, sql, *args):
    # Holding every raw analysis row at once can retain millions of candles.
    # The cursor lives inside the caller's coherent read-only transaction.
    return [_compact_record(row) async for row in connection.cursor(sql, *args, prefetch=10)]


async def _read_budget_sources(connection, financial, evidence, account_id, through):
    """Fetch at most one immutable parent per reported lifecycle, in one query."""
    from .risk_budget import FLOOR
    valid = []
    for raw in evidence:
        try:
            payload = _object(raw['payload'])
            if canonical_payload(payload)[1] != raw['payload_sha256'] or not _time(raw['event_time']) <= _time(raw['available_at']) <= through:
                continue
            valid.append((raw,payload))
        except (ValueError,KeyError,TypeError):
            continue
    digests = set()
    for sample in financial['samples'][:5]:
        if not sample.get('order_ids'):
            continue
        entries = [(r,p) for r,p in valid if r['kind']=='order_execution_result'
            and r.get('symbol')==sample['symbol']
            and str((p.get('outcome') or {}).get('order',{}).get('orderId'))==sample['order_ids'][0]]
        clients = {(p.get('outcome') or {}).get('order',{}).get('clientOrderId') for r,p in entries}
        if len(clients)!=1 or not next(iter(clients)):
            continue
        client = next(iter(clients))
        budgets = [(r,p) for r,p in valid if r['kind']=='entry_budget_reserved'
            and r.get('symbol')==sample['symbol'] and (p.get('decision') or {}).get('client_order_id')==client
            and (p.get('decision') or {}).get('budget_version')==FLOOR]
        if len(budgets)!=1:
            continue
        stages = [p for r,p in valid if r['kind']=='entry_stage_inputs'
            and r.get('symbol')==sample['symbol'] and r['run_id']==budgets[0][0]['run_id']
            and p.get('stage')=='reservation_success' and p.get('client_order_id')==client]
        if len(stages)==1:
            digest = (stages[0].get('budget_context') or {}).get('evidence_sha256')
            if isinstance(digest,str) and len(digest)==64 and all(c in '0123456789abcdef' for c in digest):
                digests.add(digest)
    if not digests:
        return []
    return [dict(row) for row in await connection.fetch(
        '''SELECT payload,evidence_sha256 FROM trading.hrs_equity_evidence
        WHERE account_id=$1 AND evidence_sha256=ANY($2::text[]) AND available_at <= $3''',
        account_id,sorted(digests),through)]


def _refresh_sample_engineering(financial, evidence, through, account_id, archives):
    """Attach source proofs without repeating any economic reconciliation."""
    for sample in financial['samples']:
        if 'engineering_checks' not in sample:
            continue
        previous = sample['engineering_checks']
        checks = _engineering_checks(sample,evidence,through,account_id,budget_source_archives=archives)
        checks.update({key:previous[key] for key in ('exchange_fill_lifecycle','fees_and_funding')})
        sample.update(engineering_checks=checks,
            engineering_status='READY' if all(v['status']=='RECONCILED' for v in checks.values()) else 'PENDING')
    financial['engineering_status'] = ('READY' if financial['observed_closed_lifecycles']>=5
        and all(sample.get('engineering_status')=='READY' for sample in financial['samples']) else 'PENDING')


async def build_report(db, *, as_of=None, account_id=None):
    """One held connection/readonly RR snapshot after income's session lock.

    Source failure returns NOT_READY/PENDING, with only the exception *type*.
    This report cannot authorize orders and never changes risk or source tables.
    """
    now = _time(as_of or datetime.now(UTC))
    account = account_id or os.getenv('TRADE_ACCOUNT_ID')
    result = dict(report_version='hrs-continuous-acceptance-v1', generated_at=now.isoformat(),
                  account_id=account, database_snapshot_coherent=False, performance_accepted=False)
    rows, fills, owners = [], [], []
    financial = reconcile_lifecycles([], [], [], {}, account_id=account,
                                    inception=now, through=now)
    try:
        if not account or getattr(db, 'pool', None) is None:
            raise ValueError('account_or_database_pool_unavailable')
        async with db.pool.acquire() as connection:
            async with income_account_lock(connection, account):
                async with connection.transaction(isolation='repeatable_read', readonly=True):
                    await require_income_integrity(connection, account)
                    start = now.replace(minute=0, second=0, microsecond=0)-timedelta(hours=24)
                    rows = await _read_evidence(connection, '''SELECT evidence_id,run_id,kind,symbol,event_time,available_at,payload_sha256,payload
                        FROM hrs.hrs_decision_evidence WHERE event_time >= $1 AND event_time <= $2
                        AND available_at <= $2 AND NOT(kind=ANY($3::text[])) ORDER BY event_time,evidence_id''', start, now, EXCLUDED_KINDS)
                    anchor = await connection.fetchrow('SELECT inception,opening_evidence FROM trading.hrs_equity_segment WHERE account_id=$1', account)
                    stored = await connection.fetchrow('SELECT ledger_state,version FROM trading.hrs_loss_gate_state WHERE account_id=$1', account)
                    inception = _time(anchor['inception']) if anchor else start
                    query_start = min(start, inception)
                    fills = [dict(r) for r in await connection.fetch('''SELECT account_id,symbol,trade_id,order_id,side,qty,price,commission,commission_asset,realized_pnl,trade_time_utc
                        FROM trading.execution_fact_fill WHERE account_id=$1 AND trade_time_utc >= $2 AND trade_time_utc <= $3''', account, query_start, now)]
                    owners = [dict(r) for r in await connection.fetch('SELECT account_id,symbol,order_id,strategy FROM trading.trade_records WHERE account_id=$1 AND order_id=ANY($2::text[])', account, sorted({str(f['order_id']) for f in fills}))]
                    if anchor and stored:
                        through = _time(_object(stored['ledger_state'])['as_of'])
                        if not inception <= through <= now or (now-through).total_seconds() > 120:
                            raise ValueError('reconciled_source_stale_or_noncausal')
                        proof = _object(anchor['opening_evidence'])
                        lifecycle_evidence = await _read_evidence(connection, '''SELECT evidence_id,run_id,kind,symbol,event_time,available_at,payload_sha256,payload,recorded_at
                            FROM hrs.hrs_decision_evidence WHERE event_time >= $1 AND available_at <= $2
                            AND (kind=ANY($3::text[]) OR (kind='entry_stage_inputs' AND payload->>'stage' IN ('reservation_success','reservation_inventory')))''', inception, through,
                            ['scan_start','entry_budget_reserved','order_execution_result','protection_order_ack'])
                        incomes = [dict(r) for r in await connection.fetch('''SELECT account_id,symbol,trade_id,tran_id,income_type,income,asset,event_time_utc
                            FROM trading.execution_fact_income WHERE account_id=$1 AND event_time_utc > $2 AND event_time_utc <= $3
                            AND income_type=ANY($4::text[])''', account, inception, through, ['COMMISSION','REALIZED_PNL','FUNDING_FEE'])]
                        economic = await read_monthly_economic_report(_Reader(connection), account,
                            int(inception.timestamp()*1000)+1, int(through.timestamp()*1000)+1, include_reviewed_overrides=True)
                        archives = await connection.fetch('''SELECT payload,evidence_sha256 FROM trading.hrs_equity_evidence
                            WHERE account_id=$1 AND as_of=$2 AND available_at <= $3 ORDER BY available_at DESC''', account, through, now)
                        archive = next((r for r in archives
                            if _object(r['payload']).get('result', {}).get('ledger_version') == stored['version']), None)
                        funding_rows=funding_count=None
                        if archive:
                            raw=_object(archive['payload'])
                            if canonical(raw)[1]!=archive['evidence_sha256']:
                                raise ValueError('equity_archive_parent_hash_mismatch')
                            publication=raw.get('capture',{}).get('funding_publication',{})
                            if 'forecasts_ref' in publication:
                                cutoff=validate_forecasts_ref(publication['forecasts_ref'],account_id=account)
                                funding_count=await connection.fetchval(COUNT_SQL,account,cutoff)
                                funding_rows=await connection.fetch(ROWS_SQL,account,cutoff)
                                resolve_evidence_forecasts(archive,funding_rows,account_id=account,
                                    expected_count=funding_count,collect=False)
                        economic = _apply_verified_funding(economic, archive, account_id=account,
                            inception=inception, through=through, fills=fills, orders=owners,
                            opening_inventory=proof.get('opening_inventory', ()),
                            funding_source_rows=funding_rows,funding_source_count=funding_count)
                        financial = reconcile_lifecycles(fills, owners, incomes, economic,
                            account_id=account, inception=inception, through=through,
                            anchor_certified=proof.get('account_flat_anchor_certified') is True or proof.get('opening_inventory_certified') is True,
                            opening_inventory=proof.get('opening_inventory', ()), decision_evidence=lifecycle_evidence)
                        budget_archives = await _read_budget_sources(connection,financial,lifecycle_evidence,account,through)
                        if budget_archives:
                            _refresh_sample_engineering(financial,lifecycle_evidence,through,account,budget_archives)
                        financial['reconciled_through'] = through.isoformat()
                    result['database_snapshot_coherent'] = True
    except Exception as exc:
        result['source_error_type'] = type(exc).__name__
        # Never retain stale passing financial evidence after a failed snapshot.
        financial = reconcile_lifecycles([], [], [], {}, account_id=account, inception=now, through=now)
    technical = summarize_evidence(rows, as_of=now, account_id=account, fills=fills, orders=owners)
    if not result['database_snapshot_coherent']:
        technical['status'] = 'NOT_READY'
    result.update(technical=technical, financial=financial,
                  status='OBSERVATION_READY' if technical['status'] == 'PASS' and financial['engineering_status'] == 'READY' else 'PENDING')
    return result


def write_report_atomic(report, path=DEFAULT_OUTPUT):
    """Serialize completely before replacement; preserve the last file on error."""
    data = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n'
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=target.parent,
                                          prefix='.'+target.name+'.', delete=False) as stream:
            temporary = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


async def _main(args):
    import asyncpg
    pool = await asyncpg.create_pool(os.environ['DATABASE_URL'], min_size=1, max_size=1)
    try:
        while True:
            write_report_atomic(await build_report(SimpleNamespace(pool=pool)), args.output)
            if not args.interval:
                return
            await asyncio.sleep(args.interval)
    finally:
        await pool.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=DEFAULT_OUTPUT)
    parser.add_argument('--interval', type=int, default=0, help='seconds; 0 writes once')
    args = parser.parse_args()
    if args.interval < 0:
        parser.error('--interval must be nonnegative')
    asyncio.run(_main(args))
