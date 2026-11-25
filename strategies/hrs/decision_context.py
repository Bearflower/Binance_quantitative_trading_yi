"""Raw capital/account/state evidence for prospective HRS replay windows."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import os
import json
from time import monotonic
import traceback

TABLES = ('hrs_positions', 'hrs_blacklist', 'hrs_active_symbols')
# JSON is rendered by PostgreSQL before asyncpg converts a naive timestamp or
# infinity into Python datetime. Preserve the original database text; a snapshot
# of an ACTIVE claim does not infer its creation/expiry timezone.
CLAIM_EVIDENCE_SELECT = 'SELECT to_jsonb(c) AS evidence_row FROM trading.position_claims c'


def decode_claim_evidence(rows):
    result = []
    for row in rows:
        value = row['evidence_row']
        value = json.loads(value) if isinstance(value, str) else value
        if not isinstance(value, dict):
            raise ValueError('claim_evidence_row_invalid')
        result.append(value)
    return result


async def _database_context(db, account):
    queries = [('''SELECT p.* FROM trading.strategy_risk_policy p
        JOIN trading.strategy_risk_active a USING(account_id,strategy_id,revision)
        WHERE p.account_id=$1 AND p.strategy_id='hrs' ''', (account,)),
        ("SELECT * FROM trading.hrs_loss_gate_state WHERE account_id=$1", (account,))]
    queries += [(f'SELECT * FROM hrs.{table}', ()) for table in TABLES]
    queries += [
        ("SELECT * FROM trading.entry_risk_reservation WHERE account_id=$1", (account,)),
        (CLAIM_EVIDENCE_SELECT + " WHERE c.claim_state IN ('PENDING','ACTIVE')", ()),
        ('''SELECT account_id,evidence_sha256,segment_id,as_of,available_at,payload
            FROM trading.hrs_equity_evidence WHERE account_id=$1 AND segment_id IS NOT NULL
            ORDER BY as_of DESC,available_at DESC LIMIT 2''', (account,)),
        ("""WITH observation AS (SELECT clock_timestamp() AS checked_at)
            SELECT g.*,o.checked_at,COALESCE((SELECT jsonb_agg(jsonb_build_object(
                'id',c.id,'symbol',c.symbol,'strategy',c.strategy,'claim_state',c.claim_state))
                FROM trading.position_claims c WHERE c.claim_state IN ('PENDING','ACTIVE')
                AND c.expires_at>o.checked_at),'[]'::jsonb) AS ownership_claims
            FROM trading.strategy_equity_gate g CROSS JOIN observation o
            WHERE g.account_id=$1 AND g.strategy_id='hrs'""", (account,))]

    pool = getattr(db, 'pool', None)
    if pool is None:
        # An explicit incomplete flag prevents test/legacy DB adapters from
        # masquerading as an atomic database snapshot.
        return [await db.fetch_all(sql, *args) for sql, args in queries], False
    async with pool.acquire() as connection, connection.transaction(isolation='repeatable_read', readonly=True):
        return [await connection.fetch(sql, *args) for sql, args in queries], True


def ledger_equity_observation(ledger, archives):
    import json
    from .equity_evidence import canonical
    if not ledger:
        return None
    try:
        state = json.loads(ledger['ledger_state']) if isinstance(ledger['ledger_state'], str) else ledger['ledger_state']
        value = Decimal(str(state['equity']))
        if not value.is_finite() or value <= 0:
            return None
        for row in archives:
            payload = json.loads(row['payload']) if isinstance(row['payload'], str) else row['payload']
            _, digest = canonical(payload)
            if (digest == row['evidence_sha256'] and row['segment_id'] == ledger['segment_id']
                    and payload.get('result', {}).get('ledger_version') == ledger['version']
                    and payload.get('as_of') == state['as_of']):
                return dict(value=str(value), as_of=state['as_of'], available_at=row['available_at'],
                            evidence_sha256=digest, ledger_version=ledger['version'])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None
    return None


async def capture_financial_context(strategy):
    started = datetime.now(timezone.utc)
    account, allocation, pending, inventory, independent = await asyncio.gather(
        strategy.binance_client.get_account_info(),
        strategy.capital_mgr.get_effective_margin_limit(),
        strategy.trading_executor.timeout_recovery.pending(),
        strategy.binance_client.get_position(), strategy.binance_client.get_um_account_positions())
    equity = account.get('totalMarginBalance') if isinstance(account, dict) else None
    if equity is None or not Decimal(str(equity)).is_finite() or Decimal(str(equity)) <= 0:
        raise ValueError('account_equity_missing_or_invalid')
    amount, source = allocation
    from .entry_occupancy import SLOT_SCOPE, capture_hrs_slot_inputs
    local = strategy.position_manager.get_all_positions()
    if not isinstance(local, dict):
        raise ValueError('local_position_inventory_missing')
    observed = datetime.now(timezone.utc)
    slot_inputs = capture_hrs_slot_inputs(inventory=inventory, independent=independent,
        local=local, observed_at=observed)
    # Gate checked_at is a real DB clock after these GETs, never backdated.
    rows, coherent = await _database_context(strategy.db, os.getenv('TRADE_ACCOUNT_ID', 'default'))
    policies, ledger = rows[:2]
    tables = rows[2:5]
    reservations, claims, sources = rows[5:8]
    gates = rows[8]
    if len(policies) != 1:
        raise ValueError('entry_policy_missing_or_ambiguous')
    gate = dict(gates[0]) if len(gates) == 1 else None
    ownership_claims = gate.get('ownership_claims') if gate else None
    if isinstance(ownership_claims, str):
        ownership_claims = json.loads(ownership_claims)
    if ownership_claims is not None and not isinstance(ownership_claims, list):
        raise ValueError('ownership_claims_invalid')
    from .margin_budget import HRS_OWNED,policy_margin_scope,account_cash_fields,validate_cash_observation
    scope=policy_margin_scope(dict(policies[0]))
    cash=account_cash_fields(account,getattr(strategy.binance_client,'use_unified_account',False))
    # started precedes the gather's actual account GET, retaining a conservative
    # cash age even when inventories or the database become ready much later.
    cash['account_cash_observed_at']=started
    available = datetime.now(timezone.utc)
    if scope==HRS_OWNED:
        validate_cash_observation(cash,checked_at=available,observed_at=observed)
    return dict(hrs_margin_scope=scope,**cash,runtime_slot_scope=SLOT_SCOPE, hrs_slot_inputs=slot_inputs, started_at=started, available_at=available, account_equity=str(equity),
        account_balances={key: account[key] for key in (
            ('totalMarginBalance', 'availableBalance', 'accountStatus') if getattr(strategy.binance_client, 'use_unified_account', False) is True
            else ('totalMarginBalance', 'totalWalletBalance', 'totalUnrealizedProfit', 'updateTime', 'availableBalance', 'accountStatus')) if key in account},
        account_equity_source=('papi/v1/account.accountEquity' if getattr(strategy.binance_client, 'use_unified_account', False) is True
                               else 'fapi/v3/account.totalMarginBalance'),
        exchange_positions=inventory, independent_positions=independent,
        reservations=[dict(row) for row in reservations if row.get('strategy_id','hrs')=='hrs'],
        account_pending_reservations=[dict(row) for row in reservations], claims=decode_claim_evidence(claims), claim_source_format='postgresql_to_jsonb_v1',
        equity_source_archives=[dict(row) for row in sources],
        equity_gate=gate, ownership_claims=ownership_claims,
        hrs_marked_equity_observation=ledger_equity_observation(dict(ledger[0]) if len(ledger)==1 else None, sources),

        allocation=str(amount) if amount is not None else None, allocation_source=source,
        entry_policy=dict(policies[0]), ledger_state=dict(ledger[0]) if len(ledger) == 1 else None,
        database_snapshot_coherent=coherent,
        replay_bootstrap_ready=coherent and len(ledger) == 1 and amount is not None,
        pending_timeout_entries=[dict(row) for row in pending],
        state_tables={name: {'rows': [dict(row) for row in table], 'captured_at': available,
                           'database_snapshot_coherent': coherent}
                      for name, table in zip(TABLES, tables)})


def capture_configuration_identity(strategy_dir, config, metadata, *, params_path=None):
    """Bind raw source identities to the config actually supplied to this process."""
    import hashlib
    from pathlib import Path
    import re
    import yaml
    from shared.config_loader import compute_config_hash, deep_merge
    from .decision_capture import _normalize
    try:
        folder = Path(strategy_dir)
        params_path = Path(params_path) if params_path else Path(__file__).resolve().parents[2]/'ai_tuner/parameters/manifests/hrs.yaml'
        base_raw = (folder/'config.yaml').read_bytes()
        params_raw = params_path.read_bytes()
        base = yaml.safe_load(base_raw)
        requested = metadata.get('requested_overrides_version')
        active_path = folder/'tuning_overrides/.active'
        active = active_path.read_text().strip() if active_path.exists() else None
        if active != requested or (requested and not re.fullmatch(r'[A-Za-z0-9_-]+', requested)):
            raise ValueError()
        overlay_raw = (folder/'tuning_overrides'/f'{requested}.yaml').read_bytes() if requested else b''
        overlay = yaml.safe_load(overlay_raw) if overlay_raw else None
        applied = metadata.get('applied_overrides_version')
        if applied and (applied != requested or not isinstance(overlay, dict)):
            raise ValueError()
        effective = deep_merge(base, overlay) if applied else base
        if compute_config_hash(effective) != metadata['config_hash'] or compute_config_hash(config) != metadata['config_hash']:
            raise ValueError()
        digest = lambda raw: hashlib.sha256(raw).hexdigest()
        return dict(config_hashes={'base_config_hash': digest(base_raw),
                    'params_manifest_hash': digest(params_raw), 'overlay_hash': digest(overlay_raw)},
            documents=_normalize({'base': base, 'params_manifest': yaml.safe_load(params_raw), 'overlay': overlay}),
            document_format='parsed_public_documents_with_original_byte_hashes',
            overlay_requested=requested, overlay_applied=bool(applied),
            metadata=dict(metadata), effective_config_hash=metadata['config_hash'])
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise ValueError('configuration_identity_unproven') from exc


SCAN_BOUNDARY_VERSION = 'hrs_reconciled_scan_boundary_v1'
SCAN_CONTEXT_TIMEOUT_SECONDS = 120
EXPECTED_SOURCE_DENIALS = frozenset({
    'local_position_inventory_conflict', 'source_collection_incomplete', 'source_observation_stale',
    'funding_settlement_pending', 'funding_schedule_coverage_gap',
})


class ScanContextUnavailable(ValueError):
    """A bounded source failure skips scoring while the caller maintains exits."""
    def __init__(self, reason, *, phase, reason_codes=(), elapsed_seconds=0):
        super().__init__(reason)
        self.phase = phase
        self.reason_codes = list(reason_codes)
        self.elapsed_seconds = round(elapsed_seconds, 3)
        self.budget_seconds = SCAN_CONTEXT_TIMEOUT_SECONDS

    @property
    def expected_source_failure(self):
        return (str(self) in {'scan_boundary_timeout', 'scan_boundary_source_timeout'}
                or bool(self.reason_codes) and all(reason in EXPECTED_SOURCE_DENIALS
                                                   for reason in self.reason_codes))


def cycle_failure_diagnostics(exc):
    # Frame locations give a useful traceback without dumping aiohttp request
    # objects, signed URLs, headers, response bodies or arbitrary cause reprs.
    frames = [dict(file=os.path.basename(frame.filename), line=frame.lineno, function=frame.name)
              for frame in traceback.extract_tb(exc.__traceback__)]
    text = str(exc)
    # Only our fixed boundary codes are safe messages. aiohttp's ordinary
    # exception string can contain the full signed URL even without repr().
    safe_message = (isinstance(exc, ValueError) and text in {
        'scan_boundary_timeout', 'scan_boundary_source_timeout', 'scan_boundary_source_denied',
        'scan_boundary_refresh_unproven', 'scan_boundary_unproven', 'scan_boundary_clock_unproven'})
    detail = dict(error=text if safe_message else type(exc).__name__, error_type=type(exc).__name__,
                  exception_frames=frames)
    if isinstance(exc, ScanContextUnavailable):
        detail.update(phase=exc.phase, reason_codes=exc.reason_codes,
                      elapsed_seconds=exc.elapsed_seconds, budget_seconds=exc.budget_seconds,
                      expected_source_failure=exc.expected_source_failure)
    status = getattr(exc, 'status_code', getattr(exc, 'status', None))
    if type(status) is int and 100 <= status <= 599:
        detail['http_status'] = status
    causes = []
    cause_frames = []
    cause = exc.__cause__
    while cause is not None and len(causes) < 5:
        causes.append(type(cause).__name__)
        cause_frames.append([dict(file=os.path.basename(frame.filename), line=frame.lineno, function=frame.name)
                             for frame in traceback.extract_tb(cause.__traceback__)])
        cause = cause.__cause__
    if causes:
        detail['cause_types'] = causes
        detail['cause_frames'] = cause_frames
    return detail


def _scan_now():
    return datetime.now(timezone.utc)


def validate_scan_boundary(context, *, requested_at, checked_at):
    """Bind a prospective scan boundary to its original committed parent.

    Economic time may precede collection availability, but never the request
    which started this prelude. Availability is retained and scoring waits for it.
    """
    from datetime import timedelta
    from .equity_evidence import canonical
    def aware(value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('scan_boundary_clock_unproven')
        return value.astimezone(timezone.utc)
    def obj(value):
        return json.loads(value) if isinstance(value, str) else value
    try:
        marker = context['scan_boundary']
        if not isinstance(marker, dict) or marker.get('version') != SCAN_BOUNDARY_VERSION:
            raise ValueError('marker')
        request, refresh_request, boundary, available, checked = map(aware, (
            requested_at, marker['requested_at'], marker['as_of'], context['available_at'], checked_at))
        parent_available = aware(marker['available_at'])
        if (not request <= refresh_request <= boundary <= parent_available <= available <= checked
                or checked-request > timedelta(seconds=120)
                or checked-boundary > timedelta(seconds=120)
                or context.get('database_snapshot_coherent') is not True
                or context.get('replay_bootstrap_ready') is not True):
            raise ValueError('clock_or_snapshot')
        parents = [p for p in context['equity_source_archives']
                   if p.get('evidence_sha256') == marker['evidence_sha256']]
        if len(parents) != 1:
            raise ValueError('parent_identity')
        parent = parents[0]
        raw = obj(parent['payload'])
        ledger = context['ledger_state']
        state = obj(ledger['ledger_state'])
        policy, gate = context['entry_policy'], context['equity_gate']
        if (canonical(raw)[1] != marker['evidence_sha256']
                or parent['account_id'] != raw['account_id'] or raw['account_id'] != policy['account_id']
                or policy['strategy_id'] != 'hrs'
                or parent['segment_id'] != raw['segment_id'] or raw['segment_id'] != ledger['segment_id']
                or state['segment_id'] != ledger['segment_id']
                or any(aware(value) != boundary for value in (parent['as_of'], raw['as_of'], state['as_of'], gate['as_of']))
                or aware(parent['available_at']) != parent_available or aware(raw['available_at']) != parent_available
                or type(marker['ledger_version']) is not int or marker['ledger_version'] <= 0
                or marker['ledger_version'] != ledger['version'] or ledger['version'] != raw['result']['ledger_version']
                or type(marker['policy_revision']) is not int or marker['policy_revision'] <= 0
                or marker['policy_revision'] != policy['revision'] or policy['revision'] != gate['policy_revision']
                or gate['evidence_sha256'] != marker['evidence_sha256']
                or not aware(policy['effective_at']) <= boundary <= available < aware(policy['expires_at'])
                or raw['source_collection'].get('complete') is not True
                or raw['source_collection'].get('gaps') != []
                or raw['income_report'].get('id_match_and_coverage_complete') is not True
                or raw['income_report'].get('coverage_gaps') != []):
            raise ValueError('source_binding')
        from .margin_budget import HRS_OWNED, policy_margin_scope, validate_cash_fields, validate_cash_observation
        if policy_margin_scope(policy) == HRS_OWNED:
            if context.get('hrs_margin_scope') != HRS_OWNED:
                raise ValueError('cash_scope')
            validate_cash_fields(context)
            validate_cash_observation(context, checked_at=checked,
                observed_at=context['hrs_slot_inputs']['observed_at'])
        for field in ('ready', 'allowed', 'reasons'):
            if field != 'reasons' and type(raw['result'][field]) is not bool:
                raise ValueError('gate_type')
            if obj(gate[field]) != raw['result'][field]:
                raise ValueError('gate_binding')
        return boundary
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError('scan_boundary_unproven') from exc


async def capture_scan_market_guard(strategy):
    """Retain the actual T-1 cache inputs before this scan can score symbols."""
    breaker=getattr(strategy,'circuit_breaker',None)
    config=getattr(strategy,'_market_guard_config',None)
    if breaker is None and config is None:
        return None
    if breaker is None or not isinstance(config,dict) or config.get('enabled') is not True or getattr(breaker,'pool',None)!='hrs':
        raise ValueError('market_guard_source_configuration_unproven')
    from datetime import timedelta
    from shared.circuit_breaker import floor_index_hour
    from .decision_capture import canonical_payload
    requested=_scan_now()
    hour=floor_index_hour(requested)-timedelta(hours=1)
    index=await breaker.load_index(hour)
    index12=await breaker.load_index_12h(hour)
    available=_scan_now()
    cache=getattr(breaker,'_index_cache',{})
    expected={hour.isoformat():index,hour.isoformat()+':12h':index12}
    if not isinstance(cache,dict) or any(key not in cache or canonical_payload(cache[key])[1]!=canonical_payload(value)[1] for key,value in expected.items()):
        raise ValueError('market_guard_source_cache_unproven')
    if not requested<=available or available-requested>timedelta(seconds=120):
        raise ValueError('market_guard_source_clock_unproven')
    observation=dict(version='hrs_market_guard_observation_v1',account_id=os.getenv('TRADE_ACCOUNT_ID','default'),
        pool='hrs',index_hour=hour.isoformat(),requested_at=requested.isoformat(),available_at=available.isoformat(),
        pool_index=index,pool_index_12h=index12,config_sha256=canonical_payload(config)[1])
    canonical_payload(observation)  # Invalid numeric source values cannot become an attestation.
    return observation


async def capture_scan_financial_context(strategy):
    """Refresh the ordinary collector before the scan, then capture fresh cash.

    No policy, gate, sizing or ledger semantics change. A failure blocks scoring
    through capture_cycle's existing maintenance path; no cached receipt replaces
    a failed or superseded refresh.
    """
    service = getattr(strategy, 'equity_service', None)
    if service is None:
        return await capture_financial_context(strategy)
    requested = _scan_now()
    started = monotonic()
    phase = 'market_guard'
    source_timeout = False
    async def collect():
        nonlocal phase
        guard = await capture_scan_market_guard(strategy)
        phase = 'equity_refresh'
        result = await service.refresh()
        if (isinstance(result, dict) and result.get('ready') is False
                and result.get('allowed') is False and result.get('policy_revision') == 0
                and not result.get('ledger_version')):
            raise ScanContextUnavailable('scan_boundary_source_denied', phase=phase,
                reason_codes=result.get('reasons', ()), elapsed_seconds=monotonic()-started)
        phase = 'financial_context'
        context = await capture_financial_context(strategy)
        if guard is not None:
            context = dict(context, market_guard_observation=guard)
        try:
            phase = 'boundary_validation'
            parents = [p for p in context['equity_source_archives']
                       if p.get('evidence_sha256') == result['evidence_sha256']]
            if len(parents) != 1:
                raise ValueError('parent')
            for field in ('ready', 'allowed', 'reasons'):
                observed = context['equity_gate'][field]
                observed = json.loads(observed) if isinstance(observed, str) else observed
                if observed != result[field] or (field != 'reasons' and type(result[field]) is not bool):
                    raise ValueError('refresh_gate')
            stamp = lambda value: value.isoformat() if isinstance(value, datetime) else value
            marker = dict(version=SCAN_BOUNDARY_VERSION, requested_at=requested.isoformat(),
                as_of=stamp(result['as_of']), available_at=stamp(parents[0]['available_at']),
                evidence_sha256=result['evidence_sha256'], ledger_version=result['ledger_version'],
                policy_revision=result['policy_revision'])
            context = dict(context, scan_boundary=marker)
            validate_scan_boundary(context, requested_at=requested, checked_at=_scan_now())
            return context
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError('scan_boundary_refresh_unproven') from exc
    async def timed_source():
        nonlocal source_timeout
        try:
            return await collect()
        except asyncio.TimeoutError:
            source_timeout = True
            raise
    try:
        return await asyncio.wait_for(timed_source(), timeout=SCAN_CONTEXT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        reason = 'scan_boundary_source_timeout' if source_timeout else 'scan_boundary_timeout'
        raise ScanContextUnavailable(reason, phase=phase,
            elapsed_seconds=monotonic()-started) from exc
