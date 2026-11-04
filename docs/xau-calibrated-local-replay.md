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

校准版不能使用 `--skip-snapshot-verify`；输入事件和 coverage 的哈希必须与逻辑 manifest 一致。回放还会逐根核对观察窗口内 1m K 线的连续性和收盘时间，并要求资金费结算记录非空、相邻记录及窗口边界间隔不超过 8 小时；缺失或截断则拒绝回放，不能把资金费当作零。若该合约实际结算周期超过 8 小时，应先取得可核验的交易所日程证据并更新校验口径，不得直接放宽。报告单独列出 `xau_calibrated` 的虚拟已结交易、净盈亏、回撤和准入检查。只有完整八周决策覆盖、无小时缺口、快照完整且检查项全部通过时，才可能出现 `calibrated` 准入；否则区分 `insufficient_data` 与 `none`。当前已知 2026-11-02 04:00–07:00 UTC 四个小时没有决策事件，因此即使随后有盈利，报告仍应是 `insufficient_data`。原版/激进版的双 profile CLI 和报告维持原样。

截至 2026-11-04 05:00 UTC 的线上只读核对：校准版共 166 条事件、160 个不同小时、6 条跨 run 重复、12 条候选信号、0 条数据质量降级；该窗口应有 164 个小时，缺的正是上述 4 小时。此计数不是回放结果，不能据此推断已结虚拟交易或盈利。

## 4. 注册有效期

当前 XAUUSDT K 线注册预计 2026-11-19 到期，需在临近到期时由用户人工授权续期。每次续期后重新读取 `expires_at`，直到覆盖校准版观察终点 2026-12-23 09:00 UTC；单次 30 天续期未必足够。巡检只提醒，不能自动续期。
