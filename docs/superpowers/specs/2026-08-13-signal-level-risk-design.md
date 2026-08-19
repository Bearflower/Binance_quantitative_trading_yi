# 信号等级止盈止损设计文档

## 概述

将 btc_eth 策略的全局止盈止损参数拆解为按信号等级（S/A/B/C）独立配置，让高置信度信号使用更宽松的止盈止损（让利润奔跑），低置信度信号使用更严格的止盈止损（快速落袋）。

## 目标

- AI-Tuner 每周为每个信号等级独立调优止盈止损参数
- 策略运行时根据信号等级读取对应的风险参数
- 试点成功后推广到 new_coin 和 HRS 策略

## 设计

### 一、Config 结构变更

**当前**：`strategies/btc_eth/config.yaml` 中所有风险参数是全局的

```yaml
risk:
  stop_loss_atr_multiplier: 1.5
  partial_take_profit:
    tp1_atr_multiplier: 4.0
    tp2_atr_multiplier: 6.0
    tp1_close_ratio: 0.25
    tp2_close_ratio: 0.25
    remaining_ratio: 0.50
  dynamic_trailing:
    enabled: true
    activation:
      min_profit_pct: 1.5
      also_on_tp1: true
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
  time_stop:
    max_holding_hours: 72
    close_ratio: 0.50
```

**变更后**：按信号等级拆解

```yaml
risk:
  signal_levels:
    S:
      stop_loss_atr_multiplier: 2.5
      partial_take_profit:
        tp1_atr_multiplier: 6.0
        tp2_atr_multiplier: 9.0
        tp1_close_ratio: 0.15
        tp2_close_ratio: 0.35
        remaining_ratio: 0.50
      dynamic_trailing:
        activation:
          min_profit_pct: 3.0
          also_on_tp1: true
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
      time_stop:
        max_holding_hours: 96
        close_ratio: 0.50
    A:
      stop_loss_atr_multiplier: 1.5
      partial_take_profit:
        tp1_atr_multiplier: 4.0
        tp2_atr_multiplier: 6.0
        tp1_close_ratio: 0.25
        tp2_close_ratio: 0.25
        remaining_ratio: 0.50
      dynamic_trailing:
        activation:
          min_profit_pct: 1.5
          also_on_tp1: true
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
      time_stop:
        max_holding_hours: 72
        close_ratio: 0.50
    B:
      stop_loss_atr_multiplier: 1.2
      partial_take_profit:
        tp1_atr_multiplier: 3.0
        tp2_atr_multiplier: 5.0
        tp1_close_ratio: 0.30
        tp2_close_ratio: 0.30
        remaining_ratio: 0.40
      dynamic_trailing:
        enabled: true
        activation:
          min_profit_pct: 1.0
          also_on_tp1: true
        regression_tiers:
          - profit_ceiling: 1.5
            retrace_ratio: 0.0
          - profit_ceiling: 3.0
            retrace_ratio: 0.5
          - profit_ceiling: 6.0
            retrace_ratio: 0.35
          - profit_ceiling: 999.0
            retrace_ratio: 0.25
        volatility_adjustment:
          enabled: true
          atr_lookback_days: 30
          atr_period: 14
          cache_ttl_seconds: 3600
      time_stop:
        max_holding_hours: 48
        close_ratio: 0.60
    C:
      stop_loss_atr_multiplier: 1.0
      partial_take_profit:
        tp1_atr_multiplier: 2.0
        tp2_atr_multiplier: 3.5
        tp1_close_ratio: 0.40
        tp2_close_ratio: 0.40
        remaining_ratio: 0.20
      dynamic_trailing:
        enabled: true
        activation:
          min_profit_pct: 0.5
          also_on_tp1: true
        regression_tiers:
          - profit_ceiling: 1.0
            retrace_ratio: 0.0
          - profit_ceiling: 2.0
            retrace_ratio: 0.5
          - profit_ceiling: 4.0
            retrace_ratio: 0.35
          - profit_ceiling: 999.0
            retrace_ratio: 0.25
        volatility_adjustment:
          enabled: true
          atr_lookback_days: 30
          atr_period: 14
          cache_ttl_seconds: 3600
      time_stop:
        max_holding_hours: 24
        close_ratio: 0.80

  # 以下保持全局（不按信号等级区分）
  close_limit_order: ...
  stop_limit_order: ...
  tp_limit_order: ...
  micro_close: ...
```

### 二、拆解范围

**按信号等级拆解的参数**：
- `stop_loss_atr_multiplier`
- `partial_take_profit.*`（tp1/tp2 atr_multiplier + close_ratio + remaining_ratio）
- `dynamic_trailing.*`（激活条件 + 回撤阶梯 + 波动率调整）
- `time_stop.*`（max_holding_hours + close_ratio）

**保持全局的参数**（不拆解）：
- `close_limit_order.*`（平仓执行机制，与信号等级无关）
- `stop_limit_order.*`（止损偏移，与逻辑无关）
- `tp_limit_order.*`（止盈偏移，与逻辑无关）
- `micro_close.*`（微持仓处理，与逻辑无关）

### 三、策略代码变更

#### 3.1 信号等级贯穿持仓生命周期

当前 `grade` 只在信号分析阶段确定，下单后不再使用。
变更后，`grade` 需要存入 PositionState 对象，在持仓监控期间持续使用。

```python
class PositionState:
    # ... 现有字段 ...
    grade: str = ""  # 新增：信号等级，用于动态读取对应的风险参数
```

#### 3.2 风险参数读取方式

```python
# 新增工具方法
def _get_grade_risk(self, grade: str) -> Dict:
    """根据信号等级获取对应的风险参数"""
    fallback = self.risk_config.get('signal_levels', {}).get('A', {})
    return self.risk_config.get('signal_levels', {}).get(grade, fallback)

# 使用方式变更
# 止损计算
grade_risk = self._get_grade_risk(position.grade)
sl_atr_mult = grade_risk['stop_loss_atr_multiplier']

# 止盈计算
partial_config = grade_risk['partial_take_profit']
tp1_atr_mult = partial_config['tp1_atr_multiplier']
```

#### 3.3 涉及的方法变更

| 方法 | 变更内容 |
|------|---------|
| `place_order()` | 分析阶段确定 grade，存入 signal 和 position |
| `_calculate_initial_stop()` | 改为从 `_get_grade_risk(grade)` 读取参数 |
| `_calculate_tp_price()` | 改为从 `_get_grade_risk(grade)` 读取参数 |
| `_check_partial_take_profit()` | 改为从 `_get_grade_risk(grade)` 读取参数 |
| `_check_dynamic_trailing()` | 改为从 `_get_grade_risk(grade)` 读取参数 |
| `_check_time_stop()` | 改为从 `_get_grade_risk(grade)` 读取参数 |

### 四、AI-Tuner 白名单变更

```yaml
# ai_tuner/config.yaml - btc_eth 策略白名单
param_whitelist:
  # 原有评分参数保持不变
  - "scoring.min_score"
  - "scoring.weights.trend_strength"
  - "scoring.weights.pattern_quality"
  - "scoring.weights.momentum_divergence"
  
  # 原有仓位/杠杆参数保持不变
  - "binance.leverage.S"
  - "binance.leverage.A"
  - "binance.position_ratio.S"
  - "binance.position_ratio.A"
  - "binance.position_ratio.B"
  - "binance.position_ratio.C"
  
  # 新增：信号等级止盈止损参数（替代原全局风险参数）
  - "risk.signal_levels.S.stop_loss_atr_multiplier"
  - "risk.signal_levels.A.stop_loss_atr_multiplier"
  - "risk.signal_levels.B.stop_loss_atr_multiplier"
  - "risk.signal_levels.C.stop_loss_atr_multiplier"
  - "risk.signal_levels.S.partial_take_profit.tp1_atr_multiplier"
  - "risk.signal_levels.A.partial_take_profit.tp1_atr_multiplier"
  - "risk.signal_levels.B.partial_take_profit.tp1_atr_multiplier"
  - "risk.signal_levels.C.partial_take_profit.tp1_atr_multiplier"
  - "risk.signal_levels.S.partial_take_profit.tp2_atr_multiplier"
  - "risk.signal_levels.A.partial_take_profit.tp2_atr_multiplier"
  - "risk.signal_levels.B.partial_take_profit.tp2_atr_multiplier"
  - "risk.signal_levels.C.partial_take_profit.tp2_atr_multiplier"
  - "risk.signal_levels.S.partial_take_profit.tp1_close_ratio"
  - "risk.signal_levels.A.partial_take_profit.tp1_close_ratio"
  - "risk.signal_levels.B.partial_take_profit.tp1_close_ratio"
  - "risk.signal_levels.C.partial_take_profit.tp1_close_ratio"
  - "risk.signal_levels.S.partial_take_profit.tp2_close_ratio"
  - "risk.signal_levels.A.partial_take_profit.tp2_close_ratio"
  - "risk.signal_levels.B.partial_take_profit.tp2_close_ratio"
  - "risk.signal_levels.C.partial_take_profit.tp2_close_ratio"
  - "risk.signal_levels.S.partial_take_profit.remaining_ratio"
  - "risk.signal_levels.A.partial_take_profit.remaining_ratio"
  - "risk.signal_levels.B.partial_take_profit.remaining_ratio"
  - "risk.signal_levels.C.partial_take_profit.remaining_ratio"
  - "risk.signal_levels.S.dynamic_trailing.activation.min_profit_pct"
  - "risk.signal_levels.A.dynamic_trailing.activation.min_profit_pct"
  - "risk.signal_levels.B.dynamic_trailing.activation.min_profit_pct"
  - "risk.signal_levels.C.dynamic_trailing.activation.min_profit_pct"
  - "risk.signal_levels.S.time_stop.max_holding_hours"
  - "risk.signal_levels.A.time_stop.max_holding_hours"
  - "risk.signal_levels.B.time_stop.max_holding_hours"
  - "risk.signal_levels.C.time_stop.max_holding_hours"
  - "risk.signal_levels.S.time_stop.close_ratio"
  - "risk.signal_levels.A.time_stop.close_ratio"
  - "risk.signal_levels.B.time_stop.close_ratio"
  - "risk.signal_levels.C.time_stop.close_ratio"
  
  # 原有频率控制参数保持不变
  - "risk.frequency_control.max_daily_total_trades"
  - "risk.frequency_control.max_daily_symbol_trades"
  - "risk.frequency_control.symbol_cooldown_hours"
  - "risk.frequency_control.consecutive_loss_pause"
  - "risk.frequency_control.pause_duration_hours"
  - "risk.frequency_control.max_daily_loss_usdt"
```

### 五、数据流

```
AI-Tuner 每周调优
    ↓
写入 signal_levels.S/A/B/C 各等级参数
    ↓
策略 `analyze()` 运行
    ↓
计算评分 → `_determine_grade()` → 确定 grade (S/A/B/C)
    ↓
`place_order()` 读取 grade 对应的 risk 参数
    ↓
计算止损价、止盈价 → 下单
    ↓
grade 存入 PositionState
    ↓
持仓监控循环中，用 `position.grade` 读取对应 risk 参数
    ↓
检查 TP1/TP2/动态止损/时间止损
```

### 六、向后兼容

- 旧 config 无 `signal_levels` 字段 → 自动回退到全局参数行为
- 检查 `risk_config.get('signal_levels')` 是否存在，不存在则使用旧逻辑
- 数据库中的持仓记录无 `grade` 字段 → 默认使用 'A' 级参数

### 七、测试要点

1. 各信号等级参数读取正确性
2. 信号等级向下兼容（无 signal_levels 时回退）
3. 持仓 grade 字段持久化
4. 动态止损/时间止损按信号等级差异化执行
5. AI-Tuner 白名单参数调优

## 风险与注意事项

- 白名单数量从 ~24 个膨胀到 ~60 个，AI-Tuner 每周调优范围增大
- 初始参数值需要基于历史回测确定，不能拍脑袋
- 建议先部署后观察 1-2 周，确认无异常再激活 AI-Tuner 调优