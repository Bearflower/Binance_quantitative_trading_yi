# 修复幽灵入场记录导致的误判互斥（R3）

- 日期：2026-10-14 ｜ 风险：**R3**（持仓归属互斥、撤单路径、生产数据修正）
- A 段模型记录：GLM-5.3-Flash（覆盖记录：默认应为 DeepSeek-V4-Pro，用户保持当前对话模型；设计独立性以 A-review 独立上下文保证）
- 基线：`main` @ `1d1b247`，工作区有无关未提交文档变更，本次不触碰

## 1. 背景与根因（已实证）

SNPSUSDT 评分 7.9 达标却报「该币种已被其他策略持有，跳过开仓」，实际无任何真实持仓。

根因链（生产库已验证）：

1. `shared/binance_api.py` 下单成功即自动 `trade_logger.log_order` 写入 trade_records（status=NEW，qty=0）。LIMIT 单 60 秒超时未成交被撤后，**该记录永久残留为 status=NEW / realized_pnl IS NULL**（成交后系统也不更新 FILLED，见 task.md:42 实证）。
2. 互斥 B 路径 `_open_entry_sql(False)`（position_ownership.py:100-118）以 `order_type IN ('LIMIT','MARKET') AND realized_pnl IS NULL` 判「未平开仓单」，幽灵 NEW 记录永久命中。
3. `strategies/new_coin/config.yaml` `record_name: "新币做空策略"`，但 TradeLogger 写库实际为 `"new_coin"`（main.py:107）→ 自己的幽灵记录被判定为「其他策略」→ `authority_conflict` 拦截。
4. 生产库现状：trade_records 中 status=NEW 的入场记录 new_coin 101 条 / hrs 147 条 / btc_eth 229 条 / btc_eth_aggr 103 条，绝大多数为同类幽灵；归属名一致的自家幽灵不误报但会跨策略误拦。

## 2. 目标 / 非目标

**目标**
- G1：SNPSUSDT 及同类型被幽灵记录拦截的币种恢复正常开仓。
- G2：撤单后入场记录同步标记 CANCELED，幽灵记录不再产生（全策略生效）。
- G3：互斥口径排除 CANCELED/EXPIRED 终态记录。
- G4：一次性清理历史幽灵记录（与交易所逐单核对，不误伤真实持仓）。
- G5：new_coin 归属名与写库名统一为 `new_coin`。

**非目标**
- 不改 trade_records「成交后不更新 FILLED」的历史行为（另立任务）。
- 不改 btc_eth 系的 `resolve_position_owner` 调用口径（strategy.py:5382/5723 默认 status_filter=True 是既有行为）。
- 不动 hrs/grid 配置（其归属名已一致）。
- 不改占用表 TTL/清理机制。

## 3. 改动设计

### D1 配置修正（解锁 SNPSUSDT 的直接开关）
- `strategies/new_coin/config.yaml`：`record_name: "新币做空策略"` → `"new_coin"`；同步修正 333-334 行注释（原注释声称与 trade_records 权威名一致，实为不一致）。
- 影响面核查：dashboard/collector 已有 `new_coin↔新币做空策略` 显示名映射，不受影响；btc_eth/aggr 的 competing_record_names 不含该名；占用表历史行全部已 RELEASED，改后 release_claim 按 `symbol+strategy+状态` 匹配新名即可。

### D2 互斥 SQL 排除终态（`shared/position_ownership.py`）
- `_open_entry_sql(status_filter)`：两分支统一追加 `AND status NOT IN ('CANCELED','EXPIRED')`。
- 依据：status 列 NOT NULL default 'NEW'；现存数据无 CANCELED 行（今日起才写入），追加条件对存量数据零影响，仅使 D3 新标记的终态记录退出互斥。
- `resolve_position_owner`（status_filter=True 分支）同步受益：终态记录不再被误报为归属方。
- 同步更新 docstring 与 `tests/test_position_ownership.py` 的 SQL 断言（69-76 行）。

### D3 撤单单点同步标记（`shared/binance_api.py` + `shared/trade_logger.py`）
- TradeLogger 新增 `mark_order_canceled(order_id, symbol)`：`UPDATE trade_records SET status='CANCELED' WHERE order_id=$1 AND symbol=$2 AND status='NEW'`（仅 NEW→CANCELED 降级，绝不触碰 FILLED）。
- `binance_api.cancel_order` 在撤单成功后调用（trade_logger 已注入时）；撤单失败（订单可能已成交）不标记。
- 全策略共用该单点，hrs/btc_eth 的撤单同样受益。
- 已知边界：部分成交后撤单会被标记 CANCELED——互斥 SQL 不依赖数量，真实持仓由 R6（交易所持仓 ∪ 本地持仓表）兜底，可接受，记录于审查。

### D4 历史幽灵数据清理脚本（`scripts/fix_ghost_entry_records.py`，新增）
- 候选集：`order_type IN ('LIMIT','MARKET') AND status='NEW' AND realized_pnl IS NULL`。
- **逐单调用交易所查询订单接口**（PM 账户 `/papi/v1/um/order`，普通 `/fapi/v1/order`，与 binance_api 同一套凭证/端点逻辑）：
  - 交易所 CANCELED/EXPIRED/不存在 → 本地置 CANCELED
  - 交易所 FILLED → 保留（真实开仓记录），不改状态
  - 查询异常 → 跳过并记录，宁保守不动
- 默认 dry-run 输出清单，`--execute` 才落库；全程日志。
- 执行环境：本地运行（遵守「禁止服务器回测/重负载」精神；轻量 API 对账脚本，凭据用本地 .env，缺凭据时记录 BLOCKED 转服务器容器内执行需用户授权）。

## 4. 验收标准

| AC | 内容 | 验证方法 |
|----|------|---------|
| AC1 | new_coin 归属判定：自家 NEW 记录不再判为「其他策略」 | 单测：config record_name=new_coin；claim 二次校验 owner=self 放行 |
| AC2 | CANCELED/EXPIRED 记录不参与互斥归属 | 单测：SQL 断言 + fake db 行为测试（CANCELED 行不返回） |
| AC3 | 撤单成功后 trade_records 对应 NEW 记录变 CANCELED | 单测：mock trade_logger 断言调用；仅 NEW 被降级 |
| AC4 | 撤单失败（订单已成交）不标记 CANCELED | 单测：cancel 抛错路径 |
| AC5 | 清理脚本 dry-run 不写库；--execute 仅改交易所确认 CANCELED/EXPIRED/不存在的记录 | 脚本单测（mock 交易所+db）+ 生产 dry-run 清单人工核对 |
| AC6 | 回归：tests/test_position_ownership.py、test_newcoin_hrs_fixes 全绿 | pytest 退出码 0 |
| AC7 | 上线后 SNPSUSDT 下次评分达标可正常进入开仓流程 | 上线验证：占用表不再出现 authority_conflict 新行 |

## 5. 测试计划（A 段锁定，B 不得降级）

- 环境：本地 `python -m pytest`（Python 3.11+，无外部服务依赖，全部 fake/mock）。
- 命令：`python -m pytest tests/test_position_ownership.py tests/test_newcoin_hrs_fixes/ -q`（回归）+ 新增测试文件 `tests/test_shared/test_ghost_entry_cleanup.py`（AC1-AC5）。
- 覆盖率门槛：本次核心变更范围（`_open_entry_sql`、`mark_order_canceled`、cancel_order 标记分支、清理脚本判定函数）**行/分支覆盖 100%**；用 `--cov` 核对变更函数级覆盖并逐项映射未覆盖行，历史无关缺口单独列出。
- 十项真实性检查按方案 6.2 执行，重点：SQL 常量与 db 列名一致性（对照生产 `\d trade_records`）、`/papi/v1/um/order` 查询参数（byOrderId）、async 调用链。

## 6. 发布与回退

- 发布：push main → GitHub Actions；`shared/` 变更 → 重建全部策略容器 + ai-tuner；new_coin config → new_coin 容器；hrs COPY 全量。
- 顺序：代码部署 → 运行 D4 清理脚本 dry-run → 人工核对清单 → --execute → 复查 trade_records 终态分布。
- 回退：git revert（镜像回退）；数据侧 CANCELED 标记不影响回退后旧口径（旧 SQL 含 CANCELED 行仅多保守拦截，无资金风险）。
- 上线验证：4.2 容器状态 + VERSION 五层验证（仅本轮构建容器）+ AC7 观察下一个评分周期。

## 7. 未验证假设（移交 A-review 重点）

1. 假设生产 trade_records 现无 status='CANCELED' 存量行（未全量查询验证，D4 dry-run 会暴露）。
2. 假设 `update_realized_pnl` 平仓回写目标是入场记录（realized_pnl IS NULL 才能表示「未平」，依赖此不变量；若回写失败会产生保守误拦，非资金风险）。
3. PM 账户订单查询接口的参数与响应字段按 binance_api 既有实现推断，B 段需对照源码验证。
4. 假设 hrs/btc_eth 撤单路径同样走 `binance_api.cancel_order`（B 段核实调用链）。

## 8. A-review 复核记录（2026-10-14，GA 未通过）

- 复核者：design-review-local ｜ 实际模型：**GLM-5.3**（独立上下文；设计者为 GLM-5.3-Flash，独立性满足）
- 检查范围：方案全文 + handoff + 源码实证（shared/position_ownership.py、shared/trade_logger.py、shared/binance_api.py、shared/condition_orders.py、strategies/{new_coin,btc_eth,btc_eth_aggressive,hrs} 撤单与回写调用链、各 config）
- 结论：**有阻塞项 4 项**，交回 A 段修订后回本复核者确认，不进入 B。
- 已验证成立的原假设：假设 3（binance_api.get_order 已存在，binance_api.py:535-559，PM/普通端点自适应，D4 可直接复用）；假设 4（new_coin executor.py:1649、hrs executor.py:1660、btc_eth strategy.py:3879/4949 均经 binance_api.cancel_order）；D1 影响面中 btc_eth/aggr competing 名单不含 new_coin（btc_eth/config.yaml:724-725、aggr:731-732）、hrs 归属名一致（config name=hrs=写库名 hrs/main.py:97）。

### 阻塞项（B1-B4）

- **B1（证伪假设 2）**：`update_realized_pnl` 回写目标是**平仓单自身行**，非入场记录。证据：trade_logger.py:357-365 模式一按平仓 order_id（调用方 btc_eth/strategy.py:4016-4022 传平仓结果 orderId；new_coin/strategy.py:1309-1335 查 side='BUY' 平仓记录再回写）；trade_logger.py:397-416 模式二 side=平仓方向（与入场相反）+ order_id IS NULL + `order_type NOT IN ('LIMIT','CONDITIONAL_ORDER')` 明确排除 LIMIT 开仓单。后果：入场记录 realized_pnl **结构性永不回写**，「realized_pnl IS NULL=未平」不变量不成立；已平仓真实入场记录（status 恒 NEW）与幽灵同样永久参与互斥。D4 需新增「交易所 FILLED 且 positionRisk=0（已平仓）」状态类的判定与处置定义，否则 G4 清理不全、该类记录继续跨策略误拦。
- **B2（撤单成功判据未定义）**：cancel_order 返回 200 时响应 status 可能为 CANCELED **或** FILLED（撤销瞬间成交/部分成交后撤）。D3「撤单成功后调用 mark_order_canceled」未定义判据；若不校验响应 status=='CANCELED'，会把真实成交记录标 CANCELED → 持仓权威记录退出互斥 → 跨策略双开（资金风险）。AC3 的 mock 断言测不出此缺陷。
- **B3（R6 未定义）**：D3 已知边界引用「R6（交易所持仓 ∪ 本地持仓表）兜底」，方案全文无 R6 定义，B 段无从实现；部分成交撤单标记 CANCELED 后的残余仓位（_zero_micro_entry 清零失败仅告警，new_coin/executor.py:1659+）失去兜底。
- **B4（改名后自我防护退化未评估）**：改名前 new_coin 被 owner(=new_coin)≠my(=新币做空策略) 全量误拦（含重复开仓的意外兜底）；D1 改名后 is_owner_blocking（position_ownership.py:93）对 owner==my 一律放行，重复开仓防护完全依赖策略层 positions 跟踪。策略重启后 positions 恢复路径未评估——恢复不全 + 评分再命中 = 对已持仓币重复开仓。需定义重启恢复核查或证明既有防护充分。

### 非阻塞发现（写入 B 段任务契约）

- N1 同名函数冲突：shared/condition_orders.py:123 已有 `mark_order_canceled(db, order_id, algo_id)` 操作 condition_orders 表（btc_eth/strategy.py:5353、hrs/position_manager.py:508 在用）；D3 新增 TradeLogger 方法同名不同表。建议 TradeLogger 方法改名（如 `mark_entry_order_canceled`）并在任务契约写明 import 边界。
- N2 -2011 边界成交分支（new_coin/executor.py:1650-1653 吞掉）：订单实际 FILLED 时记录永久 NEW 幽灵 + 交易所持仓 + claim 已释放；D3 应定义 get_order 复查终态路径。
- N3 release_claim（position_ownership.py:487-515）按 symbol+strategy 匹配，旧名 PENDING/ACTIVE 占用行改名后无法释放，需等 TTL 30min；部署前应预核查 position_claims 无「新币做空策略」未决行，或明示接受 30min 自锁窗口。
- N4 假设 1（无存量 CANCELED 行）应在 GA 前用一条 SQL 预核查，不应留到 D4 dry-run（D2 先于 D4 生效）。
- N5 filter_owned_positions 调用方仅 btc_eth/aggr（main.py:193/323），new_coin 不调用，D1 对上报过滤无影响（方案影响面未查此点，结论侥幸成立）。
- N6 测试补项：AC3 增加 cancel 响应 status=FILLED/CANCELED 双用例；AC5 增加 FILLED+已平仓分组与存量 CANCELED 预核查；AC1 增加改名后自家持仓币重复开仓防护回归；AC7 量化观察窗口与失败阈值；增加 release_claim 旧名行兼容测试。

## 9. A 段修订（回应 B1-B4，2026-10-14）

GA 状态：仍未通过，待原复核者（GLM-5.3）确认本节后放行。本节与第 3 节冲突处以本节为准。

### 9.1 生产预核查结果（回应 N3/N4，已完成）

- trade_records 入场类记录（order_type IN LIMIT/MARKET）共 **864 条全部 status=NEW，0 条 CANCELED/CLOSED** → D2 对存量数据零影响，假设 1 证实。
- position_claims 无「新币做空策略」PENDING/ACTIVE 行 → 改名无自锁窗口；当前唯一活跃占用为 btc_eth_aggressive/XRPUSDT（正常开仓窗口，不触碰）。

### 9.2 D2（替代原 D2）：互斥 SQL 排除全部终态

`_open_entry_sql` 两分支统一追加 `AND status NOT IN ('CANCELED','EXPIRED','CLOSED')`。语义重述（B1）：互斥口径 = **非终态的最新入场记录**；「是否未平」不再依赖 realized_pnl（结构性永不回写已证实），而由状态机表达：NEW=提交未终态、CANCELED=已撤未成交、CLOSED=对应持仓已了结、FILLED=市价即时成交（视为未平，直到平仓流程标记 CLOSED）。

### 9.3 D3（替代原 D3）：撤单/平仓两个单点写终态

终态标记分两处落地（均避开 condition_orders.py:123 同名函数 `mark_order_canceled`，N1）：
- `shared/trade_logger.py` 新增 **TradeLogger 方法** `mark_entry_canceled`（binance_api 经 self.trade_logger 调用）；
- `shared/position_ownership.py` 新增**模块内直接 SQL**（不引入对 TradeLogger 的跨模块依赖）在 release_claim 内调用（见下 2）。

1. `mark_entry_canceled(order_id, symbol)`：`UPDATE trade_records SET status='CANCELED' WHERE order_id=$1 AND symbol=$2 AND status='NEW'`。
   调用判据（回应 B2）：**仅当 cancel_order 响应 `status=='CANCELED' AND executedQty==0`**。响应 FILLED（撤销瞬间成交）不标记；executedQty>0（部分成交后撤）不标记并 warning——部分成交记录保持 NEW，互斥 SQL 继续拦截对家，残余仓位存在时拦截正是期望行为，其最终清理由交易所 positionRisk 与策略层持仓管理负责（回应 B3，删除原「R6 兜底」引用并修正理由表述）。**C 段 F5 加固**：响应缺 `executedQty` 键时 warning + return 不标记（保守），不因 `.get()` 默认 0 误标部分成交记录。
   -2011 分支（回应 N2）：cancel 报 -2011（订单不存在/已消失）时，经 `binance_api.get_order`（binance_api.py:535-559，复核已证实存在）复查终态——CANCELED/EXPIRED → 标记；FILLED → 不标记；查询仍失败 → 不标记仅告警（保守）。
2. 平仓终态标记 `mark_entry_closed`（回应 B1，二轮修订）：**挂钩点不在 `update_realized_pnl`**（分批止盈 TP1/TP2 每次部分平仓都会调用它，会把尚有剩余持仓的入场记录过早标 CLOSED，使剩余持仓失去互斥权威记录），**移至 `position_ownership.release_claim`：仅当 `reason == 'position_closed'`（持仓归零确认）时执行**：
   `UPDATE trade_records SET status='CLOSED' WHERE id = (SELECT id FROM trade_records WHERE symbol=$1 AND strategy=$2 AND order_type IN ('LIMIT','MARKET') AND status NOT IN ('CANCELED','EXPIRED','CLOSED') ORDER BY executed_at DESC, id DESC LIMIT 1)`。
   release_claim 是全部策略持仓归零的公共确认点（btc_eth strategy.py:5145/6064 已证；new_coin/aggr/hrs 调用点由 B 段核实，缺失如实记录为已知保守缺口——仅导致该策略记录维持 NEW 超拦，无资金风险）。SQL 不按 side 过滤：归零时该 symbol+strategy 最新非终态入场记录即本次持仓的入场单，方向翻转场景亦正确。标记失败仅告警不抛错，绝不影响 claim 释放主流程；update_realized_pnl 接口零改动。**已知边界**：mark_entry_closed 取最新非终态记录，同 symbol 若同时存在刚提交未成交的 NEW 挂单会被一并视为最新而标 CLOSED（其互斥权威由策略层 positions 与交易所 positionRisk 兜底，不影响实际持仓判定）。

### 9.4 D4（替代原 D4）：清理脚本四分类处置（回应 B1）

候选集不变（order_type IN LIMIT/MARKET AND status='NEW' AND realized_pnl IS NULL），逐单 `get_order`（复用 binance_api.py:535-559）+ 涉及币种 `positionRisk` 查询：

| 交易所订单状态 | 币种持仓 | 处置 |
|---|---|---|
| CANCELED / EXPIRED / 不存在（错误码 -2011 / -2013，**C 段 F4 加固：两者同归不存在类**） | 任意 | 置 CANCELED |
| FILLED | positionAmt==0 | 置 CLOSED（历史已平仓入场记录） |
| FILLED | positionAmt!=0 | 保留不动（真实持仓，互斥正确） |
| 仍 NEW（遗留活单） | 任意 | **不标记不撤单**，输出清单人工决策 |
| 查询异常 | — | 跳过并记录 |

默认 dry-run 输出四类清单与计数，`--execute` 仅对前两类落库；日志全程。

### 9.5 B4 回应：改名后自我防护论证 + AC8

new_coin 对自家已持仓币的重复开仓防护不依赖归属层，而由 R6 三源并集承担：交易所 positionRisk（第一来源，重启无关）∪ position_tracking ∪ short_positions（重启后由 DB 重建，executor.py:1407 `_insert_short_position` B1 修复注释明示恢复优先读 DB）。双源同时失效才会误开，防护充分。新增 AC8：short_positions 存在 open 记录时 `_is_symbol_occupied` 返回 True 的回归测试（既有测试已覆盖则引用，缺失则补）。

### 9.6 AC 增补

- AC3 扩充：cancel 响应 CANCELED+qty0 → 标记；FILLED → 不标记；executedQty>0 → 不标记+warning；-2011+get_order=CANCELED → 标记；-2011+get_order 失败 → 不标记。
- AC2 扩充：CLOSED 记录不参与归属/互斥（单测 + SQL 断言）。
- AC5 扩充：四分类判定用例（mock 交易所）；存量终态预核查已完成（9.1）。
- 新增 AC8（见 9.5）、AC9：`release_claim(reason='position_closed')` 成功后对应入场记录变 CLOSED；**部分平仓（update_realized_pnl 于 TP1/TP2）不变更入场记录状态**（反例用例）；标记 SQL 失败不影响 claim 释放。
- AC7 量化：上线后观察 ≥1 个完整评分周期（new_coin 每小时 HH:05），判定标准=期间 position_claims 无新增 authority_conflict 行且 SNPSUSDT 若再达标则进入占位/下单流程；失败阈值=再出 authority_conflict 即回退核查。
- 覆盖率考核范围更新：`_open_entry_sql`、`mark_entry_canceled`、release_claim 的 position_closed 标记分支（含失败告警路径）、cancel_order 标记分支（CANCELED/FILLED/executedQty>0/-2011×get_order 成败）、清理脚本判定函数——100% 行/分支。

### 9.7 A-review 三轮确认（2026-10-14，GA 通过）

- 复核者：design-review-local / GLM-5.3（原复核者，独立上下文）。
- 二轮阻塞闭环确认：9.3.2 挂钩点移至 `release_claim(reason='position_closed')` 消除部分平仓过早标 CLOSED（AC9 反例到位）；B3 表述已修正（9.3.1）。
- 新缺口核查（源码实证）：`reason='position_closed'` 全部调用点（btc_eth strategy.py:5145/6064、aggr 5154/6073）均在 `del self.positions[symbol]` 归零/僵尸清理语境，无误标；其余释放调用 reason 均为 open_failed/open_exception/micro_zeroed，不触发标记。多笔加仓只标最新一条，更早记录保持 NEW（保守超拦，D4 可收敛）。
- 已知保守缺口（非阻塞，B 段必须核实）：new_coin/hrs 平仓路径未发现 release_claim(position_closed) 调用点——后果为该策略入场记录保持 NEW 保守超拦（无资金风险），B 段核实后补一行调用或如实记录为已知缺口；G1（SNPSUSDT 解锁）由 D4 清理达成，不受影响。
- **结论：GA 通过，交接 B（R3 强模型 Seed-2.1-Pro）**。

### 9.8 C 段审查与复审记录（2026-10-14，GC 通过）

- **一审（code-specification-inspector-local / GLM-5.3，独立上下文）**：通过（无阻塞），非阻塞发现 F1-F7；B 修复 F1-F6 后交付（F5 executedQty 缺键保守分支、F4 错误码 -2011/-2013 同类、F6 本节边界标注、F1-F3 补 15 用例至 47 个）。
- **复审（原 C 审查者 resume，独立上下文）**：F1-F6 逐项核对落地（binance_api.py:1124-1129 缺键分支、fix_ghost_entry_records.py:93 错误码集合、9.3/9.4 标注、测试齐备）；自行复跑 `pytest tests/test_position_ownership.py tests/test_newcoin_hrs_fixes/ tests/test_shared/test_ghost_entry_cleanup.py -q` → **124 passed, EXIT=0**；无新阻塞。非阻塞 N1（缺键 warning 语义泛化）、N2（classify_entry 冗余守卫）、N3（-2011/-2013 不查持仓，靠 dry-run 人工核对兜底）均不阻塞。
- **结论：GC 通过，交接 D 段（部署 + D4 dry-run/execute + AC7 上线验证）**。
- 文档同步：docs/architecture/数据库设计.md v1.8（status 终态语义 + 互斥口径）；本节及 9.3.1/9.4 补记加固内容。
