# HRS 优化发布与验收记录（2026-11-18）

本轮得到用户授权实施、离线验证和通过后的生产部署。生产交易 API 的人工验收只使用只读查询，不为凑样本开仓、平仓或改保护单。

## 首次发布

- 候选源码 `9204d908bce3d2325c31edf184757af14ae9422a`，独立 PostgreSQL 16 与候选 Python 3.11 镜像云端定向验证 **1133 passed**。运行记录：https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44223966232 。
- 2026-11-18 02:51 UTC 初始化验收失败后，受控脚本恢复原 HRS 和市值镜像，独立报告容器移除，无关容器身份核验未变。加法数据库迁移保留。
- 运行态投影报 `InsufficientPrivilegeError`。只读排查发现实际非管理员 `trading_user` 无 `trading.hrs_equity_segment` 行锁所需 UPDATE 权限；现有 SELECT/INSERT 不足以执行 SELECT FOR UPDATE。不是资金费来源表或 sequence 权限不足。
- 修复验证中：仅授予一个列的 UPDATE 权限以满足行锁，同时用不可变触发器拒绝 UPDATE/DELETE/TRUNCATE；新增使用真实非管理员角色的完整刷新和重启验证。失败诊断只保存固定代码和白名单状态，不保存环境、原始错误文本或凭据。
- 回滚后只读仓位与保护复核：当前 WLDUSDT 数量 28，两套独立库存一致，STOP_MARKET closePosition 存在。该仓位是自然实盘事件，不能沿用之前 DOGE 快照当作当前状态。

## 发布后续修复

- `8061f173`：持久化每次初始化健康观测和回滚前失败代码；固定白名单脱敏，22 项测试通过。
- `2eb9ff78`：显式撤销主交易角色对原始决策证据的 INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER，只保留 SELECT；独立 writer 可追加，不能改旧记录，27 项采集测试通过，含真实默认 ACL 继承的复现与角色验证。
- `2193272a`：HRS 只减仓退出不再被回放误报为低于最小名义额；入场与数量步长约束保留，374 项回放/实验定向测试通过。依据 [Binance PM 错误码 -4164](https://developers.binance.com/en/docs/products/derivatives-trading-portfolio-margin/error-code)，reduce-only 有该最小名义额豁免。
- `8dd76700` / `56111762`：后台权益刷新和候选定时器的输入不再形成假扫描；真实缺起点/终点/上下文失败、篡改哈希和非因果时间仍阻止验收通过。报告原集 47 项通过，另外两项完整性用例通过；该修复已独立复审。

## 第二次候选

- 源码 `970e2e36545181045f25f3e344b2743555e7f6c6`，云端 Python 3.11 + 新 PostgreSQL 16 定向 **1148 passed**：[候选运行](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44231401156)。
- `2a978586` 的真实非管理员刷新/重启/迁移重跑与不可变性修复已通过 102 项相关测试，独立复审通过。生产只读发布前预检通过，当前 WLDUSDT 28 与两套独立库存、原硬止损一致。
- HRS 候选镜像：`ghcr.io/bearflower/trading-hrs@sha256:06744cb45259485b112eb81a36e4365e71545a69f699b17dca21cfb41d7d18c9`。
- 市值候选镜像：`ghcr.io/bearflower/trading-hrs@sha256:4610af22d16bdc2e7b7e946e987f94cb9f6efcff7f183a766063724b0cdf02ab`。
- 实际 apply 在替换容器和迁移前被 `existing_environment_drift_keys:HRS_RELEASE_SHA` 拦截。只读核对全部容器 ID 未变，原版仍健康。原因是第一次回滚留下 Compose 的 `HRS_RELEASE_SHA: null`，覆盖候选镜像的 ENV。
- `1944bb4b` 修复：候选 overlay 显式绑定本次 SHA；None/旧 SHA 两种回滚后重试先红后绿，其他环境漂移检测不放宽，发布集 24 passed。源码修复由宿主独立复核。后续使用重新验证的候选，结果待记录。

## 实际生产发布通过

- 最终实盘源码 `6bde184ce57679ec0c475d93173ea17c58369e02`，[云端候选](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44232907695) **1150 passed**。
- 2026-11-18 **12:00:10 北京时间**受控发布完成，发布状态 `complete`，三个相关活动 pin 均为该 SHA。只替换 HRS、市值服务并新增独立 DB-only 报告进程；无关容器 ID 全部不变。
- HRS 镜像 `sha256:a67c0415e5b69e921cb099c1a8c80f6c357a0a56e802c631263d77f07a684f18`，市值镜像 `sha256:3b90bc1a5c5f7d5f560bd21e8dbe8856fc15d501508382ce6fc70fa7a461f930`。实际进程发布 SHA 和配置哈希一致，冻结配置/overlay 挂载保持只读。
- 源采集和独立 writer 实际写入均新鲜，最新投影只剩 `daily_origin_missing`；不是权限、冷却或市值原因。缺真实当天午夜原点，新仓暂不放行；下一个 CST 午夜后仍需源数据和各门禁真实通过，不能保证到点必开仓。
- 只读复核现存 WLDUSDT 28：两套独立库存一致，发布前硬止损 ID 和价格均未变。未人为下单或平仓。新角色实际权限符合最小授权，主交易账号对决策证据仅 SELECT。
- 独立报告账号真实 `build_report` 核对：数据库快照 coherent、无 source_error、财务 reasons 空，闭合样本 0、完整小时 0，因此仍 `PENDING/NOT_READY`，`performance_accepted=false`。不能用服务健康代替效果验收。
- 公开脱敏状态见 `hrs-production-optimization-status-2026-11-18.json`；原始逐笔事实和凭据未加入仓库。

## 首次自然扫描与热修核验

- 12:21 起的首轮扫描暴露 legacy `position_claims.created_at/expires_at` 为无时区 PostgreSQL TIMESTAMP，不能当作 aware 时钟序列化。原硬止损和维护未停，新风险仍被门禁拒绝。
- `e2b6cdc1` / `132321d4` 保留 `to_jsonb` 的数据库时间原文及 `infinity`，不猜 UTC；新增实际只读扫描上下文就绪检查。独立 36 项、联合 516 项通过。
- 隔离热修生产 SHA **3436656d698f037fc51e4fa950bd02868e4079be**，2026-11-18 **13:07:11 CST** 发布通过；[候选 CI](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44240820228) **1157 passed**。HRS/reporter 镜像 `sha256:84fc109b554509c0c947585f2c2a7e4e34a3826ee9a10d16ab58b4098574e189`，hub 镜像和容器 ID 均未变。
- 2026-11-18 **13:21:00.002304 CST** 首次真实自然扫描已完成：一个 `scan_start`、一个 `scan_end=complete`，2 个标准分析、28 个 LV-RM 分析、真实执行批次；所有记录 hash/时间 envelope 正确，财务上下文 coherent，实际 SHA/配置 hash 一致。
- 3 个市值请求批次分别为 36/30/29 标的，可信返回 27/26/25；原始行情分母 809 保留。新出现的 9 个 crypto 身份缺口已排入公开核验，未伪称 95% 覆盖通过，未去掉未知标的凑比例。
- 日初权益门禁仍为 `daily_origin_missing`；当前小时尚未闭合、自然闭合样本不足，持续效果不标通过。
- 实测约 26 分钟的 870 条公开响应已有 48.8 MB 解码数据，已确认原单体 64MiB/50k 导出容量不适用月度；正在补完整 inventory、无损 CAS/流式分片和行情源逐表采集。

## 经济与数据验收边界

- 1% 单笔风险预算按最大亏损计算；本轮不改变 SL/TP 的经济参数。已实现冻结每仓退出计划、共用实盘/离线计算和预登记实验 CLI，缺合格窗口时不能选新参数。
- 8–9 月原始决策、OI/市值时点、实际资金流等快照缺失已完成审计封存，参见 `hrs-aug-sep-final-input-audit-2026-11-18.md`。历史旧亏损统计不是新规则的反事实结果。
- 日亏 3%、精确滚动 30×24h 回撤 6% 必须依赖真实原点与持久权益证据。盘中首次启用可能因 `daily_origin_missing` 拒新风险；不能伪造当天午夜或把初始段称为完整 30 天验收。
- 24 个连续完整小时与前 5 个自然闭合仓位是未来生产效果验收。报告容器运行健康不等于技术覆盖、交易绩效或 SL/TP 已验收通过。

## 最终合并发布与上线核验

- 最终实盘源码 **441559ae8d549c9d242dc79df0f945f8539fe6a9**，2026-11-18 **15:58:36 CST** 发布状态 complete。[Python 3.11 + 全新 PostgreSQL 16 候选 CI](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44260954948) **1288 passed**，独立最终复审293项通过；实现领域459项通过。OCR发布源码范围58/58 reviewable，后续仅文档/workflow收尾。
- 36个运行源码文件、8个加法迁移的哈希及冻结配置通过；HRS/reporter镜像 `sha256:6aa43ec2d7d17e4437b03d2141d7f893fd0089b47f79914a45df4ce0f7b107b6`，hub `sha256:e789a1699664c15a7b18273ff2bd867125b13e842d819a0dc6bb7eff141fb799`。三个服务healthy、重启计数0；三个pin指向最终发布，无关容器ID不变。
- actual WLDUSDT 28两套库存仍一致，原STOP_MARKET closePosition编号 `2000001724190866`、价格0.2533未变；实际配置hash不变，挂载几何未变。没有人工交易或修改保护单。
- 市值注册表67项，hash `c7da9c8cab95c884bd744a44fdbc18ede55bc188c7f2aeb3a7b2193c08e675b0`；16:00 CST显式诊断请求新增9项全部取得可信CoinGecko市值，provider/asset_id正确、is_proxy=false、missing_reason=null。未把诊断请求说成自然扫描95%覆盖；这也不保证未来永不429。新9项CMC仍禁用，不能绕过独立口径对照。
- 新权益parent已使用628bytes forecasts_ref，16:00 CST实际525条source/forecast闭包通过、原parent hash正确且没有inline forecasts；旧证据未回写。消除forecast累计复制的二次存储项，仍有每次前缀读取/哈希及其他字段线性增长，不声称无限容量或恒定CPU。
- 历史reconciliation与离线export共用一次增量prefix校验；独立5000父/5000源只读一遍。修复删除capture或ledger_version可绕过原hash检查的问题，原始parent在任何筛选前验证。两个独立真实PG删除探针均拒绝、source查询0、ledger版本不前进。
- 16:03 CST报告快照coherent、source_error_type=null、financial reasons空；仍0完整小时/0闭合样本、NOT_READY/PENDING、performance_accepted=false。权益门禁仅daily_origin_missing，当前不允许新风险；下一CST午夜仍须真实数据及所有门禁通过，不承诺到点开仓。
- 独立归档/导出/CLI已支持完整inventory、无损CAS、COUNT/cursor、月窗固定预算和真实首扫描边界。新[最终发现报告](hrs-exit-experiment-discovery-2026-11-18-final.json)绑定实际28个引擎源码依赖，仍BLOCKED_DATA、0次实际实验运行、无参数选择；旧v1/v2/v3登记与发现事实未覆盖。
- 手工候选workflow搬到独立 `hrs-optimization-candidate.yml`；旧deploy.yml恢复为分支基线原字节，分支差异不覆盖现有main生产CI。测试过的运行源码字节未改变。隔离分支及工作树保留，未将用户原脏工作区覆盖或自动合并main。

## 最终发布后的首轮自然扫描

2026-11-18 **16:21:00.001355 CST**实际开始，16:21:59核验已完整结束，release_sha=441559ae、配置hash相同、财务快照coherent，全部payload/envelope完整性通过。6条标准分析、25条LV分析、27条LV终止记录和真实执行批次均保留。三批真实市值请求为35/35、31/31、27/27 present，原始ticker分母809未删改；这些批次有重复标的，不能合计成93个独立标的。按实际报告的event_time口径进一步验证：三批fresh_count分别35/31/27、fresh_coverage=1.0、reasons空，原始universe inventory完整；只代表requested_scan_batch，不代表809合约或518个crypto已全部取得市值。扫描捕获的两份权益parent在原hash先验后，经549条原始source的一次prefix闭包验证通过。权益门禁仍仅daily_origin_missing，ready/allowed均false。此为自然扫描的实际结果，不是此前9项诊断请求，也不等于未来24个完整小时或自然成交样本验收。

另对最近10条实际新parent按原始hash及不同cutoff核对，一次source读取校验542条源，10份ref均628bytes；没有新自然收入修订事件，不把真实PG工程用例冒称生产修订事件。

## 当前结论

这轮工程修复和受控部署完成，实际采集、原始source闭包、9项市值诊断及现仓保护已核验。SL/TP经济参数保持，8–9月缺失原始输入仍不能完成反事实收益验收；前瞻合格窗口、24个完整小时及五个自然闭合仓位仍待真实事件。不能归档为“收益/回撤已改善”。


## 后续告警只读审计：任务仍未闭环

2026-11-18 16:42 CST再次确认生产SHA仍为441559ae，未做本次风险规则或订单变更。HRS实际仅WLD28；全账户另有激进版BNB0.04、SOL-0.27。现有max_positions=3在实盘及回放都包含账户持仓和HRS未释放预留，新增SUI会算成4仓；上限口径尚未调整。旧DOGE/SUI预留已released，timeout记录0；唯一保留WLD预留经交易所GET确认FILLED且两库存仍持有28，不能删除。SUI在11月18日00:24CST后续实际FILLED10.3枚，不能把此前四条warning当作四笔失败订单。监控将这些warning汇总为错误的分类尚未修改，未确认其采集实现位置。

当前权益门禁仍daily_origin_missing。另在16:21扫描发现ARIAUSDT、BTWUSDT、PUMPUSDT、EVAAUSDT、MMTUSDT的分析日志为error/kline_history_discontinuous；数据缺口尚未修复并重新验收。此前“扫描完整”验证流程留证闭环及真实市值覆盖，不能解释为每个标的K线均健康。1288项测试和发布成功也不能替代这些生产条件。

因此整体状态明确为INCOMPLETE：已发布一批工程修复；仓位上限口径、告警分类、五项K线连续性、真实日初权益就绪、8–9月跨月反事实、SL/TP参数优选及24小时/五个自然闭合样本仍未全部闭环。

## 21:38 CST follow-up release — 3aa32e9e

User-authorized independent HRS slot admission and full original-span candle repair are deployed. The immutable HRS/reporter image is `ghcr.io/bearflower/trading-hrs@sha256:50ada1148dc0adab906d650a7337dcc97fce03e4cc858a0b68d633035826c550`; the existing hub digest `e789a1699664c15a7b18273ff2bd867125b13e842d819a0dc6bb7eff141fb799` was reused and its container ID remained unchanged. CI [44314458303](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44314458303) passed 1408 tests with Python 3.11 and fresh PostgreSQL 16. The first failed run remains recorded: one obsolete inherited base-image test used a stale mock. Cleaning the test stage and copying the entire tracked test inventory resolved it without skipping repository tests or changing runtime stages.

Production state reached complete at `2026-11-18T13:38:05.028913+00:00`. All 39 bound source hashes, effective configuration, read-only mounts, collector and independent writer initialization passed. Other container IDs were compared against this deployment's actual rollback plan, not a stale earlier snapshot, and remained unchanged. Real post-release GET/readonly-RR proof at 13:40 UTC establishes account BNB/SOL/WLD=3, HRS WLD=1, retained HRS reservation union=1/3, full-account margin 14.636942408625 USDT. WLD remains28 and its original hard stop at0.2533 is intact. Its actual FILLED submitting reservation remains retained, and timeout count is0. These are component diagnostics, not an attempted entry.

The raw equity gate remains denied solely for `daily_origin_missing`. No midnight NAV or historical profit is manufactured. The reporter is fresh/coherent; zero consecutive complete hours and zero eligible closed lifecycles remain below24/five. Its aggregate mode counts span multiple releases and must not be represented as new-release failures or fills. First new natural scan is expected at22:21 CST and is still pending at this journal entry.

The new v4 preregistration explicitly binds v2 admission before future research inputs. Twenty-four exit candidates, costs, minimum20 lifecycles and original windows remain; the original production baseline provenance is retained. Current discovery is BLOCKED_DATA with zero engine runs. Actual source closure for the five repaired symbols passed across production Python3.11 and local3.12; old failed receipts remain untouched. Exact current gate SHA/real clock/full claims and actual consumed arrays are source-bound in replay. Missing facts block rather than permit older-parent fallback.

Overall financial optimization remains INCOMPLETE. See the current JSON status, v4 registration and follow-up review; do not archive this as completed performance acceptance.

## November 19: actual midnight and coverage follow-up

The first `3aa32e9e` natural scan at22:21 CST verified 29/29 real candle repairs, including all five prior discontinuous symbols. Its three cap batches were29/32,28/29,25/25 on actual raw denominator814=518 crypto+243 TradFi+53 unverified metadata. Newly missing ICNT/PIPPIN/PIEVERSE identities were independently verified against official Binance, provider and project contracts; only those three definitions were added, with CMC disabled. Source `1cfd61f8` passed49 independent tests and CI44330844631 passed1409; release17660e0f completed23:31:21 CST.

At exactly00:00 CST the original checkpoint exists with actual marked equity31.889305060, units32.043698480 and NAV0.9951817852706246036303359948. It was created at00:01:13.115222 after real closed mark/fill reconstruction. The first collector attempt had incomplete funding eligibility; the natural retry established the boundary and restored ready/allowed. This is inception protection, not certified full30-day history. The persistent originals were read later; independent realtime observation across midnight was interrupted, and no continuous manual observation is claimed.

The176 first00:21 and07:22 scans independently passed29/29 and28/28 raw candle closures. Seventeen original cap batches had a missing-symbol union of exactly ZIL/ZKC/ZKP; the00–01 slot's32/34 was below95%, while the06–07 slot's26/27 and21/22 each passed. Later07:22 coverage26/28 and18/20 was insufficient. The six-hour streak was therefore never24-hour acceptance, and no old batch is backfilled after registry corrections.

These remaining three identities were verified with primary project/contract evidence. Native ZIL is not misidentified as its BSC wrapper; ZKP maps to zkPass and ZKC to Boundless. Actual new public-response fixtures preserve original SHA and receipt clocks. `bb55ed85` was independently reviewed4/4 with51 tests; CI44425505039 passed1411. Release6b3d14118dc2c3b2474769ff389c23c9abc7c17f completed08:31:35 CST, with73 registry entries and unchanged39 runtime source bytes, configuration, SL/TP and risk-budget percentages. Current entry gate is ready/allowed; original WLD28 hardSL and27 other containerIDs are independently unchanged. The next new natural scan is09:21 CST; current cache diagnostics are explicitly separate from natural scans. Full financial optimization, August–September counterfactual validation, a SL/TP winner and24hours/five eligible natural closures remain unaccepted.


## November 19 09:01 CST: bounded secondary-provider qualification

Server-side CoinGecko returned HTTP429 at00:33 and00:44 UTC even with all73 identities registered. The exact35-symbol diagnostic union had23 valid caps and12 provider failures, with zero identity failures. This is an availability defect distinct from an identity defect; neither dropping missing symbols nor extending stale quote lifetime is accepted.

An authenticated read-only CMC batch and an independently recorded public CoinGecko batch were compared with original raw body hashes and receipt clocks. Source quote timestamps differ113seconds. AKE/LIT/MMT/PIPPIN meet both cap and circulating-supply relative difference<=5%; the other8 disagree and remain disabled. CMC identity checks distinguish Lighter39125 from Litentry and Momentum38231 from XMM. Qualification describes this actual paired observation, not permanent equivalence. The exact recorded429 integration test proves only qualified IDs reach the secondary request, with all8 disputed and unknown IDs denied.

Source a1a405f8 plus53907fd3 passed independent5/5-file review,72 combination tests, and [CI44432307179](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44432307179)1412 tests in207.20s. The earlier failed CI44431833018 remains recorded: three old assertions unconditionally disabled now-qualified assets, and were replaced by exact-ID/hash/numerical-bound assertions. No test inventory or runtime threshold was removed.

Immutable release facde71034e9d8ce8803d98e34acfabb871f44eb completed at2026-11-19T01:01:51.034962Z. HRS/reporter digest6af532b5c6fbf8a3eb330e064b1effb5a887fea65df55c9fbea4fc63dbfd62c0 and hub1549e2c1a5d074217cb5cf372ec704d25caa4ea06096a6263e9e40e52a921581 are pinned. All39 runtime source hashes, unchanged effective configuration and actual new collector/writer initialization passed. Gate ready/allowed is true with no reasons. This data-only registry qualification leaves SL/TP, numeric budgets and v2 runtime source bytes unchanged. Independent current source/protection, live fallback coverage and09:21 natural-scan checks follow separately; deployment is not a performance claim.


At09:20 CST the safe presence-only check detected the user-supplied Demo key field with CMC retained. Two bounded actual MarketCapHub authenticated probes, respecting persistent provider backoff, did not receive a valid quote. The second at09:24:32 returned HTTP401/error10002, not429. The key value was never exported or printed. The resident hub has not been recreated with the unverified key; planned dotenv parsing is checked before concluding the credential itself needs correction. This is not reported as authenticated primary recovery or a successful production key reload.


The first facde09:21 scan completed at09:21:47.377175CST. Root independently revalidated362 necessary payload hashes, both actual equity parents and26/26 complete raw1h/4h repair closures with the actual captured config. Original cap batches32/33,26/26,21/21 all meet95%; IRYS alone lacked identity. Four genuine candidates reached budget sizing and all were denied minimum_order_exceeds_risk_budget with risk_limit0.5236USDT and quantity0; no successful reservation or new order is claimed. IRYS was subsequently verified with official Binance/CG/project sources in2b213530,3/3 independent review and74 tests, with the old73 definitions and byte prefix unchanged. Historical09:21 missing evidence remains untouched.

After the user's real-key confirmation, safe planned-Compose parsing matched the probe, without whitespace or placeholder values. Both official Demo authentication methods then returned200 Bitcoin quotes, and the project's actual MarketCapHub AKE probe returned200 with a valid real cap at09:33:57. Prior401/10002 results remain; their root cause was not uniquely established, so no credential or code defect is invented. The verified key will load in the final registry74 release, then actual resident environment/quote verification follows.

Candidatea396 passed1412 but was deliberately not deployed: final inventory audit found the explicit cloud CLI did not collect the newIRYS file or existing cap provenance file. Commit9342acaf preserves every previous test selection and adds exactly those two files; independent1/1 review confirms54 total tests across the three cap files. Main productdeploy.yml is untouched. CI44438544547 is the fresh full inventory candidate, with identical runtime source bytes. Reporter liveness also passed by actualhealthy/restarts0/noOOM/atomic fresh file/idleClientRead evidence; the earlier read occurred during report construction, not a proven fault.


## Final verified release: November19 09:47:33CST

Immutable release4316920f0de898e3367d351e90415e035851033c completed at01:47:33.644126UTC. [CI44438544547](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44438544547) passed1447 tests in97.40s, including the previously unselected provenance andIRYS files. HRS/reporter digestbd5b829c07e0adf74cdc24449a0832a647327b4785f53dc621790a0534ba157b andhub33f29af015334afb31e24ade9791fa6e8cecc2acc122e83490c5cb4908ee7953 are pinned. All11 public bundle files matched the local SHA inventory before apply;39 runtime source hashes and unchanged config verified. Actual collector/writer initialization was fresh at01:47:30, gate ready/allowed with no reasons. Full rollback plan compared the actual27 unrelated containers, all unchanged.

Independent01:49 post-apply proof establishes registry74/SHA b384824305c567580cd7086913b3a73e38df697548107254de1192694b83f946, both config mounts read-only and correct pinned images. Only booleans were emitted for actual residentDemo andCMC equality to the existingsecret, both true. One36-symbol current cache diagnostic (old35-union plusIRYS) returned36/36 fresh real nonproxy caps; the actual residenthub made oneCGcall, zero429 and zeroCMCcalls. NewIRYS provider ID=irys, value49,883,067. No cache/backoff was cleared. All17 prior request sets have full coverage using current quotes, but none of their historical failed observations is rewritten. Four newly qualifiedCMC fallbacks remain enabled; eight disagreements andIRYS secondary remain disabled. This is current requested coverage, not all518crypto or814tickers, and not evidence of a live secondary-triggered order.

Actual preflight retained WLD28 and originalhardSL2000001724190866 at0.2533; current read-onlyDB agrees with the originalSL andone retainedsubmitting reservation. No artificial trade was created. Next post-release natural scan at10:21 has not yet occurred; the09:21 first-natural proof belongs tofacde with exactly the same39 runtime source bytes. Its4 candidates were rejected by the actual minimum_order_exceeds_risk_budget rule, not accepted orders. SL/TP numbers,1%/3% budgets andv2scope are unchanged. Overalloptimization remainsINCOMPLETE: missingAug–Sep original snapshots, noSL/TP winner, and24hours/five eligible naturalclosed lifecycles remain outstanding.

## November19 user-authorized minimum-order budget release

The earlier1%/3% hard-limit statements describe the policies before the audited HRS7 activation. Releasef144eb073c14ae3fdeb7f2d4239eced97aec97e1 now runs `hrs_minimum_executable_v1`: baseline1%/3% may be raised to fit the current minimum supported entry/TP1 lot. This explicitly changes effective risk limits while preserving numeric SL/TP and other hard gates. Fresh local and CI inventories each passed1652 tests; actual policy7/gate7,40 runtime hashes, original WLD protection and27 untouched containers were verified at11:34CST. Complete financial optimization remains INCOMPLETE. See [current deployment record](hrs-minimum-order-risk-floor-deploy-2026-11-19.md).
