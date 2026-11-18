"""Read-only signed exchange gate for live HRS and aggressive reservations."""
import asyncio
import hashlib
import json
import os
from decimal import Decimal

import asyncpg
from scripts.export_execution_facts import _build_client_from_env
from scripts.verify_ledger_risk_release import verify_exchange, verify_reservations

def row_hash(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str,
                                     separators=(',', ':')).encode()).hexdigest()

async def check():
    if os.environ.get('TRADE_ACCOUNT_ID') != 'default':
        raise RuntimeError('credential account binding mismatch')
    def env(name):
        value = os.getenv('DATABASE_' + name) or os.getenv('DB_' + name)
        if not value:
            raise RuntimeError('database configuration missing')
        return value
    client, code = _build_client_from_env()
    if code or client is None:
        raise RuntimeError('signed exchange client unavailable')
    pool = await asyncpg.create_pool(host=env('HOST'), port=int(env('PORT')),
                                    database=env('NAME'), user=env('USER'),
                                    password=env('PASSWORD'), min_size=1, max_size=1)
    try:
        async with pool.acquire() as conn:
            reservations = [dict(r) for r in await conn.fetch(
                "SELECT client_order_id,strategy_id,symbol,state,quantity FROM trading.entry_risk_reservation WHERE account_id='default' AND state<>'released'")]
            allocations = [dict(r) for r in await conn.fetch(
                "SELECT * FROM public.capital_allocation ORDER BY id")]
        positions = await client.get_position()
        orders = await client.get_open_orders()
        algos = await client.get_open_algo_orders()
        verify_exchange(positions, orders, algos)
        aggressive = [r for r in reservations if r['strategy_id'] == 'btc_eth_aggressive']
        await verify_reservations(aggressive, positions, client)
        hrs = [r for r in reservations if r['strategy_id'] == 'hrs']
        if len(aggressive) + len(hrs) != len(reservations):
            raise RuntimeError('unrecognized outstanding reservation')
        for row in hrs:
            if row['state'] != 'submitting':
                raise RuntimeError('HRS reservation not submitted')
            order = await client.get_order_by_client_id(row['symbol'], row['client_order_id'])
            if (not isinstance(order, dict) or order.get('symbol') != row['symbol']
                    or order.get('clientOrderId') != row['client_order_id']
                    or order.get('status') != 'FILLED' or order.get('reduceOnly') in (True, 'true')
                    or Decimal(str(order.get('origQty'))) != Decimal(str(row['quantity']))):
                raise RuntimeError('HRS signed entry mismatch')
            filled = Decimal(str(order.get('executedQty')))
            matching = [p for p in positions if p.get('symbol') == row['symbol']
                        and Decimal(str(p.get('positionAmt', '0'))) != 0]
            if len(matching) != 1 or not (0 < abs(Decimal(str(matching[0]['positionAmt']))) <= filled):
                raise RuntimeError('HRS occupied lot mismatch')
            amount = Decimal(str(matching[0]['positionAmt']))
            if order.get('side') != ('BUY' if amount > 0 else 'SELL'):
                raise RuntimeError('HRS direction mismatch')
        return {'ok': True, 'read_only': True,
                'position_exposure': sorted([{'symbol': p['symbol'], 'positionAmt': p['positionAmt'],
                                              'positionSide': p.get('positionSide')}
                                             for p in positions if Decimal(str(p.get('positionAmt', '0'))) != 0],
                                            key=lambda p: p['symbol']),
                'ordinary_orders_sha256': row_hash(orders),
                'algo_orders_sha256': row_hash(algos),
                'allocation_sha256': row_hash(allocations),
                'reservations_sha256': row_hash(reservations)}
    finally:
        await pool.close()
        await client.close()

if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(asyncio.wait_for(check(), 150)), sort_keys=True))
    except Exception as exc:
        print(json.dumps({'ok': False, 'reason': str(exc)}))
        raise SystemExit(2)
