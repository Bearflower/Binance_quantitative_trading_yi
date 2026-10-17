# 需求定义与验收标准：第二轮独立复审 R7–R9（2026-10-16）

> 状态：**已实现 + 已通过回归（2026-10-16 收口）**。本文定义「本轮需求 + 验收标准」；实现与回归证据见 §7「落地状态与验收结论」，不修改任何源文件、不部署、不连库。
> 权威输入：
> - 第二轮复审报告：[strategy_reaudit_2026-10-16_round2/README.md](../../reviews/strategy_reaudit_2026-10-16_round2/README.md)（第 1/2/3 节）
> - 可复现脚本：[strategy_reaudit_2026-10-16_round2/reproduce_findings.py](../../reviews/strategy_reaudit_2026-10-16_round2/reproduce_findings.py)（4 个探针：signal_probe / price_probe / pagination_probe / income_probe）
> - 上一轮需求范式：[requirements_R1-R6_2026-10-16.md](requirements_R1-R6_2026-10-16.md)
> - 业务决策记录：[decisions.md](decisions.md)（D2 / D4 / D8）

---

## 1. 需求概述

### 1.1 本轮目标

上一轮已收口 R1–R6。第二轮复审判定「尚不能接受 R1–R6 全部闭环」，新发现 3 处问题。本轮**只修这 3 项**，逐条满足「问题本质 → 期望行为 → 验收标准」的可判定闭环，并把复现脚本中的 3 个对应场景（signal_probe / price_probe / pagination_probe）固化为「先红后绿」回归用例。

| 需求 | 优先级 | 分组 | 问题一句话 |
|---|---|---|---|
| R7 | P1 | execution_facts | 成交续页同时发送 `fromId` 与 `startTime/endTime`，违反真实接口互斥约束 |
| R8 | P1 | new_coin | 执行前重取报价后，未按新价重算额度/数量/最小下单条件 |
| R9 | P2 | HRS | 同币同向生产信号先更新，把影子信号记录一并标为 executed |

### 1.2 范围边界（明确排除项，本轮不得宣称完成）

| 排除项 | 说明 |
|---|---|
| 净收益对账 / 权益守恒 / 全策略归属账本 | 属后续研究+验收，仍排除 |
| MTPCS 趋势 / 回归分支定位、分支独立净收益 | 研究/验收工作，仍排除 |
| income 同毫秒显式不完整分支 | 复审已披露的能力限制，**不另算新缺陷，不纳入本轮修复**（见 §3 末尾说明） |
| push main / 部署 / GHCR / 服务器 | 本轮不触发部署 |
| apply 迁移 / 连接生产库 | 不执行任何 DB 迁移，不连生产库 |
| 调用真实交易所 | 所有测试走 mock / Fake API / 合成行情 |

### 1.3 硬约束（全程遵守）

- 不 push main、不部署、不 apply 迁移、不连生产库、不调真实交易所。
- 工作区存在大量其他未提交改动，**只允许触碰本轮相关文件**（见 §5 分组，每个文件仅修与本轮需求相关的行），不得顺带 reformat / 重构无关文件。
- 代码注释与日志一律中文。
- 禁止硬编码业务参数（阈值、时长、限额、分位数一律读配置或由算法推导）。
- 禁止重复代码：连续 5 行以上同构、或逻辑相同仅变量名不同，必须抽公共函数。
- 禁止幽灵参数 / 死字段：函数形参与结构体字段必须被真实使用。
- 单函数 ≤ 50 行；单行 ≤ 120 字符。

### 1.4 本轮独有的硬约束（三处根因同源，必须落实到需求）

三处缺陷的根因**均属「模拟环境不够严格导致测试假绿」**：Fake API 同时接受两种过滤条件、新币测试未构造价格跳变、signal sink Fake 只按宽范围谓词更新。因此**同步收紧 mock/fake 是这三条需求的共同前置要求**，不是可选的附加项：

1. **分页 Fake API 必须校验参数互斥**：`fromId` 与 `startTime/endTime` 同时出现时立即 `raise`（不得静默接受），使 R7 的非法组合能被捕获。
2. **新币测试必须构造价格跳变报价**：`entry_guard` 返回的新报价价格 ≠ 旧报价价格，才能暴露「重取价后未重算额度」。
3. **signal sink Fake 必须校验更新谓词**：按真实 SQL 的 `WHERE` 谓词判断「一次 UPDATE 到底改了几行」，使 R9 的宽范围污染可被观测。

使每一条都满足「修复前红、修复后绿」可被自动捕获。

---

## 2. 逐条需求

### R7 [P1] 成交续页必须分离时间定位与 ID 续传（属 execution_facts 组）

**问题描述**
`export_fills()` 的 `api_call_factory` 每次调用都传入 `start_time=cs, end_time=window_end_ms`，第一页填满后下一页增加 `from_id`；而 `get_user_trades` 把 `fromId` 与 `startTime/endTime` 原样拼接发送。币安 PM UM Account Trade List 明确禁止 `fromId` 与 `startTime/endTime` 同时发送，故本地 Fake API 的通过结果不能证明真实成交分页可用——超过一页的成交窗口在真实接口下无法完成采集。

**位置**
- [export_execution_facts.py:439](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L439)（`api_call_factory` 每次都传 `start_time` / `end_time`）
- [binance_api.py:1660](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L1660)（`get_user_trades` 把 `fromId` 与 `startTime/endTime` 原样拼接）

**依据**
[币安官方 Portfolio Margin Trade API — UM Account Trade List 查询参数](https://developers.binance.com/en/docs/catalog/advanced-trading-derivatives-trading-portfolio-margin/api/rest-api/trade)：`fromId` 与 `startTime`/`endTime` 互斥，二者不可在同一请求中出现。复审报告第 1 节已核对 UM 段（非 CM 接口）并独立复现。

**影响**
- 超过一页的成交窗口无法完成采集（真实接口会拒绝非法参数组合或行为未定义）。
- 保存游标后的恢复路径仍可能遇到同一参数组合，阻塞完整成交与费用账本（R2/R3/R6 的账本完整性因此仍被 R7 牵制）。

**期望行为（修复口径）**
1. **时间定位与 ID 续传分离**：续传页（`fid` 非空）只发 `fromId`（+`symbol`+`limit`），**不再发送 `startTime`/`endTime`**；首屏定位页（`fid` 为空）才用 `start_time`/`end_time` 定位窗口。
2. **窗口右边界改由客户端过滤**：续传页不再携带 `endTime`，API 理论上可能返回越过 `window_end_ms` 的记录；客户端必须在消费阶段按 `time_ms > window_end_ms` 过滤，保证不越界采集、不污染游标。
3. **`get_user_trades` 增加参数互斥守卫**：当其同时收到 `from_id` 与 `start_time`/`end_time` 时，必须显式拦截（`raise ValueError` 或按分离规则忽略时间参数并告警），使任何未来误用都能在调用层被拦截，而非静默下发给交易所。
4. **验证 `fromId` 起点语义**：`fromId` 表示「从该 ID 之后返回」，续传不得重复、不得遗漏（同毫秒跨页必须能取回全部）。

**约束**
- 不改变 income 的续传机制（income 无 `fromId`，已走显式不完整分支，见 §3 末尾）。
- 不改变 fills 的幂等主键 `(account_id, symbol, trade_id)` 语义。
- 分页 Fake API 必须按 §1.4 要求校验参数互斥并 `raise`。

**先红后绿回归场景（对应复现脚本 `pagination_probe`）**
- 修复前（红）：合成同毫秒 3 条、`limit=2`，第二次请求参数同时含 `startTime` 与 `fromId` → Fake API 拒绝 → 导出 2 条后 `page_complete=False`；断言「存在同时含 `fromId` 与 `startTime` 的请求」为真，且 `page_complete == False`。
- 修复后（绿）：所有含 `fromId` 的请求均不含 `startTime`/`endTime`；同毫秒跨页取回全部记录且 `page_complete=True`；保存游标后恢复路径的续传请求同样只发 `fromId`。

---

### R8 [P1] 新币重取价后必须重算额度/数量/精度/最小下单条件（属 new_coin 组）

**问题描述**
`execute_short` 按旧 `current_price` 核算 `position_size` 并格式化 `quantity`；随后设置杠杆、报价超龄时重取价，但重取后仅把 `final_price` 换成新报价，仍沿用旧 `quantity` 下单。`entry_guard` 只重判「报价与反弹参照」，不重算仓位额度。当杠杆 2、可用保证金 50 时，价 100→120 使名义保证金由 50 变 60，可能越过原已校验额度；且「价格 100→120」仍可低于固定反弹参照，反弹闸门不能替代额度复核。

**位置**
- [executor.py:466-477](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L466-L477)（按旧 `current_price` 核算 position_size 并格式化数量）
- [executor.py:488-538](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L488-L538)（超龄重取价后仅替换 `final_price`，仍用旧 `quantity` 下单）

**依据**
执行顺序：核算额度 → 格式化数量 → 设杠杆 → （超龄）重取价 → 用新价与旧量下单。`entry_guard` 只在反弹/新鲜度上做复查，未覆盖「额度随最终价变化」。复审报告第 2 节已独立复现：旧价 100 时预算返回数量 1，重取价 120 后仍以数量 1、价 120 进入下单函数，预算函数仅调用一次。

**影响**
- 可能以超过已校验额度的名义保证金开仓（杠杆 2、可用保证金 50、价 100→120 → 名义保证金 60 > 50）。
- 精度与最小下单条件同样基于旧价，最终价变化后可能违反最小名义金额或精度约束。

**期望行为（修复口径）**
1. **最终有效报价改变后，重新核算可用额度、数量、精度与最小下单条件**：当 `entry_guard` 返回新报价且 `final_price != current_price` 时，必须以 `final_price` 重新调用额度核算（`_resolve_open_margin_budget(symbol, final_price)`），并重算 `position_size`、`quantity`（按 `step_size`）与最小下单门槛。
2. **下单前校验最终数量**：`_place_short_order` 收到的 `quantity` 必须基于 `final_price` 核算并已通过额度/最小金额校验；校验失败按既有「释放占用 + 返回拒绝」路径处理。
3. **不改变 D3 反弹参照口径**：反弹/新鲜度重判与额度重算是两个独立闸门，不能互相替代；「最终价未越过反弹参照」不抵消「额度超限」。

**约束**
- 不在本轮改动总分 / 技术门槛（9/7 组合行为不变）。
- 禁止把「重算额度」硬编码为固定比例/绝对值，阈值与最小金额一律读配置或由既有预算函数推导。
- 新币测试必须构造「价格跳变」报价（§1.4-2）。

**先红后绿回归场景（对应复现脚本 `price_probe`）**
- 修复前（红）：旧价 100 → 预算返回数量 1；重取价 120 后，断言下单 `quantity == 1`、`order_price == 120` 且 `budget_checks == 1`（只核算一次）。
- 修复后（绿）：
  - 上涨场景：`budget_checks >= 2`（最终价变化触发二次核算），断言「缩仓到额度内」或「拒开」，绝不以「旧数量 1 × 新价 120」下单。
  - 下跌场景：构造新价下跌，检查最小名义金额边界（低于最小下单门槛时按规则缩仓/拒开，不得下无效单）。

---

### R9 [P2] 影子信号记录不得被同币同向生产更新污染（属 HRS 组）

**问题描述**
生产信号与影子信号先一起 INSERT 进 `hrs.hrs_signals`；更新结果仍只按 `WHERE symbol=$1 AND direction=$2 AND status='triggered'` 宽范围匹配。生产桶先执行，成功更新 `executed` 时会把同币同向的影子记录一起改为 `executed`，后续影子更新已找不到 `triggered` 记录。影子评估证据被污染。

> 澄清（复审报告第 3 节明确）：**这不是影子真实下单，也不等于交易所成交事实被伪写**。影子路径从未真实成交；问题仅在于「一条结果更新错误地改到多条记录」，污染了信号结果统计与影子评估证据。

**位置**
- [strategy.py:1401](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L1401)（生产+影子信号一起 INSERT `hrs.hrs_signals`）
- [strategy.py:3768-3775](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3768-L3775)（`UPDATE ... WHERE symbol=$1 AND direction=$2 AND status='triggered'` 宽范围更新）

**依据**
`_log_signal_to_db` 的 INSERT 未返回唯一 ID；`_update_signal_resolution` 仅按 `symbol + direction + status` 定位，未区分信号来源（production / shadow）与逐条记录。复审报告第 3 节以内存 DB 模型按真实 SQL 谓词复现：同币同向 standard + emm 两条信号，最终得到 `executed, executed` 而非 `executed, shadow`。

**期望行为（修复口径，二选一，推荐方案 A）**
- **方案 A（推荐）**：INSERT 用 `RETURNING id` 返回唯一 `signal_id`，并将其贯穿信号字典（执行 / 冲突消解 / 超时 / 结果更新全链路）；`_update_signal_resolution` 改为按 `WHERE id=$1` 更新单条记录。
- **方案 B（等价替代）**：在 `UPDATE` 的 `WHERE` 增加隔离 `AND signal_origin != 'shadow_scoring' AND resolution != 'shadow'`，使生产更新不命中影子行。
- **二者共同必做**：保存 `entry_mode`、`signal_origin`（表已有 `id BIGSERIAL PRIMARY KEY`，但缺 `entry_mode`/`signal_origin` 两列）以便研究归属；INSERT 时写入这两列。

**约束**
- 表结构变更只能通过既有幂等 `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 在代码建表逻辑中声明，**不单独编写或执行生产库 migration SQL**（遵守 §1.3 不 apply 迁移）。
- 不改变 shadow 信号「不参与冲突消解、不真实下单、落库 `resolution='shadow'` 而非 `executed`」的既定口径（D2/D8）。
- signal sink Fake 必须按 §1.4-3 校验更新谓词，断言「一次 UPDATE 只改对应一行」。

**先红后绿回归场景（对应复现脚本 `signal_probe`）**
- 修复前（红）：同币同向 standard + emm 两条信号，断言最终 `resolution == ['executed', 'executed']`（影子被污染）。
- 修复后（绿）：断言最终 `resolution == ['executed', 'shadow']`（生产改为 executed、影子保持 shadow）；且按唯一 `signal_id` 更新时，一次更新仅改变对应行。

---

## 3. 逐条验收标准（可执行、可判定、可转回归测试）

> 复现脚本 [reproduce_findings.py](file:///Users/yl/vscode/Binance_quantitative_trading/docs/reviews/strategy_reaudit_2026-10-16_round2/reproduce_findings.py) 中的 3 个场景（signal_probe / price_probe / pagination_probe）必须成为回归用例，先红后绿。income_probe 对应「income 同毫秒显式不完整」已披露边界，不纳入本轮。

### R7 验收断言

1. **参数互斥不违例**：对 fills 的所有请求，任一含 `fromId` 的请求不得同时含 `startTime`/`endTime`；由分页 Fake API 强制校验（同时出现即 `raise`），修复后不触发任何 `raise`。
2. **同毫秒跨页取全**：同毫秒 3 条、`limit=2`，断言取回 3 条、`page_complete=True`（不得 2 条后中断）。
3. **窗口右边界客户端过滤**：构造 API 返回越过 `window_end_ms` 的记录，断言最终采集的每条 `time_ms <= window_end_ms`，且 `page_complete` 正确收尾。
4. **保存游标后恢复路径**：`save_cursor` 后从 `last_event_id` 恢复，续传请求同样只发 `fromId`（+symbol+limit），能继续取回后续记录。

### R8 验收断言

1. **最终价变化触发二次核算**：`entry_guard` 返回 `new.price != current_price` 时，断言额度核算被再次调用（调用次数 ≥ 2），且第二次传入 `final_price`。
2. **上涨缩仓/拒开**：旧价 100、可用保证金 50、杠杆 2，重取价 120（未越过反弹参照）→ 断言「position_size 按 120 缩到额度内」或「返回拒绝」，绝不以 `quantity=1, price=120` 下单。
3. **数量与精度按最终价重算**：下单 `quantity` 基于最终价与 `step_size` 重算并通过精度约束。
4. **下跌最小名义金额边界**：构造新价下跌场景，断言按最小名义金额/门槛正确判定（不足则缩仓或拒开）。

### R9 验收断言

1. **唯一 ID 贯穿**：INSERT 返回 `signal_id` 且贯穿执行 / 冲突 / 超时 / 结果更新；`_update_signal_resolution` 按 `WHERE id=$1` 更新（或方案 B 的隔离谓词），一次更新只改对应行。
2. **同币同向生产+影子并存**：standard（production）+ emm（shadow）同币同向，最终 `resolution == ['executed', 'shadow']`。
3. **同周期多条影子**：同一周期多条 shadow 记录，各自 resolution 独立、互不覆盖。
4. **历史未决信号共存**：存在历史 `status='triggered'` 的旧信号时，本轮结果更新不误伤历史未决行。
5. **归属字段已保存**：`entry_mode`、`signal_origin` 随 INSERT 写入（表新增两列），可据此归属统计。

---

## 4. mock / fake 收紧的落点（全部必须覆盖，缺一即判不达标）

| 收紧点 | 落入哪个 Fake/测试 | 对应需求 |
|---|---|---|
| 分页 Fake API 对 `fromId` × `startTime/endTime` 组合立即 `raise` | `pagination_probe` 的 `request`（或 `get_user_trades` 参数守卫） | R7 |
| 新币测试构造「价格跳变」报价（`entry_guard` 返回 `FreshQuote(120)` ≠ 旧 100） | `price_probe` | R8 |
| signal sink Fake 校验 `UPDATE` 谓词，判定一次更新改了几行 | `signal_probe` 的 `SignalDB.execute` | R9 |

收紧前提：所有回归用例继续「只注入 mock / 合成行情，不启动策略、不连 DB、不连交易所」，与 `reproduce_findings.py` 一脉相承。

---

## 5. 同源关系与分组

三处问题分属三个不同部件，文件零交叉，可独立开发/测试/审查：

| 分组 | 成员 | 涉及文件 | 说明 |
|---|---|---|---|
| A（execution_facts） | R7 | `scripts/export_execution_facts.py`、`shared/binance_api.py` | 成交分页契约，P1 |
| B（new_coin） | R8 | `strategies/new_coin/executor.py` | 执行前额度重算，P1 |
| C（HRS） | R9 | `strategies/hrs/strategy.py` | 信号结果定位，P2 |

建议执行顺序：R7（账本采集契约，P1）→ R8（资金额度安全，P1）→ R9（证据污染，P2）。每组回归用例先红后绿、G/V 门禁通过后再进入下一组。

---

## 6. 明确不纳入本轮范围

- **净收益对账 / 权益守恒 / 全策略归属账本**：仍属后续研究+验收，本轮不碰、不宣称。
- **MTPCS 趋势 / 回归分支定位、分支独立净收益**：研究/验收工作，本轮不碰。
- **income 同毫秒显式不完整分支**：为 R6 已披露的能力限制（income 接口未暴露 `tranId` 续传），复审明确「不另算新缺陷」，本轮不改、不补、不宣称收入流水已全部采齐。
- **push main / 部署 / GHCR / 服务器 / apply 迁移 / 连生产库 / 调真实交易所**：全程不做。
- 本轮所有修复**不得宣称「已部署 / 已上线」**，仅给出本地修复结论与回归证据。

---

## 7. 落地状态与验收结论（2026-10-16 收口）

> 本节为收口补记：R7–R9 已全部实现并通过**先红后绿**回归。范围边界（§1.2 / §6）不变——净收益对账、权益守恒、全策略归属账本、MTPCS 趋势/回归分支、部署验收等仍属后续研究与验收。**本节只声明「R7、R8、R9 三项已闭环」，不顺带宣称上述任何研究/验收项完成，也不主张「已部署 / 已上线」。**

### 7.1 逐条闭环对照（复审问题定义 → 根因 → 修复方案 → 验证证据 → 结论）

#### R7 [P1] 成交续页参数互斥（execution_facts 组）

- **复审问题定义**：复审报告 §1——`export_fills()` 续页同时发送 `fromId` 与 `startTime/endTime`，违反币安 PM UM `userTrades`「`fromId` 与 `startTime/endTime` 互斥」约束；本地 Fake 宽松放行致测试假绿。
- **根因**：`export_fills` 的 `api_call_factory` 每次调用都传 `start_time=cs, end_time=window_end_ms`，续页再叠加 `from_id`；`get_user_trades` 把三者原样拼接下发；测试侧 Fake 同时接受两种过滤，无法暴露非法组合。
- **修复方案**：
  1. `shared/binance_api.py::get_user_trades` 在 `params` 组装前新增互斥守卫——`from_id` 与 `start_time`/`end_time` 同现即 `raise ValueError`，命中即抛不进请求逻辑（[binance_api.py#L1657-L1663](file:///Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py#L1657-L1663)）。
  2. `export_fills` 的 `api_call_factory` 按 `fid` 分支——`fid is None` 走时间定位页（发 `startTime/endTime`），`fid` 非空走 ID 续传页**只发 `fromId`**（[export_execution_facts.py#L444-L449](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L444-L449)）。
  3. 续传页不携带 `endTime`，窗口右边界改由客户端过滤（`_filter_rows_within_window`，`time_ms <= window_end_ms`，[export_execution_facts.py#L927](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L927)）。
  4. 新增模块级 `_row_time_ms`（fills/income 复用，禁重复代码）与 `_resolve_page_outcome` 拆分（[export_execution_facts.py#L321](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L321)、[#L877](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L877)）。
  5. 测试侧 Fake 收紧为「同时传即 `raise`」，并按真实契约区分「仅 `fromId` 按 id 续传 / 仅时间按时间过滤」。
- **验证证据（先红后绿）**：
  - **红**：复审复现脚本 `pagination_probe`——同毫秒 3 条、`limit=2`，第二次请求同时含 `startTime` 与 `fromId` → `records=2`、`page_complete=false`、errors 提示契约违规（[reproduction_results.json](../../reviews/strategy_reaudit_2026-10-16_round2/reproduction_results.json)）。
  - **绿**：定向回归 `tests/test_shared/test_execution_facts_regression.py::TestR7PaginationContract` 共 5 用例——`test_no_request_sends_fromid_with_time_params`、`test_same_ms_three_records_limit_two_collects_all`、`test_window_right_boundary_filtered_client_side`、`test_save_cursor_then_resume_uses_fromid_only`、`test_invalid_param_combo_raises_in_production`（另含 `test_invalid_param_combo_raises_in_fake`）。
- **结论**：✅ **已闭环**。

#### R8 [P1] 新币重取价后重算额度/数量/精度/最小名义金额（new_coin 组）

- **复审问题定义**：复审报告 §2——`execute_short` 在 R5 超龄重取价后仅替换 `final_price`，仍沿用旧 `quantity` 下单（杠杆 2、保证金 50、价 100→120 时名义保证金变 60，可能越过已校验额度）；`entry_guard` 只重判反弹/新鲜度，不重算额度。
- **根因**：执行顺序为「按旧 `current_price` 核算额度 → 格式化数量 → 设杠杆 → 超龄重取价 → 用新价 + 旧量下单」，`quantity` 未随最终价重算；且开仓路径原本缺 `min_notional` 校验。
- **修复方案**：
  1. 新增统一入口 `_resolve_quantity_at_price(symbol, price, step_size)`——复用 `_resolve_open_margin_budget`（额度核算唯一实现）+ `_format_quantity`（精度唯一实现）+ `min_notional` 校验，返回 `(quantity, reject_reason)`（[executor.py#L729-L772](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L729-L772)）。
  2. `execute_short` 首次核算改走统一入口（记录 `budget_price`），R5 重取价后 `if final_price != budget_price` **二次重算** quantity/精度/min_notional，失败时释放 claim 拒开（[executor.py#L468-L475](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L468-L475)、[#L522-L540](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L522-L540)）。
  3. `min_notional` 首次引入开仓下单前路径（值来自 `trading.min_notional` 配置，非硬编码）。
- **验证证据（先红后绿）**：
  - **红**：复审复现脚本 `price_probe`——旧价 100 预算返回数量 1，重取价 120 后 `budget_checks=1`、以 `quantity=1, order_price=120` 进入下单（[reproduction_results.json](../../reviews/strategy_reaudit_2026-10-16_round2/reproduction_results.json)）。
  - **绿**：定向回归 `strategies/new_coin/tests/test_reprice_budget_r8.py` 全 4 用例——`test_reprice_rising_triggers_second_budget_check_no_old_qty`（上涨触发二次核算、绝不以旧量×新价下单）、`test_reprice_falls_triggers_min_notional_reject`（下跌低于最小名义金额拒开）、`test_fresh_quote_skips_reprice_budget_once`（报价未变不重复核算）、`test_first_check_quantity_truncates_to_zero_rejects`（精度截断为 0 拒开）。
- **结论**：✅ **已闭环**。

#### R9 [P2] 影子信号记录不被同币同向生产更新污染（HRS 组）

- **复审问题定义**：复审报告 §3——生产 + 影子信号先一起 INSERT `hrs.hrs_signals`，旧 `UPDATE ... WHERE symbol=$1 AND direction=$2 AND status='triggered'` 同币同向宽范围更新会把影子行一并标 `executed`。**注意**：这不是影子真实下单，也不等于交易所成交事实被伪写，问题仅在「一条结果更新误改多条记录」。
- **根因**：`_log_signal_to_db` 的 INSERT 未 `RETURNING id`，`signal` dict 不携带 `signal_id`；`_update_signal_resolution` 仅按 `symbol + direction + status` 定位，未区分记录来源（production / shadow）。
- **修复方案（方案 A）**：
  1. `_log_signal_to_db` 改 `INSERT ... RETURNING id`（改用 `db.fetch_one`），回填 `signal["signal_id"]`（[strategy.py#L3765-L3796](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3765-L3796)）。
  2. `_update_signal_resolution(signal_id, resolution)` 改按 `WHERE id=$1` 更新单条；`signal_id is None` 只 warning + return，**绝不回退 symbol+direction 宽范围 WHERE**（[strategy.py#L3801-L3818](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3801-L3818)）。
  3. `_finalize_and_execute_signals` 落库/超时过滤**顺序对调**为「先落库拿 `signal_id` → 再超时过滤」，使超时信号也能精准标记（[strategy.py#L1396-L1406](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L1396-L1406)）。
  4. 建表声明追加 `entry_mode`/`signal_origin` 两列，并在既有 ALTER 区追加两条幂等 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（[strategy.py#L3020-L3065](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3020-L3065)）。
  5. 11 处调用点（执行 1 + 冲突 8 + 影子 1 + 超时 1）全部改传 `signal["signal_id"]`。
- **验证证据（先红后绿）**：
  - **红**：复审复现脚本 `signal_probe`——同币同向 standard + emm 两条信号，最终得 `executed, executed`（影子被污染）。
  - **绿**：定向回归 `strategies/hrs/tests/test_r9_signal_attribution.py` 全 5 用例——`test_r9_production_and_shadow_get_distinct_resolution`（生产 `executed` + 影子 `shadow`）、`test_r9_multiple_shadows_each_get_own_resolution`（同周期多条影子互不覆盖）、`test_r9_update_only_touches_target_row_when_history_exists`（历史未决行不误伤）、`test_insert_failure_never_updates_db`（INSERT 失败 fail-safe 不更新）、`test_timeout_signal_persisted_then_marked_by_id`（超时信号先落库再按 id 标记）。
- **结论**：✅ **已闭环**。

### 7.2 验收结论汇总

| 需求 | 验收结论（验收断言见 §3） | 回归用例（文件 :: 用例） |
|---|---|---|
| R7 | ✅ 通过：任一含 `fromId` 的请求不再含 `startTime/endTime`；同毫秒跨页取全；窗口右边界客户端过滤；游标恢复续传只发 `fromId`；互斥守卫在调用层拦截 | `tests/test_shared/test_execution_facts_regression.py` :: `TestR7PaginationContract`（5 用例） |
| R8 | ✅ 通过：最终价变化触发二次额度核算；上涨缩仓/拒开（绝不旧量×新价）；数量/精度按最终价重算；下跌低于最小名义金额拒开 | `strategies/new_coin/tests/test_reprice_budget_r8.py`（4 用例） |
| R9 | ✅ 通过：唯一 `signal_id` 贯穿四条收尾分支；同币同向生产+影子并存 `executed, shadow`；多条影子独立；历史未决不误伤；`entry_mode`/`signal_origin` 随 INSERT 写入 | `strategies/hrs/tests/test_r9_signal_attribution.py`（5 用例） |

> 定向回归现场复核：`python3 -m pytest tests/test_shared/test_execution_facts_regression.py strategies/new_coin/tests/test_reprice_budget_r8.py strategies/hrs/tests/test_r9_signal_attribution.py -q` → **28 passed**。

### 7.3 能力限制披露（必须随修复一并披露）

- **RK1｜幂等 `ALTER TABLE` 迁移未 apply（本轮不连生产库、不执行迁移）**：`hrs.hrs_signals` 的 `entry_mode`/`signal_origin` 两列只在**代码建表逻辑**中声明，本轮**未部署、未 apply 迁移**。上线后需确认 hrs 容器启动日志出现「数据库 Schema 初始化完成」且两条 `ADD COLUMN IF NOT EXISTS` 未报错；否则新列缺失会使 `_log_signal_to_db` 的 INSERT（含两列）因缺列失败 → 既有 `except` 吞异常 + 返回 `None` → `signal_id` 缺失 → `_update_signal_resolution(None, …)` fail-safe 跳过更新 + warning。即**不崩溃、不回退宽范围污染**，但归属证据（`entry_mode`/`signal_origin` 及信号结果归属）会**静默丢失**。属「冷隔离、旧代码不启用新列」的安全退化路径。
- **RK2｜落库/超时过滤顺序对调改变下游统计口径**：原顺序「先超时过滤 → 后落库」使超时信号未落库；对调后**超时信号现在会被落库**（`status='triggered'` → 按 `signal_id` 精准标 `timeout`）。下游若按 `hrs.hrs_signals` 统计（信号总数 / 超时占比 / 归属），口径可能变化，**需下游同步**。
- **income 同毫秒不可续传（能力限制，非本轮引入）**：`um/income` 无 `fromId` 类事件 ID 游标，同毫秒多笔超过 `limit` 时无法翻页续取 → `page_complete=false`、`request_status='incomplete'`。此为 R6 已披露边界，复审明确「不另算新缺陷」，R7 未改变该机制；**不得据此宣称收入流水已全部采齐**。

### 7.4 旧回归失败清单（triage 结论，非本轮引入）

本轮对既有失败用例的 triage 结论如下，**均非 R7/R8/R9 引入**，不冒充本轮回归：

| 失败用例 | triage 结论 |
|---|---|
| `test_fix_verification` | `circuit_breaker` 测试夹具缺失（既有测试债），非本轮引入 |
| `test_market_data` / `test_position_claims` | 本机 Python 3.9 事件循环批量污染；**单独运行全绿**，非本轮引入 |
| `test_pattern` | 期望值过时（旧用例相对当前 config），非本轮引入 |
| `test_scoring_engine` | triage 为「真实断言失败但非本轮引入」——`scoring_engine` 有三轨拆分重构未同步期望，**需编码组确认**后修复 |
| `test_stop_loss_mark` | 测试夹具 `MagicMock` 被 `await`（需 AsyncMock），非本轮引入 |
| `test_binance_api::test_production_url` | PM 统一账户 papi 端点预期不符（旧用例相对当前端点），非本轮引入 |

### 7.5 落地事实与实现偏差（登记，不覆盖上文设计原文）

- **R7**：窗口右边界过滤以 `_filter_rows_within_window` + 模块级 `_row_time_ms` + `_resolve_page_outcome` 落地；`fromId` 起点采用保守口径「`fromId = 本页最大 trade_id`，**不 +1**」，由客户端 `seen`（`(account_id, symbol, trade_id)`）去重兜底，两种「含/不含自身」语义均不重不漏——真实语义**仍未确证**（本轮不连交易所）。
- **R8**：`_resolve_quantity_at_price` 落地为「核算 + 格式化 + `min_notional` 校验」唯一入口，首次核算与二次重算均调用它；`min_notional` 首次进入开仓路径下单前拦截（防币安 `-4164` 拒单），不改变总分/技术门槛（9/7 组合）。
- **R9**：按方案 A（`RETURNING id` + `WHERE id=$1`）落地，未启用方案 B；`hrs_signals` 建表为运行时 DDL（`strategy.py::_ensure_db_schema`），**不在 `database/` 下、不在 `docs/architecture/数据库设计.md` 的 §3 init-scripts 表清单内**（该文档按 init-scripts 口径维护，运行时 DDL 表未收录）——两列变更的权威记录即为本组文档（本需求 §2 R9 与 [架构 §2.3](architecture_R7-R9_2026-10-16.md)），无需另建 schema 文档。

**最后更新：** 2026-10-16（R7–R9 收口：状态改为「已实现 + 已通过回归」；新增 §7 逐条闭环对照 + RK1/RK2/income 能力限制披露 + 旧回归 triage；只声明 R7/R8/R9 三项闭环）