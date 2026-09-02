# HRS 策略保护单管理缺陷修复 PRD

> **文档类型：** 产品需求文档（PRD）
> **适用策略：** HRS（混合反转策略）
> **文档状态：** 待评审
> **创建日期：** 2026-08-20

---

## 1. 文档信息

| 项 | 内容 |
|----|------|
| 文档名称 | HRS 策略保护单管理缺陷修复需求文档 |
| 版本号 | V1.0 |
| 创建日期 | 2026-08-20 |
| 作者 | 需求文档专家 |
| 关联文档 | [混合反转策略（HRS）V2.5.1（融合修复版）完整文档](file:///Users/yl/vscode/Binance_quantitative_trading/docs/requirements/HRS/混合反转策略（HRS）V2.5.1（融合修复版）完整文档.md)、[限价单与孤儿单修复方案](file:///Users/yl/vscode/Binance_quantitative_trading/docs/requirements/限价单与孤儿单修复方案.md) |
| 涉及代码 | [strategies/hrs/strategy.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py)、[strategies/hrs/executor.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py)、[strategies/hrs/position_manager.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py) |

### 1.1 修订记录

| 版本 | 日期 | 修改人 | 变更说明 |
|------|------|--------|---------|
| V1.0 | 2026-08-20 | 需求文档专家 | 初稿创建，基于线上排查结果编写 |

---

## 2. 背景与问题概述

### 2.1 背景

HRS 策略在运行过程中，对持仓的保护依赖「止损单（SL）+ 分批止盈单（TP1/TP2）」三类条件单。排查发现，当前保护单的**创建、取消、恢复**链路存在 3 处管理缺陷，导致线上交易对出现以下异常现象：

| 缺陷编号 | 缺陷名称 | 线上现象 |
|---------|---------|---------|
| 缺陷 A | 保护单不完整 | 交易对持仓只有止盈单或只有止损单，甚至完全没有保护单（裸奔） |
| 缺陷 B | 平多后开反向空仓 | 同一交易对同时出现多空双向持仓（净敞口失控） |
| 缺陷 C | 多组条件单重复累积 | 同一交易对、同一角色的条件单（如 TP2）在交易所上累积多组 |

### 2.2 业务影响

- 缺陷 A 导致持仓风险暴露不可控：止损单缺失时亏损无法封顶；止盈单缺失时盈利无法按计划落袋。
- 缺陷 B 导致同一交易对多空双向持仓，方向相互对冲，实际风险敞口加倍，且保护单互相干扰。
- 缺陷 C 导致条件单数量无限累积：价格触发时多组保护单同时生效，可能引发重复平仓、超额平仓、占用保证金，并污染风控统计。

### 2.3 修复目标

1. **保证保护单完整性**：任何持仓在任意时刻都应有「止损 + 止盈」的完整保护，缺失时能自动补齐，补齐失败能告警。
2. **杜绝反向开仓**：同一交易对只允许存在单一方向持仓，开仓前必须与交易所核对实际持仓方向，禁止在已有持仓的情况下开反向仓。
3. **消除条件单重复累积**：同一交易对同一角色（SL/TP1/TP2）在交易所上最多存在一组条件单；取消旧单必须先于下单新单，且不依赖可能丢失的本地状态。

### 2.4 非目标（明确不做）

| 项 | 说明 |
|----|------|
| 不改变策略开平仓信号逻辑 | 信号生成、评分、候选池、LV-RM 等判断逻辑不做修改 |
| 不改变止盈止损参数 | SL/TP1/TP2 的价格计算公式、比例、ATR 倍数、偏移均保持不变 |
| 不引入新的数据表 | 优先复用现有 `condition_orders` 表，不新增 HRS 专属表（可复用已有 `hrs_orders` 幽灵表，见 8.2） |
| 不修改其他策略 | btc_eth、new_coin、grid 策略不在本次范围 |

---

## 3. 需求优先级（MoSCoW）

| 优先级 | 需求项 | 对应缺陷 |
|--------|--------|---------|
| **Must（必须）** | 开仓后保护单完整创建与失败补偿 | 缺陷 A |
| **Must（必须）** | 开仓前核对交易所实际持仓方向，禁止反向开仓 | 缺陷 B |
| **Must（必须）** | 条件单创建幂等 + 补单前可靠取消旧单 | 缺陷 C |
| **Must（必须）** | 保护单缺失的自动补单与告警 | 缺陷 A/C |
| **Should（应该）** | 状态恢复时 algoIds 持久化恢复 | 缺陷 C |
| **Should（应该）** | 启动对账时对交易所非本策略持仓明确告警（不接管） | 缺陷 B |
| **Could（可以）** | 提取条件单管理公共逻辑到 `shared/` 复用 | 缺陷 A/C |
| **Won't（不做）** | 调整交易参数、信号逻辑 | — |

---

## 4. 缺陷详情与根因分析

> 以下根因均为静态代码取证结论，修复实施前应在第 7 章测试中补充运行时证据。

### 4.1 缺陷 A：保护单不完整（只有止盈 / 只有止损）

#### 4.1.1 现象

交易对已建立持仓，但交易所上只有 SL 或只有 TP1/TP2，甚至三者皆无。持仓处于部分保护或无保护状态。

#### 4.1.2 根因链

1. **开仓时三类条件单独立下单、无原子性**：在 [executor.py 的 execute_short](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py#L204-L341) / [execute_long](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py#L343-L480) 中，开仓成交确认后依次创建 SL → TP1 → TP2 三个条件单。若中途任意一个 `place_conditional_order` 抛出异常（如价格校验失败、网络错误、algoId 缺失），外层 `except` 直接返回 `None`。
2. **开仓失败但持仓已建立**：[strategy.py 的 execute_signal](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L460-L477) 中 `if order:` 判定失败后**不调用 add_position**。此时交易所已实际开仓且可能已有部分保护单（先下单成功的 SL 或 TP 残留），但本地 `_positions` 无记录 → 该持仓既无监控、无补单、无平仓回调 → 保护单永久缺失。
3. **补单时把下单失败静默忽略**：[executor.py 的 replenish_position_orders](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py#L657-L819) 中，SL/TP1/TP2 下单的 `except` 分支仅记录 `logger.debug("…可能已存在")`，**不区分「已存在」与「下单失败」**，失败后不重试 → 缺单永远缺单。
4. **`has_algo_id` 误判**：[position_manager.py 的 has_algo_id](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py#L325-L339) 仅凭本地 `algo_ids` 字典判断「该角色已有条件单」即跳过补单。若本地记录仍指向一个已失效/被交易所取消的旧 algoId，则不会重新下单 → 缺单。

#### 4.1.3 证据（代码位置）

| 证据 | 位置 |
|------|------|
| SL/TP1/TP2 逐个下单、异常即整体失败 | [executor.py L271-L322](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py#L271-L322)（short）、[executor.py L410-L461](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py#L410-L461)（long） |
| 开仓返回 None 时不记录持仓 | [strategy.py L460-L477](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L460-L477) |
| 补单失败静默忽略、不重试 | [executor.py L703-L719](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py#L703-L719)、[executor.py L742-L756](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py#L742-L756)、[executor.py L793-L807](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py#L793-L807) |
| has_algo_id 依赖本地记录，可能过期 | [position_manager.py L325-L339](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py#L325-L339) |

### 4.2 缺陷 B：平多后开反向空仓（多空双向持仓）

#### 4.2.1 现象

同一交易对同时存在多头与空头持仓，形成方向对冲、净敞口失控。

#### 4.2.2 根因链

1. **开仓前只检查本地内存持仓，不核对交易所实际持仓**：[strategy.py 的 execute_signal](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L377-L387) 中仅通过 `position_manager.has_position(symbol)`（本地 `_positions` 字典）判断是否已有持仓。当本地状态与交易所实际状态不一致时，策略会对已持仓交易对开反向仓。
2. **本地状态与交易所不一致的来源**：
   - 策略重启时 `_restore_state`（[strategy.py L2042-L2180](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L2042-L2180)）从 `hrs_positions` 表恢复持仓，若该表数据丢失/损坏/读取失败，本地无持仓记录，而交易所实际仍有持仓。
   - `_reconcile_positions`（[strategy.py L2275-L2353](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L2275-L2353)）对「交易所有而本地无」的持仓**仅记录告警、不接管**（HRS 与其他策略共用账户，接管会误伤其他策略持仓）。
   - 缺陷 A 导致开仓后本地未记录持仓，交易所却有持仓。
3. **内存 `_positions` 以 symbol 为键、单方向表达**：[position_manager.py L104-L115](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py#L104-L115) 中 `_positions[symbol]` 只能存一个方向；若交易所已形成双向持仓，状态恢复时后写的一方覆盖先写的一方，本地永远无法准确表达多空并存事实。

#### 4.2.3 证据（代码位置）

| 证据 | 位置 |
|------|------|
| 开仓前仅检查本地 has_position | [strategy.py L377-L387](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L377-L387) |
| 对账不接管交易所持仓 | [strategy.py L2309-L2321](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L2309-L2321) |
| 内存单方向表达持仓 | [position_manager.py L104-L115](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py#L104-L115) |
| 信号收集阶段已有持仓检查（仅为前置，最终以 execute_signal 为准） | [strategy.py L739-L741](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L739-L741)、[strategy.py L761-L763](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L761-L763) |

### 4.3 缺陷 C：多组条件单重复累积

#### 4.3.1 现象

同一交易对、同一角色（SL/TP1/TP2）在交易所上存在多组条件单。随策略重启次数增加，条件单数量翻倍累积。

#### 4.3.2 根因链

1. **algoIds 不持久化，重启即丢失**：`hrs_positions` 表结构（[strategy.py L1925-L1933](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L1925-L1933)）不包含 algoIds 字段；`_save_state` 写持仓（[strategy.py L2516-L2528](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L2516-L2528)）也不保存 algoIds；`_restore_state` 恢复持仓（[strategy.py L2059-L2060](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L2059-L2060)）查询也不含 algoIds → **每次重启后本地 `algo_ids` 字典全空**。
2. **补单前取消旧单依赖本地 algoIds，重启后失效**：[strategy.py 的 _replenish_single_position](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L2941-L2945) 补单前调用 `position_manager.cancel_all_orders(symbol)`，而该方法（[position_manager.py L341-L376](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py#L341-L376)）仅按本地 `algo_ids` 逐个取消。重启后本地为空 → **交易所旧条件单一个都取消不掉**。
3. **补单判定 has_algo_id 为空 → 重新下单**：[executor.py 的 replenish_position_orders](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py#L700-L715) 中 `has_algo_id` 为 False → 再次创建 SL/TP1/TP2 → **交易所旧单残留 + 新单 = 多组重复**。
4. **`algo_ids` 以 role 为键仅存单个 algoId**：[position_manager.py L274-L291](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py#L274-L291) 的 `add_algo_id` 中 `pos["algo_ids"][role] = algo_id` 覆盖旧值；同一角色重复下单时本地只留最新，交易所旧单成为不可见孤儿单，无法再通过本地取消。
5. **加仓重下保护单时取消失败残留**：[executor.py 的 add_to_position](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py#L882-L893) 重新设置保护单前逐个 `cancel_algo_order` 取消旧单，取消失败（网络异常/订单已失效）时仅 debug 日志，继续下新单 → 重复累积。

#### 4.3.3 证据（代码位置）

| 证据 | 位置 |
|------|------|
| hrs_positions 表无 algoIds 字段 | [strategy.py L1925-L1933](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L1925-L1933) |
| _save_state 不保存 algoIds | [strategy.py L2516-L2528](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L2516-L2528) |
| _restore_state 查询不含 algoIds | [strategy.py L2059-L2060](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L2059-L2060) |
| 补单前取消旧单依赖本地 algoIds | [strategy.py L2941-L2945](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L2941-L2945) |
| cancel_all_orders 仅按本地记录取消 | [position_manager.py L341-L376](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py#L341-L376) |
| has_algo_id 为空导致重新下单 | [executor.py L700-L715](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py#L700-L715) |
| add_algo_id 单键覆盖 | [position_manager.py L274-L291](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py#L274-L291) |
| 加仓取消旧单失败不阻断 | [executor.py L882-L893](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py#L882-L893) |

---

## 5. 功能需求

> 编号规则：FR-<序号>。每项需求均包含「需求描述」「业务规则」「验收标准」，验收标准必须可被测试验证。

### 5.1 保护单完整性保障（缺陷 A）

#### FR-01：开仓成功后保护单原子性创建与失败补偿

- **需求描述**：开仓成交确认后，SL/TP1/TP2 三类保护单必须完整创建；任意一类创建失败时，不能静默放弃，必须走补偿流程。
- **业务规则**：
  1. 在 `execute_short` / `execute_long` 中，将 SL/TP1/TP2 的创建结果逐一收集（成功/失败），任一失败不抛异常中断，而是记录失败角色清单。
  2. 返回给调用方 `execute_signal` 的结果中必须携带「开仓是否成功」与「保护单创建是否完整」两个独立信号：
     - 开仓成功且保护单完整 → 正常记录持仓并返回 True；
     - 开仓成功但保护单不完整 → 记录持仓（避免出现「有持仓无记录」），并**立即触发补单流程**（复用 FR-05 幂等创建逻辑），返回 True 并发送告警通知；
     - 开仓失败 → 返回 False，不记录持仓。
  3. 保护单创建失败的告警级别为 `warning`，通知事件复用 `notification.events.anomaly_alert`（配置项：`config.yaml → notification.events.anomaly_alert`，默认 true）。
- **验收标准**：
  - [ ] AC-01-1：mock 模拟 TP1 下单抛异常时，开仓仍被本地记录（add_position 被调用），且返回 True。
  - [ ] AC-01-2：mock 模拟 TP1 下单抛异常时，系统自动触发一次补单流程（replenish 被调用）。
  - [ ] AC-01-3：mock 模拟 TP1 下单抛异常时，发送了 `anomaly_alert` 类型告警通知。
  - [ ] AC-01-4：三类保护单全部创建成功时，不触发补单、不发送告警。

#### FR-02：补单失败不再静默忽略，区分「已存在」与「下单失败」

- **需求描述**：`replenish_position_orders` 中，条件单创建失败必须与「已存在跳过」区分对待。
- **业务规则**：
  1. 「已存在跳过」仅当本地 `has_algo_id(role)` 为 True 且对应 algoId 有效时成立（有效性判定见 FR-05）。
  2. 其余 `place_conditional_order` 失败均视为「下单失败」：记录 `error` 级日志、计入失败统计、返回失败结果，由调用方决定重试（FR-03）并触发告警。
  3. 不得使用 `except Exception: logger.debug(...)` 掩盖真实失败原因。
- **验收标准**：
  - [ ] AC-02-1：mock 下单抛异常时，返回结果为失败（非 True），并记录 `error` 级日志。
  - [ ] AC-02-2：mock 下单抛异常时，失败角色会被计入返回统计（如 `failed_roles` 列表）。
  - [ ] AC-02-3：本地 has_algo_id 为 True 且 algoId 有效时，跳过补单且不产生任何失败日志。

### 5.2 杜绝反向开仓（缺陷 B）

#### FR-03：开仓前核对交易所实际持仓，禁止反向开仓

- **需求描述**：`execute_signal` 在开仓前，除本地 `has_position` 检查外，必须向交易所查询该交易对实际持仓，仅在交易所无该交易对持仓（或持仓方向与信号方向一致）时允许开仓。
- **业务规则**：
  1. 新增开仓前置检查：调用 `binance_client.get_position(symbol)` 获取该交易对实际持仓。
     - 交易所有持仓且方向与信号方向**相反** → 拒绝开仓，记录 `warning` 日志，返回 False（不撤销任何已有订单）。
     - 交易所有持仓且方向与信号方向**相同** → 走既有加仓判定逻辑（`_handle_add_position`），不新增独立反向仓位。
     - 交易所无持仓 → 正常开仓。
  2. 检查结果依赖交易所为准：即使本地 `has_position` 为 False，只要交易所存在反向持仓，也不得开反向仓。
  3. 该检查与本地 `has_position` 检查为「与」关系：两者任一命中（本地有持仓 或 交易所存在反向持仓）即阻止开仓。
  4. 查询交易所持仓失败（网络异常）时，**采取保守策略**：记录 `warning` 并拒绝开仓（宁可错过信号，不可冒反向开仓风险）。
  5. 配置项：新增 `trading.entry.reject_on_exchange_position: true`（默认开启），读取方式与既有配置一致，禁止硬编码。
- **验收标准**：
  - [ ] AC-03-1：mock 交易所返回该交易对存在反向多头持仓，提交做空信号 → 拒绝开仓，返回 False。
  - [ ] AC-03-2：mock 交易所返回该交易对存在同向多头持仓，提交做多信号 → 不新增独立仓位，走加仓判定。
  - [ ] AC-03-3：mock 交易所返回无持仓 → 正常开仓。
  - [ ] AC-03-4：mock `get_position` 抛异常 → 拒绝开仓，返回 False，并记录 `warning`。
  - [ ] AC-03-5：`trading.entry.reject_on_exchange_position` 配置为 false 时，上述交易所检查被跳过（保持旧行为）。

#### FR-04：启动对账时对交易所持仓仅告警、不接管（2026-08-20 二次修订）

- **需求描述**：`_reconcile_positions` 发现「交易所有而本地无」的持仓时，**只告警、不接管**。此规则为**硬性规定，无配置开关**，代码中不保留任何接管路径。
- **修订原因**：
  - 第一次修订（2026-08-20）：HRS 与 btc_eth/new_coin/grid 共用同一币安账户，`get_position()` 返回**所有策略**的持仓，自动接管会把其他策略的仓位误认为 HRS 持仓并追加保护单（XRPUSDT 曾下 12 单、CXMTUSDT 被误跟踪），造成跨策略干扰。当时做法是加配置开关 `adopt_exchange_positions` 并默认关闭。
  - 第二次修订（2026-08-20）：开关默认值在代码中为 `True`，一旦配置缺失或部署不一致会**重新启用接管**，风险未根除。用户明确要求「HRS 不需要接管其他策略的订单」，故**彻底删除接管代码路径与配置项**，仅保留告警。
- **业务规则**：
  1. 交易所有而本地无 → 记录 `warning` 日志「发现非本策略持仓，不接管，请手动处理」，**不调用** add_position、**不补单**、不发送接管类告警。
  2. 该行为为硬性规定，不提供任何配置开关或备用接管路径。
- **验收标准**：
  - [ ] AC-04-1：mock 交易所存在交易对 X 持仓、本地无记录 → 仅记录 warning，不调用 add_position(X)、不补单、不发送接管告警。
  - [ ] AC-04-2：代码中不存在 `adopt_exchange_positions` / `_adopt_exchange_positions` 引用（接管路径已彻底移除）。

### 5.3 消除条件单重复累积（缺陷 C）

#### FR-05：条件单创建幂等（同角色同交易对仅一组）

- **需求描述**：在创建任何保护单前，必须先确保该交易对该角色在交易所上不存在有效条件单，或已将其取消。
- **业务规则**：
  1. 创建保护单的入口（`execute_short`/`execute_long`/`replenish_position_orders`/`add_to_position`）统一收敛到一个内部方法 `_place_protection_order(symbol, role, ...)`，该方法先执行「取消旧单」（FR-07）再下单，避免各入口各自为政。
  2. 取消旧单后、下单新单前，清除本地对应 role 的 algoId 记录，防止残留旧 ID。
  3. 下单成功后以新 algoId 覆盖本地 role 记录。
- **验收标准**：
  - [ ] AC-05-1：同一交易对同一角色连续调用两次创建流程 → 第一次下单后第二次先取消旧单再下单，最终交易所上仅 1 组该角色条件单。
  - [ ] AC-05-2：下单成功后，本地 `algo_ids[role]` 等于新 algoId，无旧值残留。

#### FR-06：algoIds 持久化，重启不丢失

- **需求描述**：条件单 algoId 必须随持仓一起持久化，重启后可完整恢复，避免依赖失效。
- **业务规则**：
  1. 在 `hrs.hrs_positions` 表新增列 `algo_ids JSONB`（`_ensure_db_schema` 中使用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 幂等迁移，存量表自动升级）。
  2. `_save_state` 写入持仓时，将 `pos["algo_ids"]` 序列化为 JSONB 一并保存；`_restore_state` 恢复持仓时反序列化回 `algo_ids` 字典。
  3. JSONB 兜底恢复路径（`position_manager.from_dict` / `_restore_state_from_jsonb_fallback`）本就包含 algo_ids（[position_manager.py L444-L462](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py#L444-L462) 已序列化），需确保与独立表恢复路径行为一致。
  4. 恢复后的 algoIds 必须经过有效性校验（FR-05 的取消与重建流程会兜底处理失效 ID）。
- **验收标准**：
  - [ ] AC-06-1：`_ensure_db_schema` 后 `hrs_positions` 表存在 `algo_ids` 列（含存量库幂等升级）。
  - [ ] AC-06-2：`_save_state` 后查询 `hrs_positions`，algo_ids 字段与内存一致。
  - [ ] AC-06-3：`_restore_state` 恢复后，持仓的 `algo_ids` 字典与保存前一致（含 role → algoId 映射）。

#### FR-07：取消旧保护单改用交易所批量取消接口

- **需求描述**：补单/加仓/平仓场景中取消旧保护单时，优先使用交易所「按交易对批量取消所有 OPEN 条件单」接口，不依赖可能丢失的本地 algoIds。
- **业务规则**：
  1. 新增方法 `cancel_all_orders(symbol)` 的强化版（或改造现有方法）：优先调用 `binance_api.cancel_all_algo_orders(symbol)`（[binance_api.py L899-L926](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L899-L926)），该接口一次取消该交易对所有 OPEN 条件单。
  2. 批量取消成功后，清空本地 `pos["algo_ids"]`。
  3. 批量取消接口不可用（非统一账户/异常）时，回退到现有按本地 algoId 逐个取消逻辑，并记录 `warning`。
  4. `cancel_all_algo_orders` 仅支持统一账户，需在 `binance_api` 侧确认 `use_unified_account`；若 HRS 运行在非统一账户，则保持逐个取消并告警提示无法批量取消。
- **验收标准**：
  - [ ] AC-07-1：调用 `cancel_all_orders(symbol)` 时，优先调用 `cancel_all_algo_orders`（可通过 mock 断言调用顺序与参数）。
  - [ ] AC-07-2：批量取消成功后本地 `algo_ids` 被清空。
  - [ ] AC-07-3：批量取消接口抛异常时回退到逐个取消逻辑并记录 `warning`。

#### FR-08：加仓重下保护单取消失败即阻断

- **需求描述**：`add_to_position` 重下保护单时，若取消旧单失败，不得继续下新单，避免重复累积。
- **业务规则**：
  1. `add_to_position` 中取消旧单（当前 [executor.py L882-L893](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py#L882-L893)）若全部失败或部分失败，需评估：
     - 优先使用 FR-07 的批量取消接口完成取消；
     - 若批量取消失败，记录 `error` 并**中止重下流程**，返回失败，等待下一轮补单重试。
  2. 不得在取消结果未知的情况下继续下新保护单。
- **验收标准**：
  - [ ] AC-08-1：mock 批量取消失败 → 加仓流程不调用 `place_conditional_order`，返回失败。
  - [ ] AC-08-2：mock 批量取消成功 → 加仓流程正常重下 SL/TP1/TP2。

#### FR-09：补单周期与启动补单接入批量取消

- **需求描述**：`_replenish_single_position` 补单前的取消步骤，统一走 FR-07 的批量取消逻辑。
- **业务规则**：
  1. 替换 [strategy.py L2941-L2945](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L2941-L2945) 中的 `position_manager.cancel_all_orders(symbol)` 调用，改为 FR-07 强化版。
  2. 补单入口（启动 `_replenish_all_positions`、TP1 成交后 `_replenish_single_position`、保护单缺失补偿）行为保持一致。
- **验收标准**：
  - [ ] AC-09-1：启动补单时，先批量取消该交易对所有条件单，再补单。
  - [ ] AC-09-2：TP1 成交后触发补单时，先批量取消旧条件单（含已成交的 TP1 残留），再补齐 TP2。

---

## 6. 涉及文件清单

| 文件 | 变更类型 | 变更内容 |
|------|---------|---------|
| [strategies/hrs/executor.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py) | 修改 | FR-01（保护单创建结果收集）、FR-02（失败区分）、FR-05（`_place_protection_order` 收敛）、FR-08（加仓取消失败阻断） |
| [strategies/hrs/position_manager.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py) | 修改 | FR-05（角色记录清理/覆盖）、FR-06（algoIds 持久化字段）、FR-07（批量取消接口接入） |
| [strategies/hrs/strategy.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py) | 修改 | FR-03（开仓前交易所持仓核对）、FR-04（对账仅告警、不接管，无配置开关）、FR-06（表结构 ALTER + 读写 algoIds）、FR-09（补单批量取消） |
| [strategies/hrs/config.yaml](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/config.yaml) | 修改 | 新增 `trading.entry.reject_on_exchange_position` 配置项；FR-04 无接管配置项（硬性规定） |
| [shared/binance_api.py](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py) | 只读确认 | `cancel_all_algo_orders`（[L899-L926](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L899-L926)）已存在，无需新增；需确认 HRS 运行账户模式 |
| [strategies/hrs/tests/test_position_manager.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/tests/test_position_manager.py) | 修改/新增 | 补充 FR-05/06/07 相关单测 |
| [strategies/hrs/tests/test_fix_verification.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/tests/test_fix_verification.py) | 修改/新增 | 补充 FR-01/02/03/08 相关单测 |
| [strategies/hrs/tests/test_market_data.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/tests/test_market_data.py) | 只读确认 | 确认不涉及 |
| [docs/requirements/btc_eth/项目需求迭代文档.md](file:///Users/yl/vscode/Binance_quantitative_trading/docs/requirements/btc_eth/项目需求迭代文档.md) | 更新 | 需求分析阶段完成后登记本次修复需求 |

---

## 7. 测试要求

### 7.1 测试前置（幻觉测试 10 项清单）

编码完成后、功能测试前，逐项验证：

```
□ 1. 所有 import 的模块/函数/类是否存在（含 shared.binance_api.cancel_all_algo_orders）
□ 2. 所有 API 调用方法名、参数个数、参数名与 binance_api 定义一致
□ 3. 所有新增配置项（reject_on_exchange_position）在 config.yaml 中存在；FR-04 已确认无接管配置项
□ 4. 所有文件路径真实存在
□ 5. 变量名拼写正确且已定义（含新增 algo_ids JSONB 读写路径）
□ 6. 条件分支逻辑与预期一致（开仓拦截、仅告警不接管、取消失败阻断等不取反）
□ 7. 异常路径均有处理（交易所查询失败保守拒绝、批量取消失败回退）
□ 8. 异步/同步上下文匹配（await 均在 async 函数中）
□ 9. 无硬编码（新增开关均从 config.yaml 读取，禁止写死）
□ 10. 修改文件完整、无截断、无遗漏
```

### 7.2 单元测试（mock 交易所，覆盖核心逻辑与边界条件）

| 编号 | 用例 | 对应需求 |
|------|------|---------|
| UT-01 | 保护单三类全部成功 → 正常记录持仓，无告警 | FR-01 |
| UT-02 | TP1 下单抛异常 → 仍记录持仓 + 触发补单 + 发告警 | FR-01 |
| UT-03 | 补单下单抛异常 → 返回失败结果 + error 日志 | FR-02 |
| UT-04 | 交易所存在反向持仓 → 拒绝开仓 | FR-03 |
| UT-05 | 交易所存在同向持仓 → 走加仓判定，不新增独立仓位 | FR-03 |
| UT-06 | 交易所查询异常 → 保守拒绝开仓 | FR-03 |
| UT-07 | 交易所存在持仓、本地无记录 → 仅告警、不接管、不补单、不发接管告警 | FR-04 |
| UT-08 | 同一角色连续两次创建 → 仅 1 组条件单，本地覆盖为最新 | FR-05 |
| UT-09 | 保存/恢复后 algo_ids 往返一致 | FR-06 |
| UT-10 | cancel_all_orders 优先调用 cancel_all_algo_orders | FR-07 |
| UT-11 | 批量取消失败 → 回退逐个取消 + warning | FR-07 |
| UT-12 | 加仓批量取消失败 → 不重下保护单，返回失败 | FR-08 |
| UT-13 | 启动补单先批量取消再补单 | FR-09 |

### 7.3 集成 / 回归测试

| 场景 | 步骤 | 预期结果 |
|------|------|---------|
| 策略重启不丢保护单 | 开仓 → 记录条件单 → 重启策略 → 观察补单行为 | 重启后先批量取消旧条件单再补齐，交易所条件单数量不增加 |
| 反向开仓拦截 | 已有做多持仓时模拟做空信号 | 拒绝开仓，返回 False，无新增空仓 |
| 对账仅告警 | 清空 hrs_positions 表 → 重启策略（HRS 无实际持仓，交易所其他策略持仓） | 仅产生 warning 告警，不接管、不补保护单、不加保护单 |
| 多轮重启不累积 | 连续重启 3 次 | 交易所条件单数量始终 = 持仓数 × ≤3 组，无翻倍 |

### 7.4 覆盖率要求

- 新增/修改代码的分支覆盖、边界条件、异常路径覆盖 **100%**。
- 每个 `try/except` 分支必须有对应测试用例。
- 新增配置项的不同取值组合至少覆盖一次。

### 7.5 验证命令

```bash
# 本地单元测试
cd /Users/yl/vscode/Binance_quantitative_trading
python -m pytest strategies/hrs/tests/ -v

# 覆盖率
python -m pytest strategies/hrs/tests/ --cov=strategies.hrs --cov-report=term-missing

# 导入验证
python -c "from strategies.hrs.executor import TradingExecutor; print('executor OK')"
python -c "from strategies.hrs.position_manager import PositionManager; print('position_manager OK')"
python -c "from strategies.hrs.strategy import HRSStrategy; print('strategy OK')"
```

---

## 8. 依赖、风险与后续计划

### 8.1 依赖

| 依赖项 | 说明 |
|--------|------|
| 统一账户模式（PM） | FR-07 依赖 `cancel_all_algo_orders` 批量取消接口，该接口仅支持统一账户；需在实施时确认 HRS 运行账户模式，若为非统一账户需调整实现并保留告警 |
| `condition_orders` 表 | FR-06 可复用现有 `condition_orders` 表做条件单记录核对，不强制依赖 |
| 既有 `hrs_orders` 幽灵表 | [strategy.py L1936-L1944](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L1936-L1944) 定义了但当前未被使用，可作为条件单持久化的现成载体，实施时评估是否启用 |

### 8.2 风险

| 风险 | 等级 | 说明 | 缓解措施 |
|------|------|------|---------|
| `cancel_all_algo_orders` 批量取消可能误伤同交易对其他来源条件单 | 中 | 若同交易对有非 HRS 条件单会被一并取消 | 实施前确认该交易对仅 HRS 管理；批量取消后重建 HRS 保护单 |
| 自动接管误接管其他策略持仓 | 高（已修复） | 共用账户下 `get_position()` 返回所有策略持仓，接管会误加保护单（XRPUSDT 曾下 12 单） | **彻底移除接管代码路径与配置项**，仅告警不接管（2026-08-20 二次修订，硬性规定） |
| 开仓前置交易所查询增加一次 API 调用 | 低 | 每个信号多 1 次 `get_position` 调用 | 频率为每小时周期级，量级可接受；可复用周期内已有查询结果 |
| 存量重复条件单清理 | 中 | 修复上线前交易所已有累积条件单 | 上线部署时执行一次性清理脚本（人工确认后调用 `cancel_all_algo_orders` 并按当前持仓重建保护单） |

### 8.3 后续计划

| 项 | 说明 |
|----|------|
| 一次性存量清理脚本 | 部署时执行，见 8.2 风险表 |
| 条件单管理公共模块抽取（可选） | 评估将 `_place_protection_order` / 批量取消逻辑提取到 `shared/` 供多策略复用（Could 级） |
| 运行时证据补充 | 实施阶段按第 7 章测试收集日志证据，确认根因与实际运行一致 |

---

## 9. 验收标准汇总

| 需求 | 验收标准 | 缺陷 |
|------|---------|------|
| FR-01 开仓后保护单原子性创建与失败补偿 | AC-01-1 ~ AC-01-4 | A |
| FR-02 补单失败不再静默忽略 | AC-02-1 ~ AC-02-3 | A |
| FR-03 开仓前核对交易所实际持仓，禁止反向开仓 | AC-03-1 ~ AC-03-5 | B |
| FR-04 启动对账仅告警、不接管 | AC-04-1 ~ AC-04-2 | B |
| FR-05 条件单创建幂等 | AC-05-1 ~ AC-05-2 | C |
| FR-06 algoIds 持久化 | AC-06-1 ~ AC-06-3 | C |
| FR-07 取消旧保护单改用批量取消 | AC-07-1 ~ AC-07-3 | C |
| FR-08 加仓重下取消失败即阻断 | AC-08-1 ~ AC-08-2 | C |
| FR-09 补单周期接入批量取消 | AC-09-1 ~ AC-09-2 | C |

---

## 10. 自审检查

| 检查项 | 结论 |
|--------|------|
| 每个需求是否清晰描述「做什么 / 为什么 / 怎么做 / 验收」 | ✅ 全部需求含描述、业务规则、验收标准 |
| 是否存在模糊或歧义表述 | ✅ 已避免「大概/可能/尽量」，使用可量化验收标准 |
| 异常与边界场景是否覆盖 | ✅ 覆盖下单失败、交易所查询失败、批量取消失败、状态丢失、存量数据 |
| 是否区分必须做与可选做 | ✅ 见第 3 章 MoSCoW |
| 术语与命名是否一致 | ✅ 统一使用「保护单 / 条件单 / SL / TP1 / TP2 / algoId」 |
| 依赖与风险是否明确 | ✅ 见第 8 章 |
