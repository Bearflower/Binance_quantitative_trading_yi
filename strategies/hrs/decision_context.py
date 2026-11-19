"""Raw capital/account/state evidence for prospective HRS replay windows."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import os
import json

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
    from .margin_budget import policy_margin_scope,account_cash_fields
    scope=policy_margin_scope(dict(policies[0]))
    cash=account_cash_fields(account,getattr(strategy.binance_client,'use_unified_account',False))
    available = datetime.now(timezone.utc)
    return dict(hrs_margin_scope=scope,**cash,runtime_slot_scope=SLOT_SCOPE, hrs_slot_inputs=slot_inputs, started_at=started, available_at=available, account_equity=str(equity),
        account_balances={key: account[key] for key in (
            ('totalMarginBalance', 'availableBalance', 'accountStatus') if getattr(strategy.binance_client, 'use_unified_account', False) is True
            else ('totalMarginBalance', 'totalWalletBalance', 'totalUnrealizedProfit', 'updateTime', 'availableBalance', 'accountStatus')) if key in account},
        account_equity_source=('papi/v1/account.accountEquity' if getattr(strategy.binance_client, 'use_unified_account', False) is True
                               else 'fapi/v2/account.totalMarginBalance'),
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
        effective = deep_merge(base, overlay) if applied else base
        if applied and (applied != requested or not isinstance(overlay, dict) or not overlay):
            raise ValueError()
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
