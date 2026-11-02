---
id: Binance_quantitative_trading-20261029-micro-leg-zero-protection-replenish-exhausted
类型: issue
项目: Binance_quantitative_trading
标题: 微仓比例腿取整为 0 致 TP1 始终挂不出，保护补挂耗尽并告警 missing=["TP1"]
日期: 2026-10-29
标签: [bug, 保护单, 微仓, 精度, btc_eth_aggressive, 补挂, 高优先级]
任务标识: r05_r06_precision_and_na_legs_2026-10-29
状态: 已解决
审查: 已审查
---

# 微仓比例腿取整为 0 致 TP1 始终挂不出，保护补挂耗尽并告警 missing=["TP1"]

## 现象

2026-10-27 生产容器 `trading_system-btc_eth_aggressive` 反复出现保护单补挂失败，达上限后告警 `保护单补挂达上限仍未完整 missing=["TP1"]`。硬止损正常，仅 TP1 腿长期缺失。

## 根因

按比例拆分的止盈腿数量在微仓下被精度截断为 0：`initial 0.03 × TP1 比例 0.3 = 0.009`，按步长 0.01 向下取整得 0，交易所不接受零数量订单。`_place_missing_tp_orders` / `place_missing_tp_level` / `_rebuild_condition_orders` 三条落点都因数量为 0 而无法下单，但 `_place_missing_tp_orders` 只 `continue/return None`，`protection_complete` 与 `missing_protection_legs` 仍把该腿计为缺失，于是补挂重试累计到配置上限（默认 3 次）后触发 `on_exhausted` 告警，并且永远无法自愈。

## 解决

- `PositionState` 新增 `not_placeable_legs: Set[str]`，标记已判定为不可挂的保护腿。
- `shared/protection_retry.py`：`protection_complete` 对 N/A 腿豁免、`missing_protection_legs` 不计入 N/A 腿、`place_missing_tp_level` 判定为 0 时标记并仅在首次告警。
- 三处落点（`_rebuild_condition_orders`、`_place_missing_tp_orders`、`place_missing_tp_level`）统一标记；`merge_position` 加仓时清空该集合（加仓后腿可能重新可挂）。
- 口径见 [decision](../decisions/Binance_quantitative_trading-20261029-micro-leg-not-placeable-na-caliber.md)：视为 N/A，不补足到最小下单量，硬止损仍全量保护；软件 TP1 在微仓下可能全仓平仓，此行为已明确记录。

## 验证

- 上游修复提交 `174de235`（persist TP levels and settle non-placeable legs）与 `925d6693`（preserve entry clock on TP2-only recovery）已合并 `origin/main`。
- 生产容器 `trading_system-btc_eth_aggressive` 现运行 `925d6693`，`DEPLOY_TIME=2026-10-29 07:38:27 UTC`、`DEPLOY_ID=00008101`（=`(129<<8)|1`，与公式一致）。
- 该版本部署后连续观察 **18 小时**，容器日志中 `保护单补挂达上限仍未完整`（`missing=["TP1"]`）计 **0 次**，补挂重试未再耗尽告警；硬止损保持全量保护。
- 历史证据（发布前）：本地定向回归 `209 passed`，覆盖 N/A 豁免、TP 等级落库/读取、仅 TP2 重启、旧单未知、硬止损失败告警与重建 TP1 已触发的安全闸；生产按交易所实时在挂单与策略快照逐单对账，事务内为 4 张真实 TP 写入等级，并已把激进版 XRP 中不在交易所的 35 条旧 OPEN 记录改为 CANCELED（见 Actions Run #40832632773，对账报告未纳入版本库）；`174de235` 经 [Run #40832632773](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/40832632773) 部署，激进版 SOL/XRP 重启均按原 TP1/TP2 ID 接管、未重复下单。

## 适用范围与修订

模块：`shared/protection_retry.py`、`shared/condition_orders.py` + MTPCS 原版与激进版策略文件；版本：`174de235` 起修复，生产当前运行 `925d6693`（2026-10-29 07:38:27 UTC 部署，`DEPLOY_ID=00008101`）；无替代条目。

## 教训

（AI 归纳，仅供参考）「补挂失败」和「本就不可能存在」是两类事实，前者要重试、后者要豁免；把两者混在同一个完整性判定里，会制造永不收敛的重试和噪声告警。

## 关联

- 持久证据：`shared/protection_retry.py`（完整性判定、补挂、加仓）、`shared/condition_orders.py`（TP 等级）、两策略 `strategy.py`（重启接管）；`tests/test_mtpcs_fixes/test_r05_r06_precision_na_legs.py` 等定向回归 209 项。
- 必要证据摘要：代码与迁移已复审，`174de235`/`925d6693` 已合并 `origin/main` 并上线；生产容器 `925d6693`（`DEPLOY_ID=00008101`）部署后 18 小时补挂耗尽告警计数为 0。
- 相关：[同批精度键名 issue](./Binance_quantitative_trading-20261029-precision-key-camelcase-silent-fallback.md)、[N/A 口径 decision](../decisions/Binance_quantitative_trading-20261029-micro-leg-not-placeable-na-caliber.md)、[R01–R08 架构方案](../../plans/fix-2026-09-29-p0-r01-r08-architecture.md)（R05 / D2）。
