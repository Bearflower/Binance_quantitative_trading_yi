# 自动归属证据稳定性热修复记录

日期：2026-11-11，北京时间。范围：`income-attribution.service`；不改变交易策略容器或交易所订单。

## 故障与根因

15:16、16:15 两次定时执行均以 `owner conflict blocks attribution writes` 退出。只读影子对账发现 3 个交易所成交对应 6 条 `derived_evidence_conflict`。原自动归属策略均为 `btc_eth_aggressive`，证明来自精确 PM 算法单关联。随后补入的 `trade_records` 4717（ETHUSDT）、4718（SOLUSDT）也指向同一策略。订单 ID、算法单 ID 和载荷 SHA-256 不变，只是运行时证明增加了 `trade_record_ids`，完整 JSON 比较因此误判冲突。写入器对订单证据也做完整比较，即使只绕过收入冲突仍会失败。

## 修复

- `shared/accounting/order_evidence.py` 在 PM 算法单和成交记录同时存在且策略一致时，仍验证成交记录归属，但以 PM 算法单作为稳定的持久证明，不再增加后来才出现的成交记录 ID。
- `scripts/reconcile_income_attribution.py` 比较历史订单及收入证明时，仅对 `source_kind=pm_algo_link` 忽略旧版可选字段 `trade_record_ids`。策略、精确订单、算法单、载荷摘要、收入身份、金额、方法与算法版本仍须完全一致；历史行不更新。
- 测试先复现 3 个失败场景，再验证修复后 15 项相关测试通过；会计、发布和看板相关回归 `201 passed, 5 skipped`，5 项跳过因缺少专用 PostgreSQL 测试 DSN。`compileall` 与 `git diff --check` 通过。

## 生产验证

服务器原 HRS 镜像摘要 `sha256:213c03136e42c12d232da62f6eeaaa30ad432a6f89386e7380887cac53f1612f` 中两份源码哈希与修复前主干一致。将两份修复源码放在 `/root/trading_system/.releases/income-attribution/20261111-proof-stability/`，通过只读挂载覆盖镜像内对应文件。先执行只读影子对账：209 条收入、2 条候选、冲突 0、覆盖缺口 0。随后执行原有事务性 `--apply`：写入 2 条 `btc_eth_aggressive` 的 BNBUSDT 收入（手续费 −0.01356080、资金费 −0.00133937 USDT）；本月自动归属 53→55 条，金额 0.81970180→0.80480163 USDT。再次只读重跑候选 0、冲突 0、覆盖缺口 0。原 6 条收入证明没有被改写，也没有新增 `trade_record_ids`。

已安装 `/etc/systemd/system/income-attribution.service.d/20261111-proof-stability.conf`，只替换 `ExecStart` 为带只读挂载的脚本，保留原镜像摘要、环境和输出路径。16:49 手动 `systemctl start income-attribution.service` 返回 `Result=success`、`ExecMainStatus=0`；任务产物显示候选 0、冲突 0、覆盖缺口 0；timer 为 active。

17:15 的首个计划周期自动执行也返回 `Result=success`、`ExecMainStatus=0`；任务产物仍为 209 条收入、候选 0、冲突 0、覆盖缺口 0。下一计划周期为 18:15。

## 后续验收与回退

需观察至少两次计划定时执行均成功；代码并入主干且新镜像部署后，移除上述 drop-in 与临时挂载，再复核同口径影子对账。修复源码已通过现有 SSH 代理转发推送至远端分支 `codex/dashboard-closure-audit-20261111`，尚待 PR 合并和正式镜像发布。临时脚本对当前 HRS 镜像摘要做精确校验，未来镜像变更前须先移除临时覆盖并发布正式修复。

若热修复异常，可停止 timer，移除上述 drop-in，执行 `systemctl daemon-reload` 恢复原服务定义；原镜像和不可变证据均保留。恢复原定义会重新触发本次证据冲突，须在正式镜像修复上线后再启用。

看板的胜率、夏普率和最大回撤仍为 `null`，属于独立的绩效序列建设与口径验收问题；本次修复不改变这些指标。
