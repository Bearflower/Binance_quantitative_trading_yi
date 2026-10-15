# 架构方案：new_coin 止盈检测跨档漏置位修复 + 移动止损保护范围再评估

## 1. 文档信息

| 项 | 内容 |
|----|------|
| 文档名称 | 架构方案：new_coin 止盈检测跨档漏置位修复 + 移动止损保护范围再评估 |
| 版本 | v1.2（A 段二次修订，已闭环 B1~B4 + 复审 B5，见 §16「A-review 阻塞项修订记录」） |
| 作者 | backend-architect（DeepSeek-V4-Pro） |
| 上游需求 | `docs/plans/fix-2026-10-15-newcoin-tp2-miss-and-trailing-scope-requirements.md` |
| 下游环节 | A-review（R3 设计复核）→ python-engineer（B 段实现） |
| 风险级别 | R3（下单/撤单 + 持仓归属 + 风控 + 资金精度，见需求 §4） |
| 本轮边界 | 只出架构方案，不写实现代码、不部署、不 push、不跑测试 |

**AC 映射总表**（详见各节）：

| AC | 结论/设计位置 | 本轮是否改码 |
|----|---------------|-------------|
| AC-A（核心修复） | §3 状态机 + 纯函数 `derive_reached_targets` | 是 |
| AC-B（移动止损前移评估） | §4 评估结论 + 配置项设计 | 否（只出结论，若实施另立） |
| AC-C（交易所可辨识性评估） | §5 评估结论 | 否（只出结论） |
| AC-D（condition_orders 状态同步） | §6 + §7 | 是（仅在 new_coin 内补调用点） |
| AC-E（RDW/MSTU/VKTX 尾仓处置） | §8 | 处置动作需负责人拍板 + 授权 |

---

## 2. 方案概述

### 2.1 一句话结论

核心 BUG 是 `detect_take_profit_fills`（[executor.py:L3127-L3171](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3127-L3171)）的**三处叠加缺陷**：先更新 `_last_tracked_qty` 再判级（丢失跨档信息）、每轮只推进一级并立即 `return`、用 `_last_tracked_qty` 的**轮间差值**而非**累计剩余比例**做判定。修复方向：把「持仓下降」的档位判定改为**用「初始开仓量」做锚点、用「当前剩余量占锚点比例 vs 配置档位比例」做累计判定**，一次性置位所有已跨越档位，并把该判定抽成**单一纯函数**供「运行时减仓检测」与「重启基线对账」两处复用（避免重复代码、避免重启后无法自愈）。

### 2.2 关键前提修正（本轮 B 段必须一并处理）

AC-A 的锚点方案依赖「初始开仓量」作为基线。已核实的两处事实：

1. 开仓路径：`_build_tracking_entry(entry_quantity=quantity)` + `_last_tracked_qty[symbol]=quantity`（[executor.py:L425-L432](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L425-L432)），此时 `entry_quantity` = 初始开仓量，正确。
2. **重启路径存在语义破坏**：`_sync_baseline_to_tracking` 里 `entry['entry_quantity'] = pos['quantity']`（[strategy.py:L2122](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/strategy.py#L2122)），而 `pos['quantity']` 来自 `rebuild_from_exchange` 的**交易所当前持仓量**（不是初始量）。即：**重启后 `entry_quantity` 会被覆盖为「当前剩余量」**，若直接拿它做锚点会得到完全错误的档位阈值（例如 RDW 重启后 `entry_quantity=2.17`，`锚点×0.7=1.52`，判定全部错乱）。

**结论**：AC-A 必须让「初始开仓量」在重启后依然可靠。推荐做法（最小改动）：
- 新增一个 tracking 字段 `tp_anchor_quantity`（初始开仓量，语义单一、不与现有 `entry_quantity`/`remaining_quantity` 纠缠），开仓时写入 `quantity`；重启基线重建时从 **`new_coin.short_positions.quantity`**（该列存初始开仓量，`_insert_short_position` 写 `quantity`，平仓只改 `status` 不覆盖 `quantity`）回填，且 **`_sync_baseline_to_tracking` 不得覆盖它**。
- `entry_quantity` 与 `remaining_quantity` 维持现状语义（`remaining_quantity` = 交易所当前量，重启初值 = 当前量）。

> 备选：直接修正 `_sync_baseline_to_tracking` 让 `entry_quantity` 保持初始量。但 `entry_quantity` 在 `_mark_partial_close`（[L3309](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3309)）等处被当作「入场数量」读取，语义改动面更广，风险更高。**推荐新增独立 `tp_anchor_quantity`，不动 `entry_quantity` 语义**。

---

## 3. AC-A 核心修复：跨档检测状态机

### 3.1 跨档判定依据的选择（AC-A4）

**选型**：以配置 `batch_take_profit.target1_close_percent` / `target2_close_percent`（已核实存在于 [config.yaml:L126-L132](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/config.yaml#L126-L132)，默认 0.30 / 0.40）+ 初始开仓量锚点，**计算各档「剩余量阈值」**。

**弃用「condition_orders 里的 TP 委托数量」**，理由：
- `condition_orders` 表结构（[condition_orders.py:L24-L35](file:///Users/yl/vscode/Binance_quantitative_trading/shared/condition_orders.py#L24-L35)）**没有 quantity/委托数量列**，只有 `algo_id/order_type/status`，无法反推每档委托量，除非再加列（扩大范围）。
- 档位比例本就是配置驱动（TP 委托量在 `_plan_take_profit` 里由 `self.target1_close_percent × total_quantity` 计算），配置是权威来源，无需二次引入交易所/DB 数据。

**容差来源与截断量化（B2 闭环）**：档位阈值容差 = `position_detection.qty_tolerance_ratio`(0.01) 的**相对项** + `qty_tolerance_absolute`(0.0001) 的**绝对项** + `step_size` 的**方向性截断项**（三来源均取自 [config.yaml:L135-L139](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/config.yaml#L135-L139) 或交易所精度，**零硬编码**）。

- `_format_quantity` 用 `ROUND_DOWN`（[executor.py:L1286](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L1286)）截断委托量，**只减不增** ⇒ TP 成交后交易所剩余量只可能**上浮**，最大上浮 = 已触发档位累计 `N × step_size`（TP1 档 N=1、TP1+TP2 档 N=2）。
- 因此「剩余量阈值」需向**上浮方向**放宽 `N × step_size`，否则大 step_size 小锚点时漏检（A-review B2 反例：锚点 1.05、step 0.1 → TP1=0.3、TP2=0.4、成交后剩 0.35；纯 1% 容差阈值 0.3256 < 0.35 漏检；纳入 `2×step=0.2` 后阈值 0.5256 ≥ 0.35，命中）。量化关系见 §3.2 公式。
- **不新增 config key**：`step_size` 已由 `_get_symbol_precision`（[executor.py:L3543](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3543)）实时读取，直接并入容差公式即可，无需为截断单设 `tier_tolerance_ratio`。

### 3.2 纯函数契约（写一次，两处复用）

为满足「禁止重复代码」并保证「运行时减仓」与「重启对账」判定一致，抽一个纯函数（放在 `strategies/new_coin/executor.py` 内，**不抽 shared**，理由见 §6.2 / §11）：

```
derive_reached_targets(
    *,
    anchor_qty: float,        # 初始开仓量（tp_anchor_quantity）
    exchange_qty: float,      # 交易所当前剩余量（绝对值）
    target1_close_percent: Decimal,
    target2_close_percent: Decimal,
    tolerance_ratio: float,   # qty_tolerance_ratio
    tolerance_absolute: float,# qty_tolerance_absolute
    step_size: float,         # 数量精度（ROUND_DOWN 截断项，见 §3.1）
) -> Tuple[bool, bool]
    # returns (target1_hit, target2_hit)，纯计算，无副作用，无状态
    # target1_hit = exchange_qty <= anchor_qty*(1 - target1_close_percent) + tol1
    # target2_hit = exchange_qty <= anchor_qty*(1 - target1_close_percent - target2_close_percent) + tol2
    #   tol1 = anchor_qty*tolerance_ratio + tolerance_absolute + 1*step_size
    #   tol2 = anchor_qty*tolerance_ratio + tolerance_absolute + 2*step_size
    # 方向说明：exchange_qty 为「剩余量」；ROUND_DOWN 使委托量只减不增 ⇒ 剩余只增不减，
    #   故容差须向上浮方向放宽 step_size×已触发档数，否则大 step 小锚点会漏检（A-review B2 反例）。
```

**设计要点（可证明性）**：
- **不漏检**：判定基于「累计剩余比例 vs 累计档位阈值」，不依赖「单轮减少量」。100%→30% 一次到位时，`exchange_qty≈0.30×anchor`，同时 ≤ `0.70×anchor+tol` 与 `0.30×anchor+tol`，`target1_hit=True`、`target2_hit=True`，一次置位两档。
- **不误判（分步不回归）**：100%→70% 时 `exchange_qty≈0.70×anchor`，只满足 target1 阈值（≤0.70+tol），不满足 target2（≤0.30+tol）→ `(True, False)`。
- **精度/手续费容差**：阈值比较带 `tol`，吸收 `step_size` 截断与币本位手续费带来的小幅偏差。
- **幂等**：置位动作仍由 `if not tracking.get('target1_reached')` / `if not tracking.get('target2_reached')` 守卫（在 detect 侧），纯函数本身无状态、天然幂等。
- **多币种独立**：纯函数入参均来自单 symbol 的 tracking/last 状态，天然隔离。
- **全部平仓语义**：`exchange_qty < zero_qty_threshold` 在 detect 侧**先于**减仓/档位判定处理，返回 0，保持既有语义（AC-A6）。
- **查询失败不推进**：`exchange_qty is None` 由调用方 `_check_take_profit_management` 的 `if exchange_qty is not None` 守卫（[L2039](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L2039)）拦截，不进入 `detect_take_profit_fills`（AC-A8）。

### 3.3 `detect_take_profit_fills` 重构伪代码（状态机）

```
def detect_take_profit_fills(symbol, exchange_qty, current_price=None):
    if not self.position_detection_enabled: return None
    tracking = self.position_tracking.get(symbol)
    if not tracking: return None

    anchor = _resolve_tier_anchor(symbol, tracking)   # 见 3.4，锚点解析
    last_qty = self._last_tracked_qty.get(symbol)
    if last_qty is None:
        self._last_tracked_qty[symbol] = exchange_qty
        return None

    # 全部平仓（优先）
    if exchange_qty < self.zero_qty_threshold:
        self._last_tracked_qty[symbol] = exchange_qty
        return 0

    # 无变化（复用容差，语义不变）
    if exchange_qty >= last_qty - max(last_qty*self.qty_tolerance_ratio, self.qty_tolerance_absolute):
        return None

    # 锚点缺失：保守 fail-closed，不置位、更新 last 后返回（避免误判）
    if anchor is None or anchor <= 0:
        log_warning("锚点缺失，跳过档位判定")
        self._last_tracked_qty[symbol] = exchange_qty
        return None

    # 纯量档位命中（B5 闭环：置位的唯一判据是「累计剩余比例 vs 档位阈值」，价格不参与）
    hit1, hit2 = derive_reached_targets(
        anchor_qty=anchor, exchange_qty=exchange_qty,
        target1_close_percent=self.target1_close_percent,
        target2_close_percent=self.target2_close_percent,
        tolerance_ratio=self.qty_tolerance_ratio,
        tolerance_absolute=self.qty_tolerance_absolute,
        step_size=self._step_size_for(symbol),
    )

    # 来源甄别（方案甲：量主导、价格仅作排除；档位命中直接复用 derive 返回值，单一容差来源）
    source = self._classify_fill_source(symbol, tracking, current_price, tier_hit=(hit1 or hit2))

    if source == "TP":
        # 有 TP 减仓：一次性置位（可能同轮置位 1+2，不逐级 return）
        reached = None
        if hit1 and not tracking.get('target1_reached'):
            self.update_target_status(symbol, 1, exchange_qty=exchange_qty)
            reached = 1
        if hit2 and not tracking.get('target2_reached'):
            self.update_target_status(symbol, 2, exchange_qty=exchange_qty)
            reached = 2
        # 置位后才更新 last（修复点：先置位再更锚，跨档信息不丢）
        self._last_tracked_qty[symbol] = exchange_qty
        return reached

    if source == "SL":
        # SL 部分成交：本次减仓已收敛，推进 last；撤剩余 OPEN TP 交由 3.5.2
        self._last_tracked_qty[symbol] = exchange_qty
        return self._handle_non_tp_reduction(symbol, tracking, "SL")

    # source == "UNKNOWN"：不推进 last，保留下轮重判（方案乙要点，防止「先推进 last →
    # 下轮被『无变化』分支拦截 → 永不重判」的次生漏检；防重复告警的重试上限见 3.5.2）
    return self._handle_non_tp_reduction(symbol, tracking, "UNKNOWN")
```

> 新增子函数说明（满足「单函数 ≤ 50 行」）：`_classify_fill_source`（减仓来源甄别，**接收 `derive_reached_targets` 的 `tier_hit` 返回值、仅做「价格是否越 SL 触发区」的排除**，见 §3.5.1）、`_handle_non_tp_reduction`（SL/手动减仓处置，见 §3.5.2）、`_step_size_for`（从 `_get_symbol_precision` 缓存取 step_size）。三者均从 `detect_take_profit_fills` 拆出，避免该函数膨胀。`current_price` 由 `check_position_management` 提前取一次价格传入（供 §3.5.1 甄别、并供后续 target2 门控复用同一取价，**不新增重复取价**）。

**关键修复点对照**：

| 原缺陷 | 修复 |
|--------|------|
| 先 `_last_tracked_qty = exchange_qty` 再判级（[L3159](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3159)） | 先置位（可能置位 1+2 两档），最后统一更新 `_last_tracked_qty` |
| `if not target1: ... return 1` / `if not target2: ... return 2` 逐级 return（[L3163-L3170](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3163-L3170)） | 一次算 `(hit1, hit2)`，两个 `if` 顺序执行、互不 return，可同轮置位两档 |
| 依赖「轮间差值」推进 | 依赖「累计剩余比例 vs 档位阈值」，天然幂等、自愈 |

**激活时序（AC-A1 的衔接）**：`detect_take_profit_fills` 在 `_check_take_profit_management` 中先执行（[L2040](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L2040)），置位 `target2_reached` 后，紧接的 `if tracking.get('target2_reached')`（[L2055](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L2055)）**同一轮即可读到 True** 并触发移动止损激活路径，比原来「下一轮才激活」更及时。

### 3.4 锚点解析 `_resolve_tier_anchor`（含重启一致性）

```
def _resolve_tier_anchor(symbol, tracking):
    # 1. 优先读 tracking 里的 tp_anchor_quantity（开仓/基线回填写入）
    anchor = tracking.get('tp_anchor_quantity')
    if anchor is not None and anchor > 0: return float(anchor)
    # 2. 兼容回退：老进度条无该字段，退化用 entry_quantity（仅开仓路径可靠）
    entry = tracking.get('entry_quantity')
    if entry is not None and entry > 0:
        log_warning("tp_anchor_quantity 缺失，退化用 entry_quantity", symbol=symbol)
        return float(entry)
    return None   # fail-closed
```

**必需配套改动（B 段）**：
1. `_build_tracking_entry` 新增字段 `'tp_anchor_quantity': quantity`（[L3228-L3244](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3228-L3244)）。
2. `_sync_baseline_to_tracking`（[strategy.py:L2110-L2127](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/strategy.py#L2110-L2127)）在重建后：**从 `short_positions.quantity` 回填 `tp_anchor_quantity`**，且不覆盖；`entry_quantity`/`remaining_quantity` 维持「当前量」语义不动。已核实 `_insert_short_position` 写初始量（[executor.py:L1431-L1443](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L1431-L1443)）、`_update_short_position_closed` 只改 status/closed_at 不覆盖 quantity（[executor.py:L1474-L1484](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L1474-L1484)），锚点回填来源成立（原 T2 已闭环）。
3. 基线重建后做一次**档位回填自愈**：用 `derive_reached_targets(anchor=初始量, exchange_qty=当前量, ...)`（含 `step_size`）回填 `target1_reached`/`target2_reached` —— 这解决「存量 RDW 已在 30% 但 target2 未置位、之后无新减仓事件」这一 `detect` 无法覆盖的自愈盲区（AC-E 的「策略自动接管」依赖此步）。**回填前先 `await _get_symbol_precision(symbol)` 预热 `_precision_cache`**（[executor.py:L89](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L89)、[L1223-L1224](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L1223-L1224)），避免重启后缓存清空落入 `default_step_size` 兜底使 `tol` 偏小、回到漏检方向（A-review 复审次级提醒）。
4. **补挂组单基准锚定「剩余目标」（B2 闭环，关键）**：`_build_replenish_plans` 现状以**当前量**为基数组单（[executor.py:L3527](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3527)、[L3556-L3567](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3556-L3567)：`close_percent × quantity`），导致重启补挂后 TP 委托量 = 当前量×比例、而非锚点×比例，破坏 §3.2 锚点判定的前提（A-review B2 反例）。**改为档位感知组单**：以 `tp_anchor_quantity` 为锚，TP 委托量 = `max(当前量 − 该档之后的「剩余目标量」, 0)`，其中剩余目标 = `锚点 × (1 − 已跨越档累计比例)`。即：
   - 两档未达成（target1=False,target2=False）：TP1 = 锚点×30%、TP2 = 锚点×40%（等价现状，因当前量≈锚点）；
   - 仅 TP1 已达成（target1=True,target2=False）：**只挂 TP2**，委托量 = `当前量 − 锚点×30%`，保证成交后恰降到 30% 尾仓，锚点判定 `exchange_qty ≤ 锚点×30%+tol2` 成立，消除反例；
   - 两档均达成（target2=True）：**不挂 TP**（此模块决策同时服务 AC-D 守卫判定，见 §6.4）。
   > 该基准修正与 §6.4「守卫按 target 状态感知」共享同一入口：补挂前必须读 `position_tracking[symbol]` 的 `tp_anchor_quantity`/`target1_reached`/`target2_reached`。`_plan_take_profit` 内 skip 分支（名义价值不足 L3505-L3509、现价已过目标 L3501-L3503）语义不变，仅当其入参 `quantity` 改为「档位感知后的剩余目标差额」。
5. **锚点回填边界（T8 定稿）**：同 symbol 存在多条 `status='open'` 的 `short_positions` 记录（历史重复开仓的异常序列）时，取 **`ORDER BY opened_at DESC LIMIT 1`**（最新一条 open）作为锚点来源，命中多条时打 warning；open 记录缺失（已被全关但 tracking 仍在）则 `tp_anchor_quantity` 留空 → `_resolve_tier_anchor` 退化 `entry_quantity` / 返回 None（fail-closed，见 §3.5）。

### 3.5 异常与边界处理策略

- 锚点缺失（重启后 `short_positions` 查询失败、老条目无该字段）：`_resolve_tier_anchor` 返回 None → `detect` 更新 last 后返回 None，**不置位、不改 remaining**（fail-closed，宁可暂不激活移动止损也不误判成交缩仓）。
- 档位阈值配置缺失：`target1_close_percent`/`target2_close_percent` 在 `__init__` 已有默认值（[L143-L145](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L143-L145)），缺省仍 0.30/0.40，不报错但记录 warning。

#### 3.5.1 减仓来源甄别（B3 + B5 闭环，方案甲：量主导、价格仅作排除）

「持仓数量下降」本身无法区分是 TP 成交、SL 部分成交还是手动平仓。原 v1.1 设计把「`current_price ≤` TP 目标价区」作为判 TP 的**必需**条件（AND），在「TP 成交后价格反弹出 TP 区」这一新币常见场景会把真实 TP 误判为 UNKNOWN，且 §3.3 UNKNOWN 分支先推进 `_last_tracked_qty` → 下一轮被「无变化」分支拦截 → **永不重判，target2 永久漏置位**（A-review B5，与 RDW 原 BUG 同类资金风险）。

**方案甲（选定）——量主导、价格仅作排除**：置位的充分条件是「减仓量命中档位累计比例」（续用 `derive_reached_targets` 同一容差，**单一来源**），**不再要求价格进入 TP 区**；价格只用于「`current_price ≥ SL 触发区`」这一**排除**信号（封死 B3 的 SL 部分成交误置位）。纯函数 `_classify_fill_source(symbol, tracking, current_price, tier_hit) -> "TP" | "SL" | "UNKNOWN"`，其中 `tier_hit` 直接来自 `derive_reached_targets` 返回值（**不另设第二套甄别容差**）：

| 场景（做空方向） | 判据 | 结论 | 后果 |
|------|------|------|------|
| TP 成交（含价格反弹出 TP 区） | `tier_hit=True` 且 `current_price < SL 触发价`（价格在 SL 区下方即可：TP 区、**中间地带**均算） | `"TP"` | 置位已命中档 + `mark_order_executed`（见 §3.3） |
| SL 部分成交 | `current_price ≥` SL 触发价（`entry_price` 上方，`stop_loss_percent` 或紧急止损价）——**无条件排除置位**，无论 `tier_hit` 真假 | `"SL"` | 不置位 target、不 mark TP；转 `_handle_non_tp_reduction` |
| 手动/强平/不明减仓 | `tier_hit=False` 且 `current_price < SL 触发价`（减仓但未跨档、亦非 SL 区） | `"UNKNOWN"` | 不置位、不 mark、不推进 last（方案乙要点），告警并保留重判 |

**关键证明（B5 闭环）**：「TP 成交后价格反弹出 TP 区」是判定正确性的关键场景：TP 是 STOP 单，触发点价格必 ≤ 触发价，但 detect 轮询到点时价格可能已反弹。方案甲下，只要 `current_price` 仍低于 SL 触发价（落在 TP 区～中间地带），`tier_hit`（仍为 True，因为 `exchange_qty` 已跨档）即判 TP → target2 置位，**不受价格反弹影响**。仅当价格**极端反弹越过 SL 触发价**才会误入 SL 分支——见 §3.5.2 残余风险论证（固定止损仍兜底，非裸奔）。

**单一容差来源（B3 次级矛盾闭环）**：甄别的量判据不新造容差，而是**直接复用 `derive_reached_targets` 的 `(hit1, hit2)` 返回值**作 `tier_hit`，与 §3.2 的 `tol1/tol2` 完全同源，从根上消除「甄别=TP 但 hit=False」（白撤白告警）与「甄别=UNKNOWN 但量已匹配」（漏检）的错位。

**甄别依据全部来自配置或算法**：TP 目标价由 `batch_take_profit.target1/2_atr_multiplier`（[config.yaml:L128-L130](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/config.yaml#L128-L130)）与 `tracking['entry_price']/atr` 计算；SL 触发价由 `stop_loss_percent` 等既有关键推导；`tier_hit` 复用 `derive_reached_targets` 的同套配置，**零硬编码、零第二套容差**。

#### 3.5.2 非 TP 减仓处置 `_handle_non_tp_reduction`（状态转移 + 失败处理）

- `"SL"`：做空 SL 部分成交，剩余仓位仍在、但原 TP1/TP2 单的方向预期已破坏 → **异步撤除剩余 OPEN 的 TAKE_PROFIT（按 symbol）**，并 `mark_open_orders_canceled(db, "new_coin", symbol)`（复用 [condition_orders.py:L171](file:///Users/yl/vscode/Binance_quantitative_trading/shared/condition_orders.py#L171)）同步 DB；**不调用 `mark_order_executed`**（避免把未成交 TP 误标 EXECUTED，反向不同步）。随后让既有止损逻辑接管剩余仓位。此路径**异步不阻塞** detect 主流程，失败告警、下轮重试。**SL 分支已在 §3.3 先推进 `_last_tracked_qty`**（表示「本次减仓已收敛」），避免下轮重复触发 SL 处置与告警。
- `"UNKNOWN"`：**不置位 target、不 mark、不撤单**（避免误操作），仅 `log_warning("减仓来源不明，保留重判")` + 飞书告警。**关键（B5 闭环 + 方案乙要点）**：UNKNOWN 分支**不推进 `_last_tracked_qty`**，使下一轮仍能检测到「`exchange_qty < last_qty - tol`」继续重判——一旦价格回落出 SL 区（或量跨档），即按 TP/SL 收敛，**不因「先推进 last」而被「无变化」分支永久吞掉**。为防「价格长期停在中间地带、量又不跨档」导致的重复告警死循环，设**重试上限**：同一 symbol 连续 `reconcile.zombie_confirm_cycles`（[config.yaml:L122-L123](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/config.yaml#L122-L123)，默认 3）轮判定 UNKNOWN 且 `_last_tracked_qty` 未推进时，**第 N+1 轮强制推进 `_last_tracked_qty` 并升级为 critical 飞书告警**（一次性记录，不再刷屏），交由 `reconcile` 僵尸对账兜底。否决「无条件置位」的另一选项——其会误触发移动止损并把在挂 TP 标 EXECUTED，风险不可接受。
- 两种来源都不改变「全平路径」：数量 `< zero_qty_threshold` 仍优先返回 0（AC-A6），由上层清理。

**B5 残余风险接受声明（方案甲的边界）**：方案甲保证「价格反弹出 TP 区（中间地带）」这一**常见**场景不漏判；唯一残余是「`tier_hit=True` 的同时价格越过 SL 触发价」。即便此极端场景被误入 SL 分支，其后果也**不是裸奔**：TP 单本已成交（撤单幂等无害）；固定止损 `stop_loss_percent` 单仍在挂时，价格越过 SL 区即触发 SL 平掉剩余仓（有保护）。仅「固定 SL 单也已失效」且「移动止损因 target2 未置位而未激活」双重叠加才构成短暂保护缺口，靠 §3.5.2 的 critical 告警 + `reconcile` 对账兜底。此残余与「先推进 last 导致永久裸奔」的 B5 原风险**性质不同**（后者无任何兜底、且必现），故接受。

> 设计底线（对齐复核结论）：**置位 target 与 `mark_order_executed` 只发生在「来源 = TP」分支内**；任何非 TP 减仓不得进入置位/mark 链。这一底线 + 方案甲「量主导」共同闭合 B3 与 B5：漏置位方向被「量主导 + UNKNOWN 不推进 last」封死，误置位方向由「SL 触发区排除 + 固定止损兜底」兜住，二者均不再产生「尾仓永久裸奔」。

---

## 4. AC-B 移动止损是否前移到 TP2 之前生效（评估类，本轮只出结论）

### 4.1 推荐结论

**不前移（继续保持「TP2 之后才激活」为默认行为），但把「是否前移」做成真实可配置——消除当前「shared 层已支持 `also_on_tp1`/`min_profit_pct`，却被 executor 层 `target2_reached` 硬门控架空」的设计矛盾。**

### 4.2 四个维度的量化论证

| 维度 | 论证 |
|------|------|
| 保护缺口期风险 | 从开仓到 TP2 之间的敞口并非「裸奔」：固定止损 `stop_loss_percent=5%`、紧急止损（15min/1.5%，[config.yaml:L186-L190](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/config.yaml#L186-L190)）、时间止损 `max_holding_hours=72` + `time_stop_review` 多道兜底；且 TP1 成交即减仓 30%，缺口期内的最大敞口已被削至 70%。`dynamic_trailing` 只是其中最后一道「利润保护」，其缺席不改变「有止损」的事实 |
| 误触发/来回打脸成本 | new_coin 属新币高波动，TP1 前浮盈尚浅（`min_profit_pct=1.5%` 附近），此时前移会因正常反弹频繁「撤旧挂新」、并在浅浮盈阶段触发平仓，损失后续利润，且新币滑点/点差放大来回成本 |
| 与 `_check_trailing_stop` 的关系 | `_check_trailing_stop`（ATR 反弹移动止盈，[L2436](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L2436)）与 `_check_dynamic_trailing`（动态利润保护）是**两套独立机制**。前者本质是「尾仓保护」，应保持 TP2 后启用；后者才承载「可配置的利润保护」，若前移会和前者在浅浮盈阶段重叠、冲突 |
| 风险/收益 | 保护收益有限（已有三道止损兜底），误触发成本对高波动新币更可感知 → **收益不覆盖成本，结论不前移** |

### 4.3 配置项设计（供后续拍板后一键切换，本轮不改码）

- **复用** `trading.dynamic_trailing.activation` 的既有三个 key（已是真实存在的配置，[config.yaml:L200-L203](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/config.yaml#L200-L203)）：
  - `min_profit_pct`（float，默认 1.5）
  - `also_on_tp1`（bool，默认 false）
  - `also_on_tp2`（bool，默认 true）
- **重构门控（若后续实施）**：把 `_check_take_profit_management` 里 `if tracking.get('target2_reached')` 这一统一门控拆开：
  - `_check_dynamic_trailing` 的门控 = `shared.calculate_dynamic_trailing_stop` 自身的 activation 判定（`also_on_tp1 ∧ tp1_hit` ∨ `also_on_tp2 ∧ tp2_hit` ∨ `min_profit_pct` 达标），不再用 `target2_reached` 硬挡。
  - `_check_trailing_stop` 保持 `target2_reached` 门控不变（它不含 activation 语义）。
- **new_coin 与 HRS 是否分别配置**：是。两策略各自独立配置（new_coin 用 `dynamic_trailing.activation`，HRS 用 `trading.trailing` + `position_manager.check_trailing_stop` 的 target2 语义），**不共享、不互相影响**。HRS 本轮不改。

### 4.4 两个备选方案利弊

| 方案 | 利 | 弊 |
|------|-----|-----|
| A：全面前移到 `min_profit_pct`/TP1 | 保护缺口最小 | 浅浮盈高波动新币误触发频繁、与 `_check_trailing_stop` 重叠、实施面大 |
| B：完全维持现状（仅 TP2，含硬门控） | 零改动 | `also_on_tp1`/`min_profit_pct` 配置形同虚设、语义被架空，后续想调需改码 |
| **C（推荐）：默认不前移 + 门控去硬编码化** | 默认行为不变、可配置一键切换、消除矛盾 | 需一次小幅重构 executor 门控（本轮只设计，不改码） |

### 4.5 结论与剩余风险声明

**默认不前移**；缺口期剩余风险 = 「TP2 之前的利润回撤无法被动态利润保护锁定，仅由固定/紧急/时间止损兜底」，属交易保护偏好，**最终由交易负责人拍板**（需求 §8.1）。若后续拍板前移，仅需把 `also_on_tp1` 改 true（配合 4.3 的门控重构），不需改业务代码。

---

## 5. AC-C 交易所端可辨识性（评估类，本轮只出结论）

### 5.1 两条候选路径可行性

| 路径 | 可行性 | 关键证据/风险 |
|------|--------|---------------|
| 原生 `TRAILING_STOP_MARKET` | 需单独立项，本轮不做 | 要改下单语义：原生追踪单触发价是「回调比例」，而 new_coin/HRS 的移动止损是「价格点位」（ATR 反弹、动态利润保护算出具体价位），语义不直接匹配；且「撤旧挂新」的数量/精度管理、PM 账户是否支持该类型均需重测。[待核实] PM 账户对 `TRAILING_STOP_MARKET` 的支持情况（T3） |
| `order_type` 区分 `TRAILING_STOP`（保留轮询撤挂） | 低风险，推荐 | 只把移动止损那张 condition_orders 记录的 `order_type` 从 `STOP_LOSS` 改为 `TRAILING_STOP`，交易机制零变化 |

### 5.2 推荐结论

**推荐路径 2（`order_type` 标记 `TRAILING_STOP`）**，但需澄清一个关键事实：**`order_type` 标记解决的是「我们自己的 DB/看板/审计能辨识」，不能解决「币安 UI 显示追踪图标」**。币安 UI 上普通 STOP 条件单本就没有独立「追踪」标识，若用户的诉求是「在币安 UI 看到追踪单」，只有原生 `TRAILING_STOP_MARKET` 能做到（另立需求）。

### 5.3 兼容性与影响（AC-C2，B4 修正）

> **修正**（A-review B4）：§5.3 原称「下游不按 `order_type` 白名单过滤」是错的。**`find_missing_protection` 恰按 `order_type` 字面白名单判定**（[executor.py:L1045-L1047](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L1045-L1047)：`"STOP_LOSS" not in open_types` → 缺止损单、`"TAKE_PROFIT" not in open_types` → 缺止盈单）。故必须逐消费方重述真实影响：

| 消费方 | 是否按 `order_type` 过滤 | 新增 `TRAILING_STOP` 的真实影响 |
|--------|--------------------------|-------------------------------|
| **replenish 守卫** `find_missing_protection` | **是（字面白名单）** | **硬冲突**：移动止损单若改记 `TRAILING_STOP`，`"STOP_LOSS" not in open_types` 恒成立 → 守卫每周期判「缺止损单」→ 每周期补挂 SL。**AC-C 落地前必须同步把 `TRAILING_STOP` 归入「止损类存在」判定（或将止损类判定改为『STOP_LOSS 或 TRAILING_STOP 任一存在』），否则禁止落地 order_type 改造** |
| 看板 dashboard | 仅展示映射 | 需增 `TRAILING_STOP → 移动止损` 枚举映射，否则显示为未知类型 |
| 审计/对账 reconcile | 以 `status`/`strategy_name`/`symbol` 为主，是否另行按 `order_type` 需逐点核对 | 落地前作为显式前置检查项（本轮 AC-C 不改码、不落地） |
| 孤儿清理 orphan_cleanup | 按 `status='OPEN'`+ 时间窗为主 | 影响低，但需确认清理口径不会因新枚举名漏判 |

- 历史数据：**不回溯改写**，历史 `STOP_LOSS` 保留原值（历史新币移动止损单因 `order_type` 恒为 `STOP_LOSS` 不可再区分，接受此历史口径）。
- 跨策略一致性：HRS `position_manager._record_condition_order` 与 btc_eth 仍记 `STOP_LOSS`，本轮不动 → 短期内仅 new_coin 区分 `TRAILING_STOP`。是否推广到 HRS/btc_eth 另立需求（需求 §8.4）。
- **本轮边界重申**：AC-C 全项为评估类，本轮**不落地 `order_type` 改造**（§5.4），故 B4 的守卫冲突本轮**不会**被触发；但其作为后续实施的前置条件已在此显式登记，避免实施时埋雷。

### 5.4 本轮边界（AC-C3）

**只出评估结论，不落地任何交易所原生单改造、不落地 `order_type` 改造**（AC-C 全项为评估类）。

---

## 6. AC-D `condition_orders` 状态同步

### 6.1 现状核实

- shared 层**已有**完整的状态同步函数：`mark_order_canceled` / `mark_order_executed` / `mark_open_orders_canceled`（[condition_orders.py:L123-L206](file:///Users/yl/vscode/Binance_quantitative_trading/shared/condition_orders.py#L123-L206)）。
- HRS 已接入：`position_manager._sync_db_canceled` / `_cancel_individual_orders` 调用上述函数（[position_manager.py:L342-L366](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py#L342-L366)、[L508-L585](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/position_manager.py#L508-L585)）。
- **new_coin 只 import 了 `record_condition_order`（[executor.py:L17](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L17)），从不调用 `mark_*`** → TP 成交、SL 撤单后 DB 记录长期滞留 `OPEN`（附带 BUG②）。

### 6.2 是否抽 `shared/` 的取舍（AC-D 关键决策）

**推荐结论：不新抽 shared，复用 `shared/condition_orders.py` 里已存在的 `mark_*` 函数，仅在 new_coin 内补调用点。**

**理由**：
- `mark_order_executed` / `mark_order_canceled` / `mark_open_orders_canceled` **已经存在于 shared**，new_coin 只需 `import` 并调用，**不改 shared 代码**。因此不触发「shared 变更 → 全部策略容器重建」。
- 若把「TP 成交检测 → 关单」这段逻辑抽成新的 shared 模块（例如给 HRS 也用），则必然触碰 `shared/` + 需同步改 HRS（本轮非目标），触发全量重建，风险与范围远超本紧急资金安全修复的必要范围。
- 结论：**本轮只在 `strategies/new_coin/` 内做最小修复**，仅重建 `trading-new-coin` 一个容器。

### 6.3 需补的调用点（B 段实现清单）

| 事件 | 现有位置（new_coin） | 补调 shared 函数 |
|------|----------------------|------------------|
| TP1/TP2 成交 | `detect_take_profit_fills` 置位 target1/target2 后 | `mark_order_executed`（按对应 TP 的 algo_id） |
| 撤「旧移动止损单」 | `_sync_trailing_stop_order` 撤 `old_trailing_id`（[L2566-L2590](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L2566-L2590)） | `mark_order_canceled`（按 old_trailing_id） |
| 发射移动止损替代硬止损 SL | `_sync_trailing_stop_order` 撤 `old_sl_id`（[L2593-L2610](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L2593-L2610)） | `mark_order_canceled`（按 old_sl_id） |
| 平仓触发撤移动止损 | `_cancel_trailing_stop_order`（[L2512-L2539](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L2512-L2539)） | `mark_order_canceled`（按 old_id） |
| 全量撤单（replenish/清场） | `_cancel_orders_strict` / `cancel_all_algo_orders` 成功路径 | `mark_open_orders_canceled(db, "new_coin", symbol)`（对齐 HRS 的 `_sync_db_canceled` 语义） |

**约束**：`mark_*` 均为「尽力同步、失败只告警不阻断主流程」（与 shared 现有约定一致），但**幂等**（其 SQL 已加 `AND status='OPEN'`，重复调用不产生副作用）。满足 AC-D3「主动、幂等同步，不依赖被动兜底」。

### 6.4 AC-D 与 replenish 守卫的联动（B1 闭环，核心）

**问题链（已核实）**：AC-D 修复后，TP 成交 → `mark_order_executed` 关闭该 symbol 的 `TAKE_PROFIT` 记录 → 守卫 `find_missing_protection` 发现 `"TAKE_PROFIT" not in open_types`（[executor.py:L1047](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L1047)）→ 判「缺止盈单」→ `require_take_profit: true`（[config.yaml:L116](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/config.yaml#L116)）→ `_guard_symbol` 调 `replenish_conditional_orders`（[strategy.py:L1238-L1240](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/strategy.py#L1238-L1240)）对已达成 TP2 的尾仓按「当前量×30%/40%」重复补挂——**尾仓处置语义被静默改变（资金行为变更）**。现状未发生恰因附带 BUG② 的 OPEN 滞留「冒充」保护；修 AC-D 会打开这条新路径。

**修正语义（守卫必须感知 target 状态）**：`find_missing_protection` 的「缺止盈单」判定不再是「有无 OPEN `TAKE_PROFIT` 白名单」，而是**以 `position_tracking[symbol]` 的 `target1_reached`/`target2_reached` 为权威档位状态**：

| 档位状态 | 「缺止盈单」判定 | 守卫动作 |
|---------|----------------|---------|
| `target2_reached=True`（尾仓 30%） | **不要求 TP**（等同于 `require_take_profit=false`，无论 config 取值） | 只核验止损类单（STOP_LOSS / TRAILING_STOP 任一存在，见 §5.3 B4） |
| `target1_reached=True` 且 `target2_reached=False`（剩 70%） | 仅要求 TP 第二档 | 缺失时**只补挂 TP2**（委托量 = 当前量 − 锚点×30%，见 §3.4 第4点），不重复挂 TP1 |
| 两档均 `False` | 维持 `require_take_profit` 语义 | 缺失时补挂 TP1+TP2 |

**tracking 缺失 / target 状态不可知时的守卫口径（A-review 复审补）**：当 `position_tracking` 尚无该 symbol 条目（条目被清理、进程刚启动尚未基线回填）或 target 状态缺失时，`find_missing_protection` **视同「两档均未达成」并打 warning**——即维持 `require_take_profit` 原语义（要求补挂 TP1+TP2），fail 方向与现状一致（宁可多挂保护，不因状态不可知而漏保护）。该口径与 §3.4 第 3 点「基线回填自愈」先后衔接：回填完成后 target 状态变为权威，守卫切换为按档判定。

**实现方式（最小改动，均在 new_coin 内，不触 shared）**：
1. `find_missing_protection` 新增档位上下文入参（读 `self.position_tracking[symbol]` 的 target 状态），或新增包装方法 `find_missing_protection_with_tier(symbol, target1_reached, target2_reached, ...)` 复用原查询 SQL、仅改「是否要求 TP」的分支。
2. `replenish_conditional_orders` 支持**按档补挂**（新增 tier 入参），复用 §3.4 第4点的「档位感知组单」；不再无条件重建 TP1+TP2。
3. `strategy._guard_symbol` 调用 `find_missing_protection` 前，从 `executor.position_tracking` 传入 target 状态（它与 `db_position` 均已在入参可及范围内）。

**与 `config.yaml:L116 require_take_profit` 的关系**：该开关仍是「**未达成档位**是否要求止盈单」的全局默认；**档位已达成后的语义由 target 状态覆盖**（target2 达成即强制不要求 TP），二者不冲突——`require_take_profit` 只作用于「两档均未达成」与「仅 TP1 达成」的缺单判定。实现者不得把 `require_take_profit=true` 理解为「永远要求 OPEN TP」。

> 该联动同时封死「mark_executed 关闭记录后尾仓被重复平 70%」的资金行为变更，是 AC-D 得以安全落地的前提；测试用例见 §9.3「mark→守卫集成」。

---

## 7. 附带 BUG：`condition_orders` 状态同步（同 AC-D）

与 §6 完全同源，合并处理：new_coin 补 `mark_order_executed` / `mark_order_canceled` / `mark_open_orders_canceled` 调用点即可闭合附带 BUG②。验收见 AC-D4。

---

## 8. AC-E RDW（及待确认的 MSTU/VKTX）尾仓限期处置

### 8.1 处置结论（推荐：过渡保护 + 快速接管）

**推荐组合方案**：

1. **立即人工/脚本补挂保护单（过渡）**：给 `RDWUSDT -2.17@9.07` 补一张 STOP 条件单（价格可由负责人给定，或用「入场价×（1+止损百分比）」临时口径），**限时到 AC-A 修复上线**。
2. **修复上线后由策略接管**：AC-A 修复部署后，vagas 基线重建触发「档位回填自愈」（§3.4 第 3 点），自动识别 RDW 已 TP2 并激活移动止损，随后由策略接管保护。
3. **MSTU/VKTX 一并纳入**：先「只读」核对二者交易所是否仍持仓（需求 §8.6 待确认，T5），若仍持仓且 TP 已成交则同样先补过渡保护、再接管；若无持仓则无需处置。

**时限**：过渡补单应在**本文件评审通过后一个工作日内**完成；策略接管依赖 AC-A 通过 A-review → B 实现 → 部署。

### 8.2 交接前后「保护不中断」设计（关键，T6 已定稿）

人工过渡单与策略接管不得冲突/重复挂单，此节为**定稿语义**（原 §14「T6 待核实」已闭环）：

- **过渡单标识（定稿）**：人工补单落库 `condition_orders` 时 `order_type` 记为 **`MANUAL_STOP`**（独立枚举，不与 `STOP_LOSS`/`TRAILING_STOP` 混淆，本轮 AC-C 不改码、仅此处置动作临时使用），`clientOrderId` 用 `manual_guard_<symbol>_<ts>` 前缀，做到可识别、可追溯、可撤。
- **守卫对过渡单的判定（定稿，与 §6.4 同源）**：`find_missing_protection` 的「止损类存在」判定扩展为「`STOP_LOSS` 或 `TRAILING_STOP` 或 `MANUAL_STOP` 任一存在即视为止损保护已存在」。因此过渡期内仅有 `MANUAL_STOP` 时，守卫**不会**因「STOP_LOSS 不在白名单」而重复补挂 SL——从根上消除「人工单被误判为缺保护」的冲突。
- **接管时序（定稿，保护不中断）**：策略在「档位回填自愈」（§3.4 第3点，识别 target2 已达成）后、激活移动止损时，执行**「先挂策略移动止损 → 挂成功后撤 `MANUAL_STOP`」**（而非先撤后挂）：
  1. 首轮 `_sync_trailing_stop_order` 先按正常流程挂策略自己的移动止损（写入 `condition_orders`，`order_type=STOP_LOSS`，见现状 [executor.py:L2664](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L2664) 附近 record 调用）；
  2. 挂单成功后，识别并撤除 `MANUAL_STOP`（复用 `mark_order_canceled` 关单 + `cancel_algo_order` 撤交易所单）；
  3. **若挂单失败 → 保留 `MANUAL_STOP` 不撤**（仍是有效兜底），告警并下轮重试——保证任何时刻至少一张止损类单在挂，无空窗。
- **与 B1/B3 联动**：该「先挂后撤」顺序与 §6.4「守卫按 target 状态感知」、§3.5.2「非 TP 减仓不 mark TP」共同构成「保护不中断 + 不误标 + 不重复挂」的完整闭环；测试用例见 §9.3「人工过渡单共存」。

### 8.3 监控/告警要求（AC-E2）

处置落地前，对 RDW（及确认后的 MSTU/VKTX）尾仓加监控：
- 复用 `emergency_stop` 的反向波动阈值（`trigger_percent=1.5%`）做**本地日志告警 + 飞书告警**：价格较开仓价反向（做空为上涨）超阈值即告警。
- 告警降频复用 `alert_throttle_seconds` 这类现有节流，避免刷屏。

### 8.4 验收口径（AC-E3）

处置落地后，在交易所 + DB 只读核对：
- RDW 要么已平仓清零（`short_positions.status` 非 open、交易所无持仓），要么存在一张有效 STOP/移动止损保护单（`condition_orders` 有对应 OPEN/TRAILING 记录）；两者必居其一，不得出现「有持仓、无保护单」。

---

## 9. 测试计划（A 段锁定）

### 9.1 测试环境与命令

- 环境：本地执行（[禁止服务器回测](file:///Users/yl/vscode/Binance_quantitative_trading/.trae/rules/coding-standards.md)），工作目录 `/Users/yl/vscode/Binance_quantitative_trading`，Python 3.11（与 new_coin Dockerfile 一致），`unittest.mock` 替身币安 API 与 DB，不触达交易所。
- 运行命令（B 段执行，[待核实] pytest-cov 是否已装入本地环境，T4）：

```bash
cd /Users/yl/vscode/Binance_quantitative_trading

# 1) AC-A 核心回归（先失败后通过，见 9.3）
python -m pytest strategies/new_coin/tests/test_take_profit_fill_detection.py -v

# 2) AC-D 状态同步
python -m pytest strategies/new_coin/tests/test_condition_orders_sync.py -v

# 3) new_coin 全量回归 + 核心变更覆盖率
python -m pytest strategies/new_coin/tests/ \
  --cov=strategies.new_coin.executor --cov-report=term-missing -v
```

### 9.2 测试文件与覆盖门槛

| 测试文件（新增） | 覆盖对象 | 门槛 |
|------------------|----------|------|
| `strategies/new_coin/tests/test_take_profit_fill_detection.py` | `detect_take_profit_fills` + `derive_reached_targets` + `_resolve_tier_anchor` + `_sync_baseline_to_tracking` 档位回填分支 | 核心变更**行/分支 100%** |
| `strategies/new_coin/tests/test_condition_orders_sync.py` | §6.3 各 `mark_*` 调用点（成交/撤单/清场） | 同上 100%；历史无关缺口单列报告 |

> 说明：核心变更指 AC-A/AC-D 新增与修改的具体函数；`executor.py` 模块整体的历史欠覆盖单独报告，不混为一个百分比。

### 9.3 能暴露原 BUG 的回归用例（先失败后通过）

| 用例 | 断言 | 修复前 | 修复后 |
|------|------|--------|--------|
| `test_cross_tier_single_poll_levels_both`（AC-A1） | 单轮 `100%→30%`，断言 `tracking['target1_reached'] is True` 且 `tracking['target2_reached'] is True`，且日志同时出现「第一目标已达成」「第二目标已达成」 | 失败（仅 target1 置位，target2 漏） | 通过 |
| `test_stepwise_two_levels_no_backfill`（AC-A2） | `100%→70%` 仅 `target1_reached`（`target2_reached` False、无第二目标日志）；下一轮 `70%→30%` 才 `target2_reached` | 通过（不回归） | 通过 |
| `test_tolerance_no_false_hit`（AC-A3） | 减少量落在容差内（如 0.005%）→ 不置位任何目标 | 通过（不回归） | 通过 |
| `test_anchor_from_config_not_hardcoded`（AC-A4） | 修改 config 的 `target1_close_percent/target2_close_percent` 后，阈值随配置变化 | —（新） | 通过 |
| `test_multi_symbol_isolation`（AC-A5） | symbol A 检测 TP 不影响 symbol B 状态 | 通过 | 通过 |
| `test_zero_qty_full_close`（AC-A6） | `<zero_qty_threshold` 返回 0 | 通过 | 通过 |
| `test_idempotent_no_repeat_log`（AC-A7） | 已置位后再减仓不重复置位/重复日志 | 通过 | 通过 |
| `test_query_fail_no_advance`（AC-A8） | 查询失败不更新 last、不置位 | 通过 | 通过 |
| `test_restart_backfill_self_heal`（AC-E 接管） | 基线重建后，`entry=100% 剩余=30%`，回填 `target2_reached=True` | 失败（无此逻辑） | 通过 |
| `test_tp_exec_marks_executed`（AC-D1） | TP 成交 → `mark_order_executed` 被调用 | 失败 | 通过 |
| `test_sl_cancel_marks_canceled`（AC-D2） | 撤 SL/旧移动止损 → `mark_order_canceled` 被调用 | 失败 | 通过 |

#### 9.3.1 新增六类闭环用例（覆盖 A-review B1~B5 与 AC-E，必测）

| 用例（新增） | 覆盖盲点 | 断言 |
|------|----------|------|
| `test_step_size_truncation_no_miss` | B2 截断边界 | 锚点 1.05、`step_size=0.1`：TP1=0.3、TP2=0.4 成交后剩 0.35，`derive_reached_targets(..., step_size=0.1)` 返回 `(True, True)`——纳入 `2×step_size` 截断项后不漏检（反例被消除） |
| `test_replenish_tier_aware_after_tp1` | B2 补挂后跨档 | `target1_reached=True` 时 `replenish_conditional_orders` **仅组 TP2 单**（委托量 = 当前量 − 锚点×30%，无 TP1 单生成）；TP2 成交后 `target2_hit=True` |
| `test_mark_then_guard_no_replenish` | B1 mark→守卫集成 | TP2 成交并 `mark_order_executed` 后，带 `target2_reached=True` 调 `find_missing_protection` → 返回不含「止盈单」缺口 → `_guard_symbol` 不调用 `replenish_conditional_orders`（尾仓不被重复平 70%） |
| `test_sl_partial_fill_no_tp_mark` | B3 SL 部分成交 | 数量下降 + `current_price ≥ SL 触发区`：不置位 `target1/2`、不出现「第 N 目标已达成」日志、`mark_order_executed` 不被调用，且剩余 OPEN TAKE_PROFIT 被 `mark_open_orders_canceled` 收敛 |
| `test_manual_stop_guard_and_takeover` | AC-E 人工过渡单共存 | target2 达成 + 存在 `MANUAL_STOP`：守卫不补挂 SL；接管时**先**挂策略移动止损**后**撤 `MANUAL_STOP`（断言调用顺序，且挂单失败时 `MANUAL_STOP` 保留） |
| `test_tp_fill_with_price_rebound_still_sets_target` | B5 价格反弹出 TP 区不漏判 | TP2 成交后 detect 时 `current_price` 已反弹至中间地带（SL 触发价下方）且 `exchange_qty` 已跨档：断言 `source="TP"`、`target2_reached=True`、`mark_order_executed` 被调用；镜像负例 `current_price ≥ SL 触发价` 返回 `"SL"`、不置位不 mark |

### 9.4 十项真实性检查适用项

对本任务适用全部十项，逐项来源：
1. import 的模块/函数存在（`derive_reached_targets`、`mark_order_executed` 等以源码定义/import 为准）；2. API 方法与参数正确（`mark_*` 签名对照 [condition_orders.py](file:///Users/yl/vscode/Binance_quantitative_trading/shared/condition_orders.py)）；3. 配置 key 存在（`batch_take_profit.*`、`position_detection.*`，对照 [config.yaml](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/config.yaml)）；4. 文件路径正确；5. 变量定义拼写（静态检查 + 真实调用）；6. 分支与 AC 一致（§9.3 正反例）；7. 异常/失败后状态正确（锚点缺失 fail-closed、mark_* 失败不阻断）；8. async/sync 正确（`detect` 是同步、`mark_*` 是 async，调用点需 await）；9. 常量有依据（档位比例来自配置、容差来自配置，零硬编码）；10. 变更文件完整（diff 评审）。

---

## 10. 任务分工建议（B 段可拆的独立任务）

| 任务 | 目标 | 依赖 | 可改文件 | 输入/输出契约 | 测试场景 | 完成条件 |
|------|------|------|----------|---------------|----------|----------|
| T1 跨档判定纯函数 + detect 重构 | AC-A 核心修复 | 无 | `executor.py` | 输入：`(anchor, exchange_qty, percents, tol)`；输出：`(hit1, hit2)`；新增 `derive_reached_targets` + `_resolve_tier_anchor` + `tp_anchor_quantity` 字段 | §9.3 AC-A1~A8 | 核心函数 100% 覆盖率 + 回归用例全绿 |
| T2 重启锚点与档位自愈 | AC-E 接管前提 | T1 | `strategy.py`、`executor.py` | `_sync_baseline_to_tracking` 回填 `tp_anchor_quantity` + 档位回填（复用 `derive_reached_targets`） | `test_restart_backfill_self_heal` | 自愈用例通过 |
| T3 condition_orders 状态同步 | AC-D / 附带 BUG② | 无（可与 T1 并行，但同文件建议串行） | `executor.py` | 补 §6.3 五类 `mark_*` 调用点 | §9.3 AC-D1/D2 | 同步用例全绿 |
| T6 守卫档位感知 + 按档补挂 | AC-D 与 AC-A 安全落地（B1/B2） | T2（需 target 状态与锚点） | `executor.py`、`strategy.py` | `find_missing_protection` 按 target 状态判定（§6.4 表 + tracking 缺失口径）；`replenish_conditional_orders`/`_build_replenish_plans` 改「剩余目标锚定」组单（§3.4 第4点）；`_guard_symbol` 传 target 状态 | §9.3.1 `test_mark_then_guard_no_replenish`、`test_replenish_tier_aware_after_tp1` | 三档状态表 + 补挂用例全绿 |
| T7 人工过渡单 MANUAL_STOP 白名单 + 先挂后撤 | AC-E 保护不中断（B4/T6 扩展） | T6（与守卫同源） | `executor.py` | 止损类存在判定扩展为「STOP_LOSS/TRAILING_STOP/MANUAL_STOP 任一存在」（§8.2）；接管时序先挂策略移动止损后撤 MANUAL_STOP | §9.3.1 `test_manual_stop_guard_and_takeover` | 共存 + 接管用例通过 |
| T4（可选）config 容差微调 | 若 T1 发现 step_size 误差 > 容差 | T1 | `config.yaml` | 新增 `position_detection.tier_tolerance_ratio`（默认 0.01）[待核实 T1] | 精度边界用例 | 配置化、无硬编码 |
| T5 集成回归 + 覆盖率报告 | 全量 new_coin 回归 | T1~T3、T6~T7 | 测试文件 | §9.1 三条命令 + 报告落盘 | 全量通过 + 覆盖率达标 | GB 验收证据齐全 |

> T1/T2/T3/T6/T7 均改 `executor.py`（T6 另涉 `strategy.py`），建议**串行或由同一实现者接力**（同一核心文件），T4/T5 可并行准备。

---

## 11. 变更范围分析（文件/容器影响）

| 预计改动文件 | 变更点 | 容器影响 |
|--------------|--------|----------|
| `strategies/new_coin/executor.py` | `detect_take_profit_fills` 重构、新增 `derive_reached_targets`/`_resolve_tier_anchor`、`tp_anchor_quantity`、condition_orders `mark_*` 调用点 | `trading-new-coin` 重建 |
| `strategies/new_coin/strategy.py` | `_sync_baseline_to_tracking` 回填锚点 + 档位自愈 | `trading-new-coin` 重建 |
| `strategies/new_coin/config.yaml` | （仅当 T4 需要）容差/激活微调 | `trading-new-coin` 重建 |
| `strategies/new_coin/tests/test_take_profit_fill_detection.py`（新增） | AC-A 回归用例 | 仅测试，不入镜像运行 |
| `strategies/new_coin/tests/test_condition_orders_sync.py`（新增） | AC-D 同步用例 | 仅测试，不入镜像运行 |

**容器影响结论**：所有业务改动都落在 `strategies/new_coin/`，new_coin Dockerfile 显式 `COPY strategies/new_coin/`（[Dockerfile:L21](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/Dockerfile#L21)），**只重建 `trading-new-coin` 一个容器**。
- **不触碰 `shared/`**（AC-D 复用已有 `mark_*`，AC-A 纯函数放 executor 内）→ 不触发「shared 变更 → 全部策略容器重建」。
- **不触碰 `strategies/hrs/`** → 不重建 hrs（hrs Dockerfile 虽为 `COPY . /app/`，但本轮不改 hrs 目录下任何文件，且其变更-容器映射不受 new_coin 变更影响）。[待核实 T7：确认 CI 的增量判定按目录 diff，只改 new_coin 不会误触发 hrs 重建]

---

## 12. 发布验证与回退（本轮不部署）

**本轮不部署、不 push**（需求 §7.4）。以下为 B 段完成后、D 段上线时的前置要点（按 [deployment.md](file:///Users/yl/vscode/Binance_quantitative_trading/.trae/rules/deployment.md)）：

- **发布路径**：push main → GitHub Actions（build `trading-new-coin`，skip 其余）→ GHCR `ghcr.io/bearflower/trading-new-coin:latest` → SSH `docker compose pull && up -d`（仅 recreate new_coin）。
- **验证要点（五层，侧重 new_coin）**：① `docker ps` 确认 `trading-new-coin` 镜像为 ghcr.io；② 镜像 digest/ID 与本次 Run 对应；③ 容器内 `/app/VERSION` 的 `DEPLOY_ID` 与 Run Summary 一致（本轮 new_coin 会被构建）；④ 关键文件 MD5 本地 vs 容器内；⑤ 日志无 `executor.py` 报错 + **观察 RDW 类场景**：`trading.short_positions.target2_reached` 是否如实置位、`trading.condition_orders` 是否被 `mark_*` 收敛（只读核对，不下单、不回测）。
- **回退方式**：`git revert` 回滚 commit + push 触发重建；或服务器侧 `docker compose pull` 拉上一稳定镜像（`latest` 不可保证，需记 digest）。回退后重做部署验证。

---

## 13. AC → 设计映射摘要

| AC | 实现路径 | 测试用例 | 结果占位 |
|----|----------|----------|----------|
| AC-A1 | §3.3 detect 重构：一次置位两档 | `test_cross_tier_single_poll_levels_both` | 待 B 段填充 |
| AC-A2 | §3.2 累计比例判定，分步不回归 | `test_stepwise_two_levels_no_backfill` | 待 B 段 |
| AC-A3 | §3.2 容差带 | `test_tolerance_no_false_hit` | 待 B 段 |
| AC-A4 | §3.1 配置 `target1/2_close_percent` | `test_anchor_from_config_not_hardcoded` | 待 B 段 |
| AC-A5 | §3.2 纯函数按 symbol 隔离 | `test_multi_symbol_isolation` | 待 B 段 |
| AC-A6 | §3.3 全平优先返回 0 | `test_zero_qty_full_close` | 待 B 段 |
| AC-A7 | §3.3 `not reached` 守卫 | `test_idempotent_no_repeat_log` | 待 B 段 |
| AC-A8 | §3.3 None 守卫在调用方 | `test_query_fail_no_advance` | 待 B 段 |
| AC-B1~B3 | §4 评估结论+量化+配置+剩余风险 | 评审产物（文档段落） | 见 §4 |
| AC-C1~C3 | §5 两路径可行性+风险+边界 | 评审产物（文档段落） | 见 §5 |
| AC-D1~D4 | §6.3 补 `mark_*` 五类调用点 | `test_tp_exec_marks_executed` / `test_sl_cancel_marks_canceled` | 待 B 段 |
| AC-E1~E3 | §8 过渡补单+接管+监控+验收 | `test_restart_backfill_self_heal` + 只读对账 | 待处置落地 |

---

## 14. 待核实项清单（修订后状态）

| 编号 | 待核实事项 | 状态 | 影响 | 责任 |
|------|-----------|------|------|------|
| T1 | `step_size` 对 30%/40% 委托量的截断误差是否落在容差内 | **已核实 + 已闭环**（ROUND_DOWN 在 [executor.py:L1286](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L1286)；§3.1 量化结论 + §3.2 公式纳入 `N×step_size`；用例 `test_step_size_truncation_no_miss`） | AC-A 容差精度 | A 完成 |
| T2 | `short_positions.quantity` 重启后确为初始量 | **已核实成立**（`_insert_short_position` 写初始量 L1431-L1443、`_update_short_position_closed` 不覆盖 L1474-L1484，见 §3.4 第2点） | §3.4 锚点回填来源 | A 完成 |
| T3 | PM 账户是否支持原生 `TRAILING_STOP_MARKET` | 待核实（不阻塞本轮设计） | AC-C 路径 1 可行性 | 仅影响后续立项 |
| T4 | 本地环境是否已装 `pytest`/`pytest-cov` | 部分核实（复核确认 `strategies/new_coin/tests/` 已存在 4 个测试文件；pytest/pytest-cov 可执行性留待 B 段现场验证） | §9.1 命令可执行性 | B 段/对话 |
| T5 | MSTU/VKTX 交易所是否仍持仓（是否纳入 AC-E） | 待核实（阻塞 AC-E 落地、不阻塞设计） | AC-E 处置范围 | 用户/主对话只读核对 |
| T6 | 人工过渡单与 `find_missing_protection` 守卫的冲突方式 | **已定稿**（`MANUAL_STOP` 枚举 + 止损类白名单扩展 + 先挂后撤，见 §6.4/§8.2，含用例 `test_manual_stop_guard_and_takeover`） | §8.2 接管不中断 | A 完成 |
| T7 | CI 增量判定是否按目录 diff，只改 new_coin 不误触 hrs 重建 | 待核实（部署侧） | §11 容器影响 | B 段/D 段核对 |
| T8（次要，不阻塞） | 同 symbol 存在多条 `status='open'` 的 `short_positions` 记录时，锚点取哪条（及 open 记录缺失的回填行为） | **已定稿**（采纳 `ORDER BY opened_at DESC LIMIT 1` + 命中多条告警，见 §3.4 第5点） | §3.4 锚点回填边界 | B 段按 §3.4 第5点实现 |

> 上述待核实项：T1/T2/T6/T8 已由 A 段闭环（T8 定稿为 `ORDER BY opened_at DESC LIMIT 1`，见 §3.4 第5点）；T3/T5/T7 不阻塞本次设计 GA。B 段实现者不得自行猜测已闭环项的业务语义（需求 §5.7 约束）。

---

## 15. AC-B / AC-D 推荐结论（一句话）

- **AC-B**：默认**不前移**（保持 TP2 后激活），但把 `_check_dynamic_trailing` 的激活权统一交给 shared activation 语义（`also_on_tp1/min_profit_pct/also_on_tp2`）以消除 target2 硬门控架空，`_check_trailing_stop` 保持 TP2 后不变。
- **AC-D**：**不新抽 shared**，复用 `shared/condition_orders.py` 已有的 `mark_order_executed/mark_order_canceled/mark_open_orders_canceled`，仅在 new_coin 内补调用点，本轮只重建 `trading-new-coin` 一容器。

---

## 16. A-review 阻塞项修订记录（v1.1 / v1.2 闭环）

逐条记录 A-review（design-review-local/GLM-5.3，2026-10-15）B1~B4 的闭环结论与证据位置：

| 编号 | 阻塞项 | 闭环结论 | 证据位置 |
|------|--------|---------|---------|
| B1 | AC-D `mark_order_executed` 与守卫交互未定义 → 已达成 TP 尾仓被重复补挂 | **守卫必须感知 target 状态**：`find_missing_protection` 的「缺止盈单」判定从「有无 OPEN TAKE_PROFIT 白名单」改为「按 `target1/2_reached` 档位状态」——target2 达成不要求 TP（`require_take_profit` 退化为 false）、target1 达成只补 TP2、两档未达成维持 `require_take_profit`；并在 `find_missing_protection` / `replenish_conditional_orders`（按档补挂）/ `_guard_symbol`（传 target 状态）三处联动 | §6.4 全节 + 状态表；§3.4 第4点；用例 §9.3.1 `test_mark_then_guard_no_replenish` |
| B2 | AC-A 锚点判定「委托量=锚点×比例」前提在 replenish 路径不成立，可构造漏检反例 | **双管齐下**：① 补挂组单基准改为「剩余目标锚定」（TP 委托量 = 当前量 − 剩余目标，剩余目标 = 锚点×(1−累计比例)），消除「当前量×比例」被 skip 后的漏检；② 判定容差显式纳入 `N×step_size` 截断项，给出量化关系（`tol1 = 锚点×ratio + abs + 1×step`、`tol2 = …+ 2×step`），反例 1.05/step0.1 由 0.3256 < 0.35 漏检 → 0.5256 ≥ 0.35 命中 | §3.1 量化结论；§3.2 公式；§3.4 第4点；用例 §9.3.1 `test_step_size_truncation_no_miss`、`test_replenish_tier_aware_after_tp1` |
| B3 | SL 部分成交（数量下降非止盈）未定义 → 误置位 + 误标 EXECUTED | **引入双信号甄别** `_classify_fill_source`（价格方向 + 档位量）返回 TP/SL/UNKNOWN：仅「来源=TP」进入置位与 `mark_order_executed`；SL 部分成交不置位不 mark、撤剩 OPEN TP 并 `mark_open_orders_canceled` 同步；UNKNOWN fail-safe 告警、交下轮对账收敛 | §3.3 伪代码；§3.5.1 甄别表；§3.5.2 处置；用例 §9.3.1 `test_sl_partial_fill_no_tp_mark` |
| B4 | §5.3「下游不按 order_type 白名单过滤」结论错误 | **已修正**：`find_missing_protection` 确按 `order_type` 字面白名单判定（L1045-L1047），新增 `TRAILING_STOP` 会触发每周期补挂 SL；`order_type` 改造落地前必须扩展「止损类存在」判定；本轮 AC-C 不改码故不触发 | §5.3 修正表 + 边界重申 |
| T6 扩展 | 守卫判定语义（含人工过渡单）必须定稿 | **已定稿**：人工过渡单记 `MANUAL_STOP`；止损类白名单扩展为「STOP_LOSS/TRAILING_STOP/MANUAL_STOP 任一」；接管执行「先挂策略单 → 成功后撤 MANUAL_STOP → 失败则保留兜底」 | §8.2 全节；§6.4；用例 §9.3.1 `test_manual_stop_guard_and_takeover` |

#### 复审（v1.1）B5 闭环记录（标注 v1.2）

第二次复审（design-review-local/GLM-5.3，2026-10-15，见报告 §6）判定 B1/B2/B4 闭环、但新增 B5。本次 v1.2 逐条闭环如下：

| 编号 | 阻塞/附带项 | 闭环结论 | 证据位置 |
|------|-----------|---------|---------|
| B5 | §3.5.1「价格 AND 档位量」双信号在「TP 成交后价格反弹出 TP 区」判 UNKNOWN；§3.3 UNKNOWN 先推进 `_last_tracked_qty` → 永不重判 → target2 永久裸奔 | **选方案甲（量主导、价格仅作 SL 排除）+ 吸收方案乙要点（UNKNOWN 不推进 last）**：置位唯一判据 = 纯量 `derive_reached_targets`（价格不再要求进入 TP 区）；`_classify_fill_source` 改为接收 `tier_hit`（复用 derive 返回值，单一容差来源），价格只做「`current_price ≥ SL 触发价`」排除；UNKNOWN 分支不推进 `_last_tracked_qty`，保留重判 + `reconcile.zombie_confirm_cycles` 重试上限防刷屏。证明「TP 成交后价格反弹出 TP 区（中间地带）」仍正确置位 target2（置位不依赖价格）；SL 区极端反弹的残余风险非裸奔（固定止损仍兜底），已声明接受 | §3.3 伪代码；§3.5.1 全节；§3.5.2 残余风险声明；用例 §9.3.1 `test_tp_fill_with_price_rebound_still_sets_target` |
| 附带1 | 甄别容差与 derive 容差两处各写一套 | **单一来源**：`tier_hit` 直接复用 `derive_reached_targets` 的 `(hit1, hit2)` 返回值，与 §3.2 `tol1/tol2` 同源，不另设甄别容差 | §3.5.1「单一容差来源」 |
| 附带2 | §10 任务分工表漏守卫/组单/MANUAL_STOP 改动归属 | 补 T6「守卫档位感知 + 按档补挂」、T7「MANUAL_STOP 白名单 + 先挂后撤」两行，更新 T5 依赖与注释 | §10 任务表 |
| 附带3 | tracking 缺失时守卫判定口径未定义 | 补「tracking 缺失/状态不可知视同两档未达成 + 告警」（fail 方向与现状一致，衔接 §3.4 第3点自愈） | §6.4 |
| 附带4 | 补反弹场景用例 | 新增 `test_tp_fill_with_price_rebound_still_sets_target`（正例中间地带 + 镜像负例 SL 区） | §9.3.1 |
| 附带5 | T8 口径 | 采纳 `ORDER BY opened_at DESC LIMIT 1` + 命中多条告警，§3.4 补第5点、§14 T8 转「已定稿」 | §3.4 第5点；§14 |
| 附带6 | 基线回填自愈的 step_size 缓存预热 | §3.4 第3点补「回填前先 `await _get_symbol_precision` 预热 `_precision_cache`」 | §3.4 第3点 |

**核实状态更新**：T1（step_size 截断）已由复核证实的 ROUND_DOWN（L1286）转为「A 段量化闭环」；T2（`short_positions.quantity` 为初始量）已核实成立；T4（tests 目录 4 文件存在）部分核实；T6 定稿移除待定；新增 T8（同 symbol 多条 open 记录取锚点规则）为不阻塞的次要项。详见 §14。

---

## 17. 实现落地补记（B 段，2026-10-15）

> **性质说明**：本节为 B 段实现完成后的**如实补记**，用于登记「实现落地细节」与「契约漂移」。**不修改 §1~§16 的任何原始契约**；凡与上文冲突者，以上文契约为准，本节仅作实现事实索引。

### 17.1 §10 未列出的内部子函数（均为已列任务的内部拆分，不改对外契约）

| 新增内部函数 | 位置 | 归属任务 | 说明 |
|--------------|------|----------|------|
| `_resolve_detection_baseline` | [executor.py:L3451](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3451) | T1 | detect 基线/「无变化」前置判定拆分 |
| `_apply_tp_marks` | [executor.py:L3576](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3576) | T1/T3 | 档位置位 + `mark_*` 调用聚合 |
| `_fetch_current_price_or_none` | [executor.py:L3932](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L3932) | T1（B5 甄别） | 价格信号读取（失败返回 None，供 `_classify_fill_source` 价格排除用） |
| `_unknown_reduction_counts` + `_zombie_confirm_cycles` | [executor.py:L331-L333](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py#L331-L333) | T1（B5） | UNKNOWN 连续计数器**存于内存 dict**（重启清零，有基线回填自愈兜底）；`_zombie_confirm_cycles` 从 `reconcile` 配置读取。**（2026-10-15 返工补充）** 超上限强制推进分支须 `pop` 清零计数器（见 §17.5），否则后续 UNKNOWN 减仓丧失 3 轮重判窗口 |

### 17.2 配置项落地

- `trading.reconcile.zombie_confirm_cycles`（[config.yaml:L133](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/config.yaml#L133)，默认 3）：沿用既有 key，按 A-review §7.2「要点 2」提醒**以注释写明双语义**——僵尸仓关闭确认轮数 + UNKNOWN 减仓强制推进上限。

### 17.3 行号漂移（引用仅作历史定位，不作当前契约）

B 段实现使 `executor.py` / `strategy.py` 行号整体下移。§10、§14 及 A-review 证据索引中的行号（如「detect L3127-L3171」「executor.py:L1286 / L1045-L1047 / L1431-L1443」）为**实现前版本**的历史定位；当前实现锚点：`_resolve_tier_anchor` L3302、detect 相关 L3495/L3529/L3555、`_fetch_current_price_or_none` L3932。核对时以函数名检索为准（本补记自身的行号即为实现后版本）。

### 17.4 编号空间提示（A-review §7.2 次级发现）

§10 任务编号（T1~T7）与 §14 待核实编号（T1~T8）为**两个独立编号空间**，B 段实现中未混用；此处仅按复核建议登记，避免后续引用歧义。

### 17.5 深度审查返工补记（2026-10-15 第二次返工）

- **缺陷与修复**：深度审查报 1 项重要缺陷——`_handle_non_tp_reduction` 的 UNKNOWN 分支在「超上限强制推进 `_last_tracked_qty`」后**未清零** `_unknown_reduction_counts`，致后续 UNKNOWN 减仓丧失 3 轮重判窗口（与 B5/方案乙「UNKNOWN 不推进 last、保留重判」语义冲突）。已在该分支补 `self._unknown_reduction_counts.pop(symbol, None)`，**与 TP 分支对齐**。
- **用例**：新增 `TestUnknownReductionCounterResetAfterForce`（`test_unknown_force_advance_clears_counter` / `test_unknown_counter_restarts_after_force`）；B 段 6 文件由 95 → **97 passed**。
- **终审**：深度审查**通过**（无阻断问题）；本轮仅补齐计数器复位与用例，**未改变业务逻辑语义**，§1~§16 契约不受影响。
- **哈希刷新（最新冻结）**：`executor.py` = `d0d997817a892dfa91aa8b4c8dacd2de`、`test_b_segment_gaps.py` = `9652cf7da446edff6a0ade6dd47a793c`；`strategy.py` / `config.yaml` / `test_take_profit_fill_detection.py` 未变。新增代码行位于 `executor.py` L3440-3441，故 §17.1 / §17.3 的行号整体下移约 2 行，核对仍以函数名检索为准。

---

（架构文档结束）