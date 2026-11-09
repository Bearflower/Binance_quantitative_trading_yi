# 入场风控策略到期与修订版 2 续期（2026-11-09）

## 触发与影响

北京时间 2026-11-09 的只读验收发现 `strategy_gate` 返回 `missing or expired risk policy`。四个 `default` 账户活动策略仍指向修订版 1，均已于 2026-11-07 14:11:40 UTC 到期。运行时按设计拒绝新风险；持仓监控与保护不依赖策略有效期。本次未放宽入口风控，也未修改资金分配。

## 续期前证据

- 当前生产发布锁为 `6279ded829c8013062d24b3841f912d1b340fe9c`、`active`；叠加文件 SHA-256 为 `bd568038225f492c4b744e3da516db9d1ee293ef4a85a8e06774d68200536ccf`。
- HRS 镜像中的 `activate_entry_risk_policy.py` 和 `shared/entry_risk_policy.py` SHA-256 分别为 `6e59148709891d3fcd72cf312644bc12ec2983286313ffd89a719e338dd44a69`、`b1da7761df7b3ea379522274a6575d4e1a2f8f81d4fbf248a6829e7b2337f9c8`，与仓库核对一致。
- `pre_activation` 只读门禁通过，权益约 518.28 USDT，交易所无持仓、普通挂单或条件挂单；`entry_risk_reservation` 未释放行数为 0。资金分配摘要为 `d7398bee4c57fc6c55e75023b244bc394c0c5de9deefd9eb0a2eb62bc33ea057`。
- 旧策略均为 `allow_add=false`、`freeze_allocation=true`：HRS `monitor_only`；激进版 `reduced`，乘数 0.5、比例 15%；原版 `capped`，比例 20%；新币 `capped`，比例 25%。旧绝对上限依次为 0、77.8063509075、103.7418012100、129.6772515125 USDT。

## 操作与结果

在 `compose_deploy.lock` 下先运行原有激活 CLI 的 dry run，再重新读取交易所与分配证据，以原上限为 `approved-ceilings`、`expected-revision=1`、`valid-hours=72` 和明确的 `--enable-reduced` 原子激活四条策略。激活使用数据库管理员专用凭据；凭据未输出或存入报告。激活脚本 SHA-256 为 `ca4f6b264276334b19968d599c7d91235175cb2c67adea1701cfc2e075ecf82e`。写入的是新的不可变策略版本和审计记录，没有更新旧版本。

| 策略 | 修订版 2 模式 | 新绝对上限（USDT） |
| --- | --- | ---: |
| HRS | `monitor_only` | 0 |
| BTC/ETH 激进版 | `reduced` | 77.7417240630 |
| BTC/ETH 原版 | `capped` | 103.6556320840 |
| new_coin | `capped` | 129.5695401050 |

激活时间为 2026-11-09 02:54:46 UTC。独立数据库读回显示四个活动指针均为修订版 2、对应四条审计记录齐全，统一到期时间为 **2026-11-12 02:54:46 UTC（北京时间 10:54）**。`strategy_gate --revision 2 --require-reduced` 通过；权益、分配和交易所库存复核无异常。未下单、改写预留、重启容器或部署镜像。

## 后续边界

72 小时有效期是现有设计；到期仍会拒绝新风险。每日只读验收已增加风控策略实际到期时间检查，在未来 48 小时内到期或门禁失败时通知，**不自动续期**。下次续期必须重新审查实时权益、资金分配、交易所库存、活动修订版和新候选上限，再使用下一修订版 CAS 激活。
