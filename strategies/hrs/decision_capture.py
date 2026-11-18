"""Append-only, time-stamped HRS decision inputs; never infer missing history."""
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import uuid
import time
import os
from functools import wraps
from contextvars import ContextVar
import structlog

log = structlog.get_logger()

PUBLIC_INPUT_PATHS = frozenset({
    '/fapi/v1/openInterest', '/fapi/v1/premiumIndex', '/fapi/v1/fundingRate',
    '/fapi/v1/fundingInfo', '/fapi/v1/ticker/24hr', '/fapi/v1/exchangeInfo',
    '/fapi/v1/klines', '/fapi/v1/markPriceKlines',
})
SECRET_KEYS = frozenset({'api_key', 'api_secret', 'password', 'secret', 'authorization',
                        'token', 'signature', 'private_key', 'x-cmc_pro_api_key'})


def _secret_key(value):
    key = str(value).lower().replace('-', '_')
    # Match the shared config hash's credential substrings, plus common key
    # spellings. Public provenance keys such as provider_asset_id stay intact.
    return key in SECRET_KEYS or any(part in key for part in (
        'webhook', 'secret', 'password', 'token', 'api_key', 'apikey',
        'authorization', 'private_key', 'privatekey'))


def _normalize(value):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if _secret_key(k) else _normalize(v)
                for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normalize(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_normalize(v) for v in sorted(value, key=str)]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('nonfinite_evidence')
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('naive_evidence_timestamp')
        return value.isoformat()
    if hasattr(value, 'to_dict'):
        return _normalize(value.to_dict())
    if is_dataclass(value):
        fields = asdict(value)
        # Shared-market consumers attach these stable IDs dynamically to the
        # legacy six-field provenance dataclass. asdict alone discards them.
        for name in ('provider', 'provider_asset_id'):
            if hasattr(value, name):
                fields[name] = getattr(value, name)
        return _normalize(fields)
    return value


def canonical_payload(value):
    payload = json.dumps(_normalize(value), sort_keys=True, separators=(',', ':'),
                         allow_nan=False, ensure_ascii=False)
    return payload, hashlib.sha256(payload.encode()).hexdigest()


class DecisionRecorder:
    def __init__(self, db):
        self.db = db
        self._run_context = ContextVar('hrs_evidence_run', default='startup-' + uuid.uuid4().hex)
        self.failed = False
        self.context_provider = None
        self.incomplete_runs = set()

    @property
    def run_id(self):
        return self._run_context.get()

    @run_id.setter
    def run_id(self, value):
        self._run_context.set(value)

    async def record(self, run_id, kind, payload, *, as_of, available_at, symbol=None, evidence_id=None):
        try:
            if (not run_id or not kind or as_of.tzinfo is None or available_at.tzinfo is None
                    or available_at < as_of):
                raise ValueError('invalid_evidence_envelope')
            encoded, digest = canonical_payload(payload)
            evidence_id = evidence_id or hashlib.sha256(
                f'{run_id}|{kind}|{symbol}|{as_of.isoformat()}|{digest}'.encode()).hexdigest()
            await self.db.execute('''INSERT INTO hrs.hrs_decision_evidence
                (evidence_id,run_id,kind,symbol,event_time,available_at,payload_sha256,payload)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb) ON CONFLICT(evidence_id) DO NOTHING''',
                evidence_id, run_id, kind, symbol, as_of, available_at, digest, encoded)
            stored = await self.db.fetch_all(
                'SELECT run_id,kind,symbol,event_time,available_at,payload_sha256 FROM hrs.hrs_decision_evidence WHERE evidence_id=$1', evidence_id)
            if (len(stored) != 1 or stored[0]['payload_sha256'] != digest
                    or any(stored[0][name] != value for name, value in (
                        ('run_id', run_id), ('kind', kind), ('symbol', symbol),
                        ('event_time', as_of), ('available_at', available_at)))):
                raise ValueError('evidence_identity_conflict')
        except Exception:
            self.failed = True
            raise
        return evidence_id

    def attach_public_transport(self, client):
        original = client._request
        previous_guard = getattr(client, '_new_risk_submit_guard', None)

        def submit_guard(method, endpoint, params):
            if previous_guard is not None:
                previous_guard(method, endpoint, params)
            reducing = str(params.get('reduceOnly', False)).lower() == 'true'
            closing = str(params.get('closePosition', False)).lower() == 'true'
            if (self.failed and method.upper() == 'POST'
                    and endpoint in ('/fapi/v1/order', '/papi/v1/um/order')
                    and not reducing and not closing):
                raise RuntimeError('hrs_decision_evidence_incomplete')

        client._new_risk_submit_guard = submit_guard

        async def request(method, endpoint, params=None, signed=True, **kwargs):
            try:
                result = await original(method, endpoint, params=params, signed=signed, **kwargs)
            except Exception as exc:
                if method.upper() == 'GET' and endpoint in PUBLIC_INPUT_PATHS and signed is False:
                    now = datetime.now(timezone.utc)
                    public_params = params or {}
                    await safe_record(self, self.run_id, 'public_market_failure',
                        {'path': endpoint, 'params': public_params, 'available': False,
                         'error_type': type(exc).__name__}, as_of=now, available_at=now,
                        symbol=public_params.get('symbol') if isinstance(public_params, dict) else None)
                raise
            # Transport replay data is restricted to public GET inputs. No headers,
            # signing arguments or private account/order payloads are intercepted.
            if method.upper() == 'GET' and endpoint in PUBLIC_INPUT_PATHS and signed is False:
                now = datetime.now(timezone.utc)
                public_params = params or {}
                await safe_record(self, self.run_id, 'public_market_response',
                    {'path': endpoint, 'params': public_params, 'response': result}, as_of=now,
                    available_at=now, symbol=public_params.get('symbol') if isinstance(public_params, dict) else None)
            return result

        client._request = request

    def attach_market_provider(self, provider):
        self.attach_market_batches(provider)
        for name in ('get_market_cap_provenance', 'get_oi_usd', 'get_funding_rate', 'get_24h_volume'):
            original = getattr(provider, name)

            def wrap(function, operation):
                async def observed(*args, **kwargs):
                    try:
                        result = await function(*args, **kwargs)
                    except Exception as exc:
                        now = datetime.now(timezone.utc)
                        await safe_record(self, self.run_id, 'market_input_failure',
                            {'operation': operation, 'arguments': args, 'options': kwargs,
                             'available': False, 'error_type': type(exc).__name__},
                            as_of=now, available_at=now, symbol=args[0] if args else kwargs.get('symbol'))
                        raise
                    now = datetime.now(timezone.utc)
                    await safe_record(self, self.run_id, 'market_input',
                        {'operation': operation, 'arguments': args, 'options': kwargs, 'result': result},
                        as_of=now, available_at=now, symbol=args[0] if args else kwargs.get('symbol'))
                    return result
                return observed

            setattr(provider, name, wrap(original, name))

    def attach_market_batches(self, provider):
        original = provider.preload_market_caps
        async def preload(symbols):
            result = await original(symbols)
            now = datetime.now(timezone.utc)
            await safe_record(self, self.run_id, 'market_cap_batch',
                {'requested_symbols': list(symbols), 'coverage_scope': 'requested_scan_batch',
                 'universe_audit': getattr(provider, 'universe_audit', None),
                 'quotes': result, 'captured_at': now}, as_of=now, available_at=now)
            return result
        provider.preload_market_caps = preload

    async def begin(self, snapshot, as_of):
        self.run_id = uuid.uuid4().hex
        available = datetime.now(timezone.utc)
        await self.record(self.run_id, 'scan_start', snapshot, as_of=as_of, available_at=available)
        return self.run_id


async def safe_record(recorder, *args, **kwargs):
    try:
        await recorder.record(*args, **kwargs)
        return True
    except Exception:
        # A durable-evidence outage blocks entries, never delivery of already
        # fetched public data needed to maintain existing stops/positions.
        recorder.failed = True
        log.warning('hrs_evidence_unavailable', kind=args[1] if len(args) > 1 else 'unknown')
        return False


async def record_stage(strategy, kind, payload, *, symbol=None):
    recorder = getattr(strategy, 'decision_recorder', None)
    if recorder is not None:
        now = datetime.now(timezone.utc)
        if kind == 'entry_stage_inputs':
            payload = dict(payload, observed_at=now)
        await safe_record(recorder, recorder.run_id, kind, payload,
                          as_of=now, available_at=now, symbol=symbol)


def strategy_snapshot(strategy):
    pool = getattr(strategy, 'candidate_pool', None)
    positions = getattr(strategy, 'position_manager', None)
    risk = getattr(strategy, 'risk_manager', None)
    market = getattr(strategy, 'market_data', None)
    dynamic = getattr(pool, '_dynamic_thresholds', None)
    return {
        'config': getattr(strategy, 'config', {}),
        'loaded_config_metadata': getattr(strategy, 'loaded_config_metadata', None),
        'configuration_evidence': getattr(strategy, 'configuration_evidence', None),
        'config_hashes': getattr(strategy, 'config_hashes', None),
        'release_sha': getattr(strategy, 'release_sha', None),
        'runtime': {
            'signal_timestamps': {f'{symbol}:{direction}': stamp
                for symbol, directions in getattr(strategy, '_signal_timestamps', {}).items()
                for direction, stamp in directions.items()},
            'daily_reset_date': (risk._daily_reset_date.date().isoformat()
                if risk and getattr(risk, '_daily_reset_date', None) else None),
            'risk_manager': risk.to_dict() if risk else None,
            'total_pnl': getattr(strategy, '_total_pnl', None),
        },
        'candidate_scan': {
            'short_candidates': pool.get_short_candidates() if pool else [],
            'long_candidates': pool.get_long_candidates() if pool else [],
            'as_of': getattr(pool, '_last_scan_time', None),
            'dynamic_thresholds': dict(dynamic.to_dict(), computed_at=dynamic.computed_at) if dynamic else None,
            'excluded_new_coin_symbols': getattr(pool, '_new_coin_open_positions', []),
            'eliminated_symbols': getattr(pool, '_eliminated_symbols', []),
        },
        'positions': positions.get_all_positions() if positions else {},
        'market_circuit_breaker_config': getattr(strategy, '_market_guard_config', None),
        'market_circuit_breaker_state': {
            'cooldown_until_local': {symbol: stamp.isoformat() for symbol, stamp in
                getattr(getattr(strategy, 'circuit_breaker', None), '_cooldown_until', {}).items()},
            'process_timezone': list(time.tzname), 'process_utc_offset_seconds': -time.timezone,
            'index_cache': getattr(getattr(strategy, 'circuit_breaker', None), '_index_cache', {}),
        },
        'signal_timestamps': getattr(strategy, '_signal_timestamps', {}),
        'risk_state': {k: getattr(risk, k, None) for k in ('pause_until', 'drawdown_pause_until', 'blacklist', 'consecutive_losses')},
        'universe_audit': getattr(market, 'universe_audit', {}),
    }


def capture_cycle(function):
    @wraps(function)
    async def wrapped(strategy, *args, **kwargs):
        recorder = getattr(strategy, 'decision_recorder', None)
        if recorder is None:
            return await function(strategy, *args, **kwargs)
        previous_run = recorder.run_id
        now = datetime.now(timezone.utc)
        try:
            snapshot = strategy_snapshot(strategy)
            provider = getattr(recorder, 'context_provider', None)
            if callable(provider):
                snapshot['financial_context'] = await provider()
        except Exception as exc:
            failed_at = datetime.now(timezone.utc)
            await safe_record(recorder, 'context-' + uuid.uuid4().hex, 'scan_context_failure',
                {'parent_run': previous_run, 'cycle': function.__name__, 'error_type': type(exc).__name__},
                as_of=now, available_at=failed_at)
            incomplete = getattr(recorder, 'incomplete_runs', None)
            if isinstance(incomplete, set):
                incomplete.add(previous_run)
            try:
                await strategy._monitor_positions()
            finally:
                recorder.run_id = previous_run
            raise
        try:
            await recorder.begin(snapshot, now)
        except Exception:
            recorder.failed = True
            # A persistence outage cannot suppress existing position protection.
            try:
                await strategy._monitor_positions()
            finally:
                recorder.run_id = previous_run
            raise
        status = 'failed'
        try:
            result = await function(strategy, *args, **kwargs)
            incomplete = getattr(recorder, 'incomplete_runs', set())
            status = 'incomplete' if recorder.failed or recorder.run_id in incomplete else 'complete'
            return result
        finally:
            finished = datetime.now(timezone.utc)
            try:
                await safe_record(recorder, recorder.run_id, 'scan_end',
                    {'state': strategy_snapshot(strategy), 'status': status}, as_of=finished, available_at=finished)
            finally:
                incomplete = getattr(recorder, 'incomplete_runs', None)
                if isinstance(incomplete, set):
                    incomplete.discard(recorder.run_id)
                recorder.run_id = previous_run
            if os.getenv('HRS_ACCEPTANCE_REPORT_ENABLED') == '1':
                try:
                    from .acceptance_report import build_report, write_report_atomic
                    from shared.entry_risk_policy import account_id
                    report = await build_report(strategy.db, account_id=account_id())
                    write_report_atomic(report, os.getenv('HRS_ACCEPTANCE_REPORT_PATH', '/app/logs/hrs-acceptance.json'))
                except Exception as exc:
                    log.warning('hrs_acceptance_report_unavailable', error_type=type(exc).__name__)
    return wrapped


def capture_analysis(function):
    @wraps(function)
    async def wrapped(strategy, symbol, *args, **kwargs):
        result = await function(strategy, symbol, *args, **kwargs)
        inputs = result.pop('_audit_input_klines', None) if isinstance(result, dict) else None
        recorder = getattr(strategy, 'decision_recorder', None)
        if recorder is not None:
            now = datetime.now(timezone.utc)
            await safe_record(recorder, recorder.run_id, 'analysis', {
                'result': result,
                'klines_1h': inputs['klines_1h'] if inputs is not None else strategy._klines_cache.get(symbol, []),
                'klines_4h': inputs['klines_4h'] if inputs is not None else strategy._klines_4h_cache.get(symbol, []),
            }, as_of=now, available_at=now, symbol=symbol)
        return result
    return wrapped


def capture_signal(function):
    @wraps(function)
    async def wrapped(strategy, signal, *args, **kwargs):
        recorder = getattr(strategy, 'decision_recorder', None)
        if recorder is not None:
            now = datetime.now(timezone.utc)
            await safe_record(recorder, recorder.run_id, 'signal_pre_execution', signal,
                              as_of=now, available_at=now, symbol=signal.get('symbol'))
        result = await function(strategy, signal, *args, **kwargs)
        if recorder is not None:
            now = datetime.now(timezone.utc)
            await safe_record(recorder, recorder.run_id, 'signal_execution_outcome',
                {'signal': signal, 'result': result}, as_of=now, available_at=now, symbol=signal.get('symbol'))
        return result
    return wrapped


def capture_open_execution(function):
    @wraps(function)
    async def wrapped(executor, symbol, *args, **kwargs):
        recorder = getattr(executor, 'decision_recorder', None)
        try:
            result = await function(executor, symbol, *args, **kwargs)
        except Exception as exc:
            if recorder is not None:
                now = datetime.now(timezone.utc)
                await safe_record(recorder, recorder.run_id, 'order_execution_failure',
                    {'operation': function.__name__, 'error_type': type(exc).__name__},
                    as_of=now, available_at=now, symbol=symbol)
            raise
        if recorder is not None:
            now = datetime.now(timezone.utc)
            await safe_record(recorder, recorder.run_id, 'order_execution_result',
                {'operation': function.__name__, 'outcome': result},
                as_of=now, available_at=now, symbol=symbol)
        return result
    return wrapped


SCHEMA_SQL = '''CREATE TABLE IF NOT EXISTS hrs.hrs_decision_evidence (
 evidence_id text PRIMARY KEY, run_id text NOT NULL, kind text NOT NULL,
 symbol text, event_time timestamptz NOT NULL, available_at timestamptz NOT NULL,
 payload_sha256 text NOT NULL, payload jsonb NOT NULL,
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(available_at >= event_time)
);
CREATE INDEX IF NOT EXISTS hrs_decision_evidence_time ON hrs.hrs_decision_evidence(event_time,kind);
CREATE INDEX IF NOT EXISTS hrs_decision_evidence_run ON hrs.hrs_decision_evidence(run_id,kind);'''
