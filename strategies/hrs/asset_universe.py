"""Exchange-metadata eligibility; a stock ticker is not a crypto asset identity."""
from collections import Counter


def classify_contract(row):
    if not isinstance(row, dict) or not row.get('symbol'):
        return 'metadata_unverified'
    subtypes = row.get('underlyingSubType')
    subtypes = {str(x).upper() for x in subtypes} if isinstance(subtypes, list) else set()
    if row.get('contractType') == 'TRADIFI_PERPETUAL' or 'TRADFI' in subtypes:
        return 'inapplicable_tradfi'
    if row.get('status') and row['status'] != 'TRADING':
        return 'not_trading'
    if (row.get('status') == 'TRADING' and row.get('contractType') == 'PERPETUAL'
            and row.get('underlyingType') == 'COIN' and 'CRYPTO' in subtypes
            and row.get('quoteAsset') == 'USDT'):
        return 'crypto'
    return 'metadata_unverified'


def filter_crypto_tickers(tickers, metadata):
    identities = {}
    for row in metadata:
        if isinstance(row, dict) and row.get('symbol'):
            symbol = row['symbol']
            # Duplicate metadata cannot silently replace an earlier identity.
            if symbol in identities:
                raise ValueError('duplicate contract metadata')
            identities[symbol] = row
    rows = {}
    allowed = []
    for ticker in tickers:
        symbol = ticker.get('symbol')
        if not isinstance(symbol, str) or symbol in rows:
            raise ValueError('invalid or duplicate ticker identity')
        reason = classify_contract(identities.get(symbol))
        rows[symbol] = reason
        if reason == 'crypto':
            allowed.append(ticker)
    return allowed, dict(raw_count=len(tickers), eligible_count=len(allowed),
                         counts=dict(Counter(rows.values())), rows=rows)
