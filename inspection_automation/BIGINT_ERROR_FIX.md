# PostgreSQL BigInt Out of Range 错误修复报告

## 问题描述

在 PostgreSQL 数据库日志中发现 3846 个错误：
```
bigint out of range
```

错误时间：2026-04-08 15:28:42 至 15:28:49（短时间内大量发生）

## 问题原因分析

### 1. 数据类型不匹配

**根本原因**：数据库表 `trades` 中的 `update_time` 和 `create_time` 字段类型被定义为 `INTEGER`（4 字节），但代码中存储的是**毫秒级时间戳**（8 字节 BIGINT 范围）。

**数据范围对比**：
- PostgreSQL `INTEGER`（int4）范围：**-2,147,483,648 到 2,147,483,647**（约±21 亿）
- PostgreSQL `BIGINT`（int8）范围：**-9,223,372,036,854,775,808 到 9,223,372,036,854,775,807**（约±922 京）
- 当前毫秒时间戳：**1,743,526,122,300**（约 1.7 万亿）

**结论**：毫秒时间戳（1.7 万亿）远超 INTEGER 最大值（21 亿），导致溢出错误。

### 2. 代码问题定位

在 `models/database.py` 中：

```python
# 第 201-202 行
order_data.get('updateTime', int(datetime.now().timestamp() * 1000)),
order_data.get('updateTime', int(datetime.now().timestamp() * 1000)),
```

```python
# 第 213 行 - update_trade_status 方法
params = [status, int(datetime.now().timestamp() * 1000)]
```

这些代码生成的毫秒时间戳约为 1.7 万亿，当存入 INTEGER 字段时发生溢出。

### 3. 受影响的表和字段

**主要 affected 表**：
- `trades.create_time` (INTEGER → 应为 BIGINT)
- `trades.update_time` (INTEGER → 应为 BIGINT)
- `trades.transaction_id` (INTEGER → 应为 BIGINT)

**其他可能受影响的表**：
- `positions.last_update_time`
- `account_transfers.create_time`
- `closed_positions.open_time`, `close_time`
- `tp_sl_triggers.trigger_time`

## 解决方案

### 方案 1：修改数据库表结构（推荐）

将时间戳字段的类型从 INTEGER 改为 BIGINT：

```sql
-- 修复 trades 表
ALTER TABLE schema_bianace.trades 
    ALTER COLUMN create_time TYPE BIGINT;

ALTER TABLE schema_bianace.trades 
    ALTER COLUMN update_time TYPE BIGINT;

-- 修复其他表的时间字段
ALTER TABLE schema_bianace.positions 
    ALTER COLUMN last_update_time TYPE BIGINT;

ALTER TABLE schema_bianace.account_transfers 
    ALTER COLUMN create_time TYPE BIGINT;

-- 以此类推...
```

**执行修复脚本**：
```bash
cd /Users/yl/vscode/bianace_btcethbnb_trade
python fix_bigint_error.py
```

### 方案 2：修改代码使用时间戳秒数（不推荐）

如果坚持使用 INTEGER 类型，需要将代码中的毫秒时间戳改为秒级：

```python
# 修改前（毫秒）
int(datetime.now().timestamp() * 1000)

# 修改后（秒）
int(datetime.now().timestamp())
```

**缺点**：
- 精度降低（丢失毫秒精度）
- 需要修改大量代码
- 币安 API 返回的 updateTime 本身就是毫秒，仍需转换

### 方案 3：使用 TIMESTAMP 类型（最佳实践）

PostgreSQL 的 `TIMESTAMP` 类型更适合存储时间：

```sql
-- 将时间戳字段改为 TIMESTAMP
ALTER TABLE schema_bianace.trades 
    ALTER COLUMN create_time TYPE TIMESTAMP 
    USING to_timestamp(create_time / 1000.0);

ALTER TABLE schema_bianace.trades 
    ALTER COLUMN update_time TYPE TIMESTAMP 
    USING to_timestamp(update_time / 1000.0);
```

**优点**：
- 更符合 SQL 标准
- 支持日期时间运算
- 可读性更好

**缺点**：
- 需要数据转换
- 代码改动较大

## 推荐执行步骤

### 第一步：诊断当前状态

```bash
cd /Users/yl/vscode/bianace_btcethbnb_trade
python diagnose_bigint_error.py
```

### 第二步：执行修复

```bash
python fix_bigint_error.py
```

### 第三步：验证修复

```bash
# 再次运行诊断脚本
python diagnose_bigint_error.py

# 检查数据库日志
docker logs postgres-db | grep "bigint out of range"
```

### 第四步：监控

修复后持续监控数据库日志，确保错误不再出现。

## 预防措施

### 1. 数据库设计规范

- 时间戳字段统一使用 `BIGINT` 或 `TIMESTAMP`
- 金额字段使用 `DECIMAL/NUMERIC`
- ID 字段使用 `BIGINT`（避免订单 ID 溢出）

### 2. 代码审查要点

- 检查所有时间戳相关代码
- 确保毫秒时间戳存入 BIGINT 字段
- 避免混用秒级和毫秒级时间戳

### 3. 数据迁移注意

从 SQLite 迁移到 PostgreSQL 时：
- SQLite 的 `INTEGER` 是动态类型，可能存储 64 位值
- PostgreSQL 的 `INTEGER` 严格是 32 位
- 迁移时需仔细检查字段类型映射

## 相关文件

- 修复脚本：`fix_bigint_error.py`
- 诊断脚本：`diagnose_bigint_error.py`
- 数据库模型：`models/database.py`

## 总结

**问题**：INTEGER 类型无法存储毫秒时间戳（1.7 万亿 > 21 亿）

**解决**：将时间字段改为 BIGINT 类型（支持 922 京）

**影响**：修复后不再出现 bigint out of range 错误

---

生成时间：2026-04-09
修复负责人：[待填写]
修复完成时间：[待填写]
