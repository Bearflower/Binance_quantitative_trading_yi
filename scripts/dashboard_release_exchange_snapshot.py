"""Read-only exchange inventory for a dashboard-only deployment.

Run through ``docker exec -i trading_system-hrs python -`` so the existing
credential-bound container supplies its own environment. This script does not
place orders, alter reservations, or write database rows.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import sys

sys.path.insert(0, "/app")
from scripts.export_execution_facts import _build_client_from_env
from scripts.verify_ledger_risk_release import hash_rows


async def snapshot():
    client, code = _build_client_from_env()
    if code or client is None:
        raise RuntimeError("exchange client unavailable")
    try:
        positions = await client.get_position()
        orders = await client.get_open_orders()
        algos = await client.get_open_algo_orders()
        info = await client.get_account_info()
        if not all(isinstance(rows, list) for rows in (positions, orders, algos)):
            raise RuntimeError("exchange inventory unavailable")
        return {
            "ok": True, "read_only": True, "scope": "dashboard_container_only",
            "positions_sha256": hash_rows(positions),
            "ordinary_orders_sha256": hash_rows(orders),
            "algo_orders_sha256": hash_rows(algos),
            "position_exposure": sorted([
                {key: row.get(key) for key in ("symbol", "positionSide", "positionAmt", "entryPrice")}
                for row in positions if Decimal(str(row.get("positionAmt", "0"))) != 0
            ], key=lambda row: (str(row["symbol"]), str(row["positionSide"]))),
            "equity": str(info.get("accountEquity", info.get("totalMarginBalance"))),
        }
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        print(json.dumps(asyncio.run(snapshot()), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"ok": False, "read_only": True, "error": type(exc).__name__}))
        raise SystemExit(2)
