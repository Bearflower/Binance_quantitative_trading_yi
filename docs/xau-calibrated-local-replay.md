# XAU 校准版：离线多 run 汇总与回放

此流程只读取已导出的影子事件，在本地生成新的汇总/回放结果；不会修改在线事件、生产交易表或正在运行的策略。不得在交易服务器执行批量回放。

## 1. 准备输入

使用现有单 run 导出命令，逐一导出校准版的所有物理 run。每个目录须含 `run_manifest.json`、`decision_events.jsonl`。另外保留冻结的 `xau_calibrated.yaml`、`xau_calibrated_atr_manifest.json` 和它指向的原始 1h 快照文件。不要手动补造缺失的决策事件。

## 2. 汇总等价配置链

在本地仓库根目录运行（`--run-dir` 对每个物理 run 重复一次）：

```bash
python3 -m backtest.btc_eth.replay.reconcile_runs \
  --run-dir /local/exports/run-a \
  --run-dir /local/exports/run-b \
  --profile-config strategies/btc_eth/shadow/config/xau_calibrated.yaml \
  --calibration-manifest strategies/btc_eth/shadow/config/calibration/xau_calibrated_atr_manifest.json \
  --calibration-snapshot /local/data/xauusdt-1h-snapshot.jsonl \
  --start 2026-10-28T09:00:00+00:00 \
  --end 2026-12-23T09:00:00+00:00 \
  --output-dir /local/results/xau-calibrated-reconciled
```

生成 `coverage.json`、`decision_events.jsonl`、`run_manifest.json`。汇总会验证配置和校准证据哈希、逐 run 身份及逐事件身份；跨 run 同小时事件若内容不同则报错停止。输出目录必须尚不存在；先在同级临时目录写齐三份文件，再一次性发布，写入失败不会留下半成品。逻辑 run_id 同时包含决策内容和来源覆盖证据，来源变动时不会复用旧 ID。`coverage.json` 的 `missing_hours` 是真实决策缺口，不等于 `no_signal`。观察未结束时可将 `--end` 设为当前已完成的 UTC 小时之后，但不能据此准入。

## 3. 本地回放与报告

准备同期不可变 1m、资金费和交易规则快照目录，需含 `manifest.json`。然后运行：

```bash
python3 -m backtest.btc_eth.replay.main \
  --events /local/results/xau-calibrated-reconciled/decision_events.jsonl \
  --run-manifest /local/results/xau-calibrated-reconciled/run_manifest.json \
  --coverage /local/results/xau-calibrated-reconciled/coverage.json \
  --snapshot-dir /local/snapshots/xau \
  --profile-calibrated strategies/btc_eth/shadow/config/xau_calibrated.yaml \
  --output-dir /local/results/xau-calibrated-report
```

校准版不能使用 `--skip-snapshot-verify`；输入事件和 coverage 的哈希必须与逻辑 manifest 一致。回放还会逐根核对观察窗口内 1m K 线的连续性和收盘时间，并要求资金费结算记录非空、相邻记录及窗口边界间隔不超过 8 小时；明显缺失或截断则拒绝回放，不能把资金费当作零。但 8 小时间隔仍可能掩盖一笔缺失的 4 小时结算。缺少覆盖全窗口的事前结算日程时，即使快照通过上述检查，报告也只能产出参考指标，准入结果为 `insufficient_data`。资金费日程核验见下文。报告单独列出 `xau_calibrated` 的虚拟已结交易、净盈亏、回撤和准入检查。只有完整八周决策覆盖、无小时缺口、市场数据完整且检查项全部通过时，才可能出现 `calibrated` 准入；否则区分 `insufficient_data` 与 `none`。当前已知 2026-11-02 04:00–07:00 UTC 四个小时没有决策事件，因此即使随后有盈利，报告仍应是 `insufficient_data`。原版/激进版的双 profile CLI 和报告维持原样。

截至 2026-11-04 05:00 UTC 的线上只读核对：校准版共 166 条事件、160 个不同小时、6 条跨 run 重复、12 条候选信号、0 条数据质量降级；该窗口应有 164 个小时，缺的正是上述 4 小时。此计数不是回放结果，不能据此推断已结虚拟交易或盈利。

### 3.1 资金费逐笔核对

使用只读 PostgreSQL 连接导出事前观测。整个回放窗口的起点和终点均使用 UTC，并与决策覆盖报告一致。导出起点包含窗口前一个五分钟槽位，供核对窗口起点处的结算。

```bash
psql -XqAt -v ON_ERROR_STOP=1 \
  -v window_start=2026-10-28T09:00:00Z \
  -v window_end=2026-12-23T09:00:00Z \
  -f backtest/btc_eth/replay/funding_schedule_export.sql > funding-schedule-evidence.jsonl
```

从数据库导出后，将证据文件与冻结的分钟行情及历史资金费快照一同归档。回放时追加 `--funding-schedule-evidence funding-schedule-evidence.jsonl`。证据仅用于校准版核验，回放仍在本地进行。

核对逐条验证 XAUUSDT 身份、观测字段哈希、采集与事前时序。每个五分钟槽位应有有效观测。实际资金费记录须带有 XAUUSDT 和明确费率。以一秒为结算时间容差，处理币安历史记录相对于预告时点的毫秒差。同一时点只允许一条实际记录匹配。缺槽、无匹配、额外结算、重复或冲突都保留在报告中。文件哈希纳入回放 ID。哈希用于复现和完整性校验，证据来源仍依赖只读生产导出。

本轮的日程观测始于 2026-11-18 02:25 UTC，早于该时间的窗口仍会暴露证据缺口。即使未来日程核验通过，决策覆盖和其他准入门槛仍单独核对。本轮已确认的四小时决策缺口继续保留。

## 4. 注册有效期

已核对 XAUUSDT K 线注册有效至 2026-12-30 16:00 UTC（北京时间 12 月 30 日结束），覆盖校准版观察终点 2026-12-23 09:00 UTC。若之后需要延长观察，巡检在临近到期时提醒人工授权续期，续期后读取实际 `expires_at`。

## 5. XAU 资金费日程与验收时间

自 2026-11-18 02:25 UTC 起，现有资金费日程采集器开始保存 XAUUSDT 的交易所时间、接收时间、下一结算时间和结算间隔。每五分钟采集一次。XAU 单独缺失或无效时记录告警日志，原有必采币种仍按既定校验记录。首笔证据预报 2026-11-18 04:00 UTC 结算，间隔四小时。正式验收时须将事前记录逐笔与交易所实际结算核对，并单列采集启用之前的历史证据缺口。

原版与激进版在 12 月 15 日 16:00（北京时间）结束八周观察。按各自完整窗口计算结果，再用三版共同窗口做横向比较。校准版在 12 月 23 日 17:00（北京时间）结束观察。按现行规则，已确认的四小时决策缺口仍须反映为 `insufficient_data`。三版均使用冻结输入和修复后的回放器，每个 profile 独立核对不少于 12 笔有效已结虚拟交易及其余准入门槛。

## 2026-11-25：入场类型分组与结算边界采集

`replay-v1.2.0` 在三个 profile 各自原有报告中增加 `entry_intent_breakdown`：
`trend`（趋势）、`mean_reversion`（价值回归）、`unknown`（无明确证据）。标签来自策略实际执行的 `strategy_mode` 分支，经 `feature_snapshot.entry_intent` 传到挂单、成交仓位与退出明细，不根据 `market_state` 推断。缺失、非法标签和同仓位退出标签冲突统一计入 unknown。`entry_route_fallback` 额外记录策略是否沿用缺省路由。

分组列出候选信号、风控拒绝、虚拟订单、成交、截止挂单、已结仓位、成交率、净盈亏及费用。成交率分母排除风控拒绝、包含截止挂单；无订单时不可计算。分批退出按 position_id 聚合一次。手续费和滑点为正成本，资金费正值为收入。分组共享本 profile 原有权益与风控，不产生六个独立账户、不单独判定准入，原总账与准入门槛不变。历史文件不改写，旧事件维持 unknown。报告格式升级进入 replay_run_id，避免与旧结果身份混淆。

资金费采集器仅在 XAU 的预告结算时刻已过期时刷新当前批次，最多三次、每次间隔一秒、每批最多十秒，且不超出当前五分钟槽的 120 秒采集上限。刷新使用公开 GET 得到新的完整响应，成功才替换原批次及真实请求/接收时间；刷新失败则保留原始常规币种证据，排除无效的可选 XAU；XAU 属于必需集合时失败关闭。不会推算未来结算时间，也不回填历史缺口。核验器仍要求结算前收到有效预告。

上线需单独定向发布资金费采集器与两个 XAU 影子容器，并校验运行版本。不要直接用旧 `xau-review` 发布脚本：它还会更新 ai-tuner 和定时器。必须比对现役镜像的共享代码和冻结配置，防止捎带其他交易逻辑变更；切换选在新一小时尚未写事件、行情已就绪且 cron 未执行的窗口，避免启动立即采集造成旧/新标签的同小时冲突。既定观察期不重置，记录版本切换时刻；本节文档不表示代码已部署。
