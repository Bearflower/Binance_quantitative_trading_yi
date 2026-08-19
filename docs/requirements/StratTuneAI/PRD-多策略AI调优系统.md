# StratTuneAI 多策略AI调优系统 —— 产品需求文档 (PRD)

---

## 文档信息

| 字段 | 内容 |
|------|------|
| 文档版本 | v1.3 |
| 创建日期 | 2026-06-21 |
| 作者 | 需求文档专家 |
| 审核人 | 用户确认 |
| 最后更新 | 2026-08-11 |
| 关联文档 | [多策略AI调优系统技术路线](./多策略AI调优系统技术路线.md) |

### 修改记录

| 日期 | 版本 | 修改人 | 修改内容 |
|------|------|--------|----------|
| 2026-08-11 | v1.3 | 代码图书馆长 | 引入 AI 调优覆盖层（tuning_overrides）机制，ConfigOperator 拆分 apply_changes/apply_overrides，更新回滚流程 |
| 2026-06-22 | v1.1 | 用户确认 | 完成 9 项关键决策澄清，新增决策记录章节 |
| 2026-06-21 | v1.0 | 需求文档专家 | 初始版本，完整 PRD 编写 |

---

## 1. 产品概述

### 1.1 背景与问题

当前量化交易系统已运行 4 个策略（MTPCS趋势、新币做空、HRS反转、网格交易），策略参数调优完全依赖人工经验。随着市场环境变化，策略参数（如评分阈值、止损倍数、仓位比例等）需要定期调整才能保持竞争力。人工调优存在以下痛点：

- **调优频率低**：人工调优周期不固定，往往在策略表现明显恶化后才被动调整
- **经验依赖强**：调优质量高度依赖个人经验，缺乏系统化分析方法
- **缺乏历史追溯**：每次调优的理由和效果没有结构化记录，无法形成知识积累
- **多策略管理难**：4 个策略各自独立，缺乏统一的调优平台和审批流程

### 1.2 产品目标

StratTuneAI 是一个 AI 驱动的多策略参数自动调优系统，核心目标：

1. **自动化分析**：每周自动采集各策略的周度表现数据，生成标准化健康报告
2. **AI 辅助决策**：利用 DeepSeek-v4-pro 大模型分析报告，结合历史调优记忆，生成参数调整建议
3. **人工审批在环**：AI 建议默认经人工确认后方可生效，同时支持自动审批模式（auto-apply），可在 `config.yaml` 的 `approval.auto_apply.enabled` 配置项中开启
4. **知识沉淀**：每次调优过程结构化记录，形成可追溯的策略进化日志
5. **安全兜底**：自动回滚机制保护策略在极端情况下的安全

### 1.3 用户人群

| 角色 | 职责 | 使用频率 |
|------|------|----------|
| 策略管理员 | 审批 AI 调优建议，做出最终决策 | 每周一次（周日审批） |
| 系统运维 | 监控系统运行状态，处理异常告警 | 按需 |
| 策略开发者 | 新增策略接入、Prompt 模板维护 | 按需 |

### 1.4 成功指标

| 指标 | 目标值 | 衡量方式 |
|------|--------|----------|
| 系统可用性 | >= 99.5% | 每周日定时任务执行成功率 |
| AI 建议采纳率 | >= 60% | 审批通过次数 / 总建议次数 |
| 调优后胜率变化 | 正收益概率 > 50% | 调优周 vs 前一周的胜率差值 |
| 审批响应率（仅人工审批模式） | >= 80% | 48h 内完成审批的比例 |
| Token 成本 | 单次调优 < 5000 tokens | API 调用日志统计 |

### 1.5 第一期范围

| 维度 | 第一期覆盖 | 后续扩展 |
|------|-----------|----------|
| 策略 | MTPCS策略（btc_eth）+ 新币做空策略（new_coin）+ HRS反转策略（hrs） | 网格交易、其他策略 |
| 调优频率 | 仅周度调优 | 月度调优（可选） |
| 审批方式 | 飞书卡片交互确认 | Web UI 审批面板 |
| 部署 | 独立 Docker 容器 | 与主系统容器编排统一 |

---

## 2. 系统架构概览

### 2.1 五层闭环架构

```
采集层 (adapters) → 记忆层 (memory) → 决策层 (engine) → 审批层 (notifier) → 执行层 (deploy)
     ↑                                                                              ↓
     └──────────────────────── 反馈闭环（下周数据验证）──────────────────────────────┘
```

每一层职责：

| 层级 | 模块 | 职责 |
|------|------|------|
| 采集层 | `adapters/` | 适配不同策略，输出标准化 `StrategyReport` |
| 记忆层 | `memory/` | 读写 `strategy_memory` 表，构建调优历史上下文 |
| 决策层 | `engine/` | 调用 DeepSeek-v4-pro，解析校验 AI 输出 |
| 审批层 | `notifier/` | 推送飞书变更卡片，处理审批交互 |
| 执行层 | `deploy/` | 备份配置、应用变更、自动回滚 |

### 2.2 部署架构

```
┌─────────────────────────────────────────────────────────────┐
│                     StratTuneAI 容器                          │
│                                                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────────┐ │
│  │ 定时调度器 │  │ 数据适配器│  │ AI引擎   │  │ 配置管理器  │ │
│  │ scheduler│→│ adapters │→│ engine   │→│ deploy      │ │
│  │ 每周日    │  │ mtpcs    │  │ DeepSeek │  │ diff+apply  │ │
│  │ 23:55    │  │ new_coin │  │ v4-pro   │  │ +rollback   │ │
│  └──────────┘  └──────────┘  └──────────┘  └──────┬──────┘ │
│                                                    │         │
│  ┌──────────┐  ┌──────────┐                       │         │
│  │ 记忆库    │  │ 通知模块  │←──── 飞书审批 ───────┘         │
│  │ memory   │  │ notifier │                                │
│  └────┬─────┘  └──────────┘                                │
│       │                                                      │
└───────┼──────────────────────────────────────────────────────┘
        │
   ┌────▼─────┐
   │PostgreSQL│  ← trading.strategy_memory 表
   └──────────┘
```

### 2.3 技术选型

| 模块 | 技术选型 | 说明 |
|------|----------|------|
| 后端语言 | Python 3.10+ | 与主项目一致 |
| 定时调度 | APScheduler 3.x | 单容器轻量级调度，无需 Celery |
| AI 接口 | DeepSeek-v4-pro (deepseek-v4-pro)，通过 OpenAI SDK 调用，启用思考模式 (thinking_mode: enabled, reasoning_effort: high) | 兼容 OpenAI 接口协议 |
| 数据库 | 复用现有 PostgreSQL | trading schema，新建 strategy_memory 表 |
| 通知 | 复用飞书通知服务 (shared/notification.py) | 新增调优专用 Webhook 环境变量 |
| 配置管理 | PyYAML + Pydantic | 读写策略 config.yaml，数据校验 |
| 容器化 | Docker | 独立 Dockerfile，与主系统解耦 |

---

## 3. 功能需求

### 3.1 模块总览

| 模块编号 | 模块名称 | 优先级 | 简述 |
|----------|----------|--------|------|
| M1 | 定时调度模块 | 必须 | 每周日 23:55 触发调优流程 |
| M2 | 数据适配器模块 | 必须 | 采集策略周度数据，输出标准化报告 |
| M3 | 记忆管理模块 | 必须 | 读写 strategy_memory，构建上下文 |
| M4 | AI 决策引擎 | 必须 | 调用 DeepSeek，解析校验输出 |
| M5 | 通知与审批模块 | 必须 | 飞书卡片推送，关键词审批 |
| M6 | 配置管理模块 | 必须 | 备份、diff、应用、回滚配置文件 |
| M7 | 监控与告警模块 | 应该 | 成本追踪、异常告警、效果追踪 |

---

### 3.2 M1：定时调度模块

#### 3.2.1 功能描述

使用 APScheduler 在独立容器内运行定时任务，每周日 23:55（北京时间）触发全量调优流程。

#### 3.2.2 业务规则

| 规则编号 | 规则描述 |
|----------|----------|
| SCH-001 | 触发时间：每周日 23:55 (Asia/Shanghai)，使用 Cron 表达式 `55 23 * * 0` |
| SCH-002 | 触发后遍历所有已注册的策略适配器，逐个执行调优流程 |
| SCH-003 | 策略间串行执行（避免并发 API 调用导致限流），一个策略失败不影响后续策略 |
| SCH-004 | 支持手动触发（通过环境变量或 API 调用），用于调试和紧急调优 |
| SCH-005 | 每次执行完成后，将执行摘要（成功/失败策略数、耗时）写入日志 |
| SCH-006 | 容器启动时立即执行一次调度器健康检查，确保 Cron 任务已注册 |

#### 3.2.3 异常处理

| 异常场景 | 处理方式 |
|----------|----------|
| 调度器进程崩溃 | Docker 容器自动重启，APScheduler 重新注册任务 |
| 单个策略采集失败 | 记录错误日志，跳过该策略，继续处理下一个 |
| 全部策略采集失败 | 发送告警通知，终止本次调优 |
| 服务器时区不正确 | 启动时校验时区为 Asia/Shanghai，不匹配则告警并退出 |

#### 3.2.4 验收标准

- [ ] 每周日 23:55 准时触发，误差不超过 60 秒
- [ ] 手动触发功能正常（通过 `MANUAL_TRIGGER=true` 环境变量或 /trigger API）
- [ ] 一个策略失败时，其他策略正常执行
- [ ] 容器重启后调度器自动恢复

---

### 3.3 M2：数据适配器模块

#### 3.3.1 功能描述

为每个策略实现独立的数据适配器 (`DataAdapter`)，负责从数据库和外部 API 采集该策略过去一周（周一到周日）的交易数据，输出统一格式的 `StrategyReport` 对象。

#### 3.3.2 适配器接口规范

所有适配器必须实现 `BaseAdapter` 抽象类：

```python
class BaseAdapter(ABC):
    strategy_id: str          # 策略唯一标识，如 "btc_eth"、"new_coin"
    strategy_name: str        # 策略显示名称，如 "MTPCS趋势策略"
    config_path: str          # 策略 config.yaml 路径

    @abstractmethod
    async def collect(self) -> StrategyReport:
        """采集过去一周的策略数据，返回标准化报告"""
        ...

    @abstractmethod
    def get_param_whitelist(self) -> dict:
        """返回该策略的参数白名单定义"""
        ...

    @abstractmethod
    def get_redline_params(self) -> list[str]:
        """返回该策略的红线参数列表（AI 不可触碰）"""
        ...
```

#### 3.3.3 StrategyReport 数据结构

```python
class StrategyReport(BaseModel):
    meta: StrategyMeta               # 策略元信息
    performance: PerformanceMetrics  # 绩效指标
    risk: RiskMetrics                 # 风险指标
    distribution: DistributionMetrics # 分布指标
    anomalies: list[str]             # 异常事件列表

class StrategyMeta(BaseModel):
    strategy_id: str                 # 策略标识
    version: str                     # 策略版本号
    running_days: int                # 累计运行天数
    week_start: str                  # 周报起始日期 YYYY-MM-DD
    week_end: str                    # 周报结束日期 YYYY-MM-DD

class PerformanceMetrics(BaseModel):
    order_count: int                 # 本周委托笔数
    fill_count: int                  # 本周成交笔数
    wins: int                        # 盈利笔数
    losses: int                      # 亏损笔数
    win_rate: float                  # 胜率（百分比）
    total_pnl: float                 # 总盈亏（USDT）
    avg_win: float                   # 平均盈利（USDT）
    avg_loss: float                  # 平均亏损（USDT）
    profit_factor: float             # 盈亏比（总盈利/总亏损）
    sharpe_approx: float             # 夏普近似值（如果可计算）

class RiskMetrics(BaseModel):
    max_consecutive_losses: int      # 本周最大连续亏损
    current_drawdown_pct: float      # 当前回撤百分比
    is_paused: bool                  # 是否处于熔断/暂停状态
    pause_reason: str                # 暂停原因（如有）
    daily_loss_limit_hit: bool       # 是否触发日亏损限额

class DistributionMetrics(BaseModel):
    signal_grade_dist: dict          # 信号等级分布 {"S": n, "A": n, "B": n, "C": n}
    holding_hours_avg: float         # 平均持仓时长（小时）
    holding_hours_max: float         # 最大持仓时长（小时）
    symbol_trade_dist: dict          # 各币种交易笔数分布
```

#### 3.3.4 MTPCS 适配器 (mtpcs_adapter.py)

**数据采集来源**：

| 数据项 | 来源 | 方式 |
|--------|------|------|
| 订单/成交数据 | `trading.trade_records` 表 | SQL 查询 strategy='MTPCS策略' |
| 盈亏数据 | Binance income API (`REALIZED_PNL`) | 复用 WeeklyReportCollector 逻辑 |
| 当前配置 | `strategies/btc_eth/config.yaml` | 直接读取 YAML 文件 |
| 信号分布 | 策略内部日志或状态表 | 读取策略状态持久化数据 |

**采集逻辑**：

1. 确定时间范围：上周一 00:00 至上周日 23:59（北京时间）
2. 从 `trade_records` 查询该策略的订单和成交数据
3. 从 Binance API 获取已实现盈亏流水
4. 读取当前 config.yaml 获取生效中的参数
5. 从策略状态表获取暂停/熔断状态
6. 组装为 `StrategyReport` 对象返回

#### 3.3.5 新币做空适配器 (new_coin_adapter.py)

**数据采集来源**：

| 数据项 | 来源 | 方式 |
|--------|------|------|
| 订单/成交数据 | `trading.trade_records` 表 | SQL 查询 strategy='新币做空策略' |
| 盈亏数据 | Binance income API | 复用 WeeklyReportCollector 逻辑 |
| 当前配置 | `strategies/new_coin/config.yaml` | 直接读取 YAML 文件 |
| 持仓分布 | 策略内部状态 | 读取最大持仓数、保证金使用情况 |

#### 3.3.6 业务规则

| 规则编号 | 规则描述 |
|----------|----------|
| ADP-001 | 适配器数据采集失败时，返回携带 error 字段的 StrategyReport，不中断流程 |
| ADP-002 | 本周无交易的策略，生成空报告（所有计数为 0），AI 应建议"维持不变" |
| ADP-003 | 数据采集超时设置为 60 秒，超时后降级为部分数据报告 |
| ADP-004 | 独立采集——在 ai_tuner 的 adapters 中独立实现，直接从 `trading.trade_records` 和 Binance income API 获取 |

#### 3.3.7 验收标准

- [ ] MTPCS 适配器正确采集 BTCUSDT、ETHUSDT、BNBUSDT、SOLUSDT、XRPUSDT、TRXUSDT 的交易数据
- [ ] 新币做空适配器正确采集动态交易对的交易数据
- [ ] 报告中的时间范围精确对应上周一至上周日
- [ ] 数据采集失败时返回带 error 字段的报告，不抛出异常
- [ ] 本周无交易时返回空报告（wins=0, losses=0, win_rate=0）

---

### 3.4 M3：记忆管理模块

#### 3.4.1 功能描述

管理 `trading.strategy_memory` 表的读写，负责存储每次调优的完整记录，并在每次 AI 调用前构建历史调优上下文。

#### 3.4.2 数据库表设计

```sql
CREATE TABLE IF NOT EXISTS trading.strategy_memory (
    id              SERIAL PRIMARY KEY,
    strategy_id     VARCHAR(32) NOT NULL,          -- 策略标识（btc_eth / new_coin）
    version         VARCHAR(20),                   -- 当时的策略版本号
    summary         TEXT,                          -- AI 生成的50字核心摘要
    full_report     JSONB,                         -- 完整 StrategyReport
    ai_suggestions  JSONB,                         -- AI 输出的调优建议（原始 JSON）
    is_applied      BOOLEAN DEFAULT FALSE,         -- 是否已确认生效
    approved_by     VARCHAR(64),                   -- 审批人
    approved_at     TIMESTAMP,                     -- 审批时间
    created_at      TIMESTAMP DEFAULT NOW()        -- 创建时间
);

-- 索引
CREATE INDEX idx_memory_strategy_time 
    ON trading.strategy_memory(strategy_id, created_at DESC);
```

**字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `strategy_id` | VARCHAR(32) | 策略标识，与适配器中的 strategy_id 一致 |
| `version` | VARCHAR(20) | 当时的策略版本号，如 "2.0.0" |
| `summary` | TEXT | AI 生成的 50 字核心摘要，用于构建滑动窗口上下文 |
| `full_report` | JSONB | 完整的 StrategyReport JSON，备查 |
| `ai_suggestions` | JSONB | AI 原始输出的 JSON 建议，含 reasons/ adjustments/ expected_impact/ confidence |
| `is_applied` | BOOLEAN | 是否已人工确认并生效 |
| `approved_by` | VARCHAR(64) | 审批人标识（飞书用户 ID 或名称） |
| `approved_at` | TIMESTAMP | 审批确认时间 |
| `created_at` | TIMESTAMP | 记录创建时间 |

#### 3.4.3 上下文构建策略

采用**滑动窗口**策略，每次调用 AI 前：

1. 查询该 strategy_id 的最近 **3 条**已生效 (`is_applied=true`) 的记录
2. 仅提取 `summary` 和 `created_at` 字段
3. 拼接为以下格式文本：

```
【历史调优记录】
2026-06-14: 降低入场阈值从6.5→7.0，胜率从38%回升至45%
2026-06-07: 调整止损ATR倍数从2.5→2.0，减少过早止损
2026-05-31: 维持不变，策略表现稳定
```

#### 3.4.4 业务规则

| 规则编号 | 规则描述 |
|----------|----------|
| MEM-001 | 每条记录按 strategy_id 严格隔离，不同策略的记忆互不可见 |
| MEM-002 | 滑动窗口大小 N=3，可通过配置调整 |
| MEM-003 | 超过 3 个月的历史记录标记为归档，不参与滑动窗口（但保留在表中） |
| MEM-004 | 无论 AI 建议是否被采纳，每次调优都写入一条记录（is_applied 标记区分） |
| MEM-005 | summary 字段最大 200 字符，超出截断 |

#### 3.4.5 验收标准

- [ ] 表创建成功，索引生效
- [ ] 不同策略的记忆隔离正确（查询 btc_eth 不会返回 new_coin 的记录）
- [ ] 滑动窗口正确返回最近 3 条已生效记录
- [ ] 归档逻辑正确（超过 3 个月的记录不参与滑动窗口）
- [ ] 写入失败时不影响主流程，仅记录错误日志

---

### 3.5 M4：AI 决策引擎

#### 3.5.1 功能描述

封装 DeepSeek-v4-pro API 调用，加载策略专属的 Prompt 模板，将标准化报告和历史记忆发送给 AI，接收并解析 JSON 格式的调优建议，进行安全校验。

#### 3.5.2 LLM 客户端 (llm_client.py)

**技术实现**：

- 使用 OpenAI Python SDK（兼容 DeepSeek API）
- 模型：`deepseek-v4-pro`
- 调用方式：`chat.completions.create`，使用 System Prompt + User Prompt 双消息结构
- 思考模式：启用 (`thinking_mode: enabled`)，推理强度设为 `high` (`reasoning_effort: high`)，确保 AI 输出的调优建议经过充分推理

**配置项**：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_base` | `https://api.deepseek.com` | API 地址 |
| `model` | `deepseek-v4-pro` | 模型名称 |
| `temperature` | `0.3` | 温度参数（低温度保证输出稳定） |
| `max_tokens` | `4096` | 最大输出 Token |
| `timeout` | `60` | 请求超时（秒） |
| `max_retries` | `3` | 失败重试次数 |
| `thinking_mode` | `enabled` | 思考模式（启用后可进行深层推理） |
| `reasoning_effort` | `high` | 推理强度（low/medium/high） |

#### 3.5.3 Prompt 模板设计

**模板文件结构**：

```
prompts/
├── common_rules.txt          # 通用规则（所有策略共享）
├── mtpcs_system.txt          # MTPCS 策略 System Prompt
├── mtpcs_user.txt            # MTPCS 策略 User Prompt 模板
├── new_coin_system.txt       # 新币做空策略 System Prompt
└── new_coin_user.txt         # 新币做空策略 User Prompt 模板
```

**通用规则 (common_rules.txt)**：

```
你是量化策略参数调优专家。你必须遵守以下规则：

1. 只能调整白名单中的参数，严禁触碰红线参数。
2. 若本周策略表现良好（胜率≥50%且无异常），优先建议"维持不变"。
3. 严禁为了调参而调参，没有充分理由时不要建议任何调整。
4. 每次调整的参数数量不超过 3 个。
5. 调整幅度应渐进，单次调整不超过当前值的 ±20%。
6. 输出必须为严格 JSON 格式，不得包含任何其他文本。
7. 如果建议调整，必须给出清晰的调整理由和预期影响。
```

**System Prompt 示例 (mtpcs_system.txt)**：

```
你是 MTPCS（主流币种趋势回调确认策略）的参数调优专家。

该策略的核心逻辑：
- 基于多时间框架（1h/4h/1d）的趋势强度、形态质量和动量背离进行评分
- 评分分为 S/A/B/C 四个等级，不同等级对应不同杠杆和仓位
- 采用分批止盈（TP1/TP2）+ 吊灯止损 + 时间止损三重风控
- 严格的频率控制（每日最大交易数、品种冷却期、连续亏损暂停）

白名单参数（你可以调整）：
- scoring.min_score: 最低评分阈值（当前范围 60-90）
- scoring.weights.*: 评分维度权重（各维度权重之和必须为 1.0）
- risk.stop_loss_atr_multiplier: 止损 ATR 倍数（范围 1.5-3.0）
- risk.partial_take_profit.tp1_atr_multiplier: TP1 ATR 倍数（范围 1.5-4.0）
- risk.partial_take_profit.tp2_atr_multiplier: TP2 ATR 倍数（范围 2.5-6.0）
- risk.partial_take_profit.tp1_close_ratio: TP1 平仓比例（范围 0.15-0.40）
- risk.partial_take_profit.tp2_close_ratio: TP2 平仓比例（范围 0.15-0.40）
- risk.chandelier_stop.activation_atr: 吊灯止损激活阈值（范围 1.2-2.5）
- risk.chandelier_stop.trailing_atr: 吊灯止损跟踪阈值（范围 0.8-2.0）
- risk.time_stop.max_holding_hours: 最大持仓时间（范围 24-120）
- risk.frequency_control.max_daily_total_trades: 每日最大总交易数（范围 2-8）
- risk.frequency_control.max_daily_symbol_trades: 单品种每日最大交易数（范围 1-4）
- risk.frequency_control.symbol_cooldown_hours: 品种冷却期（范围 4-24）
- risk.frequency_control.consecutive_loss_pause: 连续亏损暂停阈值（范围 3-8）
- risk.frequency_control.pause_duration_hours: 暂停时长（范围 6-48）
- risk.frequency_control.max_daily_loss_usdt: 每日最大亏损限额（范围 15-50）
- binance.leverage.*: 各等级杠杆（范围 S:3-10, A:2-8, B:2-6, C:1-4）
- binance.position_ratio.*: 各等级仓位比例（各等级比例之和建议不超过 1.0）

红线参数（严禁调整）：
- strategy.symbols: 交易对列表
- strategy.timeframes: 时间框架
- risk.max_position_size: 最大仓位比例
- risk.position_sizing.*: 动态仓位计算参数
- binance.order_optimization.*: 限价单优化参数
- 任何不在白名单中的参数

请根据当前策略表现，返回 JSON 格式的调优建议。
```

**User Prompt 模板 (mtpcs_user.txt)**：

```
## 当前策略配置
{{ current_config }}

## 本周体检报告
{{ report_json }}

## 历史调优记忆
{{ memory_context }}

请分析以上数据，给出参数调整建议。严格按以下 JSON 格式输出：

{
  "reasons": "详细分析本周策略表现的原因...",
  "summary": "一句话总结调优建议（50字以内）",
  "adjustments": {
    "参数路径": {"from": 旧值, "to": 新值}
  },
  "expected_impact": "预期调优后的效果",
  "confidence": 0.75
}

注意：
- adjustments 为空对象 {} 表示建议维持不变
- confidence 范围 0.0-1.0，表示你对建议的确信度
- 如果本周胜率≥50%且无明显异常，强烈建议 adjustments 为 {}
```

#### 3.5.4 响应解析器 (response_parser.py)

**解析流程**：

```
AI 原始输出 → 提取 JSON → Pydantic 校验 → 参数白名单校验 → 参数范围校验 → 通过/拒绝
```

**校验规则**：

| 校验步骤 | 规则 | 失败处理 |
|----------|------|----------|
| JSON 格式校验 | 必须是合法 JSON，包含 `reasons`、`summary`、`adjustments`、`expected_impact`、`confidence` 五个字段 | 丢弃 + 告警通知 |
| 参数白名单校验 | `adjustments` 中的每个参数路径必须在白名单中 | 拒绝该参数 + 告警 |
| 参数范围校验 | 新值必须在预设的合理范围内 | 截断到最近边界 + 告警 |
| 空建议检测 | `adjustments` 为空对象 `{}` 视为"建议维持不变" | 正常记录 |
| confidence 校验 | 必须在 0.0-1.0 之间 | 截断 |

#### 3.5.5 成本追踪器 (cost_tracker.py)

| 功能 | 说明 |
|------|------|
| Token 用量统计 | 每次 API 调用记录 prompt_tokens + completion_tokens |
| 费用估算 | 根据 DeepSeek 定价估算费用（deepseek-v4-pro 模型：输入（缓存未命中）$1.74/百万Token，输入（缓存命中）$0.174/百万Token，输出 $3.48/百万Token） |
| 月度汇总 | 每月生成各策略的 Token 用量和费用报告 |
| 超限告警 | 单次调用超过 10000 tokens 时告警 |

#### 3.5.6 业务规则

| 规则编号 | 规则描述 |
|----------|----------|
| ENG-001 | API 调用失败最多重试 3 次，间隔 5s/10s/20s 递增 |
| ENG-002 | 3 次重试均失败，记录错误并跳过该策略，发送告警 |
| ENG-003 | 解析失败或校验不通过，丢弃 AI 输出，不写入 memory 表 |
| ENG-004 | JSON 解析使用正则提取（兼容 AI 偶尔在 JSON 前后加说明文字的情况） |
| ENG-005 | 每次调用的完整 Prompt 和 Response 记录到日志文件（用于审计） |

#### 3.5.7 验收标准

- [ ] API 调用成功返回 JSON 格式建议
- [ ] 白名单外的参数被正确拒绝
- [ ] 超出范围的参数值被正确截断
- [ ] 空建议（维持不变）被正确识别和处理
- [ ] 重试机制正常（模拟 API 失败场景）
- [ ] Token 用量和费用统计准确
- [ ] Prompt 模板正确加载和渲染

---

### 3.6 M5：通知与审批模块

#### 3.6.1 功能描述

通过飞书卡片消息推送 AI 调优建议，支持人工通过回复关键词进行审批，也支持自动生效模式。

**人工审批模式**（默认）：审批通过后触发配置生效流程，审批拒绝后仅记录不生效。

**自动审批模式（auto-apply）**：在 `config.yaml` 中将 `approval.auto_apply.enabled` 设置为 `true` 后，AI 调优建议在推送飞书通知的同时自动写入覆盖层并生效，无需人工介入。此模式为 opt-in 模式，默认关闭。

#### 3.6.2 飞书卡片消息格式

推送使用飞书**交互式卡片消息**（`interactive` 类型），卡片结构如下：

```
┌─────────────────────────────────────┐
│  🤖 StratTuneAI 周度调优建议         │
│                                     │
│  策略：MTPCS趋势策略 (btc_eth)       │
│  时间：2026-06-21 (第25周)          │
│  ────────────────────────────────   │
│                                     │
│  📊 本周表现                        │
│  胜率：42.3%  |  盈亏：-15.2 USDT  │
│  交易：12笔  |  最大连亏：4笔       │
│                                     │
│  💡 AI 分析                         │
│  本周胜率下降至42.3%，主要原因是    │
│  止损设置过紧导致3笔交易被过早止损...│
│                                     │
│  📝 建议调整                        │
│  ┌─────────────────────────────┐   │
│  │ 参数              当前→建议  │   │
│  │ stop_loss_atr     2.0→2.3  │   │
│  │ chandelier_act    1.8→2.0  │   │
│  └─────────────────────────────┘   │
│                                     │
│  预期影响：预计胜率回升至45%+       │
│  置信度：75%                        │
│                                     │
│  ────────────────────────────────   │
│  请回复：                           │
│  /confirm btc_eth 2026-06-21 确认  │
│  /reject btc_eth 2026-06-21 拒绝   │
│  ⏰ 超时：48小时内未回复自动丢弃     │
└─────────────────────────────────────┘
```

#### 3.6.3 审批交互流程

```
飞书卡片推送 → 管理员回复关键词 → Webhook 回调接收 → 解析关键词 → 执行/丢弃
```

**关键词格式**：

| 操作 | 关键词格式 | 说明 |
|------|-----------|------|
| 确认 | `/confirm {strategy_id} {date}` | 确认应用该策略在指定日期的调优建议 |
| 拒绝 | `/reject {strategy_id} {date}` | 拒绝该建议，不生效 |

示例：`/confirm btc_eth 2026-06-21`

**审批接收方式**：

由于飞书机器人 Webhook 回调需要配置飞书应用的事件订阅，第一期采用简化方案：
- 在飞书卡片中提供操作按钮，点击后发送 POST 请求到 StratTuneAI 容器的回调接口
- 同时支持关键词回复（如果配置了飞书应用事件订阅）

如无法实现交互式卡片，降级为文本消息 + 关键词回复：
- 推送文本消息，包含完整的分析、建议和关键词
- 管理员在群里回复 `/confirm` 或 `/reject`
- 通过飞书消息 API 轮询或事件订阅获取回复

**第一期推荐方案**：HTTP 回调端点——在 ai_tuner 容器中启动 HTTP 服务，通过 `POST /api/v1/approval` 接收飞书卡片按钮回调

#### 3.6.4 超时与过期处理

| 规则编号 | 规则描述 |
|----------|----------|
| APV-001 | 审批超时时间：48 小时（从卡片推送时间起算） |
| APV-002 | 超时未回复，自动标记为"过期未处理"，记录到 memory 表 |
| APV-003 | 超时后发送提醒通知："调优建议已过期，下周将重新生成" |
| APV-004 | 同一条建议不可重复确认（幂等性保障） |

#### 3.6.5 通知与审批配置

| 配置项 | 类型 | 说明 |
|--------|------|------|
| `FEISHU_WEBHOOK_TUNER` | 环境变量 | StratTuneAI 调优专用 Webhook URL |
| `FEISHU_TUNER_VERIFY_TOKEN` | 环境变量 | 飞书事件订阅的 Verification Token（如启用） |
| `approval.timeout_hours` | config.yaml | 审批超时时间（小时），默认 48 |
| `approval.feishu_webhook_env` | config.yaml | 飞书调优专用 Webhook 环境变量名 |
| `approval.auto_apply.enabled` | config.yaml | 是否启用自动应用模式，默认 `false`（opt-in 模式，需显式设置为 `true` 才启用） |

#### 3.6.6 业务规则

| 规则编号 | 规则描述 |
|----------|----------|
| NTF-001 | 通知发送失败重试 3 次，间隔 5s |
| NTF-002 | 3 次重试均失败，记录错误日志，不阻塞后续策略 |
| NTF-003 | 审批确认后，同步更新 memory 表的 is_applied、approved_by、approved_at 字段 |
| NTF-004 | 审批拒绝后，记录 is_applied=false，不修改配置 |
| NTF-005 | 配置生效后，发送"已生效"通知到对应策略的飞书群 |
| NTF-006 | 自动应用模式（auto-apply）下，配置写入覆盖层成功后发送"调优已自动应用"通知；写入失败时发送错误通知，不阻塞后续策略调优 |

#### 3.6.7 验收标准

- [ ] 飞书卡片/文本消息正确推送，格式符合预期
- [ ] 关键词回复正确解析（/confirm 和 /reject）
- [ ] 确认后正确触发配置生效流程
- [ ] 拒绝后仅记录不生效
- [ ] 超时 48h 后自动过期
- [ ] 重复确认被幂等拦截
- [ ] auto-apply 启用时（`approval.auto_apply.enabled=true`），调优建议自动写入覆盖层并发送"调优已自动应用"通知
- [ ] auto-apply 写入覆盖层失败时，发送错误通知但不中断整体流程
- [ ] auto-apply 禁用时（`approval.auto_apply.enabled=false`），走标准人工审批流程

---

### 3.7 M6：配置管理模块

#### 3.7.1 功能描述

负责将 AI 建议的 `adjustments` 安全地应用到策略的 `config.yaml` 文件中，包括备份、差异生成、原子写入、自动回滚。

#### 3.7.2 配置操作器 (config_operator.py)

**核心功能**：

| 功能 | 说明 |
|------|------|
| 读取配置 | 读取策略 config.yaml，解析为嵌套字典 |
| 写入配置（非 AI 调优） | `apply_changes()` 直接写入 config.yaml，用于资金分配等非 AI 调优场景 |
| 写入覆盖层（AI 调优） | `apply_overrides()` 写入 `tuning_overrides/` 目录，不修改 config.yaml |
| 备份配置 | 应用前备份为 `config.yaml.backup.{timestamp}`（仅 apply_changes 场景） |
| 差异生成 | 对比新旧配置，生成人类可读的变更清单 |

**AI 调优覆盖层写入流程（apply_overrides）**：

1. 生成版本号 `V{YYYYMMDD}`（同一天多次调用追加后缀）
2. 从 config_path 推导策略目录和覆盖层目录
3. 将扁平参数路径转为嵌套字典结构
4. 原子写入 `tuning_overrides/V{version}.yaml`
5. 原子写入 `.active` 指向新版本

**非 AI 调优写入流程（apply_changes）**：

1. 读取当前 config.yaml
2. 生成备份文件 `config.yaml.backup.{timestamp}`
3. 在内存中修改参数值
4. 写入临时文件 `config.yaml.tmp`
5. 原子性重命名 `config.yaml.tmp` → `config.yaml`
6. 验证写入（重新读取确认参数已生效）

#### 3.7.3 差异生成器 (diff_generator.py)

**输出格式**：

```
【配置变更清单】MTPCS趋势策略 (btc_eth)

参数路径                              当前值    新值     变化
─────────────────────────────────────────────────────────────
risk.stop_loss_atr_multiplier         2.0      2.3      +15%
risk.chandelier_stop.activation_atr   1.8      2.0      +11.1%
─────────────────────────────────────────────────────────────
变更数量：2 项
生成时间：2026-06-21 23:58
```

#### 3.7.4 回滚管理器 (rollback_manager.py)

**自动回滚触发器**：

应用新参数后的 24 小时内，监控策略表现，触发以下任一条件则自动回滚：

| 触发条件 | 阈值 | 说明 |
|----------|------|------|
| 连续亏损 | >= 3 笔 | 连续 3 笔交易亏损 |
| 累计亏损 | > 初始资金的 2% | 24h 内总亏损超过 2% |

**回滚流程（AI 调优覆盖层）**：

AI 调优参数写入 `tuning_overrides/` 覆盖层，回滚通过修改 `.active` 文件指向旧版本实现，无需恢复备份文件：

1. 读取当前 `.active` 获取版本号
2. 从历史版本文件中选择目标版本
3. 原子写入 `.active` 指向旧版本
4. 更新 memory 表状态
5. 发送"回滚完成"通知

**回滚流程（非 AI 调优，直接写入 config.yaml 的场景）**：

1. 检测到触发条件 → 发送"紧急回滚"告警
2. 恢复最近一次备份 `config.yaml.backup.{timestamp}` → `config.yaml`
3. 发送"回滚完成"通知
4. 记录回滚事件到 memory 表

**手动回滚**：

支持通过 API 手动触发回滚到任意历史备份。

#### 3.7.5 业务规则

| 规则编号 | 规则描述 |
|----------|----------|
| DEP-001 | 每次应用前必须备份，备份文件保留最近 10 个 |
| DEP-002 | 配置写入使用原子性重命名，防止写入中断导致配置损坏 |
| DEP-003 | 回滚触发后，发送"紧急回滚"通知到飞书，阻止后续手动误操作 |
| DEP-004 | 回滚后，该策略的本次调优记录标记为 `is_applied=false` |
| DEP-005 | 自动回滚监控在容器重启后失效（通过检查 memory 表恢复监控状态） |

#### 3.7.6 验收标准

- [ ] 配置备份正确生成，文件名格式正确
- [ ] 差异生成器输出格式正确
- [ ] 原子性写入不损坏配置（模拟写入中断场景）
- [ ] 自动回滚触发条件正确检测
- [ ] 回滚后配置恢复为上一个版本
- [ ] 手动回滚功能正常

---

### 3.8 M7：监控与告警模块

#### 3.8.1 功能描述

监控系统运行状态，追踪 AI 调优效果，异常时及时告警。

#### 3.8.2 监控指标

| 指标 | 采集方式 | 告警阈值 |
|------|----------|----------|
| 定时任务执行状态 | 日志 | 连续 2 次失败 |
| API 调用成功率 | 日志 | < 95% |
| API 调用延迟 | 日志 | P95 > 10s |
| Token 用量 | cost_tracker | 单次 > 10000 tokens |
| 审批超时率 | memory 表 | > 50% |
| 自动回滚次数 | memory 表 | > 1 次/月 |

#### 3.8.3 效果追踪

每次周报生成时，自动比对调优前后的绩效差异：

- 胜率变化
- 总盈亏变化
- 最大回撤变化
- 交易频率变化

追踪结果记录到 memory 表，供后续调优参考。

#### 3.8.4 验收标准

- [ ] 定时任务执行状态监控正常
- [ ] API 调用异常时告警通知正常
- [ ] 效果追踪数据正确

---

## 4. 参数白名单定义

### 4.1 MTPCS 策略 (btc_eth) 参数白名单

#### 4.1.1 可调参数（白名单）

| 参数路径 | 当前值 | 允许范围 | 步长 | 说明 |
|----------|--------|----------|------|------|
| `scoring.min_score` | 75 | [60, 90] | 5 | 最低评分阈值 |
| `scoring.weights.trend_strength` | 0.40 | [0.20, 0.60] | 0.05 | 趋势强度权重 |
| `scoring.weights.pattern_quality` | 0.35 | [0.20, 0.50] | 0.05 | 形态质量权重 |
| `scoring.weights.momentum_divergence` | 0.25 | [0.10, 0.40] | 0.05 | 动量背离权重 |
| `risk.stop_loss_atr_multiplier` | 2.0 | [1.5, 3.0] | 0.1 | 止损 ATR 倍数 |
| `risk.partial_take_profit.tp1_atr_multiplier` | 2.5 | [1.5, 4.0] | 0.1 | TP1 ATR 倍数 |
| `risk.partial_take_profit.tp2_atr_multiplier` | 4.0 | [2.5, 6.0] | 0.1 | TP2 ATR 倍数 |
| `risk.partial_take_profit.tp1_close_ratio` | 0.25 | [0.15, 0.40] | 0.05 | TP1 平仓比例 |
| `risk.partial_take_profit.tp2_close_ratio` | 0.25 | [0.15, 0.40] | 0.05 | TP2 平仓比例 |
| `risk.chandelier_stop.activation_atr` | 1.8 | [1.2, 2.5] | 0.1 | 吊灯止损激活阈值 |
| `risk.chandelier_stop.trailing_atr` | 1.2 | [0.8, 2.0] | 0.1 | 吊灯止损跟踪阈值 |
| `risk.time_stop.max_holding_hours` | 72 | [24, 120] | 12 | 最大持仓时间 |
| `risk.frequency_control.max_daily_total_trades` | 4 | [2, 8] | 1 | 每日最大总交易数 |
| `risk.frequency_control.max_daily_symbol_trades` | 2 | [1, 4] | 1 | 单品种每日最大交易数 |
| `risk.frequency_control.symbol_cooldown_hours` | 12 | [4, 24] | 4 | 品种冷却期 |
| `risk.frequency_control.consecutive_loss_pause` | 5 | [3, 8] | 1 | 连续亏损暂停阈值 |
| `risk.frequency_control.pause_duration_hours` | 24 | [6, 48] | 6 | 暂停时长 |
| `risk.frequency_control.max_daily_loss_usdt` | 25 | [15, 50] | 5 | 每日最大亏损限额 |
| `binance.leverage.S` | 5 | [3, 10] | 1 | S 级杠杆 |
| `binance.leverage.A` | 4 | [2, 8] | 1 | A 级杠杆 |
| `binance.leverage.B` | 3 | [2, 6] | 1 | B 级杠杆 |
| `binance.leverage.C` | 2 | [1, 4] | 1 | C 级杠杆 |
| `binance.position_ratio.S` | 0.50 | [0.30, 0.70] | 0.05 | S 级仓位比例 |
| `binance.position_ratio.A` | 0.30 | [0.15, 0.50] | 0.05 | A 级仓位比例 |
| `binance.position_ratio.B` | 0.15 | [0.05, 0.30] | 0.05 | B 级仓位比例 |
| `binance.position_ratio.C` | 0.05 | [0.01, 0.15] | 0.02 | C 级仓位比例 |

**权重约束**：`scoring.weights.trend_strength + scoring.weights.pattern_quality + scoring.weights.momentum_divergence` 必须等于 `1.0`。AI 调整权重时，如果和不为 1.0，系统按比例归一化。

#### 4.1.2 红线参数（禁止调整）

| 参数路径 | 原因 |
|----------|------|
| `strategy.symbols` | 交易对列表由策略设计决定，不可动态调整 |
| `strategy.timeframes` | 时间框架与策略逻辑绑定，不可动态调整 |
| `strategy.schedule.*` | 调度频率不可动态调整 |
| `scoring.grade_thresholds.*` | 等级阈值与风险体系绑定，不可动态调整 |
| `risk.max_position_size` | 最大仓位限制不可动态调整 |
| `risk.position_sizing.*` | 动态仓位计算参数与账户资金安全绑定 |
| `risk.close_limit_order.*` | 限价单优化参数不可动态调整 |
| `risk.cleanup_silent_error_codes` | 错误处理参数不可调整 |
| `risk.partial_take_profit.remaining_ratio` | 剩余仓位比例由 TP1+TP2 推导 |
| `risk.time_stop.close_ratio` | 超时平仓比例不可调整 |
| `risk.frequency_control.initial_capital_usdt` | 初始资金不可调整 |
| `binance.order_optimization.*` | 限价单优化参数不可调整 |
| `notification.*` | 通知配置不可调整 |

### 4.2 新币做空策略 (new_coin) 参数白名单

#### 4.2.1 可调参数（白名单）

| 参数路径 | 当前值 | 允许范围 | 步长 | 说明 |
|----------|--------|----------|------|------|
| `scoring.entry_threshold` | 5.0 | [3.0, 8.0] | 0.5 | 入场评分阈值 |
| `scoring.weights.contract` | 0.45 | [0.30, 0.60] | 0.05 | 合约数据权重 |
| `scoring.weights.oi_volume_ratio` | 0.30 | [0.15, 0.45] | 0.05 | OI/交易量比率权重 |
| `scoring.weights.oi_rank` | 0.15 | [0.05, 0.25] | 0.05 | OI 排名权重 |
| `scoring.weights.technical` | 0.35 | [0.20, 0.50] | 0.05 | 技术面权重 |
| `scoring.weights.sentiment` | 0.20 | [0.10, 0.35] | 0.05 | 情绪面权重 |
| `scoring.oi_volume_ratio.thresholds.veto` | 0.5 | [0.4, 0.7] | 0.05 | 一票否决阈值 |
| `scoring.oi_volume_ratio.thresholds.danger` | 0.4 | [0.3, 0.6] | 0.05 | 极危险阈值 |
| `scoring.oi_volume_ratio.thresholds.caution` | 0.3 | [0.2, 0.5] | 0.05 | 偏高阈值 |
| `scoring.oi_volume_ratio.thresholds.good` | 0.2 | [0.1, 0.3] | 0.05 | 良好阈值 |
| `scoring.technical.min_total_score` | 6.0 | [4.0, 8.0] | 0.5 | 技术总分最低要求 |
| `scoring.technical.min_three_tops_score` | 2.0 | [1.0, 4.0] | 0.5 | 三次冲顶最低评分 |
| `trading.leverage` | 2 | [1, 3] | 1 | 杠杆倍数 |
| `trading.max_positions` | 3 | [2, 5] | 1 | 最大持仓数量 |
| `trading.single_position_margin` | 50 | [25, 100] | 10 | 单笔保证金（USDT） |
| `trading.stop_loss_percent` | 0.05 | [0.03, 0.08] | 0.01 | 止损百分比 |
| `trading.take_profit_percent` | 0.10 | [0.05, 0.15] | 0.01 | 止盈百分比 |
| `trading.batch_take_profit.target1_atr_multiplier` | 1.5 | [1.0, 3.0] | 0.1 | 第一目标 ATR 倍数 |
| `trading.batch_take_profit.target2_atr_multiplier` | 3.5 | [2.0, 5.0] | 0.1 | 第二目标 ATR 倍数 |
| `trading.consecutive_loss.max_consecutive_losses` | 3 | [2, 5] | 1 | 最大连续亏损次数 |
| `trading.consecutive_loss.pause_hours` | 48 | [24, 96] | 12 | 暂停时长 |
| `trading.max_drawdown.threshold` | 0.15 | [0.10, 0.25] | 0.05 | 最大回撤阈值 |
| `trading.max_drawdown.pause_days` | 7 | [3, 14] | 1 | 熔断暂停天数 |
| `trading.emergency_stop.trigger_percent` | 0.015 | [0.01, 0.03] | 0.005 | 紧急止损触发阈值 |
| `trading.risk_control.max_loss_percent` | 0.02 | [0.01, 0.05] | 0.005 | 单笔最大亏损比例 |
| `pattern.three_tops.max_deviation` | 0.02 | [0.01, 0.05] | 0.005 | 三次冲顶最大偏差 |

#### 4.2.2 红线参数（禁止调整）

| 参数路径 | 原因 |
|----------|------|
| `strategy.*` | 策略基本信息不可调整 |
| `scoring.veto_thresholds.*` | 否决阈值与策略安全绑定 |
| `scoring.oi_volume_ratio.scores.*` | 评分映射不可调整 |
| `scoring.sentiment.*` | 情绪面评分阈值不可调整 |
| `trading.batch_take_profit.target1_close_percent` | 平仓比例不可调整 |
| `trading.batch_take_profit.target2_close_percent` | 平仓比例不可调整 |
| `trading.batch_take_profit.trailing_stop_atr_multiplier` | 移动止盈参数不可调整 |
| `trading.time_stop.*` | 时间止损参数不可调整 |
| `trading.blacklist.*` | 黑名单参数不可调整 |
| `trading.emergency_stop.check_minutes` | 紧急止损检查周期不可调整 |
| `pattern.three_tops.score_high` | 形态评分映射不可调整 |
| `pattern.three_tops.score_medium` | 形态评分映射不可调整 |
| `pattern.long_upper_shadow.*` | 形态识别参数不可调整 |
| `pattern.volume_divergence.*` | 形态识别参数不可调整 |
| `pattern.adaptive.*` | 自适应检测参数不可调整 |
| `detector.*` | 新币检测参数不可调整 |
| `kline.*` | K线参数不可调整 |
| `notification.*` | 通知配置不可调整 |
| `database.*` | 数据库配置不可调整 |
| `logging.*` | 日志配置不可调整 |

---

## 5. 数据流与时序

### 5.1 完整调优流程

```
周日 23:55 (北京时间)
    │
    ▼
┌─────────────────────────────────────────────┐
│ 1. APScheduler 触发 weekly_job.py            │
└──────────────────────┬──────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────┐
│ 2. 遍历已注册策略列表 [btc_eth, new_coin]      │
└──────────────────────┬──────────────────────┘
                       │
           ┌───────────┴───────────┐
           ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 3a. MTPCS适配器   │    │ 3b. 新币做空适配器  │
│ collect() →      │    │ collect() →      │
│ StrategyReport   │    │ StrategyReport   │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 4a. 查询记忆库     │    │ 4b. 查询记忆库     │
│ 最近3条已生效记录  │    │ 最近3条已生效记录  │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 5a. 加载Prompt    │    │ 5b. 加载Prompt    │
│ mtpcs_system.txt │    │ new_coin_system  │
│ mtpcs_user.txt   │    │ new_coin_user    │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 6a. 调用 DeepSeek │    │ 6b. 调用 DeepSeek │
│ API → JSON       │    │ API → JSON       │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 7a. 解析+校验     │    │ 7b. 解析+校验     │
│ 白名单+范围检查   │    │ 白名单+范围检查   │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 8a. 写入 memory   │    │ 8b. 写入 memory   │
│ is_applied=False │    │ is_applied=False │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────┐    ┌──────────────────┐
│ 9a. 推送飞书卡片   │    │ 9b. 推送飞书卡片   │
│ 等待人工审批      │    │ 等待人工审批      │
└────────┬─────────┘    └────────┬─────────┘
         │                       │
         └───────────┬───────────┘
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
   ┌──────────────┐    ┌──────────────┐
   │ /confirm     │    │ /reject /    │
   │ → 应用变更   │    │ 超时 → 丢弃   │
   └──────┬───────┘    └──────────────┘
          │
          ▼
┌─────────────────────────────────────────────┐
│ 10. 备份 config.yaml → 写入新配置 → 验证     │
│ 11. 更新 memory (is_applied=true)            │
│ 12. 发送"已生效"通知                         │
│ 13. 启动 24h 自动回滚监控                    │
└─────────────────────────────────────────────┘
```

### 5.2 异常处理流程

| 环节 | 异常场景 | 处理方式 |
|------|----------|----------|
| 步骤 3 | 数据采集失败 | 生成带 error 的报告，AI 仍可分析（基于部分数据） |
| 步骤 3 | 全部策略采集失败 | 终止流程，发送告警 |
| 步骤 6 | API 调用失败 | 重试 3 次，均失败则跳过该策略 |
| 步骤 7 | JSON 解析失败 | 丢弃该建议，发送告警 |
| 步骤 7 | 参数校验失败 | 拒绝违规参数，发送告警 |
| 步骤 9 | 通知发送失败 | 重试 3 次，均失败则记录日志 |
| 步骤 10 | 配置写入失败 | 从备份恢复，发送告警 |
| 步骤 13 | 回滚触发 | 自动恢复备份，发送紧急通知 |

---

## 6. 非功能需求

### 6.1 性能要求

| 指标 | 目标值 | 说明 |
|------|--------|------|
| 单策略调优耗时 | < 120 秒 | 从采集到 AI 返回结果 |
| 全量调优耗时 | < 300 秒 | 两个策略串行执行 |
| API 调用延迟 | P95 < 10 秒 | DeepSeek API 响应时间 |
| 内存占用 | < 512 MB | 容器运行内存 |
| CPU 占用 | < 1 核 | 空闲时接近 0 |

### 6.2 安全要求

| 需求编号 | 需求描述 |
|----------|----------|
| SEC-001 | DeepSeek API Key 必须通过环境变量 `DEEPSEEK_API_KEY` 传入，不可硬编码 |
| SEC-002 | 飞书 Webhook URL 通过环境变量配置，不可硬编码 |
| SEC-003 | 数据库密码通过环境变量配置，不可硬编码 |
| SEC-004 | 配置文件写入前必须备份，保留最近 10 个备份 |
| SEC-005 | API 调用日志不包含 API Key 明文 |
| SEC-006 | 容器内不存储任何敏感信息的明文文件 |

### 6.3 可用性要求

| 需求编号 | 需求描述 |
|----------|----------|
| AVL-001 | 容器崩溃后自动重启（Docker restart policy: unless-stopped） |
| AVL-002 | 定时任务失败后，次日自动重试（不依赖人工介入） |
| AVL-003 | 所有关键操作有结构化日志，方便问题排查 |
| AVL-004 | 配置回滚操作可在 1 分钟内完成 |
| AVL-005 | 系统支持手动触发调优（用于应急场景） |

### 6.4 兼容性要求

| 需求编号 | 需求描述 |
|----------|----------|
| CMP-001 | 与现有 PostgreSQL 数据库兼容，使用 trading schema |
| CMP-002 | 与现有飞书通知服务兼容，复用 `shared/notification.py` |
| CMP-003 | 与现有策略 config.yaml 格式兼容，不破坏已有配置结构 |
| CMP-004 | Python 版本 3.10+，与主项目一致 |

### 6.5 可维护性要求

| 需求编号 | 需求描述 |
|----------|----------|
| MNT-001 | 新增策略只需添加适配器文件 + Prompt 模板文件，无需修改核心代码 |
| MNT-002 | 参数白名单定义在适配器内，与策略配置分离 |
| MNT-003 | 所有日志使用 structlog，结构化输出 |
| MNT-004 | Prompt 模板独立文件，支持非开发人员修改 |

---

## 7. 接口定义

### 7.1 内部接口

#### 7.1.1 BaseAdapter 抽象接口

```python
class BaseAdapter(ABC):
    """策略数据适配器基类"""

    @property
    @abstractmethod
    def strategy_id(self) -> str:
        """策略唯一标识"""
        ...

    @abstractmethod
    async def collect(self) -> StrategyReport:
        """采集过去一周的策略数据"""
        ...

    @abstractmethod
    def get_param_whitelist(self) -> dict:
        """返回参数白名单 {param_path: {min, max, step}}"""
        ...

    @abstractmethod
    def get_redline_params(self) -> list[str]:
        """返回红线参数列表"""
        ...
```

#### 7.1.2 MemoryHandler 接口

```python
class MemoryHandler:
    """记忆管理处理器"""

    async def ensure_table(self) -> None: ...
    async def save(self, record: MemoryRecord) -> int: ...
    async def get_recent(self, strategy_id: str, limit: int = 3) -> list[MemoryRecord]: ...
    async def mark_applied(self, record_id: int, approved_by: str) -> None: ...
    async def mark_rejected(self, record_id: int) -> None: ...
```

#### 7.1.3 ConfigOperator 接口

```python
class ConfigOperator:
    """配置操作器"""

    async def read(self, config_path: str) -> dict: ...
    async def backup(self, config_path: str) -> str: ...
    async def apply(self, config_path: str, adjustments: dict) -> bool: ...
    async def rollback(self, config_path: str, backup_path: str) -> bool: ...
    async def list_backups(self, config_path: str) -> list[str]: ...
```

### 7.2 外部 API 接口

#### 7.2.1 手动触发调优

```
POST /api/v1/trigger
Content-Type: application/json

{
  "strategy_ids": ["btc_eth", "new_coin"],  // 可选，默认全部
  "force": false                             // 是否强制执行（忽略时间检查）
}

Response:
{
  "status": "accepted",
  "task_id": "uuid",
  "strategies": ["btc_eth", "new_coin"]
}
```

#### 7.2.2 审批回调接口

```
POST /api/v1/approval
Content-Type: application/json

{
  "strategy_id": "btc_eth",
  "date": "2026-06-21",
  "action": "confirm"   // confirm | reject
}

Response:
{
  "status": "ok",
  "message": "已确认应用 btc_eth 的调优建议"
}
```

#### 7.2.3 手动回滚接口

```
POST /api/v1/rollback
Content-Type: application/json

{
  "strategy_id": "btc_eth",
  "backup_file": "config.yaml.backup.20260621235800"  // 可选，默认最新备份
}

Response:
{
  "status": "ok",
  "message": "已回滚 btc_eth 配置到备份 config.yaml.backup.20260621235800"
}
```

#### 7.2.4 健康检查接口

```
GET /api/v1/health

Response:
{
  "status": "healthy",
  "scheduler": "running",
  "next_run": "2026-06-28T23:55:00+08:00",
  "strategies": ["btc_eth", "new_coin"]
}
```

---

## 8. 审批流程详细说明

### 8.1 审批状态机

```
                    ┌──────────┐
                    │  pending  │  ← AI 建议生成后
                    └────┬─────┘
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
        ┌─────────┐ ┌─────────┐ ┌─────────┐
        │confirmed│ │rejected │ │ expired │
        └────┬────┘ └─────────┘ └─────────┘
             │
             ▼
        ┌─────────┐
        │ applied │  ← 配置已生效
        └─────────┘
```

### 8.2 审批超时策略

| 时间节点 | 动作 |
|----------|------|
| 0h | 推送飞书卡片，状态设为 pending |
| 24h | 发送提醒通知："仍有 N 条调优建议待审批" |
| 48h | 超时自动过期，状态设为 expired，发送过期通知 |
| >48h | 不再接受该建议的审批，关键词回复被忽略 |

### 8.3 审批冲突处理

| 场景 | 处理方式 |
|------|----------|
| 同一建议重复确认 | 幂等拦截，返回"已确认，无需重复操作" |
| 先确认后拒绝 | 拒绝被忽略，返回"已确认，无法拒绝" |
| 先拒绝后确认 | 确认被忽略，返回"已拒绝，无法确认" |
| 过期后确认 | 返回"已过期，建议已失效" |

---

## 9. 异常处理策略

### 9.1 异常分级

| 级别 | 定义 | 处理方式 | 示例 |
|------|------|----------|------|
| P0 - 致命 | 系统无法运行 | 立即告警 + 容器重启 | 数据库连接丢失、API Key 无效 |
| P1 - 严重 | 功能受阻 | 告警 + 降级 | API 调用全部失败、所有策略采集失败 |
| P2 - 一般 | 部分功能异常 | 告警 + 跳过 | 单个策略采集失败、AI 建议解析失败 |
| P3 - 轻微 | 不影响主流程 | 仅记录日志 | Token 用量超限、审批超时 |

### 9.2 降级策略

| 场景 | 降级方案 |
|------|----------|
| DeepSeek API 不可用 | 跳过本次调优，下周重试，发送告警 |
| 数据库不可用 | 终止流程，等待容器重启后数据库恢复 |
| 飞书通知不可用 | 记录日志，调优建议仍写入 memory（可后续手动查看） |
| 配置文件不可写 | 跳过配置应用，发送告警，保留 AI 建议在 memory 中 |

### 9.3 重试策略

| 操作 | 重试次数 | 间隔 | 退避策略 |
|------|----------|------|----------|
| DeepSeek API 调用 | 3 | 5s/10s/20s | 指数退避 |
| 飞书通知发送 | 3 | 5s/5s/5s | 固定间隔 |
| 数据库写入 | 3 | 3s/6s/12s | 指数退避 |
| 配置文件写入 | 0（不重试） | - | 原子性操作保证 |

---

## 10. 风险与依赖

### 10.1 风险识别

| 风险编号 | 风险描述 | 影响 | 概率 | 缓解措施 |
|----------|----------|------|------|----------|
| R-001 | AI 幻觉导致不合理参数建议 | 高 | 中 | 白名单+范围校验+人工审批三重防护 |
| R-002 | DeepSeek API 服务不可用 | 高 | 低 | 跳过本次调优，下周重试；不影响策略运行 |
| R-003 | 自动回滚机制误触发 | 中 | 低 | 回滚阈值可配置，触发后发送告警人工确认 |
| R-004 | 配置文件写入中断导致损坏 | 高 | 低 | 原子性写入+备份机制 |
| R-005 | 飞书审批超时导致建议堆积 | 低 | 中 | 48h 自动过期，每周重新生成 |
| R-006 | 策略参数变更后 config.yaml 结构变化 | 中 | 中 | 白名单定义在适配器内，与策略版本解耦 |
| R-007 | Token 成本超预算 | 低 | 中 | 成本追踪+滑动窗口压缩上下文 |

### 10.2 外部依赖

| 依赖 | 类型 | 说明 | 降级方案 |
|------|------|------|----------|
| DeepSeek API | 必须 | AI 决策引擎 | 不可降级，不可用时跳过调优 |
| PostgreSQL | 必须 | 记忆存储 | 不可降级，不可用时终止流程 |
| 飞书通知服务 | 重要 | 审批推送 | 不可用时记录日志，不阻塞流程 |
| 策略 config.yaml | 必须 | 配置读写 | 不可降级 |
| Binance API | 重要 | 盈亏数据采集 | 不可用时使用 trade_records 估算 |

### 10.3 后续扩展规划

| 扩展项 | 优先级 | 预计时间 |
|--------|--------|----------|
| 接入 HRS 反转策略 | 中 | 已完成 |
| 接入网格交易策略 | 中 | 第二期 |
| 月度调优支持 | 低 | 第二期 |
| Web UI 审批面板 | 低 | 第三期 |
| 多模型对比（对比不同 AI 模型的建议质量） | 低 | 第三期 |
| 策略间关联分析（一个策略的调优对另一个策略的启发） | 低 | 第四期 |

---

## 11. 附录

### 11.1 术语表

| 术语 | 英文 | 说明 |
|------|------|------|
| 调优 | Tuning | AI 驱动的策略参数调整 |
| 白名单 | Whitelist | AI 可以调整的参数列表 |
| 红线 | Redline | AI 绝对不可触碰的参数 |
| 滑动窗口 | Sliding Window | 仅取最近 N 条历史记忆构建上下文 |
| 回滚 | Rollback | 自动恢复到上一个配置版本 |
| 熔断 | Circuit Breaker | 策略因连续亏损/回撤过大而暂停交易 |

### 11.2 目录结构

```
/ai_tuner/
├── Dockerfile
├── requirements.txt
├── main.py                          # 入口文件
├── config.yaml                      # 调优系统自身配置
├── adapters/
│   ├── base_adapter.py              # 适配器基类
│   ├── mtpcs_adapter.py             # MTPCS 策略适配器
│   └── new_coin_adapter.py          # 新币做空策略适配器
├── memory/
│   ├── db_handler.py                # 记忆库读写
│   └── context_builder.py           # 上下文构建
├── prompts/
│   ├── common_rules.txt             # 通用规则
│   ├── mtpcs_system.txt             # MTPCS System Prompt
│   ├── mtpcs_user.txt               # MTPCS User Prompt 模板
│   ├── new_coin_system.txt          # 新币做空 System Prompt
│   └── new_coin_user.txt            # 新币做空 User Prompt 模板
├── engine/
│   ├── llm_client.py                # DeepSeek API 封装
│   ├── response_parser.py           # 响应解析与校验
│   └── cost_tracker.py              # Token 成本追踪
├── deploy/
│   ├── config_operator.py           # 两种写入模式: apply_changes() 写 config.yaml, apply_overrides() 写 tuning_overrides/
│   ├── diff_generator.py            # 差异生成
│   └── rollback_manager.py          # 回滚管理（覆盖层回滚通过修改 .active 指向旧版本）
├── notifier/
│   └── messenger.py                 # 飞书通知
└── scheduler/
    └── weekly_job.py                # 周度调优任务
```

### 11.3 环境变量清单

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `DEEPSEEK_API_KEY` | 是 | DeepSeek API 密钥 |
| `DEEPSEEK_API_BASE` | 否 | API 地址，默认 `https://api.deepseek.com` |
| `DB_HOST` | 是 | PostgreSQL 主机地址 |
| `DB_PORT` | 否 | PostgreSQL 端口，默认 5432 |
| `DB_NAME` | 是 | 数据库名 |
| `DB_USER` | 是 | 数据库用户 |
| `DB_PASSWORD` | 是 | 数据库密码 |
| `FEISHU_WEBHOOK_TUNER` | 是 | 调优专用飞书 Webhook URL |
| `FEISHU_TUNER_VERIFY_TOKEN` | 否 | 飞书事件订阅验证 Token |
| `MANUAL_TRIGGER` | 否 | 设为 `true` 时立即执行一次调优 |
| `LOG_LEVEL` | 否 | 日志级别，默认 INFO |
| `TZ` | 否 | 时区，默认 `Asia/Shanghai` |

---

## 12. 决策记录

> 以下决策在 2026-06-22 由用户逐一确认，作为后续开发和验收的权威依据。

### 12.1 参数白名单范围

| 决策项 | 结论 |
|--------|------|
| MTPCS 策略白名单 | **26 个参数**（当前方案），覆盖评分阈值、等级划分、杠杆/仓位映射、ATR 倍数、止损/止盈乘数、频率控制 |
| 新币做空策略白名单 | **26 个参数**（当前方案），覆盖评分阈值、OI/量比否决线、技术面门槛、风控参数、熔断参数 |
| 白名单扩展原则 | 后续新增策略时，白名单由策略开发者定义，需用户单独确认 |

### 12.2 记忆窗口深度

| 决策项 | 结论 |
|--------|------|
| 滑动窗口大小 | **3 条**已完成确认的调优摘要 |
| 上下文构建方式 | 仅提取 `summary` 字段拼接为「历史调优简史」段落 |
| 归档策略 | 超过 3 个月的历史记录不再参与上下文构建 |

### 12.3 自动回滚触发条件

| 决策项 | 结论 |
|--------|------|
| 监控窗口 | 新参数应用后 **24 小时** |
| 触发条件 1 | 连续 **3 笔**交易亏损 |
| 触发条件 2 | 累计亏损超过初始资金的 **2%** |
| 回滚后行为 | 自动恢复上一个备份，发送"紧急回滚"告警，记录到 memory 表 |

### 12.4 审批超时策略

| 决策项 | 结论 |
|--------|------|
| 超时时间 | **48 小时** |
| 超时处理 | 自动标记为 `expired`，本次建议丢弃，不做任何参数变更 |
| 超时告警 | 超时后发送"审批已过期"通知 |

### 12.5 数据采集方式

| 决策项 | 结论 |
|--------|------|
| 采集方式 | **独立采集**——在 ai_tuner 的 adapters 中独立实现 |
| 不依赖 | 不依赖 `strategies/weekly_report/collector.py` |
| 数据源 | 直接从 `trading.trade_records` 和 Binance income API 获取 |

### 12.6 飞书审批回调机制

| 决策项 | 结论 |
|--------|------|
| 回调方式 | **HTTP 回调端点**——在 ai_tuner 容器中启动 HTTP 服务 |
| 端点路径 | `POST /api/v1/approval` |
| 飞书集成 | 通过飞书开放平台事件订阅，卡片按钮回调到此端点 |
| 备选方案 | 同时支持直接 HTTP 调用（用于 Dashboard 或其他工具集成） |

### 12.7 Docker 容器写入权限

| 决策项 | 结论 |
|--------|------|
| 挂载模式 | 策略 `config.yaml` 挂载为 **只读（ro）**，`tuning_overrides/` 目录挂载为 **读写（rw）** |
| 安全措施 | AI 调优参数写入 `tuning_overrides/` 覆盖层，不修改 config.yaml；写入前自动生成版本号，原子写入 |
| 风险控制 | 覆盖层写入失败时通过原子操作保证状态一致性，.active 写入失败时自动回滚已创建的覆盖层文件 |

### 12.8 HRS 策略已上线

| 决策项 | 结论 |
|--------|------|
| 上线状态 | **已上线**——`config.yaml` 中 HRS 策略已注册并启用 |
| 状态 | `enabled: true`，调度器正常执行 |
| 上线说明 | HRS 策略已接入 AI 调优系统，编写了 Prompt 模板和白名单 |
| 白名单 | 预留 26 个参数（与 PRD 中 HRS 参数白名单一致） |

### 12.9 AI 调优激进程度

| 决策项 | 结论 |
|--------|------|
| 调优策略 | **保守** |
| 单次最大调整参数数 | **3 个** |
| 单参数调整幅度 | 不超过 **±20%** |
| 维持不变策略 | 当周胜率 ≥ 50% 且无明显异常时，**优先建议维持不变**（adjustments 返回 `{}`） |
| Prompt 约束 | 在 `common_rules.txt` 中强制写入此约束 |

### 12.10 AI 思考模式与推理强度

| 决策项 | 结论 |
|--------|------|
| 思考模式 | **启用** (`thinking_mode: enabled`)，让 AI 在输出调优建议前进行深层推理，提高建议质量和一致性 |
| 推理强度 | **high** (`reasoning_effort: high`)，使用最高推理强度确保关键参数调整决策经过充分分析 |
| 选用原因 | 策略参数调优属于高风险决策，需要 AI 充分推理后再输出建议；额外的推理 Token 成本由调优的保守策略和低频率（每周一次）对冲 |
| 配置方式 | 在 DeepSeek API 调用时设置 `thinking_mode: enabled` 和 `reasoning_effort: high`，代码中通过 `llm_client.py` 的配置参数传递 |

---

**文档结束**