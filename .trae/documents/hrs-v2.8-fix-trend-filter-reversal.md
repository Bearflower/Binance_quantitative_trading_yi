# HRS V2.8 趋势过滤修复方案：反转模式

## Context

### 问题
V2.8 趋势过滤增强后，HRS 策略无开仓信号。根因分析发现两个独立问题：

**问题 1：趋势过滤方向与反转策略相悖**
`_check_lv_rm_trend_filter()` 要求做多时价格必须 **高于** EMA20（`min_price: 1.0`），做空时价格必须 **低于** EMA20（`max_price: 1.0`）。这是趋势跟踪逻辑，但 HRS 是**反转策略**——应该在价格低于 EMA20 时做多（超卖反弹），高于 EMA20 时做空（超买回落）。

当前配置将入场窗口限制在 EMA20 附近极窄的范围（做多：EMA20*0.99~EMA20*1.0；做空：EMA20*1.0~EMA20*1.01），实际运行中几乎不可能触发。

**问题 2：形态评分门槛过高**
`min_primary_pattern_score: 2.5` 过滤了所有偏弱形态（2.0 分），只有完整形态（3.0~4.0 分）才能通过，信号来源严重受限。

### 方案概要
1. 新增 `reversal_mode` 参数，反转标准模式趋势过滤的方向，LV-RM 模式不受影响
2. 降低 `min_primary_pattern_score` 至 2.0，允许偏弱形态通过

---

## 修改文件清单

| 文件 | 改动 |
|------|------|
| `strategies/hrs/scoring_engine.py` | 新增 `reversal_mode` 支持 |
| `strategies/hrs/config.yaml` | 调整趋势过滤参数 + 降低形态门槛 |
| `strategies/hrs/tests/test_scoring_engine.py` | 更新断言值 |
| `strategies/hrs/tests/test_trend_filter_validation.py` | 新增反转模式测试用例 |

---

## 修改详情

### 1. scoring_engine.py — `__init__` 加载 reversal_mode 配置

在 `__init__` 方法中（约第 99-106 行）加载趋势过滤配置后，新增 `trend_filter_reversal_mode` 属性：

```python
self.trend_filter_enabled = trend_filter_config.get("enabled", False)
self.trend_filter_ema_period = trend_filter_config.get("ema_period", 20)
self.trend_filter_long = trend_filter_config.get("long", {})
self.trend_filter_short = trend_filter_config.get("short", {})
# V2.8-FIX: 反转模式
self.trend_filter_reversal_mode = trend_filter_config.get("reversal_mode", False)
```

### 2. scoring_engine.py — `_check_lv_rm_trend_filter()` 新增 reversal_mode 参数和分支逻辑

修改方法签名，新增 `reversal_mode: bool = False` 参数。

在 `direction == "long"` 分支中，当 `reversal_mode=True` 时：
- `min_price` 作为**上限**：价格不能高于 EMA20 * min_price（反转做多要求价格在 EMA20 附近或下方）
- `max_deviation` 作为**下限**：价格不能低于 EMA20 * max_deviation（防止极端超卖入场）

在 `direction == "short"` 分支中，当 `reversal_mode=True` 时：
- `max_price` 作为**下限**：价格不能低于 EMA20 * max_price（反转做空要求价格在 EMA20 附近或上方）
- `max_deviation` 作为**上限**：价格不能高于 EMA20 * max_deviation（防止极端超买入场）

`reversal_mode=False` 时保持原有逻辑不变（LV-RM 模式使用）。

### 3. scoring_engine.py — `_check_standard_trend_filter()` 传入 reversal_mode

在调用 `_check_lv_rm_trend_filter()` 时新增 `reversal_mode=self.trend_filter_reversal_mode` 参数。

### 4. config.yaml — 调整趋势过滤参数

```yaml
scoring:
  trend_filter:
    enabled: true
    reversal_mode: true          # V2.8-FIX: 启用反转模式
    ema_period: 20
    long:
      min_price: 1.01            # 反转做多：价格必须低于 EMA20*1.01（允许略高于 EMA20）
      max_deviation: 0.97        # 反转做多：价格必须不低于 EMA20*0.97（允许最多 3% 低于 EMA20）
    short:
      max_price: 0.99            # 反转做空：价格必须高于 EMA20*0.99（允许略低于 EMA20）
      max_deviation: 1.03        # 反转做空：价格必须不高于 EMA20*1.03（允许最多 3% 高于 EMA20）
    ema_slope:
      enabled: true
      period: 3
      min_slope_for_long: -0.0005
      max_slope_for_short: 0.0005
```

反转模式有效入场窗口：

| 方向 | 下限 | 上限 | 窗口宽度 |
|------|------|------|---------|
| 做多 | EMA20 × 0.97 | EMA20 × 1.01 | 4% |
| 做空 | EMA20 × 0.99 | EMA20 × 1.03 | 4% |

### 5. config.yaml — 降低形态评分门槛

```yaml
scoring:
  technical:
    min_total_score: 4.0
    min_primary_pattern_score: 2.0    # V2.8-FIX: 从 2.5 降至 2.0
```

### 6. 测试文件更新

**test_scoring_engine.py：** `min_primary_pattern_score` 期望值从 2.5 改为 2.0。

**test_trend_filter_validation.py：** 新增 `TestTrendFilterReversalMode` 测试类，包含 4 个测试用例：
- 反转模式做多_价格低于EMA20_允许
- 反转模式做多_价格过高_阻断
- 反转模式做空_价格高于EMA20_允许
- 反转模式做空_价格过低_阻断

---

## 不受影响的范围

- **LV-RM 模式**：直接调用 `_check_lv_rm_trend_filter()` 不传 `reversal_mode`，默认 `False`，行为完全不变
- **EMM/半EMM 模式**：不经过趋势过滤，不受影响
- **现有测试**：不传 `reversal_mode` 或 `klines_4h`，不受影响

---

## V2.8.3 总持仓保证金比例上限检查

### 问题
`config.yaml` 中定义了 `position_sizing.total.account_ratio_cap: 0.30`，但**没有任何代码读取该配置**。总持仓保证金不受控制。同时，`ai_tuner/config.yaml` 中 HRS 资金分配比例为 20%，与 config 中的 30% 不一致。

### 修复

**`strategies/hrs/strategy.py`：** 在 `_open_position` 方法开仓前增加总持仓保证金比例上限检查：

```python
total_sizing_config = self.config.get("position_sizing", {}).get("total", {})
total_ratio_cap = total_sizing_config.get("account_ratio_cap")
if total_ratio_cap is not None:
    max_total_margin = balance * total_ratio_cap
    new_margin = (float(quantity) * float(current_price)) / self.trading_executor.leverage
    current_total_margin = 0.0
    for _, pos_data in self.position_manager.get_all_positions().items():
        pos_qty = abs(float(pos_data.get("entry_quantity", 0) or pos_data.get("quantity", 0)))
        pos_price = float(pos_data.get("entry_price", 0))
        current_total_margin += (pos_qty * pos_price) / self.trading_executor.leverage
    if current_total_margin + new_margin > max_total_margin + 0.001:
        # 跳过开仓，记录日志
        return False
```

**`strategies/hrs/config.yaml`：** `position_sizing.total.account_ratio_cap` 从 0.30 改为 **0.20**，与资金分配比例保持一致。

**部署方式：** 配置文件通过 volume 挂载，只需将更新后的 config.yaml 上传到服务器并重启容器即可生效。

## 验证

1. 运行 `pytest strategies/hrs/tests/ -v` 全部通过
2. 运行 `pytest strategies/hrs/tests/test_trend_filter_validation.py -v` 趋势过滤测试全部通过
3. 部署后观察日志确认趋势过滤阻断原因合理
4. 容器内 `total.account_ratio_cap` 读取验证：`docker exec trading_system-hrs python3 -c 'import yaml; cfg=yaml.safe_load(open("/app/strategies/hrs/config.yaml")); print(cfg["position_sizing"]["total"]["account_ratio_cap"])'`