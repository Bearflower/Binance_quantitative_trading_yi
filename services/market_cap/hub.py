"""One batched collector with identity-keyed durable cache and provider backoff."""
import asyncio
import hashlib
import json
import math
import os
import sqlite3
import time
from datetime import datetime

import aiohttp
import structlog

log = structlog.get_logger()
MAX_AGE = 3600


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo else 0
    except (AttributeError, TypeError, ValueError):
        return 0


class MarketCapHub:
    def __init__(self, path, registry):
        self.registry = registry
        self.db = sqlite3.connect(str(path))
        self.db.execute('CREATE TABLE IF NOT EXISTS quotes (identity TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS backoff (provider TEXT PRIMARY KEY, until REAL NOT NULL)')
        self.lock = asyncio.Lock()
        self.negative = {}
        self.session = None
        self.stats = {'requests': 0, 'cache_hits': 0, 'coingecko_calls': 0,
                      'coinmarketcap_calls': 0, 'coingecko_429': 0, 'coinmarketcap_429': 0}

    def close(self):
        self.db.close()

    def identity(self, symbol):
        definition = json.dumps(self.registry[symbol], sort_keys=True)
        return symbol + ':' + hashlib.sha256(definition.encode()).hexdigest()

    def valid(self, symbol, row):
        asset = self.registry.get(symbol)
        if not asset or not isinstance(row, dict):
            return False
        provider = row.get('provider')
        if provider == 'coinmarketcap' and asset.get('cmc_enabled') is False:
            return False
        wanted = asset.get('cg_id') if provider == 'coingecko' else asset.get('cmc_id') if provider == 'coinmarketcap' else None
        try:
            value, observed = float(row['value']), float(row['observed_at'])
            return (wanted is not None and str(wanted) == str(row['asset_id'])
                    and math.isfinite(value) and value > 0
                    and 0 <= time.time()-observed <= MAX_AGE)
        except (KeyError, TypeError, ValueError):
            return False

    def cached(self, symbol):
        if symbol not in self.registry:
            return None
        stored = self.db.execute('SELECT payload FROM quotes WHERE identity=?', (self.identity(symbol),)).fetchone()
        row = json.loads(stored[0]) if stored else None
        return row if self.valid(symbol, row) else None

    def read(self, symbols):
        result = {}
        for symbol in symbols:
            row = self.cached(symbol)
            if row:
                result[symbol] = dict(row, source='cache_hit', as_of=row['observed_at'],
                                      available_at=time.time(), is_proxy=False, missing_reason=None)
            else:
                reason = ('asset_identity_unverified' if symbol not in self.registry
                          else self.negative.get(symbol, (0, 'quote_unavailable'))[1])
                result[symbol] = dict(value=0, source='missing', as_of=0, available_at=time.time(),
                                      is_proxy=False, missing_reason=reason, provider=None, asset_id=None)
        return result

    async def collect(self, symbols):
        async with self.lock:
            self.stats['requests'] += 1
            pending = [s for s in dict.fromkeys(symbols) if s in self.registry and not self.cached(s)
                       and self.negative.get(s, (0,))[0] <= time.time()]
            for provider in ('coingecko', 'coinmarketcap'):
                if not pending:
                    break
                received = await self.fetch(provider, pending)
                for symbol, row in received.items():
                    if symbol in pending and self.valid(symbol, row):
                        self.db.execute('INSERT OR REPLACE INTO quotes VALUES (?,?)',
                                        (self.identity(symbol), json.dumps(row, allow_nan=False)))
                self.db.commit()
                pending = [s for s in pending if not self.cached(s)]
            for symbol in pending:
                self.negative[symbol] = (time.time()+600, 'providers_unavailable_or_invalid')
            result = self.read(symbols)
            good = sum(row['source'] != 'missing' for row in result.values())
            self.stats['cache_hits'] += good
            log.info('shared_market_cap_coverage', requested=len(symbols), real=good,
                     unverified=sum(s not in self.registry for s in symbols), **self.stats)
            return result

    async def fetch(self, provider, symbols):
        if provider == 'coinmarketcap':
            symbols = [s for s in symbols if self.registry[s].get('cmc_enabled', True)]
        if not symbols:
            return {}
        backoff = self.db.execute('SELECT until FROM backoff WHERE provider=?', (provider,)).fetchone()
        if backoff and backoff[0] > time.time():
            return {}
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        headers = {}
        if provider == 'coingecko':
            if os.getenv('COINGECKO_API_KEY'):
                headers['x-cg-demo-api-key'] = os.environ['COINGECKO_API_KEY']
            url = 'https://api.coingecko.com/api/v3/coins/markets'
            params = dict(vs_currency='usd', ids=','.join(self.registry[s]['cg_id'] for s in symbols), per_page=250)
        else:
            url = 'https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/quotes/latest'
            params = dict(id=','.join(str(self.registry[s]['cmc_id']) for s in symbols))
        self.stats[provider+'_calls'] += 1
        try:
            async with self.session.get(url, params=params, headers=headers) as response:
                if response.status != 200:
                    wait = 600 if response.status == 429 else 60
                    if response.status == 429:
                        self.stats[provider+'_429'] += 1
                    self.db.execute('INSERT OR REPLACE INTO backoff VALUES (?,?)', (provider, time.time()+wait))
                    self.db.commit()
                    log.warning('market_cap_provider_failure', provider=provider, status=response.status)
                    return {}
                payload = await response.json()
            rows = payload if provider == 'coingecko' else payload.get('data', [])
            if not isinstance(rows, list):
                return {}
            mapped = {str(a['id']): a for a in rows if isinstance(a, dict) and a.get('id') is not None}
            result = {}
            for symbol in symbols:
                asset = self.registry[symbol]
                asset_id = asset['cg_id'] if provider == 'coingecko' else asset['cmc_id']
                row = mapped.get(str(asset_id), {})
                if str(row.get('symbol', '')).upper() != asset['symbol']:
                    continue
                quote = row if provider == 'coingecko' else next((q for q in row.get('quote', []) if q.get('symbol') == 'USD'), {})
                result[symbol] = dict(value=quote.get('market_cap'), observed_at=timestamp(quote.get('last_updated')),
                                      provider=provider, asset_id=str(asset_id), collected_at=time.time())
            return result
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            self.db.execute('INSERT OR REPLACE INTO backoff VALUES (?,?)', (provider, time.time()+60))
            self.db.commit()
            log.warning('market_cap_provider_exception', provider=provider, error=type(exc).__name__)
            return {}
