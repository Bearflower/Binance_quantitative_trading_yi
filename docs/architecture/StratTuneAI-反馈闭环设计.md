# StratTuneAI 反馈闭环系统架构设计文档

---

## 文档信息

| 字段 | 内容 |
|------|------|
| 文档版本 | v1.0 |
| 创建日期 | 2026-08-11 |
| 作者 | 后端架构师 |
| 关联文档 | [PRD-反馈闭环系统](../requirements/StratTuneAI/PRD-反馈闭环系统.md)、[StratTuneAI架构设计](./StratTuneAI架构设计.md) |
| 关联架构图 | [StratTuneAI-反馈闭环架构图](./StratTuneAI-反馈闭环架构图.html) |

---

## 目录

1. [总体架构](#1-总体架构)
2. [模块详细设计](#2-模块详细设计)
3. [数据库设计](#3-数据库设计)
4. [协作流程](#4-协作流程)
5. [配置设计](#5-配置设计)
6. [Prompt 变更](#6-prompt-变更)
7. [受影响文件清单](#7-受影响文件清单)

---

## 1. 总体架构

### 1.1 反馈闭环在整体架构中的定位

反馈闭环系统位于现有 StratTuneAI 架构的"效果追踪层"，在每周调优流程中位于数据采集之后、Prompt 构建之前执行。其核心定位是打通"调优建议 -> 实际效果 -> 反馈给下次调优"的完整链路。

```
现有系统 (已有模块)                    反馈闭环模块 (新增)                   外部依赖/数据存储
┌─────────────────────────────────┐   ┌─────────────────────────┐   ┌─────────────────────────┐
│ 采集层 (BaseAdapter)             │   │ EffectTracker            │   │ strategy_memory 表        │
│   - collect() 采集本周绩效       │──▶│   - track_and_fill()     │◀─▶│   - active_version 字段   │
│   - [设计变更] collect(week_offset)│   │   - 读取 .active 版本    │   │   - post_* 字段回填        │
│                                 │   │   - 复用 adapter 计算绩效 │   │   - idx_memory_active_ver  │
├─────────────────────────────────┤   ├─────────────────────────┤   ├─────────────────────────┤
│ 记忆层 (MemoryDBHandler)         │   │ ContextEnhancer          │   │ tuning_overrides/         │
│   - strategy_memory 表 CRUD      │   │   - build_feedback_ctx()  │◀──│   - .active 版本指针文件    │
│   - save_memory / update_effect  │   │   - 生成 Markdown 表格   │   │                           │
├─────────────────────────────────┤   ├─────────────────────────┤   ├─────────────────────────┤
│ 上下文构建器 (ContextBuilder)      │   │ LearningSignal           │   │ trade_records 表            │
│   - build_context() 历史上下文    │   │   - build_learning_ins()  │◀──│   - 策略交易记录            │
│                                 │   │   - L1~L4 规则引擎       │   │                           │
├─────────────────────────────────┤   └─────────────────────────┘   ├─────────────────────────┤
│ 调度器 (WeeklyTuningJob)         │           │ 集成                   │ ai_tuner/config.yaml       │
│   - _tune_single_strategy()     │◀──────────┘                       │   - [新增] feedback 配置段  │
│   - 编排反馈闭环流程             │                                  └─────────────────────────┘
├─────────────────────────────────┤
│ LLM 引擎 (LLMClient/ResponseParser)│
├─────────────────────────────────┤
│ Prompt 模板                     │
│   - common_rules.txt            │
│   - btc_eth_user.txt            │
├─────────────────────────────────┤
│ 审批层 (飞书卡片通知)              │
├─────────────────────────────────┤
│ 执行层 (tuning_overrides 写入)    │
└─────────────────────────────────┘
```

### 1.2 模块依赖关系

```
EffectTracker
  ├── 依赖: VersionManager（通过 get_active_version() 读取当前生效版本号）
  ├── 依赖: MemoryDBHandler（读取/更新 strategy_memory 表）
  ├── 依赖: BaseAdapter.collect()（复用现有适配器计算绩效，需支持 week_offset 参数）
  └── 输出: EffectSummary（Dict，包含评级、指标对比、备注）

ContextEnhancer
  ├── 依赖: EffectTracker 输出的 EffectSummary
  ├── 依赖: 已有的 ContextBuilder（用于集成到历史上下文，合并输出）
  └── 输出: feedback_context（str, Markdown 格式）

LearningSignal
  ├── 依赖: EffectTracker 输出的效果评级
  ├── 依赖: MemoryDBHandler（查询历史记录判断连续不变等）
  └── 输出: learning_instructions（str, 注入到 System Prompt）
```

### 1.3 模块与现有系统的集成点

| 集成点 | 现有模块 | 变更类型 | 说明 |
|--------|---------|---------|------|
| `WeeklyTuningJob._tune_single_strategy()` | 调度器 | 修改 | 在数据采集后、Prompt 构建前插入反馈闭环流程 |
| `MemoryDBHandler.save_memory()` | 记忆层 | 修改 | 新增 `active_version` 参数写入 |
| `MemoryDBHandler` | 记忆层 | 修改 | 新增 `find_memory_by_version()`、`get_recent_applied_memories()`、`update_effect_tracking()` 方法 |
| `BaseAdapter.collect()` | 采集层 | 修改 | 新增 `week_offset` 参数，支持查询历史周数据 |
| `ContextBuilder.build_context()` | 上下文构建器 | 无修改 | 输出保持不变，ContextEnhancer 合并输出 |
| `common_rules.txt` | Prompt 模板 | 修改 | 末尾新增 `{{ learning_instructions }}` 插值 |
| `btc_eth_user.txt` 等 | Prompt 模板 | 修改 | 新增 `{{ feedback_context }}` 插值 |
| `ai_tuner/config.yaml` | 系统配置 | 修改 | 新增 `feedback` 配置段 |

---

## 2. 模块详细设计

### 2.1 模块一：EffectTracker（效果追踪器）

**文件路径**: `ai_tuner/feedback/effect_tracker.py`

#### 2.1.1 类图

```
┌─────────────────────────────────────────────────────────────────┐
│ EffectTracker                                                    │
│                                                                   │
│ 职责: 在每周调优前，计算"上周实际表现"，回填到"上上周AI建议记录"      │
│ 的 post_* 字段                                                    │
│                                                                   │
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ + __init__(                                                 │  │
│ │     config: Dict[str, Any],                                 │  │
│ │     version_manager: Optional[VersionManager] = None,       │  │
│ │   )                                                         │  │
│ │ + async track_and_fill(                                     │  │
│ │     strategy_id: str,                                       │  │
│ │     adapter: BaseAdapter,                                   │  │
│ │     db_handler: MemoryDBHandler,                            │  │
│ │   ) -> EffectSummary                                        │  │
│ │                                                             │  │
│ │ # 私有方法                                                   │  │
│ │ - _find_memory_record(                                      │  │
│ │     db_handler, strategy_id, active_version                 │  │
│ │   ) -> Optional[Dict]                                       │  │
│ │ - _is_already_filled(memory_record) -> bool                 │  │
│ │ - async _calc_effect(                                       │  │
│ │     adapter, memory_record                                  │  │
│ │   ) -> Dict[str, Any]                                       │  │
│ │ - _calc_rating(summary: Dict) -> str                        │  │
│ │ - _build_effect_notes(                                      │  │
│ │     rating, win_rate_change, pnl_change, total_trades       │  │
│ │   ) -> str                                                  │  │
│ └─────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ EffectSummary (Pydantic BaseModel)                               │
│                                                                   │
│ has_data: bool = False          # 是否有历史数据                  │
│ memory_id: int = 0              # 对应的记忆记录 ID              │
│ pre_win_rate: float = 0.0       # 调优前胜率（全量历史）          │
│ pre_total_pnl: float = 0.0      # 调优前总盈亏（全量历史）        │
│ post_win_rate: float = 0.0      # 调优后胜率（上周实际）          │
│ post_total_pnl: float = 0.0     # 调优后盈亏（上周实际）          │
│ win_rate_change: float = 0.0    # 胜率变化（百分点，正值=提升）    │
│ pnl_change: float = 0.0         # 盈亏变化（USDT，正值=提升）      │
│ max_drawdown_pct: float = 0.0   # 上周最大回撤百分比               │
│ total_trades: int = 0           # 上周总交易笔数                   │
│ rating: str = "数据不足"         # 评级：良好/一般/较差/数据不足    │
│ original_version: str = ""      # 原始版本号                      │
│ notes: str = ""                 # 备注                            │
└─────────────────────────────────────────────────────────────────┘
```

#### 2.1.2 核心流程

```
EffectTracker.track_and_fill(strategy_id, adapter, db_handler):
  1. 通过 VersionManager.get_active_version() 读取当前生效版本号
     (例如 "V20260804")，复用已有的版本管理逻辑，避免重复实现

  2. 在 strategy_memory 表中查找:
     strategy_id = $strategy_id
     AND is_applied = TRUE
     AND active_version = "V20260804"
     → 找到对应的记录（即"上上周AI建议"）

  3. 如果没有找到匹配记录 → 跳过，返回 EffectSummary(has_data=False)

  4. 如果找到记录 → 检查 post_* 字段是否已填充:
     - post_win_rate IS NOT NULL 且 post_total_pnl IS NOT NULL
     - 已填充 → 跳过，返回已有摘要（避免重复计算）
     - 未填充 → 继续

  5. 调用 adapter.collect(week_offset=-1) 获取"上周"（即上上周建议
     生效后的那一周）的绩效数据（post_win_rate, post_total_pnl 等）

  6. 回填 strategy_memory 记录:
     - post_win_rate = 上周实际胜率
     - post_total_pnl = 上周实际总盈亏
     - effect_notes = 定性评价 + 备注

  7. 返回 EffectSummary 字典
```

#### 2.1.3 关键方法签名

```python
class EffectTracker:
    """
    效果追踪器
    在每周调优前，计算"上周实际表现"，回填到"上上周AI建议记录"的 post_* 字段
    """

    def __init__(self, config: Dict[str, Any], version_manager: Optional[VersionManager] = None):
        """
        初始化效果追踪器

        Args:
            config: 系统配置字典（用于读取 tuning_overrides 目录路径等）
            version_manager: 版本管理器实例，如未提供则自动创建
        """
        self.config = config
        # 从配置读取反馈闭环配置段，使用 .get() 提供默认值确保向后兼容
        feedback_cfg = config.get("feedback", {})
        rating_cfg = feedback_cfg.get("rating", {})
        self.good_win_rate_increase = rating_cfg.get("good_win_rate_increase", 0.03)
        self.good_pnl_increase_usdt = rating_cfg.get("good_pnl_increase_usdt", 5.0)
        self.bad_win_rate_decrease = rating_cfg.get("bad_win_rate_decrease", 0.03)
        self.bad_pnl_decrease_usdt = rating_cfg.get("bad_pnl_decrease_usdt", 5.0)
        self.min_trades_for_valid = rating_cfg.get("min_trades_for_valid", 3)
        # 版本管理器：复用 VersionManager，避免重复实现版本读取逻辑
        self.version_manager = version_manager or VersionManager(config)

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

    async def _find_memory_record(
        self,
        db_handler: MemoryDBHandler,
        strategy_id: str,
        active_version: str,
    ) -> Optional[Dict[str, Any]]:
        """
        在 strategy_memory 表中查找匹配的记录

        Args:
            db_handler: 记忆库数据库处理器
            strategy_id: 策略唯一标识
            active_version: 版本号

        Returns:
            匹配的记录字典，未找到返回 None
        """
        ...

    def _is_already_filled(self, memory_record: Dict[str, Any]) -> bool:
        """
        检查 post_* 字段是否已填充（幂等性校验）

        Args:
            memory_record: 记忆记录字典

        Returns:
            True 如果已填充，False 否则
        """
        ...

    async def _calc_effect(
        self,
        adapter: BaseAdapter,
        memory_record: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        调用 adapter.collect(week_offset=-1) 计算上周绩效

        Args:
            adapter: 策略适配器实例
            memory_record: 记忆记录字典（用于提取 pre_* 基线数据）

        Returns:
            包含绩效数据的字典
        """
        ...

    def _calc_rating(
        self,
        summary: Dict[str, Any],
    ) -> str:
        """
        计算效果评级

        评级规则（从配置读取阈值）:
        - 良好: 胜率提升 >= good_win_rate_increase 或 收益提升 >= good_pnl_increase_usdt
        - 较差: 胜率下降 >= bad_win_rate_decrease 且 收益下降 >= bad_pnl_decrease_usdt
        - 数据不足: 上周交易笔数 < min_trades_for_valid
        - 一般: 不符合上述条件

        Args:
            summary: 包含 win_rate_change, pnl_change, total_trades 等字段的字典

        Returns:
            "良好" / "一般" / "较差" / "数据不足"
        """
        ...

    def _build_effect_notes(
        self,
        rating: str,
        win_rate_change: float,
        pnl_change: float,
        total_trades: int,
    ) -> str:
        """
        构建效果备注文本

        Args:
            rating: 评级
            win_rate_change: 胜率变化
            pnl_change: 盈亏变化
            total_trades: 总交易笔数

        Returns:
            备注文本
        """
        ...
```

#### 2.1.4 设计要点

- **复用 adapter.collect()**: EffectTracker 不重新实现绩效计算逻辑，而是通过 `week_offset` 参数复用已有适配器的 `collect()` 方法。这要求对 `BaseAdapter.collect()` 进行向后兼容的修改，新增 `week_offset=0` 参数。
- **复用 VersionManager**: EffectTracker 不直接读取 `.active` 文件，而是注入 `VersionManager` 实例，通过 `get_active_version()` 获取当前生效版本号，避免重复实现版本读取逻辑。
- **幂等性**: 通过检查 `post_win_rate` 和 `post_total_pnl` 是否已非空来避免重复计算。
- **异常降级**: 所有异常在 `track_and_fill()` 内部捕获，返回 `EffectSummary(has_data=False)`，不阻断主流程。
- **评级阈值从配置读取**: 评级阈值通过 `config.yaml` 的 `feedback.rating` 配置段读取，禁止硬编码。评级参数 `good_pnl_increase_usdt` 和 `bad_pnl_decrease_usdt` 以 USDT 绝对值作为阈值，而非百分比。

---

### 2.2 模块二：ContextEnhancer（上下文增强器）

**文件路径**: `ai_tuner/feedback/context_enhancer.py`

#### 2.2.1 类图

```
┌─────────────────────────────────────────────────────────────────┐
│ ContextEnhancer                                                  │
│                                                                   │
│ 职责: 把 EffectTracker 输出的效果摘要，格式化为 LLM 可理解的       │
│ 结构化上下文。与现有 ContextBuilder 合并输出，而非替代。            │
│                                                                   │
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ + __init__(config: Dict[str, Any])                          │  │
│ │ + build_feedback_context(                                   │  │
│ │     effect_summary: EffectSummary,                          │  │
│ │     current_report: Dict[str, Any],                         │  │
│ │   ) -> str                                                  │  │
│ │                                                             │  │
│ │ # 私有方法                                                   │  │
│ │ - _format_table(summary) -> str                             │  │
│ │ - _format_rating_comment(summary) -> str                    │  │
│ │ - _build_suggestion_direction(rating) -> str                │  │
│ │ - _get_pre_metrics(memory_record) -> Dict                   │  │
│ └─────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

#### 2.2.2 核心流程

```
ContextEnhancer.build_feedback_context(effect_summary, current_report):
  1. 检查 effect_summary.has_data:
     - False → 返回 "暂无历史调优效果数据，这是首次反馈追踪。"

  2. 从 effect_summary 提取指标:
     - pre_win_rate, pre_total_pnl
     - post_win_rate, post_total_pnl
     - win_rate_change, pnl_change
     - max_drawdown_pct, total_trades
     - rating, original_version

  3. 生成 Markdown 对比表格:
     | 指标 | 调优前 | 调优后 | 变化 |
     |------|--------|--------|------|
     | 胜率 | 42.3% | 48.7% | +6.4% |
     | 周收益 | -15.20 USDT | +8.50 USDT | +23.70 USDT |
     | 最大回撤 | — | 3.2% | — |
     | 上周交易笔数 | — | 12 | — |

  4. 生成定性评价段落:
     - 效果评级
     - 调优版本
     - 效果评价描述
     - 建议方向 (延续/回撤/维持)

  5. 返回完整的 Markdown 格式反馈上下文文本
```

#### 2.2.3 关键方法签名

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
        self.config = config

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

    def _format_table(self, summary: EffectSummary) -> str:
        """
        生成 Markdown 对比表格

        Args:
            summary: 效果摘要

        Returns:
            Markdown 表格字符串
        """
        ...

    def _format_rating_comment(self, summary: EffectSummary) -> str:
        """
        生成定性评价段落

        Args:
            summary: 效果摘要

        Returns:
            评价文本段落
        """
        ...

    @staticmethod
    def _build_suggestion_direction(rating: str) -> str:
        """
        根据评级生成建议方向

        Args:
            rating: 评级（良好/一般/较差/数据不足）

        Returns:
            建议方向文本
        """
        ...
```

#### 2.2.4 输出格式

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

#### 2.2.5 集成方式

ContextEnhancer 的输出通过以下方式注入到 Prompt：

1. 在 `WeeklyTuningJob._build_prompts()` 中，新增 `feedback_context` 参数
2. 在 `btc_eth_user.txt` 等 User Prompt 模板中，新增 `{{ feedback_context }}` 插值占位符
3. 反馈上下文放置在"历史调优记忆"之后，"请分析以上数据"之前

#### 2.2.6 与现有 ContextBuilder 的关系

| 维度 | ContextBuilder（现有） | ContextEnhancer（新增） |
|------|----------------------|------------------------|
| 输入 | 最近 N 条已生效记忆 | EffectTracker 输出的 EffectSummary |
| 输出 | 历史调优简史（文本段落） | 反馈对比表格 + 定性评价（Markdown） |
| 插值变量 | `{{ memory_history }}` | `{{ feedback_context }}` |
| 位置 | User Prompt 中"历史调优记忆"段 | User Prompt 中紧跟"历史调优记忆"之后 |
| 关系 | 独立执行 | 依赖 EffectTracker 输出，补充 ContextBuilder |

---

### 2.3 模块三：LearningSignal（学习信号生成器）

**文件路径**: `ai_tuner/feedback/learning_signal.py`

#### 2.3.1 类图

```
┌─────────────────────────────────────────────────────────────────┐
│ LearningSignalGenerator                                          │
│                                                                   │
│ 职责: 根据效果追踪的评级和历史记录，生成注入到 Prompt 中的学习指令   │
│ 文本。不依赖 LLM 判断，用规则引擎做前置决策。                       │
│                                                                   │
│ ┌─────────────────────────────────────────────────────────────┐  │
│ │ + __init__(config: Dict[str, Any])                          │  │
│ │ + async build_learning_instructions(                        │  │
│ │     strategy_id: str,                                       │  │
│ │     effect_summary: EffectSummary,                          │  │
│ │     db_handler: MemoryDBHandler,                            │  │
│ │     current_report: Dict[str, Any],                         │  │
│ │   ) -> str                                                  │  │
│ │                                                             │  │
│ │ # 私有方法（规则引擎）                                        │  │
│ │ - _apply_l1_direction(rating) -> str                        │  │
│ │ - async _apply_l2_avoid_over_optimization(                  │  │
│ │     db_handler, strategy_id                                 │  │
│ │   ) -> List[str]                                            │  │
│ │ - _apply_l3_low_trades_check(total_trades) -> Optional[str] │  │
│ │ - async _apply_l4_stale_trigger(                            │  │
│ │     db_handler, strategy_id                                 │  │
│ │   ) -> Optional[str]                                        │  │
│ └─────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

#### 2.3.2 规则引擎（L1-L4）

**L1：效果驱动的决策方向**

| 上次评级 | 推荐行为 | 指令文本 |
|----------|----------|---------|
| 良好 | 延续方向，可微调 | "上次调优效果良好，建议延续上次参数调整方向，可在此基础上进一步微调（调整幅度不超过上次的 50%）" |
| 一般 | 谨慎评估，小幅调整 | "上次调优效果一般，建议谨慎评估当前参数，如有必要可小幅调整或维持不变" |
| 较差 | 回撤/反向调整 | "上次调优效果较差，建议回撤上次调整，或朝相反方向调整（如上次上调了某参数，本次应考虑下调）" |
| 数据不足 | 基准参考，不做调整依据 | "上周交易数据不足，建议以更长时间维度的数据为准，不做基于噪音数据的调整" |

**L2：避免过度优化**

| 条件 | 指令 |
|------|------|
| 当前参数已达标（胜率 >= 50% 且无异常） | "当前策略表现达标，建议维持不变，避免过度优化" |
| 连续多次朝同一方向调整 | "已连续多次朝同一方向调整策略参数，本周建议暂停调整，观察效果" |
| 连续多次调整后效果一般或较差 | "连续多次调整效果不佳，建议本周维持不变，让策略稳定运行一周" |

方向一致性判断逻辑：遍历每条记录的 `ai_suggestions.adjustments`，提取每个参数的 `from` 和 `to` 值，生成 `param_path:direction` 标记（`:up` 表示上调，`:down` 表示下调）。使用 `all_up or all_down` 判断：如果所有标记全部以 `:up` 结尾则为全部向上，全部以 `:down` 结尾则为全部向下。任一方向全部一致即判定为"连续同方向调整"，触发警告。

**L3：零交易处理**

```
如果上周交易笔数 < min_trades_for_valid（从配置读取，默认 3 笔）：
  → 不基于上周数据做任何调整
  → 在 Prompt 中明确标注"上周数据样本量不足，不予参考"
  → 仅在历史多周数据充足时进行调整
```

**L4：连续不变触发**

```
如果连续 stale_unchanged_count 次"维持不变"（从配置读取，默认 3 次）：
  → 必须输出至少 1 个参数调整（即使幅度很小）
  → 强制输出的目的是打破"僵化"，让策略有机会适应新市场环境
  → 调整幅度控制在正常范围的 stale_adjustment_ratio * 100% 以内（保守调整，默认 50%）
```

#### 2.3.3 关键方法签名

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
        self.config = config
        feedback_cfg = config.get("feedback", {})
        learning_cfg = feedback_cfg.get("learning", {})
        self.consecutive_same_direction = learning_cfg.get("consecutive_same_direction", 2)
        self.stale_unchanged_count = learning_cfg.get("stale_unchanged_count", 3)
        self.stale_adjustment_ratio = learning_cfg.get("stale_adjustment_ratio", 0.5)

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

    async def _apply_l2_avoid_over_optimization(
        self,
        db_handler: MemoryDBHandler,
        strategy_id: str,
    ) -> List[str]:
        """L2：避免过度优化"""
        ...

    def _apply_l3_low_trades_check(self, total_trades: int) -> Optional[str]:
        """L3：零交易处理"""
        ...

    async def _apply_l4_stale_trigger(
        self,
        db_handler: MemoryDBHandler,
        strategy_id: str,
    ) -> Optional[str]:
        """L4：连续不变触发"""
        ...
```

#### 2.3.4 输出格式

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

#### 2.3.5 规则优先级

多条规则同时触发时，按以下优先级合并输出：

| 优先级 | 规则 | 输出方式 |
|--------|------|---------|
| 最高 | L1（决策方向） | 始终输出，作为"决策方向"段落 |
| 高 | L2（避免过度优化） | 合并输出到"注意事项"段落 |
| 中 | L3（零交易处理） | 作为附加约束追加到"注意事项" |
| 低 | L4（连续不变触发） | 作为附加约束追加到"约束"段落 |

---

## 3. 数据库设计

### 3.1 表结构变更

`strategy_memory` 表新增 `active_version` 字段，已有 `post_*` 字段首次被填充。

#### 3.1.1 新增字段

| 字段名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `active_version` | VARCHAR(20) | 空字符串 `''` | 生效的覆盖层版本号，如 `V20260804` |

#### 3.1.2 已有字段（首次被填充）

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `post_win_rate` | FLOAT | 调优应用后的实际胜率，由 EffectTracker 回填 |
| `post_total_pnl` | FLOAT | 调优应用后的实际总盈亏（USDT），由 EffectTracker 回填 |
| `effect_notes` | TEXT | 效果备注，如"良好：胜率提升6.4%" |

#### 3.1.3 DDL 变更

```sql
-- 新增 active_version 字段
ALTER TABLE trading.strategy_memory
ADD COLUMN IF NOT EXISTS active_version VARCHAR(20) DEFAULT '';

-- 创建索引（加速按版本号查找）
CREATE INDEX IF NOT EXISTS idx_memory_active_version
    ON trading.strategy_memory (strategy_id, active_version);
```

### 3.2 索引设计

| 索引名 | 列 | 类型 | 用途 |
|--------|------|------|------|
| `idx_memory_strategy_date` | `(strategy_id, created_at DESC)` | 已有 | 查询策略的历史记忆记录 |
| `idx_memory_active_version` | `(strategy_id, active_version)` | 新增 | EffectTracker 按版本号查找匹配记录 |

### 3.3 查询逻辑

**查找"上上周已生效记录"**（EffectTracker 使用）：

```sql
SELECT id, strategy_id, active_version, post_win_rate, post_total_pnl,
       effect_notes, full_report, ai_suggestions, created_at
FROM trading.strategy_memory
WHERE strategy_id = $1
  AND is_applied = TRUE
  AND active_version = $2    -- $2 从 tuning_overrides/.active 读取
ORDER BY created_at DESC
LIMIT 1
```

**查询"连续不变"历史**（L4 规则使用）：

```sql
SELECT ai_suggestions
FROM trading.strategy_memory
WHERE strategy_id = $1
  AND is_applied = TRUE
ORDER BY created_at DESC
LIMIT 3
```

**查询"连续方向"历史**（L2 规则使用）：

```sql
SELECT ai_suggestions, created_at
FROM trading.strategy_memory
WHERE strategy_id = $1
  AND is_applied = TRUE
ORDER BY created_at DESC
LIMIT 2
```

### 3.4 active_version 字段写入时机

| 时机 | 写入值 | 写入者 | 说明 |
|------|--------|--------|------|
| 新记录创建时 | 当前 `.active` 版本号 | `db_handler.save_memory()` 新增参数 | 即"上周生效的版本" |
| 记录回填时 | 不修改（保持原值） | EffectTracker 不回写该字段 | 该字段在创建时已确定 |
| 首次运行 | 空字符串 `''` | 自动迁移 | 旧记录不影响查询 |

### 3.5 向后兼容

- 旧记录无 `active_version` 字段：DDL 新增时设置默认值 `''`，EffectTracker 查询时不会匹配到
- 旧记录 `post_*` 字段为 NULL：EffectTracker 的幂等性校验正常通过，会回填新数据
- 旧版 Prompt 模板无 `{{ feedback_context }}` 插值：渲染时传入空字符串，模板正常显示

---

## 4. 协作流程

### 4.1 完整调优时序（含反馈闭环）

```
第N周周日 23:55 (北京时间)
    │
    ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 1. 调度器触发 weekly_job.run_weekly_tuning()                          │
└──────────────────────┬───────────────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 2. 遍历已注册策略列表 [btc_eth, new_coin, hrs, grid]                   │
│    对每个策略执行 _tune_single_strategy()                               │
└──────────────────────┬───────────────────────────────────────────────┘
                       │
           ┌───────────┴───────────┐
           ▼                       ▼
    ┌──────────────────┐   ┌──────────────────┐
    │ 3a. 采集数据       │   │ 3b. 采集数据       │
    │ adapter.collect() │   │ adapter.collect() │
    │ → StrategyReport │   │ → StrategyReport │
    └────────┬──────────┘   └────────┬──────────┘
             │                       │
             ▼                       ▼
    ┌──────────────────────────────────────────────────────┐
    │ 4. 反馈闭环流程（新增）                                  │
    │                                                       │
    │ 4a. EffectTracker.track_and_fill()                    │
    │     → 读取 .active → 查找 memory 记录 → 回填 post_*    │
    │     → 返回 EffectSummary                              │
    │                                                       │
    │ 4b. ContextEnhancer.build_feedback_context()          │
    │     → 接收 EffectSummary → 生成 Markdown 对比表格     │
    │     → 返回 feedback_context 文本                       │
    │                                                       │
    │ 4c. LearningSignal.build_learning_instructions()      │
    │     → 接收 EffectSummary → L1~L4 规则引擎判断         │
    │     → 返回 learning_instructions 文本                  │
    └──────────────────────┬────────────────────────────────┘
                           │
                           ▼
    ┌──────────────────────────────────────────────────────┐
    │ 5. 构建历史上下文（原有流程）                            │
    │ context_builder.build_context()                       │
    │ → memory_history 文本                                  │
    └──────────────────────┬────────────────────────────────┘
                           │
                           ▼
    ┌──────────────────────────────────────────────────────┐
    │ 6. 渲染 Prompt（含新增插值变量）                        │
    │                                                       │
    │ System Prompt:  common_rules.txt                      │
    │                + 策略 system.txt                       │
    │                + {{ learning_instructions }}  [新增]   │
    │                                                       │
    │ User Prompt:   当前配置 + 本周报告                      │
    │                + {{ memory_history }}                  │
    │                + {{ feedback_context }}      [新增]    │
    └──────────────────────┬────────────────────────────────┘
                           │
                           ▼
    ┌──────────────────────────────────────────────────────┐
    │ 7. 调用 LLM → 返回 JSON 建议（原有流程不变）            │
    └──────────────────────┬────────────────────────────────┘
                           │
                           ▼
    ┌──────────────────────────────────────────────────────┐
    │ 8. 解析 + 校验 + 保存记忆（含 active_version 写入）    │
    │ save_memory(..., active_version=当前版本号)            │
    └──────────────────────┬────────────────────────────────┘
                           │
                           ▼
    ┌──────────────────────────────────────────────────────┐
    │ 9. 推送飞书调优通知卡片                                │
    │    （维持不变时跳过推送）                               │
    └──────────────────────┬────────────────────────────────┘
                           │
                           ▼
    ┌──────────────────────────────────────────────────────┐
    │ 10. 判断审批模式：                                     │
    │                                                       │
    │ 10a. auto-apply 启用时（enabled=true）：               │
    │      ├─ ConfigOperator.apply_overrides() → 写入覆盖层 │
    │      ├─ mark_applied(approved_by="auto_apply")        │
    │      └─ Messenger.send_auto_applied_notification()    │
    │                                                       │
    │ 10b. auto-apply 禁用时（默认）：                       │
    │      ├─ 等待人工确认 /confirm 或超时过期               │
    │      ├─ 确认后：写入覆盖层 → 更新 .active → 标记 applied │
    │      └─ 拒绝/超时：仅记录，不修改配置                   │
    └──────────────────────────────────────────────────────┘
```

### 4.2 时间线示例

| 时间 | 事件 | 说明 |
|------|------|------|
| 第N周周日 23:55 | 调优 | 生成建议 V20260804，保存到 memory（is_applied=false） |
| 第N+1周周一~周三 | 人工审批（默认模式） | 确认生效，写入覆盖层 V20260804，更新 `.active`，标记 is_applied=true |
| 第N+1周周一 00:00 | auto-apply（启用时） | 系统自动写入覆盖层，标记 is_applied=true，approved_by="auto_apply" |
| 第N+1周周一~周日 | 策略运行 | 策略按 V20260804 的参数运行一周 |
| 第N+1周周日 23:55 | 下次调优 | EffectTracker 读取 `.active` = V20260804，在 memory 中找到对应记录，回填上周实际绩效 |
| 第N+1周周日 23:55 | 调优 | 生成新建议 V20260811，包含反馈上下文和学习指令 |
| ... | 循环 | ... |

### 4.3 异常处理流程

#### 4.3.1 异常分级

| 级别 | 场景 | 处理方式 | 阻断主流程？ |
|------|------|---------|:-----------:|
| P0 - 致命 | 反馈闭环模块初始化失败 | 不影响系统启动，降级为无反馈模式 | 否 |
| P1 - 严重 | EffectTracker 回填数据异常 | 记录错误日志，返回空摘要，继续执行 | 否 |
| P2 - 一般 | ContextEnhancer 格式化失败 | 记录错误日志，使用"反馈上下文构建失败"占位文本 | 否 |
| P3 - 轻微 | LearningSignal 生成失败 | 记录错误日志，跳过学习指令注入 | 否 |

#### 4.3.2 降级策略

```
                              ┌──────────────────────┐
                              │ 开始反馈闭环流程       │
                              └──────────┬───────────┘
                                         │
                              ┌──────────▼───────────┐
                              │ EffectTracker         │
                              │ .track_and_fill()     │
                              └──────────┬───────────┘
                                         │
                    ┌────────────────────┼────────────────────┐
                    ▼                    ▼                    ▼
            ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
            │ 正常完成      │    │ 无数据/跳过   │    │ 异常捕获      │
            │ → 有效摘要    │    │ → 空摘要      │    │ → 空摘要      │
            └──────┬───────┘    └──────┬───────┘    └──────┬───────┘
                   │                  │                   │
                   └──────────────────┼───────────────────┘
                                      │
                           ┌──────────▼───────────┐
                           │ ContextEnhancer       │
                           │ .build_feedback_ctx() │
                           └──────────┬───────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                  ▼
            ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
            │ 正常完成      │ │ 无数据       │ │ 异常捕获      │
            │ → 对比表格    │ │ → 占位文本   │ │ → "构建失败"  │
            └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
                   │                │                │
                   └────────────────┼────────────────┘
                                    │
                           ┌────────▼───────────┐
                           │ LearningSignal      │
                           │ .build_learning()   │
                           └────────┬───────────┘
                                    │
                    ┌───────────────┼────────────────┐
                    ▼               ▼                 ▼
            ┌──────────────┐ ┌──────────┐ ┌──────────────┐
            │ 正常完成      │ │ 无历史   │ │ 异常捕获      │
            │ → 学习指令    │ │ → 空文本 │ │ → 跳过注入    │
            └──────┬───────┘ └────┬─────┘ └──────┬───────┘
                   │              │               │
                   └──────────────┼───────────────┘
                                  │
                                  ▼
                           ┌──────────────────┐
                           │ 继续主流程        │
                           │ Prompt 构建 / LLM │
                           └──────────────────┘
```

#### 4.3.3 具体降级场景

| 场景 | 降级方案 | 日志级别 |
|------|---------|:-------:|
| `.active` 文件不存在或无法读取 | 跳过效果追踪，返回 EffectSummary(has_data=False) | WARNING |
| `strategy_memory` 表中无匹配记录 | 跳过回填，返回 EffectSummary(has_data=False) | INFO |
| `adapter.collect(week_offset=-1)` 调用失败 | 记录错误日志，返回 EffectSummary(has_data=False) | ERROR |
| 数据库连接异常 | 捕获异常，返回空摘要 | ERROR |
| 评级规则配置缺失 | 使用默认阈值（硬编码兜底值），记录警告日志 | WARNING |
| ContextEnhancer 格式化异常 | 返回"反馈上下文构建失败"占位文本 | ERROR |
| LearningSignal 生成异常 | 跳过学习指令注入，返回空字符串 | ERROR |

---

## 5. 配置设计

### 5.1 新增 feedback 配置段

在 `ai_tuner/config.yaml` 中新增以下配置：

```yaml
# ============================================================
# 反馈闭环配置
# ============================================================
feedback:
  # 效果评级阈值（用于 EffectTracker._calc_rating）
  rating:
    good_win_rate_increase: 0.03        # 胜率提升 >= 3% 视为"良好"
    good_pnl_increase_usdt: 5.0         # 收益提升 >= 5 USDT 视为"良好"
    bad_win_rate_decrease: 0.03         # 胜率下降 >= 3% 视为"较差"
    bad_pnl_decrease_usdt: 5.0          # 收益下降 >= 5 USDT 视为"较差"
    min_trades_for_valid: 3             # 最小有效交易笔数（低于此值 → 数据不足）

  # 学习信号规则
  learning:
    consecutive_same_direction: 2       # 连续同方向调整次数阈值（L2 规则）
    stale_unchanged_count: 3            # 连续维持不变次数阈值（L4 规则）
    stale_adjustment_ratio: 0.5         # 连续不变后的强制调整幅度比例（正常范围的 50%）

  # 学习信号指令模板（L1 规则）
  # 可在此处自定义各评级对应的指令文本
  instructions:
    l1_good: "上次调优效果良好，建议延续上次参数调整方向，可在此基础上进一步微调（调整幅度不超过上次的 50%）"
    l1_fair: "上次调优效果一般，建议谨慎评估当前参数，如有必要可小幅调整或维持不变"
    l1_poor: "上次调优效果较差，建议回撤上次调整，或朝相反方向调整（如上次上调了某参数，本次应考虑下调）"
    l1_insufficient: "上周交易数据不足，建议以更长时间维度的数据为准，不做基于噪音数据的调整"
```

### 5.2 配置读取示例

```python
# 在 EffectTracker 中读取评级配置
feedback_cfg = config.get("feedback", {})
rating_cfg = feedback_cfg.get("rating", {})

# 使用 .get() 提供默认值，确保旧版 config.yaml 无此配置段时仍能运行
self.good_win_rate_increase = rating_cfg.get("good_win_rate_increase", 0.03)
self.good_pnl_increase_usdt = rating_cfg.get("good_pnl_increase_usdt", 5.0)
self.min_trades_for_valid = rating_cfg.get("min_trades_for_valid", 3)

# 版本管理器：复用 VersionManager，避免重复实现版本读取逻辑
self.version_manager = version_manager or VersionManager(config)
```

### 5.3 向后兼容

- 旧版 `config.yaml` 无 `feedback` 配置段：使用 `config.get("feedback", {})` 读取，所有阈值使用默认值
- 配置项缺失：各模块在 `__init__` 中使用 `.get()` 提供默认值，不会因配置缺失而报错
- 旧版 `rating` 配置段使用 `good_pnl_increase_ratio`（百分比）作为阈值，新版改为 `good_pnl_increase_usdt`（USDT 绝对值），旧版配置需更新参数名
- 指令模板缺失：LearningSignal 使用内置指令文本作为 fallback

---

## 6. Prompt 变更

### 6.1 User Prompt 模板变更

#### btc_eth_user.txt

```diff
## 历史调优记忆

{{ memory_history }}

+## 上次调优效果追踪
+
+{{ feedback_context }}

请基于以上数据，给出本周的参数调优建议。严格按照 JSON 格式输出。
```

#### 新增插值变量说明

| 插值变量 | 类型 | 来源 | 示例值 |
|---------|------|------|--------|
| `{{ feedback_context }}` | str | ContextEnhancer.build_feedback_context() | 包含 Markdown 对比表格的完整反馈上下文 |

#### 其他策略模板（new_coin_user.txt, hrs_user.txt, grid_user.txt）

所有策略的 User Prompt 模板做相同的变更，在 `{{ memory_history }}` 之后、结尾提示之前插入 `{{ feedback_context }}` 插值。

> **注意**: `hrs_user.txt` 模板在本次变更中同时新增了 `{{ memory_history }}` 插值变量（此前缺失），确保历史调优记忆上下文在所有策略模板中统一可用。

### 6.2 System Prompt 模板变更

#### common_rules.txt

```diff
## 重要提醒

- 如果当前策略表现良好，建议 adjustments 为空对象 {}，表示"维持不变"
- 不要调整不在白名单中的参数
- 参数值必须是数字类型，不要使用字符串
- confidence 取值范围 0-1，表示你对该建议的信心程度

+{{ learning_instructions }}
```

#### 新增插值变量说明

| 插值变量 | 类型 | 来源 | 示例值 |
|---------|------|------|--------|
| `{{ learning_instructions }}` | str | LearningSignalGenerator.build_learning_instructions() | 包含决策方向、注意事项、约束的完整学习指令 |

### 6.3 渲染逻辑变更

`WeeklyTuningJob._build_prompts()` 方法需要修改以支持新的插值变量：

```python
def _build_prompts(
    self,
    strategy_id: str,
    strategy_name: str,
    adapter,
    report_dict: Dict[str, Any],
    context: str,
    feedback_context: str = "",            # [新增] 反馈上下文
    learning_instructions: str = "",       # [新增] 学习指令
) -> tuple:
    # 加载通用规则
    common_rules = self._load_template(...)

    # 组装系统提示词（含学习指令插值）
    system_prompt = Template(common_rules).render(
        learning_instructions=learning_instructions,
    )

    # 渲染用户提示词（含反馈上下文插值）
    user_prompt = Template(strategy_user_template).render(
        strategy_name=strategy_name,
        current_params=json.dumps(...),
        report=json.dumps(...),
        memory_history=context,
        feedback_context=feedback_context,  # [新增]
    )

    return system_prompt, user_prompt
```

### 6.4 向后兼容

- 旧版 Prompt 模板无 `{{ feedback_context }}` 插值：Jinja2 渲染时如果模板中无此插值，传入额外变量不会报错（Jinja2 默认行为是忽略未使用的变量）
- 旧版 `common_rules.txt` 无 `{{ learning_instructions }}` 插值：同上，渲染时忽略
- 首次运行无反馈数据：`feedback_context` 传入 ContextEnhancer 输出的"暂无数据"占位文本，`learning_instructions` 传入空字符串

---

## 7. 受影响文件清单

### 7.1 新增文件

| 文件路径 | 模块 | 说明 |
|---------|------|------|
| `ai_tuner/feedback/__init__.py` | 反馈闭环 | 模块初始化文件 |
| `ai_tuner/feedback/effect_tracker.py` | 反馈闭环 | EffectTracker 效果追踪器实现（内含 EffectSummary 数据模型） |
| `ai_tuner/feedback/context_enhancer.py` | 反馈闭环 | ContextEnhancer 上下文增强器实现 |
| `ai_tuner/feedback/learning_signal.py` | 反馈闭环 | LearningSignalGenerator 学习信号生成器实现 |
| `ai_tuner/deploy/version_manager.py` | 部署模块 | 版本管理器（EffectTracker 依赖，通过 get_active_version() 读取当前生效版本号） |
| `docs/architecture/StratTuneAI-反馈闭环架构图.html` | 文档 | 架构图 HTML 文件 |

### 7.2 修改文件

| 文件路径 | 变更类型 | 变更说明 |
|---------|---------|---------|
| `ai_tuner/adapters/base_adapter.py` | 修改 | `collect()` 方法新增 `week_offset: int = 0` 参数，支持查询历史周数据 |
| `ai_tuner/adapters/mtpcs_adapter.py` | 修改 | `collect()` 方法实现 `week_offset` 参数，根据偏移量计算时间范围 |
| `ai_tuner/adapters/new_coin_adapter.py` | 修改 | 同上，实现 `week_offset` 参数 |
| `ai_tuner/adapters/hrs_adapter.py` | 修改 | 同上，实现 `week_offset` 参数 |
| `ai_tuner/adapters/grid_adapter.py` | 修改 | 实现 `week_offset` 参数；修复硬编码兜底值，改为从 `simulation.fallback_spacing_pct` 配置读取 |
| `ai_tuner/memory/db_handler.py` | 修改 | `save_memory()` 新增 `active_version` 参数；新增 `find_memory_by_version()`、`get_recent_applied_memories()`、`update_effect_tracking()` 方法 |
| `ai_tuner/scheduler/weekly_job.py` | 修改 | `_tune_single_strategy()` 集成反馈闭环流程；`__init__` 初始化三个反馈模块（EffectTracker、ContextEnhancer、LearningSignalGenerator）及 VersionManager；`_build_prompts()` 传递新插值变量 |
| `ai_tuner/prompts/common_rules.txt` | 修改 | 末尾新增 `{{ learning_instructions }}` 插值 |
| `ai_tuner/prompts/btc_eth_user.txt` | 修改 | 新增 `{{ feedback_context }}` 插值 |
| `ai_tuner/prompts/new_coin_user.txt` | 修改 | 新增 `{{ feedback_context }}` 插值 |
| `ai_tuner/prompts/hrs_user.txt` | 修改 | 新增 `{{ memory_history }}` 和 `{{ feedback_context }}` 插值 |
| `ai_tuner/prompts/grid_user.txt` | 修改 | 新增 `{{ feedback_context }}` 插值 |
| `ai_tuner/config.yaml` | 修改 | 新增 `feedback` 和 `simulation` 配置段 |

### 7.3 不变文件（确认无修改）

| 文件路径 | 说明 |
|---------|------|
| `ai_tuner/memory/context_builder.py` | ContextBuilder 输出保持不变，ContextEnhancer 补充而非替代 |
| `ai_tuner/engine/llm_client.py` | LLM 调用逻辑不变 |
| `ai_tuner/engine/response_parser.py` | 响应解析逻辑不变 |
| `ai_tuner/engine/cost_tracker.py` | Token 计费逻辑不变 |
| `ai_tuner/deploy/diff_generator.py` | 变更清单生成逻辑不变 |

### 7.4 变更总览

| 统计项 | 数量 |
|--------|:----:|
| 新增文件 | 6 |
| 修改文件 | 13 |
| 不变文件（确认） | 5 |
| 总计受影响文件 | 24 |

---

## 附录 A：BaseAdapter.collect() 接口变更说明

### 现状

```python
class BaseAdapter(ABC):
    @abstractmethod
    async def collect(self) -> StrategyReport:
        """采集本周策略表现数据"""
        ...
```

### 变更后

```python
class BaseAdapter(ABC):
    @abstractmethod
    async def collect(self, week_offset: int = 0) -> StrategyReport:
        """
        采集策略表现数据

        Args:
            week_offset: 周偏移量
                - 0（默认）: 当前周（常规调度使用）
                - -1: 上一周（EffectTracker 回填使用）
                - -2: 上上周
                ...

        Returns:
            StrategyReport: 标准化策略报告
        """
        ...
```

### 实现说明

各具体适配器（MTPCSAdapter、NewCoinAdapter、HRSAdapter、GridAdapter）的 `collect()` 方法需要将 `week_offset` 参数应用到时间范围计算逻辑中。当前代码中时间范围计算逻辑位于 `collect()` 方法内部：

```python
# 当前 MTPCSAdapter.collect() 的时间范围计算
now = datetime.now()
this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
week_start = this_monday if now.weekday() == 6 else this_monday - timedelta(days=7)
week_end = week_start + timedelta(days=7)
```

修改为：

```python
# 修改后，支持 week_offset
now = datetime.now()
this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
base_week_start = this_monday if now.weekday() == 6 else this_monday - timedelta(days=7)
# 应用 week_offset：偏移量 * 7 天
week_start = base_week_start + timedelta(days=week_offset * 7)
week_end = week_start + timedelta(days=7)
```

---

## 附录 B：MemoryDBHandler.save_memory() 接口变更说明

### 现状

```python
async def save_memory(
    self,
    strategy_id: str,
    strategy_name: str,
    report: Dict[str, Any],
    ai_suggestions: Dict[str, Any],
    summary: str = "",
) -> int:
    # INSERT 语句中无 active_version 字段
    ...
```

### 变更后

```python
async def save_memory(
    self,
    strategy_id: str,
    strategy_name: str,
    report: Dict[str, Any],
    ai_suggestions: Dict[str, Any],
    summary: str = "",
    active_version: str = "",          # [新增] 生效版本号
) -> int:
    """
    保存一条新的调优记忆记录

    Args:
        strategy_id: 策略唯一标识
        strategy_name: 策略显示名称
        report: 完整的 StrategyReport 字典
        ai_suggestions: AI 输出的调优建议字典
        summary: AI 生成的摘要
        active_version: 生效的覆盖层版本号（新增）

    Returns:
        新记录的 ID
    """
    query = f"""
        INSERT INTO {self._schema}.strategy_memory
            (strategy_id, strategy_name, version, week_start, week_end,
             summary, full_report, ai_suggestions, active_version,
             created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, NOW(), NOW())
        RETURNING id
    """
    ...
```

---

## 附录 C：WeeklyTuningJob 集成变更说明

### 初始化变更

```python
class WeeklyTuningJob:
    def __init__(self, config, db_manager, ...):
        # ... 原有初始化 ...

        # [新增] 反馈闭环模块
        self.effect_tracker = EffectTracker(config)
        self.context_enhancer = ContextEnhancer(config)
        self.learning_signal_generator = LearningSignalGenerator(config)
```

### _tune_single_strategy() 流程变更

```python
async def _tune_single_strategy(self, strategy_cfg, force=False) -> str:
    # ... 原有 Step 1: 加载适配器 ...

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

    # Step 3: 构建历史上下文（原有）
    report_dict = report.model_dump()
    context = await self.context_builder.build_context(
        strategy_id=strategy_id,
        db_handler=self.db_handler,
        current_report=report_dict,
    )

    # Step 4: 渲染 Prompt（传递新增插值变量）
    system_prompt, user_prompt = self._build_prompts(
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        adapter=adapter,
        report_dict=report_dict,
        context=context,
        feedback_context=feedback_context,          # [新增]
        learning_instructions=learning_instructions,  # [新增]
    )

    # ... 后续流程不变 ...
```

---

## 8. P0 安全网加固（v6.24.0）

### 8.1 红线参数防护

在 `ai_tuner/config.yaml` 中为每个策略定义 `redline_params`，AI 建议涉及红线参数时直接拒绝（不截断）。

| 策略 | 红线参数 |
|------|---------|
| **btc_eth** | `binance.leverage.S/A/B/C`, `binance.position_ratio.S/A/B/C`, `risk.stop_loss_atr_multiplier`, `risk.chandelier_stop.activation_atr`, `risk.chandelier_stop.trailing_atr` |
| **new_coin** | `trading.leverage`, `trading.single_position_margin`, `trading.stop_loss_percent`, `trading.take_profit_percent`, `trading.emergency_stop.trigger_percent`, `trading.risk_control.max_loss_percent` |
| **hrs** | `risk.stop_loss.atr_hard_stop`, `risk.stop_loss.emergency_stop_percent`, `risk.stop_loss.min_absolute_stop_percent` |
| **grid** | `trading.leverage`, `trading.margin`, `trading.single_position_margin`, `risk.stop_loss_percent`, `risk.hard_stop_loss` |

### 8.2 大变化率告警

每个策略配置 `change_rate_threshold: 2.0`（200%），当 AI 建议的单参数变化率超过此阈值时，记录警告（不阻断流程，但会在审批卡片中突出显示）。

### 8.3 validate_params 统一实现

`BaseAdapter` 提供统一的 `validate_params()` 实现，4 层校验：

```
1. 白名单检查 → 不在白名单中的参数直接拒绝
2. 红线参数检查 → 红线参数直接拒绝（不截断）
3. 数值范围检查 → 超出范围的参数截断到边界值（记录为警告）
4. 大变化率检查 → 变化率超过阈值时记录警告（不阻断）
```

返回格式：`{"valid": bool, "errors": list, "warnings": list, "validated": dict}`

各策略适配器删除重复的 `validate_params` 实现，统一继承基类。

### 8.4 每日健康检查

新增 `ai_tuner/monitor/daily_health_check.py`，每天 10:00 执行：

| 检查项 | 阈值（从配置读取） | 说明 |
|--------|-------------------|------|
| 总亏损 | `large_loss_threshold_*` | 策略专用阈值，默认 -50 USDT |
| 连续亏损 | `max_consecutive_loss_threshold: 4` | 超过 4 笔连续亏损告警 |
| 胜率过低 | `low_win_rate_threshold: 0.3` | 24h 胜率低于 30% 告警 |

异常时通过飞书推送告警，推送方式复用 Messenger 的 `send_alert()` 方法。

### 8.5 受影响文件

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `ai_tuner/config.yaml` | 修改 | 新增 `redline_params`、`change_rate_threshold`、`health_check_cron`、`default_large_loss_threshold`、`large_loss_threshold_hrs` |
| `ai_tuner/adapters/base_adapter.py` | 修改 | 统一 `validate_params` 实现（4层校验），新增 `get_change_rate_threshold()` |
| `ai_tuner/adapters/mtpcs_adapter.py` | 修改 | 删除重复的 `validate_params` |
| `ai_tuner/adapters/new_coin_adapter.py` | 修改 | 同上 |
| `ai_tuner/adapters/grid_adapter.py` | 修改 | 同上，修复 `profit_rate` 变量未定义问题 |
| `ai_tuner/adapters/hrs_adapter.py` | 修改 | 同上 |
| `ai_tuner/monitor/daily_health_check.py` | 新增 | 每日健康检查模块 |
| `ai_tuner/notifier/messenger.py` | 修改 | 新增 `send_alert()` 方法 |
| `ai_tuner/main.py` | 修改 | 注册每日健康检查定时任务 |

---

**文档结束**