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


class ProviderSession:
    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        status = self.status

        class Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self):
                from datetime import datetime, timezone
                return {'data': [{'id': 1, 'symbol': 'BTC', 'quote': [{
                    'symbol': 'USD', 'market_cap': 100,
                    'last_updated': datetime.now(timezone.utc).isoformat(),
                }]}]}

        response = Response()
        response.status = status
        return response


@pytest.mark.asyncio
@pytest.mark.parametrize('key', [None, '', '  ', '  test-cmc-secret  '])
async def test_cmc_authentication_selects_endpoint_and_caches_verified_quote(tmp_path, monkeypatch, key):
    monkeypatch.delenv('COINMARKETCAP_API_KEY', raising=False)
    monkeypatch.setenv('COINGECKO_API_KEY', 'separate-cg-secret')
    if key is not None:
        monkeypatch.setenv('COINMARKETCAP_API_KEY', key)
    hub = MarketCapHub(tmp_path/'cache.db', {'BTCUSDT': {'cg_id': 'bitcoin', 'cmc_id': 1, 'symbol': 'BTC'}})
    session = ProviderSession()
    hub.session = session
    # A persisted primary outage sends the entire collection through the fallback.
    hub.db.execute('INSERT INTO backoff VALUES (?, ?)', ('coingecko', time.time()+600))
    hub.db.commit()
    try:
        result = await hub.collect(['BTCUSDT', 'BTCUSDT', 'UNVERIFIEDUSDT'])
        assert result['BTCUSDT']['provider'] == 'coinmarketcap'
        assert result['BTCUSDT']['value'] == 100
        assert result['UNVERIFIEDUSDT']['source'] == 'missing'
        await hub.collect(['BTCUSDT'])
        assert len(session.calls) == 1
        url, kwargs = session.calls[0]
        authenticated = bool(key and key.strip())
        path = '/v3/cryptocurrency/quotes/latest' if authenticated else '/public-api/v3/cryptocurrency/quotes/latest'
        assert url == 'https://pro-api.coinmarketcap.com' + path
        assert kwargs['headers'] == ({'X-CMC_PRO_API_KEY': 'test-cmc-secret'} if authenticated else {})
        assert kwargs['params'] == {'id': '1'}
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_cmc_key_is_never_sent_to_coingecko(tmp_path, monkeypatch):
    monkeypatch.setenv('COINMARKETCAP_API_KEY', 'test-cmc-secret')
    monkeypatch.setenv('COINGECKO_API_KEY', 'separate-cg-secret')
    hub = MarketCapHub(tmp_path/'cache.db', {'BTCUSDT': {'cg_id': 'bitcoin', 'cmc_id': 1, 'symbol': 'BTC'}})
    session = ProviderSession()
    hub.session = session
    try:
        await hub.fetch('coingecko', ['BTCUSDT'])
        assert session.calls[0][1]['headers'] == {'x-cg-demo-api-key': 'separate-cg-secret'}
    finally:
        hub.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 429])
async def test_cmc_auth_failure_is_redacted_and_backoff_survives_restart(tmp_path, monkeypatch, capsys, status):
    monkeypatch.setenv('COINMARKETCAP_API_KEY', 'test-cmc-secret')
    path = tmp_path/'cache.db'
    registry = {'BTCUSDT': {'cg_id': 'bitcoin', 'cmc_id': 1, 'symbol': 'BTC'}}
    session = ProviderSession(status)
    hub = MarketCapHub(path, registry)
    hub.session = session
    assert await hub.fetch('coinmarketcap', ['BTCUSDT']) == {}
    assert hub.read(['BTCUSDT'])['BTCUSDT']['source'] == 'missing'
    hub.close()
    restarted = MarketCapHub(path, registry)
    restarted.session = session
    try:
        assert await restarted.fetch('coinmarketcap', ['BTCUSDT']) == {}
        assert len(session.calls) == 1
        assert session.calls[0][1]['headers'] == {'X-CMC_PRO_API_KEY': 'test-cmc-secret'}
        captured = capsys.readouterr()
        assert 'test-cmc-secret' not in captured.out + captured.err
    finally:
        restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('capture_file,symbols',[
    ('cg_three_assets_20261118.json',['ICNTUSDT','PIPPINUSDT','PIEVERSEUSDT']),
    ('cg_two_assets_20261119.json',['ZILUSDT','ZKPUSDT']),
    ('cg_zkc_20261119.json',['ZKCUSDT']),
])
async def test_natural_scan_missing_assets_use_verified_original_cg_rows(tmp_path,monkeypatch,capture_file,symbols):
    """Replay the actual public response at its real receipt time, without network."""
    import hashlib
    import json
    from pathlib import Path
    from datetime import datetime
    from unittest.mock import Mock
    capture=json.loads((Path('tests/fixtures/market_cap')/capture_file).read_text())
    assert hashlib.sha256(capture['body_utf8'].encode()).hexdigest()==capture['body_sha256']
    assert capture['http_status']==200
    body=json.loads(capture['body_utf8'])
    monkeypatch.setattr('services.market_cap.hub.time.time',lambda:datetime.fromisoformat(capture['available_at']).timestamp())
    registry=json.loads(Path('services/market_cap/assets.json').read_text())
    expected={row['symbol'].upper()+'USDT':row for row in body}
    class Response:
        status=200
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def json(self):return body
    session=Mock();session.get.return_value=Response()
    hub=MarketCapHub(tmp_path/'cache.db',registry);hub.session=session
    try:
        result=await hub.collect(symbols+['UNKNOWNUSDT'])
        for symbol in symbols:
            assert result[symbol]['source']=='cache_hit',result[symbol]
            assert result[symbol]['provider']=='coingecko'
            assert result[symbol]['asset_id']==expected[symbol]['id']
            assert result[symbol]['value']==expected[symbol]['market_cap']>0
            assert result[symbol]['is_proxy'] is False
            assert registry[symbol]['cmc_enabled'] is False and registry[symbol]['cmc_id'] is None
        assert result['UNKNOWNUSDT']['missing_reason']=='asset_identity_unverified'
        assert hub.stats['coingecko_calls']==1 and hub.stats['coinmarketcap_calls']==0
        assert set(session.get.call_args.kwargs['params']['ids'].split(','))=={row['id'] for row in body}
    finally:hub.close()
