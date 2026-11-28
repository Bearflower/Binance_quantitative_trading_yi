"""Finite operator acceptance of unavailable history, never current evidence.

Receipts are administrator-appended and immutable. Readers retain original
coverage flags; this object is permission to use an explicitly labelled observed
history, not proof that missing exchange facts existed. Every consumer must also
validate current signed account identity and its own live-data/risk conditions.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from shared.accounting.account_cutover import account_cutover_ms

GAP_CODES = frozenset({'pm_native_income_coverage', 'pm_native_fill_coverage',
    'historical_nav_sampling', 'historical_cashflow_nav', 'pm_final_equity_missing',
    'pm_classic_valuation_discontinuity'})
CLASSIC_RISK_METHOD = 'classic_fixed_capital_cashflow_adjusted_drawdown_v1'
STRATEGIES = frozenset({'btc_eth','btc_eth_aggressive','hrs','new_coin'})


class HistoryAcceptanceError(ValueError):
    """An acceptance receipt cannot authorize an operational exception."""


def acceptance_digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _instant(value, name):
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise HistoryAcceptanceError('invalid_' + name) from None


def _amount(value, name, *, positive=True):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or (result <= 0 if positive else result < 0):
            raise ValueError()
        return result
    except (ValueError, InvalidOperation, TypeError):
        raise HistoryAcceptanceError('invalid_' + name) from None


def known_equity_points(payload, start_ms, end_ms):
    """Canonical chronological observations; duplicate timestamps are ambiguous."""
    points = payload.get('classic_known_equity_points', [])
    if not isinstance(points, list):
        raise HistoryAcceptanceError('classic_historical_points_invalid')
    seen = set()
    for point in points:
        if not isinstance(point, dict):
            raise HistoryAcceptanceError('classic_historical_point_invalid')
        stamp = point.get('as_of_ms')
        digest = point.get('source_sha256')
        if (type(stamp) is not int or not start_ms <= stamp <= end_ms or stamp in seen
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise HistoryAcceptanceError('classic_historical_point_invalid')
        for key in ('equity_usdt', 'net_transfer_usdt'):
            try:
                if not Decimal(str(point[key])).is_finite():
                    raise ValueError()
            except (KeyError, ValueError, InvalidOperation):
                raise HistoryAcceptanceError('classic_historical_point_amount_invalid') from None
        seen.add(stamp)
    return sorted(points, key=lambda point: point['as_of_ms'])


@dataclass(frozen=True)
class HistoryAcceptance:
    account_id: str
    exchange_uid_sha256: str
    revision: int
    accepted_from: datetime
    accepted_through: datetime
    classic_flat_at: datetime
    classic_flat_evidence_sha256: str
    receipt_sha256: str
    allowed_gap_codes: frozenset[str]
    effective_at: datetime
    classic_open_equity: Decimal
    risk_limit_ratio: Decimal
    hrs_daily_limit: Decimal
    hrs_rolling_limit: Decimal
    automatic_allocation: bool
    payload: dict

    def covers(self, code, start, end):
        start, end = _instant(start, 'gap_start'), _instant(end, 'gap_end')
        return (code in self.allowed_gap_codes
                and self.accepted_from <= start <= end <= self.accepted_through)


def validate_history_acceptance(row, account_id, *, now=None, expected_uid=None):
    """Validate identity, immutable hash, fixed limits and a bounded past window."""
    try:
        payload = json.loads(row['payload']) if isinstance(row['payload'], str) else copy.deepcopy(row['payload'])
        digest = acceptance_digest(payload)
    except (KeyError, TypeError, ValueError):
        raise HistoryAcceptanceError('history_acceptance_payload_invalid') from None
    if (not isinstance(payload, dict) or row.get('receipt_sha256') != digest
            or payload.get('kind') != 'historical_risk_acceptance_v1'):
        raise HistoryAcceptanceError('history_acceptance_hash_or_kind_invalid')
    if (not isinstance(account_id, str) or not account_id.strip()
            or row.get('account_id') != account_id or payload.get('account_id') != account_id):
        raise HistoryAcceptanceError('history_acceptance_account_mismatch')
    revision = payload.get('revision')
    if type(revision) is not int or revision <= 0 or row.get('revision') != revision:
        raise HistoryAcceptanceError('history_acceptance_revision_invalid')
    uid = payload.get('exchange_uid_sha256')
    if (not isinstance(uid, str) or not re.fullmatch(r'[0-9a-f]{64}', uid)
            or (expected_uid is not None and uid != expected_uid)):
        raise HistoryAcceptanceError('history_acceptance_uid_mismatch')
    now = _instant(now or datetime.now(timezone.utc), 'now')
    effective = _instant(payload.get('effective_at'), 'effective_at')
    if effective > now:
        raise HistoryAcceptanceError('history_acceptance_not_effective')
    if type(payload.get('enabled')) is not bool:
        raise HistoryAcceptanceError('history_acceptance_enabled_invalid')
    if not payload['enabled']:
        return None
    if not isinstance(payload.get('approval_ref'), str) or not payload['approval_ref'].strip():
        raise HistoryAcceptanceError('history_acceptance_approval_missing')
    start = _instant(payload.get('accepted_from'), 'accepted_from')
    end = _instant(payload.get('accepted_through'), 'accepted_through')
    flat = _instant(payload.get('classic_flat_at'), 'classic_flat_at')
    cutover = account_cutover_ms(account_id)
    if (type(payload.get('classic_cutover_ms')) is not int or cutover is None
            or payload['classic_cutover_ms'] != cutover
            or not start <= datetime.fromtimestamp(cutover/1000,timezone.utc) <= flat <= end <= effective):
        raise HistoryAcceptanceError('history_acceptance_window_invalid')
    known_equity_points(payload, int(flat.timestamp()*1000), int(end.timestamp()*1000))
    flat_hash = payload.get('classic_flat_evidence_sha256')
    if not isinstance(flat_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', flat_hash):
        raise HistoryAcceptanceError('history_acceptance_flat_proof_invalid')
    codes = payload.get('allowed_gap_codes')
    if (not isinstance(codes, list) or not codes or any(not isinstance(c,str) for c in codes)
            or len(set(codes)) != len(codes) or not set(codes) <= GAP_CODES):
        raise HistoryAcceptanceError('history_acceptance_gap_scope_invalid')
    cash = _amount(payload.get('classic_open_equity'), 'classic_open_equity')
    limits = [_amount(payload.get(key), key) for key in
              ('risk_limit_ratio','hrs_daily_limit','hrs_rolling_limit')]
    if limits != [Decimal('.05'),Decimal('.03'),Decimal('.06')]:
        raise HistoryAcceptanceError('history_acceptance_limit_increase_or_change')
    if payload.get('classic_risk_method') != CLASSIC_RISK_METHOD:
        raise HistoryAcceptanceError('history_acceptance_risk_method_invalid')
    if type(payload.get('automatic_allocation')) is not bool:
        raise HistoryAcceptanceError('history_acceptance_allocation_approval_invalid')
    ceilings = payload.get('allocation_ceilings_usdt')
    if not isinstance(ceilings, dict) or set(ceilings) != STRATEGIES:
        raise HistoryAcceptanceError('history_acceptance_allocation_scope_invalid')
    for key, value in ceilings.items():
        _amount(value, 'ceiling_' + key, positive=False)
    return HistoryAcceptance(account_id, uid, revision, start, end, flat, flat_hash,
        digest, frozenset(codes), effective, cash, *limits, payload['automatic_allocation'], payload)


async def read_history_acceptance(db, account_id, *, now=None, expected_uid=None):
    fetch = getattr(db, 'fetch_one', None) or db.fetchrow
    row = await fetch('SELECT account_id,revision,payload,receipt_sha256 '
        'FROM trading.historical_risk_acceptance WHERE account_id=$1 '
        'ORDER BY revision DESC LIMIT 1', account_id)
    if row is None:
        return None
    return validate_history_acceptance(row,account_id,now=now,expected_uid=expected_uid)
