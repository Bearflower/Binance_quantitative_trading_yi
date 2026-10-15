# new_coin 平仓链路策略名口径修复（db_strategy_name → my_record_name）

创建：2026-10-15；角色：A（需求与设计）；风险：**R2**
任务标识：`newcoin_db_strategy_name_mismatch_2026-10-15`
关联工单：[Binance_quantitative_trading-20261015-newcoin-db-strategy-name-mismatch](../knowledge/issues/Binance_quantitative_trading-20261015-newcoin-db-strategy-name-mismatch.md)。

> **实施状态（2026-10-15，B 段已落地；代码未提交、未 push，待用户授权提交/上线）**：
> - ✅ `strategy.py`：新增 `from shared.position_ownership import load_ownership_config`（L18）；`__init__` 缓存 `self.my_record_name = load_ownership_config(self.config)['my_record_name']`（L90-91 含中文注释）；4 处查询替换为 `self.my_record_name`（B 段落地后实测运行行号 **L929 / L1347 / L1460 / L1509**），SQL 文本与参数个数/顺序不变；`db_strategy_name` 全文件计数 = **0**。
> - ✅ `main.py`：L108-109 去硬编码，`my_record_name = load_ownership_config(config)['my_record_name']` → `TradeLogger(db_manager, my_record_name)`（**未降级**，按 §4.3 完整实施）。
> - ✅ 3 处 fixture 口径修正：`test_realized_pnl_fix.py`（L14 注释 + L68/L741）、`tests/test_stop_loss_mark.py`（L23 + `object.__new__` fixture 补派生属性 `strategy.my_record_name`）、`tests/test_strategies/test_new_coin_kline.py`。
> - ✅ 新增 AC 文件 `strategies/new_coin/tests/test_db_strategy_name_unified.py`（**7 用例全绿**，覆盖 AC1 有值分支 / AC5 回退+warning / AC4 周报 / AC3 两分支 / `_backfill_close_pnl` 附加 / 哨兵恒等）。
> - ✅ 运行时实测 `load_ownership_config(cfg)['my_record_name'] == 'new_coin'`；零回归夹逼对照（`git archive HEAD` 快照 vs 工作区）双侧 `25 failed, 204 passed` 且失败 ID 集合逐条一致。
> - ⏳ 待办：`git commit` + `push`（需用户授权）+ 上线观察（见 §7）。

> **风险定级说明（R2）**：本次**不改**下单 / 平仓 / 资金计算逻辑，但会**使两条此前静默失效的链路恢复执行**
> ——条件单自动平仓的「补正止损打标」与 `consecutive_losses` 累加。后者会影响 `_check_consecutive_loss_pause`
> 的输入，**可能新触发策略暂停**，故按 R2 走全流程，并在 §7 附上线观察要求。

## 1. 背景与目标

new_coin 平仓链路中 4 处查询把「本策略在 `trading.trade_records.strategy` 列中的取值」写成
`self.config.get('strategy', {}).get('db_strategy_name', '新币做空策略')`，而该键**在配置中从未定义**
（运行期实测：`strategy.record_name='new_coin'`、`db_strategy_name` 键不存在），导致 `$1` 恒为中文旧显示名
`'新币做空策略'`；但写库名是英文 `'new_coin'`。

**目标**：让 4 处查询的 `$1` 与 `trade_records` 实际写库名**单一来源**、恒等，恢复被静默门控的三条链路。

**已实测的决定性证据（同一 SQL、同一 `$2/$3/$4`，仅 `$1` 不同）**：
`$1='新币做空策略'` → **0 行**；`$1='new_coin'` → **1 行**。全表 `group by strategy` 无 `'新币做空策略'`。

**非目标**：
- 不改未时区列归一（R2 `94ca3ed` 已完成）、不改下单/平仓/资金计算、不改 SQL 结构（只改 `$1` 取值来源）。
- 不改 `shared/trade_logger.py` 的 `update_realized_pnl` 语义、不改 `grid` / `hrs` / `btc_eth` 等策略。
- **不做**历史存量数据修补（见 §8，需单独授权）。

## 2. 需求（验收条件）

- **AC1** 运行期 4 处查询的 `$1` 等于 `trade_records` 实际写库名。判定口径：`$1 == load_ownership_config(config)['my_record_name']`，且该值须与 [main.py L107](../../strategies/new_coin/main.py#L107) 传给 `TradeLogger` 的第二参**完全一致**。
- **AC2** 新增单测直接断言传给 db 的第一个位置参数（**不做宽松匹配**）；4 处查询各自至少 1 条断言。
- **AC3** 命中路径端到端成立：当窗口内存在真实平仓单（`side='BUY'`、`order_type NOT IN ('LIMIT','CONDITIONAL_ORDER')`、`realized_pnl IS NULL`）时，`close_order` 非空且 `update_realized_pnl` 被调用；须实测模式二降级匹配的三重约束（`order_type` 排除 / `realized_pnl IS NULL` / `±time_window`≤1800s）是否真的满足，**不得推断**。
- **AC4** 周一复盘在有记录时不再输出「本周无交易记录，跳过复盘」。
- **AC5** 配置缺失时**不静默**：`record_name` 缺失由 `load_ownership_config` 回退 `strategy.name` 并记 `warning`；本次修复后 `'新币做空策略'` 这一默认值须从代码中**删除**（不再有第二来源）。
- **AC6** 新增/受影响测试全部通过；且**基线失败集合不扩大**（逐条 diff 一致）。注意：当前工作区含在途未提交改动，基线须取「修复前的当前快照」重新测定，不得沿用旧数字。
- **AC7** 不改下单/平仓/资金计算；不改其他策略；不引入新的硬编码值。

## 3. 根因分析

- `trading.trade_records.strategy` 列的写入方唯一为 `shared/trade_logger.py`，其 `__init__`
  （[L151-L163](../../shared/trade_logger.py#L151-L163)）把传入的第二参 `strategy_name.strip()` 原样保存为
  `self.strategy_name`，`log_order`（[L277-L280](../../shared/trade_logger.py#L277-L280)）将其直接写入 `$1`。
  → **无任何「中文名 ↔ 英文名」映射层**（容器内 `grep` 亦确认 `main.py:107` 传的是 `"new_coin"`）。
- 查询侧 4 处却读 `strategy.db_strategy_name`（**非写入侧使用的键**），且默认值是中文旧显示名：
  `_weekly_review`、`_backfill_close_pnl`、`_monitor_positions` 的「补正止损打标」与「平仓回写」两分支。
  **行号按函数名定位**（会随在途改动漂移）：容器基线 `94ca3ed` = `L925 / L1343 / L1456 / L1505`；
  本地在途工作区 = `L1121 / L1546 / L1659 / L1708`。
- 权威取值入口既已存在：[position_ownership.py L150-L185](../../shared/position_ownership.py#L150-L185)
  `load_ownership_config(config)['my_record_name']`，其实现即 `config['strategy']['record_name']`，
  缺失时回退 `strategy.name` 并 `logger.warning`。实测该入口返回值 = `'new_coin'`（正确）。
- 后果：
  1. `_backfill_close_pnl` 的 `update_realized_pnl`（两处调用之一）**永不执行**。
     ⚠️ 但 `realized_pnl` 列另有兜底：ai_tuner `IncomeReconciler`
     （[income_reconciler.py L263](../../ai_tuner/reconciler/income_reconciler.py#L263)，按 id、无 strategy 过滤；`enabled: true`、不排除 new_coin）
     → **本条不构成「pnl 列缺值」的后果断言**。
  2. 「补正止损打标」分支恒不成立 → 条件单自动平仓的亏损不打标、`consecutive_losses` 不累加。
     独立佐证：实测 new_coin 全表 869 行 `close_reason` **全部为 NULL**。
  3. 周一复盘恒「本周无交易记录，跳过复盘」。

## 4. 设计决策

### 4.1 方案对比与选型

| 方案 | 内容 | 评价 |
|---|---|---|
| **方案 1（选定）** | 在 `NewCoinStrategy` 缓存 `self.my_record_name = load_ownership_config(self.config)['my_record_name']`；4 处查询统一改用它；并评估把 `main.py` 的 `TradeLogger(db_manager, "new_coin")` 也改为同一来源 | **根治**：查/写单一来源；复用既有权威入口；缺失时回退+warning（AC5） |
| 方案 2 | 仅 `config.yaml` 补 `strategy.db_strategy_name: new_coin` | 改动最小，但保留错误默认值 `'新币做空策略'` 与 `record_name` 双来源，未来仍可能再度不一致 → 次选 |
| 方案 3 | 去掉 `strategy` 过滤，只按 `symbol + side + 时间窗口` 匹配 | **否决**：策略共享同一 PM 账户，跨策略串扰会误回写对家仓位的盈亏 |

### 4.2 实现骨架（方案 1）

模块顶部新增导入（`strategy.py` 已 import `Optional`/`Dict`，无新增第三方依赖）：

```python
from shared.position_ownership import load_ownership_config
```

在 `NewCoinStrategy.__init__` 末尾缓存（构造期即完成，避免每处重复取值）：

```python
# 策略在 trade_records.strategy 列中的唯一来源（与 TradeLogger 写库名同源）
# load_ownership_config 内部已做「record_name → strategy.name 回退 + warning」，此处不重复兜底
self.my_record_name = load_ownership_config(self.config)['my_record_name']
```

4 处替换（逐字等价替换，**SQL 与参数个数、顺序不变**）：

```python
# 替换前
self.config.get('strategy', {}).get('db_strategy_name', '新币做空策略')
# 替换后
self.my_record_name
```

### 4.3 写侧对齐（AC1 的「写查同源」要求）

将 [main.py L107](../../strategies/new_coin/main.py#L107) 由字面量改为同一来源：

```python
from shared.position_ownership import load_ownership_config
...
my_record_name = load_ownership_config(config)['my_record_name']
trade_logger = TradeLogger(db_manager, my_record_name)
```

同时把 `main.py` L112 仅用于日志展示的 `strategy="新币做空策略"` 字样替换为 `strategy=my_record_name`
（该处为日志文本，非业务参数，但对齐可消除排查误导）。
**若评估后认为改动 main.py 会影响既有测试契约，可降级为「只改 4 处查询、main.py 保留」**，
但须在 B 段给出「两处取值相等的断言」以兜住 AC1。

### 4.4 废弃代码清理

`'新币做空策略'` 字面量在 `strategy.py` 中随 4 处替换**全部消失**（`grep '新币做空策略' strategies/new_coin/*.py`
在修复后应无命中）；不得保留死默认值。

## 5. 任务分工与可修改文件

- 实现：`strategies/new_coin/strategy.py`（+1 导入、+1 缓存行、4 处替换）——**已实施**（见顶部实施状态）。
- 实现（可选，取决于 §4.3 评估）：`strategies/new_coin/main.py`（2 处）——**已实施（未降级）**。
- 测试：`strategies/new_coin/test_realized_pnl_fix.py`、`strategies/new_coin/tests/test_stop_loss_mark.py`、
  `tests/test_strategies/test_new_coin_kline.py`（**fixture 口径修正**，见 §6.4）——**已实施**；
  另**新增** `strategies/new_coin/tests/test_db_strategy_name_unified.py`（7 用例，见顶部实施状态）。
- 不改动：`shared/`、`ai_tuner/`、其他策略、数据库 DDL、部署配置、`config.yaml`（方案 1 不需要新增键）。
- **协调要求（B 段落地时状态更新）**：规划时工作区存在在途未提交改动（`new_coin/strategy.py` +358、`new_coin/executor.py` +547、
  `hrs/strategy.py` +366、`shared/market_cap.py` +236 等）。B 段实施时对方对 `new_coin/strategy.py` 的在途改动**已停止/消失**，
  该文件已回落至 HEAD 口径，本地行号 = 容器基线 L925 / L1343 / L1456 / L1505；实施后 4 处实测运行行号 L929 / L1347 / L1460 / L1509。
  **一律按函数名定位，勿按固定行号**；基线对比已以「修复前的 `git archive HEAD` 快照」为基准重新测定（见顶部实施状态）。

## 6. 测试计划（B 段执行）

### 6.1 运行环境

- 本机 Python 3.9.6；`pytest.ini` 的 `testpaths=tests`，故**本策略测试必须显式给路径**。

### 6.2 运行命令

```bash
cd /Users/yl/vscode/Binance_quantitative_trading
python3 -m pytest strategies/new_coin/test_realized_pnl_fix.py -q
python3 -m pytest strategies/new_coin/ -q
```

### 6.3 新增用例（覆盖 AC1/AC2/AC3/AC4）

- **AC1/AC2（4 处 `$1` 断言）**：不 mock 查询结果，直接断言 `db.fetch_one` / `db.fetch_all` 收到的第一个位置参数
  == `'new_coin'`（用例中 config 的 `record_name` 设为 `'new_coin'`）；4 处各 1 条。
- **AC3（端到端）**：构造满足模式二三重约束的记录（`order_type='MARKET'`、`realized_pnl IS NULL`、
  `executed_at` 在 `entry_time` 的 ±300s 内、`order_id IS NULL`），断言 `update_realized_pnl` 被 `await`。
  **另补一条反例**：记录为 `order_type='LIMIT'` 时不回写（防误扩匹配面）。
- **AC4（周报）**：注入 ≥1 条本周 `realized_pnl IS NOT NULL` 记录，断言不再输出「本周无交易记录，跳过复盘」。
- **AC5（回退不静默）**：`record_name` 缺失时 `my_record_name` 回退为 `strategy.name` 且产生 `warning`
  （可直接单测 `load_ownership_config`，或在 `NewCoinStrategy` 构造期断言）。

### 6.4 fixture 口径修正（**必做，否则测不出**）

现有 fixture **把错误契约写死**，修复后必须同步改：

| 文件 | 现值 | 改为 |
|---|---|---|
| [test_realized_pnl_fix.py L68](../../strategies/new_coin/test_realized_pnl_fix.py#L68)、L741 | `"db_strategy_name": "新币做空策略"` | `"record_name": "new_coin"`（并同步该文件 L14 的注释） |
| [tests/test_stop_loss_mark.py L23](../../strategies/new_coin/tests/test_stop_loss_mark.py#L23) | `{'strategy': {'db_strategy_name': '新币做空策略'}}` | `{'strategy': {'name': 'new_coin', 'record_name': 'new_coin'}}` |
| [tests/test_strategies/test_new_coin_kline.py L58](../../tests/test_strategies/test_new_coin_kline.py#L58) | `'db_strategy_name': '新币做空策略测试'` | 断言目标改为 `'new_coin'` |

**口径说明**：这是把断言**修正为正确契约**（英文写库名），**不是**放宽门槛；严禁用「删断言 / skip / 放宽」替代。

### 6.5 覆盖率门槛

- 4 处替换行覆盖 100%；`my_record_name` 的「有值 / 回退」两分支均有断言。
- 报告分行覆盖、分支覆盖、业务场景覆盖三层，不合并为一个数。

### 6.6 基线失败说明（须重新测定）

- `strategies/new_coin/` 既有失败（此前测定为 25 failed / 163 passed）须**以修复前的工作区快照重新测定**
  （因存在在途未提交改动）。
- 证据命令（快照法，避免污染在途改动）：
  ```bash
  cp -R strategies/new_coin /tmp/base_newcoin && python3 -m pytest /tmp/base_newcoin -q
  python3 -m pytest strategies/new_coin/ -q
  diff /tmp/base_fail.txt /tmp/cur_fail.txt   # 须逐条一致
  ```
- **约束**：不得把基线失败计入通过率，也不得通过删断言/放宽门槛消除它们。AC6 只看「新增/受影响测试通过」与「失败集合不扩大」。

## 7. 发布与回退

- 当前段（A）**仅本地规划**，未授权提交/推送/部署，发布记 N/A。
- **B 段已实施（见顶部「实施状态」），但尚未提交 / 未 push**；上线走既有流程：`push main → GitHub Actions`（见 `.trae/rules/deployment.md`），**需用户显式授权**。
  变更范围：`strategies/new_coin/`（+ 若含 §4.3 则 `main.py`）→ 仅重建 `trading-new-coin`。
- **上线观察要求（因本修复会唤醒 `consecutive_losses` 累加）**：
  1. 发布后 24h 内核对 `docker logs` 出现「止损打标」/ 亏损计数相关日志，且 `close_reason` 开始出现非 NULL 值；
  2. 核对 `_check_consecutive_loss_pause` 是否被触发；若触发，评估是否因历史未计数导致的**一次性跳变**，
     必要时按用户决策临时调整阈值或先只上「打标」不累加计数（拆成两步发布）。
- 回退：`git revert` 对应提交后重新 push；本修复不涉及数据迁移与列结构变更，无数据回退风险。

## 8. 遗留风险与需单独授权项

- **历史存量（需单独授权）**：new_coin 全表 869 行 `close_reason` 为 NULL、`realized_pnl` 部分由 ai_tuner
  对账兜底。**是否回填历史止损打标 / 是否重算历史 `consecutive_losses` 基线，不在本次范围**，
  须单独发起并授权；本次只修「今后」的链路。
- **测试债**：`strategies/new_coin/` 既有失败集合（数量待重测）仍不修复，已另行登记 issue。
- **同链路相邻缺陷（另开工单）**：止损打标 `entry_time` 类型不符（高优先级，见
  `docs/knowledge/issues/Binance_quantitative_trading-20261015-newcoin-stoploss-entry-time-type.md`）——
  该缺陷**会与实际打标路径叠加**（即使 `$1` 修好，若 `entry_time` 仍是 str 则打标仍抛 `AttributeError`），
  建议与本修复**同批次或紧随其后**处理，否则 AC3/AC4 的上线收益可能被它掩盖。
