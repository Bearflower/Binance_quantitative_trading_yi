---
id: Binance_quantitative_trading-20261015-naive-timestamp-beijing-convention
类型: decision
项目: Binance_quantitative_trading
标题: 无时区 TIMESTAMP 列的 datetime 参数必须归一为 naive 北京时间（入口统一 _entry_time_to_naive_beijing）
日期: 2026-10-15
标签: [数据库, 时区口径, asyncpg, trade_records, 跨策略]
任务标识: newcoin_executed_at_timezone_2026-10-15
状态: 已采纳
审查: 待审查
---

# 无时区 TIMESTAMP 列的 datetime 参数必须归一为 naive 北京时间

## 背景
`trading.trade_records.executed_at` 等列为**无时区 `TIMESTAMP`**，项目约定其存储口径为**北京时间**
（写入口径 `shared/trade_logger.py`：`datetime.now(BEIJING_TZ).replace(tzinfo=None)`）。
数据库驱动为 **asyncpg**，向无时区 `timestamp` 列绑定**带时区** datetime 会抛
`DataError: invalid input for query argument $N`。

而 `entry_time` 等字段来源不一：既有无时区字符串（`datetime.now().isoformat()`），
也有带时区字符串（`datetime.now(timezone.utc).isoformat()`、aware 的 `entry_time.isoformat()`）。
R2 任务中，new_coin 三处以 `datetime.fromisoformat(entry_time)` 解析后直接作为
`executed_at BETWEEN $3 AND $4` 参数 → aware datetime 触发上述 `DataError`，盈亏回写/止损打标失败。

## 备选方案
- **A（选定）**：在解析入口把任意 `entry_time` 统一归一为**无时区北京时间**（新增公共函数
  `_entry_time_to_naive_beijing`：aware 值先 `astimezone(BEIJING_TZ)` 再去 tz，naive 值原样返回，缺失/非法返回 None），
  三处调用点复用同一函数。
- **B**：把 `executed_at` 列迁移为 `timestamptz`，全链路保持 aware。否定：DDL 迁移 + 全库读路径改造，
  爆炸半径大；历史行口径混存，迁移期易错；超出本次 R2 范围。
- **C**：仅 `dt.replace(tzinfo=None)` 去 tz（不换算）。否定：对 UTC 输入会把「UTC 时刻」当作北京时间存/查，
  整体偏移 8 小时，语义错误（见 F3 同类事故）。

## 决策
采用 **A**：**任何写入/查询无时区 TIMESTAMP 列的 datetime 参数，必须为 naive 北京时间**
（`tzinfo is None` 且已换算到 UTC+8 口径）。new_coin 侧解析入口统一为
`_entry_time_to_naive_beijing`（commit `94ca3ed`，`strategies/new_coin/strategy.py` L31-L56）。

## 理由
1. 与既有写入口径（`trade_logger` 北京 naive）一致，读写同源，不引入第二套口径。
2. 不触碰列类型与历史数据，零迁移风险，符合 R2「不改下单/平仓/资金计算」的边界。
3. 单点归一函数可消除三处重复的 `try/except` 解析（满足「禁止重复代码」规范），并顺带覆盖
   aware/naive/缺失/非法四类输入。

## 后果
- **眼下收益**：修复 `DataError`，盈亏回写与止损打标恢复；新函数可被同族的范围外缺陷复用
  （止损打标类型不符 issue、时区缺陷族 issue）。
- **技术债**：`BEIJING_TZ` 仍在 new_coin / `shared/trade_logger.py` / `dashboard/*` / `ai_tuner/*`
  多处**重复定义**，未提升为共享常量；`btc_eth` / `btc_eth_aggressive` 的同源代码（aware 北京窗口查无时区列）
  尚未按本约定整改（已另开工单）。约定应推广到全部「写/查无时区列」的路径。

## 适用范围与修订
- 适用于本项目所有对无时区 TIMESTAMP 列（`trade_records.executed_at`、`metric_snapshot`、
  `strategy_position_snapshot_history` 等）的 datetime 写入与查询参数。
- 落地提交：`94ca3ed`（2026-10-15）——`strategies/new_coin/strategy.py` 新增 `BEIJING_TZ` +
  `_entry_time_to_naive_beijing` 并替换三处调用点。
- 待推广项/替代：常量统一上收至 `shared`（未决）；`btc_eth` 侧同族缺陷见关联 issue。
- 保留原决策依据，后续如选择方案 B（迁移 `timestamptz`）应显式替代本条目并说明迁移方案。

## 关联与证据
- 关联需求/方案: [fix-2026-10-05-new-coin-executed-at-timezone-requirements](../../plans/fix-2026-10-05-new-coin-executed-at-timezone-requirements.md)（§3 根因、§4.1 公共函数、§7 回退）
- 持久证据: 提交 `94ca3ed`；[strategies/new_coin/strategy.py L31-L56](../../../strategies/new_coin/strategy.py#L31-L56)（`BEIJING_TZ` + `_entry_time_to_naive_beijing`）；[shared/trade_logger.py L18 / L240/L274](../../../shared/trade_logger.py#L18)（写入口径权威）；[database/postgres/init-scripts/03-create-trade-records.sql](../../../database/postgres/init-scripts/03-create-trade-records.sql)（列类型 TIMESTAMP 无时区）
- 必要证据摘要: asyncpg 向无时区 timestamp 列绑定 aware datetime 抛 `DataError: invalid input for query argument $3`；R2 新增函数四分支（aware/naive/缺失/非法）100% 行/分支覆盖，13 新增用例全绿、失败集合不扩大。待验证项：约定在 btc_eth 等同族路径的推广。
- 相关: [Binance_quantitative_trading-20261015-aware-naive-timezone-defect-family](../issues/Binance_quantitative_trading-20261015-aware-naive-timezone-defect-family.md)（同族范围外缺陷）、[Binance_quantitative_trading-20261015-newcoin-stoploss-entry-time-type](../issues/Binance_quantitative_trading-20261015-newcoin-stoploss-entry-time-type.md)（可复用本函数修类型不符）
