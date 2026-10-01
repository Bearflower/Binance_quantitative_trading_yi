# P0-D 架构设计：new_coin 持仓保护「增量补挂」（消除撤旧止损造成的 SL 空窗）

## 1. 文档信息

| 项 | 内容 |
|----|------|
| 文档名称 | P0-D 架构设计：new_coin 持仓保护增量补挂 |
| 版本 | v1.0（架构定稿，待评审） |
| 作者 | backend-architect |
| 创建日期 | 2026-10-01 |
| 上游需求 | [fix-2026-10-01-incremental-replenish-requirements.md](./fix-2026-10-01-incremental-replenish-requirements.md)（P0-D-AC1..20、D-Q1..D-Q6） |
| 下游环节 | python-engineer（编码实现）→ 强制测试（幻觉测试 + 功能测试 + 覆盖率） |
| 适用范围 | **仅 `strategies/new_coin/`**；不改 `shared/`（避免触发全策略容器重建） |
| 交付口径 | 本文档为「可直接照做」的实现交接依据，末尾含逐文件交接清单 |

> 本文档不含业务代码，仅给出模块/函数级设计、签名、不变式、状态流转与验收映射。

---

## 2. 目标与不变式

### 2.1 一句话目标

核验真实空头敞口缺失的保护单**类型**后，**只补挂缺失类型**，**绝不撤销或变更任何一条仍然有效（OPEN）的保护单**。

### 2.2 硬不变式（不可违反，实现与测试均围绕其展开）

| 编号 | 不变式 | 违背后果 |
|------|--------|---------|
| INV-1 | 当存在有效 SL 时，补挂默认路径**零撤单**（`cancel_all_algo_orders` 调用次数 = 0） | SL 空窗，资金风险（本 P0-D 核心） |
| INV-2 | 只挂 `missing`（类型级）覆盖的订单；`missing` 未含的类型一律不挂、不撤 | 重复 SL / 误撤 TP |
| INV-3 | 缺失判定不确定（DB 异常）时**不补挂**（零挂零撤），仅告警、下周期重试 | 凭 fail-closed 全类型结果误挂重复 SL |
| INV-4 | 保留既有 `position_tracking[symbol]['algo_ids']` 已有键值，不清空、不覆盖。**豁免**：缺失类型重挂时，允许更新其对应键为新 algo_id（旧值已失效，如 S2 的 `tp1`） | 丢失可取消依据 |
| INV-5 | `market_close` 分支只影响 TP 自身，**绝不触发对 SL 的撤销**；平仓后仍有剩余敞口则 SL 必须存在 | 以「平仓」为由跳过硬止损 |
| INV-6 | 只有本轮所有缺失类型均「挂成功或幂等命中」才置位 `_replenished_symbols`；部分成功/不确定不置位 | 缺口被误判为已闭合 |
| INV-7 | fail-closed 贯穿：任何不确定/异常收敛都不得导致「裸奔」（无保护）或「重复挂单」 | 资金安全 |

---

## 3. 关键设计决策（A~H，逐条结论）

### A. TP 补挂粒度 —— **采用需求 §4.1 方案甲（类型级）**

**结论**：`missing` 含 `止盈单` → 视为 TP 全缺，补 TP1+TP2；`missing` 不含 `止盈单` → 不补任何 TP。

**理由**：
1. 与现有 [find_missing_protection](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L991-L1035) 的 `DISTINCT order_type` 语义**完全对齐**（`tp1`/`tp2` 在 `condition_orders` 同记 `TAKE_PROFIT`，见 [_CONDITION_ORDER_META](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L63-L67)）。
2. 本轮核心止血目标是**消除「仅缺 TP 时撤 SL」造成的 SL 空窗**——方案甲已能根除此根因（因为不再撤单）。
3. 方案乙需扩展判据到档位级并与 `algo_ids` 缓存一致性耦合，扩大改动面与回归风险（违背「本轮只解决有无」的 D-Q6 边界）。

**显式记录的已知局限（写入文档 + 测试注释 + 需求 §7 后续迭代）**：
> **方案甲局限**：若「TP1 丢失但 TP2 仍在」，`find_missing_protection` 返回 `[]`（判为无缺口），**不会补挂 TP1**，形成遗留缺口。这是本轮**已知且接受**的局限；根因（撤 SL 空窗）已被消除，该局限不影响止损安全。

**AC 映射**：P0-D-AC3（仅缺 TP → 补 TP1+TP2，SL 原样保留、零撤单）。

---

### B. 取消默认路径的 Phase B 全量撤单 —— **取消，并重定义回退开关语义**

**结论**：
- **默认路径（`cancel_after_ready=True`）**：Phase A 只读准备 → Phase C **增量挂缺失项**，**不再调用任何撤单**。
- **`replenish_cancel_after_ready`**：**保留名称与默认值 `true`**（向后兼容配置），**语义重定义**为「启用增量补挂（只补不撤）」；置 `false` 时回退到**旧全量重建**（`prepare → _cancel_orders_strict → _execute_replenish_plans` 全量挂 sl+tp1+tp2），作为应急开关。
- `_cancel_orders_strict` **保留**，**仅供回退分支使用**（回退分支要求撤单失败即阻断挂单，保持旧「先算后撤」安全语义）。
- `_cancel_orders_best_effort` **删除**（新设计中无调用点，避免死代码）。

**理由**：需求 D-Q5 明确「保留开关名与默认 true，语义改为启用增量补挂，false 回退旧全量行为」。回退分支采用 strict（而非 best-effort）以对齐「旧全量重建」的原始语义（撤单失败不挂新单，避免重复单）。

**AC 映射**：P0-D-AC1（默认路径零撤单）、P0-D-AC19（冲突用例显式迁移）、P0-D-AC20（无撤单日志）。

---

### C. `condition_orders`(DB) 与 `position_tracking.algo_ids` 一致性 —— **以 DB 为权威，缓存尽力回填**

> 批注（2026-10-01 编码定案，最终实现口径）：**本轮已实现（无条件 best-effort，不新增配置开关）**。`_backfill_algo_ids_from_db(symbol)` 在增量路径挂单前被**无条件调用**：只 `setdefault` 填空缺、不覆盖已有键、异常吞掉仅告警。原计划的 `_maybe_backfill_algo_ids` 包装与配置项 `trading.replenish.algo_id_backfill_enabled` **本轮不实现**（改用无条件 best-effort 回填，见决策 H）。代码实现同时保证 INV-4（不清空、不覆盖既有 `algo_ids`，只写入新挂成功条目；缺失类型重挂时更新其对应键，见 INV-4 豁免）。

**结论**：
- **权威判据 = `condition_orders` 表**（`find_missing_protection` 即查此表，`status='OPEN'`）。
- **保留**既有单时**不清空、不覆盖** `position_tracking[symbol]['algo_ids']`；增量路径**禁止** `algo_ids = {}`。
- **尽力回填**：若某类型判定为「已存在」但其 `algo_ids` 键缺失，则从 DB `OPEN` 记录尝试回填（best-effort，**失败不阻断**补挂，也不重复挂该类型）。回填映射：`STOP_LOSS → 'sl'`；`TAKE_PROFIT → 按行序填入 'tp1'、'tp2'`（DB 无法区分档位，属尽力而为；algo_id 仅用于后续取消清理，误配不产生保护风险）。
- **新增配置门控** `trading.replenish.algo_id_backfill_enabled`（默认 `true`）。⚠️ **本轮不实现**：最终实现改为**无条件 best-effort 回填**，不新增该配置键（见本节批注与决策 H）。

**理由**：需求 D-Q2。位置：`_backfill_algo_ids_from_db`，由增量执行器在挂单前调用。回填是「锦上添花」，不构成补挂前置阻塞条件（`cancel_all_algo_orders` 已有「本地无 algoId → 回退查 DB」兜底，见 [cancel_all_algo_orders](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L2947-L3007)）。

**AC 映射**：P0-D-AC13。

---

### D. `_replenished_symbols` 置位语义 —— **本轮目标类型全成功才置位**

**结论**：
- **置位条件**：本轮 `missing` 覆盖的**每一条**都「挂成功或幂等命中」→ 置位；任一未到位 → **不置位**，下周期重试。
- `missing=None`（不可判定）/ Phase A 失败 / 部分失败：**一律不置位**。
- **「完成」口径**：以「本轮目标类型是否全部落地」为准。**跨周期自愈**由守卫每轮**重新核验** `find_missing_protection` 保证（即需求 D-Q4 所述「缺口闭合」的再核验发生在**守卫侧**，不在 executor 内重复查询，避免双重 DB 往返与「第二权威源」）。
- 守卫路径每轮补挂前 `reset_replenish_flag`（现状保留），故 `_replenished_symbols` 仅用于**同一轮内防重**与**非守卫调用方跳过**。

**理由**：需求 D-Q4 的约束（部分成功不置位、不确定不置位）被严格满足；「再次核验返回 `[]`」以守卫下一轮重算表达，符合 §3.2 S4/AC12。

**AC 映射**：P0-D-AC9、AC10、AC12。

---

### E. 取数失败（fail-closed 全类型）与「不可判定」的区分 —— **引入 strict 复核，不确定则不补挂**

**结论**：给 [find_missing_protection](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L991-L1035) 新增关键字参数 `strict: bool = False`：
- `strict=False`（默认，**保持 P0-A-AC3 现有契约**）：查询异常 → 返回「全部应存在类型」（fail-closed，用于「是否触发守卫」）。
- `strict=True`（守卫使用）：查询异常 → 返回 `None`，表示**不可判定**。

守卫侧：`missing = await find_missing_protection(symbol, strict=True)`；
- `missing is None` → **不调用补挂**（零挂零撤），累计 attempts + 告警「核验失败(不可判定)」，返回、不置位。
- 这样**不会**把 fail-closed 的全类型结果当作「真缺」去补挂，杜绝重复止损单（P0-D-AC7）。

**理由**：需求 §4.2/D-Q 倾向「不确定则不补挂，仅告警」。用 `strict` 参数而非改默认契约，可**零回归**保持既有 P0-A 用例与既有守卫用例（它们 mock 同一方法名、返回列表，不受 `strict` 影响）。

**AC 映射**：P0-D-AC7。

---

### F. `market_close` 分支 —— **只影响 TP，绝不撤 SL**

**结论**：增量路径下 TP 计划若 `market_close=True`（现价已过目标价），执行**部分市价平仓该部分**（复用 [_market_close_partial](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3625-L3637)），**零撤 SL**；SL 属「已存在类型」则不动，属「缺失类型」则由本轮一并补挂。`market_close` **不得成为跳过硬止损保护的理由**。

**理由**：需求 D-Q3。

**AC 映射**：P0-D-AC14。

---

### G. `replenish_conditional_orders` 签名变更 —— **新增必需关键字参数 `missing`**

**结论**：新签名

```python
async def replenish_conditional_orders(
    self, symbol: str, entry_price: Decimal, *, missing: List[str]
) -> bool
```

- `missing` 为**必需的关键字参数（keyword-only，非 None）**，取值来自守卫 `find_missing_protection(strict=True)` 的**权威结果**（非空列表；空列表由守卫提前返回，不进入补挂）。
- **入口 fail-closed 守卫**：`replenish_conditional_orders` 开头 `if not missing: return False` —— `missing` 为空列表或 `None` 时**零挂零撤、不置位、直接返回 `False`**（防御性双保险，防止未来调用方传入空值被静默当作「无缺口」）。
- **不设默认值**：money-safety 函数不允许「静默默认」——既不允许默认全类型（可能重复挂 SL），也不允许默认 no-op（可能静默吞掉真实缺口）。强制每个调用点显式声明缺失类型。
- **所有调用方同步更新（完整清单见 §7.4 与 §10）**。

**理由**：需求 §3.5 伪代码即 `replenish_conditional_orders(symbol, entry_price, missing)`；需求 G 明确「签名变更需向后兼容或同步更新所有调用方」。选择「同步更新所有调用方」，使增量契约在类型层不可被绕过。

**AC 映射**：P0-D-AC2/AC3/AC4/AC5（守卫在 `missing=[]` 时不调用）、AC7/AC18。

> 备注（供用户拍板）：若评审更看重「最小化既有用例改动」，可退化为 `missing: Optional[List[str]] = None` 且 `None` 表示「向后兼容的旧全量语义」——但该默认在「未来新增调用方忘记传参」时存在重复挂 SL 的隐患，本架构**不推荐**。

---

### H. 需新增/修改的配置项（全部来自 `config.yaml`，禁硬编码）

**结论**：新增 1 项、重定义 1 项语义；**无新增阈值/间隔/数值**。

| 配置键 | 类型 | 默认 | 变更 | 说明 |
|--------|------|------|------|------|
| `trading.replenish.cancel_after_ready` | bool | `true` | **语义重定义（键名/默认不变）** | `true`=启用增量补挂（只补不撤）；`false`=应急回退旧全量重建（含 strict 撤单） |
| `trading.replenish.algo_id_backfill_enabled` | bool | `true` | **本轮不实现** | 原计划：是否从 DB `OPEN` 记录尽力回填缺失的 `algo_ids`（D-Q2）。**最终改为无条件 best-effort 回填，不新增该配置键** |
| `trading.replenish.require_take_profit` | bool | `true` | 复用 | 核验是否要求 TP（影响 `missing` 是否含 `止盈单`） |
| `trading.replenish.ignore_error_codes` | list | 见配置 | 复用 | 幂等错误码（挂单命中视为成功） |
| `trading.replenish.skip_symbols` | list | 见配置 | 复用 | 托管清单，跳过 |
| `trading.replenish.guard_interval_seconds` | float | `300` | 复用 | 守卫周期 |
| `trading.replenish.alert_throttle_seconds` | float | `3600` | 复用 | 告警降频窗口 |

**理由**：需求 §8「禁硬编码」+ H。⚠️ **本轮不实现配置门控**：回填改为**无条件 best-effort**（`_backfill_algo_ids_from_db` 在增量挂单前无条件调用），故 `TradingExecutor.__init__` **不新增** `algo_id_backfill_enabled` 读取。

**AC 映射**：P0-D-AC18。

---

## 4. 模块/函数级改造清单

### 4.1 新增模块级常量（消除魔法字符串，DRY）

`strategies/new_coin/executor.py`（紧邻 `_CONDITION_ORDER_META`，L63 附近）：

```python
_MISSING_SL = "止损单"   # find_missing_protection 与增量执行器共用，杜绝魔法字符串
_MISSING_TP = "止盈单"
```

- `find_missing_protection` 内部改用上述常量（替换现有字面量 `"止损单"`/`"止盈单"`）。

### 4.2 `executor.py` 函数级清单

| 函数 | 动作 | 新签名 | 职责 |
|------|------|--------|------|
| `find_missing_protection` | **修改** | `(self, symbol, *, require_take_profit=None, strict=False) -> Optional[List[str]]` | 新增 `strict`；`strict=True` 时查询异常返回 `None`（不可判定）；默认保持 fail-closed 全类型。内部改用 `_MISSING_SL/_MISSING_TP` |
| `replenish_conditional_orders` | **修改** | `(self, symbol, entry_price, *, missing: List[str]) -> bool` | 编排：**入口 fail-closed 守卫（`missing` 空/`None` → 零挂零撤、不置位、返回 `False`）** → `skip` 判定 → Phase A 准备 →（默认）增量挂 /（回退）strict 撤 + 全量挂 |
| `_execute_incremental_plans` | **新增** | `(self, symbol, plans, missing: List[str]) -> bool` | 只挂 `missing` 覆盖类型：`止损单`→SL；`止盈单`→TP1+TP2；复用 `_place_conditional_and_record` / `_apply_take_profit`；全成功才置位 |
| `_backfill_algo_ids_from_db` | **新增** | `(self, symbol) -> None` | 从 DB `OPEN` 记录尽力回填缺失 `algo_ids`；**增量挂单前无条件调用**（只 `setdefault` 填空缺、不覆盖，best-effort，异常吞掉仅告警） |
| `_execute_replenish_plans` | **保留** | 不变 | 回退分支专用：全量挂 sl+tp1+tp2 |
| `_cancel_orders_strict` | **保留** | 不变 | 仅供回退分支 |
| `_cancel_orders_best_effort` | **删除** | — | 新设计无调用点 |
| `_apply_take_profit` | 保留 | 不变 | 单条 TP：skip / market_close / 挂单 |
| `_place_conditional_and_record` | 保留 | 不变 | SL/TP 共用挂单与 algo_id 记录路径 |
| `_market_close_partial` / `_mark_partial_close` / `_build_tracking_entry` | 保留 | 不变 | 市价部分平仓及其状态回写 |

### 4.3 `strategy.py` 函数级清单

| 函数 | 动作 | 职责 |
|------|------|------|
| `_guard_symbol` | **修改** | `find_missing_protection(strict=True)`；`None`→不可判定分支（零补挂 + 告警）；非空→清标记 + `replenish_conditional_orders(..., missing=missing)`；失败→累计 attempts + 告警 |
| `_notify_protection_issue` | **修改** | 告警文案复用入参 `reasons`（含缺失类型列表）；不可判定场景传入 `["保护单核验失败(不可判定)"]` |
| `_record_gap_and_alert`（可选新增私有助手） | **新增** | 抽取「attempts+1 且告警」重复逻辑（不可判定分支与补挂失败分支共用），满足 DRY、控函数长度 |

### 4.4 关键伪代码（实现参照，非最终代码）

```python
# executor.py —— 编排
async def replenish_conditional_orders(self, symbol, entry_price, *, missing):
    if not missing:                 # 入口 fail-closed：空列表/None → 零挂零撤、不置位
        return False
    try:
        if self._should_skip_replenish(symbol):
            return True
        logger.info(f"开始补全条件单: {symbol}", entry_price=float(entry_price))
        status, plans = await self._prepare_replenish_plans(symbol, entry_price)
        if status == _REPLENISH_NO_POSITION:
            return True
        if status != _REPLENISH_READY:
            return False
        if not self.replenish_cancel_after_ready:
            # 应急回退：旧全量重建（先算后撤，撤单失败阻断）
            if not await self._cancel_orders_strict(symbol):
                return False
            return await self._execute_replenish_plans(symbol, plans)
        return await self._execute_incremental_plans(symbol, plans, missing)
    except Exception as e:
        return self._handle_replenish_exception(symbol, e)

# executor.py —— 增量挂（只补 missing）
async def _execute_incremental_plans(self, symbol, plans, missing):
    if symbol not in self.position_tracking:
        self.position_tracking[symbol] = self._build_tracking_entry(
            entry_price=plans['entry_price'], entry_quantity=plans['quantity'], atr=plans['atr'])
        self._last_tracked_qty[symbol] = float(plans['quantity'])
    await self._backfill_algo_ids_from_db(symbol)   # 无条件 best-effort 回填（不设配置开关）
    all_success = True
    if _MISSING_SL in missing:
        ok = await self._place_conditional_and_record(
            symbol, algo_key='sl', stop_price=plans['sl']['price'],
            limit_price=plans['sl']['limit_price'], quantity=plans['quantity'])
        all_success = all_success and ok
    if _MISSING_TP in missing:
        for level, key in ((1, 'tp1'), (2, 'tp2')):
            ok = await self._apply_take_profit(symbol, plans[key], level)
            all_success = all_success and ok
    if all_success:
        self._replenished_symbols.add(symbol)
        logger.info(f"条件单增量补全完成: {symbol}")
    else:
        logger.warning(f"条件单增量补全部分失败: {symbol}")
    return all_success
```

```python
# strategy.py —— 守卫
async def _guard_symbol(self, symbol, db_position):
    executor = self.trading_executor
    missing = await executor.find_missing_protection(symbol, strict=True)
    if missing is None:                       # S6 不可判定：零补挂、仅告警
        await self._record_gap_and_alert(symbol, ["保护单核验失败(不可判定)"])
        return
    if not missing:                           # S4 无缺口
        self._protection_gap_attempts.pop(symbol, None)
        return
    entry_price = float(db_position.get('entry_price', 0) or 0)
    if entry_price <= 0:                      # S10 入场价无效
        logger.warning("守卫跳过：入场价无效，无法补挂", symbol=symbol)
        await self._notify_protection_issue(symbol, missing)
        return
    executor.reset_replenish_flag(symbol)
    ok = await executor.replenish_conditional_orders(
        symbol, Decimal(str(entry_price)), missing=missing)
    if not ok:                                # S9 部分失败：累计重试 + 告警
        await self._record_gap_and_alert(symbol, missing)
```

---

## 5. 增量补挂状态流转（对齐需求 §3.2 S1~S10）

```
守卫轮询 (guard_interval_seconds)
   │
   ├─ 权威集合取数失败? ─ 是 → 整轮跳过，不推进时间戳（现状保留，fail-closed）
   │
   └─ 逐 symbol（_guard_inflight 去重）
        │
        missing = find_missing_protection(strict=True)
        │
        ├─ None（不可判定，S6）──────────────→ 零挂零撤 + 告警 + 不置位 → 下周期重试
        ├─ []（全在，S4）────────────────────→ pop attempts；返回
        ├─ entry_price<=0（S10）─────────────→ 零挂零撤 + 告警；返回
        └─ 非空（S1/S2/S3）──────────────────→ reset_replenish_flag
                                                replenish_conditional_orders(missing)
                                                  │
                                                  ├─ _should_skip_replenish → True
                                                  ├─ Phase A 准备
                                                  │    ├─ NO_POSITION（S5）→ True（不撤不挂）
                                                  │    └─ FAILED（S7）────→ False（零触达）+ 告警
                                                  └─ 默认路径：_execute_incremental_plans
                                                       ├─ 仅"止损单" → 只挂 SL（S1）
                                                       ├─ 仅"止盈单" → 只补 TP1+TP2，SL 不动（S2）
                                                       ├─ 两者      → SL+TP1+TP2（无单可撤，S3）
                                                       ├─ 幂等错误码（S8）→ 该类记成功
                                                       └─ 结果：全成功→置位 True；否则 False（S9）
```

`market_close` 分支（S2 子情形）：TP 计划 `market_close=True` → 部分市价平仓；**SL 不受影响**（D-Q3/F）。

---

## 6. 与 20 条验收标准（P0-D-AC）的映射表

| AC | 设计落点 | 实现要点 |
|----|---------|---------|
| AC1 增量核心不变式（有效 SL 时零撤单） | §4.4 默认路径、INV-1 | 默认分支不调用 `cancel_all_algo_orders` |
| AC2 仅缺 SL → 只挂 1 条 SL | `_execute_incremental_plans` `if _MISSING_SL in missing` | TP 分支不进入 |
| AC3 仅缺 TP → 补 TP，SL 原样保留、零撤单 | 方案甲 A；`_execute_incremental_plans` | SL 分支不进入 |
| AC4 全缺 → 挂 SL+TP1+TP2，零撤单 | `_execute_incremental_plans` | 无旧单，撤单自然为 0 |
| AC5 全在 → 守卫不调用补挂 | `_guard_symbol` `if not missing: return` | 保持现状 |
| AC6 无空头持仓 → 不撤不挂返回 True | `_prepare_replenish_plans` `NO_POSITION` | 保持现状 |
| AC7 取数失败不误补 | `find_missing_protection(strict=True)`→None；守卫零补挂 + 告警 | 见 E |
| AC8 Phase A 失败零触达 | `_prepare_replenish_plans` 返回 `FAILED` | 保持现状（ATR/精度/现价/ensure-active） |
| AC9 幂等错误码视成功 | `_place_conditional_and_record`（复用） | 该类记成功；其余成功则置位 |
| AC10 部分成功不置位 | `_execute_incremental_plans` `all_success` 聚合 | 保留已成功项 |
| AC11 增量重试（下轮只补仍缺） | 守卫每轮重算 `missing` | 不触发全量重建、不撤已成功单 |
| AC12 缺口闭合自愈 | 守卫每轮重新核验（D） | 成功→下轮 `missing=[]`；再丢→重新补 |
| AC13 algo_ids 一致性 | INV-4 + `_backfill_algo_ids_from_db` | 保留既有键；新条目写入；DB 一致 |
| AC14 market_close 不撤 SL | F | 只平 TP 部分，零撤 SL |
| AC15 并发/在途去重 | `_guard_inflight` + `_should_skip_replenish` | 保持现状 |
| AC16 托管清单跳过 | `_should_skip_replenish` + `replenish_skip_symbols` | 保持现状 |
| AC17 告警降频与内容 | `_notify_protection_issue` / `_record_gap_and_alert` | 含 symbol、缺失类型列表、attempts |
| AC18 无硬编码/配置化 | §3.H | 新开关来自 config，缺省有默认 |
| AC19 既有测试显式迁移 | §7.4 | 冲突用例显式更新并标注 |
| AC20 不误判 300s 空窗 | INV-1、§4.4 | 核心场景无「撤销旧条件单」日志 |

---

## 7. 错误与边界处理 + 调用方清单

### 7.1 错误路径

| 场景 | 处理 | 返回 |
|------|------|------|
| `missing` 空/`None`（入口防御） | 入口 fail-closed 守卫：零挂零撤、不置位 | False |
| DB 查询异常（strict=True） | `None` → 守卫零补挂 + 告警 + 不置位 | 守卫 return，不进入补挂 |
| Phase A 失败（ATR=0/精度/现价/ensure-active） | 零触达，告警 | False |
| 挂单真实失败 | 不置位，下周期增量重试 | False |
| 挂单命中幂等错误码 | 该类记成功 | 视其余类型 |
| 顶层异常命中幂等码 | `_handle_replenish_exception` 收敛置位 | True |
| `market_close` 部分平仓异常 | 吞掉仅告警（现状） | TP 记成功（现状） |
| `_backfill_algo_ids_from_db` 异常 | 吞掉仅告警，不阻断补挂 | — |

### 7.2 边界

空列表/`None`（`missing`）：守卫在 `None`/`[]` 均提前返回，不进入补挂；且 `replenish_conditional_orders` 入口设 fail-closed 守卫（`missing` 空/`None` → 零挂零撤、不置位、返回 `False`）作为双保险。
数量变化（TP 部分成交后 `remaining_quantity` 减小）：复用现有 `_resolve_short_quantity` + `_mark_partial_close`。
`entry_price<=0`、精度 `tick/step<=0`：守卫/Phase A 拦截。
下单 API 抛异常 / 幂等码集合配置为 `None`：`_place_conditional_and_record` + `ignore_error_codes` 兜底（现状保留）。

### 7.3 不变式保持清单（实现须核验）

- 默认路径**不出现** `cancel_all_algo_orders`。
- 增量执行器**不写** `algo_ids = {}`。
- `missing` 未含的类型**不进入**任何挂/撤分支。
- 回退分支（`cancel_after_ready=False`）行为与「改动前的默认路径」等价。

### 7.4 `replenish_conditional_orders` / `find_missing_protection` 全部调用方（已全仓搜索）

**生产调用点**：
- `strategies/new_coin/strategy.py` `_guard_symbol`（L1165、L1176）——同步更新。

**测试调用点（需同步）**：
- `strategies/new_coin/tests/test_p0_replenish_and_registration.py`（12 处 2 参调用：L99/128/142/154/165/177/188/204/476/503/514/525）
- `tests/test_strategies/test_new_coin_tracking_entry.py`（L150）
- `tests/test_strategies/test_new_coin_reconcile_guard_p0a.py`（mock `find_missing_protection`，方法名不变，**无需改**）
- `tests/test_strategies/test_new_coin_reconcile_guard_p0a_extra.py`（同上，**无需改**）

---

## 8. 配置项清单（落地到 `config.yaml`）

`strategies/new_coin/config.yaml` → `trading.replenish`：

```yaml
  replenish:
    # 语义重定义（2026-10-01）：true=启用增量补挂（只补缺失类型、绝不撤已有效保护单）；
    # false=应急回退旧全量重建（含 strict 撤单），会重新引入 SL 空窗，仅供短期止血
    cancel_after_ready: true
    # 注：algo_id_backfill_enabled 本轮不实现——回填已改为增量挂单前「无条件 best-effort」
    #     （只 setdefault 填空缺、不覆盖），故 config.yaml 不新增该键
    ignore_error_codes: ['-4164', '-2011', '-2021', '-4136', '-4507']
    skip_symbols: [BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, XRPUSDT, TRXUSDT]
    guard_interval_seconds: 300
    require_take_profit: true
    alert_throttle_seconds: 3600
```

实现：**不新增** `algo_id_backfill_enabled` 配置读取；`_backfill_algo_ids_from_db` 在增量挂单前**无条件调用**（best-effort）。

---

## 9. 测试策略

### 9.1 需迁移的既有用例（详见 §10.3）

**A. 语义冲突（必须显式更新并标注提交说明）**：
1. `test_p0_3_ac2_prepare_before_cancel_then_place_all` → 默认路径不再撤单：断言顺序改为 `[atr, place×3]`，撤单 0 次（并补 `missing`）。
2. `test_p0_3_ac3_cancel_failure_blocks_placement` → 撤单失败阻断语义仅存在于回退分支：改为 `cancel_after_ready=False` 场景断言。
3. `test_replenish_fallback_uses_best_effort_cancel_before_read` → 回退分支改为 `prepare → strict 撤 → 全量挂`：断言顺序 `[atr, cancel, place×3]`。
4. `test_replenish_strict_cancel_exception_blocks_placement` → 迁移到回退分支（`cancel_after_ready=False`）。

**B. 机械补参（新增 `missing=[...]`，行为不变）**：
`test_p0_3_ac1_atr_failure_does_not_cancel`、`test_p0_3_ac4_partial_place_failure_not_marked`、
`test_p0_3_ac5_no_position_returns_true_without_touch`、`test_p0_3_ac6_already_replenished_skips`、
`test_p0_3_managed_symbol_skipped_from_config`、`test_p0_1_ac5_ensure_active_failure_skips_atr_and_cancel`、
`test_ensure_symbol_active_all_exceptions_skips_atr_and_cancel`、`test_replenish_top_level_exception_converges_via_handler`、
`test_replenish_tp1_market_close_hits_line_3080`（tests/test_strategies/）。

**C. 删除**：`test_cancel_orders_best_effort_swallows_exception`（对应函数已删除）。

**D. 保持不变**：`test_cancel_orders_strict`、`test_execute_replenish_plans_*`、`test_apply_take_profit_*`、
`test_build_replenish_plans_*`、`test_find_missing_protection_*`、守卫 `TestProtectionGuard` 全部（mock 方法名不变）。

### 9.2 需新增用例（覆盖 P0-D-AC）

| 用例（建议命名） | 覆盖 AC |
|------------------|---------|
| `test_incremental_zero_cancel_when_sl_exists` | AC1/AC20 |
| `test_incremental_only_missing_sl_places_sl` | AC2 |
| `test_incremental_only_missing_tp_keeps_sl` | AC3（核心止血） |
| `test_incremental_all_missing_places_all` | AC4 |
| `test_guard_skips_when_no_gap`（既有覆盖）/ 补断言 | AC5 |
| `test_no_position_returns_true`（迁移覆盖） | AC6 |
| `test_guard_uncertain_when_db_error_no_place` | AC7 |
| `test_prepare_failure_no_touch`（既有参数化覆盖） | AC8 |
| `test_idempotent_code_counts_success`（既有覆盖 + 增量入口） | AC9 |
| `test_incremental_partial_failure_not_marked` | AC10 |
| `test_incremental_retry_only_missing_second_round` | AC11 |
| `test_gap_closed_then_reopen_self_heal` | AC12 |
| `test_incremental_preserves_existing_algo_ids_and_backfills` | AC13 |
| `test_market_close_does_not_cancel_sl` | AC14 |
| `test_inflight_dedup`（既有覆盖） | AC15 |
| `test_managed_symbol_skip`（既有覆盖） | AC16 |
| `test_gap_alert_throttle_and_content`（既有覆盖 + 补类型列表文案） | AC17 |
| `test_new_config_defaults_without_crash` | AC18 |
| 迁移用例清单（§9.1.A） | AC19 |
| `test_no_cancel_log_in_incremental_core_scenario` | AC20 |

### 9.3 覆盖率要求（强制）

- 核心逻辑 100%：`replenish_conditional_orders` 两分支（默认/回退）、`_execute_incremental_plans` 的 4 种 `missing` 组合、`_backfill_algo_ids_from_db` 成功/异常。
- 边界 100%：`missing=[]`/`None`、`entry_price<=0`、精度 `<=0`、ATR=0、无持仓、幂等码集合为空。
- 错误处理 100%：DB 异常（strict 与非 strict）、挂单异常、市价平仓异常、回填异常、顶层异常收敛。
- 外部调用 mock：Binance API、DB、kline_service。

---

## 10. 风险与回滚

### 10.1 风险

| 风险 | 影响 | 缓解 |
|------|------|------|
| 重复止损单（判定出错） | 两条 SL | INV-3（不确定不补）+ DB 权威判据 + 幂等错误码兜底 |
| 方案甲遗留缺口（仅缺一档 TP） | TP1 未补 | 显式声明局限；根因（撤 SL 空窗）已消除；列入后续迭代 |
| `missing` 必需参数导致既有用例报错 | CI 红 | §9.1.B 机械补参；§9.1.A 语义迁移 |
| 回退开关二义性 | 误设 false 回退全量撤单 | 键名/默认不变 + 语义写入 config 注释与告警文案 + 用例覆盖 |
| `_cancel_orders_best_effort` 删除影响其他调用 | 运行时 AttributeError | 全仓搜索确认无其他调用点（§7.4） |

### 10.2 回滚

- 代码回滚：`git revert` → push main → Actions 自动重建 `trading-new-coin`（不改 `shared/`，不触发其他策略重建）。
- 运行时回退：`trading.replenish.cancel_after_ready=false` 回退旧全量重建（应急，会重新引入 SL 空窗，仅短期止血）。
- 部署验证：按部署规则「防部署幻觉五层验证」；`docker ps` 确认所有服务在运行（防 PostgreSQL 命名冲突导致后续服务未启动的已知坑）。

---

## 11. 给 python-engineer 的实现交接清单

### 11.1 `strategies/new_coin/executor.py`

| 动作 | 对象 | 期望签名/要点 | 关键不变式 | 必须保持的既有行为 |
|------|------|--------------|-----------|-------------------|
| 新增 | `_MISSING_SL`/`_MISSING_TP` 常量 | `_MISSING_SL="止损单"`、`_MISSING_TP="止盈单"` | 消除魔法字符串 | — |
| 修改 | `find_missing_protection` | `(symbol, *, require_take_profit=None, strict=False) -> Optional[List[str]]` | `strict=True` 查询异常返回 `None`；默认 fail-closed 全类型不变 | P0-A-AC3 语义、默认返回契约、日志 |
| 修改 | `replenish_conditional_orders` | `(symbol, entry_price, *, missing: List[str]) -> bool` | 入口 fail-closed：`missing` 空/`None`→零挂零撤、不置位、返回 False；默认路径零撤单；`missing` 必需 | `_should_skip_replenish` 优先、NO_POSITION→True、Phase A 失败→False、顶层异常收敛 |
| 新增 | `_execute_incremental_plans` | `(symbol, plans, missing) -> bool` | 只挂 `missing` 覆盖类型；不清空 algo_ids；全成功才置位 | 复用 `_place_conditional_and_record`/`_apply_take_profit`；tracking 幂等建立 |
| 新增 | `_backfill_algo_ids_from_db` | `(symbol) -> None` | 增量挂单前**无条件调用**；只 `setdefault` 填空缺、不覆盖；best-effort，异常吞掉仅告警 | 不清空/不覆盖既有键值 |
| 不实现 | `_maybe_backfill_algo_ids`（原可选包装） | — | 本轮不实现（改为无条件 best-effort 回填，不设配置门控） | — |
| 保留 | `_execute_replenish_plans`/`_cancel_orders_strict`/`_apply_take_profit`/`_place_conditional_and_record`/`_market_close_partial`/`_mark_partial_close`/`_build_tracking_entry` | 不变 | — | 全部既有行为 |
| 删除 | `_cancel_orders_best_effort` | — | — | — |
| 修改 | `__init__` | 不新增 `algo_id_backfill_enabled` 读取（本轮无条件回填） | 禁硬编码 | 其余配置读取不变 |

### 11.2 `strategies/new_coin/strategy.py`

| 动作 | 对象 | 要点 | 关键不变式 |
|------|------|------|-----------|
| 修改 | `_guard_symbol` | `find_missing_protection(strict=True)`；`None`→零补挂+告警；非空→`replenish(..., missing=missing)`；失败→累计 attempts+告警 | `reset_replenish_flag` 保留；`entry_price<=0` 分支保留；`missing=[]` 不调用补挂 |
| 修改 | `_notify_protection_issue` | 文案含缺失类型列表、symbol、attempts | 降频窗口保留 |
| 新增（建议） | `_record_gap_and_alert(symbol, reasons)` | 抽取 attempts+1 且告警 | 控函数 ≤50 行、禁重复代码 |

### 11.3 `strategies/new_coin/config.yaml`

- `trading.replenish.cancel_after_ready`：更新注释为「启用增量补挂」语义。
- ⚠️ **不新增** `trading.replenish.algo_id_backfill_enabled`：回填改为增量挂单前「无条件 best-effort」（只 `setdefault` 填空缺、不覆盖），故不落该配置键（Decision H 批注定案）。

### 11.4 测试文件

- 迁移 §9.1.A（4 个语义冲突）+ §9.1.B（9 个机械补参）+ 删除 §9.1.C（1 个）。
- 新增 §9.2 用例。

### 11.5 全局约束（强制）

- 禁硬编码：新增开关/阈值均来自 `config.yaml`。
- 禁重复代码：SL/TP 挂单复用 `_place_conditional_and_record`；告警复用 `_record_gap_and_alert`。
- 禁幽灵参数：`missing` 在增量分支被真实使用（回退分支忽略属正常编排分支）。
- 单函数 ≤50 行、单行 ≤120 字符；注释与日志全中文。
- fail-closed：不确定不补挂、不裸奔。

---

**最后更新：** 2026-10-01（v1.0）
