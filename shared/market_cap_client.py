"""Consumers refresh once per batch; individual reads never call public providers."""
import math
import os
import time

import aiohttp
import structlog

from .market_cap import MarketCapProvenance, MarketCapService

log = structlog.get_logger()
REAL_SOURCES = ('coingecko', 'coinmarketcap', 'cache_hit', 'coingecko_cache')


class SharedMarketCapClient:
    def __init__(self, url, **kwargs):
        self.url = url.rstrip('/')
        self.session = None
        self.rows = {}
        self.retry_after = 0.0

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def request(self, route, symbols):
        if time.time() < self.retry_after:
            return {symbol:self.provenance(symbol) for symbol in symbols}
        if self.session is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))
        try:
            async with self.session.post(self.url+route, json={'symbols':list(dict.fromkeys(symbols))}) as response:
                response.raise_for_status()
                rows = await response.json()
            if not isinstance(rows, dict):
                raise ValueError('invalid shared cache response')
            self.rows.update(rows)
            self.retry_after = 0.0
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.warning('shared_market_cap_unavailable', error=type(exc).__name__)
            self.retry_after = time.time()+60
            for symbol in symbols:
                self.rows.setdefault(symbol, {'missing_reason':'shared_service_unavailable'})
        return {symbol:self.provenance(symbol) for symbol in symbols}

    def provenance(self, symbol):
        row = self.rows.get(symbol, {})
        observed = row.get('as_of', 0)
        real = (row.get('source') in REAL_SOURCES and row.get('provider') in ('coingecko','coinmarketcap')
                and row.get('asset_id') and math.isfinite(float(row.get('value',0)))
                and float(row.get('value',0)) > 0 and 0 <= time.time()-observed <= 3600)
        prov = MarketCapProvenance(
            value=float(row.get('value',0)) if real else 0,
            source=row['source'] if real else 'missing', as_of=observed,
            available_at=time.time(), is_proxy=False,
            missing_reason=None if real else row.get('missing_reason') or ('shared_service_unavailable' if time.time()<self.retry_after else 'not_preloaded_or_expired'))
        prov.provider = row.get('provider')
        prov.provider_asset_id = row.get('asset_id')
        return prov

    async def get_batch_market_caps(self, symbols):
        result = await self.request('/collect', symbols)
        return {s:p.value for s,p in result.items() if p.source in REAL_SOURCES}

    async def get_batch_market_caps_with_provenance(self, symbols, oi_map=None):
        return await self.request('/collect', symbols)

    async def get_market_cap_with_provenance(self, symbol, oi_usd=0, _volume_24h_usd=0):
        # Reload the durable shared cache, but never initiate public fetches here.
        return (await self.request('/read',[symbol]))[symbol]

    async def get_market_cap_with_fallback(self, symbol, oi_usd=0, volume_24h_usd=0):
        return (await self.get_market_cap_with_provenance(symbol)).value


def market_cap_from_env(**kwargs):
    url = os.getenv('MARKET_CAP_SERVICE_URL')
    return SharedMarketCapClient(url, **kwargs) if url else MarketCapService(**kwargs)
