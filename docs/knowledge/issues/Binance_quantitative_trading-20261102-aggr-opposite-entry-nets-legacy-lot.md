---
id: Binance_quantitative_trading-20261102-aggr-opposite-entry-nets-legacy-lot
类型: issue
项目: Binance_quantitative_trading
标题: 激进策略内存漏仓时反向新单抵消交易所旧仓
日期: 2026-11-02
标签: [bug, btc_eth_aggressive, 共享净额账户, 开仓, 资金安全, ETHUSDT]
任务标识: aggr_legacy_recovery_2026-11-02
状态: 待复查
审查: 已审查
---

# 激进策略内存漏仓时反向新单抵消交易所旧仓

## 现象

2026-11-02 15:10 北京时间，旧运行版对 ETHUSDT 产生 SHORT 信号时，旧多仓 0.023 仍在交易所、却不在策略内存中。15:11 卖出 0.021 后净多仓剩 0.002，15:18 又卖出 0.002，交易所净仓归零。候选镜像预检发现 ETH 已不再是原 0.023 旧 lot；当时交易所还留有 6 张 reduce-only 条件单，部署重启后已清除。

## 根因

新开仓分支以 `self.positions` 是否有该币种决定“空仓”，下单前未再次确认交易所真实净仓。共享 PM 账户采用净额持仓，反向入场单会抵消已存在但未接管的旧仓。

## 解决

`_place_entry_order` 在真实下单前查询交易所该币种仓位；若内存无仓而交易所数量非零，拒绝新开仓并记录原因。查询失败或返回结构异常也拒绝；继续使用原有归属/占位和资金限额检查。

## 验证

- 回归测试 `test_new_entry_rejects_exchange_position_even_if_memory_is_empty` 先失败后通过，验证相反方向信号不会调用开仓下单。
- `dbfcc652` 合并主线并上线。2026-11-02 16:07 北京时间生产源码哈希与主线一致；ETH 净仓 0、条件单 0，容器健康。
- **待复查**：上线后尚未出现“内存无仓而交易所同币有仓”的下一次真实信号；生产拦截效果目前由代码、单测和部署哈希证明，尚无第二次实盘触发证据。

## 适用范围与修订

适用于 `strategies/btc_eth_aggressive/strategy.py` 的新开仓路径，自 `dbfcc652` 起。无替代条目。

## 教训

AI 归纳，仅供参考：净额账户中“策略内存空仓”不等于“交易所空仓”。所有可能增加或反转敞口的下单路径，必须在实际下单前核对交易所净仓，不能仅依赖信号阶段的内存状态。

## 关联

- 持久证据：[部署记录](../../reports/aggr-legacy-recovery-deploy-2026-11-02.md)、`dbfcc652`、`tests/test_strategies/test_aggr_safety_regressions.py`。
- 必要证据摘要：ETH 原 BUY 0.023，随后 SELL 0.021 和 SELL 0.002，净额归零；6 张残余条件单重启后清零；代码测试与上线哈希核验通过。
- 相关：[旧仓恢复问题](./Binance_quantitative_trading-20261102-aggr-legacy-position-recovery.md)。
