import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest



def contract(symbol='RAREUSDT', **changes):
    row = dict(symbol=symbol, quoteAsset='USDT', contractType='PERPETUAL',
               status='TRADING', underlyingType='COIN', underlyingSubType=['Crypto'])
    return dict(row, **changes)


def test_unknown_halted_and_tradfi_never_gain_crypto_eligibility():
    from strategies.hrs.asset_universe import classify_contract
    assert classify_contract(contract()) == 'crypto'
    assert classify_contract(contract('1000PEPEUSDT')) == 'crypto'
    assert classify_contract(contract('龙虾USDT')) == 'crypto'
    assert classify_contract(contract('XAUUSDT', contractType='TRADIFI_PERPETUAL', underlyingType='COMMODITY', underlyingSubType=['TradFi'])) == 'inapplicable_tradfi'
    assert classify_contract(contract(status='TRADING_HALT')) == 'not_trading'
    assert classify_contract({'symbol': 'RAREUSDT'}) == 'metadata_unverified'
    assert classify_contract(contract(underlyingSubType=[])) == 'metadata_unverified'


def test_filter_preserves_raw_denominator_and_missing_metadata():
    from strategies.hrs.asset_universe import filter_crypto_tickers
    tickers = [{'symbol': s} for s in ['RAREUSDT', 'XAUUSDT', 'MISSINGUSDT']]
    metadata = [contract(), contract('XAUUSDT', contractType='TRADIFI_PERPETUAL', underlyingType='COMMODITY', underlyingSubType=['TradFi'])]
    allowed, audit = filter_crypto_tickers(tickers, metadata)
    assert allowed == [{'symbol': 'RAREUSDT'}]
    assert audit['raw_count'] == 3 and audit['eligible_count'] == 1
    assert audit['counts'] == {'crypto': 1, 'inapplicable_tradfi': 1, 'metadata_unverified': 1}
    assert audit['rows']['MISSINGUSDT'] == 'metadata_unverified'


@pytest.mark.asyncio
async def test_market_data_fetches_metadata_once_and_fails_closed_on_missing_metadata():
    from strategies.hrs.market_data import MarketDataProvider
    api = AsyncMock()
    api.get_all_tickers.return_value = [{'symbol': 'RAREUSDT'}, {'symbol': 'XAUUSDT'}]
    api.get_exchange_info.return_value = {'symbols': [contract()]}
    provider = MarketDataProvider(api, AsyncMock(), {})
    try:
        assert await provider.get_all_tickers() == [{'symbol': 'RAREUSDT'}]
        assert await provider.get_all_tickers() == [{'symbol': 'RAREUSDT'}]
        api.get_exchange_info.assert_awaited_once()
        assert provider.universe_audit['raw_count'] == 2
        provider._exchange_metadata_at = 0
        api.get_all_tickers.return_value.append({'symbol': 'EXTRAUSDT'})
        api.get_exchange_info.side_effect = RuntimeError('offline')
        assert await provider.get_all_tickers() == []
        assert provider.universe_audit['raw_count'] == 3
        assert provider.universe_audit['eligible_count'] == 0
        assert provider.universe_audit['counts'] == {'metadata_unverified': 3}
        assert provider.universe_audit['error_type'] == 'RuntimeError'
    finally:
        await provider.close()


def assert_observed_secondary_qualification(row,expected_id):
    """An enabled secondary must have the original paired supply/cap proof."""
    folder=Path(__file__).parents[3]/'tests/fixtures/market_cap'
    captures=[json.loads((folder/name).read_text()) for name in (
        'cg_twelve_assets_20261119.json','cmc_twelve_assets_20261119.json')]
    comparison=row['comparison']
    assert row['cmc_enabled'] is True and row['cmc_id']==expected_id
    assert comparison['status']=='qualified' and comparison['max_relative_difference']==.05
    assert 0<=comparison['relative_difference']<=.05
    assert 0<=comparison['supply_relative_difference']<=.05
    assert comparison['quote_timestamp_gap_seconds']<=comparison['max_quote_timestamp_gap_seconds']==300
    assert comparison['cg_body_sha256']==captures[0]['body_sha256']
    assert comparison['cmc_body_sha256']==captures[1]['body_sha256']


def test_verified_six_crypto_mappings_do_not_silently_enable_cmc():
    registry = json.loads((Path(__file__).parents[3]/'services/market_cap/assets.json').read_text())
    expected = {'AKEUSDT': 'akedo', 'BLESSUSDT': 'bless-2', 'EVAAUSDT': 'evaa-protocol',
                'JCTUSDT': 'janction', 'RAREUSDT': 'superrare', '龙虾USDT': 'lobster-2'}
    for symbol, cg_id in expected.items():
        assert registry[symbol]['cg_id'] == cg_id
        if symbol=='AKEUSDT':
            assert_observed_secondary_qualification(registry[symbol],38127)
        else:
            assert registry[symbol]['cmc_enabled'] is False
        assert registry[symbol]['evidence']
    assert 'XAUUSDT' not in registry


@pytest.mark.parametrize('symbol,cg_id,provider_symbol',[
    ('BTWUSDT','bitway','BTW'),('LITUSDT','lighter','LIT'),
    ('MMTUSDT','momentum-3','MMT'),('BREVUSDT','brevis','BREV'),
    ('HUMAUSDT','huma-finance','HUMA'),('KAVAUSDT','kava','KAVA'),
    ('CROSSUSDT','cross-2','ONE'),('PENGUUSDT','pudgy-penguins','PENGU'),
    ('1000LUNCUSDT','terra-luna','LUNC'),
])
def test_natural_scan_missing_assets_use_verified_provider_identity_only(symbol,cg_id,provider_symbol):
    registry=json.loads((Path(__file__).parents[3]/'services/market_cap/assets.json').read_text())
    row=registry[symbol]
    assert row['cg_id']==cg_id and row['symbol']==provider_symbol
    qualified={'LITUSDT':39125,'MMTUSDT':38231}
    if symbol in qualified:
        assert_observed_secondary_qualification(row,qualified[symbol])
    else:
        assert row['cmc_enabled'] is False and row['cmc_id'] is None
        assert row['comparison']['reason']=='provider_supply_comparison_required'
    assert any(url.startswith('https://www.binance.com/') for url in row['evidence'])
    assert any(url.startswith('https://www.coingecko.com/') for url in row['evidence'])
    # Quote-unit multipliers do not multiply the issuer's whole market cap.
    assert cg_id not in {'litentry','momentum','momentum-2','terra-luna-2','harmony'}


@pytest.mark.asyncio
async def test_renamed_and_multiplied_contracts_keep_provider_cap_and_missing_denominator(tmp_path):
    from datetime import datetime,timezone
    from services.market_cap.hub import MarketCapHub
    registry=json.loads((Path(__file__).parents[3]/'services/market_cap/assets.json').read_text())
    class Session:
        calls=0
        def get(self,url,**kwargs):
            assert url=='https://api.coingecko.com/api/v3/coins/markets'
            self.calls+=1
            class Response:
                status=200
                async def __aenter__(self):return self
                async def __aexit__(self,*args):pass
                async def json(self):
                    return [dict(id='cross-2',symbol='one',market_cap=123,last_updated=datetime.now(timezone.utc).isoformat()),
                            dict(id='terra-luna',symbol='lunc',market_cap=456,last_updated=datetime.now(timezone.utc).isoformat())]
            return Response()
    hub=MarketCapHub(tmp_path/'assets.db',registry);hub.session=Session()
    try:
        assert await hub.fetch('coinmarketcap',['CROSSUSDT','1000LUNCUSDT'])=={}
        assert hub.session.calls==0
        result=await hub.collect(['CROSSUSDT','1000LUNCUSDT','UNKNOWNUSDT'])
        assert len(result)==3
        assert result['CROSSUSDT']['value']==123
        assert result['1000LUNCUSDT']['value']==456
        assert result['UNKNOWNUSDT']['missing_reason']=='asset_identity_unverified'
    finally:hub.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', [[{'symbol': 'RAREUSDT'}, {'symbol': 'RAREUSDT'}], [None], [{}], [{'symbol': 1}]])
async def test_malformed_tickers_leave_a_fresh_nonthrowing_failure_audit(bad):
    from strategies.hrs.market_data import MarketDataProvider
    api = AsyncMock()
    api.get_all_tickers.return_value = [{'symbol': 'RAREUSDT'}]
    api.get_exchange_info.return_value = {'symbols': [contract()]}
    provider = MarketDataProvider(api, AsyncMock(), {})
    try:
        assert await provider.get_all_tickers()
        previous = provider.universe_audit['observed_at']
        api.get_all_tickers.return_value = bad
        assert await provider.get_all_tickers() == []
        assert provider.universe_audit['raw_count'] == len(bad)
        assert provider.universe_audit['eligible_count'] == 0
        assert provider.universe_audit['observed_at'] >= previous
        assert provider.universe_audit['error_type']
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_bad_exchange_metadata_does_not_poison_next_refresh():
    from strategies.hrs.market_data import MarketDataProvider
    api = AsyncMock()
    api.get_all_tickers.return_value = [{'symbol': 'RAREUSDT'}]
    api.get_exchange_info.side_effect = [{'symbols': [contract(), contract()]}, {'symbols': [contract()]}]
    provider = MarketDataProvider(api, AsyncMock(), {})
    try:
        assert await provider.get_all_tickers() == []
        assert await provider.get_all_tickers() == [{'symbol': 'RAREUSDT'}]
        assert api.get_exchange_info.await_count == 2
    finally:
        await provider.close()
