# HRS market universe evidence, 2026-11-17

This audit used only production logs and public provider/exchange data. It made no production changes and accessed no credential values.

The 09:21 UTC HRS scoring logs contain **35** distinct symbols with `asset_identity_unverified`: **6 crypto, 29 TradFi, 0 unknown**, classified using the public Binance exchange metadata captured in the adjacent JSON. The same cycle logged an LV-RM range of **52**, but the 35 missing-symbol set also includes standard scoring. It must not be described as 35 out of that 52. The earlier reported 32 is a different time-dependent observation; its symbol artifact is unavailable.

Eligibility requires all four exchange facts: `status=TRADING`, `contractType=PERPETUAL`, `underlyingType=COIN`, and `Crypto` in `underlyingSubType`. TradFi uses `TRADIFI_PERPETUAL`, and includes EQUITY, KR_EQUITY, HK_EQUITY, COMMODITY and PREMARKET. ANTHROPIC additionally returned `TRADING_HALT`. Unknown/missing metadata must remain ineligible. Classification does not by itself verify the provider identity.

| Symbol | CoinGecko API ID | CMC UCID | Identity evidence |
|---|---|---|---|
| AKEUSDT | akedo | 38127 | [CG](https://www.coingecko.com/en/coins/akedo), [CMC](https://coinmarketcap.com/currencies/akedo/) |
| BLESSUSDT | bless-2 | 38466 | [CG](https://www.coingecko.com/en/coins/bless), [CMC](https://coinmarketcap.com/currencies/bless/) |
| EVAAUSDT | evaa-protocol | 38376 | [CG](https://www.coingecko.com/en/coins/evaa-protocol), [CMC](https://coinmarketcap.com/currencies/evaa-protocol/) |
| JCTUSDT | janction | 38909 | [CG](https://www.coingecko.com/en/coins/janction), [CMC](https://coinmarketcap.com/currencies/janction/) |
| RAREUSDT | superrare | 11294 | [CG](https://www.coingecko.com/en/coins/superrare), [CMC](https://coinmarketcap.com/currencies/superrare/) |
| 龙虾USDT | lobster-2 | unverified | [CG](https://www.coingecko.com/en/coins/lobster-2) |

CG `/coins/list` confirms these exact IDs; CG derivative tables tie the IDs to Binance Futures symbols. AKE also matches the exact BSC contract address in [Binance’s official launch announcement](https://www.binance.com/en/support/announcement/detail/f7b86984a0d34f4fbfc3204fa93ae344). `BLESS` uses API ID `bless-2`; JCT is Janction, not Jade City Token; RARE is SuperRare, not SuperRareBears. Chinese symbol 龙虾 is preserved verbatim. The provider IDs are independently corroborated; fallback quote quality remains separate. In particular same-day BLESS, EVAA and JCT provider pages show materially different circulating supplies, so their CMC fallbacks must not be enabled from identity evidence alone.

All six existing disabled fallbacks remain disabled. Original available five CG/CMC observations are 7 seconds apart (07:21:56 versus 07:22:03 UTC); their market-cap ratios track the direction and magnitude of the current circulating-supply differences. The original ASTER quote is absent from the local audit export; its registry comparison records 7.6967%. Current CG API fields and CMC page supply observations are preserved in JSON with timestamp limitations. This supports a provider supply-definition diagnostic, rather than an identity bug or permission to raise the 5% threshold. The current CMC public quote request returned 429; no authenticated retry or credential access was performed.

For future comparison, collect both providers’ price, circulating supply, market cap, provider timestamp, collection timestamp and identity in one audit batch; calculate cap ratio, price ratio and supply ratio, and record maximum observed timestamp skew. Provider fallback enablement should require matching identity, fresh positive finite cap, and an explicitly accepted market-cap definition within the existing tolerance. Never substitute FDV or equity valuation for circulating crypto market cap.

Recommended regression coverage: TradFi equity and commodity excluded despite USDT suffix/liquidity; halted and unknown symbols excluded; standard crypto and 1000-unit crypto contracts accepted using metadata; XAU cannot inherit XAUT identity; wrong/ambiguous JCT or RARE IDs rejected; Chinese symbols preserved; mapping does not automatically enable CMC; original six disabled remain disabled.

[Binance exchange metadata](https://fapi.binance.com/fapi/v1/exchangeInfo) and [official TradFi explanation](https://www.binance.com/en/learn/binance-futures-tradfi-perpetual-contracts) identify the market types. The adjacent JSON contains all 35 exact rows and per-asset provenance URLs.
