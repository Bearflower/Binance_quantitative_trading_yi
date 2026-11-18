# HRS 月度证据 archive 与 v3 登记

此修订仅涉及离线留证导出、回放和实验工具，不修改生产策略或重启服务。
[v3 登记](hrs-exit-experiment-registration-2026-11-18-v3.json) 写于 **2026-11-18 13:41:08.366100 CST**，仍早于研究下界 11 月 19 日。
[v2 登记](hrs-exit-experiment-registration-2026-11-18-v2.json) 和更早登记/报告保留；v3 的经济 baseline、24 个候选、成本、最低 20 个闭合生命周期、时间下界及首扫描规则与 v2 逐项相同。
它只增加存储与资源契约，不观察未来留出结果、不选择新参数。

## 已确认的容量缺口

根任务提供的授权只读生产测量：04:00–04:26 UTC 有 870 条公开响应，JSONB 文本共 48,838,527 bytes，TOAST 约 4.4 MB。
旧 reader 会读取全窗公开响应，没有按相同响应内容去重；完整 source 在 50,001 行或 64MiB 时拒绝。
只按 60 秒有效采集周期、每次两条公开响应计算，31 天已有 89,280 条；不能再把该问题归为“没有整月数据所以无法判断”。
循环实际是请求结束后等待 30 秒，故不把“精确每 30 秒”当作生产事实。

## 新存储契约

`hrs-evidence-archive-v2` 保留每条原始 receipt 的 ID、run、类型、符号、event_time、available_at 和原 payload 哈希。
相同 JSON 子树可以共享 CAS 对象，包括重复 exchangeInfo.symbols、资金费响应和 K 线；每次接收记录不合并，也不把后到值改成早到值。
读取时必须逐片验证文件 SHA、引用闭包、原始 envelope/payload 哈希、独立源 COUNT、顺序摘要和重复 ID 冲突。

固定预算：

| 范围 | 上限 |
|---|---:|
| archive receipts | 2,000,000 |
| 重建逻辑体积 | 256 GiB |
| archive 物理体积，含受约束临时索引 | 32 GiB |
| 全 archive 分片 | 8,192 |
| 每 receipt 分片 | 50,000 条、64 MiB 逻辑体积 |
| 派生 1m 执行 bars | 3,000,000 |
| 派生调度 events | 4,000,000 |

市场批次另有独立 `expected_item_count`；receipt 数不冒充批次内的实际 bar 行数。
31 天×52 符号×1,440 分钟为 2,321,280 个执行 bars，落在明确预算内。超出任何预算仍拒绝且不 seal。
现有引擎仍会物化派生 bars/events；这些计数预算不证明任意 universe 的恒定 RAM、运行时间或绩效。

旧 JSON source 的 50k/64MiB 限制保持；新 archive 不通过简单提高旧 cap 或最终拼回大列表工作。
转换器通过只读 SQLite 索引逐扫描、逐符号访问；背景 collector 的月度 run 不会载入为一个大分组。
bar 冲突校验与排序使用独立临时 SQLite spool。临时索引不写入或复制进封存快照。

## 真实来源读取

已取得授权 SELECT 连接的调用者可以执行：

```python
sources = await read_evidence_sources(
    connection,
    account_id,
    window_start=registered_start,
    as_of=registered_end,
    end_policy={"version": "first_scan_start_v1", "max_delay_seconds": 3600},
    archive_root=private_archive_directory,
    market_pairs="scanned",
    timezone_bindings=verified_column_timezone_bindings,
)
```

同一个 readonly repeatable-read 快照中，先独立查询 COUNT 和真实事件 min/max，再用 cursor 写 receipts。
后续权益来源只查引用哈希/账本时点/真实终点附近；资金费观测只查扫描范围的符号。
市场读取复用已有 `raw_export` 的固定列、受限表标识符和显式 naive 时区证据规则，按最多 1,000 个 bars 写批次。
支持至多 192 个 symbol/interval pairs，因此 52×1m/1h/4h 的 156 对不会被旧的 64 对上限挡住。
旧 `raw_export` 的 64 对/总 64MiB 限制没有放宽；新月窗应直接使用这个 cursor 路径。
没有明确时区证据的 naive 时间仍拒绝，不猜 UTC 或上海时区。

资金费日程按符号和实际 received_at 流式校验，保留首个有效发布 receipt、独立预告与 420 秒覆盖门槛。
采集上界为日历终点后的固定一小时；晚于实际解析终点的 receipts 仍留在原始 archive 清单中，但不被当前窗口提前使用。

## 回放与边界绑定

封存快照仍使用原 manifest builder。`raw_hrs_evidence.json` 成为小描述符，所有 inventory/receipt/object 分片均进入快照文件哈希。
CLI 完整验证 archive 后重算所有候选边界，而不是信任 receipt 中预选的起点。
最早扫描不完整或非空仓仍不能跳过；研究、两个观察月必须共享同一捕获边界和 payload 哈希。

派生 decision input 只引用原始 `analysis_evidence_id` 与 payload 哈希。
评分时按索引恢复该条实际 1h/4h 数组，并校验符号、类型、事件时刻、score_available_at 和原哈希；不会从未来最新 bars 替代。
审计记录保留该可解引用身份，不重复复制整月每次评分的 168-bar 数组。
运行时身份额外包含 archive reader 和 market source helper；正式 research 与 holdout 仍须精确匹配实际源码、loaded code、Python 和依赖版本。
普通 MTPC 和旧 list 资金费验证不依赖 HRS Python 模块；HRS 专用索引存在时才导入 HRS 清理逻辑。

导出可直接传 archive inventory，而不是巨型原始 source JSON：

```bash
python -m ai_tuner.hrs_evidence_export \
  --sources /private/archive/hrs_evidence_archive_inventory.json \
  --output-root data/hrs-replay/research_20261119_20261201_v2 \
  --window-start 2026-11-19T00:00:00+08:00 --as-of 2026-12-01T00:00:00+08:00 \
  --first-scan-max-delay-seconds 3600 --end-first-scan-max-delay-seconds 3600 \
  --participation-rate DECLARED_RATE \
  --monitor-cadence-seconds CAPTURED_CADENCE --monitor-anchor ACTUAL_ANCHOR
```

执行模型参数仍必须来自真实捕获或明确登记的假设，示例占位符不是事实。
也兼容小型旧 `--raw-market-export`：逐条重写为完整新 archive，保留独立 query 行数、实际记录与原哈希，不在内存中拼接所有来源。

## 验证与未满足门槛

工程用例实际包含超过 50k receipts、超过 64MiB 的原始逻辑体积，并完成 archive→转换→seal/preflight→真实 HRS 引擎→CLI 六次成本运行。
同内容不同接收时间、跨片完整性、缺片/篡改/重复冲突、最早坏边界、全局预算、显式时区、52 符号×3周期、旧格式和无 HRS 模块的 MTPC 路径分别验证。
这些合成工程数据不是生产或历史绩效证据。

[v3 当前发现报告](hrs-exit-experiment-discovery-2026-11-18-v3.json) 仍为 `BLOCKED_DATA`、0 engine runs、无候选选择、`performance_accepted=false`。
首条实际起点、完整来源、真实现金流/费用/资金费、自然闭合样本、30 天门禁历史、政策有效期、配置分区及正式运行时一致性仍必须满足。
当前 exporter 仍严格拒绝缺少成本/MAE/保护证明的非空仓 opening；本修订不授权实盘清仓、补造午夜状态、政策续期或宣称 SL/TP 验收完成。
