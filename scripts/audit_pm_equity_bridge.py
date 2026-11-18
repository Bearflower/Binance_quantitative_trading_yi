"""Reconcile one independently valued PM equity observation, read-only.

This diagnostic handles USDT-only collateral without liabilities. A verified
point is evidence for a future bridge, not a period return or a transfer anchor.
"""
import argparse
import asyncio
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import time

from shared.readonly_client import ReadOnlyBinanceClient

AMOUNTS = ('totalWalletBalance', 'umUnrealizedPNL', 'cmUnrealizedPNL',
           'crossMarginBorrowed', 'crossMarginInterest', 'negativeBalance')
TOLERANCE_USD = Decimal('0.01')
MAX_SPAN_MS = 3000


def amount(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('nonfinite amount')
    return result


def audit_usdt_bridge(*, before, after, balances, index, started_ms, ended_ms):
    reasons = []
    result = dict(status='BLOCKED_DATA', reasons=reasons, started_ms=started_ms,
                  ended_ms=ended_ms, tolerance_usd=str(TOLERANCE_USD))
    if (type(started_ms) is not int or type(ended_ms) is not int
            or not 0 < ended_ms - started_ms <= MAX_SPAN_MS):
        reasons.append('observation_window_invalid')
        return result
    try:
        if not isinstance(balances, list) or not balances:
            raise ValueError('missing balances')
        assets = {}
        for row in balances:
            asset = row['asset']
            if not isinstance(asset, str) or not asset or asset in assets:
                raise ValueError('ambiguous balance asset')
            assets[asset] = [amount(row[field]) for field in AMOUNTS]
        wallet, um_pnl, cm_pnl, *_ = assets['USDT']
        if any(values[3:] != [0, 0, 0] for values in assets.values()):
            reasons.append('nonzero_pm_liability')
        if any(any(values) for asset, values in assets.items() if asset != 'USDT'):
            reasons.append('non_usdt_collateral_requires_separate_valuation')
        if not isinstance(index, list) or len(index) != 1 or index[0]['asset'] != 'USDT':
            raise ValueError('index identity mismatch')
        price = amount(index[0]['assetIndexPrice'])
        if price <= 0:
            raise ValueError('nonpositive asset index')
        index_ms = index[0]['time']
        if type(index_ms) is not int or not started_ms <= index_ms <= ended_ms:
            reasons.append('index_time_outside_observation_window')
        before_equity, after_equity = amount(before['actualEquity']), amount(after['actualEquity'])
        reconstructed = (wallet + um_pnl + cm_pnl) * price
        before_residual = before_equity - reconstructed
        after_residual = after_equity - reconstructed
        if max(abs(before_residual), abs(after_residual)) > TOLERANCE_USD:
            reasons.append('equity_bridge_residual_exceeds_tolerance')
        result.update(wallet_usdt=str(wallet), unrealized_usdt=str(um_pnl + cm_pnl),
                      usdt_usd_index=str(price), index_time_ms=index_ms,
                      actual_equity_before_usd=str(before_equity), actual_equity_after_usd=str(after_equity),
                      reconstructed_usd=str(reconstructed), residual_before_usd=str(before_residual),
                      residual_after_usd=str(after_residual))
    except (KeyError, ValueError, TypeError, InvalidOperation):
        reasons.append('exchange_equity_fields_unverified')
    if not reasons:
        result['status'] = 'VERIFIED_POINT'
    return result


async def capture(scope):
    if scope == 'default':
        if os.environ.get('TRADE_ACCOUNT_ID') != 'default':
            raise ValueError('run default scope inside a verified default PM service')
        key, secret = os.environ['BINANCE_API_KEY'], os.environ['BINANCE_API_SECRET']
    elif scope == 'grid_subaccount_pm':
        key, secret = os.environ['GRID_SUBACCOUNT_API_KEY'], os.environ['GRID_SUBACCOUNT_API_SECRET']
        if key == os.environ.get('BINANCE_API_KEY'):
            raise ValueError('grid credential must differ from default PM')
    else:
        raise ValueError('unsupported PM account scope')
    pm = ReadOnlyBinanceClient(api_key=key, api_secret=secret, instance_id='pm-equity-audit')
    pricing = ReadOnlyBinanceClient(api_key=key, api_secret=secret, base_url='https://api.binance.com',
                                   use_unified_account=False, instance_id='pm-index-audit')
    try:
        started = time.time_ns() // 1_000_000
        before = await pm._request('GET', '/papi/v1/account', signed=True)
        balances = await pm._request('GET', '/papi/v1/balance', signed=True)
        for attempt in range(2):
            index = await pricing._request('GET', '/sapi/v1/portfolio/asset-index-price',
                                           {'asset': 'USDT'}, signed=False)
            # The market endpoint can repeat its most recent tick. Give it one
            # chance to advance within the existing observation window.
            if (attempt == 0 and isinstance(index, list) and len(index) == 1
                    and isinstance(index[0], dict) and type(index[0].get('time')) is int
                    and index[0]['time'] < started):
                await asyncio.sleep(0.5)
                continue
            break
        after = await pm._request('GET', '/papi/v1/account', signed=True)
        ended = time.time_ns() // 1_000_000
    finally:
        await pm.close()
        await pricing.close()
    data = dict(before=before, after=after, balances=balances, index=index,
                started_ms=started, ended_ms=ended)
    hashes = {name: hashlib.sha256(json.dumps(data[name], sort_keys=True).encode()).hexdigest()
              for name in ('before', 'after', 'balances', 'index')}
    return dict(account_scope=scope, response_sha256=hashes, **audit_usdt_bridge(**data))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', required=True, choices=('default', 'grid_subaccount_pm'))
    print(json.dumps(asyncio.run(capture(parser.parse_args().scope)), sort_keys=True))
