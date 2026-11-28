# 统一生产修复归档前复核（2026-11-28）

> 本文保留 00:13 的历史复核；同日后继整合、9,696 项回归及生产切换最新进展见[统一生产验收](successor-unified-production-acceptance-2026-11-28.md)，不要用本页旧运行组合判断当前部署。

复核时间：**2026-11-28T00:13:18+08:00（北京时间）**。结论：**本聊天原定工程修复、历史测试修复、全量回归、统一部署和网格交接已完成，可作为一个工程阶段留档；当前不能写成“全部业务与后继版本验收均已完成”。本次未执行聊天归档。**

## 逐项对账

| 事项 | 状态与依据 |
|---|---|
| 全量代码审查与修复 | 已完成；最终整合增量 OCR 覆盖 31/31，原先未改变部分延续前轮审查。 |
| 历史测试失败与完整回归 | 已完成；`c4a0bea5` 全部 553 测试文件、9,617 项通过，0 失败／错误／跳过。该计数是 11 月 27 日该源码的证据，本次没有重新运行全量测试。 |
| 全量部署和真实联调 | 已完成；11 月 27 日 19:18 验收 23/23，K 线真实复测 52/52、浏览器 19 请求无失败。详见[原验收报告](unified-production-acceptance-2026-11-27.md)。 |
| 原 0.078 ETH 与网格交接 | 已关闭；11 月 27 日取得两次空仓证明后完成交接，无强制平仓。本次仍为同一新执行器，健康、0 重启，心跳新鲜，状态 FLAT、风控 ALLOW、数据流正常。 |
| HRS／AI 当前准入 | 本次受限业务角色只读查询：HRS ready/allowed、AI risk_ready、attribution_ready 和 current_coverage_complete 均为 true。 |
| 历史归属与完整权益证明 | **未全部完成**；仍为 ACCEPTED_GAPS、original_history_complete=false。批准范围内可运行，不等于历史补齐。 |
| 后继变更的统一回归 | **本次未提供新的完整回归证明**；昨晚统一验收后 7 个服务定向升级，当前是四个源码版本组成的运行集，不能继续将 c4a0bea5 的 9,617 项结果当作当前组合全量回归。 |
| 自然周期和长期业务验收 | 未在本聊天关闭：12 月 1 日 09:30 自动分配首个自然周期、HRS 完整 30 日证明及研究晋级按各自任务保留。不能提前填完成。 |

## 当前实际版本

| 服务范围 | 数量 | 实际 VERSION GIT_SHA | 后继证据 |
|---|---:|---|---|
| 网格、主要策略、行情及其他未换实例 | 16 | c4a0bea5 | 本聊天统一发布 |
| 两个 XAU 影子容器 | 2 | d402ad0c | [Actions 46014565497](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/46014565497)；提交 ebcbba52 的 `docs/reports/xau-version-provenance-2026-11-27.md` |
| HRS、HRS acceptance、AI 主进程及快照 | 4 | 71384375 | [Actions 46023068401](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/46023068401)；提交 4eae73b9 的 `docs/reports/historical-evidence-recovery-2026-11-27.md` |
| 看板 API | 1 | 92b637a0 | 运行 VERSION 与 [Actions 46015199120](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/46015199120) 标识 |

上述 7 个实例与旧 full-regression pin 的容器 ID 不同；部分实例保留旧来源标签，但 VERSION 和后继来源标签已经更新。这与后续定向发布记录相符，不据此认定运行故障，也不把旧 pin 当作当前版本唯一真值。本轮核对了实际容器身份、VERSION 与健康；未宣称重新逐文件审查并完整回归全部后继改动。

当前 23 个目标全部 healthy、0 重启；最近 30 分钟 ERROR／CRITICAL／Traceback 标记为 0。两个独立研究容器仍为原 ID，均运行；HRS observer 原设计无 Docker Healthcheck。统一权限工具 audit 核验 1,355 对象通过，清单哈希 `62513735d6954615158f466f8561c1325567efdf3011f6899f75af30849137c1`。本次没有改生产容器、交易订单、权限、审批或风险参数。

## 归档建议与开放项

本轮原交付可标记“工程阶段完成”。若要求“所有未完成事项都关闭后再归档”，目前条件仍不满足，故本次保留聊天、不自动归档。

1. 对当前后继版本运行集冻结新的来源清单；若需要签署最新完整回归，先整合对应源码，再运行全量回归与统一验收，不能直接覆盖旧 pin 或重启旧备份。
2. PM 原生历史、切换期末权益／历史 NAV 缺口以及 HRS 长期证明继续独立保留，沿用现有有限历史批准，不通过清风险锁或新期初制造完整性。
3. 12 月 1 日自动分配的自然运行结果，届时按实际落库与审批边界验收；本次不提前执行、不人为制造成交。

知识复用：采用[有限历史容缺](../knowledge/decisions/Binance_quantitative_trading-20261127-bounded-history-acceptance.md)中“历史事实与当前准入分开”的原则。采用项目已有工程／收益验收分开记录的口径；未将知识库其他策略自然样本或研究待办一并关闭。本次为当前任务增量复核，不是全知识库巡检。

持久证据：[当前只读状态摘要](../reviews/2026-11-28-archive-readiness/current-state.json)、[原回归明细](../reviews/2026-11-27-unified-production/regression.json)。私有业务原始结果位于本机 `/tmp/binance-archive-audit-20261128/`，仓库仅保留脱敏字段及证据哈希。
