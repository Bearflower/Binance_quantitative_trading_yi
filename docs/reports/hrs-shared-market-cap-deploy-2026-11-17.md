# HRS 无密钥共享市值上线记录

2026-11-17 北京时间 16:14 左右完成最终发布，16:18 完成服务器备用源验证。

## 发布与测试

- 发布提交为 `9e184ff8a21bbdbba3edaec5dad67542972c5244`，分支为 `codex/hrs-market-cap-20261117`。
- [最终云端构建](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44044447141) 在 Python 3.11、断网测试容器中完成 458 项测试。对应本地 Python 3.12 测试为 457 项通过。云端基础镜像原有测试文件的收集范围略有差异。
- 首轮构建因 Docker 忽略测试目录而失败，已添加该 Dockerfile 专用的忽略配置并重跑通过。
- 最终镜像摘要如下。

| 服务 | 镜像摘要 |
| --- | --- |
| HRS | `ghcr.io/bearflower/trading-hrs@sha256:2cf4f020332b0df61656a3d926302893e2dba6cb617cdf2fc1deac9cc4d51433` |
| 快照采集器 | `ghcr.io/bearflower/trading-hrs@sha256:568c64fa140f7fc4c4ba2f4c980ec00fd66a1fb6dea127d47f558556d9639969` |
| 共享市值服务 | `ghcr.io/bearflower/trading-hrs@sha256:cfc99d7311db7c6addea2972262f83b41d76b3ecdc5422a2c12fe935494b8c65` |

服务器证据目录为 `/root/trading_system/.releases/hrs-market-cap/9e184ff8a21bbdbba3edaec5dad67542972c5244/`，包含前置状态、镜像覆盖配置和最终源码哈希。发布持有 Compose 部署锁，核对真实配置挂载、环境与命令后，仅重建三个目标容器。HRS 的 AI Tuner 只读配置及覆盖层挂载得到保留。

## 已实现

1. HRS 标准模式评分前先取得标准池与 LV-RM 范围的并集，进行批量预取。后续逐币评分只读取共享缓存。
2. `market-cap-service` 统一采集 CoinGecko 与 CoinMarketCap 公开行情。HRS 和 `ai-tuner-snapshot` 都已接入 `SharedMarketCapClient`。该服务没有交易所交易密钥，不映射主机端口。
3. SQLite 缓存持久化在 `/root/trading_system/data/market-cap-cache/`。缓存键包含交易对和完整资产定义摘要，记录供应商、资产 ID、供应商更新时间与采集时间。超过一小时、未来时间、非正数、非有限值或身份不匹配的数据无法成为真实市值。
4. 52 个合约使用明确资产 ID，包含两个经核对的千倍单位合约。未核验标的返回 `asset_identity_unverified`。接口不会按同名 ticker 的第一条结果猜测身份。
5. 主源失败后批量查询备用源。初始对照中偏差超过 5% 的 6 个标的禁用备用源，此阈值为本轮采取的数据质量规则。禁用名单为 ARIA、SEI、XAUT、HYPE、FET、ASTER，其 CoinGecko 主源仍可用。其余 46 个合约允许备用源。
6. 供应商 429 触发持久退避，普通失败触发短退避，缺失结果负缓存十分钟。共享服务不可用时消费者退避一分钟，避免逐币重复等待超时。
7. 快照原始数据新增 `market_cap_provider` 和 `market_cap_provider_asset_id`，保留原有时间与哈希证据。旧快照没有因此获得已核验身份。

## 真实取数验收

- 服务器初始批量采集取得 52/52 个注册合约的有效 CoinGecko 市值。随后 HRS、快照采集器和部署复核重复读取时，共享服务统计只有一次 CoinGecko 上游请求。
- 对 HRS 当时实际 LV-RM 范围进行只读取数检查，共 52 个标的。20 个已核验资产全部取得真实市值，其余 32 个因股票、商品或身份尚未核验而明确缺失，没有其他取数失败。注册合约总数与这一时刻的扫描范围是两个不同集合。
- 快照采集器实际使用共享客户端，并读取到 INJ 与 ENA 的供应商资产 ID，NVDA 明确返回身份未核验。
- 执行一次真实公开行情快照采集，共请求 14 个标的，完整数据 3 个、缺失 11 个、代理 0 个、失败 0 个。
- CoinMarketCap 服务器首次验证遇到 429。按退避间隔后重查返回 HTTP 200、46 条报价。随后在独立内存进程中模拟主源不可用，以真实备用源请求验证 46/46 个获准合约有效，其余 6 个按比较规则禁用，没有意外缺失。该验证没有改动在线主源、缓存或实盘订单。
- 三个容器均为 `running/healthy`，重启次数为 0，部署后源码和客户端 URL 与候选一致。

## Demo Key 后续接入

Key 尚未提供。认证请求头已经预留在共享采集进程，服务器秘密文件为 `/root/trading_system/.secrets/market_cap.env`，权限为 `0600`，当前 Key 为空。取得 Key 后通过安全渠道写入 `COINGECKO_API_KEY`，再重建共享市值服务使环境生效。密钥不进入仓库、URL 或普通日志。

## 范围与后续观察

无密钥取数、共享缓存、已核验映射、备用源、故障退避和两个消费者接入均已上线。32 个身份未核验标的仍然拒绝使用猜测市值。两源均不可用且真实缓存过期时仍会缺失，公共接口配额无法由代码保证。后续信号仍须满足 HRS 原有趋势、形态、评分及风险预算。

参考 [CoinMarketCap 无密钥接口](https://coinmarketcap.com/api/documentation/pro-api-reference/keyless-public-api)、[CoinMarketCap 报价接口](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency)。逐资产映射与比较结果保存在 `services/market_cap/assets.json`。

## CoinMarketCap 免费 Key 验证（同日追加）

用户授权测试认证接口。北京时间 16:26，从生产服务器调用 `/v1/key/info` 与 `/v3/cryptocurrency/quotes/latest`，均返回 HTTP 200、业务错误码 0。账户返回每月 15,000 credits、每分钟 50 次请求；批量报价一次消耗 1 credit，返回全部 52 条资产记录。未保存 Key、认证头或原始秘密到本报告。

本地复核使用与线上一致的符号大小写归一化规则：52/52 个注册合约的资产 ID、符号、正有限市值与一小时时效检查全部通过，最老报价约 65 秒；获准备用源的 46 个均有效。首轮临时校验脚本把供应商的 `XAUt` 与 `XAUT` 作区分大小写比较，已在复核时纠正；线上已有大小写归一化逻辑。另一次服务器补充校验遇到 SSH 整体超时，未改变先前认证成功的结果。

此次仅做只读验证，未保存生产凭据、改动运行配置或重建服务。线上仍使用先前无密钥方案。后续接入须增加 CoinMarketCap 专用 Key 环境变量与 `X-CMC_PRO_API_KEY` 请求头，切换认证报价路径；不能复用 CoinGecko Demo Key 配置。原有 6 个偏差禁用项及身份未核验标的限制继续保留。

## CoinMarketCap 认证接入上线（同日后续）

北京时间 16:48 已完成认证接入，以上“仅做只读验证、线上仍无密钥”描述属于本次接入之前的测试阶段。当前共享服务已读取服务器秘密文件中的 `COINMARKETCAP_API_KEY`，以专用 `X-CMC_PRO_API_KEY` 请求头调用官方 `/v3/cryptocurrency/quotes/latest`。密钥为空时保留原公开路径；CoinGecko 仍为主源，CoinMarketCap 仍为备用源。

- 发布提交：`386e43308ee11680153cab195c7f6aacf5a4dc3b`。
- 共享服务镜像：`ghcr.io/bearflower/trading-hrs@sha256:4fa94f38ebcedcaefc79a05520211130cfe3c47be9169be4822f69fd90bfa053`。
- 服务器源码哈希：`c80189a1f68edf12984962e91c4a51df0de9e689d545ef5af61ffcd92863680d`。
- 本地 464 项回归通过；[云端断网回归](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44049312894) 465 项通过。修复前新增认证测试有 3 项按预期失败，修复后通过。
- OCR 确定的 2 个审查文件全部由当前 agent 审查；另补充审查被过滤的测试及文档，无阻塞问题。
- Key 通过 SSH 标准输入传输，仅写入 `/root/trading_system/.secrets/market_cap.env`（0600）；没有进入仓库、构建参数或 URL。
- 持 Compose 部署锁核对镜像、环境与挂载，只替换 `trading_system-market-cap`。包括 HRS、快照在内的其他容器 ID 全部保持一致；三者均为 running/healthy、重启次数 0。
- 使用线上容器中的真实认证配置，在独立内存 Hub 中模拟主源退避：一次 CMC 请求取得 46/46 个获准标的的有效市值，6 个偏差禁用项仍拒绝，未出现 429 或意外缺失。该测试不修改在线缓存或策略订单。
- 在线持久缓存重启后读取 BTC、INJ、ENA 三个标的均有效。

服务器发布证据保存于 `/root/trading_system/.releases/hrs-market-cap/386e43308ee11680153cab195c7f6aacf5a4dc3b/`，包含 `before.json`、`hub.json`、`images.json`、`fallback-verification.json` 和 `state.json`。发布脚本在失败时恢复原镜像与原秘密文件；本次验收通过，没有触发回滚。

本次认证接入不扩大资产映射范围，不解除 6 个偏差禁用项，不修改 HRS 入场评分或风险预算。缺失资产身份仍需单独核验。
