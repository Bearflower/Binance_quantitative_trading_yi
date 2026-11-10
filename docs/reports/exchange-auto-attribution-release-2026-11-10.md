# 交易所收入自动归属发布与验收记录

日期：2026-11-10（北京时间）。代码分支：`codex/funding-auto-attribution-spec`。生产冻结验收水位：`1794267300614` ms，账户 `default`，窗口为北京时间 2026-11-01 至该水位。

## 发布范围

- 新增 `order_strategy_evidence`、`position_flat_baseline`、`derived_income_attribution`，对三表的 UPDATE/DELETE 加拒绝触发器；`condition_orders` 新增 `account_id`。原始交易所收入、成交事实未更新。
- 自动推导要求精确订单证据或从交易所确认零仓基点开始的持仓重放。未能证实的资金费保留未知，历史人工归属继续优先；人工与自动策略相反时阻止完整报告。
- 看板仅 `dashboard-api` 容器使用固定原镜像加只读文件挂载，启用 `LEDGER_DERIVED_ATTRIBUTION_ENABLED=1`。策略卡片在归属不全时显示“已归属部分”，完整时显示“已核实净收益”。交易容器未重启。
- systemd `income-attribution-20261110.timer` 每小时 15 分触发一次。任务根据连续采集游标选择安全水位；覆盖缺口或归属冲突时不写入。
- 现有策略容器均为 `default` 单一 PM 账户，生产条件单表的 `account_id` 仅对**未来新行**设置 `DEFAULT 'default'`。历史空值没有回填；加入第二账户前须撤销此过渡默认值，并让所有策略镜像部署显式账户写入代码。

## 生产验收

| 指标 | 发布前 | 发布后及同水位重跑 |
| --- | ---: | ---: |
| 交易所收入行数 | 197 | 197 |
| 交易所收入合计 | −30.43345400 USDT | −30.43345400 USDT |
| 人工归属行数 / 金额 | 150 / −32.13305288 USDT | 150 / −32.13305288 USDT |
| 自动归属行数 / 金额 | 0 / 0 | 43 / +2.05410941 USDT |
| 订单证据 / 零仓基点 | 0 / 0 | 28 / 10（首次写入 9，定时服务首跑再添 1） |
| 账本已归属 / 未知 | 发布前未启用自动读模型 | −30.43345400 / 0 USDT |
| 归属冲突 / 采集缺口 | — | 0 / 0 |

三类交易所金额在冻结水位内为：已实现盈亏 −29.74028997、手续费 −0.71787545、资金费 +0.02471142 USDT。`已归属 + 未知 = 交易所总额` 精确相等。账本报告 ID：`dccca0ebe2ded2f4a46a395e4840dd13eb41d71bc0e1ac1a9df26e44aa64780f`。

首次只读影子核对检查 197 条，找到 41 条收入候选、10 个零仓基点，冲突 0、覆盖缺口 0。冻结水位写入时，PM 历史映射新增可见了同一激进版订单的手续费与已实现盈亏两条，因此写入 43 条；两条新增候选的收入键为 `COMMISSION/910703635849108`、`REALIZED_PNL/910703635849108`，策略均为 `btc_eth_aggressive`。显式冻结水位只查询收入涉及的币种，因此首写入 9 个基点。第二次相同水位运行发现 0 条收入候选、0 个基点候选、0 个冲突，表行数和金额不变。定时服务首次手动运行成功，只新增了先前游标中额外币种的 1 个零仓基点。

自动算法单独观察时仍有 90 条订单归属证据不足、64 条资金费早于已验证基点或无区间；其中既有人工归属继续有效。它们不是本次正式账本的未知余额，正式账本未知池为 0。未来若出现新缺口，看板显示部分值及未知池，不伪装成完整收益。

生产月度 API 返回 `exchange-income-v1-derived-v1`、`publication_status=published`、`account_coverage_complete=true`、`strategy_attribution_complete=true`、账户总额 −30.43345400 USDT、未归属 0。日、周 API 同样发布该版本，前端静态文件含“已归属部分”和“已核实净收益”。月度策略净额分别为：原版 +2.41152268、激进版 −3.88807008、新币做空 −8.54999189、HRS −20.40691471 USDT。胜率、夏普率、最大回撤仍为 null，等待独立的交易序列与净值验收。

本地验证：会计与看板测试 `167 passed, 4 skipped`；独立 PostgreSQL 的迁移重复执行成功，UPDATE 被插入后不可修改触发器拒绝；写入器 PostgreSQL 集成测试 `1 passed`；`node --check dashboard/frontend/js/main.js` 通过。

## 发布文件与回退

服务器发布目录：`/root/trading_system/.releases/funding-auto-attribution-20261109/dde4b068/`。发布前 `condition_orders` 与人工归属表备份：该目录下 `pre_schema_tables.sql`（559756 字节）。看板覆盖文件 `dashboard-overlay.yml` 固定原镜像 SHA `056612635cef28fbf88071f660aace2d1643f7b432a69d666244635cd2924c87`；`dashboard-rollback.yml` 固定同一原镜像且不挂载新代码。

需要停止自动写入时：`systemctl disable --now income-attribution-20261110.timer`。需要回退看板读模型时，在 `/root/trading_system` 执行 `flock -n .deploy_tmp/compose_deploy.lock docker compose -f docker-compose.yml -f .releases/funding-auto-attribution-20261109/dde4b068/dashboard-rollback.yml up -d --no-deps --pull never dashboard-api`。仅在旧容器账户过渡方案不再适用时，以迁移管理员执行 `ALTER TABLE btc_eth.condition_orders ALTER COLUMN account_id DROP DEFAULT`。已产生的交易所事实、人工归属和有效自动证据不通过删除回退。

看板本次使用固定镜像与发布目录挂载。未来标准 CI/CD 若不包含本分支文件和归属开关，重新部署看板会回到旧读模型；合并代码到主干及更新标准部署配置是后续常规发布必须完成的持久化步骤。当前 systemd 定时任务独立于看板容器重建，但仍依赖上述发布目录和旧采集镜像摘要。
