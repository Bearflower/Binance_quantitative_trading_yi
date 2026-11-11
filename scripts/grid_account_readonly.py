#!/usr/bin/env python3
"""Read-only dedicated grid subaccount inventory; never prints credentials."""
from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import os
import sys

from shared.binance_api import BinanceClient


def decimal(value: object) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("non-finite account value")
    return result


async def inventory() -> dict:
    key = os.environ.get("GRID_SUBACCOUNT_API_KEY", "")
    secret = os.environ.get("GRID_SUBACCOUNT_API_SECRET", "")
    if not key or not secret:
        raise ValueError("dedicated grid credentials missing")
    pm = BinanceClient(api_key=key, api_secret=secret, use_unified_account=True,
                       testnet=False)
    spot = BinanceClient(api_key=key, api_secret=secret, use_unified_account=False,
                         testnet=False, base_url="https://api.binance.com")
    try:
        # Read sequentially so every response is validated before continuing.
        account = await pm._request("GET", "/papi/v1/account")
        mode = await pm._request("GET", "/papi/v1/um/positionSide/dual")
        positions = await pm._request("GET", "/papi/v1/um/positionRisk")
        orders = await pm._request("GET", "/papi/v1/um/openOrders")
        algo = await pm._request("GET", "/papi/v1/um/algo/openAlgoOrders")
        config = await pm._request("GET", "/papi/v1/um/symbolConfig",
                                   {"symbol": "ETHUSDT"})
        spot_account = await spot._request("GET", "/api/v3/account")
        if (not isinstance(account, dict) or not isinstance(mode, dict)
                or not isinstance(positions, list) or not isinstance(orders, list)
                or not isinstance(algo, list) or not isinstance(config, list)
                or not isinstance(spot_account, dict)
                or not isinstance(spot_account.get("balances"), list)):
            raise ValueError("malformed Binance account inventory")
        nonzero = []
        for row in positions:
            if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
                raise ValueError("malformed PM position")
            amount = decimal(row.get("positionAmt"))
            if amount != 0:
                nonzero.append({"symbol": row["symbol"], "side": row.get("positionSide"),
                                "amount": str(amount)})
        balances = {}
        other_assets = []
        for row in spot_account["balances"]:
            if not isinstance(row, dict) or not isinstance(row.get("asset"), str):
                raise ValueError("malformed spot balance")
            free, locked = decimal(row.get("free")), decimal(row.get("locked"))
            if row["asset"] == "USDT":
                balances = {"free": str(free), "locked": str(locked)}
            elif free != 0 or locked != 0:
                other_assets.append(row["asset"])
        matching = [row for row in config if isinstance(row, dict)
                    and row.get("symbol") == "ETHUSDT"]
        if len(matching) != 1:
            raise ValueError("ETHUSDT configuration unavailable")
        return {
            "account_status": account.get("accountStatus"),
            "pm_equity_usd": str(decimal(account.get("accountEquity"))),
            "pm_available_usd": str(decimal(account.get("totalAvailableBalance"))),
            "dual_side_position": mode.get("dualSidePosition"),
            "nonzero_positions": nonzero,
            "ordinary_open_orders": len(orders),
            "algo_open_orders": len(algo),
            "eth_leverage": matching[0].get("leverage"),
            "eth_margin_type": matching[0].get("marginType"),
            "spot_usdt": balances,
            "spot_other_assets": sorted(other_assets),
        }
    finally:
        await pm.close()
        await spot.close()


def main() -> int:
    try:
        print(json.dumps(asyncio.run(inventory()), sort_keys=True))
    except Exception as exc:
        print("grid account read-only inventory failed: " + type(exc).__name__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
