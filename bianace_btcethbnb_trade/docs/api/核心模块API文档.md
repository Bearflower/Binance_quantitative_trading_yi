# 核心模块 API 文档

## 目录

1. [仓位计算模块](#仓位计算模块)
2. [风险管理模块](#风险管理模块)
3. [订单生成模块](#订单生成模块)
4. [信号检测模块](#信号检测模块)
5. [应急处理模块](#应急处理模块)
6. [数据缓存模块](#数据缓存模块)
7. [评分引擎模块](#评分引擎模块)

---

## 仓位计算模块

**模块路径**: `core.position_calculator`

### 主要类

#### `PositionCalculator`

仓位计算器类，负责计算交易仓位参数。

**初始化参数**:
- `params`: StrategyParams - 策略参数对象（可选）

**主要方法**:

##### `calculate_position(symbol, entry_price, stop_loss_price, direction, signal_grade='A')`

计算仓位参数（核心功能）。

**参数**:
- `symbol` (str): 交易对
- `entry_price` (Decimal): 开仓价
- `stop_loss_price` (Decimal): 止损价
- `direction` (int): 方向（1=多，-1=空）
- `signal_grade` (str): 信号等级（S/A/B，默认A）

**返回值**:
```python
{
    'symbol': str,                    # 交易对
    'entry_price': Decimal,           # 开仓价
    'stop_loss_price': Decimal,       # 止损价
    'stop_loss_pct': Decimal,         # 止损百分比
    'direction': int,                 # 方向
    'signal_grade': str,              # 信号等级
    'base_notional_value': Decimal,   # 基础名义价值（U）
    'actual_notional_value': Decimal, # 实际名义价值（U）
    'position_coefficient': Decimal,  # 仓位系数
    'quantity': Decimal,              # 合约数量
    'margin': Decimal,                # 保证金（U）
    'leverage': int,                  # 实际使用杠杆
    'risk_amount': Decimal,           # 风险金额（U）
    'risk_ratio': Decimal             # 风险占比（%）
}
```

**示例**:
```python
from core.position_calculator import calculate_position
from decimal import Decimal

position = calculate_position(
    symbol='BTCUSDT',
    entry_price=Decimal('95000'),
    stop_loss_price=Decimal('93000'),
    direction=1,
    signal_grade='A'
)

print(f"保证金: {position['margin']}U")
print(f"杠杆: {position['leverage']}x")
print(f"合约数量: {position['quantity']}")
```

---

## 风险管理模块

**模块路径**: `core.risk_manager`

### 主要类

#### `RiskManager`

风险管理器类，负责止损止盈计算和风险监控。

**主要方法**:

##### `calculate_stop_loss(entry_price, direction, stop_loss_pct)`

计算止损价。

**参数**:
- `entry_price` (Decimal): 开仓价
- `direction` (int): 方向（1=多，-1=空）
- `stop_loss_pct` (Decimal): 止损幅度（百分比）

**返回值**: Decimal - 止损价

**示例**:
```python
from core.risk_manager import calculate_stop_loss
from decimal import Decimal

stop_loss = calculate_stop_loss(
    entry_price=Decimal('95000'),
    direction=1,
    stop_loss_pct=Decimal('0.02')
)
# 输出: 93100.00
```

##### `calculate_take_profit_levels(entry_price, direction, atr14, signal_grade='A')`

计算止盈水平。

**参数**:
- `entry_price` (Decimal): 开仓价
- `direction` (int): 方向（1=多，-1=空）
- `atr14` (Decimal): ATR14值
- `signal_grade` (str): 信号等级（默认A）

**返回值**:
```python
[
    {
        'level': 'TP1',
        'price': Decimal,      # TP1价格
        'ratio': Decimal,      # 平仓比例
        'description': str,    # 描述
        'multiplier': Decimal  # ATR倍数
    },
    # TP2, TP3...
]
```

**示例**:
```python
from core.risk_manager import calculate_take_profit_levels
from decimal import Decimal

tp_levels = calculate_take_profit_levels(
    entry_price=Decimal('95000'),
    direction=1,
    r_value=Decimal('500'),  # ATR14值
    signal_grade='A'
)

for tp in tp_levels:
    print(f"{tp['level']}: {tp['price']} ({tp['description']})")
```

##### `check_margin_ratio(account_equity, used_margin)`

检查保证金率。

**参数**:
- `account_equity` (Decimal): 账户权益
- `used_margin` (Decimal): 占用保证金

**返回值**: `(margin_ratio, risk_level, need_intervention)`
- `margin_ratio` (Decimal): 保证金率
- `risk_level` (str): 风险等级（SAFE/WARNING/EMERGENCY）
- `need_intervention` (bool): 是否需要干预

---

## 订单生成模块

**模块路径**: `core.order_generator`

### 主要函数

#### `generate_order_template(symbol, direction, entry_price, stop_loss_price, signal_grade, position_data)`

生成订单模板。

**参数**:
- `symbol` (str): 交易对
- `direction` (int): 方向（1=多，-1=空）
- `entry_price` (Decimal): 开仓价
- `stop_loss_price` (Decimal): 止损价
- `signal_grade` (str): 信号等级
- `position_data` (dict): 仓位数据

**返回值**:
```python
{
    'symbol': str,
    'direction': str,           # 'LONG' 或 'SHORT'
    'entry_price': Decimal,
    'stop_loss_price': Decimal,
    'take_profit_levels': list,
    'leverage': int,
    'quantity': Decimal,
    'margin': Decimal
}
```

---

## 信号检测模块

**模块路径**: `core.signal_detector`

### 主要类

#### `SignalDetector`

信号检测器类，负责检测交易信号。

**主要方法**:

##### `detect_signals(symbols=None)`

检测交易信号。

**参数**:
- `symbols` (list): 交易对列表，默认 ['BTCUSDT', 'ETHUSDT', 'BNBUSDT']

**返回值**:
```python
[
    {
        '币种': str,
        '开仓方向': str,      # '多' 或 '空'
        '信号等级': str,      # 'S', 'A', 'B'
        '开仓价': Decimal,
        '止损价': Decimal,
        # ... 其他字段
    }
]
```

---

## 应急处理模块

**模块路径**: `core.emergency_handler`

### 主要函数

#### `check_extreme_market(symbol, price_change_percent)`

检查极端市场。

**参数**:
- `symbol` (str): 交易对
- `price_change_percent` (Decimal): 24小时涨跌幅

**返回值**: bool - 是否为极端行情

### 主要类

#### `EmergencyHandler`

应急处理器类。

**主要方法**:

##### `is_trading_allowed()`

检查是否允许交易。

**返回值**: `(allowed, reason)`
- `allowed` (bool): 是否允许
- `reason` (str): 原因

##### `check_daily_loss(daily_loss)`

检查单日亏损。

**参数**:
- `daily_loss` (Decimal): 单日亏损金额

---

## 数据缓存模块

**模块路径**: `core.data.cache`

### 主要类

#### `DataCache`

数据缓存管理类。

**初始化参数**:
- `maxsize` (int): 最大缓存条目数（默认100）
- `ttl_seconds` (int): 缓存过期时间（秒，默认300）
- `enable_stats` (bool): 是否启用统计（默认True）

**主要方法**:

##### `set(symbol, data)`

设置缓存数据。

##### `get(symbol)`

获取缓存数据。

##### `has_symbol(symbol)`

检查是否包含指定交易对。

##### `clear()`

清除所有缓存。

##### `get_stats()`

获取缓存统计信息。

**示例**:
```python
from core.data import DataCache

# 创建缓存
cache = DataCache(maxsize=100, ttl_seconds=300)

# 设置数据
cache.set('BTCUSDT', {'price': 95000})

# 获取数据
data = cache.get('BTCUSDT')

# 检查是否存在
if cache.has_symbol('BTCUSDT'):
    print("缓存存在")

# 获取统计
stats = cache.get_stats()
print(f"命中率: {stats['hit_rate']*100:.2f}%")
```

---

## 评分引擎模块

**模块路径**: `core.scoring`

### 主要函数

#### `get_scoring_engine(version='latest')`

获取评分引擎实例。

**参数**:
- `version` (str): 引擎版本（默认'latest'）

**返回值**: ScoringEngine实例

### 主要类

#### `ScoringEngineV612`

评分引擎类（V6.12版本）。

**主要方法**:

##### `score(symbol, data)`

执行评分。

**参数**:
- `symbol` (str): 交易对
- `data` (dict): 市场数据

**返回值**:
```python
{
    'signal_grade': str,      # 信号等级
    'direction': int,         # 方向
    'total_score': Decimal,   # 总分
    'details': dict           # 详细评分
}
```

---

## 配置管理模块

**模块路径**: `config.config_manager`

### 主要类

#### `ConfigManager`

配置管理器类（单例模式）。

**主要方法**:

##### `get(key, default=None)`

获取配置值。

##### `get_decimal(key, default=Decimal('0'))`

获取Decimal类型配置值。

##### `get_bool(key, default=False)`

获取布尔类型配置值。

##### `get_list(key, default=None)`

获取列表类型配置值。

**示例**:
```python
from config.config_manager import get_config_manager

config = get_config_manager()

# 获取配置
total_capital = config.get_decimal('account.total_capital')
symbols = config.get_list('trading.symbols')
```

---

## 注意事项

1. 所有涉及金额的参数都使用 `Decimal` 类型，避免浮点数精度问题
2. 方向参数：1表示多头，-1表示空头
3. 信号等级：S（最高）、A（中等）、B（试仓）
4. 所有模块都支持单例模式，可通过 `get_xxx()` 函数获取全局实例
5. 日志输出统一使用中文

---

## 更新日志

- **2026-04-27**: 创建API文档，覆盖核心模块
