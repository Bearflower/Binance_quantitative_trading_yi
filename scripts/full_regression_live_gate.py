"""Collect deployment evidence using GET requests and read-only database connections.

Run through ``docker exec -i ... python -`` so the existing service supplies its
actual account configuration. No credentials or raw account identifiers leave it.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import os
import re
import sqlite3
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def trace_reads(client, requests):
    original = client._request_once

    async def request(method, endpoint, params=None, signed=True):
        if method != "GET" or signed is not True:
            raise RuntimeError("deployment evidence permits signed GET only")
        actual_path = client._get_endpoint(endpoint)
        if not actual_path.startswith("/fapi/"):
            raise RuntimeError("expected classic account endpoint")
        result = await original(method, endpoint, params, signed)
        requests.append(dict(method=method, path=actual_path, signed=True))
        return result

    client._request_once = request


async def main_account():
    import asyncpg
    from scripts.export_execution_facts import _build_client_from_env
    from scripts.verify_ledger_risk_release import positive, verify_exchange

    account = os.environ.get("TRADE_ACCOUNT_ID")
    if account != "default":
        raise RuntimeError("unexpected main account binding")
    client, code = _build_client_from_env(read_only=True)
    if code or client is None or client.use_unified_account:
        raise RuntimeError("classic main account unavailable")
    requests = []
    trace_reads(client, requests)
    db = None
    try:
        positions = await client.get_position()
        orders = await client.get_open_orders()
        algos = await client.get_open_algo_orders()
        verify_exchange(positions, orders, algos)
        info = await client.get_account_info()
        equity = positive(info.get("accountEquity", info.get("totalMarginBalance")))
        wallets = await client._request("GET", "/fapi/v3/balance")
        aliases = [r.get("accountAlias") for r in wallets if r.get("asset") == "USDT"]
        if len(aliases) != 1 or not isinstance(aliases[0], str) or not aliases[0].strip():
            raise RuntimeError("missing unique account identity")

        def env(key):
            result = os.environ.get("DATABASE_" + key) or os.environ.get("DB_" + key)
            if not result:
                raise RuntimeError("missing database configuration: " + key)
            return result

        db = await asyncpg.connect(host=env("HOST"), port=int(env("PORT")),
            database=env("NAME"), user=env("USER"), password=env("PASSWORD"),
            server_settings={"default_transaction_read_only": "on"})
        async with db.transaction(isolation="repeatable_read", readonly=True):
            allocations = [dict(r) for r in await db.fetch("SELECT * FROM public.capital_allocation ORDER BY month")]
            policies = [dict(r) for r in await db.fetch("SELECT * FROM trading.strategy_risk_policy WHERE account_id=$1 ORDER BY strategy_id,revision", account)]
            active = [dict(r) for r in await db.fetch("SELECT * FROM trading.strategy_risk_active WHERE account_id=$1 ORDER BY strategy_id", account)]
        if not policies or not active:
            raise RuntimeError("missing active risk policy")
        exposure = sorted([{k: r.get(k) for k in ("symbol", "positionSide", "positionAmt", "entryPrice")}
            for r in positions if Decimal(str(r["positionAmt"])) != 0], key=lambda r: (r["symbol"], str(r["positionSide"])))
        return dict(account=account, account_mode="classic", account_identity_sha256=digest("classic:" + aliases[0]),
            allocation_sha256=digest(allocations), risk_policy_sha256=digest(dict(policies=policies, active=active)),
            equity=str(equity), positions_sha256=digest(positions), ordinary_orders_sha256=digest(orders),
            algo_orders_sha256=digest(algos), position_exposure=exposure, protected_positions=len(exposure),
            writes_report_metadata=False, requests=requests)
    finally:
        await client.close()
        if db is not None:
            await db.close()


async def grid_account():
    from scripts.grid_release_guard import inspect_grid
    return await inspect_grid(os.environ)


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("main", "grid"), required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--old-id", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.source) or not re.fullmatch(r"[a-f0-9]{64}", args.old_id):
        raise RuntimeError("invalid release identity")
    observed_at = time.time()
    result = asyncio.run(asyncio.wait_for(main_account() if args.kind == "main" else grid_account(), 100))
    result.update(source=args.source, container=args.container, old_id=args.old_id,
                  observed_at=observed_at, read_only=True, ok=True)
    print("LIVE_GATE_RESULT=" + json.dumps(result, sort_keys=True, default=str))


if __name__ == "__main__":
    cli()
