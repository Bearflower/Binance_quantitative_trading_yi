# 2026-11-25 生产错误日志修复

## 发布范围和结果

修复源分支 `codex/classic-account-cutover`，运行镜像源版本 `be7484a7f9ef484a79187b801f0a37a6a2bdbe78`。
CI Run [45585380726](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/45585380726) 成功，Run number 11 / attempt 1，`DEPLOY_ID=00000B01`。

北京时间 17:29–17:31 依次替换 data-backend、dashboard-api、ai-tuner、trading_system-kline。四个服务均验证运行状态、镜像 ID、VERSION、变更文件 SHA256、运行日志/功能。旧容器保留且 restart=no。未重启实盘策略或网格执行器。

| 服务 | 新镜像 ID | 变更 |
|---|---|---|
| data-backend | e1d8a85f05c9b322c5c3c5fb8245b5e60a5abbcfc4f4dac56dd80ae4f1afdae8 | 新密钥、经典只读接口、经过签名证据核验的采集恢复 |
| dashboard-api | 197e99457a25f96c0dfc5e2ac1c1f53a774b436971d3bdd171f0c1261b16c262 | 同上，按空仓凭据和完整成交链确定持仓策略 |
| ai-tuner | 4054408dfc4b28769aef0a433fba2316d0448e0c76883fd148e79b603760fac2 | 合法空字典覆盖文件按无参数调整处理 |
| kline-service | af667fa3e204f0f2485627d8463e3d54a6cebb083f33ae1f6dd1913bd6b2d8bc | 删除已退市 MATICUSDT 默认采集任务 |

恢复凭据通过只读挂载 `/run/classic-collector` 提供，pin 为 `ce7eee6472df7d7853d7da554a6e7fa06d809cb6b9732048de858389c9f785af`。后续重新创建数据服务时必须保留本轮账户模式、凭据挂载和 pin；补齐并复核真实权益锚点后再走正常切换账本流程。仅改变凭据环境配置无法绕过账户、签名证据、摘要及完整成交覆盖校验。

## 成交与资金口径验收

BNBUSDT 订单 `99055739126`：唯一记录归属 `default/btc_eth_aggressive`，FILLED，数量 `0.04000000`，价格 `543.84000000`。真实手续费在 trade_records 记支出 `-0.00435072 USDT`；原始成交事实的正手续费与之口径相符。尚未平仓，realized_pnl 仍为 NULL。

持仓快照仅将该仓归到激进版。交易所三张原生 reduceOnly 保护单仍有效：`2000001760373378`、`2000001760373385`、`2000001760373406`。本次未发送下单或撤单请求。

仅新增经典模式当日实际权益观测 `classic_daily`，没有插入 `pm_final` 或 `classic_open`。跨模式月收益返回 `yield_unavailable=true`、yield=NULL。用户要求的连续 PM 历史和回撤预算仍等待资金明细；HRS 权益门禁、AI 自动分配限制没有放宽。

## 无效任务与配置告警

Kline 的 MATIC 任务数为 0。当前 136 个任务精确等于 27 个固定标的 × 4 周期与 26 个动态注册的任务并集，missing=[]、unexpected=[]。旧任务总数139，单纯减4预测135不能反映注册数据库的完整配置；没有保存旧全量任务列表，不能确定重启额外恢复的具体任务。17:35 的 Kline monitor 健康检查通过。

HRS 的 `V20260819.yaml` 原为纯注释，语义为“尚无调优建议”。在验证文件摘要、保留备份后增加明确的 `{}`。变更前后合并配置完全一致，config_hash 不变。此配置变更提交 `b3f8d69d`，通过现有挂载生效，不重启策略。17:35:55，AI 自然轮询记录合并成功，不再出现该告警；HRS 旧加载器仍可能按旧空字典规则回退，但实际参数不变。

## 磁盘

`docker image prune -f` 回收 0B：当时无符合条件的悬空镜像。
进一步逐项审计后，仅定点删除无任何容器、活动配置或回滚引用的旧网格镜像 `a17e721ab608f5feed64c5dc1c987acfa9197ddb67bafa739e68f006a9fef4a2` 的两个标签。实际 df 净释放 632,786,944 B；未删除容器、卷、日志或其他镜像。后续镜像部署后 17:35 可用空间约 8.85 GB，使用率87%。

## 数据库维护

详见 [PostgreSQL 维护报告](2026-11-25-postgres-autovacuum-bootstrap.md)。37项限时、节流 VACUUM/ANALYZE 全部成功，积压已清理，权限审计1343对象0问题。原生 autovacuum 的 bootstrap OID10 身份问题仍需单独维护窗口处理；本轮禁止提升业务账户权限或在线迁移角色身份。

已增设独立的 `trading-postgres-maintenance` 维护补偿：范围固定42张受保护表，每轮先权限审计，再按维护债务阈值选择；包含TOAST清理/冻结债务，排除不会清零的TOAST分析计数。最多8表、240秒维护预算、1秒锁等待，既有管理员执行，不授新权限。17:38:15 的生产执行审计通过，selected=0、failures=0，未对无债务表进行重复维护。完整权限/脚本测试85 passed。运行和停用方式见维护报告与部署手册。

## 验证和证据

- 采集恢复、成交归属、切换边界、手续费及未实现盈亏相关测试：73 passed。
- 配置覆盖及固定标的定向复核：23 passed；配置覆盖完整代理验证48 passed/1 skipped，Kline配置/调度/注册验证64 passed。
- 运行镜像文件校验：data-backend/dashboard-api 各6文件，AI/Kline各1文件；四个服务健康且 restart_count=0。
- 数据后台启动预热成功，实际权益快照、持仓归属及手续费回填完成。
- 原始发布验收目录：`/root/trading_system/.releases/pm-window-89160896/logfix-release/`，包含 five-layer-acceptance.json、accounting-acceptance.json、各服务预检/权限/发布记录、配置修复前后证据。

数据后台仍有一次启动提示：镜像没有 dashboard/backend/config.yaml，使用既有默认配置；当前账户和数据库连接来自环境变量，任务正常完成。此提示不等于此前反复发生的 Binance -2015 故障，未通过隐藏真实异常来消除日志。
