# HRS 策略 PnL 回写修复需求文档

## 文档信息

| 字段 | 内容 |
|------|------|
| 文档版本 | v1.0 |
| 创建日期 | 2026-08-10 |
| 作者 | 需求文档专家 |
| 状态 | 草稿 |

## 修订历史

| 版本 | 日期 | 修改内容 | 修改人 |
|------|------|---------|--------|
| v1.0 | 2026-08-10 | 初稿 | 需求文档专家 |

---

## 1. 产品概述

### 1.1 问题背景

HRS 策略的 `trade_records` 表中，有 3 个币种（BEATUSDT、AAVEUSDT、CLUSDT）的 PnL 记录与 Binance API 实际数据不一致。每个币种均缺少一条 PnL 记录，缺失金额分别为 -0.632、-0.145、-0.0576 USDT。

### 1.2 业务目标

修复 HRS 策略的 PnL 回写逻辑，确保 `trade_records` 表中的 `realized_pnl` 字段与 Binance 交易所的实际已实现盈亏完全一致，不再出现遗漏记录的情况。

### 1.3 成功指标

1. 修复后，所有通过条件单（STOP/TAKE_PROFIT）平仓的交易的 PnL 都能正确写入 `trade_records`
2. 对于已存在缺失记录的 3 个币种，修复后不再产生新的缺失记录
3. 修复方案不影响主交易流程（写入失败不阻断交易）
4. 不重复写入已存在的 PnL 记录

---

## 2. 根因分析

### 2.1 问题根源

HRS 策略使用条件单（STOP/TAKE_PROFIT）来执行止盈止损。当条件单被触发时，币安内部会生成市价单来平仓，这些市价单可能被拆成**多笔成交**。

### 2.2 当前逻辑缺陷

当前 `_writeback_pnl_for_full_close` 方法（[strategy.py:1465-1553](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L1465-L1553)）存在两个缺陷：

**缺陷一：使用理论值而非实际值**

该方法根据策略参数（TP1/TP2 百分比、ATR 倍数）计算**理论 PnL**：

```python
# TP1 退出价 = 入场价 - ATR * 1.5（做空）
tp1_price = entry_price - atr * t1_mult
# 计算理论 PnL
pnl = TradeLogger.calculate_pnl(direction, entry_price, tp1_price, tp1_qty)
```

但实际条件单触发时，市价单的成交价可能与理论价存在偏差（滑点、流动性等因素），导致理论 PnL 与实际 PnL 不一致。

**缺陷二：3 次调用 update_realized_pnl 只有第 1 次生效**

该方法分别计算 TP1、TP2、剩余部分 3 个 PnL，每个都调用 `update_realized_pnl`：

```python
# TP1 写入
await trade_logger.update_realized_pnl(order_id="", realized_pnl=pnl, ...)
# TP2 写入
await trade_logger.update_realized_pnl(order_id="", realized_pnl=pnl, ...)
# 剩余部分写入
await trade_logger.update_realized_pnl(order_id="", realized_pnl=pnl, ...)
```

由于 `order_id=""`，全部走降级匹配模式。降级匹配的 SQL 条件是 `realized_pnl IS NULL` 且 `LIMIT 1`，这意味着**只有第 1 条记录被更新**，后面 2 条 PnL 根本写不进去。

### 2.3 影响范围

| 场景 | 影响 |
|------|------|
| 条件单全部平仓（TP1+TP2+剩余） | TP1 的部分 PnL 可能写入，TP2 和剩余部分的 PnL 丢失 |
| 条件单全部平仓（仅 TP1 或仅 TP2） | 部分 PnL 丢失 |
| 时间止损平仓 | 使用 `_writeback_pnl` 方法（有 `order_id`），不受影响 |
| 移动止盈平仓 | 同上，不受影响 |

---

## 3. 功能需求

### 3.1 功能列表

| 编号 | 功能名称 | 优先级 | MoSCoW |
|------|---------|--------|--------|
| F-01 | 从 Binance API 获取实际 PnL 代替理论计算 | 高 | Must |
| F-02 | 合并多次 PnL 为一次写入 | 高 | Must |
| F-03 | 避免重复写入已有 PnL 记录 | 高 | Must |
| F-04 | 异常降级处理（API 调用失败时回退到理论计算） | 中 | Should |
| F-05 | API 调用频率限制保护 | 中 | Should |
| F-06 | 日志记录增强 | 低 | Could |

### 3.2 功能详情

#### F-01：从 Binance API 获取实际 PnL

**说明：** 修改 `_writeback_pnl_for_full_close` 方法，改为调用 `binance_client.get_income_history` 获取该交易期间的实际 REALIZED_PNL 数据。

**输入参数：**
```
symbol: str          - 交易对（如 "BEATUSDT"）
direction: str       - 方向 ('short'/'long')
entry_price: float   - 入场价格（保留，用于降级回退）
entry_quantity: float - 入场数量（保留，用于降级回退）
atr: float           - ATR 值（保留，用于降级回退）
current_price: float - 当前价格（保留，用于降级回退）
pos: Dict[str, Any]  - 持仓数据（包含 entry_time 等）
```

**业务流程：**
1. 从 `pos` 中获取 `entry_time`（UTC datetime），转换为毫秒时间戳作为 `start_time`
2. 以当前时间（毫秒时间戳）作为 `end_time`
3. 调用 `binance_client.get_income_history(start_time=entry_ts, end_time=now_ts, income_type="REALIZED_PNL")`
4. 从返回结果中过滤出 `symbol` 匹配的记录
5. 将所有 `income` 值求和（使用 Decimal 精度计算）
6. 如果求和结果不为 0，调用 `trade_logger.update_realized_pnl` 写入

**关键代码路径：**
- 调用方：[strategy.py:1589-1597](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L1589-L1597) — `_monitor_positions` 中 `tp_result == 0` 分支
- 被调用 API：[shared/binance_api.py:1108-1149](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L1108-L1149) — `get_income_history`
- 写入方法：[shared/trade_logger.py:220-357](file:///Users/yl/vscode/Binance_quantitative_trading/shared/trade_logger.py#L220-L357) — `update_realized_pnl`

**`get_income_history` API 返回格式示例：**
```json
[
  {"symbol": "BEATUSDT", "incomeType": "REALIZED_PNL", "income": "-0.632", "time": 1690000000000},
  {"symbol": "AAVEUSDT", "incomeType": "REALIZED_PNL", "income": "-0.145", "time": 1690000001000},
  ...
]
```

#### F-02：合并多次 PnL 为一次写入

**说明：** 将原来的 3 次调用（TP1、TP2、剩余部分）合并为**1 次调用**，写入汇总后的总 PnL。

**替换逻辑：**
```
# 旧逻辑：3 次调用，每次写入部分 PnL（实际只有第 1 次生效）
→ 调用 update_realized_pnl (TP1 PnL)
→ 调用 update_realized_pnl (TP2 PnL)  ← 不生效
→ 调用 update_realized_pnl (剩余 PnL)  ← 不生效

# 新逻辑：1 次调用，写入从 Binance API 获取的实际总 PnL
→ 从 Binance API 获取该时间段内所有 REALIZED_PNL
→ 求和得到总 PnL
→ 调用 update_realized_pnl (总 PnL)  ← 1 次成功
```

#### F-03：避免重复写入

**说明：** 确保同一笔交易的 PnL 不会被重复写入。

**实现方式：**
- `update_realized_pnl` 的降级匹配模式已经包含 `realized_pnl IS NULL` 条件，天然具有去重能力
- 同一笔交易的 `trade_records` 记录只有一条 `realized_pnl IS NULL`，第一次写入后，后续调用不会重复更新
- 不需要额外去重逻辑，复用现有机制即可

#### F-04：异常降级处理

**说明：** 当 Binance API 调用失败（网络异常、账户非 PM 模式等）时，回退到原有的理论计算逻辑，确保 PnL 回写不会完全失效。

**降级策略：**
```python
try:
    # 1. 尝试从 Binance API 获取实际 PnL
    income_records = await self.binance_client.get_income_history(...)
    if income_records:
        # 过滤当前 symbol，求和
        total_pnl = sum(Decimal(r["income"]) for r in income_records if r["symbol"] == symbol)
        # 写入
        await trade_logger.update_realized_pnl(...)
        return
except Exception:
    logger.warning("API获取实际PnL失败，降级到理论计算", ...)

# 2. 降级：使用原有的理论计算逻辑（保留现有代码）
# ... 原有的 TP1/TP2/剩余部分计算逻辑 ...
```

#### F-05：API 调用频率限制保护

**说明：** 避免每个监控周期都调用 `get_income_history`。

**实现方式：**
- 仅在 `_writeback_pnl_for_full_close` 被调用时（即 `tp_result == 0` 时）才调用 API
- 每个持仓仅在全部平仓时调用一次，不额外增加频率
- 如果担心短时间内多次平仓，可添加一个轻量级缓存（如 `dict` 记录 `{symbol: last_query_time}`），但考虑到 `tp_result == 0` 每个持仓只触发一次，可以不额外加缓存

#### F-06：日志记录增强

**说明：** 增加清晰的日志记录，方便后续排查 PnL 回写问题。

**日志输出要求：**
```
# 成功从 API 获取实际 PnL
"从Binance API获取实际PnL", symbol=xxx, 
  income_records_count=N, total_pnl=xxx.xx, 
  time_range=[entry_time → now]

# 降级到理论计算
"API获取实际PnL失败，降级到理论计算", symbol=xxx, error=xxx

# API 返回空结果
"Binance API未返回任何PnL记录", symbol=xxx, time_range=[...]

# PnL 写入成功
"实际PnL回写成功", symbol=xxx, total_pnl=xxx.xx
```

---

## 4. 交互设计

### 4.1 方法调用流程

```
_monitor_positions()
  └── detect_take_profit_fills() → 返回 0（全部平仓）
       └── _writeback_pnl_for_full_close()
            ├── [新] 从 pos 获取 entry_time
            ├── [新] 调用 binance_client.get_income_history()
            ├── [新] 过滤 symbol，求和
            ├── [新] 调用 trade_logger.update_realized_pnl() 写入总 PnL
            │     └── 成功：记录日志
            │     └── 失败（异常）：降级到理论计算
            └── [旧] 降级路径：原有的 TP1/TP2/剩余部分计算
```

### 4.2 异常处理流程

| 异常场景 | 处理方式 | 影响 |
|---------|---------|------|
| `get_income_history` 返回空列表 | 降级到理论计算 | 无数据丢失 |
| `get_income_history` 抛出异常 | 降级到理论计算 | 无数据丢失 |
| 账户非 PM 模式（API 不可用） | 降级到理论计算（已有降级逻辑） | 无数据丢失 |
| `update_realized_pnl` 写入失败 | 异常被内部捕获，不影响主流程 | 该笔 PnL 丢失 |
| 求和 Decimal 转换异常 | 异常被捕获，降级到理论计算 | 无数据丢失 |

---

## 5. 非功能需求

### 5.1 性能要求

| 编号 | 要求 | 指标 |
|------|------|------|
| NF-01 | API 调用频率 | 每个持仓仅在全部平仓时调用 1 次 `get_income_history` |
| NF-02 | 响应时间 | PnL 回写逻辑不应阻塞主监控循环超过 2 秒 |

### 5.2 安全性要求

| 编号 | 要求 | 说明 |
|------|------|------|
| NF-03 | 异常安全 | 所有外部调用必须包裹在 try/except 中，异常不能传播到主流程 |
| NF-04 | 精度安全 | PnL 计算必须使用 Decimal 类型，避免浮点数精度问题 |

### 5.3 兼容性要求

| 编号 | 要求 | 说明 |
|------|------|------|
| NF-05 | 数据库兼容 | 写入的 `trade_records` 表结构不变，仅更新 `realized_pnl` 字段 |
| NF-06 | 账户兼容 | `get_income_history` 仅 PM 账户可用，非 PM 账户自动降级 |

---

## 6. 验收标准

### 6.1 功能验收

| 编号 | 验收条件 | 验证方式 |
|------|---------|---------|
| AC-01 | 条件单全部平仓后，`trade_records.realized_pnl` 与 Binance API 的实际 REALIZED_PNL 一致 | 对比数据库记录与 API 返回数据 |
| AC-02 | 不存在 PnL 记录缺失的情况（TP1/TP2/剩余部分均被覆盖） | 检查 3 个问题币种的新交易记录 |
| AC-03 | 同一笔交易的 PnL 不会被重复写入 | 多次触发平仓场景，验证 `realized_pnl` 只更新一次 |
| AC-04 | API 调用失败时，降级到理论计算，PnL 回写不中断 | 模拟 API 异常，验证降级路径执行 |
| AC-05 | PnL 回写失败不影响主交易流程（策略继续运行） | 模拟写入异常，验证策略不崩溃 |

### 6.2 回归验证

| 编号 | 验证内容 | 说明 |
|------|---------|------|
| RV-01 | 时间止损平仓的 PnL 回写不受影响 | 验证 `_writeback_pnl` 方法未被修改 |
| RV-02 | 移动止盈平仓的 PnL 回写不受影响 | 同上 |
| RV-03 | 存量数据的 `realized_pnl` 不会被修改 | 验证 `realized_pnl IS NULL` 条件生效 |

### 6.3 测试要点

**测试用例 1：API 正常返回**
1. 模拟一个持仓，`entry_time` 为 1 小时前
2. 模拟 `get_income_history` 返回 3 条 REALIZED_PNL 记录（对应 TP1、TP2、止损）
3. 验证 `update_realized_pnl` 被调用 1 次，且参数为 3 条记录的和
4. PnL 精度：使用 Decimal 计算，与手算结果一致

**测试用例 2：API 返回空列表**
1. 模拟 `get_income_history` 返回空列表
2. 验证降级到理论计算逻辑
3. 验证 `update_realized_pnl` 被调用且参数为理论 PnL

**测试用例 3：API 抛出异常**
1. 模拟 `get_income_history` 抛出网络异常
2. 验证异常被捕获，日志记录
3. 验证降级到理论计算逻辑

**测试用例 4：非 PM 账户**
1. 模拟 `use_unified_account` 为 False
2. 验证 `get_income_history` 返回空列表
3. 验证降级到理论计算逻辑

**测试用例 5：重复写入防护**
1. 第一次调用 `_writeback_pnl_for_full_close`，PnL 写入成功
2. 第二次调用 `_writeback_pnl_for_full_close`（同一笔交易）
3. 验证 `update_realized_pnl` 返回 False（`realized_pnl IS NULL` 条件不满足）
4. 验证 `trade_records` 中该记录只有一条 PnL 值

---

## 7. 修改范围

### 7.1 需修改的文件

| 文件 | 修改内容 | 预计改动量 |
|------|---------|-----------|
| [strategies/hrs/strategy.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L1465-L1553) | 重写 `_writeback_pnl_for_full_close` 方法，新增 `_get_actual_pnl_from_binance`（[1301-1358](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L1301-L1358)）和 `_calculate_theoretical_total_pnl`（[1360-1463](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L1360-L1463)）辅助方法，改为从 Binance API 获取实际 PnL | 约 200 行 |
| [strategies/hrs/tests/](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/tests/) | 新增测试用例（可选） | 约 100 行 |

### 7.2 无需修改的文件

| 文件 | 原因 |
|------|------|
| [shared/binance_api.py](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py) | `get_income_history` 方法已存在，无需修改 |
| [shared/trade_logger.py](file:///Users/yl/vscode/Binance_quantitative_trading/shared/trade_logger.py) | `update_realized_pnl` 方法已满足需求，无需修改 |
| [strategies/hrs/position_manager.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py) | 不涉及持仓管理逻辑修改 |
| 数据库表结构 | 无需 DDL 变更 |

---

## 8. 依赖与风险

### 8.1 依赖关系

| 依赖项 | 说明 | 风险等级 |
|--------|------|---------|
| `get_income_history` API | 仅在 PM（统一账户）模式下可用 | 中 |
| `pos` 中的 `entry_time` 字段 | 必须准确记录入场时间，否则时间窗口不正确 | 中 |
| Binance API 可用性 | 网络波动可能导致 API 调用失败 | 低 |

### 8.2 风险说明

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|---------|
| `entry_time` 不准确 | 低 | 时间窗口偏移，API 返回不完整 PnL | 降级到理论计算 |
| 同一持仓在短时间内多次平仓（加仓场景） | 低 | 多个平仓的 PnL 混在一起 | 每个平仓事件独立查询，时间窗口以当前 `entry_time` 到平仓时间为界 |
| `get_income_history` 返回的 PnL 包含其他 v 策略的同一币种记录 | 低 | 跨策略 PnL 混淆 | HRS 策略独占交易对，不与其他策略共享（业务约定） |
| API 限频 | 低 | 短时间内大量平仓触发限频 | 每个持仓仅调用一次，不会触发限频 |

---

## 9. 后续计划

### 9.1 遗留问题

| 问题 | 说明 | 计划 |
|------|------|------|
| 存量数据修复 | 3 个问题币种已有的缺失 PnL 记录 | 本次修复仅防止新缺失，存量数据单独通过脚本修复 |
| 非 PM 账户兼容 | 非 PM 账户无法使用 `get_income_history`，始终使用理论计算 | 当前降级策略已覆盖，无需额外处理 |

### 9.2 监控建议

部署后持续监控以下指标：
1. `trade_records` 中 `realized_pnl` 为空的比例
2. 降级到理论计算的频率
3. `get_income_history` API 调用成功率