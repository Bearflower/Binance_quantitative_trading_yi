"""Independent local enforcement for an externally pinned HRS registration.

``python -m ai_tuner.hrs_exit_partition_guard --help`` checks by default;
``--execute-research`` additionally calls the immutable experiment evaluator.
No exchange, database, deployment, publication or trading access is performed.
CHECKED_LOCAL/AVAILABLE describe local integrity/simulation only. Source
authenticity, deployment continuity and production admission remain unverified.

Holdout requires a pre-pinned Ed25519 local authority and authenticated research
selection before any holdout file access. A caller timestamp or self-hashed
report cannot supply that authority. No boundary is moved.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re

from ai_tuner import hrs_exit_experiment as experiment
from ai_tuner.hrs_evidence_export import _decoded, close_portal_indexes, snapshot_evidence_sources
from ai_tuner.replay.artifacts import write_report
from ai_tuner.replay.data_portal import DataPortal, sha256_file
from ai_tuner.replay.models import DataBlocked, FutureDataAccess
from shared.config_loader import compute_config_hash

MODEL = 'hrs_exit_partition_guard_v1'
SOURCE_COUNT = 60
_HASH = re.compile(r'[0-9a-f]{64}')


def _require(condition, reason):
    if not condition:
        raise DataBlocked([reason])


def _observed_now():
    # Intentionally no public clock argument and no CLI --now override.
    return datetime.now(timezone.utc)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _safe_source(root, name):
    relative = PurePosixPath(name)
    _require(isinstance(name, str) and not relative.is_absolute() and str(relative)==name
        and '..' not in relative.parts and '.' not in relative.parts and '\\' not in name,
        'partition_source_path_invalid:'+str(name))
    path = root/name
    _require(not any(p.is_symlink() for p in (path, *path.parents)), 'partition_source_symlink:'+name)
    _require(path.is_file(), 'partition_source_missing:'+name)
    return path


def _partition(registration, root):
    partition = registration.get('prospective_runtime_partition')
    _require(isinstance(partition, dict), 'runtime_partition_missing')
    _require(re.fullmatch(r'[0-9a-f]{40}', partition.get('expected_release_sha', '')) is not None,
        'partition_release_required')
    files = partition.get('source_files')
    _require(isinstance(files, dict) and len(files)==SOURCE_COUNT, 'partition_source_inventory_count_invalid')
    _require(experiment.fingerprint(files)==partition.get('source_files_sha256'), 'partition_source_inventory_digest_mismatch')
    for name, expected in files.items():
        _require(isinstance(expected, str) and _HASH.fullmatch(expected), 'partition_source_hash_invalid:'+name)
        path = _safe_source(root, name)
        _require(sha256_file(str(path))==expected, 'partition_source_hash_mismatch:'+name)
    engine = experiment.engine_identity()
    _require(engine['status']=='AVAILABLE', 'engine_identity_unavailable')
    for name, actual in engine['source_files'].items():
        _require(files.get(name)==actual, 'import_source_hash_mismatch:'+name)
        module = importlib.import_module(name[:-3].replace('/', '.'))
        source = inspect.getsourcefile(module)
        _require(source is not None and Path(source).resolve()==(root/name).resolve(),
            'import_source_path_mismatch:'+name)
    registry = 'services/market_cap/assets.json'
    _require(files.get(registry)==partition.get('market_cap_assets_sha256'), 'partition_registry_hash_mismatch')
    assets = _read_json(_safe_source(root, registry))
    _require(isinstance(assets, dict) and type(partition.get('expected_market_cap_asset_count')) is int
        and len(assets)==partition['expected_market_cap_asset_count'], 'partition_registry_count_mismatch')
    _require(isinstance(partition.get('expected_runtime_loaded_config_hash'), str)
        and _HASH.fullmatch(partition['expected_runtime_loaded_config_hash']), 'partition_loaded_config_hash_required')
    return partition, engine


def _stat_identity(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _clock_check(row, observed, *, family=None):
    # Visit one bounded receipt at a time; future expiries/forecasts are allowed,
    # but original observations and event/availability clocks cannot be future.
    # The archive preserves original envelopes, including JSON-encoded bodies.
    # Decode only the source-schema body fields, after archive hash verification;
    # ordinary free text is never interpreted as an additional source object.
    row = dict(row)
    for key in ('payload', 'receipt'):
        if isinstance(row.get(key), str):
            row[key] = _decoded(row[key])
    stack = [(row, ())]
    fields = {'event_time', 'available_at', 'received_at', 'observed_at', 'source_time', 'as_of',
        'close_time', 'requested_at', 'captured_at'}
    if family=='equity_checkpoints':
        # This original SQL column is the archive schema's availability fallback.
        fields.add('created_at')
    while stack:
        value, path = stack.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key in fields and item is not None:
                    if type(item) in (int, float):
                        # Known retained source schemas: market-cap observations
                        # use epoch seconds; Binance kline close_time uses ms.
                        # Never infer units from magnitude or reinterpret an
                        # out-of-range/future seconds value as milliseconds.
                        units = {'as_of':1, 'observed_at':1, 'source_time':1, 'close_time':1000}
                        quote_context = ((row.get('kind')=='market_cap_batch'
                            and len(path)==3 and path[:2]==('payload','quotes'))
                            or (row.get('kind')=='market_input' and path==('payload','result')
                                and row.get('payload',{}).get('operation')=='get_market_cap_provenance'))
                        if quote_context:
                            units['available_at'] = 1
                        _require(key in units, 'source_numeric_clock_unit_unknown:'+key)
                        at = datetime.fromtimestamp(item/units[key], timezone.utc)
                    else:
                        at = experiment._time(item)
                    _require(at<=observed, 'source_available_after_observation:'+key)
                elif isinstance(item, (dict, list)):
                    stack.append((item, path+(key,)))
                elif key in ('payload', 'receipt') and isinstance(item, str):
                    stack.append((_decoded(item), path+(key,)))
        elif isinstance(value, list):
            stack.extend((item, path+(index,)) for index, item in enumerate(value))


def _window_source(window, partition, observed, expected_manifest_sha256):
    _require(all(isinstance(window.get(field), dict)
        and window[field].get('version')=='first_scan_start_v1'
        for field in ('start_policy','end_policy')), 'partition_fixed_scan_policies_required')
    directory = Path(window.get('snapshot_dir', ''))
    _require(isinstance(expected_manifest_sha256, str) and _HASH.fullmatch(expected_manifest_sha256),
        'snapshot_external_pin_required')
    encoded = (directory/'manifest.json').read_bytes()
    _require(hashlib.sha256(encoded).hexdigest()==expected_manifest_sha256, 'snapshot_external_pin_mismatch')
    # Parse the SAME externally pinned bytes. The portal subsequently receives
    # this pinned envelope, never an envelope read from a replacement manifest.
    manifest = json.loads(encoded)
    _require(manifest.get('manifest_version')==2, 'partition_manifest_v2_required')
    stamps = {str(_safe_source(directory, name)): _stat_identity(directory/name) for name in manifest['files']}
    stamps[str(directory/'manifest.json')] = _stat_identity(directory/'manifest.json')
    # The existing portal verifies every manifest file, plus envelope/identity;
    # _window_portal independently resolves BOTH boundaries from original scans.
    portal = DataPortal(directory, 'hrs', experiment._time(manifest['as_of']),
        experiment._time(manifest['window_start']), required_datasets=experiment.REQUIRED_FILES,
        expected_envelope_sha256=manifest.get('envelope_sha256'))
    try:
        _require(portal.as_of<=observed, 'snapshot_after_observation')
        sources = snapshot_evidence_sources(portal)
        archive = getattr(sources, 'archive', None)
        _require(archive is not None, 'independent_original_source_counts_required')
        # EvidenceArchive.open, invoked above, checks the FULL inventory,
        # independent original counts, receipt keys, payload hashes and CAS
        # closure, before any time/release/quality subset is selected.
        for family in archive.counts:
            for row in archive.iter_records(family):
                _clock_check(row, observed, family=family)
        checked, boundary = experiment._window_portal(directory, window,
            source_cache={str(directory.resolve()): sources})
        try:
            start = experiment._time(boundary['actual_start'])
            end = experiment._time(boundary['actual_end'])
            _require(end<=observed, 'resolved_window_not_elapsed')
            records = sources.get('decision_evidence')
            count = 0
            for run, rows in records.runs():
                starts = list(rows.select(kind='scan_start'))
                endings = list(rows.select(kind='scan_end'))
                if not starts:
                    continue
                # Match _cycles' actual consumer predicate exactly, including
                # a pre-start run with delayed ordinary activity. Late read-only
                # protection observations cannot reopen an ended old cycle.
                activity = (r for r in rows if r['kind']!='protection_order_observation')
                ended_before = (all(r['available_at']<=start for r in (*starts,*endings))
                    and max(r['available_at'] for r in activity)<=start)
                consumed = not ended_before and min(r['event_time'] for r in starts)<end
                boundary_scan = any(start<=r['event_time']<=end for r in starts)
                if not consumed and not boundary_scan:
                    continue
                _require(len(starts)==1, 'partition_scan_start_missing_or_ambiguous:'+run)
                row = starts[0]
                at = experiment._time(row['event_time'])
                count += 1
                if at in (start, end):
                    financial = row['payload'].get('financial_context', {})
                    _require(row['payload'].get('positions')=={}
                        and financial.get('state_tables', {}).get('hrs_positions', {}).get('rows')==[],
                        'partition_boundary_nonflat_or_unproven:'+row['evidence_id'])
                _require(len(endings)==1 and endings[0]['payload'].get('status')=='complete',
                    'partition_scan_end_missing_or_incomplete:'+row['evidence_id'])
                for evidence, state in ((row, row['payload']), (endings[0], endings[0]['payload'].get('state'))):
                    _require(isinstance(state, dict) and state.get('release_sha')==partition['expected_release_sha'],
                        'scan_release_mismatch:'+evidence['evidence_id'])
                    _require(compute_config_hash(state.get('config', {}))==partition['expected_runtime_loaded_config_hash'],
                        'scan_loaded_config_mismatch:'+evidence['evidence_id'])
            _require(count>0, 'partition_scan_inventory_empty')
            facts = dict(snapshot_dir=str(directory.resolve()), manifest_sha256=portal.manifest_sha256,
                manifest_file_sha256=expected_manifest_sha256,
                envelope_sha256=manifest['envelope_sha256'], archive=archive.descriptor(relative_to=directory),
                original_source_counts=archive.counts, original_source_metadata_sha256=experiment.fingerprint(archive.source_metadata),
                scan_count=count, boundary=boundary, source_attested=False)
            # Full verification at candidate boundaries; cheap file identity
            # checks immediately before EVERY engine call prevent stale caches
            # from accepting replacement files without rehashing 32 GiB x 72.
            for name, stamp in stamps.items():
                _require(_stat_identity(Path(name))==stamp, 'snapshot_changed_during_check:'+name)
            return facts, stamps
        finally:
            # checked uses the explicitly shared archive, owned by portal.
            close_portal_indexes(checked)
    finally:
        close_portal_indexes(portal)


def _research_gaps(registration, selection, report, engine):
    """Reject fabricated/incomplete research without reading any holdout data."""
    try:
        experiment._selection_candidates(registration, selection, report, current_engine_identity=engine)
        expected = {(c['candidate_id'], c['mode'], w['window_id']): c
            for c in registration['candidates'] for w in registration['windows'] if w['role']=='research'}
        evaluations = report.get('evaluations', [])
        _require(len(evaluations)==len(expected), 'research_evaluations_incomplete')
        seen = set()
        for row in evaluations:
            key = (row['candidate_id'], row['mode'], row['window_id'])
            _require(key in expected and key not in seen, 'research_evaluation_identity_invalid')
            seen.add(key)
            _require(row.get('status')=='AVAILABLE' and not row.get('blocked_reasons')
                and row.get('candidate_sha256')==experiment.fingerprint(expected[key])
                and row.get('engine_identity')==engine
                and row.get('is_baseline')==(not expected[key]['exit_parameters']), 'research_evaluation_unqualified')
            _require(set(row.get('tiers', {}))==set(experiment.TIERS), 'research_cost_tiers_incomplete')
            for tier in row['tiers'].values():
                lifecycles = tier.get('lifecycles', {})
                _require(not tier.get('blocked_reasons') and tier.get('future_data_accesses')==0
                    and len(lifecycles.get('lifecycle_r', []))>=registration['minimum_closed_lifecycles']
                    and not lifecycles.get('blocked_reasons') and bool(tier.get('audit_sha256'))
                    and bool(tier.get('source_manifest_sha256')), 'research_tier_unqualified')
        _require(report.get('engine_runs')==len(expected)*len(experiment.TIERS), 'research_engine_runs_incomplete')
        _require(report.get('partition_guard', {}).get('window_sources'), 'research_guard_source_identities_missing')
        return []
    except DataBlocked as error:
        return list(error.gaps)
    except (KeyError, ValueError, TypeError):
        return ['research_evaluations_incomplete']


def _run_guard(registration_path, *, expected_registration_sha256, source_root=None,
              execute=False, stage='research', selection=None, research_report=None,
              deployment_facts=None, expected_deployment_facts_sha256=None,
              expected_snapshot_manifest_sha256=None, output=None,
              expected_snapshot_manifest_sha256_by_window=None, _verified=None, _authority_gaps=None):
    """Check or execute research. External pins are supplied out of band.

    Optional deployment facts are retained with an external RAW-file hash;
    binding that observation never proves historical continuity or authenticity.
    A caller cannot enable holdout with a timestamp or a purported freeze file.
    """
    root = Path(source_root or Path(__file__).resolve().parents[1]).absolute()
    observed = _observed_now()
    report = dict(schema_version=1, model=MODEL, status='BLOCKED_DATA', stage=stage,
        engine_runs=0, evaluations=[], selected_candidates={}, blocked_reasons=[],
        observed_at=observed.isoformat(), registration_sha256=None, window_sources={},
        source_attested=False, performance_accepted=False, official_run_ready=False,
        deployment_facts_status='UNVERIFIED', guard_sha256=sha256_file(__file__),
        qualification='local integrity and original experiment criteria; no source or production approval')
    registration = None
    try:
        if _authority_gaps:
            raise DataBlocked(_authority_gaps)
        _require(isinstance(expected_registration_sha256, str) and _HASH.fullmatch(expected_registration_sha256),
            'external_registration_pin_required')
        registration = _read_json(registration_path)
        _require(experiment.fingerprint(registration)==expected_registration_sha256, 'registration_external_pin_mismatch')
        report['registration_sha256'] = experiment.validate_registration(registration)
        partition, engine = _partition(registration, root)
        report.update(partition_sha256=experiment.fingerprint(partition), partition=partition, engine_identity=engine)
        if deployment_facts is not None:
            _require(isinstance(expected_deployment_facts_sha256, str)
                and sha256_file(str(deployment_facts))==expected_deployment_facts_sha256, 'deployment_facts_external_pin_mismatch')
            receipt = _read_json(deployment_facts)
            _require(receipt.get('registration_sha256')==expected_registration_sha256
                and receipt.get('release_sha')==partition['expected_release_sha']
                and receipt.get('source_files')==partition['source_files']
                and receipt.get('source_files_sha256')==partition['source_files_sha256']
                and experiment._time(receipt['observed_at'])<=observed, 'deployment_facts_partition_mismatch')
            report['deployment_facts'] = dict(path=str(Path(deployment_facts).resolve()), sha256=expected_deployment_facts_sha256,
                observed_at=receipt['observed_at'], proves_historical_continuity=False)
            report['deployment_facts_status'] = 'BOUND_OBSERVATION_UNVERIFIED_CONTINUITY'
        _require(stage in ('research','holdout'), 'unknown_stage')
        if stage=='holdout':
            if _verified is None:
                report['blocked_reasons'].extend(_research_gaps(registration, selection, research_report, engine))
                raise DataBlocked(_authority_gaps or ['authenticated_selection_authority_required'])
            selection, research_report = _verified['selection'], _verified['research_report']
            candidates = experiment._selection_candidates(registration, selection, research_report, current_engine_identity=engine)
            report['selected_candidates'] = selection['candidate_by_mode']
            report['freeze_raw_sha256'] = _verified['freeze_raw_sha256']
        else:
            candidates = registration['candidates']
        windows = [w for w in registration['windows'] if w['role']==stage]
        _require(all(experiment._time(w['end'])<=observed for w in windows), stage+'_window_not_elapsed')
        def manifest_pin(window):
            return ((expected_snapshot_manifest_sha256_by_window or {}).get(window['window_id'])
                if stage=='holdout' else expected_snapshot_manifest_sha256)
        stamps = {}
        for window in windows:
            facts, current_stamps = _window_source(window, partition, observed, manifest_pin(window))
            report['window_sources'][window['window_id']] = facts
            stamps.update(current_stamps)

        def unchanged():
            _require(_read_json(registration_path)==registration, 'registration_changed_after_check')
            current_partition, current_engine = _partition(registration, root)
            _require(current_partition==partition and current_engine==engine, 'engine_changed_after_check')
            _require(sha256_file(__file__)==report['guard_sha256'], 'guard_changed_after_check')
            for name, stamp in stamps.items():
                _require(_stat_identity(Path(name))==stamp, 'snapshot_changed_after_check:'+name)
            for window in windows:
                _require(sha256_file(str(Path(window['snapshot_dir'])/'manifest.json'))==manifest_pin(window),
                    'snapshot_external_pin_mismatch')
            if deployment_facts is not None:
                _require(sha256_file(str(deployment_facts))==expected_deployment_facts_sha256, 'deployment_facts_changed_after_check')

        unchanged()
        if not execute:
            report['status'] = 'CHECKED_LOCAL'
        else:
            # The original evaluator owns ALL cost tiers, minimum sample counts,
            # risk admission, NAV/lifecycle criteria, open-end checks and replay.
            # This entry only adds partition/clock/source barriers around it.
            report.update(registered_candidates=[dict(c, candidate_sha256=experiment.fingerprint(c)) for c in registration['candidates']],
                registered_windows=registration['windows'], cost_model=registration['cost_model'],
                baseline_identity={key:registration['baseline'][key] for key in ('effective_config_sha256','config_hashes','code_commit','source')},
                snapshot_inventory=[dict(snapshot_dir=w['snapshot_dir']) for w in windows], window_boundaries={},
                boundary_policy='flat_start_no_open_end')
            for key in ('runtime_admission_scope','runtime_risk_budget_scope','runtime_margin_scope'):
                report[key]=registration.get(key)
            for window in windows:
                for candidate in candidates:
                    unchanged()
                    def counted_run():
                        unchanged()
                        report['engine_runs'] += 1
                    # The original evaluator exposes shared archive ownership;
                    # close every retained archive even if replay fails.
                    source_cache = {}
                    try:
                        evaluation = experiment._run_candidate(registration, candidate, window, window['snapshot_dir'],
                            counted_run, expected_engine_identity=engine, source_cache=source_cache)
                    finally:
                        for sources in source_cache.values():
                            sources.__exit__(None, None, None)
                    unchanged()
                    facts, _ = _window_source(window, partition, _observed_now(), manifest_pin(window))
                    _require(facts==report['window_sources'][window['window_id']], 'source_identity_changed_during_run')
                    report['evaluations'].append(evaluation)
                    previous=report['window_boundaries'].setdefault(window['window_id'],evaluation['window_boundary'])
                    _require(previous==evaluation['window_boundary'], 'candidate_window_boundary_changed')
                    report['blocked_reasons'].extend(window['window_id']+':'+candidate['candidate_id']+':'+gap for gap in evaluation['blocked_reasons'])
            if stage=='holdout':
                report['blocked_reasons'].extend(experiment._shared_boundary_gaps(registration,report['window_boundaries'],research_report))
            if report['evaluations'] and not report['blocked_reasons']:
                report['status'] = 'AVAILABLE'
                if stage=='holdout':
                    report['frozen_selection']=selection
                    report['comparison']=experiment.compare_holdouts(report['evaluations'],selection['candidate_by_mode'])
                    report['holdout_comparison_passed']=all(row['stable_improvement'] for row in report['comparison'])
                    report['formal_qualification_required']=True
        unchanged()
    except DataBlocked as error:
        report['blocked_reasons'].extend(error.gaps)
        report['status'] = 'BLOCKED_DATA'
    except FutureDataAccess:
        report['status'] = 'FAIL'
        report['blocked_reasons'].append('future_data_access')
    except (OSError, KeyError, ValueError, TypeError, ArithmeticError) as error:
        report['status'] = 'BLOCKED_DATA'
        report['blocked_reasons'].append('partition_or_source_invalid:'+str(error))
    # A digest sidecar is a receipt, never an independent trust authority.
    report['partition_guard'] = dict(model=MODEL, guard_sha256=report['guard_sha256'],
        registration_sha256=report['registration_sha256'], partition_sha256=report.get('partition_sha256'),
        engine_sha256=report.get('engine_identity',{}).get('sha256'), window_sources=report['window_sources'],
        source_attested=False, official_run_ready=False, deployment_facts=report.get('deployment_facts'))
    report['partition_guard']['sha256'] = experiment.fingerprint(report['partition_guard'])
    if output is not None:
        write_report(output, report)
        write_report(str(output)+'.partition.json', report['partition_guard'])
    return report



def run_guard(registration_path, *, expected_registration_sha256, source_root=None,
              execute=False, stage='research', selection=None, research_report=None,
              deployment_facts=None, expected_deployment_facts_sha256=None,
              expected_snapshot_manifest_sha256=None, output=None,
              authority_dir=None, expected_anchor_sha256=None,
              expected_snapshot_manifest_sha256_by_window=None):
    """Public entry: authenticate under the shared lock before holdout access."""
    options=dict(expected_registration_sha256=expected_registration_sha256,source_root=source_root,
        execute=execute,stage=stage,selection=selection,research_report=research_report,
        deployment_facts=deployment_facts,expected_deployment_facts_sha256=expected_deployment_facts_sha256,
        expected_snapshot_manifest_sha256=expected_snapshot_manifest_sha256,output=output,
        expected_snapshot_manifest_sha256_by_window=expected_snapshot_manifest_sha256_by_window)
    def output_collision(*, resolve_targets):
        if output is None or authority_dir is None:
            return False
        # Check both reports and the temporary paths used by write_report.
        # Lexical checks never stat an unauthenticated holdout output path.
        targets=[Path(output),Path(str(output)+'.partition.json')]
        targets += [target.with_suffix(target.suffix+'.tmp') for target in targets]
        states={Path(os.path.abspath(authority_dir)),Path(authority_dir).resolve()}
        for target in targets:
            paths={Path(os.path.abspath(target))}
            if resolve_targets:
                paths.add(target.resolve())
            if any(path==state or state in path.parents for path in paths for state in states):
                return True
        return False

    def collision_report():
        options['output']=None
        return _run_guard(registration_path,**options,_authority_gaps=['authority_output_collision'])

    if output_collision(resolve_targets=False):
        return collision_report()
    if stage!='holdout':
        if output_collision(resolve_targets=True):
            return collision_report()
        return _run_guard(registration_path,**options)
    if authority_dir is None:
        options['output']=None
        return _run_guard(registration_path,**options,_authority_gaps=['authenticated_selection_authority_required'])
    from ai_tuner.hrs_exit_selection_freeze import authenticated_holdout
    try:
        with authenticated_holdout(registration_path,authority_dir,expected_anchor_sha256=expected_anchor_sha256,
                expected_registration_sha256=expected_registration_sha256,
                expected_holdout_manifest_sha256=expected_snapshot_manifest_sha256_by_window) as verified:
            if output_collision(resolve_targets=True):
                return collision_report()
            return _run_guard(registration_path,**options,_verified=verified)
    except DataBlocked as error:
        # An unauthenticated caller cannot use output aliases to overwrite the
        # authority. Return the blocked report without touching output paths.
        options['output']=None
        return _run_guard(registration_path,**options,_authority_gaps=list(error.gaps))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registration', required=True)
    parser.add_argument('--expected-registration-sha256', required=True, help='independently pinned canonical registration hash')
    parser.add_argument('--expected-snapshot-manifest-sha256', help='independently pinned raw research manifest.json hash; required once the window has elapsed')
    parser.add_argument('--source-root')
    parser.add_argument('--execute-research', action='store_true')
    parser.add_argument('--stage', choices=('research','holdout'), default='research')
    parser.add_argument('--authority-dir')
    parser.add_argument('--expected-anchor-sha256')
    parser.add_argument('--expected-snapshot-manifest-sha256-by-window', type=json.loads, help='explicit JSON window-ID to external raw manifest SHA mapping')
    parser.add_argument('--selection')
    parser.add_argument('--research-report')
    parser.add_argument('--deployment-facts')
    parser.add_argument('--expected-deployment-facts-sha256')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    result = run_guard(args.registration, expected_registration_sha256=args.expected_registration_sha256,
        source_root=args.source_root, execute=args.execute_research, stage=args.stage,
        selection=_read_json(args.selection) if args.selection and args.stage!='holdout' else None,
        research_report=_read_json(args.research_report) if args.research_report and args.stage!='holdout' else None,
        deployment_facts=args.deployment_facts, expected_deployment_facts_sha256=args.expected_deployment_facts_sha256,
        expected_snapshot_manifest_sha256=args.expected_snapshot_manifest_sha256,
        output=args.output,authority_dir=args.authority_dir,expected_anchor_sha256=args.expected_anchor_sha256,
        expected_snapshot_manifest_sha256_by_window=args.expected_snapshot_manifest_sha256_by_window)
    print(json.dumps(dict(status=result['status'], engine_runs=result['engine_runs'], blocked_reasons=result['blocked_reasons'])))
    return 0 if result['status'] in ('CHECKED_LOCAL','AVAILABLE') else 2


if __name__=='__main__':
    raise SystemExit(main())
