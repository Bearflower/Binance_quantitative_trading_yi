# 策略修复复审（2026-10-16）

结论：**不通过“全部问题已修复”的验收。** 已完成多处真实修复，但仍有6项需要处理的调用链／采集完整性问题。此结论针对本地当前工作区，不能推断线上已经受到这些问题影响。

本轮只读审查生产代码，执行本地mock测试；新增本目录审查证据，没有修改策略、配置、数据库或部署。HEAD为 `bc1df60`（完整SHA见 manifest.json），审查包含未提交和未跟踪实现，不能只用HEAD代表版本。未运行旧审计脚本，避免覆盖原始9项问题的证据。

## 1. 发现（按处理优先级）

### R1 [P1] 影子逻辑替代原有真实策略，未做到旁路比较

定位：`strategies/hrs/strategy.py:505–521`、`:608–616`、`:853–870`；`strategies/hrs/config.yaml:540–542`；`strategies/hrs/scoring_engine.py:1162` 起。

当前score在EMM触发时只返回EMM结果，原先standard结果不再独立计算。analyze把它作为唯一信号，execute_signal再以shadow拦住。独立探针固定技术/情绪分，原输入price_change_24h=None时standard得10分、可入场；接通30%涨幅后变成emm得7.75分、仍符合入场，但被shadow拦住。**影子功能减少了原策略原本可以执行的交易。** 此外接通price_change_24h也会影响标准技术分的极端行情加分，不只是EMM。

LV-RM同样存在范围扩张：配置和_is_shadow_entry_mode只看entry_mode，非空候选池中原来可交易的LV-RM也一并影子化；D8要求影子观察的是新接通的空池路径。这样无法把“旧策略实盘”与“新增逻辑影子”作公平差异比较。

建议：保留原生产决策分支，新增独立shadow结果/意图，不让shadow参与生产冲突消解或抢占唯一信号；用signal_origin区分empty_pool/nonempty_pool。若确实要停止全部LV-RM，应作为显式策略调整记录，不把它称为修复零行为变化。

验收补充：同一输入启用shadow前后，旧版真实订单意图完全一致；新增模式单独出证据；非空原LV-RM保持原政策。

### R2 [P1] 游标先于事实落库，失败后重试永久漏掉较早记录

定位：`scripts/export_execution_facts.py:592–593`、`:680–698`；`shared/execution_facts_sink.py:186–203`。

先_save_cursor，再_maybe_sink_upsert；后者及实际Postgres sink都吞写入异常，不把失败反馈为导出未完成。复现两笔成交t=1000/1100，第一次写事实失败：结果仍page_complete=true、errors=[]，游标已到1100。恢复写入后重试，只保存了第二笔，第一笔没有回补。

建议：同事务提交事实及其覆盖游标，或先可靠写事实并确认成功后再推进游标；失败必须返回不完整并保持可重试范围。若采集需继续，原始响应必须先进入可恢复的持久队列，不能只在内存/日志里保留。

验收补充：事实写入失败、游标写入失败、中途进程终止分别注入；最终重试后的持久化集合必须等于完整采集集合，不止验证函数返回成功。

### R3 [P1] 收入游标未包含income_type，手续费／资金费可能被跳过

定位：`scripts/export_execution_facts.py:430–449`、`:700–704`；`shared/execution_facts_sink.py`游标SQL；`database/postgres/init-scripts/11-execution-facts.sql`游标唯一键。

不同income_type均用(source='um/income', symbol=None, window_start_ms)读取进度。复现同一窗口：REALIZED_PNL在1100、COMMISSION在500。先采集盈亏，再采集手续费，后者从1100开始，返回0条且page_complete=true，实际应有1条费用。游标键也没有account_id，持久化sink跨账户使用有同类碰撞风险。

建议：游标身份纳入account_id及完整查询过滤条件（income_type等），或者无类型过滤完整采集后再本地分类；定义窗口扩展的恢复规则并迁移旧游标，不能简单复用有歧义的完成标记。

验收补充：同起点依次采集REALIZED_PNL、COMMISSION、FUNDING_FEE及不同账户，互不影响且完整。

### R4 [P1] 市值来源已区分，但缺失真实市值仍可通过新入场

定位：`strategies/hrs/strategy.py:333–340`、`:574–589`；`strategies/hrs/scoring_engine.py:240–242`；`strategies/hrs/candidate_pool.py:491`。

正确的修复：OI代理不再混入真实市值截面。尚未兑现的规则：开仓侧代理/缺失只返回has_market_cap=False，仍调用评分并使用旧fallback分（long=5），没有显式拒绝；OR-2候选也可由其他两项入池。探针证明无真实市值、技术/情绪足够时should_entry仍True。这与A2/D4“缺失币跳过依赖该阈值的新入场路径”的严格口径不一致。

另一个质量缺口：动态阈值最小样本门槛仍检查funding_rates长度，不是OI/真实市值有效样本长度；可能以很少的真实市值样本生成被认为有效的阈值。

建议：先确认是否另有后续明确批准的“缺市值仍允许按其他因子入场”政策；本轮所读decisions.md仍是严格规则。按该规则显式拒绝依赖市值的新入场，并分别校验每个截面的有效样本量，存量退出不受阻。若改政策，应独立命名降级模式并更新决策记录。

验收补充：缺失/代理/过期的新入场断言实际execute不发生；真实有效样本不足时阈值不能以费率样本数冒充有效。

### R5 [P2] 新币价格新鲜度检查只测返回后的本地时间差

定位：`strategies/new_coin/strategy.py:467–472`、`:612–621`；`strategies/new_coin/executor.py:execute_short`下单前余额/占位/额度/杠杆链。

observed_at在await ticker返回后才设为time.time，随后再取一次time.time比较，基本恒为0。独立探针把请求耗时推进60秒，max_age_seconds=5仍接受。且该时间未传递到executor，后续余额、锁、额度、精度与杠杆请求耗时后没有再次确认价格/反弹条件。

建议：保留源行情时间（可得时）、请求起止时间和有效性；无法取得源时间时至少限制请求延迟并明确代理口径。把报价及其时间/固定参照传至实际下单前，过期则重取并重判；不能只在helper单测里mock一个“过期返回值”。

验收补充：慢返回、执行准备超时、重取后突破参照、未知源时间分别覆盖。

### R6 [P2] 同一毫秒记录超过一页时无法继续翻页

定位：`scripts/export_execution_facts.py:340–378`、`:404–411`、`:620–643`。

翻页只将start_time推进至已消费的最大时间，不使用保存的last_event_id续传。同一毫秒4笔，page_limit=2时请求起点为[1,1000,1000]，只得到2笔，最终max_pages错误退出；重试仍无法越过这批记录。现有“同毫秒”测试只有5笔且单页500，未覆盖同毫秒跨页。

此项没有伪装完成（page_complete=false），但说明尚不具备声明的完整分页能力。不能简单+1ms，那会直接跳过余下记录。

建议：按各接口可用能力分别使用ID或页码推进；不可推进时保持不完整并显式报告，不无限重取同页。补同毫秒数量=limit、limit+1、多页及最后时间等于窗口边界的测试。

## 2. 本轮已经确认的改进

| 原问题 | 本轮状态 |
|---|---|
| EMM缺24h输入 | 已计算25个连续收盘点的涨跌幅，传给多/空score；entry_mode传递已补。影子行为见R1 |
| 空池LV信号丢弃 | 已用具名列表并调用统一收尾，不再直接丢弃；范围见R1 |
| 早退不监控持仓 | 主周期已用finally收尾监控/上报，新增相关测试通过 |
| 新币收盘与含自身高点比较 | 已改固定参照+实时价，降级标志已传递；新鲜度见R5 |
| OI×20当真实市值 | provenance模型及截面排除已实现；新入场政策和样本门槛见R4 |
| 原始开仓时间被复核覆盖 | opened_at/next_review_at分离，持久化与旧状态迁移已添加，相关新测试通过 |
| 成交量/均价与计划值混用 | HRS OpenResult、新币实际成交记录、MTPCS加仓均价路径已有修复，新增一致性测试通过；不等于实盘逐单已对账 |
| 决策和成交事实证据 | 新增模型、sink、MTPCS接线和采集脚本；采集完整性仍有R2/R3/R6 |

## 3. 测试证据及边界

新增修复及证据相关定向测试：**299 passed**，exit=0，见 `targeted_tests.txt`。测试范围包括HRS三项B段、新币反弹/B段、market_cap provenance、fill consistency、decision/sink/export、MTPCS recorder与接线、tests/test_accounting。

扩大回归命令：

```bash
python3 -m pytest strategies/hrs/tests/ strategies/new_coin/tests/ tests/test_shared/test_order_fill_waiter.py tests/test_strategies/test_time_stop_review.py tests/test_strategies/test_capital_allocation_enforcement.py ai_tuner/allocation/tests/ -q --disable-warnings --tb=short --maxfail=8
```

结果：**119 passed、4 failed、4 errors**，达到停止阈值，未执行完全部范围。见 `regression_tests.txt`。

其中3个HRS execute_signal旧测试缺circuit_breaker初始化；另有market_data测试在本机Python3.9下无current event loop。它们属于需要清理/隔离验证的测试适配与环境问题，本报告没有将这些失败直接当成新增实盘缺陷。新测试全绿仍不足以替代跨版本回归。

独立探针见 `reproduce_findings.py` 和 `reproduction_results.json`。执行方式（仓库根目录）：

```bash
python3 docs/reviews/strategy_reaudit_2026-10-16/reproduce_findings.py
```

仅导入类并注入mock/合成行情，不启动策略、不连接DB/交易所。探针输出 `/tmp/strategy_reaudit_probes.json`，本目录JSON为本轮保留结果。对影子/市值案例固定技术和情绪分，证明的是调用契约与可达路径，不代表真实市场触发频率；后者仍需实际日志。

## 4. 不应混为“已全部完成”的工作

本地已实现的主要是B段与C1/D1的一部分。当前仍可见HRS按symbol/time查询REALIZED_PNL及理论fallback；本次没有看到完整的手续费/资金费率/外部资金流权益守恒、全策略归属账本、组合故障回放验收。新增事实表不等于已经完成净收益对账。

MTPCS的趋势/回归定位、分支独立净收益及切换与不交易区的有效性，也不因B段修复就得到证明。它们是研究和验收工作，不能要求修几处bug自动解决。

当前证据接线任务文档明确将部署列为本轮不做。本次未访问线上，因此只给本地修复结论，不认定已上线或未上线；应另拿部署镜像/源码hash/迁移执行/日志证据验收。

建议收口顺序：先R1–R4（行为与账本完整性），再R5–R6及旧回归适配；按上面补充案例复验后，进入净收益与策略定位的后续验证。

> 以上第 1–4 节为**原复审方结论与原文，保留不改**（其「不通过」是 2026-10-16 复审当时状态）。下方第 7 节为**修复方补记**，记录 R1–R6 收口后的状态，与原文并列、不覆盖原文。

---

## 7. 修复收口（2026-10-16 修复方补记）

> 本节由修复方（`code-document-curator-local` 记录）补记。**不改写原复审方的结论与上文任何原文**；原「不通过『全部问题已修复』验收」是复审当时状态，本节记录其后的修复收口结果。
> 本轮**只改代码/测试，未部署、未 push、未 apply 迁移、未连生产库、未调真实交易所**；所有复现/回归用例均为 mock / 合成行情离线执行。**本轮 R1–R6 新增失败：0**。

### 7.1 逐条修复状态

| 项 | 原优先级 | 修复状态 | 回归用例（文件 :: 用例） | 复现脚本现状证据 |
|---|---|---|---|---|
| R1 影子替代原生产信号 | P1 | **已修复**：`score()` 主结果恒为旧版 standard（不接收 `price_change_24h` 影响），EMM/半EMM 作为独立 shadow 挂在 `.shadows` 且标记 `signal_origin="shadow_scoring"`；`_is_shadow_entry_mode` 对 `lv_rm` 按 `signal_origin ∈ {empty_pool, nonempty_pool}` 区分，`nonempty_pool` 保持原政策、缺失来源 fail-closed | `strategies/hrs/tests/test_r1_r4_regressions.py` :: `test_r1_main_result_entry_mode_always_standard`、`test_r1_main_result_intent_unchanged_by_price_change`、`test_r1_main_result_extreme_bonus_always_zero`、`test_r1_shadow_results_attached_when_price_change_available`、`test_lv_rm_nonempty_pool_not_shadow`、`test_lv_rm_empty_pool_is_shadow`、`test_lv_rm_missing_origin_fail_closed` | `shadow_replaces_standard` 已反转：`before_entry == after_entry`；`lv_rm_existing_also_shadow` 已反转：`nonempty_pool` 判 False |
| R2 游标先于事实落库 | P1 | **已修复**：改为「先事实（含 raw）落库并确认成功，再推进游标」；sink 的 upsert / save_cursor 由「吞异常」改为 **raise**，任一失败 → `page_complete=False` + `errors` 非空、游标不推进 | `tests/test_shared/test_execution_facts_regression.py` :: `test_upsert_fails_retry_collects_all`、`test_save_cursor_fails_retry_collects_all`、`test_mid_run_termination_retry_collects_all` | `cursor_before_persist` 已反转：`first_marked_complete=False`、`first_errors` 非空、`persisted_after_retry_ids == expected_ids` |
| R3 游标身份缺 income_type/account_id | P1 | **已修复**：游标唯一键扩为 `(source, account_id, income_type, symbol, window_start_ms)`；不同 income_type、不同账户互不影响 | `tests/test_shared/test_execution_facts_regression.py` :: `test_income_types_do_not_collide`、`test_different_accounts_do_not_collide`、`test_cursor_key_includes_full_identity`、`test_window_extension_new_start_gets_fresh_cursor` | `income_filter_cursor_collision` 已反转：`fee_expected == fee_actual == 1` |
| R4 缺真实市值仍可通过新入场 | P1 | **已修复**：开仓侧 `has_market_cap=False`（`oi_proxy`/`missing`/过期）在 `score()/score_lv_rm()` 入口**显式 veto**，`should_entry=False`、真实 `execute_*` 不发生；动态阈值改为**逐截面**（funding / OI市值比）有效样本校验；存量退出不受阻 | `strategies/hrs/tests/test_r1_r4_regressions.py` :: `test_score_veto_when_no_market_cap_short`、`test_score_veto_when_no_market_cap_long`、`test_score_lv_rm_veto_when_no_market_cap`、`test_invalid_when_oi_section_insufficient`、`test_invalid_when_funding_section_insufficient`、`test_exit_side_proxy_value_not_blocked` | `missing_cap_entry` 已反转：`should_entry == False` 且 execute 调用数为 0 |
| R5 新币新鲜度只测返回后本地时间差 | P2 | **已修复**：新增 `FreshQuote`（价格 + 请求起止 + 源时间/代理口径），`_fetch_fresh_ticker` **请求延迟独立判超龄**；`execute_short(quote, entry_guard)` 在下单前重校、超龄重取并重判反弹 | `strategies/new_coin/tests/test_price_freshness_r5.py` :: `test_slow_ticker_rejected`、`test_entry_guard_passed_to_executor`、`test_executor_triggers_entry_guard_when_prep_exceeds_max_age`、`test_breakout_non_shadow_rejects`、`test_unknown_source_time_not_silently_passed` | `slow_ticker_accepted` 已反转：60s 请求不再被接受 |
| R6 同毫秒跨页无法推进 | P2 | **已修复**：fills 用 `fromId`（`get_user_trades` 增可选 `from_id`）续传，同毫秒跨页完整取回；income 接口**不支持 `tranId` 续传**时同毫秒满页 → **显式不完整**（`page_complete=False`、`request_status='incomplete'`），不 `+1ms`、不无限重取 | `tests/test_shared/test_execution_facts_regression.py` :: `test_same_ms_exactly_at_limit`、`test_same_ms_limit_plus_one`、`test_same_ms_four_trades_limit_two`、`test_last_time_equals_window_boundary`、`test_income_no_from_id_falls_back_to_incomplete` | `same_ms_cross_page` 已反转：`expected == 4`、`actual == 4` |

### 7.2 设计/实现偏差（如实记录）

- **income 分页**：`shared/binance_api.get_income_history` **未暴露 `tranId` 续传能力**，故 R6 income 走「同毫秒满页且无 ID 续传 → 显式不完整（`page_complete=False`、`request_status='incomplete'`）」分支，杜绝 `+1ms` 跳过；与架构设计 §2.5「能力分支」一致。
- **游标迁移**：`database/postgres/init-scripts/11-execution-facts.sql` 的**新唯一键仅对全新安装生效**；**本轮未对任何既有库执行迁移**。旧游标行（缺 `account_id`/`income_type`）因新代码按新键查询而**自然不命中** → 从窗口起点重采 + 幂等 upsert 回补，即「冷隔离迁移」。
- **HRS shadow 语义**：EMM/半EMM 的 shadow 结果经 `signal_origin="shadow_scoring"` 标识；shadow **是否影子化依赖 config** `{mode}.execution_mode == "shadow"`——若改为 `execute`，该链信号将转为正式下单（属显式放行，需用户单独拍板，本轮未放行）。
- **new_coin 新鲜度口径**：`shared/binance_api` 的 ticker **不返回源时间戳**，故 `FreshQuote` 采用**请求延迟代理口径**（`age_ms = now_ms − request_started_ms`，含请求耗时 + 执行准备耗时）；下单前重校已生效。
- **代码规范已知偏差**：`scripts/export_execution_facts.py` 有 3 个函数**仅因 `async def` 参数声明行**计数超过 50 行（实际函数体 ≤51 行），记为可接受偏差，进一步拆分有语义风险。

### 7.3 测试证据与既有失败 triage

- R1–R6 六场景均已固化为**先红后绿**回归用例（见 7.1 用例列）；本轮新增失败 **0**。
- **triage：已知既有失败（非本轮引入）**，与 R1–R6 收口无关，均属旧用例相对当前 config / 本机 Python 环境过时：
  - `test_fix_verification` 3F（旧 fixture 未初始化 `circuit_breaker`）；
  - `test_market_data` / `test_position_claims` / `test_position_ownership`（本机 Python 3.9 无 current event loop）；
  - `test_pattern` 4F、`test_stop_loss_mark` 4F；
  - `test_scoring_engine::test_技术总分不足不应入场` 1F（config `scoring.technical.short_min_total_score=2.0` 使技术分 2.0 通过门槛，旧用例相对 config 过时，HEAD 亦红）；
  - `test_binance_api::production_url` 1F。
- 上述既有失败**未在本轮修复**（属测试债/环境适配），登记口径与既有 triage 一致，不冒充本轮回归。

### 7.4 边界（与原文 §4 一致，未变）

本轮仅完成 R1–R6 的**本地代码修复 + 回归证据**；**净收益对账 / 权益守恒 / 全策略归属账本、MTPCS 趋势定位与分支净收益、部署镜像与迁移执行验收**仍属后续研究/验收工作，**不因本节而视为完成**。本节不主张「已部署 / 已上线」。
