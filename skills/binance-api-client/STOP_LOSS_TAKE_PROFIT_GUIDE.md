# 止损止盈条件单设置指南

> 📅 2026-03-23 调试经验总结

## ⚠️ 重要提示

**止损止盈必须使用条件单接口，不能使用普通订单接口！**

## ❌ 错误做法

```python
# 错误：这些是普通订单接口，会报错 Invalid orderType
api.place_stop_market_order(...)
api.place_take_profit_market_order(...)
```

## ✅ 正确做法

```python
from binance_trade_api import get_trade_api
from decimal import Decimal

api = get_trade_api()

# 1. 先开仓
order = api.place_market_order(
    symbol="ETHUSDT",
    side="BUY",
    position_side="BOTH",
    quantity=Decimal('0.07')
)

# 2. 获取持仓数量
positions = api.get_position_risk("ETHUSDT")
position_qty = abs(Decimal(positions[0]['positionAmt']))

# 3. 设置止损（使用条件单接口）
stop_loss = api.place_pm_conditional_order(
    symbol="ETHUSDT",
    side="SELL",              # 多单平仓用 SELL
    position_side="BOTH",
    strategy_type="STOP_MARKET",
    quantity=position_qty,     # 平仓数量
    stop_price=Decimal('2095.0'),
    reduce_only=True           # 只减仓
)

# 4. 设置止盈
take_profit = api.place_pm_conditional_order(
    symbol="ETHUSDT",
    side="SELL",
    position_side="BOTH",
    strategy_type="TAKE_PROFIT_MARKET",
    quantity=position_qty,
    stop_price=Decimal('2200.0'),
    reduce_only=True
)
```

## 🔑 关键参数说明

### 1. strategy_type（必填）
- `"STOP_MARKET"` - 止损市单
- `"TAKE_PROFIT_MARKET"` - 止盈市单
- `"STOP"` - 止损限价单（需要 price 参数）
- `"TAKE_PROFIT"` - 止盈限价单（需要 price 参数）

### 2. quantity（必填）
- 必须指定具体的平仓数量
- 通过 `get_position_risk()` 获取当前持仓
- 使用 `abs()` 取绝对值

### 3. reduce_only（必填）
- 必须设置为 `True`
- 确保只减仓，不会反向开仓
- 防止止损触发后变成反向持仓

### 4. side（方向）
- 多单（BUY）平仓用 `SELL`
- 空单（SELL）平仓用 `BUY`

## 📊 接口对比

| 接口 | 路径 | 用途 | 是否用于止损止盈 |
|------|------|------|----------------|
| `place_um_order` | `/papi/v1/um/order` | 普通订单 | ❌ 否 |
| `place_market_order` | `/papi/v1/um/order` | 市价单 | ❌ 否 |
| `place_pm_conditional_order` | `/papi/v1/um/conditional/order` | 条件单 | ✅ 是 |

## 🐛 常见错误及解决方案

### 错误 1：Invalid orderType

**错误原因**：使用了错误的接口

```python
# ❌ 错误
api.place_stop_market_order(...)

# ✅ 正确
api.place_pm_conditional_order(
    strategy_type="STOP_MARKET", ...
)
```

### 错误 2：Order's notional must be greater than 100

**错误原因**：平仓数量太小，名义价值 < 100 USDT

**解决方案**：
```python
min_qty = Decimal('100') / current_price
if position_qty < min_qty:
    position_qty = min_qty  # 向上调整
```

### 错误 3：Reduce only order is not supported

**错误原因**：没有设置 `reduce_only=True`

**解决方案**：
```python
api.place_pm_conditional_order(
    ...,
    reduce_only=True  # ✅ 必须设置
)
```

## 📝 完整示例（分批止盈）

```python
from binance_trade_api import get_trade_api
from decimal import Decimal

api = get_trade_api()

# 步骤 1：开仓
order = api.place_market_order(
    symbol="ETHUSDT",
    side="BUY",
    position_side="BOTH",
    quantity=Decimal('0.07')
)

# 步骤 2：获取持仓数量
positions = api.get_position_risk("ETHUSDT")
position_qty = abs(Decimal(positions[0]['positionAmt']))

# 步骤 3：设置止损
stop_loss = api.place_pm_conditional_order(
    symbol="ETHUSDT",
    side="SELL",
    position_side="BOTH",
    strategy_type="STOP_MARKET",
    quantity=position_qty,
    stop_price=Decimal('2095.0'),
    reduce_only=True
)

# 步骤 4：设置分批止盈
tp_levels = [
    (Decimal('2200.0'), '50%'),  # TP1: 50%
    (Decimal('2250.0'), '30%'),  # TP2: 30%
    (Decimal('2300.0'), '20%')   # TP3: 20%
]

for tp_price, ratio in tp_levels:
    tp_qty = position_qty * Decimal(ratio.replace('%', '')) / 100
    tp_order = api.place_pm_conditional_order(
        symbol="ETHUSDT",
        side="SELL",
        position_side="BOTH",
        strategy_type="TAKE_PROFIT_MARKET",
        quantity=tp_qty,
        stop_price=tp_price,
        reduce_only=True
    )
    print(f"止盈{tp_price}设置成功，策略 ID={tp_order['strategyId']}")
```

## ✅ 验证结果

实际测试成功：
```
2026-03-23 23:29:16,771 - PM 条件单：ETHUSDT SELL BOTH, 类型：STOP_MARKET, 触发价：2095.0
2026-03-23 23:29:17,008 - 条件单成功：策略 ID=81786402, 状态：NEW

2026-03-23 23:29:17,233 - PM 条件单：ETHUSDT SELL BOTH, 类型：TAKE_PROFIT_MARKET, 触发价：2200.0
2026-03-23 23:29:17,468 - 条件单成功：策略 ID=81786403, 状态：NEW

2026-03-23 23:29:17,692 - PM 条件单：ETHUSDT SELL BOTH, 类型：TAKE_PROFIT_MARKET, 触发价：2250.0
2026-03-23 23:29:17,930 - 条件单成功：策略 ID=81786404, 状态：NEW

2026-03-23 23:29:18,154 - PM 条件单：ETHUSDT SELL BOTH, 类型：TAKE_PROFIT_MARKET, 触发价：2300.0
2026-03-23 23:29:18,388 - 条件单成功：策略 ID=81786405, 状态：NEW
```

## 📚 参考文档

- 币安官方文档：[UM 条件单下单 (TRADE)](https://binance-docs.github.io/apidocs/portfolio_margin/cn/#um-6b9c3e0a)
- 接口路径：`POST /papi/v1/um/conditional/order`

---

**版本**: 1.0  
**最后更新**: 2026-03-23  
**作者**: Binance API Client Skill
