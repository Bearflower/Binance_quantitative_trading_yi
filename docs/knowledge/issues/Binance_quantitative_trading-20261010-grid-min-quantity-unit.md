---
id: Binance_quantitative_trading-20261010-grid-min-quantity-unit
类型: issue
项目: Binance_quantitative_trading
标题: 网格 min_quantity 单位错误导致资本可行性提醒全线误报
日期: 2026-10-10
标签: [bug, grid, 单位换算]
任务标识: grid_min_quantity_unit_fix_2026-10-10.md
状态: 已解决
审查: 已审查
---

# 网格 min_quantity 单位错误导致资本可行性提醒全线误报

## 现象
V2.5.5 方案B 上线后，用户按 14:05（北京 10:05）真实推送在币安实际成功建成 12 格网格，但推送文案「当前建议保证金 673 USDT 无法支撑最少 5 格（每格仅 0.54 张，需 ≥1 张）」全线误报，与实际可建 12 格矛盾。用户确认「1 张 = 0.01 ETH」后推翻 D 段验算。

## 根因
`config.yaml` 的 `trading.min_quantity: 1` 被当作「1 ETH」使用（实际 1 张 = 0.01 ETH）；公式 `N_cap = floor(M×L/(P×q_min))`（grid_calculator.py#L1013）代入 q_min=1，把 1 张当 1 ETH，放大约 100 倍。全 `strategies/grid` 无 contract_size 换算，quantity 全程 ETH 口径。

## 解决
改 `min_quantity: 1 → 0.01`（ETH 口径，公式结构不动，本次不引入 contract_size）；修 config 注释与 signal_bot 文案「张」→「ETH」；O-1 并入：binding=PROFIT 不可行时抑制「可达上限」行与方案1/2；附带 revert be0a71a 资本下限抬升机制。

## 验证
本任务（min_quantity 单位修复）真实证据：命令1 `tests/test_strategies/test_grid_capital_linkage.py` → 54 passed（52 基线 + 2 新增）；命令2 三文件 + `--cov=strategies/grid --cov-branch` → 101 passed, 1 failed（唯一失败 = 基线既有 `test_strategy_analyze`，非本次引入）；命令3 `test_dynamic_grid_params.py` 场景 1/2 通过、场景 3 基线既有中断。部署 commit cb4551e 后 §9.10 条件④（O-1 生产生效）经 18:05 真实推送核实达成，条件①②③ 待一条 `feasible=true` 推送补验。
（注：58/58 + 717/717 属后续「PROFIT 不可行文案修复」任务（commit 663edea）的证据，非本任务，勿混用。）

## 适用范围与修订
strategies/grid/{config.yaml, grid_calculator.py, signal_bot.py}；commit cb4551e 已上线（grid 生产 /app/VERSION=cb4551e）。min_quantity 取值依据 ETHUSDT 永续 1 张合约面值 0.01 ETH；若未来引入多币种需重议 contract_size 参数。

## 教训
「数值互算自洽 ≠ 正确」——D 段验算三处数字自洽但仍误报，验证必须回溯到源头输入假设（单位），不能止于公式内部自洽。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/grid_min_quantity_unit_fix_2026-10-10.md)（不随 Git 同步）
- 持久证据: 需求文档 docs/requirements/grid/网格交易系统 V2.5.5 保证金-网格数联动（方案B）需求与设计.md §9；commit cb4551e
- 必要证据摘要: min_quantity 1→0.01 ETH（ETHUSDT 永续 1 张合约面值 0.01 ETH）；命令1 54 passed、命令2 101 passed/1 failed（唯一失败=基线既有）、命令3 场景1/2通过；§9.10 条件④已核实、①②③ 待 feasible=true 推送补验
- 相关: [作废 be0a71a 资本下限抬升机制](../decisions/Binance_quantitative_trading-20261010-grid-capital-floor-reverted.md)