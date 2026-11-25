# 币安主账户与网格子账户原地切换

适用于同一币安 UID 由 PM 转为经典 USDⓈ-M、继续使用原交易凭据所属账户的情形。用户在上线窗口平掉主账户和全部子账户的仓位。工程侧负责停止新增订单、查询残留委托、采集成交、保存权益、核对归属和分批恢复服务。替换为另一个 UID 时另建账本，本流程的连续性依据是同一个账户。

## 发布前

1. 核实生产中原版、激进版、HRS、新币、网格、独立网格执行器、孤儿单清理及定时写任务的实际容器、镜像和部署 SHA。曾有单独发布的策略、网格与看板实例。逐项比对有效代码、schema、挂载和 env 来源，把当前生产热修复合入待发布版本；历史受限发布包有独立签名，使用对应的发布流程。测试分支基线为 b3a32cd2。
2. 保留所有存量账户的逻辑身份、策略状态和历史账本。主账户保持原 TRADE_ACCOUNT_ID（默认部署为 default）。核对当前 HRS、MTPCS 的实盘策略名与原来的值一致。网格子账户独立使用 GRID_SUBACCOUNT_API_KEY 和 GRID_SUBACCOUNT_API_SECRET。主账户两端都要允许同一套凭据签名读取现货 GET /api/v3/account 的 uid。权益锚点仅记录 UID 摘要；数据库拒绝绑定到另一个 UID 的经典期初。策略、经典事实采集及看板启动时复核签名身份与双锚点。先在 PM 阶段验证只读权限可用。[币安现货账户身份接口](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account)
3. 约定 UTC 毫秒 T 为两段采集的分界。两次权益取样之间维持冻结，全部平仓应在 PM 末次取样前完成。把 T 配给交易、采集、看板、调优、对账定时器的相同账户。先以 BINANCE_ACCOUNT_MODE=pm、GRID_SUBACCOUNT_MODE=pm 发布兼容版本，校验各容器生效的非敏感配置。新旧模式配置同时存在时值必须一致。
4. 按发布流程先重放生产 HRS 的八份 11 月 17 日幂等迁移，保留其损失账本、净值、核对、资金费、决策证据、分配审计、退出计划和条件单归属；随后依次执行 2026-11-25-account-mode-equity.sql、2026-11-25-classic-entry-observation.sql 和 2026-11-25-account-fact-provenance.sql。迁移依赖既有交易活动观察表及成交、收入事实表。由迁移管理员按项目契约执行权限 apply 和 audit，全部通过后才激活新服务。生产权限、既有独立发布和数据库版本需在目标环境核实。按各交易标的核对全仓与目标杠杆，经典主账户采用单向、单资产。主账户实盘入口启动时校验交易权限、持仓模式、资产模式及当前切点的双侧权益锚点。[币安 USDⓈ-M 账户契约](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)

PM 写入者全部停止后，经典阶段才执行 `2026-11-25-scoped-open-condition-ids.sql`。它将保护单在挂唯一键限定在账户、策略和标的内。生产角色的搜索路径优先读取 `btc_eth.condition_orders`，另有 `public.condition_orders`。初始化在一个事务中归档两张表内已证明属于本账户的旧记录，再标记为 `CUTOVER_RETIRED`。账户为空或属其他账户的行保留。

## 已核实的生产基线（2026-11-25，北京时间）

生产 PostgreSQL 15.19 使用交易账本 trading。用业务角色在只读事务中按近 90 天查询 trade_records，得到下表。数量按交易记录行统计，同一笔委托可以产生多条记录；这张表不用于计算收益。

| 策略标识 | 账户标识为 default | 账户标识为 NULL |
|---|---:|---:|
| btc_eth | 13 | 121 |
| btc_eth_aggressive | 185 | 1,006 |
| hrs | 65 | 1,123 |

对在运行的原版、激进版、HRS、采集器和看板分别发起了一次签名现货账户只读查询，五处返回同一 UID，网格执行器凭据返回另一个 UID。主账户的签名子账户列表仅有 1 个子账户，其 subUserId 与网格执行器的 UID 一致。[币安子账户清单接口](https://developers.binance.com/en/docs/catalog/vip-and-institutional-sub-account/api/rest-api/account-management)

三种标识均有生产历史。保留 NULL 行的原始内容；即使该行写有策略名称，也要用独立的账户、订单与成交证据连接到收入。运行已有的逐事件 reviewed/derived 归属流程，记录可证明的归属与未决项。不要把这些行统一更新为 default。上线日重采此表与切换窗口报告，近 90 天是滚动区间。

另一次生产核对在同一 PostgreSQL 只读、可重复读事务中，对北京时间 11 月 1 日 00:00 至 11 月 25 日 00:00（不含）运行现有主线报表与候选报表。启用了同一组 reviewed 和 derived 逐事件归属。仅在该验证进程内将窗口末端设为模拟 T，以检验 PM 历史。两份报表均有 395 条收入事件，已实现盈亏、手续费、资金费及净额逐策略一致；覆盖缺口、成交缺少对应收入、归属冲突、待归属均为零。

| 主账户策略 | 原口径净额（USDT） | 切换候选口径净额（USDT） |
|---|---:|---:|
| MTPCS 原版 | +2.41152268 | +2.41152268 |
| MTPCS 激进版 | −8.38938822 | −8.38938822 |
| HRS | −19.49421621 | −19.49421621 |
| 新币 | −2.42792296 | −2.42792296 |
| 主账户合计 | −27.90000471 | −27.90000471 |

权益锚点另有计价口径。PM 期末取 PAPI accountEquity 的美元估值，经典单资产期初取 FAPI totalMarginBalance 的 USDT 余额。跨段收益率在各自的首末权益上求比例再相乘；钱包范围、抵押折算及 USD/USDT 的差异需单列核对。[币安 PM 账户字段](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/account) · [币安经典合约账户字段](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)

利润提取提醒在经典期按两侧权益锚点的比例换算原初始资金和历史高点，以经典钱包的实际 USDT 保证金权益判断是否创新高。经典期新高单独保存。

这次核对覆盖已入库的 USDT 交易收入；网格子账户独立核算。上线窗口需要再次核对人工平仓成交、迟到收入和经典期初权益。

在实际容器中读到的 MTPCS 原版、激进版发布标识均为 72579274，HRS 为 471844f4；新币镜像标签为 1878e773。网格执行器 fc8fd997、实时服务 c6315a04、信号与通知 ac33f8d6、导出 af8ee812、投影 ab38fbc7 分开发布。局部发布沿用过旧 VERSION 文件，上线时逐服务核对实际包内源码散列和生效配置。

## 平仓交接

冻结新开仓、加仓、自动补网格及可重新开仓的定时任务。若某写入服务缺少独立开仓开关，在交接人已就位且已核实交易所保护单有效后，先停止写入容器再采证。主栈写入服务为 btc-eth-strategy、btc-eth-aggressive-strategy、hrs-strategy、new-coin-strategy、grid-strategy，网格独立执行器 grid-executor 需要在其实际部署项目中同时停止。容器停止后交易所已接收的保护单继续存在，本地追踪或补挂要等策略重新运行。确认币安与宿主上没有其他自动开仓入口，再交接人工平仓。在用户操作前先保存下列平仓前证据，尤其是同一交易对上原版、激进版及 HRS 的策略认领、入场委托标识、仓位方向和数量。用户完成平仓后停止交易写入者，保留事实采集；清除残留普通单和 Algo 单。对每个账户保留一份带时间的验收记录。以下命令在部署目录运行，读取实际服务配置；一键运维命令不代替账户持有人平仓。

```sh
umask 077
mkdir -p "$EVIDENCE_DIR"
docker compose run --rm --no-deps -v "$EVIDENCE_DIR:/evidence" execution-facts-collector python -m scripts.check_account_cutover --account main --before-close --output /evidence/main-before.json
docker compose run --rm --no-deps -v "$EVIDENCE_DIR:/evidence" execution-facts-collector python -m scripts.check_account_cutover --account grid --before-close --output /evidence/grid-before.json
# 用户平仓、执行侧确认清理余单后执行
docker compose run --rm --no-deps execution-facts-collector python -m scripts.check_account_cutover --account main
docker compose run --rm --no-deps execution-facts-collector python -m scripts.check_account_cutover --account grid
```

EVIDENCE_DIR 使用受限权限的绝对路径。平仓前的记录保留原始证据，最后两次检查的结果必须为 flat=true，ordinary_count、algo_count 均为零，open_symbols 为空。以上检查覆盖 USDⓈ-M。另在币安逐账户核对跨仓杠杆借款、币本位持仓和挂单、负余额及产品模式。额外子账户逐个使用 --account other；通过独立的一次性容器传入 CUTOVER_SUBACCOUNT_ID、CUTOVER_SUBACCOUNT_MODE 和对应 CUTOVER_SUBACCOUNT_API_KEY、CUTOVER_SUBACCOUNT_API_SECRET，先带 --before-close 保存证据，随后再验空仓。额外子账户的证据不载入主账户策略认领。每个文件包含签名 UID 摘要，验收时逐账户比对前后摘要、数量与完整子账户清单。[币安账户切换条件](https://www.binance.com/en-KZ/support/faq/detail/7ee6b3f65d5a421491c0a5588223fd14)

在 T 前、最终平仓清单后采样主账户 PM 期末权益。evidence-ref 指向本次验收、钱包和划转记录。T 到达后继续保持 PM 身份，最后补拉截至 T−1 的 PM 时间窗，核对所有平仓成交、佣金、资金费和人工平仓归属。命令中的环境变量由当班人员按实际窗口设置。遇到交易所延迟入账应等待并复核原始流水与持久化结果；已经封口的历史游标不能证明最近一次复查再次读取过交易所。

```sh
docker compose run --rm --no-deps execution-facts-collector python -m scripts.capture_account_mode_equity pm_final --evidence-ref "$PM_EVIDENCE_REF"
docker compose run --rm --no-deps execution-facts-collector python -m scripts.collect_execution_facts --start-ms "$PM_START_MS" --end-ms "$((BINANCE_CLASSIC_CUTOVER_MS - 1))"
```

在 PM 数据封口后，先存下固定窗口的报告；切换后对同一账户、月份、截止毫秒、配置和归属选项重跑。下例适用于 T 落在月内；若 T 为北京时间月初，取前一个自然月，以 T 作为该月排他右边界。报告命令返回 2 时会照常输出报告，需逐项核对报告中的缺口。其他非零状态表示命令失败，不能用其输出作证据。

```sh
historical_report() {
  docker compose run --rm --no-deps -T -v "$EVIDENCE_DIR:/evidence" execution-facts-collector python -m scripts.report_economic_ledger --output "/evidence/$1" \
    --month "$PM_BEIJING_MONTH" --end-ms "$BINANCE_CLASSIC_CUTOVER_MS" \
    --use-reviewed-overrides --use-derived-attribution --include-hrs-provenance
}
historical_report pm-at-freeze.json
# 在经典采集、归属重放完成后执行
historical_report pm-after-replay.json
diff -u "$EVIDENCE_DIR/pm-at-freeze.json" "$EVIDENCE_DIR/pm-after-replay.json"
```

若有差异，以新增的迟到成交、修订的审核证据或归属冲突逐项说明，保留前后两份结果。跨月再按自然月分别读取完整历史月份；固定窗口报告中的三策略净额、待归属、来源和覆盖状态须可复查。

确认零敞口期间没有额外交易。修改宿主 .env 之前，用已审核候选制品执行 python3 ".deploy_tmp/account-mode-$RELEASE_SHA/cutover_income_timer.py" pause --sha "$RELEASE_SHA"，保存原收益归属定时器状态。完整发布的健康检查阶段验证并绑定新镜像摘要，对同一窗口做影子核对后按原运行状态恢复定时器。

在已确认的上线窗口完成各账户的币安模式切换后，分别设置主账户 BINANCE_ACCOUNT_MODE=classic、网格 GRID_SUBACCOUNT_MODE=classic。保留同一 T、同一账户 ID。已启用网格执行器时，新建独立 GRID_EXECUTION_DB 路径供信号生产、执行、导出与迁移共用，归档原 PM SQLite。保持原 PM 文件及历史快照可读取；先对新文件运行 grid execution store 迁移。网格的 PM 与经典 PostgreSQL 投影分别使用 grid_pm_ethusdt_01 和 grid_pm_ethusdt_01:classic；其他配置的账户标识沿用相同的 :classic 后缀。详见[网格运行手册](grid-order-executor.md)。独立网格检查 ETHUSDT 的单向、全仓、杠杆、余额别名和风控限额。

## 先恢复数据，再恢复交易

1. 用经典配置重新执行两个只读核验。逐账户核对可交易权限、目标钱包及初始资金，记录人工划转和钱包范围差额，先不启动交易写入者。
2. 宿主侧看板月度采集与 AI 观察任务也要使用新脚本。完整发布流程会检查并安装 cutover-host-watchers.pyz。受限发布将已审核的同一制品放入主机，在获准的服务范围内先执行 python3 cutover-host-watchers.pyz --scope dashboard --check，再执行 --apply；AI 范围使用 --scope ai。安装后比对脚本散列及原定时任务状态。
3. 在所有交易写入者仍冻结时，使用 PM 兼容发布已暂存的同一提交执行下列迁移和权限核验。随后创建主账户经典期初权益，重启采集、对账及看板服务并核对镜像和生效配置。单纯修改 env 文件不能改变已运行容器的环境。两次采样必须在 T 的正确一侧，并且都有普通与 Algo 空挂单证明。

```sh
ACCOUNT_MODE_DIR=".deploy_tmp/account-mode-$RELEASE_SHA"
docker exec -i trading_system-postgres psql -X -v ON_ERROR_STOP=1 -U trading_migration_admin -d trading_platform < "$ACCOUNT_MODE_DIR/2026-11-25-scoped-open-condition-ids.sql"
python3 "$ACCOUNT_MODE_DIR/database_permissions.py" apply --manifest "$ACCOUNT_MODE_DIR/permissions.json"
python3 "$ACCOUNT_MODE_DIR/database_permissions.py" audit --manifest "$ACCOUNT_MODE_DIR/permissions.json"
docker compose run --rm --no-deps execution-facts-collector python -m scripts.capture_account_mode_equity classic_open --evidence-ref "$CLASSIC_EVIDENCE_REF"
docker compose run --rm --no-deps execution-facts-collector python -m scripts.initialize_classic_runtime
docker compose run --rm --no-deps execution-facts-collector python -m scripts.collect_execution_facts --start-ms "$BINANCE_CLASSIC_CUTOVER_MS" --end-ms "$CLASSIC_END_MS"
docker compose run --rm --no-deps execution-facts-collector python -m scripts.report_economic_ledger --month "$BEIJING_MONTH" --end-ms "$((CLASSIC_END_MS + 1))" --use-reviewed-overrides --use-derived-attribution --include-hrs-provenance
```

4. 比较报告内 PM 和经典分段、三个策略的单独金额、未归属项、冲突、成交无对应收入、覆盖缺口。人工平仓必须通过订单、成交、持仓生命周期或有证据的审核映射分配；同币种、多策略和资金费缺少持仓证据时保留未归属。在 PM 签名接口仍可读的冻结期，将旧风险预留、持仓认领和挂单绑定逐笔与终态核对，保存原始回执并通过策略既有释放流程结清。交易进程启动时发现 T 前仍为 reserved/submitting 的风险预留或 PENDING/ACTIVE 的认领就会停止启动，采集与看板可继续核对差异。历史原有缺口单独列出，切换窗口新增差异须解释、完成核对后才能放量。账户收入应等于逐策略收入加未归属项，按资产分别比对。PM 期末权益与经典期初权益单独列账，差额不计策略亏损。
5. 使用已批准的小额额度恢复一个策略；核对普通单、止损及止盈 ID、交易所与本地成交、手续费、平仓记录、重启后的保护单归属。再分别恢复 btc_eth、btc_eth_aggressive、hrs 及其他原已启用实例，并对每个实例重复核对。实际上线后才会出现的成交不得用测试流水填补。经典账户独立风控政策批准前，AI 调优的 PM 专属回撤预算维持冻结。
分批启动仅使用已审核镜像，按服务名执行 `docker compose up -d --no-deps <service>`。主账户及已启用的子账户执行器均验收后，再执行本切点第一次经典完整发布。完整发布会先读取各写入容器的实际模式和切点，尚有冻结或运行在 PM 的实例时停止，以免一次唤起全部策略。

6. 任一账户身份、保护单、流水、权限、行情或权益校验失败时暂停增加敞口，保留已有保护并处理实际成交。若尚未切账号，可继续使用已验证的 PM 版本。切换成功后的第一处置是留在经典账户接管现仓；需要回到 PM 时重新执行交易所空仓清单和双段核算，禁止只改路由或删除游标。

### 漏采 PM 期末权益时单独恢复主账户事实采集

交易写入者继续冻结。对已停止 PM 且两侧权益锚点都没有入库的账户，`scripts.collect_execution_facts` 接受采集专用凭据。它按 T 限定经典采集窗口，使用仅放行 GET 的币安客户端，并在每轮采集前重新签名查询现货 UID。收益换算及利润提取继续要求双侧权益记录；单独配置采集清单时交易写入者继续冻结。该流程不创建权益锚点或运行状态标记。

保存三份完整 JSON 原件，以各自文件的 SHA256（64 位小写十六进制）加 `.json` 为文件名，和 `collector-recovery.json` 放在同一目录。沿用冻结交接与账户复核脚本的回执格式。三份原件分别是：

- PM 冻结交接的 `handoff-confirm-main.json`，含签名 UID 摘要、采样起止毫秒、两份 UM 仓位、普通和 Algo 单清单。
- 平仓核对的 `rotated-result.json`，含交易写入者冻结清单、缺失的 PM 期末采样、已撤保护单数、平仓成交及收入核对数、经典账户资产和敞口。
- 经典签名复核的 `rotated-proof-main.json`，含同账户 UID 摘要、PM 模式已关闭的账户信息、单向单资产配置、账户权益、独立仓位与两类挂单、每个旧仓位的 reduce-only 平仓回执和逐笔成交、费用及已实现盈亏，以及旧保护单的终态。

清单字段如下，时间使用实际 UTC Unix 毫秒。下列尖括号字段应替换为对应回执的值，时间字段以 JSON 整数写入。

```json
{
  "version": 1,
  "scope": "execution_facts_collector",
  "reason": "missing_pm_final",
  "account_id": "<TRADE_ACCOUNT_ID>",
  "cutover_ms": "<T>",
  "pm": {
    "uid_sha256": "<PM 回执 uid_hash>",
    "identity_observed_ms": "<PM 回执 end_ms>",
    "identity_evidence_sha256": "<PM 冻结交接文件 SHA256>",
    "closure_evidence_sha256": "<平仓核对文件 SHA256>",
    "closure_reconciled": true,
    "writers_frozen": true
  },
  "classic": {
    "uid_sha256": "<经典回执 queries.uid.data>",
    "observed_ms": "<经典回执 end_ms>",
    "total_margin_balance": "<经典签名账户 totalMarginBalance>",
    "open_symbols": [],
    "ordinary_count": 0,
    "algo_count": 0,
    "snapshot_evidence_sha256": "<经典签名复核文件 SHA256>"
  }
}
```

将该目录以只读方式挂入专用采集容器，例如 `/run/classic-collector`。仅为该容器设置 `CLASSIC_COLLECTOR_RECOVERY_MANIFEST=/run/classic-collector/collector-recovery.json` 和 `CLASSIC_COLLECTOR_RECOVERY_SHA256`（已审核清单原始字节的 SHA256），并配置同一 `TRADE_ACCOUNT_ID`、经典模式及 T。先执行一次 `python -m scripts.collect_execution_facts --days 2`；确认报告的账户、模式、T、`collector_recovery_sha256`、采集起点和 `complete=true` 后，才以同一配置恢复该采集服务的轮询。任一凭据不符或数据库出现任一权益锚点，采集轮次将失败。保持 PM 期末净值缺口和原 PM 游标，单独归档已知的历史覆盖缺口。

### 已批准解冻主账户交易，PM 期末权益仍然缺失

在账户持有人单独确认恢复交易后，为已批准的主账户写入者准备交易专用清单。复用上述三份原始回执和同一个 T，将清单的 `scope` 改为 `classic_trading_runtime`，另存为 `trading-recovery.json`。对最终文件重新计算 SHA256。把清单与三个以摘要命名的回执一起只读挂载到 `/run/classic-trading`。向初始化容器和该批已批准的主账户策略容器提供两项独立配置：

```sh
export CLASSIC_TRADING_RECOVERY_MANIFEST=/run/classic-trading/trading-recovery.json
export CLASSIC_TRADING_RECOVERY_SHA256="${REVIEWED_TRADING_MANIFEST_SHA256:?缺少已审核摘要}"
```

1. 保持所有交易写入者冻结，使用已审核镜像、原账户 ID、原 T、经典模式、数据库写入角色和真实凭据，执行 `python -m scripts.initialize_classic_runtime`。该命令在原归档事务取得账户锁后复核清单和三份原件，重新查询签名 UID、现时 PM 状态、密钥合约权限和单向单资产配置，并确认切点前风险预留及认领已结清。发现任一已入库的权益锚点时，须核对双侧证据。
2. 首次初始化需要正的经典合约权益，同时核对两份实时签名仓位和两类挂单。V3 positionRisk 返回空列表时，独立的 V3 account 仍须返回有效余额和明确的仓位列表。原有切点后交易、未归属运行记录检查及旧状态原子归档继续执行。成功标记保存交易专用清单摘要，历史权益表保持原始记录。[币安 USDⓈ-M 接口变更说明](https://developers.binance.com/zh-CN/docs/products/derivatives-trading-coin-futures/change-log) · [币安 API Key 权限接口](https://developers.binance.com/en/docs/catalog/core-trading-wallet/api/rest-api/account)
3. 分批启动已批准的写入者。每次重启都重新验证本账户签名身份、交易权限、清单、原始证据、旧风险预留及标记的摘要；允许接管已在经典阶段建立的仓位。HRS 继续独立检查持仓表、备份和超时恢复任务。出现摘要、配置、身份或持仓归属冲突时，先核对对应证据。跨 PM 与经典的收益率和利润提取继续保留缺失状态。

## 三套策略的数据核对

| 策略 | 信号 / 生命周期 | 成交及收益 |
|---|---|---|
| MTPCS 原版 | btc_eth 信号及原版策略状态 | btc_eth |
| MTPCS 激进版 | btc_eth_aggressive 信号及独立状态 | btc_eth_aggressive |
| HRS | hrs 信号、持仓生命周期及保护角色 | hrs |

同一账户的原始成交、收入集中落库。策略归属由同一账户、交易对、成交与真实订单关联证明。Algo ID 和触发后的实际订单 ID 分开核验。归属凭据区分 pm_algo_link 与 classic_algo_link；经典保护单需把切点后的策略委托回执与签名订单结果关联。经典收入及成交采用单独来源的采集游标；T−1 属于 PM，T 属于经典。跨日、跨月统计以北京时间展示；日净值依赖有来源的权益快照和切换锚点，缺失部分保持未知。网格子账户通过单独凭据、资产账本和快照核算。

同一交易对的普通委托号可跨切点复用。HRS 的净值、资金费持仓、风险槽及导出均按所属模式读取。成交事实的唯一键为账户、交易对及成交 ID，收入事实的唯一键为账户、收入类型及流水 ID。若同一事实键收到相互矛盾的内容，采集记录缺口并保全原始事实，须取得交易所凭据后核实。

## 经典保护单结果未知时的恢复

当策略报告 `UnknownOrderResultError`，先停该策略的新增风险。每个实盘策略在既有持久卷 `/app/data/classic_algo_intents.sqlite3` 里先写账户、策略、币种、保护类别、原始 `clientAlgoId` 和无签名的完整参数，再发一次 POST。同类补单、重启以及限价与市价回退都受同一记录约束。即使普通及 Algo 挂单列表均为空，该记录仍阻止再次提交。

1. 从相应主机目录读取待核实记录，例如 `sqlite3 -readonly data/btc_eth/classic_algo_intents.sqlite3 'SELECT account,strategy,symbol,family,client_id,request,started_ms FROM pending'`。新币、激进版和 HRS 分别使用各自的数据目录。
2. 在原账户配置的 `ReadOnlyBinanceClient` 中调用 `await client.get_algo_order(client_algo_id=client_id)`。保留原始响应，核对原始客户端标识、`algoId`、交易对、方向、类型、数量、触发价、创建时间和状态。缺少结果或 -2013 时继续保留意图。若已有 `actualOrderId`，按该真实子单号调用 `get_order(symbol, order_id=...)` 和 `get_user_trades(symbol, order_id=...)`，核对逐笔成交、手续费、剩余仓位以及普通和 Algo 挂单。已触发但仍在挂的普通子单也需要接管。
3. 挂单仍有效时，以该回执恢复唯一的账户级策略创建记录，并按该策略的入场生命周期、止损角色或 TP 档位绑定原 `algoId`。终态则按真实子单及其成交完成策略状态与账本核对；没有子单仅能依靠交易所明确的未成交终态释放。保存原始响应、未决记录和核对结果后，在停止策略写入的情况下，用 `ClassicAlgoIntents(path).resolve(account, strategy, symbol, client_id)`（`shared.accounting.classic_algo_intents`）仅解除已核实的这一条。激进版还须按其原有 STOP journal 与入场 ID 恢复绑定，再启动策略巡检。

### HRS 恢复状态及宿主收益定时器

切换前保留 HRS 净值分段、单位净值、资金分配审计、日内和滚动亏损锁。冻结入场后，以 PM 原接口核对人工平仓订单、成交和费用，等待 HRS 处理已完结订单并保存持仓状态。HRS 重启会同时读取持仓表、JSONB 备份以及可能继续撤单或平仓的超时恢复任务。在部署目录用只读事务检查这三处并保存结果。

```sql
BEGIN READ ONLY;
SELECT symbol,direction,position_id,opened_at,remaining_quantity,algo_ids
FROM hrs.hrs_positions ORDER BY symbol,direction;
SELECT state_data->'positions'->'positions' AS backup_positions
FROM hrs.hrs_state WHERE state_key='main';
SELECT key,value FROM hrs.hrs_meta WHERE key LIKE 'hrs-timeout:%' ORDER BY key;
ROLLBACK;
```

账户已平仓而某条恢复记录仍存在时，从停机前的 SQL、JSONB 及 HRS 日志提取原持仓 ID、入场 orderId 和保护 algoId。以 PM 签名查询原订单、父子条件单、实际成交、平仓数量及账户当前零仓，按策略、标的逐条留存证据。允许原 PM 进程在冻结入场期间完成恢复和落库。对无法自然结清的记录，保留原行与证据，按现有数据修复流程复核主表及备份中的同一持仓，再重复只读检查。保留亏损锁、资金审计及历史净值行。切换后经典 HRS 启动门槛会拒绝切点前的持仓、备份或超时任务。

宿主的 `income-attribution.timer` 固定使用单独的镜像摘要。将下列命令中的 `RELEASE_SHA` 设置为已审核且已暂存的完整候选提交。**在把宿主 .env 改成 classic 之前**暂停旧定时器，等待执行中的核对完成。如果提示正在运行，待其结束后重跑相同命令。

```sh
python3 ".deploy_tmp/account-mode-$RELEASE_SHA/cutover_income_timer.py" pause --sha "$RELEASE_SHA"
```

经典完整发布在服务健康检查后，用实际 HRS 镜像摘要预演当月收益归属。预演须确认账户 ID、经典模式、切点与核对窗口。通过后，安装 systemd 镜像覆盖配置并恢复原启用状态。核对记录在 `.deploy_tmp/account-mode-$RELEASE_SHA/income-attribution-shadow.json`。若原单元、定时器或包装脚本与已审计版本有差异，先复核宿主变更；若预演失败，处理后使用同一版本重试。保留单元及既有收益归属归档。
