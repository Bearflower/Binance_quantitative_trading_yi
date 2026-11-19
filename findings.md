# 发现与约束

- 2026-11-19最新4316920f于09:47:33部署，1447完整CI通过，74身份包含最后IRYS，39运行代码/配置/风险数字/SLTP不变。Demo认证两种方法及实际项目AKE请求均200；新发布已加载secret且实际env布尔匹配通过；当前36/36真实CG诊断通过，一次CG请求/429=0，原CMC保持。早先401原因未唯一确定，原失败记录保留。
- 09:21自然原payload362/两个exact父记录及26repair由根代理独立原始来源重建通过；原3批32/33、26/26、21/21，IRYS原缺失不回写。4候选minimum_order_exceeds_risk_budget，risk_limit0.5236USDT/effective margin26.180/quantity0；不能将无新单误认新的槽位或冷却故障。


## 以下为历史调查快照（以各条记录时刻为准）

- 2026-11-19当前发布facde7103（09:01 CST，1412云端测试）；实际午夜权益原点已形成，门禁ready/allowed，仍只是inception protection。原先daily_origin_missing为历史观察，已由真实新日边界解决。
- 73身份注册并不能消除CG429：00:33/00:44 UTC历史35symbol诊断23有值/12provider失败/0身份失败；09:02 CST最新同集合35/35新鲜CG已恢复，这只是当前组件诊断，不回填原小时。双源原始批次相隔113秒，仅AKE/LIT/MMT/PIPPIN市值及供应量均<=5%；8个明显口径差异继续禁用。09:20安全检查发现CGDemoKey字段，09:24真实认证探测401/10002，未获有效报价、未重载常驻服务；安全核对待用户，不能将字段存在等同认证成功。不能假设免费CMC可替代所有市值。无历史回填、无代理估值、无放宽TTL/95%。
- 自然K线完整来源验证已通过29/29、29/29、28/28，包含五个实际缺口标的。每一轮有效不能替代连续24h与五合格自然闭合，更不能替代缺失历史的跨月策略绩效。以下为原始调查时间序列。


- 当前生产3aa32e9e（21:38 CST），云端1408测试通过。槽位v2是用户批准的准入行为变化，不能写成经济等价；单笔1%/组合3%、全账户保证金约束和SL/TP数字保持。成功预留仅事务commit后留本次audit，并即时捕获actualgate/fullclaims；score-only先完成双库存GET再读同RR gate DBclock，拒旧parent fallback和candidate-only归属替代。
- 真实五标的完整rawGET来源包跨Python3.11/3.12闭包通过；float sum跨版本1ulp已由decimal128一次转换修复。未来/过去越界raw响应在过滤前拒绝；实际scorer输入在成功和异常时均独立于并发cache。新版本自然整轮仍待实际发生。
- 云端基础镜像残留一份当前Git不存在的31行identity旧测试，导致第一次1408pass/1fail；test stage先清理继承测试再COPY完整当前测试，独审确认无skip/运行stage变化，第二轮1408pass。

- 当前生产441559ae（15:58 CST），1288云端测试及293独立复审；新增九项身份实际9/9可信CG诊断，新parent525条源闭包通过。历史不足与未来窗口仍不标绩效通过。

- 3436656d已于13:07 CST上线，13:21与14:21自然扫描均complete且原始hash/配置/财务上下文一致。九项新crypto身份已核验，尚待合并受控发布。
- 月窗旧64MiB/50k单体格式有实际容量缺口；17c3411a+fff8f040的完整CAS归档/来源COUNT/cursor/真实CLI链路已由宿主重跑416项通过，并有119项独立复审。实际窗口仍未形成，不能把合成容量用例当收益证据。
- 生产forecast全前缀重复保存导致O(n²)存储增长，ebe17ee7仅对新parent采用常数引用、旧payload/hash不改，134定向通过。复审又发现逐parent全prefix闭包校验的O(n²)读取回归，正补一次增量校验后再发布；不删除原始留证。

- 生产已启用新风险门禁/原始留证/持续报告。日初真实权益缺失会拒新仓；当前是这个原因，不能说上线即能开仓。
- 前瞻实验原来要求预登记午夜精确等于含微秒的实际scan_start，但实际align_minute=21。72f414c2已补事前固定、可重算的首扫描边界，v2/v3登记保持经济基线/24候选/成本/窗口一致；扫描对齐的观察月不能冒充完整自然月。

- 2026-11-18 发布暴露真实权限差异：非管理员 trading_user 虽具权益段 SELECT/INSERT，SELECT FOR UPDATE 仍需某列 UPDATE 权限；不得以管理员测试代替运行角色验收。首次候选自动回滚，正按最小权限和权益段不可变性补齐。

- 本轮起始生产 HRS 镜像 2cf4f020…，共享市值镜像 4fa94f38…；原认证发布 SHA 386e4330。2026-11-18 12:00 CST 实际升级到6bde184c，HRS a67c0415…、hub3b90bc1a…；AI Tuner冻结配置挂载保留，真实配置hash不变。
- HRS 原始工作区不等于生产源码。新分支从认证发布分支 954ee5f2 建立；先导出实际 HRS 模块再开始整合。
- 初始通用 HRS replay adapter 的 intents 曾明确抛 hrs_decision_not_wired；本轮已接通共享实盘决策核心并完成因果成交回放。历史输入仍须实际快照，不能以零交易假装回放成功。
- 历史 Aug/Sep 缺候选、决策、外部快照；可闭合交易只用于退出方案探索，不能等同完整反事实回放。
- 认证 Key 已在服务器 secrets 文件，禁止带回仓库、构建参数或普通输出。


- 关键新增发现：通用broker曾对HRS TP2按剩余量分配、首根成交bar无保护、4h误撮合；agent正在补HRS能力路径并保持MTPC旧行为。完整历史资金费timetable需manifest证明，不可用当前8h假设。
- Collector事务锁统一建议：income_account session guard -> account policy advisory lock(hashtextextended(account,7183)) -> loss-gate lock，不能nested pool leases。生产确有 derived_income_attribution 表（schema只读核对），仓库旧source可能无定义；不能混淆不存在。收入按(account,income_type,tranId)识别，fee/funding不能漏或重复加fill盈亏。
- 当前loss gate严格每日00:00CST；初次intraday不就绪。未来可从真实flat起点+完整fill/income/allocation重建flat midnight，effective_time和available_time分开。不能把当前flat伪造历史flat。无30d新历史只标inception protection，不标已通过30d。
- SL/TP最新用户授权已给，不需要例行确认；不宣称现有/缺失窗口已通过，不为凑样本下真实单。


---
## 先前七项整改记录（保留，不属于本次 HRS 状态）

# 七项整改调查发现

- 隔离基线：`origin/main` 的 `8991d03d3c4625612b4d42d2014e829aa6750f4f`；生产活动 pin 同 SHA，状态 active。
- 2026-11-07 早间，HRS、激进版、原版、新币、网格、dashboard-api、ai-tuner 容器均为 running/healthy。
- 现有代码对未完成策略归属保持显式 unknown，分配仍冻结；`pnl_collector.py` 明确因 `missing_capital_evidence`、`missing_activity_evidence` 拒绝排名。
- 新币恢复仓的 `time_exit_status` 为 review_required/unknown，代码有禁自动时间退出保护。
- 激进版频控恢复、时区和故障拒新单代码在当前基线中，但 STOP 专属退出冷却仍缺归属到 lot/order/trade 的已证成交事件。
- 原版历史亏损、网格收益样本和现金流调整收益率的缺口，不能用账户币种净额或净值变动替代。
- 生产 `strategy_gate` 于 2026-11-07 00:03 UTC 通过；账户有 SOLUSDT 多仓 0.27，门禁确认 1 个受保护持仓，非空仓。部署前的“无持仓”快照已过时。
- 网格 `signal_bot.mode=semi` 时，`OrderManager.place_grid_order` 仍能直接下单；若 `signal_bot.enabled=false`，`GridStrategy.is_signal_mode` 会变成 false。两处均有可复现失败测试。
- 旧网格集成测试 `test_strategy_analyze` 在未改动的分析函数中把 dict 价格送到 `GridCalculator`，报类型错误；不能把全套旧集成测试称为通过。
- 看板 `get_account_returns` 当前计算未剔除现金流的净值变动率；API 与页面已明确文字说明，完整投资收益率缺现金流/估值桥接证据。
- 生产实际条件单表是 `btc_eth.condition_orders`（3915 行）；`public.condition_orders` 为空。激进版 STOP_LOSS 355 行全部有 algo_id、全部缺普通 order_id；原版 120 行也同样缺 order_id。成交事实 `trading.execution_fact_fill` 有 2110 行且 order_id/trade_id/PnL 均齐全，但缺 algo→actualOrderId 的持久映射。
- 币安官方 PM UM `GET /papi/v1/um/algo/algoOrder` 可按 algoId 查询，响应包含 actualOrderId；取消/过期无成交的订单有查询时限。查询结果仍需普通订单 FILLED 和 userTrades 数量逐笔核对，不能单看 FINISHED/actualOrderId。
- 激进版 `FrequencyController.record_trade(..., pnl)` 原本会把平仓再算作一次开仓次数；只有开仓路径实际调用，但这是接入已核退出事件前的错误语义。
- 2026-11-09：`PositionState.entry_order_id` 在 `merge_position` 加仓时被覆盖，不能用作持仓批次键；增加不随加仓变化的首笔入场单身份。历史快照缺该字段时必须保持未知。
- 新挂 STOP 有通用 `_place_conditional_order_and_record` 路径，极端行情 `_tighten_extreme_stop` 直接调用交易所；两条路径均需记录候选绑定。仅有候选绑定不足以认定 STOP 成交或启动冷却。
- `fetch_lot_fills` 以六天窗口分页。旧恢复测试假设 2026-10-31 至当前日期始终只需一次查询，11 月 9 日开始失效；改成首窗返回成交、次窗为空，并检查首次调用参数。
- STOP 订单本身 FILLED 且逐笔数量相等，仍不足以证明它平的是同一批次；从首入场成交时间到 STOP 最后一笔成交的完整 userTrades 历史必须显示同向持仓持续不归零、STOP 后归零。闭合批次证明已作为只读函数实现，未核证据不入退出事件表。
- 币安当前 PM UM algo 下单支持 `clientAlgoId`，查询接口也可按该 ID 查订单；无成交撤销/过期单 3 天、所有单 90 天后可能不可查。先在策略持久卷 fsync 该 ID 与批次，再 POST，可覆盖响应未知和数据库中断重启后的恢复。查询不到不能当作未成交，意图保持待核并阻断新入场。
- 激进版 Docker Compose 将 `./data/btc_eth_aggressive` 挂到 `/app/data`；持久意图日志使用此卷，入场门禁在新开仓前执行写入与读取探针。
- 生产 2026-11-09 午后只读 `strategy_gate` 版本 2 通过，账户空仓、无普通/条件单。激进版 `public.strategy_states` 四条历史 `position:*` 快照都缺 `episode_entry_order_id`；不能凭快照的最后入场单或旧 algoId 补认闭合批次，需单独核交易所成交链。
