# HRS 策略保护单管理缺陷修复 — 技术设计文档

> **文档类型：** 技术设计文档（方案类）
> **适用策略：** HRS（混合反转策略）
> **关联需求：** [PRD-HRS保护单管理修复.md](PRD-HRS保护单管理修复.md)
> **文档状态：** 待评审
> **创建日期：** 2026-08-20

---

## 1. 设计总览

### 1.1 设计目标

对应 PRD FR-01~09，修复三大缺陷：

| 缺陷 | 根因定位（代码取证） | 对应 FR |
|------|---------------------|---------|
| A 保护单不完整 | SL/TP1/TP2 逐个下单无原子性、补单失败静默忽略、`add_algo_id` 在持仓未建立时写入丢失、`has_algo_id` 依赖可能失效的本地记录 | FR-01/02/05/06 |
| B 反向开仓 | 开仓前只查本地内存持仓、对账不接管交易所持仓 | FR-03/04 |
| C 条件单重复累积 | algoIds 不持久化、补单前取消依赖本地记录、取消失败不阻断 | FR-05/06/07/08/09 |

### 1.2 核心设计原则

1. **保护单创建收敛为统一入口**：四个创建入口（execute_short/execute_long/replenish_position_orders/add_to_position）收敛到 `_place_protection_order`，消除重复代码。
2. **取消与下单分离**：`cancel_all_orders`（批量取消）在保护单集合创建流程开头执行一次，`_place_protection_order` 只负责下单+记录 algoId，不承担取消。原因：交易所批量取消是 symbol 级而非 role 级，若放在单角色方法内部，会出现「SL 下单后被 TP1 的批量取消误删」的连环错误。
3. **交易所为准，本地为镜像**：开仓、补单、接管一律以交易所持仓/条件单事实为准，本地 `algo_ids` 仅作加速与审计。
4. **保守失败策略**：查询失败拒绝开仓、取消失败阻断加仓、接管失败告警，宁可错过不可失控。
5. **无硬编码**：新增开关、阈值全部从 config.yaml 读取。

---

## 2. 关键类/方法签名设计

### 2.1 新增结果类型（executor.py 模块顶部）

```python
@dataclass
class OpenResult:
    """开仓执行结果：分离「开仓成交」与「保护单完整性」两个独立信号"""
    order_filled: bool             # 开仓是否成交（决定是否记录持仓）
    protection_complete: bool      # 三类保护单是否完整创建
    failed_roles: List[str]        # 创建失败的保护单角色（"sl"/"tp1"/"tp2"）
    order: Optional[Dict[str, Any]] = None  # 开仓订单信息

@dataclass
class ReplenishResult:
    """补单执行结果：区分「已存在跳过」与「下单失败」"""
    success: bool                  # 整体是否成功（任一角色失败则为 False）
    failed_roles: List[str]        # 下单失败的角色
    placed: int = 0                # 成功下单数量
    note: str = ""                 # 特殊状态（沿用 "price_past_tp2" 语义）
```

### 2.2 TradingExecutor（executor.py）

#### 2.2.1 新增统一下单入口（FR-05）

```python
async def _place_protection_order(
    self, symbol: str, direction: str, role: str, side: str, order_type: str,
    stop_price: Decimal, limit_price: Decimal, quantity: Decimal,
    close_position: bool = False,
) -> Dict[str, Any]:
    """
    创建单个保护单（幂等入口）
    - 数量 <= 0 时视为「跳过」（非失败）
    - 下单成功且返回 algoId → 写入本地记录
    - 下单失败 → 返回 error，不抛异常（由调用方收集 failed_roles）
    返回: {"success": bool, "skipped": bool, "algo_id": int|None, "error": str|None}
    """
```

要点：
- 唯一调用 `place_conditional_order` 的保护单下单点。
- 下单成功但未返回 algoId → 记为失败（`error="下单成功但未返回algoId"`），纳入 `failed_roles`。

#### 2.2.2 新增保护单集合创建（FR-01，收敛 execute_short/long 的三段重复逻辑）

```python
async def _create_protection_orders(
    self, symbol: str, direction: str, entry_price: float, atr: float,
    quantity: Decimal, tick_size: Decimal, step_size: Decimal,
    params: Dict[str, Any],
) -> List[str]:
    """创建 SL/TP1/TP2 三类保护单并收集结果。返回: 失败角色列表（空=完整）"""
```

价格计算拆为独立纯函数：

```python
def _calc_sl_price(self, direction, entry_price, atr, tick_size, params) -> float
def _calc_tp_price(self, direction, target, entry_price, atr, tick_size, params) -> Decimal
def _calc_tp_qty(self, quantity, close_percent, step_size) -> Decimal
```

#### 2.2.3 改造 `execute_short` / `execute_long`（L204-L341 / L343-L480）

- 返回类型改为 `OpenResult`。
- 保护单创建段整体替换为 `_create_protection_orders`。
- 开仓流程不调用批量取消（新开仓应无残留；残留由补单流程清场）。
- 返回 `OpenResult(order_filled=True, protection_complete=not failed_roles, failed_roles=failed_roles, order=order)`。

#### 2.2.4 改造 `replenish_position_orders`（L657-L819，FR-02/09）

- 返回类型改为 `ReplenishResult`。
- 入口先执行 `await self.position_manager.cancel_all_orders(symbol)`（批量取消清场）。
- 取消后本地 `algo_ids` 已清空 → SL/TP1/TP2 全量重建（`target1_reached`/`target2_reached` 逻辑不变）。
- 每个角色调用 `_place_protection_order`，原 `except: logger.debug("可能已存在")` 删除，改为：skipped → info；失败 → error + 计入 `failed_roles`。
- 保留 `"price_past_tp2"` 语义：返回 `ReplenishResult(success=True, note="price_past_tp2")`。

#### 2.2.5 改造 `add_to_position`（L821-L1000，FR-08）

- L882-L893 替换为：

```python
cancel_result = await self.position_manager.cancel_all_orders(symbol)
if cancel_result["failed"] > 0:
    logger.error("加仓取消旧条件单失败，中止重下", symbol=symbol, result=cancel_result)
    return None
```

- 取消失败 → 不调用任何 `place_conditional_order`，返回 None，等待下一轮补单。
- 重下 SL/TP1/TP2 复用 `_place_protection_order`，quantity 用交易所最新持仓总量。

### 2.3 PositionManager（position_manager.py）

#### 2.3.1 修复 `add_algo_id` 隐藏 bug（pending 缓冲）

现状：execute_short/long 内创建保护单时调 `add_algo_id`，此刻 `add_position` 尚未执行，`_positions` 无该 symbol → 内存写入被静默丢弃。

```python
self._pending_algo_ids: Dict[str, Dict[str, int]] = {}  # symbol -> {role: algoId}

def add_algo_id(self, symbol, role, algo_id):
    pos = self._positions.get(symbol)
    if pos is not None:
        pos.setdefault("algo_ids", {})[role] = algo_id
    else:
        self._pending_algo_ids.setdefault(symbol, {})[role] = algo_id
```

`add_position` 初始化 algo_ids 时合并 pending：

```python
"algo_ids": self._pending_algo_ids.pop(symbol, {}),
```

#### 2.3.2 新增 `clear_algo_ids`（FR-05/07）

```python
def clear_algo_ids(self, symbol: str) -> None:
    """清空本地 algo_ids 记录（批量取消成功后调用）"""
    pos = self._positions.get(symbol)
    if pos:
        pos["algo_ids"] = {}
```

#### 2.3.3 强化 `cancel_all_orders`（L341-L376，FR-07）

```python
async def cancel_all_orders(self, symbol: str) -> Dict[str, Any]:
    """
    取消该交易对所有 OPEN 条件单
    - 优先调用 binance_api.cancel_all_algo_orders(symbol)（批量，仅统一账户）
    - 批量不可用/失败 → 回退按本地 algo_ids 逐个取消，记录 warning
    - 任一方式成功 → 清空本地 algo_ids
    返回: {"total": int, "cancelled": int, "failed": int, "method": "batch"|"individual"}
    """
```

要点：
- 捕获 `ValueError`（非统一账户）→ warning 回退逐个取消；捕获其他异常 → 同样回退。
- 批量成功后 `clear_algo_ids(symbol)`。
- 该方法为唯一取消失败判定源，供 FR-08 阻断与 FR-09 补单清场使用。

### 2.4 HRSStrategy（strategy.py）

#### 2.4.1 `__init__` 读取新配置（L141 附近）

```python
entry_config = trading_config.get("entry", {})
self._reject_on_exchange_position = entry_config.get("reject_on_exchange_position", True)
# FR-04（2026-08-20 二次修订）：对非本策略持仓仅告警、不接管。
# 不再读取任何接管开关（无配置项），代码中不保留接管路径。
```

#### 2.4.2 新增开仓前置检查（FR-03）

```python
async def _get_exchange_position_status(self, symbol: str, direction: str) -> str:
    """返回: "none"=无持仓 / "opposite"=反向持仓 / "same"=同向持仓 / "error"=查询失败"""

async def _check_exchange_position_allows_entry(self, symbol: str, direction: str) -> bool:
    """反向持仓或查询失败 → 拒绝（False）；无持仓 → 放行；同向 → 放行（交由加仓判定）"""
```

`execute_signal`（L377-L387 后）插入：

```python
if not await self._check_exchange_position_allows_entry(symbol, direction):
    return False
```

同向持仓处理：`"same"` 时走加仓判定（本地无 pos 则保守跳过）。

#### 2.4.3 改造 `execute_signal` 开仓结果处理（L460-L477，FR-01）

```python
result = await self.trading_executor.execute_short(...)
if result.order_filled:
    self.position_manager.add_position(...)
    self.risk_manager.record_open(direction)
    await self._save_state()
    if not result.protection_complete:
        await self._replenish_single_position(symbol)
        await self._send_anomaly_alert(
            f"HRS开仓保护单不完整：{symbol} {direction} 失败角色={result.failed_roles}"
        )
    return True
return False
```

#### 2.4.4 新增 `_send_anomaly_alert`（FR-01/04 共用）

```python
async def _send_anomaly_alert(self, message: str) -> None:
    if not self._should_notify("anomaly_alert"):
        return
    try:
        await self.notification_client.send(message=message, level="warning", project="hrs")
    except Exception as e:
        logger.warning("发送异常告警失败", error=str(e))
```

#### 2.4.5 改造 `_reconcile_positions`（L2309-L2321，FR-04）

「交易所有而本地无」分支为**仅告警、不接管**（2026-08-20 二次修订：HRS 与其他策略共用账户，接管会误伤其他策略持仓，如 XRPUSDT 曾下 12 单。故**彻底删除接管路径与配置项**，硬性规定无配置开关）：

```python
for symbol, exch_pos in exchange_pos_map.items():
    if symbol in local_positions:
        continue
    # 2026-08-20 硬性规定：仅告警、不接管（无配置开关，无备用接管路径）
    logger.warning(
        "发现非本策略持仓，不接管，请手动处理",
        symbol=symbol, direction=exch_pos["direction"],
        position_amt=exch_pos["position_amt"], entry_price=exch_pos["entry_price"],
    )
```

#### 2.4.6 改造 `_save_state` / `_restore_state`（FR-06，见第 3 章）

#### 2.4.7 `_replenish_single_position`（L2941-L2960，FR-09）

- L2943 的 `cancel_all_orders` 调用保持不变（强化版已内部接入批量取消）。
- L2958 适配新返回值：`result == "price_past_tp2"` → `result.note == "price_past_tp2"`。

---

## 3. 数据库迁移（FR-06）

### 3.1 `hrs_positions` 表加 `algo_ids JSONB` 列

`_ensure_db_schema`（strategy.py L1924-L1933）中：

1. 建表语句追加列（新库）：
```sql
CREATE TABLE IF NOT EXISTS hrs.hrs_positions (
    symbol TEXT,
    direction TEXT,
    entry_price DOUBLE PRECISION,
    quantity DOUBLE PRECISION,
    entry_time BIGINT,
    algo_ids JSONB,            -- FR-06: 条件单 algoId 映射 {role: algoId}
    PRIMARY KEY (symbol, direction)
)
```
2. 幂等迁移（存量库升级关键）：
```sql
ALTER TABLE hrs.hrs_positions ADD COLUMN IF NOT EXISTS algo_ids JSONB
```

### 3.2 `_save_state` 写入 algoIds（L2516-L2528）

```python
algo_ids_json = json.dumps(pos.get("algo_ids", {}), ensure_ascii=False)
INSERT INTO hrs.hrs_positions (symbol, direction, entry_price, quantity, entry_time, algo_ids)
VALUES ($1, $2, $3, $4, $5, $6::jsonb)
ON CONFLICT (symbol, direction) DO UPDATE
SET entry_price = $3, quantity = $4, entry_time = $5, algo_ids = $6::jsonb
```

### 3.3 `_restore_state` 读取 algoIds（L2058-L2060 / L2073-L2086）

```python
SELECT symbol, direction, entry_price, quantity, entry_time, algo_ids FROM hrs.hrs_positions
# 恢复循环内：
if row.get("algo_ids"):
    algo_ids = row["algo_ids"]
    if isinstance(algo_ids, str):
        algo_ids = json.loads(algo_ids)
    pos["algo_ids"] = algo_ids or {}
```

> 实施检查点：确认 `DatabaseManager.execute` 对 `::jsonb` 参数绑定与返回值类型（dict 或 str）的处理。

### 3.4 JSONB 兜底路径

`position_manager.from_dict`（L444-L462）已包含 `algo_ids` 序列化，与独立表恢复路径行为一致。

---

## 4. 配置项新增（config.yaml）

```yaml
trading:
  entry:
    reject_on_exchange_position: true    # FR-03 开仓前核对交易所实际持仓；反向或查询失败禁止开仓
  reconcile:
    # FR-04（2026-08-20 二次修订）：无接管配置项。对非本策略持仓仅告警、不接管（硬性规定）
  entry_timeout:
    fill_tolerance: 0.9999               # 订单完全成交判定容差（已成交/委托 ≥ 该比例视为完全成交）
  position_detection:
    zero_qty_threshold: 0.0001           # 持仓数量低于该值视为全部平仓/零持仓
    tp1_filled_ratio: 0.9                # 交易所持仓量 < 初始开仓量 × 该比例 视为 TP1 已成交
atr:
  min_recalc_klines: 15                  # ATR重新计算所需最少K线数
```

读取路径（strategy.py `__init__`）：`trading.entry.reject_on_exchange_position`。
读取路径（executor.py `__init__`）：`trading.entry_timeout.fill_tolerance`。
读取路径（position_manager.py / strategy.py `__init__`）：`trading.position_detection.zero_qty_threshold`、`trading.position_detection.tp1_filled_ratio`。
读取路径（strategy.py `__init__`）：`atr.min_recalc_klines`。
告警开关复用现有 `notification.events.anomaly_alert`（默认 true）。

---

## 5. 异常处理策略

| 场景 | 策略 | 级别 | 对应 FR |
|------|------|------|---------|
| 开仓时单个保护单下单抛异常 | 收集进 `failed_roles`，不中断其余角色；返回 `protection_complete=False` | error + warning 告警 | FR-01/02 |
| 补单时下单失败 | 计入 `failed_roles`，返回 `success=False`，不静默忽略 | error | FR-02 |
| 下单成功但未返回 algoId | 视为失败 | error | FR-05 |
| 查询交易所持仓失败（开仓前） | 保守拒绝开仓 | warning | FR-03 |
| 批量取消接口不可用/失败 | 回退逐个取消 + 告警 | warning | FR-07 |
| 加仓批量取消失败 | 阻断重下保护单，返回 None，等待下轮补单 | error | FR-08 |
| 对账发现「交易所有而本地无」持仓 | 仅告警、不接管（硬性规定） | warning | FR-04 |
| 开仓成交但保护单不完整 | 仍记录持仓 + 立即补单 + 告警 | warning 告警 | FR-01 |
| pending algoId 缓冲 | `add_algo_id` 持仓未建立时写 pending，`add_position` 合并 | info | FR-01/06 |

统一原则：所有失败路径必须有日志（区分 error/warning）+ 可被测试断言，禁止 `except: pass` / debug 掩盖。

---

## 6. 改造点清单（精确到行号）

### 6.1 executor.py

| 位置 | 现状 | 改造 |
|------|------|------|
| L1-L16 模块头部 | 仅导入 | 新增 `from dataclasses import dataclass`；新增 `OpenResult`/`ReplenishResult` |
| L204-L341 `execute_short` | 逐一下单、整体 try | 返回 `OpenResult`；L261-L322 替换为 `_create_protection_orders` |
| L343-L480 `execute_long` | 同上 | 同上；L400-L461 替换 |
| L657-L819 `replenish_position_orders` | 返回 bool/字符串 | 返回 `ReplenishResult`；入口加 `cancel_all_orders`；L718/L756/L807 改失败统计 |
| L787 `return "price_past_tp2"` | 字符串 | 改为 `ReplenishResult(note="price_past_tp2")` |
| L821-L1000 `add_to_position` | 逐个取消不阻断 | `cancel_all_orders` + 失败阻断（FR-08）；L914-L980 复用 `_place_protection_order` |
| 新增 | — | `_place_protection_order`、`_create_protection_orders`、`_calc_sl_price`、`_calc_tp_price`、`_calc_tp_qty` |

### 6.2 position_manager.py

| 位置 | 现状 | 改造 |
|------|------|------|
| L73 附近 `__init__` | `_positions`、`_last_tracked_qty` | 新增 `self._pending_algo_ids` |
| L86-L125 `add_position` | `algo_ids: {}` 硬编码初始化 | 合并 `_pending_algo_ids.pop(symbol, {})` |
| L274-L291 `add_algo_id` | pos 不存在时丢弃 | 写 pending 缓冲 |
| L341-L376 `cancel_all_orders` | 仅按本地逐个取消 | 优先批量取消（FR-07），失败回退，成功清空本地 |
| 新增 | — | `clear_algo_ids(symbol)` |

### 6.3 strategy.py

| 位置 | 现状 | 改造 |
|------|------|------|
| L141-L142 `__init__` | 读取通知配置 | 新增读取开仓前置检查开关（FR-03）；FR-04 无接管开关 |
| L377-L387 `execute_signal` | 仅本地 has_position | 插入 FR-03 交易所持仓核对 |
| L442-L477 `execute_signal` | `if order:` 判定 | 基于 `OpenResult`；不完整时补单 + 告警（FR-01） |
| L1924-L1933 `_ensure_db_schema` | hrs_positions 无 algo_ids | 建表加列 + ALTER 幂等迁移 |
| L2058-L2060 `_restore_state` | SELECT 不含 algo_ids | SELECT 加 algo_ids |
| L2073-L2086 `_restore_state` | 恢复不读 algo_ids | 恢复 algo_ids 字典 |
| L2309-L2321 `_reconcile_positions` | 自动接管（旧实现） | 仅告警、不接管（FR-04，硬性规定，无配置开关） |
| L2516-L2528 `_save_state` | INSERT 不含 algo_ids | UPSERT 加 algo_ids JSONB |
| L2943 `_replenish_single_position` | 调 cancel_all_orders（旧实现） | 调用点不变，方法内部已强化（FR-09） |
| L2958 `_replenish_single_position` | `result == "price_past_tp2"` | 适配 `result.note` |
| 新增 | — | `_get_exchange_position_status`、`_check_exchange_position_allows_entry`、`_send_anomaly_alert` |

### 6.4 config.yaml

`trading:` 下新增 `entry.reject_on_exchange_position`（FR-03）。FR-04 无接管配置项（硬性规定，2026-08-20 二次修订）。

### 6.5 binance_api.py（只读确认，不改）

- `cancel_all_algo_orders`（L899-L926）已存在，仅统一账户。
- `get_position(symbol)`（L825-L832）直接复用。
- HRS 已确认运行在统一账户（PM）模式，批量取消可用。

---

## 7. 测试要点

### 7.1 幻觉测试（编码完成后、功能测试前）
- import 有效性：`cancel_all_algo_orders`、`OpenResult`、`ReplenishResult` 存在。
- API 参数核对：`place_conditional_order`、`cancel_all_algo_orders(symbol)`、`get_position(symbol)` 签名一致。
- 配置项存在性：`reject_on_exchange_position` 在 config.yaml 真实存在；FR-04 无接管配置项。
- 返回值契约：`execute_short/long` 调用点全部改为 `result.order_filled`；`replenish_position_orders` 调用点适配 `result.note`。
- 异常路径：批量取消失败回退、开仓查询失败保守拒绝、对账仅告警不接管。

### 7.2 单元测试（PRD UT-01~13 + 架构层补充）
| 用例 | 验证点 | 对应 |
|------|--------|------|
| 保护单三类成功 → `protection_complete=True` | FR-01 | UT-01 |
| TP1 抛异常 → `order_filled=True`、`protection_complete=False`、触发补单 + 告警 | FR-01 | UT-02 |
| 补单下单抛异常 → `success=False`、`failed_roles` 非空、error 日志 | FR-02 | UT-03 |
| `add_algo_id` 持仓未建立时写 pending，`add_position` 后合并 | FR-01 隐藏 bug | 新增 |
| 反向持仓拒绝开仓；同向走加仓判定；查询异常保守拒绝；开关 false 跳过 | FR-03 | UT-04/05/06 |
| 对账仅告警：交易所存在持仓、本地无记录 → 不 add_position、不补单、不发接管告警 | FR-04 | UT-07 |
| 同一角色两次创建 → 先取消再下单，交易所仅 1 组 | FR-05 | UT-08 |
| algo_ids 往返一致（含存量表 ALTER 幂等） | FR-06 | UT-09 |
| `cancel_all_orders` 优先批量取消，成功后清空本地 | FR-07 | UT-10 |
| 批量取消抛 ValueError → 回退逐个 + warning | FR-07 | UT-11 |
| 加仓批量取消失败 → 不调用 `place_conditional_order`，返回 None | FR-08 | UT-12 |
| 启动补单先批量取消再补单 | FR-09 | UT-13 |
| `note="price_past_tp2"` 时 strategy 正确标记 target2_reached | FR-09 | 回归 |

### 7.3 集成/回归测试
- 策略重启不丢保护单：重启后先批量取消再补齐，条件单数量不增加。
- 反向开仓拦截：已有做多持仓时模拟做空信号 → 拒绝。
- 对账仅告警：清空 `hrs_positions` 表重启 → 仅 warning 告警，不接管、不补保护单。
- 多轮重启不累积：连续重启 3 次，条件单数量 = 持仓数 × ≤3 组。

### 7.4 覆盖率要求
- 新增/修改代码分支、边界、异常路径覆盖 100%；每个 `try/except` 分支必须有测试；新配置项不同取值组合各覆盖一次。

---

## 8. 风险与实施注意

1. **批量取消误伤同 symbol 非 HRS 条件单**：实施前确认每个交易对仅由 HRS 管理；设计上开仓流程不主动批量取消，仅补单/加仓/对账接管时清场。
2. **非统一账户**：已确认 HRS 运行统一账户（PM），批量取消可用；非统一账户自动回退逐个取消并告警（设计已覆盖）。
3. **`add_to_position` 取消失败阻断可能阻塞加仓**：属预期行为（FR-08），由下一轮补单周期兜底恢复。
4. **函数行数约束**：`execute_short/long` 改造后经 `_create_protection_orders` 与价格计算辅助函数拆分，确保单函数 ≤ 50 行。
5. **JSONB 参数绑定**：实施时核对 `shared/database.py` 对 `$6::jsonb` 与 dict/str 的序列化行为。
6. **存量重复条件单清理**：上线时执行一次性清理脚本（人工确认后 `cancel_all_algo_orders` 并按当前持仓重建）。

---

## 9. 文件变更汇总

| 文件 | 变更类型 | 覆盖 FR |
|------|---------|---------|
| [executor.py](../../strategies/hrs/executor.py) | 修改 | FR-01/02/05/08 |
| [position_manager.py](../../strategies/hrs/position_manager.py) | 修改 | FR-01/05/06/07 |
| [strategy.py](../../strategies/hrs/strategy.py) | 修改 | FR-01/03/04/06/09 |
| [config.yaml](../../strategies/hrs/config.yaml) | 修改 | FR-03/04 配置开关 |
| [binance_api.py](../../shared/binance_api.py) | 只读确认 | FR-07 接口复用 |
| tests/test_position_manager.py、tests/test_fix_verification.py | 修改/新增 | 全部 FR 单测 |
