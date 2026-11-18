# HRS 首次自然扫描缺失的九项资产身份

核验日：2026-11-18。新增九项仅启用 CoinGecko 身份，`cmc_enabled=false`、`cmc_id=null`；尚未做同一时刻的流通供应量/市值口径对照，不启用 CMC。现有注册表条目、原始 ticker 分母、crypto 资格规则均未更改；未部署。

| Binance 合约 | 项目身份与 Binance 原始来源 | CoinGecko 页面 / 已验证 API ID |
|---|---|---|
| BTWUSDT | [Bitway，期货上市公告](https://www.binance.com/es-MX/support/announcement/detail/61e41ce0e4b74dc7a794cc6bf9c57d38) | [Bitway](https://www.coingecko.com/en/coins/bitway) / `bitway` |
| LITUSDT | [Lighter Protocol，期货上市公告](https://www.binance.com/en/support/announcement/detail/6a33be00231c4539b3a4a625538e4d1e) | [Lighter](https://www.coingecko.com/en/coins/lighter) / `lighter` |
| MMTUSDT | [Momentum，期货上市公告](https://www.binance.com/en/support/announcement/detail/c18b1bfd706648189e9f7ac9097351cc) | [Momentum](https://www.coingecko.com/en/coins/momentum-2) / **`momentum-3`** |
| BREVUSDT | [Brevis，Binance 项目页](https://www.binance.com/en/price/brevis) | [Brevis](https://www.coingecko.com/en/coins/brevis) / `brevis` |
| HUMAUSDT | [Huma Finance，期货上市公告](https://www.binance.com/en/support/announcement/detail/e3840e610d3c4b4db5816e0abfc33f9e) | [Huma Finance](https://www.coingecko.com/en/coins/huma-finance) / `huma-finance` |
| KAVAUSDT | [Kava，Binance 交易页](https://www.binance.com/en-GB/trade/KAVA_USDT?type=isolated) | [Kava](https://www.coingecko.com/en/coins/kava) / `kava` |
| CROSSUSDT | [Cross，期货上市公告](https://www.binance.com/en/support/announcement/detail/54c97e398f6d4390a3daefea4ec6ee68)，[当前 Alpha 项目页](https://www.binance.com/en/alpha/bsc/0x6bf62ca91e397b5a7d1d6bce97d9092065d7a510)显示 ONEchain | [ONEchain](https://www.coingecko.com/en/coins/onechain) / **`cross-2`** |
| PENGUUSDT | [Pudgy Penguins，期货上市公告](https://www.binance.com/en/support/announcement/detail/c669e07a85214c55a4f15af7717675c6) | [Pudgy Penguins](https://www.coingecko.com/en/coins/pudgy-penguins) / `pudgy-penguins` |
| 1000LUNCUSDT | [1000LUNC，期货上市公告](https://www.binance.com/en-ZA/support/announcement/detail/5b9bc7ef56834de895cf829af859207e) | [Terra Luna Classic](https://www.coingecko.com/en/coins/terra-luna-classic) / **`terra-luna`** |

已实际读取公开 [CoinGecko coins/list](https://api.coingecko.com/api/v3/coins/list)，与页面的 API ID 独立交叉核对。网页 slug 不作为 API ID：Momentum 页面是 `momentum-2`，API ID 则为 `momentum-3`；Terra Classic 页面重定向后仍显示 API ID `terra-luna`。

LIT 的旧 [Litentry→Heima 公告](https://www.binance.com/en-AE/support/announcement/detail/2a9feaa556f74dcdaa2192366f0e247c)明确旧 LIT 合约已结算，随后新的 LIT 上市公告明确底层为 Lighter。没有映射到 `litentry`、`lit` 或 `timeless`。

CROSS 的身份通过合约地址核实，未按名称猜测：原期货公告的 Alpha 链接、当前 Binance Alpha 与 CoinGecko explorer 链接均指向 BSC **`0x6bf62ca91e397b5a7d1d6bce97d9092065d7a510`**。CoinGecko 当前响应符号为 `ONE`，因此注册表的 provider 校验符号为 `ONE`；Binance 键仍为 `CROSSUSDT`。没有映射到 Harmony 的 ONE。

`1000LUNC` 是合约计价单位，注册表 provider 符号为 `LUNC`，市值使用整个发行资产的市值，不乘 1000；没有使用新 Terra 的 `terra-luna-2` 或 wrapped LUNC。

本次未能从本机取得 Binance exchangeInfo（HTTP 错误）；资格元数据引用已存档的 2026-11-17 实际 Binance exchangeInfo：九项均为 `TRADING/PERPETUAL/COIN`，subtype 含 `Crypto`。该历史快照不是“刚刚重新拉取”的声明；后续实时资格仍由现有 metadata gate 决定。CoinGecko 身份核验不保证任何后续报价的时效或可用性。
