# 第一批交易安全问题修复（R01–R08）需求文档

## 1. 文档信息

| 项 | 内容 |
|----|------|
| 文档名称 | 第一批交易安全问题修复需求文档（R01–R08） |
| 版本 | v1.0（初稿，待评审） |
| 作者 | requirements-document-expert |
| 创建日期 | 2026-09-29 |
| 基线 commit | `e4912619e513`（当前 main HEAD） |
| 需求来源 | `docs/reviews/2026-09-29/review.md` 第 R01–R08 条（已经 4 个独立智能体源码级复核，确认属实、无误报） |
| 适用范围 | 本轮只修 R01–R08；R09–R18 不在本轮 |
| 下游环节 | backend-architect（架构设计）→ python-engineer（编码实现） |
| 验收口径 | 每条需求以本文件「验收标准」小节为准，编号 `R0x-ACn`，可直接转为测试用例 |

> 说明：本文档只描述「应该是什么行为」，不写代码实现。文件中出现的「伪代码级状态流转」仅用于消除歧义，实现细节由架构/编码环节决定。

---

## 2. 本轮范围总表

| 编号 | 严重度 | 类别 | 位置（基线） | 影响容器 | 是否触发全量重建¹ | 预估改动文件 |
|------|--------|------|--------------|----------|------------------|--------------|
| R01 | P1 | security | `services/kline_service/api/routes.py:88`（及 `:181` indicators 同类逻辑） | kline-service | 否（仅 kline-service） | `routes.py`、`registry_routes.py`、`shared/core/config.py`、可选新增校验模块 |
| R02 | P1 | bug | `shared/binance_api.py:195`（关联 `shared/utils.py:63`、`binance_api.py:528`） | 全量 | **是** | `shared/binance_api.py`、`shared/utils.py`、新增 shared 重试/幂等配置；各调用方下单处 |
| R03 | P1 | bug | `strategies/new_coin/executor.py:2582`（关联 `:2526`/`:2556`） | new-coin | 否（仅 new-coin） | `strategies/new_coin/executor.py`、`strategies/new_coin/config.yaml` |
| R04 | P1 | bug | `strategies/hrs/strategy.py:1938`（关联 `:1983`、`executor.py:913`） | hrs | 否（仅 hrs） | `strategies/hrs/strategy.py`、`strategies/hrs/executor.py`、`strategies/hrs/config.yaml` |
| R05 | P1 | bug | `strategies/btc_eth/strategy.py:2656`；同构 `strategies/btc_eth_aggressive/strategy.py:2665` | btc-eth、btc-eth-aggr | 否（两个策略容器）³ | 两套 `strategy.py`、两份 `config.yaml` |
| R06 | P1 | bug | `strategies/btc_eth/strategy.py:2854`（关联 `:5384`）；`new_coin/executor.py:309`；`btc_eth_aggressive/strategy.py:2863` | btc-eth、btc-eth-aggr、new-coin（建议抽 shared 则全量）² | 视方案² | 见 R06「影响面」 |
| R07 | P1 | bug | `shared/database.py:308`（关联 `shared/position_ownership.py:109`、`btc_eth/strategy.py:2628`） | 全量 | **是** | `shared/database.py`、`shared/position_ownership.py`、各开仓调用方、数据库迁移脚本 |
| R08 | P1 | bug | `ai_tuner/cleanup/orphan_cleanup.py:416`（同函数 `:429-435` 有正确保护可参照） | ai-tuner | 否（仅 ai-tuner） | `ai_tuner/cleanup/orphan_cleanup.py`、`ai_tuner/config.yaml` |

> ¹ 全量重建：按 `deployment.md`，`shared/*` 变更触发 CI 重建所有策略容器 + ai-tuner + data-backend + dashboard-api。
> ² R06 跨 3 个策略，若把「等待成交」统一抽到 `shared/`（推荐，满足「禁止重复代码」硬约束）则触发全量重建；若各策略内部各自实现则违反重复代码规范。**该取舍见开放问题 Q2。**
> ³ **修订（2026-09-30）**：R05 同构逻辑最终抽取到 `shared/protection_retry.py` 两策略共用，实际**触发全量重建**，本表原「仅两策略容器」判断作废（详见架构方案 §7.7 与本文 §8.7 修订注）。

### 2.1 建议实施顺序

`R01`（独立、低耦合）→ `R03` / `R04` / `R05` / `R08`（单容器、互不依赖）→ `R02` / `R06` / `R07`（涉及 `shared/`，尽量合并为一次全量重建，减少停机次数）。

---

## 3. 全局约束（所有 R0x 必须遵守）

### 3.1 币安 PM 统一账户硬约束

1. **限价条件单不支持 `closePosition=true`**：`STOP` / `TAKE_PROFIT`（限价条件单）在 PM 账户若带 `closePosition=true` 会报 `[-4136]`；**市价条件单** `STOP_MARKET` / `TAKE_PROFIT_MARKET` 才支持 `closePosition=true`。因此平仓保护单传 `quantity` 替代 `closePosition` 时必须注意：全仓止损用限价条件单时应传数量；仅市价条件单可用 `closePosition`。
2. **ReduceOnly 单可能被拒**：PM 账户下平仓 `ReduceOnly` 单可能返回 `[-2022] ReduceOnly Order is rejected`。处理顺序必须是：先 `_sync_position_with_exchange` 对账，或先撤同方向条件单，再提交平仓单，避免 `[-4118]` / `[-4130]`。
3. **下单后约百 ms 级 API 可见延迟**：下单后立即查单会返回 `[-2013] Order does not exist`（现有 `_wait_for_order_fill` 已在首循环前 `sleep` 兜底，R06 需沿用该机制，且延迟值不得硬编码）。
4. **PM 净仓位由「原版 btc_eth / 激进版 btc_eth_aggressive」两个策略共享**：同一币种两策略会合并/抵消为同一净仓位，已有归属隔离机制（`shared/position_ownership.py`）。**R07 的目标是「加强」该机制，不是推翻。** 任何修复都不得破坏归属隔离既有语义。
5. **禁止服务器回测**：回测仅本地执行；本轮所有验证一律用本地 mock/单测，禁止在服务器跑回测。
6. **禁止硬编码**：所有阈值、重试次数、超时、延迟、偏移比例、开关必须来自配置文件或环境变量。凡本文件标注「新增配置项」的，编码时必须落到配置文件并提供默认值。

### 3.2 代码质量硬约束（编码环节强制）

- **禁止重复代码**：连续 5 行以上相同代码块必须提取公共函数；R06 三处同构逻辑、R05 两策略同构逻辑、R04 平仓校验逻辑必须评估是否可提取。
- **禁止幽灵参数**：未使用参数用 `_` 前缀（或直接删除）。
- 单函数 ≤ 50 行；单行 ≤ 120 字符（按字符数计）。
- 注释、日志一律中文。

### 3.3 全局术语表

| 术语 | 定义 |
|------|------|
| 成交确认 | 通过交易所查询确认订单 `status=FILLED` 且 `executedQty` 与预期一致 |
| 部分成交 | 订单 `executedQty > 0` 但 `executedQty < origQty` 后进入 `CANCELED`/`EXPIRED`，或超时撤单时已成交一部分 |
| 持仓对账 | 调用交易所持仓接口读取真实 `positionAmt`，与本地状态比对 |
| 保护完整性 | 真实持仓是否已挂齐硬止损/止盈等保护单的独立状态标记 |
| 减仓约束 | 平仓单必须保证只减不增仓（如 `reduceOnly`，或等价的对账前置） |
| 归属 | 同一 PM 账户内某币种未平开仓单所属的策略名 |
| 占用记录 | R07 新增的「某币种开仓中/持有中」持久化记录，用于跨策略互斥 |

---

## 4. R01 — K 线查询允许外部输入改变 SQL 结构

### 4.1 问题现状

`services/kline_service/api/routes.py` 中 `symbol`、`interval` 为公开查询参数，未经白名单校验即通过 `table_name = f"kline_{symbol.lower()}_{interval}"`（`routes.py:71`）拼入 SQL 表名。`_table_exists` 返回 false 后，代码尝试自动建表，**建表失败仅记 warning 并继续**（`routes.py:82-83`），随后执行 `query = f"SELECT * FROM {table_name} ..."`（`routes.py:88-93`）直接把外部输入拼进 SQL 结构。`/indicators`（`routes.py:164`、`:181-186`）存在完全相同的问题。Compose 将 8765 映射到接口，应用无认证，公网可达性取决于防火墙。报告复现：传入带 `CROSS JOIN` 的 `interval` 后，恶意 SQL 片段原样到达 `fetch_all`。

### 4.2 修复目标

所有对外查询接口的 `symbol`、`interval` 必须经**严格白名单校验**后才可用于构造表名，任何不满足白名单的请求在拼 SQL 之前即被拒绝；自动建表失败后**不得**继续执行查询；表名必须在「已注册/已知标的」范围内。

### 4.3 功能需求

- **R01-F1 统一校验入口**：新增一个统一的表名解析/校验函数（单词点，R01 与 indicators、registry、collect 共用，禁止各处重复实现）。输入 `symbol`、`interval`，输出「合法表名」或「校验失败」。
- **R01-F2 白名单来源（内存校验 + 数据库校验双层）**：
  - 内存层：`symbol` 须命中「固定标的 ∪ `SymbolRegistry` 已激活标的 ∪ `settings.SYMBOLS`」；`interval` 须命中「`settings.COLLECT_INTERVALS` ∪ 已注册标的的 `intervals`」。
  - **不新建表名正则之外的宽松规则**；表名本身还须匹配严格正则（见配置项）。
- **R01-F3 建表失败即终止**：`_table_exists` 为 false 且自动建表失败（或 `collector` 不可用）时，立即返回「无数据/数据不可用」，**禁止**继续执行该表查询。
- **R01-F4 覆盖所有拼接点**：`/klines/latest`、`/indicators`、`/collect/manual`（`symbol`/`interval` 同样会用于建表/查询）、`registry_routes` 中所有以 symbol/interval 构造表名/采集任务的入口，全部改走 R01-F1。
- **R01-F5 接口访问限制**：见开放问题 Q1（是否加鉴权/网络限制）；若结论为「加」，则须在路由层统一实现，不得散落。

### 4.4 验收标准

- **R01-AC1**：请求 `/api/v1/klines/latest?symbol=BTCUSDT';DROP TABLE x;--&interval=1h` → 返回参数非法错误（HTTP 4xx），**不执行任何 SQL**，数据库无副作用。
- **R01-AC2**：请求 `interval=1h CROSS JOIN ...` → 4xx，SQL 未到达 `fetch_all`（可用假连接断言 `fetch_all` 未被调用）。
- **R01-AC3**：`symbol` / `interval` 均合法但表不存在且自动建表失败 → 返回「无数据」，且**不执行** `SELECT * FROM ...`。
- **R01-AC4**：合法请求（如 `BTCUSDT`/`1h`）→ 行为与修复前一致（正常返回数据），无回归。
- **R01-AC5**：`/indicators`、`/collect/manual` 对非法输入的拒绝行为与 `/klines/latest` 一致。
- **R01-AC6**：表名正则单元测试覆盖：合法 `kline_btcusdt_1h`、`kline_ethusdt_15m`、`kline_solusdt_1d` 通过；注入样例、含空格/分号/引号/CROSS JOIN/大写可疑片段全部拒绝。

### 4.5 边界与异常场景

- 已注册标的的 `intervals` 动态变化（注册后新增周期）：校验必须实时反映 `SymbolRegistry` 缓存，不得只在启动时快照一次。
- 数据库不可用：`SymbolRegistry` 未初始化时，退化为「固定标的 ∪ settings.SYMBOLS」白名单，且不得因校验层异常而放行任意输入（失败即拒绝）。
- 大小写：`symbol` 统一大写比对，表名小写生成；`interval` 严格精确匹配。
- 并发：校验不得引入跨请求共享可变状态导致的竞态。

### 4.6 配置化要求

| 配置项 | 建议默认值 | 位置 | 说明 |
|--------|-----------|------|------|
| `SYMBOLS` | 现有值 `BTCUSDT,ETHUSDT,BNBUSDT` | `services/kline_service/shared/core/config.py`（环境变量，已存在） | 白名单来源之一 |
| `COLLECT_INTERVALS` | 现有值 `15m,1h,4h,1d`（环境变量，已存在） | 同上 | interval 白名单来源之一 |
| `TABLE_NAME_PATTERN`（**新增**） | `^kline_[a-z0-9]{3,20}_([0-9]+[mhdw]|1M)$` | `config.py`（Pydantic Settings，环境变量可覆盖） | 表名严格正则；禁止硬编码在路由内 |
| `KLINE_API_AUTH_ENABLED` / `KLINE_API_TOKEN`（**新增，视 Q1 结论**） | `false` / 空 | `config.py` | 若决定鉴权则启用 |
| 固定标的列表（**建议新增**） | `["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","TRXUSDT"]` | `config.py` 或复用 `main.py` 常量 | 目前硬编码在 `main.py:173`，建议提取为配置 |

> 注：`routes.py` 内不得出现任何字面量白名单/正则，必须从配置读取。

### 4.7 影响面

仅 `services/kline_service/*`，只重建 kline-service 容器。**不触发全量重建**。向后兼容：合法请求行为不变；非法请求从「可能 500/注入」变为「4xx 拒绝」，属预期收紧。

> 修订（2026-09-30）：本次修复的「非法请求一律 4xx」为 R01 当时的**写路径语义**。读路径（`/klines/latest`、`/indicators`）已由 fix-2026-09-30 **P0-2** 放宽为「白名单 OR（格式合法且数据表已存在）」放行；**写路径（注册/建表/采集）语义不变**。详见 [P0 修复架构方案：持仓保护单丢失](fix-2026-09-30-p0-protection-orders-missing-architecture.md) §4。

### 4.8 风险与回滚

- 风险：白名单过严可能拒绝历史上合法但未登记的 `symbol`（如策略新币）→ 缓解：白名单须包含 `SymbolRegistry` 全部激活标的；上线前核对线上已注册标的清单。
- 风险：`interval` 正则漏掉合法周期（如 `1w`、`1M`）→ 缓解：以线上 `registered_symbols.intervals` 实际值为准设计正则。
- 回滚：revert 该 commit（仅 kline-service 重建）。

> 修订（2026-09-30）：上述「白名单过严拒绝历史上合法但未登记的 symbol」风险，已由 fix-2026-09-30 **P0-2** 在读路径上缓解——新增读路径入口 `build_readable_table_name`，「数据表已存在且格式合法」的标的可绕过白名单读取（`ALLOW_EXISTING_TABLE_SYMBOLS` 默认 `true`）；写路径白名单语义与注入防护（严格正则 + 参数化查询）保持不变。回退方式：关闭 `ALLOW_EXISTING_TABLE_SYMBOLS`。详见 [P0 修复架构方案：持仓保护单丢失](fix-2026-09-30-p0-protection-orders-missing-architecture.md) §4、§8。

---

## 5. R02 — 非幂等下单被通用重试器重复提交

### 5.1 问题现状

`shared/binance_api.py:195` 的 `_request` 被 `@retry_on_failure(max_retries=3, delay=1.0, ...)` 装饰，**对所有 method 一体重试**，包括开仓 POST。`shared/utils.py:63` 的 `wrapper` 对每个失败请求重新执行整个函数。`place_order`（`binance_api.py:427-552`，`_request("POST", endpoint, params)` 在 `:538`）**未生成 `newClientOrderId`**，也不查询前一请求结果。若交易所已受理而响应超时，重试会创建**新订单**；默认 `max_retries=3` 即最多 **4 次**提交。报告复现：第一次请求已受理但响应 `TimeoutError`、第二次成功 → 交易所接受订单 `[1,2]`，调用方只拿到订单 2。

### 5.2 修复目标

读请求与非幂等写请求的重试策略分离；非幂等下单在重试前必须先核对「上一请求结果」，只有确认「未受理」才可重发；同一交易意图在交易所侧至多产生一个订单。

### 5.3 功能需求

- **R02-F1 重试策略分级**：
  - 读请求（GET 类，如查单/查持仓/查余额/查精度）：保持现有重试语义（可重试瞬时错误）。
  - 非幂等写请求（下单 POST / 撤单除外的写操作）：默认**不重试**，或仅在满足 R02-F2 的幂等前提下重试。
- **R02-F2 交易意图稳定标识**：`place_order` 必须为每次「交易意图」生成稳定 `newClientOrderId`（同一意图多次重发使用同一 ID；不同意图不同 ID），并随请求发送。交易所对同 `newClientOrderId` 的重复提交会拒绝，从而天然去重。
- **R02-F3 未知结果先核对再决策**：写请求遇到「结果未知」（超时、连接中断、`TimeoutError` 等）时：
  1. 先用 `newClientOrderId` 查询该订单（新增按客户订单号查询的能力）；
  2. 查到订单 → 返回该订单，**不重发**；
  3. 确认交易所无该订单 → 才允许按策略重发（重发仍用同一 `newClientOrderId`）。
- **R02-F4 调用方接口兼容**：`place_order` 的对外签名/返回结构保持向后兼容（调用方仍可用 `orderId` 等字段）；`newClientOrderId` 作为新增可选入参或在内部生成后回传，不得破坏现有调用点。
- **R02-F5 与其他写接口一致**：`place_conditional_order`、`cancel_order` 等写接口的重试/幂等策略同样按 R02-F1 分级（撤单对 `-2011` 幂等；下单严禁盲重发）。

### 5.4 验收标准

- **R02-AC1**：模拟首次下单响应超时、第二次请求成功 → 交易所侧**只接受 1 个订单**；调用方拿到的订单即该唯一订单。
- **R02-AC2**：写请求超时后，代码先发起「按 clientOrderId 查单」；查到则直接返回，不再调用下单端点。
- **R02-AC3**：读请求（如查持仓）在瞬时错误下仍按配置次数重试（行为不回归）。
- **R02-AC4**：同一交易意图两次提交发送**相同** `newClientOrderId`；不同意图发送不同 ID（单测断言）。
- **R02-AC5**：`place_order` 现有调用方（各策略开仓/平仓）**无需改动即可编译运行**（兼容性）。

### 5.5 边界与异常场景

- 交易所「真正未受理」与「已受理但不可见」必须区分：靠 `newClientOrderId` 查单，不能用「查不到就直接重发」。
- 查单本身也可能因 PM 可见延迟返回 `[-2013]`：需在查单环节做有限次重试（次数/间隔配置化），超过后按「未知」处理（保留占用、告警，不盲目重发）。
- 连续超时 + 查单也全失败：必须进入「未知结果」保守路径——不得再次提交，返回明确「未知」给调用方，由上层决定后续。
- 并发同意图：同一 `newClientOrderId` 并发提交时交易所行为由交易所保证，代码不得自行去重缓存造成内存泄漏。
- 向后兼容：`-9999`/`-2011`/`-2013`/`-4108` 等既有「不重试错误码」语义保持。

### 5.6 配置化要求

`binance_api.py:195` 的 `max_retries=3` / `delay=1.0` / `backoff=2.0` 目前**硬编码**，必须提取。

| 配置项 | 建议默认值 | 位置 | 说明 |
|--------|-----------|------|------|
| `api_retry.read.max_retries` | 3 | **新增** `shared/api_retry_config.yaml`（经 `load_shared_config` 读取） | 读请求重试次数 |
| `api_retry.read.delay_seconds` | 1.0 | 同上 | 初始延迟 |
| `api_retry.read.backoff` | 2.0 | 同上 | 退避系数 |
| `api_retry.write.max_retries` | 0（默认不重试） | 同上 | 写请求重试次数，仅幂等核对通过后生效 |
| `api_retry.write.retry_after_verify` | true | 同上 | 是否允许「核对未受理后」重发 |
| `api_retry.client_order_id_prefix` | `"sq"` | 同上 | `newClientOrderId` 前缀 |
| `api_retry.order_verify_max_attempts` | 3 | 同上 | 超时后按 clientOrderId 查单次数 |
| `api_retry.order_verify_interval_seconds` | 0.5 | 同上 | 查单间隔（兼顾 PM 可见延迟） |

> 若项目已有等价 shared 配置文件命名习惯，可按现有命名落地，但**必须经 `shared/config_loader.py` 读取**（禁止硬编码），并允许环境变量覆盖。

### 5.7 影响面

改动 `shared/binance_api.py`、`shared/utils.py` → **触发全量重建**（所有策略容器 + ai-tuner + data-backend + dashboard-api）。向后兼容：`place_order` 签名扩容但不破坏现有调用；重试语义变化需回归所有下单路径。

### 5.8 风险与回滚

- 风险：`newClientOrderId` 与策略侧「同一意图」的界定不清，可能导致同一次真实开仓被误判为两次意图而生成两个 ID → 缓解：由调用方在「一次交易决策」内复用同一 ID（架构环节明确 ID 生成时机与生命周期）。
- 风险：查单重试引入额外延迟 → 缓解：次数/间隔配置化且默认较小。
- 风险：写请求「不重试」后，原本靠重试扛住的瞬时网络抖动会直接失败 → 缓解：保留「核对未受理后重发」路径。
- 回滚：revert commit（全量重建）。

---

## 6. R03 — 新币平仓重试原数量，部分成交后可能反向开多

### 6.1 问题现状

`strategies/new_coin/executor.py:_close_position`（`:2491` 起）中 `close_quantity = position_amt * close_percent` 只在**重试循环外**计算一次（`:2525`）。首笔 BUY 部分成交并撤单后，下一笔仍买入**原数量**且**未设 reduceOnly**（`:2581-2588`）；市价兜底沿用同一数量（`:2661-2666`）。止盈单同时成交时也有相同过量平仓风险。报告复现：初始 `position=-1`，第一笔 BUY 1 成交 0.4 后撤单，第二笔继续 BUY 1 → 最终 `position=+0.4`，函数返回 `True`。

### 6.2 修复目标

平仓过程中任何一次提交的数量都只能等于「当前剩余待平数量」；所有平仓请求带减仓约束；重试/兜底不得把仓位打成反向；仅当「目标平仓量已达成且交易所真实持仓已对账」时才返回成功。

### 6.3 功能需求

- **R03-F1 剩余量重算**：进入每次提交（含限价重试、市价兜底）前，必须重新读取交易所真实持仓并计算「剩余待平量 = 目标平仓量 − 已成交累计量」；剩余量为 0 立即结束。
- **R03-F2 撤单后确认最终成交量**：每次撤单后必须读取该订单最终 `executedQty` 并累加；不得以「原计划数量」继续下一笔。
- **R03-F3 减仓约束**：所有平仓请求必须带减仓约束；PM 账户下若 `reduceOnly` 被拒 `[-2022]`，须先 `_sync_position_with_exchange` 或先撤同方向条件单后再提交（遵守 §3.1 约束 2）。
- **R03-F4 兜底同量约束**：市价兜底只能提交「剩余待平量」，不得使用初始 `close_quantity`。
- **R03-F5 成功语义收紧**：返回值 `True` 仅当「剩余待平量为 0 / 达到 `close_percent` 目标」且经持仓对账确认；否则返回失败并保留重试/告警路径。
- **R03-F6 伪代码级状态流转**：
  ```
  target = position_amt * close_percent
  closed = 0
  while closed < target and attempts <= max_retries:
      remaining = target - closed
      order = place(side=BUY, qty=min(remaining, 当前可平量), reduce_only=True)
      wait fill(timeout) 或 超时撤单
      closed += order.executedQty (撤单后必须重读)
      # 每轮结束对账真实 positionAmt，防止反向
      if 真实持仓已为 0: break
  return closed >= target 且 对账通过
  ```

### 6.4 验收标准

- **R03-AC1（报告复现场景）**：`position=-1`，第一笔 BUY 1 成交 0.4 后撤单，第二笔提交数量必须为 **0.6**（而非 1）；最终 `position=0`；函数返回 `True`。
- **R03-AC2**：若剩余量因精度截断为 0 且真实持仓 > 0，函数返回「失败/需人工处置」，**不得**提交任何反向单。
- **R03-AC3**：所有平仓提交（限价 + 市价兜底）均带减仓约束（断言请求参数）。
- **R03-AC4**：撤单时订单恰好成交（撤单抛 `-2011`）→ 视为已成交并累加 `executedQty`，不再补单。
- **R03-AC5**：止盈单与平仓单叠加导致超量场景 → 以交易所真实持仓为准，不产生反向仓。
- **R03-AC6**：`-2022` 拒绝路径：先对账/撤同向条件单后重试，重试成功，不报 `[-4118]`/`[-4130]`。

### 6.5 边界与异常场景

- 部分成交 + 撤单竞态（查询与撤单之间刚好成交）。
- 交易所超时/未知结果（订单是否成交未知）→ 必须先查单确认，再决定是否继续。
- 精度截断导致剩余量为 0 但仍有微小持仓。
- 平仓过程中同币新开仓（理论上同策略不应发生，但仍需以交易所持仓为准）。
- 重复调用幂等：连续两次调用 `_close_position`，第二次应识别「已无空头持仓」直接返回。
- 数据库不可用不应阻塞平仓对账（平仓以交易所为准）。

### 6.6 配置化要求

`close_position` 段已存在（`strategies/new_coin/config.yaml:217-222`）。需**新增**：

| 配置项 | 建议默认值 | 位置 | 说明 |
|--------|-----------|------|------|
| `trading.close_position.reduce_only` | `true` | `new_coin/config.yaml` | 是否对平仓单加减仓约束 |
| `trading.close_position.sync_before_reduce_only` | `true` | 同上 | `-2022` 时是否先对账/撤同向单 |
| `trading.close_position.position_confirm_retries` | 2 | 同上 | 对账真实持仓的重试次数 |
| `trading.close_position.position_confirm_interval` | 1（秒） | 同上 | 对账间隔 |

> `max_retries`/`retry_interval`/`poll_interval`/`timeout` 沿用现有值，不得新增硬编码。

### 6.7 影响面

仅 `strategies/new_coin/*`，只重建 new-coin 容器。向后兼容：正常全额成交平仓行为不变；部分成交/超时路径行为收紧。

### 6.8 风险与回滚

- 风险：`reduceOnly` 与 PM 规则冲突导致平仓单被拒 → 缓解：按 `sync_before_reduce_only` 先对账/撤同向条件单；若仍被拒，保留既有「市价兜底 + 对账」路径与告警（**该取舍见开放问题 Q3**）。
- 风险：每轮重算持仓增加 API 调用、触及频率限制 → 缓解：`position_confirm_retries`/间隔配置化且默认小。
- 回滚：revert commit（仅 new-coin 重建）。

---

## 7. R04 — HRS 平仓失败或未成交仍撤保护单并删除持仓

### 7.1 问题现状

`strategies/hrs/strategy.py` 中时间止损分支（`:1922-1966`）与移动止盈分支（`:1969-2001`）在调用 `close_position` 后**不检查返回值**，直接 `_writeback_pnl` → `cancel_all_orders(symbol)`（`:1939`/`:1987`）→ `remove_position(symbol)`（`:1940`/`:1988`）。而执行器 `close_position`（`executor.py:863-940`）异常时返回 `None`（`:938-940`），**正常限价单仅收到 NEW 回执也当成功返回**（`:917-934` 未等待成交）。报告复现：`close_position` 返回 `None` 时，`_monitor_positions` 仍调用 `cancel_all_orders` 一次、`remove_position` 一次。后果：真实仓位可能仍在，但本地状态与交易所止损被一起清除。

### 7.2 修复目标

只有「成交/持仓对账确认归零」后才允许回写盈亏、撤保护单、删除持仓状态；平仓失败或部分成交时保留（或重建）剩余仓位的保护，并告警、下轮重试。

### 7.3 功能需求

- **R04-F1 平仓受理与成交分离**：`executor.close_position` 返回结构必须区分「受理（NEW/未成交）」「部分成交」「完全成交」「失败（None/异常）」，或返回带 `status`/`executed_qty` 的结构化对象；**不得**把「收到 NEW 回执」当作成功。
- **R04-F2 成交确认**：`close_position` 提交后须等待成交或对账真实持仓；仅当真实持仓已归零（或达到 `close_percent` 目标）才判定成功。
- **R04-F3 分支返回校验**：时间止损/移动止盈分支必须依据 R04-F1 的结果：
  - 完全成交 → 原有回写、撤单、`remove_position` 流程；
  - 失败/部分成交 → **不**撤保护单、**不** `remove_position`；保留/重建保护单；发送告警；下一监控周期重试。
- **R04-F4 剩余仓位保护**：部分成交后，剩余仓位必须有对应止损保护（重建或保留原单并按剩余量调整）。
- **R04-F5 幂等与重试**：多次进入监控周期对同一未平仓位重复平仓必须幂等，不产生超额单（与 R03 同类约束）。

### 7.4 验收标准

- **R04-AC1**：`close_position` 返回 `None`（失败）→ `cancel_all_orders` **不被调用**、`remove_position` **不被调用**、盈亏不回写；仓位保留在 `position_manager` 中；发送失败告警。
- **R04-AC2**：`close_position` 返回「仅 NEW 未成交」→ 同上（不撤单、不删仓）。
- **R04-AC3**：`close_position` 返回「完全成交」→ 正常回写、撤单、`remove_position`（不回归）。
- **R04-AC4**：部分成交（如平了 40%）→ 保留仓位，剩余量有止损保护，下轮继续平仓。
- **R04-AC5**：连续两轮均失败 → 不产生重复超额平仓单；告警可观测。

### 7.5 边界与异常场景

- 撤保护单失败（如交易所不可用）：不得因清理失败而删除本地仓位状态（否则彻底失去管理）。
- 真实持仓已归零但本地仍有状态（人工/交易所侧平仓）：对账后应正确判定为「已平」并清理。
- `-2022`/`-4118`/`-4130`：按 §3.1 约束 2 处理。
- 数据库不可用：对账以交易所为准，写库失败不应阻塞仓位保留判定。
- 重复调用幂等。

### 7.6 配置化要求

`strategies/hrs/config.yaml` 已含 `retry_count: 2`、`retry_interval: 1.0`（`:12-13`）。需**新增/复用**：

| 配置项 | 建议默认值 | 位置 | 说明 |
|--------|-----------|------|------|
| `trading.close_order.wait_timeout_seconds`（**新增**） | 10 | `hrs/config.yaml` | 平仓单等待成交超时（对齐 btc_eth `close_limit_order.timeout_seconds`） |
| `trading.close_order.poll_interval_seconds`（**新增**） | 2 | 同上 | 成交轮询间隔 |
| `trading.close_order.reduce_only`（**新增**） | `true` | 同上 | 平仓减仓约束 |
| `trading.close_order.sync_before_reduce_only`（**新增**） | `true` | 同上 | `-2022` 前置对账 |
| `trading.close_order.keep_protection_on_partial`（**新增**） | `true` | 同上 | 部分成交是否保留保护 |
| `trading.retry_count` / `retry_interval` | 现有 `2` / `1.0` | 同上 | 复用 |

### 7.7 影响面

仅 `strategies/hrs/*`，只重建 hrs 容器。向后兼容：完全成交路径不变；失败/未成交路径由「误清理」变为「保留 + 告警」。

### 7.8 风险与回滚

- 风险：收紧后，真正已平仓但交易所对账延迟导致误判「未平」→ 缓解：对账带重试次数/间隔；误判为未平时只是延迟清理，不产生资金风险。
- 风险：保留仓位导致保护单重复叠加（旧单未撤 + 重建）→ 缓解：重建前先撤旧单，撤失败则保留旧单不叠新单。
- 回滚：revert commit（仅 hrs 重建）。

---

## 8. R05 — MTPCS 保护单失败后丢失已成交开仓的管理状态

### 8.1 问题现状

`strategies/btc_eth/strategy.py:_open_new_position`（`:2606`）中，入场成交后调用 `_place_entry_protection_orders`（`:2657`），**任一保护单失败即 `return False`**（`:2658-2659`），而 `self.positions[symbol]` 直到**全部成功**才写入（`:2662-2663`）。止损失败会留下无止损仓位；TP 失败也留下不受本地完整管理的仓位。激进版同构（`:2665-2673`）。报告复现：入场 `FILLED`、保护单失败 → 方法返回 `False`，`positions` 仍为空。

### 8.2 修复目标

入场确认为真实成交后，必须**先登记真实持仓**，把「保护完整性」作为独立状态；保护单失败不能把已成交交易当作未发生，须进入可重试补挂或减仓兜底流程。

### 8.3 功能需求

- **R05-F1 成交即登记**：入场订单确认为成交（`FILLED`，含部分成交确认，见 R06）后，立即写入 `self.positions[symbol]`（含真实成交量、入场价、方向），并持久化状态；**此步骤不得晚于保护单创建**。
- **R05-F2 保护完整性独立状态**：`PositionState` 增加「保护完整性」标记（如 `protection_pending` / 各保护单 ID 可空），表示「已持仓但保护未挂齐」。
- **R05-F3 失败不丢态**：任一保护单失败时，`_open_new_position` **不得**`return False` 丢失仓位；应返回「持仓已建立、保护待补」的结果，并保留已成功创建的保护单 ID。
- **R05-F4 补挂/兜底**：保护失败进入「可重试补挂」流程（配置化次数/间隔）；补挂仍失败时按配置进入「减仓兜底」（平掉该仓位以消除无保护风险）或持续告警（**该取舍见开放问题 Q4**）。
- **R05-F5 两策略一致**：`btc_eth` 与 `btc_eth_aggressive` 同构逻辑必须一致修复；重复代码评估提取公共实现（遵守 §3.2）。
- **R05-F6 伪代码级状态流转**：
  ```
  entry = place_entry(); entry = wait_fill(...)   # 含部分成交
  if entry.executed_qty <= 0: return 未成交
  positions[symbol] = build_state(entry)          # 先登记
  persist(positions)
  ids, ok = place_protection()
  positions[symbol].protection_pending = not ok
  if not ok: 记录补挂任务(告警)
  ```

### 8.4 验收标准

- **R05-AC1（报告复现场景）**：入场 `FILLED`、止损单失败 → 方法返回「持仓已建立、保护待补」；`self.positions[symbol]` **非空**且含真实成交量。
- **R05-AC2**：入场 `FILLED`、TP1 失败但止损成功 → 持仓已登记，`stop_loss_order_id` 有值，`protection_pending=True`，进入补挂。
- **R05-AC3**：补挂成功 → `protection_pending` 置 `False`，保护单 ID 齐备。
- **R05-AC4**：补挂达到配置重试上限仍失败 → 按配置执行减仓兜底或持续告警（行为符合所选策略）。
- **R05-AC5**：激进版对以上场景行为一致（同构测试）。

### 8.5 边界与异常场景

- 入场部分成交（R06）：登记的量必须是实际成交量，保护单量按实际量。
- 保护单创建「未知结果」（超时）：先按 R02 核对，再决定补挂，避免重复保护单。
- 状态持久化失败（DB 不可用）：内存 `positions` 仍须保留；下次补挂/监控依赖内存与交易所对账。
- 进程在「登记后、补挂前」重启：启动恢复流程须能识别 `protection_pending` 并补挂（评估现有 `_startup_orphan_cleanup`/恢复路径）。
- 重复调用幂等：同 symbol 已有持仓不得重复登记。

### 8.6 配置化要求

**新增**（两份 config 均需，键名一致）：

| 配置项 | 建议默认值 | 位置 | 说明 |
|--------|-----------|------|------|
| `risk.protection_retry.max_retries` | 3 | `btc_eth/config.yaml` + `btc_eth_aggressive/config.yaml` | 保护补挂重试次数 |
| `risk.protection_retry.retry_interval_cycles` | 1 | 同上 | 补挂重试间隔（主循环周期数） |
| `risk.protection_retry.on_exhausted` | `"alert"`（可选 `"reduce"`） | 同上 | 补挂耗尽后的动作（告警 / 减仓兜底） |
| `risk.protection_retry.notify` | `true` | 同上 | 是否告警 |

> 归属/互斥、`cancel_retry`、`close_limit_order` 等既有配置沿用。

### 8.7 影响面

`strategies/btc_eth/*` + `strategies/btc_eth_aggressive/*`，重建 btc-eth 与 btc-eth-aggr 两个容器。**不触发全量重建**。向后兼容：正常（保护单全部成功）路径行为不变。

> **修订（2026-09-30）**：实现阶段按用户决策将 R05 与 R06/R07 的两策略同构逻辑抽取到 `shared/protection_retry.py` 共用（彻底消除重复代码），故 R05 **改为触发全量重建**（`shared/*` 变更）。参数落地位置与最终部署影响面以架构方案 §7.7 为准。

### 8.8 风险与回滚

- 风险：登记后保护未挂齐期间，若减仓兜底触发误平真仓 → 缓解：`on_exhausted` 默认 `alert`，`reduce` 需人工确认开启。
- 风险：`protection_pending` 状态与重启恢复/归属过滤（`position_ownership.py`）交互不清 → 缓解：架构环节明确该状态对归属判定/上报的影响（**见开放问题 Q4**）。
- 回滚：revert commit（两策略容器重建）。

---

## 9. R06 — 入场超时撤单忽略部分成交和撤单竞态

### 9.1 问题现状

`_wait_for_order_fill`（`btc_eth/strategy.py:5384`）只把 `FILLED` 当成交，对 `CANCELED`/`EXPIRED`/`REJECTED` **即使 `executedQty>0` 也返回 `None`**（`:5384-5390`）；超时路径（`:5394-5399`）返回 `None` 后调用方撤单（`:2857-2861`）**不读取最终成交量**。`new_coin/executor.py:309-334`、`btc_eth_aggressive/strategy.py:2863` 同类。报告复现：订单 `CANCELED`、`executedQty=0.4`、`origQty=1` → 入场方法返回 `None`，撤单回执中的成交量也未处理。后果：已成交的部分仓位不进入正常保护流程。

### 9.2 修复目标

入场等待/撤单过程必须处理「部分成交」与「查询-撤单竞态」；只要累计成交 `executedQty > 0`，该部分仓位就必须进入建仓保护或明确减仓清零，绝不静默丢弃。

### 9.3 功能需求

- **R06-F1 统一等待成交助手**：将「等待订单达到终态并返回结构化结果」抽为**单点公共实现**（推荐放 `shared/`，避免 3 处重复，遵守 §3.2）。返回结构至少含：`status`、`executed_qty`、`avg_price`、原始订单；**不得只返回 `Optional[Dict]`**。
- **R06-F2 部分成交识别**：终态为 `CANCELED`/`EXPIRED`/`REJECTED` 但 `executedQty > 0` 时，返回「部分成交」结果（含成交量），由调用方进入保护/减仓流程。
- **R06-F3 超时撤单后读最终量**：超时路径撤单后必须重新查询订单最终状态与累计成交量，并据此返回「完全未成交 / 部分成交 / 完全成交」。
- **R06-F4 撤单竞态**：撤单抛 `[-2011]`/`[-2013]`（订单已成交/已不存在）时，重新查单获取最终 `executedQty`，按成交结果处理，**不得**当作「未成交」丢弃。
- **R06-F5 三处一致**：`btc_eth`、`btc_eth_aggressive`、`new_coin` 三处入场等待逻辑统一走 R06-F1；调用方（建仓主流程）据返回结果决定「建仓 + 挂保护」或「放弃」。
- **R06-F6 首循环可见性延迟**：沿用现有「首循环前小等」机制，延迟值来自配置（不得在公共助手里硬编码）。

### 9.4 验收标准

- **R06-AC1（报告复现场景）**：订单 `CANCELED`、`executedQty=0.4`、`origQty=1` → 等待助手返回「部分成交，0.4」；入场方法据此**建立 0.4 的持仓并挂保护**（而非返回 `None`）。
- **R06-AC2**：超时后撤单，重新查单得到 `executedQty=0.4` → 按部分成交处理。
- **R06-AC3**：超时后撤单，重新查单得到 `executedQty=0` → 按未成交处理（行为与修复前一致）。
- **R06-AC4**：撤单抛 `-2011` 且实际已成交 → 查单确认成交量并按成交处理。
- **R06-AC5**：`FILLED` 路径行为不变（不回归）。
- **R06-AC6**：三个策略分别对以上场景结果一致（同构测试）。

### 9.5 边界与异常场景

- 部分成交极小量（低于最小下单/精度）：按精度截断后可能为 0 → 需明确「减仓清零」或「按最小可处理量建仓」策略（**见开放问题 Q5**）。
- 查询-撤单竞态：查询返回挂单中、撤单瞬间成交。
- 交易所超时/未知结果：先按 R02 核对。
- `-2013` PM 可见延迟：助手内消化重试，不落外层直接返回「未成交」。
- 重复调用幂等：同一订单多次查询/撤单不产生副作用。

### 9.6 配置化要求

| 配置项 | 建议默认值 | 位置 | 说明 |
|--------|-----------|------|------|
| `entry_order_timeout_seconds` | btc_eth `300`、new_coin `60`（现有） | 各策略 config | 沿用 |
| `order_fill_check_interval_seconds`（**新增**统一键） | 2 | 各策略 config / 公共助手入参 | 轮询间隔 |
| `pm_order_visibility_delay_seconds`（**新增**） | 0.5 | 公共助手配置（shared 或各策略） | PM 首查可见延迟（当前硬编码 `min(check_interval, 0.5)`） |
| `order_final_state_read_retries`（**新增**） | 2 | 同上 | 撤单后读最终状态重试次数 |

### 9.7 影响面

**推荐方案**：助手抽到 `shared/` → 触发**全量重建**。替代方案：各策略各自实现 → 违反 §3.2「禁止重复代码」（不可取）。因此本轮 R06 实际按全量重建处理（与 R02/R07 合并一次重建）。

### 9.8 风险与回滚

- 风险：部分成交建仓后若紧接着价格不利，保护单可能立即触发 → 属正常风控，但需确认保护单量与部分成交量一致（防超量）。
- 风险：抽公共助手改动面大，回归风险高 → 缓解：保留各策略现有调用点语义，仅替换内部实现；补三策略同构测试。
- 回滚：revert commit（全量重建）。

---

## 10. R07 — 归属 advisory lock 没有覆盖开仓预占，无法互斥

### 10.1 问题现状

`shared/database.py:fetch_one_advisory_lock`（`:282-313`）在事务内 `pg_advisory_xact_lock` 后执行查询，**事务结束即释放锁**。调用方 `shared/position_ownership.py:_resolve_owner_with_lock`（`:90-125`）读完后返回；`btc_eth/strategy.py:_open_new_position` 在归属判定（`:2628-2646`）之后才设置杠杆、仓位检查、下单、写交易记录（`:2648-2663`）。因此**两个策略可依次读到「无归属」并同时开同币仓位**；共享 PM 净仓位合并/抵消后，单一归属判断无法撤销结果。报告复现：并发执行两个策略，A、B 的 `blocked` 均为 `false`，均获准进入开仓。

### 10.2 修复目标

「判定归属」与「占位」必须原子完成：在 advisory lock 内**原子创建持久化的币种占用/交易意图记录**，再释放锁去做外部请求；以唯一约束 + 超时恢复维护占用状态，从而真正互斥同币开仓。

### 10.3 功能需求

- **R07-F1 锁内原子占位**：新增「占用记录」写入操作，与归属查询在**同一 advisory lock 事务内**完成：查归属 → 若无冲突则同时写入本策略的占用记录；两者原子。
- **R07-F2 唯一约束互斥**：占用记录对「同一 symbol 的有效占用」建立唯一约束（部分唯一索引），使两个策略并发时**至多一个**成功；另一个拿到冲突并按「已被持有」跳过。
- **R07-F3 释放锁后做外部请求**：占位成功后释放锁，再执行设杠杆、下单、写交易记录；**外部请求不得在锁内**（避免长事务阻塞）。
- **R07-F4 占用生命周期管理**：
  - 开仓成功/失败/放弃 → 相应更新或释放占用；
  - 平仓归零 → 释放占用；
  - 进程崩溃 → 依赖「超时过期」恢复（占用记录带 `expires_at`，过期后可被清理/重占）。
- **R07-F5 与现有归属机制兼容**：`resolve_position_owner`、`filter_owned_positions`、`is_symbol_owned_by_other` 的既有语义（基于 `trade_records` 未平开仓单归属）不得破坏；占用记录是「开仓前的预占」，归属判定仍是「开仓后的权威」。两者需明确衔接（**占用记录与 `trade_records` 的对应关系见开放问题 Q6**）。
- **R07-F6 降级与容错**：DB 不可用时按现有「查询异常返回 None → 保守不放行双开」的处理；占用写入失败不得静默放行。
- **R07-F7 数据恢复**：启动/定时任务清理过期占用，避免残留占用永久阻塞某币种。

### 10.4 验收标准

- **R07-AC1（报告复现场景）**：A、B 两策略并发对同 symbol 判定+占位 → **至多一个** `blocked=false`（获准），另一个 `blocked=true`（被互斥），**不再两者同时获准**。
- **R07-AC2**：A 占位成功后 B 再次尝试 → B 被拒；A 平仓归零释放后 B 可再占位。
- **R07-AC3**：A 占位后进程崩溃、超时过期 → 过期后 B 可占位（不永久阻塞）。
- **R07-AC4**：外部请求（下单）在锁**外**执行（断言锁持有期间未发生下单调用）。
- **R07-AC5**：归属判定既有测试（`position_ownership`）全部不回归。
- **R07-AC6**：占用写入冲突时，代码走「已被持有，跳过开仓」并告警，不抛异常中断主循环。

### 10.5 边界与异常场景

- 两策略真并发（同时进入判定）。
- 占用写入成功后下单失败 → 必须释放/标记占用，避免残留。
- 占用写入成功后进程崩溃 → 超时恢复。
- 归属为「本策略」（加仓场景）→ 允许（不得被自身占用互斥误拦）。
- 数据库主线不可用 / advisory lock 不可用（测试 mock）→ 降级到普通查询 + 保守策略。
- 重复调用幂等：同一策略重复占位自身不冲突。
- 与 R05「保护待补」状态的交互：占位在开仓时建立，不因保护失败而被误释放。

### 10.6 配置化要求

| 配置项 | 建议默认值 | 位置 | 说明 |
|--------|-----------|------|------|
| `ownership.claim_ttl_minutes`（**新增**） | 30 | 各策略 config `ownership` 段 或 shared 配置 | 占用记录有效期（超时恢复） |
| `ownership.claim_cleanup_interval_minutes`（**新增**） | 10 | 同上 | 过期占用清理周期 |
| `ownership.enabled`（**新增**） | `true` | 同上 | 开关，便于灰度/回滚 |
| `ownership.lock_timeout_seconds`（**新增**） | 5 | 同上 | advisory lock 获取/事务超时保护 |

> 现有 `ownership.record_name`、`ownership.competing_record_names` 沿用。

### 10.7 影响面

改动 `shared/database.py`、`shared/position_ownership.py`、各开仓调用方（btc_eth / 激进版 / new_coin 等）→ **触发全量重建**。需新增数据库迁移脚本（占用表）。

### 10.8 风险与回滚

- 风险：**持久化占位可能带来死锁/残留占用**：若释放路径缺失或 TTL 过长，某币种可能被永久阻塞 → 缓解：TTL + 定时清理 + 失败即释放；开启 `ownership.enabled` 开关灰度。
- 风险：advisory lock 与占用表事务组合可能放大锁竞争 → 缓解：锁内只做「查+写占用」轻量操作，外部请求移到锁外。
- 风险：新表/迁移若需 DBA 介入（表结构、索引、权限）→ **见开放问题 Q6**。
- 回滚：关闭 `ownership.enabled` 回到既有行为；或 revert commit + 回滚迁移（保留表不删，避免数据丢失）。

---

## 11. R08 — 孤儿清理在缺少策略状态时误撤真实持仓的止损

### 11.1 问题现状

`ai_tuner/cleanup/orphan_cleanup.py:execute` 遍历 OPEN 条件单时，**`strategy_states` 无对应记录即无条件 `stale_orders.append(order); continue`**（`:416-419`），**绕过**了「交易所是否仍持仓」的保护判断。对比同函数 `:429-435`（超时分支）有正确保护（`exchange_positions is not None and symbol in exchange_positions` 则跳过）。首次持久化失败、状态尚未写入或状态丢失时，即使本轮已确认交易所持仓存在，也会撤掉该仓位的保护单。报告复现：交易所明确持有 `BTCUSDT`、策略状态为空、数据库有 STOP_LOSS → `execute` 仍调用 `_cancel_order` 一次。

### 11.2 修复目标

所有清理分支在撤单前**必须先检查交易所实时持仓**；缺状态或无法确认「无仓」时保留保护单并告警，不得仅凭「状态缺失」判定孤儿。

### 11.3 功能需求

- **R08-F1 统一前置检查**：`execute` 中所有进入取消流程的分支（含「无状态」分支、超时分支、场景B分支）撤单前**必须**先判断交易所持仓。
- **R08-F2 无状态分支保护**：`strategy_states` 无该策略记录时：
  - 若交易所确认该 `symbol` **有持仓** → **跳过**（保留保护单），并记 skipped；
  - 若交易所**明确无持仓** → 才可继续按孤儿处理（且仍建议走 `_strategy_has_position` 二次确认，见 R08-F3）；
  - 若**无法确认**（`exchange_positions is None`）→ 保守**跳过**并告警。
- **R08-F3 复用 `_strategy_has_position`**：`_strategy_has_position`（`:130-196`）已有「ENTRY 挂单 / 近 N 天未平仓记录 → 视为有活交易」判断。无状态分支应复用该判断，避免仅依据 `strategy_states`。
- **R08-F4 保守优先**：任何「无法判定」情形（API 失败、状态缺失、解析异常）一律**不移除保护单**，改为保留 + 告警。
- **R08-F5 告警可观测**：跳过/保留动作必须进入通知与日志（含 symbol、strategy、原因）。

### 11.4 验收标准

- **R08-AC1（报告复现场景）**：交易所持有 `BTCUSDT`、`strategy_states` 无记录、DB 有 STOP_LOSS → `execute` **不调用** `_cancel_order`；记 skipped/告警。
- **R08-AC2**：交易所无持仓、`strategy_states` 无记录、且 `_strategy_has_position=False` → 才调用 `_cancel_order`（孤儿清理仍生效）。
- **R08-AC3**：`exchange_positions is None`（API 失败）+ 状态缺失 → 跳过并告警，不撤单。
- **R08-AC4**：原有超时分支（`:429-435`）行为不变（不回归）。
- **R08-AC5**：场景B（交易所无持仓 + 策略无活交易）行为不变（不回归）。

### 11.5 边界与异常场景

- `strategy_states.state_data` 解析失败/非 dict → 已按空处理（`:116-118`），需与 R08-F1/F3 衔接，不得因「空」直接判孤儿。
- 交易所 API 失败（`exchange_positions=None`）：全流程保守跳过。
- DB 查询 `_strategy_has_position` 异常 → 已保守返回 `True`（`:190-196`），保持。
- 状态存在但过期（超时）且交易所**有**持仓 → 现有 `:431` 已跳过，保持。
- 多策略同 symbol：仅处理该订单所属策略的判定，不误伤对家（复用现有逐单检查）。

### 11.6 配置化要求

`ai_tuner/config.yaml` 已有 `orphan_cleanup`（`:632-635`）。需**新增**：

| 配置项 | 建议默认值 | 位置 | 说明 |
|--------|-----------|------|------|
| `orphan_cleanup.require_exchange_confirmation`（**新增**） | `true` | `ai_tuner/config.yaml` | 无状态/无法确认时是否强制保留 |
| `orphan_cleanup.alert_on_missing_state`（**新增**） | `true` | 同上 | 状态缺失时是否告警 |
| `orphan_cleanup.stale_hours_threshold` | 现有 `2` | 同上 | 沿用 |
| `orphan_cleanup.interval_minutes` | 现有 `30` | 同上 | 沿用 |

> `position_lookback_days` 目前为构造函数默认 `7.0`（`orphan_cleanup.py:41`），建议一并提取为配置项（如 `orphan_cleanup.position_lookback_days`），避免硬编码默认值散落。

### 11.7 影响面

仅 `ai_tuner/*`，只重建 ai-tuner 容器。**不触发全量重建**。向后兼容：孤儿清理仍生效，仅收紧「无状态」分支。

### 11.8 风险与回滚

- 风险：过保守导致真正孤儿条件单长期残留（占用交易所条件单配额）→ 缓解：仅「无法确认」时保留，且告警推动人工处理；「明确无仓 + 无活交易」仍清理。
- 风险：`exchange_positions` 为空集合（确实无任何持仓）与 `None`（查询失败）必须严格区分（现有代码已区分，保持）。
- 回滚：revert commit（仅 ai-tuner 重建）。

---

## 12. 汇总：新增配置项一览

| 编号 | 配置项 | 默认值 | 文件 |
|------|--------|--------|------|
| R01 | `TABLE_NAME_PATTERN` | `^kline_[a-z0-9]{3,20}_([0-9]+[mhdw]|1M)$` | `services/kline_service/shared/core/config.py` |
| R01 | `KLINE_API_AUTH_ENABLED` / `KLINE_API_TOKEN`（视 Q1） | `false` / 空 | 同上 |
| R01 | 固定标的列表 | 6 个币种 | 同上 |
| R02 | `api_retry.*`（read/write/verify/前缀） | 见 §5.6 | 新增 `shared/api_retry_config.yaml` |
| R03 | `trading.close_position.reduce_only` 等 | 见 §6.6 | `strategies/new_coin/config.yaml` |
| R04 | `trading.close_order.*` | 见 §7.6 | `strategies/hrs/config.yaml` |
| R05 | `risk.protection_retry.*` | 见 §8.6 | `btc_eth` + `btc_eth_aggressive` config |
| R06 | `order_fill_check_interval_seconds` / `pm_order_visibility_delay_seconds` / `order_final_state_read_retries` | 见 §9.6 | shared 或各策略 config |
| R07 | `ownership.claim_ttl_minutes` 等 | 见 §10.6 | 各策略 config `ownership` 段 / shared |
| R08 | `orphan_cleanup.require_exchange_confirmation` 等 | 见 §11.6 | `ai_tuner/config.yaml` |

---

## 13. 汇总：测试与验收（供验证环节）

- 每条 R0x 的 `R0x-ACn` 必须全部有对应自动化测试；**核心逻辑（分支/异常路径）100% 覆盖**。
- 必须新增的异常/并发测试：
  - R02：写请求超时 → 去重（单测 + 假 HTTP）。
  - R03/R04：部分成交 + 撤单竞态 + `-2022`。
  - R05/R06：入场保护失败、部分成交、重启恢复。
  - R07：两策略并发占位（并发测试）、崩溃后 TTL 恢复、释放路径。
  - R08：状态缺失 + 交易所持仓存在（不撤单）。
- 全部在 Python 3.11（生产版本）环境复跑（基线报告指出本机为 3.9，存在环境差异）。
- **禁止**在服务器运行回测；所有验证本地 mock/单测完成。

---

## 14. 开放问题（需人工拍板，本文档不定论）

**Q1（R01）接口鉴权范围**：kline-service 接口当前无认证，8765 映射对公网可达性取决于防火墙。是否本轮引入 Token 鉴权 / 仅收窄防火墙 / 仅做白名单校验即可？需要运维/安全拍板。

**Q2（R06）公共助手归属**：R06 跨 3 策略。抽到 `shared/` 会触发全量重建（停机面大），但不抽会违反「禁止重复代码」。是否接受本轮以「全量重建」换取代码单点？还是允许短期在三策略内实现、后续再抽？需架构/负责人拍板。

**Q3（R03/R04）reduceOnly 与 PM 账户取舍**：PM 账户下 `ReduceOnly` 单可能报 `[-2022]`。方案 A：始终带 `reduceOnly`，被拒时先对账/撤同向单再重试；方案 B：改用「先对账确定剩余量 + 不依赖 reduceOnly」的纯数量控制。两方案在并发与竞态下的安全性不同，需交易负责人拍板。

**Q4（R05）保护失败兜底策略**：`on_exhausted` 默认「告警」还是「减仓兜底」？减仓兜底能消除无保护风险但会主动平掉真实仓位（可能实现亏损）。需交易负责人拍板；同时需明确 `protection_pending` 对归属判定/看板上报的影响。

**Q5（R06）部分成交极小量处理**：部分成交量经精度截断后为 0（或低于最小下单量）时，是「减仓清零（平掉微仓）」还是「按最小可处理量建仓」？涉及 PM 账户最小名义价值限制，需交易负责人拍板。

**Q6（R07）占用表设计与 DBA 介入**：新增「币种占用/交易意图」表的字段、唯一约束（部分唯一索引）、TTL/清理策略，以及它与既有 `trading.trade_records` 归属判定的对应关系（何时从「占用」转为「归属」）。是否需要 DBA 介入建表/索引/权限？需架构 + DBA 拍板。

**Q7（R02）交易意图与 `newClientOrderId` 生命周期**：由「谁」在「何时」生成并复用 ID（一次交易决策 / 一次函数调用 / 一次重试）？需要架构环节明确，否则去重边界不清。

**Q8（全局）本轮是否需要开关灰度**：R02/R06/R07 风险最高。是否要求所有高风险修复带配置开关（`enabled`），支持线上快速回退？需负责人拍板。

---

（文档结束）