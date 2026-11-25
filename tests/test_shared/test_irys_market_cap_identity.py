"""The observed IRYS gap uses the issuer's verified public provider identity."""
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from services.market_cap.hub import MarketCapHub


ROOT = Path(__file__).resolve().parents[2]
CAPTURE = ROOT / 'tests/fixtures/market_cap/cg_irys_20261119.json'
BINANCE = 'https://www.binance.com/en/square/post/32909849744210'
CG = 'https://www.coingecko.com/en/coins/irys'
PROJECT = 'https://irys.xyz/assets/IrysWhitepaper.pdf'


def test_irys_identity_matches_primary_announcement_and_issuer_chain():
    registry = json.loads((ROOT / 'services/market_cap/assets.json').read_text())
    row = registry['IRYSUSDT']
    assert (row['symbol'], row['name'], row['cg_id']) == ('IRYS', 'Irys', 'irys')
    assert row['cmc_enabled'] is False and row['cmc_id'] is None
    assert row['comparison'] == {
        'status': 'not_qualified', 'reason': 'provider_supply_comparison_required',
    }
    assert {BINANCE, CG, PROJECT} <= set(row['evidence'])
    assert row['contracts'] == {
        'binance-smart-chain': '0x91152b4ef635403efbae860edd0f8c321d7c035d',
        'ethereum': '0x50f41f589afaca2ef41fdf590fe7b90cd26dee64',
    }
    assert 'native' in row['identity_notes'].lower()


@pytest.mark.asyncio
async def test_actual_irys_primary_response_enters_shared_cache_without_losing_missing_denominator(tmp_path, monkeypatch):
    capture = json.loads(CAPTURE.read_text())
    assert capture['http_status'] == 200
    assert hashlib.sha256(capture['body_utf8'].encode()).hexdigest() == capture['body_sha256']
    body = json.loads(capture['body_utf8'])
    assert [(r['id'], r['symbol'], r['name']) for r in body] == [('irys', 'irys', 'Irys')]
    received = datetime.fromisoformat(capture['available_at']).timestamp()
    assert datetime.fromisoformat(capture['started_at']).timestamp() <= received
    # Reproduce the actual receipt time; never rewrite the captured provider row.
    monkeypatch.setattr('services.market_cap.hub.time.time', lambda: received)
    monkeypatch.delenv('COINGECKO_API_KEY', raising=False)
    monkeypatch.delenv('COINMARKETCAP_API_KEY', raising=False)

    class Session:
        calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            assert url == 'https://api.coingecko.com/api/v3/coins/markets'
            assert kwargs['params'] == {'vs_currency': 'usd', 'ids': 'irys', 'per_page': 250}
            assert kwargs['headers'] == {}

            class Response:
                status = capture['http_status']

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                async def json(self):
                    return json.loads(capture['body_utf8'])

            return Response()

    registry = json.loads((ROOT / 'services/market_cap/assets.json').read_text())
    hub = MarketCapHub(tmp_path / 'cache.sqlite', registry)
    session = Session()
    hub.session = session
    try:
        quotes = await hub.collect(['IRYSUSDT', 'UNKNOWNUSDT'])
        assert set(quotes) == {'IRYSUSDT', 'UNKNOWNUSDT'}
        assert quotes['IRYSUSDT']['provider'] == 'coingecko'
        assert quotes['IRYSUSDT']['asset_id'] == 'irys'
        assert quotes['IRYSUSDT']['value'] == body[0]['market_cap']
        assert quotes['IRYSUSDT']['is_proxy'] is False
        assert quotes['IRYSUSDT']['missing_reason'] is None
        assert quotes['UNKNOWNUSDT']['missing_reason'] == 'asset_identity_unverified'
        assert await hub.fetch('coinmarketcap', ['IRYSUSDT']) == {}
        assert session.calls == 1
    finally:
        hub.close()
