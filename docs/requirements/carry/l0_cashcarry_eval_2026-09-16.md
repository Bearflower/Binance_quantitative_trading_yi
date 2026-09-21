# L0 cash-and-carry 择机回测与立项评估 - 交接存档
- 创建时间: 2026-09-16
- 任务源: 用户探索新策略方向，先后证伪 momentum/regime/alt-season/朴素配对，最终锁定 L0 cash-and-carry（做多现货+做空等额永续吃正 funding）
- 取决于上一份: N/A

## 🎯 终极目标
判断相对价值策略族 L0（现金-持有套利）是否值得立项：验证"阈值 gate + 精选池"的择机型形态能否捕到真实正收益，并给出资金门槛，为推进会/落地做准备。

## ✅ 已完成事项（会固化，不再改动）
- 服务器全量拉取 357 币 × 2 年现货/永续 1h + 8h funding，777MB 已 scp 拉回本地：`backtest/cash_carry/data_2y/sym/{币}/`（kline.csv / kline_perp.csv / funding.csv）
- 数据校正两处 bug：现货 limit 1000 逻辑、人民币混入币种剔除
- L0 静态测算：约 20 币长期正费率（SFP/CELR/DYDX/PEOPLE 全期净年化稳定 +3~6%）；崩段 funding 均值 -12.9%、震荡 -7.7%，全币种等权中位数 -1.4% → 必须精选池 + 阈值 gate
- **择机型回测原型已跑通**：精选池 82 币（2年净年化>3.1%）+ 阈值 gate 3.1%，2 年总净值 1.120，折合年化 **+5.6%**（2024 +11.9% / 2025 +4.8% / 2026 +3.9%）；组合约 72% 月份在场；单币到场期年化 SFP/ONE/GTC/DYDX 达 +8~10%
- 资金门槛结论：本金不决定能否正收益（比例收益），但建议 **1000~2000 USDT（约1~2万 RMB）起步**，300 USDT 是纯技术下限
- 立项评估报告已更新：[L0_cashcarry_立项评估_2026-09-16.md](docs/requirements/discover/)（新增第 9 节择机回测+资金门槛）
- 持仓窗口时长统计：中位数 2 个月、均值 3.2 个月；今年(2026)高费率机会月占比 44%，与去年持平

## 🚧 进行中 / 未完成事项
- [块] 是否正式立项：待用户决策 | 择机回测 (+5.6%) 与资金门槛已明确，但落地形态（精选池币种名单、gate 参数、资金分配）尚未推进会确认
- [块] 择机回测脚本的"年份年化"显示曾有计算 bug：已修复为复合净值口径，但结果未与真实资金费率字段双重交叉核验 | 低优先，数据源已直接用 funding.csv 月均值糙算

## 📝 最新可执行代码/配置快照
择机回测入口：`backtest/cash_carry/scripts/l0_selective_backtest.py`
```python
# 运行（默认参数：阈值3.1%、精选池82币、扣0.15%手续费）
python3 backtest/cash_carry/scripts/l0_selective_backtest.py
# 可选参数
# --threshold 0.0315   成本阈值年化
# --poolsize N         精选池数量上限（取 annual_mean 降序前 N）
# --all                全 357 币池（对照，非精选）
```

## 🔗 关键路径与依赖
- 报告：docs/requirements/discover/L0_cashcarry_立项评估_2026-09-16.md
- 设计文档：docs/requirements/discover/配对交易讨论稿091601.md
- 数据：backtest/cash_carry/data_2y/（sym/{币}/*.csv、funding_cyclicity.csv、持仓窗口统计）
- 回测脚本：backtest/cash_carry/scripts/l0_selective_backtest.py、l0_download_on_server.py
- 已互补证伪记录：docs/handoffs/new_strategy_exploration_2026-09-15.md

## 💡 给新对话的启动指令
第一步先读 `docs/requirements/discover/L0_cashcarry_立项评估_2026-09-16.md`（含第9节最新结论）和 `docs/requirements/discover/配对交易讨论稿091601.md`。若用户要正式立项：从精选池 82 币中确认最终币名单与等权资金分配、gate 阈值敏感度（0.02/0.0315/0.04 扫参）确认稳健性，再决定是否接入生产。若不再推进，可直接以 +5.6% 与 1000-2000 USDT 门槛作为结论收尾。