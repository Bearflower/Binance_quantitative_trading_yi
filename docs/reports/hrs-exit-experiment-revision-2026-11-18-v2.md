# HRS 前瞻退出实验登记修订 v2

修订登记于 **2026-11-18 12:12:26.906088 CST**，在新的研究输入发生前。
原始 [v1 登记](hrs-exit-experiment-registration-2026-11-17.json) 和旧发现报告保留原样。
本次未观察未来留出结果，也未调整 24 个候选、经济基线、成本、最低 20 个闭合生命周期或收益判定门槛。

原文件 SHA-256：`719d1fe724b43c43c3b444d76cc7e0199d8c4f80a8563c91364af7745ee11ea5`。
原 canonical registration SHA-256：`b3508f27215a58aee11594fe896ce2109fef9c27685f9a1270ecf13474bd9dca`。
候选清单 SHA-256：`021139e496c630ced8468122281a12ce5f1dd3d90db2903724ad35cd60f0a1e1`。
修订身份及实际登记时间同时记录在 [v2 登记](hrs-exit-experiment-registration-2026-11-18-v2.json)。

v1 研究起点是 11 月 18 日午夜，而完整留证在该时刻尚未成功上线。该缺口不能用后来数据填补。
v2 将研究下界改为 **11 月 19 日 00:00 CST**；研究结束下界及两个留出窗的日历下界仍为 12 月 1 日、1 月 1 日、2 月 1 日午夜。
这些日期只是预先固定的时间下界，不是采集或空仓承诺。实际扫描按原有运行时刻发生，不假定整点扫描。

每个边界使用 `first_scan_start_v1`：从完整原始扫描清单中，选择下界之后（含下界）不超过 3600 秒的首个实际 `scan_start.event_time`。
不能按收益、完整性、是否空仓或其他质量条件挑选后面的扫描。同刻多个独立起点、首条扫描不完整、缺失、超时或哈希不符均阻断。
首条非空仓不能跳过；当前 exporter 仍因缺少实际开仓费用、资金费、MAE 和保护状态的完整证明而拒绝非空仓 opening。不得为了实验强平实盘。

导出器将真实 start/end、选中证据 ID、原始 payload 哈希、候选边界清单和延迟写入 manifest 绑定的 receipt。
CLI 再从 manifest 绑定的完整原始证据重算这两个边界，使用同一实际起止时刻执行 DataPortal、真实策略回放和 NAV 计算。
结束边界上的新扫描属于下一个窗口；其决策不会加入前一个窗口。不回填午夜权益，也不把后到的输入改成提前可用。

12 月与 1 月采用另一个明确命名的 `scan_aligned_month_v1` 观察模型：

- 实际跨度必须至少 28×24 小时，并完整保留整个实际区间的数据、费用和资金费。
- 相邻窗口必须共享同一个实际扫描边界、证据 ID 和 payload 哈希；正式研究结束与首个留出起点也必须一致。
- 报告同时列出名义日历跨度、实际跨度及两端偏移。本登记中的两个名义月均为 31 天，因此允许的实际跨度约为 31 天±1小时。
- 它们是事前固定规则的观察月，**不是完整自然月**，不能替代旧 8–9 月或任何要求精确午夜的历史完整自然月验收。
- 未采用该新模型的 legacy 登记仍要求原有完整自然月；边界偏移将使其不合格。

边界规则只消除“扫描不会恰好发生在午夜”的工具障碍。缺少真实来源、首个起点不合格、样本不足、风险政策到期、配置变化未分区、未完成 30 天门禁历史或运行时漂移仍会阻断。
新政策的生效/到期日必须来自真实历史证据；本修订不续期生产风险政策。跨月配置变化必须另有事前登记和分区，不能把旧经济配置复制成未来事实。

`baseline.code_commit=954ee5f2…` 保留为 11 月 17 日生产经济配置采集的来源身份。
它不表示今天的回放引擎运行旧代码。每份 CLI 报告绑定实际 source、loaded code、Python 与依赖版本；边界解析器源码也包含在这个身份中。
正式 holdout 必须精确匹配正式 research 报告的运行时身份。当前发现报告不能充当研究成功、冻结候选选择或 holdout 放行证明。

当前 [CLI 发现报告](hrs-exit-experiment-discovery-2026-11-18-v2.json) 为 `BLOCKED_DATA`：`historical_hrs_snapshots_missing`，0 次 engine run、无候选选择、`performance_accepted=false`。
未来满足所有数据门槛后才可执行注册窗口；扫描对齐本身不构成 SL/TP 绩效验收。

导出时明确传入与登记相同的两个策略参数，并读取足够覆盖实际结束扫描的原始来源：

```bash
python -m ai_tuner.hrs_evidence_export \
  --sources /private/path/raw-source-bundle.json \
  --output-root data/hrs-replay/research_20261119_20261201_v2 \
  --window-start 2026-11-19T00:00:00+08:00 --as-of 2026-12-01T00:00:00+08:00 \
  --first-scan-max-delay-seconds 3600 --end-first-scan-max-delay-seconds 3600 \
  --participation-rate DECLARED_RATE \
  --monitor-cadence-seconds CAPTURED_CADENCE --monitor-anchor ACTUAL_ANCHOR
```

参与率、监控节奏和锚点仍需实际证据或明确登记的执行假设，不能把示例占位符当事实。
`read_evidence_sources(..., end_policy={"version":"first_scan_start_v1","max_delay_seconds":3600})` 仅扩大固定只读查询的时间上界；不修改任何来源时钟。

```bash
python -m ai_tuner.hrs_exit_experiment \
  --registration docs/reports/hrs-exit-experiment-registration-2026-11-18-v2.json \
  --snapshot-root data/hrs-replay \
  --output /private/path/research-report.json
```

本次只改离线工具与登记文档，不重启或替换生产交易镜像。
