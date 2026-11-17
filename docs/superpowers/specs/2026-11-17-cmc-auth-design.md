# CoinMarketCap 认证备用源

沿用用户已授权的共享市值与密钥接入方案。CoinGecko 保持主源，CoinMarketCap 保持备用源，6 个偏差超标标的仍禁用备用源。

共享服务读取 `COINMARKETCAP_API_KEY`，去除首尾空白；非空时访问官方 `/v3/cryptocurrency/quotes/latest` 并只通过 `X-CMC_PRO_API_KEY` 请求头认证。未配置时继续使用已有公开接口。CoinGecko 请求不得携带该 Key。身份、时效、市值、持久缓存与故障退避规则保持一致，认证失败不能绕过校验。

秘密只通过 SSH 标准输入写入服务器 `.secrets/market_cap.env`（0600），不进入仓库、构建参数、URL、普通日志或报告。仅重建共享市值服务，保留消费者与其他容器。以本地回归、云端断网回归、服务器真实认证备用源取数和消费者健康作为验收。新配置不可用时回滚镜像及原配置。
