# 币安 API Client - 快速使用指南

## 📦 模块位置

```
.trae/skills/binance-api-client/
├── SKILL.md              # 完整文档
└── package/              # 可复用包
    ├── binance_api.py
    ├── binance_trade_api.py
    ├── technical_indicators.py
    ├── rate_limiter.py
    ├── requirements.txt
    └── README.md
```

## 🚀 快速开始

### 1. 复制到新项目

```bash
# 方法 1: 直接复制
cp -r .trae/skills/binance-api-client/package /your/project/binance_client

# 方法 2: 使用相对路径
cd /your/project
mkdir -p lib
cp -r ../bianace_btcethbnb_trade/.trae/skills/binance-api-client/package lib/binance_client
```

### 2. 安装依赖

```bash
cd /your/project/binance_client
pip install -r requirements.txt
```

### 3. 配置环境变量

创建 `.env` 文件：

```env
# 币安 API 配置
BINANCE_API_KEY=your_api_key_here
BINANCE_SECRET_KEY=your_secret_key_here
BINANCE_API_BASE_URL=https://papi.binance.com
BINANCE_TESTNET=false
ENVIRONMENT=production
```

## 💡 常用示例

### 示例 1: 获取行情数据

```python
from binance_api import get_binance_futures_data

# 获取 BTC 数据
btc_data = get_binance_futures_data("BTCUSDT")
print(f"BTC 价格：${btc_data['lastPrice']}")
print(f"24h 涨跌幅：{btc_data['priceChangePercent']}%")
```

### 示例 2: 获取技术指标

```python
from technical_indicators import get_technical_indicators

# 获取完整技术指标
indicators = get_technical_indicators("BTCUSDT")

# 访问 1 小时数据
ema21 = indicators['1h']['ema21'][-1]
rsi = indicators['1h']['rsi'][-1]
print(f"EMA21: {ema21}, RSI: {rsi}")
```

### 示例 3: 交易下单

```python
from binance_trade_api import BinanceTradeAPI
from decimal import Decimal

# 初始化 API
api = BinanceTradeAPI()

# 市价开多
order = api.place_market_order(
    symbol="BTCUSDT",
    side="BUY",
    position_side="LONG",
    quantity=Decimal('0.01')
)
print(f"订单 ID: {order['orderId']}")
```

### 示例 4: 查询持仓

```python
from binance_trade_api import get_trade_api

api = get_trade_api()

# 获取所有持仓
positions = api.get_all_positions()
for pos in positions:
    print(f"{pos['symbol']}: {pos['positionAmt']} @ {pos['entryPrice']}")
    print(f"未实现盈亏：{pos['unRealizedProfit']} USDT")
```

### 示例 5: 设置止损止盈 ⭐

**重要**：必须使用条件单接口，不能使用普通订单接口！

```python
from binance_trade_api import get_trade_api
from decimal import Decimal

api = get_trade_api()

# 1. 先获取持仓数量
positions = api.get_position_risk("BTCUSDT")
position_qty = abs(Decimal(positions[0]['positionAmt']))

# 2. 设置止损（使用条件单接口）
stop_order = api.place_pm_conditional_order(
    symbol="BTCUSDT",
    side="SELL",                    # 多单平仓用 SELL
    position_side="BOTH",
    strategy_type="STOP_MARKET",    # 止损市单
    quantity=position_qty,          # 平仓数量
    stop_price=Decimal('90000'),
    reduce_only=True                # 只减仓
)

# 3. 设置止盈（使用条件单接口）
take_profit_order = api.place_pm_conditional_order(
    symbol="BTCUSDT",
    side="SELL",
    position_side="BOTH",
    strategy_type="TAKE_PROFIT_MARKET",  # 止盈市单
    quantity=position_qty,
    stop_price=Decimal('100000'),
    reduce_only=True
)
```

**❌ 错误做法**（会报错 Invalid orderType）：
```python
api.place_stop_market_order(...)         # 错误！这是普通订单接口
api.place_take_profit_market_order(...)  # 错误！
```

**✅ 正确做法**：
```python
api.place_pm_conditional_order(
    strategy_type="STOP_MARKET",  # 或 TAKE_PROFIT_MARKET
    ...
)
```

## 📋 API 快速参考

### 行情 API (binance_api.py)

```python
get_binance_futures_data(symbol)              # 获取单个币种数据
get_multiple_symbols_data(symbols)            # 获取多个币种数据
save_api_data(data, filename)                 # 保存数据到文件
```

### 交易 API (binance_trade_api.py)

```python
# 初始化
api = BinanceTradeAPI()
api = get_trade_api()  # 获取全局实例

# 账户管理
api.get_umfut_balance(asset)                  # 获取合约账户余额
api.get_spot_balance(asset)                   # 获取现货余额
api.get_account_info()                        # 获取账户信息
api.set_um_leverage(symbol, leverage)         # 设置杠杆

# 下单交易
api.place_market_order(...)                   # 市价单
api.place_limit_order(...)                    # 限价单
api.place_stop_market_order(...)              # 止损市价单
api.place_take_profit_market_order(...)       # 止盈市价单

# 订单管理
api.cancel_order(symbol, order_id)            # 撤销订单
api.get_order_status(symbol, order_id)        # 查询订单状态
api.get_all_open_orders()                     # 查询所有挂单

# 持仓管理
api.get_all_positions()                       # 获取所有持仓
api.get_position(symbol, position_side)       # 获取指定持仓
api.calculate_pnl_rate(position, price)       # 计算盈亏率

# 资金管理
api.transfer_spot_to_umfut(asset, amount)     # 现货转合约
api.transfer_umfut_to_spot(asset, amount)     # 合约转现货
api.get_simple_earn_flexible_list(...)        # 理财产品查询
api.redeem_simple_earn_flexible(...)          # 理财赎回
```

### 技术指标 (technical_indicators.py)

```python
get_technical_indicators(symbol)              # 获取完整技术指标
get_binance_klines(symbol, interval, limit)   # 获取 K 线数据
calculate_ema(prices, period)                 # 计算 EMA
calculate_rsi(prices, period)                 # 计算 RSI
calculate_atr(highs, lows, closes, period)    # 计算 ATR
calculate_macd(prices, ...)                   # 计算 MACD
calculate_bollinger_bands(prices, ...)        # 计算布林带
```

### 限流器 (rate_limiter.py)

```python
limiter = get_rate_limiter()                  # 获取全局限流器
limiter.acquire(endpoint)                     # 获取请求许可
status = limiter.get_status()                 # 获取限流状态
```

## ⚙️ 配置选项

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| BINANCE_API_KEY | API 密钥 | 必填 |
| BINANCE_SECRET_KEY | Secret 密钥 | 必填 |
| BINANCE_API_BASE_URL | API 基础 URL | https://papi.binance.com |
| BINANCE_TESTNET | 是否使用测试网 | false |
| ENVIRONMENT | 环境 (development/production) | development |

## 🔒 安全提示

1. **不要提交 API 密钥到版本控制**
   ```bash
   # .gitignore
   .env
   *.key
   *.secret
   ```

2. **使用测试网开发**
   ```env
   BINANCE_TESTNET=true
   ```

3. **设置合理的权限**
   ```bash
   chmod 600 .env
   ```

## 📊 完整示例

```python
#!/usr/bin/env python3
"""
简单交易机器人示例
"""

from binance_trade_api import get_trade_api
from technical_indicators import get_technical_indicators
from decimal import Decimal
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SimpleBot:
    def __init__(self):
        self.api = get_trade_api()
    
    def run(self):
        # 获取技术指标
        indicators = get_technical_indicators("BTCUSDT")
        
        # 简单策略：RSI < 30 开多
        rsi = indicators['1h']['rsi'][-1]
        if rsi < 30:
            logger.info(f"RSI={rsi:.2f} < 30, 开多信号")
            
            # 获取价格
            price = self.api.get_ticker_price("BTCUSDT")
            
            # 计算数量 (30U * 20 倍杠杆)
            quantity = (Decimal('30') * Decimal('20')) / price
            
            # 设置杠杆
            self.api.set_um_leverage("BTCUSDT", 20)
            
            # 市价开多
            order = self.api.place_market_order(
                symbol="BTCUSDT",
                side="BUY",
                position_side="LONG",
                quantity=quantity
            )
            
            logger.info(f"开多成功：{order['orderId']}")
            
            # 设置止损 (-2%)
            stop_price = price * Decimal('0.98')
            self.api.place_stop_market_order(
                symbol="BTCUSDT",
                side="SELL",
                position_side="LONG",
                quantity=quantity,
                stop_price=stop_price
            )
            
            logger.info(f"止损已设置：{stop_price}")

if __name__ == '__main__':
    bot = SimpleBot()
    bot.run()
```

## 📚 更多文档

详细文档请查看：
- **完整使用文档**: `.trae/skills/binance-api-client/SKILL.md`
- **包说明**: `.trae/skills/binance-api-client/package/README.md`

## ❓ 常见问题

### Q: 如何在其他项目中使用？
A: 复制 `package` 目录到目标项目，安装依赖并配置 .env 即可。

### Q: 测试网如何配置？
A: 设置 `BINANCE_TESTNET=true` 即可使用测试网。

### Q: 如何调试？
A: 设置 `ENVIRONMENT=development`，交易功能将返回模拟数据。

### Q: 支持哪些交易对？
A: 支持所有币安 USDT 合约交易对，如 BTCUSDT, ETHUSDT, BNBUSDT 等。

---

**版本**: v1.1  
**更新时间**: 2026-03-23  
**更新内容**: 添加止损止盈条件单设置指南
