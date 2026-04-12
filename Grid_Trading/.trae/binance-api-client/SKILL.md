---
name: "binance-api-client"
description: "提供完整的币安 API 集成，包括行情获取、交易下单、账户管理等功能。支持 PM 账户 (投资组合保证金账户) 专用接口和自动精度处理。Invoke when you need to integrate Binance futures trading API in any Python project."
---

# Binance API Client Skill

这是一个可复用的币安合约交易 API 客户端模块，提供了完整的行情获取、交易下单、账户管理等功能。可以在任何 Python 项目中快速集成币安合约交易功能。

## 核心功能

### 1. 行情数据获取
- 实时价格查询
- K 线数据获取
- 技术指标计算（EMA、布林带、RSI、ATR、MACD）
- 资金费率查询
- 订单簿深度数据
- **交易对精度查询**（新增）

### 2. 交易功能
- 市价单/限价单/止损单/止盈单
- 订单撤销
- 订单状态查询
- 持仓风险查询
- 杠杆设置
- **自动精度处理**（新增）
- **PM 账户适配**（新增）

### 3. 账户管理
- 账户余额查询（兼容 PM 账户和传统账户）
- 持仓信息查询
- 盈亏计算
- 资金划转

### 4. 高级功能
- API 限流保护
- 自动重试机制
- 签名认证
- 生产/测试环境切换
- **PM 账户 vs 传统账户自动适配**（新增）

## 文件结构

```
binance-api-client/
├── binance_api.py          # 基础行情 API
├── binance_trade_api.py    # 交易 API（完整封装，含精度处理）
├── technical_indicators.py # 技术指标计算
├── rate_limiter.py         # API 限流器
├── requirements.txt        # 依赖包列表
├── setup.py               # 安装脚本
└── README.md              # 使用说明
```

## 快速开始

### 1. 安装依赖

```bash
pip install requests pandas numpy tenacity python-dotenv
```

### 2. 配置环境变量

创建 `.env` 文件：

```env
BINANCE_API_KEY=your_api_key
BINANCE_SECRET_KEY=your_secret_key
BINANCE_API_BASE_URL=https://papi.binance.com
USE_UNIFIED_ACCOUNT=true  # PM 账户必须设置
BINANCE_TESTNET=false
ENVIRONMENT=production
```

### 3. 使用示例

#### 示例 1：获取行情数据

```python
from binance_api import get_binance_futures_data, get_multiple_symbols_data

# 获取单个币种数据
btc_data = get_binance_futures_data("BTCUSDT")
print(f"BTC 24h 涨跌幅：{btc_data['priceChangePercent']}%")

# 获取多个币种数据
symbols_data = get_multiple_symbols_data(["BTCUSDT", "ETHUSDT", "BNBUSDT"])
for symbol, data in symbols_data.items():
    print(f"{symbol}: ${data['lastPrice']}")
```

#### 示例 2：技术指标计算

```python
from technical_indicators import get_technical_indicators

# 获取完整技术指标
indicators = get_technical_indicators("BTCUSDT")

# 访问不同时间周期的数据
print(f"1 小时 EMA21: {indicators['1h']['ema21'][-1]}")
print(f"4 小时 RSI: {indicators['4h']['rsi'][-1]}")
print(f"日线 ATR: {indicators['1d']['atr14'][-1]}")
```

#### 示例 3：交易下单（自动处理精度）

```python
from binance_trade_api import BinanceTradeAPI, get_trade_api
from decimal import Decimal

# 初始化 API（自动识别 PM 账户）
api = get_trade_api()

# 测试连接
if api.test_connectivity():
    print("API 连接成功")

# 获取账户余额（兼容 PM 账户）
usdt_balance = api.get_umfut_balance('USDT')
print(f"USDT 余额：{usdt_balance}")

# 设置杠杆
api.set_um_leverage("BTCUSDT", leverage=20)

# 市价开单（自动处理精度和 PM 账户适配）
order = api.place_market_order(
    symbol="BTCUSDT",
    side="BUY",
    position_side="LONG",  # PM 账户会自动转换为 BOTH
    quantity=Decimal('0.01')
)
print(f"订单 ID: {order['orderId']}, 状态：{order['status']}")

# 限价单（自动格式化价格和数量）
limit_order = api.place_limit_order(
    symbol="BTCUSDT",
    side="SELL",
    position_side="SHORT",  # PM 账户会自动转换为 BOTH
    quantity=Decimal('0.01'),
    price=Decimal('95000'),
    time_in_force='GTC'
)
```

#### 示例 4：精度处理工具

```python
from binance_trade_api import get_trade_api
from decimal import Decimal

api = get_trade_api()

# 获取 BTCUSDT 精度
tick_size, step_size = api.get_symbol_precision('BTCUSDT')
print(f"价格精度：{tick_size}, 数量精度：{step_size}")
# 输出：价格精度：0.1, 数量精度：0.001

# 格式化价格
price = Decimal('68131.567')
formatted_price = api.format_price(price, tick_size)
print(f"格式化后：{formatted_price}")
# 输出：68131.5

# 格式化数量（自动检查最小名义价值）
quantity = Decimal('0.001456')
formatted_qty = api.format_quantity(quantity, step_size, Decimal('100'), Decimal('68000'))
print(f"格式化后：{formatted_qty}")
# 输出：0.002（向上取整确保名义价值 >= 100 USDT）

# 自动格式化订单参数
price, qty = api.format_order_params('BTCUSDT', Decimal('68131.567'), Decimal('0.001456'))
print(f"格式化后：价格={price}, 数量={qty}")
```

#### 示例 5：持仓管理

```python
from binance_trade_api import get_trade_api

api = get_trade_api()

# 获取所有持仓
positions = api.get_all_positions()
for pos in positions:
    print(f"{pos['symbol']}: {pos['positionAmt']} @ {pos['entryPrice']}")
    print(f"  未实现盈亏：{pos['unRealizedProfit']} USDT")
    print(f"  保证金率：{pos['marginRatio']*100:.2f}%")

# 获取单个持仓
btc_position = api.get_position("BTCUSDT", "LONG")
if btc_position:
    pnl_rate = api.calculate_pnl_rate(
        btc_position, 
        current_price=Decimal('95000')
    )
    print(f"BTC 持仓盈亏率：{pnl_rate}%")
```

#### 示例 6：订单管理

```python
from binance_trade_api import get_trade_api

api = get_trade_api()

# 查询订单状态
order = api.get_order_status("BTCUSDT", order_id=12345678)
print(f"订单状态：{order['status']}")

# 撤销订单
cancel_result = api.cancel_order("BTCUSDT", order_id=12345678)
print(f"订单已撤销")

# 查询所有挂单
open_orders = api.get_all_open_orders()
print(f"当前挂单数：{len(open_orders)}")
```

#### 示例 7：账户资金管理

```python
from binance_trade_api import get_trade_api
from decimal import Decimal

api = get_trade_api()

# 查询现货余额
spot_balance = api.get_spot_balance('USDT')
print(f"现货 USDT 余额：{spot_balance}")

# 现货钱包转向合约钱包
transfer = api.transfer_spot_to_umfut(
    asset='USDT',
    amount=Decimal('100')
)
print(f"划转成功：{transfer['tranId']}")

# 查询赚币活期产品
products = api.get_simple_earn_flexible_list(asset='USDT', size=10)
for prod in products:
    print(f"{prod['productId']}: 年化 {prod['latestAnnualPercentageRate']}%")

# 赎回活期产品
redeem = api.redeem_simple_earn_flexible(
    product_id='USDT',
    redeem_all=True,
    dest_account='SPOT'
)
```

## PM 账户（统一账户）重要说明

### API 端点差异

| 功能 | PM 账户 | 传统合约账户 |
|------|--------|-------------|
| 账户信息 | `/papi/v1/account` | `/fapi/v2/account` |
| 下单 | `/papi/v1/um/order` | `/fapi/v1/order` |
| 持仓查询 | `/papi/v1/um/positionRisk` | `/fapi/v2/positionRisk` |

### 数据格式差异

**PM 账户返回（扁平结构）**:
```json
{
  "accountEquity": "142.09",
  "totalAvailableBalance": "142.09"
}
```

**传统账户返回（包含数组）**:
```json
{
  "availableBalance": "142.09",
  "assets": [
    {"asset": "USDT", "availableBalance": "142.09"}
  ]
}
```

### 订单精度要求

| 交易对 | 价格精度 | 数量精度 | 最小名义价值 |
|--------|---------|---------|-------------|
| BTCUSDT | 0.1 | 0.001 | 100 USDT |
| ETHUSDT | 0.1 | 0.001 | 100 USDT |
| BNBUSDT | 0.1 | 0.001 | 100 USDT |

**示例**:
```python
# 错误：精度不符合要求
price = Decimal('68131.567')  # 3 位小数
quantity = Decimal('0.001456')  # 6 位小数

# 正确：使用工具方法格式化
price, quantity = api.format_order_params('BTCUSDT', price, quantity)
# 结果：price=68131.5, quantity=0.002
```

### 仓位方向要求

**PM 账户（单向持仓模式）**:
- 必须使用 `positionSide: 'BOTH'`
- 不支持 `LONG` 和 `SHORT`

**传统账户（双向持仓模式）**:
- 可以使用 `positionSide: 'LONG'` 或 `'SHORT'`

**解决方案**: API 会自动处理，PM 账户强制使用 `BOTH`

## 依赖包

```txt
requests==2.31.0
pandas==2.2.1
numpy==1.26.4
tenacity==8.2.3
python-dotenv==1.0.0
```

## 安装脚本 (setup.py)

```python
#!/usr/bin/env python3
from setuptools import setup, find_packages

setup(
    name='binance-api-client',
    version='2.0.0',
    packages=find_packages(),
    install_requires=[
        'requests==2.31.0',
        'pandas==2.2.1',
        'numpy==1.26.4',
        'tenacity==8.2.3',
        'python-dotenv==1.0.0',
    ],
    author='Your Name',
    description='Binance Futures Trading API Client with PM Account Support',
    python_requires='>=3.8',
)
```

## 注意事项

1. **安全第一**：API Key 和 Secret Key 必须保存在 `.env` 文件中，不要提交到版本控制
2. **测试环境**：开发时建议使用测试网（设置 `BINANCE_TESTNET=true`）
3. **限流保护**：模块已内置限流器，但高频交易时仍需注意币安的频率限制
4. **资金管理**：生产环境务必设置合理的止损和仓位管理
5. **错误处理**：所有 API 调用都应该包含异常处理
6. **PM 账户**：使用 PM 账户时设置 `USE_UNIFIED_ACCOUNT=true`

## 错误处理

```python
from binance_trade_api import BinanceAPIError, InsufficientFundsError

try:
    order = api.place_market_order(...)
except BinanceAPIError as e:
    print(f"API 错误：{e.code} - {e.msg}")
except InsufficientFundsError:
    print("余额不足")
except Exception as e:
    print(f"未知错误：{e}")
```

## 常见问题

### 1. PM 账户订单创建失败（400 错误）

**错误**: "Order's position side does not match user's setting"

**原因**: PM 账户默认使用单向持仓模式（BOTH）

**解决**: API 已自动处理，确保使用最新版本的 `place_limit_order` 方法

### 2. 精度错误（400 错误）

**错误**: "Precision is over the maximum defined for this asset"

**原因**: 价格或数量精度不符合要求

**解决**: 使用 `format_order_params()` 方法自动格式化

### 3. 余额查询返回 0

**原因**: PM 账户和传统账户返回格式不同

**解决**: API 已自动适配两种格式

### 4. 最小名义价值错误

**错误**: "Order's notional must be greater than 100"

**原因**: 订单价值 < 100 USDT

**解决**: 使用 `format_quantity()` 方法，会自动调整数量

## 总结

此 Skill 提供了完整的币安合约交易 API 封装，包括：
- ✅ 行情数据获取
- ✅ 技术指标计算
- ✅ 交易下单功能（自动精度处理）
- ✅ 订单管理
- ✅ 持仓管理
- ✅ 账户资金管理
- ✅ API 限流保护
- ✅ 自动重试机制
- ✅ **PM 账户完全适配**
- ✅ **自动精度格式化**

可以在任何 Python 项目中快速集成，实现自动化交易功能。

---

**版本**: 2.0.0  
**更新日期**: 2026-03-23  
**更新内容**: 添加 PM 账户适配和自动精度处理功能
