# StratTuneAI 反馈闭环系统 —— 产品需求文档 (PRD)

---

## 文档信息

| 字段 | 内容 |
|------|------|
| 文档版本 | v1.0 |
| 创建日期 | 2026-08-11 |
| 作者 | 需求文档专家 |
| 审核人 | 用户确认 |
| 最后更新 | 2026-08-11 |
| 关联文档 | [PRD-多策略AI调优系统](./PRD-多策略AI调优系统.md) |

### 修改记录

| 日期 | 版本 | 修改人 | 修改内容 |
|------|------|--------|----------|
| 2026-08-11 | v1.0 | 需求文档专家 | 初始版本，完整 PRD 编写 |

---

## 1. 产品概述

### 1.1 背景与问题

StratTuneAI 多策略AI调优系统已实现以下闭环流程：

```
采集数据 → 构建上下文 → 调用 LLM 生成建议 → 保存到 DB（pending）→ 自动执行系统写入 tuning_overrides → 结束
```

该流程存在一个关键缺口：**效果追踪和反馈闭环没有打通**。具体表现为：

| 问题 | 影响 | 严重程度 |
|------|------|----------|
| LLM 看不到"上次调优实际带来了什么效果" | 每次调优是"盲人摸象"，不知道过去的调整是有效还是无效 | 高 |
| 没有反馈机制，LLM 无法从过去的调优结果中学习 | 调优质量无法持续提升，每周都是"从零开始" | 高 |
| 效果追踪数据字段（`post_win_rate`, `post_total_pnl`）已存在但从未被填充 | 数据库设计已预留反馈能力，但整个链路未打通 | 中 |
| 历史调优上下文仅展示摘要，缺乏"当时预期 vs 实际效果"的对比 | LLM 无法判断自己的建议是否靠谱，难以建立信任 | 中 |
| 无学习信号引导，LLM 可能反复犯同样的错误 | 调优方向可能来回摇摆，缺乏连贯性 | 低 |

### 1.2 产品目标

构建一个完整的反馈闭环系统，使 StratTuneAI 能够：

1. **效果可追踪**：每次调优在一周后自动回填"实际绩效"，让 LLM 看到调优的真实效果
2. **上下文可对比**：在历史调优上下文中加入"调优前 vs 调优后"的指标对比表格
3. **学习可传导**：在 Prompt 中注入明确的学习信号，引导 LLM 基于反馈做出有方向的调整
4. **闭环可迭代**：每次调优都基于前一次的效果反馈，逐步提升调优质量

### 1.3 用户人群

与主系统一致，无新增角色：

| 角色 | 职责 | 影响 |
|------|------|------|
| 策略管理员 | 审批 AI 调优建议 | 无变化，仍通过飞书卡片审批 |
| 系统运维 | 监控系统运行状态 | 无变化 |
| 策略开发者 | 维护 Prompt 模板 | 需理解学习信号规则，但无需修改模板 |

### 1.4 成功指标

| 指标 | 目标值 | 衡量方式 |
|------|--------|----------|
| `post_*` 字段填充率 | >= 90% | 每周调优前检查上周记录是否已回填 |
| 调优效果可追溯率 | 100% | 每条已生效记录都有对应的效果数据 |
| 学习信号指令执行率 | >= 80% | LLM 输出中"依据反馈调整"的比例 |
| 连续正向调整延续率 | 待观察 | 有效策略方向的延续次数 |

---

## 2. 系统架构

### 2.1 反馈闭环在整体架构中的位置

```
采集层 (adapters) → 记忆层 (memory) → 决策层 (engine) → 审批层 (notifier) → 执行层 (deploy)
     ↑                                                                              ↓
     └── [新增] 效果追踪层 (feedback) ← 下周调优前触发 ──────────────────────────────┘
```

### 2.2 新增模块总览

| 模块编号 | 模块名称 | 职责 | 优先级 |
|----------|----------|------|--------|
| FB-1 | EffectTracker（效果追踪器） | 在每周调优前，计算上周实际效果，回填到上上周记录 | 必须 |
| FB-2 | ContextEnhancer（上下文增强器） | 将效果追踪的摘要格式化为 LLM 可理解的结构化上下文 | 必须 |
| FB-3 | LearningSignal（学习信号注入） | 在 Prompt 中注入明确的"学习指令"，引导 LLM 基于反馈调整 | 必须 |

### 2.3 协作时序

```
第N周周日 23:55 触发（修改后的完整流程）：

  Step 0:  检查是否有"上上周已生效"的记录
                │
                ▼
  Step 1:  EffectTracker.track_and_fill()
            → 读取 tuning_overrides/.active 找到上周生效的版本
            → 在 strategy_memory 表中通过 active_version 匹配记录
            → 复用 Adapter.collect() 计算上周绩效
            → 回填 post_win_rate, post_total_pnl, effect_notes
            → 返回效果摘要
                │
                ▼
  Step 2:  ContextEnhancer.build_feedback_context()
            → 接收效果摘要
            → 生成 Markdown 对比表格（调优前 vs 调优后）
            → 生成定性评价（良好/一般/较差/数据不足）
            → 返回结构化反馈文本
                │
                ▼
  Step 3:  构建完整 Prompt（原有流程，但注入反馈上下文 + 学习信号）
            → 原有 memory_history 插值
            → [新增] feedback_context 插值
            → [新增] learning_instructions 插值
                │
                ▼
  Step 4:  调用 LLM → 返回 JSON 建议（原有流程不变）
                │
                ▼
  Step 5:  保存新记录（原有流程，但预填 active_version 字段）
                │
                ▼
  Step 6:  自动执行系统（外部）生效后标记 is_applied=TRUE（原有流程不变）
```

### 2.4 模块依赖关系

```
EffectTracker
  ├── 依赖: version_manager (通过 tuning_overrides/.active 读取版本号)
  ├── 依赖: MemoryDBHandler (读取/更新 strategy_memory 表)
  ├── 依赖: BaseAdapter.collect() (复用现有适配器计算绩效)
  └── 输出: EffectSummary (Dict)

ContextEnhancer
  ├── 依赖: EffectTracker 输出的 EffectSummary
  ├── 依赖: 已有的 ContextBuilder (用于集成到历史上下文)
  └── 输出: feedback_context (str, Markdown 格式)

LearningSignal
  ├── 依赖: 已有的 prompts/ 目录（通过 Prompt 模板插值注入）
  ├── 依赖: EffectTracker 输出的效果评级
  └── 输出: learning_instructions (str, 注入到 System Prompt)
```

---

## 3. 功能需求

### 3.1 FB-1：EffectTracker（效果追踪器）

#### 3.1.1 功能描述

EffectTracker 是反馈闭环的起点。它的职责是：**在每周调优开始前，计算"上周实际表现"，回填到"上上周AI建议记录"的 `post_*` 字段**。

#### 3.1.2 核心流程

```
┌─────────────────────────────────────────────────────────────────────┐
│ EffectTracker.track_and_fill(strategy_id, adapter, db_handler)      │
│                                                                     │
│ 1. 读取 tuning_overrides/.active → 获取"上周生效的版本号"            │
│    (例如 "V20260804")                                                │
│                                                                     │
│ 2. 在 strategy_memory 表中查找:                                      │
│    strategy_id = $strategy_id                                        │
│    AND is_applied = TRUE                                             │
│    AND active_version = "V20260804"                                  │
│    → 找到对应的记录（即"上上周AI建议"）                                │
│                                                                     │
│ 3. 如果没有找到匹配记录 → 跳过，返回空摘要                            │
│                                                                     │
│ 4. 如果找到记录 → 检查 post_* 字段是否已填充：                         │
│    - 已填充 → 跳过，返回已有摘要（避免重复计算）                       │
│    - 未填充 → 继续                                                  │
│                                                                     │
│ 5. 调用 adapter.collect() 获取"上周"（即上上周建议生效后的那一周）的    │
│    绩效数据（即 post_win_rate, post_total_pnl 等）                   │
│                                                                     │
│ 6. 回填 strategy_memory 记录:                                        │
│    - post_win_rate = 上周实际胜率                                    │
│    - post_total_pnl = 上周实际总盈亏                                 │
│    - effect_notes = 定性评价 + 备注                                  │
│                                                                     │
│ 7. 返回 EffectSummary 字典                                           │
└─────────────────────────────────────────────────────────────────────┘
```

#### 3.1.3 接口定义

```python
class EffectTracker:
    """
    效果追踪器
    在每周调优前，计算"上周实际表现"，回填到"上上周AI建议记录"的 post_* 字段
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化效果追踪器

        Args:
            config: 系统配置字典（用于读取 tuning_overrides 目录路径等）
        """
        ...

    async def track_and_fill(
        self,
        strategy_id: str,
        adapter: BaseAdapter,
        db_handler: MemoryDBHandler,
    ) -> EffectSummary:
        """
        执行效果追踪和回填

        Args:
            strategy_id: 策略唯一标识
            adapter: 策略适配器实例（复用其 collect() 方法）
            db_handler: 记忆库数据库处理器

        Returns:
            EffectSummary: 效果摘要（包含评级、指标对比、备注）
        """
        ...


class EffectSummary(BaseModel):
    """效果摘要数据模型"""
    has_data: bool = False                           # 是否有历史数据
    memory_id: int = 0                                # 对应的记忆记录 ID
    pre_win_rate: float = 0.0                         # 调优前胜率（全量历史）
    pre_total_pnl: float = 0.0                        # 调优前总盈亏（全量历史）
    post_win_rate: float = 0.0                        # 调优后胜率（上周实际）
    post_total_pnl: float = 0.0                       # 调优后盈亏（上周实际）
    win_rate_change: float = 0.0                      # 胜率变化（百分点，正值=提升）
    pnl_change: float = 0.0                           # 盈亏变化（USDT，正值=提升）
    max_drawdown_pct: float = 0.0                     # 上周最大回撤百分比
    total_trades: int = 0                              # 上周总交易笔数
    rating: str = "数据不足"                           # 评级：良好/一般/较差/数据不足
    original_version: str = ""                         # 原始版本号（从 .active 读取）
    notes: str = ""                                    # 备注
```

#### 3.1.4 业务规则

| 规则编号 | 规则描述 |
|----------|----------|
| EFT-001 | 执行时机：每周调优流程的第一步，在任何其他操作之前执行 |
| EFT-002 | 数据来源：`tuning_overrides/.active` 文件记录的是"当前生效版本"，**它代表的是上周 AI 建议的版本**（因为上周审批通过后，`.active` 被更新指向上周版本） |
| EFT-003 | 查找逻辑：通过 `strategy_memory.active_version` 字段匹配 `.active` 中的版本号，找到"上上周 AI 建议记录" |
| EFT-004 | 幂等性：如果 `post_*` 字段已非空，说明已回填过，跳过本次回填 |
| EFT-005 | 时间边界：调用 `adapter.collect()` 时，需要传入"上周"的时间范围（上周一 0:00 ~ 上周日 23:59），而非"最近7天" |
| EFT-006 | 首次运行：如果没有任何历史记录（`strategy_memory` 表为空），跳过回填，返回 `has_data=False` |
| EFT-007 | 版本号不匹配：如果 `.active` 版本号在 `strategy_memory` 表中找不到匹配记录，记录警告日志，跳过回填 |
| EFT-008 | 异常容忍：`track_and_fill()` 内部异常不应阻断主流程，捕获异常后记录错误日志，返回空摘要 |

#### 3.1.5 效果评级规则

| 评级 | 条件 | 说明 |
|------|------|------|
| **良好** | 胜率提升 >= 3 个百分点 **或** 收益提升 >= 5% | 调优效果显著，建议延续方向 |
| **一般** | 不符合"良好"和"较差"条件 | 效果中性，需结合其他指标判断 |
| **较差** | 胜率下降 >= 3 个百分点 **且** 收益下降 >= 5% | 调优效果负面，建议回撤或反向调整 |
| **数据不足** | 上周交易笔数 < 3 笔 | 样本量不足，不具备统计意义 |

**评级优先级说明**：评级从"良好"开始判断，依次降级。即：先判断是否满足"良好"条件，再判断"较差"，都不满足则为"一般"，交易笔数不足则覆盖为"数据不足"。

#### 3.1.6 验收标准

- [ ] 每周调优前，EffectTracker 正确读取 `.active` 文件获取版本号
- [ ] 在 `strategy_memory` 表中通过 `active_version` 字段匹配到正确的记录
- [ ] 复用 `adapter.collect()` 方法计算上周绩效，不重新实现绩效计算逻辑
- [ ] `post_win_rate`, `post_total_pnl`, `effect_notes` 字段正确回填
- [ ] 幂等性校验正确：已回填的记录不再重复计算
- [ ] 首次运行/无历史数据时，跳过回填，返回 `has_data=False`
- [ ] 版本号不匹配时，记录警告日志，不抛出异常
- [ ] 评级规则正确（良好/一般/较差/数据不足）
- [ ] 所有异常场景均被捕获，不阻断主流程

---

### 3.2 FB-2：ContextEnhancer（上下文增强器）

#### 3.2.1 功能描述

ContextEnhancer 把 EffectTracker 输出的效果摘要，格式化为 LLM 可理解的结构化上下文。它不是替代现有的 `ContextBuilder`，而是在其基础上增强。

#### 3.2.2 核心流程

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ContextEnhancer.build_feedback_context(effect_summary, current_report)  │
│                                                                         │
│ 1. 接收 EffectTracker 输出的 EffectSummary 对象                          │
│                                                                         │
│ 2. 检查 effect_summary.has_data:                                        │
│    - False → 返回 "暂无历史调优效果数据，这是首次反馈追踪"                  │
│                                                                         │
│ 3. 生成对比表格（Markdown 格式）：                                       │
│    | 指标 | 调优前 | 调优后 | 变化 |                                    │
│    |------|--------|--------|------|                                    │
│    | 胜率 | 42.3%  | 48.7%  | +6.4% ▲ |                                 │
│    | 周收益 | -15.2 | +8.5  | +23.7 USDT ▲ |                            │
│    | 最大回撤 | - | 3.2%  | - |                                        │
│    | 相对BTC表现 | - | +2.1% | - |                                      │
│                                                                         │
│ 4. 生成定性评价段落：                                                    │
│    "效果评级：良好（胜率提升6.4个百分点，收益扭亏为盈）"                    │
│    "总交易笔数：12笔（样本充足）"                                         │
│    "建议方向：延续上次调优方向"                                           │
│                                                                         │
│ 5. 返回结构化反馈文本（str）                                              │
└─────────────────────────────────────────────────────────────────────────┘
```

#### 3.2.3 接口定义

```python
class ContextEnhancer:
    """
    上下文增强器
    将效果追踪的摘要格式化为 LLM 可理解的结构化上下文
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化上下文增强器

        Args:
            config: 系统配置字典
        """
        ...

    def build_feedback_context(
        self,
        effect_summary: EffectSummary,
        current_report: Dict[str, Any],
    ) -> str:
        """
        构建反馈上下文文本

        Args:
            effect_summary: EffectTracker 输出的效果摘要
            current_report: 当前的策略报告字典（用于获取本周的 BTC 表现等）

        Returns:
            Markdown 格式的反馈上下文文本
        """
        ...
```

#### 3.2.4 输出格式规范

**有效果数据时**（`has_data=True`）：

```markdown
## 上次调优效果追踪

### 效果评级：良好

| 指标 | 调优前 | 调优后 | 变化 |
|------|--------|--------|------|
| 胜率 | 42.3% | 48.7% | +6.4% |
| 周收益 | -15.20 USDT | +8.50 USDT | +23.70 USDT |
| 最大回撤 | — | 3.2% | — |
| 上周交易笔数 | — | 12 | — |

### 定性分析
- 上次调优版本：V20260804
- 效果评价：胜率显著提升，收益扭亏为盈，调优方向正确。
- 建议：**延续上次调优方向**，可在此基础上进一步微调。

---

```

**无效果数据时**（`has_data=False`）：

```markdown
## 上次调优效果追踪

暂无历史调优效果数据，这是首次反馈追踪。

---

```

#### 3.2.5 集成方式

`ContextEnhancer` 的输出通过以下方式注入到 Prompt 中：

1. 在 `WeeklyTuningJob._build_prompts()` 中，新增 `feedback_context` 插值变量
2. 在 `btc_eth_user.txt` 等 User Prompt 模板中，新增 `{{ feedback_context }}` 插值占位符
3. 反馈上下文放置在"历史调优记忆"之后，"请分析以上数据"之前

#### 3.2.6 业务规则

| 规则编号 | 规则描述 |
|----------|----------|
| CEH-001 | 反馈上下文必须紧跟在"历史调优记忆"之后 |
| CEH-002 | 对比表格中的"调优前"指标从 `strategy_memory` 记录中的 `full_report` 字段提取 |
| CEH-003 | "调优后"指标从 EffectTracker 回填的 `post_*` 字段提取 |
| CEH-004 | 定性评价中必须包含"建议方向"（延续/回撤/维持），基于评级规则自动生成 |
| CEH-005 | 当 `total_trades < 3` 时，在表格下方增加"数据量不足，请谨慎参考"提示 |
| CEH-006 | 无历史数据时，返回固定文本，不报错 |

#### 3.2.7 验收标准

- [ ] 有效果数据时，输出完整的 Markdown 对比表格
- [ ] 无效果数据时，输出"暂无历史调优效果数据"提示
- [ ] 表格格式正确，指标对齐
- [ ] 定性评价中的"建议方向"与评级规则一致
- [ ] 交易笔数不足3笔时，正确显示"数据不足"提示
- [ ] 输出文本可直接插入到 User Prompt 中，无需额外格式化

---

### 3.3 FB-3：LearningSignal（学习信号注入）

#### 3.3.1 功能描述

LearningSignal 在 Prompt 中注入明确的"学习指令"，引导 LLM 基于反馈做出有方向的调整。它不是一个独立的类，而是一组规则和指令文本，在构建 System Prompt 时注入。

#### 3.3.2 学习信号规则

##### L1：效果驱动的决策方向

| 上次评级 | 推荐行为 | Prompt 指令 |
|----------|----------|-------------|
| 良好 | 延续方向，可微调 | "上次调优效果良好，建议延续上次参数调整方向，可在此基础上进一步微调（调整幅度不超过上次的 50%）" |
| 一般 | 谨慎评估，小幅调整 | "上次调优效果一般，建议谨慎评估当前参数，如有必要可小幅调整或维持不变" |
| 较差 | 回撤/反向调整 | "上次调优效果较差，建议回撤上次调整，或朝相反方向调整（如上次上调了某参数，本次应考虑下调）" |
| 数据不足 | 基准参考，不做调整依据 | "上周交易数据不足，建议以更长时间维度的数据为准，不做基于噪音数据的调整" |

##### L2：避免过度优化

| 条件 | 指令 |
|------|------|
| 当前参数已达标（胜率 >= 50% 且无异常） | "当前策略表现达标，建议维持不变，避免过度优化" |
| 连续两周朝同一方向调整 | "已连续两周朝同一方向调整策略参数，本周建议暂停调整，观察效果" |
| 连续两周调整后效果一般或较差 | "连续两周调整效果不佳，建议本周维持不变，让策略稳定运行一周" |

##### L3：零交易处理

```
如果上周交易笔数 < 3 笔：
  → 不基于上周数据做任何调整
  → 在 Prompt 中明确标注"上周数据样本量不足，不予参考"
  → 仅在历史多周数据充足时进行调整
```

##### L4：连续不变触发

```
如果连续 3 次"维持不变"：
  → 必须输出至少 1 个参数调整（即使幅度很小）
  → 强制输出的目的是打破"僵化"，让策略有机会适应新市场环境
  → 调整幅度控制在正常范围的 50% 以内（保守调整）
```

#### 3.3.3 接口定义

```python
class LearningSignalGenerator:
    """
    学习信号生成器
    根据效果追踪的评级和历史记录，生成注入到 Prompt 中的学习指令文本
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化学习信号生成器

        Args:
            config: 系统配置字典
        """
        ...

    async def build_learning_instructions(
        self,
        strategy_id: str,
        effect_summary: EffectSummary,
        db_handler: MemoryDBHandler,
        current_report: Dict[str, Any],
    ) -> str:
        """
        构建学习指令文本

        Args:
            strategy_id: 策略唯一标识
            effect_summary: EffectTracker 输出的效果摘要
            db_handler: 记忆库数据库处理器（用于查询历史记录判断连续不变等）
            current_report: 当前的策略报告字典

        Returns:
            str: 学习指令文本，注入到 System Prompt 的末尾
        """
        ...

    def _apply_l1_direction(self, rating: str) -> str:
        """L1：效果驱动的决策方向"""
        ...

    def _apply_l2_avoid_over_optimization(
        self, recent_memories: List[Dict]
    ) -> List[str]:
        """L2：避免过度优化"""
        ...

    def _apply_l3_low_trades_check(self, total_trades: int) -> Optional[str]:
        """L3：零交易处理"""
        ...

    def _apply_l4_stale_trigger(
        self, recent_memories: List[Dict]
    ) -> Optional[str]:
        """L4：连续不变触发"""
        ...
```

#### 3.3.4 输出格式规范

```markdown
## 学习指令（基于上次调优反馈）

### 决策方向
- 上次调优评级：良好
- 建议方向：延续上次调优方向，可在此基础上微调
- 理由：胜率提升6.4个百分点，收益扭亏为盈

### 注意事项
- 当前策略表现达标（胜率52.3%），优先维持不变
- 避免过度优化：已连续两周上调止损ATR倍数，建议本周暂停调整该参数
- 交易数据充足（12笔），可正常参考

### 约束
- 如果当前策略表现达标，建议 adjustments 为空对象 {}
- 如果连续3次维持不变，必须输出至少1个调整
```

#### 3.3.5 注入方式

学习指令注入到 `System Prompt`（即 `common_rules.txt` 或策略专属 `system.txt` 的末尾），通过新增的 `{{ learning_instructions }}` 插值变量实现。

修改后的 System Prompt 结构：

```
[common_rules.txt 内容]

[btc_eth_system.txt 内容]

{{ learning_instructions }}
```

#### 3.3.6 业务规则

| 规则编号 | 规则描述 |
|----------|----------|
| LRN-001 | L1 规则必须基于 EffectTracker 的评级执行，不可凭空判断 |
| LRN-002 | L2 规则中的"连续两周"通过查询 `strategy_memory` 表的最近 2 条已生效记录判断 |
| LRN-003 | L3 规则中"上周"指 `current_report.meta.week_start ~ week_end` 对应的时间范围，`total_trades` 从 `current_report.performance.total_trades` 读取 |
| LRN-004 | L4 规则中"连续 3 次维持不变"通过查询 `strategy_memory` 表的最近 3 条已生效记录判断：如果 3 条的 `ai_suggestions->adjustments` 均为空对象 `{}`，则触发 |
| LRN-005 | 多条规则同时触发时，按 L1 > L2 > L3 > L4 优先级输出，L1 和 L2 的指令合并输出，L3 和 L4 的指令作为附加约束追加 |
| LRN-006 | 首次运行无历史数据时，跳过所有学习指令，输出"暂无历史调优数据，请基于当前数据做判断" |

#### 3.3.7 验收标准

- [ ] L1 规则正确：评级为"良好"时输出"延续方向"，"较差"时输出"回撤/反向"
- [ ] L2 规则正确：连续两周同方向调整时输出"暂停调整"
- [ ] L3 规则正确：交易笔数 < 3 时输出"数据不足，不予参考"
- [ ] L4 规则正确：连续 3 次维持不变时输出"必须输出至少 1 个调整"
- [ ] 多条规则同时触发时，按优先级合并输出，不冲突
- [ ] 首次运行无历史数据时，跳过学习指令
- [ ] 输出文本格式规范，可直接插入 System Prompt 末尾

---

## 4. 数据库变更

### 4.1 strategy_memory 表变更

**新增字段**：

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `active_version` | VARCHAR(20) | 空字符串 | 生效的覆盖层版本号，如 `V20260804` |

**已有字段（无需新增，但首次被填充）**：

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `post_win_rate` | FLOAT | 调优应用后的实际胜率，由 EffectTracker 回填 |
| `post_total_pnl` | FLOAT | 调优应用后的实际总盈亏（USDT），由 EffectTracker 回填 |
| `effect_notes` | TEXT | 效果备注，如"良好：胜率提升6.4%" |

### 4.2 active_version 字段写入时机

| 时机 | 写入值 | 写入者 |
|------|--------|--------|
| 新记录创建时（Step 5） | 当前 `.active` 版本号（即"上周生效的版本"） | `db_handler.save_memory()` |
| 记录回填时（Step 1） | 不修改（保持原值） | EffectTracker 不回写该字段 |

### 4.3 查询逻辑变更

**查找"上上周已生效记录"**：

```sql
SELECT id, strategy_id, active_version, post_win_rate, post_total_pnl, effect_notes, full_report
FROM trading.strategy_memory
WHERE strategy_id = $1
  AND is_applied = TRUE
  AND active_version = $2    -- $2 从 tuning_overrides/.active 读取
ORDER BY created_at DESC
LIMIT 1
```

**查询"连续不变"历史**（L4 规则）：

```sql
SELECT ai_suggestions
FROM trading.strategy_memory
WHERE strategy_id = $1
  AND is_applied = TRUE
ORDER BY created_at DESC
LIMIT 3
```

### 4.4 DDL 变更

```sql
-- 新增 active_version 字段
ALTER TABLE trading.strategy_memory
ADD COLUMN IF NOT EXISTS active_version VARCHAR(20) DEFAULT '';

-- 创建索引（加速按版本号查找）
CREATE INDEX IF NOT EXISTS idx_memory_active_version
    ON trading.strategy_memory (strategy_id, active_version);
```

### 4.5 验收标准

- [ ] `active_version` 字段正确新增，默认值为空字符串
- [ ] 新记录创建时正确写入 `active_version` 字段
- [ ] 索引创建成功，查询性能达标
- [ ] 已有数据向后兼容（旧记录的 `active_version` 为空字符串，不影响查询）

---

## 5. Prompt 模板变更

### 5.1 User Prompt 模板变更

在 `btc_eth_user.txt` 中新增 `{{ feedback_context }}` 插值：

```diff
## 历史调优记忆

{{ memory_history }}

+## 上次调优效果追踪
+
+{{ feedback_context }}

请基于以上数据，给出本周的参数调优建议。严格按照 JSON 格式输出。
```

### 5.2 System Prompt 模板变更

在 `common_rules.txt` 末尾或策略专属 `system.txt` 末尾新增 `{{ learning_instructions }}` 插值：

```diff
## 重要提醒

- 如果当前策略表现良好，建议 adjustments 为空对象 {}，表示"维持不变"
- 不要调整不在白名单中的参数
- 参数值必须是数字类型，不要使用字符串
- confidence 取值范围 0-1，表示你对该建议的信心程度

+{{ learning_instructions }}
```

### 5.3 验收标准

- [ ] User Prompt 模板中 `{{ feedback_context }}` 插值正确渲染
- [ ] System Prompt 模板中 `{{ learning_instructions }}` 插值正确渲染
- [ ] 无反馈上下文时（首次运行），渲染为空字符串或占位文本
- [ ] 无学习指令时（首次运行），渲染为空字符串或占位文本
- [ ] 所有策略（btc_eth, new_coin, hrs, grid）的 Prompt 模板均做相应更新

---

## 6. 调度器变更

### 6.1 WeeklyTuningJob 流程变更

修改 `_tune_single_strategy()` 方法，在 Step 3（采集数据）之后、Step 4（构建上下文）之前，插入反馈闭环流程：

```python
async def _tune_single_strategy(self, strategy_cfg, force=False) -> str:
    # ... 原有 Step 1-2 ...

    # Step 2: 采集数据（原有）
    report = await adapter.collect()

    # [新增] Step 2.5: 效果追踪与回填
    effect_summary = await self.effect_tracker.track_and_fill(
        strategy_id=strategy_id,
        adapter=adapter,
        db_handler=self.db_handler,
    )

    # [新增] Step 2.6: 构建反馈上下文
    feedback_context = self.context_enhancer.build_feedback_context(
        effect_summary=effect_summary,
        current_report=report.model_dump(),
    )

    # [新增] Step 2.7: 构建学习指令
    learning_instructions = await self.learning_signal_generator.build_learning_instructions(
        strategy_id=strategy_id,
        effect_summary=effect_summary,
        db_handler=self.db_handler,
        current_report=report.model_dump(),
    )

    # Step 3: 构建历史上下文（原有，但 feedback_context 已注入到 Prompt 模板）
    report_dict = report.model_dump()
    context = await self.context_builder.build_context(
        strategy_id=strategy_id,
        db_handler=self.db_handler,
        current_report=report_dict,
    )

    # Step 4: 加载并渲染 Prompt 模板（传递 feedback_context 和 learning_instructions）
    system_prompt, user_prompt = self._build_prompts(
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        adapter=adapter,
        report_dict=report_dict,
        context=context,                # 原有历史上下文
        feedback_context=feedback_context,  # [新增] 反馈上下文
        learning_instructions=learning_instructions,  # [新增] 学习指令
    )

    # ... 后续流程不变 ...
```

### 6.2 WeeklyTuningJob 初始化变更

```python
class WeeklyTuningJob:
    def __init__(self, config, db_manager, ...):
        # ... 原有初始化 ...

        # [新增] 反馈闭环模块
        self.effect_tracker = EffectTracker(config)
        self.context_enhancer = ContextEnhancer(config)
        self.learning_signal_generator = LearningSignalGenerator(config)
```

### 6.3 验收标准

- [ ] 反馈闭环流程在数据采集之后、Prompt 构建之前执行
- [ ] 效果追踪不阻断主流程（异常时降级为空摘要，继续执行）
- [ ] 反馈上下文和学习指令正确注入到 Prompt 模板
- [ ] 无反馈数据时，系统正常运行（向后兼容）

---

## 7. 非功能需求

### 7.1 性能要求

| 指标 | 目标值 | 说明 |
|------|--------|------|
| EffectTracker 耗时 | < 5 秒 | 主要耗时在 `adapter.collect()` 的数据库查询 |
| ContextEnhancer 耗时 | < 0.1 秒 | 纯文本拼接，无外部依赖 |
| LearningSignal 耗时 | < 0.5 秒 | 含一次数据库查询（最近 3 条记录） |
| 反馈闭环总耗时 | < 10 秒 | 不影响原有调优流程的 120 秒目标 |

### 7.2 数据一致性要求

| 需求编号 | 需求描述 |
|----------|----------|
| CON-001 | EffectTracker 的幂等性校验必须可靠，防止同一条记录被重复回填 |
| CON-002 | `post_*` 字段的更新使用数据库事务，确保胜率和盈亏字段同时更新或同时不更新 |
| CON-003 | `.active` 文件读取和 `strategy_memory` 表查询之间没有事务保护，但允许最终一致性（差几秒不影响） |
| CON-004 | 回填操作应在数据库事务中执行，防止部分更新 |

### 7.3 可用性要求

| 需求编号 | 需求描述 |
|----------|----------|
| AVL-001 | EffectTracker 的任何异常不应阻断主流程，捕获异常后返回空摘要 |
| AVL-002 | ContextEnhancer 的格式化异常不应阻断主流程，捕获异常后返回"反馈上下文构建失败" |
| AVL-003 | LearningSignal 的生成异常不应阻断主流程，捕获异常后跳过学习指令注入 |
| AVL-004 | 反馈闭环模块的初始化失败不应阻止系统启动，初始化失败时相关功能降级 |

### 7.4 可维护性要求

| 需求编号 | 需求描述 |
|----------|----------|
| MNT-001 | 效果评级规则（良好/一般/较差/数据不足的阈值）必须通过配置读取，禁止硬编码 |
| MNT-002 | 学习信号规则（L1-L4 的指令文本）必须通过配置或独立文件管理，方便非开发人员修改 |
| MNT-003 | 所有新增模块使用 structlog 输出结构化日志，方便问题排查 |
| MNT-004 | 反馈闭环相关代码统一放在 `ai_tuner/feedback/` 目录下，与现有模块解耦 |

### 7.5 配置项

新增配置项（在 `ai_tuner/config.yaml` 中）：

```yaml
# ============================================================
# 反馈闭环配置
# ============================================================
feedback:
  # 效果评级阈值
  rating:
    good_win_rate_increase: 0.03        # 胜率提升 >= 3 个百分点 → 良好
    good_pnl_increase_ratio: 0.05       # 收益提升 >= 5% → 良好
    bad_win_rate_decrease: 0.03         # 胜率下降 >= 3 个百分点 → 较差
    bad_pnl_decrease_ratio: 0.05        # 收益下降 >= 5% → 较差
    min_trades_for_valid: 3             # 最小有效交易笔数（低于此值 → 数据不足）
  # 学习信号规则
  learning:
    consecutive_same_direction: 2       # 连续同方向调整次数阈值（L2）
    stale_unchanged_count: 3            # 连续维持不变次数阈值（L4）
    stale_adjustment_ratio: 0.5         # 连续不变后的强制调整幅度比例（正常范围的 50%）
```

---

## 8. 数据流与时序

### 8.1 完整调优流程（含反馈闭环）

```
第N周周日 23:55 (北京时间)
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ 1. 调度器触发 weekly_job.py                                   │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. 遍历已注册策略列表 [btc_eth, new_coin, hrs, grid]           │
└──────────────────────┬──────────────────────────────────────┘
                       │
           ┌───────────┴───────────┐
           ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 3a. 采集数据      │    │ 3b. 采集数据      │
│ adapter.collect()│    │ adapter.collect()│
│ → StrategyReport │    │ → StrategyReport │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 4a. 效果追踪      │    │ 4b. 效果追踪      │
│ EffectTracker     │    │ EffectTracker     │
│ .track_and_fill() │    │ .track_and_fill() │
│ → EffectSummary  │    │ → EffectSummary  │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 5a. 构建反馈上下文 │    │ 5b. 构建反馈上下文 │
│ ContextEnhancer   │    │ ContextEnhancer   │
│ .build_feedback.. │    │ .build_feedback.. │
│ → feedback_context│    │ → feedback_context│
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 6a. 构建学习指令   │    │ 6b. 构建学习指令   │
│ LearningSignal    │    │ LearningSignal    │
│ .build_learning.. │    │ .build_learning.. │
│ → learning_inst.. │    │ → learning_inst.. │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 7a. 查询记忆库     │    │ 7b. 查询记忆库     │
│ 最近N条已生效记录  │    │ 最近N条已生效记录  │
│ → memory_history  │    │ → memory_history  │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 8a. 渲染Prompt    │    │ 8b. 渲染Prompt    │
│ System: 通用规则   │    │ System: 通用规则   │
│        + 策略规则  │    │        + 策略规则  │
│        + 学习指令  │    │        + 学习指令  │
│ User: 当前配置     │    │ User: 当前配置     │
│      + 本周报告    │    │      + 本周报告    │
│      + 历史记忆    │    │      + 历史记忆    │
│      + 反馈上下文  │    │      + 反馈上下文  │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 9a. 调用 DeepSeek │    │ 9b. 调用 DeepSeek │
│ API → JSON 建议   │    │ API → JSON 建议   │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 10a. 解析+校验     │    │ 10b. 解析+校验     │
│ 白名单+范围检查    │    │ 白名单+范围检查    │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 11a. 保存记忆      │    │ 11b. 保存记忆      │
│ 写入strategy_     │    │ 写入strategy_     │
│ memory（含active  │    │ memory（含active  │
│ _version）        │    │ _version）        │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 12a. 推送飞书卡片  │    │ 12b. 推送飞书卡片  │
│ 等待人工审批      │    │ 等待人工审批      │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         └───────────┬───────────┘
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
   ┌──────────────┐    ┌──────────────┐
   │ 确认生效      │    │ 拒绝/超时     │
   │ → 写入覆盖层  │    │ → 丢弃建议    │
   │ → 更新.active │    │              │
   │ → 标记applied │    │              │
   └──────┬───────┘    └──────────────┘
          │
          ▼
   ┌─────────────────────────────────────────────┐
   │ 13. 下周调优时，EffectTracker 读取 .active   │
   │     找到上上周记录 → 回填 effect 数据         │
   └─────────────────────────────────────────────┘
```

### 8.2 时间线示例

| 时间 | 事件 | 说明 |
|------|------|------|
| 第N周周日 23:55 | 调优 | 生成建议 V20260804，保存到 memory（is_applied=false） |
| 第N+1周周一 ~ 周三 | 人工审批 | 确认生效，写入覆盖层 V20260804，更新 `.active`，标记 is_applied=true |
| 第N+1周周一 ~ 第N+1周周日 | 策略运行 | 策略按 V20260804 的参数运行一周 |
| 第N+1周周日 23:55 | 下次调优 | EffectTracker 读取 `.active` = V20260804，在 memory 中找到对应记录，回填上周实际绩效 |
| 第N+1周周日 23:55 | 调优 | 生成新建议 V20260811，包含反馈上下文和学习指令 |
| ... | 循环 | ... |

---

## 9. 目录结构变更

```
ai_tuner/
├── feedback/                          # [新增] 反馈闭环模块
│   ├── __init__.py
│   ├── effect_tracker.py              # EffectTracker 效果追踪器
│   ├── context_enhancer.py            # ContextEnhancer 上下文增强器
│   └── learning_signal.py             # LearningSignalGenerator 学习信号生成器
├── memory/
│   ├── db_handler.py                  # [修改] save_memory 新增 active_version 写入
│   └── context_builder.py             # [无修改，但输出会被 ContextEnhancer 补充]
├── prompts/
│   ├── common_rules.txt               # [修改] 末尾新增 {{ learning_instructions }} 插值
│   ├── btc_eth_user.txt               # [修改] 新增 {{ feedback_context }} 插值
│   ├── new_coin_user.txt              # [修改] 新增 {{ feedback_context }} 插值
│   ├── hrs_user.txt                   # [修改] 新增 {{ feedback_context }} 插值
│   ├── grid_user.txt                  # [修改] 新增 {{ feedback_context }} 插值
│   └── ... (system.txt 可选新增学习指令插值)
├── scheduler/
│   └── weekly_job.py                  # [修改] 集成反馈闭环流程
├── config.yaml                        # [修改] 新增 feedback 配置段
└── ...
```

---

## 10. 异常处理策略

### 10.1 异常分级

| 级别 | 场景 | 处理方式 |
|------|------|----------|
| P0 - 致命 | 反馈闭环模块初始化失败 | 不影响系统启动，降级为无反馈模式 |
| P1 - 严重 | EffectTracker 回填数据异常 | 记录错误日志，返回空摘要，继续执行 -->
| P2 - 一般 | ContextEnhancer 格式化失败 | 记录错误日志，使用"反馈上下文构建失败"占位文本 |
| P3 - 轻微 | LearningSignal 生成失败 | 记录错误日志，跳过学习指令注入 |

### 10.2 降级策略

| 场景 | 降级方案 |
|------|----------|
| `.active` 文件不存在或无法读取 | 跳过效果追踪，返回空摘要，继续执行 |
| `strategy_memory` 表中无匹配记录 | 跳过回填，返回空摘要，继续执行 |
| `adapter.collect()` 调用失败 | 记录错误日志，返回空摘要，继续执行 |
| 数据库连接异常 | 捕获异常，返回空摘要，继续执行（不影响主流程） |
| 评级规则配置缺失 | 使用默认阈值（硬编码兜底值），记录警告日志 |

### 10.3 向后兼容

| 场景 | 兼容策略 |
|------|----------|
| 旧版 `strategy_memory` 记录无 `active_version` 字段 | DDL 新增字段时设置默认值 ''，旧记录不影响查询 |
| 旧版 Prompt 模板无 `{{ feedback_context }}` 插值 | 渲染时传入空字符串，模板正常显示，无报错 |
| 旧版 `config.yaml` 无 `feedback` 配置段 | 读取配置时使用 `config.get("feedback", {})`，所有阈值使用默认值 |
| 首次运行，memory 表为空 | EffectTracker 返回 `has_data=False`，ContextEnhancer 显示"暂无数据" |

---

## 11. 验收标准

### 11.1 功能验收清单

#### FB-1：EffectTracker

- [ ] 读取 `.active` 文件获取版本号正确
- [ ] 通过 `active_version` 字段匹配到 `strategy_memory` 记录正确
- [ ] 复用 `adapter.collect()` 计算绩效，不重新实现
- [ ] `post_win_rate`, `post_total_pnl`, `effect_notes` 正确回填
- [ ] 幂等性校验：已回填记录不重复计算
- [ ] 首次运行/无历史数据时，返回 `has_data=False`
- [ ] 版本号不匹配时，记录警告日志，不抛异常
- [ ] 评级规则正确：良好/一般/较差/数据不足
- [ ] 所有异常被捕获，不阻断主流程

#### FB-2：ContextEnhancer

- [ ] 有效果数据时，输出完整 Markdown 对比表格
- [ ] 无效果数据时，输出"暂无历史调优效果数据"
- [ ] 表格格式正确，指标对齐
- [ ] 定性评价与评级规则一致
- [ ] 交易笔数不足3笔时，正确显示"数据不足"提示
- [ ] 输出文本可直接插入 User Prompt

#### FB-3：LearningSignal

- [ ] L1 规则：评级驱动的决策方向正确
- [ ] L2 规则：避免过度优化检测正确
- [ ] L3 规则：低交易量处理正确
- [ ] L4 规则：连续不变触发正确
- [ ] 多条规则按优先级合并输出
- [ ] 首次运行跳过学习指令

#### 集成测试

- [ ] 完整闭环流程：效果追踪 → 上下文增强 → 学习信号 → Prompt 渲染 → LLM 调用 → 保存
- [ ] 数据采集后的反馈闭环在 Prompt 构建前执行
- [ ] 反馈上下文和学习指令正确渲染到 Prompt 中
- [ ] 无反馈数据时，系统正常运行（向后兼容）
- [ ] 异常降级时，系统正常运行（向下兼容）

### 11.2 幻觉测试清单

编码完成后，需要逐项验证以下内容：

```
□ 1. EffectTracker 所有 import 语句的模块/函数/类是否存在
□ 2. EffectTracker 调用的 adapter.collect() 方法签名是否与 BaseAdapter 定义一致
□ 3. 配置项 feedback.rating.* 是否在 config.yaml 中真实存在
□ 4. 所有文件路径（feedback/ 目录）是否真实存在
□ 5. 所有变量名是否拼写正确且已定义（如 EffectSummary 中字段名）
□ 6. 评级规则逻辑：良好(胜率↑≥3% 或 收益↑≥5%)、较差(胜率↓≥3% 且 收益↓≥5%)
□ 7. 所有异常路径有 try/except 处理
□ 8. 异步/同步上下文匹配（track_and_fill 是 async，build_feedback_context 是同步）
□ 9. 评级阈值从配置读取，非硬编码
□ 10. 所有修改过的文件（weekly_job.py, db_handler.py, 模板文件, config.yaml）完整无截断
```

---

## 12. 风险与依赖

### 12.1 风险识别

| 风险编号 | 风险描述 | 影响 | 概率 | 缓解措施 |
|----------|----------|------|------|----------|
| R-001 | `.active` 文件在 EffectTracker 读取后被覆盖（竞态条件） | 低 | 低 | 效果追踪发生在调优流程第一步，`.active` 更新在审批通过后，两者时间差至少数小时 |
| R-002 | 效果评级规则过于简单，不适用于所有市场环境 | 中 | 中 | 评级阈值可配置，后续可根据实际效果调整 |
| R-003 | 学习信号过于强势，限制了 LLM 的自主判断 | 中 | 低 | 学习信号以"建议"形式注入，不强制 LLM 执行；L4 强制输出仅在小幅度内 |
| R-004 | 连续不变触发（L4）可能导致不必要的调整 | 低 | 低 | 调整幅度限制在正常范围的 50%，且仅当连续 3 次才触发 |
| R-005 | 低交易量（<3笔）时调优质量差 | 中 | 中 | L3 规则明确禁止基于噪音数据调整，LLM 仅基于历史数据判断 |

### 12.2 外部依赖

| 依赖 | 类型 | 说明 | 降级方案 |
|------|------|------|----------|
| `tuning_overrides/.active` 文件 | 必须 | 读取生效版本号 | 文件不存在时跳过回填 |
| `strategy_memory` 表 | 必须 | 存储和查询历史记录 | 表不可用时跳过反馈闭环 |
| `adapter.collect()` 方法 | 必须 | 复用绩效计算 | 调用失败时返回空摘要 |
| 评级配置 | 重要 | 效果评级阈值 | 配置缺失时使用默认值 |

### 12.3 后续扩展规划

| 扩展项 | 优先级 | 预计时间 | 说明 |
|--------|--------|----------|------|
| 多周效果聚合 | 低 | 第二期 | 将多条历史效果聚合成趋势图，让 LLM 看到"连续几周的效果变化" |
| 效果归因分析 | 低 | 第三期 | 分析"哪些参数调整带来了正收益"，形成经验知识库 |
| 自动调优策略推荐 | 低 | 第三期 | 基于历史效果，自动推荐"本月该采用哪种调优策略" |
| 跨策略效果对比 | 低 | 第四期 | 对比不同策略的调优效果，找出共性规律 |

---

## 13. 术语表

| 术语 | 英文 | 说明 |
|------|------|------|
| 反馈闭环 | Feedback Loop | 每次调优后追踪效果，将效果反馈给下次调优的机制 |
| 效果追踪 | Effect Tracking | 计算上周实际绩效，回填到上上周调优记录的过程 |
| 效果评级 | Effect Rating | 对调优效果进行定性评价（良好/一般/较差/数据不足） |
| 学习信号 | Learning Signal | 注入到 Prompt 中的指令，引导 LLM 基于反馈做出有方向的调整 |
| 覆盖层 | Override Layer | AI 调优参数写入 `tuning_overrides/` 目录，不修改基础配置 |
| `.active` | Active Pointer | 指向当前生效版本的指针文件，EffectTracker 通过它找到上上周版本 |
| 连续不变触发 | Stale Trigger | 连续 3 次"维持不变"后强制输出至少 1 个调整的规则 |

---

**文档结束**