---
id: Binance_quantitative_trading-20261013-dashboard-pnl-caliber-map
类型: decision
项目: Binance_quantitative_trading
标题: 看板盈亏三口径设计确认（本期净盈亏=净值差法，总盈亏=全历史已实现+浮动）
日期: 2026-10-13
标签: [dashboard, 指标口径, 盈亏]
任务标识: hrs_pnl_dedup_2026-10-13.md
状态: 已采纳
审查: 待审查
---

# 看板盈亏三口径设计确认（本期净盈亏=净值差法，总盈亏=全历史已实现+浮动）

## 背景
用户核对看板时质疑：本期净盈亏 2.66「不等于总盈亏，不等于浮动盈亏与已实现盈亏的汇总计算」，怀疑计算有 bug。需要核查三个盈亏数字的口径关系并定论。

## 核查（2026-10-13 生产实测）

| 卡片 | 公式 | 实测值 | 窗口 | 来源 |
|---|---|---|---|---|
| 本期净盈亏 | 今日实时净值 − 期初快照净值（**净值差法**） | 568.25 − 563.45 = **+4.80**（用户看页面时为 2.66，随实时净值变动） | 本期（日/周/月，随周期联动） | `equity_snapshot` 期初快照 + 币安实时净值，`get_account_returns` `period_pnl` |
| 总盈亏 | **已实现 −7.24 + 浮动 +7.61 = +0.37**（**分解法**） | +0.37 | **账户全历史** | 前端 `main.js` `updateOverview` 同响应内 `realizedValue + unrealizedValue` 加法 |
| 已实现盈亏 | 历史全部已平仓订单净额累计（已扣佣金） | −7.24 | 账户全历史 | trade_records 聚合 / income 链 `total_pnl` |
| 浮动盈亏 | 当前持仓未实现盈亏（币安 positionRisk 实时） | +7.61 | 当前时点 | `_query_unrealized_pnl` |

关键事实：总盈亏卡是前端**同一响应内的加法**（`data.total_pnl + data.total_unrealized_pnl`），与"已实现卡 + 浮动卡"**数学恒等**，不存在自相矛盾；视觉 0.01 级差异仅为显示舍入。

## 决策
维持三口径现状，**不改文案**（用户拍板「先不改」）。理由：

1. 本期净盈亏与总盈亏回答的是不同问题：前者是「本期净值涨了多少」（期初净值已吸收历史盈亏），后者是「账户开天辟地以来累计 + 当前浮盈」；
2. 两者相等需要苛刻条件（期初快照日 = 开户日、期间无出入金/划转、无口径差），日常必然不等，属设计行为而非 bug；
3. 现有 tooltip 已各自说明公式（本期净盈亏=「期初快照到当前的净资产增量，含浮动盈亏」、已实现=「历史全部已平仓订单」），信息可查。

## 已知误导点（未来可选优化）
「总盈亏」卡片名未强调「已实现部分是全历史累计」，tooltip 只说「已实现盈亏 + 浮动盈亏（账户当前真实盈亏）」，易被误读为「本期的总盈亏」。若未来用户再困惑，最小改动是 tooltip 补「全历史已实现累计」字样（纯文案，R1）。

## 验证方法（下次再疑可复用）
```bash
ssh root@43.156.242.184 "curl -s http://localhost:8767/api/overview | head -c 400"
ssh root@43.156.242.184 "curl -s 'http://localhost:8767/api/account/returns?period=daily'"
```
核对三关系：① 总盈亏 ≈ 已实现 + 浮动（同一响应，应恒等）；② 本期净盈亏 = 返回体 `equity - period_start_equity`；③ 本期净盈亏 ≠ 总盈亏为正常（窗口不同），差异 ≈ 期初前已实现累计 ± 本期出入金/资金费净值效应。

## 关联
- 持久证据: `dashboard/frontend/index.html` 三处 data-tip；`dashboard/frontend/js/main.js` `updateOverview` L270-293；`dashboard/backend/services/data_service_docker.py` `get_account_returns`（净值差法 L1391-1402）
- 相关: [Binance_quantitative_trading-20261013-hrs-pnl-double-count](./issues/Binance_quantitative_trading-20261013-hrs-pnl-double-count.md)（同日排查；「已实现 −7.24」为 HRS 防双计部署后、预计算重写前的过渡值，将随新口径收敛）
- 相关: [Binance_quantitative_trading-20261009-max-drawdown-caliber](./Binance_quantitative_trading-20261009-max-drawdown-caliber.md)（同属看板指标口径设计）
