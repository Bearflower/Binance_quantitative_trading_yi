# PRD: realized_pnl 回写修复

## 文档信息

| 项目 | 内容 |
|------|------|
| 文档版本 | v1.0 |
| 作者 | 需求文档专家 |
| 创建日期 | 2026-07-27 |
| 状态 | 初稿待评审 |

## 修订记录

| 版本 | 日期 | 修改内容 | 修改人 |
|------|------|---------|--------|
| v1.0 | 2026-07-27 | 初稿创建 | 需求文档专家 |
| v1.1 | 2026-07-27 | 修复方案A：update_realized_pnl 新增 side 参数，解决做多平仓 side='BUY' 硬编码问题 | 需求文档专家 |
| v1.2 | 2026-07-27 | 架构评审修复：模式一 side 动态化、降级匹配增加 LIMIT 1、盈亏公式集中管理 | 架构师 |

---

## 1. 产品概述

### 1.1 背景

`trading.trade_records` 表中的 `realized_pnl` 字段全部为 NULL，导致：

1. **AI 调优系统（ai_tuner）无法正确评估策略表现**：PnLCollector 从 `trade_records` 查询 `SUM(realized_pnl)` 时，全部为 NULL 导致 `COALESCE` 返回 0，总盈亏 total_pnl=0，进而 win_rate=0，AI 无法区分策略优劣，调优失去依据。
2. **日报/周报等统计报表无法展示盈亏数据**：`get_daily_stats` 方法中 `win_count` 和 `loss_count` 目前使用固定占位值 0，无法反映真实盈亏情况。

### 1.2 业务目标

1. 所有已平仓的 `trade_records` 记录应有正确的 `realized_pnl` 值
2. 确保未来所有平仓操作都能自动回写盈亏
3. AI 调优系统能采集到真实的盈亏数据，做出正确的资金分配决策

### 1.3 影响范围

| 影响方 | 影响级别 | 说明 |
|--------|---------|------|
| MTPCS 策略 (btc_eth) | 高 | 平仓后完全不回写 realized_pnl |
| 新币做空策略 (new_coin) | 高 | 平仓后回写逻辑存在 bug，匹配不到正确的平仓单 |
| 共享模块 (trade_logger.py) | 高 | update_realized_pnl 仅支持 order_id 匹配，不支持无条件单 ID 的场景 |
| AI 调优系统 (ai_tuner) | 高 | 盈亏数据全为 0，无法做出正确分配决策 |
| 日报/周报/仪表盘 | 中 | 盈亏统计缺失，影响策略评估展示 |

---

## 2. 问题分析

### 2.1 问题1：btc_eth (MTPCS) 策略 — 平仓后完全不回写盈亏

**根因描述：**

[`_close_position`](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L2463-L2761) 方法执行平仓操作（第2605行下限价单，第2634-2638行确认成交）后，只做了以下操作：

- 更新 `position.current_quantity`（第2696行）
- 发送平仓通知（第2699-2726行）
- 首次完全平仓后异步取消残余条件单（第2729-2735行）

**完全没有调用 `TradeLogger.update_realized_pnl` 来回写盈亏。**

**关键代码路径：**

```
_binance.place_order(symbol, side=close_side, ...)  →  binance_api.py place_order() 自动调用 log_order() 写入 trade_records
                                                                                      ↓
                                                                          trade_records 中有该笔平仓订单记录
                                                                          但 realized_pnl 字段为 NULL
                                                                                      ↓
                                                                          _close_position 返回 True
                                                                          没有调用 update_realized_pnl
```

**影响：** 该策略所有平仓记录的 `realized_pnl` 永远为空。

**为何能计算 PnL：** [`PositionState`](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L29-L59) 包含 `entry_price`（入场价格）和 `direction`（方向），平仓订单的 `order_result` 中包含 `avgPrice`（成交均价）。平仓 PnL 计算公式：

- LONG 方向：`(exit_price - entry_price) * quantity`
- SHORT 方向：`(entry_price - exit_price) * quantity`

---

### 2.2 问题2：new_coin 策略 — 平仓单 order_id 匹配不上

**根因描述：**

[`_monitor_positions`](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/strategy.py#L930-L1029) 方法在检测到持仓已平仓后，执行以下 SQL 查询平仓单的 order_id：

```sql
SELECT order_id
FROM trading.trade_records
WHERE strategy = $1 AND symbol = $2 AND side = 'BUY'
AND executed_at >= $3
ORDER BY executed_at DESC
LIMIT 1
```

**问题在于：** 该查询只按 `side = 'BUY'` 和 `executed_at >= entry_time` 过滤，返回的是最近的一条 BUY 记录。但最近的一条 BUY 记录可能是**条件单（STOP_MARKET/TAKE_PROFIT_MARKET）**，条件单的 `order_id` 字段为 NULL。

**后续逻辑：** 第1008行检查 `if close_order and close_order.get('order_id')`，发现 `order_id` 为 NULL 后跳过回写，不报错但也不写入。

**关键代码路径：**

```
平仓成交 → Binance 自动触发条件单（STOP_MARKET/TAKE_PROFIT_MARKET）成交
         → binance_api.py place_conditional_order() 调用 log_order() 写入 trade_records
         → 条件单的 orderId 为 NULL，写入 trade_records.order_id = NULL
         → _monitor_positions 查询到该条记录，但 order_id 为 NULL
         → 跳过回写（日志：未找到平仓订单记录，无法回写盈亏）
```

**影响：** new_coin 策略虽然有回写逻辑，但因为匹配到了条件单记录（order_id= NULL），导致回写被跳过，`realized_pnl` 仍然为空。

---

### 2.3 问题3：shared/trade_logger.py — update_realized_pnl 只支持 order_id 匹配

**根因描述：**

[`update_realized_pnl`](file:///Users/yl/vscode/Binance_quantitative_trading/shared/trade_logger.py#L221-L272) 方法的 SQL 更新语句为：

```sql
UPDATE trading.trade_records
SET realized_pnl = $1
WHERE order_id = $2 AND strategy = $3 AND side = 'BUY'
```

**依赖条件：** `order_id` 必须不为空，且 `trade_records` 表中必须有匹配的 `order_id`。

**问题：** 条件单（STOP_MARKET、TAKE_PROFIT_MARKET）通过 [`place_conditional_order`](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L528-L696) 下单，该 API 返回的结果中 `orderId` 可能为 NULL（取决于 Binance 条件单响应的结构），导致 `log_order` 写入的 `order_id` 为 NULL。

当 `update_realized_pnl` 无法通过 `order_id` 匹配到记录时，就无法回写盈亏。

---

## 3. 功能需求

### 3.1 F1: 增强 update_realized_pnl 方法（核心）

| 项目 | 内容 |
|------|------|
| 优先级 | P0 - 必须做 |
| 涉及文件 | [shared/trade_logger.py](file:///Users/yl/vscode/Binance_quantitative_trading/shared/trade_logger.py) |
| 估算工时 | 2h |

#### 3.1.1 需求描述

增强 `update_realized_pnl` 方法，使其支持两种匹配模式：

**模式一（主模式）：order_id 匹配**（已有逻辑，保持不变）
- 当 `order_id` 不为空时，优先使用 `WHERE order_id = $2` 匹配
- 精确匹配，无歧义

**模式二（降级匹配）：按 (strategy, symbol, side, executed_at 范围) 匹配**
- 当 `order_id` 为空或匹配不到记录时，启动降级匹配
- 使用 `WHERE strategy = $1 AND symbol = $2 AND side = $3 AND executed_at BETWEEN $4 AND $5` 匹配

#### 3.1.2 方法签名变更

```python
async def update_realized_pnl(
    self,
    order_id: str,
    realized_pnl: Decimal,
    strategy: Optional[str] = None,
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    executed_at: Optional[datetime] = None,
    time_window: int = 300,
) -> bool:
```

**新增参数说明：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `symbol` | Optional[str] | 降级匹配时必填 | 交易对（如 "BTCUSDT"） |
| `side` | Optional[str] | 否 | 平仓方向（BUY/SELL），默认使用 `side = 'BUY'`。传入时动态匹配对应方向，解决做多平仓（SELL方向）无法回写的问题 |
| `executed_at` | Optional[datetime] | 降级匹配时必填 | 平仓成交时间，用于确定时间匹配窗口 |

#### 3.1.3 匹配逻辑（伪代码）

```
# 确定 side 条件：传入时使用传入值，否则默认 'BUY'
match_side = side if side else 'BUY'

if order_id:
    # 模式一：order_id 精确匹配
    UPDATE ... WHERE order_id = $2 AND strategy = $3 AND side = match_side
    if 匹配到记录并更新成功:
        return True

if symbol AND executed_at:
    # 模式二：降级匹配（order_id 为空或匹配失败）
    # 时间窗口：executed_at 前后各 time_window/2 秒（可配置，默认 300 秒）
    time_window = 300  # 秒
    UPDATE trading.trade_records
    SET realized_pnl = $1
    WHERE strategy = $2
      AND symbol = $3
      AND side = match_side    -- 动态 side，支持 BUY 和 SELL
      AND executed_at BETWEEN $4 AND $5
      AND order_id IS NULL
      AND realized_pnl IS NULL
      AND order_type NOT LIKE 'STOP%'  -- 排除条件单本身的记录
    if 匹配到记录并更新成功:
        return True

return False
```

#### 3.1.4 业务规则

| 规则编号 | 规则描述 |
|---------|---------|
| R1 | 模式一优先：只要有 order_id，优先使用精确匹配 |
| R2 | 降级匹配的 UPDATE 必须加 `AND realized_pnl IS NULL`，防止重复回写覆盖 |
| R3 | 降级匹配的 UPDATE 必须加 `AND order_id IS NULL`，防止误更新已有 order_id 的记录 |
| R4 | 降级匹配必须限制 `executed_at` 时间窗口（默认前后 5 分钟），防止跨交易匹配错误 |
| R5 | 降级匹配返回的 `ROW_COUNT` 必须 > 0 才算成功，否则返回 False |
| R5b | 降级匹配必须排除条件单类型记录（`order_type NOT LIKE 'STOP%'`），避免误更新条件单记录 |
| R5c | `side` 参数传入时动态匹配对应方向，不传入则默认 `side = 'BUY'` |

#### 3.1.5 数据流

```
策略层调用 update_realized_pnl(order_id, pnl, side=close_side, symbol=symbol, executed_at=executed_at)
    │
    ├─ 确定 match_side = side if side else 'BUY'
    │
    ├─ order_id 不为空 → 模式一：WHERE order_id = $2 AND side = match_side
    │   ├─ 匹配成功 → UPDATE realized_pnl → 返回 True
    │   └─ 匹配失败 → 尝试模式二
    │
    └─ order_id 为空 → 跳过模式一，直尝试模式二
        │
        └─ symbol 和 executed_at 均不为空 → 模式二：降级匹配
            ├─ 匹配到记录 → UPDATE realized_pnl (side = match_side) → 返回 True
            └─ 匹配不到记录 → 返回 False
```

---

### 3.2 F2: btc_eth 策略平仓后回写 realized_pnl

| 项目 | 内容 |
|------|------|
| 优先级 | P0 - 必须做 |
| 涉及文件 | [strategy.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py) |
| 估算工时 | 3h |

#### 3.2.1 需求描述

在 `_close_position` 方法平仓成功后，增加调用 `update_realized_pnl` 的逻辑。

#### 3.2.2 修改位置

在 `_close_position` 方法中，第2696行 `position.current_quantity -= actual_close_quantity` 之后，第2729行（首次完全平仓后异步取消残余条件单）之前，插入盈亏回写逻辑。

#### 3.2.3 具体逻辑

```python
# 计算平仓盈亏并回写
try:
    # 计算成交均价
    exit_price = Decimal(str(order_result.get('avgPrice', '0')))
    if exit_price <= 0:
        exit_price = current_price or Decimal('0')
    
    if exit_price > 0 and position.entry_price:
        if position.direction == 'LONG':
            pnl = (exit_price - position.entry_price) * actual_close_quantity
        else:  # SHORT
            pnl = (position.entry_price - exit_price) * actual_close_quantity
        
        # 获取 trade_logger 实例
        trade_logger = getattr(self.binance, 'trade_logger', None)
        if trade_logger:
            close_side = "SELL" if position.direction == "LONG" else "BUY"
            await trade_logger.update_realized_pnl(
                order_id=str(order_result.get('orderId', '')),
                realized_pnl=pnl,
                side=close_side,  # 传入平仓方向，支持做多(SELL)和做空(BUY)
                symbol=symbol,
                executed_at=datetime.now()
            )
except Exception as e:
    logger.warning(f"回写平仓盈亏失败", error=str(e))
```

#### 3.2.4 业务规则

| 规则编号 | 规则描述 |
|---------|---------|
| R6 | 回写盈亏失败不影响平仓主流程（异常被内部捕获，仅记日志） |
| R7 | 平仓盈亏计算公式：LONG 为 `(exit_price - entry_price) * quantity`，SHORT 为 `(entry_price - exit_price) * quantity` |
| R8 | 如果 `order_result` 中 `avgPrice` 为 0 或空，回退到 `current_price` 参数计算 |
| R9 | 如果 `entry_price` 或 `exit_price` 任一为 0，跳过盈亏回写 |
| R10 | 通过 `getattr(self.binance, 'trade_logger', None)` 获取 trade_logger，避免直接依赖 |

#### 3.2.5 数据流

```
_close_position 执行限价平仓
    │
    ├─ 重试循环中确认成交 (filled = True)
    │
    ├─ 更新 position.current_quantity
    │
    ├─ [新增] 计算平仓盈亏
    │   ├─ 获取 exit_price = order_result.avgPrice
    │   ├─ 获取 entry_price = position.entry_price
    │   └─ 按方向公式计算 pnl
    │
    ├─ [新增] 调用 update_realized_pnl(order_id, pnl, symbol, executed_at)
    │   ├─ 成功 → 日志记录
    │   └─ 失败 → 仅记警告日志，不阻断流程
    │
    ├─ 发送平仓通知
    │
    └─ 首次完全平仓后异步取消残余条件单
```

---

### 3.3 F3: new_coin 策略修复平仓单查询逻辑

| 项目 | 内容 |
|------|------|
| 优先级 | P0 - 必须做 |
| 涉及文件 | [new_coin/strategy.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/strategy.py) |
| 估算工时 | 2h |

#### 3.3.1 需求描述

修改 `_monitor_positions` 方法中查询平仓单 order_id 的 SQL，确保能正确匹配到 LIMIT/MARKET 平仓单，而不是条件单记录。

#### 3.3.2 修改位置

第994-1007行，查询 close_order 的 SQL 语句。

#### 3.3.3 具体方案

**方案A（推荐）：过滤条件单记录，且要求 order_id 不为空**

```sql
SELECT order_id
FROM trading.trade_records
WHERE strategy = $1 AND symbol = $2 AND side = 'BUY'
  AND executed_at >= $3
  AND order_id IS NOT NULL
  AND order_type NOT IN ('STOP_MARKET', 'TAKE_PROFIT_MARKET', 'STOP', 'TAKE_PROFIT')
ORDER BY executed_at DESC
LIMIT 1
```

**优点：** 精确匹配，不会误匹配到条件单记录。
**缺点：** 如果平仓单本身也是通过条件单执行的（如 TP/SL），则仍然查不到。但此时配合 F1 的降级匹配可以兜底。

**方案B（备选）：直接使用 order_id 不为空过滤**

```sql
SELECT order_id
FROM trading.trade_records
WHERE strategy = $1 AND symbol = $2 AND side = 'BUY'
  AND executed_at >= $3
  AND order_id IS NOT NULL
ORDER BY executed_at DESC
LIMIT 1
```

**优点：** 简单直接，只要 trade_records 中的平仓单有 order_id 即可匹配。
**缺点：** 如果平仓方向本身就是 BUY 的条件单，仍然匹配不到。

**推荐方案A**，因为方案B只是过滤了 NULL，但可能仍然匹配到有 order_id 的 STOP/TAKE_PROFIT 条件单记录（虽然条件单的 order_id 通常为 NULL，但某些情况下 Binance 可能返回 orderId）。

#### 3.3.4 业务规则

| 规则编号 | 规则描述 |
|---------|---------|
| R11 | 查询条件增加 `order_id IS NOT NULL`，排除无条件单 ID 的记录 |
| R12 | 查询条件增加 `order_type NOT IN` 过滤，排除条件单类型的记录 |
| R13 | 如果仍然匹配不到 order_id，配合 F1 的降级匹配模式兜底 |

---

### 3.4 F4: 历史数据回写（可选但建议）

| 项目 | 内容 |
|------|------|
| 优先级 | P1 - 待实现（当前版本未包含） |
| 涉及文件 | 新增脚本 |
| 估算工时 | 4h |

#### 3.4.1 需求描述

提供一个独立的**一次性回写脚本**，遍历 `trade_records` 表中 `realized_pnl IS NULL` 的记录，根据策略的持仓记录计算已实现盈亏并回写。

#### 3.4.2 回写逻辑

对于 `trade_records` 中 `side = 'BUY'` 且 `realized_pnl IS NULL` 的记录：

1. 找到该策略、该交易对最近的一条 `side = 'SELL'` 记录作为开仓记录
2. 计算盈亏 = `(exit_price - entry_price) * quantity`（LONG）或 `(entry_price - exit_price) * quantity`（SHORT）
3. 如果方向无法确定，默认按 LONG 处理
4. 回写到 `realized_pnl` 字段

#### 3.4.3 业务规则

| 规则编号 | 规则描述 |
|---------|---------|
| R14 | 回写脚本必须是**幂等的**：可重复执行，不会重复累加盈亏 |
| R15 | 回写脚本必须在**本地执行**，不得在服务器上直接运行 |
| R16 | 回写结果必须输出日志，记录每笔回写的详细信息（策略、交易对、盈亏金额、时间） |
| R17 | 回写前必须备份 `trade_records` 表数据 |

---

## 4. 非功能需求

### 4.1 性能要求

| 需求编号 | 描述 | 验收标准 |
|---------|------|---------|
| NFR1 | update_realized_pnl 执行时间不超过 100ms | 99% 的调用在 100ms 内完成 |
| NFR2 | 回写逻辑不影响平仓主流程的执行时间 | 平仓总耗时增加不超过 200ms |
| NFR3 | 历史数据回写脚本支持分批处理，不锁表 | 支持 `--batch-size` 参数控制每批处理数量 |

### 4.2 安全性要求

| 需求编号 | 描述 | 验收标准 |
|---------|------|---------|
| NFR4 | 盈亏回写失败不阻断主交易流程 | 所有异常被内部捕获，仅记录日志 |
| NFR5 | 降级匹配 UPDATE 必须加 `realized_pnl IS NULL` 条件 | 防止重复回写导致数据错误 |
| NFR6 | 降级匹配 UPDATE 必须加 `order_id IS NULL` 条件 | 防止误更新已有 order_id 的记录 |

### 4.3 可维护性要求

| 需求编号 | 描述 | 验收标准 |
|---------|------|---------|
| NFR7 | 降级匹配的时间窗口可配置 | 通过 `config.yaml` 或方法参数配置 |
| NFR8 | 盈亏计算公式集中管理 | 避免在不同策略中重复实现相同公式 |
| NFR9 | 回写日志必须包含足够信息用于排查 | 日志包含：策略、交易对、order_id、盈亏金额、匹配模式 |

---

## 5. 验收标准

### 5.1 F1: update_realized_pnl 增强

| 验收编号 | 验收条件 | 测试方法 |
|---------|---------|---------|
| AC1-1 | 传入 order_id 且不为空时，使用模式一（order_id 精确匹配）更新 | 单元测试：mock db.execute，验证 SQL 中包含 `WHERE order_id = $2` |
| AC1-2 | 传入 order_id 为空但 symbol 和 executed_at 不为空时，使用模式二（降级匹配）更新 | 单元测试：mock db.execute，验证 SQL 中包含 `WHERE strategy = $1 AND symbol = $2 AND executed_at BETWEEN` |
| AC1-3 | 降级匹配的 UPDATE 包含 `AND realized_pnl IS NULL` | 单元测试：验证 SQL 中包含该条件 |
| AC1-4 | 降级匹配的 UPDATE 包含 `AND order_id IS NULL` | 单元测试：验证 SQL 中包含该条件 |
| AC1-5 | 模式一匹配成功时，不执行模式二 | 集成测试：mock db.execute 返回成功，验证只调用一次 |
| AC1-6 | 模式一匹配失败时，自动降级到模式二 | 集成测试：第一次 mock 返回 0 行，第二次返回 1 行 |

### 5.2 F2: btc_eth 策略回写

| 验收编号 | 验收条件 | 测试方法 |
|---------|---------|---------|
| AC2-1 | 平仓成功后，`update_realized_pnl` 被调用 | 单元测试：mock trade_logger，验证 `update_realized_pnl` 被调用 |
| AC2-2 | 传入的盈亏金额计算正确（LONG 方向） | 单元测试：entry_price=100, exit_price=110, quantity=1 → pnl=10 |
| AC2-3 | 传入的盈亏金额计算正确（SHORT 方向） | 单元测试：entry_price=110, exit_price=100, quantity=1 → pnl=10 |
| AC2-4 | `update_realized_pnl` 调用失败时，平仓流程不中断 | 集成测试：mock 抛出异常，验证平仓流程继续执行完毕 |
| AC2-5 | 传入的 order_id、symbol、executed_at 参数正确 | 单元测试：验证调用参数与 order_result 一致 |

### 5.3 F3: new_coin 策略修复

| 验收编号 | 验收条件 | 测试方法 |
|---------|---------|---------|
| AC3-1 | 查询 SQL 中增加 `order_id IS NOT NULL` 条件 | 单元测试：验证 SQL 内容 |
| AC3-2 | 查询 SQL 中增加 `order_type NOT IN` 过滤条件单 | 单元测试：验证 SQL 内容 |
| AC3-3 | 存在多笔 BUY 记录时，能正确匹配到 LIMIT/MARKET 平仓单 | 集成测试：插入多条记录，验证返回正确的 order_id |
| AC3-4 | 匹配不到 order_id 时，不报错，仅记录警告日志 | 单元测试：mock 返回空结果，验证不抛出异常 |

### 5.4 F4: 历史数据回写（可选）

| 验收编号 | 验收条件 | 测试方法 |
|---------|---------|---------|
| AC4-1 | 回写脚本执行后，所有已平仓记录的 realized_pnl 不为 NULL | 执行脚本后查询 `SELECT COUNT(*) FROM trade_records WHERE realized_pnl IS NULL AND ...` |
| AC4-2 | 回写脚本可重复执行，不会重复累加 | 第一次执行后有 realized_pnl，第二次执行后值不变 |
| AC4-3 | 脚本执行前自动备份数据 | 验证备份文件生成 |

---

## 6. 依赖关系

### 6.1 执行顺序

```
F1 (增强 update_realized_pnl) → 必须先完成，F2 和 F3 依赖它
    │
    ├─ F2 (btc_eth 策略回写) → 依赖 F1 的新签名
    │
    └─ F3 (new_coin 策略修复) → 依赖 F1 的降级匹配兜底
    │
    └─ F4 (历史数据回写) → 依赖 F1、F2、F3 完成后执行
```

### 6.2 风险说明

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|---------|
| 降级匹配误更新到其他交易的记录 | 低 | 高 | 时间窗口限制 + realized_pnl IS NULL + order_id IS NULL 三重防护 |
| avgPrice 返回 0 导致盈亏计算错误 | 中 | 中 | 回退到 current_price 参数，若仍为 0 则跳过回写 |
| 历史数据回写脚本计算方向错误 | 中 | 中 | 优先从 trade_records 的 side 判断方向，side='BUY'=平多，side='SELL'=平空 |

---

## 7. 术语表

| 术语 | 说明 |
|------|------|
| realized_pnl | 已实现盈亏，单位为 USDT，正值为盈利，负值为亏损 |
| trade_records | 统一交易记录表，位于 `trading` schema 下 |
| 条件单 | STOP_MARKET、TAKE_PROFIT_MARKET 等类型的订单，由 Binance 条件触发执行 |
| 限价单 | LIMIT 类型的订单，指定价格挂单等待成交 |
| 降级匹配 | 当无法通过 order_id 精确匹配时，使用策略+交易对+时间范围等条件匹配 |
| PnLCollector | AI 调优系统中的盈亏数据采集器，从 trade_records 读取 realized_pnl |

---

## 8. 附录

### 8.1 相关文件清单

| 文件 | 用途 |
|------|------|
| [shared/trade_logger.py](file:///Users/yl/vscode/Binance_quantitative_trading/shared/trade_logger.py) | 核心修改：增强 update_realized_pnl 方法 |
| [strategies/btc_eth/strategy.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py) | 修改：_close_position 增加盈亏回写 |
| [strategies/new_coin/strategy.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/strategy.py) | 修改：_monitor_positions 平仓单查询逻辑 |
| [strategies/btc_eth/main.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/main.py) | 初始化 TradeLogger，无需修改 |
| [strategies/new_coin/main.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/main.py) | 初始化 TradeLogger，无需修改 |
| [shared/binance_api.py](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py) | 自动调用 log_order，无需修改 |
| [ai_tuner/allocation/pnl_collector.py](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/allocation/pnl_collector.py) | 消费者，修复后自动受益，无需修改 |

### 8.2 现有单元测试文件

[test_realized_pnl_fix.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/test_realized_pnl_fix.py) 已有部分测试覆盖，修复后需补充：
- 降级匹配逻辑的单元测试
- btc_eth 策略回写逻辑的单元测试
- 历史数据回写脚本的单元测试