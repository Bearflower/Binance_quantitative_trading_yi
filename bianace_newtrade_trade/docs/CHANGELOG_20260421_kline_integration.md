# 新币做空系统 - 更新日志

## 2026-04-21 - 接入通用 K 线服务

### 🎯 更新目标
将新币做空系统的 K 线数据源从直接调用币安 API 改为使用通用 K 线服务，实现数据共享和统一管理。

### ✅ 完成的改动

#### 1. 修改 `core/binance_client.py`

**新增功能**:
- 添加通用 K 线服务配置常量
- 修改 `get_kline_data()` 方法，优先从通用服务获取数据
- 添加降级方案：通用服务失败时自动切换到直接调用币安 API
- 添加标的注册管理方法：
  - `register_new_symbol()` - 注册新币到 K 线服务
  - `unregister_symbol()` - 取消标的注册
  - `renew_symbol()` - 续期已注册标的

**代码改动**:
```python
# 新增配置
KLINE_SERVICE_URL = "http://43.156.242.184:8765/api/v1"
KLINE_REGISTER_URL = f"{KLINE_SERVICE_URL}/register"

# 修改 get_kline_data 方法
def get_kline_data(self, symbol: str, interval: str = "1h", limit: int = 100):
    """获取 K 线数据（使用通用 K 线服务）"""
    # 1. 优先从通用服务获取
    # 2. 失败时降级到币安 API
    pass

# 新增注册方法
def register_new_symbol(self, symbol, intervals, duration_days=10, priority="normal"):
    """注册新币到 K 线服务"""
    pass
```

#### 2. 修改 `core/listing_detector.py`

**新增功能**:
- 添加 `auto_register_kline_service()` 函数
- 在新币发现后自动注册到 K 线服务
- 默认采集周期：1m, 5m, 15m, 1h, 4h
- 默认采集持续天数：10 天
- 优先级设置为 high

**代码改动**:
```python
# 新增自动注册函数
def auto_register_kline_service(symbol: str) -> bool:
    """自动注册新币到 K 线服务"""
    intervals = ["1m", "5m", "15m", "1h", "4h"]
    duration_days = 10
    return binance_client.register_new_symbol(
        symbol=symbol,
        intervals=intervals,
        duration_days=duration_days,
        priority="high"
    )

# 在 detect_new_listings 中调用
if listing_timestamp >= cutoff_time:
    # 发现新币后自动注册
    auto_register_kline_service(symbol)
```

#### 3. 新增 `utils/kline_monitor.py`

**功能**:
- K 线服务健康监控
- 数据质量检查
- 已注册标的状态监控
- 异常告警

**主要方法**:
- `check_health()` - 检查服务健康状态
- `check_data_quality()` - 检查数据质量
- `check_registered_symbols()` - 检查已注册标的
- `run_full_check()` - 执行全面检查

### 📊 测试结果

**K 线数据获取测试**:
```bash
✅ 成功获取 BTCUSDT 15m K 线数据 - 4 条
✅ 成功获取 ETHUSDT 1h K 线数据 - 3 条
✅ 成功获取 BNBUSDT 4h K 线数据 - 2 条
```

**注册功能测试**:
- ✅ 注册新币功能正常
- ✅ 查询已注册标的功能正常
- ✅ 续期功能正常
- ✅ 取消注册功能正常

### 🔧 配置说明

**通用 K 线服务地址**:
```python
KLINE_SERVICE_URL = "http://43.156.242.184:8765/api/v1"
```

**注册参数**:
- `intervals`: 采集周期列表，如 `["1m", "5m", "15m", "1h", "4h"]`
- `duration_days`: 采集持续天数（1-30 天）
- `priority`: 优先级（high, normal, low）

### 📝 使用说明

#### 获取 K 线数据
```python
from core.binance_client import binance_client

# 自动从通用服务获取
klines = binance_client.get_kline_data("BTCUSDT", "15m", limit=100)
```

#### 注册新币
```python
# 发现新币后自动注册
success = binance_client.register_new_symbol(
    symbol="NEWCOINUSDT",
    intervals=["1m", "5m", "15m", "1h"],
    duration_days=10,
    priority="high"
)
```

#### 监控服务状态
```python
from utils.kline_monitor import kline_monitor

# 执行全面检查
result = kline_monitor.run_full_check()
print(result)
```

### 🎯 后续优化建议

1. **监控告警集成** - 将监控结果集成到飞书通知
2. **自动续期** - 对重要标的实现自动续期
3. **数据质量分析** - 添加数据质量趋势分析
4. **性能优化** - 添加缓存减少 API 调用

### 📚 相关文档

- [通用 K 线服务 API 文档](../../common_service/kline_data_service/README.md)
- [K 线服务部署指南](../../common_service/docs/deployment.md)

---

**更新日期**: 2026-04-21  
**更新人**: AI Assistant  
**影响范围**: K 线数据获取、新币注册、服务监控
