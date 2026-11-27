"""Immutable-row provenance for holdings ceilings; never an entry budget."""
from decimal import Decimal, InvalidOperation
import hashlib
import json

METHOD = 'classic_history_acceptance_v1'


def entries_list(value):
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, list) or any(not isinstance(e, dict) for e in value):
        raise ValueError('invalid_allocation_entries')
    return value


def entries_digest(value):
    return hashlib.sha256(json.dumps(entries_list(value), sort_keys=True,
        separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def amount(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError()
        return result
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('invalid_allocation_amount') from None


def validated_holdings_limit(entry, previous_row):
    if (entry.get('publication_method') != METHOD
            or type(entry.get('previous_allocation_id')) is not int
            or entry['previous_allocation_id'] != previous_row['id']
            or entry.get('previous_entries_sha256') != entries_digest(previous_row['entries'])):
        raise ValueError('allocation_parent_provenance_invalid')
    rows = entries_list(previous_row['entries'])
    owners = [e.get('strategy_id') for e in rows]
    if len(owners) != len(set(owners)) or entry.get('strategy_id') not in owners:
        raise ValueError('allocation_parent_scope_invalid')
    previous = rows[owners.index(entry['strategy_id'])]
    if 'holdings_floor_usdt' in previous and previous.get('publication_method') != METHOD:
        raise ValueError('allocation_parent_method_invalid')
    expected = max(amount(previous['allocated_amount']),
                   amount(previous.get('holdings_floor_usdt', previous['allocated_amount'])))
    if amount(entry.get('holdings_floor_usdt')) != expected:
        raise ValueError('allocation_holdings_floor_changed')
    return max(amount(entry['allocated_amount']), expected)


PROTECTION_FIELDS = frozenset({'publication_method', 'holdings_floor_usdt',
    'previous_allocation_id', 'previous_entries_sha256', 'history_acceptance_sha256'})
MAX_CHAIN_DEPTH = 512


def allocation_anchor(payload):
    """The approved legacy row is a fixed root, not a replaceable chain terminus."""
    import re
    identity = payload.get('allocation_anchor_id')
    digest = payload.get('allocation_anchor_entries_sha256')
    if (type(identity) is not int or identity <= 0 or not isinstance(digest, str)
            or re.fullmatch(r'[0-9a-f]{64}', digest) is None):
        raise ValueError('allocation_anchor_invalid')
    return identity, digest


def _strategy_entries(row):
    if type(row.get('id')) is not int or row['id'] <= 0:
        raise ValueError('allocation_row_identity_invalid')
    entries = entries_list(row['entries'])
    ids = [item.get('strategy_id') for item in entries]
    if (not entries or any(not isinstance(sid, str) or not sid for sid in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('allocation_strategy_scope_invalid')
    return entries, {item['strategy_id']:item for item in entries}


async def validate_allocation_chain(db, row, account='default', policy=None):
    """Verify every ancestor, including its original receipt, to the fixed root.

    Returns holdings limits for the supplied row only. Entry budgets remain the
    original ``allocated_amount`` fields. A legacy row is accepted without an
    approval only when it is the original input; any protected chain must reach
    the exact legacy row and entries hash bound by all its immutable receipts.
    """
    from shared.accounting.history_tolerance import validate_history_acceptance

    fetch = getattr(db, 'fetch_one', None) or db.fetchrow
    anchor = None
    uid = None
    if policy is not None:
        checked = validate_history_acceptance(dict(account_id=policy.account_id,
            revision=policy.revision, payload=policy.payload,
            receipt_sha256=policy.receipt_sha256), account)
        if checked is None or not checked.automatic_allocation:
            raise ValueError('allocation_chain_approval_invalid')
        anchor = allocation_anchor(checked.payload)
        uid = checked.exchange_uid_sha256
    receipts = {}
    original_limits = None
    original_scope = None
    for _ in range(MAX_CHAIN_DEPTH):
        entries, owners = _strategy_entries(row)
        if original_scope is None:
            original_scope = set(owners)
        elif set(owners) != original_scope:
            raise ValueError('allocation_chain_strategy_scope_changed')
        if not any(PROTECTION_FIELDS.intersection(item) for item in entries):
            limits = {sid: amount(item['allocated_amount']) for sid,item in owners.items()}
            if anchor is not None and (row['id'], entries_digest(entries)) != anchor:
                raise ValueError('allocation_chain_legacy_anchor_mismatch')
            return original_limits if original_limits is not None else limits
        if any(item.get('publication_method') != METHOD
               or not PROTECTION_FIELDS <= item.keys() for item in entries):
            raise ValueError('allocation_chain_protection_incomplete')
        first = entries[0]
        receipt_sha = first['history_acceptance_sha256']
        parent_id = first['previous_allocation_id']
        if (type(parent_id) is not int or not 0 < parent_id < row['id']
                or not isinstance(receipt_sha, str)):
            raise ValueError('allocation_chain_parent_identity_invalid')
        if any(item['history_acceptance_sha256'] != receipt_sha
               or item['previous_allocation_id'] != parent_id for item in entries):
            raise ValueError('allocation_chain_row_provenance_mismatch')
        if receipt_sha not in receipts:
            receipt = await fetch('SELECT account_id,revision,payload,receipt_sha256 '
                'FROM trading.historical_risk_acceptance WHERE account_id=$1 AND receipt_sha256=$2',
                account,receipt_sha)
            if receipt is None:
                raise ValueError('allocation_chain_original_approval_missing')
            approved = validate_history_acceptance(receipt,account,expected_uid=uid)
            if (approved is None or not approved.automatic_allocation
                    or approved.receipt_sha256 != receipt_sha):
                raise ValueError('allocation_chain_original_approval_invalid')
            receipts[receipt_sha] = approved
        approved = receipts[receipt_sha]
        approved_anchor = allocation_anchor(approved.payload)
        if anchor is None:
            anchor, uid = approved_anchor, approved.exchange_uid_sha256
        elif anchor != approved_anchor:
            raise ValueError('allocation_chain_approval_anchor_changed')
        ceilings = approved.payload['allocation_ceilings_usdt']
        if set(owners) != set(ceilings):
            raise ValueError('allocation_chain_approved_scope_mismatch')
        if any(amount(item['allocated_amount']) > amount(ceilings[sid]) for sid,item in owners.items()):
            raise ValueError('allocation_chain_entry_cap_exceeded')
        previous = await fetch('SELECT id,entries FROM public.capital_allocation WHERE id=$1',parent_id)
        if previous is None:
            raise ValueError('allocation_chain_parent_missing')
        limits = {sid:validated_holdings_limit(item,previous) for sid,item in owners.items()}
        if original_limits is None:
            original_limits = limits
        row = previous
    raise ValueError('allocation_chain_depth_exceeded')
