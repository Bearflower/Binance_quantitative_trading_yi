# XAU 影子巡检（ai-tuner）

`ai-tuner` 在北京时间 00:35、04:35、08:35、12:35、16:35、20:35 执行只读巡检，并将完整结果发送到独立飞书机器人。密钥文件为服务器 `./logs/ai_tuner/xau_monitor_webhook`（容器内 `/app/logs/xau_monitor_webhook`）；不提交到 Git。该文件缺失时任务不注册，其他 ai-tuner 任务照常运行。

检查范围：三个 XAU profile 最新决策事件及数据质量、校准版 ATR 字段、三个周期最近 100 根 K 线、active/closed run、校准版小时缺口及跨 run 重复、四张生产表零写入、K 线注册到期时间。历史基线为 4 个缺口和 6 个跨 run 重复小时；超过基线才报告新增异常。注册不足七天时报告需要人工授权续期，巡检不会续期。

ai-tuner 没有 Docker socket 权限，因此容器重启次数、StartedAt 和镜像 SHA 不在该任务的直接检查范围内；事件及时性与 active run 是其运行代理指标。Docker 自身 healthcheck 仍持续运行。巡检器本身停机时无法发送消息，需由现有主机容器监控覆盖。

上线顺序：安装密钥文件（权限 0600）→ 部署 ai-tuner → 验证调度任务注册、手动执行一次只读巡检并收到飞书报告 → 确认下一个四小时周期自动送达 → 撤销旧 Codex `xau` 定时任务。回退时恢复旧任务并移除 ai-tuner 密钥文件。
