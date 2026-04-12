# 币安 API 接口文档

> **重要提示**: 本文档会随币安 API 更新而变化，请定期检查更新。
> 
> **PM 账户专用**: 本交易系统使用**投资组合保证金账户 (Portfolio Margin)**，所有合约接口必须使用 `/papi/v1/*` 端点。

---

## 📋 接口分类

### 1. 赚币接口 (Simple Earn)

#### 查询赚币活期产品列表
- **端点**: `GET /sapi/v1/simple-earn/flexible/list`
- **权重**: 150 (IP)
- **PM 账户兼容性**: ✅ 通用接口，PM 账户可用
- **主要用途**: 查询闲置资金理财产品的收益率

**请求参数**:
```
asset: STRING (可选)
current: LONG (可选，默认 1)
size: LONG (可选，默认 10，最大 100)
timestamp: LONG (必填)
```

**响应示例**:
```json
{
  "rows": [{
    "asset": "BTC",
    "latestAnnualPercentageRate": "0.05000000",
    "tierAnnualPercentageRate": {"0-5BTC": 0.05, "5-10BTC": 0.03},
    "canPurchase": true,
    "canRedeem": true,
    "productId": "BTC001"
  }],
  "total": 1
}
```

#### 赎回活期产品
- **端点**: `POST /sapi/v1/simple-earn/flexible/redeem`
- **权重**: 1 (UID)
- **频率限制**: 每个账户最多三秒一次
- **PM 账户兼容性**: ✅ 通用接口

---

### 2. 资金划转接口 (Universal Transfer)

#### 用户万向划转
- **端点**: `POST /sapi/v1/asset/transfer`
- **权重**: 900 (UID)
- **PM 账户兼容性**: ⚠️ **注意**: PM 账户使用特殊划转类型

**关键划转类型**:
```
MAIN_PORTFOLIO_MARGIN    现货钱包 → 统一账户钱包 ✅ PM 账户专用
PORTFOLIO_MARGIN_MAIN    统一账户钱包 → 现货钱包 ✅ PM 账户专用
UMFUTURE_MAIN            U 本位合约 → 现货 (传统账户)
MAIN_UMFUTURE            现货 → U 本位合约 (传统账户)
```

**PM 账户资金使用流程**:
```
1. 现货钱包 USDT → MAIN_PORTFOLIO_MARGIN → 统一账户钱包
2. 统一账户钱包自动作为合约保证金
3. 盈利/本金返回：PORTFOLIO_MARGIN_MAIN → 现货钱包
```

---

### 3. U 本位合约接口 (PM 账户专用) ⭐

> **重要**: PM 账户必须使用 `/papi/v1/um/*` 端点，而不是 `/fapi/v1/*`

#### UM 下单 (Place Order)
- **端点**: `POST /papi/v1/um/order` ⭐ **PM 账户专用**
- **权重**: 1 (Order)
- **频率限制**: 2 订单/秒，10 订单/3 秒，300 订单/15 分钟

**请求参数**:
```
symbol: STRING (必填) - 交易对，如 BTCUSDT
side: ENUM (必填) - BUY/SELL
positionSide: ENUM (必填) - BOTH(单向)/LONG/SHORT(双向)
type: ENUM (必填) - LIMIT/MARKET
quantity: DECIMAL (必填) - 下单数量
price: DECIMAL (可选) - 限价单价格
timeInForce: ENUM (可选) - GTC/IOC/FOK/GTD
reduceOnly: STRING (可选) - true/false，默认 false
newClientOrderId: STRING (可选) - 自定义订单号
```

**positionSide 说明**:
- **单向持仓模式**: 只能填 `BOTH`
- **双向持仓模式**: 必须填 `LONG` 或 `SHORT`
- **PM 账户推荐**: 使用双向持仓模式，便于同时管理多空

**响应示例**:
```json
{
  "orderId": 22542179,
  "symbol": "BTCUSDT",
  "side": "BUY",
  "positionSide": "LONG",
  "status": "NEW",
  "type": "MARKET",
  "origQty": "0.1",
  "avgPrice": "95000.00",
  "executedQty": "0.1",
  "cumQuote": "9500.00"
}
```

#### 查询当前 UM 挂单
- **端点**: `GET /papi/v1/um/openOrder`
- **权重**: 1
- **PM 账户兼容性**: ✅ PM 账户专用

**请求参数**:
```
symbol: STRING (必填)
orderId: LONG (可选)
origClientOrderId: STRING (可选)
```

**注意**: 
- orderId 或 origClientOrderId 至少发送一个
- 已成交或取消的订单会返回 "Order does not exist"

---

### 4. UM 条件单接口 (Conditional Order) ⭐

#### UM 条件单下单
- **端点**: `POST /papi/v1/um/conditional/order` ⭐ **PM 账户专用**
- **权重**: 1 (Order)
- **用途**: 止损单、止盈单、跟踪止损单

**请求参数**:
```
symbol: STRING (必填)
side: ENUM (必填) - BUY/SELL
positionSide: ENUM (必填) - BOTH/LONG/SHORT
strategyType: ENUM (必填) - 条件单类型
  - STOP: 止损单 (限价)
  - STOP_MARKET: 止损单 (市价)
  - TAKE_PROFIT: 止盈单 (限价)
  - TAKE_PROFIT_MARKET: 止盈单 (市价)
  - TRAILING_STOP_MARKET: 跟踪止损
quantity: DECIMAL (必填)
price: DECIMAL (可选) - 限价单价格
stopPrice: DECIMAL (必填) - 触发价格
workingType: ENUM (可选) - MARK_PRICE/COTRACT_PRICE
priceProtect: STRING (可选) - TRUE/FALSE，默认 FALSE
```

**条件单触发规则**:

**止损单 (STOP/STOP_MARKET)**:
- 买入：最新价格 ≥ stopPrice
- 卖出：最新价格 ≤ stopPrice

**止盈单 (TAKE_PROFIT/TAKE_PROFIT_MARKET)**:
- 买入：最新价格 ≤ stopPrice
- 卖出：最新价格 ≥ stopPrice

**跟踪止损 (TRAILING_STOP_MARKET)**:
- 需要参数：activationPrice(激活价格), callbackRate(回调幅度 0.1-5%)
- 买入：价格从最低点反弹 ≥ callbackRate%
- 卖出：价格从最高点下跌 ≥ callbackRate%

**响应示例**:
```json
{
  "strategyId": 123445,
  "strategyType": "STOP_MARKET",
  "strategyStatus": "NEW",
  "symbol": "BTCUSDT",
  "side": "SELL",
  "positionSide": "LONG",
  "stopPrice": "93000",
  "workingType": "MARK_PRICE",
  "priceProtect": true
}
```

---

## 🔑 PM 账户 vs 传统账户 接口对比

| 功能 | 传统账户端点 | PM 账户端点 | 说明 |
|------|-------------|-----------|------|
| 合约下单 | `/fapi/v1/order` | `/papi/v1/um/order` | PM 账户必须用 papi |
| 条件单 | `/fapi/v1/conditional/order` | `/papi/v1/um/conditional/order` | PM 账户必须用 papi |
| 查询挂单 | `/fapi/v1/openOrder` | `/papi/v1/um/openOrder` | PM 账户必须用 papi |
| 资金划转 | `MAIN_UMFUTURE` | `MAIN_PORTFOLIO_MARGIN` | PM 账户用统一账户 |
| 持仓查询 | `/fapi/v2/positionRisk` | `/papi/v1/um/positionRisk` | PM 账户返回不同结构 |
| 账户信息 | `/fapi/v2/balance` | `/papi/v1/balance` | PM 账户返回多资产保证金 |

---

## ⚠️ PM 账户特殊注意事项

### 1. 杠杆限制
- **传统账户**: 每个交易对可独立设置杠杆
- **PM 账户**: **同一交易对只能有一个杠杆值**
  - 不能同时存在 BTCUSDT 多仓 3x 和空仓 5x
  - 解决方案：使用统一杠杆 (如 5x)，通过仓位比例控制风险

### 2. 持仓方向 (positionSide)
- **单向持仓**: positionSide = "BOTH"
- **双向持仓**: positionSide = "LONG" 或 "SHORT"
- **PM 账户推荐**: 双向持仓模式，便于对冲

### 3. 保证金计算
```
传统账户：保证金 = 名义价值 / 杠杆 (每个交易对独立)
PM 账户：组合保证金 = Σ(各头寸风险) - 组合折扣
```

### 4. 资金划转
```
传统账户：现货 ↔ U 本位合约 (MAIN_UMFUTURE)
PM 账户：现货 ↔ 统一账户 (MAIN_PORTFOLIO_MARGIN)
        统一账户自动作为所有衍生品保证金
```

---

## 📊 实际交易中的接口调用顺序

### 开仓流程
```
1. GET /papi/v1/um/positionRisk     # 查询当前持仓
2. POST /papi/v1/um/leverage        # 设置杠杆 (首次)
3. POST /papi/v1/um/order          # 开仓下单
4. GET /fapi/v1/exchangeInfo        # 获取精度 (首次)
5. POST /papi/v1/um/conditional/order  # 设置止损
6. POST /papi/v1/um/conditional/order  # 设置止盈
```

### 延迟控制 (避免 -1015 错误)
```python
# 信号之间延迟
time.sleep(1.0)  # 1 秒

# 开仓后延迟
time.sleep(0.5)  # 0.5 秒

# 止损/止盈设置后延迟
time.sleep(0.3)  # 0.3 秒
```

---

## 🔧 常用接口精度查询

### GET /fapi/v1/exchangeInfo
```json
{
  "symbols": [{
    "symbol": "BTCUSDT",
    "filters": [{
      "filterType": "LOT_SIZE",
      "stepSize": "0.001",      # 数量精度
      "minQty": "0.001"
    }, {
      "filterType": "PRICE_FILTER",
      "tickSize": "0.10",       # 价格精度
      "minPrice": "0.10"
    }]
  }]
}
```

**精度处理最佳实践**:
```python
# BTCUSDT: 数量 3 位小数，价格 1 位小数
quantity = Decimal('0.123')  # ✅
price = Decimal('95123.4')   # ✅

# BNBUSDT: 数量 2 位小数 (特殊)
quantity = Decimal('1.23')   # ✅
quantity = Decimal('1.234')  # ❌ 精度超限
```

---

## 📝 文档更新记录

| 日期 | 更新内容 | 版本 |
|------|---------|------|
| 2026-03-13 | 初始版本，包含基础接口文档 | v1.0 |
| 2026-04-08 | 添加 PM 账户专用标注和说明 | v2.0 |

---

## 🔗 相关文档

- [TRADING_EXPERIENCE.md](./TRADING_EXPERIENCE.md) - 实战经验总结
- [QUICK_REFERENCE.md](./QUICK_REFERENCE.md) - 快速参考卡
- [SKILL.md](./SKILL.md) - 技能主文档
