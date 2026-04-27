# BTC/ETH 交易系统 - 更新日志

## 2026-04-21 - 接入通用 K 线服务

### 🎯 更新目标
将 BTC/ETH 交易系统的 K 线数据源从直接调用币安 API 改为使用通用 K 线服务。

### ✅ 完成的改动

#### 修改 `utils/technical_indicators.py`

**改动内容**:
- 修改 `get_binance_klines()` 函数，从通用 K 线服务获取数据
- 更新 API 路径和响应格式解析

**代码改动**:
```python
# 新增配置
KLINE_SERVICE_URL = "http://43.156.242.184:8765/api/v1"

# 修改获取 K 线方法
def get_binance_klines(symbol="BTCUSDT", interval="1h", limit=100):
    """从通用 K 线服务获取 Binance K 线数据"""
    url = f"{KLINE_SERVICE_URL}/klines/latest?symbol={symbol}&interval={interval}&limit={limit}"
    
    response = requests.get(url, timeout=10)
    if response.status_code == 200:
        result = response.json()
        if result.get('code') == 0:
            return result['data']  # 直接返回 K 线数组
    return None
```

### 📊 测试结果

**测试脚本**: `test_kline_service_v2.py`

**测试结果**:
```bash
测试 1: 获取 BTCUSDT 1 小时 K 线 (limit=5)
✅ 成功获取 5 条 K 线数据
最新 K 线：{'open_time': ..., 'open_price': ..., ...}

测试 2: 获取 ETHUSDT 15 分钟 K 线 (limit=5)
✅ 成功获取 5 条 K 线数据

测试 3: 获取 BNBUSDT 日线 K 线 (limit=5)
✅ 成功获取 5 条 K 线数据
```

### 🔧 配置说明

**通用 K 线服务地址**:
```python
KLINE_SERVICE_URL = "http://43.156.242.184:8765/api/v1"
```

**API 端点**:
- `/klines/latest` - 获取最新 K 线数据
- 参数：`symbol`, `interval`, `limit`

**响应格式**:
```json
{
  "code": 0,
  "message": "success",
  "data": [
    {
      "symbol": "BTCUSDT",
      "interval": "1h",
      "open_time": 1776744000000,
      "open_price": 75710.3,
      "high_price": 75800.0,
      "low_price": 75600.0,
      "close_price": 75750.5,
      "volume": 123.45,
      "close_time": 1776747599999,
      "quote_volume": 9345678.90,
      "trade_count": 1234
    }
  ]
}
```

### 📝 使用说明

#### 获取 K 线数据
```python
from utils.technical_indicators import get_binance_klines

# 获取 BTCUSDT 1 小时 K 线
klines = get_binance_klines("BTCUSDT", "1h", limit=100)

# 获取 ETHUSDT 15 分钟 K 线
klines = get_binance_klines("ETHUSDT", "15m", limit=50)
```

#### 计算技术指标
```python
import pandas as pd
from utils.technical_indicators import add_technical_indicators

# 转换 K 线数据为 DataFrame
df = pd.DataFrame(klines)

# 添加技术指标
df = add_technical_indicators(df)

# 现在可以使用 EMA、RSI、MACD 等指标
print(df.columns)
```

### ⚠️ 注意事项

1. **字段名变化**:
   - 旧格式：`open`, `close`, `high`, `low`
   - 新格式：`open_price`, `close_price`, `high_price`, `low_price`
   
2. **数据源优先级**:
   - 优先从通用 K 线服务获取
   - 通用服务不可用时，代码中暂无降级方案（可根据需要添加）

3. **网络依赖**:
   - 依赖服务器 43.156.242.184:8765 端口可访问
   - 确保服务器安全组已开放 8765 端口

### 🎯 性能对比

**直接调用币安 API**:
- 每次请求都需要调用外部 API
- 受网络延迟影响
- 可能触发 API 限流

**使用通用 K 线服务**:
- 数据已预先采集和存储
- 响应速度快
- 无 API 限流问题
- 支持多系统共享数据

### 📚 相关文档

- [通用 K 线服务 API 文档](../../common_service/kline_data_service/README.md)
- [技术指标计算文档](./technical_indicators.md)

---

**更新日期**: 2026-04-21  
**更新人**: AI Assistant  
**影响范围**: K 线数据获取、技术指标计算
