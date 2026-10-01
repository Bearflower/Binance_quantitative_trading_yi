# P0 修复架构方案：持仓保护单丢失（补全条件单失败 + 重复裸奔）

## 1. 文档信息

| 项 | 内容 |
|----|------|
| 文档名称 | P0 修复架构方案：持仓保护单丢失（补全条件单失败 + 重复裸奔） |
| 版本 | v1.0 |
| 作者 | backend-architect |
| 创建日期 | 2026-09-30 |
| 基线 commit | `cfe4ebe`（Actions Run #44，上线 R01–R08 后暴露本事件） |
| 上游输入 | `docs/plans/fix-2026-09-30-p0-protection-orders-missing-requirements.md`（需求定稿，含 AC）、生产只读证据 |
| 下游环节 | python-engineer（编码实现）→ code-specification-inspector（检测）→ 强制测试 |
| 适用范围 | 仅需求 §4 的 P0-1 / P0-2 / P0-3 三项；R09–R18 不在本轮 |
| 约束 | 遵守 `coding-standards.md`（禁止硬编码/重复代码/幽灵参数、单函数≤50 行、单行≤120 字符、中文注释）、`deployment.md`（`shared/*` 变更触发全量重建）、`CLAUDE.md` |

> 本文档只做架构设计与接口约定，不含业务实现代码；涉及契约的伪代码因需要而给出。
>
> 🔁 **后续变更（2026-10-01）**：本轮 P0-2「表已存在即放行」因存在性判断硬编码 `table_schema='public'` 成为**死代码**（已由 [P0 补漏架构方案](./fix-2026-10-01-p0-remaining-unprotected-positions-architecture.md) 改用 `to_regclass` 修复）；`_replenish_done` 一次性语义已被**每周期持续保护守卫**取代。本文档 §5.4 等关于 `_replenish_done` 协同的描述为**当轮设计快照**，不再反映现行实现。

### 1.1 需求已定（用户拍板，三项全做，不得削减）

| 编号 | 类别 | 目标 |
|------|------|------|
| P0-1 | 策略侧 + 服务侧防御 | 持仓标的必须保持 K 线可用（有持仓不注销 / 用前 ensure-active / 注册接口幂等复位 status / 缓存一致） |
| P0-2 | 服务侧 | kline 白名单放行「格式合法且数据表已存在」的标的，保持 fail-closed |
| P0-3 | 策略侧 | 补全流程改为「先算 ATR 再撤旧单」，任何失败路径都不裸奔 |

### 1.2 架构师定论（先给出，后文论证）

| 编号 | 结论 | 依据 |
|------|------|------|
| **A1（是否抽 shared）** | **本轮不落 root `shared/`，不新增 shared 公共补全模块**；修复全部落在 `strategies/new_coin/**` 与 `services/kline_service/**` | 三处补全路径**非同构**（§2.3），`ensure-active`/`有持仓不注销` 仅 new_coin 需要且 <3 处同构，未达「必须抽公共模块」门槛；`shared/kline_service.py` 无需改动（§3.2 论证） |
| **A2（镜像重建范围）** | **仅重建 2 个镜像**：`trading-kline-service`、`trading-new-coin`；**不触发全量重建** | A1 未改 root `shared/`；P0-2/P0-1服务侧 ⊆ `services/kline_service/**`；P0-1策略侧/P0-3 ⊆ `strategies/new_coin/**` |
| **A3（总体实施顺序）** | 见 §10：**第一批 = new-coin（P0-3 + P0-1策略侧 + 硬编码提取）；第二批 = kline-service（P0-2 + P0-1服务侧）**。两批各自独立可验证 | P0-3 单独不能解除「当前裸奔」（§10.1 论证），故须在 P0-2 上线后由补全流程自然收敛；但 P0-3 必须先上，避免 P0-2 上线后「撤了旧单又挂不回」的新窗口 |

---

## 2. 总体方案与分层落点

### 2.1 根因闭环（与需求 §3 对齐，逐环给出代码落点）

```
[入场成功] new_coin/strategy.py:776-784
     └─ 无条件 unregister_symbol(symbol)  ──►  registered_symbols.status = 'cancelled'
                                                   │
[下周期补全] strategy.py:492-576  (not _replenish_done 恒真)
     └─ executor.replenish_conditional_orders(symbol)  executor.py:3215
            1) cancel_all_algo_orders(symbol)          executor.py:3251-3255   ← 先撤旧保护单
            2) _calculate_atr(symbol)                  executor.py:3275-3281
                   └─ kline_service.get_klines → HTTP 400（不在白名单）          shared/kline_service.py:118-121
                   └─ except → return Decimal('0')      executor.py:1662-1664
            3) atr <= 0 → return False                  executor.py:3277-3281   ← 旧单已撤、新单未挂 = 裸奔
     └─ 返回 False → all_success=False → _replenish_done 不置位 → 每周期重复
```

修复后闭环：

```
[入场成功] if 有未平持仓: 不注销（保留注册态与采集任务）
[补全]     ① 只读前置：读持仓 → ensure-active → 算 ATR → 取精度/现价 → 组价组量
           ② 全部 OK 才 cancel_all_algo_orders；撤失败即返回（不挂新单）
           ③ 挂 SL/TP1/TP2；失败记录缺口，下周期收敛
[服务侧]   白名单 OR 表存在 即放行读取；注册接口幂等复位 status=active 并回传真实 status
```

### 2.2 分层落点总表

| 项 | 层 | 文件 | 关键函数 | 改动性质 |
|----|----|------|---------|---------|
| P0-1 策略侧 | 策略层 | `strategies/new_coin/strategy.py` | `_execute_cycle`(L776-784、L658)、新增 `_has_open_position` / `_ensure_symbol_active` | 新增守卫 + 新增 helper |
| P0-1 服务侧 | 服务层 | `services/kline_service/core/registry.py`、`api/registry_routes.py` | `register`(L76-115)、`_save_to_database`(L286-340)、`get_active_symbols`(L266) | 幂等复位 + 缓存一致性 + 回传真实 status |
| P0-2 | 服务层 | `services/kline_service/core/table_name_guard.py`、`api/routes.py` | 新增 `build_readable_table_name`；`_validated_table_name` 分读/写两路 | 新增读路径入口 + 存在性注入 |
| P0-3 | 策略层 | `strategies/new_coin/executor.py` | `replenish_conditional_orders`(L3215-3530) | 重排 + 失败语义收紧 |
| 配置 | — | `strategies/new_coin/config.yaml`、`services/kline_service/shared/core/config.py` | — | 新增键（§6） |

### 2.3 「是否抽 shared」的论证（对应需求开放问题 Q2）

需求 §12.1 把 Q2 交给架构拍板。三处「保护单补全」现状：

| 实现 | 文件 | 撤旧单与算 ATR 的顺序 | 是否本次缺陷 |
|------|------|---------------------|-------------|
| new_coin 条件单补全 | `strategies/new_coin/executor.py::replenish_conditional_orders` | **先撤单（L3251）→ 后算 ATR（L3275）→ 失败即 return（L3281）** | **是（本 P0-3）** |
| hrs 条件单补单 | `strategies/hrs/executor.py::replenish_position_orders` | **先校验 ATR（L1017 `if atr<=0: return`）→ 再撤单（L1025）** | 否（ATR 已是入参且校验在撤单前） |
| btc_eth / 激进版补挂 | `shared/protection_retry.py::replenish_missing_protection`(L336) | **不撤旧单，只补缺失腿** | 否（无「撤了挂不回」中间态） |

结论：
1. **三处非同构**——数据模型不同（new_coin 用 `position_tracking` dict + `algo_ids`；hrs 用 `ReplenishResult` + `position_manager`；btc_eth 用 `PositionState` 对象）、下单模型不同（algo 条件单 vs 统一账户条件单）、失败语义不同。不构成 `coding-standards.md` 定义的「连续 5 行以上相同代码块 / 逻辑结构相同仅变量名不同」。
2. 「先算后撤」这一**安全不变式**只有 new_coin 违反；hrs 已在撤单前校验 ATR；btc_eth 不撤单。
3. `有持仓不注销` 与 `ensure-active` 仅 new_coin 需要（hrs 的注销条件已含「无持仓」L1318；btc_eth/grid 标的恒在白名单）。
4. 强行把一个**异构造**的 new_coin 条件单补全重构成 shared 公共模块，属于过度抽象，且把 P0 止血变成大范围重构，违背「最小止血」。

**A1 结论：本轮不抽 shared。** 在 new_coin 内部用**本地 helper** 消除新引入的重复（§3.3、§5.2）。

---

## 3. P0-1 详细设计（持仓标的必须保持 K 线可用）

### 3.1 改动点 1：有持仓不注销（策略侧）

**现状**（`strategies/new_coin/strategy.py`）：

| 位置 | 现状 | 问题 |
|------|------|------|
| L776-784 | `entry_success` 后无条件 `await self.kline_service.unregister_symbol(symbol)` | **根因 R-A**：把刚建仓的标的注销，后续 ATR 被白名单拒绝 |
| L653-662 | `SKIP_REASON_TOO_LONG`（上线过久）后 `unregister_symbol(symbol)` | 该分支发生在**入场前**，理论上无持仓；但需同构守卫以防未来回归 |

**改动前后对比**：

```
改动前：
  if entry_success:
      known_symbols.add(symbol); save_known_symbols()
      await unregister_symbol(symbol)                 # 无条件
      log("入场成功，已停止监控")

改动后：
  if entry_success:
      known_symbols.add(symbol); save_known_symbols() # 停止「新币检测」（保留业务收益）
      if not self._has_open_position(symbol):         # 仅无持仓才注销
          await self._unregister_if_safe(symbol)      # 统一出口（含缓存同步）
      log("入场成功，已停止监控")
```

**新增单点 helper（new_coin/strategy.py）**：

```
def _has_open_position(self, symbol) -> bool:
    # 内存优先；缺失时查 DB（new_coin.short_positions status='open'）
    # 查询异常 → 保守返回 True（宁可不注销）

async def _unregister_if_safe(self, symbol) -> bool:
    # 统一注销出口：_has_open_position 为真 → 不注销并记 INFO
    # 否则 unregister_symbol；成功后 self._registered_symbols.discard(symbol)
    # 失败（返回 False）→ 保留 _registered_symbols，记 WARNING（P0-1-F4：不误判）
```

L653-662 分支同样改用 `_unregister_if_safe`（消除两处重复注销逻辑）。

### 3.2 改动点 2：用前 ensure-active（策略侧，兜底）

**落点**：`strategies/new_coin/executor.py` 新增 `_ensure_symbol_active(symbol, interval)`，在 `replenish_conditional_orders` 的**只读前置阶段**调用（§5.3 的 Phase A）。同时在 `_calculate_atr`（executor.py:1601）入口可选调用（兜底）。

**改动前后对比**：

```
改动前：
  取 K 线（_calculate_atr）→ 无任何注册态保障 → 非 active 直接 400

改动后（ensure-active）：
  if not config.kline.ensure_active_before_use: return True      # 开关关闭即跳过
  if symbol in self._registered_symbols: return True             # 本策略已注册
  for attempt in range(ensure_active_retries):                   # 配置化重试
      ok = await self.kline_service.register_symbol(symbol, intervals=[interval])
      if ok:
          self._registered_symbols.add(symbol)
          return True
      await asyncio.sleep(ensure_active_retry_interval)
  logger.warning(...); return False                              # 不进入 ATR
```

**为什么不需要改 `shared/kline_service.py`（A1 的关键论证）**：

- 策略侧需要的只是「注册是否成功」这一**布尔信号**：`register_symbol` 已返回该布尔（`shared/kline_service.py:166-214`，成功 = HTTP 200 且 `code==0`）。
- 需求 P0-1-F2 的「校验返回 status 确为 active」中，**「status 落库为 active」由服务侧 P0-1-F3 保证**（服务端返回体已含 `data.status`，见 `registry_routes.py:238` 返回 `RegisterResponse(data=config)`），策略侧以「注册成功 + 服务侧幂等复位保证」作为前置，等价成立；`P0-1-AC3` 是**服务侧**验收（AC 明确「接口返回体 data.status=='active'」）。
- 若强行让客户端读取 `data.status` 原文，唯一单点是改 `shared/kline_service.py` → **触发全量重建**（8+ 容器停机）。**架构判定：不值得**。策略侧改以「注册成功布尔 + 服务侧 F3 保证」实现，`P0-1-AC2`（注册成功后 ATR 成功）与 `P0-1-AC5`（注册失败不进 ATR）均满足。
- 兜底再加一层「功能性验证」：ensure-active 成功后由紧接着的 `_calculate_atr` 实际取 K 线，若仍失败则 ATR=0 → 不撤单（与 P0-3 协同，天然端到端验证）。

> 若审查要求「必须读取原文 status」，则退化为改 `shared/kline_service.py` 新增 `ensure_symbol_active() -> Tuple[bool, str]`（纯增量、零回归），**代价是全量重建**。本文档按 A1（不重建全量）设计。

### 3.3 改动点 3：服务侧幂等复位 status + 回传真实 status（服务侧）

**落点**：`services/kline_service/core/registry.py`、`api/registry_routes.py`。

现状核实（需求 §3.2 已证「注册写入路径本身正确」）：
- `register()`（L76-115）：`symbol in self._cache` 且 `status=='active'` → `_update_registration()`（不改 status，保持 active）；否则 → `_reactivate_registration()`（L145-162，`config.status='active'` 后落库）。
- `_save_to_database()`（L286-340）：UPDATE 语句**包含 `status = :status`**（L303）。→ 写入路径无缺陷，与需求结论一致。

**但架构核实发现一个 P0-1 直接相关的潜在缺陷（服务重启后才暴露）**：

- `_load_from_database()`（L43-74）只加载 `WHERE status='active'`（L51）的行到 `_cache`。
- `register()` 的存在性判定**只看 `self._cache`**（L88 `if request.symbol in self._cache`）。
- 因此：**kline-service 重启后**，一个 DB 中 `status='cancelled'`（或 `expired`）的行**不在 `_cache`**，new_coin 的 ensure-active 重新注册该 symbol → 落入 **INSERT 分支**（L317-338）→ 撞 `symbol VARCHAR(20) NOT NULL UNIQUE`（`create_registered_symbols_table.py:11`）→ **唯一约束冲突，注册失败** → ATR 失败 → 裸奔。
- 生产证据中的 `USDBRLUSDT`（registered_at=09-18，早已 cancelled）正是这种「重启后重新注册会 INSERT 冲突」的典型对象。

**改动设计（服务侧）**：

| 子项 | 改动 | 目的 |
|------|------|------|
| F3-a | `register()` 的存在性判定改为「`_cache` 命中 **或** DB 存在」：未命中缓存时先 `SELECT symbol, status FROM registered_symbols WHERE symbol=:symbol`；存在 → 走 `_reactivate_registration`（或 `_update`），**禁止盲 INSERT** | 修复重启后重注册撞 UNIQUE |
| F3-b | `_reactivate_registration()` 落库后**回读**该行 status（或直接采用 `config.status='active'`），保证返回体为**落库后真实 status** | 满足 F3「不得只回传内存对象」 |
| F3-c | `register()` 返回体 `RegisterResponse(data=config)` 增加/确保 `status` 字段为落库值 | 满足 AC3 |
| F3-d | `unregister()`（L164-196）成功后从 `_cache` **保留**该行（现状保留但 status 已改），确保后续 `register` 命中 `_reactivate` | 避免「注销后缓存仍 active」误判 |

**缓存一致性（对应 Q3）**：

- 现状 `_cache` 仅启动加载一次（`initialize()` L34-41），长期运行会与 DB 漂移（外部手工改库、多实例等）。
- 设计：新增 `SymbolRegistry.refresh_active()`（重载 active 行，与 `_load_from_database` 同源复用），并新增**配置化的周期性刷新**：kline-service 调度器按 `REGISTRY_CACHE_REFRESH_SECONDS` 调用；`0` 表示禁用（保持现状）。
- 同时 `get_active_symbols()`（L266-271）保持「实时读内存」语义不变（R01 已如此）。
- 另加**惰性兜底**：`register()` 未命中缓存时的 DB 存在性查询（F3-a）本身即一次 DB 对齐。

> 归属说明：登记/注销的「本策略持仓判定」仅存在于策略侧（new_coin），服务侧不做业务判定，避免误伤他策略（需求 §7.5）。

### 3.4 hrs / btc_eth / grid 同构核对结论（需求 §11.1）

| 策略 | 是否有「持仓中注销」 | 是否有 K 线兜底 | 结论 |
|------|--------------------|----------------|------|
| **btc_eth / 激进版** | 无动态注销；标的恒 ∈ `FIXED_SYMBOLS ∪ settings.SYMBOLS` | 恒白名单，不存在 400 | **不受影响** |
| **grid** | 无动态注销；ETHUSDT/BTCUSDT ∈ `settings.SYMBOLS` | 恒白名单 | **不受影响** |
| **hrs** | 注销条件已含「无持仓」：`_should_unregister`（`strategy.py:1291-1325`）`if self.position_manager.has_position(symbol): return False`（L1318） | `market_data.get_klines_1h`（`hrs/market_data.py:399-415`）kline 失败/空 → **回退币安 API** | **实质安全**；但仍建议按 §12 复核 `position_manager.has_position` 是否覆盖「交易所已开仓但本地未登记」的窗口 |
| **new_coin** | **无**（L776-784 无条件注销） | **无**（`_calculate_atr` 仅走 kline，失败即 0，`executor.py:1662-1664`） | **两样都缺 = 本 P0** |

→ 与需求结论一致：new_coin 比 hrs 缺 (a)「有持仓不注销」、(b)「K 线兜底」；P0-1 补 (a)，(b) 属可选增强（需求 Q5，本轮不做）。

---

## 4. P0-2 详细设计（白名单放行「表已存在且格式合法」）

### 4.1 现状与放行判定所在层

- 唯一校验入口：`table_name_guard.build_kline_table_name()`（L122-141），三层：格式层 → 白名单层（`_collect_whitelist` L76-88，仅含 `FIXED_SYMBOLS ∪ settings.SYMBOLS ∪ registry active`）→ 表名层（`is_valid_table_name` L110-119）。
- 读路径调用点：`routes.py::_validated_table_name`（L52-61）→ `/klines/latest`（L106）、`/indicators`（L192）；写路径：`/collect/manual`（L278）、`registry_routes`（复用 L21）。
- 具备 DB 连接的是**路由层**（`db.get_connection()`）；`table_name_guard` 是**纯函数层**（无 DB）。

**放行判定落点（架构定论）**：**下沉到 `table_name_guard` 的新读路径入口，但「表存在」的事实由路由层以回调注入**——guard 保持无 DB，路由层提供 `async (table_name)->bool`。禁止在路由层另写第二套格式/白名单校验（满足 P0-2-F4「单一入口」）。

### 4.2 接口设计

```python
# services/kline_service/core/table_name_guard.py

def build_kline_table_name(symbol, interval, *, registry=None, settings=None) -> str:
    """【保持现状】白名单语义的写路径入口（format+whitelist+pattern 三层，fail-closed）"""

def build_table_name_by_format(symbol, interval, *, settings=None) -> str:
    """仅 format 层 + TABLE_NAME_PATTERN 层（无白名单）；供读路径存在性放行使用。
    复用 validate_symbol_interval_format + is_valid_table_name（不新增第二套校验）。"""

async def build_readable_table_name(
    symbol: str, interval: str, *,
    table_exists: Optional[Callable[[str], Awaitable[bool]]] = None,
    registry=None, settings=None,
) -> str:
    """【新增】读路径唯一入口：白名单 OR 表存在。
    1) format 层：validate_symbol_interval_format（symbol 必须匹配 SYMBOL_FORMAT_PATTERN）
    2) 生成候选表名并整体匹配 TABLE_NAME_PATTERN（interval 合法性由后缀组隐含约束）
    3) 命中白名单（FIXED ∪ SYMBOLS ∪ registry active 与 intervals）→ 返回
    4) 否则若 ALLOW_EXISTING_TABLE_SYMBOLS 且 table_exists 非空：
         True  → 记 INFO（symbol/interval/source=existing_table）+ 返回
         False → 抛 TableNameValidationError
         异常  → 抛 TableNameValidationError（fail-closed）
    5) 否则（开关关闭 / 无回调）→ 抛 TableNameValidationError
    """
```

**路由层注入（`routes.py`）**：

```python
def _make_table_exists_checker(conn):
    async def _check(table_name: str) -> bool:
        return await _table_exists(conn, table_name)      # 复用 L41-49，参数化查询
    return _check

async def _validated_read_table_name(conn, symbol, interval) -> str:
    try:
        return await build_readable_table_name(
            symbol, interval, table_exists=_make_table_exists_checker(conn), registry=registry)
    except TableNameValidationError as e:
        logger.warning(f"K 线查询参数非法：symbol={symbol!r} interval={interval!r} - {e}")
        raise HTTPException(status_code=400, detail=f"参数非法：{e}") from e
```

`/klines/latest`、`/indicators` 由「先校验后连库」改为「先连库 → `await _validated_read_table_name(conn, ...)`」；`/collect/manual`、`registry_routes` **继续用同步 `_validated_table_name`（白名单语义不变，P0-2-F5）**。

> 修订（2026-10-01）：审查期对读端点做了同构收敛，**实现函数名与调用链以本节以下描述为准**（原设计与 `_validated_read_table_name` 三段内联为历史表述）：
>
> - 两个读端点（`/klines/latest`、`/indicators`）不再各自内联「`_precheck_read_format` → `_validated_read_table_name` → `_ensure_table_ready`」三步，而是收敛为共用协程 **`_resolve_ready_table(conn, symbol, interval) -> str`**（不可用返回 `""`，调用方据此返回「无数据」）。
> - **`_precheck_read_format` 仍在两个端点内、连库之前调用**（保证注入型输入在触达 DB 前即 400），该前置守卫语义不变。
> - `_validated_read_table_name` 与 `_ensure_table_ready` **仍然存在**：前者被 `_resolve_ready_table` 调用，后者签名不变；读端点不再直接调用它们。
> - 另：`_ensure_table_ready` 内部已改为复用带 TTL 缓存的 `_resolve_table_exists`（不再直查 `_table_exists`），与 §4.3 缓存策略一致。

### 4.3 缓存策略（对应 Q4）

- 默认 `EXISTING_TABLE_CHECK_CACHE_TTL_SECONDS = 0` → **实时查 `information_schema`，不缓存**（最准确；查询轻量）。
- TTL > 0 时启用模块级 TTL 缓存：`dict[table_name] = (exists: bool, ts: float)`，命中且未过期即用；过期即重查。失效口径 = TTL；「表被删/新建」最坏在 TTL 内收敛（默认禁用，无此风险）。
- 并发：kline-service 为单事件循环，dict 读写原子；不引入跨请求共享可变状态的竞态（缓存项为不可变元组）。
- 异常：`table_exists` 抛异常 → **不写缓存 + fail-closed 拒绝**（P0-2-AC5）。

### 4.4 「表存在但数据陈旧」的语义（明确）

- 存在性放行**只决定「是否允许读取」**，**不做数据新鲜度校验**（本轮非目标）。
- 表存在但无新数据 → 现有 `_ensure_table_ready`（L64-81）返回 True → SELECT 返回旧/空数据 → 仍 200。此时 `_calculate_atr`：若行数 `< period+1`（`executor.py:1634-1636`）→ ATR=0 → P0-3 不撤单（安全）。
- 风险标注：陈旧但行数足够的表可能算出**偏离当前市场的 ATR**，进而给出偏松/偏紧的保护价。**属独立问题（数据新鲜度），建议列入后续增强**（如「表存在但最新 K 线时间 > N 个周期 → 记 WARNING」），本轮不实现。

---

## 5. P0-3 详细设计（先算 ATR 再撤旧单）

### 5.1 现状（`strategies/new_coin/executor.py::replenish_conditional_orders` L3215-3530）

| 行号 | 动作 | 顺序 |
|------|------|------|
| L3236 | `mctps_symbols` 跳过 | 前置 |
| L3242 | `_replenished_symbols` 跳过 | 前置 |
| **L3251-3255** | **`cancel_all_algo_orders(symbol)`（撤旧单；异常仅告警吞掉）** | **①** |
| L3257-3273 | 读 `positionRisk`，无空头持仓 → `return False` | ② |
| **L3275-3281** | **`_calculate_atr`；`atr<=0` → `return False`** | **③（失败即裸奔）** |
| L3284 | `_get_symbol_precision` | ④ |
| L3286-3294 | 初始化 `position_tracking` | ⑤ |
| L3296-3302 | 取现价 | ⑥ |
| L3307 | `ignore_error_codes` 局部字面量 | — |
| L3310-3504 | 挂 SL / TP1 / TP2 | ⑦ |
| L3506-3513 | `all_success` → `_replenished_symbols.add` | ⑧ |

缺陷：①先于③，③失败即 `return`，产生「旧保护单已撤、新保护单未挂」的中间态；`cancel_all_algo_orders` 异常被吞（L3255）→ 撤单失败也继续，可能新旧叠加。

### 5.2 改动后流程（重排为三阶段）

```
Phase A（只读准备，绝不撤单）
  A1  mctps/保护清单 跳过判定（config.trading.replenish.skip_symbols）
  A2  _replenished_symbols 跳过判定（保持幂等）
  A3  config.trading.replenish.cancel_after_ready 关闭时 → 直接走旧顺序（回退开关）
  A4  读 positionRisk → 无空头持仓 → return NO_POSITION（不撤不挂，AC5）
  A5  ensure-active（§3.2）→ 失败 return FAIL（不撤单，AC5）
  A6  _calculate_atr → atr<=0 → return FAIL（不撤单，AC1）
  A7  _get_symbol_precision → (tick, step)；失败 return FAIL（不撤单）
  A8  取现价；无效 return FAIL（不撤单）
  A9  组价组量（纯计算）：SL 价/量、TP1 价/量 + 是否「已过目标→市价平仓」标志、
      TP2 价/量 + 标志；逐项校验（量>0、价>0、min_notional）
      → 任一硬性不可用 return FAIL（不撤单，AC 由 A6/A7/A8 覆盖主要路径）

Phase B（确认可挂后，才撤旧单）
  B1  cancel_all_algo_orders(symbol)
        - 异常 or failed>0  → return FAIL（不挂新单，AC3）
        - 成功              → 继续

Phase C（挂新单；失败记录缺口，下周期收敛）
  C1  挂 SL            → 记录 algo_id / record_condition_order
  C2  TP1 分支：标志「已过目标」→ 市价平仓部分；否则挂 TP1 条件单
  C3  TP2 分支：同上
  C4  all_success → _replenished_symbols.add；return True（AC2）
      否则 → 不置位；return False（AC4，缺口可见）
```

要点：
- **撤单失败必须阻断挂新单**（当前 L3255 吞异常，须改为「撤单异常或 `failed>0` → 返回 FAIL」）。
- **市价平仓分支**（当前 L3356-3380、L3432-3458）只依赖「现价 vs 目标价」，是纯判定，可留在 Phase A 计算标志、Phase C 执行，不依赖撤单结果。
- Phase A 内所有失败 `return` 都**不触碰交易所**（只读），保证「撤了旧单却挂不回」不再发生。

### 5.3 函数拆分（满足单函数 ≤50 行）

`replenish_conditional_orders` 现约 315 行，重排后建议拆为：

| 新函数 | 职责 | 预估行数 |
|--------|------|---------|
| `_prepare_replenish_plans(symbol, entry_price)` | Phase A：只读准备 + 组计划（返回 plans 或 None） | ≤45 |
| `_execute_replenish_plans(symbol, plans)` | Phase B+C：撤单 + 挂单 + 标记 | ≤45 |
| `replenish_conditional_orders` | 编排（跳过判定 + 调两段 + 异常收敛） | ≤25 |

（plans 可用简单 dict/dataclass 承载 SL/TP1/TP2 的价、量、分支标志。）

### 5.4 与 `_replenish_done` / `_replenished_symbols` 的协同（需求 §15.1 第 6 点）

- `_replenished_symbols`（executor.py:209）保证**同一 symbol 成功后不再重复处理**；失败不置位 → 下周期重试（AC4）。
- `_replenish_done`（strategy.py:102）仅在**所有持仓都成功**时置位（strategy.py:562-564）；单个长期失败 → `_replenish_done` 恒 False → 每周期遍历全部持仓，但已成功的按 per-symbol 跳过。修复后若 P0-2/P0-1 生效，ATR 不再失败，收敛自然达成。
- 风险：无持仓返回 `True`（NO_POSITION 视同成功）→ 若某 symbol 无持仓但有孤儿条件单，本函数不再替其清理（保守，符合 AC5「不得误撤他人保护单」）。

---

## 6. 新增/修改配置项清单（不得引用未落地的 key）

### 6.1 `strategies/new_coin/config.yaml`

| key | 默认值 | 含义 | 段 |
|-----|--------|------|----|
| `kline.keep_registration_when_position_open` | `true` | 有未平持仓时是否保留 K 线注册 | `kline` |
| `kline.ensure_active_before_use` | `true` | 取 K 线前是否 ensure-active | `kline` |
| `kline.ensure_active_retries` | `2` | ensure-active 重试次数 | `kline` |
| `kline.ensure_active_retry_interval` | `2` | ensure-active 重试间隔（秒） | `kline` |
| `trading.min_notional` | `5` | 最小名义价值（USDT），替换 `executor.py:72` `_MIN_NOTIONAL` | `trading` |
| `trading.replenish.cancel_after_ready` | `true` | 「先算后撤」总开关（`false` 回退旧顺序） | `trading.replenish` |
| `trading.replenish.ignore_error_codes` | `['-4164','-2011','-2021','-4136','-4507']` | 幂等忽略错误码，替换 `executor.py:66/3307` 字面量 | `trading.replenish` |
| `trading.replenish.skip_symbols` | `['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','TRXUSDT']` | MCTPS 托管跳过清单，替换 `executor.py:3236` `mctps_symbols` | `trading.replenish` |

> 现有 `kline.interval='1h'`、`kline.atr_period=14`、`trading.stop_loss_percent`、`trading.emergency_stop`、`trading.target*_atr_multiplier`、`trading.close_position.*` 沿用，不新增硬编码。

### 6.2 `services/kline_service/shared/core/config.py`

| key | 默认值 | 含义 |
|-----|--------|------|
| `ALLOW_EXISTING_TABLE_SYMBOLS` | `true` | 是否启用「表存在即放行」读路径 |
| `EXISTING_TABLE_CHECK_CACHE_TTL_SECONDS` | `0` | 表存在性缓存 TTL（秒）；`0` = 实时查询不缓存 |
| `REGISTRY_CACHE_REFRESH_SECONDS` | `300` | 注册表缓存周期性重载间隔（秒）；`0` = 禁用 |
| `TABLE_NAME_PATTERN` | `^kline_[a-z0-9]{3,20}_([0-9]+[mhdw]|1M)$` | **已有**，沿用 |
| `SYMBOL_FORMAT_PATTERN` | `^[A-Z0-9]{3,20}$` | **已有**，沿用 |
| `FIXED_SYMBOLS` / `SYMBOLS` / `COLLECT_INTERVALS` | 现有 | **已有**，落点见需求 §8.6 |

> 路由/校验层不得出现任何字面量正则或币种白名单，一律从配置读取。

---

## 7. 失败路径与回滚语义矩阵

### 7.1 P0-3 补全路径（改后）

| 失败点 | 是否撤旧单 | 旧保护单状态 | 是否可能裸奔 | `_replenished_symbols` | 下周期 |
|--------|-----------|-------------|-------------|------------------------|--------|
| A3 开关关闭 | 否 | 原样保留 | 否 | 不置位 | 走旧顺序重试 |
| A4 无空头持仓 | 否 | — | 否 | — | 视为成功（NO_POSITION） |
| A5 ensure-active 失败 | 否 | 原样保留 | 否 | 不置位 | 重试 |
| A6 ATR≤0 | 否 | 原样保留 | **否（核心修复）** | 不置位 | 重试 |
| A7 精度失败 | 否 | 原样保留 | 否 | 不置位 | 重试 |
| A8 现价无效 | 否 | 原样保留 | 否 | 不置位 | 重试 |
| A9 计划不可用 | 否 | 原样保留 | 否 | 不置位 | 重试 |
| B1 撤单失败 | 尝试过（可能部分） | 可能部分保留 | 极短暂（旧单部分仍在） | 不置位 | 重试（不挂新单，无叠加） |
| C1-C3 挂单部分失败 | 是（已撤） | 已被撤 | **可能（缺口）** | 不置位 | 重试补挂（AC4） |
| C4 全部成功 | 是 | 已重建 | 否 | **置位** | 跳过 |

> 唯一无法完全消除的窗口是「B1 成功撤单 → 挂新单期间进程崩溃」；由重启后的补全流程 + `_replenished_symbols` 收敛（沿用现有机制，需求 §9.5 已认可）。

### 7.2 P0-1 路径

| 失败点 | 行为 | 标记/回滚 |
|--------|------|----------|
| `_has_open_position` 查 DB 异常 | 保守返回 True → 不注销 | 记 WARNING；下周期重查 |
| `unregister_symbol` 失败 | 保留 `_registered_symbols`（不 discard） | 记 WARNING；P0-1-F4 不误判 |
| ensure-active 全重试失败 | 不进入 ATR → 不撤单 | 记 WARNING + 告警；下周期重试 |
| 服务侧重注册撞 UNIQUE（现状缺陷） | P0-1-F3-a 修复后不再发生 | 未修复前 ensure-active 会失败（故 P0-1服务侧必须落地） |

### 7.3 P0-2 路径

| 失败点 | 行为 |
|--------|------|
| symbol 格式非法 | 4xx，**不执行任何 SQL**（存在性回调不被调用） |
| 表名不匹配 TABLE_NAME_PATTERN | 4xx，不查存在性 |
| 白名单未命中且开关关闭 | 4xx（不回归旧行为） |
| 白名单未命中、开关开、表不存在 | 4xx，不对该表 SELECT |
| 存在性查询异常（DB 不可用） | 4xx/500，不放行（fail-closed） |

---

## 8. 安全论证：为什么「表存在即放行」不引入 SQL 注入

**校验顺序（严进严出，任一不满足即 400，绝不拼 SQL）**：

1. **符号名严格匹配**：`symbol.strip().upper()` 必须 `fullmatch(SYMBOL_FORMAT_PATTERN = ^[A-Z0-9]{3,20}$)`（`validate_symbol_interval_format`，table_name_guard.py:91-107）。该正则**仅允许大写字母与数字**，从字符集上排除空格、单/双引号、分号、括号、`-`、`_`、`*` 等一切注入所需字符 → `BTCUSDT';DROP TABLE x;--`、`1);CROSS JOIN` 等一律在**格式层**被拒。
2. **interval 非空**，且**由表名整体正则隐含约束**：候选表名 `kline_{symbol.lower()}_{interval}` 必须 `fullmatch(TABLE_NAME_PATTERN = ^kline_[a-z0-9]{3,20}_([0-9]+[mhdw]|1M)$)`（`is_valid_table_name`，L110-119）。后缀组 `([0-9]+[mhdw]|1M)` 只允许形如 `15m/1h/4h/1d/1w/1M`，排除注入。
3. **存在性查询参数化**：`SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = :table_name AND table_schema='public')`（复用 `routes.py:_table_exists` L41-49）。**表名以绑定参数传入，绝不拼接**（P0-2-F2）。
4. **后续真实查询**：`SELECT * FROM {table_name}` 中的 `table_name` 是**第 1、2 步严格正则校验后的产物**，非用户原文（既有 R01 设计，保持不变）。

结论：放行与否由「正则白名单 + 参数化存在性查询」共同决定；**放行不会放宽任何一层正则，也不会把 symbol/interval 拼进 SQL**。即使放行，`table_name` 也只可能形如 `kline_<[a-z0-9]{3,20}>_<合法周期>`，无法表达 SQL 结构。`symbol` 的字符集约束（仅 `A-Z0-9`）是先决条件，故「表存在即放行」不引入新注入面。

---

## 9. 影响面与镜像重建范围结论

| 变更集 | 目录 | 触发重建镜像 |
|--------|------|-------------|
| P0-3 + P0-1策略侧 + 硬编码提取 | `strategies/new_coin/**` | `trading-new-coin` |
| P0-2 + P0-1服务侧 | `services/kline_service/**` | `trading-kline-service` |
| （未改）root `shared/**` | — | — |

**结论（A2）**：仅重建 **2 个镜像**（`trading-kline-service`、`trading-new-coin`），**不触发全量重建**。

- 向后兼容：合法请求行为不变；非法请求仍拒绝（P0-2-AC4）；`build_kline_table_name` 写路径语义不变。
- 注意：kline-service 重建会**短暂中断** K 线服务，所有策略的 K 线读取在重建窗口内可能失败（需求 §11.3）。因此 P0-3（new-coin）**先上线**，保证即使 kline 短暂不可用也不撤单；kline-service 重建窗口内的 ATR 失败不再造成裸奔。
- 存量裸奔持仓（`USDBRLUSDT`/`ACNUSDT`）的解除路径：**P0-2 上线后**，kline 对「表已存在」标的返回 200 → 下一周期 `_calculate_atr` 成功 → P0-3 流程挂齐 SL/TP1/TP2。若 P0-2 未上线，仅 P0-1 的 ensure-active 也可能恢复（但受 §3.3 的 UNIQUE 缺陷制约，故 P0-2 更可靠）。

---

## 10. 分阶段实施计划（可独立验证的最小步）

### 10.1 关键顺序论证：P0-3 单独不能解除「当前裸奔」

- 生产现状：`USDBRLUSDT`/`ACNUSDT` 的旧条件单**已被撤且「已不存在」**，当前**无任何保护单**。
- 仅上 P0-3：ATR 仍失败（kline 400）→ 不撤单（也无单可撤）→ **仍无保护单**。P0-3 只是防止**未来**的「撤了挂不回」，不能恢复**已丢失**的保护。
- 因此要恢复当前持仓保护，**必须让 ATR 成功**：首选 **P0-2**（与注册态无关，最可靠），或 P0-1 的 ensure-active（依赖服务侧 F3 修复）。
- 综合：**P0-3 先上（止血、防新窗口）→ P0-2 随后（恢复读取、自然补单）**；P0-1 与 P0-2 同批（均为 kline-service 重建）。

### 10.2 批次划分

| 批次 | 内容 | 重建镜像 | 前置依赖 | 验证 |
|------|------|---------|---------|------|
| **第一批** | P0-3（先算后撤）+ P0-1 策略侧（有持仓不注销 + ensure-active）+ 硬编码提取 | `trading-new-coin` | 无 | §10.3 命令 A/B |
| **第二批** | P0-2（表存在放行）+ P0-1 服务侧（幂等复位 status + 回传真实 status + 缓存刷新 + UNIQUE 修复） | `trading-kline-service` | 第一批已上线 | §10.3 命令 C/D/E |

> 两批各自一个 commit、可分别 push；也可一次 push（一次 Action Run 同时重建 2 镜像）。**建议分批**以缩小 kline-service 中断窗口的影响面并逐批验证。

### 10.3 每步验收命令

**本地（mock/单测，禁止服务器回测）**

```bash
# 第一批：P0-3 先算后撤 + P0-1 策略侧
pytest -q strategies/new_coin/tests/ -k "replenish or P0_3 or atr or registration or ensure_active or P0_1"

# 第二批：P0-2 白名单/表存在/注入拒绝 + P0-1 服务侧
pytest -q services/kline_service/tests/ -k "table_name or whitelist or P0_2 or registry or P0_1"

# 全量回归
pytest -q
```

**服务器只读核对（对应需求 §10.2）**

```bash
# A) new_coin 日志：不再出现「K线服务请求失败: 400」「ATR计算失败，跳过补全条件单」
ssh root@43.156.242.184 "docker logs --since 30m trading_system-new_coin 2>&1 | grep -E 'K线服务请求失败: 400|ATR计算失败，跳过补全|条件单全部补全完成' | tail -20"

# B) 交易所侧确认两笔持仓挂齐保护单
ssh root@43.156.242.184 "docker logs --since 30m trading_system-new_coin 2>&1 | grep -E '补全止损条件单成功|补全 TP1|补全 TP2|条件单全部补全完成' | tail -20"

# C) 表已存在标的的 K 线查询：400 → 200（P0-2-AC1）
ssh root@43.156.242.184 "docker exec trading_system-kline sh -c \"curl -s -o /dev/null -w '%{http_code}\\n' 'http://127.0.0.1:8000/api/v1/klines/latest?symbol=USDBRLUSDT&interval=1h&limit=18'\""
ssh root@43.156.242.184 "docker exec trading_system-kline sh -c \"curl -s -o /dev/null -w '%{http_code}\\n' 'http://127.0.0.1:8000/api/v1/klines/latest?symbol=ACNUSDT&interval=1h&limit=18'\""
# 期望：两条均 200

# D) 注入用例必须被拒（P0-2-F2）
ssh root@43.156.242.184 "docker exec trading_system-kline sh -c \"curl -s -o /dev/null -w '%{http_code}\\n' \\\"http://127.0.0.1:8000/api/v1/klines/latest?symbol=BTCUSDT';DROP%20TABLE%20x;--&interval=1h\\\"\""
# 期望：4xx

# E) 注册表状态核对（P0-1）：有持仓标的最新一次注册后应为 active
ssh root@43.156.242.184 "docker exec trading_system-postgres psql -U trading_user -d trading_platform -c \"SELECT symbol,status,registered_at,expires_at FROM registered_symbols WHERE symbol IN ('ACNUSDT','USDBRLUSDT');\""
```

---

## 11. 与需求 AC 的逐条映射

| AC | 设计对应 | 验证层 |
|----|---------|--------|
| P0-1-AC1 有持仓不注销 | §3.1 `_has_open_position` + `_unregister_if_safe` | new-coin 单测/服务器 B、E |
| P0-1-AC2 cancelled 持仓取 K 线前 ensure-active → ATR 成功 | §3.2 ensure-active + §3.3 F3；P0-2 兜底 | new-coin 单测/服务器 A |
| P0-1-AC3 重注册 cancelled 行 → 落库 active，返回体 status=='active' | §3.3 F3-a/b/c（含 UNIQUE 修复） | kline-service 单测/服务器 E |
| P0-1-AC4 无持仓且过龄仍注销 | §3.1 guard 为假时 `_unregister_if_safe` 照旧注销 | new-coin 单测 |
| P0-1-AC5 ensure-active 失败不进 ATR、不撤旧单、告警 | §3.2 return False + §5.2 A5 在撤单前 | new-coin 单测 |
| P0-1-AC6 `_registered_symbols` 与真实态一致 | §3.1 成功才 discard；失败保留 | new-coin 单测 |
| P0-2-AC1 表存在 + cancelled → 200 带数据 | §4.2 放行判定 3) | kline-service 单测/服务器 C |
| P0-2-AC2 非法 symbol → 4xx，不执行 SQL | §8 步骤 1；断言存在性回调未被调用 | kline-service 单测 |
| P0-2-AC3 合法但无白名单且无表 → 4xx | §4.2 放行判定 4) False 分支 | kline-service 单测 |
| P0-2-AC4 BTCUSDT/1h 行为不变 | §4.2 白名单命中即返回（原逻辑） | kline-service 单测/回归 |
| P0-2-AC5 DB 不可用 → fail-closed | §4.3 异常不缓存 + 拒绝 | kline-service 单测 |
| P0-2-AC6 表名正则单测 | `is_valid_table_name` 复用（L110-119） | kline-service 单测 |
| P0-2-AC7 `/indicators` 与 `/klines/latest` 一致 | 两路由共用 `_validated_read_table_name` | kline-service 单测 |
| P0-3-AC1 ATR=0 → 不撤单、return False、告警 | §5.2 A6 | new-coin 单测 |
| P0-3-AC2 ATR 正常 → 先算完再撤 → 挂成功 → True | §5.2 A6→B1→C4 | new-coin 单测 |
| P0-3-AC3 撤单失败 → 不挂新单、失败、告警 | §5.2 B1 | new-coin 单测 |
| P0-3-AC4 撤成功但 TP1 失败 → False、不置位、下周期重试 | §5.2 C1-C3、C4 | new-coin 单测 |
| P0-3-AC5 无空头持仓 → 不撤不挂，返回「无需补全」 | §5.2 A4 | new-coin 单测 |
| P0-3-AC6 连续两周期幂等 | §5.4 `_replenished_symbols` | new-coin 单测 |

---

## 12. 额外发现与风险（需求文档未覆盖）

1. **★服务重启后「重注册撞 UNIQUE」潜在缺陷（P0-1 直接相关）**：`_load_from_database` 只加载 `status='active'`（`registry.py:51`），而 `register()` 仅查内存 `_cache`（L88）。服务重启后对已 cancelled 行重注册 → 走 INSERT → 撞 `symbol ... UNIQUE`（`create_registered_symbols_table.py:11`）→ 注册失败 → ATR 失败。**P0-1 的 ensure-active 依赖此修复，必须在第二批落地**（§3.3 F3-a）。
2. **hrs 补单的相反缺陷**：`_cancel_old_orders_for_replenish`（`hrs/executor.py:1034-1046`）撤单失败**仅告警但仍继续重建**（L1041-1046），可能导致新旧保护单叠加（与 P0-3-F3「撤单失败不挂新单」相反）。hrs 不在本轮，**建议列入后续 R 系列**。
3. **`_replenish_done` 的「全或无」语义**：单个长期失败持仓使 `_replenish_done` 恒 False，每周期遍历全部持仓（已成功者靠 `_replenished_symbols` 跳过，无重复下单）。修复后自然收敛，无需改动，但应知悉。
4. **`_replenish_done` 触发条件**：补全仅在策略**首次未完成周期**进入（strategy.py:492）；若本修复部署时进程已运行且 `_replenish_done=False`（正常，因一直失败），下周期即重试——无需重启容器即可恢复。**但若部署会重启 new-coin 容器则更彻底**（进程内 `_replenished_symbols`/`_replenish_done` 重置）。
5. **`skip_symbols`（mctps_symbols）与 FIXED_SYMBOLS 的口径**：`executor.py:3236` 的 6 币种与 kline 配置 `FIXED_SYMBOLS` 一致，但二者是**独立配置**，存在漂移风险；建议编码时以配置项互校（可选，不扩大本轮范围）。
6. **`_get_collect_minutes` 硬编码 INTERVAL_MINUTES**（`registry_routes.py:99-103`）：属 kline-service 既有硬编码（interval→分钟表），本轮**不改**（避免扩大 kline-service 改动面），标记为后续清理项。
7. **ATR 无交易所兜底（需求 Q5）**：`_calculate_atr`（executor.py:1601-1664）kline 失败即返回 0，而 hrs 有币安兜底。本轮**不实现**；P0-2 上线后 kline 对「表已存在」标的可用，可覆盖存量持仓。**建议后续为 new_coin ATR 加币安兜底**，彻底消除「kline 不可用即无法补全」。
8. **陈旧数据语义（§4.4）**：存在性放行不校验数据新鲜度；建议后续增加「最新 K 线时间过旧 → WARNING」，本轮不做。

---

## 13. 待办清单（供 python-engineer 原子执行）

### 第一批（`strategies/new_coin/**` → 重建 `trading-new-coin`）

| ID | 任务 | 涉及文件 | 依赖 | 对应 AC |
|----|------|---------|------|---------|
| T01 | 新增 `_has_open_position` / `_unregister_if_safe`；L776-784、L653-662 改用守卫 | `strategies/new_coin/strategy.py` | — | P0-1-AC1/AC4/AC6 |
| T02 | 新增 `_ensure_symbol_active`（配置化重试）；Phase A 中调用 | `strategies/new_coin/executor.py` | — | P0-1-AC2/AC5 |
| T03 | `replenish_conditional_orders` 重排为三阶段（拆 `_prepare_replenish_plans`/`_execute_replenish_plans`）；撤单失败阻断挂新单 | `strategies/new_coin/executor.py` | T02 | P0-3-AC1..AC6 |
| T04 | 硬编码提取：`min_notional`/`ignore_error_codes`/`skip_symbols` | `strategies/new_coin/executor.py`、`config.yaml` | T03 | 硬编码约束 |
| T05 | new_coin 单测（含 P0-1/P0-3 全部 AC） | `strategies/new_coin/tests/` | T01-T04 | §11 |

### 第二批（`services/kline_service/**` → 重建 `trading-kline-service`）

| ID | 任务 | 涉及文件 | 依赖 | 对应 AC |
|----|------|---------|------|---------|
| T06 | 新增 `build_readable_table_name` + `build_table_name_by_format`；config 加 `ALLOW_EXISTING_TABLE_SYMBOLS`/`EXISTING_TABLE_CHECK_CACHE_TTL_SECONDS` | `core/table_name_guard.py`、`shared/core/config.py` | — | P0-2-AC1..AC7 |
| T07 | `/klines/latest`、`/indicators` 改「先连库→读路径入口」，注入参数化存在性回调；写路径保持不变 | `api/routes.py` | T06 | P0-2-AC1..AC7 |
| T08 | `register()` 存在性判定加 DB 兜底（修 UNIQUE）；`_reactivate` 后回读/确保返回真实 status；`refresh_active` + 周期刷新；config 加 `REGISTRY_CACHE_REFRESH_SECONDS` | `core/registry.py`、`api/registry_routes.py`、`shared/core/config.py` | — | P0-1-AC3 |
| T09 | kline-service 单测（表名正则/注入拒绝/存在性放行/DB 异常/注册幂等） | `services/kline_service/tests/` | T06-T08 | §11 |

> 幻觉测试 10 项 + 覆盖率验证在两批完成后各执行一次；T01-T05 与 T06-T09 可分两次 push（各一次 Action Run），或一次 push。

---

（文档结束）
