# Binance 量化交易数据看板 · 多模块改造 PRD

| 项 | 内容 |
|---|---|
| 文档版本 | v1.1 |
| 编写日期 | 2026-09-10 |
| 文档状态 | 已评审（对齐实现） |
| 适用范围 | dashboard 数据看板（FastAPI 后端 + 原生前端 + PostgreSQL + Docker） |

> v1.1 变更：月度资金分配条目补充 `occupied_amount`（占用金额）字段与展示列；风控逼近/超限判定由单全局门槛改为逐策略判定（比例型 cap<1 / 绝对额型 cap≥1）；`account_ratio_caps` 字段口径补充两种语义说明。

> 本文档是业务方确认需求的唯一依据，必须无歧义、可落地、可被前端展示与后端开发验证。
> 文中所有表名、API、Service 方法均为探查后预配置的真实对象；凡"**新增**"标注的均需本次开发落地。

---

## 1. 需求背景与目标

### 1.1 背景
当前数据看板已具备总览统计、账户净资产实时卡、趋势图、策略概览卡片、详情页等能力，但缺少
以下业务能力闭环：

- 缺少**净资产的历史积累与收益率口径**，无法回答"今天/本周/本月赚了百分之多少"；
- 首页核心统计区未把"账户总资产 + 收益率"作为用户最关心的第一价值信息突出；
- **AI 调优（ai-tuner）** 的周度执行结果、月度资金分配、优化建议明细未在看板可视化；
- 策略概览卡片缺少**当前持仓相关的实盘维度**（持仓订单量、持仓占用保证金）；
- **风控数据**散落在 `shared/capital_manager.py` 与策略配置中，未在看板集中呈现与预警。

### 1.2 业务目标
1. 建立从今日起每日积累的**净资产历史快照**，并据此计算可追溯的日/周/月收益率。
2. 重构首页为核心"**收益模块**"，突出账户总资产与收益率。
3. 新增"**AI 监控模块**"，可视化调优执行、资金分配、优化建议全链路。
4. 策略概览展示**当前持仓维度**，让业务看到每策略"还压着多少钱"。
5. 新增"**风控模块**"，集中呈现持仓保证金占用、可用开仓额度、阈值预警等。

### 1.3 成功指标
- 每日 23:30（北京时间）净资产快照成功落库，次日可查昨日收益率；
- 收益模块各指标在页面无需刷新即随日/周/月切换正确联动；
- AI 监控、风控模块数据源真实、口径透明、展示无空白态依赖人工补数。

---

## 2. 用户故事 / 业务价值（简明）

| 用户 | 用户故事 | 业务价值 |
|---|---|---|
| 交易者/负责人 | 我想知道今天、本周、本月账户到底赚了百分之几 | 快速定位收益质量，辅助决策 |
| 负责人 | 我想一眼看到当前总资产和收益率，而不是逐个数卡 | 首页第一屏即核心结论 |
| 负责人 | 我想看 AI 每周到底调没调策略、调了啥、哪来的钱分给谁 | 验证 AI 调优的价值与资金去向透明 |
| 负责人 | 我想知道每个策略当前还压着多少保证金 | 识别资金占用与风险暴露 |
| 风控关注者 | 我想看持仓保证金离上限还有多少、最近止损了几次 | 提前预警风险，避免超限 |

---

## 3. 功能需求详述（按模块）

> 术语统一说明：
> - **净资产（equity）** = 合约账户总权益（含未实现盈亏），即 `shared/binance_api.py get_account_info()` 的 `totalMarginBalance`（见 `data_service_docker.py` 的 `get_account_equity()`）。
> - **快照** = 每日 23:30（北京时间）将净资产落库形成的记录。
> - **净盈亏（pnl）口径**：本文所有收益率基于**净资产快照的增量**计算（见 3.1.2），避免与 income API 的已实现/未实现口径混淆。

---

### 3.1 模块①：净资产快照与收益率

#### 3.1.1 功能描述
- 系统每日 **北京时间 23:30** 自动采集一次合约账户净资产（USDT），写入历史快照表（**新增** `public.equity_snapshot`）。
- 快照自功能上线之日开始累计，一天一条；同日重复执行采用 **UPSERT 覆盖**，保证幂等。
- 基于快照计算 **日 / 周 / 月收益率**，历史不足时在页面展示 "--"。

#### 3.1.2 计算口径（公式，避免歧义）

以快照表为准，记某账户权益为 `equity(d)`（`d` 为快照日期，北京时区）：

| 指标 | 口径 | 公式 | 分母说明 | 历史不足条件 → "--" |
|---|---|---|---|---|
| 日收益率 | 今日 vs 昨日 | `(equity(today) - equity(yesterday)) / equity(yesterday)` | 昨日净资产快照 | 昨日快照不存在 |
| 周收益率 | 今日 vs 本周首日 | `(equity(today) - equity(this_monday)) / equity(this_monday)` | 本周一净资产快照 | 本周一快照不存在（含本周一未落库、跨周首日无快照） |
| 月收益率 | 今日 vs 本月首日 | `(equity(today) - equity(month_1st)) / equity(month_1st)` | 本月 1 号净资产快照 | 本月 1 号快照不存在 |

**严格边界条件：**
- `equity(yesterday)` / `equity(this_monday)` / `equity(month_1st)` 中对应**期初净资产若为 0 或缺失**，收益率一律显示 "--"，不做除零运算。
- 快照日期按**北京时间**取日；周首日固定为**周一**。
- 收益率为百分比，保留 1 位小数（如 `+2.4%` / `-1.3%`），正负号必须展示。
- 若今日与期初为同一条快照（如同日），分子为 0，收益率展示 `0.0%` 而非 "--"。

#### 3.1.3 数据来源
- 实时净资产：复用 `DataService.get_account_equity()`（`dashboard/backend/services/data_service_docker.py`）。
- 快照存储：**新增** `public.equity_snapshot` 表。
- 定时触发：**新增** dashboard 后台定时任务（见第 4 节技术方案要点）。

#### 3.1.4 接口设计（**新增**）

```
GET /api/account/returns?period=[daily|weekly|monthly]
```
响应（沿用 `BaseResponse` 包裹，data 结构如下）：
```json
{
  "period": "daily",
  "equity": "12345.67",
  "yield": 2.4,
  "yield_text": "+2.4%",
  "yield_unavailable": false,
  "period_start_equity": "12055.34",
  "period_pnl": "290.33",
  "snapshot_date": "2026-09-10"
}
```
字段说明：
- `equity`：当前/最新净资产（字符串，USDT）。
- `yield_unavailable`：`true` 时前端显示 "--"，忽略 `yield`/`yield_text`。
- `period_start_equity`：期初净资产（对应昨日/周一/1号快照）；不可用时返回 `null`。
- `period_pnl`：`equity - period_start_equity`；不可用返回 `null`。

#### 3.1.5 前端展示方案
- 位置：首页"**收益模块**"内（见模块②），提供 **日/周/月 收益率切换器**，复用现有 `toggle-btn` 交互样式。
- 切换后：收益率数字、期初净资产、本期净盈亏随切换联动刷新。
- EMPTY 态：`yield_unavailable=true` 时收益率显示 "--"，并在卡片副标题提示"历史数据积累中"。
- loading 态：请求期间展示骨架/"更新中"标记，与现有 `.stat-value.updating` 一致。

#### 3.1.6 边界条件
- 定时任务重复触发、服务器重启补采、同一天多次执行 → 同一 `snapshot_date` UPSERT 不产生重复行。
- 采集失败（Binance API 超时等）→ 本次不落库、记日志告警；次日 23:30 正常补一次当日快照（若当日缺失则重新尝试）。
- 未产生交易（权益不变）仍照常快照，收益率自然为 0。

---

### 3.2 模块②：收益模块（首页核心统计区）

#### 3.2.1 功能描述
在首页将当前"总览统计 + 净资产卡"区域升级为**收益模块**，集中展示一次：
1. 账户总资产（实时净资产）
2. 收益率（日/周/月切换）
3. 总盈亏、总胜率、总平仓数、总委托数、总佣金

#### 3.2.2 数据来源
- 账户总资产：`get_account_equity()` 的 `total_equity`。
- 收益率：模块①的 `GET /api/account/returns`。
- 总盈亏/总胜率/总平仓/总委托/总佣金：`GET /api/overview` 现有字段（`total_pnl/total_commission/total_orders/total_closed/total_wins/win_rate`，见 `data_service_docker.py get_overview()`）。

#### 3.2.3 接口设计
- 复用现有 `GET /api/overview?type=[daily|weekly|monthly]`（不修改）。
- 复用现有 `GET /api/account/equity`（不修改）。
- 新增 `GET /api/account/returns`（见模块①）。

#### 3.2.4 前端展示方案
- 布局：将现有 `.equity-card` 升级为收益模块主卡，左侧/顶部为**账户总资产**，紧邻为**收益率切换器 + 收益率数字**；右侧仍为 5 个统计卡（总盈亏/总胜率/总平仓/总委托/总佣金）。
- 交互：切换器（日/周/月）切换时，**收益率**与**总览**（overview）同步按该 `type` 重新拉取；净资产卡为实时快照不随切换变化。
- EMPTY/loading：净资产、收益率、各统计卡独立空态；任一请求失败仅该卡回退 "--"，不阻塞整页。

#### 3.2.5 边界条件
- `total_pnl` 为负时以红色/负号展示；`commission` 恒为支出（负值）沿用现有红字提示。
- 收益率切换器与顶部导航报表切换器**职责分离**：顶部导航切趋势图报表类型，收益模块切换器专门切收益率口径，两者独立但可联动刷新 overview。

---

### 3.3 模块③：AI 监控模块

#### 3.3.1 功能描述
新增独立区域，三类信息：
A. **各策略 ai-tuner 周度调优执行情况**：执行时间、状态（成功/跳过/失败）。
B. **月度资金分配**：当月/最近一期各策略保证金分配（金额、比例）。
C. **最近优化建议明细**：参数变更方向、是否已应用/待审批。

#### 3.3.2 数据来源与口径

**A. 周度调优执行情况**
- 现状：`ai_tuner/scheduler/weekly_job.py` 每周日 23:55 执行，对每个策略返回结果语义为 `"success" / "skip" / "error"`，但**未持久化到独立执行记录表**（仅写入 `trading.strategy_memory` 的建议、及发送通知）。
- **变更（新增）**：新增 `public.ai_tuner_runs` 表；`weekly_job.run_weekly_tuning()` 在本次执行结束时，为每个策略写入一条执行记录（成功/跳过/失败 + 时间）。
- 看板读取 `public.ai_tuner_runs` 近 N 周记录，按策略分组展示时间线。

**表结构（新增）`public.ai_tuner_runs`**
```
id           SERIAL PK
run_key      VARCHAR(40)            -- 一次周度调优批次标识（如 2026-09-13）
strategy_id  VARCHAR(32)
strategy_name VARCHAR(64)
status       VARCHAR(10)            -- 'success' | 'skip' | 'error'
executed_at  TIMESTAMP              -- 北京时间该策略执行结束时间
created_at   TIMESTAMP DEFAULT NOW()
```

**B. 月度资金分配**
- 来源：`public.capital_allocation`（真实存在），取其最新一条 `status='active'` 记录。
- `entries`（JSONB）数组内含真实字段：`strategy_id, strategy_name, realized_pnl, initial_capital, return_rate, rank, allocated_ratio, allocated_amount, occupied_amount`。其中 `occupied_amount`（家庭级占用保证金，USDT）为看板在返回时实时计算填充（统一复用 `_entry_family_margin` 公共方法）。
- 展示每策略 `strategy_name`、`allocated_amount`（分配金额）、`allocated_ratio`（比例 %）、`return_rate`、`rank`、`occupied_amount`（占用金额）。

**C. 最近优化建议明细**
- 来源：`trading.strategy_memory`（真实存在）最近 N 条，读取 `ai_suggestions`（JSONB，含 `adjustments` 参数变更）、`is_applied`、`is_rejected` 字段。
- 展示：策略名、建议时间、参数变更项（`adjustments` 为字符串文案列表，如 `["scoring=12","threshold=0.5"]`，由 `ai_suggestions` 展开为纯文本）、状态标签（已应用 / 待审批 / 已驳回）。

#### 3.3.3 接口设计（**新增**）

```
GET /api/ai-monitor?weeks=8&limit=10
```
响应 data：
```json
{
  "tuning_runs": [
    {"run_key":"2026-09-13","strategy_id":"btc_eth","strategy_name":"MTPCS策略",
     "status":"success","executed_at":"2026-09-13T23:56:10"}
  ],
  "capital_allocation": {
    "month":"2026-09-01","total_capital":"360.00","strategy_count":3,
    "entries":[
      {"strategy_id":"btc_eth","strategy_name":"MTPCS策略","allocated_amount":"129.60",
       "allocated_ratio":"0.36","return_rate":"3.2","rank":1,"occupied_amount":"88.40"}
    ],
    "status":"active"
  },
  "recent_suggestions": [
    {"strategy_id":"btc_eth","strategy_name":"MTPCS策略","created_at":"...",
     "adjustments":{"param_x":12,"param_y":0.5},"is_applied":true,"is_rejected":false}
  ]
}
```

#### 3.3.4 前端展示方案
- 位置：首页新增"AI 监控"板块（`section-header`），内含三个卡片/分栏：
  - 调优执行列表（近 8 周，策略 × 时间线，状态色块：成功绿、跳过灰、失败红）。
  - 月度资金分配表（本期 + 可选历史各期）。
  - 最近优化建议列表（near-real，最新 10 条）。
- EMPTY：`tuning_runs` 为空显示"暂无调优记录"；`capital_allocation` 为空显示"暂无资金分配"；`recent_suggestions` 为空显示"暂无建议"。
- loading：各卡独立 loading。

#### 3.3.5 边界条件
- `weeks` 参数上限 26，默认 8；`limit` 上限 20，默认 10。
- 某策略本周"跳过/成功"但 `strategy_memory` 无新记录属正常（skip 不一定写 memory）；**周度执行状态一律以 `ai_tuner_runs` 为准**，避免歧义。
- 建议状态标签优先级：`is_applied=true`→"已应用"；`is_rejected=true`→"已驳回"；否则→"待审批"。

---

### 3.4 模块④：策略概览增强

#### 3.4.1 功能描述
在现有策略概览卡片指标（`order_count / fill_count / closed_count / win_rate / total_pnl`）基础上
**追加**：
- **当前持仓订单量**（该策略当前未平仓的正持仓币种数/订单数）
- **持仓总保证金**（该策略当前正持仓占用的保证金，USDT）

#### 3.4.2 数据来源
**现状缺口**：当前数据库无法直接给出"策略当前正持仓占用保证金"，需策略侧上报。

**新增表 `trading.strategy_open_positions`（新增）**：
```
strategy_id  VARCHAR(32)
symbol       VARCHAR(20)
margin       DECIMAL(20,8)          -- 当前该(c策略,币种)占用保证金
quantity     DECIMAL(20,8)
updated_at   TIMESTAMP
PRIMARY KEY (strategy_id, symbol)
```

**策略上报改造点（明确列出，需各策略容器配合）：**
- 各策略在**开仓成交**时，向 `trading.strategy_open_positions` UPSERT `(strategy_id, symbol, margin, quantity)`；
- 在**平仓/全部了结**时 DELETE 对应 `(strategy_id, symbol)` 行；
- 在**部分减仓/加仓**时 UPDATE `margin/quantity`；
- 策略 ID 与看板 `_STRATEGY_KEY_MAP` 的 key 保持一致：`btc_eth / btc_eth_aggressive / new_coin / hrs / grid`。

**聚合口径（看板侧）：**
- 单策略：`open_position_count = COUNT(*)`，`open_margin = SUM(margin)`，其中仅统计 `margin > 0` 的行。
- 若某策略本次看板请求范围无上报或表无数据 → 两个新指标返回 `0`（有明确默认值），前端展示 `0`。

#### 3.4.3 接口设计（**修改**现有）

现有 `GET /api/overview?type=...` 与 `GET /api/strategies?type=...` 返回的 `strategies[]` 每一项**追加字段**：
```
"open_position_count": 3,     // 新增
"open_margin": "240.50"        // 新增，字符串 USDT
```
对应 `models/schemas.py` 的 `StrategySummary` **新增两个字段**（`: str` 类型与现有 `total_pnl` 保持一致风格）。
同时 `GET /api/strategies/{strategy_id}`（StrategyDetailData）一并追加上述两个字段。

#### 3.4.4 前端展示方案
- 策略卡片 `.strategy-card` 指标区新增两个指标项：`持仓量`、`持仓保证金`。
- 展示格式：持仓量为整数，持仓保证金保留 2 位小数 USDT。
- 卡片在无持仓时显示 `0` 与 `0.00`，保持布局稳定（不闪动）。

#### 3.4.5 边界条件
- 策略容器未完成上报改造期间，字段返回 0，卡片照常渲染（降级不报错）。
- 同一 `(strategy_id, symbol)` 只保留一行，避免重复累加。

---

### 3.5 模块⑤：风控模块

#### 3.5.1 功能描述
从 `shared/capital_manager.py` 提炼风控数据，并追加可采集的监控指标，在看板集中展示与预警。

#### 3.5.2 指标清单（来源 / 口径 / 展示）——以可采集为准

`shared/capital_manager.py` 提供的可读方法：
- `get_total_margin_limit()` → 持仓总保证金上限（优先月度分配 `capital_limits.monthly_limit`，兜底 `trading.total_position_margin_limit`）。
- `get_account_ratio_cap()` → 总持仓保证金占账户权益的比例阈值（`position_sizing.total.account_ratio_cap`）。
- `get_allocated_ratio()` → 分配比例。
- `can_open_position(current_positions_value, new_position_value)` → 是否可开新仓（布尔）。

| 指标 | 来源 | 计算口径 | 展示方式 |
|---|---|---|---|
| 当前持仓总保证金 | `trading.strategy_open_positions` SUM(margin)（新增表，模块④） | `Σ margin across all strategies` | 数字卡 USDT |
| 持仓上限 | `capital_manager.get_total_margin_limit()` | 直接读取 | 数字卡 USDT |
| 上限占用率 | 上三者 | `当前持仓总保证金 / 持仓上限`，保留 1 位小数 % | 进度条 + 百分比 |
| 权益比例占用率 | 净资产 + `get_account_ratio_cap()` | `当前持仓总保证金 / 当前净资产`；阈值=`account_ratio_cap` | 进度条，超阈值标红 |
| 可用开仓额度 | 上限 - 当前持仓总保证金 | `get_total_margin_limit() - Σmargin`，非负取 0 | 数字卡 USDT |
| 可用开仓比例 | 上者 | `可用开仓额度 / 持仓上限` | 数字卡 % |
| 逼近阈值提醒 | 占用率 | **逐策略判定**：对 `account_ratio_caps` 中每个策略单独计算占用并对其各自 cap 判定——比例型（cap<1，如 btc_eth=0.3、hrs=0.2）用「占用 / 净资产」；绝对额型（cap≥1，如 new_coin=150，即其 config.yaml 的 `total_position_margin_limit`）用「占用 / 绝对额上限」。任一策略达到 cap×0.8 则 `approaching_threshold`，任一策略达到 cap 则 `threshold_exceeded`（消除此前"取各 cap 最小值当全局占用率门槛"导致占用率不高却误报的问题） | 色条/角标警告文案 |
| 最近止损触发次数 | `trading.trade_records` | 近 N 天（配置，默认 7）内 `close_reason='STOP_LOSS'` 的平仓记录条数（由各策略在真实止损平仓点调用 `trade_logger.log_stop_loss()` 打标写入；grid 策略无自动止损平仓闭环，**不计入**） | 数字卡 + 趋势 |
| 连续亏损提醒 | 各策略日盈亏序列（复用 overview/trend 或快照增量） | 连续 `pnl < 0` 的天数 ≥ 阈值（默认 3） | 文案级提醒 |
| 大额回撤提醒 | 净资产快照 | 单日回撤（权益较前日下降比例）≥ 阈值（默认 5%） | 文案级提醒 |

**口径约束与来源统一：**
- "持仓上限/占用比例"类阈值的**权威来源 = capital_manager 读取的策略配置**，看板不自行硬编码（遵守项目禁止硬编码规范）。
- 若 `get_total_margin_limit()` 返回 `None`（未配置）→ 该卡显示 "--"，并提示"未配置持仓上限"。
- 止损统计的判定依据为 `trading.trade_records.close_reason='STOP_LOSS'`（统一止损打标机制），由已接入打标的策略（btc_eth / new_coin / hrs）在真实止损平仓点写入；grid 策略未接入，不计入。已接入策略中，止盈类（TP1/TP2/TRAILING_STOP）与时间止损流程不打此标记。

#### 3.5.3 接口设计（**新增**）

```
GET /api/risk?days=7
```
响应 data：
```json
{
  "total_position_margin": "240.50",
  "margin_limit": "360.00",
  "limit_occupancy": 66.81,
  "account_ratio_caps": {"btc_eth": 0.30, "hrs": 0.20, "new_coin": 0.25, "grid": 0.30},
  "equity_ratio_occupancy": 33.18,
  "available_margin": "119.50",
  "approaching_threshold": true,
  "threshold_exceeded": false,
  "recent_stop_count": 2,
  "recent_stop_trend": [{"date":"09-05","count":1},{"date":"09-08","count":1}],
  "consecutive_loss_days": 2,
  "max_drawdown_period": "2026-09-08",
  "drawdown_pct": 4.2,
  "daily_drawdown_pct": 5.0,
  "updated_at": "2026-09-10T23:31:00"
}
```
> 字段口径（与实现对齐）：
> - `account_ratio_caps`：字典，键为策略 ID，值为各策略的占用阈值（来自 `risk.yaml` 的 `account_ratio_caps`，由各策略 config 自动生成）。取值分两种语义：**比例型**（cap<1）为「占用 / 净资产」的比例阈值，如 btc_eth=0.3、hrs=0.2；**绝对额型**（cap≥1）为占用保证金的绝对额上限（USDT），如 new_coin=150（即其 config.yaml 的 `total_position_margin_limit`）。判定按上述逐策略规则进行，详见 3.5.2 表「逼近阈值提醒」。
> - `limit_occupancy` / `equity_ratio_occupancy`：后端以百分比小数保留 2 位（round），前端展示时再 `toFixed(1)`。
> - 预警用 `approaching_threshold` / `threshold_exceeded` 两个布尔表达，无独立的连续亏损 / 回撤预警布尔。
> - 回撤相关为 `max_drawdown_period`（最大回撤发生日期）+ `drawdown_pct`（最大单段回撤百分比）+ `daily_drawdown_pct`（单日回撤预警阈值，配置）。

#### 3.5.4 前端展示方案
- 位置：新增"风控"独立板块（`section-header`），含数字卡 + 进度条卡 + 提醒文案区。
- 占用率用进度条，接近阈值用橙色、超限用红色；正常绿色。
- 提醒项（逼近阈值 / 连续亏损 / 大额回撤）以醒目角标或顶部提示条呈现，normal 时不显示。
- EMPTY：`margin_limit` 为 null 显示 "--" 与提示；`recent_stop_*` 无数据显示 0。

#### 3.5.5 边界条件
- 占用率分子为 0 → 0%；分母为 0/None → "--"。
- `available_margin` 计算为负时归 0（已超限）。
- 连续亏损 / 回撤阈值需走配置（`.env` 或 dashboard config），不得硬编码。

---

## 4. 技术方案要点

### 4.1 净资产快照定时任务如何架设
- **方案（推荐）**：在 dashboard 后端（`dashboard/backend/main_docker.py` 的 `lifespan`）集成 **APScheduler AsyncIOScheduler**（需新增依赖声明到 `requirements.txt`），注册 cron job：北京时区 `23:30` 每天执行 `snapshot_equity()`。
- job 逻辑（**新增** `dashboard/backend/services/equity_snapshot_job.py`）：
  1. 调用 `DataService.get_account_equity()`；
  2. 计算北京今日日期 `snapshot_date`；
  3. `INSERT ... ON CONFLICT (snapshot_date) DO UPDATE` 写入 `public.equity_snapshot`；
  4. 失败记日志并告警；退出不阻塞。
- 备选：dashboard 容器 crontab 定期调用一个 CLI 入口脚本。两者选其一，PRD 倾向 APScheduler（生命周期内自管理）。
- 时区：统一 `Asia/Shanghai` 校准（`datetime.now(BEIJING_TZ)`，与现有 `data_service_docker.py` 保持一致）。

### 4.2 快照表结构 DDL（**新增** `public.equity_snapshot`）
```sql
CREATE TABLE IF NOT EXISTS public.equity_snapshot (
    id              SERIAL PRIMARY KEY,
    snapshot_date   DATE NOT NULL UNIQUE,
    total_equity    DECIMAL(20,8) NOT NULL,
    available_balance DECIMAL(20,8) NOT NULL,
    open_positions  INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_equity_snapshot_date ON public.equity_snapshot(snapshot_date);
```
> 授权：需 `GRANT` 给看板连接用户（`trading_user`）读写权限；DDL 放置于 `database/postgres/init-scripts/`（幂等）。

### 4.3 策略上报持仓的接口与表
- 表：`trading.strategy_open_positions`（见 4.4.2，**新增**）。
- 写入方：各策略容器；读取方：dashboard。
- 为避免 dashboard 反向依赖策略容器 HTTP 可用性，采用**策略直接 DB UPSERT** 的轻量方案（复用各策略已有 `shared.database` 连接能力），不强制走 dashboard POST；若后续需要也可提供 `POST /api/positions/report` 兜底（**新增**、可选）。
- 归属逻辑：`strategy_id` 使用看板 `_STRATEGY_KEY_MAP` 的 key（`btc_eth/btc_eth_aggressive/new_coin/hrs/grid`），聚合 `GROUP BY strategy_id`。

### 4.4 ai_tuner 执行记录
- 表：`public.ai_tuner_runs`（见 4.3.2，**新增**）。
- 改造点：`weekly_job.run_weekly_tuning()` 内，在每个策略分支（success/skip/error）调用新增写入函数落一条记录（`run_key` 用本次执行的周日日期；每次执行开头生成统一 run_key）。
- 看板并读 `strategy_memory`（建议）与 `capital_allocation`（资金分配）。

### 4.5 性能 / 缓存
- 快照写库在 23:30 低频执行，无性能压力。
- 收益率接口 `GET /api/account/returns`：当日数据多次请求可复用现有 `CacheService`（短 TTL，如 `cache_ttl_account=30s`），历史快照直接用 DB 索引查询。
- `trading.strategy_open_positions` 数据量小（策略×币种），`(strategy_id,symbol)` 主键即索引，直接聚合。
- `ai-monitor` / `risk`：低频变化（周/月/实时），接入 `CacheService`，`risk` 用短 TTL，`ai-monitor` 用中长 TTL（如 10 分钟）。

---

## 5. 非功能需求

| 类别 | 要求 |
|---|---|
| 数据准确性口径 | 收益率统一以**净资产快照增量**计算；快照日起于功能上线日；期初缺失一律 "--"。止损 `order_type` 过滤值需在开发时核对 `trading.trade_records` 真实枚举，防止过滤失效 |
| 缓存策略 | 复用现有 `CacheService`；新端点按变化频率配 TTL；禁止硬编码阈值（占用比例、连续亏损、回撤阈值均配置化） |
| 兼容性 | 现有 `/api/overview`、`/account/equity`、`/trend` 响应不破坏；`StrategySummary` 只增字段，不删不改旧字段，保证旧前端/旧客户端兼容 |
| 权限安全 | dashboard 延续现状对内提供读取接口，无鉴权（与现状一致）；快照/持仓上报仅内网可用；`ai_tuner_runs` 写入不暴露为公网 POST |
| 时区 | 全部按北京时间统一存储与展示 |
| 可用性 | 单模块数据源失败仅该卡 "--"，页面整体可用；定时任务失败自动记录下次补采 |

---

## 6. 验收标准（每个模块可测试验收点）

### 模块① 净资产快照与收益率
- [ ] 配置 APScheduler 后，到北京 23:30 落库成功；手动触发同名 job 二次执行不产生重复行（UPSERT 幂等）。
- [ ] 数据库存在 `public.equity_snapshot`，上线次日可有 ≥1 条记录。
- [ ] `GET /api/account/returns?type=daily`：有昨日快照时返回正确百分比；无昨日快照时 `yield_unavailable=true`。
- [ ] 周/月收益率：本周一/本月 1 号快照缺失时返回 `yield_unavailable=true`；存在时分母正确。
- [ ] 前端切换日/周/月，收益率数字与"/期初/净值"正确联动；历史不足显示 "--"。
- [ ] 除零与期初为 0 场景不报错、显示 "--"。

### 模块② 收益模块
- [ ] 首页核心区展示：账户总资产、收益率（可切换）、总盈亏、总胜率、总平仓数、总委托数、总佣金。
- [ ] 净资产卡为实时快照（不随日/周/月切换变化）；overview 随切换刷新。
- [ ] 任一统计卡请求失败仅该卡 "--"，不阻塞整页。

### 模块③ AI 监控模块
- [ ] 部署 `weekly_job` 改造后，`public.ai_tuner_runs` 有为每个策略成功/跳过/失败写入一条记录。
- [ ] `GET /api/ai-monitor` 返回 tuning_runs、capital_allocation（最新 active）、recent_suggestions 三块数据。
- [ ] 资金分配展示 `allocated_amount/allocated_ratio/return_rate/rank` 正确（对照 `capital_allocation.entries`）。
- [ ] 建议状态标签正确区分"已应用/待审批/已驳回"。
- [ ] 前端三块均有无数据空态。

### 模块④ 策略概览增强
- [ ] 策略完成上报改造后（造数/联调），`overview.strategies[i].open_position_count`、`open_margin` 正确聚合。
- [ ] 未上报时两字段返回 0，卡片渲染 `0`/`0.00`。
- [ ] 前端卡片展示"持仓量、持仓保证金"两项。

### 模块⑤ 风控模块
- [ ] `GET /api/risk` 返回可用字段（持仓总保证金/上限/占用率/可用额度/止损次数等）。
- [ ] 占用率的分子=当前持仓总保证金，分母=capital_manager 上限并验证一致性。
- [ ] 占用率≥0.8×阈值时 `approaching_threshold=true`；≥阈值 `threshold_exceeded=true`（阈值来自配置）。
- [ ] 最近止损次数来自 `trade_records.close_reason='STOP_LOSS'`（各策略止损打标写入；grid 不纳入），数值可核对。
- [ ] `margin_limit=None`（未配置）时对应卡显示 "--"。
- [ ] 连续亏损/大额回撤阈值可配置，告警文案按阈值触发。

---

## 7. 里程碑 / 优先级划分

### 一期（P0 核心落地）
- 净资产快照与收益率（模块①：表 + 定时任务 + 收益率接口）
- 收益模块首页改造与切换（模块②）
- 策略概览字段与展示（模块④：含 `strategy_open_positions` 表 + overview 扩展 + 前端）
- AI 监控读取已有表（模块③）A2/B/C 部分（`capital_allocation`、`strategy_memory` 直接可读）

### 二期（P1 补齐）
- `ai_tuner_runs` 表 + `weekly_job` 改造落库（模块③ A1，调优执行时间线含成功/跳过/失败）
- 风控模块（模块⑤）全量指标与预警
- 各策略容器持仓上报改造联调完成（P0 已建表，P1 各策略接入）

### 三期（P2 后续）
- 连续亏损 / 大额回撤提醒深化（依赖更长快照历史）
- 风控历史趋势图、止损走势
- 可选的 `POST /api/positions/report` HTTP 上报兜底通道

---

## 8. 已知风险与依赖

| 风险 / 依赖 | 说明 | 缓解 |
|---|---|---|
| 策略改造需多容器发布 | 持仓上报要各策略容器同时改并同版本发布，存在版本漂移 | 双阶段：先建表 + dashboard 侧容错（缺字段回 0），再逐策略灰度接入 |
| 历史快照积累需时间 | 快照自上线日才积累，前期周/月收益率必显 "--" | 界面明确"积累中"提示；周/月收益率上线初期可接受空态 |
| 收益率依赖实时净资产稳定性 | 实时权益含未实现盈亏，23:30 采样点受行情瞬时影响 | 样本噪声属预期；如需更稳可后续改为日内多次均值（P2） |
| 按月/周上报的归一化难点 | 周/月收益率期初快照要求周一/月初有记录，跨周/跨月首日无快照则无法计算 | 严格按"期初快照缺失 → -- "口径执行，文档化说明 |
| capital_manager 依赖策略配置读取 | dashboard 容器需能读取各策略 `config.yaml`（volume 挂载），否则风控上限读空 | 部署时确保 dashboard 挂载策略配置目录；读取失败显示 "--" 兜底 |
| 止损 `order_type` 枚举 | `trade_records.order_type` 取值需核对，避免过滤漏判 | 开发期查询产品库中真实枚举并在文档/代码注释固化 |
| ai_tuner_runs 写入依赖 weekly_job | 若 weekly_job 未跑 / 失败，调优时间线为空 | 看板空态提示"本周无调优记录"，不虚报 |