# 波动率目标仓位（Vol-Target 30%/30）落地方案

**文档版本**：v1.2（实施规格版）  
**交付日期**：2026-09-18  
**课题名称**：波动率目标仓位在多策略体系中的顶层资金分配应用  
**交付状态**：正式交付，可作为实施规格书  
**适用范围**：作为多策略体系的顶层总仓位分配器，叠加在 btc_eth / new_coin / grid / hrs 之上  
**核心结论**：推荐目标年化波动率 30%、滚动窗口 30 天、最大杠杆 1.5 的 Vol-Target 方案。不预测方向，只按波动率反向调整总仓位，目标是在尽量保留收益的同时大幅压缩回撤并提升夏普与 Calmar。

**v1.2 相对 v1.1 的变更**：

1. 修正单日暴跌归属与阈值：移到 `risk.global`，阈值对齐全局 5%，动作改为 `set_max_weight`，明确不叠加缩仓；
2. 明确 `vol_anomaly` 动作，并拆成“组合级波动异常”与“vol-target 参数健康度”两条；
3. 新增多风控同时触发的“取严不叠加”规则；
4. 新增恢复条件与冷却期表；
5. 新增 `universe.yaml`，明确 point-in-time 币池；
6. 新增 `vol_target.yaml`，策略参数与风控参数分离；
7. 新增 `risk_state` 状态持久化结构；
8. 明确与 AI-tuner `account_ratio_caps` 的取严关系；
9. 补全 Vol-Target 输出接口字段；
10. 补全波动率估计细节、回测费用假设、换手参数；
11. 补全飞书审计字段；
12. 补全灰度接口、运维失败处理、测试清单；
13. 清理 typo 与命名。

---

## 一、背景与目标

### 1.1 问题

Top30 等权组合年化约 +18.0%，但最大回撤约 66.0%、波动约 79.9%、夏普仅 0.23。收益可观，“收益-风险”比差。

此前试过方向择时（BTC 均线）控回撤，结果：

| 方案 | 年化 | 最大回撤 | 夏普 | 结论 |
|---|---:|---:|---:|---|
| 纯等权躺平 | +18.0% | 66.0% | 0.23 | 基准 |
| 均线择时空仓（SMA100） | -12.9% | 57.3% | -0.24 | 压回撤有限，收益崩 |
| 分级降仓（SMA100） | -7.1% | 54.5% | -0.13 | 同上 |
| **Vol-Target 30%/30** | **+21.2%** | **29.1%** | **0.66** | **回撤大降 + 夏普提升** |

**核心洞察**：方向择时（猜涨跌）无法在控回撤同时保收益；波动率目标仓位不赌方向，是唯一同时实现“回撤大幅下降、风险调整后收益提升”的方案。

### 1.2 目标

1. 将组合最大回撤从约 66% 压缩到 30% 以内；
2. 将年化波动从约 80% 压缩到 30% 左右；
3. 年化收益尽量保持在 20% 附近，不出现均线择时式崩坏；
4. 夏普和 Calmar 显著优于躺平；
5. 与现有 PM 账户、`risk.yaml` 风控、飞书推送无缝衔接。

---

## 二、定义与核心逻辑

### 2.1 一句话定义

**持仓规模反比于当前市场波动。波动大就少拿，波动小就多拿，不判断方向，只维持目标风险。**

### 2.2 核心公式

设目标年化波动率 `σ_target = 30%`、当前预测年化波动率 `σ_forecast`、最大杠杆 `max_leverage = 1.5`：

```text
σ_forecast = std(组合日收益[ t-W : t-1 ]) × √365      # W = 30 滚动窗
w_raw      = σ_target / σ_forecast
w          = clip( w_raw, 0, max_leverage )
当日组合收益 = w × 基础组合日收益
```

- 波动高 → `w` 自动变小 → 少暴露，躲过大跌段；
- 波动低 → `w` 放大（受 `max_leverage` 上限约束）→ 在趋势段多赚。

### 2.3 防未来函数（强制）

`w_t` **只能用 `t-1` 及之前的数据**计算：

- 滚动 std 在 `t-1` 时刻取值，即对日收益序列 `rolling(W).std().shift(1)`；
- 严禁使用当日收盘/当日收益，否则构成未来函数，回测与实盘口径不一致。

这是从研究代码到生产代码迁移时最易被破坏的约束，必须写在实现注释与测试断言里。

### 2.4 参数含义

“Vol-Target 30%/30” = **目标年化波动率 30% + 滚动窗口 30 交易日**。它不是“30% 仓位”，而是“让组合年化波动尽量贴近 30%”。

---

## 三、参数与计算规则

### 3.1 推荐参数表

| 参数 | 推荐值 | 说明 |
|---|---:|---|
| 目标年化波动率 | 30% | 甜点区，兼顾收益与回撤 |
| 滚动窗口 | 30 天 | 反应速度与稳定性平衡 |
| 年化因子 | √365 | 加密资产 7×24 交易 |
| 最大杠杆 | 1.5 | 防止低波动时过度放大 |
| 最小仓位 | 0 | 极端风险可清仓 |
| 调仓频率 | 每日 | 收盘后计算，次日执行 |
| 执行时点 | T+1 | 避免未来函数 |
| 换手成本假设 | 单边 0.05% | 实际按交易所与滑点调整 |
| 调仓缓冲区 | `\|Δw\| < 5%` 不调 | 降低换手 |
| 单日最大换手 | 50% | 硬约束，防止一次性大调 |
| 最小调仓单位 | 1% | 低于此不调 |
| 基础组合币池 | 平均成交额前 30、等权、季度再平衡 | 与回测口径一致 |

> 已移除原稿的“EMA 3 日信号平滑”——`|Δw|<5%` 缓冲已足够，再叠 EMA 会延迟对急跌的反应。

### 3.2 波动率估计细节（强制统一）

| 项 | 规定 |
|---|---|
| 收益率类型 | 简单收益率 `r_t = P_t / P_{t-1} - 1` |
| 价格来源 | 与回测一致的数据源（默认 Binance 现货 UTC 00:00 收盘） |
| 日切点 | UTC 00:00 |
| 缺失值处理 | 用上一有效价格计算收益；连续缺失 > 1 日则触发数据质量告警 |
| 年化因子 | √365 |
| 窗口 | 30 个交易日，含 `t-1`，不含 `t` |
| 计算时点 | 每日 UTC 00:10 |

### 3.3 每日计算步骤

1. 获取基础组合（前 30 等权）的日收益序列；
2. 取 `t-1` 及之前最近 30 个交易日日收益；
3. 计算日收益标准差，年化 `σ_forecast = 日标准差 × √365`；
4. 计算原始仓位 `w_raw = 30% / σ_forecast`；
5. 截断 `w = clip(w_raw, 0, 1.5)`；
6. 叠加风控硬约束（见第七章，风控优先级高于 Vol-Target）；
7. 生成次日执行指令；
8. 推送飞书日报。

---

## 四、回测依据与预期表现

### 4.1 回测结果

基于 Top30 等权组合（平均成交额前 30、2.7 年 / 999 交易日）：

| 方案 | 年化 | 波动 | 夏普 | 最大回撤 | Calmar |
|---|---:|---:|---:|---:|---:|
| 纯等权躺平 | +18.0% | 79.9% | 0.23 | 65.96% | 0.27 |
| 均线择时 SMA100 | -12.9% | — | -0.24 | 57.3% | -0.23 |
| **Vol-Target 30%/30** | **+21.2%** | **32.2%** | **0.66** | **29.1%** | **0.73** |
| 保守版 Vol-Target 20%/30 | +15.0% | 21.5% | 0.70 | 20.2% | 0.74 |

- 回撤 66% → 29%，降幅约 56%；
- 年化不降反升至 21.2%；
- 夏普 0.23 → 0.66，Calmar 0.27 → 0.73。

**30%/30 是推荐主方案**；风险偏好更低可选 20%/30。

### 4.2 回测假设（必须注明）

| 项 | 假设 |
|---|---|
| 样本区间 | 999 交易日，约 2.7 年 |
| 换手成本 | 单边 0.05%，含滑点 |
| 杠杆成本 | `w > 1` 时按资金费率计入（若回测未计，实盘需重新校准） |
| 无风险利率 | 夏普计算默认 rf = 0 |
| 执行假设 | T 日收盘计算，T+1 开盘执行 |
| 币池 | point-in-time 前 30 等权，季度再平衡 |
| 生存偏差 | 已排除退市/停牌币，但需确认回测是否 point-in-time |
| 子区间稳健性 | 需补充分年度、分市场状态（牛/熊/震荡）表现 |

> 若实盘包含杠杆资金费率，年化 21.2% 可能被高估，需在 v1.3 回测中补算。

---

## 五、基础组合币池定义（`universe.yaml`）

### 5.1 设计原则

Vol-Target 的 `σ_target=30%`、`window=30`、`max_leverage=1.5` 都是基于“Top30 等权组合”回测出来的。**基础组合一变，参数含义就变，回测结果不再对应实盘。**

因此正式定义：

> **波动统计基准组合 = 实盘风险基准组合 = 回测基础组合 = Top30 等权、季度再平衡、point-in-time。**

### 5.2 `universe.yaml` 完整配置

```yaml
# configs/universe.yaml
version: 1
meta:
  name: top30_equal_weight
  purpose: volatility_estimation_benchmark
  owner: research
  description: "Vol-Target 波动估计基础组合：平均成交额前30、等权、季度再平衡"
  config_version: "2026.09.18-1"

universe:
  ranking:
    metric: avg_daily_quote_volume_usd
    window_days: 30
    source: binance_spot
    min_history_days: 90
    exclude:
      - stablecoins          # USDT, USDC, DAI, TUSD, FDUSD 等
      - wrapped_assets       # WBTC, WETH 等
      - leveraged_tokens     # BTCUP, BTCDOWN 等
      - delisted_or_suspended
    exclude_symbols: []      # 手工补充黑名单

  size: 30
  weighting: equal

  rebalance:
    frequency: quarterly
    calc_time: "UTC 00:00 last day of quarter"
    execute_time: "next day UTC 00:10"
    use_t_minus_1: true
    point_in_time: true       # 历史回测必须用当时名单，禁止当前名单回溯

  return_calculation:
    frequency: daily
    return_type: simple       # simple | log
    annualization: sqrt(365)
    price_time: "UTC 00:00"
    missing_data: use_last_valid_price
    max_missing_days: 1

  audit:
    snapshot_dir: "data/universe_snapshots/"
    snapshot_format: "universe_YYYYMMDD.csv"
    record_fields:
      - date
      - symbol
      - rank
      - avg_daily_quote_volume_usd
      - data_source
      - config_hash
```

### 5.3 关键规则

1. **point-in-time**：回测与实盘都使用当时名单，禁止用当前 Top30 回溯历史，否则存在生存偏差；
2. **季度再平衡**：季度末最后一天 UTC 00:00 计算，次日执行；
3. **排名用 T-1 数据**：避免未来函数；
4. **新币上市**：至少 90 天历史才纳入；
5. **退市/停牌**：立即移除，下一交易日再平衡，等权再分配；
6. **数据缺失**：用上一有效价格；连续缺失 > 1 日触发告警；
7. **实盘不可复制时**：若实盘无法交易全部 30 个，必须按可交易子集重算历史收益并重新回测，不能直接套 30%/30。

### 5.4 方案 3 的正确用法

“实施时重算”是方案 1 的上线步骤，不是替代选项：

1. 回测阶段：固定 Top30 等权规则，跑出 30%/30 参数；
2. 上线前：用最新数据按同一规则重算初始名单；
3. 上线后：每季度再平衡，规则不变；
4. 参数冻结：30%、30、1.5 不变；
5. 若实盘无法复制 Top30，重新回测校准。

---

## 六、配置管理与状态持久化

### 6.1 配置文件清单

| 文件 | 作用 | 是否可热更 |
|---|---|---|
| `configs/vol_target.yaml` | Vol-Target 策略参数 | 否 |
| `configs/universe.yaml` | 基础组合币池规则 | 否 |
| `configs/risk.default.yaml` | 风控默认参考值，只读 | 否 |
| `configs/risk.yaml` | 风控基础配置 | 否 |
| `configs/risk.prod.yaml` | 生产覆盖，需审批 | 否 |
| `state/risk_state.json` | 运行时风控状态 | 是（程序写） |

**加载顺序**：`risk.default.yaml` → `risk.yaml` → `risk.prod.yaml`，后者覆盖前者。

**严禁**：生产环境使用 `risk.local.yaml` 覆盖；密钥、Webhook、@ 用户 ID 走环境变量。

### 6.2 `vol_target.yaml` 完整配置

```yaml
# configs/vol_target.yaml
version: 1
meta:
  name: vol_target
  owner: research
  config_version: "2026.09.18-1"
  description: "Vol-Target 30/30 策略参数"

vol_target:
  target_vol_annual: 0.30
  window_days: 30
  annualization_factor: 1.5811   # sqrt(365)
  min_weight: 0.0
  max_leverage: 1.5

  execution:
    timezone: "UTC"
    calc_time: "00:10"
    execute_time: "00:20"
    min_weight_change: 0.05      # |Δw| < 5% 不调
    min_trade_size: 0.01         # 最小调仓单位
    max_turnover_per_day: 0.50   # 单日最大换手
    use_t_minus_1: true

  cost:
    fee_one_way: 0.0005          # 单边 0.05%
    include_slippage: true
    slippage_bps: 5

  health:
    actual_vs_target_ratio_high: 1.5
    actual_vs_target_ratio_low: 0.5
    persist_days: 10
    action: require_review

  output:
    emit_w_raw: true
    emit_w_clipped: true
    emit_w_final: true
    emit_config_hash: true
    emit_universe_version: true
```

### 6.3 `risk.default.yaml` 完整配置

```yaml
# configs/risk.default.yaml
version: 1
meta:
  name: risk_default
  owner: risk
  config_version: "2026.09.18-1"
  description: "风控默认参考值，只读。生产以 risk.yaml / risk.prod.yaml 为准。"

risk:
  precedence:
    - data_quality
    - drawdown_l3
    - consecutive_loss_l2
    - drawdown_l2
    - consecutive_loss_l1
    - drawdown_l1
    - single_day_loss
    - volatility_abnormal
    - normal

  # ============ 全局组合级风控，所有策略共用 ============
  global:
    leverage:
      max_gross: 1.5
      max_net: 1.5

    drawdown:
      basis: portfolio_nav_close
      peak: all_time_high
      levels:
        - id: drawdown_l1
          threshold: 0.10
          actions:
            - {type: set_max_weight, value: 1.0}
            - {type: alert, channel: feishu}
        - id: drawdown_l2
          threshold: 0.15
          actions:
            - {type: set_max_weight, value: 0.5}
            - {type: alert, channel: feishu}
        - id: drawdown_l3
          threshold: 0.20
          actions:
            - {type: set_max_weight, value: 0.0}
            - {type: halt_new_positions, cooldown_days: 3}
            - {type: require_review}
            - {type: alert, channel: feishu}
      recovery:
        drawdown_l1: {below: 0.05, require_review: false}
        drawdown_l2: {below: 0.10, require_review: false}
        drawdown_l3: {below: 0.15, require_review: true}

    consecutive_losses:
      basis: portfolio_daily_return
      loss_definition: "return < 0"
      reset_on: "return >= 0"
      levels:
        - id: consecutive_loss_l1
          days: 3
          actions:
            - {type: set_max_weight, value: 0.5}
            - {type: alert, channel: feishu}
        - id: consecutive_loss_l2
          days: 5
          actions:
            - {type: set_max_weight, value: 0.0}
            - {type: halt_new_positions, cooldown_days: 3}
            - {type: require_review}
            - {type: alert, channel: feishu}
      recovery:
        consecutive_loss_l1: {reset_on_profit_day: true, require_review: false}
        consecutive_loss_l2: {cooldown_days: 3, require_review: true}

    single_day_loss:
      threshold: 0.05
      action: set_max_weight
      max_weight: 0.5
      cooldown_days: 1
      actions:
        - {type: alert, channel: feishu}

    volatility_abnormal:
      forecast_annual_vol_threshold: 0.80
      persist_days: 1
      actions:
        - {type: require_review}
        - {type: alert, channel: feishu}

    data_quality:
      max_missing_days: 1
      actions:
        - {type: hold_previous_weight}
        - {type: alert, channel: feishu}

  # ============ Vol-Target 专属风控维度 ============
  vol_target:
    max_leverage: 1.5
    vol_target_health:
      ratio_high: 1.5
      ratio_low: 0.5
      persist_days: 10
      actions:
        - {type: require_review}
        - {type: alert, channel: feishu}

notifications:
  feishu:
    webhook_env: FEISHU_RISK_WEBHOOK
    mention:
      risk_owner: "ou_xxx"
      pm: "ou_yyy"
    templates:
      daily: vol_target_daily_v1
      alert: vol_target_risk_alert_v1
```

### 6.4 `risk.yaml` 基础配置

```yaml
# configs/risk.yaml
# 基础配置：只放项目级覆盖，默认值继承 risk.default.yaml
version: 1
meta:
  name: risk_base
  config_version: "2026.09.18-1"

risk:
  global:
    leverage:
      max_gross: 1.5
```

### 6.5 `risk.prod.yaml` 生产覆盖

```yaml
# configs/risk.prod.yaml
# 生产覆盖：任何放松风控的变更需风控负责人 + PM 审批
version: 1
meta:
  name: risk_prod
  config_version: "2026.09.18-1"
  approved_by: ["risk_owner", "pm"]
  effective_from: "2026-09-18"

risk:
  global:
    drawdown:
      levels:
        - id: drawdown_l1
          threshold: 0.10
          actions:
            - {type: set_max_weight, value: 1.0}
            - {type: alert, channel: feishu}
        - id: drawdown_l2
          threshold: 0.15
          actions:
            - {type: set_max_weight, value: 0.5}
            - {type: alert, channel: feishu}
        - id: drawdown_l3
          threshold: 0.20
          actions:
            - {type: set_max_weight, value: 0.0}
            - {type: halt_new_positions, cooldown_days: 3}
            - {type: require_review}
            - {type: alert, channel: feishu}
    consecutive_losses:
      levels:
        - id: consecutive_loss_l1
          days: 3
          actions:
            - {type: set_max_weight, value: 0.5}
            - {type: alert, channel: feishu}
        - id: consecutive_loss_l2
          days: 5
          actions:
            - {type: set_max_weight, value: 0.0}
            - {type: halt_new_positions, cooldown_days: 3}
            - {type: require_review}
            - {type: alert, channel: feishu}
    single_day_loss:
      threshold: 0.05
      max_weight: 0.5
      cooldown_days: 1
```

### 6.6 `risk_state.json` 状态结构

```json
{
  "as_of": "2026-09-18",
  "peak_nav": 123.45,
  "current_drawdown": 0.082,
  "consecutive_loss_days": 0,
  "active_level": "normal",
  "active_levels": [],
  "cooldown_until": null,
  "max_weight_override": null,
  "halt_new_positions": false,
  "force_liquidate": false,
  "require_review": false,
  "last_config_hash": "abc123",
  "last_universe_version": "2026.09.18-1",
  "last_updated": "2026-09-18T00:10:00Z"
}
```

### 6.7 配置校验规则

启动时 fail fast，必须校验：

1. `drawdown_l1 < drawdown_l2 < drawdown_l3`；
2. `consecutive_loss_l1.days < consecutive_loss_l2.days`；
3. `set_max_weight` 值在 `[0, max_gross]`；
4. `precedence` 中的 id 必须存在；
5. `recovery.below` 必须小于对应触发阈值；
6. `webhook_env` 必须存在；
7. `config_version` 非空；
8. `universe.size == 30`；
9. `universe.rebalance.point_in_time == true`。

### 6.8 配置审计

- 每次运行记录 `config_path`、`config_hash`、`config_version`；
- 飞书日报带短 hash，如 `cfg:abc123`；
- 配置变更走 Git PR，`CODEOWNERS` 指定风控负责人；
- 生产回滚只需切回上一版 YAML；
- 热更新默认关闭；若开启，只允许“更严格”方向自动生效，放松风控必须人工审批。

---

## 七、与风控的衔接

### 7.1 优先级

**风控硬约束 > Vol-Target 信号 > 基础组合信号**

### 7.2 多风控同时触发：取严不叠加

**核心规则**：

```text
global_cap = min(所有触发的 max_weight)
w_final    = min(w_vol, global_cap)
```

- 回撤二级 + 连亏一级 + 单日暴跌同时触发 → 取最严上限；
- 不叠加、不连乘、不二次缩仓。

### 7.3 单日暴跌：上限封顶，不叠加缩仓

**动作定义**：

- `action: set_max_weight`，不是 `de_risk_factor`，不是乘系数；
- 触发后 `global_cap = min(global_cap, 0.5)`；
- 冷却 1 天；
- 与其他全局风控取最严。

**为什么**：vol-target 已因波动率上升而降仓，再乘一次系数是双重惩罚，会过杀、破坏回测一致性。

### 7.4 波动率异常：拆成两条

| 条目 | 归属 | 阈值 | 动作 |
|---|---|---|---|
| 组合级波动异常 | `risk.global.volatility_abnormal` | 年化波动 > 80% | `require_review` |
| Vol-Target 参数健康度 | `risk.vol_target.vol_target_health` | 实际/目标 > 1.5 或 < 0.5，持续 10 日 | `require_review` |

两者含义不同，不得混用。

### 7.5 恢复条件与冷却期

| 状态 | 触发阈值 | 恢复条件 | 是否人工复核 |
|---|---:|---:|---|
| 回撤一级 | 10% | 回撤 < 5% | 否 |
| 回撤二级 | 15% | 回撤 < 10% | 否 |
| 回撤三级 | 20% | 回撤 < 15% | 是 |
| 连亏一级 | 3 日 | 出现盈利日 | 否 |
| 连亏二级 | 5 日 | 冷却 3 日 + 人工复核 | 是 |
| 单日暴跌 | 5% | 冷却 1 日 | 否 |
| 组合波动异常 | 80% | 波动 < 80% | 是 |
| 参数健康度 | 偏离持续 10 日 | 偏离解除 | 是 |

**冷却期作用**：防止“暴跌次日反弹 → 立即满仓 → 再次暴跌”的反复打脸。

### 7.6 执行顺序

1. 算 Vol-Target 原始仓位 `w_vol = clip(σ_target / σ_forecast, 0, max_leverage)`；
2. 套 `risk.vol_target` 专属约束：`w_vol = clip(w_vol, min_weight, max_leverage)`；
3. 汇总所有触发的全局风控：`global_cap = min(所有 max_weight)`；
4. 最终仓位：`w_final = min(w_vol, global_cap)`；
5. 若 `halt_new_positions = true`，PM 只减不加；
6. 若 `force_liquidate = true`，`w_final = 0`；
7. 若 `require_review = true`，飞书 @ 风控负责人和 PM，人工确认后才能恢复。

**全局永远优先于 Vol-Target 专属。**

### 7.7 人工复核触发

- 回撤三级 `drawdown_l3`；
- 连亏二级 `consecutive_loss_l2`；
- 组合波动异常 > 80%；
- 参数健康度偏离持续 10 日；
- 杠杆触顶且持续 3 日；
- 数据源异常导致信号不可信。

---

## 八、与 PM 账户 / AI-tuner 的衔接

### 8.1 层级关系

```text
策略层 → 基础组合层 → Vol-Target 资金分配层 → 风控层 → PM 执行层
```

### 8.2 与 AI-tuner `account_ratio_caps` 的关系

**三者取最严**：

```text
最终账户名义仓位 = min(
  vol_target_w,
  risk_global_cap,
  account_ratio_caps
) × 各策略目标权重
```

- `vol_target_w`：Vol-Target 输出仓位系数；
- `risk_global_cap`：风控硬上限；
- `account_ratio_caps`：AI-tuner 账户级上限。

**不得出现 vol-target 说 1.5 倍、AI-tuner 说 1.0 倍、PM 不知道听谁的情况。**

### 8.3 PM 账户接收字段

PM 接收：

- `w_final`
- `max_weight_override`
- `halt_new_positions`
- `force_liquidate`
- `cooldown_until`
- `active_risk_level`
- `config_version` / `config_hash`
- `universe_version`

PM 执行规则：

- `w_final > 1`：需杠杆、借币、保证金管理，强平线 + 最大杠杆 1.5 兜底；
- `halt_new_positions`：拒绝加仓，只执行减仓；
- `force_liquidate`：按流动性分批清仓；
- 每日与风控对账，偏差超阈值人工处理。

### 8.4 执行规则

- 每日收盘后计算，次日固定时间执行；
- `|w_final - 昨日 w_final| < 5%` 不调仓；
- 单日换手不超过 `max_turnover_per_day`；
- 低于 `min_trade_size` 不调；
- 触发风控时优先执行风控指令。

---

## 九、Vol-Target 输出接口定义

每日输出字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `date` | string | 计算日期 |
| `base_portfolio` | string | 基础组合名称 |
| `universe_version` | string | 币池版本 |
| `sigma_forecast_annual` | float | 30 日年化波动 |
| `sigma_target_annual` | float | 目标年化波动 |
| `w_raw` | float | 原始仓位 |
| `w_clipped` | float | 截断后仓位 |
| `w_vol` | float | Vol-Target 输出 |
| `global_cap` | float | 全局风控上限 |
| `w_final` | float | 最终仓位 |
| `delta_w` | float | 较前日变化 |
| `max_weight_override` | float\|null | 风控覆盖 |
| `halt_new_positions` | bool | 是否禁止加仓 |
| `force_liquidate` | bool | 是否强制清仓 |
| `cooldown_until` | string\|null | 冷却截止 |
| `active_risk_level` | string | 当前风控状态 |
| `estimated_turnover_cost` | float | 预估换手成本 |
| `next_day_instruction` | object | 次日指令 |
| `config_version` | string | 配置版本 |
| `config_hash` | string | 配置 hash |

---

## 十、飞书推送设计

### 10.1 推送类型

| 类型 | 频率 | 接收方 |
|---|---|---|
| 日报 | 每日一次 | PM、风控、研究群 |
| 周报 | 每周一 | 投研群、管理层 |
| 预警 | 实时 | 风控负责人、PM |

### 10.2 日报模板

**标题**：Vol-Target 日报｜{日期}

**字段**：

- 基础组合 / 币池版本
- 30 日年化波动 / 目标波动
- 原始仓位 `w_raw` / 截断后 `w` / 最终 `w_final`
- 较前日变化 `Δw`
- 当前回撤 / 连亏天数
- 风控状态（正常/一级/二级/三级）
- `max_weight_override` / `halt_new_positions` / `cooldown_until`
- 次日指令（总仓位 + 各币目标权重）
- `config_version` / `config_hash`
- 备注

**示例**：

> 【Vol-Target 日报】2026-09-18  
> 基础组合：Top30 等权（uni:2026.09.18-1）  
> 30 日年化波动：32.4%  
> 目标波动：30.0%  
> 原始仓位：0.93 / 截断后：0.93 / 最终：0.93  
> 较前日：-0.07  
> 当前回撤：-8.2% / 连亏：0 日  
> 风控状态：正常  
> 次日指令：总仓位 93%，各币 3.10%  
> 配置：cfg:abc123  
> 备注：波动率略高于目标，轻微降仓。

### 10.3 周报模板

字段：本周收益、本周波动、本周最大回撤、平均仓位、调仓次数、换手成本、滚动夏普、滚动 Calmar、风控触发次数、参数健康度、下周建议。

### 10.4 预警模板

字段：预警级别、触发条件、当前值、建议动作、需确认人、时间戳、`config_hash`。

**示例**：

> 【预警｜二级】Vol-Target  
> 触发条件：组合回撤 ≥ 15%  
> 当前回撤：-15.3%  
> 建议动作：仓位上限降至 0.5，暂停加仓  
> 需确认人：@风控负责人 @PM  
> 时间：2026-09-18 08:00 UTC  
> 配置：cfg:abc123

### 10.5 推送通道

- 飞书群自定义机器人 Webhook；
- 消息类型建议为交互式卡片；
- 预警消息 @ 相关负责人；
- 所有推送留存日志，便于复盘与审计。

---

## 十一、上线与运维流程

### 11.1 灰度上线（分层推进）

1. **阶段 0**：只计算不执行，飞书推送，核对信号合理性（2 周）；
2. **阶段 1**：btc_eth 单策略接入 Vol-Target，paper / 小仓验证（4~8 周）；
   - 接口澄清：整个账户都用 `w`，但只观察 btc_eth 表现；其他策略名义规模暂不变；
3. **阶段 2**：验证通过后扩散到全账户（含 new_coin / grid / hrs）；
4. **阶段 3**：作为全局顶层资金分配器常驻，与 AI-tuner `account_ratio_caps` 共同构成仓位上限体系；
5. **阶段 4**：季度再校准阈值。

> 理由：Vol-Target 影响所有策略名义规模，一上来全账户风险更高。

### 11.2 每日流程

1. 数据更新；
2. 计算基础组合（前 30 等权）日收益；
3. 计算 30 日滚动波动；
4. 生成 Vol-Target 仓位；
5. 风控硬约束覆盖；
6. 飞书日报推送；
7. PM 账户执行；
8. 收盘后对账。

### 11.3 每周流程

- 汇总周报；
- 检查实际波动与目标波动偏差；
- 检查换手成本；
- 检查风控触发记录；
- 评估参数健康度。

### 11.4 每月/每季度再校准

- 每季度重新评估目标波动率、窗口、最大杠杆；
- 若连续 20 日实际波动 > 目标 1.5 倍或 < 目标 0.5 倍，检查参数；
- 若市场波动中枢显著上移，考虑降低目标波动率或提高窗口；
- 参数变更需走变更记录，避免过度拟合。

### 11.5 运维失败处理

| 故障 | 处理 |
|---|---|
| 数据更新失败 | 保持上一仓位，飞书告警 |
| 计算失败 | 不执行新指令，人工介入 |
| 飞书推送失败 | 重试 + 记录日志 |
| PM 执行失败 | 对账告警，偏差超阈值人工处理 |
| 配置加载失败 | 拒绝启动，告警 |
| 状态文件损坏 | 从上一快照恢复，人工确认 |

---

## 十二、测试清单

1. **未来函数测试**：T 日数据不能影响 T 日执行；
2. **边界测试**：回撤 9.9% / 10.0% / 10.1%；
3. **状态机测试**：多条件同时触发的优先级；
4. **恢复测试**：三级 → 二级 → 一级；
5. **取严测试**：多风控同时触发取 min，不叠加；
6. **配置变更测试**：改 YAML 不改代码，结果符合预期；
7. **配置校验测试**：阈值单调、权重范围、优先级存在；
8. **飞书渲染测试**；
9. **point-in-time 测试**：回测名单与当时一致；
10. **换手测试**：`|Δw|`、`max_turnover_per_day`、`min_trade_size` 生效。

---

## 十三、风险与注意事项

1. **历史波动不等于未来波动**：低波动后可能突然闪崩，Vol-Target 反应滞后；
2. **肥尾风险**：必须配合硬止损、回撤熔断、人工复核；
3. **杠杆成本**：`w > 1` 时涉及借币、资金费率、强平风险；
4. **流动性风险**：前 30 等权仍需检查单币流动性；
5. **相关性上升**：极端行情下分散失效，实际波动可能高于预测；
6. **参数过拟合**：30%/30 是回测甜点区，需持续监控；
7. **基础组合口径漂移**：前 30 名单随成交额变化，必须按季度再平衡口径刷新；
8. **回测未计杠杆成本**：若实盘含资金费率，年化 21.2% 可能被高估；
9. **配置漂移**：生产环境禁止 `risk.local.yaml` 覆盖。

---

## 十四、结论与建议

**正式推荐方案**：

> Vol-Target 30%/30 + 最大杠杆 1.5 + 两层风控（`risk.yaml` 配置化）+ point-in-time Top30 等权币池 + 分层灰度 + 飞书日报/预警。

预期表现：

- 年化：约 +21%；
- 波动：约 32%；
- 最大回撤：约 29%；
- 夏普：约 0.66；
- Calmar：约 0.73。

若 PM 账户不支持杠杆，设 `max_leverage = 1.0`，方案退化为 0~100% 仓位调节，收益下降但回撤控制仍有效。

若风险偏好更低，用保守版 **20%/30**：年化约 +15%、最大回撤约 20%、夏普约 0.70。

**最终建议**：以 30%/30 主方案上线，先阶段 0 只计算不执行 2 周，再 btc_eth 单策略灰度 4~8 周，确认信号、执行、风控、飞书全链路稳定后，逐步放大到全账户。

---

## 附录 A：文件清单

| 文件 | 作用 |
|---|---|
| `configs/vol_target.yaml` | Vol-Target 策略参数 |
| `configs/universe.yaml` | 基础组合币池规则 |
| `configs/risk.default.yaml` | 风控默认参考值 |
| `configs/risk.yaml` | 风控基础配置 |
| `configs/risk.prod.yaml` | 风控生产覆盖 |
| `state/risk_state.json` | 运行时风控状态 |
| `backtest/equity_timing/backtest_buyhold.py` | 回测脚本 |
| `backtest/equity_timing/report_buyhold.md` | 回测报告 |
| `backtest/momentum/data/klines/*_1d.csv` | 日线数据 |
| `data/universe_snapshots/` | 币池历史快照 |

## 附录 B：变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| v1.0 | 2026-09-18 | 初稿 | — |
| v1.1 | 2026-09-18 | 融合版 | — |
| v1.2 | 2026-09-18 | 实施规格版：修正单日暴跌、补全 YAML、状态持久化、恢复冷却、接口字段、测试清单 | — |

## 附录 C：上线检查清单

- [ ] `vol_target.yaml` 已冻结并评审；
- [ ] `universe.yaml` point-in-time 校验通过；
- [ ] `risk.default.yaml` / `risk.yaml` / `risk.prod.yaml` 加载顺序验证；
- [ ] schema 校验通过；
- [ ] `risk_state.json` 初始化；
- [ ] 未来函数测试通过；
- [ ] 边界测试通过；
- [ ] 状态机测试通过；
- [ ] 飞书卡片渲染测试通过；
- [ ] 阶段 0 只计算不执行运行 2 周；
- [ ] 阶段 1 btc_eth 灰度 4~8 周；
- [ ] 阶段 2 全账户扩散评审通过。