"""Reconcile one independently valued PM equity observation, read-only.

This diagnostic handles USDT-only collateral without liabilities. A verified
point is evidence for a future bridge, not a period return or a transfer anchor.
"""
import argparse
import asyncio
import json
import os

from shared.readonly_client import ReadOnlyBinanceClient
from shared.accounting.pm_equity_bridge import observe_pm_equity


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
        return dict(account_scope=scope, **await observe_pm_equity(pm, pricing))
    finally:
        await pm.close()
        await pricing.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', required=True, choices=('default', 'grid_subaccount_pm'))
    print(json.dumps(asyncio.run(capture(parser.parse_args().scope)), sort_keys=True))
