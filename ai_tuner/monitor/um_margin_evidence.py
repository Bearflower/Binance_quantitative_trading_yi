"""Exchange-reported USDT UM margin with inventory and split-total reconciliation."""

import asyncio
from decimal import Decimal, InvalidOperation
import hashlib
import json
import time


SOURCE = 'signed:/papi/v1/um/account'
MARGIN_FIELDS = ('positionInitialMargin', 'openOrderInitialMargin', 'initialMargin')


def _number(value):
    n = Decimal(str(value))
    if not n.is_finite():
        raise ValueError('invalid number')
    return n


def _split(row):
    position, order, total = (_number(row[k]) for k in MARGIN_FIELDS)
    if min(position, order) < 0 or position + order != total:
        raise ValueError('margin split does not reconcile')
    return position, order, total


def _inventory(rows):
    if not isinstance(rows, list):
        raise ValueError('inventory unavailable')
    result = {}
    for row in rows:
        quantity = _number(row['positionAmt'])
        if not quantity:
            continue
        symbol = row['symbol']
        if (not symbol.endswith('USDT') or symbol in result or row['positionSide'] != 'BOTH'
                or type(row['updateTime']) is not int or row['updateTime'] <= 0):
            raise ValueError('non-unique or unsupported position')
        result[symbol] = quantity, row['updateTime']
    return result


def blocked_margin(reason):
    return dict(status='BLOCKED_DATA', reason=reason, source=SOURCE,
                owned_position_initial_margin_usdt=None)


def reconcile_um_margin(positions, detail, owned_symbols):
    """Bind one account detail to the separately signed live position inventory."""
    try:
        rows, assets = detail['positions'], detail['assets']
        if not isinstance(assets, list) or not assets:
            raise ValueError('asset margins missing')
        live = _inventory(positions)
        if _inventory(rows) != live or not set(owned_symbols).issubset(live):
            raise ValueError('signed position inventories differ')

        summary = {}
        for asset in assets:
            key, split = asset['asset'], _split(asset)
            if key in summary or (key != 'USDT' and any(split)):
                raise ValueError('ambiguous or non-USDT margin')
            summary[key] = split

        by_symbol = {}
        for row in rows:
            split = _split(row)
            quantity = _number(row['positionAmt'])
            if not quantity and not any(split):
                continue
            symbol = row['symbol']
            if (not symbol.endswith('USDT') or symbol in by_symbol
                    or row['positionSide'] != 'BOTH'
                    or (quantity and split[0] <= 0) or (not quantity and split[0])):
                raise ValueError('unsupported or inconsistent margin row')
            by_symbol[symbol] = split

        total = tuple(sum((v[i] for v in by_symbol.values()), Decimal(0)) for i in range(3))
        if total != summary['USDT']:
            raise ValueError('USDT asset and position margins differ')
        owned = {key: str(by_symbol[key][0]) for key in sorted(owned_symbols)}
        return dict(status='VERIFIED', source=SOURCE,
                    owned_position_initial_margin_usdt=str(sum((by_symbol[key][0] for key in owned), Decimal(0))),
                    account_um_position_initial_margin_usdt=str(total[0]),
                    account_um_open_order_initial_margin_usdt=str(total[1]),
                    owned_position_initial_margin_by_symbol_usdt=owned)
    except (AttributeError, KeyError, TypeError, InvalidOperation):
        return blocked_margin('invalid_um_margin_fields')
    except ValueError as exc:
        return blocked_margin(str(exc))


async def read_um_margin(exchange, positions, owned_symbols, position_as_of_ms):
    path = '/papi/v1/um/account' if getattr(exchange, 'use_unified_account', True) else '/fapi/v2/account'
    try:
        detail = await asyncio.wait_for(
            exchange._request('GET', path, signed=True), timeout=5)
    except Exception as exc:
        return blocked_margin('um_margin_request:' + type(exc).__name__)
    observed = time.time_ns() // 1_000_000
    if not 0 <= observed - position_as_of_ms <= 15_000:
        return blocked_margin('signed_position_snapshot_stale')
    result = reconcile_um_margin(positions, detail, owned_symbols)
    result['source'] = 'signed:' + path
    if result['status'] == 'VERIFIED':
        result.update(as_of_ms=observed,
                      account_detail_sha256=hashlib.sha256(
                          json.dumps(detail, sort_keys=True).encode()).hexdigest())
    return result
