# 每日巡检汇总（07:35 单条飞书）

每天 07:30–08:00 原本分散发送的 6 条巡检通知，统一改成**只渲染、写 outbox**，再由 `ai_tuner/ops/daily_digest.py` 在 07:35（北京时间）合并成一条飞书文本消息发送。

## 1. 时间线

| 时间 | 段落 | 执行位置 | 入口 |
|---|---|---|---|
| 07:30 | XAU 运行证据采集 | 宿主 systemd | `xau-runtime-evidence.timer`（只采证，不发消息） |
| 07:31 | HRS 生产只读巡检 | 宿主 systemd | `trading-hrs-acceptance.timer` |
| 07:32 | 网格实盘与行情巡检 | 宿主 systemd | `trading-grid-shadow.timer` |
| 07:33 | BTC–XAU 影子巡检 | 宿主 systemd | `trading-btc-xau-inspection.timer` |
| 07:33 | XAU 影子巡检 | ai-tuner 容器 | APScheduler `xau_shadow_patrol` |
| 07:34 | 利润提取提醒 | ai-tuner 容器 | APScheduler `profit_extraction_job` |
| 07:34 | 生产容器日志巡检 | 宿主 systemd | `trading-log-retention.timer` |
| 07:35 | 汇总发送 | 宿主 systemd | `trading-daily-digest.timer` |

07:30 的宿主机 `server_check.sh` 不在本汇总范围内。

## 2. outbox 协议

每个生产者把该段正文写入一个 JSON 文件，由汇总器统一读取；生产者不再直接调用飞书。

```json
{
  "key": "hrs",
  "generated_at": "2026-11-19T07:31:04+08:00",
  "status": "warn",
  "summary": "1 项异常",
  "text": "【HRS 生产只读巡检】……"
}
```

- `key`：`hrs` / `grid` / `btc_xau` / `xau` / `profit` / `logs`
- `status`：`ok` / `warn` / `error` / `skip`；`warn`、`error` 和缺失段进入首部「异常概览」`
- `summary`：一句短语，如 `1 项异常`、`未触发`、`已触发，建议提取 12.34 U`
- 写入方式：同目录临时文件 + `os.replace` 原子替换，避免汇总器读到半截文件
- 只接受 `generated_at` 属于当天的文件；昨天的残留视为缺失

目录（汇总器两处都读，同名 key 先到先得）：

- 宿主：`/var/lib/trading-digest/outbox`（`StateDirectory=trading-digest`，root，0700）
- 容器：`/app/logs/digest/outbox`，宿主实际路径 `/root/trading_system/logs/ai_tuner/digest/outbox`

容器侧目录由 compose 环境变量指定：

```yaml
- DIGEST_OUTBOX_DIR=/app/logs/digest/outbox
```

## 3. 汇总器行为

- 07:35 启动后最多等待 240 秒（`--wait-seconds`），轮询缺失段落。
- 消息结构：标题行 + `本次共 6 项：…` + `异常概览：…`，随后 6 个段块，每块以 `━━━━━━━━━━ N/6 名称（HH:MM）━━━━━━━━━━` 开头。
- 6 段齐全且无 `warn`/`error` 时概览为 `全部正常`；任一段缺失时概览为 `<段名> 未生成｜其余正常`，正文为 `【名称】本次未生成（HH:MM 任务未产出），请检查对应服务。`
- 序列化后超过 24000 字节时按段边界拆成两条：第一条首部改为「日期 + 异常概览 + 本次内容较长，分两条发送。」，第二条首行 `（续 2/2）`。
- 发送成功（`code==0`）后把用过的 outbox 移到同目录 `archive/YYYY-MM-DD/`；发送失败保留文件并以退出码 1 结束。
- 缺段不算失败，退出码 0。

利润提取未触发时仍保留状态行：

```
【利润提取提醒】本次未触发
当前权益 600.00 U 未超过历史最高 ATH 666.77 U，未创新高，不发送提取建议。
（触发时的内容：当前权益 / 初始资金 / 累计盈利 / 建议提取金额及比例 / ATH 变更）
```

## 4. 部署

1. 随 ai-tuner 镜像发布 `ai_tuner/ops/`（`digest_outbox.py`、`daily_digest.py`、各生产者与 unit 文件）。
2. 安装/刷新宿主单元：

   ```bash
   cd /root/trading_system/ai_tuner/ops
   cp trading-*.service trading-*.timer /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now trading-hrs-acceptance.timer trading-grid-shadow.timer \
     trading-btc-xau-inspection.timer trading-log-retention.timer trading-daily-digest.timer
   systemctl list-timers 'trading-*'
   ```

3. 创建汇总 webhook（0600，与现有巡检同群地址，不要打印明文）：

   ```bash
   install -m 600 /dev/null /root/trading_system/.secrets/daily_digest_webhook
   vi /root/trading_system/.secrets/daily_digest_webhook
   ```

4. 汇总器首次运行前，清理/归档历史 outbox：

   ```bash
   mkdir -p /var/lib/trading-digest/outbox/archive
   find /var/lib/trading-digest/outbox -maxdepth 1 -name '*.json' -delete
   find /root/trading_system/logs/ai_tuner/digest/outbox -maxdepth 1 -name '*.json' -delete
   ```

5. 重启 ai-tuner 使新的 cron 时间与 `DIGEST_OUTBOX_DIR` 生效。

## 5. 验证

```bash
# 不发送、不归档，只打印合并结果
python3 /root/trading_system/ai_tuner/ops/daily_digest.py --dry-run

# 手工触发各段（只写 outbox）
systemctl start trading-hrs-acceptance.service
systemctl start trading-grid-shadow.service
systemctl start trading-btc-xau-inspection.service
systemctl start trading-log-retention.service

# 查看汇总器日志与退出码
journalctl -u trading-daily-digest.service -n 100 --no-pager
```

验收：07:35 只收到 1 条（超长时 2 条）消息，6 段齐全且顺序正确；各段不再单独发消息。

## 6. 排障

| 现象 | 排查 |
|---|---|
| 某段显示「本次未生成」 | `journalctl -u trading-<段>.service`；容器段查 ai-tuner 日志与 `outbox` 目录 |
| 显示的是昨天内容 | 生产者当天未运行或写入了旧 `generated_at`；检查 timer 与 `Persistent=true` 补跑 |
| 消息超长被拆成 2 条 | 正常兜底；若长期发生，优先压缩日志巡检的服务明细行 |
| 汇总器退出码 1 | 看 stderr 的 `daily digest failed:`；多为 webhook 文件缺失/权限或飞书 `code!=0` |
| 07:35 没有任何消息 | `systemctl list-timers trading-daily-digest.timer`；确认 webhook 文件与网络 |

## 7. 回退

1. `systemctl disable --now trading-daily-digest.timer`。
2. 给宿主生产者追加直发：在 `trading-hrs-acceptance.service`、`trading-btc-xau-inspection.service`、`trading-log-retention.service`、`trading-grid-shadow.service` 的 `ExecStart` 末尾加 `--send`，`daemon-reload` 后重启对应 timer。
3. XAU 与利润提取在容器内运行，没有 `--send` 开关；恢复直发需回滚 ai-tuner 镜像。
4. 第 1、2 步放在同一维护窗口，避免出现「汇总已停、各段仍只写 outbox」的空档。
