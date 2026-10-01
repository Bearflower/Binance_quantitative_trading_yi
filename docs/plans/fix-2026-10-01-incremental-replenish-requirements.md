# P0-D 需求文档：new_coin 持仓保护「增量补挂」（消除撤旧止损造成的 SL 空窗）

## 1. 文档信息

| 项 | 内容 |
|----|------|
| 文档名称 | P0-D 需求文档：new_coin 持仓保护增量补挂（只补缺失类型、绝不撤已存在的有效保护单） |
| 版本 | v1.0（初稿，待评审） |
| 作者 | requirements-document-expert |
| 创建日期 | 2026-10-01 |
| 需求来源 | 生产隐患（资金安全）：仅缺 TP 时补挂流程会先撤掉已有 SL，产生 SL 空窗 |
| 适用范围 | **仅 `new_coin`**（`strategies/new_coin/`）。其他策略（`btc_eth` / `btc_eth_aggressive` / `grid` / `hrs`）同类逻辑**不在本轮**（见 §7） |
| 上游依据 | [fix-2026-10-01-p0-remaining-unprotected-positions-requirements.md](./fix-2026-10-01-p0-remaining-unprotected-positions-requirements.md)（P0-A-AC3 定义了 `find_missing_protection` 语义） |
| 下游环节 | backend-architect（架构设计）→ python-engineer（编码实现） |
| 验收口径 | 编号 `P0-D-ACn`（见 §5），每条可直接转为测试用例 |

> 本文档只定义「应该是什么行为」，不含代码实现。文中"伪代码级状态流转"仅用于消除歧义。

---

## 2. 问题陈述与根因

### 2.1 现象

`new_coin` 策略的持仓保护守卫（`_guard_protection_orders`，默认每 300s 一轮）在核验真实空头敞口的保护单时，一旦发现"缺任一类型"（例如**只有止盈单被成交/被撤，止损单仍在**），就会触发 `replenish_conditional_orders`。而该函数的实现是**全量重建**：先把该标的**所有** algo 条件单撤掉（**包含仍然有效的止损单**），再重新挂回 SL+TP1+TP2。

后果：在"撤 SL"与"重挂 SL 成功"之间存在一个 **SL 保护空窗**——亚秒级（正常路径），但若重挂失败，空窗将**延续到下一个守卫周期**（默认 300s，最长可达 `guard_interval_seconds`）。在该窗口内，真实空头持仓**无止损保护**，价格不利波动时可能造成超出预期的亏损。

### 2.2 根因（含源码证据，已复核）

| 编号 | 根因 | 证据（已复核） |
|------|------|--------------|
| R-D1 | 补挂为**全量重建**语义：无论缺什么，都撤全部再挂全部 | [replenish_conditional_orders](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3353-L3386)：Phase A 只读准备 → **Phase B `_cancel_orders_strict`** → Phase C 挂 SL+TP1+TP2 |
| R-D2 | Phase B 撤的**是该标的全部条件单**（含仍有效的 SL） | [_cancel_orders_strict](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3547-L3562) → [cancel_all_algo_orders](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L2947)：清空 `position_tracking[symbol]['algo_ids']` 并逐条取消（本地 algoId + DB 回退） |
| R-D3 | Phase C 无条件重挂 SL/TP1/TP2，且不以"缺什么"为输入 | [_execute_replenish_plans](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3571-L3602)：固定 `sl` + `tp1` + `tp2` 三连挂 |
| R-D4 | **缺失信息其实已经算出来，只是没被使用** | [find_missing_protection](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L991-L1035) 返回 `["止损单"]` / `["止盈单"]` 等；但策略侧 [_guard_symbol](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/strategy.py#L1162-L1180) 只把 `missing` 用作"是否触发"的开关，**未传入** `replenish_conditional_orders` |

### 2.3 复核补充证据（本轮新增，用于消除歧义）

1. **`find_missing_protection` 是「类型级」而非「档位级」**：它按 `condition_orders.order_type` 的 `DISTINCT` 值判定，只有 `STOP_LOSS` 与 `TAKE_PROFIT` 两种，`TP1`/`TP2` 在表中**同记 `TAKE_PROFIT`**（见 [_CONDITION_ORDER_META](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L63-L67)：`tp1`/`tp2` 的 `record_type` 均为 `'TAKE_PROFIT'`）。
   → 因此"存在任意一条 OPEN 的 TAKE_PROFIT"即视为"止盈单不缺"。**若 TP1 丢失但 TP2 仍在，`find_missing_protection` 会返回 `[]`（判为无缺口），不会补挂 TP1。** 这是"只补缺失类型"落地时必须先拍板的粒度问题（见 §3.2、§4.1）。
2. **`find_missing_protection` 的 fail-closed 语义用于"是否触发"是安全的，但直接用于"挂什么"是不安全的**：DB 查询异常时它返回"全部应存在类型"（[L1022-L1028](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L1022-L1028)）。在增量语义下若把它当成"全部缺失"去补挂，可能对**实际已存在**的 SL 再挂一条，产生**重复止损单**。故增量语义必须区分"确认缺失"与"无法判定"（见 §4.3、P0-D-AC7）。
3. **`_execute_replenish_plans` 建 tracking 是幂等的**：仅当 `symbol not in position_tracking` 才建（[L3581-L3586](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3581-L3586)），已有 `algo_ids` 会被保留。这条对"保留既有单的 algo_id"有利。
4. **`_replenished_symbols` 在守卫路径上形同虚设**：`_guard_symbol` 在调用补挂前先 `reset_replenish_flag(symbol)` 清除标记（[L1174-L1176](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/strategy.py#L1174-L1176)），故"已完成标记"只对其他调用方（如启动补全）起跳过作用。置位语义需重新定义（见 §3.4）。
5. **回退分支与主分支顺序相反**：`replenish_cancel_after_ready=False` 时是"best-effort 撤单 → 只读准备 → 挂单"（[L3372-L3377](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3372-L3377)），即**真的先撤**——与"先算后撤"相反（该分支本就是应急回退，见 §3.5）。

---

## 3. 目标语义

### 3.1 一句话目标

**增量补挂：核验真实敞口缺失的保护单类型后，只补挂缺失的类型，绝不撤销或变更任何一条仍然有效（OPEN）的保护单。**

- 撤单动作在增量语义下**不再是默认步骤**（Phase B 被取消或最小化，见 §3.6）。
- "有效保护单"指 `condition_orders` 表中该标的 `status='OPEN'` 的 `STOP_LOSS` / `TAKE_PROFIT` 记录所对应的交易所订单。

### 3.2 场景判定矩阵（核心）

输入：`missing = find_missing_protection(symbol)`（类型级）；`quantity = _resolve_short_quantity(symbol)`；Phase A 只读准备状态。

| # | 场景 | `missing` | 持仓 | 期望动作 | 撤单 | 返回值 | 置位 |
|---|------|-----------|------|---------|------|--------|------|
| S1 | 仅缺 SL | `["止损单"]` | >0 | 只补 1 条 SL；TP1/TP2 原样保留 | **否** | 成功 True | 成功才置位 |
| S2 | 仅缺 TP | `["止盈单"]` | >0 | 只补缺失的 TP 档；**SL 原样保留** | **否** | 成功 True | 成功才置位 |
| S3 | 全缺（无任何条件单） | `["止损单","止盈单"]` | >0 | 补 SL + TP1 + TP2（此时无单可撤） | 否（无单可撤） | 成功 True | 成功才置位 |
| S4 | 全在 | `[]` | >0 | 守卫直接返回，**不调用补挂** | 否 | 视为 True | 不涉及 |
| S5 | 无空头持仓 | 不适用 | =0 | 不撤不挂，直接返回 | 否 | True | 不涉及 |
| S6 | 取数失败（DB 查询异常，fail-closed 返回全类型） | 全类型（**不代表真缺**） | 不确定 | **不得凭 fail-closed 结果补挂**；须先做权威复核，未确认前只告警、下周期重试 | 否 | False | 不置位 |
| S7 | Phase A 只读准备失败（ATR=0 / 精度失败 / 现价失败 / ensure-active 失败） | 不适用 | >0 | 不触达交易所（零撤单零挂单）；告警 | 否 | False | 不置位 |
| S8 | 幂等错误码（挂单返回 `-2021`/`-4164`/`-4136`/`-4507` 等） | 视缺失类型 | >0 | 视为"订单已存在"，该类记成功；不重复挂 | 否 | 按其余类型结果 | 全成功才置位 |
| S9 | 部分成功（SL 成功、TP 失败） | 全类型 | >0 | 保留已挂成功的，下周期只补仍缺的 | 否 | False | 不置位 |
| S10 | 入场价无效（`entry_price<=0`） | 不适用 | >0 | 守卫跳过补挂并告警（现状保留） | 否 | 不调用补挂 | 不涉及 |

> 说明：S4/S5/S10 描述的是**守卫侧**（`_guard_symbol`）的既有行为，本轮**不得回退**；S1/S2/S3/S6/S8/S9 是增量语义要新定义/收紧的行为。

### 3.3 关键设计问题（给出约束与倾向，供架构阶段决策）

> 以下每条均给出**约束（必须满足）**与**倾向（推荐但不强制）**，不留空。

**D-Q1｜是否保留「Phase A → Phase B 撤单 → Phase C 挂单」的顺序？**
- 约束：增量语义下，**对"已存在的有效类型"不得触发任何撤单**。因此"撤全部再挂全部"的 Phase B 语义必须被取消或最小化。
- 倾向：保留 **Phase A（只读准备，先算后撤的守护价值仍在）**；**取消**全量 Phase B；**仅**在"确认某既有单已失效/需替换"时才做**按单撤销**（本轮不引入替换语义，见 D-Q6）。即顺序简化为：A（准备）→ C（增量挂缺失项）。`_cancel_orders_strict` / `_cancel_orders_best_effort` 在增量路径上不再被默认调用。

**D-Q2｜`condition_orders` 表与 `position_tracking[symbol]['algo_ids']` 如何保持一致？既有 SL 保留时其 algo_id 是否需要回填？**
- 约束：增量补挂**不得清空或覆盖** `position_tracking[symbol]['algo_ids']` 中已存在的键值（[`_execute_replenish_plans`](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3581-L3586) 现有"已存在则不重建"的行为必须保持，且不得在增量路径上执行 `algo_ids = {}`）。
- 倾向：**以 `condition_orders`（DB）作为"是否已存在"的权威判据**（`find_missing_protection` 即查此表）；`position_tracking.algo_ids` 作为进程内缓存尽力维护：若某类型判定为"已存在"但其 `algo_ids` 缺失，则**尝试回填**（从 DB 的 `OPEN` 记录取 algo_id）；回填失败**不阻断**补挂，也不重复挂该类型。理由：`cancel_all_algo_orders` 已有"本地无 algoId 则回退查 DB"的兜底（[L2975-L3007](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L2975-L3007)），故 algo_id 未回填不会造成"取消失败"的资金风险；回填是"锦上添花"，不应成为补挂的前置阻塞条件。

**D-Q3｜TP 的 `market_close` 分支在增量语义下如何处理？**
- 约束：`market_close`（现价已过目标价，改为市价平仓该部分，[_apply_take_profit](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3604-L3623)/[_plan_take_profit](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3474-L3476)）**只影响 TP 自己**，**绝不触发对 SL 的撤销**。
- 倾向：保持现有语义不变——增量路径下若判定 TP 应 `market_close`，则执行部分市价平仓；**只要该部分平仓后仍有剩余空头敞口，就必须确保 SL 存在**（SL 属"已存在类型"则不动；属"缺失类型"则一并补挂）。即 `market_close` 不得成为"跳过硬止损保护"的理由。

**D-Q4｜`_replenished_symbols` 的置位语义（部分成功是否置位、如何定义"完成"）？**
- 约束：**只有本轮所有"缺失类型"都确认到位（挂成功或幂等命中）才置位**；任一缺失类型未到位则**不置位**，下周期重试。`S6`（无法判定）**一律不置位**。
- 倾向：将"完成"定义为"`find_missing_protection` 再次核验返回 `[]`"（即缺口闭合），而非"函数返回 True"。同时明确：守卫路径每轮补挂前会 `reset_replenish_flag`，故 `_replenished_symbols` 仅用于**同一轮内**的防重与**非守卫调用方**的跳过；跨周期自愈依赖"缺口是否闭合"的再核验。

**D-Q5｜`replenish_cancel_after_ready` 回退开关是否保留？**
- 约束：该开关的语义是"是否启用先算后撤"。增量语义下**没有"撤"这一步骤**，该开关的原始含义失效。
- 倾向：**保留开关名与默认值 `true`（向后兼容配置），但重新定义语义**为"启用增量补挂（只补不撤）"；当置为 `false` 时回退到**当前全量重建（含撤单）**的旧行为，作为应急回退（保留旧路径代码或明确迁移方案，见 §6 风险）。不允许出现"开关关闭导致增量语义被静默绕过且无测试覆盖"。

**D-Q6｜是否需要"替换/重定价"语义（既存 SL 价格已过期时）？**
- 约束：本轮**不引入**按单替换语义；增量语义只解决"有无"，不解决"价格是否正确"。
- 倾向：若既存 SL 存在但价格明显失真，**不在本轮处理**（列入 §7 范围外）。理由：引入替换会重新打开"撤旧挂新"的空窗风险，与本轮目标直接冲突。

### 3.4 挂单失败 / 部分失败的重试与告警

- 沿用现状：失败不置位、下周期（`guard_interval_seconds`）重试；告警按 `(kind, symbol)` 降频窗口 `alert_throttle_seconds`（[`_notify_protection_issue`](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/strategy.py#L1182-L1201) 与 [`_protection_gap_attempts`](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/strategy.py#L1178-L1180)）。
- 增量语义下告警内容需更精确：应包含"缺失类型列表"（复用 `missing`），而非笼统"补全失败"。
- 重试必须是**增量重试**：下一轮只补"仍缺的类型"，不得因上一轮部分失败而全量重建。

### 3.5 伪代码级状态流转（消除歧义用，非实现要求）

```
_guard_symbol(symbol):
    missing = find_missing_protection(symbol)        # 类型级
    if missing 为空: 返回                              # S4
    if entry_price <= 0: 告警; 返回                     # S10
    reset_replenish_flag(symbol)
    ok = replenish_conditional_orders(symbol, entry_price, missing=missing)   # 新增：把 missing 传入
    if not ok: 累计 attempts; 告警(missing, attempts)

replenish_conditional_orders(symbol, entry_price, missing):   # 增量语义
    if _should_skip_replenish(symbol): 返回 True
    status, plans = _prepare_replenish_plans(...)     # Phase A：只读，含 S7 判定
    if status == NO_POSITION: 返回 True               # S5
    if status != READY: 返回 False                    # S7（零撤单零挂单）
    # 关键：不再调用 _cancel_orders_strict（取消 Phase B）
    return _execute_incremental_plans(symbol, plans, missing)   # Phase C：只补 missing 覆盖的类型
```

侧栏（与既有差异）：
- 与现状相比，**删除**了 `_cancel_orders_strict` 这一步（默认路径）。
- `missing` 由策略侧传入（现状是函数内部不感知缺失类型）。
- `_execute_incremental_plans` 需要把 `missing`（类型级）映射为"要挂哪几条"（档位级，见 D-Q 待定项）。

---

## 4. 需要先拍板的关键取舍（架构阶段）

### 4.1 TP 补挂的粒度（**最关键的未决项**）

`find_missing_protection` 只能判"有无 TAKE_PROFIT"，**无法区分 TP1/TP2**。因此存在两种定义：

- **方案甲（保守，粒度类型级）**：`missing` 含"止盈单"时，视为**TP 全缺**，补 TP1+TP2；若 `missing` 不含（即存在任一条 TP），则不补任何 TP。
  - 优点：改动最小，与现有 `find_missing_protection` 语义完全对齐。
  - 缺点：**"只缺 TP1、TP2 仍在"的场景无法被发现和补挂**（遗留缺口）。
- **方案乙（精细，粒度档位级）**：升级判据为按 `position_tracking.algo_ids` 的 `tp1`/`tp2`（或 DB 细分）判定，能识别"仅缺某一档"，只补该档。
  - 优点：真正"只补缺失档"。
  - 缺点：需扩展 `find_missing_protection` 或新增判定；且与 `algo_ids` 缓存一致性耦合（D-Q2）。

**倾向**：**本轮采用方案甲**（类型级），把方案乙列入后续迭代（§7）。理由：本轮核心目标是**消除 SL 空窗的资金风险**，方案甲已能消除"仅缺 TP 时撤 SL"这一根因；方案乙引入的档位级判定会扩大改动面与回归风险。**但必须在文档与测试中显式声明方案甲的已知局限**（仅缺一档 TP 不会被补挂）。

### 4.2 取数失败的"不可判定"与 fail-closed 的边界（见 §5 P0-D-AC7 / §3.2 S6）

`find_missing_protection` 的 fail-closed（异常返回全类型）语义在"是否触发守卫"上是安全的，但在"补挂内容"上不安全。**必须**在补挂前增加一次"权威复核"或直接规定"判定不确定则不补挂，仅告警"。倾向后者（简单、安全、可测）。

### 4.3 与其他策略的一致性

本轮仅改 `new_coin`。若架构阶段发现"增量补挂"逻辑适合下沉到 `shared/`，需评估：放进 `shared/` 会触发**全部策略容器重建**（见部署规则），且可能影响其他策略的既有行为。**倾向**：逻辑先留在 `strategies/new_coin/` 内，收敛部署影响；抽象为公共模块作为独立后续任务。

---

## 5. 验收标准（P0-D-AC）

> 编号规则：`P0-D-ACn`。每条均可直接转为测试用例，均为"可验证"。测试基线复用 `strategies/new_coin/tests/test_p0_replenish_and_registration.py` 与 `tests/test_strategies/test_new_coin_reconcile_guard_p0a*.py`。

| 编号 | 验收标准 | 验证方式（正例/反例/边界） |
|------|---------|--------------------------|
| P0-D-AC1 | **增量核心不变式**：当存在有效 SL 时，补挂流程（默认路径）**零调用撤单**（`cancel_all_algo_orders` 调用次数 = 0），且已存在的 SL 订单一律不被取消或变更 | mock：`missing=["止盈单"]` 且 SL 已存在 → 断言撤单 0 次、SL 未被触碰（正例）；对照旧行为应失败 |
| P0-D-AC2 | **仅缺 SL**：`missing=["止损单"]` → 只挂 1 条 SL，不挂 TP，TP 既有单不变 | mock 已存在 TP → 断言只挂 SL、撤单 0 次（正例） |
| P0-D-AC3 | **仅缺 TP**：`missing=["止盈单"]` → 只补 TP（方案甲=TP1+TP2），**SL 原样保留、零撤单** | mock 已存在 SL → 断言撤单 0 次、SL 未被取消（正例，核心止血场景） |
| P0-D-AC4 | **全缺**：`missing=["止损单","止盈单"]` 且无任何条件单 → 挂 SL+TP1+TP2，零撤单（无单可撤） | 断言挂单 3 条、撤单 0 次（正例） |
| P0-D-AC5 | **全在**：`missing=[]` → 守卫**不调用**补挂函数 | 断言 `replenish_conditional_orders` 未被调用（边缘正例） |
| P0-D-AC6 | **无空头持仓**：`quantity<=0` → 不撤不挂、返回 True | 断言零撤单零挂单（边界） |
| P0-D-AC7 | **取数失败不误补**：`find_missing_protection` 底层查询异常（fail-closed 返回全类型）时，补挂**不得据此直接挂单**；须判定不确定 → 仅告警、返回 False、不置位，下周期重试 | mock DB 抛异常 → 断言零挂单零撤单、返回 False、未置位（反例，防重复止损单） |
| P0-D-AC8 | **Phase A 失败零触达**：ATR=0 / 精度失败 / 现价失败 / ensure-active 失败 → 零撤单零挂单、返回 False、不置位、告警 | 参数化四类失败（反例） |
| P0-D-AC9 | **幂等错误码视成功**：挂 SL/TP 命中 `replenish_ignore_error_codes` → 该类记为成功、不重复挂；其余类型成功则置位 | mock `-2021` → 断言返回 True 且不重复挂（边界） |
| P0-D-AC10 | **部分成功不置位**：SL 成功、TP 失败 → 返回 False、不置位，保留已成功项 | mock side_effect=[True,False,True] → 断言 False、未置位（反例） |
| P0-D-AC11 | **增量重试**：上一轮部分失败后，下一轮只补仍缺的类型（不触发全量重建、不撤已成功的单） | 连跑两轮：断言第二轮挂单只覆盖仍缺类型、撤单 0 次（正例） |
| P0-D-AC12 | **缺口闭合即自愈**：某轮补挂成功后，下一轮 `find_missing=[]` → 不重复挂、不撤单；若随后某类型丢失则应重新补该类型 | 连跑三轮（成功→无缺口→再丢 TP）断言行为（正例+边界） |
| P0-D-AC13 | **algo_ids 一致性**：保留既有单时，`position_tracking[symbol]['algo_ids']` 中已有的 `sl`/`tp1`/`tp2` 条目**不被清空或覆盖**；补挂成功的新条目被正确写入；`condition_orders` 记录与之一致 | 预置 `algo_ids={'sl':111}` → 补 TP → 断言 `sl` 仍为 111、新增 tp 项、未落空（正例） |
| P0-D-AC14 | **market_close 分支不撤 SL**：TP 计划 `market_close=True` → 执行部分市价平仓（见既有语义），**零撤 SL**；若平仓后仍有剩余敞口，SL 必须存在 | mock `market_close` → 断言零撤单、SL 保持（边界） |
| P0-D-AC15 | **并发/在途去重**：同一 symbol 并发/在途不重复补挂（沿用 `_guard_inflight` + `_should_skip_replenish`） | 并发两次守卫 → 断言该 symbol 只处理一次（边界） |
| P0-D-AC16 | **托管清单跳过**：`replenish_skip_symbols` 命中 → 跳过，不触碰交易所 | mock 命中 → 断言零调用（正例） |
| P0-D-AC17 | **告警降频与内容**：缺口告警沿用 `(kind, symbol)` × `alert_throttle_seconds` 降频；消息含 symbol、缺失类型列表、累计重试次数 | 连续触发 → 断言窗口内仅 1 次且文案含类型列表（正例） |
| P0-D-AC18 | **无硬编码 / 配置化**：新增或改动的开关、阈值、间隔、粒度选项**全部来自 `config.yaml`**，且缺省有默认值；代码中无写死数值 | 代码审查 + 单测：缺省配置不崩溃、配置生效 |
| P0-D-AC19 | **既有测试显式迁移**：与增量语义冲突的旧用例（如断言"先撤后挂"顺序的 `test_p0_3_ac2`、断言"撤单失败阻断挂单"的 `test_p0_3_ac3`）必须**显式更新**并在提交说明中标注；其余既有用例（P0-1/P0-3/P0-A/P0-C）**不得回归失败** | 运行全部既有测试，断言全绿；对被改用例记录差异 |
| P0-D-AC20 | **不误判 300s 空窗**：核心场景（P0-D-AC3）在**正常路径**下不存在"SL 从撤到挂"的时间窗（因根本不撤），日志中不得出现该 symbol 的"撤销旧条件单"记录 | 日志断言：P0-D-AC3 场景无撤单日志（正例） |

> 边界条件补充（必须覆盖）：空列表/`None`（`missing` 为空/未知）、数量变化（TP 部分成交后 `remaining_quantity` 减小）、`entry_price<=0`、精度 `tick/step<=0`、超时/网络断开（下单 API 抛异常）、幂等错误码集合为空（配置 `None` 兜底）。

---

## 6. 影响面与风险回滚

### 6.1 影响面

| 变更点 | 影响文件 | 风险 |
|--------|---------|------|
| 增量补挂语义 | [executor.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py)（`replenish_conditional_orders`、`_execute_replenish_plans` 及其调用者） | 触及资金安全主流程，必须走强制测试（幻觉测试 + 功能测试 + 覆盖率） |
| 守卫传参 | [strategy.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/strategy.py)（`_guard_symbol`） | 需保证 `missing` 传递不破坏既有告警/重试语义 |
| 开关语义 | [config.yaml](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/config.yaml)（`trading.replenish`） | 开关语义变更需同步文档与运维认知 |
| 部署 | 仅 `trading-new-coin` 镜像重建 | 不改 `shared/`，不触发其他策略容器重建 |

### 6.2 风险

1. **重复止损单风险**：若"是否已存在"判定出错（如 S6 误判），增量补挂可能对已有 SL 再挂一条 → 两条 SL。缓解：P0-D-AC7（不确定则不补）+ 以 DB `OPEN` 为权威判据 + 幂等错误码兜底。
2. **粒度遗留缺口**：方案甲下"仅缺一档 TP"不会被补挂（§4.1）。缓解：显式声明局限 + 列入后续迭代。
3. **既有用例语义冲突**：旧测试断言"先撤后挂"，需显式迁移（P0-D-AC19），否则 CI 红或误改。
4. **回退开关二义性**：`replenish_cancel_after_ready` 语义若不清，可能被误设为 `false` 而回退到全量撤单旧行为（重新引入空窗）。缓解：D-Q5 明确语义 + 测试覆盖。

### 6.3 回滚考量

- 代码回滚：`git revert` 对应提交 → push main → Actions 自动重建 `trading-new-coin`（见部署规则）。
- 运行时回退：`replenish_cancel_after_ready=false` 回退到**全量重建旧行为**（作为应急开关保留，但需明确其会重新引入 SL 空窗，仅供短期止血）。
- 部署验证：按部署规则"防部署幻觉五层验证"，确认 `trading_system-new_coin` 容器内本次构建的 VERSION 与 Actions Run 一致；`docker ps` 确认所有服务在运行（防 PostgreSQL 命名冲突导致后续服务未启动的已知坑）。

---

## 7. 不在本次范围

1. 其他策略（`btc_eth` / `btc_eth_aggressive` / `grid` / `hrs`）的同类"全量重建/撤单"逻辑——**本轮仅改 `new_coin`**（用户已确定）。
2. TP 档位级（TP1/TP2）精细判定与补挂（方案乙，§4.1）——作为后续迭代。
3. 既存保护单的**价格替换/重定价**（D-Q6）——本轮只解决"有无"，不解决"价格是否正确"。
4. `shared/` 层公共模块下沉（§4.3）。
5. 前端/看板展示变更。
6. 上一轮 `c494571` / `c6082a5` 与 P0-A/P0-C 已验收逻辑（本轮不重做，仅在必要时对齐）。
7. 条件单表历史数据清洗。

---

## 8. 非功能需求与项目强制规范（写进实现）

- **禁硬编码**：所有阈值/间隔/开关/粒度选项必须来自 `config.yaml`（`trading.replenish` 段），不得写死。
- **禁重复代码**：连续 ≥5 行同构逻辑视为违规；SL/TP 挂单路径应复用 [_place_conditional_and_record](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3639-L3673)。
- **禁幽灵参数**：定义即使用，或 `_` 前缀标记。
- **单函数 ≤ 50 行、单行 ≤ 120 字符**，同文件风格一致。
- **注释与日志一律中文**（含新增告警文案）。
- **fail-closed**：判定不确定一律不补挂、不静默"当作已保护"；异常收敛不得导致裸奔。
- **回测只能在本地执行**（本次不涉及回测）。
- **不写业务代码**：本文档只定义行为，实现由架构/编码环节完成。

---

## 9. 术语表（统一命名，避免歧义）

| 术语 | 含义 |
|------|------|
| 真实敞口 | 交易所实际空头 ∩ DB `new_coin.short_positions` 中 `status='open'`（P0-A-AC1 口径） |
| 保护单 | `condition_orders` 中 `status='OPEN'` 的 `STOP_LOSS`（SL）/ `TAKE_PROFIT`（TP） |
| 缺失类型 | `find_missing_protection` 返回的中文类型列表（`["止损单"]` / `["止盈单"]` / 两者） |
| 增量补挂 | 只补缺失类型、绝不撤/改已存在有效保护单 |
| SL 空窗 | 从撤销 SL 到重挂 SL 成功之间，真实敞口无止损保护的时段 |
| 全量重建 | 现状语义：撤该标的全部条件单后重挂 SL+TP1+TP2 |
| 托管清单 | `replenish_skip_symbols`，由 MCTPS 管理保护单、本策略不补挂的交易对 |
| 置位 | 把 symbol 加入 `_replenished_symbols`（表示本轮补挂完成） |

---

**最后更新：** 2026-10-01（v1.0 初稿）
