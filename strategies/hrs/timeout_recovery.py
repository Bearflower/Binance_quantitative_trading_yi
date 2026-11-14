"""Recover a timed-out HRS entry using its own entry and exit fill quantities."""
import asyncio
import json
from decimal import Decimal
from uuid import uuid4

from shared.binance_api import BinanceAPIError

PREFIX = 'hrs-timeout:'
TERMINAL = {'FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}


class PendingReconciliation(Exception):
    pass


class TimeoutRecovery:
    def __init__(self, db, api):
        self.db = db
        self.api = api

    async def begin(self, symbol, order_id, direction, last_fill, atr, entry_mode, exit_ratios=None):
        key = f'{PREFIX}{symbol}:{order_id}'
        data = dict(symbol=symbol, order_id=str(order_id), direction=direction,
                    last_fill=str(last_fill), atr=atr, entry_mode=entry_mode,
                    exit_ratios=[str(r) for r in exit_ratios] if exit_ratios is not None else None)
        await self.db.execute(
            'INSERT INTO hrs.hrs_meta (key,value) VALUES ($1,$2) ON CONFLICT (key) DO NOTHING',
            key, json.dumps(data))
        row = await self.db.fetch_one('SELECT value FROM hrs.hrs_meta WHERE key=$1', key)
        return key, json.loads(row['value'])

    async def pending(self):
        return await self.db.fetch_all('SELECT key,value FROM hrs.hrs_meta WHERE key LIKE $1', PREFIX + '%')

    async def finish(self, key):
        await self.db.execute('DELETE FROM hrs.hrs_meta WHERE key=$1', key)

    async def entry_final(self, data):
        symbol, oid = data['symbol'], int(data['order_id'])
        try:
            order = await self.api.cancel_order(symbol, oid)
        except Exception:
            order = None
        for attempt in range(4):
            if (order and str(order.get('orderId')) == str(oid)
                    and order.get('symbol') == symbol and order.get('status') in TERMINAL
                    and order.get('executedQty') is not None):
                filled = Decimal(str(order['executedQty']))
                if filled.is_finite() and filled >= Decimal(data['last_fill']):
                    return order, filled
            if attempt < 3:
                if attempt:
                    await asyncio.sleep(0.2)
                order = await self.api.get_order(symbol, oid)
        raise PendingReconciliation(f'{symbol} {oid}: entry fill unconfirmed')

    async def visible_quantity(self, data):
        for attempt in range(3):
            positions = await self.api.get_position(data['symbol'])
            for pos in positions:
                if pos.get('symbol') == data['symbol']:
                    amount = Decimal(str(pos['positionAmt']))
                    signed = amount if data['direction'] == 'long' else -amount
                    if signed > 0:
                        return signed
            if attempt < 2:
                await asyncio.sleep(0.2)
        raise PendingReconciliation('entry fill not yet visible in position')

    async def reconcile(self, key, data):
        order, filled = await self.entry_final(data)
        if filled == 0:
            await self.finish(key)
            return None
        filled -= Decimal(data.get('previously_closed', '0'))
        if filled <= 0:
            raise PendingReconciliation('inconsistent cumulative exit fills')
        side = 'SELL' if data['direction'] == 'long' else 'BUY'
        client_id = data.get('close_id')
        if client_id:
            close = await self.api.get_order_by_client_id(data['symbol'], client_id)
        else:
            visible = await self.visible_quantity(data)
            client_id = 'hrc' + uuid4().hex
            # Only the worker that reserves the ID may submit this exit.
            reserved = await self.db.fetch_one(
                "UPDATE hrs.hrs_meta SET value=$2 WHERE key=$1 AND value::jsonb=$3::jsonb RETURNING key",
                key, json.dumps(dict(data, close_id=client_id)), json.dumps(data))
            if not reserved:
                raise PendingReconciliation('exit already reserved')
            try:
                close = await self.api.place_order(
                    symbol=data['symbol'], side=side, order_type='MARKET',
                    quantity=min(visible, filled), client_order_id=client_id, reduceOnly='true')
            except BinanceAPIError as exc:
                # A definitive rejection can leave the full fill for protection.
                if exc.code not in {-2022, -2019, -4164}:
                    raise
                close = dict(clientOrderId=client_id, symbol=data['symbol'], executedQty='0', status='REJECTED')
                await self.db.execute('UPDATE hrs.hrs_meta SET value=$2 WHERE key=$1',
                                      key, json.dumps(dict(data, close_id=client_id, rejected=True)))
        if data.get('rejected'):
            close = dict(clientOrderId=client_id, symbol=data['symbol'], executedQty='0', status='REJECTED')
        if not close or close.get('status') not in TERMINAL:
            close = await self.api.get_order_by_client_id(data['symbol'], client_id)
        if (not close or close.get('status') not in TERMINAL or close.get('symbol') != data['symbol']
                or close.get('clientOrderId') != client_id or close.get('executedQty') is None):
            raise PendingReconciliation('exit awaiting exchange confirmation')
        closed = Decimal(str(close['executedQty']))
        if not closed.is_finite() or not 0 <= closed <= filled:
            raise PendingReconciliation('invalid exit fill')
        if closed == filled:
            await self.finish(key)
            return None
        visible = await self.visible_quantity(data)
        if visible < filled - closed:
            raise PendingReconciliation('residual position and fills disagree')
        if visible > filled - closed:
            # closePosition SL would cover other holdings. Only retry the owned remainder.
            if closed > 0:
                next_data = {k: v for k, v in data.items() if k not in ('close_id', 'rejected')}
                next_data['previously_closed'] = str(Decimal(data.get('previously_closed', '0')) + closed)
                await self.db.execute(
                    "UPDATE hrs.hrs_meta SET value=$2 WHERE key=$1 AND value::jsonb->>'close_id'=$3",
                    key, json.dumps(next_data), client_id)
            raise PendingReconciliation('shared position: residual cannot use a whole-position stop')
        return dict(order, executedQty=str(filled - closed), recovery_key=key)
