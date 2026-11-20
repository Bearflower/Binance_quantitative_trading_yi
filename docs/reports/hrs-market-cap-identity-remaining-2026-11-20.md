# Remaining four HRS market-cap identities

The verified asset registry grows from 74 to 78 entries. SKYUSDT, INITUSDT,
SENTUSDT and VELVETUSDT have both primary CoinGecko identities and qualified
CoinMarketCap secondary identities. This is source qualification evidence;
it does not assert production deployment or subsequent strategy acceptance.

## Identity chain

| Binance pair | Project | CoinGecko ID | CMC ID | Native asset / contract |
| --- | --- | --- | --- | --- |
| SKYUSDT | Sky, formerly Maker | sky | 33038 | Ethereum `0x56072C95FAA701256059aa122697B133aDEd9279` |
| INITUSDT | Initia | initia | 33120 | Native Initia `uinit` |
| SENTUSDT | Sentient | sentient | 38868 | Ethereum `0x56A3BA04E95d34268A19b2a4474DC979baBDaf76` |
| VELVETUSDT | Velvet | velvet | 37298 | BNB Chain `0x8b194370825E37b33373e74A41009161808C1488` |

The [SKY futures announcement](https://www.binance.com/en/support/announcement/detail/4009f8bd8d85456189e9265d311b25de)
explicitly identifies Maker's Sky upgrade and its Ethereum contract.
[CoinGecko Sky](https://www.coingecko.com/en/coins/sky) identifies API ID `sky`
and links that contract; [CMC Sky](https://coinmarketcap.com/currencies/sky/)
identifies UCID 33038 and the same contract. This excludes ticker collisions
with SKYAI and Skycoin.

The [INIT futures announcement](https://www.binance.com/en/support/announcement/detail/4e07a703c9da44709e22e79d0b4be452)
identifies Initia; the [Launchpool announcement](https://www.binance.com/en/support/announcement/detail/c288d0d677304b9e9246eceae0237ff3)
links the native Initia explorer and project. [CoinGecko Initia](https://www.coingecko.com/en/coins/initia)
identifies API ID `initia` and denomination `uinit`; [CMC Initia](https://coinmarketcap.com/currencies/initia/)
identifies UCID 33120 and the same project and native explorer. No EVM
contract is invented for this asset.

The [SENT futures announcement](https://www.binance.com/en/support/announcement/detail/5fe5a15ee7cb403fb09f1e9f9b4cd80c)
identifies Sentient, and the [spot listing](https://www.binance.com/en/support/announcement/detail/85af9e52f60547dd8f7b67fd3dc2f7b4)
publishes the Ethereum contract. [CoinGecko Sentient](https://www.coingecko.com/en/coins/sentient)
and [CMC Sentient](https://coinmarketcap.com/currencies/sentient/) share the
contract, API ID `sentient` and UCID 38868.

The [VELVET futures announcement](https://www.binance.com/en/support/announcement/detail/4f59bfc195ed4484ac810a9b8869fa86)
publishes Velvet's BNB Chain contract. [CoinGecko Velvet](https://www.coingecko.com/en/coins/velvet)
and [CMC Velvet](https://coinmarketcap.com/currencies/velvet/) share the
contract, API ID `velvet` and UCID 37298.

The captured current Binance exchangeInfo marks all four instruments
`TRADING`, `PERPETUAL`, `underlyingType=COIN`, with `Crypto` among their
underlying subtypes. These are issuer crypto-asset market caps.

## Paired provider qualification

Collected at 2026-11-20 01:33:28 UTC using the existing `MarketCapHub.fetch`
provider clients inside an isolated in-memory hub. Both provider HTTP
statuses were 200. Existing service credentials stayed within the container
process and are absent from the fixtures. No persistent production cache,
registry, process, database or notification was changed.

| Pair | CoinGecko USD cap | CMC USD cap | Cap difference | Circulating supply difference |
| --- | ---: | ---: | ---: | ---: |
| SKYUSDT | 1,069,882,234 | 1,071,339,070.04 | 0.136168% | 0.001013% |
| INITUSDT | 21,874,986 | 21,838,350.85 | 0.167475% | 0.000197% |
| SENTUSDT | 151,267,420 | 151,037,647.86 | 0.151898% | 0.000044% |
| VELVETUSDT | 31,976,242 | 32,140,910.12 | 0.514970% | 0% |

Differences use the CoinGecko value as denominator. All are below the 5%
cap and circulating-supply limits. Provider quote timestamps are
01:31:58 UTC and 01:32:01 UTC, a 3-second skew; both observations were
received within 91 seconds and satisfy the 300-second qualification bound.
The raw CMC response's token addresses also match the proven Binance
contracts for the three EVM tokens; INIT has a native, null platform.

Raw provider UTF-8 bodies, SHA-256 digests, request parameters and receipt
timestamps are frozen in `cg_remaining_four_assets_20261120.json` and
`cmc_remaining_four_assets_20261120.json` under `tests/fixtures/market_cap/`.
The public Binance subset is frozen beside them. No private trading or
financial export is included.

Tests first failed on the four missing registry entries. Regression tests
then replay both real provider captures through the shared hub, verify
exact IDs, native identity and contract evidence, recompute qualification,
and retain an explicit unknown-symbol missing denominator.
