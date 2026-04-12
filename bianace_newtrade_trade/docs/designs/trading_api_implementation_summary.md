# 币安交易 API 实现总结

## 概述

本文档总结了为币安新币做空系统实现的完整交易 API 功能。

---

## 实现的功能

### ✅ 1. 下单功能

**文件**: `core/binance_trading_api.py`

#### 支持的订单类型

| 订单类型 | 方法 | 说明 |
|---------|------|------|
| 市价单 | `place_market_order()` | 立即以市场价成交 |
| 限价单 | `place_limit_order()` | 指定价格成交 |
| 止损单 | `place_stop_loss_order()` | 触发后以市价成交 |
| 止盈单 | `place_take_profit_order()` | 触发后以市价成交 |

#### 核心特性

- ✅ **自动精度处理** - 根据币种自动调整数量和价格精度
- ✅ **签名验证** - HMAC SHA256 签名确保请求安全
- ✅ **重试机制** - 网络异常自动重试（最多 3 次）
- ✅ **错误处理** - 完善的异常捕获和日志记录

---

### ✅ 2. 查询订单

**文件**: `core/binance_trading_api.py` + `core/trading_executor.py`

#### 查询功能

| 方法 | 说明 |
|------|------|
| `query_order()` | 查询指定订单状态 |
| `query_open_orders()` | 查询所有未成交订单 |
| `get_order_history()` | 获取本地订单历史 |

#### 订单状态

- `NEW` - 新订单
- `PARTIALLY_FILLED` - 部分成交
- `FILLED` - 完全成交
- `CANCELED` - 已取消
- `REJECTED` - 被拒绝
- `EXPIRED` - 已过期

---

### ✅ 3. 撤销订单

**文件**: `core/binance_trading_api.py`

#### 撤销功能

| 方法 | 说明 |
|------|------|
| `cancel_order()` | 撤销指定订单 |
| `cancel_all_orders()` | 撤销某币种所有挂单 |

#### 使用场景

- 取消未成交的限价单
- 调整止损止盈价格
- 紧急平仓前撤销所有挂单

---

### ✅ 4. 止盈止损

**文件**: `core/trading_executor.py`

#### 自动化流程

`execute_short_trade()` 方法自动完成：

1. **设置杠杆** - 调用 `set_leverage()`
2. **开仓** - 调用 `place_market_order()`
3. **止损单** - 调用 `place_stop_loss_order()`
4. **止盈单 1** - 调用 `place_take_profit_order()` (50% 仓位)
5. **止盈单 2** - 调用 `place_take_profit_order()` (剩余 50%)

#### 止盈止损类型

| 类型 | 触发条件 | 操作 |
|------|---------|------|
| 止损 (STOP_MARKET) | 价格 ≥ 止损价 | 市价买入平仓 |
| 止盈 1 (TAKE_PROFIT_MARKET) | 价格 ≤ 止盈价 1 | 买入平仓 50% |
| 止盈 2 (TAKE_PROFIT_MARKET) | 价格 ≤ 止盈价 2 | 买入平仓剩余 |

---

### ✅ 5. 持仓管理

**文件**: `core/trading_executor.py`

#### 持仓功能

| 方法 | 说明 |
|------|------|
| `get_all_positions()` | 获取 API 持仓（实时同步） |
| `get_position()` | 获取本地持仓记录 |
| `get_open_positions()` | 获取未平仓位 |
| `close_position()` | 平仓（支持部分平仓） |

#### 持仓信息

- 币种符号
- 持仓数量
- 入场价格
- 标记价格
- 未实现盈亏
- 杠杆倍数
- 止损止盈价格

---

### ✅ 6. 账户查询

**文件**: `core/binance_trading_api.py`

#### 查询功能

| 方法 | 说明 |
|------|------|
| `get_account_balance()` | 查询账户余额 |
| `get_position()` | 查询持仓信息 |
| `set_leverage()` | 设置杠杆倍数 |
| `get_mark_price()` | 获取标记价格 |
| `get_futures_ticker()` | 获取 24 小时行情 |

---

## 文件结构

```
short_selling_system/
├── core/
│   ├── binance_trading_api.py    # 币安交易 API 客户端（新增）
│   ├── trading_executor.py       # 交易执行器（已更新）
│   ├── binance_client.py         # 币安数据客户端（已有）
│   └── signal_manager.py         # 信号管理器（已有）
├── docs/
│   ├── binance_api_usage.md      # 使用指南（新增）
│   ├── binance_api_quick_reference.md  # 快速参考（新增）
│   └── binance_api_config.md     # 配置说明（新增）
├── tests/
│   └── test_binance_trading_api.py  # 单元测试（新增）
└── config/
    └── settings.py               # 配置文件（已更新）
```

---

## 核心类说明

### 1. BinanceTradingAPI

**位置**: `core/binance_trading_api.py`

**功能**: 币安期货交易 API 客户端

**主要方法**:
```python
# 下单
place_order()              # 通用下单方法
place_market_order()       # 市价单
place_limit_order()        # 限价单
place_stop_loss_order()    # 止损单
place_take_profit_order()  # 止盈单

# 订单管理
query_order()              # 查询订单
cancel_order()             # 撤销订单
cancel_all_orders()        # 撤销所有订单
query_open_orders()        # 查询挂单

# 账户管理
get_position()             # 查询持仓
get_account_balance()      # 查询余额
set_leverage()             # 设置杠杆

# 工具方法
get_symbol_precision()     # 获取精度信息
adjust_quantity()          # 调整数量精度
adjust_price()             # 调整价格精度
```

---

### 2. TradingExecutor

**位置**: `core/trading_executor.py`

**功能**: 高级交易执行器（整合币安 API）

**主要方法**:
```python
# 交易执行
execute_short_trade()      # 一键开仓 + 止损止盈
close_position()           # 平仓

# 订单管理
query_order()              # 查询订单
cancel_order()             # 撤销订单
cancel_all_orders()        # 撤销所有订单
get_order_history()        # 订单历史

# 持仓管理
get_all_positions()        # 获取持仓
get_position()             # 获取单个持仓
get_open_positions()       # 获取未平仓位

# 账户查询
get_account_balance()      # 获取余额
```

---

## 使用示例

### 快速开始

```python
from core.trading_executor import trading_executor

# 一键交易（推荐）
order_id = trading_executor.execute_short_trade(
    symbol="BTCUSDT",
    entry_price=50000.0,
    stop_loss=52000.0,
    take_profit_1=48000.0,
    take_profit_2=47000.0,
    quantity=0.01,
    leverage=5,
    reason="信号触发"
)

# 查询订单
order = trading_executor.query_order("BTCUSDT", int(order_id))

# 平仓
pnl = trading_executor.close_position("BTCUSDT")
```

### 底层 API 调用

```python
from core.binance_trading_api import binance_trading_api

# 市价开空
result = binance_trading_api.place_market_order(
    symbol="BTCUSDT",
    side="SELL",
    quantity=0.01,
    position_side="SHORT"
)

# 设置止损
stop_result = binance_trading_api.place_stop_loss_order(
    symbol="BTCUSDT",
    side="BUY",
    quantity=0.01,
    stop_price=52000.0,
    position_side="SHORT"
)

# 查询持仓
positions = binance_trading_api.get_position()
```

---

## 配置要求

### 环境变量

在 `.env` 文件中配置：

```bash
BINANCE_API_KEY=your_api_key
BINANCE_SECRET_KEY=your_secret_key
```

### API 权限

必需权限：
- ✅ 读取
- ✅ 合约交易

推荐配置：
- ✅ IP 白名单
- ✅ 子账户隔离

---

## 测试

### 运行单元测试

```bash
cd short_selling_system
pytest tests/test_binance_trading_api.py -v
```

### 测试连接

```bash
python -c "
from core.binance_trading_api import binance_trading_api
balance = binance_trading_api.get_account_balance()
print('连接成功!' if balance else '连接失败!')
"
```

---

## 安全特性

### 1. 签名验证

- 使用 HMAC SHA256 签名所有请求
- 时间戳验证防止重放攻击
- 接收窗口限制（默认 5 秒）

### 2. 精度处理

- 自动获取币种精度信息
- 数量和价格自动对齐 step_size/tick_size
- 避免因精度问题导致的订单失败

### 3. 错误处理

- 完善的异常捕获
- 自动重试机制
- 详细的错误日志

### 4. 密钥管理

- 密钥从环境变量读取
- 不硬编码在代码中
- 建议定期轮换

---

## 性能优化

### 1. 缓存机制

- 精度信息缓存（避免重复请求）
- 减少 API 调用次数

### 2. 批量操作

- 支持批量撤销订单
- 批量查询持仓

### 3. 异步支持

当前为同步实现，可扩展为异步：

```python
# 未来可扩展
import asyncio
import aiohttp

async def place_order_async():
    async with aiohttp.ClientSession() as session:
        # 异步请求
        pass
```

---

## 最佳实践

### 1. 使用 TradingExecutor

推荐优先使用 `TradingExecutor` 而非直接调用 API：

```python
# 推荐
trading_executor.execute_short_trade(...)

# 不推荐（除非需要精细控制）
binance_trading_api.place_order(...)
```

### 2. 错误处理

```python
try:
    order_id = trading_executor.execute_short_trade(...)
    if not order_id:
        logger.error("交易执行失败")
except Exception as e:
    logger.error(f"交易异常：{e}")
```

### 3. 订单监控

```python
# 定期检查订单状态
while True:
    order = trading_executor.query_order(symbol, order_id)
    if order['status'] == 'FILLED':
        break
    time.sleep(1)
```

### 4. 日志记录

```python
# 所有关键操作都有日志
logger.info(f"✅ 下单成功：{symbol}, ID={order_id}")
logger.warning(f"⚠️ 订单未完全成交：{status}")
logger.error(f"❌ 平仓失败：{e}")
```

---

## 未来扩展

### 可能的增强功能

1. **移动止盈** - 根据浮盈动态调整止盈价
2. **条件单** - 支持更复杂的触发条件
3. **组合订单** - 一篮子订单同时执行
4. **回测集成** - 与回测系统对接
5. **风险控制** - 更严格的风控检查

---

## 相关文档

- [使用指南](docs/binance_api_usage.md) - 详细使用文档
- [快速参考](docs/binance_api_quick_reference.md) - 快速查阅手册
- [配置说明](docs/binance_api_config.md) - API 配置指南

---

## 总结

已实现完整的币安期货交易 API 功能：

✅ **下单** - 市价/限价/止损/止盈  
✅ **查询** - 订单/持仓/余额  
✅ **撤销** - 单个/全部订单  
✅ **止盈止损** - 自动化设置和管理  
✅ **精度处理** - 自动适配不同币种  
✅ **错误处理** - 完善的异常和重试机制  
✅ **测试覆盖** - 完整的单元测试  

使用 `trading_executor` 可以一键完成开仓 + 止损止盈设置，简化交易流程。
