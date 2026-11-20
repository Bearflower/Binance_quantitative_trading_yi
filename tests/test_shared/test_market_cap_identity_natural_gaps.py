"""Three actual natural-scan gaps: verified identity is separate from fallback parity."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from services.market_cap.hub import MarketCapHub

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests/fixtures/market_cap'
EXPECTED = {
    'YBUSDT': ('YB', 'yield-basis', 38651, False, '0x01791f726b4103694969820be083196cc7c045ff'),
    'STRKUSDT': ('STRK', 'starknet', 22691, True, '0xca14007eff0db1f8135f4c25b34de49ab0d42766'),
    'NAORISUSDT': ('NAORIS', 'naoris', 35114, False, '0x1b379a79c91a540b2bcd612b4d713f31de1b80cc'),
}


def registry():
    return json.loads((ROOT / 'services/market_cap/assets.json').read_text())


@pytest.mark.parametrize('pair,expected', EXPECTED.items())
def test_natural_gap_identity_uses_exact_provider_ids_and_keeps_discrepant_fallback_disabled(pair, expected):
    assets = registry()
    assert pair in assets, f'{pair} is missing from verified registry'
    asset = assets[pair]
    symbol, cg_id, cmc_id, enabled, contract = expected
    assert (asset['symbol'], asset['cg_id'], asset['cmc_id'], asset['cmc_enabled']) == expected[:4]
    assert asset['contracts']['ethereum'].lower() == contract
    assert any('binance.com/en/support/announcement/detail/' in url for url in asset['evidence'])
    if pair == 'NAORISUSDT':
        assert asset['name'] == 'NaoX Protocol'
        assert 'rebrand' in asset['identity_notes'].lower()
    if not enabled:
        assert 'circulating' in asset['fallback_disabled_reason']


def test_existing_78_assets_and_disabled_fallbacks_are_unchanged():
    assets = registry()
    old = {key:value for key,value in assets.items() if key not in EXPECTED}
    assert len(old) == 78
    assert hashlib.sha256(json.dumps(old, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == 'aaf5cf903dbd150a830856d89ac7aad2fda2082a1bf2bdd8c478a3e6a14716a6'


def capture(provider):
    receipt = json.loads((FIXTURES / f'{provider}_natural_three_assets_20261120.json').read_text())
    assert receipt['http_status'] == 200
    assert hashlib.sha256(receipt['body_utf8'].encode()).hexdigest() == receipt['body_sha256']
    assert datetime.fromisoformat(receipt['headers_received_at']) <= datetime.fromisoformat(receipt['available_at'])
    return receipt, json.loads(receipt['body_utf8'])


@pytest.mark.parametrize('pair,expected', EXPECTED.items())
def test_paired_original_quotes_recompute_parity_and_freshness_before_enabling_fallback(pair, expected):
    asset = registry().get(pair)
    assert asset is not None
    cg_receipt, cg_body = capture('cg')
    cmc_receipt, cmc_body = capture('cmc')
    cg = next(row for row in cg_body if row['id'] == expected[1])
    cmc = next(row for row in cmc_body['data'] if row['id'] == expected[2])
    quote = next(row for row in cmc['quote'] if row['symbol'] == 'USD')
    assert cg['symbol'].upper() == cmc['symbol'] == expected[0]
    assert cmc['platform']['token_address'].lower() == expected[4]
    cap_diff = abs(cg['market_cap'] - quote['market_cap']) / cg['market_cap']
    supply_diff = abs(cg['circulating_supply'] - cmc['circulating_supply']) / cg['circulating_supply']
    cg_at, cmc_at = (datetime.fromisoformat(value.replace('Z', '+00:00'))
                     for value in (cg['last_updated'], quote['last_updated']))
    skew = abs((cg_at-cmc_at).total_seconds())
    for source_at, receipt in ((cg_at, cg_receipt), (cmc_at, cmc_receipt)):
        assert 0 <= (datetime.fromisoformat(receipt['available_at'])-source_at).total_seconds() <= 300
    assert skew <= 300
    comparison = asset['comparison']
    assert comparison['relative_difference'] == pytest.approx(cap_diff)
    assert comparison['circulating_supply_relative_difference'] == pytest.approx(supply_diff)
    assert comparison['provider_time_skew_seconds'] == skew
    assert comparison['max_relative_difference'] == .05
    assert comparison['max_provider_time_skew_seconds'] == 300
    assert comparison['coingecko_market_cap'] == cg['market_cap']
    assert comparison['coinmarketcap_market_cap'] == quote['market_cap']
    assert comparison['coingecko_circulating_supply'] == cg['circulating_supply']
    assert comparison['coinmarketcap_circulating_supply'] == cmc['circulating_supply']
    assert comparison['coingecko_observed_at'] == cg['last_updated']
    assert comparison['coinmarketcap_observed_at'] == quote['last_updated']
    assert asset['cmc_enabled'] is (cap_diff <= .05 and supply_diff <= .05)


def test_frozen_provider_contracts_and_official_binance_identities_agree():
    evidence = json.loads((FIXTURES / 'identity_natural_three_assets_20261120.json').read_text())
    exchange = json.loads((FIXTURES / 'binance_natural_three_assets_20261120.json').read_text())
    for pair, (symbol, cg_id, _, _, contract) in EXPECTED.items():
        row = next(row for row in exchange['symbols'] if row['symbol'] == pair)
        assert (row['baseAsset'], row['quoteAsset'], row['contractType'], row['underlyingType']) == (symbol, 'USDT', 'PERPETUAL', 'COIN')
        assert row['status'] == 'TRADING' and 'Crypto' in row['underlyingSubType']
        cg = evidence['coingecko'][cg_id]
        assert cg['status'] == 200 and cg['data']['id'] == cg_id
        assert cg['data']['symbol'].upper() == symbol
        assert cg['data']['platforms']['ethereum'].lower() == contract
    for symbol in ('YB', 'NAORIS'):
        alpha = next(row for row in evidence['binance_alpha']['data'] if row['symbol'] == symbol)
        cg_id = next(value[1] for value in EXPECTED.values() if value[0] == symbol)
        assert alpha['chainName'] == 'BSC'
        assert alpha['contractAddress'].lower() == evidence['coingecko'][cg_id]['data']['platforms']['binance-smart-chain'].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['coingecko', 'coinmarketcap'])
async def test_real_quote_replay_resolves_primary_and_refuses_discrepant_secondary(tmp_path, monkeypatch, provider):
    cg_receipt, cg_body = capture('cg')
    cmc_receipt, cmc_body = capture('cmc')
    received = max(datetime.fromisoformat(r['available_at']).timestamp() for r in (cg_receipt, cmc_receipt))
    monkeypatch.setattr('services.market_cap.hub.time.time', lambda: received)
    monkeypatch.delenv('COINGECKO_API_KEY', raising=False)
    monkeypatch.delenv('COINMARKETCAP_API_KEY', raising=False)
    calls = []
    class Session:
        def get(self, url, **kwargs):
            calls.append(kwargs['params'])
            assert kwargs['headers'] == {}
            if provider == 'coingecko':
                assert url == cg_receipt['url'] and kwargs['params'] == cg_receipt['request_params']
            else:
                assert url.endswith('/public-api/v3/cryptocurrency/quotes/latest')
                assert kwargs['params'] == {'id': '22691'}
            class Response:
                status = 200
                async def __aenter__(self): return self
                async def __aexit__(self, *args): pass
                async def json(self): return cg_body if provider == 'coingecko' else cmc_body
            return Response()
    hub = MarketCapHub(tmp_path / 'cache.sqlite', registry())
    hub.session = Session()
    if provider == 'coinmarketcap':
        hub.db.execute('INSERT INTO backoff VALUES (?,?)', ('coingecko', received+600))
        hub.db.commit()
    try:
        result = await hub.collect([*EXPECTED, 'UNKNOWNUSDT'])
        for pair, expected in EXPECTED.items():
            if provider == 'coingecko' or expected[3]:
                assert result[pair]['provider'] == provider
                assert result[pair]['value'] > 0 and result[pair]['is_proxy'] is False
                assert result[pair]['asset_id'] == str(expected[1 if provider == 'coingecko' else 2])
            else:
                assert result[pair]['source'] == 'missing'
                assert result[pair]['missing_reason'] == 'providers_unavailable_or_invalid'
        assert result['UNKNOWNUSDT']['missing_reason'] == 'asset_identity_unverified'
        assert len(calls) == 1
    finally:
        hub.close()
