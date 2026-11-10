# AI Tuner 缩量试跑日周评估发布与影子验收

日期：2026-11-10，北京时间。范围：`btc_eth_aggressive`、`hrs` 的只读巡检与评估。

## 发布证据

- CI [42769827440](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/42769827440) 全部成功，远端部署 job 成功。主提交 `503f7e562c9123eac14b6dfaba9028cd20fef247`。
- 生产 pin `status=active`，指向该 SHA；发布 state `status=complete`、`step=success`、`changed=["ai-tuner"]`。AI Tuner 镜像 digest 为 `sha256:1685598d6a6fd9c3d5c12f698bee68e09d1db67ea6640ce5cf76867939e2ecfb`。
- `ai-tuner` 容器为 `running/healthy`，启动于 `2026-11-10T06:40:34Z`。发布脚本核验了容器镜像与源码哈希、`audit`/`selected_strategy=null`、调度器启动、其他容器身份、交易所只读证据及政策/分配前后摘要。没有风险政策或资金分配写入。
- 发布前运行的定向测试：AI Tuner `270 passed`；MVP `757 passed, 5 skipped`；限定发布 `115 passed`；会计 `190 passed, 5 skipped`。干净检出的 SHA 发布包通过清单与 digest 预演。

## 生产影子运行（无真实通知）

在新容器里用只读交易所客户端、数据库读连接和通知替身执行日巡检；报告写入独立 `shadow-verify-503f7e56` 目录，没有下单、策略配置写入或真实推送。

| 策略 | 政策 revision | 结果 | 已核证据与缺口 |
| --- | ---: | --- | --- |
| 激进版 | 3 | `BLOCKED_DATA` | 上一窗口净收入 `+0.16354585 USDT`，报告 ID `7a580dc65274d3b54150d3811aca265406abc13527ddc714145507a3b3686567`；实盘信号 4、记录已执行 1、取消 3；ETH/SOL 持仓已归属且有全量止损。信号到实际订单/成交的漏斗未核；PM 持仓无 `contractSize`，保证金审计值未知。 |
| HRS | 4 | `BLOCKED_DATA` | 上一完整北京时间日截止 `2026-11-09T16:00Z`，早于 HRS 试跑起点 `2026-11-10T01:10:55Z`；`trial_window_precedes_policy`，本窗净额和信号数保持未知。首个有效完整日窗是下一日任务。 |

当前两策略政策到期时间均为 `2026-11-13T03:37:20Z`；到期续跑仍由独立风险政策流程决定。每日日巡检设为北京时间 10:15，每周评估设为周一 11:15。首次真实定时执行和飞书送达尚未验收，原 Codex 到期巡检继续运行。

## 留待后续验收

1. 观察下一个真实日任务与下一个完整周任务：核对 job 实际执行、报告文件、通知送达/去重与数据水位。HRS 首个有效完整日窗在 2026-11-11 10:15 的运行中评估。
2. 建立可核查的信号→入场单→成交→保护单漏斗，并区分取消、拒单与部分成交；当前记录的 `executed_signals` 不是交易所实际成交批次数。
3. 为 PM 持仓建立与政策保证金预算一致的交易所或可重放保证金数据契约；当前 `notional/leverage` 只是估算，不能据此声称预算通过。
4. 建立策略权益曲线、资金划转归属、压力损失和事前冻结的提额阈值；至少两个不重叠月度留出窗口仍未完成。周评估在此之前维持 `BLOCKED_DATA`，不自动续期、提额或下单。
