# 策略修复基线（A1）

> 日期：2026-10-15。配套机器可读清单：[baseline_manifest.json](baseline_manifest.json)。
> 依据：[代码审计报告](../code_audit_2026-10-15/README.md)、[源码哈希与离线证据](../code_audit_2026-10-15/verification.json)。
> 关联方案：[策略行为修复、数据补证与相对收益研究执行方案](../../superpowers/plans/2026-10-15-strategy-evidence-and-relative-return-plan.md)。

**目的：** 在 B/C/D 段开工前，把「本地代码版本 / 线上运行版本 / 已由其他任务修复的范围」三者固定下来，避免重复修复与版本误判。

---

## 1. 本地代码基线

| 项 | 值 |
|---|---|
| 仓库根 | `/Users/yl/vscode/Binance_quantitative_trading` |
| remote | `Bearflower/Binance_quantitative_trading_yi` |
| HEAD | `94ca3eda8a6a51d69c1a1e6b36008f2fe10f51e7` |
| 采集时间 | 2026-10-15（Asia/Shanghai） |

### 1.1 未提交改动范围

未提交改动**全部落在** `docs/`、`.github/`、`.trae/`、`.claude/`、`skills/`、`task.md`。**`strategies/` 与 `shared/` 目录无任何未提交改动。** 因此**本地策略生产代码 == HEAD `94ca3ed`**，可直接按 HEAD 建立基线，不需要剔除工作区污染。

> **修订（2026-10-15，C1/D1 只读采集交付后）**：该结论针对 A1 采集时点（B/C/D 段开工前）。此后 **C1/D1 本轮新增了对 `shared/` 的未提交改动**——`shared/binance_api.py` 的 `get_user_trades` 增加 `end_time` / `limit` 分页参数（**纯新增可选参数，不传时行为与引入分页能力前完全一致，向后兼容**）；`strategies/` 仍无未提交改动。B 段若在 C1/D1 之后开工，须按函数名核对，勿按固定行号。
> 本轮其余交付均为**新增文件**，不改动既有生产逻辑：`shared/decision_evidence.py`、`scripts/export_execution_facts.py`、`database/postgres/migrations/2026-10-15-{decision-evidence,execution-facts}.sql`（**仅落仓库、未 apply**）、`tests/test_shared/test_decision_evidence.py`、`tests/test_accounting/`。
> **修订（2026-10-15，C1/D1 消费方/生产者接线 + DDL 迁入 init-scripts R1/R2/R3 落地后）**：上述「`strategies/` 仍无未提交改动」的表述**对 `btc_eth` 已不再成立**——R2 接线新增 `strategies/btc_eth/evidence_recorder.py` 并**修改** `strategies/btc_eth/main.py`（initialize/run_strategy 单点旁路接线）与 `strategies/btc_eth/config.yaml`（新增 `decision_evidence:` 段，默认关闭）。`shared/` 亦新增两份 sink：`shared/execution_facts_sink.py`、`shared/decision_evidence_sink.py`。变更均为**开关默认关闭的增量旁路接线**（生产行为零变化），但按部署规范 `shared/` 改动会触发**全部 10 个镜像重建**。此外两份 DDL 已由 `migrations/` **迁入** `database/postgres/init-scripts/10-decision-evidence.sql`、`11-execution-facts.sql`（**随 CI 部署执行**），`migrations/` 源文件保留不删、不 apply。B/C/D 段后续开工须按函数名核对，勿按固定行号，并注意 `btc_eth` 现有未提交改动。

### 1.2 审计复现脚本结果（本地基线检查命令）

```bash
python3 docs/research/code_audit_2026-10-15/verify_readonly.py
```

结果：**9 项检查全部通过（`passed: 9`）** —— 即在**当前本地源码**上，审计描述的 9 个问题仍可复现，说明这些问题**尚未被修复**（未被 94ca3ed / fce42bf 覆盖）。B 段按方案修复后，这些旧探针**故意保持失败是预期行为**，应新增目标行为测试，不得改旧证据让它重新变绿。

### 1.3 其他任务已改动 / 负责的文件（修改边界）

| 提交 | 日期 | 改动范围 | 对本轮的约束 |
|---|---|---|---|
| `fce42bf` | 2026-10-14 | `shared/binance_api.py`、`shared/position_ownership.py`、`shared/trade_logger.py`、`strategies/new_coin/config.yaml`、`scripts/fix_ghost_entry_records.py` + 测试 | 互斥/终态状态机改动；共享模块变更按规范会触发全部策略容器重建。B3/B4 不得回退这些改动 |
| `94ca3ed` | 2026-10-15 | `strategies/new_coin/strategy.py`（仅 `_backfill_close_pnl` 与盈亏回写路径，时区编码）、`test_realized_pnl_fix.py` | **只动盈亏回写，未触及 `analyze` / `should_entry` / 反弹判断**。B3（反弹参照）与它无重叠，但新测试必须基于当前 HEAD 版 `strategy.py` 编写 |

**结论：** B3/B4 与已完成的其他任务无功能重叠；new_coin `strategy.py` 的 signaled 版本已含 94ca3ed 修复，实施前以此为基线。

---

## 2. 线上运行版本（只读采集）

采集方式：`ssh root@43.156.242.184 docker ps / docker inspect / docker exec ... cat /app/VERSION`（只读，无写操作）。采集时间 2026-10-15。

| 容器 | 镜像 digest（sha256） | 容器内 GIT_SHA | 部署 Run（DEPLOY_ID） | 创建时间 |
|---|---|---|---|---|
| trading_system-hrs | `d5ed68d46ad7…` | `94ca3ed` | `00004B01` | 2026-10-15 09:03 |
| trading_system-new_coin | `59c320460cfb…` | `94ca3ed` | `00004B01` | 2026-10-15 09:03 |
| dashboard-api | `e217553edbf9…` | `94ca3ed` | `00004B01` | 2026-10-15 09:03 |
| trading_system-btc_eth | `f8792c95c7c1…` | `fce42bf` | `00004A01` | 2026-10-14 21:25 |
| trading_system-btc_eth_aggressive | `f5ca46dd2b17…` | `fce42bf` | `00004A01` | 2026-10-14 21:25 |
| trading_system-grid | `60a3e033ed70…` | `fce42bf` | `00004A01` | 2026-10-14 21:25 |
| trading_system-grid-realtime | `60a3e033ed70…`（同 grid 镜像） | `fce42bf` | `00004A01` | 2026-10-14 21:25 |
| trading_system-kline | `bee48dc5135e…` | `fce42bf` | `00004A01` | 2026-10-14 21:25 |
| trading_system-kline-monitor | `894f7edf66b4…` | `fce42bf` | `00004A01` | 2026-10-14 21:25 |
| ai-tuner | `315ddc8f2a61…` | `fce42bf` | `00004A01` | 2026-10-14 21:25 |
| data-backend | `615c736a4deb…` | `fce42bf` | `00004A01` | 2026-10-14 21:25 |
| trading_system-aggtrade-collector | `967e287a34bb…` | `fce42bf` | `00004A01` | 2026-10-14 21:25 |

**第三层 VERSION 校验结论：** 10 个业务镜像全部内置 `/app/VERSION`，读到的 `GIT_SHA` 与 `DEPLOY_ID` 可反解（`run_number = DEPLOY_ID >> 8`、`attempt = DEPLOY_ID & 0xFF`；`00004B01`→run 75 / `00004A01`→run 74）。

**关键判断：** 线上 `hrs` / `new_coin` 镜像 = **HEAD `94ca3ed`**；其余策略容器 = **`fce42bf`**。由于 `strategies/` / `shared/` 本地无未提交改动，**线上 hrs/new_coin 代码等价于本地 HEAD**；btc_eth/grid 等早一个提交，差异仅为 `fce42bf` 后的文档/CI 与 new_coin 改动（不涉及其自身逻辑）。

> 注：`kline-monitor` 已确认在运行（部署后易遗漏项，此处已核验）。另观察到服务器上并存 `trading_system-postgres`(15-alpine) 与 `common_service_postgres`(14-alpine)，属既有部署，不在本轮范围。

---

## 3. 关键源码哈希（git blob，HEAD `94ca3ed`）

| 文件 | git blob 哈希 |
|---|---|
| `strategies/hrs/strategy.py` | `8aead78c125ebbdce7f34460e7884c76aaf8da36` |
| `strategies/hrs/scoring_engine.py` | `3c57cc21bb7caa1ec014bb6a4295f0b36b99bbf3` |
| `strategies/hrs/candidate_pool.py` | `afd913bdd0f8d26417a80b5a56753f3f0f88ffb1` |
| `strategies/hrs/market_data.py` | `52e16482f53d5f0b5664a93517913430f0e8b1ed` |
| `strategies/hrs/position_manager.py` | `3ee637e83ac9128cba6470170b53a7bedd3f493f` |
| `strategies/new_coin/strategy.py` | `c6b46ed6e1b7fe71d5ec90e27a97bc72fadabf6c` |
| `strategies/new_coin/executor.py` | `9acef00268c7ad5bb71fb9d0ec4991cf2c029bfe` |
| `shared/market_cap.py` | `980f850958efa160dd1bb7840e9c4714cff1ef33` |
| `shared/config_loader.py` | `3c094d9041c47317907d0cf1f07c58440fa22a60` |
| `shared/capital_manager.py` | `173074650794b8dc4257e78ee4716120966782cf` |
| `shared/protection_retry.py` | `c627d592c94829567255e49a12cf0527e65b1f26` |

---

## 4. 实际应用配置版本（config_loader 离线加载）

| 策略 | `.active` 请求版本 | 本地实际应用 | 说明 |
|---|---|---|---|
| btc_eth | V20260811 | **V20260811** | 覆盖杠杆与等级仓位比例 |
| btc_eth_aggressive | V20260910 | **未应用覆盖层** | `load_error`：覆盖文件缺失/解析失败，回退基础配置 |
| new_coin | V20260830 | **V20260830** | 总分门槛 6.5、技术 9、三次冲顶 3.5 |
| hrs | V20260819 | **未应用覆盖层** | `load_error`：覆盖文件缺失/解析失败，回退基础配置 |
| grid | V20260830 | **V20260830** | 不改变 semi 与 realtime shadow 的模式事实 |

**结论：** `hrs` 与 `btc_eth_aggressive` 的 `.active` 请求版本**未被实际应用**，是「不回退不生效」而非「已按该版本调参」。B1 若需显式影子/执行策略，须在 config.yaml 中显式声明，不能依赖上述失效覆盖层。

---

## 5. 审计问题 → 当前状态对照

| # | 问题 | 状态 | 负责工作包 |
|---|---|---|---|
| 1 | HRS 常规 `analyze` 两次 `score` 未传 `price_change_24h`，EMM/半EMM 不可触发 | **仍存在**（verify 复现） | B1 |
| 2 | HRS 空候选池 LV-RM 只算不执行（信号被丢弃） | **仍存在**（verify 复现） | B2 |
| 3 | HRS `_resolve_conflicts` 非按全市场强弱排序执行 | **仍存在** | B2 观察项 |
| 4 | `market_cap` 回退 OI×20，OI/市值恒为 0.05 混入截面 | **仍存在**（verify 复现） | B4 |
| 5 | LV-RM 初始保护与后续管理不同口径（`add_position` 不存 `entry_mode`，`check_time_stop` 用全局时限） | **仍存在** | B5 |
| 6 | 新币 `recent_high` 含末根 K 线，`close > recent_high` 恒不触发 | **仍存在** | B3 |
| 7 | 新币 `sentiment_degraded` 未传入 `should_entry`（当前配置下无额外门槛影响） | **仍存在** | B3 |
| 8 | HRS `reset_time_stop` 混用开仓时间与复核时间，可能截短收入匹配窗 | **仍存在** | B5 |
| 9 | MTPCS 加仓 `merge_position` 用信号价参与均价 | **仍存在** | B6c |

**差异未知项（单列，不统一写"最新版本"）：** 无。本地策略代码 = 线上 hrs/new_coin = HEAD；其余容器早一个提交且差异不涉及自身逻辑。审计基线 `fce42bf` 与当前 HEAD 的差异已定位为「new_coin 盈亏回写 + 文档/CI」。

---

## 6. 验收

- [x] 每个策略可唯一关联代码（GIT_SHA / blob 哈希）、信号配置版本（§4）、运行额度来源（§下方说明）。
- [x] 差异未知项已单列（§5 末尾）、结论为「无未确认版本差异」。
- [ ] 运行额度来源与生效月（`CapitalManager.get_effective_margin_limit` 的 DB 月度额度 / 配置 / 静态限额三分支实际命中）**待 D 段接入账本时补齐**；本段仅记录口径。

**基线判定：通过。** B/C/D 段可以以 HEAD `94ca3ed` 为唯一本地基线开工。
