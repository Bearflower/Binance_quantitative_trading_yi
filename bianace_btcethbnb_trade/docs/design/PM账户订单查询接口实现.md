# PM 账户订单查询接口 - 实现说明

## 一、PM 账户可用接口

根据您的币安接口文档，PM 账户可以使用的订单查询接口：

### ✅ 普通订单接口

| 接口 | 端点 | 用途 | 已实现 |
|------|------|------|--------|
| 查询 UM 订单 | `GET /papi/v1/um/order` | 查询单个订单状态 | ✅ `get_order_status()` |
| 查询所有 UM 订单 | `GET /papi/v1/um/allOrders` | 查询历史订单（包括已平仓） | ✅ `get_um_order_history()` |

### ✅ 条件单接口

| 接口 | 端点 | 用途 | 已实现 |
|------|------|------|--------|
| 查询条件单 | `GET /papi/v1/um/conditional/order` | 查询单个条件单 | ⬜ 暂不需要 |
| 查询所有条件订单 | `GET /papi/v1/um/conditional/allOrders` | 查询条件单历史 | ✅ `get_pm_conditional_order_history()` |

## 二、新增接口实现

### 2.1 查询历史订单

```python
def get_um_order_history(self, symbol: str, limit: int = 100,
                        start_time: int = None,
                        end_time: int = None) -> List[Dict[str, Any]]:
    """
    查询所有 UM 订单（包括历史订单）
    端点：GET /papi/v1/um/allOrders
    
    参数:
        symbol: 交易对
        limit: 返回数量限制，默认 100，最大 1000
        start_time: 开始时间戳（毫秒）
        end_time: 结束时间戳（毫秒）
    
    返回:
        订单列表
    """
```

**特点**:
- ✅ 支持时间范围查询（最大 7 天）
- ✅ 返回已成交、已取消、已过期的订单
- ✅ 用于平仓检测模块

### 2.2 查询条件单历史

```python
def get_pm_conditional_order_history(self, symbol: str = None,
                                    limit: int = 100,
                                    start_time: int = None,
                                    end_time: int = None) -> List[Dict[str, Any]]:
    """
    查询 UM 所有条件订单（包括历史订单）
    端点：GET /papi/v1/um/conditional/allOrders
    
    参数:
        symbol: 交易对（可选）
        limit: 返回数量限制
        start_time: 开始时间戳
        end_time: 结束时间戳
    
    返回:
        条件单列表
    """
```

**特点**:
- ✅ 支持查询所有交易对的条件单
- ✅ 返回已触发、已取消的条件单
- ✅ 用于止盈止损触发检测

## 三、接口使用示例

### 3.1 查询最近 7 天的历史订单

```python
from utils.binance_trade_api import get_trade_api
from datetime import datetime, timedelta

api = get_trade_api()

# 计算时间范围
end_time = int(datetime.now().timestamp() * 1000)
start_time = int((datetime.now() - timedelta(days=7)).timestamp() * 1000)

# 查询 BTCUSDT 历史订单
orders = api.get_um_order_history(
    symbol='BTCUSDT',
    limit=500,
    start_time=start_time,
    end_time=end_time
)

# 过滤已完成的订单
filled_orders = [o for o in orders if o['status'] == 'FILLED']
print(f"查询到 {len(filled_orders)} 笔已完成订单")
```

### 3.2 查询条件单历史

```python
# 查询所有交易对的条件单历史
conditional_orders = api.get_pm_conditional_order_history(
    limit=100,
    start_time=start_time,
    end_time=end_time
)

# 过滤已触发的条件单
triggered_orders = [o for o in conditional_orders if o['strategyStatus'] == 'TRIGGERED']
print(f"查询到 {len(triggered_orders)} 笔已触发条件单")
```

### 3.3 平仓检测应用

```python
from services.close_detector import get_close_detector

detector = get_close_detector()

# 自动从币安 API 获取最近 7 天的已完成订单
# 并检测哪些订单已平仓
closed_positions = detector.detect_closed_positions()

for close in closed_positions:
    print(f"订单 {close['order_id']}: "
          f"盈亏={close['net_pnl']} USDT, "
          f"收益率={close['pnl_rate']}%")
```

## 四、接口限制说明

### 4.1 时间范围限制

- **历史订单**: 查询时间范围最大不得超过 7 天
- **条件单历史**: 查询时间范围最大不得超过 7 天
- **默认**: 查询最近 7 天内的数据

### 4.2 订单查询限制

以下订单**无法查询到**：
- 订单状态为 CANCELED 或 EXPIRED
- **且** 订单没有任何成交记录
- **且** 订单生成时间 + 3 天 < 当前时间

### 4.3 权重限制

| 接口 | 权重 | 说明 |
|------|------|------|
| GET /papi/v1/um/allOrders | 5 | 每 5 个权重单位 |
| GET /papi/v1/um/conditional/allOrders | 1 (带 symbol) / 40 (不带) | 建议指定 symbol |

## 五、已更新的文件

### 5.1 API 接口

**文件**: `utils/binance_trade_api.py`

**新增方法**:
- ✅ `get_um_order_history()` - 查询历史订单
- ✅ `get_pm_conditional_order_history()` - 查询条件单历史

### 5.2 平仓检测

**文件**: `services/close_detector.py`

**更新内容**:
- ✅ `_get_filled_trades()` 方法现在优先从币安 API 获取数据
- ✅ 支持查询多个交易对（BTC/ETH/BNB）
- ✅ 降级方案：API 失败时使用数据库数据

## 六、数据流程

```
监控系统定时任务（每 15 分钟）
    ↓
平仓检测模块
    ↓
1. 调用 get_um_order_history() 查询最近 7 天历史订单
2. 过滤状态为 FILLED 的订单
3. 对比数据库中的平仓记录
4. 检测新平仓订单
    ↓
计算盈亏并保存到 closed_positions 表
    ↓
更新统计数据到 trade_statistics 表
    ↓
每周日发送周报
```

## 七、优势说明

### ✅ 使用币安 API 的优势

1. **数据准确性高**
   - 直接从币安获取订单数据
   - 避免本地数据丢失或不一致

2. **支持历史查询**
   - 可以查询最近 7 天的所有订单
   - 包括已成交、已取消、已过期的订单

3. **条件单支持**
   - 可以查询止盈止损条件单的触发记录
   - 精确记录止盈止损触发情况

### ✅ 降级方案

- 如果 API 调用失败，自动降级到数据库查询
- 保证系统的高可用性

## 八、测试验证

### 8.1 测试 API 接口

```bash
python3 -c "
from utils.binance_trade_api import get_trade_api
from datetime import datetime, timedelta

api = get_trade_api()

# 计算时间范围
end_time = int(datetime.now().timestamp() * 1000)
start_time = int((datetime.now() - timedelta(days=7)).timestamp() * 1000)

# 测试历史订单查询
orders = api.get_um_order_history(
    symbol='BTCUSDT',
    limit=10,
    start_time=start_time,
    end_time=end_time
)

print(f'查询到 {len(orders)} 个订单')
for order in orders[:5]:
    print(f'订单{order[\"orderId\"]}: {order[\"status\"]} - {order[\"avgPrice\"]}')

# 测试条件单历史
conditional = api.get_pm_conditional_order_history(
    symbol='ETHUSDT',
    limit=10
)

print(f'\n查询到 {len(conditional)} 个条件单')
for cond in conditional[:5]:
    print(f'策略{cond[\"strategyId\"]}: {cond[\"strategyStatus\"]}')
"
```

### 8.2 预期输出

```
查询到 10 个订单
订单 123456: FILLED - 68000
订单 123457: FILLED - 68100
...

查询到 5 个条件单
策略 81786402: TRIGGERED
策略 81786403: TRIGGERED
...
```

## 九、总结

### ✅ 已实现的功能

1. **历史订单查询** - 支持查询最近 7 天的所有订单
2. **条件单历史查询** - 支持查询所有条件单历史
3. **平仓检测增强** - 优先使用币安 API 数据
4. **降级方案** - API 失败时自动使用数据库数据

### 📊 满足的需求

- ✅ 可以使用 `#查询 UM 订单` 接口查询单个订单
- ✅ 可以使用 `#查询所有 UM 订单` 接口查询历史订单
- ✅ 可以使用 `#查询 UM 所有条件订单` 接口查询条件单历史
- ✅ 完全满足 PM 账户的订单查询需求

---

**实现完成时间**: 2026-03-26  
**状态**: ✅ 已完成并测试
