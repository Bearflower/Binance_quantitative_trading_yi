"""Bind four observed gaps to authoritative identities and paired public quotes."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from services.market_cap.hub import MarketCapHub


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests/fixtures/market_cap'
EXPECTED = {
    'SKYUSDT': ('SKY', 'Sky', 'sky', 33038, 'ethereum',
                '0x56072C95FAA701256059aa122697B133aDEd9279'),
    'INITUSDT': ('INIT', 'Initia', 'initia', 33120, 'initia', 'uinit'),
    'SENTUSDT': ('SENT', 'Sentient', 'sentient', 38868, 'ethereum',
                 '0x56A3BA04E95d34268A19b2a4474DC979baBDaf76'),
    'VELVETUSDT': ('VELVET', 'Velvet', 'velvet', 37298, 'binance-smart-chain',
                   '0x8b194370825E37b33373e74A41009161808C1488'),
}
ANNOUNCEMENTS = {
    'SKYUSDT': '4009f8bd8d85456189e9265d311b25de',
    'INITUSDT': '4e07a703c9da44709e22e79d0b4be452',
    'SENTUSDT': '5fe5a15ee7cb403fb09f1e9f9b4cd80c',
    'VELVETUSDT': '4f59bfc195ed4484ac810a9b8869fa86',
}


def registry():
    return json.loads((ROOT / 'services/market_cap/assets.json').read_text())


def capture(provider):
    data = json.loads((FIXTURES / f'{provider}_remaining_four_assets_20261120.json').read_text())
    assert data['http_status'] == 200
    assert hashlib.sha256(data['body_utf8'].encode()).hexdigest() == data['body_sha256']
    assert datetime.fromisoformat(data['headers_received_at']) <= datetime.fromisoformat(data['available_at'])
    return data, json.loads(data['body_utf8'])


@pytest.mark.parametrize('pair,expected', EXPECTED.items())
def test_verified_identity_prevents_symbol_collision_and_native_contract_guess(pair, expected):
    assets = registry()
    assert pair in assets, f'{pair} is missing from the verified registry'
    asset = assets[pair]
    symbol, name, cg_id, cmc_id, chain, contract = expected
    assert (asset['symbol'], asset['name'], asset['cg_id'], asset['cmc_id']) == expected[:4]
    assert asset['contracts'] == {chain: contract}
    assert f'https://www.binance.com/en/support/announcement/detail/{ANNOUNCEMENTS[pair]}' in asset['evidence']
    assert f'https://www.coingecko.com/en/coins/{cg_id}' in asset['evidence']
    assert f'https://coinmarketcap.com/currencies/{cg_id}/' in asset['evidence']
    if pair == 'SKYUSDT':
        assert 'Maker' in asset['identity_notes']
    if pair == 'INITUSDT':
        assert 'native' in asset['identity_notes'].lower()
        assert not contract.startswith('0x')
    info = json.loads((FIXTURES / 'binance_remaining_four_assets_20261120.json').read_text())
    exchange = next(row for row in info['symbols'] if row['symbol'] == pair)
    assert (exchange['baseAsset'], exchange['quoteAsset'], exchange['underlyingType']) == (symbol, 'USDT', 'COIN')
    assert 'Crypto' in exchange['underlyingSubType']


@pytest.mark.parametrize('pair', EXPECTED)
def test_enabled_fallback_has_same_identity_cap_supply_and_fresh_paired_evidence(pair):
    asset = registry().get(pair)
    assert asset is not None, f'{pair} is missing from the verified registry'
    cg_capture, cg_body = capture('cg')
    cmc_capture, cmc_body = capture('cmc')
    primary = next(row for row in cg_body if row['id'] == asset['cg_id'])
    secondary = next(row for row in cmc_body['data'] if row['id'] == asset['cmc_id'])
    assert primary['symbol'].upper() == secondary['symbol'] == asset['symbol']
    assert primary['name'] == secondary['name'] == asset['name']
    quote = next(row for row in secondary['quote'] if row['symbol'] == 'USD')
    cg_at = datetime.fromisoformat(primary['last_updated'].replace('Z', '+00:00'))
    cmc_at = datetime.fromisoformat(quote['last_updated'].replace('Z', '+00:00'))
    skew = abs((cg_at - cmc_at).total_seconds())
    for observed, receipt in ((cg_at, cg_capture), (cmc_at, cmc_capture)):
        assert 0 <= (datetime.fromisoformat(receipt['available_at']) - observed).total_seconds() <= 300
    cap_diff = abs(primary['market_cap'] - quote['market_cap']) / primary['market_cap']
    supply_diff = abs(primary['circulating_supply'] - secondary['circulating_supply']) / primary['circulating_supply']
    assert cap_diff <= 0.05 and supply_diff <= 0.05 and skew <= 300
    if secondary['platform'] is not None:
        assert secondary['platform']['token_address'].lower() == next(iter(asset['contracts'].values())).lower()
    assert asset['cmc_enabled'] is True
    comparison = asset['comparison']
    assert comparison['relative_difference'] == pytest.approx(cap_diff)
    assert comparison['circulating_supply_relative_difference'] == pytest.approx(supply_diff)
    assert comparison['provider_time_skew_seconds'] == skew
    assert comparison['max_relative_difference'] == 0.05
    assert comparison['max_provider_time_skew_seconds'] == 300
    assert comparison['coingecko_market_cap'] == primary['market_cap']
    assert comparison['coinmarketcap_market_cap'] == quote['market_cap']
    assert comparison['coingecko_circulating_supply'] == primary['circulating_supply']
    assert comparison['coinmarketcap_circulating_supply'] == secondary['circulating_supply']
    assert comparison['coingecko_observed_at'] == primary['last_updated']
    assert comparison['coinmarketcap_observed_at'] == quote['last_updated']


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,prefix', [('coingecko', 'cg'), ('coinmarketcap', 'cmc')])
async def test_real_captures_resolve_four_quotes_and_keep_unknown_missing(tmp_path, monkeypatch, provider, prefix):
    data, body = capture(prefix)
    received = datetime.fromisoformat(data['available_at']).timestamp()
    monkeypatch.setattr('services.market_cap.hub.time.time', lambda: received)
    monkeypatch.delenv('COINGECKO_API_KEY', raising=False)
    monkeypatch.delenv('COINMARKETCAP_API_KEY', raising=False)

    class Session:
        calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            assert url == ('https://api.coingecko.com/api/v3/coins/markets'
                           if provider == 'coingecko' else
                           'https://pro-api.coinmarketcap.com/public-api/v3/cryptocurrency/quotes/latest')
            assert kwargs['headers'] == {}
            assert kwargs['params'] == data['request_params']

            class Response:
                status = 200

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                async def json(self):
                    return body

            return Response()

    hub = MarketCapHub(tmp_path / 'cache.sqlite', registry())
    hub.session = Session()
    if provider == 'coinmarketcap':
        hub.db.execute('INSERT INTO backoff VALUES (?,?)', ('coingecko', received + 600))
        hub.db.commit()
    try:
        result = await hub.collect([*EXPECTED, 'UNVERIFIEDUSDT'])
        for pair, expected in EXPECTED.items():
            assert result[pair]['provider'] == provider
            assert result[pair]['asset_id'] == str(expected[2 if provider == 'coingecko' else 3])
            assert result[pair]['value'] > 0 and result[pair]['is_proxy'] is False
        assert result['UNVERIFIEDUSDT']['missing_reason'] == 'asset_identity_unverified'
        assert hub.session.calls == 1
    finally:
        hub.close()
