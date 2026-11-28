---
id: Binance_quantitative_trading-20261127-recovery-digest-collector-metadata
类型: issue
项目: Binance_quantitative_trading
标题: 采集刷新时间进入资金归属凭据摘要导致审核计划不稳定
日期: 2026-11-27
标签: [accounting, 归属, 幂等, 证据摘要]
任务标识: production-ledger-reconcile_2026-11-27
状态: 已解决（固定模块维护验证；常驻镜像待后续正常发布携带）
审查: 已审查
---

## 现象与根因

同一固定收入窗口，两次只读预演的 `suffix_fill_sha256` 不同，金额和策略不变。`closed_lot_plan` 对 SELECT * 的 fill 行计算哈希，包含采集器会刷新的 `fetched_at`，导致审核计划失效。

## 解决与验证

`stable_fill_digest` 明确绑定 account/symbol/trade/order/side/qty/price/quote_qty/commission/commission_asset/realized_pnl/trade_time_utc，排除采集元数据；新证明标记 `suffix_fill_hash_version=2`，旧落库证据不可变。

新增失败测试先复现，再修复：相关测试 50 passed。全路径刷新元数据后计划保持不变；12 个经济字段逐项修改都会改变摘要。生产固定计划追加 15 条订单证据、11 条资金费归属，原样重跑新增 0，经济事实及固定窗口报告一致。权限审计 1353 对象、0 issue。

## 适用范围与来源

仅 `scripts/recover_proven_order_income.py::closed_lot_plan/stable_fill_digest` 的维护证据摘要，不改变收入真值或 PM/HRS/AI 门槛。基线提交 `81be98b0`；修复源码 SHA256 `19bb4bce2aae354597431c5a49ae625c6d60f271bbdcdc1e488298cac7c661e5`。常驻镜像未更新，本轮通过固定版本的一次性维护模块执行。

来源：[正式核对报告](../../reports/production-ledger-reconciliation-2026-11-27.md)、`tests/test_accounting/test_recover_proven_order_income.py`。同模型独立上下文复核及主负责人来源核对通过，不声称跨模型审查。

## 教训（AI 归纳，仅供参考）

经济事实证据哈希要列明字段、带版本；采集时间等可更新元数据不能决定财务归属的幂等身份。
