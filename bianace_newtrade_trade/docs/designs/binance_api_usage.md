# 币安交易 API 使用指南

本文档介绍如何使用币安期货交易 API 模块实现下单、查询订单、止盈止损、撤销订单等功能。

## 目录

- [功能概述](#功能概述)
- [快速开始](#快速开始)
- [API 配置](#api-配置)
- [核心功能](#核心功能)
  - [下单](#下单)
  - [查询订单](#查询订单)
  - [撤销订单](#撤销订单)
  - [止盈止损](#止盈止损)
  - [持仓管理](#持仓管理)
  - [账户查询](#账户查询)
- [使用示例](#使用示例)
- [错误处理](#错误处理)

---

## 功能概述

币安交易 API 模块 (`binance_trading_api.py`) 提供以下核心功能：

### 交易功能
- ✅ **市价单** - 立即以市场价格成交
- ✅ **限价单** - 指定价格成交
- ✅ **止损单** (STOP_MARKET) - 触发价格后以市价成交
- ✅ **止盈单** (TAKE_PROFIT_MARKET) - 触发价格后以市价成交

### 订单管理
- ✅ **查询订单** - 获取订单状态
- ✅ **撤销订单** - 取消指定订单
- ✅ **撤销所有订单** - 取消币种所有挂单
- ✅ **查询挂单** - 获取当前所有未成交订单

### 账户管理
- ✅ **持仓查询** - 获取当前持仓信息
- ✅ **余额查询** - 获取账户余额
- ✅ **杠杆设置** - 设置持仓杠杆倍数

### 自动精度处理
- ✅ **数量精度** - 自动根据币种调整下单数量精度
- ✅ **价格精度** - 自动根据币种调整价格精度
- ✅ **step_size/tick_size** - 确保符合交易所规则

---

## 快速开始

### 1. 导入模块

```python
from core.binance_trading_api import binance_trading_api
from core.trading_executor import trading_executor
```

### 2. 配置 API 密钥

在 `.env` 文件中配置：

```bash
# 币安 API 配置
BINANCE_API_KEY=your_api_key
BINANCE_SECRET_KEY=your_secret_key
```

---

## API 配置

### 初始化参数

```python
from core.binance_trading_api import BinanceTradingAPI

api = BinanceTradingAPI()
```

### 配置项

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `api_key` | 币安 API Key | 必填 |
| `secret_key` | 币安 Secret Key | 必填 |
| `timeout` | 请求超时时间 (秒) | 10 |
| `recv_window` | 接收窗口 (毫秒) | 5000 |

---

## 核心功能

### 下单

#### 市价单

```python
# 开空单
result = binance_trading_api.place_market_order(
    symbol="BTCUSDT",
    side="SELL",
    quantity=0.01,
    position_side="SHORT"
)

if result:
    print(f"订单 ID: {result['orderId']}")
    print(f"状态：{result['status']}")
    print(f"成交均价：{result['avgPrice']}")
```

#### 限价单

```python
# 限价开空
result = binance_trading_api.place_limit_order(
    symbol="BTCUSDT",
    side="SELL",
    quantity=0.01,
    price=50000.0,
    position_side="SHORT",
    time_in_force="GTC"  # GTC/IOC/FOK
)
```

#### 止损单

```python
# 设置止损（价格上涨触及止损价触发）
result = binance_trading_api.place_stop_loss_order(
    symbol="BTCUSDT",
    side="BUY",  # 空头止损是买入
    quantity=0.01,
    stop_price=52000.0,  # 触发价格
    position_side="SHORT"
)
```

#### 止盈单

```python
# 设置止盈（价格下跌触及止盈价触发）
result = binance_trading_api.place_take_profit_order(
    symbol="BTCUSDT",
    side="BUY",  # 空头止盈是买入
    quantity=0.01,
    stop_price=48000.0,  # 触发价格
    position_side="SHORT"
)
```

---

### 查询订单

#### 查询指定订单

```python
# 通过订单 ID 查询
order = binance_trading_api.query_order(
    symbol="BTCUSDT",
    order_id=12345678
)

if order:
    print(f"状态：{order['status']}")
    print(f"成交数量：{order['executedQty']}")
    print(f"成交均价：{order['avgPrice']}")
```

#### 查询所有挂单

```python
# 查询所有币种的挂单
open_orders = binance_trading_api.query_open_orders()

# 查询指定币种的挂单
btc_orders = binance_trading_api.query_open_orders(symbol="BTCUSDT")

for order in open_orders:
    print(f"{order['symbol']}: {order['side']} {order['type']}")
```

---

### 撤销订单

#### 撤销指定订单

```python
# 撤销订单
result = binance_trading_api.cancel_order(
    symbol="BTCUSDT",
    order_id=12345678
)

if result:
    print("✅ 撤销成功")
```

#### 撤销所有订单

```python
# 撤销某币种所有挂单
result = binance_trading_api.cancel_all_orders(symbol="BTCUSDT")

if result:
    print(f"✅ 撤销成功，共撤销 {len(result)} 个订单")
```

---

### 止盈止损

#### 完整示例：开仓 + 止损止盈

```python
from core.trading_executor import trading_executor

# 执行做空交易（自动设置止损止盈）
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

if order_id:
    print(f"✅ 交易执行成功，订单 ID: {order_id}")
```

#### 止损止盈说明

系统会自动设置三个订单：

1. **止损单** (STOP_MARKET): 价格触及止损价时，市价买入平仓
2. **止盈单 1** (TAKE_PROFIT_MARKET): 价格触及第一止盈价时，买入平仓 50%
3. **止盈单 2** (TAKE_PROFIT_MARKET): 价格触及第二止盈价时，买入平仓剩余 50%

---

### 持仓管理

#### 获取持仓信息

```python
# 获取所有持仓
positions = binance_trading_api.get_position()

for pos in positions:
    print(f"币种：{pos['symbol']}")
    print(f"持仓数量：{pos['positionAmt']}")
    print(f"入场价格：{pos['entryPrice']}")
    print(f"标记价格：{pos['markPrice']}")
    print(f"未实现盈亏：{pos['unrealizedProfit']}")
    print(f"杠杆：{pos['leverage']}")
```

#### 获取单个持仓

```python
from core.trading_executor import trading_executor

# 获取本地持仓记录
position = trading_executor.get_position("BTCUSDT")

if position:
    print(f"入场价：{position['entry_price']}")
    print(f"止损价：{position['stop_loss']}")
    print(f"止盈价：{position['take_profit_1']}, {position['take_profit_2']}")
```

#### 平仓

```python
# 全部平仓
pnl = trading_executor.close_position(
    symbol="BTCUSDT",
    reason="manual"  # manual/stop_loss/take_profit/time
)

# 部分平仓
pnl = trading_executor.close_position(
    symbol="BTCUSDT",
    quantity=0.005,  # 平仓一半
    reason="partial_close"
)

if pnl is not None:
    print(f"盈亏：{pnl:.2f} USDT")
```

---

### 账户查询

#### 查询余额

```python
balances = binance_trading_api.get_account_balance()

for balance in balances:
    if float(balance['availableBalance']) > 0:
        print(f"{balance['asset']}: {balance['availableBalance']} USDT")
```

#### 设置杠杆

```python
# 设置杠杆倍数
result = binance_trading_api.set_leverage(
    symbol="BTCUSDT",
    leverage=10,
    position_side="SHORT"
)

if result:
    print(f"✅ 杠杆设置成功：{result['leverage']}x")
```

---

## 使用示例

### 示例 1：完整交易流程

```python
from core.binance_trading_api import binance_trading_api
from core.trading_executor import trading_executor

def complete_trading_flow():
    """完整交易流程示例"""
    
    symbol = "BTCUSDT"
    
    # 1. 检查账户余额
    balances = binance_trading_api.get_account_balance()
    usdt_balance = next(
        (b for b in balances if b['asset'] == 'USDT'), 
        None
    )
    print(f"USDT 余额：{usdt_balance['availableBalance']}")
    
    # 2. 获取当前价格
    ticker = binance_trading_api.get_mark_price(symbol)
    current_price = ticker
    print(f"当前价格：{current_price}")
    
    # 3. 计算止损止盈
    stop_loss = current_price * 1.05  # 5% 止损
    take_profit_1 = current_price * 0.98  # 2% 止盈
    take_profit_2 = current_price * 0.95  # 5% 止盈
    
    # 4. 执行交易
    order_id = trading_executor.execute_short_trade(
        symbol=symbol,
        entry_price=current_price,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        quantity=0.01,
        leverage=5
    )
    
    if not order_id:
        print("❌ 交易执行失败")
        return
    
    print(f"✅ 交易执行成功，订单 ID: {order_id}")
    
    # 5. 查询订单状态
    order = trading_executor.query_order(symbol, int(order_id))
    if order:
        print(f"订单状态：{order['status']}")
    
    # 6. 监控持仓
    positions = trading_executor.get_all_positions()
    for pos in positions:
        if pos['symbol'] == symbol:
            print(f"持仓数量：{pos['positionAmt']}")
            print(f"未实现盈亏：{pos['unrealizedProfit']}")

# 执行
complete_trading_flow()
```

### 示例 2：订单管理

```python
def order_management_example():
    """订单管理示例"""
    
    symbol = "BTCUSDT"
    
    # 1. 查询所有挂单
    open_orders = binance_trading_api.query_open_orders(symbol)
    print(f"当前挂单：{len(open_orders)}")
    
    # 2. 撤销特定订单
    if open_orders:
        order_id = open_orders[0]['orderId']
        result = binance_trading_api.cancel_order(symbol, order_id)
        if result:
            print(f"✅ 订单 {order_id} 已撤销")
    
    # 3. 撤销所有挂单
    result = binance_trading_api.cancel_all_orders(symbol)
    if result:
        print(f"✅ 所有挂单已撤销")
    
    # 4. 查询订单历史（本地记录）
    from core.trading_executor import trading_executor
    order_history = trading_executor.get_order_history(symbol)
    for order in order_history:
        print(f"{order['type']} {order['side']} @ {order.get('avg_price', 'N/A')}")

order_management_example()
```

### 示例 3：持仓监控

```python
def position_monitoring():
    """持仓监控示例"""
    
    from core.trading_executor import trading_executor
    import time
    
    while True:
        # 获取所有持仓
        positions = trading_executor.get_all_positions()
        
        if not positions:
            print("无持仓")
            time.sleep(60)
            continue
        
        for pos in positions:
            symbol = pos['symbol']
            pnl = float(pos['unrealizedProfit'])
            entry_price = float(pos['entryPrice'])
            mark_price = float(pos['markPrice'])
            
            print(f"{symbol}:")
            print(f"  入场价：{entry_price}")
            print(f"  标记价：{mark_price}")
            print(f"  未实现盈亏：{pnl:.2f} USDT")
            
            # 检查是否需要手动干预
            if pnl < -10:  # 亏损超过 10 USDT
                print(f"  ⚠️  警告：亏损较大，考虑是否止损")
        
        time.sleep(30)  # 每 30 秒检查一次

# position_monitoring()
```

---

## 错误处理

### 常见错误码

| 错误码 | 说明 | 解决方案 |
|--------|------|----------|
| -1021 | 时间戳偏差 | 同步系统时间 |
| -1013 | 订单不存在 | 检查订单 ID |
| -1003 | 签名验证失败 | 检查 API 密钥 |
| -2019 | 余额不足 | 充值或减少仓位 |
| -2014 | 杠杆倍数过高 | 降低杠杆 |

### 异常处理示例

```python
try:
    result = binance_trading_api.place_market_order(
        symbol="BTCUSDT",
        side="SELL",
        quantity=0.01,
        position_side="SHORT"
    )
    
    if not result:
        logger.error("下单失败")
        return
    
except Exception as e:
    logger.error(f"交易异常：{e}")
    # 可以添加重试逻辑
```

---

## 最佳实践

### 1. 精度处理

系统会自动处理精度，但也可以手动调整：

```python
# 自动调整数量
adjusted_qty = binance_trading_api.adjust_quantity("BTCUSDT", 0.123456)
# 输出：0.123 (根据 BTC 精度)

# 自动调整价格
adjusted_price = binance_trading_api.adjust_price("BTCUSDT", 50123.456)
# 输出：50123.46 (根据 BTC 精度)
```

### 2. 订单状态检查

```python
def check_order_status(symbol, order_id):
    """检查订单是否完全成交"""
    order = binance_trading_api.query_order(symbol, order_id)
    
    if not order:
        return False
    
    status = order.get('status')
    
    # 完全成交状态
    if status in ['FILLED', 'PARTIALLY_FILLED']:
        return True
    
    # 未成交状态
    elif status in ['NEW', 'PENDING_NEW']:
        return False
    
    # 已取消或拒绝
    elif status in ['CANCELED', 'EXPIRED', 'REJECTED']:
        return False
    
    return False
```

### 3. 批量操作

```python
def batch_close_positions():
    """批量平仓所有持仓"""
    positions = binance_trading_api.get_position()
    
    for pos in positions:
        symbol = pos['symbol']
        pnl = trading_executor.close_position(symbol, reason="batch_close")
        print(f"{symbol}: 盈亏={pnl:.2f} USDT")
```

---

## 总结

币安交易 API 模块提供了完整的期货交易功能：

✅ **下单** - 市价/限价/止损/止盈  
✅ **查询** - 订单状态/持仓/余额  
✅ **撤销** - 单个/全部订单  
✅ **止盈止损** - 自动设置和管理  
✅ **精度处理** - 自动适配不同币种  

使用 `trading_executor` 可以简化交易流程，一键完成开仓 + 止损止盈设置。
