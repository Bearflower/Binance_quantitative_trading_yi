import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from services.market_cap.hub import MarketCapHub


def quote(value=100, age=0):
    return dict(value=value, observed_at=time.time()-age, provider='coinmarketcap', asset_id='1')


@pytest.mark.asyncio
async def test_fallback_is_batched_shared_and_survives_restart(tmp_path):
    registry={'BTCUSDT': {'cg_id':'bitcoin','cmc_id':1,'symbol':'BTC'}}
    hub=MarketCapHub(tmp_path/'cache.db',registry)
    hub.fetch=AsyncMock(side_effect=[{}, {'BTCUSDT':quote()}])
    result=await hub.collect(['BTCUSDT','NVDAUSDT'])
    assert result['BTCUSDT']['provider']=='coinmarketcap'
    assert result['NVDAUSDT']['missing_reason']=='asset_identity_unverified'
    await hub.collect(['BTCUSDT'])
    assert hub.fetch.await_count==2
    hub.close()
    second=MarketCapHub(tmp_path/'cache.db',registry)
    second.fetch=AsyncMock()
    assert (await second.collect(['BTCUSDT']))['BTCUSDT']['value']==100
    second.fetch.assert_not_awaited()
    second.close()


@pytest.mark.asyncio
async def test_negative_cache_stops_repeated_misses(tmp_path):
    hub=MarketCapHub(tmp_path/'cache.db',{'BTCUSDT': {'cg_id':'bitcoin','cmc_id':1,'symbol':'BTC'}})
    hub.fetch=AsyncMock(return_value={})
    await asyncio.gather(*(hub.collect(['BTCUSDT']) for _ in range(8)))
    assert hub.fetch.await_count==2
    hub.close()


@pytest.mark.asyncio
async def test_stale_nonfinite_and_wrong_identity_never_enter_cache(tmp_path):
    hub=MarketCapHub(tmp_path/'cache.db',{'BTCUSDT': {'cg_id':'bitcoin','cmc_id':1,'symbol':'BTC'}})
    for bad in [quote(age=7200),quote(float('inf')),dict(quote(),asset_id='2')]:
        hub.fetch=AsyncMock(return_value={'BTCUSDT':bad})
        hub.negative.clear()
        assert (await hub.collect(['BTCUSDT']))['BTCUSDT']['source']=='missing'
    hub.close()


@pytest.mark.asyncio
async def test_registry_change_invalidates_previous_asset_cache(tmp_path):
    path=tmp_path/'cache.db'
    hub=MarketCapHub(path,{'BTCUSDT': {'cg_id':'bitcoin','cmc_id':1,'symbol':'BTC'}})
    hub.fetch=AsyncMock(side_effect=[{}, {'BTCUSDT':quote()}])
    await hub.collect(['BTCUSDT']);hub.close()
    changed=MarketCapHub(path,{'BTCUSDT': {'cg_id':'different','cmc_id':2,'symbol':'BTC'}})
    assert changed.read(['BTCUSDT'])['BTCUSDT']['source']=='missing'
    changed.close()


@pytest.mark.asyncio
async def test_429_backoff_is_persisted_without_repeated_calls(tmp_path):
    class Response:
        status=429
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class Session:
        calls=0
        def get(self,*args,**kwargs):
            self.calls+=1
            return Response()
    hub=MarketCapHub(tmp_path/'cache.db',{'BTCUSDT': {'cg_id':'bitcoin','cmc_id':1,'symbol':'BTC'}})
    session=Session();hub.session=session
    await hub.fetch('coingecko',['BTCUSDT'])
    await hub.fetch('coingecko',['BTCUSDT'])
    assert session.calls==1
    hub.close()
    restarted=MarketCapHub(tmp_path/'cache.db',hub.registry);restarted.session=session
    await restarted.fetch('coingecko',['BTCUSDT'])
    assert session.calls==1
    restarted.close()


@pytest.mark.asyncio
async def test_http_client_reads_never_initiate_provider_fetch(tmp_path):
    from aiohttp.test_utils import TestClient,TestServer
    from services.market_cap.main import create_app
    from shared.market_cap_client import SharedMarketCapClient
    app=create_app(tmp_path/'cache.db',{'BTCUSDT': {'cg_id':'bitcoin','cmc_id':1,'symbol':'BTC'}})
    app['hub'].fetch=AsyncMock(side_effect=[{}, {'BTCUSDT':quote()}])
    server=TestClient(TestServer(app));await server.start_server()
    client=SharedMarketCapClient(str(server.make_url('/')).rstrip('/'))
    try:
        assert (await client.get_market_cap_with_provenance('BTCUSDT')).source=='missing'
        app['hub'].fetch.assert_not_awaited()
        await client.get_batch_market_caps(['BTCUSDT'])
        cap=await client.get_market_cap_with_provenance('BTCUSDT')
        assert cap.value==100 and cap.provider=='coinmarketcap' and cap.provider_asset_id=='1'
        assert app['hub'].fetch.await_count==2
    finally:await client.close();await server.close()


@pytest.mark.asyncio
async def test_comparison_failed_secondary_cannot_be_used(tmp_path):
    hub=MarketCapHub(tmp_path/'cache.db',{'BTCUSDT': {'cg_id':'bitcoin','cmc_id':1,'symbol':'BTC','cmc_enabled':False}})
    assert not hub.valid('BTCUSDT',quote())
    assert await hub.fetch('coinmarketcap',['BTCUSDT'])=={}
    hub.close()


@pytest.mark.asyncio
async def test_shared_service_outage_does_not_serialize_symbol_timeouts():
    from unittest.mock import MagicMock
    import aiohttp
    from shared.market_cap_client import SharedMarketCapClient
    client=SharedMarketCapClient('http://unavailable')
    client.session=MagicMock()
    client.session.post.side_effect=aiohttp.ClientError('unavailable')
    for symbol in ['BTCUSDT','ETHUSDT','AAVEUSDT']:
        cap=await client.get_market_cap_with_provenance(symbol)
        assert cap.source=='missing' and cap.missing_reason=='shared_service_unavailable'
    assert client.session.post.call_count==1
