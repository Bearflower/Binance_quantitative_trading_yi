# 修复需求文档：new_coin 止盈检测跨档漏置位 + 移动止损保护范围再评估

## 1. 文档信息

| 项 | 内容 |
|----|------|
| 文档名称 | 修复需求文档：new_coin 止盈检测跨档漏置位 + 移动止损保护范围再评估 |
| 版本 | v1.0（初稿，待评审） |
| 作者 | requirements-document-expert |
| 创建日期 | 2026-10-15 |
| 触发事件 | 用户核对最近开仓 5 合约（MSTUUSDT/RDWUSDT/VKTXUSDT 属 new_coin；GRASSUSDT/QQQUSDT 属 hrs），发现交易所 UI 看不到移动止损保护标识；RDW 尾仓在 TP1/TP2 均已成交、持仓 7.21→5.05→2.17 且 `income` 对上的前提下，容器日志从未出现「第二目标已达成」 |
| 需求来源 | 生产事件（现象 + 根因已由"主对话 + 数据库 + 交易所"三层核对确认） |
| 风险级别 | R3（判定依据见 §4） |
| 适用范围 | 本文件 §6 的 AC-A ~ AC-E；AC-B/AC-C 为评估类（只在架构阶段出结论，本轮不改代码） |
| 下游环节 | backend-architect（架构设计，R3 加 A-review）→ python-engineer（编码实现） |
| 验收口径 | 每项以 §6「验收标准」为准，编号 `AC-Ax` ~ `AC-Ex`，可直接转为测试用例 |

> 说明：本文档只描述"应该是什么行为"与"如何验收"，不写实现方案（实现方案放架构文档）。重点中的「盘点逻辑伪代码」仅用于消除歧义。
>
> 本任务处于 A 段（需求与设计），**本轮不写实现代码、不部署、不 push**。

---

## 2. 任务背景

### 2.1 现象

用户近期开仓的 5 个合约（MSTUUSDT、RDWUSDT、VKTXUSDT 属 new_coin；GRASSUSDT、QQQUSDT 属 hrs）在交易所 UI 上均"看不到移动止损保护"，其中部分被用户手动止盈。用户据此怀疑移动止损保护缺失。

### 2.2 结论要点（三层核对，详见 §3）

1. **可辨识性问题（非单纯功能缺失）**：两套策略都不用币安原生 `TRAILING_STOP_MARKET`，而是轮询「撤旧挂新」普通 STOP 条件单，落库 `trading.condition_orders.order_type` 统一记为 `STOP_LOSS`。交易所 UI 没有独立"追踪"标识，用户因此误判为"无保护"。
2. **HRS 正常工作**：QQQ `target1_reached=t, target2_reached=t` 已激活移动止盈；GRASS 未到 TP2，故无移动止损（符合设计）。HRS 非本次问题对象。
3. **new_coin 保护"只在 TP2 之后生效"是设计现状**：`config.yaml` 的 `dynamic_trailing.activation.always_on_tp2=true`、`also_on_tp1=false`，且 `executor.py` 里 `_check_dynamic_trailing` / `_check_trailing_stop` 仅在 `tracking['target2_reached']` 为真时调用。
4. **存在一个真实资金风险 BUG（RDW 实锤）**：RDW 的 TP1、TP2 均已成交，但 `target2_reached` 未置位，导致按 `also_on_tp2` 应激活的移动止损未激活，剩余 2.17 尾仓无移动止损兜底。

---

## 3. 已核实事实陈述（含证据来源）

### 3.1 事实清单

| # | 事实 | 证据来源 |
|---|------|---------|
| F1 | 两套策略均非币安原生 `TRAILING_STOP_MARKET`，采用轮询「撤旧挂新」普通 STOP 条件单，`order_type` 统一记 `STOP_LOSS` | 代码路径（轮询撤挂）＋ `trading.condition_orders.order_type` 取值 |
| F2 | HRS 移动止盈仅在 TP2 成交后挂（role `tp2_trailing`，做空取「最低价 + 1.5×ATR」，每小时重建） | `strategies/hrs/`（TP2 后激活逻辑）；QQQ `target1_reached=t, target2_reached=t` |
| F3 | GRASS 未到 TP2 故无移动止损，符合 HRS 设计，非缺陷 | `short_positions` 中 GRASS `target2_reached=f` |
| F4 | new_coin 的 `dynamic_trailing.activation` 为 `min_profit_pct:1.5, also_on_tp1:false, also_on_tp2:true` | `strategies/new_coin/config.yaml:198-203` |
| F5 | 移动止损类检查仅在 `target2_reached` 为真时才调用 | `strategies/new_coin/executor.py:2053-2070`（`_check_take_profit_management` 内 `if tracking.get('target2_reached')` 门控） |
| F6 | MSTU/VKTX/RDW 的 `condition_orders` 全程只有开仓时的 1×STOP_LOSS + 2×TAKE_PROFIT，无任何重建记录 → 移动止损保护从未激活 | `trading.condition_orders` 记录核对 |
| F7 | **核心 BUG（RDW 实锤）**：RDW 持仓 7.21→5.05→2.17、`income` 能对上（TP1/TP2 均已成交），但容器日志只出现「第一目标已达成」，从未出现「第二目标已达成」→ `target2_reached` 从未置位 | 容器日志 + `trading.condition_orders` + 交易所持仓 |
| F8 | **根因**：`detect_take_profit_fills` 每轮只推进一级且立即 `return`；且先把 `_last_tracked_qty` 更新为当前值，再按 `if not target1_reached: 置位并 return 1`、`if not target2_reached: 置位并 return 2` | `strategies/new_coin/executor.py:3127-3171`（尤其 L3159 先更新数量、L3163-3166 与 L3167-3170 的分支） |
| F9 | **触发链**：TP1 与 TP2 两次成交落在同一轮询间隔（数量一次从 100% 掉到 30%）时，首轮判定为 target1 后 `return 1`、并把 `_last_tracked_qty` 更新为 2.17；下一轮数量无变化（`exchange_qty >= last_qty - tolerance` 直接 `return None`）→ `target2_reached` 永不置位 | `strategies/new_coin/executor.py:3150-3170` |
| F10 | **附带 BUG②（纳入 AC-D）**：`condition_orders` 状态与交易所不同步——TP 已成交、SL 已撤，但表中对应记录仍留 `OPEN`，清理只能靠「订单已不存在（可能已触发）」兜底 | `trading.condition_orders.status` vs 交易所订单状态 |
| F11 | **附带 BUG①（已修复，仅登记，不纳入本次范围）**：平仓盈亏回写 DB 报 `invalid input for query argument $3: datetime(...)`（时区编码），已由 commit `94ca3ed`（fix(new_coin): 修复平仓盈亏回写 executed_at 时区编码错误）修复 | `git show 94ca3ed` |
| F12 | **实时风险**：复核时点 `RDWUSDT -2.17@9.07` 仍在交易所持有（new_coin 30% 尾仓），当前无移动止损保护 | 交易所持仓实时核对 |

### 3.2 关键代码位置与日志文案

| 代码/文案 | 位置 | 说明 |
|-----------|------|------|
| `detect_take_profit_fills`（检测入口） | `executor.py:3127-3171` | 持仓数量对比，核心缺陷所在 |
| `exchange_qty >= last_qty - qty_tolerance` → `return None` | `executor.py:3155-3157` | 数量无变化直接返回，档位不再推进 |
| `self._last_tracked_qty[symbol] = exchange_qty`（先更新） | `executor.py:3159` | 交叉档位后 `_last_tracked_qty` 被提前写入，丢失"跨档"信息 |
| `if not target1_reached: ... return 1`；`if not target2_reached: ... return 2` | `executor.py:3163-3170` | 每轮只推进一级并立即返回 |
| `update_target_status`（置位入口） | `executor.py:3324-3365` | 日志文案「第一目标已达成」（L3344）、「第二目标已达成」（L3348） |
| 移动止损门控 `if tracking.get('target2_reached')` | `executor.py:2053-2070` | `_check_dynamic_trailing` / `_check_trailing_stop` 仅 TP2 后调用 |
| `position_detection` 配置 | `config.yaml:135-139` | `qty_tolerance_ratio=0.01`、`qty_tolerance_absolute=0.0001`、`zero_qty_threshold=0.0001` |
| `dynamic_trailing.activation` 配置 | `config.yaml:198-203` | `also_on_tp1=false`、`also_on_tp2=true` |

### 3.3 时区陷阱（约束）

- `trading.condition_orders` / `short_positions` 存 **UTC** 时间。
- `trade_records` 存 **北京时间**。
- 任何"时间线比对"（如 TP 成交时间 vs 日志发生时间）必须先做时区换算，否则结论会偏差 8 小时。
- 本需求要把"比对时间线需换算时区"作为验收/排查的显式约束写入（见 §5）。

---

## 4. 风险级别判定

**结论：R3（最高风险）。**

**依据**（对照 `model-development-workflow` 方案第 3 节 R3 定义）：

| R3 明示类别 | 本任务是否命中 | 说明 |
|-------------|--------------|------|
| 下单/撤单 | 是 | 移动止损靠「撤旧挂新」条件单；AC-A 决定是否挂保护单 |
| 持仓归属 | 是 | `target2_reached` 未置位 → 30% 尾仓"有持仓、无归属保护" |
| 风控 | 是 | 移动止损失效 = 尾仓裸奔，真实资金风险 |
| 资金与精度 | 是 | TP1/TP2 成交检测依赖数量精度与容差（`qty_tolerance_ratio/absolute`） |

附加因素：AC-E（RDW 尾仓）属"当前正在发生的真实资金敞口"，需要限期处置口径；AS-C 涉及是否引入原生下单语义变更。综上定为 R3，编码前需 A-review，编码后独立审查不省略。

---

## 5. 全局约束（强制）

1. **禁止硬编码**：所有阈值、比例、容差、档位跨越判定参数必须来自 `config.yaml` 或算法推导（如 `target1_close_percent`/`target2_close_percent`/`qty_tolerance_ratio` 等）。AC-A 的"跨档判定"所需的"每档平仓比例"不得写死在代码中。
2. **禁止服务器回测**：验证一律本地 mock/单测；服务器只做只读核对（日志、DB、交易所查询）。
3. **禁止重复代码**：`detect_take_profit_fills` 的跨档判定、`condition_orders` 状态同步等逻辑，若与其它策略/其它路径同构，必须抽公共模块，不得再复制。
4. **禁止幽灵参数**：未使用参数用 `_` 前缀或删除。
5. 单函数 ≤ 50 行；单行 ≤ 120 字符；注释、日志一律中文。
6. **时区约束**：任何跨表时间线比对必须先明确 UTC/北京时间并换算（见 §3.3）。
7. **本轮不写实现代码**：只产出需求/方案口径，不修改除本文件外的任何文件，不部署、不 push。

---

## 6. 功能需求与验收标准（AC-A ~ AC-E）

### AC-A 止盈成交检测：单次持仓下降跨越多档必须一次置位，不得漏检

**目标**：`detect_take_profit_fills` 在某个轮询间隔内"单次持仓下降"跨越多档（如 100% 一次掉到 30%，跨越 TP1 与 TP2 两档）时，必须**同时**置位 `target1_reached` 与 `target2_reached`，不能只推进一级后因"下一轮数量无变化"而漏置位。

**判级依据**：这是把"TP 已成交"这一客观事实映射为"该激活的保护是否激活"的核心状态机缺陷，属于风控 + 持仓归属。

**必须覆盖的边界**：

| 边界 | 说明 |
|------|------|
| 同时成交（跨多档） | 一个轮询间隔内数量从 100% 掉到 30%（或更少但跨越两档比例门槛），必须一次判定并置位所有已跨越档位 |
| 正常分步推进 | 100%→70%（仅 TP1）；下一轮 70%→30%（TP2），分两轮推进不回归 |
| 部分成交 / 精度截断 / 手续费容差 | 数量下降在容差内（≤ `qty_tolerance_ratio` 相对量 + `qty_tolerance_absolute` 绝对量）不得误判为"目标成交" |
| 多币种独立 | 每个 symbol 的 `_last_tracked_qty`、`target1_reached`、`target2_reached` 互不影响 |
| 全部平仓 | 数量 < `zero_qty_threshold` 仍应判定为"全部平仓"，交由上层清理，不得误判为某一档目标 |
| 交换所数量查询失败 | 查询失败（返回 None）不得推进任何状态 |

**验收标准**：

- **AC-A1（复现核心缺陷）**：构造单轮持仓从 100% 掉到 30%（跨越 TP1+TP2）的场景 → 断言日志**同时/先后**出现「第一目标已达成」与「第二目标已达成」，且 `target1_reached == True` 且 `target2_reached == True`，并触发移动止损激活路径（本地 mock 单测）。
- **AC-A2（正常分步不回归）**：100%→70% 单轮仅置位 `target1_reached`（`target2_reached` 仍为 False、不出现「第二目标已达成」）；下一轮 70%→30% 才置位 `target2_reached`。
- **AC-A3（容差不误判）**：数量下降幅度落在容差内（如仅手续费导致的 <0.01% 减少）→ 不置位任何目标、不写「第 N 目标已达成」日志。
- **AC-A4（跨档判定比例配置化）**：跨档判定所依赖的"每档平仓比例"必须从 `config.yaml`（`target1_close_percent` / `target2_close_percent` 或等价配置）读取，代码中不得出现硬编码比例值。
- **AC-A5（多币种独立）**：symbol A 检测到 TP 成交并置位，不影响 symbol B 的 `_last_tracked_qty` / 目标状态；两个 symbol 的检测结果互不干扰。
- **AC-A6（全部平仓语义保持）**：数量 < `zero_qty_threshold` 时仍返回"全部平仓"（0），由上层清理跟踪，不留脏状态。
- **AC-A7（幂等，不重复推进）**：`target1_reached`/`target2_reached` 均已置位后，后续数量变化不再重复置位/重复打日志（同量级变化不产生重复告警）。
- **AC-A8（查询失败不推进）**：`_get_exchange_position_qty` 返回 None（接口异常）时，不更新 `_last_tracked_qty`、不置位任何目标、不误报成交。

**可观察验收方法**：本地 mock 单测断言（unittest.mock 代币安 API 返回模拟持仓）；日志断言（`第一目标已达成` / `第二目标已达成` 出现与缺失）；追踪状态断言（`position_tracking[symbol]` 的 `target1_reached` / `target2_reached`）。修复部署后，用 RDW 同类场景在服务器**只读**核对 `trading.short_positions` 的 `target2_reached` 是否如实置位（不做回测、不下单）。

---

### AC-B 评估：移动止损是否应前移到 TP2 之前生效（评估类，本轮只出结论）

**目标**：评估"new_coin 移动止损保护从'TP2 之后才生效'前移到'TP2 之前（如浮盈达标或 TP1 后）即生效'的必要性与代价"，由架构阶段给出推荐结论与配置项设计。**本轮不改代码。**

**评估维度（必须覆盖，逐条给数据/论证）**：

1. **保护缺口期风险**：从开仓到 TP2 成交之间，尾仓/整体持仓无移动止损，仅靠固定百分比止损/紧急止损/时间止损兜底。量化该缺口期在历史仓位上可能损失多少利润或暴露多少回撤。
2. **误触发 / 来回打脸成本**：前移后，移动止损更早介入，可能在浮盈尚浅时因正常波动触发平仓，损失后续利润；需估算"过早止损"的期望代价（含做空方向的反弹打脸）。
3. **与 `_check_trailing_stop`（ATR 反弹移动止盈）的关系**：TP2 后二者"双保险"并存；前移后是否冗余或冲突，需给出口径。
4. **风险/收益结论**：综合 1~3，给出"是否前移"的推荐结论。

**验收标准**：

- **AC-B1（推荐结论）**：架构阶段必须给出明确结论——**前移 / 不前移 / 分阶段前移**，并附上述四维度的量化依据（不得只给"建议前移"一句话）。
- **AC-B2（配置项设计，若结论为前移）**：若推荐前移，必须给出激活条件与参数（如新的激活阈值、可用 `min_profit_pct`/`also_on_tp1` 的语义调整）的**配置项设计**，并遵守"禁止硬编码"。
- **AC-B3（替代缓解，若结论为不/暂不前移）**：若推荐不前移，必须给出"保护缺口期"的替代缓解口径（如缺口期内补弱保护单 / 告警升级 / 人工盯盘），并明示剩余风险接受声明。

**可观察验收方法**：以架构文档中"AC-B 评估结论"段落及其量化表/论据为验收产物；评审时核对四维度是否全覆盖、结论是否可追溯到数据。

---

### AC-C 交易所端可辨识性（评估类，本轮只出结论）

**目标**：评估是否让"移动止损保护"在交易所端可被识别，以消除用户"看不到保护即误判无保护"的问题。**本轮不改代码。**

**两条候选路径（评估可行性 + 风险）**：

1. **原生 `TRAILING_STOP_MARKET`**：改用币安原生追踪止损单。需评估：PM 账户是否支持、触发价语义差异、与"撤旧挂新"精度/数量管理的兼容性、原生单的可控粒度是否满足策略需求。
2. **`order_type` 区分 `TRAILING_STOP`**：保留现有"普通 STOP 条件单 + 轮询"机制，但把移动止损那张单落库 `order_type` 从 `STOP_LOSS` 区分为 `TRAILING_STOP`（或等价字段），使 DB/看板可辨；需评估对下游消费方（看板、审计、清理逻辑）的影响。

**必须覆盖**：`condition_orders`、下单 API、看板展示、历史数据迁移口径、跨策略一致性（是否波及 btc_eth/激进版——本轮**不改**它们，但需评估若只在 new_coin 生效会否造成口径不一致）。

**验收标准**：

- **AC-C1（可辨识性可行性）**：架构阶段对两条候选路径分别给出可行性结论（含 PM 账户/API 能力核对证据），并给推荐项与理由。
- **AC-C2（风险口径）**：对推荐项给出风险清单（下单语义变化风险、历史 `STOP_LOSS` 数据口径风险、看板/审计影响），以及是否会波及 btc_eth/激进版（本轮不纳入范围，必须明示）。
- **AC-C3（本轮边界）**：明确本轮**只产出评估结论**，不落地任何交易所原生单改造；若结论要求后续实施，须另立需求。

**可观察验收方法**：以架构文档"AC-C 交易所端可辨识性评估"段落为验收产物；核对是否覆盖两条路径 + 风险 + 波及范围 + 本轮不改码的边界声明。

---

### AC-D `condition_orders` 状态同步（附带 BUG②）

**目标**：`trading.condition_orders` 中条件单状态与交易所实际状态保持一致——TP 已成交、SL 已撤单后，对应记录应从 `OPEN` 及时关闭（`FILLED`/`CANCELLED` 或等价终态），不再依赖"订单已不存在（可能已触发）"这一兜底来被动收敛。

**范围界定**：本轮聚焦 new_coin；是否抽成通用同步逻辑（跨策略）由架构评估（见 §7）。

**验收标准**：

- **AC-D1（TP 成交即关闭）**：TP 条件单在交易所成交后，`condition_orders` 对应记录 `status` 由 `OPEN` 变为 `FILLED`（或等价终态），关闭动作发生在成交检测之后、可追溯到成交事件。
- **AC-D2（SL 撤单即关闭）**：SL/移动止损旧单在"撤旧挂新"被撤后，对应记录 `status` 变为 `CANCELLED`（或等价终态），不再长期残留 `OPEN`。
- **AC-D3（无兜底依赖）**：清理逻辑不再**仅**依赖"订单已不存在（可能已触发）"这一被动兜底；主路径应主动、幂等地同步状态。
- **AC-D4（可观察验收方法）**：本地 mock 单测断言"成交/撤单事件 → 对应记录状态变更"；修复部署后，在服务器**只读**执行
  `SELECT order_type, status, algo_id FROM trading.condition_orders WHERE symbol IN ('RDWUSDT','MSTUUSDT','VKTXUSDT')`
  并与交易所订单实际状态对账，断言"已成交的 TP 记录 ≠ OPEN、已撤单的 STOP 记录 ≠ OPEN"（时区比对按 §3.3 换算）。

**可观察验收方法**：见 AC-D4。

---

### AC-E RDW 尾仓限期处置（实时风险）

**目标**：复核时点 `RDWUSDT -2.17@9.07` 在交易所仍持有，当前无移动止损保护。必须限期处置，避免带病过夜。**需求阶段只列选项与验收口径，最终结论由方案阶段定。**

**处置选项（列出各自风险，不替用户拍板）**：

| 选项 | 做法 | 风险 |
|------|------|------|
| 立即补挂保护单 | 人工/脚本给 2.17 尾仓补一张 STOP（止损/追踪价可手工给定） | 手动价格易错；与策略后续自动接管可能冲突（重复挂单） |
| 立即平仓 | 人工市价平掉 2.17 尾仓 | 放弃后续可能利润；平仓盈亏回写依赖已修复的 BUG①，需确认该修复已生效 |
| 纳入修复后由策略自动接管 | 等 AC-A 修复上线后由策略重新识别并挂移动止损 | 修复前持续裸露一段时间，期间价格反向无兜底 |

**验收标准**：

- **AC-E1（限期结论）**：方案阶段必须给出**明确处置选择**（上述其一或组合）及**时限**（如"修复上线前"或"当日 X 点前"），不得以"评估中"无限期拖延。
- **AC-E2（监控/告警要求）**：在处置结论落地前，必须有对 RDW 尾仓的监控/告警（如价格反向超过阈值触发告警到飞书），确保裸露期间有人可介入。
- **AC-E3（可观察验收方法）**：方案文档给出处置结论与时限；监控告警项可见（飞书告警、日志告警）；处置落地后在交易所与 `short_positions`/`condition_orders` 核对 RDW 的最终状态（平仓清零或已有保护单）。

**可观察验收方法**：见 AC-E3。

---

## 7. 非目标（本轮明确不做）

1. **不改** btc_eth / btc_eth_aggressive 实现。
2. **不动** grid。
3. **不改** HRS（GRASS/QQQ 属 HRS 设计内行为，非本次问题对象）。
4. **不写实现代码、不部署、不 push**：本轮只产出需求口径（本文件）。
5. **不落地** AC-C 的交易所原生单改造（只评估）。
6. **不纳入** 已修复的 BUG①（时区编码，commit `94ca3ed`，仅登记）。
7. **不抽** `shared/`（是否抽公共同步/检测模块由架构评估后另定，见 §8）。

---

## 8. 未确认假设与待澄清项

1. **AC-B 前移结论的拍板权**：移动止损是否前移涉及交易偏好（保护优先 vs 利润优先），最终结论需交易负责人拍板，架构阶段只能给推荐。
2. **AC-A 跨档判定的档位比例来源确认**：跨档判定所需"每档平仓比例"是否已完全落于 `config`（`target1_close_percent`/`target2_close_percent`），需架构阶段核实现有配置键与语义，避免实现时新造硬编码。
3. **AC-D 是否抽 `shared/` 通用同步**：`condition_orders` 状态同步若抽通用模块会触发全量重建，本轮是否只为 new_coin 做最小修复，需架构 + 负责人拍板。
4. **AC-C 仅有 new_coin 生效是否制造口径不一致**：若只在 new_coin 区分 `TRAILING_STOP` 而 btc_eth/激进版仍记 `STOP_LOSS`，看板/审计口径是否可接受，需确认。
5. **AC-E 处置时限**：RDW 尾仓的处理时限与授权方式（人工补单需明确授权）需用户给出。
6. **历史存量**：除 RDW 外，MSTU/VKTX 是否仍有未平仓尾仓需要一并纳入 AC-E 处置范围（当前据 `condition_orders` 无重建、但需确认其交易所是否仍持仓），属待确认项。

---

## 9. 核实记录

| 文件/来源 | 关键位置 | 用途 |
|-----------|---------|------|
| `strategies/new_coin/executor.py` | `:2037-2043`（检测调用点）、`:2053-2070`（`target2_reached` 门控） | 确认移动止损仅在 TP2 后调用 |
| `strategies/new_coin/executor.py` | `:3127-3171`（`detect_take_profit_fills`）、`:3155-3157`（容差 return None）、`:3159`（先更新 `_last_tracked_qty`）、`:3163-3170`（单级推进 + return） | 核心 BUG 根因 |
| `strategies/new_coin/executor.py` | `:3324-3365`（`update_target_status`，日志「第一目标已达成」/「第二目标已达成」） | 日志文案佐证 `target2_reached` 未置位 |
| `strategies/new_coin/config.yaml` | `:135-139`（`position_detection`）、`:198-203`（`dynamic_trailing.activation`） | 配置现状与默认值 |
| `trading.condition_orders` / `short_positions` / `trade_records` | — | DB 三层核对（订单重建、target2 状态、时区口径） |
| 交易所持仓 | RDWUSDT -2.17@9.07 仍持有 | 实时风险（AC-E） |
| `git show 94ca3ed` | 时区编码修复 | BUG①（已修复，不纳入） |

---

（文档结束）