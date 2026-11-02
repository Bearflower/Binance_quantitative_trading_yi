---
id: Binance_quantitative_trading-20261029-precision-key-camelcase-silent-fallback
类型: issue
项目: Binance_quantitative_trading
标题: 精度键名驼峰/下划线不一致致静默兜底，BNBUSDT 下单被交易所拒 -4003
日期: 2026-10-29
标签: [bug, 精度, btc_eth, btc_eth_aggressive, 保护单, 静默失效, 高优先级]
任务标识: r05_r06_precision_and_na_legs_2026-10-29
状态: 已解决
审查: 已审查
---

# 精度键名驼峰/下划线不一致致静默兜底，BNBUSDT 下单被交易所拒 -4003

## 现象

2026-10-27 生产容器 `trading_system-btc_eth_aggressive` 下单被交易所拒绝，返回 `[-4003] Quantity less than zero.`，标的 BNBUSDT。订单数量为按兜底步长算出的 0.009，小于交易所可接受的正数下限。

## 根因

代码 4 处用 `precision.get('step_size', '0.001')` / `precision.get('tick_size', '0.01')` 读取步长与最小价格变动，但 `BinanceClient.get_symbol_info` 返回的是驼峰键 `stepSize` / `tickSize`。下划线键永远取不到，`get` 恒落入兜底值 0.001。

- 涉及函数：`_sync_trailing_stop_order`（移动止损）、`_get_protection_precision`（保护单精度），`strategies/btc_eth/strategy.py` 与 `strategies/btc_eth_aggressive/strategy.py` 各 2 处，共 4 处。
- 后果链：BNBUSDT 真实步长 0.01，按兜底 0.001 计算 `initial 0.03 × TP1 比例 0.3 = 0.009` 未再取整（0.009 是 0.001 的整数倍），提交即被拒。

## 解决

4 处统一改调 `_get_precision_params()`（内部按驼峰键读取并返回 `(step_size, tick_size)`），不再自行解析字典键。另在 `_sync_trailing_stop_order` 补「数量精度调整后 ≤0 则跳过下单」分支。`_place_missing_tp_orders` 等零数量 N/A 处理属独立改动，随后由 `174de235` 一并上线（详见关联 issue）。

## 验证

- 上游修复提交 `47af6c02`（fix(mtpcs): read exchange precision keys for protection orders）已合并 `origin/main`：两策略 4 处下划线键读取全部改为 `_get_precision_params()`，并在 `_sync_trailing_stop_order` 补「精度调整后数量 ≤0 则跳过下单」守卫；新增精度回归测试 `tests/test_mtpcs_fixes/test_precision_key_reading.py`。
- 生产容器 `trading_system-btc_eth_aggressive` 现运行 `925d6693`（含 `47af6c02` 及后续微仓 N/A 修复），`DEPLOY_TIME=2026-10-29 07:38:27 UTC`、`DEPLOY_ID=00008101`（=`(129<<8)|1`，与公式一致）。
- 该版本部署后连续观察 **18 小时**，容器日志中 `[-4003] Quantity less than zero.` 计 **0 次**，无同类拒单复发；BNBUSDT 步长按交易所真实值 0.01 参与取整。
- 范围说明：本提交修正**有效交易所元数据中的键名误读**；`get_symbol_info` 查询失败时使用默认精度是既有独立风险，本次未改全局下单接口口径，不能声称其已被消除。

## 适用范围与修订

模块：MTPCS 原版与激进版策略文件的移动止损/保护单精度读取；版本：`47af6c02` 起修复，生产当前运行 `925d6693`（2026-10-29 07:38:27 UTC 部署，`DEPLOY_ID=00008101`）；无替代条目。

## 教训

（AI 归纳，仅供参考）带默认值的 `dict.get` 会把「键名写错」伪装成「参数缺失」，问题不报错、只表现为数值偏小；凡涉及交易所过滤器字段，键名必须与 API 返回的真实大小写对齐，并以单元测试固化键名。

## 关联

- 持久证据：提交 `47af6c0` 中两策略 `strategy.py` 与 `tests/test_mtpcs_fixes/test_precision_key_reading.py`；[部署 Run #40812741247](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/40812741247)；历史文档 [BTCUSDT 交易错误修复报告](../../reports/BTCUSDT交易错误修复报告_20260515.md) L67-68 已写明过滤器键为 `stepSize` / `tickSize`。
- 必要证据摘要：4 处调用点已全部改为 `_get_precision_params()`；生产容器版本 `925d6693` / `DEPLOY_ID=00008101` 已核对，部署后 18 小时 `-4003` 计数为 0。
- 相关：微仓腿 N/A 口径另见 [issue](./Binance_quantitative_trading-20261029-micro-leg-zero-protection-replenish-exhausted.md) 与 [decision](../decisions/Binance_quantitative_trading-20261029-micro-leg-not-placeable-na-caliber.md)。
