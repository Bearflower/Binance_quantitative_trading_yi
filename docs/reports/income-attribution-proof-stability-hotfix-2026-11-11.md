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

17:15 的首个计划周期自动执行也返回 `Result=success`、`ExecMainStatus=0`；任务产物仍为 209 条收入、候选 0、冲突 0、覆盖缺口 0。18:00 的正式发布启动再次成功。

## 正式发布与验收

首次迁移脚本原先只接受旧 `income-attribution-20261110.timer` 为 active，已有 `income-attribution.timer` 时会被前置检查拦截。本次增加现有定时器的更新路径：保留策略容器和看板进程，拉取固定摘要的 HRS 镜像，备份原 systemd 单元和临时 drop-in，停用现有定时器并等待服务退出，替换单元、移除临时 drop-in、启动新服务及定时器；若启动或健康检查失败，则恢复原单元和 drop-in。发布模式仍按审定的主干基点和文件范围选择定向部署。相关发布测试包含成功更新和失败回退；会计及发布相关回归共 `205 passed, 5 skipped`，5 项跳过因缺少专用 PostgreSQL 测试 DSN。

代码提交 `ae9c7e5949586b54e96043eb18ebc1533e6b0203` 已进入主干，通过 [GitHub Actions CI/CD](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/42999571345) 构建和 SSH 定向部署，部署任务成功。生产 `income-attribution.service` 指向该提交的 release 目录和 HRS 镜像 `sha256:da579e5727fcccc34e6011d991bcbd36f672e44e7a815ae0f207901c3c3cbfb0`；`income-attribution.timer` 为 active，临时 drop-in 已移除。18:00 部署时运行 `Result=success`、`ExecMainStatus=0`，处理 209 条收入，候选 0、冲突 0、覆盖缺口 0。release 状态为 `active`；看板 `/api/overview?type=monthly` 返回已发布的 `exchange-income-v1-derived-v1`、未归属 0。16:49、17:15 的热修复运行与 18:00 的正式镜像运行均成功。

发布器在更新失败时从内存中的副本恢复此前单元和 drop-in；正式发布后若需人工回退，应以此前已验证的 release 及 CI 部署记录为依据，先停止定时器并确认服务退出，再恢复对应版本。不要单独恢复旧镜像而继续运行有冲突的归属逻辑。

看板的胜率、夏普率和最大回撤仍为 `null`，属于独立的绩效序列建设与口径验收问题；本次修复不改变这些指标。
