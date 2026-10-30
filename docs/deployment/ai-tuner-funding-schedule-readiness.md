# AI Tuner 资金费日程采集：上线后就绪门禁

本清单只用于前瞻采集器的独立上线核对；它不授权历史回填、服务器回测、调优自动生效或资格考试 PASS。

部署顺序：新服务位于 `funding-schedule` Compose profile。普通部署分支显式清空 `COMPOSE_PROFILES`（覆盖服务器环境与 `.env`），并先仅停止既有 `ai-tuner-funding-schedule`，因此普通 `up -d --remove-orphans` 不会提前启动它。只有 14 号 DDL、独立密码设置和 ACL 门禁全部成功后，部署脚本才用显式 profile、`--no-deps` 启动。重复部署也遵守相同顺序；ACL 失败时再次停止精确目标，不能留下未验证的采集器运行。提前停机造成的缺槽是真实数据缺口，不可伪造回填，后续资格考试按缺口处理。

## 容器存活与采集成功是两件事

`ai-tuner-funding-schedule` 的 Compose healthcheck 检查容器内监视任务的成功数据库查询心跳（最多 180 秒）。进程启动先清除旧心跳，首次连接数据库并完成监视查询后才写入；查询失败不会刷新心跳，监视任务会发脱敏错误码并退出。采集循环与监视循环互不阻塞：单次采集最多等待 90 秒，超时取消，不把取消中的部分观测标成成功；监视任务在槽位结束 120 秒并再留 10 秒缓冲后，只读检查**证据表中的 run**，缺槽时按槽位告警。进程重启会重查最近已截止槽位；同一运行进程内的同一缺槽只告警一次。DB 连接和监视查询各有 15 秒超时，告警发送最多等待 10 秒。

`healthy` 仅表示监视任务近期成功查询了数据库，不表示交易所请求成功、每槽都有 run、逐币种数据完整或告警实际送达。人为停掉容器、宿主机故障或网络整体中断时，容器内任务无法自行告警，须由独立基础设施监控发现。部署成功不能仅凭容器健康态宣称数据采集就绪。

## 生产就绪门禁（全部满足才标记采集器就绪）

1. 检查独立密钥文件为 0600，容器环境只含专用 `AI_TUNER_FUNDING_DB_USER`/密码，不含交易 API Key。部署脚本的数据库 ACL 断言必须返回 1；若失败，停止新采集服务并修复权限，不放宽角色。
2. 从启动后的第一个 UTC 五分钟整点起，连续观察 **3 个 scheduled 槽位**（至少约 15 分钟）。以只读 SQL 核验三槽各有 `status='success'` 的 run，`request_started_at`、`received_at` 没有跨出允许窗口；每槽均有 BTCUSDT、ETHUSDT 和当时 HRS universe 的逐币种 observation，并确认哈希、`source_time`、`next_funding_time` 非空。缺任一槽或只有 failed run 均不通过。
3. 核对三次采集的日志与数据库行一致，不出现未落库的异常、身份错误或重复冲突；刻意验证监视日志没有 `MISSING_SCHEDULE_SLOT`、`RUN_QUERY_FAILED`。只读查询可以按 `scheduled_at DESC` 查看 `public.ai_tuner_funding_schedule_runs`，并用 `attempt_id` 联查 `public.ai_tuner_funding_schedule_observations`；不要用 `docker logs` 中的 `healthy` 代替 DB 证据。
4. 在维护窗口通过同一 `tuner` 飞书路由发一条标记为“资金费日程采集告警测试”的受控告警，人工确认实际送达；若发送失败、未送达或目标配置为空，则不通过。不要为测试制造生产失败批次或修改证据表。

任何门禁不通过，服务保持未就绪，真实资格考试继续 `BLOCKED_DATA`。停用新服务不影响 `ai-tuner`、`ai-tuner-snapshot` 与交易容器；证据表保留供排查。即使三槽全部通过，仍须累积完整考试窗口的独立历史日程，不能据此让参数自动生效。
