---
id: Binance_quantitative_trading-20261128-derived-attribution-export-omission
类型: issue
项目: Binance_quantitative_trading
标题: 待认领清单漏读派生归属层，误列已归属收入
日期: 2026-11-28
标签: [账本, 归属, 报表口径, 导出]
任务标识: historical-income-owner-confirmation-2026-11-20
状态: 已解决
审查: 已审查
---

# 待认领清单漏读派生归属层，误列已归属收入

## 现象

2026-11-20 的 PM 账户历史收入处理期间，一份临时导出的「3298 笔待补充」清单把已由自动派生层归属的事件也列为未归属。该清单已撤回，不能据此重复回填。这是清单导出口径错误，不代表交易所收入事实丢失或正式月报有 3298 笔未归属。

## 根因

临时清单漏选自动派生归属层。`read_monthly_economic_report` 的 `include_derived_attribution` 参数默认是 `False`；`read_published_report` 未显式指定时则由环境配置决定是否启用。生产看板与 AI Tuner 当时的正式月报采用 `exchange-income-v1-derived-v1`，因此仅按基础层生成的清单与正式消费口径不同。

## 解决

撤回临时清单，以同账户、同时间窗口的正式派生归属月报核对待认领事件身份；人工认领只针对仍未归属且身份、金额和既有记录核对一致的事件。此次没有因清单错误修改原始收入事实。

## 验证

2026-11-20 的记录载明：764 笔原始清单中 89 笔已有一致的复核归属，其余 675 笔经账户所有者指定后新增复核记录。随后 2026-08、09、10 三个正式派生归属月报的未归属数均为 0（事件数分别为 1,416、1,341、1,302）。这些数字仅证明当时对应账户和窗口的归属报告状态，不证明独立交易所属主、策略盈利或后续月份状态。

## 适用范围与修订

适用于 PM 历史收入导出与同类多层归属报告。2026-11-20 的运行状态是历史快照；后续 Classic 账户切换和验收以其独立记录为准。无替代条目。

## 教训

AI 归纳，仅供参考：生成待认领清单前，应明确账户、窗口、报告版本、归属层和数据水位，并与实际消费方的正式报告按事件身份核对；不能把基础层未归属数直接当作待人工认领数。

## 关联

- 持久证据：[历史收入人工归属与正式口径复核](../../reports/historical-income-owner-confirmation-2026-11-20.md)「逐事件处理」「正式月报与运行状态」；[报表读取实现](../../../shared/accounting/report_reader.py) `read_monthly_economic_report`；[发布报告实现](../../../shared/accounting/published_report.py) `read_published_report`。
- 必要证据摘要：临时 3298 笔清单因漏读派生层撤回；正式派生归属月报当时三个封窗月未归属数为 0。没有重新执行生产数据查询，本条是对已存档记录与当前代码的知识整理。
- 相关：[人工归属证据分层决策](../decisions/Binance_quantitative_trading-20261128-owner-attestation-provenance.md)、[Classic 历史容缺决策](../decisions/Binance_quantitative_trading-20261127-bounded-history-acceptance.md)。
