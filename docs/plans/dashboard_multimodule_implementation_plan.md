# 数据看板多模块改造 · 实施规划

| 项 | 内容 |
|---|---|
| 规划版本 | v1.0 |
| 编写日期 | 2026-09-08 |
| 需求依据 | `docs/requirements/dashboard_multimodule_prd.md`（已评审） |
| 设计稿 | `docs/design/dashboard_multimodule_mockup.html`（已确认） |
| 适用范围 | dashboard 数据看板（FastAPI + 原生前端 + PostgreSQL + Docker） |

> 规划目标：把已确认的 PRD / 设计稿落为**可执行、可拆期、可验收**的工程方案。
> 所有新增表 DDL 幂等，接口向后兼容（只增不改旧字段），分三期灰度，避免一次性大爆炸改造。

---

## 一、总体架构决策

1. **生产路径**：实际生产走 `routes_docker.py` + `data_service_docker.py`（真实数据）；`main_docker.py` 的 `MockDataService` 仅本机演示，**不动**。
2. **前端**：原生 HTML/CSS/JS，直接改 `dashboard/frontend/{index,detail}.html` + `css/style.css` + `js/{main,api,config,charts}.js`。
3. **时区**：统一北京时间（`BEIJING_TZ = timezone(timedelta(hours=8))`，与现有 `data_service_docker.py` 一致）。
4. **禁止硬编码**（强制规则）：所有风控阈值（占用比例、连续亏损天数、回撤百分比、止损天数窗口）放配置文件，严禁写死在代码。
5. **新增表**：`public.equity_snapshot`、`public.ai_tuner_runs`、`trading.strategy_open_positions`；DDL 幂等、授权给看板连接用户 `trading_user`；放 `database/postgres/init-scripts/`。
6. **向后兼容**：`StrategySummary` 等 Model 只增字段；现有 `/overview` `/account/equity` `/trend` 响应不破坏。

### 关键实施决策（2026-09-08 用户确认）

| 决策项 | 结论 |
|---|---|
| **改造范围** | 三期一次性全做（快照+收益率、收益模块、AI监控、策略持概览、风控全量） |
| **策略持仓上报** | 策略直写 DB：各策略开/平仓时 UPSERT/DELETE `trading.strategy_open_positions`（复用 `shared.database`，无新增 HTTP 依赖） |
| **风控阈值统一** | 策略 config 为源，自动生成 risk.yaml：各策略 `config.yaml` 内的占用上限是唯一权威源；部署前由 `dashboard/backend/scripts/generate_risk_config.py` 读取各策略 config 汇总写入 `dashboard/backend/config/risk.yaml` 的 `account_ratio_caps`（看板只读生成物）。解决"写DB怕代码读不到 / 只写config怕不好统一管理"的双向顾虑——值只存文件、路径唯一、看板与策略永远一致，零手写双份 |
| **发布节奏** | 一次性全量发布：4 策略 + dashboard + ai-tuner 统一版本 + 五层验证 |

---

## 二、分期与交付范围

### 一期（P0 核心落地，建议先做）

| 模块 | 交付内容 |
|---|---|
| ①净资产快照+收益率 | 新增 `equity_snapshot` 表；dashboard 集成 APScheduler，北京 23:30 每日快照；新增 `GET /api/account/returns`；快照 UPSERT 幂等 |
| ②收益模块 | 首页重构为收益模块：账户总资产 + 日/周/月收益率切换 + 统计卡；前端改 index.html/main.js/api.js/style.css |
| ④策略概览增强 | 新增 `strategy_open_positions` 表 + DDL；dashboard 侧扩展 overview/strategies 返回 `open_position_count`/`open_margin`；前端策略卡新增2指标（无上报时回 0） |
| ③AI监控(可读部分) | 新增 `GET /api/ai-monitor`（读 `capital_allocation` active + `strategy_memory` 建议）；前端 AI 监控板块 |
| 风控(基础) | 新增 `GET /api/risk`（基础字段）；前端风控板块 + 归口提示 |

**一期验收门槛**：快照能落库、收益率接口正确、收益模块切换联动、策略卡显示持仓量/保证金（可回0）、AI监控可读、风控数字卡正确。**各策略容器上报不阻塞一期**（缺字段回0降级）。

### 二期（P1 补齐）

| 模块 | 交付内容 |
|---|---|
| ③AI监控(执行记录) | 新增 `ai_tuner_runs` 表；改 `weekly_job.run_weekly_tuning()` 每个策略 success/skip/error 落一条；前端调优时间线含状态色块 |
| ④持仓上报接入 | 各策略容器（btc_eth/new_coin/hrs/grid）开平仓 UPSERT/DELETE `strategy_open_positions`，多容器联调 |
| ⑤风控全量 | 占用量/上限/逼近阈值/超限/最近止损次数对接；阈值配置化 |

**二期验收门槛**：`ai_tuner_runs` 有真实记录、调优时间线正确；各策略真实持仓量/保证金聚合正确；风控预警按阈值触发。

### 三期（P2 后续）
- 连续亏损 / 大额回撤提醒深化（依赖更长快照历史）
- 风控历史趋势、止损走势图
- 可选 `POST /api/positions/report` HTTP 上报兜底通道

---

## 三、新增数据表（DDL 概要）

### 3.1 `public.equity_snapshot`（模块①，一期）
```sql
CREATE TABLE IF NOT EXISTS public.equity_snapshot (
    id                SERIAL PRIMARY KEY,
    snapshot_date     DATE NOT NULL UNIQUE,          -- 北京日期
    total_equity      DECIMAL(20,8) NOT NULL,
    available_balance DECIMAL(20,8) NOT NULL,
    open_positions    INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_equity_snapshot_date ON public.equity_snapshot(snapshot_date);
```
> UPSERT：`ON CONFLICT (snapshot_date) DO UPDATE`，保证同日幂等。

### 3.2 `public.ai_tuner_runs`（模块③，二期）
```sql
CREATE TABLE IF NOT EXISTS public.ai_tuner_runs (
    id            SERIAL PRIMARY KEY,
    run_key       VARCHAR(40) NOT NULL,              -- 一次周度调优批次标识（周日日期，如 2026-09-13）
    strategy_id   VARCHAR(32),
    strategy_name VARCHAR(64),
    status        VARCHAR(10) NOT NULL,              -- 'success'|'skip'|'error'
    executed_at   TIMESTAMP NOT NULL,                -- 北京时间执行结束
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ai_tuner_runs_skey ON public.ai_tuner_runs(run_key);
```

### 3.3 `trading.strategy_open_positions`（模块④，一期建表/二期接入）
```sql
CREATE TABLE IF NOT EXISTS trading.strategy_open_positions (
    strategy_id VARCHAR(32),
    symbol      VARCHAR(20),
    margin      DECIMAL(20,8) NOT NULL DEFAULT 0,
    quantity    DECIMAL(20,8) NOT NULL DEFAULT 0,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (strategy_id, symbol)
);
```

> DDL 全部幂等，放置于 `database/postgres/init-scripts/`，并 `GRANT` 给 `trading_user`。

---

## 四、后端改造清单

### 4.1 新增 Service 方法（`data_service_docker.py`）
| 方法 | 用途 |
|---|---|
| `get_account_returns(type)` | 读快照表，按 ③日/周/月② 公式算收益率；期初缺失 → `yield_unavailable=true` |
| `get_ai_monitor(weeks, limit)` | 聚合 tuning_runs(二期读表) + capital_allocation + strategy_memory |
| `get_risk(days)` | 聚合当前持仓保证金(读 strategy_open_positions) + capital_manager 上限 + 止损次数 |
| `get_open_positions_summary()` | 按 strategy_id 聚合 open_position_count / sum(margin)，供 overview/strategies 扩展 |

### 4.2 新增路由（`routes_docker.py`）
| 端点 | 一期/二期 | 说明 |
|---|---|---|
| `GET /api/account/returns` | 一期 | 收益率，短 TTL 缓存 |
| `GET /api/ai-monitor` | 一期(可读)/二期(执行记录) | 中 TTL（10 分钟） |
| `GET /api/risk` | 一期(基础)/二期(全量) | 短 TTL |

### 4.3 修改现有接口
- `GET /api/overview`、`GET /api/strategies`、`GET /api/strategies/{id}`：`strategies[]` 每项追加 `open_position_count`、`open_margin`（读 `strategy_open_positions` 聚合；无数据回 0）。-> `models/schemas.py` `StrategySummary` 增两字段。

### 4.4 定时快照任务（`dashboard/backend/services/equity_snapshot_job.py`，一期）
- 集成 APScheduler `AsyncIOScheduler` 到 `main_docker.py` `lifespan`。
- cron：北京 `23:30`，每天 `snapshot_equity()`。
- 逻辑：`get_account_equity()` -> 计算北京今日 `snapshot_date` -> `ON CONFLICT` UPSERT -> 失败记日志告警，不抛阻断。
- `requirements.txt` 增 `apscheduler`。

### 4.5 `weekly_job.py` 改造（二期）
- `run_weekly_tuning()` 循环内每个策略分支（success/skip/error）写入一条 `ai_tuner_runs`，`run_key` 用本次执行周日日期。

---

## 五、前端改造清单

### 5.1 `dashboard/frontend/index.html`
- 首页重构为**收益模块**（账户总资产 + 日/周/月收益率切换器 + 统计卡）。
- 新增 **AI 监控板块**：调优时间线(二期) + 月度资金分配 + 最近优化建议。
- 新增 **风控板块**：数字卡 + 进度条 + 归口提示（问号 tooltip + 底部小字）。
- 策略卡指标区增"持仓量 / 持仓保证金"两项。

### 5.2 `dashboard/frontend/js/api.js`
- 新增 `getAccountReturns(period)`、`getAiMonitor()`、`getRisk()` 方法。

### 5.3 `dashboard/frontend/js/main.js`
- 加载收益率、AI监控、风控数据；收益率切换器联动刷新 returns + overview。

### 5.4 `dashboard/frontend/css/style.css`
- 收益模块、AI监控、风控板块样式；复用现有玻璃卡片 + tooltip 样式（基于设计稿）。

### 5.5 设计稿落地产物
- `docs/design/dashboard_multimodule_mockup.html` 已确认，作为样式与布局的事实来源。

---

## 六、数据归口（页面须标明，来自设计稿确认）

| 指标 | 归口 |
|---|---|
| 账户总资产 | 币安 PM `accountEquity` 实时 |
| 持仓总保证金 | 净资产 − 可用余额，或 `strategy_open_positions` Σmargin |
| 持仓上限 | 本月各策略月度资金分配之和（`capital_allocation`） |
| 占用率阈值 | 各策略真实 `account_ratio_cap`（MTPCS 30%、HRS 20%、新币做空绝对额150） |
| 最近止损次数 | `trade_records` 近7天 `close_reason='STOP_LOSS'`（各策略止损打标写入；grid 不计入） |
| 收益率 | 净资产快照增量（非 income API） |

> 每项在页面用「问号 tooltip + 底部小字」双形式标注归口，杜绝无源数据。

---

## 七、验收（每期结束逐项核对 PRD 第 6 节验收点）

- 一期：快照落库 + returns 正确 + 收益模块切换 + 策略卡持仓字段 + AI监控可读 + 风控基础。
- 二期：ai_tuner_runs 落库 + 多容器持仓上报联调 + 风控全量预警。
- 幻觉测试 10 项清单逐项过 + 功能测试 + 覆盖率验证 + 代码规范检测（工作流强制）。

---

## 八、风险与依赖
| 风险 | 缓解 |
|---|---|
| 多容器持仓上报版本漂移 | 一期先建表+dashboard容错(缺字段回0)，二期逐策略接入 |
| 历史快照需积累 | 前期周/月收益率显 "--" + 积累中提示 |
| dashboard 需读策略 config 算上限 | 部署确保 dashboard 挂载策略配置目录；读取失败 "--" |
| 止损 order_type 枚举需核对 | 开发期查产品库真实枚举并在代码/文档固化 |
| 23:30 采样受行情瞬时影响 | 属预期；如需稳改日内多次均值(三期) |

---

**最后更新：2026-09-08**