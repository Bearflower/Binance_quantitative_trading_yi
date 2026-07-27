# StratTuneAI — 多策略AI调优系统实施计划

> **创建时间**: 2026-06-21  
> **状态**: 待执行  
> **参考文档**: `docs/requirements/StratTuneAI/多策略AI调优系统技术路线.md`

---

## 一、摘要

基于技术路线文档中的「采集—记忆—决策—审批—执行」五层闭环架构，实现一个**可平行扩展的多策略AI长期调优系统**。第一期覆盖 MTPCS（BTC/ETH趋势策略）和新币做空策略，以周度为频率，通过 DeepSeek-v4-pro 生成参数调整建议，经飞书人工确认后生效。

---

## 二、当前状态分析

### 2.1 已有基础设施（可直接复用）

| 基础设施 | 位置 | 复用方式 |
|---------|------|---------|
| PostgreSQL 数据库 | `shared/database.py` | 新建 `strategy_memory` 表，复用连接池 |
| 飞书通知服务 | `shared/notification.py` | 复用 `send()` 方法，新增调优专用 Webhook |
| 周报数据采集 | `strategies/weekly_report/collector.py` | 复用 `WeeklyStrategyStats` 和 P&L 采集逻辑 |
| 交易记录 | `shared/trade_logger.py` → `trading.trade_records` | 已记录所有策略的每笔成交 |
| K线服务 | `shared/kline_service.py` | 可查询历史K线用于补充分析 |
| 策略配置 | `strategies/*/config.yaml` | 各策略的参数白名单来源 |

### 2.2 缺失部分（需新建）

- AI/LLM 调用模块（项目中无任何 AI 代码）
- 策略记忆数据库表（`strategy_memory`）
- 数据适配器层（统一各策略的报告格式）
- Prompt 模板引擎
- 配置变更与回滚管理
- 定时调度器（独立于策略的 Cron 触发）
- Docker 容器化和编排

---

## 三、需澄清与讨论的关键问题

> 以下问题已在前期调研中识别，部分已由用户决策，部分仍需在实施过程中细化。

### 3.1 ✅ 已决策

| # | 问题 | 决策 |
|---|------|------|
| 1 | 第一期覆盖哪些策略？ | MTPCS + 新币做空策略 |
| 2 | 部署方式？ | 独立 Docker 容器 |
| 3 | 审批确认方式？ | 飞书交互卡片确认（回复关键词） |
| 4 | 调优频率？ | 仅周度调优（每周日触发） |

### 3.2 ⚠️ 待细化（实施中需进一步明确）

| # | 问题 | 建议方案 | 需确认方 |
|---|------|---------|---------|
| 5 | **各策略的参数白名单**（AI 可调整哪些参数） | MTPCS：评分阈值、杠杆等级映射、ATR倍数、连亏熔断次数；新币做空：入场阈值、OI/量比否决线、连续亏损暂停次数、回撤熔断百分比 | 用户确认各策略的"不可触碰红线" |
| 6 | **记忆窗口深度** | 滑动窗口保留最近 3 条已生效记忆，超过 3 个月的归档到冷存储 | 可后续调整 |
| 7 | **自动回滚触发条件** | 应用新参数后 24h 内连续 3 笔亏损 或 总亏损超 2% | 用户确认阈值 |
| 8 | **飞书审批超时策略** | 超过 48h 未确认则自动丢弃本次建议 | 用户确认 |
| 9 | **HRS 策略接入时机** | HRS 上线后作为一个新的 Adapter 注册即可，无需改动核心引擎 | 架构已预留扩展点 |
| 10 | **未知策略的扩展方式** | 只需实现 `BaseAdapter` 接口 + 提供 `config.yaml` 白名单 + 编写 Prompt 模板，即可注册 | 架构已支持 |

### 3.3 🔴 核心技术风险

| 风险 | 描述 | 缓解措施 |
|------|------|---------|
| DeepSeek API 不稳定 | API 可能超时或返回非 JSON | 3次重试 + JSON 校验 + 解析失败自动丢弃告警 |
| AI 幻觉参数 | 建议超出合理范围的参数值 | 白名单边界校验 + 参数范围硬限制 |
| Token 成本过高 | 周报数据量大导致 Prompt 过长 | 记忆压缩（只传摘要不传原始数据）+ Token 用量追踪 |
| 配置变更导致策略异常 | 新参数生效后策略表现恶化 | 自动回滚触发器 + 备份机制 |

---

## 四、系统架构设计

### 4.1 总体架构

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
   │PostgreSQL│  ← strategy_memory 表
   └──────────┘
```

### 4.2 数据流时序（一次完整周调优）

```
周日 23:55  Cron触发
    │
    ▼
[1] 遍历已注册策略列表 (MTPCS, new_coin)
    │
    ▼
[2] 调用各策略 Adapter.collect() 生成标准化 StrategyReport
    │   ├── 从 trading.trade_records 查询本周交易
    │   ├── 从 Binance income API 获取 P&L
    │   └── 计算胜率、盈亏比、回撤、连亏等指标
    │
    ▼
[3] 从 strategy_memory 拉取该策略最近 3 条 summary
    │
    ▼
[4] 加载 Prompt 模板 → 填充参数 → 调用 DeepSeek-v4-pro
    │
    ▼
[5] 解析 JSON 响应 → 校验参数边界 → 生成 Diff 清单
    │
    ▼
[6] 通过飞书推送变更卡片（含理由+预估影响）
    │
    ▼
[7] 等待人工确认（超时 48h 则丢弃）
    │
    ▼
[8] 确认后：备份当前 config → 写入新参数 → 记录到 memory 表
    │
    ▼
[9] 发送"已生效"通知 + 启动效果追踪标记
```

---

## 五、目录结构设计

```
/ai_tuner/                          ← 项目根目录下的新模块
├── Dockerfile                      ← 独立容器镜像
├── requirements.txt                ← 独立依赖（openai, jinja2, pydantic 等）
├── main.py                         ← 入口：启动调度器
├── config.yaml                     ← AI调优系统自身配置
│
├── adapters/                       ← 数据适配器层
│   ├── __init__.py
│   ├── base_adapter.py             ← 抽象基类，定义 collect() 接口
│   ├── mtpcs_adapter.py            ← MTPCS 策略适配器
│   └── new_coin_adapter.py         ← 新币做空策略适配器
│
├── memory/                         ← 记忆管理层
│   ├── __init__.py
│   ├── db_handler.py               ← strategy_memory 表 CRUD
│   └── context_builder.py          ← 滑动窗口上下文构建
│
├── prompts/                        ← Prompt 模板（策略隔离）
│   ├── mtpcs_system.txt            ← MTPCS 系统提示词
│   ├── mtpcs_user.txt              ← MTPCS 用户提示词模板
│   ├── new_coin_system.txt         ← 新币做空系统提示词
│   ├── new_coin_user.txt          ← 新币做空用户提示词模板
│   └── common_rules.txt            ← 通用调优规则（所有策略共享）
│
├── engine/                         ← AI 调用核心
│   ├── __init__.py
│   ├── llm_client.py               ← DeepSeek API 封装
│   ├── response_parser.py          ← JSON 解析与边界校验
│   └── cost_tracker.py             ← Token 用量统计
│
├── deploy/                         ← 配置生效与回滚
│   ├── __init__.py
│   ├── config_operator.py          ← 读写各策略 config.yaml
│   ├── diff_generator.py           ← 生成人类可读变更清单
│   └── rollback_manager.py         ← 备份与回滚
│
├── notifier/                       ← 通知模块
│   ├── __init__.py
│   └── messenger.py                ← 飞书交互卡片推送与确认监听
│
└── scheduler/                      ← 定时调度
    ├── __init__.py
    └── weekly_job.py               ← 周度调优主流程
```

---

## 六、核心模块详细设计

### 6.1 数据适配器（Adapter）

**BaseAdapter 接口定义**：

```python
class BaseAdapter(ABC):
    """策略数据适配器基类，所有策略适配器必须实现此接口"""

    strategy_id: str          # 策略唯一标识（如 "mtpcs", "new_coin"）
    config_path: str          # 策略 config.yaml 路径
    param_whitelist: list     # AI 可调参数白名单（配置项路径列表）

    @abstractmethod
    async def collect(self) -> StrategyReport:
        """采集本周策略表现数据，返回标准化报告"""
        ...

    @abstractmethod
    def get_current_params(self) -> dict:
        """获取当前策略的可调参数值"""
        ...

    @abstractmethod
    def validate_params(self, adjustments: dict) -> bool:
        """校验 AI 建议的参数是否在白名单范围内"""
        ...
```

**StrategyReport 统一 Schema**（Pydantic）：

```python
class StrategyReport(BaseModel):
    meta: StrategyMeta               # 策略ID、版本、运行时长
    performance: PerformanceMetrics  # 胜率、盈亏比、总盈亏、夏普近似值
    risk: RiskMetrics                 # 最大连亏、当前回撤%、熔断状态
    distribution: DistributionMetrics # 信号分布、持仓时长分布
    anomalies: list[str]             # 异常事件列表
```

**MTPCS Adapter 数据采集来源**：
- `trading.trade_records`（strategy="MTPCS策略"）→ 本周交易统计
- Binance income API → P&L 流水
- `strategies/btc_eth/config.yaml` → 当前参数值

**新币做空 Adapter 数据采集来源**：
- `trading.trade_records`（strategy="新币做空策略"）→ 本周交易统计
- Binance income API → P&L 流水
- `strategies/new_coin/config.yaml` → 当前参数值

### 6.2 记忆库（Memory）

**数据库表**（在现有 PostgreSQL 中新建）：

```sql
CREATE TABLE IF NOT EXISTS trading.strategy_memory (
    id SERIAL PRIMARY KEY,
    strategy_id VARCHAR(32) NOT NULL,
    version VARCHAR(20),
    summary TEXT,                       -- AI 生成的 50 字核心摘要
    full_report JSONB,                  -- 完整 StrategyReport
    ai_suggestions JSONB,               -- AI 输出的调优建议
    is_applied BOOLEAN DEFAULT FALSE,   -- 是否已确认生效
    approved_by VARCHAR(64),            -- 审批人
    approved_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_memory_strategy_date
    ON trading.strategy_memory (strategy_id, created_at DESC);
```

**上下文构建策略**：
- 每次调用 API 前，查询该 `strategy_id` 最近 3 条 `is_applied=true` 的记录
- 仅提取 `summary` 字段拼接，组成「历史调优简史」段落
- 超过 3 个月的历史记录可标记为归档，不参与上下文构建

### 6.3 AI 引擎（Engine）

**DeepSeek API 调用**：
- 使用 `openai` Python SDK，设置 `base_url="https://api.deepseek.com"`
- 模型：`deepseek-chat`（对应 v4-pro）
- 温度：0.3（保证输出稳定性）
- 最大 Token：4096
- 重试：3 次，指数退避

**Prompt 模板结构**（Jinja2）：

```
System: {{ common_rules }}
        {{ strategy_specific_rules }}

User:   当前策略配置：{{ current_params }}
        本周体检报告：{{ report }}
        历史调优记忆：{{ memory_history }}
        输出要求：严格 JSON 格式，包含 reasons, adjustments, expected_impact
```

**输出 JSON Schema**：

```json
{
  "reasons": "本周胜率下降至38%，主要原因是...",
  "summary": "建议降低入场阈值，收紧风控",
  "adjustments": {
    "scoring.entry_threshold": {"from": 6.5, "to": 7.0},
    "risk.max_consecutive_losses": {"from": 3, "to": 2}
  },
  "expected_impact": "预计胜率将回升至45%以上，但交易频率会降低20%",
  "confidence": 0.75
}
```

**安全校验**：
1. JSON 格式校验 → 解析失败则丢弃 + 告警
2. 参数白名单校验 → 不在白名单的参数直接拒绝
3. 参数范围校验 → 超出预设边界的值截断到边界值
4. 空建议检测 → 如果 AI 建议"维持不变"，也记录到记忆库

### 6.4 审批与生效（Deploy）

**飞书交互卡片**（参考现有通知格式）：

```
📊 StratTuneAI 周度调优建议
━━━━━━━━━━━━━━━━━━━━
策略：MTPCS（主流币趋势回调）
时间：2026-06-21 周日

📋 变更清单：
  • 入场阈值：75 → 78
  • S级杠杆：5x → 4x
  • 连亏熔断：5次 → 3次

💡 AI 理由：近两周胜率从52%降至41%，提高阈值可过滤低质量信号

📈 预估影响：胜率回升至48%+，交易频率降低约15%

━━━━━━━━━━━━━━━━━━━━
回复 /confirm MTPCS_20260621 确认应用
回复 /reject MTPCS_20260621 拒绝
超时 48h 自动丢弃
```

**确认流程**：
1. 飞书机器人监听群消息，匹配 `/confirm <strategy_id>_<date>` 格式
2. 确认后触发 `config_operator.apply()`：备份 → 写入 → 记录
3. 发送「已生效」通知，附带变更前后对比

**回滚机制**：
- 每次应用前备份当前 `config.yaml` → `config.yaml.backup.{timestamp}`
- 回滚触发器（可选）：应用后 24h 内，连续 3 笔亏损或总亏损超 2%，自动回滚并告警
- 手动回滚：管理员回复 `/rollback <strategy_id>` 恢复到上一个备份

### 6.5 定时调度（Scheduler）

- 使用 **APScheduler**（轻量级，适合单容器部署）
- Cron 表达式：`55 23 * * 0`（每周日 23:55）
- 启动时检查：如果上次调优距今超过 7 天，立即执行一次补偿调优

---

## 七、Docker 容器化

### 7.1 Dockerfile

```dockerfile
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
```

### 7.2 docker-compose 集成

在根级 `docker-compose.yml` 中新增：

```yaml
strattuneai:
  build: ./ai_tuner
  container_name: strattuneai
  restart: unless-stopped
  environment:
    - DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}
    - DB_HOST=postgres
    - FEISHU_WEBHOOK_TUNER=${FEISHU_WEBHOOK_TUNER}
  volumes:
    - ./strategies/btc_eth/config.yaml:/app/strategies/btc_eth/config.yaml:ro
    - ./strategies/new_coin/config.yaml:/app/strategies/new_coin/config.yaml:ro
  depends_on:
    - postgres
```

### 7.3 新增依赖

```
openai>=1.0.0        # DeepSeek API（兼容 OpenAI SDK）
jinja2>=3.0.0        # Prompt 模板引擎
pydantic>=2.0.0      # 数据校验
apscheduler>=3.10.0  # 定时调度
asyncpg>=0.29.0      # 数据库（复用已有）
pyyaml>=6.0          # 配置文件读写
```

---

## 八、实施步骤

| 序号 | 步骤 | 涉及文件 | 负责智能体 |
|------|------|---------|-----------|
| 1 | **需求分析**：输出完整 PRD，明确各策略参数白名单、Prompt 边界、审批流程 | 需求文档 | requirements-document-expert |
| 2 | **架构设计**：确认数据库表结构、API 接口、模块间通信协议 | 架构文档 | backend-architect |
| 3 | **编码实现**：按目录结构逐模块实现 | `ai_tuner/` 下全部文件 | python-engineer |
| 4 | **代码检测**：检查编码规范、硬编码、SQL 注入防护 | — | code-specification-inspector |
| 5 | **强制测试**：功能测试 + 契约测试 + 性能测试 | 测试脚本 | api-test-pro |
| 6 | **代码审查**：深度审查 + 安全检查 | — | TRAE-code-review |
| 7 | **文档更新**：更新 `docs/` 下的架构和部署文档 | `docs/` | code-document-curator |

---

## 九、验证方案

### 9.1 功能验证

| 验证点 | 方法 | 预期结果 |
|--------|------|---------|
| 数据采集正确性 | 手动运行 Adapter.collect()，与周报数据对比 | 数据一致 |
| AI 调用成功 | 发送测试 Prompt，检查返回 JSON | 返回合法 JSON |
| 参数校验 | 发送越界参数，检查拒绝逻辑 | 越界参数被拒绝 |
| 飞书通知 | 触发一次调优流程，检查飞书消息 | 卡片正确推送 |
| 确认生效 | 回复 /confirm，检查 config.yaml | 参数已更新 |
| 回滚 | 触发回滚条件，检查 config.yaml | 恢复为备份版本 |

### 9.2 集成验证

| 验证点 | 方法 | 预期结果 |
|--------|------|---------|
| Docker 构建 | `docker-compose build strattuneai` | 构建成功 |
| 容器启动 | `docker-compose up -d strattuneai` | 容器正常运行 |
| 数据库连接 | 检查容器日志 | 连接池创建成功 |
| 调度触发 | 修改 Cron 为每分钟，观察执行 | 按预期触发 |

---

## 十、扩展预留（未来策略接入）

### 接入新策略只需 3 步：

1. **创建 Adapter**：继承 `BaseAdapter`，实现 `collect()`、`get_current_params()`、`validate_params()`
2. **编写 Prompt 模板**：在 `prompts/` 目录下新增 `<strategy_id>_system.txt` 和 `<strategy_id>_user.txt`
3. **注册白名单**：在 `config.yaml` 中定义该策略的 `param_whitelist`

### 示例：HRS 策略接入

```python
# adapters/hrs_adapter.py
class HRSAdapter(BaseAdapter):
    strategy_id = "hrs"
    config_path = "strategies/hrs/config.yaml"
    param_whitelist = [
        "scoring.entry_threshold",
        "risk.max_consecutive_losses",
        "risk.max_drawdown_pause_percent",
        "candidate.short_24h_gain_threshold",
        # ... 其他可调参数
    ]
```

---

## 十一、关键决策记录

| 决策 | 内容 | 日期 |
|------|------|------|
| 第一期范围 | MTPCS + 新币做空 | 2026-06-21 |
| 部署方式 | 独立 Docker 容器 | 2026-06-21 |
| 审批方式 | 飞书卡片交互确认 | 2026-06-21 |
| 调优频率 | 仅周度 | 2026-06-21 |
| AI 模型 | DeepSeek-v4-pro（deepseek-chat） | 2026-06-21 |
| 调度器 | APScheduler（单容器） | 2026-06-21 |
| 数据库 | 复用现有 PostgreSQL（trading schema） | 2026-06-21 |
| 通知 | 复用飞书通知服务 + 新增调优专用 Webhook | 2026-06-21 |