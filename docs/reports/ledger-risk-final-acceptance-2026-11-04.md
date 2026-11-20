# 交易所账本与风险收缩：实际发布验收报告

本报告记录 2026-11-04 发布及恢复的实际结果。最终生产执行由调度者完成并独立核验：2026-11-04 **15:23:52.097 UTC（北京时间23:23:52.097）**，第三阶段恢复 state 为 `complete / success`（updated_at=1793805832.0978196），持久 release pin 为 `active`，八个目标容器健康。本文档更新本身未执行生产命令、修改代码或推送 main。48 小时稳定性观察尚不能据此宣称完成。

## 发布身份、范围及恢复链

交易镜像对应原发布 SHA `d77c232881d75f65ae13ee4fcad33946b6c8413a`；原 manifest canonical SHA256 为 `560e60bce57b5b0f6809494444f1a6036527890be929dea6e9b3327aa0f7dc5b`。恢复代码为独立审计分支后续提交，最终第三阶段为 `f3221b89`，不会把恢复脚本提交号误写成容器镜像版本。

七个不可变镜像用于八个明确目标：HRS、激进版、BTC/ETH 原版、新币、execution-facts-collector、data-backend、dashboard-api、ai-tuner。collector 复用 HRS 镜像。使用经过审核的生产 Compose 显式组合及 release image overlay；未发布 latest，未执行 broad compose up/清理。最终门禁包括排除服务身份保全，active pin 防止后续普通 full 部署静默覆盖本次镜像。grid 不在本次风险收缩或重建范围，不能把数据库候选行解释为 grid 运行时禁单。

| 阶段 | 实际结果与边界 | 保留证据 |
|---|---|---|
| 原发布 | 备份及三项迁移完成；政策候选的 Decimal 字符串 `0.5/.5` 等表示差异造成误拒绝，尚未激活政策、未重建目标服务 | 原 `state.json` 为 failed；changed=[]；database-before.dump 28,921,034 bytes；preflight/schema/pre_activation proofs |
| 第一次实际恢复 | 有限 Decimal 语义比较修复后，政策激活成功，collector 与 HRS 切换成功；HRS 结构化日志的 Unicode 转义导致中文启动标记误判 | `manual-recovery/`；failed，changed=[collector,HRS]；新 HRS 保护重建证据 |
| 第二阶段准备与独立风险处置 | after-HRS 入口只读检查发现 MELI 变化；独立授权平仓后，将精确成交/撤单证据纳入恢复；after-hrs-recovery 未执行 | 原缺口声明、两份私有签名快照、MELI 独立授权及订单证据 |
| 第二次实际恢复 | 激进版、BTC/ETH、新币、data-backend 依次切换；data-backend 导入成功但前置 structlog 日志使整个 stdout 与成功标记不相等，consumer gate 误拒绝 | `after-meli-recovery/`；failed，changed 精确为 collector、HRS、aggressive、BTC、new-coin、data-backend |
| 第三次实际恢复 | 重新只读核验六个已切换服务、策略启动、MELI 证据链、持仓/保护/资金分配；从 data consumer gate 续跑 dashboard、freeze、tuner、final，最终 success | `after-data-recovery/state.json`、validated-live、consumer import、final proof、dashboard HTTP、transition audit；active pin |

各次恢复均持有原部署锁，绑定原失败状态/审批/代码及证据哈希；不重放政策激活、迁移或已完成容器重建，不做自动回滚。原失败状态保留，不用最终 success 覆盖原始过程。原数据库事实、审计和备份未作为恢复手段删除。

## 账本与看板月度验收

验收口径为账户 `default`、北京时间 Asia/Shanghai 月界、canonical `monthly:{start_ms}`，包含交易收益、手续费和资金费。8–10 月期望值来源于先前独立交易所核对，最终门禁要求 canonical 正式发布与看板 HTTP 返回相同 report identity、window、version 和金额，并要求完整账户覆盖、无 refresh_error；归因未知不能写成策略零收益。

| 北京时间月份 | start_ms | end_ms | canonical window_key | 交易所与看板净额（USDT） |
|---|---:|---:|---|---:|
| 2026-08 | 1785513600000 | 1788192000000 | monthly:1785513600000 | -115.45319304 |
| 2026-09 | 1788192000000 | 1790784000000 | monthly:1788192000000 | -6.65665904 |
| 2026-10 | 1790784000000 | 1793462400000 | monthly:1790784000000 | +5.80965338 |

最终成功意味着三个月均通过 `publication_status=published`、`account_coverage_complete=true`、`strategy_attribution_complete=false`，以及 dashboard 对同一 report_id/report_version 的 HTTP 对照。最终 15:23 UTC proof 与 dashboard HTTP 核验的 report_version 均为 `exchange-income-v1`，账户均为 `default`，refresh_error 为空。正式 identity 如下（完整原件保存在私有 release 审计目录）：

| 月份 | report_id |
|---|---|
| 8月 | 0ee434e00354ce61bbb510ae53dc695832fd2711b135c6500a02d0c1b2b5fe1f |
| 9月 | a36cf172e30f3bd12430c46316c6decab81c2bff6f9443e6d9a7c03234b7c9f1 |
| 10月 | f06a75cfdceaa9c650ae1858d7f743124b5a0bda07e4fd208717509140cd082d |

所有策略历史排名仍冻结，当前资本分配或小时快照不被冒充历史本金证据。

11 月属于未完成月份，其净额随新成交、费用与资金费动态变化。约 **2026-11-04 15:27 UTC**，调度者在运行中的 dashboard-api 再次 GET，11月窗口 start_ms=1793462400000、total_pnl=**-31.82606384 USDT**、account_coverage_complete=true、strategy_attribution_complete=false。这是现场动态快照，不能作为11月最终结算或三个历史月的固定验收期望值；该次动态快照未在本报告转录 report_id/请求截止时间，不据此声称已完成同 identity 的独立交易所逐项复核。

## 已激活政策与资金边界

实际冻结绝对入口上限低于最初候选 0/78/104/130，来自激活时权益进一步收紧；不得再用初始候选金额冒充线上生效金额。

| 策略 | mode | multiplier | ratio_cap | 实际 entry_ceiling（USDT） | allow_add | freeze_allocation |
|---|---|---:|---:|---:|---|---|
| HRS | monitor_only | 0 | 0 | 0 | false | true |
| 激进版 btc_eth_aggressive | reduced | 0.5 | 0.15 | 77.8063509075 | false | true |
| BTC/ETH 原版 btc_eth | capped | 1 | 0.20 | 103.7418012100 | false | true |
| 新币 new_coin | capped | 1 | 0.25 | 129.6772515125 | false | true |

HRS 新开仓与直接加仓入口拒绝新增风险，持仓监控/保护/退出保留。激进版压力风险减半、禁加仓，持久预约在超时/未知结果下保留风险占用和符号归属；未知遗留挂单不能自动放行。BTC/新币入口使用原有效预算、冻结绝对上限和实时净值比例上限的最小值，未知净值或政策不可用拒绝新单。政策表虽统一记录 allow_add=false，但 BTC/新币本轮消费的是 capped 模式/有效性/入口上限，未重写其原有预算内加仓规则或最终下单并发机制，不能宣称全账户加仓均被该字段禁止或所有策略最终写入均具备原子预约。

月度分配、每日刷新及直接 config updater 均保持冻结。最终 allocation hash 与原 preflight 相同，未降低存量 allocated_amount 来触发旧仓强平回调；MELI 平仓是另行授权的风险处置，不是政策或分配调整强制关闭。释放风险额度保持未分配，不归一化重分配；entry_ceiling 不是可提现现金。政策有效期原设计为 72 小时，缺失/过期/数据库失败拒绝新风险，不自动恢复宽松交易；线上四政策 expires_at 均为 **2026-11-07 14:11:40 UTC（北京时间22:11:40）**，须在该时间前明确复审。

## MELI 授权平仓及成本

原 MELI 空仓 -0.06 的止损限价单触发后仍未成交，用户另行明确授权关闭该风险：

| 证据 | 核验结果 |
|---|---|
| STOP algo 2000001555621647 | FINISHED；关联实际 LIMIT order 1785285；触发 1719.31，限价 1721.03，qty0.06；actualQty0 |
| LIMIT 1785285 | BUY/BOTH/reduceOnly；CANCELED；executedQty0，未作为已成交退出记账 |
| MARKET 1803995 | clientOrderId=codex-meli-stopclose-20261104-1；BUY/BOTH/reduceOnly；FILLED 0.06，avgPrice1749.30 |
| userTrade 38450 | qty0.06、price1749.30、realizedPnl=-6.7116 USDT、commission=0.04198320 USDT |
| 单次平仓收益减该次手续费 | -6.75358320 USDT；此数不是整个持仓周期含历史资金费的总收益 |
| 两笔遗留 TP | algos 2000001555621787 / 2000001555621677 均 CANCELED |
| 后置签名账户查询 | MELI 无持仓、无普通单及条件挂单；未扩大其他持仓 |

独立签名证据 raw SHA256：post-HRS `d1ebf9dacd301b707c03b506b939183d3d00b737b017fc32a312ffc4c07bf4ff`（14:23:09 UTC）；post-MELI `0dbad1540bdd398fcccf19453603c2215bcf1d0735360ff5f19592067bef06cb`（14:58:00 UTC）。两份快照中 ARIA/PFE 的敞口字段以及其五笔当前 algo 完整原始行相等；此比较只覆盖已有原始数据的两份快照，不能延伸为最初 preflight raw 比对。

## 当前持仓保护及采集

最终门禁于 **2026-11-04 15:23 UTC** 将新鲜签名查询与授权后的上述 endpoint 对照：ARIA -30、PFE -3.28，protected_positions=2，普通挂单 0、条件单 5；MELI 为零。调度者约15:15 UTC另做签名查询，五笔保护单无漂移。每笔保护单均 NEW、BUY、BOTH、reduceOnly=true。

| 持仓 | algoId | 类型 | 数量 | 触发价 | 限价/语义 |
|---|---:|---|---:|---:|---|
| ARIA -30 | 2000001577898163 | STOP_MARKET | 0 | 0.12977 | closePosition=true，全仓保护；qty0不代表无保护 |
| ARIA -30 | 2000001577898176 | TAKE_PROFIT_MARKET | 16 | 0.1068 | 市价止盈腿；已在原 preflight 前完成的 TP1 12 张不重复重建 |
| PFE -3.28 | 2000001554798275 | STOP | 3.28 | 31.93 | 限价31.96，覆盖全量但不保证触发后立即成交 |
| PFE -3.28 | 2000001554798281 | TAKE_PROFIT | 0.98 | 29.8 | 限价29.83 |
| PFE -3.28 | 2000001554798291 | TAKE_PROFIT | 1.31 | 28.99 | 限价29.02 |

collector 的已审命令/healthcheck/journald 配置保留：启动先删除旧 heartbeat，成功采集才 touch，3600 秒循环；健康检查要求 heartbeat age<5400 秒。首次切换门禁确认 heartbeat 新于容器启动，最终目标容器健康。现场最近 heartbeat 为 **2026-11-04 15:11:56.241641 UTC**，约15:27 UTC查询时 age=925秒，低于5400秒阈值。不能把健康状态或文件存在等同于所有未来采集都成功。严格收入写入、pending/conflict 隔离及发布门禁已上线；不得重新启动旧 collector 覆盖不可变经济事实。

## 未完成事项与 48 小时观察

发布验收完成不等于历史策略归因完成。账户覆盖完整与策略归因不完整同时成立；unknown 保持 unknown，排序/分配冻结不能因净值上涨、时间流逝或一次成功采集自动解除。原 preflight raw 订单缺失保留在审计链中；原版/新币最终价格及并发 legacy 边界仍在。

后续48小时观察区间为 **2026-11-04 15:23:52 UTC 至 2026-11-06 15:23:52 UTC**（北京时间11月4日23:23:52至11月6日23:23:52），逐项记录而非提前打勾；本文未创建自动任务，也未宣称该观察已完成：

- collector 每次成功 heartbeat、最近成功时间、pending/conflict 与 quarantine 是否新增；新鲜缓存不得绕过这些门禁。
- 四政策 revision/有效期、entry cap、冻结状态及拒单日志；激进版未知订单/预约、持仓增量保护是否按预期闭环。
- ARIA/PFE 保护状态、实际成交与费用、普通/条件单残留；任何新变化都按交易所事件核对，不把订单哈希变化一概解释为发布故障或安全。
- 看板账户净额与 canonical identity/coverage/freshness；11 月数值按时间戳快照核对，策略 unknown 不变成0。
- 八目标容器日志/健康、排除服务身份、active image pin；后续 full 部署必须尊重/阻断 active pins。
- 在政策到期前作明确人工复核；不能通过续期/重启或解除冻结绕过未完成证据。

特别保留 STOP-LIMIT 风险：MELI 事件已实际证明，触发后挂出限价单不等于止损已执行。PFE 仍为 STOP 限价保护，需要观察触发后成交状态、剩余持仓和未成交挂单；不能只看 algo FINISHED 或已有保护数量就判定风险消失。是否改变订单类型/价格或人工退出，需要明确授权及独立证据；本验收报告不自动执行这些动作。STOP_MARKET 亦可能产生滑点，不能据此承诺固定损失。

## 验证与审计文档

恢复代码最后本地 scoped 回归 293 passed；第三阶段模块 94 statements / 14 branches 测得100%覆盖，外部 Docker/交易所调用为模拟；真实完成另由生产 state、pin、final proof、HTTP 和签名查询证明，二者不混同。

相关文档：scoped-ledger-risk-release-2026-11-04.md、manual-ledger-risk-recovery-2026-11-04.md、recovery-after-hrs-design-2026-11-04.md、recovery-after-meli-close-2026-11-04.md、recovery-after-data-import-2026-11-04.md。本报告中的 completion、历史 report identity、动态11月快照、heartbeat 与到期时间均由调度者在完成生产操作后提供并核验；文档编写者仅读取本机既有材料并更新此报告，未追加生产操作。
