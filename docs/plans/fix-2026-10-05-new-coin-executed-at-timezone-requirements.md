# new_coin 平仓盈亏回写：executed_at 时区编码错误修复

创建：2026-10-05；角色：A（需求与设计）；风险：**R2**

修订：2026-10-15（A 段回流）：AC5 原措辞假设基线全绿，实测基线已有 25 个无关失败，改为"新增/受影响测试通过且基线失败集合不扩大"，见 §6.5。风险级别保持 **R2** 不变，依据不变：本次仅影响 `realized_pnl` 回写与止损打标统计口径，不改下单/平仓/资金计算。

## 1. 背景与目标

容器 `trading_system-new_coin` 日志报错（level=warning，event=回写平仓盈亏失败）：

```json
{"symbol": "SECZUSDT",
 "error": "invalid input for query argument $3: datetime.datetime(2026, 10, 1, 2, 35, 28...",
 "event": "回写平仓盈亏失败", "logger": "strategies.new_coin.strategy"}
```

**目标**：修复 new_coin 策略在回写平仓盈亏 / 止损打标时，因 datetime 时区编码问题导致查询失败的缺陷。

**影响范围**：`realized_pnl` 回写失败 + 条件单止损打标可能漏打（影响周报复盘与风控看板"最近止损次数"统计口径）。异常被 try/except 捕获，**不影响平仓主流程**。

**非目标**：不修改交易下单/平仓/资金计算逻辑；不调整 `executed_at` 列类型或数据迁移；不改其他策略。

## 2. 需求（验收条件）

- **AC1** 带时区的 `entry_time`（如 `+00:00` / `+08:00` 的 ISO 字符串）解析后，传给 `db.fetch_one` 查询参数 `$3`/`$4` 的值必须是**无时区** datetime（`tzinfo is None`），且换算为北京时间口径。
- **AC2** 无时区 `entry_time` 解析结果行为不变（原样返回，不额外位移）。
- **AC3** `entry_time` 缺失或非法时返回 `None`，走既有跳过/告警分支，不抛异常。
- **AC4** 三处解析逻辑统一抽取为同一公共函数，消除重复代码（满足"禁止重复代码"规范）。
- **AC5** 本次新增与受影响的测试全部通过；且基线既有失败集合不得扩大（B 段须给出基线与当前的失败集合逐条比对证据）。新增测试覆盖 AC1/AC2/AC3。

## 3. 根因分析

- `trading.trade_records.executed_at` 列为 `TIMESTAMP`（无时区），约定存北京时间（见 `database/postgres/init-scripts/03-create-trade-records.sql` 与 `shared/trade_logger.py` 的写入 `datetime.now(BEIJING_TZ).replace(tzinfo=None)`）。
- 项目数据库驱动为 `asyncpg`（`shared/database.py`）。asyncpg 向无时区 `timestamp` 列写入**带时区** datetime 会抛 `DataError: invalid input for query argument $N`。
- `strategies/new_coin/strategy.py` 中三处用 `datetime.fromisoformat(entry_time)` 解析后直接传给 `executed_at BETWEEN $3 AND $4`；而 `entry_time` 来源既有无时区字符串（`datetime.now().isoformat()`），也有带时区字符串（`datetime.now(timezone.utc).isoformat()`、aware 的 `entry_time.isoformat()`），后者解析结果为 aware datetime，触发上述错误。

## 4. 设计决策

### 4.1 公共函数（新增，`strategies/new_coin/strategy.py` 模块级）

```python
# 北京时间时区（与 trade_records.executed_at 的无时区北京时间口径一致）
BEIJING_TZ = timezone(timedelta(hours=8))


def _entry_time_to_naive_beijing(entry_time: Optional[str]) -> Optional[datetime]:
    """把 entry_time 解析为无时区北京时间 datetime。

    trade_records.executed_at 为无时区 TIMESTAMP（存北京时间），asyncpg 拒绝
    向该列写入带时区 datetime；而 entry_time 来源既有无时区也有带时区 ISO 字符串。
    本函数统一转成无时区北京时间，避免查询抛 DataError。

    Args:
        entry_time: 入场时间 ISO 字符串，可为空。

    Returns:
        无时区北京时间 datetime；缺失或解析失败返回 None。
    """
    if not entry_time:
        return None
    try:
        dt = datetime.fromisoformat(entry_time)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(BEIJING_TZ).replace(tzinfo=None)
    return dt
```

依赖已就绪：`strategy.py` 顶部已 import `datetime, timedelta, timezone` 与 `Optional`。無需新增依赖。

### 4.2 三处替换点（均在 `strategies/new_coin/strategy.py`）

| 位置 | 现逻辑 | 替换为 |
|---|---|---|
| `_backfill_close_pnl`（约 1276-1284） | `entry_time_obj = None` + `try: datetime.fromisoformat(entry_time)` | `entry_time_obj = _entry_time_to_naive_beijing(entry_time)` |
| `_monitor_positions` 条件单补打标分支（约 1397-1402） | 同上的 try/except 解析 | 同上 |
| `_monitor_positions` 回写分支（约 1447-1452） | 同上的 try/except 解析 | 同上 |

替换后删除原 try/except 包装（解析失败 → None 由函数内处理，外层既有 `if entry_time_obj:` 判断保持不变）。三处 `upper_time = entry_time_obj + timedelta(hours=24)` 及查询 SQL 均不变。

### 4.3 废弃代码清理

原三处各自内联的 `entry_time_obj = None` + `try/except` 解析片段随替换删除，不留死代码。

## 5. 任务分工与可修改文件

- 实现文件：`strategies/new_coin/strategy.py`（新增 1 个常量 + 1 个函数 + 3 处替换）。
- 测试文件：`strategies/new_coin/test_realized_pnl_fix.py`（新增用例）。
- 不改动：`shared/`、其他策略、数据库 DDL、部署配置。

## 6. 测试计划（B 段执行）

### 6.1 运行环境

- 解释器：项目 Python（见 `requirements.txt`；依赖 `asyncpg`、`pytest`、`pytest-asyncio`）。
- 测试配置：`pytest.ini`（`asyncio_mode=auto`）。注意 `testpaths=tests`，本策略测试需**显式指定路径**运行。

### 6.2 运行命令

```bash
cd /Users/yl/vscode/Binance_quantitative_trading
pytest strategies/new_coin/test_realized_pnl_fix.py -v
```

回归（受影响相邻模块）：

```bash
pytest strategies/new_coin/ -v
```

### 6.3 新增用例（覆盖 AC1/AC2/AC3）

- `_entry_time_to_naive_beijing` 是纯函数，直接单测：
  - aware `+00:00` 输入 → 返回无时区、北京时间（+8h）datetime，`tzinfo is None`。
  - aware `+08:00` 输入 → 返回无时区、原时刻 datetime，`tzinfo is None`。
  - naive `"2026-06-01T00:00:00"` → 原样返回，`tzinfo is None`。
  - `None` / 空串 / 非法串 → 返回 `None`。
- 端到端断言（复用现有 `strategy` fixture，mock `db.fetch_one`）：
  - 以 aware `entry_time` 走平仓回写分支，断言 `db.fetch_one` 收到的第 3 个位置参数为 `datetime` 且 `tzinfo is None`。

### 6.4 覆盖率门槛

- 核心变更（`_entry_time_to_naive_beijing` 及三处调用）行/分支覆盖 100%：`not entry_time`、解析失败、aware、naive 四个分支均有断言。
- 报告分行覆盖、分支覆盖、业务场景覆盖三层，不混为一个数。

### 6.5 基线既有失败说明（2026-10-15 补充）

基准（`git archive HEAD` 导出的干净副本）在 `strategies/new_coin/` 下即存在 25 个失败，与本次修复**无关**：

- **数量与结论**：基线 `pytest strategies/new_coin/ -q` = **25 failed, 163 passed**；当前工作区（含本次修复）= **25 failed, 176 passed**（+13 为本次新增用例，全部通过）。
- **根因**：
  - 生产代码新增 `await self.trading_executor._update_short_position_closed(...)`（strategy.py 约 1418 行）等调用，但多个既有测试的 fixture 用 `MagicMock()`，其方法不可 await → `_monitor_positions` 中途抛 `object MagicMock can't be used in 'await' expression`，导致断言（如「持仓被清理」「fetch_one 被调用」）失败；
  - 另有 2 个陈旧断言：`test_update_realized_pnl_success` 期望 `'100.50'`（字符串）但生产代码传 `Decimal`；`test_fallback_sql_correctness` 期望 SQL 含 `SELECT id FROM trading.trade_records`，生产已改为 `SELECT tr.id FROM trading.trade_records tr` 别名形式。
- **证据命令**：
  ```bash
  git archive HEAD | tar -x -C /tmp/base_checkout   # 导出干净基线
  cd /tmp/base_checkout && pytest strategies/new_coin/ -q   # 25 failed, 163 passed
  cd /Users/yl/vscode/Binance_quantitative_trading && pytest strategies/new_coin/ -q   # 25 failed, 176 passed
  diff /tmp/base_fail.txt /tmp/cur_fail.txt   # 失败集合逐条一致，零回归
  ```
- **约束**：**本任务不修复**这 25 个既有失败，另行登记为独立 issue。**不得**把基线失败计入本次通过率，也**不得**通过删断言/放宽门槛来消除它们。AC5 的判定只看「新增/受影响测试通过」与「基线失败集合不扩大（逐条 diff 一致）」。

## 7. 发布与回退

- 当前段（A）仅本地规划，**未授权提交/推送/部署**，发布记 N/A。
- 实现+测试+审查通过后，上线走既有流程：`push main → GitHub Actions`（见 `.trae/rules/deployment.md`），需用户显式授权推送。
- 回退：`git revert` 对应提交后重新 push；本修复不涉及数据迁移，`executed_at` 列无结构变化，无数据回退风险。