"""Append a reviewed finite-history acceptance or revocation, never rewrite history.

Run with ``python -m scripts.activate_history_acceptance``. The CLI defaults to
validation only and reads PostgreSQL connection settings from standard PG*
environment variables. ``--apply`` is required to persist the reviewed payload.
This tool does not certify exchange evidence or deployed consumer versions;
those release preflight checks must finish before an enabled receipt is applied.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import re

from shared.accounting.history_tolerance import acceptance_digest, validate_history_acceptance
from shared.accounting.allocation_provenance import allocation_anchor


class AcceptanceActivationError(ValueError):
    """An approval cannot be applied to the current administrator-owned state."""



def _matches(row, revision, digest):
    if row is None:
        return revision == 0 and digest is None
    return row['revision'] == revision and row['receipt_sha256'] == digest


def _preserve_observed_history(previous, candidate):
    """A new approval is not permission to erase a known economic observation."""
    if allocation_anchor(previous.payload) != allocation_anchor(candidate.payload):
        raise AcceptanceActivationError('history_acceptance_allocation_anchor_changed')
    for key in ('exchange_uid_sha256', 'classic_flat_evidence_sha256', 'classic_cutover_ms'):
        if previous.payload[key] != candidate.payload[key]:
            raise AcceptanceActivationError('history_acceptance_baseline_changed')
    if (previous.classic_flat_at != candidate.classic_flat_at
            or previous.classic_open_equity != candidate.classic_open_equity):
        raise AcceptanceActivationError('history_acceptance_baseline_changed')
    points = {p['as_of_ms']: p for p in candidate.payload.get('classic_known_equity_points', [])}
    for old in previous.payload.get('classic_known_equity_points', []):
        if points.get(old['as_of_ms']) != old:
            raise AcceptanceActivationError('history_acceptance_known_equity_point_removed_or_changed')


async def append_acceptance(conn, payload, expected_revision, expected_sha, *, dry_run=False):
    """Compare-and-append under the same locks used by AI publication and HRS.

    The caller supplies the last reviewed revision/hash (0/None initially).
    Repeating the identical latest receipt is idempotent, including a retry
    carrying its original parent CAS. A superseded receipt cannot be replayed.
    """
    payload = copy.deepcopy(payload)
    if (type(expected_revision) is not int or expected_revision < 0
            or (expected_revision == 0 and expected_sha is not None)
            or (expected_revision > 0 and (not isinstance(expected_sha, str)
                or re.fullmatch(r'[0-9a-f]{64}', expected_sha) is None))):
        raise AcceptanceActivationError('history_acceptance_compare_and_swap_input_invalid')
    if not isinstance(payload, dict):
        raise AcceptanceActivationError('history_acceptance_payload_invalid')
    digest = acceptance_digest(payload)
    account = payload.get('account_id')
    row = dict(account_id=account, revision=payload.get('revision'), payload=payload, receipt_sha256=digest)
    async with conn.transaction(isolation='read_committed'):
        if await conn.fetchval('SELECT current_user') != 'trading_migration_admin':
            raise AcceptanceActivationError('history_acceptance_administrator_required')
        # Validate before deriving lock identities; repeat with the post-wait DB clock.
        validate_history_acceptance(row, account, now=await conn.fetchval('SELECT clock_timestamp()'))
        await conn.execute("SET LOCAL lock_timeout='5s'")
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))',
                           'classic-account-risk-v1:'+account)
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,7183))', account)
        now = await conn.fetchval('SELECT clock_timestamp()')
        candidate = validate_history_acceptance(row, account, now=now)
        if candidate is not None:
            allocation_anchor(candidate.payload)
        latest = await conn.fetchrow('SELECT account_id,revision,payload,receipt_sha256 '
            'FROM trading.historical_risk_acceptance WHERE account_id=$1 ORDER BY revision DESC LIMIT 1',account)
        if latest and latest['receipt_sha256'] == digest:
            if not _matches(latest,expected_revision,expected_sha):
                parent = await conn.fetchrow('SELECT revision,receipt_sha256 FROM trading.historical_risk_acceptance '
                    'WHERE account_id=$1 AND revision=$2',account,expected_revision) if expected_revision else None
                if (payload['revision'] != expected_revision+1 or not _matches(parent,expected_revision,expected_sha)):
                    raise AcceptanceActivationError('history_acceptance_compare_and_swap_failed')
            return dict(status='already_applied',account_id=account,revision=payload['revision'],receipt_sha256=digest)
        if (not _matches(latest,expected_revision,expected_sha)
                or payload['revision'] != expected_revision+1):
            raise AcceptanceActivationError('history_acceptance_compare_and_swap_failed')
        previous_row = await conn.fetchrow('SELECT account_id,revision,payload,receipt_sha256 '
            "FROM trading.historical_risk_acceptance WHERE account_id=$1 AND payload->>'enabled'='true' "
            'ORDER BY revision DESC LIMIT 1',account)
        if previous_row:
            previous = validate_history_acceptance(previous_row,account,now=now)
            if previous is None or payload['exchange_uid_sha256'] != previous.exchange_uid_sha256:
                raise AcceptanceActivationError('history_acceptance_baseline_changed')
            if candidate is not None:
                _preserve_observed_history(previous,candidate)
        if not isinstance(payload.get('approval_ref'),str) or not payload['approval_ref'].strip():
            raise AcceptanceActivationError('history_acceptance_approval_missing')
        if dry_run:
            return dict(status='dry_run',account_id=account,revision=payload['revision'],receipt_sha256=digest)
        await conn.execute('INSERT INTO trading.historical_risk_acceptance '
            '(account_id,revision,payload,receipt_sha256) VALUES ($1,$2,$3::jsonb,$4)',
            account,payload['revision'],json.dumps(payload,sort_keys=True,allow_nan=False),digest)
        await conn.execute("""UPDATE trading.strategy_equity_gate
            SET ready=false,allowed=false,valid_until=LEAST(valid_until,clock_timestamp()),
                reasons=CASE WHEN reasons @> '["history_acceptance_changed"]'::jsonb THEN reasons
                    ELSE reasons || '["history_acceptance_changed"]'::jsonb END,
                updated_at=clock_timestamp()
            WHERE account_id=$1 AND strategy_id='hrs'""",account)
        return dict(status='applied',account_id=account,revision=payload['revision'],receipt_sha256=digest)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--payload',type=Path,required=True,help='Reviewed JSON payload file')
    result.add_argument('--expected-revision',type=int,required=True)
    result.add_argument('--expected-sha',required=True,help='Expected current receipt SHA256; use none for revision 0')
    result.add_argument('--apply',action='store_true',help='Persist receipt and invalidate the old HRS gate')
    return result


async def _main(args):
    import asyncpg
    payload = json.loads(args.payload.read_text())
    connection = await asyncpg.connect(host=os.environ.get('PGHOST'),
        port=int(os.environ.get('PGPORT','5432')),user=os.environ.get('PGUSER'),
        password=os.environ.get('PGPASSWORD'),database=os.environ.get('PGDATABASE'),
        timeout=10)
    try:
        return await append_acceptance(connection,payload,args.expected_revision,
            None if args.expected_sha == 'none' else args.expected_sha,dry_run=not args.apply)
    finally:
        await connection.close()


def main():
    args = parser().parse_args()
    try:
        result = asyncio.run(_main(args))
    except Exception as exc:
        # No connection string, password, or supplied payload is printed.
        print(json.dumps(dict(status='error',error_type=type(exc).__name__,
            reason=str(exc) if isinstance(exc,ValueError) else 'activation_failed')))
        return 1
    print(json.dumps(result,sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
