"""Eight observed gaps use verified identities; provider responses are synthetic."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services.market_cap.hub import MarketCapHub

ROOT = Path(__file__).resolve().parents[2]
NOW = 1800000000
EXPECTED = {
    'ACEUSDT': ('ACE', 'Fusionist', 'endurance', 28674, False, 'binance-smart-chain',
                '0xc27a719105a987b4c34116223cae8bd8f4b5def4'),
    'AIGENSYNUSDT': ('AI', 'Gensyn', 'gensyn', 39883, True, 'ethereum',
                    '0x4d7078ddd6ccfed2f85db5b7d3ff16828d378d48'),
    'ELSAUSDT': ('ELSA', 'HeyElsa', 'elsa', 39387, False, 'base',
                 '0x29cc30f9d113b356ce408667aa6433589cecbdca'),
    'ENSOUSDT': ('ENSO', 'Enso', 'enso', 38668, True, 'ethereum',
                 '0x699f088b5dddcafb7c4824db5b10b57b37cb0c66'),
    'IDOLUSDT': ('IDOL', 'MEET48', 'meet48', 36775, False, 'binance-smart-chain',
                 '0x3b4de3c7855c03bb9f50ea252cd2c9fa1125ab07'),
    'PROMUSDT': ('PROM', 'Prom', 'prometeus', 4120, True, 'ethereum',
                 '0xfc82bb4ba86045af6f327323a46e80412b91b27d'),
    'SKYAIUSDT': ('SKYAI', 'SkyAI', 'skyai', 36300, True, 'binance-smart-chain',
                  '0x92aa03137385f18539301349dcfc9ebc923ffb10'),
    'VVVUSDT': ('VVV', 'Venice Token', 'venice-token', 35509, True, 'base',
                '0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf'),
}


def registry():
    return json.loads((ROOT / 'services/market_cap/assets.json').read_text())


@pytest.mark.parametrize('pair,expected', EXPECTED.items())
def test_eight_gaps_bind_official_contract_and_exact_provider_identity(pair, expected):
    asset = registry().get(pair)
    assert asset is not None, f'{pair} missing verified identity'
    symbol, name, cg_id, cmc_id, enabled, chain, contract = expected
    assert (asset['symbol'], asset['name'], asset['cg_id'], asset['cmc_id'], asset['cmc_enabled']) == expected[:5]
    assert asset['contracts'][chain].lower() == contract
    assert any('binance.com/' in url for url in asset['evidence'])
    assert f'https://api.coingecko.com/api/v3/coins/{cg_id}' in asset['evidence']
    comparison = asset['comparison']
    assert comparison['max_relative_difference'] == .05
    assert comparison['max_quote_timestamp_gap_seconds'] == 300
    assert 0 <= comparison['quote_timestamp_gap_seconds'] <= 300
    assert comparison['max_receipt_age_seconds'] == 300
    for prefix, provider in (('cg', 'coingecko'), ('cmc', 'coinmarketcap')):
        received = datetime.fromisoformat(comparison[prefix + '_available_at'])
        observed = datetime.fromisoformat(comparison[provider + '_observed_at'].replace('Z', '+00:00'))
        assert 0 <= (received-observed).total_seconds() <= 300
    for provider in ('cg', 'cmc'):
        digest = comparison[provider + '_body_sha256']
        assert len(digest) == 64 and set(digest) <= set('0123456789abcdef')
    if enabled:
        assert comparison['status'] == 'qualified'
        assert comparison['relative_difference'] <= .05
        assert comparison['supply_relative_difference'] <= .05
    else:
        assert comparison['status'] == 'not_qualified'
        assert max(comparison['relative_difference'], comparison['supply_relative_difference']) > .05
        assert '5%' in asset['fallback_disabled_reason']


def test_previous_81_entries_remain_byte_identical_and_semantically_unchanged():
    raw = (ROOT / 'services/market_cap/assets.json').read_bytes()
    old_bytes = raw.split(b',\n  "ACEUSDT":', 1)[0] + b'\n}\n' if b',\n  "ACEUSDT":' in raw else raw
    assert hashlib.sha256(old_bytes).hexdigest() == 'cc836a85e184c1b3134e85abd1df79cf8064b0d3d37915b518291324a64b4be1'
    old = {key: value for key, value in registry().items() if key not in EXPECTED}
    assert len(old) == 81
    assert hashlib.sha256(json.dumps(old, sort_keys=True, separators=(',', ':')).encode()).hexdigest() == '46724ec00fca2331378842a9afc9a42fb15d9a7cdee8e53cd9a8a213a6ea3916'


def test_gensyn_alias_is_contract_bound_and_not_artificial_inu_or_another_ai():
    asset = registry().get('AIGENSYNUSDT')
    assert asset is not None
    assert asset['symbol'] == 'AI' and asset['cg_id'] == 'gensyn' and asset['cmc_id'] == 39883
    assert asset['contracts']['ethereum'] == EXPECTED['AIGENSYNUSDT'][-1]
    assert 'Artificial Inu' in asset['identity_notes']
    assert 'AIGENSYN' in asset['identity_notes']
    assert 'https://www.binance.com/en/support/announcement/detail/524345e64dd14a23989bfe1e547760b5' in asset['evidence']


class Response:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.body


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr('services.market_cap.hub.time.time', lambda: NOW)
    monkeypatch.delenv('COINGECKO_API_KEY', raising=False)
    monkeypatch.delenv('COINMARKETCAP_API_KEY', raising=False)


def cg_rows():
    return [dict(id=expected[2], symbol=expected[0].lower(), market_cap=10000,
                 last_updated=datetime.fromtimestamp(NOW-30, timezone.utc).isoformat()) for expected in EXPECTED.values()]


@pytest.mark.asyncio
async def test_verified_primary_resolves_all_eight_with_alias_and_keeps_unknown_missing(tmp_path, clock):
    rows = cg_rows()
    for row in rows:
        row['last_updated'] = datetime.fromtimestamp(NOW-30, timezone.utc).isoformat()
    calls = []
    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response(200, rows)
    hub = MarketCapHub(tmp_path/'cache.sqlite', registry())
    hub.session = Session()
    try:
        result = await hub.collect([*EXPECTED, 'UNVERIFIEDUSDT'])
        for pair, expected in EXPECTED.items():
            assert result[pair]['provider'] == 'coingecko'
            assert result[pair]['asset_id'] == expected[2]
            assert result[pair]['value'] == 10000 and result[pair]['is_proxy'] is False
        assert result['UNVERIFIEDUSDT']['missing_reason'] == 'asset_identity_unverified'
        assert len(calls) == 1
        assert set(calls[0][1]['params']['ids'].split(',')) == {e[2] for e in EXPECTED.values()}
    finally:
        hub.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('pair', EXPECTED)
async def test_matching_ticker_with_wrong_provider_id_does_not_enter_cache(tmp_path, clock, pair):
    expected = EXPECTED[pair]
    class Session:
        def get(self, url, **kwargs):
            return Response(200, [dict(id='wrong-project', symbol=expected[0].lower(), market_cap=10000,
                last_updated=datetime.fromtimestamp(NOW-30, timezone.utc).isoformat())])
    hub = MarketCapHub(tmp_path/'cache.sqlite', registry())
    hub.session = Session()
    try:
        assert await hub.fetch('coingecko', [pair]) == {}
        assert hub.read([pair])[pair]['source'] == 'missing'
    finally:
        hub.close()


@pytest.mark.parametrize('change', ['contract', 'provider_id'])
def test_changed_contract_or_provider_definition_cannot_reuse_old_identity_cache(tmp_path, clock, change):
    assets = registry()
    assert 'AIGENSYNUSDT' in assets
    hub = MarketCapHub(tmp_path/'cache.sqlite', assets)
    pair = 'AIGENSYNUSDT'
    try:
        row = dict(value=10000, observed_at=NOW-30, provider='coingecko', asset_id='gensyn')
        hub.db.execute('INSERT INTO quotes VALUES (?,?)', (hub.identity(pair), json.dumps(row)))
        hub.db.commit()
        assert hub.read([pair])[pair]['source'] == 'cache_hit'
        if change == 'contract':
            assets[pair]['contracts']['ethereum'] = '0x0000000000000000000000000000000000000000'
        else:
            assets[pair]['cg_id'] = 'wrong-project'
        assert hub.read([pair])[pair]['source'] == 'missing'
    finally:
        hub.close()


@pytest.mark.asyncio
async def test_cg_failure_only_uses_five_parity_qualified_cmc_identities(tmp_path, clock):
    calls = []
    rows = [dict(id=e[3], symbol=e[0], quote=[dict(symbol='USD', market_cap=10000,
        last_updated=datetime.fromtimestamp(NOW-30, timezone.utc).isoformat())]) for e in EXPECTED.values()]
    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response(429, {}) if 'api.coingecko.com' in url else Response(200, {'data': rows})
    hub = MarketCapHub(tmp_path/'cache.sqlite', registry())
    hub.session = Session()
    try:
        result = await hub.collect([*EXPECTED, 'UNVERIFIEDUSDT'])
        for pair, expected in EXPECTED.items():
            if expected[4]:
                assert result[pair]['provider'] == 'coinmarketcap'
                assert result[pair]['asset_id'] == str(expected[3])
            else:
                assert result[pair]['source'] == 'missing'
                assert result[pair]['missing_reason'] == 'providers_unavailable_or_invalid'
        assert result['UNVERIFIEDUSDT']['missing_reason'] == 'asset_identity_unverified'
        cmc = [kwargs for url, kwargs in calls if 'coinmarketcap.com' in url]
        assert len(cmc) == 1
        assert set(cmc[0]['params']['id'].split(',')) == {str(e[3]) for e in EXPECTED.values() if e[4]}
    finally:
        hub.close()
