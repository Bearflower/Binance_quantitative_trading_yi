---
id: Binance_quantitative_trading-20261019-mtpc-signal-persistence-dedup-and-status
类型: decision
项目: Binance_quantitative_trading
标题: MTPCS 系信号持久化：dedup_key 小时粒度幂等 + 仅 cancelled→executed 单向升级 + 有效信号按策略声明 status 过滤
日期: 2026-10-19
标签: [信号持久化, dedup_key, 幂等, ON CONFLICT, ai_tuner, readiness, 状态域]
任务标识: mtpc_signal_persistence_2026-10-19
状态: 已采纳
审查: 待审查
---

# MTPCS 系信号持久化：dedup_key 小时粒度幂等 + 仅 cancelled→executed 单向升级 + 有效信号按策略声明 status 过滤

## 背景
阶段一准备度审计结论为 `decision=STOP`，其中 `btc_eth_aggressive` 的有效信号数为 **0**（`min_effective_signals=100` 门禁不合格）。核查生产库确认根因：**MTPCS 系（btc_eth / btc_eth_aggressive）只 `execute_signal` 执行、从不把信号结果持久化到 DB**，故 `*.trade_signals` 均 0 行（属**真实数据缺口，非缺陷**，详见 plan 回标 D13）。阶段二前置项① 即「MTPCS 系信号持久化」。需在设计上一次性确定：**幂等键、冲突升级语义、写库失败对主流程的影响边界、以及审计侧「有效信号」的状态口径**。

## 备选方案
- **A（幂等键用 `signal_id` 之类唯一标识）**：需信号侧先产出稳定 id，改动面大、且历史信号无 id；与阶段一「零行为变更、旁路写入」的克制要求不符。
- **B（幂等键用 `{SYMBOL}:{DIRECTION}:{YYYYMMDDHH}` 小时粒度）**：无需新增信号侧标识；同小时同币同方向自然合并为同一「决策」，与 MTPCS 系「小时级决策、执行后定终态」的运行节奏一致；重复写天然幂等，最贴合现有数据。
- **C（每次执行都无条件 INSERT 新行）**：会产生重复行、污染「有效信号」计数，且无法表达「同一决策被后续升级」，不可取。
- **审计侧口径 ①（保持纯 `COUNT(*)`）**：会把 `cancelled`（未成交/被拒）也计为「有效信号」，口径失真。
- **审计侧口径 ②（硬编码统一 `status IN ('pending','executed')`）**：违反「禁止硬编码」，且 MTPCS 系与 hrs（状态域 triggered/executed/timeout/conflict）状态集合本就不同，统一硬编码会误伤 hrs。
- **审计侧口径 ③（策略级配置声明 status 过滤）**：由 `readiness.effective_signal_statuses` 按策略声明，auditor 只查表、不写死；未声明策略（如 hrs）保持纯 `COUNT(*)`。

## 决策
采纳 **B + 审计侧口径 ③**，并固化四条不变量：
1. **dedup_key 小时粒度幂等**：`dedup_key = {SYMBOL}:{DIRECTION}:{YYYYMMDDHH}`（symbol/direction 统一大写，决策时间取小时；同小时同币同方向视为同一决策）。
2. **`ON CONFLICT (dedup_key) DO UPDATE` 仅允许 cancelled → executed 单向升级**：`WHERE {schema}.trade_signals.status = 'cancelled' AND EXCLUDED.status = 'executed'`——首写事实优先，`executed` 不被后续 `cancelled` 覆盖（单向、不可逆）。
3. **状态域 executed｜cancelled**（落在既有表 `CHECK (status IN ('pending','executed','cancelled'))` 内）：`trade_success=True → executed`，否则 `cancelled`（原因记 metadata）。
4. **effective_signal_statuses 按策略配置声明、禁止硬编码**：`readiness.effective_signal_statuses` 为**可选键**（**不在 `_REQUIRED_READINESS_KEYS`**），仅对声明策略（`btc_eth` / `btc_eth_aggressive` → `["pending","executed"]`）追加 `AND status IN (...)`；未声明者保持纯 `COUNT(*)`。
另配两条硬约束：5. **落库失败告警但不改决策**——整段 try/except 包裹，**绝不 raise、绝不影响主流程**，失败仅 `logger.warning` + `notification.send_alert`（告警调用本身也兜底）。6. **DDL 幂等**：`init-scripts/12-mtpc-signal-persistence.sql` 用 `ADD COLUMN IF NOT EXISTS` + `CREATE UNIQUE INDEX IF NOT EXISTS`，策略启动侧亦用 `execute_ddl` 兜底建列（失败仅记日志、不阻断启动）。

## 理由
- 小时粒度 `dedup_key` 复用现有数据即可幂等，**不引入信号侧新标识**，契合阶段一「旁路、零行为变更」的克制要求（备选 A 被否）。
- `ON CONFLICT ... WHERE cancelled→executed` 把「先记事实、后允许升级」编码进 SQL：既消重、又保留「首写事实优先」的可审计语义，避免后到的 cancelled 抹掉已成交事实。
- 审计侧口径 ③ 同时满足两条约束：**不硬编码**（策略状态集合差异由配置声明）+ **不误伤 hrs**（未声明即保持纯 `COUNT(*)`）；将「有效信号」口径标准化为「已进入执行集合的信号」。
- 落库失败「告警但不改决策」保证持久化是**纯旁路**，任何 DB 异常都不改变下单/风控行为，符合资金安全前提。

## 后果
- 收益：MTPCS 系补齐信号持久化，缺口① 收口；`btc_eth` / `btc_eth_aggressive` 的「有效信号」在审计中为可计数值（排除 cancelled）；重复写无副作用、失败可重试。
- 技术债 / 边界：①「有效信号」口径对 MTPCS 系定义为 `pending` + `executed`，若未来引入新的非终态 status，需同步在 `effective_signal_statuses` 声明；② 该键为可选，若某策略需要状态过滤却忘记声明，则静默退回纯 `COUNT(*)`（口径可能偏松），需在巡检/审计报告中留意；③ DDL 走 `init-scripts/` 随 CI 部署生效，`migrations/` 不自动执行（沿用既有口径）。
- 回退安全：策略侧接线（`main.py` 的 `ensure_signal_table_exists` / `persist_signal`）与审计侧过滤（`effective_signal_statuses`）均为独立开关式改动，移除/清空配置即可退回纯 `COUNT(*)`；不涉及交易决策路径。

## 适用范围与修订
- 适用：`shared/mtpc_signal_persistence.py`（`build_dedup_key` / `persist_signal` / `ensure_signal_table_exists`）、`database/postgres/init-scripts/12-mtpc-signal-persistence.sql`、`ai_tuner/readiness/auditor.py`（`_validate_effective_signal_statuses` / 信号计数附加 `AND status IN (...)`）、`ai_tuner/config.yaml`（`purpose_driven_mvp.readiness.effective_signal_statuses`）、`strategies/btc_eth/main.py`、`strategies/btc_eth_aggressive/main.py`。基线与收口：`mtpc_signal_persistence_2026-10-19`。
- 与既有决策关系：**不替代**任何既有 decision；其 DDL 部署路径沿用 `20261001-deploy-yml-init-scripts-unsynced` 口径；审计侧可选键设计沿用阶段一 plan 回标 D12「强制键 vs 可选键」的取舍。
- 替代条件：若未来把有效信号口径升级为「可回放集合/含 regime 维度」，或信号持久化引入稳定 `signal_id`（备选 A 前提具备），本决策可迁移；届时新建 decision 并双向链接，无取代关系。

## 关联与证据
- 来源任务: `mtpc_signal_persistence_2026-10-19`
- 证据摘要: 新增模块测试 **280 passed，`shared/mtpc_signal_persistence.py` 100% 覆盖**；接线测试覆盖 `initialize()` 调 `ensure_signal_table_exists`（每 schema 一次）与 `run_strategy()` 在 `execute_signal` 后调 `persist_signal`、`trade_success` 已写入 result、落库失败不影响主流程（`tests/test_signal_persistence_wiring.py`）。
- 关联计划/设计: plan `docs/superpowers/plans/2026-10-17-ai-tuner-mvp-safety-readiness.md` 回标 **D14**（阶段二前置项① 缺口表更新 + 口径回标）；spec `docs/superpowers/specs/2026-10-16-purpose-driven-ai-tuner-design.md` 回标 **S12**。
- 关联文档: 部署指南 `docs/deployment/统一交易系统部署指南.md`（init-scripts 第 12 项）。
- 关联 issue: [deploy-yml-init-scripts-unsynced](../issues/Binance_quantitative_trading-20261001-deploy-yml-init-scripts-unsynced.md)（DDL 部署路径口径，复用而非新根因）；**本项不新增 issue**（根因为真实数据缺口，非缺陷，已由 plan D13/D14 承载）。
