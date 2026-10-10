# grid V2.5.4「实时风险预警」M2 影子设施上线 —— C 段独立审查报告

- 审查日期：2026-10-09
- 审查对象：M2 里程碑（影子设施上线），提交 `927559d`（53 files, +10709/-92），DEPLOY_ID=00003701
- 审查角色：C 段独立审查者（只读；不得修改代码/配置/文档，不得部署）
- 审查方式：独立复跑测试与静态检查 + 代码走读 + 生产服务器只读核对（未照抄 B 段自述）
- 关联文档：`docs/handoffs/grid_realtime_alert_2026-10-08.md`、`docs/plans/grid_realtime_alert_实施计划.md`、`docs/requirements/grid/Grid_Trading_V2.5.4.md`

---

## 1. 结论

**GA（可关 M2 门），阻塞项 0。**

部署链路、shadow 不外发、状态机一致性、(b) 档最低集 AC 均可独立复现通过。

唯一实质性偏差：B 段自述「两处 coverage.py 误报」**仅 `feed.py:179` 成立**；`main.py:173` 实为**真实未覆盖行**（代码正确、生产可达，属测试覆盖缺口），列为非阻塞。

---

## 2. 阻塞项

无。

---

## 3. 非阻塞观察项

| 编号 | 说明 | 处置建议 |
|---|---|---|
| O1 | **自述不符**：`strategies/grid/realtime/main.py:173` 未被任何测试执行。`tests/test_grid_realtime/test_main.py:441` 的 `test_sync_engine_same_reference_returns_empty` 传入 fixture `snap`（`conftest.py:29`，`reference_id="grid-ev-test"`），而 `_publish_reference` 经 `ExportStore.prepare` 落库的是 `make_reference_id(...)` 生成 id，二者不等 → `if current.reference_id == snap.reference_id` 为假，走 `_switch_reference` 路径（返回值恰为 `[]`），该分支未真正覆盖 | M3 前补一条「同 reference_id 命中该分支」的断言 |
| O2 | **确认误报**：`feed.py:179`（`break`）及 `131->133` 确为 coverage.py 对 **async 生成器 `while True` 内 break** 的漏记。已独立复现：同结构探针在 sync 生成器下 100% 覆盖，在 async 生成器下 `break` 行被判 Missing | 无需改；如升级 coverage.py 可复验 |
| O3 | `trading_system-grid-realtime` **无任何日志输出**（`/root/trading_system/logs/grid` 为空、`docker logs` 为空），运行态仅靠 sqlite 可观测，运维排障弱 | M3 前补 `basicConfig` 与心跳/健康日志 |
| O4 | 迁移命令 `docker compose run` **未加 `-T`**（默认分配 TTY）；非 TTY 的 CI/SSH 环境依赖 compose 容忍。本次实证成功 | 建议 `-T` 加固 |
| O5 | `strategies/grid/signal_bot.py` 有 5 行 >120 字符（189/190/191/393/930），**均为历史遗留、非本次新增**（本次 commit 新增行字符级均 ≤120，已核） | 择机清理，不阻 M2 |
| O6 | `delivery.py:274`（`timeout_seconds=5.0`）、`main.py:44`（`maxlen=2000`）、`main.py:54`（分位表）、`main.py:198`（效率窗口 `[300]`）为内联字面量（属固定规格/观测常量，非业务阈值） | 建议提常量；shadow 下 sender 未实例化，当前无影响 |
| O7 | 生产 profile 为 `shadow_e30`（`min_efficiency=0.30`），与需求 §9.2 种子 `research_seed_01`（0.60）不同，属 M1 结论且已注释 | 非阻塞，M3 报告需注明口径 |

---

## 4. 独立复现的证据清单

### 4.1 审查者实际复现（命令原文与关键输出）

- `pytest tests/test_grid_realtime/ --cov --cov-branch`：`308 passed`；`TOTAL 2327 stmts / 2 miss / 99%`；`feed.py 149 1 98% Missing 131->133, 179`、`main.py 312 1 99% Missing 173`。
- `pytest tests/test_aggtrade_collector/ --cov`：`93 passed`；`TOTAL 645 stmts / 0 miss / 100%`。
- `pyflakes` exit 0；`py_compile` 全通过；字符级行长：本次新增代码 0 违规。
- coverage 误报复现：async 生成器探针 `aprobe.py 14 1 89% Missing 12`（第 12 行即 `break`）→ 证实 O2。
- **SSH 容器**：13 个 `trading_system*` 容器全 `Up (healthy)`；`grid-realtime`/`aggtrade-collector` `restarts=0`；两者 `/app/VERSION` 均 `GIT_SHA=927559d54db6…4bfbb`、`DEPLOY_ID=00003701`；`/proc/1/cmdline = python -m strategies.grid.realtime.main`。
- **SSH 库**：`reference 0 / session 1 ACTIVE / event 3 / outbox 3 / schema_meta version=1`；`outbox_status=[('SHADOW', 3)]`；event 三条均 HEALTH（`health-degraded-sync_uncertain`、`health-degraded-reference_missing`、`health-recovered-sync_uncertain`，时刻 21:26:13–15）。
- **SSH 部署日志**：`[2026-10-09 21:26:34] DEPLOY_SUCCESS commit=927559d54db6… deploy_id=00003701`；`./data/grid/grid_realtime.sqlite3` 建于 21:25，**早于**容器 21:26:13 启动。
- **SSH 落库器**：`agg_trades 1,566,444`（较 B 自述 411,593 持续增长，回溯中）、`price_samples_1s 0`；库文件位于命名卷 `aggtrade-data`（`/app/data/aggtrades/ethusdt_aggtrades.sqlite` + `-wal`/`-shm`）。
- `git HEAD = 927559d`。

### 4.2 读代码推断（非运行时复现）

- **迁移必须早于 `up -d`**：`main.run()` 仅 `check_schema`（缺失即 `RuntimeError` → crash-loop，`reference_store.py:130-147`）；`signal_bot._init_export`（`:614`）仅在构造时 `check_schema` 一次、失败只 warning **不重试** → 交接永久停用。
- **deploy.yml 顺序**：`docker compose pull`（:387）→ **schema 迁移 :398-400** → `up -d --remove-orphans`（:404）✓；matrix 11 项（含 `aggtrade-collector`）✓；`RESIDUAL_CANDIDATES` 已含 `grid-realtime`/`aggtrade-collector`（:362-365）✓。
- **shadow 无外发路径**：`needs_sender = (mode == "alert") or shadow_research.enabled` = `False` → `sender=None`；`record()` 在 shadow 写 `SHADOW`，`_due_rows` 只取 `PENDING/FAILED/UNKNOWN`，故 `pump` 永不触达 sender；`record_research` 在 `enabled=false` 直接 `return False`。运行态 outbox 全 SHADOW 佐证。
- **compose**：`grid-realtime` 与 `grid-strategy` 共用 `./data/grid`（刻意双进程交接），落库器用独立命名卷 `aggtrade-data`；无容器名/卷名/端口冲突。

---

## 5. AC (b) 档逐条判定

| AC | 判定 | 证据 |
|---|---|---|
| AC-03 重试/重启/持久化失败/非法边界 | 已满足 | `delivery` FAILED/UNKNOWN 退避重试；`current_snapshot` 非法→INVALID；test_reference_store 多例 |
| AC-10 新参考/小时消息/过期 | 已满足 | `apply_send_result`/`_switch_reference` 原子切换；STALE 由 max_age 派生；signal_bot 无边界不清参考 |
| AC-11 重复/乱序/缺口/断线/补齐 | 已满足 | `feed.py:144-146` 去重；退避重连 + `_refill`；`_fresh_price`/`_gap_ok` 过滤旧价；test_feed / test_refill_* |
| AC-12 数据不足与恢复 | 已满足 | 特征缺失返回 None 不填零；`on_second` 清 holds；`_check_recovery` 严格区间 |
| AC-13 失败/积压/过期/反弹 | 已满足 | 退避 + `condition_holds`；锁存事实无 TTL；`_render_latch` 30s 补报；pump 优先级排序 |
| AC-17 峰值负载/时间偏移 | 已满足 | `LatencyTracker` p50/90/99/999；`processing_lag` 降级 |
| AC-19 小时压力与实时并行 | 已满足 | 独立进程容器（运行态已证）；分组延迟分位 |
| AC-21 发送后落盘失败及出口重启 | 已满足 | `row.session_id != session.session_id` → SYNC_UNCERTAIN；test_reader_old_session_sent_sync_uncertain |
| AC-22 STALE 穿越与渠道中断 >30s | 已满足 | `on_fact` BOUNDARY 允许 STALE（带 stale 标记）；补报标题 |
| AC-26 配置/存储/投递故障 | 已满足 | `parse_profile` 全量校验；兼容 dict 加载；queue 溢出→storage_degraded；投递三态 |
| AC-27 同步不确定有无历史快照 | 已满足 | `historical_snapshot` + `_evaluate_historical`；test_sync_uncertain_with/without_cross |
| AC-28 等待/压力/心跳中断 | 已满足 | `HeartbeatThread`（daemon + 独立连接）；heartbeat=1s / max_silence=5s；test_heartbeat 全套 |
| AC-29 双进程并发/重建/迁移/磁盘满 | 已满足 | WAL + busy_timeout；`ensure_schema` 仅迁移命令独占、业务 `check_schema`；锁存不被清理；容量降级可见；运行态双进程共库 |
| AC-14 shadow/alert（§9.3 三护栏） | 已满足 | 文案「【研究参考】」+ 免责、不套 §8.3、`_render_latch` 不前置补报；频率护栏用配置项且只计 SENT；开关默认 false；`shadow_research` 关闭无外发 |

**最低集 AC-11/12/21/26/28/29：全部已满足。**

---

## 6. 对 B 段自述不符 / 无法复现之处

1. **`main.py:173` 不是 coverage.py 误报**（B 称两处均为误报）：复现为真实未覆盖行，测试因 reference_id 不匹配而未走到该分支（详见 O1）。
2. B 称「落库器 93 passed」→ 复现通过；覆盖率实为 100%（B 未声明覆盖率，无冲突）。
3. B 称 `agg_trades=411,593` → 审查者核对时为 **1,566,444**（回溯持续推进，非矛盾）。
4. B 称「13 容器全 Up(healthy)」「两容器 DEPLOY_ID=00003701」「outbox 全 SHADOW」「session=1 ACTIVE」「reference=0」——**全部复现一致**。
5. B 称「迁移在 `up -d` 之前」——无法从部署日志直接读步骤序，但由「库文件 21:25 早于容器 21:26:13 启动 + 业务进程仅 `check_schema` 且 `restarts=0`」**间接证实成立**。

---

**审查期间未修改仓库任何文件。**