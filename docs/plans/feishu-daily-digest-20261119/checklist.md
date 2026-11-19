# 检查清单：每日巡检汇总（07:35）

- [x] 6 段顺序为 HRS → 网格 → BTC–XAU → XAU → 利润提取 → 生产容器日志，段头为 `━━━━━━━━━━ N/6 名称（HH:MM）━━━━━━━━━━`
- [x] 首部含标题时间、`本次共 6 项：…`、`异常概览：…`；6 段齐全且无 warn/error 时为 `全部正常`
- [x] 缺失段显示「本次未生成（HH:MM 任务未产出）」并计入概览；昨日陈旧文件视为缺失
- [x] 序列化超过 24000 字节时按段边界拆两条，第二条含 `（续 2/2）`
- [x] 发送成功后才归档到 `archive/YYYY-MM-DD/`；发送失败保留并退出码 1；缺段退出码 0；`--dry-run` 不发送不归档
- [x] outbox 原子写（临时文件 + `os.replace`）；非法 key/status/空正文抛 `ValueError`；损坏 JSON 跳过不中断
- [x] 时间表一致：HRS 07:31、网格 07:32、BTC–XAU 07:33、XAU 07:33、利润 07:34、日志 07:34、汇总 07:35、XAU 证据 07:30
- [x] 各段默认只写 outbox；宿主四段保留 `--send` 排障；XAU/利润在写失败或旧镜像时回退直发
- [x] 利润提取未触发保留「本次未触发」+ 权益/ATH；已触发只进 outbox，不再单独直发
- [x] 日志巡检「可查窗口不足 14 天」合并为 1 条全局异常；3 GiB 容量告警单位换算正确
- [x] `trading-btc-xau-inspection.service` 具备写 `/var/lib/trading-digest/outbox` 的权限（`StateDirectory=trading-digest`）
- [x] 文档与代码一致：`docs/deployment/log-retention.md`、`docs/deployment/daily-digest.md`、`docs/deployment/README.md`、`docs/ops/xau-ai-tuner-patrol.md`
- [x] `compileall` 通过；6 个测试文件 32 passed
- [x] 真实样例干跑：7458 字节、单条、6 段顺序正确
- [x] 未提交、未 push、未执行任何生产变更（等待复审）
