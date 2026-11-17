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


def test_verified_six_crypto_mappings_do_not_silently_enable_cmc():
    registry = json.loads((Path(__file__).parents[3]/'services/market_cap/assets.json').read_text())
    expected = {'AKEUSDT': 'akedo', 'BLESSUSDT': 'bless-2', 'EVAAUSDT': 'evaa-protocol',
                'JCTUSDT': 'janction', 'RAREUSDT': 'superrare', '龙虾USDT': 'lobster-2'}
    for symbol, cg_id in expected.items():
        assert registry[symbol]['cg_id'] == cg_id
        assert registry[symbol]['cmc_enabled'] is False
        assert registry[symbol]['evidence']
    assert 'XAUUSDT' not in registry
