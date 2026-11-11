#!/usr/bin/env python3
"""Set dedicated flat PM ETHUSDT account to 10x exactly once."""
from __future__ import annotations

import asyncio
import json
import os
import sys

from shared.binance_api import BinanceClient
from grid_account_readonly import inventory


async def run() -> dict:
    before = await inventory()
    if (before["account_status"] != "NORMAL" or before["dual_side_position"] is not False
            or before["nonzero_positions"] or before["ordinary_open_orders"]
            or before["algo_open_orders"] or int(before["eth_leverage"]) != 5
            or float(before["pm_equity_usd"]) != 0):
        raise ValueError("leverage preconditions changed")
    client = BinanceClient(api_key=os.environ["GRID_SUBACCOUNT_API_KEY"],
                           api_secret=os.environ["GRID_SUBACCOUNT_API_SECRET"],
                           use_unified_account=True, testnet=False)
    try:
        response = await client._request_once("POST", "/papi/v1/um/leverage",
                                              {"symbol": "ETHUSDT", "leverage": 10})
        after = await inventory()
        if after["dual_side_position"] is not False or int(after["eth_leverage"]) != 10:
            raise ValueError("10x leverage not confirmed")
        return {"response_leverage": response.get("leverage"), "after": after}
    finally:
        await client.close()


def main() -> int:
    try:
        print(json.dumps(asyncio.run(run()), sort_keys=True))
    except Exception as exc:
        code = getattr(exc, "code", None)
        print("grid leverage stopped: " + type(exc).__name__
              + (" code=" + str(code) if isinstance(code, int) else ""), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
