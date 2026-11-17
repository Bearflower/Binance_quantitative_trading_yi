"""Raw capital/account/state evidence for prospective HRS replay windows."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import os

TABLES = ('hrs_positions', 'hrs_blacklist', 'hrs_active_symbols')


async def _database_context(db, account):
    queries = [('''SELECT p.* FROM trading.strategy_risk_policy p
        JOIN trading.strategy_risk_active a USING(account_id,strategy_id,revision)
        WHERE p.account_id=$1 AND p.strategy_id='hrs' ''', (account,)),
        ("SELECT * FROM trading.hrs_loss_gate_state WHERE account_id=$1", (account,))]
    queries += [(f'SELECT * FROM hrs.{table}', ()) for table in TABLES]
    pool = getattr(db, 'pool', None)
    if pool is None:
        # An explicit incomplete flag prevents test/legacy DB adapters from
        # masquerading as an atomic database snapshot.
        return [await db.fetch_all(sql, *args) for sql, args in queries], False
    async with pool.acquire() as connection, connection.transaction(isolation='repeatable_read', readonly=True):
        return [await connection.fetch(sql, *args) for sql, args in queries], True


async def capture_financial_context(strategy):
    started = datetime.now(timezone.utc)
    account, allocation, pending, database = await asyncio.gather(
        strategy.binance_client.get_account_info(),
        strategy.capital_mgr.get_effective_margin_limit(),
        strategy.trading_executor.timeout_recovery.pending(),
        _database_context(strategy.db, os.getenv('TRADE_ACCOUNT_ID', 'default')))
    equity = account.get('totalMarginBalance') if isinstance(account, dict) else None
    if equity is None or not Decimal(str(equity)).is_finite() or Decimal(str(equity)) <= 0:
        raise ValueError('account_equity_missing_or_invalid')
    amount, source = allocation
    rows, coherent = database
    policies, ledger, *tables = rows
    if len(policies) != 1:
        raise ValueError('entry_policy_missing_or_ambiguous')
    available = datetime.now(timezone.utc)
    return dict(started_at=started, available_at=available, account_equity=str(equity),
        account_balances={key: account[key] for key in ('totalMarginBalance', 'totalWalletBalance',
            'totalUnrealizedProfit', 'updateTime') if key in account},
        allocation=str(amount) if amount is not None else None, allocation_source=source,
        entry_policy=dict(policies[0]), ledger_state=dict(ledger[0]) if len(ledger) == 1 else None,
        database_snapshot_coherent=coherent,
        replay_bootstrap_ready=coherent and len(ledger) == 1 and amount is not None,
        pending_timeout_entries=[dict(row) for row in pending],
        state_tables={name: {'rows': [dict(row) for row in table], 'captured_at': available,
                           'database_snapshot_coherent': coherent}
                      for name, table in zip(TABLES, tables)})
