# 规格：交易系统每日巡检汇总（07:35 单条飞书）

- 状态：已批准；代码与文档已完成，等待复审（未提交、未部署）
- 分支：`codex/feishu-daily-digest-20261119`（基于 `origin/main` @ `f85f32c6`）
- 工作树：`/private/tmp/feishu-daily-digest-20261119`
- 日期：2026-11-19

## 1. 目标

把每天 07:30–08:00 分散的多条飞书巡检通知，合并成**一条**在 **07:35（北京时间）** 发送。各段之间用 `━━━━━━━━━━ N/6 名称（HH:MM）━━━━━━━━━━` 分隔。结构以已确认样例 `/tmp/feishu_merged_sample.txt` + `/tmp/feishu_log_section.txt` 为准。

范围（6 段）：

| 序 | 段 | 现触发 | 新触发 | 来源 |
|---|---|---|---|---|
| 1 | HRS 生产只读巡检 | 07:33 systemd | 07:31 | `ai_tuner/ops/hrs_acceptance.py` |
| 2 | 网格实盘与行情巡检 | 07:35 systemd | 07:32 | `ai_tuner/ops/grid_shadow_check.py`（仅生产存在） |
| 3 | BTC–XAU 影子巡检 | 07:38 systemd | 07:33 | `ai_tuner/ops/btc_xau_inspection.py` |
| 4 | XAU 影子巡检 | 07:45 容器内 APScheduler | 07:33 | `ai_tuner/monitor/xau_shadow_patrol.py` |
| 5 | 利润提取提醒 | 07:35 容器内 APScheduler | 07:34 | `ai_tuner/allocation/profit_extraction_job.py` |
| 6 | 生产容器日志巡检 | 09:00 systemd | 07:34 | `ai_tuner/ops/log_retention_check.py` |

不纳入：07:30 宿主机 `/root/inspection/server_check.sh`。

## 2. 架构：outbox + 汇总器

各段**只渲染、不直发**，写入 outbox 文件；新增 07:35 汇总器读取全部段落，拼成一条消息发送。

```
07:30 xau-runtime-evidence.timer  →  logs/ai_tuner/xau_runtime.json（采集证据，保持不变）
07:31 HRS        ─┐
07:32 网格       │
07:33 BTC–XAU    ├─→ outbox/<key>.json ─┐
07:33 XAU 影子   │                      ├─→ 07:35 daily_digest.py ─→ 飞书（1 条，必要时 2 条）
07:34 利润提取   │                      │
07:34 日志巡检   ┘                      ┘
```

outbox 目录（两处，汇总器都读）：

- 宿主机脚本：`/var/lib/trading-digest/outbox`（systemd `StateDirectory=trading-digest`，root，0700）
- 容器内脚本：`/app/logs/digest/outbox`，宿主实际路径 `/root/trading_system/logs/ai_tuner/digest/outbox`（`/app/logs` 已是 rw 挂载，容器用户 1000）

outbox 文件格式（`.json`，UTF-8）：

```json
{"key":"hrs","generated_at":"2026-11-19T07:31:04+08:00","status":"warn","summary":"1 项异常","text":"……段正文……"}
```

- `key`：`hrs|grid|btc_xau|xau|profit|logs`
- `status`：`ok | warn | error | skip`
- `summary`：一句短语，用于首部「异常概览」
- 写入方式：先写 `*.tmp` 再 `os.replace`，原子替换

## 3. 新增文件

### 3.1 `ai_tuner/ops/digest_outbox.py`

职责：段落定义 + 发布函数（宿主与容器共用，纯标准库）。

```python
SECTIONS = [
    ("hrs",     "HRS 生产只读巡检",     "07:31"),
    ("grid",    "网格实盘与行情巡检",   "07:32"),
    ("btc_xau", "BTC–XAU 影子巡检",     "07:33"),
    ("xau",     "XAU 影子巡检",         "07:33"),
    ("profit",  "利润提取提醒",         "07:34"),
    ("logs",    "生产容器日志巡检",     "07:34"),
]
DEFAULT_OUTBOX = "/var/lib/trading-digest/outbox"   # 宿主

def outbox_dir() -> Path                     # 读 env DIGEST_OUTBOX_DIR，缺省 DEFAULT_OUTBOX
def publish(key, text, *, status, summary, now=None) -> Path   # 校验 key/status，原子写
def read_all(dirs, today) -> dict            # 汇总器用：只接受 generated_at 属于 today 的条目
```

- `status` 非法、`key` 非法 → `ValueError`。
- `text` 为空 → `ValueError`（避免空段）。
- `read_all` 对损坏 JSON 跳过并返回告警列表，不抛异常。

### 3.2 `ai_tuner/ops/daily_digest.py`

职责：07:35 汇总并发送。

参数：

- `--outbox-dir`（可重复）：默认 `/var/lib/trading-digest/outbox` 与 `/root/trading_system/logs/ai_tuner/digest/outbox`
- `--webhook-file`：默认 `/root/trading_system/.secrets/daily_digest_webhook`
- `--wait-seconds`：默认 240，轮询等待缺失段落
- `--dry-run`
- `--no-archive`

渲染规则（与确认样例一致）：

```
【交易系统每日巡检汇总】2026-11-19 07:35（北京时间）
本次共 6 项：HRS 生产只读｜网格实盘与行情｜BTC–XAU 影子｜XAU 影子｜利润提取｜生产容器日志
异常概览：HRS 1 项异常｜日志 19 项异常        ← 无 warn/error 时输出「全部正常」

━━━━━━━━━━ 1/6 HRS 生产只读巡检（07:31）━━━━━━━━━━
<该段正文>

━━━━━━━━━━ 2/6 …（07:32）━━━━━━━━━━
…
```

- 缺失段落（文件不存在 / 非今日）：正文替换为 `【<名称>】本次未生成（<HH:MM> 任务未产出），请检查对应服务。`，`status=error`，计入异常概览。
- 首部时间取实际发送时刻。
- 长度兜底：序列化后（`json.dumps({"msg_type":"text","content":{"text":msg}}, ensure_ascii=False)`）**> 24000 字节**时，按段边界拆成 2 条发送，第二条首行加 `（续 2/2）`。
- 发送成功（每段 `code==0`）后：把已用 outbox 文件移到 `archive/YYYY-MM-DD/`（默认开启）；失败则保留文件并 `exit 1`。
- 日志与退出码：缺段不算失败（`exit 0`）；发送失败 `exit 1`；`--dry-run` 只打印。

### 3.3 `ai_tuner/ops/trading-daily-digest.service` / `.timer`

```ini
# service
[Unit]
Description=Trading system daily inspection digest
After=docker.service network-online.target
Wants=docker.service network-online.target
[Service]
Type=oneshot
WorkingDirectory=/root/trading_system
StateDirectory=trading-digest
StateDirectoryMode=0700
UMask=0077
ExecStart=/usr/bin/python3 /root/trading_system/ai_tuner/ops/daily_digest.py
TimeoutStartSec=600
```

```ini
# timer
[Timer]
OnCalendar=*-*-* 07:35:00 Asia/Shanghai
AccuracySec=1s
Persistent=true
Unit=trading-daily-digest.service
```

## 4. 修改文件

### 4.1 `ai_tuner/ops/log_retention_check.py`

1. **合并窗口异常**：`render()` 中把逐服务的 `"<svc> 可查窗口不足 14 天"` 改为一条全局异常：

   ```
   - 18 个服务可查窗口不足 14 天（最早 11-15 07:00，要求 ≥14 天）
   ```

   实现：循环中只收集不足窗口的服务列表（保留 `LATER`/`BASELINE` 逐服务基线判断），循环结束后若列表非空则追加 1 条；数量为 1 时仍用同一句式（`1 个服务…`）。
2. **outbox 模式**：默认 `publish("logs", report, status=..., summary=...)`；新增 `--send` 保留直发；`--dry-run` 行为不变。`status = "error" if issues else "ok"`；`summary = f"{len(issues)} 项异常" or "正常"`。
3. 保留现有 `journal 占用达到 3 GiB` 检查（3G 上限后的越界告警）。

### 4.2 `ai_tuner/ops/hrs_acceptance.py`

1. 默认改为 `publish("hrs", report, status=..., summary=...)`；新增 `--send` 强制直发（保留 `send_report`）。
2. `status = "warn" if issues else "ok"`；`summary = f"{len(issues)} 项异常" if issues else "正常"`。
3. 失败分支（`read-only probe failed`）也写 outbox：`status="error"`、`summary="证据读取失败"`，正文用现有失败文案。
4. 状态文件（`--state`）逻辑不变，仍只在成功渲染后落盘。

### 4.3 `ai_tuner/ops/btc_xau_inspection.py`

1. 默认 `publish("btc_xau", message, ...)`；`--send` 保留直发。
2. `status`：正文含 `采集 异常` 或 `异常，` → `error`；`采集 待核验` → `warn`；否则 `ok`。`summary` 取 `ok→正常`、`warn→待核验`、`error→采集异常`。
3. 异常兜底分支（无法核对报告）同样写 outbox（`status="error"`）。

### 4.4 `ai_tuner/monitor/xau_shadow_patrol.py`（origin/main 版本）

1. `daily_patrol_trigger()` → `CronTrigger(hour=7, minute=33, …)`，docstring 同步。
2. `run_once()` 末尾改为：优先 `digest_outbox.publish("xau", message, status=..., summary=...)`（容器内 outbox 由 `DIGEST_OUTBOX_DIR=/app/logs/digest/outbox` 指定，AI_TUNER 环境变量在 compose 中注入）；`publish` 抛错时回退 `self.notifier.send(...)`，保证不丢报告。
3. 返回值增加 `"published": bool`；`notifier` 参数保留（回退路径使用，非幽灵参数）。
4. `status = "warn" if issues else "ok"`；`summary = f"{len(issues)} 项异常" or "正常"`。

### 4.5 `ai_tuner/allocation/profit_extraction_job.py`

1. `get_cron_trigger()` → `minute=34`（07:34），docstring 同步。
2. `run_daily_check()` 各返回分支补 `equity` / `ath`，并在**每个分支**写入 outbox：
   - `notified`：正文用现有通知全文（保留 `💰 利润提取提醒`），`status="warn"`（需人工操作）、`summary="已触发，建议提取 X.XX U"`。
   - `skip`（含 `not_new_high` / `not_profitable` / `below_min_extract` / `already_notified_this_week` / `zero_or_negative_equity` / `disabled`）：正文按确认样例：

     ```
     【利润提取提醒】本次未触发
     当前权益未超过历史最高 ATH 666.77 U，未创新高，不发送提取建议。
     （触发时的内容：当前权益 / 初始资金 / 累计盈利 / 建议提取金额及比例 / ATH 变更）
     ```

     其中权益/ATH 用真实值；无法取数时写 `当前权益读取失败（<原因>）`。`status="skip"`、`summary="未触发"`。
   - `error`：`status="error"`、`summary="检查异常"`，正文含异常摘要。
3. 新增私有方法 `_publish(text, status, summary)`：写 outbox，失败只记 warning，绝不影响原有 ATH/通知逻辑。
4. `digest_outbox` 导入失败（容器内旧镜像）时降级为直接 `notification_client.send`，避免整任务失败。

### 4.6 systemd 单元（`ai_tuner/ops/`）

| 文件 | 变更 |
|---|---|
| `trading-hrs-acceptance.timer` | `07:33` → `07:31` |
| `trading-grid-shadow.timer`（新增入库） | `07:35` → `07:32` |
| `trading-btc-xau-inspection.timer` | `07:38` → `07:33` |
| `trading-log-retention.timer` | `09:00` → `07:34`（Description 同步） |
| `trading-daily-digest.timer` | 新增 `07:35` |

`xau-runtime-evidence.timer`（`07:40` → `07:30`）由 `scripts/deploy_xau_review.py` 生成并安装；本次同步修改该脚本与 `docs/ops/xau-ai-tuner-patrol.md`。

### 4.7 `ai_tuner/ops/grid_shadow_check.py`（新增入库）

来源：生产 `/root/trading_system/ai_tuner/ops/grid_shadow_check.py`（298 行），纳入版本控制后新增 `--send`，默认改为 `publish("grid", message, ...)`。`status`/`summary` 从正文结论行提取（`异常：无` → `ok`；含 `异常：` 非无 → `warn`）。

> 阻塞项：当前 SSH 到 43.156.242.184 被拒（`Connection closed`），需恢复后取回源码；若始终取不到，此段暂按现有 07:35 直发并在汇总中显示「本次未生成」，其余 5 段照常合并。

## 5. 生产变更（本次不执行，仅随批准单列出）

1. **journald 扩容**：`/etc/systemd/journald.conf` → `SystemMaxUse=3G`，`systemctl restart systemd-journald`。
2. **两个附加服务改 journald**：`trading_system-hrs-acceptance`（service `hrs-acceptance-reporter`）与 `trading_system-market-cap`（service `market-cap-service`）由 json-file 改 journald。因其由受限发布覆盖文件（`.releases/**/images.json`）创建，做法：
   - 在**当前生效**的覆盖文件对应 service 下加入 `"logging": {"driver": "journald"}`；
   - 用创建时相同的 `config_files`（`docker inspect` 的 `com.docker.compose.project.config_files`）执行 `docker compose up -d --no-deps <service>`；
   - 校验 `HostConfig.LogConfig.Type == journald` 且 health 正常。
   - 后续该服务每次受限发布须沿用同一覆盖文件，否则会回落 json-file；在部署文档中登记该约束。
3. 安装/刷新上述 systemd 单元：`cp` 到 `/etc/systemd/system/`、`daemon-reload`、`enable --now`。
4. 新增 webhook 文件 `/root/trading_system/.secrets/daily_digest_webhook`（0600），与现有各巡检 webhook 同群（部署时用 sha256 比对确认）。
5. 汇总器首次运行前手工清空/归档历史 outbox。

## 6. 测试

新增 `tests/test_ai_tuner_mvp/test_daily_digest.py`：

1. 6 段齐全 → 输出顺序、表头、概览正确；
2. 缺 1 段 → 该段显示「本次未生成」且计入异常概览，`exit 0`；
3. 陈旧段落（昨天生成的同 key 文件）→ 视为缺失；
4. 长度超 24000 字节 → 拆 2 条，第二条含「续 2/2」；
5. 发送失败 → 不归档、`exit 1`；
6. `--dry-run` 不写归档、不发请求（mock）。

新增 `tests/test_ai_tuner_mvp/test_digest_outbox.py`：原子写、非法 key/status、损坏 JSON 跳过。

修改 `tests/test_ai_tuner_mvp/test_log_retention_check.py`：窗口异常由 N 条变 1 条；`--send`/默认 outbox 分支。

新增 `tests/test_ai_tuner_mvp/test_profit_extraction_digest.py`：未触发/已触发/异常三分支正文与 outbox 字段。

## 7. 验证步骤（编码后执行）

```bash
cd /private/tmp/feishu-daily-digest-20261119
python3 -m compileall -q ai_tuner/ops ai_tuner/monitor ai_tuner/allocation
python3 -m pytest tests/test_ai_tuner_mvp/test_digest_outbox.py \
  tests/test_ai_tuner_mvp/test_daily_digest.py \
  tests/test_ai_tuner_mvp/test_log_retention_check.py \
  tests/test_ai_tuner_mvp/test_profit_extraction_digest.py -q
# 手工样例渲染（不发送）
DIGEST_OUTBOX_DIR=/tmp/digest-demo … python3 ai_tuner/ops/daily_digest.py --dry-run
```

幻觉测试 10 项逐项核对：导入、方法/参数、配置 key、路径、变量、分支、异常路径、同步/异步（`run_once`/`run_daily_check` 均为 async，`publish` 为同步，不得误加 `await`）、硬编码、文件完整性。

## 8. 验收标准

- 07:35 只收到 1 条飞书消息（超长时 2 条），含 6 段且顺序正确。
- 各段不再单独发消息（`--send` 仅手动排障）。
- 日志巡检的「可查窗口不足 14 天」由 18 行收敛为 1 行。
- 利润提取未触发时仍显示「本次未触发」+ 权益/ATH 说明。
- 任一生产者失败不影响其余段落发送。

## 9. 非目标

- 不修改利润提取的业务阈值与提取逻辑。
- 不纳入 07:30 宿主机 server_check.sh。
- 不在本次自动执行任何生产变更（等待批准 + 复审）。
