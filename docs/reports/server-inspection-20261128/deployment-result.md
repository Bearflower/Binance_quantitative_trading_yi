# 服务器巡检修复上线结果

2026-11-28，北京时间。用户已明确允许提交、推送及继续上线。运行版本为 `513ca815ba4f3c0b9d1bd5eff395ad034f44ecdc`，独立分支 `codex/server-inspection-fix-20261128`，没有合并 main。

## 已完成

- 容器巡检采用完整身份和生命周期核验，历史、备用和已结束任务不再统一报宕机；历史失败仍单独提醒。
- 重复历史行情已双端归档、逐文件校验后清理；无引用旧浏览器缓存已清理。09:43 磁盘 79.15%，可用 13.42 GB。数据库备份、数据卷和 21 天日志保留策略未缩减。
- HRS 报告服务已替换，启动后新报告 `database_snapshot_coherent=true`，无 `source_error_type/source_error_code`。真实完整只读探针峰值 RSS 137692 KiB，低于 256 MiB 限制的 80% 验收线。
- ai-tuner 已替换，补录查询和事务复核按经典/PM 模式切点隔离。用部署代码在生产只读事务中执行候选分流：经典候选 0、未解决当前候选 0、PM 历史缺口 2。探针禁止交易接口访问，以已有表断言代替建表，不写账务；这不等同于已观察自然调度周期。

## 发布与核验

[CI Run 46093510787](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/46093510787) 全部成功，`DEPLOY_ID=00000201`。只替换两个实例：

| 实例 | 新容器 ID 前缀 | 镜像 digest 前缀 | 结果 |
|---|---|---|---|
| ai-tuner | f1a68f4edc78 | sha256:870f00b549e8 | healthy，零重启，无启动后错误日志 |
| trading_system-hrs-acceptance | 5690502e8dd8 | sha256:a0c4ea2df541 | healthy，零重启，新报告来源完整 |

镜像、VERSION、源码 SHA-256/MD5、环境、挂载、网络、资源限制核验通过；阶段和激活前统一权限 audit 均通过，1355 对象、issues 为空，无权限 apply/扩权。其余 30 个运行实例 ID 和启动时间不变。最终 32 个运行容器中 31 个 Docker healthy；无 Docker healthcheck 的 HRS 盘口观察器通过读库心跳检查。两旧实例保留，退役登记已完成。

第一次 `c0ff6c82` 激活被配置检查拦截并成功回退：Docker 更新重启策略后将默认 `OomKillDisable=false` 返回为 `null`。发布器仅对该默认等价表示做规范化，真实限制差异仍阻断。新增回归先失败后通过，发布器最终 14 项测试及独立复核通过。首次失败证据保留，弃用候选仅按完整 ID 移除，没有删除卷。

## 仍保留的真实状态

- HRS 业务验收仍为 `PENDING`：技术完整小时不足；财务证据原因是 `fill_owner_unproven`、`income_or_fill_reconciliation_incomplete`。本次恢复报告读取，没有伪造样本或把历史缺口改成通过。
- ETH/XRP 两笔 PM 历史订单缺少可证明的策略归属，仍为 `BLOCKED_DATA`；未补写未经证明的归属。
- 最终选定检查函数的巡检结果仍有 **1 项 PostgreSQL 历史窗口日志告警**，包含昨日错误连接参数、本次首次回退窗口内查询取消、HRS 只读账号查询 XAU 研究表被拒绝三条记录。未扩大权限或删除日志来消除该告警。脚本片段的退出码 0 是命令执行成功，不能解释为零告警。
- 一个历史失败预检提醒和一次 grid WebSocket 1006 警告继续保留。磁盘已低于 80% 错误阈值，仍高于 70% 预警线。

## 证据

- `docs/reviews/server-inspection-20261128/runtime-postflight.json`：实际版本、源码校验、权限、报告和受保护实例核验。采样时清单曾含一个短暂额外实例；以随后 `final-inventory.json` 的完整核验为最终清单。
- `docs/reviews/server-inspection-20261128/final-inventory.json`：最终 32 运行、31 healthy、0 重启和磁盘。
- `docs/reviews/server-inspection-20261128/final-inspection-smoke.txt`：实际检查输出，未调用通知函数。
- `docs/reviews/server-inspection-20261128/nightly-exit-live-readonly.json`：部署版本的生产只读候选验证。
- `docs/reviews/server-inspection-20261128/postgres-postrelease-log-summary.json`：上线后新增错误计数与历史窗口区分。
- 服务器私有完整配置和回滚证据：`/root/trading_system/.releases/server-inspection/513ca815ba4f3c0b9d1bd5eff395ad034f44ecdc/`；未将凭据快照提交 Git。
