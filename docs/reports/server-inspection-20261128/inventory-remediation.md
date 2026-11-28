# 容器生命周期修复说明（待生产应用）

基线 b52f442a。所有服务器操作均为只读；未修改 Docker、登记、备份或日志保留。

## 修复

- `ai_tuner/ops/container_inventory.py` 保留原 v1 登记、`partition()` 三元组及 `--names` 接口。新增显式 lifecycle 登记，包含完整容器 ID、名称、镜像 ID、命令指纹、退出码、FinishedAt、证据引用。
- 新类别要求源容器 restart=no、未 OOM、无 Docker Error；在役接替实例必须 running + healthy。任何身份/状态/命令/结果漂移均返回 issues，调用方巡检失败关闭。
- `completed_task` 额外要求经审阅的任务结果 success=true、error_count=0 和日志 SHA-256；不能把 exit0 当作充分成功证据。
- `standby` 仅接受 created；`retired` 接受已核验的 exited 老版本。
- `archived_failed_task` 仅供显式登记已结束的一次性历史失败，保存失败摘要、带时区发生时间、失败结果和日志指纹；不会将其标为成功。默认 JSON 和 `--historical-failures` 均提供历史失败记录。
- `scripts/ops/plan_container_lifecycle.py` 只生成草案，不写生产登记、不修改容器，不按名称全局忽略。请求必须给出快照完整 ID，重名换 ID、重复登记、无健康接替或现有登记漂移均拒绝。

## 本次草案

`container-retirements.proposed.json` 保留原35条登记，增加61条：28个已验证成功的网格发布任务、30个退役旧实例、2个未激活备用、1个显式历史预检失败。

`container-snapshot.json` 为北京时间08:53:16的脱敏快照。离线分区为32个在役、61个非在役、0项漂移。28个网格任务都有结构化 `GRID_RELEASE_RESULT` 结果，退出0且 `protected=true`、`flat=true`、`risk_status=ALLOW`；探针的只读/对账/心跳等字段同时通过。

预检 `history-tolerance-hrs-preflight-1c7c99cf` 在2026-11-27 09:04:30Z和09:05:19Z各有一次缺少apscheduler，最终退出0；草案保留两次失败，不将其当成功，也不将已结束的预检当在役宕机。

应用前必须在当前服务器快照重跑以下只读生成器并审阅差异；当前文件不是可跳过复核的永久删除清单：

```bash
python3 scripts/ops/plan_container_lifecycle.py \
  --registry /root/trading_system/.ops/container_retirements.json \
  --requests lifecycle-requests.json > container-retirements.proposed.json
```

需要同时部署新的 inventory 模块，保留旧模块和登记备份，再以原子替换应用已经核验的草案。通过 `--names`、`--historical-failures` 及巡检函数干跑核验，禁止直接运行发送通知的整份巡检入口。

后续发布必须为新ID生成并维护精确登记；未知新容器仍会报警，这是安全默认值。发布集成不应只按名称排除所有 `probe` 或 `before`。

## 测试

先增加新回归验证旧实现失败，再实施修复。指定环境 `/tmp/binance-candidate-venv/bin/python` 运行：

```text
pytest tests/test_ai_tuner_mvp/test_container_inventory.py \
       tests/test_ai_tuner_mvp/test_container_lifecycle.py \
       tests/test_ai_tuner_mvp/test_log_retention_check.py -q
43 passed
```

覆盖未登记失败仍可见、退出0但结果失败拒绝、已归档失败仍可查询、身份/命令/镜像/退出码/完成时间/OOM/重启策略漂移、接替实例故障、重复请求、旧v1兼容。CLI离线输出与草案逐字节一致，py_compile及git diff --check通过。
