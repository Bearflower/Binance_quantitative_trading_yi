"""Read a reconciled strategy-equity projection inside the entry-policy lock.

The producer and order authorization share the account policy advisory lock.
This module only restricts new entries; it is never called by reduce-only exits.
"""
from datetime import timedelta
import os

from shared.accounting.income_integrity import require_income_integrity


class EquityGateDenied(RuntimeError):
    pass


def _configured(strategy):
    configured = {s.strip() for s in os.getenv('ENTRY_EQUITY_GATE_STRATEGIES', '').split(',') if s.strip()}
    return strategy in configured


async def enforce_equity_gate(connection, strategy, account, policy_revision, *, submitting_client_id=None):
    if not _configured(strategy):
        return
    await require_income_integrity(connection, account)
    row = await connection.fetchrow('''SELECT *, clock_timestamp() AS checked_at
        FROM trading.strategy_equity_gate WHERE account_id=$1 AND strategy_id=$2''', account, strategy)
    if row is None or row['ready'] is not True or row['allowed'] is not True:
        raise EquityGateDenied('equity_gate_not_ready_or_latched')
    if row['policy_revision'] != policy_revision:
        raise EquityGateDenied('equity_gate_policy_revision_changed')
    as_of, expiry, now = row['as_of'], row['valid_until'], row['checked_at']
    if not (as_of <= now < expiry and timedelta(0) < expiry-as_of <= timedelta(seconds=120)):
        raise EquityGateDenied('equity_gate_stale_or_invalid')
    newer = await connection.fetchval('''SELECT EXISTS(SELECT 1 FROM trading.entry_risk_reservation
        WHERE account_id=$1 AND strategy_id=$2 AND state='submitting'
          AND (updated_at IS NULL OR updated_at>$3)
          AND client_order_id IS DISTINCT FROM $4::text)''',
        account, strategy, as_of, submitting_client_id)
    if newer:
        raise EquityGateDenied('equity_gate_unreconciled_submission')


async def invalidate_after_submission(connection, strategy, account):
    """Called on the held policy connection before committing an HTTP attempt.

    This is a consumer invalidation, not a new equity observation. Preserve the
    original valuation identity and require a later producer reconciliation.
    """
    if not _configured(strategy):
        return
    await connection.execute('''UPDATE trading.strategy_equity_gate
        SET ready=false,allowed=false,valid_until=LEAST(valid_until,clock_timestamp()),
            reasons=COALESCE(reasons,'[]'::jsonb) || '["entry_submission_requires_equity_refresh"]'::jsonb,
            updated_at=clock_timestamp()
        WHERE account_id=$1 AND strategy_id=$2''', account, strategy)
