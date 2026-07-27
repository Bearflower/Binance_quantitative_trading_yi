# btc_eth 策略 v6.16.10 补全重构计划

## 概要

v6.16.10 系统性重构的主体工作已完成（config.yaml 全量更新、strategy.py 新增 6 个方法、analyze() 流程重构），本计划仅覆盖 **4 个剩余缺失项**。

下单交易模块（`execute_signal`、`_close_position`、`_cleanup_position_orders` 等）**确认无需改动**。

---

## 当前状态

### 已完成项（无需再动）

| 模块 | 方法/配置 | 状态 |
|------|----------|------|
| config.yaml | symbol_config、trend_filter、prohibition、dynamic_atr、dynamic_volume、extreme_market、liquidation_warning、position_management | 已更新 |
| strategy.py | `_check_trend_filter()` | 已实现 |
| strategy.py | `_check_prohibited_conditions()` | 已实现 |
| strategy.py | `_check_volume_filter()` | 已实现 |
| strategy.py | `_determine_grade()` 币种差异化 S 级阈值 | 已实现 |
| strategy.py | `_calculate_score()` A 级额外加分 | 已实现 |
| strategy.py | `_check_extreme_market()` | 已实现 |
| strategy.py | `_check_liquidation_warning()` | 已实现 |
| strategy.py | `DynamicATRFilter` 集成 | 已实现 |
| strategy.py | `analyze()` 流程重构 | 已实现 |

### 不改动模块（确认）

| 模块 | 方法 | 原因 |
|------|------|------|
| 下单 | `execute_signal()` | 开仓流程不变 |
| 平仓 | `_close_position()` | 平仓逻辑不变 |
| 限价优化 | `_get_optimized_price()` | 不变 |
| 精度 | `_get_symbol_precision()`、`_adjust_*_precision()` | 不变 |
| 条件单清理 | `_cleanup_position_orders()`、`_cleanup_residual_orders()`、`cleanup_orphan_algo_orders()` | 不变 |
| 止盈止损 | `_check_partial_take_profit()`、`_check_chandelier_stop()`、`_check_time_stop()` | 不变 |
| 调度 | `main.py` | 不变 |

---

## 缺失项与修改方案

### 缺失项 1：`update_positions()` 未集成风控检查

**位置：** [strategy.py#L1811-L1855](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L1811)

**问题：** `_check_liquidation_warning()` 和 `_check_extreme_market()` 已实现但从未被调用。

**修改方案：** 在 `update_positions()` 的持仓循环中，获取当前价后、更新最高/最低价之前，插入强平预警和极端行情检查：

```python
async def update_positions(self):
    for symbol, position in self.positions.items():
        if position.current_quantity <= 0:
            continue
        
        current_price = await self._get_current_price(symbol)
        if current_price is None:
            continue
        
        # 【新增】强平预警检查（v6.16.10）
        liq_triggered = await self._check_liquidation_warning(symbol, position)
        if liq_triggered:
            continue  # 已全部平仓或减仓，跳过后续检查
        
        # 【新增】极端行情检查（v6.16.10）
        extreme_triggered = await self._check_extreme_market(symbol, position, current_price)
        if extreme_triggered:
            continue  # 已处理极端行情，跳过后续检查
        
        # 更新最高/最低价
        if position.direction == 'LONG':
            ...
```

**改动量：** ~10 行

---

### 缺失项 2：`_calculate_position_size()` 未实现波动率目标仓位

**位置：** [strategy.py#L1291-L1381](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L1291)

**问题：** 当前实现仅使用 `usable_balance × position_ratio`，未按 v6.16.10 规范实现波动率目标仓位：
> 单笔风险 = 10U × (历史中位ATR% / 当前ATR%)，限制 [5U, 15U]

**修改方案：** 在仓位计算末尾，对 `position_size` 应用波动率调整系数：

```python
# 波动率目标仓位调整（v6.16.10）
pm_config = self.risk_config.get('position_management', {})
if pm_config.get('volatility_target_risk', 0) > 0:
    target_risk = Decimal(str(pm_config['volatility_target_risk']))  # 10U
    min_risk = Decimal(str(pm_config.get('volatility_target_min', 5)))
    max_risk = Decimal(str(pm_config.get('volatility_target_max', 15)))
    
    # 从动态ATR过滤器获取历史中位ATR%和当前ATR%
    if self.atr_filter and self.atr_filter.enabled:
        stats = self.atr_filter.get_statistics(symbol)
        median_atr_pct = Decimal(str(stats.get('percentile_50', 1.0)))
        current_atr_pct = Decimal(str(stats.get('current_atr_pct', 1.0)))
        
        if current_atr_pct > 0 and median_atr_pct > 0:
            vol_ratio = median_atr_pct / current_atr_pct
            risk_amount = target_risk * vol_ratio
            risk_amount = max(min_risk, min(max_risk, risk_amount))
            
            # 调整仓位：position_size × (risk_amount / target_risk)
            position_size = position_size * (risk_amount / target_risk)
```

**注意：** 此改动依赖缺失项 4（`DynamicATRFilter.get_statistics()` 需返回 `current_atr_pct`）。

**改动量：** ~20 行

---

### 缺失项 3：`FrequencyController.can_trade()` 未检查百分比每日亏损

**位置：** [strategy.py#L213-L273](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L213)

**问题：** 当前仅检查绝对亏损 `max_daily_loss_usdt`（25U），未检查百分比亏损 `max_daily_loss_ratio`（5%）。

**修改方案：** 在每日最大亏损检查处，同时检查绝对值和百分比：

```python
# 检查每日最大亏损（绝对值 + 百分比双重限制）
today = current_time.date().isoformat()
if today in self.daily_pnl:
    max_loss_abs = Decimal(str(self.config['max_daily_loss_usdt']))
    max_loss_ratio = Decimal(str(self.config.get('max_daily_loss_ratio', 0.05)))
    initial_capital = Decimal(str(self.config.get('initial_capital_usdt', 500)))
    
    loss_limit = max(max_loss_abs, initial_capital * max_loss_ratio)
    if self.daily_pnl[today] <= -loss_limit:
        return False, f"已达每日最大亏损限额{float(loss_limit):.1f}U"
```

**改动量：** ~6 行（替换原有 3 行）

---

### 缺失项 4：`DynamicATRFilter.get_statistics()` 缺少所需字段

**位置：** [shared/dynamic_atr_filter.py#L282-L315](file:///Users/yl/vscode/Binance_quantitative_trading/shared/dynamic_atr_filter.py#L282)

**问题：** `get_statistics()` 返回 `percentile_20`、`percentile_50`、`percentile_80`，但：
1. 波动率目标仓位计算需要 `percentile_35`（配置中的 `atr_percentile` 值）
2. 需要 `current_atr_pct`（当前最新 ATR%）

**修改方案：** 在 `get_statistics()` 返回值中增加 `percentile_35` 和 `current_atr_pct` 字段：

```python
def get_statistics(self, symbol: str) -> Dict:
    ...
    atr_percents = list(self._atr_history[symbol])
    
    return {
        ...
        'percentile_35': float(np.percentile(atr_percents, 35)),
        'current_atr_pct': atr_percents[-1] if atr_percents else None,
        ...
    }
```

**改动量：** ~2 行

---

## 实施顺序

| 顺序 | 缺失项 | 文件 | 行数 | 依赖 |
|------|--------|------|------|------|
| 1 | 缺失项 4 | `shared/dynamic_atr_filter.py` | ~2 行 | 无 |
| 2 | 缺失项 2 | `strategies/btc_eth/strategy.py` | ~20 行 | 缺失项 4 |
| 3 | 缺失项 3 | `strategies/btc_eth/strategy.py` | ~6 行 | 无 |
| 4 | 缺失项 1 | `strategies/btc_eth/strategy.py` | ~10 行 | 无 |

**总计：约 38 行新增/修改代码**

---

## 验证方案

1. **语法检查：** `python -m py_compile strategies/btc_eth/strategy.py shared/dynamic_atr_filter.py`
2. **代码规范检查：** 调用 `code-specification-inspector` 检查硬编码、命名规范
3. **逻辑验证：**
   - 确认 `update_positions()` 中强平预警在极端行情之前执行（先救命再减伤）
   - 确认波动率目标仓位计算使用正确的 `percentile_50` 作为历史中位值
   - 确认每日亏损取 `max(绝对值, 百分比)` 双重限制
4. **代码审查：** 调用 `TRAE-code-review` 进行审查

---

## 风险与注意事项

1. **波动率目标仓位需要 ATR 历史数据积累：** 启动初期 `DynamicATRFilter` 历史不足时，`percentile_50` 可能为 None，需做降级处理（使用默认值）
2. **强平预警依赖 `get_account_info()` 返回 `positions` 字段：** 需确认 PM 账户 API 返回格式一致
3. **极端行情处理中 `_close_position()` 会修改 `position.current_quantity`：** 后续 `_check_extreme_market` 返回后需 `continue` 跳过，避免基于已变更数量做后续检查