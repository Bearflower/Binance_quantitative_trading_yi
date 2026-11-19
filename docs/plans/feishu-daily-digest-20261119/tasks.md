# 任务清单：交易系统每日巡检汇总（07:35）

对应规格：[spec.md](./spec.md)　分支 `codex/feishu-daily-digest-20261119`

## T0 前置（阻塞项）

- [x] T0.1 恢复 SSH 到 `43.156.242.184`，取回 `/root/trading_system/ai_tuner/ops/grid_shadow_check.py`（298 行）与 `trading-grid-shadow.service/.timer`，放入工作树。
- [x] T0.2 用 sha256 比对 `.secrets/{hrs_acceptance,btc_xau_inspection,log_retention,grid-inspection}` 指向的 webhook 是否同群；已确认同群。`daily_digest_webhook` 文件创建纳入生产变更单 T7.2（未在服务器落盘）。
- [x] T0.3 确认容器内 `/app/logs` 为 rw 且 `/app/logs/digest` 可由 uid 1000 创建（宿主 `/root/trading_system/logs/ai_tuner`）。

## T1 公共模块

- [x] T1.1 新增 `ai_tuner/ops/digest_outbox.py`：`SECTIONS`、`outbox_dir()`、`publish()`、`read_all()`；原子写；非法 key/status/空正文抛 `ValueError`。
- [x] T1.2 新增 `tests/test_ai_tuner_mvp/test_digest_outbox.py`（原子写、非法参数、损坏 JSON 跳过）。

## T2 汇总器

- [x] T2.1 新增 `ai_tuner/ops/daily_digest.py`：多目录读取、缺段/陈旧判定、等待窗口、渲染（表头+概览+6 段）、24KB 拆分、归档、退出码、`--dry-run`。
- [x] T2.2 新增 `trading-daily-digest.service` / `.timer`（07:35 Asia/Shanghai，`StateDirectory=trading-digest`）。
- [x] T2.3 新增 `tests/test_ai_tuner_mvp/test_daily_digest.py`（齐全/缺段/陈旧/超长拆分/发送失败/干跑）。

## T3 宿主生产者改为 outbox

- [x] T3.1 `log_retention_check.py`：窗口异常合并为 1 条全局异常；默认 publish，新增 `--send`；publish 失败回退直发。
- [x] T3.2 `hrs_acceptance.py`：默认 publish，新增 `--send`；失败分支也 publish；publish 失败回退直发。
- [x] T3.3 `btc_xau_inspection.py`：默认 publish，新增 `--send`；异常兜底分支 publish；unit 增加 `StateDirectory` 以允许写入。
- [x] T3.4 `grid_shadow_check.py`（T0.1 取得后）：默认 publish，新增 `--send`；纳入入库。
- [x] T3.5 更新 `tests/test_ai_tuner_mvp/test_log_retention_check.py`（全局窗口异常 + outbox 分支 + journal 容量换算）。

## T4 容器内生产者改为 outbox

- [x] T4.1 `ai_tuner/monitor/xau_shadow_patrol.py`：触发 07:45 → 07:33；publish，失败回退 notifier；返回 `published`。
- [x] T4.2 `ai_tuner/allocation/profit_extraction_job.py`：cron → 07:34；三态（已触发/未触发/异常）写 outbox；未触发保留「本次未触发」+ 权益/ATH；已触发不再直发（避免与汇总重复）；导入失败降级直发。
- [x] T4.3 `ai_tuner/main.py`：确认容器内注入 `DIGEST_OUTBOX_DIR=/app/logs/digest/outbox`（compose 环境变量，无需改 main.py）。
- [x] T4.4 新增 `tests/test_ai_tuner_mvp/test_profit_extraction_digest.py`（三分支正文、outbox 字段、去重、降级直发）。

## T5 单元与文档

- [x] T5.1 调整 `trading-hrs-acceptance.timer`(07:31)、`trading-grid-shadow.timer`(07:32)、`trading-btc-xau-inspection.timer`(07:33)、`trading-log-retention.timer`(07:34)、新增 `trading-daily-digest.timer`(07:35)。
- [x] T5.2 `docs/deployment/log-retention.md` 补充：journald 3G 生效值核实、两个附加服务改 journald（含受限发布覆盖文件的持久化约束）、07:35 汇总与 outbox 路径、`xau-runtime-evidence` 07:30、回退方式。
- [x] T5.3 新增 `docs/deployment/daily-digest.md`：架构、outbox 协议、部署/排障/回退；并在 `docs/deployment/README.md` 登记。
- [x] T5.4 `xau-runtime-evidence` 由 07:40 改为 07:30：更新 `scripts/deploy_xau_review.py` 与 `docs/ops/xau-ai-tuner-patrol.md`（07:45 → 07:33）。

## T6 质量关卡

- [x] T6.1 `python3 -m compileall -q ai_tuner/ops ai_tuner/monitor ai_tuner/allocation scripts/deploy_xau_review.py`。
- [x] T6.2 pytest 6 个相关测试文件：32 passed。
- [x] T6.3 幻觉测试 10 项逐项记录（导入、方法/参数、配置 key、路径、变量、分支、异常路径、同步/异步、硬编码、文件完整性）。
- [x] T6.4 `code-specification-inspector` 快速检查；阻断项（btc_xau `ProtectSystem=strict` 无法写 outbox）已修复，回退策略已统一。
- [x] T6.5 `code-document-curator` 对照 docs 并更新；时间点、3G 口径、拆分首部、缺失段概览均已修正。
- [x] T6.6 干跑：用真实样例内容 + 日志段渲染，7458 字节、6 段顺序正确、单条发送。
- [x] T6.7 审查修复：利润提取「已触发」不再直发（消除重复通知）；journal 容量单位换算修正（`KMGT` 指数 +1）并补测试。

## T7 交付（默认不执行）

- [ ] T7.1 在分支上提交（含 spec/tasks）——按用户要求先不提交/推送，等待复审。
- [ ] T7.2 输出生产变更单（journald 3G、两服务改 journald、单元安装、`daily_digest_webhook` 创建）交用户批准。
- [ ] T7.3 批准后：合并进 `main` → push → 观察 Actions 重建 `trading-ai-tuner` 镜像 → 服务器安装单元 → 07:35 首次汇总验证。
