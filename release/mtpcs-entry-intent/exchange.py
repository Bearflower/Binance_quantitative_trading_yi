"""Read signed PM state for the MTPCS symbols before and after replacement."""
import asyncio
from decimal import Decimal
import json
import logging
import os

import structlog

from shared.binance_api import BinanceClient

SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT')


async def read_state():
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
    client = BinanceClient(os.environ['BINANCE_API_KEY'], os.environ['BINANCE_API_SECRET'],
                           use_unified_account=True)
    try:
        positions, orders, algos = await asyncio.gather(
            client.get_position(), client.get_open_orders(), client.get_open_algo_orders(),
        )
        result = dict(positions=[], orders=[], algos=[])
        for row in positions:
            if row['symbol'] in SYMBOLS and Decimal(row['positionAmt']):
                result['positions'].append([row['symbol'], row['positionSide'], row['positionAmt']])
        for row in orders:
            if row['symbol'] in SYMBOLS:
                result['orders'].append([row['symbol'], str(row['orderId']), row['status']])
        for row in algos:
            if row['symbol'] in SYMBOLS:
                result['algos'].append([row['symbol'], str(row['algoId']), row['side'],
                                        row['orderType'], row['quantity'], row['reduceOnly']])
        return {key: sorted(values) for key, values in result.items()}
    finally:
        await client.close()


if __name__ == '__main__':
    print(json.dumps(asyncio.run(read_state()), sort_keys=True))
