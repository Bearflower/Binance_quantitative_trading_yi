# Binance_quantitative_trading 知识库索引

最后巡检: 2026-10-15（2026-10-13 完成冷启动初始化 + 5 条样例 + 第一批 11 条放下时复核 + 第二批 8 条扩充；2026-10-14 新增 1 条诊断沉淀；2026-10-15 新增 5 issue + 2 decision 并入索引。2026-10-14/15 两次均为增量补充，未重跑全量巡检，孤立/引用链分析沿用 2026-10-13 结论；2026-10-15 另对 ghost-entry-record-mutex / entry-terminal-state-hookpoint 两条做增量补齐（BNB/SOL 续报，提交 `8071f06`，条目统计不变））
条目统计: 问题 28 / 决策 7 / 变更日志累计 3

## 巡检状态汇总
| 状态   | 数量 | 判定规则              |
|--------|------|-----------------------|
| 正常   | 19   | 本次检查未发现需复查项，不代表重新验证全部结论（= 问题 28 − 待复查 9） |
| 待复查 | 9    | 存在证据缺口、相关代码变化或后续跟踪项等复查提示（= 待复查清单未勾选数） |
| 孤立   | 8    | 无其他知识正文条目引用；索引入口不算入链，仅作提示（沿用 2026-10-13 全量链分析，未含 2026-10-15 新增 7 条） |

> 冷启动说明：5 条黄金样例的「审查」字段原均为「待审查」（仅表是否核对来源与表述，与状态互不混用）。2026-10-13 code-document-curator 完成首次放下时复核，5 条均已转「已审查」（其中 grid-min-quantity-unit 经「必要证据摘要」修正后转正）。
>
> 扩充说明（2026-10-13，第一批）：从近 30 天 handoff/review/commit 提炼 11 条（9 issue + 2 decision），覆盖资金限额/共享账户归属、new_coin 状态恢复与对账、grid 实时行情与部署、hrs DB 一致性、grid 保证金引导与组合熔断等主题。同日 code-document-curator 完成放下时复核：11 条来源逐条核对通过，全部转「已审查」；其中 grid-margin-five-factor-guide 的 commit 引用经**更正**（部署确认报告误记 `8f882bf`，实为当时 HEAD 快照；该文件实际于 `75f1735` 首次入库）。
>
> 扩充说明（2026-10-13，第二批）：同批继续提炼 8 条 issue，覆盖 09-21 月度分配（PnL 采集恒 0 / 首月 fallback 口径 / HRS 收益率失真）与佣金回填（总佣金恒 0 / 符号错误）、10-01 new_coin 增量补挂与 deploy.yml init-scripts 同步、10-13 分配收益率分母置 0。新增条目「审查」均为「待审查」。同日 code-document-curator 完成放下时复核：8 条来源逐条核对通过，全部转「已审查」；其中两处更正——09-21 月度分配两条的单测口径归属（按存档名明确：monthly 记 19 条 / 姊妹存档记 14/14），以及 09-21 分配三条「未提交」补记「首次入库 commit `3e17962`（2026-09-24）」。
>
> 扩充说明（2026-10-15）：新增 5 条 issue（ghost-entry-record-mutex / newcoin-stoploss-entry-time-type / newcoin-db-strategy-name-mismatch / newcoin-legacy-test-debt / aware-naive-timezone-defect-family）与 2 条 decision（entry-terminal-state-hookpoint / naive-timestamp-beijing-convention），源自 2026-10-14~15 的「幽灵入场互斥（ghost_entry_mutex_fix_2026-10-14）」与「new_coin executed_at 时区修复（newcoin_executed_at_timezone_2026-10-15）」两条在途任务及其**发布后复查**。新增条目「审查」均为「待审查」，待 code-document-curator 放下时复核。
>
> 「审查」与「状态」是两个独立轴：审查=是否核对来源，状态=问题是否解决/决策是否生效。

## 问题模式库
| ID | 标题 | 标签 | 日期 | 状态 |
|----|------|------|------|------|
| Binance_quantitative_trading-20261015-newcoin-db-strategy-name-mismatch | new_coin 平仓/复盘查询用错策略名（db_strategy_name 缺失默认「新币做空策略」）致 3 条静默失效链路 | bug,new_coin,trade_records,策略名不一致,配置缺失,静默空转,高优先级 | 2026-10-15 | 待复查 |
| Binance_quantitative_trading-20261015-newcoin-stoploss-entry-time-type | new_coin 止损打标 entry_time 类型不符（str 传入 datetime 形参）致 AttributeError 被吞、持仓残留 + consecutive_losses 虚增 | bug,new_coin,止损打标,类型不符,静默吞异常,高优先级 | 2026-10-15 | 待复查 |
| Binance_quantitative_trading-20261015-aware-naive-timezone-defect-family | aware/naive 时区缺陷族（范围外）：60 秒守卫静默失效 / 周报窗口漏计北京周一 00:00-08:00 / btc_eth aware 窗口查无时区列 + BEIJING_TZ 重复定义 | bug,new_coin,btc_eth,时区,aware/naive,静默吞异常,重复定义 | 2026-10-15 | 待复查 |
| Binance_quantitative_trading-20261015-ghost-entry-record-mutex | 幽灵 NEW 入场记录 + 归属名不一致致跨策略开仓误拦（SNPSUSDT 案例） | bug,持仓归属,互斥,trade_records,全策略 | 2026-10-15 | 已解决 |
| Binance_quantitative_trading-20261015-newcoin-legacy-test-debt | new_coin 既有测试 25 项长期失败（测试债）：MagicMock fixture 被 await + 3 处陈旧断言 | 测试债,new_coin,测试,AsyncMock,断言脱节 | 2026-10-15 | 待复查 |
| Binance_quantitative_trading-20261014-hrs-third-close-path-missing-summary | HRS 第三平仓路径（时间止损/移动止盈）结构漏写 PNL_SUMMARY 汇总 | bug,hrs,盈亏口径,写入层 | 2026-10-14 | 已解决（代码修复完成，待部署验证） |
| Binance_quantitative_trading-20261014-adhoc-sql-guess-columns | 临时 psql 会话猜列名/窗口函数误用，postgres 日志报 5 条 ERROR（非生产缺陷） | 运维,postgres,ad-hoc SQL,窗口函数,日志排查 | 2026-10-14 | 已解决（无需代码修复） |
| Binance_quantitative_trading-20261013-hrs-pnl-double-count | HRS 盈亏双重记账，混合求和致亏损虚增 13 倍 | bug,hrs,盈亏口径,防双计,dashboard,ai_tuner | 2026-10-13 | 已解决（统计层部署，写入层待部署） |
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
| Binance_quantitative_trading-20261015-entry-terminal-state-hookpoint | 入场记录终态标记挂钩点选在 release_claim(position_closed) 而非 update_realized_pnl | 2026-10-15 | 已采纳 | — |
| Binance_quantitative_trading-20261015-naive-timestamp-beijing-convention | 无时区 TIMESTAMP 列的 datetime 参数必须归一为 naive 北京时间（入口统一 _entry_time_to_naive_beijing） | 2026-10-15 | 已采纳 | — |
| Binance_quantitative_trading-20261013-dashboard-pnl-caliber-map | 看板盈亏三口径设计确认（本期净盈亏=净值差法，总盈亏=全历史已实现+浮动） | 2026-10-13 | 已采纳 | — |
| Binance_quantitative_trading-20261010-grid-capital-floor-reverted | 作废 be0a71a「资本下限抬升」机制，恢复诚实单位口径 | 2026-10-10 | 已采纳 | — |
| Binance_quantitative_trading-20261009-max-drawdown-caliber | 看板最大回撤统一为「资金基数 + 累计已实现盈亏」口径 | 2026-10-09 | 已采纳 | — |
| Binance_quantitative_trading-20260921-market-circuit-breaker | 组合级 1h 等权双向熔断（二期补 12h 累计维度） | 2026-09-21 | 已采纳 | — |
| Binance_quantitative_trading-20260917-grid-margin-five-factor-guide | 网格保证金由静态值改为五因子动态引导（BTC 独立检测器） | 2026-09-17 | 已采纳 | — |

## 每日变更（近 30 天）
| 日期 | 变更数 | 摘要 |
|------|--------|------|
| 2026-10-15 | 4 | [ghost_entry_mutex_fix] 幽灵入场记录误拦修复收口（R3 GA/GB/GC，124 tests，`fce42bf` Run 74 五层验证 DEPLOY_ID=00004A01；D4 dry-run→execute：候选 582 → CANCELED 240 / KEPT 49 / SKIP 293，复查 SNPSUSDT 互斥命中数=0；**本轮增量修补 `8071f06` Run 76 五层验证 DEPLOY_ID=00004C01：BNB 8 条转 CLOSED、SOL 47 条真仓 KEPT、D2 扩 reason（open_failed/open_exception/micro_zeroed→CANCELED）、T2b 分类器修复、**AC1b 已通过（16:05 轮次日志互斥条数=0 + SQL 直查 BNBUSDT 互斥 owner 为空）**），沉淀 issue 1 + decision 1（其内容已随本轮增量补齐）；[newcoin_executed_at_timezone] R2 平仓盈亏回写 executed_at 时区修复上线（`94ca3ed` Run #75 DEPLOY_ID=00004B01，13 新增用例全绿、基线 25F/163P→25F/176P 失败集合逐条同名零回归；业务验收仍在观察中），沉淀 decision 1 + 追加工单 issue 2；[newcoin_legacy_test_debt] 另行登记 new_coin 既有测试 25 项长期失败（测试债，未修复）；[newcoin_db_strategy_name_mismatch] R2 发布后复查新发现的既有缺陷，容器内 A/B 实测确认属实（`$1='新币做空策略'` rows=0 / `$1='new_coin'` rows=1），工单 + 方案1 修复方案成文、代码未改 |
| 2026-10-14 | 2 | [postgres_error_triage] postgres 日志 5 条 ERROR 定性为临时 psql 会话误用（非生产缺陷，零数据影响），沉淀 issue 1 条（含修正 SQL 与日志排查要点），INDEX/changelog 同步；[hrs-pnl-governance] HRS income 对账定真值（7月+6.36/8月-54.53/9月-39.6538/10月+3.43，修正旧结论 -2.28 非真值）+ H6 第三平仓路径漏写汇总修复（178 tests/GC 通过，待部署）+ 回填 21 组 0 未决（锚 income 合计 -52.920，dry-run 待授权），沉淀 H6 issue 1 条并更正 hrs-pnl-double-count 状态 |
| 2026-10-13 | 9 | [knowledge-base-build] 建立知识库样板（3 模板 + 5 样例 + INDEX）、knowledge 入 Git、修正 3 条样例证据/状态、补齐 skill 与 Claude 入口接入；[knowledge-base-expand] 第一批 11 条 + 放下时复核转「已审查」（1 条 commit 引用更正）；第二批 8 条（09-21 分配/佣金 + 10-01 补挂/部署 + 10-13 分母口径）；[hrs-pnl-dedup] HRS 盈亏双计防双计口径修复（f940a61/91e99c2，统计层，写入层待治理）；[pnl-caliber-map] 看板盈亏三口径设计确认（本期净盈亏/总盈亏/已实现+浮动，决策：维持现状不改文案） |

## 待复查清单
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
