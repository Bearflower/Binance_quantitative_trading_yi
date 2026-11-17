"""Read a reconciled strategy-equity projection inside the entry-policy lock.

The producer and order authorization share the account policy advisory lock.
This module only restricts new entries; it is never called by reduce-only exits.
"""
from datetime import timedelta
import os

from shared.accounting.income_integrity import require_income_integrity


class EquityGateDenied(RuntimeError):
    pass


async def enforce_equity_gate(connection, strategy, account, policy_revision):
    configured = {s.strip() for s in os.getenv('ENTRY_EQUITY_GATE_STRATEGIES', '').split(',') if s.strip()}
    if strategy not in configured:
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
