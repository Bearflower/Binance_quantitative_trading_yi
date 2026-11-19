"""Pure explicit HRS quota and actual account free-margin accounting.

Callers must validate the immutable financial receipt and positive ownership
proof first. No cash is inferred from equity, allocation or simulated releases.
"""
import json
from decimal import Decimal, InvalidOperation, localcontext
from .risk_budget import FLOOR, policy_budget_version

ALL_ACCOUNT = 'all_account_positions_v1'
HRS_OWNED = 'hrs_owned_quota_account_free_v1'
CASH_SOURCES = ('papi/v1/account.totalAvailableBalance', 'fapi/v2/account.availableBalance')


def policy_margin_scope(policy):
    version = policy_budget_version(policy)
    reason = policy.get('reason')
    try:
        contract = json.loads(reason) if reason else {}
    except (TypeError, ValueError):
        # The budget parser above has already rejected malformed JSON contracts.
        return ALL_ACCOUNT
    scope = contract.get('hrs_margin_scope', ALL_ACCOUNT)
    if not isinstance(scope, str) or scope not in (ALL_ACCOUNT, HRS_OWNED):
        raise ValueError('hrs_margin_scope_invalid')
    if scope == HRS_OWNED and version != FLOOR:
        raise ValueError('hrs_margin_scope_requires_floor')
    return scope


def exact(value, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError('hrs_margin_number_invalid')
    try:
        amount = Decimal(value)
        if not amount.is_finite() or (amount < 0 if zero else amount <= 0):
            raise ValueError('hrs_margin_number_invalid')
        return amount
    except (InvalidOperation, ArithmeticError) as exc:
        raise ValueError('hrs_margin_number_invalid') from exc


def account_cash_fields(account, unified):
    """Retain normalized actual GET fields; never supply a financial fallback."""
    if not isinstance(account, dict):
        raise ValueError('hrs_account_cash_missing')
    return dict(account_available_balance=account.get('availableBalance'),
        account_available_balance_source=CASH_SOURCES[0 if unified is True else 1],
        account_status=account.get('accountStatus'))


def validate_cash_fields(financial):
    available = exact(financial.get('account_available_balance'))
    if (financial.get('account_available_balance_source') not in CASH_SOURCES
            or financial.get('account_status') != 'NORMAL'):
        raise ValueError('hrs_account_cash_source_invalid')
    return available


def _precision(values):
    # Guard sums/subtractions against loss of small cash shortfalls at the
    # ambient Decimal precision; retain supplied scales across products too.
    if not values:return 28
    span=max(v.adjusted() for v in values)-min(v.as_tuple().exponent for v in values)+1
    return max(28,4*span+len(values)+16)


def pending_margin(rows, *, account=None):
    """All retained margins, including submitting/filled ambiguous intents."""
    if not isinstance(rows, (list, tuple)):
        raise ValueError('hrs_account_pending_invalid')
    amounts, identities = [], set()
    for row in rows:
        identity = row['client_order_id']
        if (not isinstance(identity, str) or not identity or identity in identities
                or not isinstance(row['account_id'], str) or not row['account_id']
                or (account is not None and row['account_id'] != account)
                or not isinstance(row['strategy_id'], str) or not row['strategy_id']
                or row['state'] not in ('reserved', 'submitting')):
            raise ValueError('hrs_account_pending_invalid')
        identities.add(identity)
        amounts.append(exact(row['margin']))
    with localcontext() as context:
        context.prec=_precision(amounts)
        return sum(amounts,Decimal(0))


def _margin_limits(financial, proof, pending, account_pending, allocation, policy):
    """Named Decimal ceilings; remaining_margin is the shared assessor ceiling.

    Physical HRS and retained HRS margins deliberately accumulate. Account cash
    deducts every strategy's retained margins. The caller excludes its own intent
    from both pending lists when reconstructing its original authorization.
    """
    if policy_margin_scope(policy) != HRS_OWNED or financial.get('hrs_margin_scope') != HRS_OWNED:
        raise ValueError('hrs_margin_scope_mismatch')
    available = validate_cash_fields(financial)
    inventory = financial['slot_inputs']['inventory']
    owners = proof['owners']
    seen, account_margin, owned_margin = set(), Decimal(0), Decimal(0)
    for row in inventory:
        raw = row['positionAmt']
        if isinstance(raw, bool) or not isinstance(raw, (str, int, Decimal)):
            raise ValueError('hrs_margin_inventory_invalid')
        qty = exact(abs(Decimal(raw)), zero=True)
        if not qty:
            continue
        symbol = row['symbol']
        if symbol in seen or not isinstance(owners.get(symbol), str) or not owners[symbol]:
            raise ValueError('hrs_margin_owner_unknown')
        seen.add(symbol)
        margin = qty * exact(row['markPrice']) / exact(row['leverage'])
        account_margin += margin
        if owners[symbol] == 'hrs':
            owned_margin += margin
    if (seen != set(owners) or proof['account_symbols'] != sorted(seen)
            or proof['hrs_symbols'] != sorted(s for s in seen if owners[s] == 'hrs')):
        raise ValueError('hrs_margin_owner_partition_invalid')
    reserved = pending_margin(pending, account=financial.get('account_id'))
    account_reserved = pending_margin(account_pending, account=financial.get('account_id'))
    if any(row['strategy_id'] != 'hrs' for row in pending):
        raise ValueError('hrs_margin_pending_scope_invalid')
    own = {row['client_order_id']:dict(row) for row in pending}
    all_own = {row['client_order_id']:dict(row) for row in account_pending if row['strategy_id']=='hrs'}
    if own != all_own:
        raise ValueError('hrs_margin_pending_sources_mismatch')
    cap = min(exact(allocation)*exact(policy['multiplier']), exact(financial['effective_allocation']),
              exact(financial['account_equity'])*exact(policy['ratio_cap']), exact(policy['entry_ceiling']))
    quota_remaining = cap - owned_margin - reserved
    cash_remaining = available - account_reserved
    return dict(account_margin=account_margin, hrs_owned_margin=owned_margin,
        reserved_margin=reserved, margin_cap=cap, hrs_quota_remaining_margin=quota_remaining,
        account_available_balance=available, account_reserved_margin=account_reserved,
        account_remaining_margin=cash_remaining, remaining_margin=min(quota_remaining, cash_remaining))


def margin_limits(financial, proof, pending, account_pending, allocation, policy):
    """Return named Decimal quota/cash limits with scale-preserving arithmetic.

    remaining_margin is min(hrs_quota_remaining_margin,account_remaining_margin)
    and is the direct shared risk-assessor margin ceiling.
    """
    values=[exact(allocation),validate_cash_fields(financial),exact(financial['account_equity']),
        exact(financial['effective_allocation'])]+[exact(policy[k]) for k in ('multiplier','ratio_cap','entry_ceiling')]
    for row in financial['slot_inputs']['inventory']:
        raw=row['positionAmt']
        if isinstance(raw,bool) or not isinstance(raw,(str,int,Decimal)):
            raise ValueError('hrs_margin_inventory_invalid')
        qty=exact(Decimal(raw).copy_abs(),zero=True)
        values.append(qty)
        if qty:values.extend((exact(row['markPrice']),exact(row['leverage'])))
    values.extend(exact(row['margin']) for row in list(pending)+list(account_pending))
    with localcontext() as context:
        context.prec=_precision(values)
        return _margin_limits(financial,proof,pending,account_pending,allocation,policy)
