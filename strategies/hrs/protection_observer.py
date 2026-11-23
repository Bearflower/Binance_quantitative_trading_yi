"""Optional GET-only parent/child observations; never an execution authority."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json

import structlog

from .decision_capture import DecisionRecorder, canonical_payload

log = structlog.get_logger()
KIND = 'protection_order_observation'
ENDPOINT = '/papi/v1/um/algo/algoOrder'
MAX_SOURCE_BYTES = 4 * 1024 * 1024
INTERVAL_SECONDS = 300
GET_TIMEOUT_SECONDS = 20
TERMINAL = frozenset({'FINISHED', 'CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED'})


def utc_now():
    return datetime.now(timezone.utc)


def stamp(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('protection_observation_clock_unproven')
    return value.astimezone(timezone.utc)


def identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit() or int(value) <= 0:
        raise ValueError('protection_observation_identity_unproven')
    return str(int(value))


def verified_row(source, cutoff):
    row = dict(source)
    raw = row['payload']
    size = row.get('source_payload_bytes')
    if (type(size) is int and not 0 <= size <= MAX_SOURCE_BYTES
            or isinstance(raw, str) and len(raw) > MAX_SOURCE_BYTES):
        raise ValueError('protection_observation_source_budget')
    payload = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(payload, dict) or canonical_payload(payload)[1] != row['payload_sha256']:
        raise ValueError('protection_observation_source_hash_unproven')
    event, available = stamp(row['event_time']), stamp(row['available_at'])
    if not event <= available <= stamp(cutoff):
        raise ValueError('protection_observation_clock_unproven')
    if row.get('recorded_at') is not None and not available <= stamp(row['recorded_at']) <= stamp(cutoff):
        raise ValueError('protection_observation_clock_unproven')
    return dict(row, payload=payload, event_time=event, available_at=available)


def source_hash(row):
    # The reporter retains this only after verifying the original complete body.
    return row.get('source_payload_sha256', row['payload_sha256'])


def ack_identity(ack, start, account, cutoff):
    ack, start = verified_row(ack, cutoff), verified_row(start, cutoff)
    payload = ack['payload']
    request, response = payload['request'], payload['response']
    if (ack['kind'] != 'protection_order_ack' or start['kind'] != 'scan_start'
            or ack['run_id'] != start['run_id'] or start['available_at'] > ack['event_time']
            or start['payload']['financial_context']['entry_policy']['account_id'] != account
            or payload['role'] not in ('sl', 'tp1', 'tp2')
            or payload['position_id'].split(':')[:2] != ['hrs', ack['symbol']]
            or request['symbol'] != ack['symbol'] or response['symbol'] != ack['symbol']
            or request['side'] not in ('BUY', 'SELL') or response['side'] != request['side']
            or not isinstance(request['client_id'], str) or not request['client_id'].strip()
            or not isinstance(request.get('order_type'), str) or not request['order_type'].strip()
            or response.get('clientAlgoId') != request['client_id']):
        raise ValueError('protection_observation_parent_unproven')
    parent = identifier(response['algoId'])
    if identifier(payload['algo_id']) != parent:
        raise ValueError('protection_observation_parent_unproven')
    return ack, start, int(parent)


def validate_observation(row, ack, start, account, cutoff, *, child_required=True):
    ack, start, parent = ack_identity(ack, start, account, cutoff)
    row = verified_row(row, cutoff)
    p = row['payload'];response = p['response'];original = ack['payload'];request = original['request']
    if (row['kind'] != KIND or row['run_id'] != ack['run_id'] or row['symbol'] != ack['symbol']
            or p.get('version') != 'hrs_protection_observation_v1' or p.get('account_id') != account
            or p.get('run_id') != ack['run_id'] or p.get('symbol') != ack['symbol']
            or p.get('position_id') != original['position_id'] or p.get('role') != original['role']
            or p.get('ack_evidence_id') != ack['evidence_id'] or p.get('ack_payload_sha256') != source_hash(ack)
            or p.get('scan_start_evidence_id') != start['evidence_id'] or p.get('scan_start_payload_sha256') != source_hash(start)
            or p.get('query') != dict(method='GET', endpoint=ENDPOINT, params={'algoId': parent})
            or type(p['query']['params']['algoId']) is not int
            or not ack['available_at'] <= stamp(p['requested_at']) <= row['event_time'] <= row['available_at']
            or p.get('response_sha256') != canonical_payload(response)[1]
            or identifier(response['algoId']) != str(parent)
            or response.get('clientAlgoId') != request['client_id']
            or response.get('symbol') != ack['symbol'] or response.get('side') != request['side']
            or not request.get('order_type') or response.get('orderType') != request['order_type']
            or original['response'].get('orderType', request['order_type']) != request['order_type']):
        raise ValueError('protection_observation_binding_unproven')
    for key in ('algoType', 'positionSide', 'createTime'):
        if key in original['response'] and response.get(key) != original['response'][key]:
            raise ValueError('protection_observation_binding_unproven')
    update = datetime.fromtimestamp(int(identifier(response['updateTime'])) / 1000, timezone.utc)
    status = response.get('algoStatus')
    earliest = ack['event_time']
    if not child_required and status == 'NEW' and original['response'].get('createTime') is not None:
        # An unchanged NEW snapshot can retain its original server creation time,
        # which precedes the client's receipt of the successful submission ACK.
        earliest = min(earliest, datetime.fromtimestamp(
            int(identifier(original['response']['createTime'])) / 1000, timezone.utc))
    if not earliest <= update <= row['available_at']:
        raise ValueError('protection_observation_clock_unproven')
    if child_required and status not in ('FINISHED', 'TRIGGERED'):
        raise ValueError('protection_observation_child_unproven')
    child = identifier(response.get('actualOrderId')) if child_required or status in ('FINISHED', 'TRIGGERED') else None
    return child, status


_SELECT = f'''SELECT evidence_id,run_id,kind,symbol,event_time,available_at,recorded_at,payload_sha256,
    CASE WHEN octet_length(payload::text)<={MAX_SOURCE_BYTES} THEN payload ELSE NULL END AS payload,
    octet_length(payload::text) AS source_payload_bytes FROM hrs.hrs_decision_evidence '''


class ProtectionObserver:
    def __init__(self, db, api, recorder, *, account_id):
        self.db, self.api, self.account = db, api, account_id
        # Optional observations must not poison the main recorder's entry guard.
        self.recorder = DecisionRecorder(recorder.db)
        self._after = None

    async def _rows(self, connection, where, *args, cutoff, advance=False, strict=False):
        async for raw in connection.cursor(_SELECT + where, *args, prefetch=1):
            if advance:
                self._after = (stamp(raw['event_time']), raw['evidence_id'])
            try:
                yield verified_row(raw, cutoff)
            except (ValueError, KeyError, TypeError, AttributeError):
                if strict:
                    raise
                log.warning('hrs_protection_source_unproven', error_type='ValueError')

    async def _load(self, now):
        candidates = []
        async with self.db.pool.acquire() as connection, connection.transaction(isolation='repeatable_read', readonly=True):
            after = self._after or (now-timedelta(days=30), '')
            async for ack in self._rows(connection,
                'WHERE kind=$1 AND event_time >= $2 AND (event_time,evidence_id)>($3,$4) AND event_time<=$5 ORDER BY event_time,evidence_id LIMIT 5',
                'protection_order_ack', now-timedelta(days=30), *after, now, cutoff=now, advance=True):
                self._after = (ack['event_time'], ack['evidence_id'])
                try:
                    starts = []
                    async for start in self._rows(connection,
                        'WHERE kind=$1 AND run_id=$2 ORDER BY event_time,evidence_id LIMIT 2', 'scan_start', ack['run_id'], cutoff=now, strict=True):
                        # Validate one full snapshot at a time; never retain histories.
                        policy = start['payload'].get('financial_context', {}).get('entry_policy', {})
                        compact = {'financial_context': {'entry_policy': policy}}
                        start = dict(start, source_payload_sha256=start['payload_sha256'], payload=compact,
                                     payload_sha256=canonical_payload(compact)[1])
                        starts.append(start)
                    if len(starts) != 1:
                        continue
                    ack, start, parent = ack_identity(ack, starts[0], self.account, now)
                except (ValueError, KeyError, TypeError, AttributeError):
                    # Reject only this ACK. A malformed neighboring snapshot must
                    # not discard already verified candidates after keyset advance.
                    continue
                seen, terminal = set(), False
                async for prior in self._rows(connection,
                    "WHERE kind=$1 AND run_id=$2 AND payload->>'ack_evidence_id'=$3 ORDER BY event_time DESC LIMIT 32",
                    KIND, ack['run_id'], ack['evidence_id'], cutoff=now):
                    try:
                        _, status = validate_observation(prior, ack, start, self.account, now, child_required=False)
                        seen.add(prior['payload']['response_sha256'])
                        terminal |= status in TERMINAL
                    except (ValueError, KeyError, TypeError, AttributeError):
                        continue
                if not terminal:
                    candidates.append((ack, start, parent, seen))
        if self._after == after:
            self._after = None
            # The exhausted page contained no ACKs. Reopen at the window start
            # in this same poll rather than skipping an entire 300-second turn.
            return await self._load(now)
        return candidates

    async def collect_once(self):
        now = utc_now()
        try:
            candidates = await asyncio.wait_for(self._load(now), timeout=GET_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('hrs_protection_source_unavailable', error_type=type(exc).__name__)
            return
        for ack, start, parent, seen in candidates:
            try:
                requested = utc_now()
                response = await asyncio.wait_for(self.api.get_algo_order(algo_id=parent), timeout=GET_TIMEOUT_SECONDS)
                available = utc_now()
                digest = canonical_payload(response)[1]
                payload = dict(version='hrs_protection_observation_v1', account_id=self.account, run_id=ack['run_id'],
                    symbol=ack['symbol'], position_id=ack['payload']['position_id'], role=ack['payload']['role'],
                    ack_evidence_id=ack['evidence_id'], ack_payload_sha256=source_hash(ack),
                    scan_start_evidence_id=start['evidence_id'], scan_start_payload_sha256=source_hash(start),
                    query=dict(method='GET', endpoint=ENDPOINT, params={'algoId': parent}), requested_at=requested.isoformat(),
                    response=response, response_sha256=digest)
                envelope = dict(kind=KIND, run_id=ack['run_id'], symbol=ack['symbol'], event_time=available,
                                available_at=available, payload=payload, payload_sha256=canonical_payload(payload)[1])
                validate_observation(envelope, ack, start, self.account, available, child_required=False)
                if digest in seen:
                    continue
                identity = hashlib.sha256(f'{self.account}|{ack["evidence_id"]}|{digest}'.encode()).hexdigest()
                await self.recorder.record(ack['run_id'], KIND, payload, as_of=available, available_at=available,
                                           symbol=ack['symbol'], evidence_id='protection-observation-' + identity)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('hrs_protection_observation_unavailable', error_type=type(exc).__name__)

    async def run(self):
        while True:
            try:
                await self.collect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('hrs_protection_observer_unavailable', error_type=type(exc).__name__)
            await asyncio.sleep(INTERVAL_SECONDS)
