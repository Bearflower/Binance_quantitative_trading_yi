# Binance API Client Package

币安合约交易 API 客户端模块 - 可复用的交易接口封装

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

创建 `.env` 文件：

```env
BINANCE_API_KEY=your_api_key_here
BINANCE_SECRET_KEY=your_secret_key_here
BINANCE_API_BASE_URL=https://papi.binance.com
BINANCE_TESTNET=false
ENVIRONMENT=production
```

### 3. 使用示例

```python
from binance_trade_api import BinanceTradeAPI, get_trade_api
from technical_indicators import get_technical_indicators
from decimal import Decimal

# 初始化 API
api = get_trade_api()

# 获取技术指标
indicators = get_technical_indicators("BTCUSDT")

# 下单交易
order = api.place_market_order(
    symbol="BTCUSDT",
    side="BUY",
    position_side="LONG",
    quantity=Decimal('0.01')
)
```

## 文件说明

- `binance_api.py` - 基础行情数据 API
- `binance_trade_api.py` - 完整交易 API 封装
- `technical_indicators.py` - 技术指标计算
- `rate_limiter.py` - API 限流器

## 完整文档

详细使用文档请参考上层的 SKILL.md 文件。
