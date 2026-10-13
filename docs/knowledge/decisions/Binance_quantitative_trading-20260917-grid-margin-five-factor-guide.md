---
id: Binance_quantitative_trading-20260917-grid-margin-five-factor-guide
类型: decision
项目: Binance_quantitative_trading
标题: 网格保证金由静态值改为五因子动态引导（BTC 一致性用独立检测器）
日期: 2026-09-17
标签: [grid, 保证金, 动态参数]
任务标识: grid_margin_advisor_v25_2026-09-17.md
状态: 已采纳
审查: 已审查
---

# 网格保证金由静态值改为五因子动态引导（BTC 一致性用独立检测器）

## 背景
网格策略保证金原为静态写死（`trading.margin=500`），不能随市场状态调整。策略需要按市场状态主动给出加码 / 减码 / 清仓建议，即「多因子保证金智能引导系统」（V2.5）。

## 备选方案
原始 handoff 未记录备选方案对比（仅记最终决策），故此处不补造。已确认的取舍细节如下：
- BTC 一致性因子：对 BTCUSDT **跑完整 MarketStateDetector**（新建独立检测器），而非复用 ETH 主检测器或仅取简单相关性；
- BTC 背离**不单独推送**，只在现有推送模板里加一行提示；
- 冷却期**保留建议但抑制动作**（文案「冷却中，暂不操作」），而非直接不推送。

## 决策
保证金由静态值改为五因子动态引导（市场状态、趋势强度、波动率、BTC 一致性、资金费率），由网格信号灯每小时:05 推送加码/减码/清仓建议，全程无硬编码、Decimal 计算。

关键映射规则：
- BTC 一致性映射系数：同向 1.0 / 震荡 0.7 / 反向 0.4（ETH 震荡时系数=1.0，不受 BTC 影响）；
- 状态系数按 MarketState 枚举映射：价格紧急/极端/波动异常 → 0，普通强趋势 → 0.3，弱趋势 → 0.6，震荡 → 1.0，震荡 + ADX<18 → 1.2；
- 冷却期保留建议但抑制动作。

## 理由
目标即理由：把保证金从「写死常量」变为随市场自适应的引导值；因 BTC 与 ETH 的市场状态可能不同步，BTC 一致性必须用**独立**检测器而非复用 ETH 结果。（AI 归纳，仅供参考）

## 后果
- 眼下收益：保证金随市场状态自适应，便于加码/减码/清仓择时；
- 技术债 / 待偿：五因子系数与触发阈值全部走 `strategies/grid/config.yaml` 的 `margin_guide` 配置节，后续调参须改配置而非代码；BTC 背离仅一行提示，未做独立信号。

## 适用范围与修订
`strategies/grid/{margin_advisor.py（新增）, signal_bot.py, config.yaml, Dockerfile}`；2026-09-17 13:15 CST 部署（DEPLOY_ID 5EDB85E6，五层验证通过）。**commit 引用更正**：部署时 `margin_advisor.py` 尚为未提交的工作区内容，其首次入库 commit 为 `75f1735`（2026-09-21，宽提交）；部署确认报告所记 `8f882bf` 系当时 HEAD 快照（2026-09-15 提交），并非本次改动提交。无替代条目。

## 关联与证据
- 本地补充来源: [handoff](../../handoffs/grid_margin_advisor_v25_2026-09-17.md)、[部署确认报告](../../reports/grid_v25_margin_deploy_2026-09-17.md)（handoff 不随 Git 同步）
- 持久证据: 需求文档 `docs/requirements/grid/网格交易系统 V2.5 完整策略文档.md`、`网格策略迭代记录.md`；`margin_advisor.py` 首次入库 commit `75f1735`（部署报告所记 `8f882bf` 为当时 HEAD 快照，非本改动）
- 必要证据摘要: 37 用例全通过（test_margin_advisor.py）；部署五层验证通过（容器 healthy / 镜像 sha256:276b1fda... / DEPLOY_ID 5EDB85E6 一致 / 3 文件 MD5 全对 / 日志 0 error）；两个 MarketStateDetector 实例初始化成功
- 相关: 无
