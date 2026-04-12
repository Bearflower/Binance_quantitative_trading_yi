# 币安 API 快速参考卡片

## 精度要求速查

| 交易对 | 价格精度 | 数量精度 | 最小名义价值 | 备注 |
|--------|---------|---------|-------------|------|
| BTCUSDT | 0.1 (1 位小数) | 0.001 (3 位) | 100 USDT | |
| ETHUSDT | 0.1 (1 位小数) | 0.001 (3 位) | 100 USDT | |
| BNBUSDT | 0.1 (1 位小数) | **0.01 (2 位)** | 100 USDT | **注意！** |

## 频率限制速查

| 限制类型 | 限制值 | 安全阈值 | 建议延迟 |
|---------|--------|---------|---------|
| 订单频率 | 2 订单/秒 | 1 订单/秒 | 1 秒/单 |
| 短期频率 | 10 订单/3 秒 | 6 订单/3 秒 | 0.5 秒/操作 |
| 长期频率 | 300 订单/15 分钟 | 200 订单/15 分钟 | - |

## 常见错误码速查

| 错误码 | 错误信息 | 原因 | 解决方案 |
|--------|---------|------|---------|
| **-1015** | Too many new orders | 下单太频繁 | 添加延迟控制 (1 秒/单) |
| **-1111** | Precision is over the maximum | 精度不符合要求 | 使用 `format_order_params()` |
| **-1015** | Order's notional must be > 100 | 订单价值 < 100 USDT | 使用 `format_quantity()` |
| **-2019** | Margin is insufficient | 保证金不足 | 检查余额或降低仓位 |
| **-4057** | Position side does not match | PM 账户使用了 LONG/SHORT | 使用 `BOTH` |

## 代码片段速查

### 1. 格式化订单参数

```python
from binance_trade_api import get_trade_api
from decimal import Decimal

api = get_trade_api()

# 自动格式化价格和数量
price = Decimal('68131.567')
quantity = Decimal('0.001456')

# ✅ 正确：使用工具方法
price, quantity = api.format_order_params('BTCUSDT', price, quantity)
# 结果：price=68131.5, quantity=0.002
```

### 2. 添加延迟控制

```python
import time

for i, signal in enumerate(signals):
    # 信号之间延迟 1 秒
    if i > 0:
        time.sleep(1)
    
    # 设置杠杆
    api.set_um_leverage(symbol, leverage=5)
    time.sleep(0.5)  # 延迟 0.5 秒
    
    # 开仓
    api.place_um_order(**params)
    time.sleep(0.5)  # 延迟 0.5 秒
    
    # 设置止损止盈
    api.place_pm_conditional_order(**stop_loss)
    time.sleep(0.3)  # 延迟 0.3 秒
```

### 3. 带重试的下单

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def place_order_with_retry(symbol, params):
    """带重试的下单方法"""
    return api.place_um_order(**params)

# 使用
try:
    order = place_order_with_retry('BTCUSDT', params)
except Exception as e:
    print(f"下单失败：{str(e)}")
```

### 4. 精度验证和修正

```python
def get_symbol_precision(symbol: str) -> tuple:
    """获取精度并智能验证"""
    # 获取 API 返回的精度
    tick_size, step_size = api.get_symbol_precision(symbol)
    
    # 验证和修正
    if symbol.startswith('BNB'):
        if step_size < Decimal('0.01'):
            logger.warning(f"{symbol} 精度修正：{step_size} -> 0.01")
            step_size = Decimal('0.01')
    
    return tick_size, step_size
```

## 执行时间估算

**执行 3 个信号的总时间**：

| 操作 | 数量 | 延迟 | 总时间 |
|------|------|------|--------|
| 信号间隔 | 2 次 | 1 秒 | 2 秒 |
| 设置杠杆 | 3 次 | 0.5 秒 | 1.5 秒 |
| 开仓 | 3 次 | 0.5 秒 | 1.5 秒 |
| 止损设置 | 3 次 | 0.3 秒 | 0.9 秒 |
| 止盈设置 | 6 次 | 0.3 秒 | 1.8 秒 |
| **总计** | - | - | **约 7.7 秒** |

**之前（无延迟）**：< 1 秒 → 经常触发限流 ❌  
**现在（有延迟）**：约 8 秒 → 从不触发限流 ✅

## 监控检查清单

### 每日检查

- [ ] API 连接状态
- [ ] 账户余额（> 100 USDT）
- [ ] 持仓保证金率（< 80%）
- [ ] API 限流使用情况（< 80%）
- [ ] 日志中的错误记录

### 每周检查

- [ ] 精度数据验证
- [ ] 延迟控制效果
- [ ] 重试机制触发次数
- [ ] 订单成功率（目标 > 95%）
- [ ] 系统性能优化

## 故障排查流程图

```
订单失败
  ├─ 错误码 -1015 (Too many new orders)
  │   └─ 添加延迟控制 → 重试
  │
  ├─ 错误码 -1111 (Precision error)
  │   └─ 使用 format_order_params() → 重试
  │
  ├─ 错误码 -2019 (Margin insufficient)
  │   └─ 检查余额 → 降低仓位 → 划转资金
  │
  └─ 错误码 -4057 (Position side error)
      └─ 使用 BOTH → 重试
```

## 最佳实践口诀

```
精度验证不能忘，BNB 两位别搞错
下单之前加延迟，一秒一个最稳妥
杠杆开仓各半秒，止损止盈零点三
重试机制要带上，错误分类处理好
日志记录要详细，监控告警不能少
PM 账户用 BOTH，最小名义一百刀
```

---

**版本**: 1.0.0  
**更新日期**: 2026-04-08  
**快速参考**: 打印此卡片贴在工位随时查阅！
