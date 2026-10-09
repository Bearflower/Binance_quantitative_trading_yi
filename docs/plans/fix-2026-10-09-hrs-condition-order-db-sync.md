# HRS 条件单 DB 状态不同步修复（「假孤儿单」根因）

- 日期：2026-10-09
- 风险分级：**R3**（撤单 / 持仓归属 / 状态一致性）
- 涉及文件：`shared/condition_orders.py`、`strategies/hrs/position_manager.py`、`tests/test_position_manager_cancel.py`

## 1. 现象与根因

**现象**：飞书收到「孤儿条件单自动清理完成」，对 `hrs` 的 LITEUSDT / HBARUSDT 等交易对逐条报
「✅ 成功取消（场景B: 交易所无持仓+无活交易记录）」，且「跳过（正常持仓）」数量高达 376~420，
远超真实保护单数量（单持仓理论仅 3 条）。

**根因**：条件单只在下单成功拿到 `algoId` 时写入 `condition_orders`（`status=OPEN`），但撤单路径
（补单清场 / 平仓清场 / 批量撤单）**只清本地内存 `algo_ids`，不回写数据库**：

1. `PositionManager.cancel_all_orders()` 批量成功分支只调用 `clear_algo_ids(symbol)`（内存），
   未更新 DB。
2. 逐个取消回退路径中，Step 1 取消的是本地 `algo_ids`，而 Step 2 的 DB 兜底把
   「已在 `already_cancelled` 里」的记录排除在外，同样不落库。
3. 平仓路径（`strategy.py` 两处）走的就是 `cancel_all_orders()`，因此同样不落库。

后果：TP2 达成后监控循环每轮（1 小时）重建一次保护单并清场，旧 `algoId` 在交易所被撤、在 DB 里
却永久保留 `OPEN`，线性沉积（`420 ≈ 140h × 3`）。当持仓平掉后，这些行被孤儿清理任务逐条尝试
取消；Binance 返回 `-2011`（订单不存在）被
[orphan_cleanup.py](../../ai_tuner/cleanup/orphan_cleanup.py) 当作「取消成功」，于是产生上述误导性告警。

**重要澄清**：这些单**当时确实在交易所创建成功**（拿不到 `algoId` 不会写库），并非下单失败；
也没有下单失败类飞书告警，因为下单本身是成功的。

## 2. 范围

- 目标：撤单成功后同步 `condition_orders` 状态；平仓路径自动获得同一行为。
- 非目标：不改动 `ai_tuner` 孤儿清理的 `-2011` 口径（列为残余风险）；不改动 `new_coin` / `btc_eth`
  各自的撤单逻辑。

## 3. 设计

| 项 | 方案 |
|---|---|
| D1 | 新增共享助手 `mark_open_orders_canceled(db, strategy_name, symbol)`：一条 `UPDATE ... WHERE strategy_name=$1 AND symbol=$2 AND status='OPEN'`，异常吞掉并告警（不阻断主流程） |
| D2 | `cancel_all_orders()` 批量成功分支：`clear_algo_ids` 后调用 `_sync_db_canceled(symbol)` |
| D3 | `_cancel_individual_orders()`：`failed == 0` 时调用 `_sync_db_canceled(symbol)`；`failed > 0` 保守保留 `OPEN`，不误标 |
| D4 | Step 2 的单条落库改为复用 `mark_order_canceled(db, algo_id=...)`，消除重复 SQL |
| D5 | 新增模块常量 `_STRATEGY_NAME = "hrs"`，替代散落的魔法字符串 |

平仓路径（`strategy.py` 全平 / `_finalize_close_if_filled`）均调用 `cancel_all_orders()`，被 D2 覆盖，
无需额外改动。

**表归属已核实**：`search_path` 首位为 `btc_eth`（`database/postgres/init-scripts/01-create-schema.sql`），
故 `shared/condition_orders.py` 的无限定表名与 `btc_eth.condition_orders` 为同一张物理表。

## 4. 验收条件

| 编号 | 验收条件 |
|---|---|
| AC1 | 批量撤单成功（`{complete: true}`）后，该 `strategy_name + symbol` 的 OPEN 行全部 → CANCELED |
| AC2 | 逐个撤单回退路径全部成功（`failed == 0`）后，同样全部 → CANCELED |
| AC3 | 逐个撤单部分失败（`failed > 0`）时，不做整币种标记（保守保留 OPEN） |
| AC4 | `db is None`（回测 / 无持久化环境）时不报错、不产生 DB 调用 |
| AC5 | 平仓路径无需额外改动即获得 AC1 行为 |
| AC6 | DB 同步失败只告警，不阻断撤单主流程 |

## 5. 验证证据

- `python3 -m pytest tests/test_position_manager_cancel.py tests/test_r01_r08_fixes/test_ai_tuner_orphan_cleanup_r08.py -q` → **16 passed**
- `python3 -m pytest strategies/hrs/tests/test_fix_verification.py tests/test_newcoin_hrs_fixes -q` → **119 passed, 3 failed**；其中 3 个失败经 `git stash` 比对确认为**基线既有失败**（`'HRSStrategy' object has no attribute 'circuit_breaker'`，与本次改动无关）
- `python3 -m py_compile` 两处源码 + 测试文件通过；`flake8 --max-line-length=120` 对新增代码无告警
- 新增用例：AC1/AC2/AC3/AC4/AC6 各 1 条，含「同步 SQL 参数为策略名+交易对」「部分失败不标记」断言

## 6. 残余风险（未在本次范围）

1. `shared/reduce_only_close.py` 在减仓单被拒（`-2022`）时会直接调用 `client.cancel_all_algo_orders(symbol)`，
   该路径无 DB 句柄、未同步；若随后平仓彻底失败且未触发补单，仍可能残留少量 OPEN 行
   （会在下一次成功的 `cancel_all_orders()` 或孤儿清理兜底时收敛）。
2. `ai_tuner/cleanup/orphan_cleanup.py` 仍把 `-2011` 记为「成功取消」，无法区分「本就已撤销」与「真的取消」；
   本次修复后该类假孤儿应大幅减少，但建议后续单独修正口径。

## 7. 流程关卡状态

| 阶段 | 状态 |
|---|---|
| 0 前置关卡 / 1 需求 / 2 设计（A） | 已执行（风险 R3） |
| 2 内 R3 独立设计复核（A-review） | **已通过**（2026-10-09，GLM-5.3 独立会话，见 §7.1） |
| 3 编码 / 4 代码检测 / 5 强制测试（B） | 已执行 |
| 6 审查与文档对照 / 7 文档更新（C） | **已通过**（2026-10-09，GLM-5.3 独立会话实现对照，见 §7.1） |
| 8 部署（D） | 未部署（需用户授权发布；下一角色 D：Seed-2.1-Pro-0915） |

### 7.1 R3 独立复核结论（A-review + C 段实现对照片审查，GLM-5.3，2026-10-09）

**结论：GA 通过、GC 通过，无阻塞项。** 复核命令 `python3 -m pytest tests/test_position_manager_cancel.py tests/test_r01_r08_fixes/test_ai_tuner_orphan_cleanup_r08.py -q` 复现 **16 passed**。

关键前提核查（均有源码/运行时证据）：

1. **search_path 表归属成立**：`database/postgres/init-scripts/01-create-schema.sql:29` 首位 `btc_eth`；运行时行为证据——hrs 无前缀写入的 OPEN 行确实被 ai_tuner 孤儿清理读到（飞书告警出现 hrs 行），证明写入端与读取端解析到同一物理表 `btc_eth.condition_orders`。
2. **`cancel_all_algo_orders` 成功判定成立**：`shared/binance_api.py:378-381` 对 `code not in (0, 200)` 直接抛 `BinanceAPIError`（错误 JSON 到不了 position_manager 判定层）；`strategies/hrs/position_manager.py:464-467` 兼容 `{"complete": true}` 与 `{"code": 200}` 双形态，双层防护。
3. **`-2011` 语义成立**：订单不存在 → `orphan_cleanup.py:290-293` 标 CANCELED，对「确保交易所无单」目标语义正确；口径问题已在 §6 记录。

失败状态与恢复路径（闭环确认）：

- 部分失败（`failed>0`）保守保留 OPEN，由下一轮补单清场整币种同步或孤儿清理（-2011）收敛。
- DB 同步失败三层吞异常只告警（AC6），OPEN 残留由孤儿清理兜底。
- 并发误标：4 处撤单调用点（`strategy.py:1889`、`strategy.py:3523`、`executor.py:1040`、`executor.py:1448`）全部同协程「先撤后建」或「撤后不建」，`_sync_db_canceled` 先于新 OPEN 写入；监控循环串行处理 symbol（`strategy.py:2221` 的 gather 仅评分不撤单）→ 当前代码无并发窗口。

测试证明力缺口（不阻塞，记录在案）：

- **AC5 无测试覆盖**：16 passed 不含平仓路径（`strategy.py:1889/3523`）；AC5 证据由本轮 C 段代码审查替代（两处均直接调用 `cancel_all_orders`，无旁路）。补测需拉起完整 HRSStrategy，成本高，暂以审查证据放行。
- AC3 测试 7 断言真实有效，但未覆盖 Step 2 兜底路径的部分失败变体（同一门判定，风险低）。
- AC4「不产生 DB 调用」由 `db is None` 早退结构保证，断言偏弱但语义无争议。

非阻塞观察项：

1. 表名前缀不一致：`mark_open_orders_canceled`（无前缀）与 `_cancel_individual_orders` Step 2（显式 `btc_eth.condition_orders`）并存，隐式耦合 `search_path`，建议后续统一。
2. 并发安全是「现状无窗口」而非结构性互斥；未来引入并行手动操作入口（如 HTTP 指令）需加 symbol 级互斥。
3. 测试 2 的 mock 直接返回 `{"code": -2011}` dict，与真实路径（`_request` 抛异常进 except）层次不同，但双层防护均被验证。