# 网格策略回测AI调优系统（方案D）PRD

## 文档信息

| 项目 | 内容 |
|:---|:---|
| 文档版本 | V1.0 |
| 创建日期 | 2026-08-19 |
| 作者 | 需求文档专家 |
| 状态 | 待评审 |
| 关联文档 | [网格策略AI调优系统 -- 落地实施方案](./网格策略AI调优系统 —— 落地实施方案.md) |

---

## 1. 需求背景与目标

### 1.1 背景

当前网格策略AI调优系统（方案C / 混合模式）中，`GridAdapter._simulate()` 方法使用**简单估算模型**替代真实回测：

- 根据当前价格和ATR计算网格间距
- 用"总价格摆动 / 网格间距 x 填充效率因子(0.6)"估算填充次数
- 用"填充次数 x 每格利润"估算周收益

该估算模型存在以下问题：

| 问题 | 影响 |
|:---|:---|
| 填充效率因子(0.6)是固定经验值，未经验证 | 预估收益与实际偏差可能很大 |
| 不计算手续费和滑点 | 短期窄幅网格的实际利润可能为负，但估算为正 |
| 不区分上涨/下跌/横盘市况 | 单边趋势中网格被击穿的风险无法体现 |
| 不生成逐笔交易记录，无法计算夏普比率 | AI调优缺少关键风险调整收益指标 |
| 无法验证LLM建议的参数是否真的优于当前参数 | 采纳劣化参数的风险存在 |

### 1.2 目标

将方案C的简单估算替换为**真实K线逐笔回测引擎**，实现：

1. **逐笔模拟**：基于ETHUSDT 1h K线，按网格逻辑逐小时模拟挂单、成交、止盈止损
2. **精确成本**：计入maker手续费(0.04%)、taker手续费(0.06%)、滑点(0.01%)
3. **全面评估**：计算收益率、最大回撤、夏普比率、成交频率、手续费占比、资金利用率
4. **市况细分**：分别统计上涨/下跌/横盘三种市况下的表现
5. **参数验证**：LLM建议的新参数必须经过回测验证，综合评分优于旧参数才采纳
6. **无交易也调优**：即使本周网格策略无真实成交，也基于回测数据执行AI调优

### 1.3 与方案C的差异对比

| 维度 | 方案C（当前） | 方案D（目标） |
|:---|:---|:---|
| 模拟方式 | 简单公式估算 | 逐笔K线回测 |
| 手续费 | 不计 | 计入(0.04%+0.06%) |
| 滑点 | 不计 | 计入(0.01%) |
| 评估指标 | 预估填充数、预估利润、置信度 | 收益率、最大回撤、夏普、成交频率、手续费占比、资金利用率 |
| 市况分析 | 无 | 上涨/下跌/横盘分别统计 |
| 参数验证 | 无（LLM说了算） | 回测验证，综合评分制 |
| 无交易时调优 | 跳过 | 执行（基于回测数据） |

---

## 2. 功能需求

### 2.0 总体流程

```
第N周 周日 23:55 触发
  |
  Step 1: 获取上周 ETHUSDT 1h K线数据（168根），从 PostgreSQL 直查
  |
  Step 2: 用【当前网格参数】回测上周行情 → 生成绩效报告
  |
  Step 3: 效果追踪器回填（写入 memory 表 post_* 字段）
  |
  Step 4: 上下文增强 + 学习信号注入
  |
  Step 5: LLM 生成参数调整建议
  |
  Step 6: 用【新参数】回测上周行情（验证）→ 综合评分优于旧参数才采纳
  |
  推送飞书通知 / 自动应用配置
```

### 2.1 Step 1: 获取K线数据

#### 2.1.1 输入

| 参数 | 值 | 说明 |
|:---|:---|:---|
| 交易对 | ETHUSDT | 固定 |
| K线周期 | 1h | 固定 |
| 数据量 | 168根 | 7天 x 24小时 |

#### 2.1.2 处理逻辑

**优先级1：PostgreSQL直查**

```sql
SELECT open_time, open_price, high_price, low_price, close_price, volume
FROM kline_ethusdt_1h
WHERE open_time >= '{week_start}'
  AND open_time < '{week_end}'
ORDER BY open_time ASC;
```

- 表名：`kline_ethusdt_1h`（由K线服务自动创建，命名规则为 `kline_{symbol_lower}_{interval}`）
- 数据来源：K线服务 `services/kline_service/core/collector.py` 持续采集并写入
- 时间范围：上周一 00:00:00 至 本周一 00:00:00（与 `weekly_job.py` 的 `week_start`/`week_end` 计算方式一致）

**优先级2（Fallback）：K线服务API**

当PostgreSQL查询失败或返回数据不足（少于24根K线）时，降级为通过K线服务HTTP API获取：

```python
GET http://kline-service:8000/api/v1/klines/latest
  ?symbol=ETHUSDT
  &interval=1h
  &limit=200
```

#### 2.1.3 输出

```python
List[Dict]:
[
    {
        "open_time": datetime,    # K线开盘时间
        "open": float,            # 开盘价
        "high": float,            # 最高价
        "low": float,             # 最低价
        "close": float,           # 收盘价
        "volume": float,          # 成交量
    },
    ...
]
```

#### 2.1.4 异常处理

| 异常场景 | 处理方式 |
|:---|:---|
| PostgreSQL查询异常（超时/连接失败） | 降级到K线服务API |
| K线服务API也失败 | 记录错误日志，发送飞书告警，**跳过本次调优** |
| 返回K线数量 < 24根 | 视为数据不足，记录警告日志，**跳过本次调优** |
| 返回K线数量在 24-167 之间 | 使用可用数据继续回测，但在报告中标注"数据不完整" |
| 返回K线数量 >= 168根 | 正常流程 |

#### 2.1.5 验收标准

- [ ] 优先从PostgreSQL直查，读取 `kline_ethusdt_1h` 表
- [ ] PostgreSQL不可用时，降级为K线服务API
- [ ] 两路都失败时，发送飞书告警，不崩溃
- [ ] K线数量 < 24时，跳过本次调优，记录原因
- [ ] 使用 `ai_tuner` 容器内已有的 `db_manager` 连接PostgreSQL，不新建连接

---

### 2.2 Step 2: 回测当前参数

#### 2.2.1 回测引擎设计

**文件**：`ai_tuner/backtest/grid_backtest.py`

**核心逻辑**：逐K线遍历，模拟网格策略的挂单、成交、持仓管理。

```
输入：
  - klines: List[Kline]（168根1h K线）
  - params: GridParams（网格参数）
  - fee_config: FeeConfig（费率配置）

输出：
  - BacktestResult（回测结果）

算法：
  1. 初始化：根据当前价格和网格参数，计算网格层级和各层挂单价
  2. 遍历每根K线：
     a. 检查价格是否触及挂单价（用 high/low 判断）
     b. 触及则成交，记录成交方向和价格
     c. 扣除手续费（maker 0.04% 或 taker 0.06%）
     d. 计算滑点影响（0.01%）
     e. 更新持仓和资金
     f. 检查止盈止损条件
  3. 遍历结束后，计算统计指标
```

#### 2.2.2 网格回测参数

从 `strategies/grid/config.yaml` 读取，与生产环境使用的参数完全一致：

| 参数路径 | 用途 | 示例值 |
|:---|:---|:---|
| `grid.base_grid_count` | 基准网格数 | 6 |
| `grid.grid_spacing_atr_multiplier` | 网格间距ATR倍数 | 2.5 |
| `grid.stop_loss_buffer` | 止损缓冲 | 2 |
| `trading.leverage` | 杠杆倍数 | 10 |
| `trading.margin` | 总保证金 | 500 |
| `trading.single_position_margin` | 单格保证金 | 100 |
| `risk.stop_loss_percent` | 止损百分比 | 0.10 |
| `risk.hard_stop_loss` | 硬止损 | -0.15 |

#### 2.2.3 费率模型

| 费率项 | 值 | 适用场景 |
|:---|:---|:---|
| Maker手续费 | 0.04% | 挂单成交（限价单被吃） |
| Taker手续费 | 0.06% | 止盈止损触发（市价单） |
| 滑点 | 0.01% | 每笔成交额外扣除 |

**注意**：回测中假设网格挂单均为限价单（maker），止盈止损为市价单（taker）。

#### 2.2.4 回测结果数据模型

**文件**：`ai_tuner/adapters/base_adapter.py` 中的 `SimulationMetrics` 扩展

```python
class BacktestResult(BaseModel):
    """网格回测结果（方案D新增）"""

    # === 基础信息 ===
    scenario_name: str = Field(default="", description="场景名称")
    symbol: str = Field(default="ETHUSDT", description="交易对")
    kline_count: int = Field(default=0, description="回测使用的K线数量")

    # === 收益类 ===
    total_return_pct: float = Field(default=0.0, description="总收益率(%)")
    annualized_return_pct: float = Field(default=0.0, description="年化收益率(%)")
    total_pnl: float = Field(default=0.0, description="总盈亏(USDT)")

    # === 风险类 ===
    max_drawdown_pct: float = Field(default=0.0, description="最大回撤(%)")
    sharpe_ratio: float = Field(default=0.0, description="夏普比率(周度)")

    # === 网格专属 ===
    fill_count: int = Field(default=0, description="成交次数")
    avg_profit_per_fill: float = Field(default=0.0, description="单次成交平均利润(USDT)")
    fee_ratio: float = Field(default=0.0, description="手续费占利润比(%)")
    capital_utilization: float = Field(default=0.0, description="资金利用率(%)")

    # === 市况细分 ===
    uptrend_return_pct: float = Field(default=0.0, description="上涨市况收益率(%)")
    downtrend_return_pct: float = Field(default=0.0, description="下跌市况收益率(%)")
    sideways_return_pct: float = Field(default=0.0, description="横盘市况收益率(%)")

    # === 综合评分 ===
    composite_score: float = Field(default=0.0, description="综合评分(0-100)")
```

#### 2.2.5 综合评分公式

```
综合评分 = 收益率得分 × 0.40 + 回撤得分 × 0.30 + 夏普得分 × 0.30

其中：
- 收益率得分 = min(100, max(0, total_return_pct × 10))
  （收益率 10% = 满分 100，负收益 = 0 分）
- 回撤得分 = min(100, max(0, (1 - max_drawdown_pct / 0.20) × 100))
  （回撤 0% = 满分 100，回撤 20% = 0 分）
- 夏普得分 = min(100, max(0, sharpe_ratio × 50))
  （夏普 2.0 = 满分 100，负夏普 = 0 分）
```

#### 2.2.6 市况分类标准

回测引擎在遍历K线时，按以下规则将每根K线归入对应市况，分别统计收益：

| 市况 | 判断条件 | 判定周期 |
|:---|:---|:---|
| 上涨 | 最近24根K线收盘价累计涨幅 > 2% | 动态滑动窗口 |
| 下跌 | 最近24根K线收盘价累计跌幅 > 2% | 动态滑动窗口 |
| 横盘 | 最近24根K线收盘价累计变动在 [-2%, 2%] 之间 | 动态滑动窗口 |

#### 2.2.7 验收标准

- [ ] 回测引擎能正确遍历168根K线，每根K线模拟成交判断
- [ ] 成交判断正确使用 high/low 判断是否触及挂单价
- [ ] 手续费计算正确：挂单成交扣 0.04%，止盈止损扣 0.06%
- [ ] 滑点 0.01% 正确应用于每笔成交
- [ ] 止盈止损逻辑正确触发
- [ ] 综合评分在 0-100 范围内
- [ ] 市况细分统计正确（上涨/下跌/横盘分别累计）
- [ ] 回测结果包含完整的 `BacktestResult` 所有字段
- [ ] 回测单次执行时间 < 5秒（168根K线，0.5 CPU限制下）

---

### 2.3 Step 3: 效果追踪器回填

#### 2.3.1 功能说明

复用现有的 `EffectTracker.track_and_fill()` 方法，将本次回测绩效数据写入 `memory` 表的 `post_*` 字段。

**与现有流程的差异**：

- 方案C中，`EffectTracker` 回填的数据来自真实成交（`performance` 字段）
- 方案D中，网格策略**无真实成交时也执行调优**，此时 `post_*` 字段回填的是**回测数据**而非真实成交数据

**回填字段映射**：

| memory 表字段 | 数据来源 | 说明 |
|:---|:---|:---|
| `post_win_rate` | `BacktestResult.fill_count` | 用成交次数近似 |
| `post_pnl` | `BacktestResult.total_pnl` | 回测总盈亏 |
| `post_max_drawdown` | `BacktestResult.max_drawdown_pct` | 回测最大回撤 |
| `post_sharpe` | `BacktestResult.sharpe_ratio` | 回测夏普比率 |

#### 2.3.2 验收标准

- [ ] 复用现有 `EffectTracker`，不新建回填逻辑
- [ ] 无真实成交时，`post_*` 字段正确写入回测数据
- [ ] 有真实成交时，`post_*` 字段保持原有逻辑（写入真实数据）
- [ ] 回填失败不阻断后续流程

---

### 2.4 Step 4: 上下文增强与学习信号

#### 2.4.1 功能说明

复用现有的 `ContextEnhancer` 和 `LearningSignalGenerator`，将回测绩效数据注入到上下文中。

**新增内容**：

- 在 `feedback_context` 中增加回测绩效摘要
- 在 `learning_instructions` 中增加基于回测的学习信号

#### 2.4.2 上下文增强内容

```
## 本周回测绩效（基于当前参数）

| 指标 | 当前参数 |
|:---|:---|
| 总收益率 | {total_return_pct}% |
| 最大回撤 | {max_drawdown_pct}% |
| 夏普比率 | {sharpe_ratio} |
| 成交次数 | {fill_count} |
| 手续费占比 | {fee_ratio}% |
| 资金利用率 | {capital_utilization}% |
| 综合评分 | {composite_score}/100 |

## 市况细分

| 市况 | 收益率 |
|:---|:---|
| 上涨 | {uptrend_return_pct}% |
| 下跌 | {downtrend_return_pct}% |
| 横盘 | {sideways_return_pct}% |
```

#### 2.4.3 验收标准

- [ ] 回测绩效数据正确注入到 `feedback_context` 中
- [ ] 学习信号包含回测数据的对比分析
- [ ] 不影响 `btc_eth`、`hrs`、`new_coin` 等其他策略的上下文构建

---

### 2.5 Step 5: LLM生成参数建议

#### 2.5.1 功能说明

复用现有的 `LLMClient.call_llm()` 流程，使用更新后的 Prompt 模板。

**与方案C的差异**：

- 方案C：LLM看到的是"预估填充数"和"预估利润"（简单估算）
- 方案D：LLM看到的是真实回测的完整绩效数据（收益率、回撤、夏普、市况细分）

#### 2.5.2 Prompt 模板更新

**文件**：`ai_tuner/prompts/grid_system.txt`

**更新内容**：

1. 移除"模拟推演解读"章节（方案C的3个场景对比）
2. 新增"回测绩效解读"章节，说明各指标含义和调优方向
3. 新增"市况细分调优策略"章节
4. 更新"调优策略建议"章节，基于回测指标而非估算

**关键调优策略**：

| 回测发现 | 建议方向 |
|:---|:---|
| 手续费占比 > 30% | 加大网格间距（`grid_spacing_atr_multiplier` ↑），减少成交频率 |
| 资金利用率 < 30% | 增加网格数（`base_grid_count` ↑），或减小间距 |
| 横盘收益高、趋势收益负 | 收紧止损（`stop_loss_percent` ↓），趋势中减少网格数 |
| 最大回撤 > 15% | 收紧止损、降低杠杆 |
| 夏普 < 0.5 | 综合评估是否需要调整网格结构 |
| 上涨市况收益显著低于下跌 | 检查网格区间是否偏下 |

#### 2.5.3 验收标准

- [ ] `grid_system.txt` 更新后，LLM能正确解读回测数据
- [ ] `grid_user.txt` 模板正确渲染回测绩效数据
- [ ] LLM输出的JSON格式与现有 `response_parser` 兼容
- [ ] 参数建议路径在 `ai_tuner/config.yaml` 的 `param_whitelist` 范围内

---

### 2.6 Step 6: 参数验证（回测验证）

#### 2.6.1 功能说明

**这是方案D的核心新增步骤。** LLM建议的新参数不直接采纳，必须经过回测验证。

```
LLM建议新参数
  → 用新参数回测同一段K线数据
  → 计算新参数的综合评分
  → 与旧参数的综合评分比较
  → 新评分 > 旧评分：采纳
  → 新评分 ≤ 旧评分：拒绝，记录原因
```

#### 2.6.2 验证逻辑

```python
async def validate_params(
    old_params: GridParams,
    new_params: GridParams,
    klines: List[Kline],
    backtest_engine: GridBacktestEngine,
) -> ValidationResult:
    """
    验证新参数是否优于旧参数

    Args:
        old_params: 当前参数
        new_params: LLM建议的新参数
        klines: 上周K线数据（与Step 2使用同一批数据）
        backtest_engine: 回测引擎实例

    Returns:
        ValidationResult: 验证结果
    """
    # 回测旧参数（复用Step 2的结果，不重复回测）
    old_result = backtest_engine.run(klines, old_params)

    # 回测新参数
    new_result = backtest_engine.run(klines, new_params)

    # 比较综合评分
    if new_result.composite_score > old_result.composite_score:
        return ValidationResult(
            passed=True,
            reason=f"新参数综合评分({new_result.composite_score}) > 旧参数({old_result.composite_score})",
            old_result=old_result,
            new_result=new_result,
        )
    else:
        return ValidationResult(
            passed=False,
            reason=f"新参数综合评分({new_result.composite_score}) <= 旧参数({old_result.composite_score})，拒绝采纳",
            old_result=old_result,
            new_result=new_result,
        )
```

#### 2.6.3 验证失败处理

| 场景 | 处理方式 |
|:---|:---|
| 新评分 > 旧评分 | 采纳，正常推送飞书通知 |
| 新评分 ≤ 旧评分 | 拒绝，记录到 memory 表（`summary` 字段）并发送飞书通知说明原因 |
| 新参数回测异常（如除零、参数越界） | 拒绝，记录错误，发送飞书告警 |
| LLM建议"维持不变"（`adjustments` 为空） | 跳过验证，直接记录 |

#### 2.6.4 验收标准

- [ ] 新参数必须经过回测验证才可采纳
- [ ] 综合评分严格使用公式：收益率×0.40 + 回撤×0.30 + 夏普×0.30
- [ ] 新评分 > 旧评分时采纳，否则拒绝
- [ ] 验证失败的详细信息记录到 memory 表和日志
- [ ] 验证过程在 5 秒内完成（单次回测 < 5秒，共两次）
- [ ] 验证失败不阻断飞书通知推送（发送"建议被拒绝"的通知）

---

### 2.7 无交易时调优

#### 2.7.1 功能说明

**当前行为（方案C）**：`weekly_job.py` 第 192-194 行，当 `total_trades == 0` 时跳过调优。

**目标行为（方案D）**：网格策略即使无真实成交，也执行完整的AI调优流程。

#### 2.7.2 修改点

**文件**：`ai_tuner/scheduler/weekly_job.py`

**修改位置**：`_tune_single_strategy()` 方法第 192-194 行

```python
# 修改前（方案C）：
if report.performance.total_trades == 0:
    logger.info("策略本周无交易，跳过调优", strategy_id=strategy_id)
    return "skip"

# 修改后（方案D）：
if report.performance.total_trades == 0:
    if strategy_id == "grid":
        # 网格策略：无交易也继续调优（基于回测数据）
        logger.info("网格策略本周无真实成交，使用回测数据继续调优",
                    strategy_id=strategy_id)
    else:
        # 其他策略：保持原有逻辑
        logger.info("策略本周无交易，跳过调优", strategy_id=strategy_id)
        return "skip"
```

#### 2.7.3 验收标准

- [ ] `strategy_id == "grid"` 且无真实成交时，流程继续执行（不跳过）
- [ ] `strategy_id != "grid"` 时，保持原有逻辑（无交易则跳过）
- [ ] 无交易时，`report.performance` 各字段为默认值（0），不影响后续流程
- [ ] 无交易时，回测数据正常生成并传递给LLM

---

## 3. 非功能需求

### 3.1 性能要求

| 指标 | 要求 | 测量方法 |
|:---|:---|:---|
| 单次回测耗时 | < 5秒（168根K线） | 在 ai-tuner 容器内计时执行 |
| Step 6 验证耗时 | < 10秒（两次回测） | 计时 |
| 完整调优流程耗时 | < 30秒 | 从 Step 1 到 Step 6 总耗时 |
| 内存占用增量 | < 128MB | 回测前后比较 RSS |

**约束**：ai-tuner 容器资源限制为 **0.5 CPU / 512MB**，不修改。

**如果超时**：单次回测超过 10 秒视为超时，终止回测，记录错误，跳过本次调优。

### 3.2 可靠性要求

| 指标 | 要求 |
|:---|:---|
| PostgreSQL连接超时 | 5 秒 |
| K线服务API超时 | 10 秒 |
| 回测异常不崩溃 | try/except 包裹，异常时记录日志并跳过 |
| 数据不足时降级 | K线数 < 24 时跳过，不崩溃 |
| 回测结果确定性 | 相同输入参数必须产生相同输出（无随机性） |

### 3.3 可维护性

| 要求 | 说明 |
|:---|:---|
| 回测引擎独立模块 | `ai_tuner/backtest/` 目录，可单独测试 |
| 评估指标独立模块 | `ai_tuner/backtest/metrics.py`，可复用 |
| 费率可配置 | 不硬编码，从 `ai_tuner/config.yaml` 读取 |
| 评分权重可配置 | 从 `ai_tuner/config.yaml` 读取 |
| 日志完善 | 每步记录耗时、K线数、关键指标值 |

### 3.4 兼容性

| 要求 | 说明 |
|:---|:---|
| 不影响其他策略 | btc_eth、hrs、new_coin 的调优流程不变 |
| 不修改 base_adapter 接口 | 只扩展 `SimulationMetrics`，不改变现有方法签名 |
| 不修改 LLM 调用接口 | 复用 `LLMClient.call_llm()` |
| 不修改飞书推送接口 | 复用 `Messenger.send_tuning_card()` |

---

## 4. 文件变更清单

| 文件 | 变更类型 | 说明 |
|:---|:---|:---|
| `ai_tuner/backtest/__init__.py` | 新建 | 模块初始化 |
| `ai_tuner/backtest/grid_backtest.py` | 新建 | 网格回测引擎，约150行 |
| `ai_tuner/backtest/metrics.py` | 新建 | 网格专属评估指标，约80行 |
| `ai_tuner/adapters/base_adapter.py` | 修改 | 扩展 `SimulationMetrics` 为 `BacktestResult` |
| `ai_tuner/adapters/grid_adapter.py` | 重写 `_simulate()` | 替换为调用回测引擎 |
| `ai_tuner/scheduler/weekly_job.py` | 修改 | 网格策略无交易时也调优 |
| `ai_tuner/config.yaml` | 修改 | 新增回测配置段 |
| `ai_tuner/prompts/grid_system.txt` | 修改 | 更新提示词适配新指标体系 |
| `ai_tuner/prompts/grid_user.txt` | 修改 | 更新模板适配回测数据 |

---

## 5. 配置变更

### 5.1 ai_tuner/config.yaml 新增配置段

```yaml
# ============================================================
# 网格回测配置（方案D）
# ============================================================
backtest:
  # 费率配置
  fee:
    maker: 0.0004               # Maker手续费 0.04%
    taker: 0.0006               # Taker手续费 0.06%
    slippage: 0.0001            # 滑点 0.01%

  # 综合评分权重
  scoring:
    return_weight: 0.40         # 收益率权重
    drawdown_weight: 0.30       # 最大回撤权重
    sharpe_weight: 0.30         # 夏普比率权重

  # 性能限制
  max_execution_time: 10        # 单次回测最大执行时间(秒)
  min_kline_count: 24           # 最少K线数量，低于此值跳过回测

  # 市况分类阈值
  market_regime:
    trend_threshold: 0.02       # 趋势判定阈值：24h累计涨跌幅 > 2%
    lookback_hours: 24          # 市况判定回溯小时数

  # 收益率得分计算参数
  return_score_scale: 10        # 收益率 × 10 = 得分（10%收益率 = 100分）
  drawdown_cap: 0.20            # 回撤上限：20%回撤 = 0分
  sharpe_score_scale: 50        # 夏普 × 50 = 得分（2.0夏普 = 100分）
```

### 5.2 ai_tuner/config.yaml 参数路径修复

当前 `ai_tuner/config.yaml` 中网格策略的 `param_whitelist` 引用了 `atr_multipliers.oscillation` 和 `atr_multipliers.weak_trend`，但策略配置 `strategies/grid/config.yaml` 中的实际路径为 `market.atr_multipliers.oscillation`。

**需要修复**：将 `ai_tuner/config.yaml` 中的白名单路径与策略配置文件的实际路径对齐。

---

## 6. 验收标准（汇总）

### 6.1 功能验收

- [ ] **AC-01**：Step 1 能从 PostgreSQL `kline_ethusdt_1h` 表获取168根1h K线
- [ ] **AC-02**：Step 1 PostgreSQL不可用时，降级为K线服务API
- [ ] **AC-03**：Step 2 回测引擎正确模拟网格成交，包含手续费和滑点
- [ ] **AC-04**：Step 2 回测结果包含所有要求的字段（收益率、回撤、夏普、成交频率、手续费占比、资金利用率、市况细分）
- [ ] **AC-05**：Step 2 综合评分在 0-100 范围内，计算公式正确
- [ ] **AC-06**：Step 3 效果追踪器回填正确，无真实交易时使用回测数据
- [ ] **AC-07**：Step 4 上下文增强正确注入回测绩效数据
- [ ] **AC-08**：Step 5 LLM能正确解读回测数据并生成参数建议
- [ ] **AC-09**：Step 6 新参数回测验证，综合评分优于旧参数才采纳
- [ ] **AC-10**：Step 6 验证失败时，记录原因并推送飞书通知
- [ ] **AC-11**：网格策略无真实成交时，不跳过调优
- [ ] **AC-12**：其他策略（btc_eth、hrs、new_coin）的调优流程不受影响
- [ ] **AC-13**：飞书通知卡片正确展示回测绩效数据

### 6.2 性能验收

- [ ] **AC-14**：单次回测耗时 < 5秒（0.5 CPU / 512MB 限制下）
- [ ] **AC-15**：完整调优流程耗时 < 30秒
- [ ] **AC-16**：回测超时（>10秒）时正确终止并记录错误

### 6.3 异常处理验收

- [ ] **AC-17**：K线数据不足（< 24根）时跳过调优，不崩溃
- [ ] **AC-18**：PostgreSQL连接失败时降级到K线服务API
- [ ] **AC-19**：K线服务API也失败时发送飞书告警
- [ ] **AC-20**：回测过程中异常不崩溃，记录错误日志

### 6.4 代码质量验收

- [ ] **AC-21**：回测引擎无硬编码参数（费率、权重、阈值均从配置读取）
- [ ] **AC-22**：回测引擎无重复代码，符合编码规范
- [ ] **AC-23**：所有新增代码有中文注释
- [ ] **AC-24**：回测结果确定性：相同输入产生相同输出

---

## 7. 风险与假设

### 7.1 风险

| 风险 | 概率 | 影响 | 缓解措施 |
|:---|:---|:---|:---|
| 回测引擎在0.5 CPU下超时 | 中 | 调优流程中断 | 设置10秒超时，超时则跳过；优化回测算法（纯Python循环，无pandas依赖） |
| 回测结果与真实成交偏差大 | 中 | AI调优方向错误 | 回测引擎计入手续费和滑点；在Prompt中说明"回测 vs 真实"的差异 |
| LLM不理解新指标体系 | 低 | 参数建议质量下降 | 在Prompt中详细说明每个指标的含义和调优方向 |
| K线表数据缺失（K线服务故障） | 低 | 无法执行回测 | 双路Fallback机制（PG + API），两路都失败时发送告警 |
| 内存占用超限 | 低 | 容器被OOM Kill | 预计增量 < 128MB，在512MB限制内；回测结果不缓存大量中间数据 |

### 7.2 假设

| 假设 | 说明 |
|:---|:---|
| `kline_ethusdt_1h` 表正常存在 | 由K线服务自动创建，持续采集数据 |
| 回测使用1h K线，足够精确 | 1h粒度的高/低/开/收能够合理判断是否触及网格挂单价 |
| 网格策略为"等差网格"模式 | 回测引擎按等差网格实现；等比网格模式暂不支持 |
| 回测中不考虑网格重置 | 假设网格区间一周内不变；实际生产中网格可按市场状态重置 |
| 市场状态分类阈值（2%）合理 | 由回测配置提供，后续可根据实际回测结果调优 |

---

## 8. MoSCoW 优先级

| 优先级 | 内容 |
|:---|:---|
| **Must（必须）** | Step 1-6 完整流程、回测引擎、参数验证、无交易调优 |
| **Should（应该）** | 市况细分统计、飞书通知展示回测数据 |
| **Could（可以）** | 等比网格模式支持、回测结果缓存（避免重复回测） |
| **Won't（不做）** | 修改 ai-tuner 容器资源限制、支持其他交易对的回测 |

---

## 9. 附录

### 9.1 术语表

| 术语 | 说明 |
|:---|:---|
| 方案C | 当前混合模式：真实成交数据 + 简单公式估算模拟推演 |
| 方案D | 本PRD目标：真实K线逐笔回测引擎 |
| 网格回测 | 基于历史K线逐根模拟网格策略的挂单、成交、持仓管理 |
| 综合评分 | 收益率×0.40 + 最大回撤×0.30 + 夏普比率×0.30 |
| 效果追踪器 | `EffectTracker`，负责将调优前后的绩效数据写入 memory 表 |
| K线服务 | `trading_system-kline`，负责从币安采集K线并存储到PostgreSQL |

### 9.2 参考文件

- [grid_adapter.py](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/adapters/grid_adapter.py) -- 当前网格适配器（方案C）
- [base_adapter.py](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/adapters/base_adapter.py) -- 适配器基类 + 数据模型
- [weekly_job.py](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/scheduler/weekly_job.py) -- 周度调优主流程
- [ai_tuner/config.yaml](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/config.yaml) -- AI调优系统配置
- [grid/config.yaml](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/grid/config.yaml) -- 网格策略配置
- [docker-compose.yml](file:///Users/yl/vscode/Binance_quantitative_trading/docker-compose.yml) -- 容器编排（ai-tuner资源限制）
- [grid_system.txt](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/prompts/grid_system.txt) -- 网格策略System Prompt
- [grid_user.txt](file:///Users/yl/vscode/Binance_quantitative_trading/ai_tuner/prompts/grid_user.txt) -- 网格策略User Prompt模板
- [collector.py](file:///Users/yl/vscode/Binance_quantitative_trading/services/kline_service/core/collector.py) -- K线采集器（表名规则）