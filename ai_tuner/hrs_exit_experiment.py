"""Preregistered HRS exit experiments over actual frozen raw-input replays.

Run ``python -m ai_tuner.hrs_exit_experiment --help``. Research and holdout
are separate invocations: holdout requires a frozen selection referencing the
canonical research report hash. No dataset, score or bootstrap state is invented.
An AVAILABLE experiment is evidence, never production admission or publication.
Only the explicit HrsAdapter experiment API changes exits for NEW positions;
captured configuration identities and existing-position parameters stay intact.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import importlib
from importlib import metadata
import inspect
from types import CodeType
import platform
import sys
import json
from pathlib import Path
import random
import re
from zoneinfo import ZoneInfo

from ai_tuner.replay.adapters.hrs import HrsAdapter
from ai_tuner.replay.artifacts import canonical_json, to_jsonable, write_report
from ai_tuner.replay.broker import SymbolFilters, VirtualBroker
from ai_tuner.replay.data_portal import DataPortal
from ai_tuner.replay.engine import run_replay
from ai_tuner.replay.models import DataBlocked, FutureDataAccess, parse_utc
from strategies.hrs.exit_geometry import mode_exit_parameters
from ai_tuner.hrs_evidence_export import resolve_scan_start

CST = ZoneInfo('Asia/Shanghai')
D = Decimal
TIERS = {'baseline': D(1), '1.5x': D('1.5'), '2.0x': D(2)}
EXIT_KEYS = frozenset({'atr_multiplier', 'emergency_percent', 'min_absolute_percent',
    'target1_atr_multiplier', 'target1_close_percent', 'target2_atr_multiplier',
    'target2_close_percent', 'trailing_atr_multiplier', 'max_holding_hours'})
REQUIRED_FILES = frozenset({'hrs_replay_state.json', 'hrs_decision_inputs.jsonl',
    'hrs_decision_calendar.jsonl', 'hrs_cash_flows.jsonl', 'funding_schedule_evidence.json',
    'universe_slices.json', 'config_hashes.json', 'exchange_info.json'})


def fingerprint(value):
    encoded = json.dumps(to_jsonable(value), sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


# Loaded-module paths bind the code this interpreter imports, rather than a Git
# commit annotation or a clean/dirty worktree assumption. Include shared helpers
# called lazily by scoring/risk and the runner/report implementation itself.
ENGINE_MODULES = (
    'ai_tuner.hrs_evidence_export',
    'ai_tuner.replay.adapters.hrs', 'ai_tuner.replay.adapters.base',
    'ai_tuner.replay.engine', 'ai_tuner.replay.broker', 'ai_tuner.replay.models',
    'ai_tuner.replay.data_portal', 'ai_tuner.replay.clock',
    'ai_tuner.replay.funding_history', 'ai_tuner.replay.artifacts',
    'ai_tuner.funding_schedule.models', 'strategies.hrs.core_decision',
    'strategies.hrs.scoring_engine', 'strategies.hrs.pattern',
    'strategies.hrs.exit_geometry', 'strategies.hrs.risk_budget',
    'strategies.hrs.risk_ledger', 'strategies.hrs.loss_gate',
    'shared.entry_risk_policy', 'shared.circuit_breaker', 'shared.config_loader',
    'shared.trade_logger',
)
ENGINE_DEPENDENCIES = ('structlog', 'asyncpg', 'PyYAML', 'numpy', 'pandas', 'tzdata')


def _loaded_code_digest(module):
    # Remove checkout paths while preserving the actual loaded bytecode. This
    # distinguishes a cached/monkey-patched interpreter from fresh source bytes.
    def constant(value):
        if isinstance(value,CodeType):return normalized(value)
        if value is None or type(value) in (bool,int,str):return value
        if isinstance(value,bytes):return {'bytes':value.hex()}
        if isinstance(value,float):return {'float':value.hex()}
        if isinstance(value,complex):return {'complex':[value.real.hex(),value.imag.hex()]}
        if isinstance(value,tuple):return {'tuple':[constant(item) for item in value]}
        if isinstance(value,frozenset):return {'frozenset':sorted((constant(item) for item in value),key=fingerprint)}
        if value is Ellipsis:return {'ellipsis':True}
        raise TypeError('unsupported_runtime_code_constant')
    def normalized(code):
        # marshal reference/intern flags can change after first execution even
        # when code is unchanged. Hash semantic code fields instead of those
        # serialization details; CPython exposes de-specialized co_code here.
        return dict(bytecode=code.co_code.hex(),constants=[constant(item) for item in code.co_consts],
            names=code.co_names,varnames=code.co_varnames,freevars=code.co_freevars,cellvars=code.co_cellvars,
            argcount=code.co_argcount,posonlyargcount=code.co_posonlyargcount,kwonlyargcount=code.co_kwonlyargcount,
            nlocals=code.co_nlocals,stacksize=code.co_stacksize,flags=code.co_flags,
            exceptiontable=getattr(code,'co_exceptiontable',b'').hex())
    functions={}
    def collect(name,value):
        if isinstance(value,(classmethod,staticmethod)):value=value.__func__
        if inspect.isfunction(value):
            functions[name]=fingerprint(normalized(value.__code__))
        elif isinstance(value,property):
            for role in ('fget','fset','fdel'):
                if getattr(value,role) is not None:collect(name+'.'+role,getattr(value,role))
    for name,value in vars(module).items():
        collect(name,value)
        if inspect.isclass(value) and value.__module__==module.__name__:
            for method,descriptor in vars(value).items():collect(name+'.'+method,descriptor)
    return fingerprint(functions)


def engine_identity():
    """Content identity of actual runtime sources and relevant environment.

    Identity is engineering traceability, never source/performance acceptance.
    A missing source file remains an explicit unavailable identity; no Git hash
    or installed package version is substituted for missing runtime source.
    """
    files = {}; loaded_code = {}; gaps = []
    for name in ENGINE_MODULES:
        try:
            loaded = importlib.import_module(name)
            loaded_code[name] = _loaded_code_digest(loaded)
            source = inspect.getsourcefile(loaded)
            if source is None:
                raise ValueError('source_file_missing')
            files[name.replace('.', '/')+'.py'] = hashlib.sha256(Path(source).read_bytes()).hexdigest()
        except (ImportError, OSError, TypeError, ValueError) as error:
            gaps.append('engine_source_unavailable:'+name+':'+type(error).__name__)
    try:
        files['ai_tuner/hrs_exit_experiment.py'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        loaded_code['ai_tuner.hrs_exit_experiment'] = _loaded_code_digest(sys.modules[__name__])
    except OSError as error:
        gaps.append('engine_source_unavailable:runner:'+type(error).__name__)
    dependencies = {}
    for name in ENGINE_DEPENDENCIES:
        try:
            dependencies[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            dependencies[name] = None  # Absence is an identity fact, never version 0.
    identity = dict(schema_version=1, model='hrs_engine_runtime_v1',
        status='UNAVAILABLE' if gaps else 'AVAILABLE', source_files=files, loaded_code_sha256=loaded_code,
        python=dict(implementation=platform.python_implementation(), version=sys.version,
            cache_tag=sys.implementation.cache_tag, optimization=sys.flags.optimize,
            machine=platform.machine(), system=platform.system()),
        dependencies=dependencies, blocked_reasons=gaps)
    identity['sha256'] = fingerprint(identity)
    return identity


def _require_runtime_identity(expected, reason):
    current = engine_identity()
    if current['status'] != 'AVAILABLE':
        raise DataBlocked(current['blocked_reasons'] or ['engine_identity_unavailable'])
    if current != expected:
        raise DataBlocked([reason])


def _decimal(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError('invalid_number')
    number = D(str(value))
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError('invalid_number')
    return number


def _time(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError('timestamp_required')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError('timezone_required')
    return parse_utc(dt)


def validate_registration(registration):
    """Validate without mutating the actual baseline or candidate registration."""
    if not isinstance(registration, dict) or registration.get('schema_version') != 1 or not registration.get('experiment_id'):
        raise ValueError('experiment_identity_required')
    baseline = registration['baseline']
    if baseline.get('source') != 'production_effective' or not isinstance(baseline.get('config'), dict):
        raise ValueError('actual_baseline_required')
    if fingerprint(baseline['config']) != baseline.get('effective_config_sha256'):
        raise ValueError('baseline_config_hash_mismatch')
    if not re.fullmatch('[0-9a-f]{40}', baseline.get('code_commit', '')):
        raise ValueError('baseline_code_identity_required')
    hashes = baseline.get('config_hashes', {})
    if set(hashes) != {'base_config_hash', 'params_manifest_hash', 'overlay_hash'} or any(
            not re.fullmatch('[0-9a-f]{64}', value or '') for value in hashes.values()):
        raise ValueError('baseline_config_identity_required')
    groups = defaultdict(list)
    ids = set()
    for candidate in registration['candidates']:
        identity, mode, overrides = candidate['candidate_id'], candidate['mode'], candidate['exit_parameters']
        if not identity or identity in ids or mode not in ('standard', 'lv_rm') or not isinstance(overrides, dict):
            raise ValueError('candidate_identity_invalid')
        ids.add(identity); groups[mode].append(candidate)
        if len(groups[mode]) > 12:
            raise ValueError('candidate_limit_per_mode')
        if set(overrides) - EXIT_KEYS:
            raise ValueError('unsupported_exit_parameter')
        allocation_factor = (candidate.get('factor') == 'tp_allocation' and
            set(overrides) == {'target1_close_percent', 'target2_close_percent'})
        if len(overrides) > 1 and not allocation_factor:
            raise ValueError('one_factor_per_candidate_required')
        for key, value in overrides.items():
            number = _decimal(value, positive=key != 'target2_close_percent')
            if number < 0:
                raise ValueError('exit_parameter_out_of_range')
            if (key.endswith('_percent') and number > 1) or (key == 'max_holding_hours' and number > 720):
                raise ValueError('exit_parameter_out_of_range')
        params = dict(mode_exit_parameters(baseline['config'], mode), **overrides)
        if D(str(params['target1_close_percent']))+D(str(params['target2_close_percent'])) > 1:
            raise ValueError('exit_allocation_exceeds_original_quantity')
    if not groups or any(sum(not c['exit_parameters'] for c in candidates) != 1 for candidates in groups.values()):
        raise ValueError('exactly_one_baseline_per_mode_required')
    windows = registration['windows']
    if len({w['window_id'] for w in windows}) != len(windows):
        raise ValueError('window_identity_duplicate')
    research = [w for w in windows if w['role'] == 'research']
    holdouts = sorted((w for w in windows if w['role'] == 'holdout'), key=lambda w: _time(w['start']))
    if len(research) != 1 or len(holdouts) != 2 or len(windows) != 3:
        raise ValueError('research_and_two_holdout_months_required')
    for window in windows:
        if _time(window['start']) >= _time(window['end']):
            raise ValueError('invalid_window')
        for field in ('start_policy','end_policy'):
            policy=window.get(field)
            if policy is not None and (not isinstance(policy,dict) or policy.get('version')!='first_scan_start_v1'
                    or type(policy.get('max_delay_seconds')) is not int or not 0<policy['max_delay_seconds']<=3600):
                raise ValueError('scan_boundary_policy_invalid')
        model=window.get('observation_model','full_calendar_month')
        if model not in ('full_calendar_month','scan_aligned_month_v1'):
            raise ValueError('unknown_observation_model')
        if model=='scan_aligned_month_v1' and (not window.get('start_policy') or
                window.get('start_policy')!=window.get('end_policy') or
                type(window.get('minimum_duration_seconds')) is not int or
                not 28*86400<=window['minimum_duration_seconds']<=(_time(window['end'])-_time(window['start'])).total_seconds()):
            raise ValueError('scan_aligned_month_contract_invalid')
    for window in holdouts:
        start, end = (_time(window[key]).astimezone(CST) for key in ('start', 'end'))
        next_month = (start.replace(year=start.year+1, month=1) if start.month == 12
                      else start.replace(month=start.month+1))
        if start.day != 1 or start.time() != datetime.min.time() or end != next_month:
            raise ValueError('holdout_month_must_be_exact_cst_calendar_month')
    ordered = research + holdouts
    if any(_time(a['end']) > _time(b['start']) for a, b in zip(ordered, ordered[1:])):
        raise ValueError('chronological_windows_overlap')
    if any(w.get('observation_model')=='scan_aligned_month_v1' for w in holdouts):
        if any(w.get('observation_model')!='scan_aligned_month_v1' for w in holdouts) or any(
                _time(a['end'])!=_time(b['start']) or a.get('end_policy')!=b.get('start_policy')
                for a,b in zip(ordered,ordered[1:])):
            raise ValueError('scan_aligned_month_shared_boundaries_required')
    if type(registration.get('minimum_closed_lifecycles')) is not int or registration['minimum_closed_lifecycles'] < 20:
        raise ValueError('minimum_sample_must_be_preregistered')
    model = registration['cost_model']
    for key in ('fee_bps', 'slippage_bps', 'stop_offset_pct', 'tp_offset_pct', 'max_leverage'):
        number = _decimal(model[key], positive=key == 'max_leverage')
        if number < 0 or (key.endswith('_pct') and number >= 1):
            raise ValueError('cost_model_invalid')
    return fingerprint(registration)


def discover_snapshots(roots):
    """Inventory only manifest-bound HRS snapshots; a filename is not qualification."""
    found = []
    for root in roots:
        root = Path(root).expanduser().resolve()
        paths = [root/'manifest.json'] if (root/'manifest.json').is_file() else sorted(root.rglob('manifest.json')) if root.is_dir() else []
        for path in paths:
            try:
                raw = json.loads(path.read_text())
                files = set(raw.get('files', {}))
                if raw.get('strategy_id') != 'hrs' and not {'hrs_replay_state.json', 'hrs_decision_inputs.jsonl'} & files:
                    continue
                found.append({'snapshot_dir': str(path.parent), 'manifest_file_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'as_of': raw.get('as_of'), 'window_start': raw.get('window_start'),
                    'missing_required_files': sorted(REQUIRED_FILES-files)})
            except (ValueError, OSError, TypeError):
                continue
    return sorted({row['snapshot_dir']: row for row in found}.values(), key=lambda row: row['snapshot_dir'])


def nav_metrics(checkpoints, start, end):
    """Cashflow-adjusted NAV, with exact Shanghai midnight observations."""
    points = sorted((p for p in checkpoints if start <= p.as_of <= end), key=lambda p: p.as_of)
    result = {'status': 'AVAILABLE', 'nav_return': None, 'max_drawdown': None,
              'daily_returns_cst': [], 'blocked_reasons': []}
    if not points or points[0].as_of != start or points[-1].as_of != end or len({p.as_of for p in points}) != len(points):
        result.update(status='BLOCKED_DATA', blocked_reasons=['nav_window_boundaries_missing'])
        return result
    values = {p.as_of: _decimal(p.nav, positive=True) for p in points}
    peak = values[start]; worst = D(0)
    for p in points:
        peak = max(peak, values[p.as_of]); worst = max(worst, 1-values[p.as_of]/peak)
    result.update(nav_return=str(values[end]/values[start]-1), max_drawdown=str(worst))
    midnight = start.astimezone(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    if midnight < start:
        midnight += timedelta(days=1)
    boundaries = []
    while midnight <= end:
        if midnight not in values:
            result['blocked_reasons'].append('cst_midnight_nav_missing:'+midnight.isoformat())
        else:
            boundaries.append(values[midnight])
        midnight += timedelta(days=1)
    if result['blocked_reasons']:
        result['status'] = 'BLOCKED_DATA'
    else:
        result['daily_returns_cst'] = [str(b/a-1) for a, b in zip(boundaries, boundaries[1:])]
    return result


def lifecycle_metrics(trades, entry_risks, *, minimum):
    grouped = defaultdict(list)
    for trade in trades:
        grouped[trade.position_id].append(trade)
    result = dict(status='AVAILABLE', closed_lifecycles=len(grouped), net_pnl_per_entry_risk=None,
                  without_best_lifecycle_net_pnl=None, worst_lifecycle_r=None,
                  longest_losing_streak=0, by_direction={}, monthly_net_pnl={}, blocked_reasons=[],
                  mean_holding_hours=None, remaining_position_giveback=None,
                  giveback_unavailable_reason='position_lifecycle_peak_equity_not_exported')
    if len(grouped) < minimum:
        result['blocked_reasons'].append('minimum_closed_lifecycles_not_met')
    outcomes = []
    for identity, legs in grouped.items():
        risk = entry_risks.get(identity)
        if risk is None or not _decimal(risk, positive=True):
            result['blocked_reasons'].append('entry_risk_missing:'+identity)
            continue
        net = sum((_decimal(t.net_pnl) for t in legs), D(0))
        at = max(t.exit_time for t in legs)
        outcomes.append((at, identity, net, _decimal(risk, positive=True), legs[0].direction))
    if len(outcomes) == len(grouped) and outcomes:
        total = sum((row[2] for row in outcomes), D(0))
        result['net_pnl_per_entry_risk'] = str(total/sum((row[3] for row in outcomes), D(0)))
        result['without_best_lifecycle_net_pnl'] = str(total-max(row[2] for row in outcomes))
        result['worst_lifecycle_r'] = str(min(row[2]/row[3] for row in outcomes))
        streak = 0
        for at, _, net, risk, direction in sorted(outcomes):
            streak = streak+1 if net < 0 else 0
            result['longest_losing_streak'] = max(result['longest_losing_streak'], streak)
            month = at.astimezone(CST).strftime('%Y-%m')
            result['monthly_net_pnl'][month] = str(D(result['monthly_net_pnl'].get(month, '0'))+net)
            result['by_direction'].setdefault(direction, []).append(str(net/risk))
        result['lifecycle_r'] = [str(row[2]/row[3]) for row in sorted(outcomes)]
        if all(hasattr(trade, 'entry_time') for trade in trades):
            hours = [D(str((max(t.exit_time for t in legs)-min(t.entry_time for t in legs)).total_seconds()))/3600
                     for legs in grouped.values()]
            result['mean_holding_hours'] = str(sum(hours, D(0))/len(hours))
    if result['blocked_reasons']:
        result['status'] = 'BLOCKED_DATA'
    return result


def _risk_inventory(events):
    latest, orders, risks, modes = {}, {}, {}, {}
    for event in events:
        payload = event.get('payload', {})
        if event['type'] == 'hrs_budget' and payload.get('allowed') is True:
            latest[payload['symbol']] = payload
        elif event['type'] == 'fill' and payload.get('side', '').startswith('OPEN_'):
            identity = payload['order_id']
            if identity not in orders and payload['symbol'] in latest:
                orders[identity] = [latest[payload['symbol']], D(0)]
            if identity in orders:
                orders[identity][1] += D(payload['quantity'])
    for identity, (budget, filled) in orders.items():
        risks[identity] = _decimal(budget['worst_loss'], positive=True)*filled/_decimal(budget['quantity'], positive=True)
        modes[identity] = budget.get('entry_mode')
    return risks, modes


def select_mode_trades(trades, entry_modes, mode):
    family = {'standard', 'emm', 'semi_emm'} if mode == 'standard' else {'lv_rm'}
    return tuple(trade for trade in trades if entry_modes.get(trade.position_id) in family)


def _boundary_summary(window,start,end,start_resolution,end_resolution):
    nominal_start,nominal_end=_time(window['start']),_time(window['end'])
    full=start==nominal_start and end==nominal_end
    duration=(end-start).total_seconds()
    model=window.get('observation_model','full_calendar_month')
    eligible=full if model=='full_calendar_month' else duration>=window['minimum_duration_seconds']
    return dict(registered_start=nominal_start.isoformat(),registered_end=nominal_end.isoformat(),
        actual_start=start.isoformat(),actual_end=end.isoformat(),end=end.isoformat(),
        delay_seconds=(start-nominal_start).total_seconds(),end_delay_seconds=(end-nominal_end).total_seconds(),
        nominal_duration_seconds=(nominal_end-nominal_start).total_seconds(),actual_duration_seconds=duration,
        full_registered_span=full,qualification_model=model,monthly_window_eligible=eligible,
        start_resolution=start_resolution,end_resolution=end_resolution)


def _window_portal(directory,window):
    start,end=_time(window['start']),_time(window['end'])
    start_resolution=end_resolution=None
    if window.get('start_policy') is not None or window.get('end_policy') is not None:
        # Read the declared boundary only to instantiate the verifier. It gains
        # authority from manifest validation and independent raw re-resolution.
        manifest=json.loads((Path(directory)/'manifest.json').read_text())
        actual=_time(manifest['window_start']);actual_end=_time(manifest['as_of'])
        portal=DataPortal(directory,'hrs',actual_end,actual,required_datasets=REQUIRED_FILES)
        for name in ('hrs_export_receipt.json','raw_hrs_evidence.json'):
            portal._require_dataset(name)
        receipt=portal._read_json('hrs_export_receipt.json')
        raw=portal._read_json('raw_hrs_evidence.json')
        if window.get('start_policy') is not None:
            start_resolution=resolve_scan_start(raw['decision_evidence'],start,end,window['start_policy'])
            start=_time(start_resolution['actual_start'])
        if window.get('end_policy') is not None:
            end_resolution=resolve_scan_start(raw['decision_evidence'],end,end+timedelta(hours=1,microseconds=1),window['end_policy'])
            end=_time(end_resolution['actual_start'])
        if (receipt.get('boundary_resolution')!=start_resolution or receipt.get('end_boundary_resolution')!=end_resolution
                or start!=actual or end!=actual_end):
            raise DataBlocked(['scan_boundary_resolution_mismatch'])
    else:
        portal=DataPortal(directory,'hrs',end,start,required_datasets=REQUIRED_FILES)
    return portal,_boundary_summary(window,start,end,start_resolution,end_resolution)


def _run_candidate(registration, candidate, window, directory, on_engine_run=None, *, expected_engine_identity=None):
    expected_engine_identity = expected_engine_identity or engine_identity()
    _require_runtime_identity(expected_engine_identity, 'engine_identity_changed_before_run')
    tiers = {}; all_gaps = []
    for tier, multiplier in TIERS.items():
        portal,boundary = _window_portal(directory,window)
        start,end=portal.window_start,portal.as_of
        for name in sorted(REQUIRED_FILES):
            portal._require_dataset(name)
        raw = portal.hrs_replay_state(start)
        baseline = registration['baseline']
        if raw['config'] != baseline['config'] or raw['config_hashes'] != baseline['config_hashes']:
            raise DataBlocked(['registered_production_baseline_mismatch'])
        states = raw.get('state_tables')
        if not isinstance(states, dict):
            # Exported opening state is an independent immutable dataset.
            portal._require_dataset('hrs_state_tables.json')
            states = portal._read_json('hrs_state_tables.json')
        if states.get('hrs_positions', {}).get('rows') != []:
            raise DataBlocked(['experiment_flat_opening_inventory_required'])
        model = registration['cost_model']
        adapter = HrsAdapter(lambda at: states, exit_parameter_overrides={candidate['mode']: candidate['exit_parameters']},
            experiment_id=fingerprint({'registration': fingerprint(registration), 'candidate': candidate}),
            execution_costs=dict(fee_rate=_decimal(model['fee_bps'])*multiplier/10000,
                                 slippage=_decimal(model['slippage_bps'])*multiplier/10000))
        filters = {symbol: SymbolFilters(**{k: value for k, value in portal.symbol_filters(symbol).items()
                                            if k in ('tick_size', 'step_size', 'min_notional')})
                   for symbol in portal.universe().symbols}
        opening = [row for row in raw['ledger']['history'] if _time(row['as_of']) == start]
        if len(opening) != 1:
            raise DataBlocked(['experiment_opening_equity_checkpoint_missing_or_duplicate'])
        cash = _decimal(opening[0]['equity'], positive=True)
        broker = VirtualBroker(filters, _decimal(model['fee_bps'])*multiplier,
            _decimal(model['slippage_bps'])*multiplier, _decimal(model['stop_offset_pct']),
            _decimal(model['tp_offset_pct']), cash, _decimal(model['max_leverage'], positive=True), D('.3'))
        _require_runtime_identity(expected_engine_identity, 'engine_identity_changed_before_run')
        if on_engine_run is not None:
            on_engine_run()
        replay = run_replay(portal, adapter, broker, include_snapshots=True)
        _require_runtime_identity(expected_engine_identity, 'engine_identity_changed_during_run')
        checkpoints = adapter.equity_checkpoints() if hasattr(adapter, 'equity_checkpoints') else tuple(adapter._history)
        nav = nav_metrics(checkpoints, start, end)
        risks, entry_modes = _risk_inventory(replay.audit_events)
        lifecycles = lifecycle_metrics(select_mode_trades(replay.closed_trades, entry_modes, candidate['mode']), risks,
            minimum=registration['minimum_closed_lifecycles'])
        portfolio_lifecycles = lifecycle_metrics(replay.closed_trades, risks, minimum=1)
        gaps = list(nav['blocked_reasons'])+list(lifecycles['blocked_reasons'])
        if window['role']=='holdout' and not boundary['monthly_window_eligible']:
            gaps.append('monthly_observation_window_not_qualified')
        gaps.extend(portfolio_lifecycles['blocked_reasons'])
        if any(entry_modes.get(trade.position_id) not in {'standard', 'emm', 'semi_emm', 'lv_rm'} for trade in replay.closed_trades):
            gaps.append('entry_mode_attribution_missing')
        if any(p.is_open() for p in broker.positions.values()):
            gaps.append('experiment_open_end_inventory_requires_handover_or_longer_window')
        if replay.invariant_violations:
            gaps.extend('replay_invariant:'+value for value in replay.invariant_violations)
        all_gaps.extend(tier+':'+value for value in gaps)
        tiers[tier] = dict(source_manifest_sha256=replay.manifest_ref, nav=nav, lifecycles=lifecycles,
            portfolio_lifecycles=portfolio_lifecycles,
            score_events=sum(event['type'] == 'hrs_score' for event in replay.audit_events),
            audit_sha256=fingerprint(replay.audit_events),
            entry_budget_diagnostics=[event['payload'] for event in replay.audit_events if event['type'] == 'hrs_budget'],
            future_data_accesses=0, blocked_reasons=gaps)
    return dict(candidate_id=candidate['candidate_id'], mode=candidate['mode'], window_id=window['window_id'],
        is_baseline=not candidate['exit_parameters'], candidate_sha256=fingerprint(candidate), status='BLOCKED_DATA' if all_gaps else 'AVAILABLE',
        tiers=tiers, window_boundary=boundary, engine_identity=expected_engine_identity, blocked_reasons=all_gaps)


def _selection_candidates(registration, selection, research_report, *, current_engine_identity):
    if not selection or not research_report:
        raise DataBlocked(['frozen_research_selection_required'])
    if (selection.get('registration_sha256') != fingerprint(registration)
            or selection.get('research_report_sha256') != fingerprint(research_report)
            or research_report.get('registration_sha256') != fingerprint(registration)
            or research_report.get('stage') != 'research'
            or research_report.get('status') != 'AVAILABLE'):
        raise DataBlocked(['frozen_research_selection_identity_invalid'])
    if not research_report.get('engine_identity'):
        raise DataBlocked(['research_engine_identity_missing'])
    if research_report['engine_identity'] != current_engine_identity:
        raise DataBlocked(['research_engine_identity_mismatch'])
    selected = selection.get('candidate_by_mode', {})
    groups = {c['mode'] for c in registration['candidates']}
    if set(selected) != groups:
        raise DataBlocked(['frozen_research_selection_modes_incomplete'])
    candidates = []
    for mode in sorted(groups):
        matches = [c for c in registration['candidates'] if c['mode'] == mode and c['candidate_id'] == selected[mode]]
        if len(matches) != 1:
            raise DataBlocked(['frozen_research_selection_candidate_unknown'])
        candidates.extend(c for c in registration['candidates'] if c['mode'] == mode
                          and (not c['exit_parameters'] or c['candidate_id'] == selected[mode]))
    return candidates


def _shared_boundary_gaps(registration,boundaries,research_report):
    holdouts=sorted((w for w in registration['windows'] if w['role']=='holdout'),key=lambda w:_time(w['start']))
    if not any(w.get('observation_model')=='scan_aligned_month_v1' for w in holdouts):return []
    research=next(w for w in registration['windows'] if w['role']=='research')
    previous=(research_report or {}).get('window_boundaries',{}).get(research['window_id'])
    gaps=[]
    for window in holdouts:
        current=boundaries.get(window['window_id'])
        if not previous or not current:
            gaps.append('shared_scan_boundary_missing:'+window['window_id'])
        elif (previous['actual_end']!=current['actual_start'] or
              previous.get('end_resolution') is None or
              previous['end_resolution'].get('selected_evidence_id')!=current.get('start_resolution',{}).get('selected_evidence_id') or
              previous['end_resolution'].get('selected_payload_sha256')!=current.get('start_resolution',{}).get('selected_payload_sha256')):
            gaps.append('shared_scan_boundary_mismatch:'+window['window_id'])
        previous=current
    return gaps


def run_experiment(registration, *, snapshot_roots=(), output=None, stage='research', selection=None, research_report=None):
    report = dict(schema_version=1, status='BLOCKED_DATA', stage=stage, performance_accepted=False,
        registration_sha256=None, engine_identity=engine_identity(), selected_candidates={}, evaluations=[], engine_runs=0,
        blocked_reasons=[], snapshot_inventory=[], window_boundaries={},boundary_policy='flat_start_no_open_end',
        qualification='two complete independent holdout months required; no automatic production admission')
    report['discovery_roots'] = [dict(path=str(Path(root).expanduser().resolve()), exists=Path(root).expanduser().is_dir())
                                 for root in snapshot_roots]
    try:
        if report['engine_identity']['status'] != 'AVAILABLE':
            raise DataBlocked(report['engine_identity']['blocked_reasons'] or ['engine_identity_unavailable'])
        report['registration_sha256'] = validate_registration(registration)
        report['baseline_identity'] = {key: registration['baseline'][key] for key in
            ('effective_config_sha256', 'config_hashes', 'code_commit', 'source')}
        report['registered_candidates'] = [dict(candidate, candidate_sha256=fingerprint(candidate))
                                            for candidate in registration['candidates']]
        report['registered_windows'] = registration['windows']
        if any(w.get('observation_model')=='scan_aligned_month_v1' for w in registration['windows']):
            report['qualification']='two preregistered scan-aligned observation months, each at least 28 days, with shared captured boundaries; not complete natural months or automatic production admission'
        report['cost_model'] = registration['cost_model']
        if stage not in ('research', 'holdout'):
            raise ValueError('unknown_stage')
        candidates = (_selection_candidates(registration, selection, research_report, current_engine_identity=report['engine_identity']) if stage == 'holdout'
                      else registration['candidates'])
        report['snapshot_inventory'] = discover_snapshots(snapshot_roots)
        if not report['snapshot_inventory']:
            raise DataBlocked(['historical_hrs_snapshots_missing'])
        windows = [w for w in registration['windows'] if w['role'] == ('holdout' if stage == 'holdout' else 'research')]
        for window in windows:
            choices = [row for row in report['snapshot_inventory'] if row['snapshot_dir'] == str(Path(window.get('snapshot_dir', '')).resolve())]
            if len(choices) != 1:
                report['blocked_reasons'].append('registered_window_snapshot_missing:'+window['window_id'])
                continue
            for candidate in candidates:
                try:
                    def counted_run():
                        report['engine_runs'] += 1
                    evaluation = _run_candidate(registration, candidate, window, choices[0]['snapshot_dir'], counted_run, expected_engine_identity=report['engine_identity'])
                    report['evaluations'].append(evaluation)
                    previous=report['window_boundaries'].setdefault(window['window_id'],evaluation['window_boundary'])
                    if previous!=evaluation['window_boundary']:
                        raise DataBlocked(['candidate_window_boundary_changed'])
                    report['blocked_reasons'].extend(window['window_id']+':'+candidate['candidate_id']+':'+gap
                                                      for gap in evaluation['blocked_reasons'])
                except FutureDataAccess:
                    report['status'] = 'FAIL'
                    report['blocked_reasons'].append('future_data_access:'+window['window_id'])
                except DataBlocked as error:
                    report['blocked_reasons'].extend(window['window_id']+':'+str(gap) for gap in error.gaps)
        if stage=='holdout':
            report['blocked_reasons'].extend(_shared_boundary_gaps(registration,report['window_boundaries'],research_report))
        if not report['blocked_reasons'] and report['evaluations']:
            report['status'] = 'AVAILABLE'
        if stage == 'holdout' and report['status'] == 'AVAILABLE':
            report['frozen_selection'] = selection
            report['comparison'] = compare_holdouts(report['evaluations'], selection['candidate_by_mode'])
            report['holdout_comparison_passed'] = all(row['stable_improvement'] for row in report['comparison'])
            # The sealer labels source provenance unverified. Hash integrity and
            # simulation statistics alone cannot establish production parity or
            # source qualification; a separate admission review owns acceptance.
            report['formal_qualification_required'] = True
    except DataBlocked as error:
        report['blocked_reasons'].extend(error.gaps)
    except (KeyError, ValueError, TypeError, ArithmeticError) as error:
        report['blocked_reasons'].append('registration_or_snapshot_invalid:'+str(error))
    if output is not None:
        write_report(output, report)
    return report


def compare_holdouts(evaluations, selected):
    """Compare preselected candidates only; never select using holdout scores."""
    comparisons = []
    for mode, identity in sorted(selected.items()):
        rows = [row for row in evaluations if row['mode'] == mode]
        chosen = [row for row in rows if row['candidate_id'] == identity]
        references = [row for row in rows if row['is_baseline']]
        results = []
        for candidate in chosen:
            reference = next((row for row in references if row['window_id'] == candidate['window_id']), None)
            if reference is None:
                results.append(dict(window_id=candidate['window_id'], stable_improvement=False, reason='baseline_selected_or_missing'))
                continue
            a, b = candidate['tiers']['baseline'], reference['tiers']['baseline']
            sample_a, sample_b = a['lifecycles'].get('lifecycle_r', []), b['lifecycles'].get('lifecycle_r', [])
            ci = _difference_ci(sample_a, sample_b, fingerprint([identity, candidate['window_id']]))
            passed = (candidate.get('window_boundary',{}).get('monthly_window_eligible',True)
                and reference.get('window_boundary',{}).get('monthly_window_eligible',True)
                and D(a['lifecycles']['net_pnl_per_entry_risk']) >= D(b['lifecycles']['net_pnl_per_entry_risk'])
                and D(a['nav']['max_drawdown']) <= D(b['nav']['max_drawdown'])
                and D(a['lifecycles']['worst_lifecycle_r']) >= D(b['lifecycles']['worst_lifecycle_r'])
                and D(a['lifecycles']['without_best_lifecycle_net_pnl']) >= D(b['lifecycles']['without_best_lifecycle_net_pnl'])
                and ci is not None and D(ci[0]) > 0
                and all(D(candidate['tiers'][tier]['nav']['nav_return']) >= D(reference['tiers'][tier]['nav']['nav_return'])
                    and D(candidate['tiers'][tier]['nav']['max_drawdown']) <= D(reference['tiers'][tier]['nav']['max_drawdown'])
                    and D(candidate['tiers'][tier]['lifecycles']['worst_lifecycle_r']) >= D(reference['tiers'][tier]['lifecycles']['worst_lifecycle_r'])
                    for tier in TIERS))
            results.append(dict(window_id=candidate['window_id'], stable_improvement=passed,
                window_boundary=candidate.get('window_boundary'),
                mean_lifecycle_r_difference_ci95=ci, trade_retention=str(D(len(sample_a))/D(len(sample_b))) if sample_b else None))
        comparisons.append(dict(mode=mode, candidate_id=identity, windows=results,
                                stable_improvement=len(results) == 2 and all(row['stable_improvement'] for row in results)))
    return comparisons


def _difference_ci(left, right, seed):
    if not left or not right:
        return None
    rng = random.Random(seed); a = list(map(D, left)); b = list(map(D, right)); differences = []
    for _ in range(1000):
        differences.append(sum((rng.choice(a) for _ in a), D(0))/len(a)-sum((rng.choice(b) for _ in b), D(0))/len(b))
    differences.sort()
    return [str(differences[24]), str(differences[974])]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registration', type=Path)
    parser.add_argument('--snapshot-root', action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stage', choices=('research', 'holdout'), default='research')
    parser.add_argument('--selection', type=Path)
    parser.add_argument('--research-report', type=Path)
    args = parser.parse_args(argv)
    if args.registration is None:
        report = dict(schema_version=1, status='BLOCKED_DATA', stage=args.stage, performance_accepted=False,
            registration_sha256=None, engine_identity=engine_identity(), selected_candidates={}, evaluations=[], engine_runs=0,
            blocked_reasons=['frozen_production_baseline_and_candidate_registration_missing'],
            snapshot_inventory=discover_snapshots(args.snapshot_root))
        report['discovery_roots'] = [dict(path=str(Path(root).expanduser().resolve()), exists=Path(root).expanduser().is_dir())
                                     for root in args.snapshot_root]
        if not report['snapshot_inventory']:
            report['blocked_reasons'].append('historical_hrs_snapshots_missing')
        write_report(args.output, report)
    else:
        try:
            registration = json.loads(args.registration.read_text())
            selection = json.loads(args.selection.read_text()) if args.selection else None
            research = json.loads(args.research_report.read_text()) if args.research_report else None
        except (OSError, ValueError) as error:
            report = run_experiment({}, snapshot_roots=args.snapshot_root, stage=args.stage)
            report['blocked_reasons'] = ['experiment_input_file_unreadable:'+type(error).__name__]
            write_report(args.output, report)
        else:
            report = run_experiment(registration, snapshot_roots=args.snapshot_root,
                output=args.output, stage=args.stage, selection=selection, research_report=research)
    print(canonical_json({'status': report['status'], 'output': str(args.output.resolve()),
                          'blocked_reasons': report['blocked_reasons']}), end='')
    return 2 if report['status'] in ('BLOCKED_DATA', 'FAIL') else 0


if __name__ == '__main__':
    raise SystemExit(main())
