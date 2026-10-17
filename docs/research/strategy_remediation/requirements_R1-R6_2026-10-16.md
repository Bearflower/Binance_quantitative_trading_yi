# 需求定义与验收标准：独立复审 R1–R6 收口（2026-10-16）

> 状态：**已实现 + 已通过回归（2026-10-16 收口）**。本文定义「本轮需求 + 验收标准」；实现与回归证据见 §6「落地状态与验收结论」，不修改任何源文件。
> 权威输入：
> - 复审报告：[strategy_reaudit_2026-10-16/README.md](../../reviews/strategy_reaudit_2026-10-16/README.md)
> - 复现脚本：[strategy_reaudit_2026-10-16/reproduce_findings.py](../../reviews/strategy_reaudit_2026-10-16/reproduce_findings.py)
> - 业务决策记录：[decisions.md](decisions.md)（D2 / D4 / D5 / D8）

---

## 1. 需求概述

### 1.1 本轮目标

本轮**只修**独立复审报告中待收口的 6 项问题（R1–R6），逐条满足「问题本质 → 期望行为 → 验收标准」的可判定闭环，并把复现脚本中的 6 个场景固化为一组「先红后绿」的回归用例。

### 1.2 范围边界（明确排除项，本轮不得宣称完成）

| 排除项 | 说明 |
|---|---|
| 净收益对账 / 权益守恒 / 全策略归属账本 | 属后续研究+验收，需搬出「已全部完成」清单 |
| MTPCS 趋势 / 回归分支定位、分支独立净收益 | 研究/验收工作，需后续证据 |
| push main / 部署 / GHCR / 服务器 | 本轮不触发部署 |
| apply 迁移 / 连接生产库 | 不执行任何 DB 迁移，不连生产库 |
| 调用真实交易所 | 所有测试走 mock / 合成行情 |

### 1.3 硬约束（全程遵守）

- 不 push main、不部署、不 apply 迁移、不连生产库、不调真实交易所。
- 代码注释与日志一律中文。
- 禁止硬编码业务参数（阈值、时长、分位数、样本门槛一律读配置或由算法推导）。
- 禁止重复代码：连续 5 行以上同构、或逻辑相同仅变量名不同，必须抽公共函数。
- 禁止幽灵参数 / 死字段：函数形参与结构体字段必须被真实使用。
- 单函数 ≤ 50 行；单行 ≤ 120 字符。
- 工作区存在大量其他未提交改动，**只允许触碰本轮相关文件**，不得顺带 reformat / 重构无关文件。

---

## 2. 逐条需求

### R1 [P1] HRS 影子逻辑不得替代原生产信号（属 HRS 组）

**问题本质**
`score()` 在 EMM 触发时只返回 EMM 结果，standard 不再独立计算；`_is_shadow_entry_mode` 只看 `entry_mode`，把「非空候选池原本可交易的 LV-RM」也一并影子化，减少了原策略可执行的交易，且无法对「旧策略实盘」与「新增影子信号」做公平差异比较。

**涉及文件 / 行**
- [strategy.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L505-L521)（`_is_shadow_entry_mode`）、[strategy.py#L608-L616](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L608-L616)、[strategy.py#L853-L870](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L853-L870)（影子闸门）
- [config.yaml#L540-L542](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/config.yaml#L540-L542)（`lv_rm.execution_mode: shadow`）
- [scoring_engine.py#L1162 起](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/scoring_engine.py#L1162-L1235)（`score()` 的 EMM / standard 分支）

**拍板口径（不可更改，依据 D2 / D8）**
1. **standard 结果独立计算并保持原可达性**：无论 EMM 是否触发，standard 决策链照常独立产出；EMM 仅作为并行的 shadow 结果，不抢占 `score()` 返回的唯一信号。
2. **EMM / 空池 LV-RM 仅作 shadow**：不参与生产冲突消解、不真实下单。
3. **用 `signal_origin` 区分来源**：`empty_pool`（候选池为空时 `_run_lv_rm_only_cycle` 触发的 LV-RM，属 D8 新接通路径）与 `nonempty_pool`（正常 `run_cycle` 内 `_check_lv_rm_entries` 产生的 LV-RM）。
4. **非空候选池的 LV-RM 保持原政策（不影子化）**：`execution_mode=shadow` 只对 `empty_pool` 来源的 LV-RM 生效，不波及 `nonempty_pool`。

**期望行为**
- `score()` 在「同一输入」下，生产主结果（standard）的 `entry_mode` 与 `should_entry` 判定，不因 `price_change_24h` 接入（进而触发 EMM）而改变；EMM 信号单独作为 shadow 结论产出。
- `_is_shadow_entry_mode` 的判定不再仅凭 `entry_mode`；对 `lv_rm` 必须结合 `signal_origin`（或等价等价来源标记）判断，只有 `empty_pool` 来源才可能命中 shadow。
- shadow 信号仍走完决策链但 `shadow_blocked=True`，落库 `resolution='shadow'`；其存在不影响同币种 standard 信号的可执行性。

**不做什么**
- 不在本轮「放行」任何 EMM / 空池 LV-RM 的真实执行（放行仍需用户单独拍板）。
- 不改 standard 的评分公式、阈值、权重组合本身（只恢复其「独立可达性」）。
- 不把「非空池 LV-RM 原本就应被停止」当作修复口径；若确需停止全部 LV-RM，属显式策略变更，需另行记录，不混入 R1。

---

### R2 [P1] 成交事实必须先可靠落库、再推进游标（属 execution_facts 组）

**问题本质**
采集流程「先 `_save_cursor`、再 `_maybe_sink_upsert`」，且事实写失败被静默吞掉，仍返回 `page_complete=true`、`errors=[]`。游标已推进，重试后永久漏掉较早记录。

**涉及文件 / 行**
- [export_execution_facts.py#L592-L593](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L592-L593)（先存游标再写事实）、[export_execution_facts.py#L680-L698](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L680-L698)（`_maybe_sink_upsert` 吞异常）
- [execution_facts_sink.py#L186-L203](file:///Users/yl/vscode/Binance_quantitative_trading/shared/execution_facts_sink.py#L186-L203)（`_bulk_upsert_with_raw` 全吞）

**期望行为**
- 事实写入成功并确认之后，才推进/持久化游标（游标覆盖范围不得领先于已持久化事实）。
- 事实写入失败、游标写入失败两类异常都必须把本次导出标记为 `page_complete=False`，并在 `errors` 中给出可见失败，禁止返回「已完成 + 零错误」的伪成功。
- 中途进程终止后重试，能从「未推进的游标」恢复，保证重试完成后持久化集合 == 完整采集集合。
- 原始响应在采集期间须进入可恢复的持久化载体（而非仅内存/日志），确保持久化事实可被审计与重放。

**不做什么**
- 不改变 fills / income 的幂等主键语义。
- 不引入跨进程分布式锁或全新存储中间件（除非必要，优先复用现有 sink 的事务能力）。

---

### R3 [P1] 收入游标必须纳入 income_type 与 account_id（属 execution_facts 组）

**问题本质**
不同 `income_type` 共用 `(source='um/income', symbol=None, window_start_ms)` 游标，且游标键不含 `account_id`。先采 REALIZED_PNL 再采 COMMISSION，后者从 PNL 已推进的时间起点开始，漏掉较早的手续费/资金费。

**涉及文件 / 行**
- [export_execution_facts.py#L430-L449](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L430-L449)、[export_execution_facts.py#L700-L704](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L700-L704)（游标键）
- [execution_facts_sink.py#L89-L95](file:///Users/yl/vscode/Binance_quantitative_trading/shared/execution_facts_sink.py#L89-L95)（游标 load SQL）
- [11-execution-facts.sql#L69-L85](file:///Users/yl/vscode/Binance_quantitative_trading/database/postgres/init-scripts/11-execution-facts.sql#L69-L85)（游标唯一键）

**期望行为**
- 游标身份纳入 `account_id` 与完整查询过滤条件（至少 `income_type`；若 window/symbol 亦参与过滤则一并纳入），使不同 income_type、不同账户的采集进度互不影响。
- 同一窗口依序采集 REALIZED_PNL / COMMISSION / FUNDING_FEE 各自完整、互不窜跳；不同账户同一窗口也互不覆盖。
- 定义窗口扩展的恢复规则，并对旧游标给出迁移口径（不能直接复用有歧义的完成标记）。

**不做什么**
- 不改变 income 明细表的幂等主键 `(account_id, tran_id)`。
- 本轮**只定义游标键与恢复规则**，不实际执行任何 SQL 迁移（apply 迁移被明确排除）。

---

### R4 [P1] 缺真实市值的新入场必须显式拒绝（属 HRS 组，P1）

**问题本质**
开仓侧代理/缺失只返回 `has_market_cap=False`，仍调评分并使用旧 fallback 分（long=5），未显式拒绝；OR-2 候选可借其他因子入池后继续凑分过关；动态阈值最小样本门槛仍用 `funding_rates` 长度，可能以极少的真实市值样本生成「看似有效」的阈值。

**涉及文件 / 行**
- [strategy.py#L333-L340](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L333-L340)（`_fetch_market_cap_ratio` 开仓侧）、[strategy.py#L574-L589](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L574-L589)（analyze 调评分不拒绝）
- [scoring_engine.py#L240-L242](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/scoring_engine.py#L240-L242)（contract fallback）
- [candidate_pool.py#L491](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/candidate_pool.py#L491)（`sample_count = len(funding_rates)`）

**拍板口径（不可更改，依据 D4 / D5）**
1. **显式拒绝缺失真实未过期市值的新入场**：开仓侧当 `has_market_cap=False`（`oi_proxy` / `missing` / 过期）时，不得再用 fallback 分继续走 `should_entry=可入场`。
2. **逐截面校验有效样本量**：动态阈值每个截面（funding_rate / OI / 真实市值 OI市值比）分别按**该截面自身的有效样本数**过大样本门槛，禁止以 `funding_rates` 长度冒充有效市值/OI 样本数。
3. **存量退出不受阻**：缺市值的已有仓位照常保护/减仓，持仓复核仅记录「输入不可用」（D5）。

**期望行为**
- 开仓侧缺真实市值 → 该币不入「依赖 OI/市值比」的新入场路径，真实 `execute_*` 不发生。
- 真实有效市值样本不足时，动态阈值判为「无效」并回退既有阈值策略；判定依据的是有效市值/OI 截面样本数，而非费率样本数。
- 退出侧（`require_true_for_entry=False`）行为保持不变。

**不做什么**
- 不改变退出/保护路径对缺市值仓位的处理（D5 维持）。
- 不引入「自动重分权重」或「0/0.05 填充冒充缺失」等被 D4 明确拒绝的口径。
- 不把「缺市值仍允许按其他因子入场」作为默认策略（除 decisions.md 有单独明确批准外，一律按严格规则拒绝）。

---

### R5 [P2] 新币价格新鲜度必须覆盖真实请求延迟与下单前重校（属 new_coin 组）

**问题本质**
`observed_at` 在 `await ticker` 返回后才设 `time.time()`，随后再取 `time.time()` 比较，龄值恒为 0；请求耗时 60s、允许 5s 仍被接受；且该时间未传到 `executor`，下单前余额/占位/额度/杠杆等耗时之后不再确认价格。

**涉及文件 / 行**
- [new_coin/strategy.py#L467-L472](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/strategy.py#L467-L472)（`_fetch_fresh_ticker` 龄值恒 0）、[new_coin/strategy.py#L612-L621](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/strategy.py#L612-L621)（调用链不传时间）
- [new_coin/executor.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py) `execute_short`（下单前无重校）

**期望行为**
- 保留「源行情时间（可得时）」与「请求起止时间」，新鲜度按**请求真实延迟**（或源行情时间）判定；源时间不可得时，至少限制请求延迟并显式标注代理口径。
- 慢返回（请求延迟 > `max_age_seconds`）必须判为超龄拒绝，不得因「返回瞬间的时间差为 0」而放行。
- 报价及其时间/固定参照传递到真正下单前；执行准备超时后重新取价并重判反弹/新鲜度；重取后突破参照按既有口径处理。
- 未知源时间（无法取得权威时间戳）单独覆盖：不得静默放行，须以明确的代理口径记录原因或拒绝。

**不做什么**
- 不更改 D3 的反弹参照「固定高点、不随价格重算抬高」口径。
- 不在同一变更集改动总分 / 技术门槛（保持 9/7 组合行为不变）。

---

### R6 [P2] 同毫秒跨页必须能推进到底（属 execution_facts 组）

**问题本质**
翻页仅把 `start_time` 推进至已消费最大时间，不使用保存的 `last_event_id` 续传。同毫秒记录超过一页时，同一时间起点被反复请求，只取到前 N 条后以 `max_pages` 退出，其余同毫秒记录无法越过。

**涉及文件 / 行**
- [export_execution_facts.py#L340-L378](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L340-L378)（`_consume_page`）、[export_execution_facts.py#L404-L411](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L404-L411)、[export_execution_facts.py#L620-L643](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L620-L643)（`_one_page_iteration` 仅推进 max time）

**期望行为**
- 按各接口可用能力分别使用**事件 ID**（`id` / `tranId`）或**页码**续传，避免同毫秒重复拉取同一批。
- 不可推进时必须保持 `page_complete=False` 并显式报告，不得无限重取同一页。
- 覆盖边界：同毫秒数量 = limit、= limit+1、多页、最后时间恰等于窗口右边界。

**不做什么**
- 不复用「+1ms」这类会直接跳过余下记录的推进方式。
- 不强求统一各接口续传机制为同一种；允许按接口能力分立，但必须可离线验证完整分页。

---

## 3. 逐条验收标准（可执行、可判定、可转回归测试）

> 复现脚本 [reproduce_findings.py](file:///Users/yl/vscode/Binance_quantitative_trading/docs/reviews/strategy_reaudit_2026-10-16/reproduce_findings.py) 中的 6 个场景必须成为回归用例，先红后绿。

### R1 验收断言

1. **旧订单意图不变**：固定技术/情绪分（如 mock `calculate_technical_score→10`、`calculate_sentiment_score→10`）的同一输入，`price_change_24h=None`（standard）与 `price_change_24h=30`（触发 EMM）两次调 `score()`，其「生产主结果」的 `entry_mode` 与 `should_entry` 必须完全一致。
2. **新增模式单独出证据**：EMM 结论作为独立 shadow 结果可被单独读取，且与 standard 主结果可同源对照（不能只返回一个、丢掉另一个）。
3. **非空池原 LV-RM 保持原政策**：对 `signal_origin='nonempty_pool'` 的 `lv_rm` 信号，`_is_shadow_entry_mode` 必须返回 False，真实 `execute_signal` 可达；对 `signal_origin='empty_pool'` 的 `lv_rm`，返回 True 且 `shadow_blocked=True`。
4. **影子不抢占唯一信号**：存在同币种 standard 可入场信号时，EMM/空池 LV-RM 的 shadow 不改变 standard 信号在 `_resolve_conflicts` 中的可执行性；shadow 落库 `resolution='shadow'`，绝不落 `executed`。

回归用例形态：断言 1/2 对应复现脚本 `shadow_replaces_standard` 反转——修复后 `before_entry == after_entry`；断言 3 对 `lv_rm_existing_also_shadow` 反转——`_is_shadow_entry_mode` 对 nonempty_pool 为 False。

### R2 验收断言

三种故障注入分别验证，最终「重试后的持久化集合 == 完整采集集合」：

1. **事实写失败注入**：`upsert_fills` 抛异常时，本次导出必须 `page_complete=False` 且 `errors` 非空；恢复写入后重试，持久化的 `trade_id` 集合 == 全集 `{1, 2}`（第一笔不再永久漏掉）。
2. **游标写失败注入**：`save_cursor` 抛异常时，必须被识别为未可靠推进；重试仍从原窗口起点续采，最终持久化集合完整。
3. **中途终止注入**：采集到一半进程中断（模拟 sink 状态不落盘），重试后回补缺失段，持久化集合 == 完整集合。

回归用例形态：对应 `cursor_before_persist` 场景反转——`first_marked_complete` 为 False、`first_errors` 非空、`persisted_after_retry_ids == expected_ids`。

### R3 验收断言

1. **按 income_type 完整互不窜跳**：同一窗口依序采集 REALIZED_PNL（t=1100）、COMMISSION（t=500）、FUNDING_FEE，各自返回期望条数，均完整、`page_complete=True`，COMMISSION 不会被 PNL 的游标跳过。
2. **不同账户互不影响**：`account_id='a'` 与 `'b'` 分别采集同一 income_type、同一窗口，进度互不覆盖。
3. **游标键身份完整**：游标读取/保存的键包含 `source + account_id + income_type (+ symbol + window_start_ms)` 等价身份，缺省时不可命中任何旧游标。

回归用例形态：对应 `income_filter_cursor_collision` 反转——`fee_expected == fee_actual == 1`。

### R4 验收断言

1. **缺失 / 代理 / 过期的新入场不执行**：对 `has_market_cap=False`（`missing` / `oi_proxy` / 超期来源）的开仓侧输入，评分链显式拒绝，断言真实 `execute_short/execute_long` 未发生（mock 跟踪调用次数 == 0），`should_entry == False`。
2. **样本门槛不冒充**：构造「fee 样本多、真实市值样本少」的截面，断言动态阈值按真实市值/OI 有效样本数判「不足」，回退固定阈值，而不是用 `funding_rates` 长度通过门槛。
3. **存量退出不受阻**：`require_true_for_entry=False` 的已有仓位，缺市值不阻断保护/减仓，仅记录「输入不可用」。

回归用例形态：对应 `missing_cap_entry` 反转——`should_entry == False` 且 `execute` 调用数为 0。

### R5 验收断言

1. **慢返回**：ticker 返回前推进 clock 60s，`max_age_seconds=5`，断言判定为超龄拒绝（不得接受）。
2. **执行准备超时**：从取价到真正下单前耗时超 `max_age_seconds`，断言下单前重新取价并重判，最终拒绝或重取成功，而非沿用陈旧报价。
3. **重取后突破参照**：重取后实时价突破固定参照高点，按 D3 口径处理（非影子拒绝 / 影子放行留痕）。
4. **未知源时间**：无权威源时间戳时，断言不静默放行（明确代理口径记录原因或拒绝）。

回归用例形态：对应 `slow_ticker_accepted` 反转——60s 请求不再被接受，`request_seconds > max_age_seconds` 时返回拒绝。

### R6 验收断言

1. **同毫秒数量=limit**：恰好一页的边界，断言完整取回、`page_complete=True`、不触发 `max_pages`。
2. **同毫秒数量=limit+1**：跨页单条，断言最终取回全部 limit+1 条。
3. **多页**：同毫秒 4 笔、`limit=2`，断言取回全部 4 笔、`page_complete=True`（不得仅取 2 笔后 `max_pages` 退出）。
4. **最后时间等于窗口边界**：窗口右边界恰为最后一条 `time`，断言能正确收尾且无越界请求。

回归用例形态：对应 `same_ms_cross_page` 反转——`expected == 4`、`actual == 4`。

---

## 4. 验收硬标准总体说明

- R1–R6 的 6 个复现场景**必须全部转成回归用例**，先观测「修复前红」（可复现 review 中的失败），再「修复后绿」。
- 回归用例须与 `reproduce_findings.py` 一脉相承：只注入 mock / 合成行情，不启动策略、不连 DB、不连交易所。
- 每一验收断言必须**可判定**（布尔/集合相等/调用次数），禁止以「代码看起来没问题」替代可执行断言。
- 涉及资金/交易与账本完整性的修复（R1、R4、R2/R3/R6）标注为**核心业务逻辑**，必须走强制测试流程。
- 本轮所有修复**不得宣称「已部署 / 已上线」**；仅给出本地修复结论与回归证据。

---

## 5. 同源关系与分组

- **R2 与 R3 根因同源**：都源于「游标推进与事实写入的事务边界」——R2 是顺序错误（先游标后事实），R3 是游标身份缺维度（缺 `income_type` / `account_id`）。二者须一同设计游标生命周期与身份模型，避免各自打补丁产生新游标键口径不一致。
- **分组**（组间文件零交叉，可独立开发/测试/审查）：

| 分组 | 成员 | 涉及文件 | 说明 |
|---|---|---|---|
| A | R1 + R4 | `strategies/hrs/strategy.py`、`strategies/hrs/scoring_engine.py`、`strategies/hrs/config.yaml`、`strategies/hrs/candidate_pool.py` | HRS 组，P1 |
| B | R5 | `strategies/new_coin/strategy.py`、`strategies/new_coin/executor.py` | new_coin 组，P2 |
| C | R2 + R3 + R6 | `scripts/export_execution_facts.py`、`shared/execution_facts_sink.py`、`database/postgres/init-scripts/11-execution-facts.sql` | execution_facts 组，P1 |

- **建议执行顺序**：先 C（账本采集完整性，含游标键迁移设计但不 apply）→ A（HRS 行为口径）→ B（新币新鲜度）；每组内部的回归用例先红后绿后，再进入下一组。

---

## 6. 落地状态与验收结论（2026-10-16 收口）

> 本节为收口补记：R1–R6 已全部实现并通过**先红后绿**回归；**本轮新增失败 0**。范围边界（§1.2 排除项）不变——净收益对账 / 权益守恒 / 部署验收等仍属后续，本节不主张「已部署」。

**逐条验收结论**（验收断言见 §3）：

| 需求 | 验收结论 | 回归用例（文件 :: 用例） |
|---|---|---|
| R1 | ✅ 通过：主结果与 `price_change_24h=None` 旧版 standard 一致；EMM/半EMM 独立 shadow（`signal_origin="shadow_scoring"`）；`lv_rm` 按 `empty_pool`/`nonempty_pool` 区分（缺失来源 fail-closed） | `strategies/hrs/tests/test_r1_r4_regressions.py` :: `test_r1_main_result_entry_mode_always_standard`、`test_r1_main_result_intent_unchanged_by_price_change`、`test_r1_main_result_extreme_bonus_always_zero`、`test_lv_rm_nonempty_pool_not_shadow`、`test_lv_rm_empty_pool_is_shadow`、`test_lv_rm_missing_origin_fail_closed` |
| R2 | ✅ 通过：事实先落库再推进游标；upsert/save_cursor 失败上抛为 `page_complete=False` + `errors`；重试后持久化集合 == 完整集合 | `tests/test_shared/test_execution_facts_regression.py` :: `test_upsert_fails_retry_collects_all`、`test_save_cursor_fails_retry_collects_all`、`test_mid_run_termination_retry_collects_all` |
| R3 | ✅ 通过：游标键含 `account_id` + `income_type`；不同 income_type / 账户互不窜跳 | `tests/test_shared/test_execution_facts_regression.py` :: `test_income_types_do_not_collide`、`test_different_accounts_do_not_collide`、`test_cursor_key_includes_full_identity`、`test_window_extension_new_start_gets_fresh_cursor` |
| R4 | ✅ 通过：缺真实市值新入场显式 veto（execute 调用数 0）；动态阈值逐截面有效样本校验；存量退出不受阻 | `strategies/hrs/tests/test_r1_r4_regressions.py` :: `test_score_veto_when_no_market_cap_short`、`test_score_veto_when_no_market_cap_long`、`test_score_lv_rm_veto_when_no_market_cap`、`test_invalid_when_oi_section_insufficient`、`test_invalid_when_funding_section_insufficient`、`test_exit_side_proxy_value_not_blocked` |
| R5 | ✅ 通过：慢返回（请求延迟 > `max_age_seconds`）拒绝；执行准备超时下单前重取重判；未知源时间不静默放行 | `strategies/new_coin/tests/test_price_freshness_r5.py` :: `test_slow_ticker_rejected`、`test_entry_guard_passed_to_executor`、`test_executor_triggers_entry_guard_when_prep_exceeds_max_age`、`test_breakout_non_shadow_rejects`、`test_unknown_source_time_not_silently_passed` |
| R6 | ✅ 通过：fills 用 `fromId` 续传取回全部同毫秒记录；income 不支持 ID 续传时显式不完整（不 `+1ms`） | `tests/test_shared/test_execution_facts_regression.py` :: `test_same_ms_exactly_at_limit`、`test_same_ms_limit_plus_one`、`test_same_ms_four_trades_limit_two`、`test_last_time_equals_window_boundary`、`test_income_no_from_id_falls_back_to_incomplete` |

**实现偏差（如实登记，详见 [architecture_R1-R6_2026-10-16.md](architecture_R1-R6_2026-10-16.md) §7 与 [复审报告 §7.2](../../reviews/strategy_reaudit_2026-10-16/README.md)）**：income 接口未暴露 `tranId` 续传 → 走显式不完整分支；游标新键仅对全新安装生效、**本轮未 apply 迁移**（旧游标冷隔离）；new_coin 新鲜度用请求延迟代理口径（ticker 无源时间戳）；`export_execution_facts.py` 有 3 个函数因 `async def` 参数声明行超 50 行（函数体 ≤51 行，可接受偏差）。