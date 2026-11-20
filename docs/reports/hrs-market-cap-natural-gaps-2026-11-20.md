# Three natural-scan market-cap identity gaps

The verified registry grows from 78 to 81 assets, adding only YBUSDT,
STRKUSDT and NAORISUSDT. Existing 78 entries, including every disabled
fallback, remain semantically unchanged. This closes identity registration
for these observed gaps; it does not retroactively qualify the earlier
41-symbol batch or assert production deployment, a future coverage result,
or HRS financial acceptance.

## Identity evidence

| Binance perpetual | Project | CoinGecko API ID | CMC UCID | Ethereum contract |
| --- | --- | --- | --- | --- |
| YBUSDT | Yield Basis / YieldBasis | `yield-basis` | 38651 | `0x01791f726b4103694969820be083196cc7c045ff` |
| STRKUSDT | Starknet | `starknet` | 22691 | `0xca14007eff0db1f8135f4c25b34de49ab0d42766` |
| NAORISUSDT | Naoris Protocol, rebranded NaoX Protocol | `naoris` | 35114 | `0x1b379a79c91a540b2bcd612b4d713f31de1b80cc` |

The [Binance YB futures announcement](https://www.binance.com/en/support/announcement/detail/b9993dfcc8a04aa5932d1b87f2c06f87)
identifies the perpetual's underlying asset as Yield Basis. The
[official HODLer announcement](https://www.binance.com/en/support/announcement/detail/8b48c0db0eeb472ab0314377938af2dc)
publishes its Ethereum contract and BNB Chain contract
`0xfb93ee8152dd0a0e6f4b49c66c06d800cf1db72d`.
[CoinGecko](https://www.coingecko.com/en/coins/yield-basis) and
[CMC](https://coinmarketcap.com/currencies/yieldbasis/) share these token
identities despite the spacing difference in their project names.

The [Binance STRK futures announcement](https://www.binance.com/en/support/announcement/detail/2bfb6f8dccf447ada57165b7e6a4cf1b)
and [spot listing](https://www.binance.com/en/support/announcement/detail/cb15311b17c440d0b0cf077a9b9979a3)
identify Starknet and link its project site. [CoinGecko](https://www.coingecko.com/en/coins/starknet)
and [CMC](https://coinmarketcap.com/currencies/starknet-token/) identify the
same Ethereum token and project. CoinGecko additionally records the
Starknet representation
`0x04718f5a0fc34cc1af16a1cdee98ffb20c31f5cd61d6ab07201858f4287c938d`.
The registry records these representations as one issuer asset.

The [Binance NAORIS futures announcement](https://www.binance.com/en/support/announcement/detail/5283c247c18640228013647ae95333a4)
identifies Naoris Protocol. The
[official Binance Alpha token list](https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list)
returns NAORIS and its exact BNB Chain token address, which is identical
to the Ethereum address above. CoinGecko's
[page](https://www.coingecko.com/en/coins/naox-protocol) and
[CMC](https://coinmarketcap.com/currencies/naoris-protocol/) identify the
rebrand to NaoX Protocol with the same ticker and contracts. The actual
CoinGecko API ID is **`naoris`**, not either webpage slug. The provider
metadata response confirms this ID and both network addresses. No
mapping is inferred from the ticker alone.

The captured current Binance exchangeInfo marks all three `TRADING`,
`PERPETUAL`, `underlyingType=COIN`, and includes `Crypto` in their
underlying subtypes. Public identity field projections from Binance Alpha
and CoinGecko metadata are retained in
`tests/fixtures/market_cap/identity_natural_three_assets_20261120.json`;
exchangeInfo's three-symbol subset is retained beside it. Metadata is
explicitly labelled as a selected-field projection, not a complete raw
HTTP response. Alpha supply/cap fields are excluded from that identity
projection and are not used to qualify either provider.

## Paired quote qualification

The existing `MarketCapHub.fetch` client collected both providers inside
an isolated in-memory hub at approximately **2026-11-20 03:29:00 UTC**.
Both HTTP responses were 200. No persistent production cache, service,
registry, database, notification or financial source was changed. Request
headers and any credentials stayed in the server process and are absent
from all saved evidence.

| Pair | CoinGecko USD cap | CMC USD cap | Cap difference | Circulating-supply difference | CMC fallback |
| --- | ---: | ---: | ---: | ---: | --- |
| YBUSDT | 16,006,516 | 12,490,654.34 | 21.965190% | 21.755679% | Disabled |
| STRKUSDT | 245,323,344 | 245,172,330.11 | 0.061557% | 0% | Enabled |
| NAORISUSDT | 16,844,575 | 15,303,726.56 | 9.147446% | 9.516545% | Disabled |

Differences use CoinGecko as denominator. Source quote times are
03:27:56 UTC for CoinGecko and 03:28:01 UTC for CMC: a 5-second skew.
Receipts arrived within 65 seconds of those source times, satisfying the
300-second age and skew limits. STRK satisfies both 5% parity limits.
YB and NAORIS have proven identities but exceed both parity limits, so
only their CoinGecko primary mappings are enabled; their CMC fallbacks
remain explicitly disabled. Self-reported CMC supplies are not substituted
for the quoted circulating supplies.

The original UTF-8 provider bodies, SHA-256 hashes, receipt timestamps
and public request parameters are retained in
`cg_natural_three_assets_20261120.json` and
`cmc_natural_three_assets_20261120.json` under
`tests/fixtures/market_cap/`. These are public market data, not private
trading or financial artifacts.

## Verification

The initial identity tests failed on the three missing registry entries.
After the entries and captures were added, 10 focused tests passed. They
bind IDs and contracts, recompute both parity ratios and freshness from
the original bodies, replay all three CoinGecko quotes through the real
hub, and verify CMC fallback requests contain only STRK. YB/NAORIS remain
missing when only CMC is available; unknown symbols remain unverified.
A canonical hash assertion preserves the existing 78 entries.

The complete bounded market-cap regression command passed **74 tests**:

```text
/tmp/tuner-fact-lag-venv/bin/python -m pytest -q tests/test_shared/test_market_cap_*.py tests/test_shared/test_irys_market_cap_identity.py
74 passed in 2.20s
```
