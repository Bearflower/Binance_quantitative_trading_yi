#!/usr/bin/env python3
"""One-time PM account preparation after a fresh, flat read-only inventory.

Each write is sent once and followed by a read-only verification. A failed or
ambiguous response never causes an automatic repeat of a financial transfer.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import os
import sys

from shared.binance_api import BinanceClient
from grid_account_readonly import inventory


def d(value: object) -> Decimal:
    return Decimal(str(value))


def flat(state: dict) -> bool:
    return (state["account_status"] == "NORMAL"
            and state["nonzero_positions"] == []
            and state["ordinary_open_orders"] == 0
            and state["algo_open_orders"] == 0
            and state["spot_other_assets"] == [])


async def main_async() -> dict:
    first = await inventory()
    if (not flat(first) or first["dual_side_position"] is not True
            or d(first["pm_equity_usd"]) != 0
            or d(first["pm_available_usd"]) != 0
            or d(first["spot_usdt"].get("free", 0)) != 300
            or d(first["spot_usdt"].get("locked", 0)) != 0
            or int(first["eth_leverage"]) != 5):
        raise ValueError("dedicated account preconditions changed")
    key = os.environ["GRID_SUBACCOUNT_API_KEY"]
    secret = os.environ["GRID_SUBACCOUNT_API_SECRET"]
    spot = BinanceClient(api_key=key, api_secret=secret, use_unified_account=False,
                         base_url="https://api.binance.com", testnet=False)
    pm = BinanceClient(api_key=key, api_secret=secret, use_unified_account=True,
                       testnet=False)
    try:
        permissions = await spot._request("GET", "/sapi/v1/account/apiRestrictions")
        if (not isinstance(permissions, dict)
                or permissions.get("enablePortfolioMarginTrading") is not True):
            raise ValueError("API lacks PM trading permission")
        # Position mode applies to every UM symbol, which is safe only while flat.
        await pm._request_once("POST", "/papi/v1/um/positionSide/dual",
                               {"dualSidePosition": "false"})
        changed = await inventory()
        if not flat(changed) or changed["dual_side_position"] is not False:
            raise ValueError("one-way position mode not confirmed")
        await pm._request_once("POST", "/papi/v1/um/leverage",
                               {"symbol": "ETHUSDT", "leverage": 10})
        leveraged = await inventory()
        if (not flat(leveraged) or leveraged["dual_side_position"] is not False
                or int(leveraged["eth_leverage"]) != 10):
            raise ValueError("10x leverage not confirmed")
        if (d(leveraged["spot_usdt"].get("free", 0)) != 300
                or d(leveraged["pm_equity_usd"]) != 0):
            raise ValueError("wallet changed before transfer")
        if permissions.get("permitsUniversalTransfer") is not True:
            return {"mode_and_leverage_prepared": True,
                    "transfer_pending_permission": True,
                    "final": leveraged}
        transfer = await spot._request_once("POST", "/sapi/v1/asset/transfer",
                                            {"type": "MAIN_PORTFOLIO_MARGIN",
                                             "asset": "USDT", "amount": "300"})
        if not isinstance(transfer, dict) or not isinstance(transfer.get("tranId"), int):
            raise ValueError("transfer response unconfirmed; do not retry")
        final = await inventory()
        if (not flat(final) or final["dual_side_position"] is not False
                or int(final["eth_leverage"]) != 10
                or d(final["spot_usdt"].get("free", 0)) != 0
                or d(final["pm_equity_usd"]) < 299):
            raise ValueError("transfer balance not confirmed; do not retry")
        return {"transfer_id": transfer["tranId"], "final": final}
    finally:
        await spot.close()
        await pm.close()


def main() -> int:
    try:
        print(json.dumps(asyncio.run(main_async()), sort_keys=True))
    except Exception as exc:
        code = getattr(exc, "code", None)
        print("grid account preparation stopped: " + type(exc).__name__
              + (" code=" + str(code) if isinstance(code, int) else ""), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
