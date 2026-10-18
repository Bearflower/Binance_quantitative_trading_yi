# Binance_quantitative_trading 知识库索引

最后巡检: 2026-10-15（2026-10-13 完成冷启动初始化 + 5 条样例 + 第一批 11 条放下时复核 + 第二批 8 条扩充；2026-10-14 新增 1 条诊断沉淀；2026-10-15 新增 5 issue + 2 decision 并入索引。2026-10-14/15 两次均为增量补充，未重跑全量巡检，孤立/引用链分析沿用 2026-10-13 结论；2026-10-15 另对 ghost-entry-record-mutex / entry-terminal-state-hookpoint 两条做增量补齐（BNB/SOL 续报，提交 `8071f06`，条目统计不变）；2026-10-15 另对 hrs-pnl-double-count / hrs-third-close-path-missing-summary / dashboard-pnl-caliber-map 三条做增量更新（`hrs_pnl_governance` /finish 归档：D 段上线 Run #71-73 + 生产回填 −52.91988 定案），条目统计不变；2026-10-15 另新增 1 条 issue（income-history-silent-empty-non-unified，源自 C1/D1 只读采集交付）并对 deploy-yml-init-scripts-unsynced 补记同类边界场景，条目 +1；2026-10-15 另因「看板资金基数 / 最大回撤口径修复」B 段文档债更新，新增 1 条 decision（segmented-drawdown-caliber，**取代** 20261009-max-drawdown-caliber，后者状态转「已替代」）并对 allocation-return-denominator-zero 补注「看板口径升级」、monthly 月度资金分配需求文档升 v1.6，决策 +1）；2026-10-15 另因 C1/D1 消费方/生产者接线 + DDL 迁入 `init-scripts/`（R1/R2/R3）落地，对 2026-10-15 每日变更与 income-history issue 待复查口径做增量补充（**未新增知识条目**，条目统计不变）；2026-10-16 新增 1 issue + 1 decision（CI `deploy.yml` 并发策略缺陷修复 `ci_concurrency_queue_max_2026-10-16`，条目 +1/+1）并入索引，并对「CI 并发策略（`cancel-in-progress:false` + `queue:max`）」登记路径 ③（构建失败的 Run 其变更不被后继 Run diff 覆盖）为**待复查/知悉项**；同日另因「独立复审 R1–R6 六项修复收口（`strategy_remediation_R1-R6_2026-10-16`）」新增 **2 issue**（execution-facts-cursor-before-fact-silent-drop / execution-facts-same-ms-page-advance）+ **2 decision**（fact-before-cursor-transaction-boundary / hrs-shadow-signal-origin-not-substitute），条目 **+2/+2**（本轮为增量补充，未重跑全量巡检，孤立/引用链分析沿用 2026-10-13 结论）；2026-10-18 因「目的驱动型 AI Tuner MVP 阶段一（参数安全 + 90 天准备度审计）」收口，新增 **2 issue**（relative-path-wrong-root-silent-load-failure / db-api-hallucination-asyncpg-vs-wrapper），条目 **+2**（本轮为增量补充，未重跑全量巡检，孤立/引用链分析沿用 2026-10-13 结论）
条目统计: 问题 34 / 决策 11 / 变更日志累计 5

## 巡检状态汇总
| 状态   | 数量 | 判定规则              |
|--------|------|-----------------------|
| 正常   | 21   | 本次检查未发现需复查项，不代表重新验证全部结论（= 问题 34 − 待复查 13） |
| 待复查 | 13   | 存在证据缺口、相关代码变化或后续跟踪项等复查提示（= 待复查清单未勾选数） |
| 孤立   | 8    | 无其他知识正文条目引用；索引入口不算入链，仅作提示（沿用 2026-10-13 全量链分析，未含 2026-10-15 新增 8 条） |

> 冷启动说明：5 条黄金样例的「审查」字段原均为「待审查」（仅表是否核对来源与表述，与状态互不混用）。2026-10-13 code-document-curator 完成首次放下时复核，5 条均已转「已审查」（其中 grid-min-quantity-unit 经「必要证据摘要」修正后转正）。
>
> 扩充说明（2026-10-13，第一批）：从近 30 天 handoff/review/commit 提炼 11 条（9 issue + 2 decision），覆盖资金限额/共享账户归属、new_coin 状态恢复与对账、grid 实时行情与部署、hrs DB 一致性、grid 保证金引导与组合熔断等主题。同日 code-document-curator 完成放下时复核：11 条来源逐条核对通过，全部转「已审查」；其中 grid-margin-five-factor-guide 的 commit 引用经**更正**（部署确认报告误记 `8f882bf`，实为当时 HEAD 快照；该文件实际于 `75f1735` 首次入库）。
>
> 扩充说明（2026-10-13，第二批）：同批继续提炼 8 条 issue，覆盖 09-21 月度分配（PnL 采集恒 0 / 首月 fallback 口径 / HRS 收益率失真）与佣金回填（总佣金恒 0 / 符号错误）、10-01 new_coin 增量补挂与 deploy.yml init-scripts 同步、10-13 分配收益率分母置 0。新增条目「审查」均为「待审查」。同日 code-document-curator 完成放下时复核：8 条来源逐条核对通过，全部转「已审查」；其中两处更正——09-21 月度分配两条的单测口径归属（按存档名明确：monthly 记 19 条 / 姊妹存档记 14/14），以及 09-21 分配三条「未提交」补记「首次入库 commit `3e17962`（2026-09-24）」。
>
> 扩充说明（2026-10-15）：新增 5 条 issue（ghost-entry-record-mutex / newcoin-stoploss-entry-time-type / newcoin-db-strategy-name-mismatch / newcoin-legacy-test-debt / aware-naive-timezone-defect-family）与 2 条 decision（entry-terminal-state-hookpoint / naive-timestamp-beijing-convention），源自 2026-10-14~15 的「幽灵入场互斥（ghost_entry_mutex_fix_2026-10-14）」与「new_coin executed_at 时区修复（newcoin_executed_at_timezone_2026-10-15）」两条在途任务及其**发布后复查**。新增条目「审查」均为「待审查」，待 code-document-curator 放下时复核。
>
> 扩充说明（2026-10-15，C1/D1 只读采集交付）：新增 1 条 issue（income-history-silent-empty-non-unified）——源自 `c1_d1_evidence_contract_2026-10-15` 交付的 D1 采集脚本（`scripts/export_execution_facts.py`）暴露的 ×1 已知风险：非统一账户客户端 `get_income_history` 静默返回 `[]`，采集侧无法区分「真无收入」与「被拦截」；同批对既有条目 deploy-yml-init-scripts-unsynced **补记同类边界场景**（本轮 SQL 有意放 `migrations/` 不 apply）。新增条目「审查」为「待审查」。
>
> 「审查」与「状态」是两个独立轴：审查=是否核对来源，状态=问题是否解决/决策是否生效。

## 问题模式库
| ID | 标题 | 标签 | 日期 | 状态 |
|----|------|------|------|------|
| Binance_quantitative_trading-20261018-relative-path-wrong-root-silent-load-failure | 仓库根相对路径用错基准目录（ai_tuner/ 而非仓库根）+ 吞异常，致 manifest/策略配置静默加载失败 | 配置,路径口径,ai_tuner,静默失效,部署,高优先级 | 2026-10-18 | 已解决 |
| Binance_quantitative_trading-20261018-db-api-hallucination-asyncpg-vs-wrapper | 审计只读查询误用 asyncpg 原生 API 名（fetch/fetchrow/fetchval/close），项目 DatabaseManager 仅暴露 fetch_one/fetch_all/disconnect | 数据库,API幻觉,ai_tuner,只读审计,静默失效 | 2026-10-18 | 已解决 |
| Binance_quantitative_trading-20261016-ci-concurrency-cancelled-tearing-and-pending-drop | deploy 并发策略致撕裂镜像集与 pending Run 变更静默丢失 | 配置,部署,CI,GitHub Actions,并发 | 2026-10-16 | 已解决 |
| Binance_quantitative_trading-20261016-execution-facts-cursor-before-fact-silent-drop | 采集游标先于事实落库且写失败被吞，重试后永久漏掉较早成交记录 | bug,静默失效,数据采集,execution_facts,游标,事务边界,高优先级 | 2026-10-16 | 已解决 |
| Binance_quantitative_trading-20261016-execution-facts-same-ms-page-advance | 同毫秒多笔成交/收入超过一页时无法续传，分页卡死在同一时间起点 | bug,数据采集,execution_facts,分页,同毫秒,fromId,高优先级 | 2026-10-16 | 已解决 |
| Binance_quantitative_trading-20261015-income-history-silent-empty-non-unified | get_income_history 非统一账户静默返回 []，调用方无法区分「真无收入」与「被拦截」 | 静默失效,binance_api,统一账户,income,数据采集 | 2026-10-15 | 待复查 |
| Binance_quantitative_trading-20261015-newcoin-db-strategy-name-mismatch | new_coin 平仓/复盘查询用错策略名（db_strategy_name 缺失默认「新币做空策略」）致 3 条静默失效链路 | bug,new_coin,trade_records,策略名不一致,配置缺失,静默空转,高优先级 | 2026-10-15 | 待复查 |
| Binance_quantitative_trading-20261015-newcoin-stoploss-entry-time-type | new_coin 止损打标 entry_time 类型不符（str 传入 datetime 形参）致 AttributeError 被吞、持仓残留 + consecutive_losses 虚增 | bug,new_coin,止损打标,类型不符,静默吞异常,高优先级 | 2026-10-15 | 待复查 |
| Binance_quantitative_trading-20261015-aware-naive-timezone-defect-family | aware/naive 时区缺陷族（范围外）：60 秒守卫静默失效 / 周报窗口漏计北京周一 00:00-08:00 / btc_eth aware 窗口查无时区列 + BEIJING_TZ 重复定义 | bug,new_coin,btc_eth,时区,aware/naive,静默吞异常,重复定义 | 2026-10-15 | 待复查 |
| Binance_quantitative_trading-20261015-ghost-entry-record-mutex | 幽灵 NEW 入场记录 + 归属名不一致致跨策略开仓误拦（SNPSUSDT 案例） | bug,持仓归属,互斥,trade_records,全策略 | 2026-10-15 | 已解决 |
| Binance_quantitative_trading-20261015-newcoin-legacy-test-debt | new_coin 既有测试 25 项长期失败（测试债）：MagicMock fixture 被 await + 3 处陈旧断言 | 测试债,new_coin,测试,AsyncMock,断言脱节 | 2026-10-15 | 待复查 |
| Binance_quantitative_trading-20261014-hrs-third-close-path-missing-summary | HRS 第三平仓路径（时间止损/移动止盈）结构漏写 PNL_SUMMARY 汇总 | bug,hrs,盈亏口径,写入层 | 2026-10-14 | 已解决（已部署上线验证通过 Run #71-73 + 生产回填 −52.91988） |
| Binance_quantitative_trading-20261014-adhoc-sql-guess-columns | 临时 psql 会话猜列名/窗口函数误用，postgres 日志报 5 条 ERROR（非生产缺陷） | 运维,postgres,ad-hoc SQL,窗口函数,日志排查 | 2026-10-14 | 已解决（无需代码修复） |
| Binance_quantitative_trading-20261013-hrs-pnl-double-count | HRS 盈亏双重记账，混合求和致亏损虚增 13 倍 | bug,hrs,盈亏口径,防双计,dashboard,ai_tuner | 2026-10-13 | 已解决（统计层+写入层已部署上线验证 Run #71-73；生产回填已执行 −52.91988） |
| Binance_quantitative_trading-20261013-grid-feed-silent-unhealthy | 币安 @aggTrade 停推叠加未捕获 TimeoutError 致 feed 静默假健康 | bug,实时行情,可观测性 | 2026-10-13 | 已解决 |
| Binance_quantitative_trading-20261013-grid-feed-queue-drop-latest | 队列溢出丢弃最新数据致采样取滞后价 | bug,实时行情,队列 | 2026-10-13 | 已解决 |
| Binance_quantitative_trading-20261013-allocation-return-denominator-zero | 月度分配收益率分母取 0 被静默置 0 | bug,ai_tuner,分母口径 | 2026-10-13 | 已解决 |
| Binance_quantitative_trading-20261010-grid-min-quantity-unit | 网格 min_quantity 单位错误导致资本可行性提醒全线误报 | bug,grid,单位换算 | 2026-10-10 | 已解决 |
| Binance_quantitative_trading-20261010-grid-profit-infeasible-copy | PROFIT 不可行分支把「利润率不达标」误述为「资金不足」 | bug,grid,文案 | 2026-10-10 | 已解决 |
| Binance_quantitative_trading-20261009-capital-allocation-month-offset | 月度资金分配「记录月份错位」导致 10 月限额落空 | bug,ai_tuner,月份口径 | 2026-10-09 | 已解决 |
| Binance_quantitative_trading-20261009-schema-migrate-before-up | DB schema 迁移晚于 docker up -d 致容器 crash-loop 且出口永久停用 | bug,部署,运维 | 2026-10-09 | 已解决 |
| Binance_quantitative_trading-20261009-hrs-cancel-not-persisted | 撤单只清内存未回写 DB 致 OPEN 残留被当假孤儿反复清理 | bug,hrs,DB一致性 | 2026-10-09 | 已解决 |
| Binance_quantitative_trading-20261001-newcoin-zombie-reconcile | 幽灵 open 对账须 fail-closed；kline 表存在性硬编码成死代码 | bug,new_coin,对账 | 2026-10-01 | 已解决 |
| Binance_quantitative_trading-20261001-newcoin-incremental-replenish-sl-gap | new_coin 全量重建补挂产生 SL 空窗（改为增量补挂） | bug,new_coin,资金安全 | 2026-10-01 | 已解决 |
| Binance_quantitative_trading-20261001-deploy-yml-init-scripts-unsynced | deploy.yml 未同步 init-scripts 致新表迁移失效 | 配置,部署,postgres | 2026-10-01 | 已解决 |
| Binance_quantitative_trading-20260927-newcoin-atr-loss-on-restart | 重启后 ATR 丢失致阈值退 0 立即误平 | bug,new_coin,状态恢复 | 2026-09-27 | 已解决 |
| Binance_quantitative_trading-20260922-shared-account-position-ownership | 共享 PM 账户持仓按币种而非归属过滤致互挂保护单/误平 | bug,mtpcs,持仓归属 | 2026-09-22 | 待复查 |
| Binance_quantitative_trading-20260921-pm-positionrisk-no-isolation | PM 账户 positionRisk 无策略隔离致占用虚增与越权管理他策略仓位 | bug,资金限额,PM账户 | 2026-09-21 | 已解决 |
| Binance_quantitative_trading-20260921-allocation-pnl-collect-always-zero | 月度分配 PnL 采集恒 0 致排名失效（落库名映射缺失 + SQL 不支持数组） | bug,ai_tuner,PnL采集 | 2026-09-21 | 已解决 |
| Binance_quantitative_trading-20260921-allocation-first-month-fallback-caliber | 月度分配首月 fallback 误用 allocatable_amount 致口径错误 | bug,ai_tuner,首月口径 | 2026-09-21 | 已解决 |
| Binance_quantitative_trading-20260921-hrs-monthly-return-distortion | HRS 月度收益率 -185.55% 失真（分母取月初 open_margin） | bug,ai_tuner,收益率口径 | 2026-09-21 | 待复查 |
| Binance_quantitative_trading-20260921-dashboard-commission-always-zero | dashboard 总佣金恒 0（commission 仅存在于 userTrades） | bug,dashboard,佣金 | 2026-09-21 | 已解决 |
| Binance_quantitative_trading-20260921-commission-backfill-sign-error | 佣金回填符号错误（无符号 commission 被写成正值，口径应为负值） | bug,佣金,口径 | 2026-09-21 | 已解决 |
| Binance_quantitative_trading-20260915-newcoin-target2-never-marked | target2_reached 从未被标记致移动止损整条链路永不生效 | bug,移动止损,止盈检测 | 2026-09-15 | 已解决 |

## 决策记录
| ID | 标题 | 日期 | 状态 | 替代入口 |
|----|------|------|------|----------|
| Binance_quantitative_trading-20261016-ci-concurrency-cancel-false-queue-max | CI 并发组采用 cancel-in-progress:false + queue:max，方案 D（持久化 last-deployed sha）评估为可选 | 2026-10-16 | 已采纳 | — |
| Binance_quantitative_trading-20261016-fact-before-cursor-transaction-boundary | 幂等采集采用「先事实后游标」事务边界：事实落库确认成功才推进游标，失败上抛为不完整 | 2026-10-16 | 已采纳 | — |
| Binance_quantitative_trading-20261016-hrs-shadow-signal-origin-not-substitute | HRS 影子信号不得替代生产信号：主结果恒 standard，EMM/半EMM 与空池 LV-RM 仅作 shadow（signal_origin 区分） | 2026-10-16 | 已采纳 | — |
| Binance_quantitative_trading-20261015-segmented-drawdown-caliber | 看板最大回撤改为「资金段」分段口径（segmented-v1，取代 20261009-max-drawdown-caliber） | 2026-10-15 | 已采纳 | — |
| Binance_quantitative_trading-20261015-entry-terminal-state-hookpoint | 入场记录终态标记挂钩点选在 release_claim(position_closed) 而非 update_realized_pnl | 2026-10-15 | 已采纳 | — |
| Binance_quantitative_trading-20261015-naive-timestamp-beijing-convention | 无时区 TIMESTAMP 列的 datetime 参数必须归一为 naive 北京时间（入口统一 _entry_time_to_naive_beijing） | 2026-10-15 | 已采纳 | — |
| Binance_quantitative_trading-20261013-dashboard-pnl-caliber-map | 看板盈亏三口径设计确认（本期净盈亏=净值差法，总盈亏=全历史已实现+浮动） | 2026-10-13 | 已采纳 | — |
| Binance_quantitative_trading-20261010-grid-capital-floor-reverted | 作废 be0a71a「资本下限抬升」机制，恢复诚实单位口径 | 2026-10-10 | 已采纳 | — |
| Binance_quantitative_trading-20261009-max-drawdown-caliber | 看板最大回撤统一为「资金基数 + 累计已实现盈亏」口径 | 2026-10-09 | 已替代 | [→ 20261015-segmented-drawdown-caliber](./decisions/Binance_quantitative_trading-20261015-segmented-drawdown-caliber.md) |
| Binance_quantitative_trading-20260921-market-circuit-breaker | 组合级 1h 等权双向熔断（二期补 12h 累计维度） | 2026-09-21 | 已采纳 | — |
| Binance_quantitative_trading-20260917-grid-margin-five-factor-guide | 网格保证金由静态值改为五因子动态引导（BTC 独立检测器） | 2026-09-17 | 已采纳 | — |

## 每日变更（近 30 天）
| 日期 | 变更数 | 摘要 |
|------|--------|------|
| 2026-10-18 | 2 | [ai_tuner_mvp_stage1_safety_readiness_2026-10-17]「目的驱动型 AI Tuner MVP」**阶段一（参数安全 + 90 天准备度审计）收口**：新增 `ai_tuner/parameters/{models,manifest}.py` + 两份 manifest（btc_eth_aggressive / hrs 各 4 参数 `entry_quality`、`max_change_pct=0.10`）、`ai_tuner/readiness/{models,auditor}.py` + `scripts/ai_tuner_mvp.py`（`audit`，exit 0=SELECT/2=STOP/1=错误）；`base_adapter` 双契约 `validate_params(...,decision)` + H1 `_PROJECT_ROOT`；`response_parser` 双契约（新 8 字段信封）；`learning_signal` 禁用 L4（恒返回 None）；`weekly_job` MVP 审计模式**仅拦截步骤12 写入** + H2 路径口径；`config.yaml` 新增 `purpose_driven_mvp`（mode=audit；外部快照为**策略级**门禁：全局 `0.95` + required、`btc_eth_aggressive` 覆写 `0.0`、`hrs` 保持 `0.95`）；`Dockerfile` 补 `COPY strategies/btc_eth_aggressive/config.yaml`；**阶段一累计测试 227 用例通过**。plan（D1–D10）/ spec（S1–S9）「实现偏差回标」；文档同步 `docs/README.md`、根 README、StratTuneAI 架构/反馈闭环/PRD（L4 回标）。沉淀 issue 2 条 |
| 2026-10-16 | 2 | [ci_concurrency_queue_max_2026-10-16] GitHub Actions `deploy.yml` 并发策略缺陷修复（已 push、上线验证闭环）：`concurrency` 块改为 `cancel-in-progress: false`（`0c4d088`，闭合 D1 在跑 Run 被取消致撕裂镜像集）+ 新增 `queue: max`（`a17a1c2`，闭合 D3 pending Run 被顶替致变更静默丢失；同时更正 §2.7 归因：pending 顶替系默认 `queue: single` 语义、与 `cancel-in-progress` 无关）；`.trae/rules/deployment.md` §2.7 升 v3.5。验证 Run #83 success / Run #84 13 job 全绿，真构建仅 `hrs`+`dashboard-api`，`ai-tuner`/`data-backend` 正确未重建，`DEPLOY_ID=00005401`（=`(84<<8)|1`）与公式一致、防幻觉第三/四层无混版；沉淀 issue 1 + decision 1；路径 ③（构建失败的 Run 其变更不被后继 Run diff 覆盖）保留为可选设计项（方案 D「持久化 last-deployed sha」评估为非必须）。**[strategy_remediation_R1-R6_2026-10-16] 独立复审 R1–R6 六项修复收口**（本地修复 + 先红后绿回归，**未 push/未部署/未 apply 迁移/未连生产库与交易所**）：HRS（R1+R4）主结果恒 standard + shadow（`signal_origin`）、缺市值入口 veto、逐截面样本校验；new_coin（R5）`FreshQuote` + 请求延迟判超龄 + 下单前 `entry_guard` 重校；execution_facts（R2+R3+R6）先事实后游标 + sink 失败改 raise、游标键扩 `account_id`+`income_type`、`get_user_trades` 增 `from_id`、income 无 ID 续传走显式不完整；**本轮新增失败 0**，既有失败 triage 非本轮引入；文档同步复审报告 §7 / 需求 §6 / 架构 §7 / `数据库设计.md` §3.15 / 部署指南 §11 / `spec.md` / `decisions.md` D2/D4/D8 / `docs/README.md`；沉淀 issue 2 + decision 2 |
| 2026-10-15 | 8 | [c1_d1_evidence_contract] C1「决策与证据契约」+ D1「只读采集与不可变原始事实」交付（旁路只读、先影子、零行为变更）：新增 `shared/decision_evidence.py`（纯模型/序列化，稳定 `cycle_id`/`decision_id`、`available_at` 校验、脱敏、`EvidenceSerializeResult`）+ 两张迁移 SQL（`decision_evidence` / `execution_fact_*` 四表，**仅落仓库未 apply**）+ `scripts/export_execution_facts.py`（分页/去重/游标断点恢复/`max_pages` 守卫/脱敏+sha256；**CLI 不带真实 sink，结果走 JSONL**）+ `shared/binance_api.py` 仅 `get_user_trades` 加 `end_time`/`limit`（向后兼容）；测试 84 passed（C1 19 + D1 65）；沉淀 issue 1 条（income-history-silent-empty-non-unified）+ 回补 deploy-yml-init-scripts-unsynced；**[ghost_entry_mutex_fix] 幽灵入场记录误拦修复收口（R3 GA/GB/GC，124 tests，`fce42bf` Run 74 五层验证 DEPLOY_ID=00004A01；D4 dry-run→execute：候选 582 → CANCELED 240 / KEPT 49 / SKIP 293，复查 SNPSUSDT 互斥命中数=0；**本轮增量修补 `8071f06` Run 76 五层验证 DEPLOY_ID=00004C01：BNB 8 条转 CLOSED、SOL 47 条真仓 KEPT、D2 扩 reason（open_failed/open_exception/micro_zeroed→CANCELED）、T2b 分类器修复、**AC1b 已通过（16:05 轮次日志互斥条数=0 + SQL 直查 BNBUSDT 互斥 owner 为空）**），沉淀 issue 1 + decision 1（其内容已随本轮增量补齐）；[newcoin_executed_at_timezone] R2 平仓盈亏回写 executed_at 时区修复上线（`94ca3ed` Run #75 DEPLOY_ID=00004B01，13 新增用例全绿、基线 25F/163P→25F/176P 失败集合逐条同名零回归；业务验收仍在观察中），沉淀 decision 1 + 追加工单 issue 2；[newcoin_legacy_test_debt] 另行登记 new_coin 既有测试 25 项长期失败（测试债，未修复）；[newcoin_db_strategy_name_mismatch] R2 发布后复查新发现的既有缺陷，容器内 A/B 实测确认属实（`$1='新币做空策略'` rows=0 / `$1='new_coin'` rows=1），工单 + 方案1 修复方案成文、代码未改；[hrs_pnl_governance] D 段收口 + /finish 归档：`8808e74`/`8cf1ae4`/`1d1b247`（Run #71-73）五层验证，生产回填 21 行 SUM −52.91988，回填后月度 7月+5.75/8月−29.09/9月−30.74/10月+10.29，回撤 1.0 定性为真实（income 7-9 月累计约 −87.8 > 基数 58.37）；**已完成 21 项 / 未完成转出 1 项**；[capital_base_caliber_fix_2026-10-15] 「看板资金基数 / 最大回撤口径修复」**B 段文档债更新**（文档侧收尾）：新增 decision `segmented-drawdown-caliber`（资金段分段口径 `segmented-v1`：E1/E2/E3 段边界 + K=3 配置化、段初向后取 `_SEGMENT_OPENING_CAPITAL_SQL`、段内 `nv=max(0,1+cum/段初)`、跨段不比取各段 max、残缺段 cum-reset（非比例缩放）、零分母整段跳过 + INFO 审计、CS3 持仓级 CTE 去重、`caliber_version='segmented-v1'`），**取代** `20261009-max-drawdown-caliber`（状态转「已替代」，原文保留）；`allocation-return-denominator-zero` 补注「看板侧 `_query_strategy_base_capital` 已删、口径升级」；月度资金分配需求文档升 v1.6（4 步回退链仅限 ai_tuner 侧）；`数据库设计.md` §3.12 补 `caliber_version` 字段；**[c1_d1_evidence_contract · R1/R2/R3 落地，追加]** C1/D1 消费方与生产者接线 + DDL 迁入部署路径（**开关默认关闭、零行为变更**）：R1 新增 `shared/execution_facts_sink.py`（幂等写 execution_fact 4 表）+ `export_execution_facts.py` 增 `EXECUTION_FACTS_DB_WRITE` 开关（缺省关闭、connect 失败降级）；R2 新增 `shared/decision_evidence_sink.py` + `strategies/btc_eth/evidence_recorder.py`（旁路全吞异常）+ `main.py` initialize/run_strategy 单点接线 + `config.yaml` `decision_evidence.enabled: false`；R3 DDL 迁入 `init-scripts/10-decision-evidence.sql`、`11-execution-facts.sql`（随 CI 部署执行，`migrations/` 源文件保留不删不 apply）；测试 123 passed；**`migrations/` 不自动执行、`init-scripts/` 为唯一随部署生效路径** |
| 2026-10-14 | 2 | [postgres_error_triage] postgres 日志 5 条 ERROR 定性为临时 psql 会话误用（非生产缺陷，零数据影响），沉淀 issue 1 条（含修正 SQL 与日志排查要点），INDEX/changelog 同步；[hrs-pnl-governance] HRS income 对账定真值（7月+6.36/8月-54.53/9月-39.6538/10月+3.43，修正旧结论 -2.28 非真值）+ H6 第三平仓路径漏写汇总修复（178 tests/GC 通过，待部署）+ 回填 21 组 0 未决（锚 income 合计 -52.920，dry-run 待授权），沉淀 H6 issue 1 条并更正 hrs-pnl-double-count 状态 |
| 2026-10-13 | 9 | [knowledge-base-build] 建立知识库样板（3 模板 + 5 样例 + INDEX）、knowledge 入 Git、修正 3 条样例证据/状态、补齐 skill 与 Claude 入口接入；[knowledge-base-expand] 第一批 11 条 + 放下时复核转「已审查」（1 条 commit 引用更正）；第二批 8 条（09-21 分配/佣金 + 10-01 补挂/部署 + 10-13 分母口径）；[hrs-pnl-dedup] HRS 盈亏双计防双计口径修复（f940a61/91e99c2，统计层，写入层待治理）；[pnl-caliber-map] 看板盈亏三口径设计确认（本期净盈亏/总盈亏/已实现+浮动，决策：维持现状不改文案） |

## 待复查清单
- [ ] 2026-10-18 新增 2 条（`relative-path-wrong-root-silent-load-failure`、`db-api-hallucination-asyncpg-vs-wrapper`）「审查」字段**待复核**（沉淀时默认「待审查」，需下次放下时核对来源与表述后再转「已审查」，与状态维度相互独立）
- [ ] Binance_quantitative_trading-20261016-ci-concurrency-cancelled-tearing-and-pending-drop：**知悉项/可选设计项**——CI 并发策略（`cancel-in-progress:false` + `queue:max`，已上线验证）已闭合路径 ①②（在跑 Run 被取消 / pending Run 被顶替），**路径 ③ 仍开放**：某服务**构建失败**的 Run 其目录变更不被后继 Run 的 `git diff`（`github.event.before..HEAD`）覆盖 → 该服务可滞留旧镜像。**该 Run 变红可见、可重跑恢复**；经多智能体评估「方案 D（持久化 last-deployed sha 作 diff 基）」为**非必须**（推送节奏实测近 6 天仅 1 次「≥3 push 落入构建窗」且为代码提交、路径 ③ 从未真实触发）。如需**根治**（而非依赖重跑）才启动方案 D；无强制修复期限。
- [ ] Binance_quantitative_trading-20261015-income-history-silent-empty-non-unified：`get_income_history` 在 `use_unified_account=False` 时**静默返回 `[]`**（只打 warning，不发请求），采集侧无法区分「真无收入」与「被拦截」；本轮 D1 交付**未改该行为**，仅以 `ExecutionFactExporter` docstring 告知调用方须传统一账户客户端。**待独立修复**（应在返回值/异常通道显式暴露可用性或让采集侧 fail-closed）；本轮迁移 SQL 亦未 apply、CLI 未带真实 sink。（2026-10-15 R1/R3 落地后更新口径：DDL 已迁入 `init-scripts/10-*`、`11-*`（随 CI 部署执行），CLI 已可注入真实 PG sink，但 `EXECUTION_FACTS_DB_WRITE` **默认关闭**、本轮未 push 未部署；`get_income_history` 静默空返回的**代码行为仍未改**，仍待独立修复。）
- [x] Binance_quantitative_trading-20261015-ghost-entry-record-mutex：**AC1b 已通过**——BNB 幽灵记录清理（`8071f06`）后，btc_eth 北京时间 16:05 轮次全量日志**互斥相关条数 = 0**（该轮 5 币均在信号阶段被拒、未走到互斥判定，属弱证据），并补 SQL 直查：`BNBUSDT` 互斥 owner 命中数 = 0；**更正**：SOLUSDT **11:05 告警时点** owner 实为幽灵 id=3926，该次拦截**同属幽灵误拦（与 BNB 同根因，是缺陷）**，原「SOL 真仓、拦截正确」结论已废止（错因：用当前态回溯历史事件）；**SOL 存量清理已执行（2026-10-15 17:32 北京时间，用户手动平仓后）**：空仓窗口 dry-run `CLOSED 49` → `--execute --symbol SOLUSDT` `CLOSED=49`，复检互斥 SQL 命中数 = 0，49 条逐条 CLOSED（旧「44 条 aggr NEW」已清零）
- [ ] Binance_quantitative_trading-20261015-newcoin-stoploss-entry-time-type：**高优先级**，`strategy.py` 止损打标 `entry_time` 类型不符（str 传入 datetime 形参）致 AttributeError 被吞 + `consecutive_losses` 每轮 +1，**未修复**，待立修复工单
- [ ] Binance_quantitative_trading-20261015-newcoin-db-strategy-name-mismatch：R2 发布后复查发现的既有缺陷（已独立复核确认属实），工单 + 修复方案（方案1）已成文；**B 段已落地（代码已改、未提交、未 push）**——`strategy.py` 缓存 `self.my_record_name`（`db_strategy_name` 全文件计数=0）+ 4 处查询替换（实测运行行号 L929/L1347/L1460/L1509）、`main.py` 写库名去硬编码统一到同一来源、3 处 fixture 口径修正、新增 AC 文件 7 用例全绿（覆盖 AC1/AC3/AC4/AC5 + 哨兵恒等）+ 零回归夹逼对照（`25 failed, 204 passed`，失败集合逐条一致）；**待提交 + 上线观察**（唤醒 `consecutive_losses` 累加可能新触发策略暂停）
- [ ] Binance_quantitative_trading-20261015-aware-naive-timezone-defect-family：范围外时区缺陷族 ②③④（60 秒守卫静默失效 / 周报窗口漏计北京周一 00:00-08:00 / btc_eth aware 窗口查无时区列 + BEIJING_TZ 重复定义），**未修复**，须另开修复工单
- [ ] Binance_quantitative_trading-20261015-newcoin-legacy-test-debt：25 项既有测试长期失败（测试债），**尚未修复**，待独立任务（优先统一 fixture 用 `AsyncMock`；禁止删断言/skip/放宽门槛）
- [ ] Binance_quantitative_trading-20261010-grid-min-quantity-unit：§9.10 条件①②③（feasible=true 互算自洽）待补验
- [ ] Binance_quantitative_trading-20261010-grid-profit-infeasible-copy：§10.9 发布后真实推送观察尚无完成记录，待验证
- [ ] Binance_quantitative_trading-20260922-shared-account-position-ownership：根治方案（方案 C 持仓归属互斥）代码已落地但**尚未部署**，无 commit、无验证证据
- [ ] Binance_quantitative_trading-20260915-newcoin-target2-never-marked：移动止损已上线，但**尚无真实 TP2 成交数据**验证激活效果（部署时容器无持仓），待观察
- [ ] Binance_quantitative_trading-20260921-hrs-monthly-return-distortion：分母改月初 open_margin 已上线（DEPLOY_ID b4f70588），但 history 表 2026-09-10 起才有数据，**本月仍失真、下月生效**，待 10/31 回填结论
- [ ] Binance_quantitative_trading-20261013-hrs-pnl-double-count：`hrs_pnl_governance` **未完成项已转出**——ai_tuner 月度口径 `_PNL_QUERY_TEMPLATE` 跨月 carry-in（10月 +10.29 vs income 真值 +3.43）本任务未动，转出至 `capital_base_caliber_fix_2026-10-15`（目标3）后续处理
- [ ] 本轮 R1–R6 收口新增 4 条（`execution-facts-cursor-before-fact-silent-drop`、`execution-facts-same-ms-page-advance` 2 issue + `fact-before-cursor-transaction-boundary`、`hrs-shadow-signal-origin-not-substitute` 2 decision）「审查」字段**待复核**（沉淀时默认「待审查」，需下次放下时核对来源与表述、以及后续替代记录后再转「已审查」）
- [x] 5 条黄金样例「审查」字段首次复核：2026-10-13 code-document-curator 逐条核对来源完成，5 条全部转「已审查」
- [x] 第一批新增 11 条的「审查」字段首次复核：2026-10-13 code-document-curator 逐条核对来源完成，11 条全部转「已审查」（其中 grid-margin-five-factor-guide 经 commit 引用更正后转正）（与状态维度相互独立）
- [x] 第二批新增 8 条的「审查」字段首次复核：2026-10-13 code-document-curator 逐条核对来源完成，8 条全部转「已审查」（其中 09-21 分配两条更正单测口径归属、三条补记首次入库 commit `3e17962`）（与状态维度相互独立）

## 孤立条目
- Binance_quantitative_trading-20260917-grid-margin-five-factor-guide（无其他知识正文条目引用）
- Binance_quantitative_trading-20260921-market-circuit-breaker
- Binance_quantitative_trading-20261009-hrs-cancel-not-persisted
- Binance_quantitative_trading-20261010-grid-profit-infeasible-copy
- Binance_quantitative_trading-20261009-max-drawdown-caliber
- Binance_quantitative_trading-20261013-allocation-return-denominator-zero
- Binance_quantitative_trading-20261001-newcoin-incremental-replenish-sl-gap
- Binance_quantitative_trading-20261001-deploy-yml-init-scripts-unsynced
- （2026-10-15 新增 7 条未参与本次链分析，沿用 2026-10-13 结论）

## 关联
- [项目文档入口](../README.md)
