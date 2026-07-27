# 新币做空策略复用 btc_eth 动态利润保护 - 架构设计文档

## 文档信息

| 项目 | 内容 |
|------|------|
| 文档版本 | v1.0 |
| 创建日期 | 2026-07-22 |
| 作者 | 后端架构师 |
| 状态 | 待评审 |
| 关联需求 | `new_coin动态利润保护需求文档.md` |
| 源代码版本 | btc_eth v6.x, new_coin v1.1.0 |

---

## 一、架构总览

### 1.1 设计目标

将 btc_eth 策略的**动态利润保护（移动止损）**核心计算逻辑抽取到 `shared/dynamic_trailing.py`，供 btc_eth 和 new_coin 两策略共用。new_coin 做空策略仅 TP2 到达后激活该功能，作为现有 ATR 反弹移动止盈的补充保护。

### 1.2 架构分层

```
┌─────────────────────────────────────────────────────┐
│                   策略层 (strategies/)                │
│                                                       │
│  ┌─────────────────────┐  ┌─────────────────────┐    │
│  │  btc_eth/strategy.py │  │ new_coin/executor.py │    │
│  │                      │  │                      │    │
│  │  _check_dynamic      │  │  _check_dynamic      │    │
│  │  _trailing()         │  │  _trailing()         │    │
│  │  _sync_trailing      │  │  _sync_trailing      │    │
│  │  _stop_order()       │  │  _stop_order()       │    │
│  └──────────┬───────────┘  └──────────┬───────────┘    │
│             │                          │                │
└─────────────┼──────────────────────────┼────────────────┘
              │       调用纯计算函数       │
              ▼                          ▼
┌─────────────────────────────────────────────────────┐
│                   shared 层 (shared/)                 │
│                                                       │
│  ┌────────────────────────────────────────────────┐  │
│  │  dynamic_trailing.py                            │  │
│  │                                                  │  │
│  │  calculate_dynamic_trailing_stop()  ← 核心函数   │  │
│  │  calculate_retrace_stop_price()    ← 辅助函数    │  │
│  │  calculate_hard_stop_price()       ← 辅助函数    │  │
│  │  apply_one_way_protection()        ← 辅助函数    │  │
│  │  get_volatility_adjustment()       ← 异步函数    │  │
│  └────────────────────────────────────────────────┘  │
│                                                       │
│  ┌────────────────────────────────────────────────┐  │
│  │  condition_orders.py (已有)                     │  │
│  │  record_condition_order()  ← 两策略共用         │  │
│  └────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

### 1.3 调用关系

```
check_position_management(symbol)
  │
  ├── _check_time_stop()              # 时间止损（已有）
  ├── _check_emergency_stop()         # 紧急止损（已有）
  │
  ├── _check_dynamic_trailing()       # 新增：动态利润保护
  │     │                               仅在 target2_reached == True 时调用
  │     ├── calculate_dynamic_trailing_stop()  ← shared 层纯计算
  │     │     ├── calculate_retrace_stop_price()
  │     │     ├── calculate_hard_stop_price()
  │     │     └── apply_one_way_protection()
  │     │
  │     ├── 触发平仓 → _close_position()
  │     └── 止损价改善 → _sync_trailing_stop_order()
  │
  └── _check_trailing_stop()          # 已有：ATR 反弹移动止盈
```

---

## 二、shared/dynamic_trailing.py 模块详细设计

### 2.1 文件位置

```
shared/dynamic_trailing.py  ← 新建文件
```

### 2.2 TypedDict 类型定义

```python
from typing import TypedDict, Optional, List, Dict, Any
from decimal import Decimal


class RegressionTier(TypedDict):
    """回撤阶梯定义"""
    profit_ceiling: float    # 浮盈上限百分比
    retrace_ratio: float     # 允许回撤比例


class VolatilityAdjustmentConfig(TypedDict, total=False):
    """波动率调节配置"""
    enabled: bool
    atr_lookback_days: int
    atr_period: int
    cache_ttl_seconds: int


class DynamicTrailingConfig(TypedDict, total=False):
    """动态利润保护配置（完整配置节）"""
    enabled: bool
    activation: Dict[str, Any]           # min_profit_pct, also_on_tp1, also_on_tp2
    regression_tiers: List[RegressionTier]
    volatility_adjustment: VolatilityAdjustmentConfig
    stop_limit_order: Dict[str, Any]     # offset_pct
    cleanup_silent_error_codes: List[int]


class TrailingStopResult(TypedDict):
    """计算动态利润保护止损价的结果"""
    trailing_stop_price: Decimal          # 最终止损价
    trailing_activated: bool              # 是否已激活
    pending_profit_pct: float             # 更新后的浮盈%
    current_tier_index: int               # 更新后的阶梯索引
    triggered: bool                       # 当前价是否已突破止损价
    tier_retrace_ratio: float             # 当前阶梯回撤比例
    vol_adj: float                        # 使用的波动率调节因子
```

### 2.3 核心函数：`calculate_dynamic_trailing_stop`

**这是唯一需要由策略层调用的入口函数**，其余辅助函数均为内部调用。

```python
def calculate_dynamic_trailing_stop(
    *,
    # 方向与价格
    direction: str,                          # 'LONG' / 'SHORT'
    entry_price: Decimal,                    # 入场价格
    current_price: Decimal,                  # 当前价格（仅用于触发检查）
    highest_price: Optional[Decimal],        # 做多时的最高价
    lowest_price: Optional[Decimal],         # 做空时的最低价
    
    # 激活状态
    trailing_activated: bool,                # 是否已激活
    tp1_hit: bool,                           # TP1是否触发
    tp2_hit: bool,                           # TP2是否触发（new_coin 特有）
    
    # 当前状态（用于更新）
    pending_profit_pct: Optional[float],     # 上次浮盈%
    current_tier_index: int,                 # 当前阶梯索引
    current_trailing_stop_price: Optional[Decimal],  # 当前止损价（单向移动保护）
    
    # 配置参数
    config: Dict[str, Any],                  # dynamic_trailing 配置节
    atr: Decimal,                            # 入场ATR
    stop_loss_atr_multiplier: Decimal,       # 硬止损ATR倍数
    
    # 波动率调节（可选，由调用方传入，如 volatility_adjustment 关闭则传 1.0）
    volatility_adj: float = 1.0,
    
) -> Optional[TrailingStopResult]:
    """
    计算动态利润保护止损价（纯函数，无副作用）
    
    核心逻辑：
    1. 基于参考价（最高价/最低价）计算浮盈百分比
    2. 检查是否应激活（配置项：also_on_tp1, also_on_tp2, min_profit_pct）
    3. 确定回撤阶梯，计算允许回撤
    4. 计算硬止损价（兜底）
    5. 单向移动保护
    6. 返回计算结果或 None（未激活时）
    
    Args:
        direction: 方向 ('LONG' / 'SHORT')
        entry_price: 入场价格
        current_price: 当前价格（仅用于参考价回退和触发检查）
        highest_price: 做多时的最高价（做空时传 None）
        lowest_price: 做空时的最低价（做多时传 None）
        trailing_activated: 是否已激活
        tp1_hit: TP1是否触发
        tp2_hit: TP2是否触发（new_coin 特有激活条件）
        pending_profit_pct: 上次计算的浮盈%
        current_tier_index: 当前回撤阶梯索引
        current_trailing_stop_price: 当前生效的止损价
        config: dynamic_trailing 配置节（完整字典）
        atr: 入场时的 ATR 值
        stop_loss_atr_multiplier: 硬止损 ATR 倍数
        volatility_adj: 波动率调节因子（由调用方传入，默认 1.0）
    
    Returns:
        None: 未激活
        TrailingStopResult: 计算结果
    """
    ...
```

**内部逻辑流程：**

```
1. 配置检查
   if not config.get('enabled', True): return None

2. 计算浮盈百分比
   - LONG:  reference_price = max(highest_price, entry_price, current_price)
            profit_pct = (reference_price - entry_price) / entry_price * 100
   - SHORT: reference_price = min(lowest_price, entry_price, current_price)
            profit_pct = (entry_price - reference_price) / entry_price * 100
   - profit_pct = max(profit_pct, 0.0)  # 浮亏不计入

3. 激活判断（关键差异点）
   min_profit = config['activation']['min_profit_pct']
   profit_activated = profit_pct >= min_profit
   tp1_activated = config['activation'].get('also_on_tp1', True) and tp1_hit
   tp2_activated = config['activation'].get('also_on_tp2', False) and tp2_hit
   
   if not profit_activated and not tp1_activated and not tp2_activated:
       if trailing_activated:
           pass  # 已激活不退出
       else:
           return None

4. 确定阶梯索引
   - 遍历 regression_tiers，找到 profit_pct < profit_ceiling 的 tier
   - 保本模式：profit_pct < first_tier_ceiling 且 TP1未触发 → stop_price = entry_price

5. 计算止损价
   stop_price = calculate_retrace_stop_price(
       direction, reference_price, entry_price, retrace_ratio, vol_adj
   )

6. 硬止损兜底
   hard_stop_price = calculate_hard_stop_price(
       direction, entry_price, atr, stop_loss_atr_multiplier
   )
   final_stop = max(stop_price, hard_stop_price)  # LONG
   final_stop = min(stop_price, hard_stop_price)  # SHORT

7. 单向移动保护
   final_stop = apply_one_way_protection(
       direction, final_stop, current_trailing_stop_price
   )

8. 触发检查
   triggered = (direction == 'LONG' and current_price <= final_stop) or \
               (direction == 'SHORT' and current_price >= final_stop)

9. 返回结果
   return TrailingStopResult(
       trailing_stop_price=final_stop,
       trailing_activated=True,
       pending_profit_pct=profit_pct,
       current_tier_index=tier_index,
       triggered=triggered,
       tier_retrace_ratio=retrace_ratio,
       vol_adj=vol_adj
   )
```

### 2.4 辅助函数

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
    
    公式：
    - LONG:  profit_per_unit = reference_price - entry_price
             allowed_retrace = profit_per_unit * retrace_ratio * vol_adj
             stop_price = reference_price - allowed_retrace
    - SHORT: profit_per_unit = entry_price - reference_price
             allowed_retrace = profit_per_unit * retrace_ratio * vol_adj
             stop_price = reference_price + allowed_retrace
    """


def calculate_hard_stop_price(
    direction: str,
    entry_price: Decimal,
    atr: Decimal,
    stop_loss_atr_multiplier: Decimal,
) -> Decimal:
    """
    计算硬止损价（兜底）
    
    公式：
    - LONG:  hard_stop_price = entry_price - atr * stop_loss_atr_multiplier
    - SHORT: hard_stop_price = entry_price + atr * stop_loss_atr_multiplier
    """


def apply_one_way_protection(
    direction: str,
    new_stop_price: Decimal,
    current_stop_price: Optional[Decimal],
) -> Decimal:
    """
    单向移动保护：止损价只能向有利方向移动
    
    - LONG:  new >= current → new, 否则 → current
    - SHORT: new <= current → new, 否则 → current
    
    如果 current_stop_price 为 None（首次设置），直接返回 new_stop_price。
    """
```

### 2.5 异步函数：`get_volatility_adjustment`

```python
async def get_volatility_adjustment(
    *,
    symbol: str,
    entry_price: Decimal,
    atr: Decimal,
    kline_service: Any,          # KLineService 实例
    config: Dict[str, Any],      # volatility_adjustment 配置节
    cache: Dict[str, Any],       # 外部传入的缓存字典（按引用传递）
    logger: Any = None,          # 可选的 logger 实例
) -> float:
    """
    计算波动率调节因子
    
    依赖外部 kline_service 获取历史日线数据。
    缓存由调用方管理（传入 cache 字典），避免模块内部持有状态。
    
    逻辑：
    1. 检查缓存（cache_key = f"base_atr_pct_{symbol}"）
    2. 获取历史日线数据（lookback_days + atr_period + 10 根）
    3. 计算日线 ATR 和 ATR% 中位数
    4. 计算当前 ATR% = atr / entry_price
    5. vol_adj = current_atr_pct / base_atr_pct_median
    6. clamp 到 [0.5, 2.0]
    7. 写入缓存，返回结果
    
    Returns:
        float: 波动率调节因子，失败或关闭时返回 1.0
    """
```

**设计要点：**
- 缓存字段约定：`{ 'value': float, 'time': float, 'base_atr_pct': float, 'current_atr_pct': float }`
- K线数据兼容 `close` / `close_price`、`high` / `high_price`、`low` / `low_price` 字段名
- 使用 `shared.indicators.TechnicalIndicators.calculate_atr()` 计算 ATR

---

## 三、new_coin executor 集成方案

### 3.1 position_tracking 字典扩展

#### 新增字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `direction` | str | `'SHORT'` | 方向，显式存储 |
| `highest_price` | float | `current_price` | 做空时追踪最高价（反弹触发止损用） |
| `trailing_activated` | bool | `False` | 动态利润保护是否激活 |
| `trailing_stop_price` | Optional[float] | `None` | 当前动态止损价 |
| `pending_profit_pct` | Optional[float] | `None` | 上次计算的浮盈% |
| `current_tier_index` | int | `-1` | 当前回撤阶梯索引 |

#### algo_ids 新增 key

| key | 类型 | 默认值 | 说明 |
|-----|------|--------|------|
| `trailing_stop` | Optional[int] | `None` | 动态止损条件单 algoId |

#### execute_short 中初始化代码

```python
# 在 execute_short() 中原有 position_tracking 初始化处增加字段
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
    'algo_ids': {...},  # 原有 sl, tp1, tp2
    
    # 新增字段
    'direction': 'SHORT',
    'highest_price': current_price,      # 做空追踪最高价
    'trailing_activated': False,
    'trailing_stop_price': None,
    'pending_profit_pct': None,
    'current_tier_index': -1,
}
```

#### 价格更新逻辑

在 `_check_dynamic_trailing` 或 `check_position_management` 中更新 `highest_price`：

```python
# 做空时，价格反弹（上涨）更新最高价
current_price = Decimal(str(ticker_price))
if current_price > Decimal(str(tracking.get('highest_price', 0))):
    tracking['highest_price'] = float(current_price)
```

### 3.2 `_check_dynamic_trailing` 方法设计

```python
async def _check_dynamic_trailing(
    self,
    symbol: str,
    current_price: Decimal
) -> None:
    """
    检查并执行动态利润保护（new_coin 实现）
    
    调用 shared 层计算函数，判断是否触发平仓或需要更新交易所条件单。
    
    Args:
        symbol: 交易对
        current_price: 当前价格（Decimal）
    """
    try:
        tracking = self.position_tracking.get(symbol)
        if not tracking:
            return
        
        # 读取配置
        trading_config = self.config.get('trading', {})
        dt_config = trading_config.get('dynamic_trailing', {})
        if not dt_config.get('enabled', True):
            return
        
        # 读取动态利润保护所需字段
        entry_price = Decimal(str(tracking.get('entry_price', 0)))
        atr = Decimal(str(tracking.get('atr', 0)))
        highest_price = tracking.get('highest_price')
        lowest_price = tracking.get('lowest_price')
        
        # 获取波动率调节因子（如果配置启用）
        vol_adj = 1.0
        vol_config = dt_config.get('volatility_adjustment', {})
        if vol_config.get('enabled', True) and self.kline_service:
            vol_adj = await get_volatility_adjustment(
                symbol=symbol,
                entry_price=entry_price,
                atr=atr,
                kline_service=self.kline_service,
                config=vol_config,
                cache=self._volatility_cache,  # 需在 __init__ 中初始化
            )
        
        # 调用 shared 层纯计算函数
        result = calculate_dynamic_trailing_stop(
            direction='SHORT',
            entry_price=entry_price,
            current_price=current_price,
            highest_price=Decimal(str(highest_price)) if highest_price else None,
            lowest_price=Decimal(str(lowest_price)) if lowest_price else None,
            trailing_activated=tracking.get('trailing_activated', False),
            tp1_hit=tracking.get('target1_reached', False),
            tp2_hit=tracking.get('target2_reached', False),
            pending_profit_pct=tracking.get('pending_profit_pct'),
            current_tier_index=tracking.get('current_tier_index', -1),
            current_trailing_stop_price=Decimal(str(tracking['trailing_stop_price'])) if tracking.get('trailing_stop_price') is not None else None,
            config=dt_config,
            atr=atr,
            stop_loss_atr_multiplier=Decimal(str(trading_config.get('atr_stop', {}).get('multiplier', 2.5))),
            volatility_adj=vol_adj,
        )
        
        if result is None:
            # 未激活，更新状态后返回
            tracking['trailing_activated'] = False
            return
        
        # 更新 position_tracking 状态
        old_trailing_stop = tracking.get('trailing_stop_price')
        tracking['trailing_activated'] = result['trailing_activated']
        tracking['pending_profit_pct'] = result['pending_profit_pct']
        tracking['current_tier_index'] = result['current_tier_index']
        tracking['trailing_stop_price'] = float(result['trailing_stop_price'])
        
        # 更新最高价（做空时追踪反弹价格）
        current_price_float = float(current_price)
        if current_price_float > tracking.get('highest_price', 0):
            tracking['highest_price'] = current_price_float
        
        # 情况1：触发平仓
        if result['triggered']:
            # 平仓前取消交易所上的移动止损条件单
            await self._cancel_trailing_stop_order(symbol)
            
            logger.info(
                f"{symbol} 触发动态利润保护止损",
                current_price=float(current_price),
                trailing_stop=float(result['trailing_stop_price']),
                pending_profit_pct=result['pending_profit_pct'],
                close_quantity=tracking.get('remaining_quantity', 0)
            )
            
            await self._close_position(
                symbol=symbol,
                close_percent=self.close_percent,
                reason="动态利润保护"
            )
            return
        
        # 情况2：止损价未改善，无需更新交易所条件单
        if old_trailing_stop is not None and float(result['trailing_stop_price']) == old_trailing_stop:
            return
        
        # 情况3：止损价改善 → 同步到交易所条件单
        await self._sync_trailing_stop_order(symbol, result['trailing_stop_price'])
    
    except Exception as e:
        logger.error(f"{symbol} 检查动态利润保护失败", error=str(e), exc_info=True)
```

### 3.3 `_sync_trailing_stop_order` 方法设计

```python
async def _sync_trailing_stop_order(
    self,
    symbol: str,
    trailing_stop: Decimal
) -> None:
    """
    将动态止损价同步到交易所条件单（new_coin 实现）
    
    取消旧条件单，创建新条件单。
    首次激活时同时取消原有硬止损单（algo_ids['sl']）。
    
    Args:
        symbol: 交易对
        trailing_stop: 计算出的动态止损价
    """
    tracking = self.position_tracking.get(symbol, {})
    algo_ids = tracking.get('algo_ids', {})
    trading_config = self.config.get('trading', {})
    dt_config = trading_config.get('dynamic_trailing', {})
    
    stop_side = 'BUY'  # 做空止损方向为买入
    stop_offset_pct = Decimal(str(dt_config.get('stop_limit_order', {}).get('offset_pct', 0.002)))
    silent_error_codes = set(dt_config.get('cleanup_silent_error_codes', [-2022, -2011]))
    
    # 1. 取消旧移动止损条件单
    old_trailing_id = algo_ids.get('trailing_stop')
    if old_trailing_id is not None:
        try:
            await self.binance_api.cancel_algo_order(symbol, old_trailing_id)
            logger.info(f"{symbol} 旧移动止损条件单已取消", algo_id=old_trailing_id)
        except BinanceAPIError as e:
            if e.code in silent_error_codes:
                logger.debug(
                    f"{symbol} 旧移动止损条件单取消失败（可能已成交）",
                    algo_id=old_trailing_id, error_code=e.code
                )
            else:
                logger.warning(
                    f"{symbol} 取消旧移动止损条件单异常",
                    algo_id=old_trailing_id, error_code=e.code
                )
        except Exception as e:
            logger.warning(
                f"{symbol} 取消旧移动止损条件单异常",
                algo_id=old_trailing_id, error=str(e)
            )
        algo_ids['trailing_stop'] = None
    
    # 2. 首次激活时，取消原有硬止损单
    old_sl_id = algo_ids.get('sl')
    if old_sl_id is not None:
        try:
            await self.binance_api.cancel_algo_order(symbol, old_sl_id)
            logger.info(f"{symbol} 硬止损单已取消（由动态止损替代）", algo_id=old_sl_id)
        except BinanceAPIError as e:
            if e.code in silent_error_codes:
                logger.debug(
                    f"{symbol} 硬止损单取消失败（可能已成交）",
                    algo_id=old_sl_id, error_code=e.code
                )
            else:
                logger.warning(
                    f"{symbol} 取消硬止损单异常",
                    algo_id=old_sl_id, error_code=e.code
                )
        except Exception as e:
            logger.warning(
                f"{symbol} 取消硬止损单异常",
                algo_id=old_sl_id, error=str(e)
            )
        algo_ids['sl'] = None
    
    # 3. 计算止损限价（做空：限价 = 止损价 * (1 + offset_pct)，向不利方向偏移）
    stop_limit_price = trailing_stop * (Decimal('1') + stop_offset_pct)
    
    # 4. 精度调整（new_coin 返回 tuple）
    try:
        tick_size, step_size = await self._get_symbol_precision(symbol)
    except Exception:
        tick_size = self.default_tick_size
        step_size = self.default_step_size
    
    stop_limit_price = self._format_price(stop_limit_price, tick_size)
    close_qty = Decimal(str(tracking.get('remaining_quantity', 0)))
    close_quantity = self._format_quantity(close_qty, step_size)
    
    # 5. 下新止损条件单
    logger.info(
        f"{symbol} 下移动止损条件单",
        stop_side=stop_side,
        stop_price=float(trailing_stop),
        limit_price=float(stop_limit_price),
        quantity=float(close_quantity)
    )
    
    try:
        new_order = await self.binance_api.place_conditional_order(
            symbol=symbol,
            side=stop_side,
            stop_price=trailing_stop,
            price=stop_limit_price,
            quantity=close_quantity,
            order_type="STOP",
            reduce_only=True
        )
        
        new_order_id = new_order.get('algoId') or new_order.get('orderId')
        algo_ids['trailing_stop'] = new_order_id
        
        logger.info(
            f"{symbol} 移动止损条件单已创建",
            order_id=new_order_id,
            trailing_stop=float(trailing_stop)
        )
        
        # 记录条件单到数据库
        if new_order_id and self.db and new_order.get('algoId'):
            await record_condition_order(
                self.db, "new_coin", symbol,
                algo_id=new_order['algoId'],
                order_type="STOP_LOSS"
            )
    except Exception as e:
        logger.error(
            f"{symbol} 创建移动止损条件单失败",
            error=str(e), exc_info=True
        )
```

### 3.4 `check_position_management` 修改方案

```python
async def check_position_management(self, symbol: str) -> None:
    """
    检查持仓管理（移动止盈、时间止损、动态利润保护）
    """
    try:
        if symbol not in self.position_tracking:
            return
        
        tracking = self.position_tracking[symbol]
        entry_time = tracking.get('entry_time')
        
        # 1. 检查时间止损
        if self.time_stop_enabled:
            await self._check_time_stop(symbol, entry_time)
        
        # 1.5 检查紧急止损
        if self.emergency_stop_enabled:
            await self._check_emergency_stop(symbol, entry_time)
        
        # 2. 检查动态利润保护（新增，仅在 TP2 到达后激活）
        if tracking.get('target2_reached'):
            # 获取当前价格
            ticker = await self.binance_api._request(
                "GET", "/fapi/v1/ticker/price",
                params={'symbol': symbol}, signed=False
            )
            current_price = Decimal(str(ticker.get('price', 0)))
            
            # 更新最高价（做空时追踪反弹）
            if current_price > Decimal(str(tracking.get('highest_price', 0))):
                tracking['highest_price'] = float(current_price)
            
            await self._check_dynamic_trailing(symbol, current_price)
        
        # 3. 检查移动止盈（原有，TP2 到达后激活）
        if tracking.get('target2_reached'):
            await self._check_trailing_stop(symbol)
    
    except Exception as e:
        logger.error(f"检查持仓管理失败: {symbol}, 错误: {e}")
```

**激活顺序说明：**

```
TP2 到达后，每次 check_position_management 调用顺序：

1. 获取当前价格
2. 更新 highest_price（做空追踪反弹最高价）
3. _check_dynamic_trailing()  ← 动态利润保护：价格从最高价回落触发
4. _check_trailing_stop()     ← ATR 反弹移动止盈：价格从最低价反弹触发

两者不冲突：
- 动态利润保护覆盖"价格冲高回落"场景
- ATR 反弹移动止盈覆盖"价格下跌后反弹"场景
- 形成双保险
```

### 3.5 `__init__` 新增初始化

```python
# 在 __init__ 方法末尾新增
self._volatility_cache: Dict[str, Any] = {}  # 波动率计算缓存
```

### 3.6 辅助方法：`_cancel_trailing_stop_order`

```python
async def _cancel_trailing_stop_order(self, symbol: str) -> None:
    """
    取消移动止损条件单（平仓触发时调用）
    """
    tracking = self.position_tracking.get(symbol, {})
    algo_ids = tracking.get('algo_ids', {})
    dt_config = self.config.get('trading', {}).get('dynamic_trailing', {})
    silent_error_codes = set(dt_config.get('cleanup_silent_error_codes', [-2022, -2011]))
    
    old_id = algo_ids.get('trailing_stop')
    if old_id is not None:
        try:
            await self.binance_api.cancel_algo_order(symbol, old_id)
        except BinanceAPIError as e:
            if e.code not in silent_error_codes:
                logger.warning(
                    f"{symbol} 取消移动止损条件单失败",
                    algo_id=old_id, error_code=e.code
                )
        except Exception as e:
            logger.warning(
                f"{symbol} 取消移动止损条件单异常",
                algo_id=old_id, error=str(e)
            )
        algo_ids['trailing_stop'] = None
```

---

## 四、配置结构设计

### 4.1 new_coin config.yaml 新增配置节

在 `trading` 下新增：

```yaml
# 动态利润保护（移动止损）
# 源自 btc_eth risk_config.dynamic_trailing，适配 new_coin
dynamic_trailing:
  enabled: true
  activation:
    min_profit_pct: 1.5    # 浮盈计算阈值（用于阶梯计算，不作为激活条件）
    also_on_tp1: false      # TP1 触发不激活（new_coin：仅 TP2 激活）
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

### 4.2 配置项对照表

| 配置项路径 | btc_eth 配置路径 | new_coin 配置路径 | 说明 |
|-----------|-----------------|-------------------|------|
| `enabled` | `risk_config.dynamic_trailing.enabled` | `trading.dynamic_trailing.enabled` | 两策略路径不同，但传入 shared 函数时是同一结构 |
| `activation.min_profit_pct` | `risk_config.dynamic_trailing.activation.min_profit_pct` | `trading.dynamic_trailing.activation.min_profit_pct` | 一致 |
| `activation.also_on_tp1` | `risk_config.dynamic_trailing.activation.also_on_tp1` | `trading.dynamic_trailing.activation.also_on_tp1` | new_coin 固定 false |
| `activation.also_on_tp2` | 无 | `trading.dynamic_trailing.activation.also_on_tp2` | new_coin 特有 |
| `regression_tiers` | `risk_config.dynamic_trailing.regression_tiers` | `trading.dynamic_trailing.regression_tiers` | 一致 |
| `volatility_adjustment` | `risk_config.dynamic_trailing.volatility_adjustment` | `trading.dynamic_trailing.volatility_adjustment` | 一致 |
| `stop_limit_order.offset_pct` | `risk_config.stop_limit_order.offset_pct` | `trading.dynamic_trailing.stop_limit_order.offset_pct` | new_coin 放在 dynamic_trailing 下 |
| `cleanup_silent_error_codes` | `risk_config.cleanup_silent_error_codes` | `trading.dynamic_trailing.cleanup_silent_error_codes` | new_coin 放在 dynamic_trailing 下 |

### 4.3 关键配置差异说明

1. **`stop_limit_order` 和 `cleanup_silent_error_codes` 放在 `dynamic_trailing` 下**：new_coin 的 `dynamic_trailing` 是独立功能模块，这些配置仅与该功能相关，放在模块内更内聚。btc_eth 的 `stop_limit_order` 和 `cleanup_silent_error_codes` 是全局配置，因历史原因放在 `risk_config` 顶层。

2. **`stop_loss_atr_multiplier` 使用已有配置**：new_coin 使用 `trading.atr_stop.multiplier`（默认 2.5），而非 btc_eth 的 `risk_config.stop_loss_atr_multiplier`（1.5）。两策略硬止损倍数不同，new_coin 更激进（2.5x vs 1.5x），这是合理的策略差异。

---

## 五、两策略差异对照表

### 5.1 关键差异矩阵

| 维度 | btc_eth | new_coin | shared 层处理方案 |
|------|---------|----------|-----------------|
| 持仓数据结构 | `PositionState` 类（属性访问） | `position_tracking` 字典 | shared 函数接受解构参数，不关心来源 |
| 方向 | 支持 LONG/SHORT | 固定 SHORT | shared 函数通过 `direction` 参数区分 |
| 激活条件 | 浮盈>=1.5% 或 TP1 触发 | 仅 TP2 到达后激活 | `also_on_tp1`, `also_on_tp2` 配置项区分 |
| API 调用对象 | `self.binance` | `self.binance_api` | 策略层各自实现 `_sync_trailing_stop_order` |
| 精度获取 | `_get_symbol_precision` 返回 dict | `_get_symbol_precision` 返回 tuple | 策略层各自处理精度 |
| 精度调整方法 | `_adjust_price_precision` / `_adjust_quantity_precision` | `_format_price` / `_format_quantity` | 策略层各自处理 |
| 数据库 | `self.db_manager` | `self.db` | 策略层传入，`record_condition_order` 接口一致 |
| 硬止损倍数 | `risk_config.stop_loss_atr_multiplier` (1.5) | `trading.atr_stop.multiplier` (2.5) | shared 函数接收 `stop_loss_atr_multiplier` 参数 |
| 波动率缓存 | `self._base_atr_cache` (实例属性) | `self._volatility_cache` (新加) | shared 函数接收 cache 字典参数 |
| 配置读取路径 | `self.risk_config['dynamic_trailing']` | `self.config['trading']['dynamic_trailing']` | shared 函数直接接收配置字典 |
| 平仓方法 | `_close_position(symbol, position, close_quantity, close_reason, current_price)` | `_close_position(symbol, close_percent, reason)` | 策略层各自实现平仓 |
| 条件单记录 | `record_condition_order(self.db_manager, "btc_eth", ...)` | `record_condition_order(self.db, "new_coin", ...)` | 策略层各自调用 |

### 5.2 精度处理差异详解

**btc_eth 精度处理流程：**
```python
precision = await self._get_symbol_precision(symbol)  # 返回 dict
tick_size = Decimal(str(precision.get('tick_size', '0.01')))
step_size = Decimal(str(precision.get('step_size', '0.001')))
stop_limit_price = self._adjust_price_precision(stop_limit_price, tick_size)
close_quantity = self._adjust_quantity_precision(position.current_quantity, step_size)
```

**new_coin 精度处理流程：**
```python
tick_size, step_size = await self._get_symbol_precision(symbol)  # 返回 tuple
stop_limit_price = self._format_price(stop_limit_price, tick_size)
close_quantity = self._format_quantity(close_qty, step_size)
```

**结论：** 精度处理差异较大，不适合在 shared 层统一。各策略在 `_sync_trailing_stop_order` 中自行处理。

---

## 六、btc_eth 改为调用 shared 层的评估

### 6.1 可行性分析

| 评估维度 | 分析 |
|---------|------|
| 函数签名匹配度 | `_calculate_dynamic_trailing_stop` 需要从 `PositionState` 提取参数，与 shared 函数参数一一对应 |
| 副作用处理 | shared 函数是纯函数，无副作用；btc_eth 现有代码直接修改 `position` 属性，需改为返回结果后外部赋值 |
| 异步调用迁移 | `_get_volatility_adjustment` 可改为调用 shared 层异步函数 |
| 配置路径 | `self.risk_config` 需在调用前提取为字典传入 |
| 缓存管理 | `self._base_atr_cache` 需传入 shared 函数 |

### 6.2 改造方案

如果 btc_eth 改为调用 shared 层，`_calculate_dynamic_trailing_stop` 可简化为：

```python
async def _calculate_dynamic_trailing_stop(
    self, symbol: str, position: PositionState, current_price: Decimal
) -> Optional[Decimal]:
    """（改为调用 shared 层版本）"""
    dt_config = self.risk_config.get('dynamic_trailing', {})
    if not dt_config.get('enabled', True):
        return None
    
    vol_config = dt_config.get('volatility_adjustment', {})
    vol_adj = 1.0
    if vol_config.get('enabled', True):
        vol_adj = await get_volatility_adjustment(
            symbol=symbol,
            entry_price=position.entry_price,
            atr=position.atr,
            kline_service=self.kline_service,
            config=vol_config,
            cache=self._get_or_create_volatility_cache(),
        )
    
    result = calculate_dynamic_trailing_stop(
        direction=position.direction,
        entry_price=position.entry_price,
        current_price=current_price,
        highest_price=position.highest_price,
        lowest_price=position.lowest_price,
        trailing_activated=position.trailing_activated,
        tp1_hit=position.tp1_hit,
        tp2_hit=position.tp2_hit,
        pending_profit_pct=position.pending_profit_pct,
        current_tier_index=position.current_tier_index,
        current_trailing_stop_price=position.trailing_stop_price,
        config=dt_config,
        atr=position.atr,
        stop_loss_atr_multiplier=Decimal(str(self.risk_config.get('stop_loss_atr_multiplier', 1.5))),
        volatility_adj=vol_adj,
    )
    
    if result is None:
        return None
    
    # 更新 PositionState
    position.trailing_activated = result['trailing_activated']
    position.pending_profit_pct = result['pending_profit_pct']
    position.current_tier_index = result['current_tier_index']
    position.trailing_stop_price = result['trailing_stop_price']
    
    return result['trailing_stop_price']
```

### 6.3 风险评估

| 风险 | 等级 | 说明 |
|------|------|------|
| 回归风险 | **高** | btc_eth 是线上运行的策略，任何重构都可能导致线上异常 |
| 测试覆盖 | 中 | 需要 mock PositionState 和 kline_service 写完整测试 |
| 代码变更量 | 低 | 约 50-80 行，但涉及核心逻辑路径 |
| 收益 | 中 | 消除重复代码，但 shared 层已设计为兼容，不重构也不影响 new_coin 功能 |

### 6.4 建议

**本次不做 btc_eth 重构，建议延后到 P1 迭代。**

理由：
1. **风险与收益不匹配**：btc_eth 重构带来的收益（减少重复代码）远小于潜在风险（线上策略异常）
2. **shared 层已设计为兼容**：即使不重构，btc_eth 仍可正常运行，shared 层函数与 btc_eth 现有逻辑完全一致
3. **测试成本高**：btc_eth 的 `PositionState` 类逻辑复杂，需要大量 mock 测试
4. **优先级问题**：new_coin 功能激活和验证是 P0，btc_eth 重构是 P1

**建议在以下时机再做 btc_eth 重构：**
- new_coin 动态利润保护功能上线并稳定运行 1 周以上
- shared 层函数经过足够的生产验证
- 有专门的测试覆盖 shared 层与 btc_eth 现有逻辑的一致性

---

## 七、修改文件清单

### 7.1 新增文件

| 文件路径 | 说明 | 预估行数 |
|---------|------|---------|
| `shared/dynamic_trailing.py` | 动态利润保护纯计算逻辑（核心函数 + 辅助函数 + TypedDict） | 250-350 行 |

### 7.2 修改文件

| 文件路径 | 修改内容 | 预估行数 | 影响范围 |
|---------|---------|---------|---------|
| `strategies/new_coin/executor.py` | 1. `__init__` 新增 `_volatility_cache` 初始化<br>2. `execute_short` 扩展 `position_tracking` 初始化<br>3. 新增 `_check_dynamic_trailing()` 方法<br>4. 新增 `_sync_trailing_stop_order()` 方法<br>5. 新增 `_cancel_trailing_stop_order()` 辅助方法<br>6. 修改 `check_position_management()` 增加调用点 | 150-200 行 | 中 |
| `strategies/new_coin/config.yaml` | 新增 `trading.dynamic_trailing` 完整配置节 | 30 行 | 低 |

### 7.3 本次不修改的文件

| 文件路径 | 原因 |
|---------|------|
| `strategies/btc_eth/strategy.py` | 延后到 P1 迭代，本次不做重构 |
| `strategies/new_coin/strategy.py` | 调用点通过 executor 的 `check_position_management()` 透传，无需修改策略层 |
| `strategies/new_coin/main.py` | 无需修改主入口 |
| `shared/condition_orders.py` | 已有 `record_condition_order` 接口，new_coin 直接调用 |

---

## 八、关键设计决策记录

### 决策 1：shared 层函数是否持有策略实例引用

**结论：** 不持有。所有 shared 层函数都是纯计算或通过参数传入依赖。

**理由：**
- 避免循环依赖（shared 层不应 import 策略层）
- 便于单元测试（传入参数即可，无需 mock 策略实例）
- 两策略可独立调用，互不干扰

### 决策 2：activation 的 `also_on_tp2` 配置项放在 shared 层还是策略层

**结论：** 放在 shared 层配置字典中，`calculate_dynamic_trailing_stop` 函数读取。

**理由：**
- 函数签名中已包含 `tp2_hit` 参数
- shared 层函数已经根据 `also_on_tp1` 判断 TP1 激活，同理可处理 `also_on_tp2`
- 这样 btc_eth 未来如果想支持 TP2 激活，只需修改配置即可

### 决策 3：波动率调节的缓存由谁管理

**结论：** 调用方管理（传入 `cache` 字典）。

**理由：**
- shared 层函数不应持有可变状态（否则不是纯函数）
- 调用方（策略实例）可以更好地控制缓存生命周期
- 便于单元测试（传入空字典即可）

### 决策 4：`_sync_trailing_stop_order` 是否放入 shared 层

**结论：** 不放入 shared 层，每个策略各自实现。

**理由：**
- 两策略精度处理方式不同（tuple vs dict, `_format_price` vs `_adjust_price_precision`）
- API 调用对象不同（`self.binance_api` vs `self.binance`）
- 数据库对象不同（`self.db` vs `self.db_manager`）
- 平仓方法签名不同（`_close_position` 参数不同）
- 这些差异无法通过参数传递优雅解决

### 决策 5：`_check_dynamic_trailing` 中 `highest_price` 更新位置

**结论：** 在 `check_position_management()` 中获取当前价格时统一更新，而非在 `_check_dynamic_trailing` 内部。

**理由：**
- 减少重复的 API 调用（获取一次价格，同时用于更新最高价和传入计算函数）
- `check_position_management()` 是主调用入口，逻辑集中
- 未来如果需要在 `_check_trailing_stop()` 中用到 `highest_price`，也可直接使用

### 决策 6：new_coin 硬止损倍数的选择

**结论：** 使用 `trading.atr_stop.multiplier`（默认 2.5），而非 btc_eth 的 1.5。

**理由：**
- new_coin 原本就有独立的 ATR 止损配置（2.5x），这是策略差异
- 动态利润保护中的硬止损作为兜底，应与策略自身的风控一致
- 通过 shared 函数的 `stop_loss_atr_multiplier` 参数传入，可灵活配置

---

## 九、风险与注意事项

### 9.1 实现注意事项

1. **Decimal 精度转换**：`position_tracking` 存储的是 `float`，传入 shared 层函数前需通过 `Decimal(str(value))` 转换，避免精度丢失

2. **`highest_price` 初始化**：做空时初始化为 `current_price`，后续在 `check_position_management` 中更新。注意：如果价格一直下跌，`highest_price` 保持为初始值，这是正确的（因为价格没有反弹）。

3. **`_check_trailing_stop` 和 `_check_dynamic_trailing` 的互斥性**：两者不冲突，一个追踪最低价反弹，一个追踪最高价回落。但需注意：如果动态利润保护触发了平仓，`_check_trailing_stop` 不会执行（因为 `check_position_management` 没有 return，但 `_close_position` 会清理 `position_tracking`，所以后续的 `_check_trailing_stop` 会因 `symbol not in self.position_tracking` 而直接返回）。

4. **条件单清理**：平仓时（`_close_position`）现有逻辑会调用 `cancel_all_algo_orders`，这会清理包括 `trailing_stop` 在内的所有条件单。但为了安全，在 `_check_dynamic_trailing` 触发平仓时，应主动取消 `trailing_stop` 条件单。

5. **首次激活取消硬止损单**：`_sync_trailing_stop_order` 中，首次激活时 `algo_ids['sl']` 不为空，需要取消；后续调用时 `algo_ids['sl']` 已经为 None，跳过。

### 9.2 风险项

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| `highest_price` 更新不及时导致止损价计算偏差 | 低 | 每次 `check_position_management` 获取价格后立即更新 |
| 硬止损单取消失败但动态止损单已创建 | 中 | 日志记录 + 原有 `cancel_all_algo_orders` 兜底清理 |
| 波动率计算中 K 线字段名不兼容 | 低 | shared 函数兼容多字段名 |
| 精度处理错误导致条件单被拒 | 中 | 使用 `_format_price` / `_format_quantity` 确保精度合规 |
| `_close_position` 参数不同导致调用错误 | 中 | 注意 `close_percent` 使用 `self.close_percent` (1.0) |

---

## 十、参考代码

### 10.1 参考文件

| 文件 | 关键行号 | 内容 |
|------|---------|------|
| `strategies/btc_eth/strategy.py` | L29-L62 | PositionState 类定义 |
| `strategies/btc_eth/strategy.py` | L2844-L2930 | `_check_dynamic_trailing` 方法 |
| `strategies/btc_eth/strategy.py` | L2932-L3064 | `_calculate_dynamic_trailing_stop` 核心计算 |
| `strategies/btc_eth/strategy.py` | L3066-L3164 | `_get_volatility_adjustment` 波动率计算 |
| `strategies/btc_eth/strategy.py` | L3166-L3306 | `_sync_trailing_stop_order` 条件单同步 |
| `strategies/btc_eth/config.yaml` | L158-L177 | dynamic_trailing 配置节 |
| `strategies/new_coin/executor.py` | L38-L108 | TradingExecutor 初始化 |
| `strategies/new_coin/executor.py` | L123-L234 | `execute_short` 方法 |
| `strategies/new_coin/executor.py` | L848-L878 | `check_position_management` 方法 |
| `strategies/new_coin/executor.py` | L991-L1056 | `_check_trailing_stop` 方法 |
| `shared/condition_orders.py` | 全部 | 条件单记录模块 |

### 10.2 变更记录

| 版本 | 日期 | 变更内容 | 作者 |
|------|------|---------|------|
| v1.0 | 2026-07-22 | 初稿 | 后端架构师 |