# 新币做空策略复用 btc_eth 动态利润保护功能 - 需求文档

## 文档信息

| 项目 | 内容 |
|------|------|
| 文档版本 | v1.0 |
| 创建日期 | 2026-07-22 |
| 作者 | 需求文档专家 |
| 状态 | 待评审 |
| 策略版本 | new_coin v1.2.0（目标版本） |

---

## 一、需求概述

### 1.1 背景

新币做空策略（new_coin）目前使用固定百分比止损（5%）和基于 ATR 反弹的移动止盈作为持仓保护。btc_eth 策略已实现了一套完整的**动态利润保护（移动止损）**机制，包括：

- 基于浮盈百分比的阶梯回撤止损
- 波动率调节因子
- 硬止损兜底
- 单向移动保护
- 交易所条件单同步

该机制在 btc_eth 策略中已经过验证，能够更精细地保护利润。新币做空策略需要复用此功能，提升持仓风险管理水平。

### 1.2 业务目标

1. 将动态利润保护的核心计算逻辑抽取到 shared 层，实现两策略共用
2. 在 new_coin 策略中接入动态利润保护，替代现有静态止损的部分功能
3. 激活条件从"浮盈+TP1"调整为"仅 TP2 到达后激活"，适配 new_coin 做空策略特性
4. 保留现有 `_check_trailing_stop()`（ATR 反弹移动止盈）作为 TP2 后的补充保护

### 1.3 用户人群

- 量化交易系统开发者（维护 shared 层和两策略代码）
- 策略运维人员（配置动态利润保护参数）
- 交易系统测试人员（验证功能正确性）

### 1.4 成功指标

1. shared 层函数在 btc_eth 和 new_coin 中调用结果一致
2. new_coin 持仓在 TP2 到达后自动激活动态利润保护
3. 交易所条件单正确同步，非首次激活时不上报错误
4. 兼容 new_coin 现有的 `position_tracking` 字典结构

---

## 二、功能清单

### 2.1 功能总览

| 编号 | 功能模块 | 优先级 | 说明 |
|------|---------|--------|------|
| F1 | shared 层纯计算函数抽取 | P0 | 将阶梯回撤、波动率调节、硬止损兜底、单向移动保护抽到 shared |
| F2 | new_coin executor 新增 `_check_dynamic_trailing` | P0 | 调用 shared 层计算，判断是否触发平仓 |
| F3 | new_coin executor 新增 `_sync_trailing_stop_order` | P0 | 将动态止损价同步到交易所条件单 |
| F4 | new_coin `position_tracking` 新增字段 | P0 | 存储动态利润保护所需状态 |
| F5 | new_coin config.yaml 新增配置节 | P0 | 新增 `dynamic_trailing` 完整配置 |
| F6 | new_coin strategy 主循环增加调用点 | P0 | 在 `_monitor_positions` 中调用 |
| F7 | btc_eth 策略改为调用 shared 层 | P1 | 保持向后兼容，减少重复代码 |
| F8 | 激活条件适配 new_coin 做空策略 | P0 | 仅 TP2 到达后激活，不从浮盈百分比激活 |

### 2.2 功能详细说明

#### F1: shared 层纯计算函数抽取

**文件：** `shared/dynamic_trailing.py`（新建）

抽取以下纯计算逻辑，不依赖任何策略类的实例属性（只依赖传入的参数）：

| 函数名 | 来源 | 说明 |
|--------|------|------|
| `calculate_dynamic_trailing_stop()` | btc_eth `_calculate_dynamic_trailing_stop` | 核心计算逻辑，返回止损价或 None |
| `get_volatility_adjustment()` | btc_eth `_get_volatility_adjustment` | 波动率调节因子计算，依赖 kline_service |
| `calculate_retrace_stop_price()` | 拆分自阶梯回撤部分 | 基于阶梯回撤和波动率计算止损价 |
| `calculate_hard_stop_price()` | 拆分自硬止损部分 | 计算硬止损价兜底 |
| `apply_one_way_protection()` | 拆分自单向移动保护 | 确保止损价单向移动 |

**设计原则：**
- 所有函数不持有策略实例引用
- 函数参数为简单数据类型或 TypedDict
- 依赖外部服务（如 kline_service）通过参数传入
- 返回值为简单数据结构（Decimal, float, Optional[Decimal]）

#### F2: new_coin executor 新增 `_check_dynamic_trailing`

**位置：** `strategies/new_coin/executor.py`

**函数签名：**
```python
async def _check_dynamic_trailing(
    self,
    symbol: str,
    current_price: Decimal
) -> None
```

**逻辑：**
1. 从 `self.position_tracking[symbol]` 读取动态利润保护所需字段
2. 调用 `shared.dynamic_trailing.calculate_dynamic_trailing_stop()` 计算止损价
3. 如果返回 None，跳过（未激活）
4. 如果当前价格突破止损价 -> 调 `_close_position()` 平仓
5. 如果止损价改善 -> 调 `_sync_trailing_stop_order()` 同步到交易所
6. 如果止损价未改善 -> 跳过

**异常处理：**
- 平仓前尝试取消旧条件单（忽略已成交的错误码）
- 所有异常 catch 并记录日志，不阻断主循环

#### F3: new_coin executor 新增 `_sync_trailing_stop_order`

**位置：** `strategies/new_coin/executor.py`

**函数签名：**
```python
async def _sync_trailing_stop_order(
    self,
    symbol: str,
    trailing_stop: Decimal
) -> None
```

**逻辑：**
1. 取消旧移动止损条件单（`self.position_tracking[symbol]['algo_ids']['trailing_stop']`）
2. 如果是首次激活，取消原有硬止损条件单（`algo_ids['sl']`）
3. 计算止损限价（触发价向不利方向偏移 `stop_limit_order.offset_pct`）
4. 调用 `self.binance_api.place_conditional_order()` 创建新止损单
5. 保存新条件单 algoId 到 `position_tracking`
6. 记录条件单到数据库（`record_condition_order`）

**注意：** 使用 `self.binance_api` 而非 `self.binance`（btc_eth 差异）

#### F4: position_tracking 新增字段

**位置：** `strategies/new_coin/executor.py`，`execute_short()` 中初始化

新增字段（与 `PositionState` 字段映射）：

| 新增字段 | 类型 | 默认值 | 对应 PositionState 字段 | 说明 |
|---------|------|--------|------------------------|------|
| `trailing_activated` | bool | False | `trailing_activated` | 动态利润保护是否激活 |
| `trailing_stop_price` | Optional[float] | None | `trailing_stop_price` | 当前动态止损价 |
| `pending_profit_pct` | Optional[float] | None | `pending_profit_pct` | 上次计算的浮盈% |
| `current_tier_index` | int | -1 | `current_tier_index` | 当前回撤阶梯索引 |
| `highest_price` | float | entry_price | `highest_price` | 做空时追踪最高价（反向指标） |

**注意：** `lowest_price` 已存在，用于做空追踪最低价。做空时动态利润保护需要追踪**最高价**（因为价格反弹触达止损），所以需要新增 `highest_price`。

**algo_ids 新增 key：**
- `'trailing_stop'`：存储动态止损条件单 algoId

#### F5: new_coin config.yaml 新增配置节

**位置：** `strategies/new_coin/config.yaml`，在 `trading` 下新增

```yaml
# 动态利润保护（移动止损）
dynamic_trailing:
  enabled: true
  activation:
    min_profit_pct: 1.5  # 浮盈 >= 1.5% 激活（new_coin 不使用此条件，仅用于计算）
    also_on_tp1: false    # TP1 触发不激活（new_coin 仅 TP2 激活）
    also_on_tp2: true     # TP2 触发激活（new_coin 特有）
  regression_tiers:
    - profit_ceiling: 1.5
      retrace_ratio: 0.0
    - profit_ceiling: 4.0
      retrace_ratio: 0.5
    - profit_ceiling: 8.0
      retrace_ratio: 0.35
    - profit_ceiling: 999.0
      retrace_ratio: 0.25
  volatility_adjustment:
    enabled: true
    atr_lookback_days: 30
    atr_period: 14
    cache_ttl_seconds: 3600
  # 止损限价单偏移（确保触发后成交）
  stop_limit_order:
    offset_pct: 0.002  # 0.2%
  # 条件单清理静默错误码
  cleanup_silent_error_codes: [-2022, -2011]
```

#### F6: new_coin strategy 主循环增加调用点

**位置：** `strategies/new_coin/strategy.py`，`_monitor_positions()` 方法

**修改：** 在 `self.trading_executor.check_position_management(symbol)` 内部（或在其调用后），增加动态利润保护的调用点。

**推荐方案：** 在 `check_position_management()` 内部增加 `_check_dynamic_trailing()` 调用，顺序为：

```
check_position_management(symbol):
  1. 检查时间止损
  2. 检查紧急止损
  3. 检查动态利润保护（新增，在移动止盈之前）
  4. 检查移动止盈（原有，TP2 到达后激活）
```

**激活条件适配：** 在 `check_position_management()` 中，动态利润保护仅在 `target2_reached == True` 时调用（TP2 到达后激活）。

#### F7: btc_eth 策略改为调用 shared 层（可选）

**位置：** `strategies/btc_eth/strategy.py`

**修改：** 将 `_calculate_dynamic_trailing_stop()` 和 `_get_volatility_adjustment()` 改为调用 shared 层函数，保持方法签名不变，内部实现委托给 shared 层。

**向后兼容：** 方法签名不变，调用方 `_check_dynamic_trailing()` 无需修改。

---

## 三、接口定义（shared 层函数签名）

### 3.1 核心计算函数

```python
# shared/dynamic_trailing.py

from typing import Optional, Dict, List, Any
from decimal import Decimal
import pandas as pd


def calculate_dynamic_trailing_stop(
    *,
    # 方向与价格
    direction: str,                          # 'LONG' / 'SHORT'
    entry_price: Decimal,                    # 入场价格
    current_price: Decimal,                  # 当前价格（仅用于触发检查）
    highest_price: Optional[Decimal],        # 做多最高价
    lowest_price: Optional[Decimal],         # 做空最低价
    
    # 激活状态
    trailing_activated: bool,                # 是否已激活
    tp1_hit: bool,                           # TP1是否触发
    tp2_hit: bool,                           # TP2是否触发（new_coin 特有）
    
    # 当前状态
    pending_profit_pct: Optional[float],     # 上次浮盈%（用于更新）
    current_tier_index: int,                 # 当前阶梯索引
    current_trailing_stop_price: Optional[Decimal],  # 当前止损价（单向移动保护）
    
    # 配置参数
    config: Dict[str, Any],                  # dynamic_trailing 配置节
    atr: Decimal,                            # 入场ATR
    stop_loss_atr_multiplier: Decimal,       # 硬止损ATR倍数
    
    # 波动率调节（可选，如果 volatility_adjustment.enabled）
    volatility_adj: float = 1.0,            # 外部传入的波动率调节因子
    
) -> Optional[Dict[str, Any]]:
    """
    计算动态利润保护止损价
    
    纯计算函数，不依赖任何外部服务（波动率调节因子由调用方传入）。
    
    Returns:
        None: 未激活
        Dict: {
            'trailing_stop_price': Decimal,      # 最终止损价
            'trailing_activated': bool,          # 是否已激活
            'pending_profit_pct': float,         # 更新后的浮盈%
            'current_tier_index': int,           # 更新后的阶梯索引
            'triggered': bool,                   # 当前价是否已突破止损价
            'tier_retrace_ratio': float,         # 当前阶梯回撤比例
            'vol_adj': float,                    # 使用的波动率调节因子
        }
    """
    ...


async def get_volatility_adjustment(
    *,
    symbol: str,
    entry_price: Decimal,
    atr: Decimal,
    kline_service: Any,       # KLineService 实例
    config: Dict[str, Any],   # volatility_adjustment 配置节
    cache: Dict[str, Any],    # 外部传入的缓存字典（按引用传递）
) -> float:
    """
    计算波动率调节因子
    
    依赖外部 kline_service 获取历史日线数据，带缓存。
    
    Returns:
        float: 波动率调节因子，clamp 到 [0.5, 2.0]
    """
    ...
```

### 3.2 辅助函数

```python
def calculate_retrace_stop_price(
    direction: str,
    reference_price: Decimal,
    entry_price: Decimal,
    retrace_ratio: float,
    vol_adj: float,
) -> Decimal:
    """
    计算阶梯回撤止损价
    
    Args:
        direction: 方向
        reference_price: 参考价（最高价/最低价）
        entry_price: 入场价
        retrace_ratio: 回撤比例
        vol_adj: 波动率调节因子
    
    Returns:
        Decimal: 回撤止损价
    """
    ...


def calculate_hard_stop_price(
    direction: str,
    entry_price: Decimal,
    atr: Decimal,
    stop_loss_atr_multiplier: Decimal,
) -> Decimal:
    """
    计算硬止损价（兜底）
    
    Returns:
        Decimal: 硬止损价
    """
    ...


def apply_one_way_protection(
    direction: str,
    new_stop_price: Decimal,
    current_stop_price: Optional[Decimal],
) -> Decimal:
    """
    单向移动保护：做多只能上移，做空只能下移
    
    Args:
        direction: 方向
        new_stop_price: 计算出的新止损价
        current_stop_price: 当前生效的止损价
    
    Returns:
        Decimal: 最终止损价
    """
    ...
```

### 3.3 TypedDict 定义

```python
from typing import TypedDict


class TrailingStopResult(TypedDict):
    """计算动态利润保护止损价的结果"""
    trailing_stop_price: Decimal
    trailing_activated: bool
    pending_profit_pct: float
    current_tier_index: int
    triggered: bool
    tier_retrace_ratio: float
    vol_adj: float
```

---

## 四、数据映射（PositionState -> position_tracking 字典）

### 4.1 字段映射表

| 分类 | PositionState 字段 | position_tracking key | 类型转换 | 说明 |
|------|-------------------|----------------------|---------|------|
| 基础 | `entry_price` | `entry_price` | Decimal -> float | 已存在 |
| 基础 | `entry_time` | `entry_time` | datetime -> datetime | 已存在 |
| 基础 | `current_quantity` | `remaining_quantity` | Decimal -> float | 已存在，语义不同 |
| 基础 | `atr` | `atr` | Decimal -> float | 已存在 |
| 基础 | `direction` | `direction` | str -> str | new_coin 始终是 SHORT，但需显式存储 |
| 止盈 | `tp1_hit` | `target1_reached` | bool -> bool | 已存在 |
| 止盈 | `tp2_hit` | `target2_reached` | bool -> bool | 已存在 |
| 价格 | `highest_price` | `highest_price` | Decimal -> float | **新增** |
| 价格 | `lowest_price` | `lowest_price` | Decimal -> float | 已存在 |
| 动态止损 | `trailing_activated` | `trailing_activated` | bool -> bool | **新增** |
| 动态止损 | `trailing_stop_price` | `trailing_stop_price` | Decimal -> float | **新增** |
| 动态止损 | `trailing_stop_order_id` | `algo_ids['trailing_stop']` | int -> int | **新增** |
| 动态止损 | `pending_profit_pct` | `pending_profit_pct` | float -> float | **新增** |
| 动态止损 | `current_tier_index` | `current_tier_index` | int -> int | **新增** |
| 硬止损 | `stop_loss_order_id` | `algo_ids['sl']` | int -> int | 已存在 |

### 4.2 position_tracking 初始化完整代码（execute_short 中）

```python
self.position_tracking[symbol] = {
    # 原有字段
    'entry_price': current_price,
    'entry_time': datetime.now(timezone.utc),
    'entry_quantity': float(quantity),
    'atr': float(atr),
    'lowest_price': current_price,
    'target1_reached': False,
    'target2_reached': False,
    'remaining_quantity': float(quantity),
    'algo_ids': {'sl': ..., 'tp1': ..., 'tp2': ...},
    # 新增字段
    'direction': 'SHORT',
    'highest_price': current_price,  # 做空时追踪最高价
    'trailing_activated': False,
    'trailing_stop_price': None,
    'pending_profit_pct': None,
    'current_tier_index': -1,
}
```

---

## 五、配置项清单

### 5.1 new_coin config.yaml 新增配置

```yaml
trading:
  # ... 原有配置 ...
  
  # 新增：动态利润保护（移动止损）
  # 源自 btc_eth risk_config.dynamic_trailing，适配 new_coin
  dynamic_trailing:
    enabled: true
    activation:
      min_profit_pct: 1.5    # 浮盈 >= 1.5% 浮盈计算阈值（用于阶梯计算，但不作为激活条件）
      also_on_tp1: false      # TP1 触发不激活（new_coin 特有：仅 TP2 激活）
      also_on_tp2: true       # TP2 触发激活（new_coin 特有）
    regression_tiers:
      - profit_ceiling: 1.5
        retrace_ratio: 0.0
      - profit_ceiling: 4.0
        retrace_ratio: 0.5
      - profit_ceiling: 8.0
        retrace_ratio: 0.35
      - profit_ceiling: 999.0
        retrace_ratio: 0.25
    volatility_adjustment:
      enabled: true
      atr_lookback_days: 30
      atr_period: 14
      cache_ttl_seconds: 3600
    # 止损限价单偏移（触发价向不利方向偏移，确保成交）
    # 做空（BUY）时：限价 = 止损价 * (1 + offset_pct)
    stop_limit_order:
      offset_pct: 0.002       # 0.2%
    # 条件单取消时静默忽略的错误码
    # -2022: 订单已取消/已成交
    # -2011: 订单未找到
    cleanup_silent_error_codes: [-2022, -2011]
```

### 5.2 配置项说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enabled` | bool | true | 总开关 |
| `activation.min_profit_pct` | float | 1.5 | 浮盈计算阈值百分比 |
| `activation.also_on_tp1` | bool | false | TP1 触发是否激活（new_coin 固定 false） |
| `activation.also_on_tp2` | bool | true | TP2 触发是否激活（new_coin 特有） |
| `regression_tiers[].profit_ceiling` | float | - | 阶梯浮盈上限 |
| `regression_tiers[].retrace_ratio` | float | - | 阶梯回撤比例 |
| `volatility_adjustment.enabled` | bool | true | 波动率调节开关 |
| `volatility_adjustment.atr_lookback_days` | int | 30 | 历史日线 ATR 回溯天数 |
| `volatility_adjustment.atr_period` | int | 14 | ATR 计算周期 |
| `volatility_adjustment.cache_ttl_seconds` | int | 3600 | 波动率缓存有效期（秒） |
| `stop_limit_order.offset_pct` | float | 0.002 | 止损限价偏移比例 |
| `cleanup_silent_error_codes` | list | [-2022, -2011] | 条件单取消时静默忽略的错误码 |

---

## 六、修改文件清单

### 6.1 新增文件

| 文件路径 | 说明 | 预估行数 |
|---------|------|---------|
| `shared/dynamic_trailing.py` | 动态利润保护纯计算逻辑 | 200-300 行 |

### 6.2 修改文件

| 文件路径 | 修改内容 | 预估行数 |
|---------|---------|---------|
| `strategies/new_coin/executor.py` | 新增 `_check_dynamic_trailing()`、`_sync_trailing_stop_order()` 方法；修改 `__init__()` 读取配置；修改 `execute_short()` 扩展 `position_tracking` 初始化；修改 `check_position_management()` 增加调用点 | 150-200 行 |
| `strategies/new_coin/config.yaml` | 新增 `trading.dynamic_trailing` 配置节 | 30 行 |
| `strategies/btc_eth/strategy.py` | 可选：改为调用 shared 层函数 | 50-80 行 |

### 6.3 无需修改的文件

| 文件路径 | 原因 |
|---------|------|
| `strategies/new_coin/strategy.py` | 调用点通过 executor 的 `check_position_management()` 透传，无需修改策略层 |
| `strategies/new_coin/main.py` | 无需修改主入口 |

---

## 七、注意事项与风险

### 7.1 关键差异点

| 维度 | btc_eth | new_coin | 影响 |
|------|---------|----------|------|
| 持仓数据结构 | `PositionState` 类（属性访问） | `position_tracking` 字典 | shared 层函数需接受字典参数或解构参数 |
| API 调用对象 | `self.binance` | `self.binance_api` | `_sync_trailing_stop_order` 中注意调用对象 |
| 配置访问 | `self.risk_config['dynamic_trailing']` | `self.config['trading']['dynamic_trailing']` | shared 层函数直接接收配置字典 |
| 数据库 | `self.db_manager` | `self.db` | `record_condition_order` 调用时注意参数 |
| 激活条件 | 浮盈>=1.5% 或 TP1 触发 | 仅 TP2 到达后激活 | 需在 `check_position_management` 中判断 `target2_reached` |
| 做空方向 | 已支持对称处理 | 本来就是做空策略 | 无需额外适配，但需验证做空方向逻辑正确 |
| 硬止损 | 1.5xATR 硬止损单 | 百分比止损 + 紧急止损 + 时间止损 | 首次激活时需取消原有硬止损条件单（`algo_ids['sl']`） |
| 波动率计算 | 使用 `self.kline_service` | new_coin executor 也有 `self.kline_service` | 传入 shared 层函数即可 |

### 7.2 风险项

| 风险编号 | 风险描述 | 影响 | 概率 | 缓解措施 |
|---------|---------|------|------|---------|
| R1 | shared 层函数抽取时，btc_eth 的 `self.risk_config` 路径与 new_coin 的 `self.config['trading']` 路径不一致，导致配置读取错误 | 配置不生效 | 低 | shared 函数接收完整的配置字典，由调用方传入 |
| R2 | new_coin 的 `kline_service.get_klines()` 返回的 K 线数据字段名（`close` vs `close_price`）与 btc_eth 不一致 | 波动率计算失败 | 低 | 在 `get_volatility_adjustment` 中兼容两种字段名 |
| R3 | 做空方向下，`highest_price` 追踪需要用于判断是否"反弹"触发止损，与做多方向逻辑对称但方向相反 | 止损方向错误 | 中 | 在 shared 层函数中已通过 `direction` 参数区分，需单元测试验证 |
| R4 | 交易所条件单创建失败（如网络问题），导致动态止损价未同步到交易所 | 利润保护不生效 | 中 | 失败时记录日志，下个周期重试；硬止损单作为兜底 |
| R5 | 波动率计算中 `_base_atr_cache` 缓存膨胀 | 内存泄漏 | 低 | 使用 LRU 或在 shared 层使用 `@lru_cache` |
| R6 | btc_eth 修改为调用 shared 层后，回归测试不足导致功能异常 | 影响线上交易 | 中 | 优先保证 new_coin 功能，btc_eth 重构作为 P1 延后 |

### 7.3 注意事项

1. **激活条件逻辑**：new_coin 的 `check_position_management` 中，动态利润保护仅在 `target2_reached == True` 时调用。这意味着：
   - TP2 到达前，使用原有止损机制（固定百分比止损、紧急止损、时间止损）
   - TP2 到达后，`_check_trailing_stop()`（ATR 反弹移动止盈）和 `_check_dynamic_trailing()`（动态利润保护）同时生效
   - 两者不冲突：`_check_trailing_stop` 从最低价反弹触发，`_check_dynamic_trailing` 从最高价回落触发

2. **`_check_trailing_stop()` 保留**：现有的 ATR 反弹移动止盈作为 TP2 后的补充保护，与动态利润保护形成双保险。动态利润保护覆盖价格回落场景，ATR 反弹覆盖价格反弹场景。

3. **`highest_price` 更新**：在 `check_position_management` 或 `_check_dynamic_trailing` 中，需要更新 `highest_price`（做空追踪最高价），与 `lowest_price` 的更新逻辑对称。

4. **首次激活时取消硬止损单**：`_sync_trailing_stop_order` 中，首次激活时需取消原有硬止损条件单（`algo_ids['sl']`），因为动态止损已替代硬止损。

5. **条件单取消静默错误码**：`cleanup_silent_error_codes` 中的错误码（-2022, -2011）在取消旧条件单时静默忽略，避免日志污染。

6. **精度处理**：`_sync_trailing_stop_order` 中需要调用 `_get_symbol_precision` 获取精度，使用 `_format_price` 和 `_format_quantity` 格式化价格和数量。

7. **波动率缓存**：`get_volatility_adjustment` 的缓存需传入外部字典，由调用方管理生命周期。

---

## 八、验收标准

### 8.1 功能验收

| 编号 | 验收项 | 验证方法 | 预期结果 |
|------|--------|---------|---------|
| AC1 | shared 层函数调用正确 | 在 btc_eth 和 new_coin 中分别调用，输入相同参数，输出一致 | 输出完全一致 |
| AC2 | 动态利润保护在 TP2 到达后激活 | 模拟持仓，TP2 到达后，检查 `trailing_activated` 变为 True | 激活成功 |
| AC3 | 动态利润保护在 TP2 到达前不激活 | 模拟持仓，仅 TP1 到达，检查 `trailing_activated` 仍为 False | 不激活 |
| AC4 | 价格回落触发止损平仓 | 模拟价格从最高价回落超过允许回撤，检查 `_close_position` 被调用 | 触发平仓 |
| AC5 | 交易所条件单同步 | 止损价改善时，检查 `place_conditional_order` 被调用 | 创建新条件单 |
| AC6 | 首次激活取消硬止损单 | 首次激活时，检查 `cancel_algo_order` 对原硬止损单被调用 | 硬止损单被取消 |
| AC7 | 条件单取消静默错误 | 取消不存在的条件单，检查日志不报 ERROR | 日志为 DEBUG 或 WARNING |
| AC8 | 单向移动保护 | 做空方向下，止损价仅单向向下移动，不反向 | 止损价不上升 |

### 8.2 非功能验收

| 编号 | 验收项 | 验证方法 | 预期结果 |
|------|--------|---------|---------|
| AC9 | 异常不影响主流程 | 模拟 `_check_dynamic_trailing` 抛出异常 | 主循环继续运行，记录 ERROR 日志 |
| AC10 | 波动率计算缓存生效 | 1 小时内重复调用，检查 `kline_service.get_klines` 调用次数 | 仅首次调用 |
| AC11 | 配置可热加载 | 修改配置后重启策略，检查新配置生效 | 新配置生效 |

---

## 九、执行计划

### 9.1 实施步骤

| 步骤 | 内容 | 涉及文件 | 预估工时 |
|------|------|---------|---------|
| 1 | 创建 `shared/dynamic_trailing.py`，抽取核心计算函数 | 新建文件 | 1 天 |
| 2 | 修改 `strategies/new_coin/executor.py`，新增方法 | executor.py | 1 天 |
| 3 | 修改 `strategies/new_coin/config.yaml`，新增配置 | config.yaml | 0.5 天 |
| 4 | 单元测试（shared 层纯计算 + executor 方法） | 新增测试文件 | 1 天 |
| 5 | 集成测试（模拟完整持仓周期） | 新增测试文件 | 0.5 天 |
| 6 | 可选：修改 btc_eth 策略调用 shared 层 | strategy.py | 0.5 天 |
| 7 | 代码审查 | - | 0.5 天 |

### 9.2 测试策略

1. **单元测试**：对 shared 层每个纯计算函数做独立测试，覆盖：
   - 正常场景（各种浮盈阶梯）
   - 边界条件（浮盈刚好等于阶梯阈值）
   - 异常场景（参数为 None、配置为空）
   - 做空/做多双向验证

2. **集成测试**：Mock 交易所和 K 线服务，测试完整流程：
   - 开仓 -> TP1 到达 -> TP2 到达 -> 激活动态止损 -> 价格回落触发平仓
   - 开仓 -> TP2 到达 -> 激活动态止损 -> 止损价改善 -> 交易所条件单同步
   - 开仓 -> 止损触发（未达到 TP2，动态利润保护不激活）

---

## 十、术语表

| 术语 | 说明 |
|------|------|
| 动态利润保护 | 基于浮盈百分比和波动率调节的阶梯回撤止损机制 |
| 回撤阶梯 | 根据浮盈百分比分档，每档对应不同的允许回撤比例 |
| 波动率调节因子 | 基于历史日线 ATR 中位数计算的调节系数，用于调整回撤比例 |
| 硬止损 | 基于 ATR 倍数的固定止损价，作为动态止损的兜底 |
| 单向移动保护 | 止损价只能向有利方向移动（做多向上，做空向下） |
| 条件单同步 | 将动态止损价以条件单形式同步到交易所，由交易所自动触发 |

---

## 十一、附录

### 11.1 参考文件

- `strategies/btc_eth/strategy.py` L29-L62（PositionState 类）
- `strategies/btc_eth/strategy.py` L2844-L2930（`_check_dynamic_trailing`）
- `strategies/btc_eth/strategy.py` L2932-L3064（`_calculate_dynamic_trailing_stop`）
- `strategies/btc_eth/strategy.py` L3066-L3164（`_get_volatility_adjustment`）
- `strategies/btc_eth/strategy.py` L3166-L3306（`_sync_trailing_stop_order`）
- `strategies/btc_eth/config.yaml` L158-L177（`dynamic_trailing` 配置节）
- `strategies/new_coin/executor.py` L38-L108（TradingExecutor 初始化）
- `strategies/new_coin/executor.py` L203-L214（position_tracking 初始化）
- `strategies/new_coin/executor.py` L848-L878（check_position_management）
- `strategies/new_coin/executor.py` L991-L1056（`_check_trailing_stop`）

### 11.2 变更记录

| 版本 | 日期 | 变更内容 | 作者 |
|------|------|---------|------|
| v1.0 | 2026-07-22 | 初稿 | 需求文档专家 |