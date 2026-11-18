# XAU 影子巡检（ai-tuner）

`ai-tuner` 在北京时间每天 07:45 执行只读巡检，并将完整结果发送到独立飞书机器人。密钥文件为服务器 `./logs/ai_tuner/xau_monitor_webhook`（容器内 `/app/logs/xau_monitor_webhook`）；不提交到 Git。该文件缺失时任务不注册，其他 ai-tuner 任务照常运行。

检查范围：三个 XAU profile 最新决策事件及数据质量、校准版 ATR 字段、三个周期最近 100 根 K 线、active/closed run、校准版小时缺口及跨 run 重复、四张生产表零写入、K 线注册到期时间。历史基线为 4 个缺口和 6 个跨 run 重复小时；超过基线才报告新增异常。注册不足七天时报告需要人工授权续期，巡检不会续期。

宿主机的 `xau-runtime-evidence.timer` 每天北京时间 07:40 生成运行证据，ai-tuner 巡检读取容器状态、重启次数、StartedAt、镜像和版本，并与前次证据比较。调度或送达核验可按容器名称查宿主机 journald，跨容器重建时也能查到重建前的巡检记录。

上线顺序：安装密钥文件（权限 0600）→ 部署 ai-tuner → 验证调度任务注册、正式镜像手动执行一次只读巡检并收到飞书报告 → 撤销旧 Codex `xau` 定时任务 → 核对首个每日 07:45 自动周期送达。回退时恢复旧任务并移除 ai-tuner 密钥文件。
