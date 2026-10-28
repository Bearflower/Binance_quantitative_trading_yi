---
id: Binance_quantitative_trading-20261027-xau-calibrated-shadow-isolation
类型: decision
项目: Binance_quantitative_trading
标题: XAU ATR 校准采用独立影子实例与独立八周观察窗口
日期: 2026-10-27
标签: [XAUUSDT, MTPCS, 影子, ATR校准, 隔离, 不可变快照, 虚拟权益]
任务标识: xau_mtcps_calibrated_shadow_2026-10-27
状态: 已采纳
审查: 已补最终部署边界审查
---

# XAU ATR 校准采用独立影子实例与独立八周观察窗口

## 背景

既有 `mtcps-xau-shadow` 同时观察 MTCPS 原版与激进版。XAUUSDT 的价格量纲和 ATR 分布与 ETH 不同，直接在既有 profile 上改参数会污染已开始的观察窗口，也无法从报告中区分“策略差异”和“ATR 量纲校准”。因此需要在不增加真实交易暴露的前提下，建立可重放、可审计的独立对照组。

## 决策

1. 展示名固定为**“MTPCS 黄金校准版（XAU）”**，技术标识保持英文：容器 `mtcps-xau-calibrated-shadow`、instance `mtcps_xau_calibrated_shadow`、profile `xau_calibrated`、run prefix `xau-calibrated-shadow`。
2. 策略必须基于 **MTCPS 原版**，白名单外参数与 `xau_original` 保持一致；只允许校准 `atr_abs_min`、ATR fallback 与支撑确定性 history bootstrap 的必要字段。
3. 使用 **300 USDT 独立虚拟权益**，不与原版/激进版共享状态、run 或运行目录；`execution_mode=shadow_batch`、`real_ordering_enabled=false`，不发真单。
4. 每小时第 20 分运行。八周观察期只能从部署后**首个数据质量合格的干净事件**开始；开发、冻结和部署前验证均不计入。
5. 既有原版/激进版观察期**不重置**。最终报告对黄金校准版、原版和激进版使用相同时间窗口做比较，不混算全历史数据。
6. 运维停止命令固定为 `docker compose stop mtcps-xau-calibrated-shadow`，不影响既有影子服务或生产策略。
7. 首次创建采用 push changed-files 一次性 marker 驱动的目标化部署，只 pull/up 新服务并
   使用 `--no-deps`；仅 marker 的 `name-status=A` 触发，异常 push base fail-closed，普通后续
   部署保持全量路径。禁止以远端容器是否存在作为模式判据。
8. 健康状态必须来自运行态就绪标记：主 PID 存活、Linux starttime token 匹配且
   instance/profile/run prefix/cron/
   execution mode/安全开关与环境契约一致；恒成功探针不构成健康证据。
9. Run #120 在 Actions 解析阶段因单个 remote heredoc 表达式过长失败，未产生 job 或产物。
   远端 runtime 固定为仓库脚本 `scripts/remote_deploy_runtime.sh`；规范 retry marker 仅在
   `status=A` 时复用同一目标化边界，且该次 build matrix 只强制重建 `btc-eth`。原 marker
   与 retry marker 同时出现或 rename 形成歧义时 fail-closed。

## ATR 冻结证据

- 生成器：`scripts/generate_xau_atr_calibration.py`。
- 数据：2038 根有效 XAUUSDT 1h 闭合 K 线，产生 2025 个 ATR(14) 样本。
- 冻结值：P25 / `atr_abs_min` = `0.0016161930783960743`；P50 / fallback = `0.3160363742979797`。
- manifest hash：`46942cc14ceaa28dc8e9c397ad2c70b21de594f282b9def9c1410e66a4a71c78`。
- snapshot hash：`fb4cbe2c9cd0d39a1495faa584591466541f89eb25e932affe78c73137caea17`。
- `strategies/btc_eth/shadow/config/calibration/xau_calibrated_atr_manifest.json` 同时绑定快照 hash 与校准值；`xau_calibrated.yaml` 反向绑定 manifest/snapshot hash 与参数，任一不一致均 fail-closed。

## 命名技术债

旧 profile 存在 `strategy.risk.dynamic_atr.min_samples`命名，但生产实现读取的正式字段是 `min_history_count`。本次只在校准 profile 使用正确字段，**不做全局兼容、不顺手修改原版/激进版**，避免扩大回归面；该命名不一致作为独立技术债后续治理。

## 后果与边界

- 收益：ATR 校准效果与原/激进版观察完全隔离，冻结证据可验证、可重放。
- 代价：多一个影子容器和独立八周窗口；评估时必须严格对齐报告窗口。
- 安全边界：本决策仅授权影子实现和部署物料，**不表示已部署、不表示观察期已开始**；push / 部署仍受独立授权门禁约束。
- 部署边界：首次上线的目标化分支不得重建 `btc-eth-strategy` 或
  `mtcps-xau-shadow`；就绪超时自动停止且只停止新容器。`/tmp` 就绪标记不属于交易状态，不写数据库。

## 关联与证据

- 设计：[`2026-10-27-xau-mtcps-calibrated-shadow-design.md`](../../superpowers/specs/2026-10-27-xau-mtcps-calibrated-shadow-design.md)。
- 实施计划：[`2026-10-27-xau-mtcps-calibrated-shadow-implementation-plan.md`](../../superpowers/plans/2026-10-27-xau-mtcps-calibrated-shadow-implementation-plan.md)。
- 契约测试：`tests/test_strategies/test_xau_calibrated_profile.py`、`test_xau_atr_calibration.py`、`test_xau_calibrated_compose.py`。
- 与既有决策关系：不替代 [`xau-replay-report-caliber-position-aggregation`](Binance_quantitative_trading-20261019-xau-replay-report-caliber-position-aggregation.md)；后者定义回放报告指标口径，本条定义 ATR 校准版的隔离与观察窗口。
