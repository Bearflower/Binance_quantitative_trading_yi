---
id: Binance_quantitative_trading-20261015-aware-naive-timezone-defect-family
类型: issue
项目: Binance_quantitative_trading
标题: aware/naive 时区缺陷族（范围外）：new_coin 60 秒守卫静默失效 / 周报窗口漏计北京周一 00:00-08:00 / btc_eth 以 aware 北京窗口查无时区列 + BEIJING_TZ 重复定义
日期: 2026-10-15
标签: [bug, new_coin, btc_eth, 时区, aware/naive, 静默吞异常, 重复定义]
任务标识: aware_naive_timezone_defect_family_2026-10-15
状态: 待复查
审查: 待审查
---

# aware/naive 时区缺陷族（范围外）

R2「new_coin executed_at 时区修复」发布前复审（GLM-5.3 独立对话）暴露**同一类**缺陷的若干**范围外**实例：
数据库列 `trading.trade_records.executed_at` 为**无时区 TIMESTAMP**（约定存北京时间），
但代码在**不同位置**把 aware datetime 传入无时区列查询窗口、或反向用 `replace(tzinfo=None)` 制造偏移、
或用裸 `datetime.now()`（naive 主机本地时间）与 aware 值做运算。本条目按子项分列，均为**既有、非 R2 引入、
未修复**，故未阻塞 R2 发布；须另开修复工单。

## 现象 / 根因 / 影响（按子项分列）

### F2 — `new_coin` 入场 60 秒守卫静默失效（`strategy.py` L1400-1411）
- 位置 [strategy.py L1400-L1411](../../../strategies/new_coin/strategy.py#L1400-L1411)：
  `entry_dt = datetime.fromisoformat(entry_time_str)`、`now = datetime.now()`（**无时区**），
  以 `(now - entry_dt).total_seconds() < 60` 判定「刚入场不足 60 秒，跳过平仓检测（避免 API 传播延迟误判）」。
- 若 `entry_time_str` 为**带时区** ISO 串（如 `+00:00`），`entry_dt` 为 aware，与 naive `now` 相减
  抛 `TypeError: can't subtract offset-naive and offset-aware datetimes`。
- 该异常被 `except (ValueError, TypeError): pass` **静默吞掉** → 守卫**静默失效**：
  本应被 60 秒窗口挡住的「传播延迟误判平仓」不再被挡住。
- 修复方向：统一用 `_entry_time_to_naive_beijing` 把 `entry_time_str` 归一为 naive 北京时间，`now` 亦取
  naive 北京时间口径；并**去除空 except**（至少记日志）。

### F3 — `new_coin` 周报窗口起点漏计北京周一 00:00-08:00（`strategy.py` L919-938）
- 位置 [strategy.py L919-L938](../../../strategies/new_coin/strategy.py#L919-L938)：
  `now = datetime.now(timezone.utc)`；`week_start = (now - timedelta(days=now.weekday())).replace(hour=0, ..., tzinfo=None)`。
- `now` 为 UTC，`week_start.replace(tzinfo=None)` 得到的是**UTC 周一 00:00 的 naive 值**；
  而 `executed_at` 存的是**北京时间**。UTC 周一 00:00 == 北京周一 **08:00**
  → 查询区间 `executed_at >= week_start` **漏计北京周一 00:00-08:00 成交**（每周最多 8 小时窗口的成交被排除）。
- 正确做法：先把 `now` 归一到北京时区再取周一 00:00（或 `week_start.replace(tzinfo=BEIJING_TZ).astimezone(...)` 后再去 tz）。

### F4 — `btc_eth` / `btc_eth_aggressive` 以 aware 北京窗口查无时区列；`BEIJING_TZ` 重复定义
- [btc_eth/strategy.py L5204-L5219](../../../strategies/btc_eth/strategy.py#L5204-L5219)：
  `beijing_tz = timezone(timedelta(hours=8))`；`now = datetime.now(beijing_tz)`（**aware**）；
  `window_start/window_end` 为 aware，直接作为 `executed_at >= $2 AND executed_at <= $3` 参数
  → 与 `executed_at` 无时区列比较/绑定存在与 R2 同类的时区风险（enforce 取决于 asyncpg 对该查询的绑定行为）。
  [btc_eth_aggressive/strategy.py L5215 起](../../../strategies/btc_eth_aggressive/strategy.py#L5215-L5228) 为同一段代码的镜像副本。
- **常量重复定义**：`BEIJING_TZ = timezone(timedelta(hours=8))` 在 [new_coin/strategy.py L32](../../../strategies/new_coin/strategy.py#L31-L32)、
  [shared/trade_logger.py L18](../../../shared/trade_logger.py#L18)、`dashboard/backend/services/*`、`ai_tuner/*` 等多处各自定义，
  口径应统一（建议提升为 `shared` 共享常量，各模块复用），避免偏移写错或改口径时漏改。
- 修复方向：查询参数一律归一为 naive 北京时间；`btc_eth` / `btc_eth_aggressive` 两处同源代码同步修。

## 解决
**未解决（另开工单，范围外）**。三子项独立成因、同一「aware/naive 未归一」主题，建议一次性
按 [决策：naive 北京时间归一约定](../decisions/Binance_quantitative_trading-20261015-naive-timestamp-beijing-convention.md) 统一整改：
- F2/F4：查询/守卫前把 datetime 归一为 naive 北京时间；
- F3：窗口起点先归一到北京时区再取整点；
- 常量：抽到 `shared` 单点定义后各模块 import 复用。
- 特别提示：F2 的 `except (ValueError, TypeError): pass` 属「静默吞异常」反模式，整改时不得只改时区而保留空 except。

## 验证
- 静态定位：上列行号经仓库读码核对（2026-10-15），四段代码形态与描述一致。
- 动态证据：F2 的 `TypeError` 由 GLM-5.3 独立复审按 aware 入参构造复现；
  F3 的窗口偏移为可直接推演的时区换算（UTC 周一 00:00 = 北京周一 08:00）；
  F4 与 R2 同源（R2 已在 new_coin 侧修复）。
- **待验证项**：各子项修复后补针对性用例（aware 入参不再抛异常 / 窗口起点含北京周一 00:00 / btc_eth 查询参数 tzinfo 为 None），并确认无回归。

## 适用范围与修订
- 适用于 `strategies/new_coin/strategy.py`、`strategies/btc_eth/strategy.py`、`strategies/btc_eth_aggressive/strategy.py`；
  基线 `fce42bf`（2026-10-15 复核）。
- 与 R2（commit `94ca3ed`，仅修 new_coin 三处查询调用点）**同族但范围外**：R2 未触碰本三处以避免破坏其证据链。
- 与 [决策：naive 北京时间归一约定](../decisions/Binance_quantitative_trading-20261015-naive-timestamp-beijing-convention.md) 配套；无替代条目。

## 教训
（AI 归纳，仅供参考）
1. **aware/naive 混用是「静默型」缺陷**：类型错误发生在相减/绑定时，且常被宽泛 except 吞掉，
   表面看是「守卫没生效/少算了几笔」，实为整段监控失效，排查成本极高。跨「UTC 存储」与「北京时间列」边界的运算，
   应显式全程保持单一 tz 口径。
2. **`replace(tzinfo=None)` 不是「去时区」，而是「丢掉偏移」**：对 UTC 值去 tz 得到的 naive 值等于北京时间 -8h，
   窗口起点会整体偏移（本例漏计 8 小时成交）。去 tz 前必须先 `astimezone(BEIJING_TZ)`。
3. **同一常量/规则跨模块复制粘贴 = 定时炸弹**：`BEIJING_TZ` 在十余处各自 new 出来，改口径或纠错时极易漏改；
   共享口径应单点定义。

## 关联
- 本地补充来源: [new_coin_executed_at_timezone 交接](../../handoffs/new_coin_executed_at_timezone_2026-10-15.md)（不随 Git 同步）
- 持久证据: [new_coin/strategy.py L1400-L1411](../../../strategies/new_coin/strategy.py#L1400-L1411)（F2）、[L919-L938](../../../strategies/new_coin/strategy.py#L919-L938)（F3）、[btc_eth/strategy.py L5204-L5219](../../../strategies/btc_eth/strategy.py#L5204-L5219)（F4）、[btc_eth_aggressive/strategy.py L5215-L5228](../../../strategies/btc_eth_aggressive/strategy.py#L5215-L5228)（F4 镜像）、[shared/trade_logger.py L18](../../../shared/trade_logger.py#L18)（`BEIJING_TZ` 权威定义）
- 必要证据摘要: F2 = aware 入参与 naive `now` 相减抛 `TypeError` 被 `except (ValueError, TypeError): pass` 吞掉致 60 秒守卫静默失效；F3 = `week_start.replace(tzinfo=None)` 使周报起点为北京周一 08:00，漏计北京周一 00:00-08:00 成交；F4 = btc_eth/aggressive 以 aware 北京窗口查无时区列 + `BEIJING_TZ` 在 3+ 模块重复定义。均为既有、非 R2 引入。待验证项：整改后针对性用例 + 无回归。
- 相关: [Binance_quantitative_trading-20261015-newcoin-stoploss-entry-time-type](./Binance_quantitative_trading-20261015-newcoin-stoploss-entry-time-type.md)（同目录同源于「未归一/类型不符」）、[Binance_quantitative_trading-20260922-shared-account-position-ownership](./Binance_quantitative_trading-20260922-shared-account-position-ownership.md)（跨模块共享口径主题）
