# 架构设计定稿：策略修复第二轮 R7–R9（2026-10-16）

> 状态：**设计定稿 + 已落地（2026-10-16 收口）**。本文定义架构与契约；各组落地实现、偏差与能力限制见 §8「落地状态（2026-10-16 收口）」，不写生产实现代码、不改任何源文件、不部署、不连库。
> 权威输入（与本设计逐条对齐）：
> - 需求与验收：[requirements_R7-R9_2026-10-16.md](requirements_R7-R9_2026-10-16.md)（含三项验收判据 + mock/fake 收紧前置要求）
> - 复审报告：`../../reviews/strategy_reaudit_2026-10-16_round2/README.md`
> - 复现脚本：`../../reviews/strategy_reaudit_2026-10-16_round2/reproduce_findings.py`（signal_probe / price_probe / pagination_probe / income_probe）
> - 上一轮设计范式：[architecture_R1-R6_2026-10-16.md](architecture_R1-R6_2026-10-16.md)（R5 的 FreshQuote / entry_guard 与本轮 R8 直接衔接）
> - 业务决策：[decisions.md](decisions.md)（D2/D3/D4/D8）

> 本文所有行号为**当前工作区实际行号**（含未提交改动），编码前须用 `grep` 复核再落刀。

---

## 1. 总体设计概览

### 1.1 三处缺陷与一条共同根因

| 需求 | 优先级 | 分组/文件 | 缺陷一句话 |
|---|---|---|---|
| R7 | P1 | execution_facts：`scripts/export_execution_facts.py` + `shared/binance_api.py` | 成交续页同时发 `fromId` 与 `startTime/endTime`，违反 PM UM `userTrades` 互斥约束 |
| R8 | P1 | new_coin：`strategies/new_coin/executor.py` | `entry_guard` 重取价后只换 `final_price`，未按新价重算额度/数量/最小名义金额 |
| R9 | P2 | HRS：`strategies/hrs/strategy.py` | `_update_signal_resolution` 按 `symbol+direction+status='triggered'` 宽范围更新，污染同币同向 shadow 行 |

**共同根因（需求 §1.4）**：三处缺陷均属「模拟环境不够严格导致测试假绿」——分页 Fake 同时接受两种过滤、新币测试未构造价格跳变、signal sink Fake 只按宽谓词更新。因此**同步收紧 mock/fake 是三条需求的共同前置要求**，每条回归都要「先红后绿」并被自动捕获。

### 1.2 分组与文件交叉（零交叉，可并行编码）

| 分组 | 成员 | 唯一涉及文件 | 优先级 |
|---|---|---|---|
| A（execution_facts） | R7 | `scripts/export_execution_facts.py`、`shared/binance_api.py` | P1，先做 |
| B（new_coin） | R8 | `strategies/new_coin/executor.py` | P1 |
| C（HRS） | R9 | `strategies/hrs/strategy.py` | P2 |

三组文件零交叉，可并行开发、独立红绿。**顺序建议 A（账本采集契约）→ B（资金额度安全）→ C（证据污染）**，与需求 §5 一致。

### 1.3 四条贯穿性原则

1. **契约优先于实现**：R7 把「时间定位页 / ID 续传页」两条参数路径显式化，并让 `get_user_trades` 在非法组合一出现就 raise，而不是静默下发交易所。
2. **额度重算与反弹闸门正交**（承 R5/D3）：`entry_guard` 的「反弹/新鲜度重判」与「额度随最终价重算」是两个独立闸门，后者用「最终有效报价」只读重核，不重复 claim、不改变反弹参照口径。
3. **信号结果定位唯一化**（承 R1/D2/D8）：`hrs_signals` 以 `id BIGSERIAL PRIMARY KEY`（建表已存在）为唯一定位键，INSERT 返回 `id` 并贯穿执行/冲突/超时/影子四条收尾分支，杜绝宽范围 UPDATE。
4. **一轮只改本轮相关文件、不 apply 迁移、不部署**：schema 变更只在代码建表逻辑（幂等 `ADD COLUMN IF NOT EXISTS`）声明，本轮不执行、不部署、不连生产库、不调真实交易所。

---

## 2. 逐项设计

## 2.1 R7 [P1] 成交分页参数互斥：时间定位页与 ID 续传页分离

### 2.1.1 现状（根因）

- `export_fills` 的 `api_call_factory`（`scripts/export_execution_facts.py:439-442`）每次调用都传 `start_time=cs, end_time=window_end_ms, from_id=fid`；`get_user_trades`（`shared/binance_api.py:1657-1667`）把三者原样拼接进 `params`。
- 第一页 `fid=None` 没问题；第二页起 `fid` 非空，`fromId` 与 `startTime/endTime` 同时下发 → 违反币安 PM UM `userTrades`「`fromId` 与 `startTime/endTime` 互斥」约束。
- `_one_page_iteration`（`export_execution_facts.py:836-837`）以 `api_call_factory(loop_advance.time_ms, loop_advance.event_id)` 调用，两参语义是 `(开始时间, from_id)`，未表达「本页是否续传」。

### 2.1.2 目标契约

1. **两条参数路径分离**（`api_call_factory(cs, fid)` 语义约定）：
   - `fid is None` → **时间定位页**：发 `startTime=cs + endTime=window_end_ms + limit`（首采 / 断点恢复且无 `last_event_id`）。
   - `fid is not None` → **ID 续传页**：只发 `symbol + limit + fromId`，**不再发 `startTime/endTime`**。
   - 这个 `fid None/非 None` 即「本页是时间定位还是 ID 续传」的显式表达，无需新增布尔 mode 参数。
2. **窗口右边界改由客户端过滤**：ID 续传页不带 `endTime`，API 可能返回 `time_ms > window_end_ms` 的记录；消费前在客户端按 `time_ms <= window_end_ms` 过滤后再入 `collected`/推进 `PageAdvance`。
3. **`get_user_trades` 参数互斥守卫**：`from_id` 与 `start_time`/`end_time` 同时出现时显式 `raise ValueError`（契约保护，任意未来误用在调用层拦截）。
4. **`fromId` 起点语义**：采用「`fromId = 本页最大 trade_id`，不 +1」，由客户端 `seen` 去重兜底兼容「含自身 / 不含自身」两种语义（详见 2.1.6 口径说明）。

### 2.1.3 具体改造点（文件/函数级）

**A. `export_fills` 的 `api_call_factory`（`export_execution_facts.py:439-442`）**

改为按 `fid` 分支：

```python
# 伪代码（仅示意分支，不写生产实现）
def _fills_api_call(loop_start_ms: int, from_id: Optional[int]):
    if from_id is None:
        # 时间定位页：首采或断点恢复无 last_event_id
        return api.get_user_trades(
            symbol, start_time=loop_start_ms,
            end_time=window_end_ms, limit=self.page_limit,
        )
    # ID 续传页：只发 fromId，窗口右边界交由客户端过滤
    return api.get_user_trades(
        symbol, limit=self.page_limit, from_id=from_id,
    )
```

以 `self.api_call_factory=_fills_api_call` 形态传入（保持 `Callable[[int, Optional[int]], Any]` 签名不变，`_run_export_loop` 等下游无需改签名）。

**B. `_one_page_iteration` 增加窗口右边界客户端过滤（`export_execution_facts.py:835-842`）**

- 请求成功后、`_consume_page` 之前，新增一步过滤（ID 续传页不携带 `endTime`，可能返回越界记录）：

```python
# 伪代码
rows = await self._try_fetch_page(...)
if isinstance(rows, CursorRecord):
    return ...  # 错误游标，原样返回
# 窗口右边界过滤：仅保留 time_ms <= window_end_ms 的行
rows = [r for r in rows if self._row_time_ms(r) is None
        or self._row_time_ms(r) <= window_end_ms]
page_complete = len(rows) < self.page_limit   # 基于过滤后的行数判定
new_items, new_advance = self._consume_page(rows, ...)
```

- 新增回调 `row_time_ms: Callable[[Dict[str, Any]], Optional[int]]`，fills 与 income 均传 `lambda r: _safe_int(r.get("time"))`（两个接口原始行均有 `time` 字段；income 时间定位页本不越界，统一走同一过滤逻辑，行为不变、无特判、不重复代码）。
- `row_time_ms` 随 `api_call_factory` 同链路下传：`export_fills`/`export_income` → `_run_export_loop` → `_prepare_cursor_and_fetch` → `_run_paging_until_done` → `_one_page_iteration`。

**C. `get_user_trades` 互斥守卫（`shared/binance_api.py:1657-1667` 之前）**

```python
# 伪代码
if from_id is not None and (start_time is not None or end_time is not None):
    raise ValueError(
        "from_id 与 start_time/end_time 互斥，不能同时传入"
        "（PM UM userTrades 契约约束）"
    )
```

- 守卫置于 `params` 组装之前，命中即抛，不进任何请求逻辑。

**D. 分页 Fake API 收紧（回归测试侧，非导出器本体）**

- `reproduce_findings.py` 的 `request(...)` 已对「`fromId` 与 `startTime/endTime` 同现」`raise`（本轮已满足）。
- **生产侧测试的 Fake 必须同等待遇**，并实现正确的从 `fromId` 续传语义：
  - 仅含 `fromId` → 按 `id > from_id`（见 2.1.6 语义）返回后续记录，**不再接受时间过滤语义**；
  - 含 `startTime/endTime`（且无 `fromId`）→ 按 `start_ms <= time <= end_ms` 过滤返回；
  - 二者同现 → 立即 `raise ValueError`（不得静默接受）。
  - 现有 Fake 用 `id > from_id` 且同时接受时间过滤，**不能替代真实契约，必须修正**。

### 2.1.4 事务 / 错误处理

- 互斥守卫 `raise ValueError` 由 `_try_fetch_page` 的 `except Exception` 捕获 → 转 error 游标（`request_status="error"`、`error_detail` 截断）→ `_resolve_iteration_dispatch` 追加 errors 并 break，**不丢已取数据**、游标不推进。这是既有错误路径，无需新增。
- 窗口右边界过滤不改变 `_detect_unstoppable` 三连检测与 `_build_unstoppable_result`；过滤若使本页行变空 → `page_complete = len([]) < page_limit = True`（窗口内已无数据），正确收尾。
- `seen` 去重集合继续保证 `(account_id, symbol, trade_id)` 幂等，不因 `fromId` 语义差异重复入账。

### 2.1.5 回归场景（对应 `pagination_probe` + 验收断言）

| 验收断言（需求 §3 R7） | 回归做法 |
|---|---|
| 1. 任一含 `fromId` 请求不同时含 `startTime/endTime` | 捕获所有请求参数列表，断言「含 `fromId` 的请求均无 `startTime`/`endTime`」；非法组合会触发 Fake `raise`，修复后不触发 |
| 2. 同毫秒跨页取全 | 合成同毫秒 3 条、`limit=2` → 断言取回 3 条、`page_complete=True` |
| 3. 窗口右边界客户端过滤 | 假 API 返回一条 `time > window_end_ms` 记录 → 断言每条采集 `time_ms <= window_end_ms` 且 `page_complete` 正确 |
| 4. 保存游标后恢复路径 | `save_cursor` 后从 `last_event_id` 恢复 → 断言续传请求只发 `fromId`(+symbol+limit)，可继续取回后续 |

**先红后绿**：修复前 `pagination_probe` 断言「存在同时含 `fromId` 与 `startTime` 的请求」为真、`page_complete=False`；修复后反转。

### 2.1.6 `fromId` 起点语义口径（确定口径 + 待验证标注）

- **确定口径（保守，且不依赖官方语义）**：`fromId = 本页最大 trade_id`，**不做 +1**。客户端 `seen`（`(account_id, symbol, trade_id)`）保证：
  - 若官方语义为「`id > fromId`」（不含自身）→ 恰好不重复；
  - 若官方语义为「`id >= fromId`」（含自身）→ 下一页重复返回的末条被 `seen` 去重，且因每条都在推进 `max event_id` 不会造成 `id_stuck` 误判。
- **待验证**：币安 PM UM `userTrades` 的 `fromId` 究竟「含 / 不含自身」在各文档措辞间存在历史歧义，本设计**不依赖该语义**（去重兜底），故列为待验证项；本轮不连真实交易所，唯一确证方式需官方文档逐字或真实接口验证。
- **结论**：本设计的正确性不建立在 `fromId` 含/不含之上，两种语义均「不重不漏」。

---

## 2.2 R8 [P1] 新币重取价后必须重算额度/数量/精度/最小名义金额

### 2.2.1 现状（根因）

- `execute_short` 按旧 `current_price` 核算 `position_size`（`executor.py:466`）并格式化 `quantity`（`:477`）。
- `entry_guard` 重取价成功（`:506-513`）后仅 `quote = new_quote`、`final_price = float(quote.price)`（`:514`），仍沿用旧 `quantity` 下单（`:535-538`）。
- `_resolve_open_margin_budget`（`:658`）只做「占用统计 → 限额读取 → 缩仓 → 门槛 → 限额校验」，**不校验最小名义金额 `min_notional`**（`min_notional` 目前只用于 TP 补全/减仓路径，开仓路径缺失）。
- 杠杆 2、可用保证金 50、旧价 100→新价 120：旧量 1 对应的名义保证金由 50 变 60，越过已校验额度；`entry_guard` 的反弹闸门不能替代额度复核。

### 2.2.2 目标契约

1. **最终有效报价改变后重算**：`entry_guard` 返回新报价且 `final_price != 首核算价` 时，以 `final_price` 重算可用额度 → 数量 → 精度 → 最小下单条件。
2. **下单前最终校验**：`_place_short_order` 收到的 `quantity` 必须基于 `final_price` 核算并通过额度/最小名义金额校验；失败走既有「释放 claim + 拒开」路径。
3. **额度重算与反弹闸门正交**：反弹/新鲜度重判与额度重算互不替代，「最终价未越过反弹参照」不抵消「额度超限」。
4. **禁重复代码 / 口径不漂移**：缩仓/限额/门槛逻辑**唯一实现**，不得在二次重算处复制一份同构逻辑。

### 2.2.3 具体改造点（复用优先）

**A. 结论：直接复用 `_resolve_open_margin_budget`，无重复 claim、无副作用**

`_resolve_open_margin_budget` 内部链路为：`_read_available_budget`（`calc_current_occupied_margin()` 只读统计 + `get_effective_margin_limit()` 读限额）→ `_shrink_position_and_margin`（纯计算）→ `_reject_if_below_min_margin`（纯判定）→ `_enforce_limit_check`（`can_open_within_limit` 校验，非 claim）。

- **claim 已独立于额度核算**：占位发生在 `execute_short` 的 `_claim_symbol`（`:459`），额度核算**不写占用、不重复 claim**。因此重算时再次调用 `_resolve_open_margin_budget(symbol, final_price)` 只多一次只读统计，无副作用、不产生重复 claim。
- **口径不漂移**：重算复用同一函数，天然与首次核算同口径（同一缩仓公式、同一限额入口），满足「禁止重复代码」。

**B. 新增「按价核算数量 + 最小名义金额」统一入口 `_resolve_quantity_at_price`**

把「核算 → 格式化数量 → 最小名义金额校验」收敛为一个函数，首次核算与重算**均调用它**，消除 L466-477 与新增重算点的重复，并把缺失的开仓 `min_notional` 校验补进统一入口：

```python
# 伪代码（函数名/签名/职责示意，非生产实现）
async def _resolve_quantity_at_price(
    self,
    symbol: str,
    price: float,
    step_size: Decimal,
) -> Tuple[Optional[Decimal], str]:
    """按最终价核算仓位数量 + 最小名义金额校验（开仓下单前唯一入口）。

    返回 (quantity, reject_reason)；quantity 为 None 表示拒开。
    内部复用 _resolve_open_margin_budget（额度核算唯一实现）+
    _format_quantity（精度唯一实现）+ min_notional 校验。
    """
    position_size, reject = await self._resolve_open_margin_budget(symbol, price)
    if position_size is None:
        return None, reject
    quantity = self._format_quantity(position_size, step_size)
    if quantity <= 0:
        return None, "精度截断后数量为 0，无法开仓"
    notional = quantity * Decimal(str(price))
    if notional < self.min_notional:
        return None, (
            f"名义金额 {float(notional):.6f} 低于最小下单金额 "
            f"{float(self.min_notional):.6f}，拒绝开仓"
        )
    return quantity, ""
```

- `min_notional` 读 `self.min_notional`（`executor.py:303`，来自 `trading.min_notional` 配置，非硬编码）。
- 该函数 ≤ 20 行，若实现超行数可把 `min_notional` 校验拆为 `_reject_below_min_notional(quantity, price) -> Optional[str]`（≤ 12 行）。

**C. `execute_short` 执行顺序微调与重算插入点**

当前顺序：L466 核算 → L474 取精度 → L477 格式化 → L480 设杠杆 → R5 重校 → L535 下单。

调整目标（最小侵入）：
1. **精度获取前移**：把 L474 `_get_symbol_precision` 提到 L466 核算之前（`step_size` 是 symbol 属性，不随价格变，重复获取计算量极小，但为保持单一入口，先取一次复用）。
2. **首次核算改走统一入口**：L466-477 两处计算合并为一次 `quantity, reject = await self._resolve_quantity_at_price(symbol, current_price, step_size)`；`reject` 非空 → `_release_claim_quietly + return None, reject`（替代原 L469-471）。
3. **重算插入点（R5 重取成功之后、下单之前，原 L513-522 区间）**：

```python
# 伪代码：R5 重校得出 final_price 后
budget_price = current_price            # 首次核算价（保留变量）
# ... R5 重校得出 final_price ...
if final_price <= 0:
    await self._release_claim_quietly(symbol, "open_failed")
    return None, "最终开仓价无效"

# R8：最终有效报价与首核算价不一致 → 二次核算
if final_price != budget_price:
    logger.warning(
        "重取价改变，按最终价重算额度与数量",
        symbol=symbol, budget_price=budget_price, final_price=final_price,
    )
    quantity, reject = await self._resolve_quantity_at_price(
        symbol, final_price, step_size
    )
    if reject:
        await self._release_claim_quietly(symbol, "open_failed")
        return None, reject
```

- 下行 `_place_short_order(symbol, quantity, Decimal(str(final_price)), ...)` 收到的 `quantity` 必然基于 `final_price`。
- 二次核算若缩仓后仍满足 `min_notional`/最小保证金 → 缩仓；否则 `_resolve_quantity_at_price` 返回 reject → 释放 claim + 中文日志 + `return None`（拒绝，绝不以下单 `旧量 1 × 新价 120`）。

### 2.2.4 事务 / 错误处理

- 二次核算失败（`None`，含超限 / 缩仓后低于最小保证金 / 低于最小名义金额）→ `_release_claim_quietly(symbol, "open_failed")` + 返回中文原因；与首次核算失败路径完全对称。
- `_release_claim_quietly` 幂等，重复释放无害（占位 `claimed` 标志在 `execute_short` 顶部控制，拒绝路径复用既有释放逻辑）。
- 不需新增 claim / 限额写入，二次核算纯只读，无事务副作用。

### 2.2.5 回归场景（对应 `price_probe` + 验收断言）

| 验收断言（需求 §3 R8） | 回归做法 |
|---|---|
| 1. 最终价变化触发二次核算 | mock `_resolve_open_margin_budget` 计数；`entry_guard` 返回 `FreshQuote(120) != 100` → 断言调用次数 ≥ 2，第二次传 `final_price=120` |
| 2. 上涨缩仓/拒开 | 旧价 100、可用保证金 50、杠杆 2、重取价 120（未越反弹参照）→ 断言不以 `quantity=1, price=120` 下单；结果要么缩仓到 `position_value ≤ 100`（数量 ≈ 100/120 = 0.833 → 格式化）要么拒开 |
| 3. 数量与精度按最终价重算 | 断言 `_place_short_order` 收到的 `quantity` 基于最终价与 `step_size` 重算（`_format_quantity` 再次被调用且入参为新 `position_size`） |
| 4. 下跌最小名义金额边界 | 构造新价下跌使 `quantity * final_price < min_notional` → 断言拒开（不产生 `_place_short_order` 调用），不主动 mock 交易所下单 |

**先红后绿**：修复前 `price_probe` 断言 `budget_checks == 1`、`(quantity, order_price) == (1, 120)`；修复后断言 `budget_checks >= 2` 且非「旧量 1 × 新价 120」。

### 2.2.6 与既有 `min_notional` 的关系（口径说明）

- 本轮把 `min_notional` 校验**首次引入开仓路径下单前**（原仅 TP 补全/减仓路径使用），语义为「最终下单名义金额 `quantity × final_price` 不得低于最小下单金额」。这是对 `_place_short_order` 前置的本地拦截，避免币安 `-4164` 拒单；改变的是「更早更安全地拒开」，不改总量/技术门槛（保持 9/7 组合行为）。
- 若未来 TP 补全等路径希望复用同一金额校验，可在 `shared/` 下沉 `min_notional` 比较为公共纯函数；本轮范围仅 executor 开仓路径，不做跨文件重构。

---

## 2.3 R9 [P2] 影子 signal 归属隔离：按唯一 signal_id 精准更新

### 2.3.1 现状（根因）

- `hrs.hrs_signals` 建表（`strategies/hrs/strategy.py:3006-3019`）已有 `id BIGSERIAL PRIMARY KEY`，但**缺 `entry_mode`/`signal_origin` 两列**。
- `_log_signal_to_db`（`:3688`）INSERT 未 `RETURNING id`，`signal` dict 不携带 `signal_id`。
- `_update_signal_resolution`（`:3751`）`UPDATE ... WHERE symbol=$1 AND direction=$2 AND status='triggered'` 宽范围更新，production 先执行会把同币同向 shadow 一并改 `executed`。
- 四条调用点：执行（`:1961`）、冲突（`:1920-1950`）、影子（`:1884`）、超时（`:3849`）。

### 2.3.2 目标契约（方案 A，主推）

1. `_log_signal_to_db` 用 `INSERT ... RETURNING id` 取回唯一 `signal_id`，回填到 `signal["signal_id"]`。
2. `_update_signal_resolution` 改为按 `WHERE id=$1` 更新单条；四个分支均携带对应 `signal["signal_id"]`。
3. INSERT 同时写入 `entry_mode`、`signal_origin`（表新增两列），供研究归属。
4. 分桶时 production/shadow 的信号对象为**同一 dict 引用**（见 2.3.3-D），`_log_signal_to_db` 回填 `signal_id` 后分桶引用均可见，无需另建映射。

### 2.3.3 具体改造点（文件/函数级）

**A. 建表 schema：新增 `entry_mode`/`signal_origin` 两列（`strategy.py::_ensure_db_schema`）**

落点确认：`hrs_signals` 建表位于 `strategies/hrs/strategy.py::_ensure_db_schema`（`:2882`，运行时 DDL），**不在 `database/` 下**（`database/` 无 `hrs_signals` 独立 SQL，已核实）。改动：
- 建表语句（`:3006-3019`）列清单追加 `entry_mode TEXT`、`signal_origin TEXT`（对全新安装生效）。
- 在既有 `ALTER TABLE` 区（`:3033-3039` 之后）追加两条幂等添加：

```sql
-- 伪代码（DDL 示意）
ALTER TABLE hrs.hrs_signals ADD COLUMN IF NOT EXISTS entry_mode TEXT;
ALTER TABLE hrs.hrs_signals ADD COLUMN IF NOT EXISTS signal_origin TEXT;
```

- **本轮不 apply、不部署、不连生产库**：只改代码建表逻辑，作为「未部署」标注入档。

**B. `_log_signal_to_db` 改造（`strategy.py:3688`）**

- 签名扩展返回值：`async def _log_signal_to_db(self, symbol, direction, signal, resolution=None) -> Optional[int]`。
- INSERT 追加两列 + `RETURNING id`，改用 `self.db.fetch_one(...)` 执行（`db.execute` 不返回结果集，无法取 `RETURNING` 值；`fetch_one` 内部 `fetchrow` 支持 `INSERT ... RETURNING`），取 `row["id"]` 回填 `signal["signal_id"]`：

```python
# 伪代码（结构化示意）
entry_mode = signal.get("entry_mode")
signal_origin = signal.get("signal_origin")
row = await self.db.fetch_one(
    """
    INSERT INTO hrs.hrs_signals
        (symbol, direction, total_score, technical_score, sentiment_score,
         contract_score, current_price, status, resolution,
         bb_position, rsi_value, entry_mode, signal_origin)
    VALUES ($1,$2,$3,$4,$5,$6,$7,'triggered',$8,$9,$10,$11,$12)
    RETURNING id
    """,
    symbol, direction, round(total_score,2), round(technical_score,2),
    round(sentiment_score,2), round(contract_score,2), round(current_price,6),
    resolution, bb_position, rsi_value, entry_mode, signal_origin,
)
if row and row.get("id") is not None:
    signal["signal_id"] = int(row["id"])
    return signal["signal_id"]
return None
```

- 失败仍是既有 `except` 吞异常 + warning，返回 `None`（`signal_id` 缺失由下游 fail-safe 兜底，见 D）。
- 保留 `_validate_sql` 对单条 `INSERT ... RETURNING` 的放行性待确认（见 §4 待确认事项）。

**C. `_update_signal_resolution` 改签名（`strategy.py:3751`）**

```python
# 伪代码
async def _update_signal_resolution(self, signal_id: int, resolution: str) -> None:
    """按唯一 signal_id 更新单条结果（杜绝宽范围 WHERE）。"""
    if signal_id is None:
        logger.warning(
            "信号缺少 signal_id，跳过结果更新（避免误伤其他信号）",
            resolution=resolution,
        )
        return
    try:
        await self.db.execute(
            """
            UPDATE hrs.hrs_signals
            SET status='resolved', resolution=$2, resolved_at=NOW()
            WHERE id=$1
            """,
            signal_id, resolution,
        )
    except Exception as e:
        logger.warning("信号结果更新失败", signal_id=signal_id, error=str(e))
```

- **fail-safe 关键**：`signal_id is None` 时**直接跳过更新并 warning**，绝不回退到 symbol+direction 宽范围 WHERE（否则重新引入 R9 缺陷）。

**D. 四条收尾分支调整（`_finalize_and_execute_signals` 及其下游）**

- **落库与超时过滤顺序调整（关键）**：当前顺序「超时过滤（`:1397-1398`）→ 落库（`:1401`）」导致超时信号**未落库、无 signal_id**。改为「先落库 → 再超时过滤」：

```python
# 伪代码：_finalize_and_execute_signals 内顺序调整
# 1. 先落库（拿到 signal_id，回填到同一 dict 引用）
for sig in short_signals + long_signals:
    await self._log_signal_to_db(sig["symbol"], sig["direction"], sig)
# 2. 再超时过滤（超时信号按 signal_id 标 timeout）
short_signals = await self._filter_timeout_signals(short_signals, "short")
long_signals = await self._filter_timeout_signals(long_signals, "long")
# 3. 熔断预过滤 / 分桶等保持不变（在其后）
```

- **分桶同一引用**：`:1422-1447` 的分桶用 `production_signals.append(sig)`、`shadow_short.append(s)` 等，均为**同一 dict 引用**；`signal_id` 已在落库时回填，分桶引用可见，无需另建 id 映射。
- **执行分支（`:1961`）**：`await self._update_signal_resolution(signal["signal_id"], resolution)`。
- **冲突分支（`:1920-1950`，共 8 处）**：`_update_signal_resolution(long_s["signal_id"], "conflict")` / `short_s["signal_id"]` 等（`short_s`/`long_s` 来自 `short_by_symbol`/`long_by_symbol`，同一引用）。
- **影子分支（`:1884`）**：`await self._update_signal_resolution(signal["signal_id"], "shadow")`。
- **超时分支（`:3849`）**：`_filter_timeout_signals` 内改为 `await self._update_signal_resolution(signal["signal_id"], "timeout")`（`signal` 是本周期已落库并回填 id 的对应用 dict）。

### 2.3.4 事务 / 错误处理

- INSERT 失败 → `signal["signal_id"]` 缺失，后续 `_update_signal_resolution(None, ...)` 走 fail-safe 跳过 + warning，**不误伤其他行**（改变原「必然宽范围 UPDATE」的行为）。
- `resolution='shadow'` 与 `executed` 严格分离（承 D2/D8）：shadow 分支更新只改 shadow 行 id，production 分支只改 production 行 id，互不覆盖。
- 无新增事务范围：INSERT/UPDATE 各自原子，与现状一致。

### 2.3.5 方案 B（退路，仅在方案 A 的 schema/顺序调整不可行时启用）

- 在 `UPDATE` 的 `WHERE` 增加隔离：`AND (signal_origin IS DISTINCT FROM 'shadow_scoring') AND (resolution IS DISTINCT FROM 'shadow')`，使 production 更新不命中 shadow 行。
- **局限**：方案 B 只能隔离「production 更新不污染 shadow」，但**同向多条 production（或历史多条同向 triggered）仍会被一次性宽范围更新**，不能满足验收断言 1「一次更新只改对应行」。故仅作退路，且需在文档明确其能力边界。
- 本轮**默认按方案 A 落地**；若 `db.fetch_one` 对 `INSERT ... RETURNING` 不可用（见 §4），再评估「`db.execute` 改暴露返回值 + 单独 `SELECT lastval()` / `currval`」等替代而非回退宽范围。

### 2.3.6 回归场景（对应 `signal_probe` + 验收断言）

| 验收断言（需求 §3 R9） | 回归做法 |
|---|---|
| 1. 唯一 ID 贯穿 / 一次更新只改一行 | signal sink Fake 按真实 `WHERE id=$1` 谓词判定「一次 UPDATE 只改对应行」；断言 `_update_signal_resolution` 收到具体 `signal_id` 而非 symbol/direction |
| 2. 同币同向 production(standard)+shadow(emm) | 断言最终 `resolution == ['executed', 'shadow']`（生产 executed、影子 shadow） |
| 3. 同周期多条影子 | 同一周期多条 shadow，各自 `resolution='shadow'`、互不覆盖 |
| 4. 历史未决信号共存 | 存在历史 `status='triggered'` 旧行时，本轮按 id 更新不误伤历史行 |
| 5. 归属字段已保存 | 断言 INSERT 写入 `entry_mode`、`signal_origin` 两列 |

**先红后绿**：修复前 `signal_probe` 断言 `resolution == ['executed', 'executed']`（影子被污染）；修复后反转 `['executed', 'shadow']`。

### 2.3.7 说明：生产测试的 signal sink Fake 收紧（需求 §1.4-3）

- signal sink Fake 必须校验 `UPDATE` 谓词，判定「一次 UPDATE 到底改了几行」，使宽范围污染可被观测：即 Fake 按 `WHERE id=$1` 只更新指定行，若某调用仍未带 `id`（仍是 symbol+direction）应 `raise AssertionError`，暴露回归。

---

## 3. 接口 / 契约变更清单（汇总）

| 变更点 | 谁改 | 改什么 | 调用方需同步 |
|---|---|---|---|
| `get_user_trades` 互斥守卫 | `shared/binance_api.py` | `from_id` + `start/end_time` 同现 raise | `export_fills` 的 factory 已按 fid 分支，不再同现 |
| `api_call_factory(cs, fid)` 分支语义 | `scripts/export_execution_facts.py` | `fid None` 时间定位 / 非空 ID 续传 | `_one_page_iteration` 不变签名 |
| `_one_page_iteration` 右边界过滤 + `row_time_ms` 回调 | `scripts/export_execution_facts.py` | 消费前过滤 `time<=window_end_ms` | `export_fills/income` 传回调 |
| `_resolve_quantity_at_price` 新函数 | `strategies/new_coin/executor.py` | 核算+格式化+min_notional 唯一入口 | `execute_short` 首核算与重算两处调用 |
| `_update_signal_resolution(signal_id, resolution)` | `strategies/hrs/strategy.py` | 按 `id` 更新，签名改 | 执行/冲突/影子/超时四分支传 id |
| `_log_signal_to_db` 返回 id + 两列 | `strategies/hrs/strategy.py` | `RETURNING id` 回填 | `_finalize_and_execute_signals` 顺序调整 |
| `hrs_signals` 加 `entry_mode`/`signal_origin` | `strategies/hrs/strategy.py::_ensure_db_schema` | 建表列 + 幂等 ALTER | 无（不 apply） |

---

## 4. 待确认事项

1. **`shared/database.py::fetch_one` 对 `INSERT ... RETURNING` 的支持**：`fetch_one` 用 asyncpg `fetchrow`，理论上支持 `INSERT ... RETURNING`，但需确认 `_validate_sql` 不拦截带 `RETURNING` 的 INSERT；若拦截，改用「`db.execute` 暴露返回值」或「`INSERT` 后单独 `fetch_one` 一条 `SELECT lastval()`」替代（编码时实测，不改变「按 id 精准更新」原则）。
2. **`fromId` 含/不含自身语义**：保守口径见 2.1.6，本设计不依赖该语义（去重兜底），待真实接口/官方文档逐字确证后可选做显式对齐。
3. **R8 首次引入开仓路径 `min_notional` 校验**是否改变既有「可能被币安 -4164 拒绝」的边界：属「本地更早更安全拦截」，不改变总分/技术门槛；需确认是否纳入本轮（本设计默认纳入，验收断言 4 依赖）。

---

## 5. 对既有代码的最小侵入说明

- **R7**：`export_fills` 仅改 `api_call_factory` 分支；`_one_page_iteration` 新增过滤 + 回调参数；`get_user_trades` 仅加守卫。`income` 续传机制（无 `tranId`，显式不完整分支）**不改变**；fills 幂等键 `(account_id, symbol, trade_id)` 不改变。
- **R8**：`execute_short` 仅做「精度前移 + 首次核算走统一入口 + 重算插入点」三处微调，不改变 D3 反弹参照、总分/技术门槛（9/7 组合）、`_place_short_order` 签名。
- **R9**：`_finalize_and_execute_signals` 仅调「落库/超时过滤」顺序；`_log_signal_to_db`、`_update_signal_resolution` 改签名与 SQL；`_ensure_db_schema` 追加幂等 DDL。不改变 shadow「不参与冲突消解、不真实下单、落 `resolution='shadow'`」口径（D2/D8）。

---

## 6. 风险与不做清单（含能力限制披露）

### 6.1 不做（本轮明确排除）

- 不 push main、不部署、不 apply 迁移、不连生产库、不调真实交易所（全程）。
- 不改 income 续传机制、不新增 `tranId` 续传；income「同毫秒显式不完整」仍是已披露能力限制，不宣称收入流水已采齐。
- 不改变 fills/income 幂等主键；不改变 score 公式/总分/技术门槛；不改变 D3/D8 反弹参照与影子口径。
- 不实现 `fromId` 语义的真实接口确证（本轮不连交易所）。
- 不统一三种下单资金公式（承 D6，保持现状）。
- 熔断预过滤（`circuit_breaker`）移除的信号在 `triggered` 态未 resolve，属既有边界，不在本轮范围内修改（在文档「能力限制」中披露）。

### 6.2 能力限制披露

- R7 修复后 fills 可完整续传，但**收入（income）流水仍受「接口未暴露 `tranId` 续传」限制**，同毫秒超页仍 `page_complete=False`，这不是本轮缺陷、也不是「收入已全部采齐」的声明。
- R8 的缩仓/拒开口径依赖 `_resolve_open_margin_budget` 现有「占用统计 + 限额校验」链路，若上游「占用统计」口径本身有偏差且未在 R1–R6 暴露，本轮不额外修（保持职责边界）。
- R9 若表结构在**未部署**期间由旧代码继续跑，新列 `entry_mode`/`signal_origin` 不存在，INSERT 会因缺列失败 → 既有 `except` 吞异常 + `signal_id=None` → fail-safe 跳过更新（不宽范围污染），属「冷隔离、旧代码不启用新列」的安全退化路径；一旦部署，幂等 `ADD COLUMN IF NOT EXISTS` 生效。

### 6.3 关键风险

1. **R7 若 `api_call_factory` 分支遗漏首采时间定位** → 首屏无 `fid` 却仍带时间，首采正确；漏的是「断点恢复有 last_event_id 时仍走 ID 续传」；已明确由 `loop_advance.event_id` 初始化为 `last_event_id` 保证。
2. **R8 二次重算未覆盖 `min_notional`** → 仍可能下无效单，验收断言 4 失败；已明确校验并入 `_resolve_quantity_at_price`。
3. **R9 落库/超时顺序调整若漏改调用方** → 超时分支拿不到 `signal_id`，fail-safe 跳过导致超时信号不 resolve；已明确 `_filter_timeout_signals` 改用 `signal["signal_id"]`。
4. **禁止重复代码**：`_resolve_quantity_at_price`（R8 唯一入口）、窗口右边界过滤（R7 唯一实现）、`_update_signal_resolution` 的 fail-safe（R9 唯一实现）必须收敛，禁止各组复制同构逻辑。

---

## 7. 实现交接清单（供 3 组 python-engineer 分工）

### 组 A（R7，execution_facts；P1 先做）

| 文件 | 函数/位置 | 改动 |
|---|---|---|
| `shared/binance_api.py` | `get_user_trades`（`:1622`） | `from_id` 与 `start/end_time` 同现 raise（互斥守卫，params 组装前） |
| `scripts/export_execution_facts.py` | `export_fills`（`:439-442`） | `api_call_factory` 按 `fid None/非空` 分支（时间定位 / ID 续传） |
| `scripts/export_execution_facts.py` | `_one_page_iteration`（`:819`） | 新增 `row_time_ms` 参数 + 消费前窗口右边界过滤 |
| `scripts/export_execution_facts.py` | `export_income` / `_run_export_loop` / `_prepare_cursor_and_fetch` / `_run_paging_until_done` | 透传 `row_time_ms` 回调 |
| 测试（分页 Fake） | 生产侧 Fake `request`/`get_user_trades` 替身 | 参数互斥 raise + 正确续传语义（同现 raise；仅 `fromId` 按 id 续传；仅时间按时间过滤） |

### 组 B（R8，new_coin；P1）

| 文件 | 函数/位置 | 改动 |
|---|---|---|
| `strategies/new_coin/executor.py` | 新增 `_resolve_quantity_at_price`（或拆 `_reject_below_min_notional`） | 核算+格式化+min_notional 唯一入口 |
| `strategies/new_coin/executor.py` | `execute_short`（`:466-522`） | 精度前移；首核算改走统一入口；R5 重取成功后 `final_price != budget_price` 时二次核算并覆盖 quantity，失败释放 claim 拒开 |
| 测试（price_probe） | `entry_guard` 返回 `FreshQuote(120)` | 构造价格跳变；断言 `budget_checks ≥ 2`、非「旧量×新价」、下跌 min_notional 拒开 |

### 组 C（R9，HRS；P2）

| 文件 | 函数/位置 | 改动 |
|---|---|---|
| `strategies/hrs/strategy.py` | `_ensure_db_schema`（`:2882`，建表 `:3006` / ALTER 区 `:3033`） | 建表加 `entry_mode`/`signal_origin`；追加两条幂等 `ADD COLUMN IF NOT EXISTS`（不 apply） |
| `strategies/hrs/strategy.py` | `_log_signal_to_db`（`:3688`） | `INSERT ... RETURNING id`（改 `fetch_one`）、回填 `signal["signal_id"]`、写两列；返回 id |
| `strategies/hrs/strategy.py` | `_update_signal_resolution`（`:3751`） | 改 `(signal_id, resolution)`，`WHERE id=$1`；signal_id None → 跳过 + warning |
| `strategies/hrs/strategy.py` | `_finalize_and_execute_signals`（`:1396-1402`） | 「先落库后超时过滤」顺序调整 |
| `strategies/hrs/strategy.py` | `_filter_timeout_signals`（`:3849`）、`_execute_shadow_signals`（`:1884`）、`_resolve_conflicts`（`:1920-1950`）、执行分支（`:1961`） | 全部改为传 `signal["signal_id"]` |
| 测试（signal_probe） | signal sink Fake 的 `execute` | 校验 UPDATE 谓词、按 id 只改一行；构造同币同向 standard+emm、多条影子、历史未决行 |

---

**最后更新：** 2026-10-16（架构设计定稿，供 3 组 `python-engineer-local` 分组并行编码；不部署、不 apply、不连库）

---

## 8. 落地状态（2026-10-16 收口）

> R7–R9 三组（A/B/C）均已按本文契约落地，回归**先红后绿**通过。以下登记「各组落地落点」「与设计定稿的实现偏差」与「能力限制披露」，**不覆盖上文设计原文**。验收判据逐条闭环对照见 [需求 §7](requirements_R7-R9_2026-10-16.md)；复审问题定义见 [第二轮复审报告](../../reviews/strategy_reaudit_2026-10-16_round2/README.md)。本节**只登记 R7/R8/R9 三项**，不主张「已部署 / 已上线」。

### 8.1 各组落地

| 组 | 状态 | 关键实现落点 |
|---|---|---|
| A（R7） | ✅ 落地 | `shared/binance_api.py::get_user_trades` 新增 `from_id`×`start_time/end_time` 互斥守卫（`raise ValueError`）；`scripts/export_execution_facts.py::export_fills` 的 `api_call_factory` 按 `fid None/非空` 分支（时间定位 / 只发 `fromId` 续传）；`_filter_rows_within_window` 客户端过滤窗口右边界；模块级 `_row_time_ms` + `_resolve_page_outcome` 抽公共；测试 Fake 收紧「同现即 raise」 |
| B（R8） | ✅ 落地 | `strategies/new_coin/executor.py` 新增 `_resolve_quantity_at_price`（核算 + `_format_quantity` + `min_notional` 唯一入口）；`execute_short` 精度前移 + 首次核算走统一入口 + R5 重取价后 `final_price != budget_price` 二次重算（失败释放 claim 拒开） |
| C（R9） | ✅ 落地 | `strategies/hrs/strategy.py::_ensure_db_schema` 建表追加 `entry_mode`/`signal_origin` + 两条幂等 `ADD COLUMN IF NOT EXISTS`（未 apply）；`_log_signal_to_db` 改 `INSERT ... RETURNING id`（`fetch_one`）回填 `signal["signal_id"]`；`_update_signal_resolution(signal_id, resolution)` 改 `WHERE id=$1`、`None` 走 fail-safe；`_finalize_and_execute_signals` 落库/超时过滤顺序对调；11 处调用点改传 `signal_id` |

### 8.2 实现偏差（修正/补充设计表述）

- **R7 落点命名（补充 §2.1.3）**：窗口右边界过滤实际以 `_filter_rows_within_window(rows, row_time_ms, window_end_ms)` 独立方法 + 本页结果收尾下沉为 `_resolve_page_outcome` 落地；`_row_time_ms` 为模块级函数（fills/income 复用），未引入实例回调，满足「禁重复代码」。
- **R7 `fromId` 语义（补充 §2.1.6）**：落地为保守口径「`fromId = 本页最大 trade_id`，不 +1」，由 `seen` 去重兜底；币安 PM UM `userTrades` 的 `fromId`「含/不含自身」**仍未确证**（本轮不连交易所），本设计正确性不依赖该语义。
- **R8 `min_notional` 边界（确认 §2.2.6）**：`min_notional` 已首次引入开仓路径下单前校验（防币安 `-4164`），值取自 `trading.min_notional` 配置；本设计 §4 待确认事项 3 已纳入本轮，`_resolve_quantity_at_price` 内含该校验。
- **R9 DDL 位置（确认 §2.3.3-A）**：`hrs.hrs_signals` 建表确为运行时 DDL（`strategy.py::_ensure_db_schema`），不在 `database/` 下；`docs/architecture/数据库设计.md` 按 init-scripts 口径维护 §3.1–§3.15，未收录运行时 DDL 表，故两列变更以本文档为权威记录。
- **R9 `fetch_one` 支持（确认 §4 待确认事项 1）**：`INSERT ... RETURNING id` 已按 `db.fetch_one` 落地可行（`fetch_one` 内部 `fetchrow` 支持 `RETURNING`），未回退方案 B 的隔离谓词。

### 8.3 能力限制披露

- **RK1｜幂等 `ALTER TABLE` 未 apply**：`entry_mode`/`signal_origin` 两列仅在代码建表逻辑声明，**本轮未连生产库、未执行迁移**。上线后需确认 hrs 容器启动日志「数据库 Schema 初始化完成」且 `ADD COLUMN IF NOT EXISTS` 未报错；若两列缺失，`_log_signal_to_db` 的 INSERT 会因缺列失败 → `except` 吞异常 + `signal_id=None` → `_update_signal_resolution(None,…)` fail-safe 跳过更新 + warning（**不崩溃、不回退宽范围**），但归属证据（`entry_mode`/`signal_origin` 与结果归属）**静默丢失**。属设计 §6.2「冷隔离、旧代码不启用新列」的安全退化路径。
- **RK2｜落库/超时过滤顺序对调影响下游统计口径**：对调后**超时信号会被落库**（`triggered` → 按 `signal_id` 标 `timeout`）；下游按 `hrs.hrs_signals` 统计的口径可能变化，**需下游同步**。
- **income 同毫秒不可续传（承设计 §6.2）**：`um/income` 无 `fromId` 类游标，同毫秒超页仍 `page_complete=false`；非本轮缺陷、也不是「收入已全部采齐」的声明。R7 未改变 income 续传机制。
- **熔断预过滤移除的信号在 `triggered` 态未 resolve（承设计 §6.1）**：属既有边界，不在本轮范围，随本文披露。

### 8.4 回归证据

- 定向回归 `tests/test_shared/test_execution_facts_regression.py`（R7 组 `TestR7PaginationContract` 等）+ `strategies/new_coin/tests/test_reprice_budget_r8.py`（4 用例）+ `strategies/hrs/tests/test_r9_signal_attribution.py`（5 用例）→ **28 passed**。
- 先红基线：第二轮复审复现脚本 `reproduce_findings.py` 的 signal_probe / price_probe / pagination_probe 三场景（`reproduction_results.json`）。
- 旧回归失败 triage（非本轮引入）：见 [需求 §7.4](requirements_R7-R9_2026-10-16.md)。

**最后更新（收口）：** 2026-10-16（R7–R9 状态改为「设计定稿 + 已落地」；新增 §8 落地状态 / 实现偏差 / RK1·RK2·income 能力限制 / 回归证据；不部署、不 apply、不连库）