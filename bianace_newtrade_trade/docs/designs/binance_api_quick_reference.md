# 币安交易 API 快速参考

## 导入模块

```python
from core.binance_trading_api import binance_trading_api
from core.trading_executor import trading_executor
```

---

## 下单

### 市价开空
```python
result = binance_trading_api.place_market_order(
    symbol="BTCUSDT",
    side="SELL",
    quantity=0.01,
    position_side="SHORT"
)
```

### 限价开空
```python
result = binance_trading_api.place_limit_order(
    symbol="BTCUSDT",
    side="SELL",
    quantity=0.01,
    price=50000.0,
    position_side="SHORT"
)
```

### 止损单（空头止损）
```python
result = binance_trading_api.place_stop_loss_order(
    symbol="BTCUSDT",
    side="BUY",
    quantity=0.01,
    stop_price=52000.0,
    position_side="SHORT"
)
```

### 止盈单（空头止盈）
```python
result = binance_trading_api.place_take_profit_order(
    symbol="BTCUSDT",
    side="BUY",
    quantity=0.01,
    stop_price=48000.0,
    position_side="SHORT"
)
```

---

## 查询订单

### 查询指定订单
```python
order = binance_trading_api.query_order(
    symbol="BTCUSDT",
    order_id=12345678
)
```

### 查询所有挂单
```python
orders = binance_trading_api.query_open_orders(symbol="BTCUSDT")
```

---

## 撤销订单

### 撤销指定订单
```python
result = binance_trading_api.cancel_order(
    symbol="BTCUSDT",
    order_id=12345678
)
```

### 撤销所有订单
```python
result = binance_trading_api.cancel_all_orders(symbol="BTCUSDT")
```

---

## 一键交易（推荐）

### 执行做空交易（自动设置止损止盈）
```python
order_id = trading_executor.execute_short_trade(
    symbol="BTCUSDT",
    entry_price=50000.0,
    stop_loss=52000.0,      # 止损价
    take_profit_1=48000.0,  # 第一止盈价
    take_profit_2=47000.0,  # 第二止盈价
    quantity=0.01,
    leverage=5,
    reason="信号触发"
)
```

### 平仓
```python
pnl = trading_executor.close_position(
    symbol="BTCUSDT",
    reason="manual"  # manual/stop_loss/take_profit/time
)
```

---

## 持仓管理

### 获取所有持仓
```python
positions = binance_trading_api.get_position()
```

### 获取账户余额
```python
balances = binance_trading_api.get_account_balance()
```

### 设置杠杆
```python
result = binance_trading_api.set_leverage(
    symbol="BTCUSDT",
    leverage=10,
    position_side="SHORT"
)
```

---

## 订单状态说明

| 状态 | 说明 |
|------|------|
| `NEW` | 新订单，未成交 |
| `PARTIALLY_FILLED` | 部分成交 |
| `FILLED` | 完全成交 |
| `CANCELED` | 已取消 |
| `REJECTED` | 被拒绝 |
| `EXPIRED` | 已过期 |

---

## 常见错误码

| 错误码 | 说明 | 解决方案 |
|--------|------|----------|
| -1021 | 时间戳偏差 | 同步系统时间 |
| -1013 | 订单不存在 | 检查订单 ID |
| -1003 | 签名验证失败 | 检查 API 密钥 |
| -2019 | 余额不足 | 充值或减少仓位 |
| -2014 | 杠杆倍数过高 | 降低杠杆 |

---

## 精度处理

系统会自动处理数量和价格精度：

```python
# 自动调整
adjusted_qty = binance_trading_api.adjust_quantity("BTCUSDT", 0.123456)
adjusted_price = binance_trading_api.adjust_price("BTCUSDT", 50123.456)
```

---

## 完整示例

```python
from core.trading_executor import trading_executor

# 一键完成：开仓 + 止损 + 止盈
order_id = trading_executor.execute_short_trade(
    symbol="BTCUSDT",
    entry_price=50000.0,
    stop_loss=52000.0,
    take_profit_1=48000.0,
    take_profit_2=47000.0,
    quantity=0.01,
    leverage=5
)

# 查询订单
order = trading_executor.query_order("BTCUSDT", int(order_id))

# 平仓
pnl = trading_executor.close_position("BTCUSDT", reason="manual")
```
