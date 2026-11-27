# 生产发布记录（2026-11-27）

**全量发布尚未完成。** 本聊天统一完成了 22／23 个目标的实际切换。最后的网格执行器因真实持仓未清而被严格交接门禁停止，旧执行器继续管理原持仓，新候选保持 `created`、未启动。不能把以下已通过的回归和联调当作第 23 个服务已发布的证明。

## 源码与构建

- 部署源码：`acee4c82dfefda216ee35b365533284d8ada44ee`，DEPLOY_ID `00000401`。
- [CI Run 45929301380](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/45929301380)：11 个 Linux amd64 镜像及 manifest 全部成功，使用不可变 digest；774 项产物源码与提交逐项一致。
- 最终全仓回归：541 个 Python 文件、9,326 项通过，零失败、错误和跳过；另 2 项 Node 测试通过。1,371 个源码及测试文件在回归前后哈希一致。
- `open-code-review-delegate` 最终可审差异 78／78 覆盖，未解决发现 0；其余 106 个变更条目按 OCR 规则排除，不算已审代码。完整记录见本目录的回归和 OCR 文件。
- 权限契约审计：1,353 个对象、零问题，各次激活前再次审计。没有写入权限或更改业务风险政策。

后续只更新本文的提交与上述实际部署源码区分。原工作区中的无关未提交修改及未解决文档冲突保持原状。

## 已完成的部署与联调

已切换：BTC/ETH、两个 XAU shadow、激进版、新币、网格信号、grid dashboard exporter／PG projector／notifier／realtime、HRS／HRS acceptance、execution-facts、市值、AI tuner／snapshot／funding schedule、data-backend、dashboard-api、Kline／Kline monitor、aggtrade collector，共 22 个。

两个独立研究观察器 `trading_system-hrs-research-observer`、`trading_system-mtpcs-direction-shadow` 不在本次更新范围。发布保留旧容器和过程证据，没有自动回滚、重放已成功激活或清理无关服务。

| 检查 | 当前已取得的结果 |
| --- | --- |
| 22 个已激活目标 | 独立只读验收 22／22 通过：healthy、0 重启，5,491 份运行源码哈希匹配，镜像／VERSION／配置一致，22 个旧实例已停止且 restart=no；两个研究观察器 2／2 保持一致 |
| 真实 Kline 联调 | 2026-11-27 03:23:15–03:23:52 UTC，51 项接口测试及 1 项共享客户端测试全部通过；前后健康检查均 HTTP 200；经本地 SSH 转发执行，未在服务器运行 pytest |
| 真实网页 | 看板 HTTP 200，19 个资源及 API 请求全部成功，0 浏览器脚本错误；页面显示真实网格暂停持仓及历史数据待核对状态 |
| 发布后主账户与政策 | 03:47:04 UTC 只读验收通过：主账户 5 次 GET，经典账户身份、持仓保护、资金分配和全部风险政策哈希匹配基线；HRS 同事务政策快照、配置和权益门控保留，PG projector／notifier 心跳新鲜 |

## 门禁中断与诊断

首次发布在第 13 个服务激活前遇到网格普通订单归属不一致，流程停止。只读日志显示该时点与 user-stream 重连、对账及订单变化重叠；原失败没有记录具体失配谓词，不能据此把原因断言为某一代码缺陷。原 execution-facts 未被切换，后续重新获取完整严格证据通过后，才继续剩余目标。前 12 个已完成目标没有重新激活。

发布后的配置审计首次在全部 22 个目标报告同一项差异：Docker 创建时 `HostConfig.OomKillDisable=false`，启动后 inspect 返回 `null`。精确诊断确认所有其他配置字段、网络、环境摘要及挂载相符。原失败证据保留。修正后的临时审计器先核实 daemon 的 cgroup／OOM 能力，仅对原 null→创建 false→运行 null 的比较副本做等价处理；实际 `true`、缺失字段、其他漂移及未启动候选仍严格拒绝。修复经过独立复审，18 项离线边界测试通过；03:42:46 UTC 的真实复核 22／22 通过，未修改任何容器配置。

HRS 的真实活动政策为 revision 8、`reduced`，`allow_add=false`、`freeze_allocation=true`，multiplier 0.5、ratio_cap 0.15、entry_ceiling 26.18，生效于 2026-11-20，有效至 2026-12-14。完整政策及 active 指针哈希与发布前一致。临时验收脚本曾错误假设数据库政策是 `monitor_only` 且三个数值均为零，原失败保留并更正这一验收假设；本次没有把生产政策改成另一模式。

## 未完成项与真实业务限制

2026-11-27 03:26:03 UTC 的严格网格只读证据为：交易所持仓与账本净仓均为多头 0.078 ETH，普通挂单 0、保护单 1，保护有效、归属对账完成、风控 ALLOW、stream ready，状态为 PAUSED。22 次请求均为 GET，真实账本未改。该证据仅说明当时持仓一致且受到保护，不能证明现在已经空仓。

网格激活记录不存在，旧实例保持 healthy、0 重启及原 restart policy；候选未启动。下一步必须等待自然空仓，再取得全新的交易所和账本证据，只选择尚未激活的网格执行器继续；不能复用过期证明、强制平仓、伪造不存在的成交或清除风险锁。切换完成后仍须执行完整 23 服务及业务验收，才可签署全量发布成功。

HRS 扫描仍受 `inventory_owner_path_coverage_missing` 历史归属证据缺口阻断，不进行本轮评分。经典账户切换前后的历史现金流、权益锚点及归属未因代码发布而自动补齐；看板中待核对字段继续保持未知。服务健康、受限风险政策和历史财务完整性须分别判断。

## 证据位置

已提交的完整测试与 OCR 结果在本目录。部署过程的私有原始证据保存在服务器 `/root/trading_system/.releases/full-regression/acee4c82dfefda216ee35b365533284d8ada44ee/`，本机汇总位于 `/tmp/binance-production-readiness/`。凭据、完整 Env、签名请求和原始账户标识不提交入库。

机器可读的 [部署汇总](production-release.json) 记录本次真实结果和原始证据 SHA256，明确 `release_complete=false`。已发布源码及测试文件再次对照最终回归快照，1,371 项均未变化。完整发布验收只有在网格交接实际完成后才能签署。
