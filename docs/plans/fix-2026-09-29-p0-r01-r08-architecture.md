# 第一批交易安全问题修复（R01–R08）架构方案

## 1. 文档信息

| 项 | 内容 |
|----|------|
| 文档名称 | 第一批交易安全问题修复（R01–R08）架构方案 |
| 版本 | v1.0 |
| 作者 | backend-architect |
| 创建日期 | 2026-09-29 |
| 基线 commit | `e4912619e513` |
| 上游输入 | `docs/plans/fix-2026-09-29-p0-r01-r08-requirements.md`（需求，已评审）、`docs/reviews/2026-09-29/review.md`（问题来源） |
| 下游环节 | python-engineer（编码实现）→ code-specification-inspector（检测）→ 强制测试 |
| 适用范围 | 仅 R01–R08；R09–R18 不在本轮 |
| 修订 | 2026-09-30 修正 §7.7：R05 同构逻辑抽取到 `shared/protection_retry.py` 共用（推翻原「策略内实现」方案），R05 随之触发全量重建 |
| 约束 | 遵守 `coding-standards.md`（禁止硬编码/重复代码/幽灵参数、单函数≤50 行、单行≤120 字符、中文注释）、`deployment.md`（shared/* 变更触发全量重建）、`CLAUDE.md` |

> 本文档只做架构设计与接口约定，不含业务实现代码；SQL DDL 与接口签名因契约需要而完整给出。

### 1.1 用户已拍板决策（无条件采纳，不再动摇）

| 编号 | 决策 | 落地位置 |
|------|------|---------|
| **D1**（Q3，R03/R04） | 平仓单**强制带 `reduceOnly`**；被拒 `[-2022]` 时先 `_sync_position_with_exchange` 对账 / 先撤同向条件单，再按「剩余待平量」重试 | R03-F3、R04-F3 |
| **D2**（Q4，R05） | 保护补挂达上限仍失败 → **仅告警 + 保持 `protection_pending`（不自动减仓）**；`on_exhausted` 默认 `"alert"` | R05-F4 |
| **D3**（Q5，R06） | 部分成交经精度截断后小于最小下单量或为 0 → **减仓清零（平掉微仓）**；清零失败则告警 | R06-F2 边界 |
| **D4**（Q8，全局） | R02 / R06 / R07 三处最高风险改动统一加 `enabled` 配置开关（默认 `true`） | §5/§9/§10 配置项 |

### 1.2 调度者已定论（直接采纳）

| 编号 | 结论 | 影响 |
|------|------|------|
| **Q1**（R01） | 本轮**不加接口鉴权**（加鉴权会打断 4 个策略容器对 kline-service 的调用，单列一轮）；只做「严格白名单校验 + fail-closed + 建表失败即终止」 | R01-F5 标注为「本轮不做」，`KLINE_API_AUTH_ENABLED` 仅预留不启用 |
| **Q2**（R06） | **抽 `shared/` 公共助手**（R02/R07 本就触发全量重建，无额外部署代价），满足「禁止重复代码」 | R06 助手放 `shared/` |
| **Q6**（R07） | 用 `database/` 目录下 **SQL 迁移脚本** 新建占用表（不引外部 DBA） | 新增 `database/postgres/init-scripts/08-position-claims.sql` |
| **Q7**（R02） | `newClientOrderId` 在「一次交易决策」内生成并复用（由调用方传入或在一次调用内生成，重试复用同一 ID） | R02-F2 |

---

## 2. 总体设计思路

### 2.1 共同根因：订单「受理」与「完全成交/终态」脱节

R02 / R04 / R05 / R06 表面是四个不同 bug，根因是同一个：**现有代码把「下单 API 返回了回执」当作「交易已经完成」，中间缺少统一的「订单终态确认」环节**。

| 编号 | 表现 | 本质 |
|------|------|------|
| R02 | 写请求超时后重试，交易所产生两笔订单 | 「结果未知」= 「未受理」是错误假设；缺稳定幂等标识 |
| R04 | 限价平仓仅收到 NEW 回执即当成功，撤保护单+删持仓 | 「已受理」≠「已成交」，未等待终态 |
| R05 | 入场已成交，保护单失败即 `return False` 丢持仓 | 「下单成功」≠「实物已到手」，状态登记晚于保护创建 |
| R06 | 入场 CANCELED 但 `executedQty>0` 返回 `None` 丢弃 | 终态分类不全，忽略「部分成交」这一终态子类 |

R03 与 R04 同源（平仓重试未重算剩余量、未带减仓约束）；R01 与 R08 属独立边界问题（R01 输入边界、R08 保守判定）。

### 2.2 统一抽象

**核心抽象一：订单终态统一表示 `OrderFillResult`**（消除 `Optional[Dict]` 的语义贫乏）

```
OrderFillResult{
    status:        str      # FILLED | PARTIAL | CANCELED_UNFILLED | EXPIRED_UNFILLED | REJECTED | UNKNOWN | FAILED
    executed_qty:  Decimal  # 累计已成成交量（对账后的权威值）
    orig_qty:      Decimal  # 原始委托量
    remaining_qty: Decimal  # orig_qty - executed_qty（已按 stepSize 截断）
    avg_price:     Decimal  # 成交均价（部分成交时为已成交部分均价）
    order_id:      Optional[int]
    client_order_id: Optional[str]
    is_filled:     bool     # status == FILLED 且 executed_qty ≈ orig_qty
    has_fill:      bool     # executed_qty > 0（含部分成交）
    raw:           Dict     # 交易所原始订单对象（审计用）
}
```

**核心抽象二：统一的「等待订单至终态」助手** `shared/order_fill_waiter.py`

所有「下单 → 等待 → 判定终态」的路径（入场等待、平仓等待、撤单后读最终量、超时后按 clientOrderId 查单）统一走这一个助手，杜绝三处（btc_eth / btc_eth_aggressive / new_coin）重复实现。

**核心抽象三：统一的「减仓平仓」助手** `shared/reduce_only_close.py`

「读真实持仓 → 算剩余待平量 → 带 `reduceOnly` 提交 → `-2022` 前置对账/撤同向单 → 撤单后读最终量累加 → 对账防反向」统一走这一个助手，供 R03 / R04 / R05 减仓兜底复用。

**核心抽象四：统一的「交易执行加固」助手** `shared/protection_retry.py`（2026-09-30 追加）

R05（保护失败不丢态 + 补挂）/ R06（入场终态等待）/ R07（占用薄编排）在 `btc_eth` 与 `btc_eth_aggressive` 形成的约 250 行/文件同构代码统一走这一个助手，两策略仅保留 ≤4 行极薄委托；差异通过「策略实例首参 + 模块级可替换函数引用」注入（详见 §7.7）。

### 2.3 状态流转图

```
                        ┌─────────────────────────────┐
                        │  提交订单（写请求，幂等ID）   │
                        │  newClientOrderId 稳定复用    │
                        └───────────────┬─────────────┘
                                        │
                     ┌──────────────────▼───────────────────┐
                     │ 受理 / 部分成交 (NEW / PARTIALLY_FILLED)│
                     └──────────────────┬───────────────────┘
                                        │ 轮询 get_order / 查终态
        ┌───────────────────────────────┼────────────────────────────────┐
        │                               │                                │
        ▼                               ▼                                ▼
 ┌─────────────┐              ┌──────────────────┐            ┌────────────────────┐
 │ 完全成交     │              │ 超时 → 撤单       │            │ 结果未知(超时/断连) │
 │ FILLED      │              │  -2011/-2013 竞态 │            │                    │
 └──────┬──────┘              └────────┬─────────┘            └─────────┬──────────┘
        │                              │ 撤单成功/竞态后         按 clientOrderId 查单
        │                              ▼ 读最终 executedQty     （有限次重试）
        │                     ┌────────────────────┐           ┌──────────┴──────────┐
        │                     │ CANCELED/EXPIRED    │           │ 查到 → 采用其终态     │
        │                     │  Q=0 → 完全未成交    │           │ 查不到(用尽) → UNKNOWN│
        │                     │  0<Q<orig → 部分成交 │           │ 保留占用+告警，不重发 │
        │                     │  Q=orig → 视同FILLED │           └─────────────────────┘
        │                     └──────────┬─────────┘
        └───────────────┬───────────────┘
                        ▼
        ┌───────────────────────────────────────────────┐
        │ 持仓对账 _sync_position_with_exchange / get_position │
        └──────┬───────────────────┬────────────────────┬───────┘
               │                   │                    │
               ▼                   ▼                    ▼
        ┌────────────┐    ┌──────────────────┐  ┌──────────────────────┐
        │ 归零/达标   │    │ 残留 > 最小可处理量│  │ 残留 < 最小下单量/(=0) │
        │ 归档清理    │    │ 剩余量重算→重试    │  │ 减仓清零(D3)          │
        │ remove_state│    │ 平仓 / 补挂保护    │  │ 清零失败 → 告警        │
        └────────────┘    └──────────────────┘  └──────────────────────┘
```

---

## 3. R01 — K 线查询 SQL 结构注入

### 3.1 改动文件清单

| 文件 | 改动 |
|------|------|
| `services/kline_service/core/table_name_guard.py`（**新增**） | 统一表名解析/校验单词点 |
| `services/kline_service/api/routes.py` | `/klines/latest`（:71/:82-93）、`/indicators`（:164/:181-186）改走 guard；建表失败即终止 |
| `services/kline_service/api/registry_routes.py` | 所有以 symbol/interval 构造表名/采集任务的入口改走 guard |
| `services/kline_service/shared/core/config.py` | 新增 `TABLE_NAME_PATTERN`、`FIXED_SYMBOLS`、（预留）`KLINE_API_AUTH_ENABLED` |
| `services/kline_service/tests/`（新增测试） | 表名正则与注入拒绝单测 |

### 3.2 关键接口签名

```python
# services/kline_service/core/table_name_guard.py
class TableNameValidationError(ValueError):
    """表名/参数校验失败（路由层据此返回 4xx，禁止继续拼 SQL）"""

def build_kline_table_name(
    symbol: str,
    interval: str,
    *,
    registry=None,           # SymbolRegistry（可为 None，降级为固定白名单）
    settings=None,           # Settings 实例（默认取全局 settings）
) -> str:
    """输入 symbol/interval，返回合法表名；任一校验不通过抛 TableNameValidationError。

    校验三层（fail-closed，任一层异常即视为失败）：
      1) 格式层：symbol 匹配 ^[A-Z0-9]{3,20}$、interval 匹配配置化的周期白名单
      2) 白名单层：symbol ∈ (FIXED_SYMBOLS ∪ registry.active ∪ settings.SYMBOLS)；
                   interval ∈ (settings.COLLECT_INTERVALS ∪ registry 各标的 intervals)
      3) 表名层：生成表名匹配 settings.TABLE_NAME_PATTERN
    """
```

### 3.3 状态流转（伪代码）

```
try:
    table_name = build_kline_table_name(symbol, interval, registry=registry)
except TableNameValidationError as e:
    raise HTTPException(400, detail="参数非法")        # 不执行任何 SQL

async with db.get_connection() as conn:
    if not await _table_exists(conn, table_name):
        if not collector:
            return {"code":0, "message":"无数据", "data":[]}   # R01-F3
        try:
            await collector.ensure_table(symbol, interval)
        except Exception:
            logger.warning(...)
            return {"code":0, "message":"数据不可用", "data":[]}  # R01-F3 失败即终止
    rows = await conn.fetch_all(f"SELECT * FROM {table_name} ...", {"limit": limit})
```

### 3.4 配置项落地

| 配置项 | 默认值 | 位置 |
|--------|--------|------|
| `TABLE_NAME_PATTERN` | `^kline_[a-z0-9]{3,20}_([0-9]+[mhdw]\|1M)$` | `config.py`（Pydantic Settings，环境变量可覆盖） |
| `FIXED_SYMBOLS` | `BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,TRXUSDT` | `config.py` |
| `KLINE_API_AUTH_ENABLED` / `KLINE_API_TOKEN` | `false` / `""` | `config.py`（**本轮预留不启用**，Q1） |

> 路由内**不得**出现任何字面量白名单/正则；周期白名单以线上 `registered_symbols.intervals` 实际值为准（需编码前核对：`15m,1h,4h,1d` 是否覆盖 `1w/1M`，见 §8 风险）。

### 3.5 与既有机制衔接

- 复用 `core/registry.py` 的 `SymbolRegistry` 缓存（**实时读取，非启动快照**，满足 R01-AC6/边界）；具体方法名编码前以 `registry.py` 实际暴露的查询接口为准。
- DB 不可用/registry 未初始化 → 降级为「固定标的 ∪ `settings.SYMBOLS`」，且校验层异常一律拒绝（fail-closed）。

### 3.6 验收标准对应

| 验收 | 对应设计 |
|------|---------|
| R01-AC1/AC2 | 格式层+白名单层拦截，`TableNameValidationError` → 4xx，未触达 `fetch_all` |
| R01-AC3 | 建表失败分支 `return {"无数据"}`，不执行 SELECT |
| R01-AC4 | 合法输入行为不变（格式/白名单通过后原逻辑） |
| R01-AC5 | `/indicators`、`/collect/manual`、registry 入口共用 guard，行为一致 |
| R01-AC6 | `TABLE_NAME_PATTERN`（配置）单测覆盖合法/注入样例 |

### 3.7 重复代码抽取

所有入口（`/klines/latest`、`/indicators`、`/collect/manual`、`registry_routes`）统一调用 `build_kline_table_name`，删除各处 `f"kline_{symbol.lower()}_{interval}"` 内联拼接。

---

## 4. R02 — 非幂等下单被通用重试器重复提交

### 4.1 改动文件清单

| 文件 | 改动 |
|------|------|
| `shared/utils.py` | `retry_on_failure`（:18-107）保留但**不再装饰 `_request`**；新增读/写分级装饰或保留供读请求复用 |
| `shared/binance_api.py` | `_request`（:195）拆分为 `_request_read`（带重试）/ `_request_write`（不盲重发 + 未知结果核对）；`place_order`（:427）新增 `client_order_id` 参数并生成 `newClientOrderId`；新增 `get_order_by_client_id` |
| `shared/api_retry_config.yaml`（**新增**） | 读/写重试、幂等、查单参数 |
| 各 `place_order` 调用方（btc_eth / aggr / new_coin / hrs / grid） | 兼容（签名扩容，可选传 `client_order_id`；不传则内部生成） |

### 4.2 关键接口签名

```python
# shared/binance_api.py
async def _request(self, method: str, endpoint: str, params=None, signed=True) -> Dict:
    """路由分发：GET → _request_read（可重试）；POST/DELETE → _request_write（不盲重发）"""

async def _request_read(self, method, endpoint, params, signed) -> Dict: ...  # 沿用 read 重试语义

async def _request_write(self, method, endpoint, params, signed, *,
                         idempotency_key: Optional[str] = None) -> Dict:
    """写请求：默认不重试；仅当结果未知时按 idempotency_key 核对，确认未受理才允许重发"""

async def place_order(self, symbol, side, quantity=None, price=None, order_type="MARKET",
                      *, client_order_id: Optional[str] = None, **kwargs) -> Dict
    # 兼容 R02-F4：client_order_id 为新增可选入参；未传则在函数内生成一次并全程复用

async def get_order_by_client_id(self, symbol: str, client_order_id: str) -> Optional[Dict]
    # 新增：GET /papi/v1/um/order?origClientOrderId=...（PM）/ fapi 等价
```

### 4.3 状态流转（伪代码）

```
# 写请求
cid = client_order_id or f"{prefix}{uuid4hex}"     # 一次调用内生成一次
params["newClientOrderId"] = cid
try:
    return await _http_once("POST", endpoint, params)
except (TimeoutError, ConnectionError, aiohttp.ClientError) as e:
    # R02-F3 未知结果：先按 cid 核对，禁止直接重发
    if config.enabled and config.write.retry_after_verify:
        for i in range(verify_max_attempts):
            order = await get_order_by_client_id(symbol, cid)   # -2013 视为不可见，重试
            if order: return order                              # 查到即返回，不重发
            await asyncio.sleep(verify_interval)
    raise UnknownOrderResultError(cid, symbol)   # 保守：不提交，返回明确「未知」给调用方
```

### 4.4 配置项落地

`shared/api_retry_config.yaml`（经 `load_shared_config("api_retry_config.yaml")` 读取）：

```yaml
api_retry:
  enabled: true                     # D4 开关（默认 true，可线上快速回退）
  read:    {max_retries: 3, delay_seconds: 1.0, backoff: 2.0}
  write:   {max_retries: 0, retry_after_verify: true}
  order_verify_max_attempts: 3
  order_verify_interval_seconds: 0.5
  client_order_id_prefix: "sq"
```

### 4.5 与既有机制衔接

- `_NON_RETRYABLE_ERROR_CODES`（`-9999/-2011/-2013/-4108` 等）语义保持；读请求重试继续使用。
- `trade_logger.log_order` 记录逻辑保持；`newClientOrderId` 随响应回传（调用方可见）。
- `place_conditional_order`（:554）、`cancel_order`（:839）按同分级：**下单严禁盲重发**；撤单对 `-2011` 幂等（已实现，保持）。

### 4.6 验收标准对应

| 验收 | 对应设计 |
|------|---------|
| R02-AC1 | 写请求不盲重发 + `newClientOrderId` 去重 → 交易所至多 1 单 |
| R02-AC2 | 超时后先 `get_order_by_client_id`，查到即返回，不调用下单端点 |
| R02-AC3 | GET 仍走 `_request_read` 重试（不回归） |
| R02-AC4 | 同一意图 `client_order_id` 复用；不同意图不同（单测断言） |
| R02-AC5 | `client_order_id` 为可选 kwarg，现有调用点零改动可运行 |

### 4.7 重复代码抽取

读/写分级下沉到 `_request` 单点分发，所有上层写接口（下单/条件单/撤单）共享同一写策略，不再各自实现重试。

---

## 5. R03 — 新币平仓重试原数量导致反向开多

### 5.1 改动文件清单

| 文件 | 改动 |
|------|------|
| `strategies/new_coin/executor.py` | `_close_position`（:2492-2690）重构：循环内重算剩余量、带 `reduceOnly`、市价兜底同量约束、成功语义收紧 |
| `strategies/new_coin/config.yaml` | `trading.close_position`（:218-223）新增键 |

### 5.2 关键接口签名

```python
# strategies/new_coin/executor.py
async def _close_position(self, symbol: str, close_percent: Decimal, reason: str) -> bool
    # 返回值语义收紧（R03-F5）：True 仅当剩余待平量=0 且持仓对账通过

async def _close_with_reduce_only(self, symbol, side, target_qty, *,
                                  reduce_only: bool, sync_before_reduce_only: bool,
                                  step_size, position_confirm_retries: int,
                                  position_confirm_interval: float) -> CloseOutcome
    # 单点：剩余量重算 + reduceOnly + -2022 前置处理 + 撤单后读最终量 + 对账防反向
```

`CloseOutcome{closed_qty: Decimal, target_qty: Decimal, success: bool, reason: str}`。

### 5.3 状态流转（伪代码）

```
target = 初始真实持仓 * close_percent（按 stepSize 截断）
closed = Decimal(0)
for attempt in range(max_retries + 1):
    remaining = target - closed
    if remaining <= 0: break
    real_amt = await get_position(symbol)             # R03-F1 每次提交前重读交易所
    remaining = min(remaining, 真实可平量)             # 防止反向
    order = await place(限价/市价, qty=remaining, reduce_only=True)   # R03-F3/F4
    outcome = await _close_with_reduce_only(...)       # 含 -2022 处理 / 撤单后读最终量
    closed += outcome.closed_qty                       # R03-F2 撤单后必须重读
    if 真实持仓已归零: break
return closed >= target 且 对账通过                   # R03-F5
```

### 5.4 配置项落地（`strategies/new_coin/config.yaml`）

```yaml
trading:
  close_position:
    max_retries: 3                 # 沿用
    retry_interval: 2              # 沿用
    poll_interval: 2               # 沿用
    timeout: 10                    # 沿用
    reduce_only: true              # 新增（D1）
    sync_before_reduce_only: true  # 新增（-2022 前置对账，D1）
    position_confirm_retries: 2    # 新增
    position_confirm_interval: 1   # 新增（秒）
```

### 5.5 与既有机制衔接

- 对账复用 `binance_api.get_position`；`-2022` 处理调用 `_sync_position_with_exchange` 等价逻辑（新币侧以交易所持仓读取 + 撤同向条件单为准，平仓以交易所为准，不依赖 DB）。
- 幂等（R03-AC/边界）：第二次调用识别「已无空头持仓」直接返回。

### 5.6 验收标准对应

| 验收 | 对应设计 |
|------|---------|
| R03-AC1 | `remaining = target - closed`，第二笔为 0.6 |
| R03-AC2 | 剩余量截断为 0 且真实持仓>0 → 不提交反向单，返回失败 |
| R03-AC3 | 限价+市价兜底均带 `reduce_only`（断言参数） |
| R03-AC4 | 撤单抛 `-2011` → 读单累加 `executedQty`，不再补单 |
| R03-AC5 | 以交易所真实持仓为准，`remaining` 上限=真实可平量 |
| R03-AC6 | `-2022` → 先对账/撤同向单再重试 |

### 5.7 重复代码抽取

「剩余量重算 + reduceOnly + `-2022` 前置处理 + 撤单后读最终量」抽为 `shared/reduce_only_close.py::close_remaining`，供 R03/R04/R05 共用（见 §2.2 核心抽象三）。

---

## 6. R04 — HRS 平仓失败/未成交仍撤保护单并删持仓

### 6.1 改动文件清单

| 文件 | 改动 |
|------|------|
| `strategies/hrs/executor.py` | `close_position`（:863-940）返回 `OrderFillResult`/结构化，提交后等待成交或对账 |
| `strategies/hrs/strategy.py` | 时间止损分支（:1922-1966）、移动止盈分支（:1969-2001）依据返回值决定是否撤单/删持仓 |
| `strategies/hrs/config.yaml` | 新增 `trading.close_order` 段 |

### 6.2 关键接口签名

```python
# strategies/hrs/executor.py
async def close_position(self, symbol: str, direction: str,
                         close_percent: float = 1.0, reason: str = "") -> OrderFillResult
    # 不再返回 Optional[Dict]（消除 R04-F1「NEW 即成功」歧义）

# strategies/hrs/strategy.py  新增守卫
async def _finalize_close_if_filled(self, symbol, direction, result: OrderFillResult,
                                    *, close_reason: str) -> bool
    # 仅当 result.is_filled（或对账归零）才 _writeback_pnl → cancel_all_orders → remove_position
    # 部分成交 → 保留仓位 + 按剩余量重建保护 + 告警；失败 → 不撤单不删仓 + 告警
```

### 6.3 状态流转（伪代码）

```
result = await close_position(symbol, direction, close_percent, reason="时间止损")
if result.is_filled :
    _writeback_pnl(...); cancel_all_orders(symbol); remove_position(symbol)   # R04-AC3
elif result.has_fill (部分成交):
    保留仓位；按剩余量重建保护(先撤旧单，撤失败则保留旧单不叠新单)             # R04-F4/AC4
    告警；下一监控周期重试
else:  # 失败 / UNKNOWN / 仅 NEW 未成交
    保留仓位与保护单；不 _writeback_pnl；不 cancel_all_orders；不 remove_position  # R04-AC1/AC2
    告警；下一周期重试（幂等，不重复超额平仓）                                  # R04-AC5
```

### 6.4 配置项落地（`strategies/hrs/config.yaml`）

```yaml
trading:
  retry_count: 2                    # 沿用
  retry_interval: 1.0               # 沿用
  close_order:
    wait_timeout_seconds: 10        # 新增（对齐 btc_eth close_limit_order）
    poll_interval_seconds: 2        # 新增
    reduce_only: true               # 新增（D1）
    sync_before_reduce_only: true   # 新增
    keep_protection_on_partial: true# 新增
```

### 6.5 与既有机制衔接

- 真实持仓归零但本地仍有状态（人工/交易所侧平仓）→ 对账判定「已平」并清理（边界）。
- 撤保护单失败 → 不因清理失败删本地状态（否则彻底失去管理）。
- `-2022/-4118/-4130` 按 D1 处理；DB 不可用不阻塞保留判定（对账以交易所为准）。

### 6.6 验收标准对应

| 验收 | 对应设计 |
|------|---------|
| R04-AC1/AC2 | 失败/仅 NEW → 分支不进清理，`cancel_all_orders`/`remove_position` 不被调用 |
| R04-AC3 | `is_filled` → 原流程（不回归） |
| R04-AC4 | 部分成交 → 保留仓位 + 剩余量保护 |
| R04-AC5 | 多轮失败幂等，不产生超额单；告警可观测 |

### 6.7 重复代码抽取

时间止损与移动止盈两分支「依据 close 结果决定清理」抽为 `_finalize_close_if_filled`（同类守卫单点）；平仓提交复用 §5.7 的 `shared/reduce_only_close.py`。

---

## 7. R05 — MTPCS 保护单失败后丢失已成交开仓的管理状态

### 7.1 改动文件清单

| 文件 | 改动 |
|------|------|
| `shared/protection_retry.py`（**新增**） | R05/R06/R07 两策略同构逻辑抽取（保护完整性 / 补挂收敛 / 占用薄编排 / 入场执行 / 入场终态适配 / 新开仓主流程），详见 §7.7 |
| `strategies/btc_eth/strategy.py` | `_open_new_position` 成交即登记；`_place_entry_protection_orders` 失败不丢态；`PositionState`（:84）加 `protection_pending`；补挂流程。重逻辑转存 `shared/protection_retry.py`，本文件仅保留 ≤4 行极薄委托 |
| `strategies/btc_eth_aggressive/strategy.py` | 与 `btc_eth` 一致修复；同样仅保留极薄委托，两者共用 `shared/protection_retry.py` |
| `strategies/btc_eth/config.yaml` + `strategies/btc_eth_aggressive/config.yaml` | 新增 `risk.protection_retry` 段（键名一致） |

### 7.2 关键接口签名

```python
# PositionState 新增字段
self.protection_pending: bool = False   # 已持仓但保护未挂齐（R05-F2）

# strategies/btc_eth/strategy.py
async def _open_new_position(self, signal: Dict) -> bool:
    # 语义变化：True = 持仓已建立（保护可能待补）；False 仅当「完全未成交」

async def _place_entry_protection_orders(self, symbol, signal) -> Tuple[Dict[str,int], bool]
    # 任一项失败 → 返回 (已成功的部分 ids, False)，不再 return None 丢态   # R05-F3

async def _try_replenish_protection(self, symbol, position) -> bool:
    # 可重试补挂（配置化次数/间隔）；达上限按 on_exhausted 处理（默认 alert） # R05-F4（D2）

def _build_position_state(self, signal, entry_order_id, order_ids, *,
                          actual_quantity: Decimal, protection_pending: bool) -> PositionState
```

### 7.3 状态流转（伪代码）

```
entry = await _place_entry_order(symbol, signal)          # 内部经 R06 结构化等待
if entry is None or entry.executed_qty <= 0:
    return False                                          # 完全未成交，不登记
positions[symbol] = _build_position_state(signal, entry.orderId, {},
                                          actual_quantity=entry.executed_qty,
                                          protection_pending=True)   # R05-F1 先登记
await persist_state(positions[symbol])
ids, ok = await _place_entry_protection_orders(symbol, signal)       # 传入实际成交量
positions[symbol].protection_pending = not ok
if not ok:
    logger.warning(...); notify(...)                      # 记录补挂任务
    await _try_replenish_protection(symbol, positions[symbol])       # 达上限 → alert（D2）
return True                                               # 持仓已建立，保护待补
```

### 7.4 配置项落地（两份 config 键名一致）

```yaml
risk:
  protection_retry:
    max_retries: 3                # 新增
    retry_interval_cycles: 1      # 新增
    on_exhausted: "alert"         # 新增，默认 alert（D2 已定论，reduce 需人工开启）
    notify: true                  # 新增
```

### 7.5 与既有机制衔接

- **重启恢复**：`protection_pending=True` 的持仓由启动恢复流程识别并补挂（复用既有 `_retry_rebuild_pending`/启动孤儿清理路径，需评估接入点）。
- **归属判定**：`protection_pending` **不影响** `position_ownership`（归属仍以 `trade_records` 未平开仓单为准）；登记持仓后应确保 `trade_records` 已写入（R05-F1 与 `trade_logger` 协同）。**不因保护失败释放归属**。
- 保护单「未知结果」（超时）→ 先按 R02 核对，再决定补挂，避免重复保护单。
- DB 持久化失败 → 内存 `positions` 仍须保留。

### 7.6 验收标准对应

| 验收 | 对应设计 |
|------|---------|
| R05-AC1 | 止损失败 → 返回「持仓已建立」，`positions[symbol]` 非空且含真实成交量 |
| R05-AC2 | TP1 失败止损成功 → `stop_loss_order_id` 有值，`protection_pending=True` |
| R05-AC3 | 补挂成功 → `protection_pending=False`，ID 齐备 |
| R05-AC4 | 达上限 → `on_exhausted=alert` 仅告警并保持 pending（D2） |
| R05-AC5 | 激进版同构测试一致 |

### 7.7 重复代码抽取（2026-09-30 修正：已按「抽 shared 共用」落地）

> 本节已于实现阶段按用户决策修正，**与实际实现保持一致**。原设计为「主推在策略内实现、仅保证两份同步」，**该方案已被推翻**：`btc_eth` 与 `btc_eth_aggressive` 约 250 行/文件的同构代码已抽取为共用模块 `shared/protection_retry.py`，两策略仅保留极薄委托包装，彻底消除重复代码（满足 `coding-standards.md`「禁止重复代码」硬约束）。

**`shared/protection_retry.py` 承载的职责**（R05/R06/R07 交易执行加固）：

| 分组 | 内容 |
|------|------|
| 保护完整性 | 三类保护单（硬止损 / TP1 / TP2）就位判定、`protection_pending` 标记、缺失腿列举、补挂节流（`protection_complete` / `apply_protection_order_ids` / `missing_protection_legs` / `protection_due` / `mark_protection_pending_if_incomplete`） |
| 补挂收敛 | 一轮补挂、缺失腿补挂、TP 腿补挂、`on_exhausted` 处理、待补持仓节流补挂（`replenish_protection_round` / `replenish_missing_protection` / `place_missing_tp_level` / `retry_protection_pending` / `handle_protection_exhausted`） |
| 占用薄编排 | 占用冲突告警、未登记持仓时释放占位、过期占用定时清理（`handle_claim_conflict` / `release_claim_if_no_position` / `maybe_cleanup_expired_claims`）；占用原语仍由 `shared.position_ownership` 提供，本模块只做薄编排，不重复实现占用逻辑 |
| 入场执行 | 按实际成交量解析可建仓量、微仓清零（D3）、按真实成交量下保护单、构建/合并持仓状态（`resolve_entry_quantity` / `zero_micro_entry` / `fill_entry_protection_orders` / `build_position_state` / `merge_position`） |
| 入场终态适配 | 统一走 `shared.order_fill_waiter.wait_order_final_state`（识别部分成交）；含 `order_fill.enabled=false` 的 D4 回退路径（`wait_for_order_fill` / `wait_for_order_fill_legacy` / `place_and_wait_entry_order` / `build_legacy_fill_result`） |
| 新开仓主流程 | 归属互斥（R07-F5）→ 原子占位 → 入场下单 → 成交即登记 → 挂保护 → 即时补挂一轮（`open_new_position` 及其内部 `_acquire_entry_slot` / `_establish_position`） |

**差异注入方式**（保证两策略运行时行为零变化，且既有测试打桩继续生效）：
- **策略实例以首参注入**：共享函数统一通过 `strategy.*` 访问通知、`positions`、`db_manager`、配置属性与各类策略内下单助手；实例级打桩（如 `s._notify_warning = AsyncMock()`）继续生效。
- **模块级可替换函数引用传入**：`release_claim` / `close_remaining` / `cleanup_expired_claims` / `try_claim_symbol` / `is_symbol_owned_by_other` 由策略侧在调用点传入「本策略模块作用域」的引用，使既有测试的 `monkeypatch.setattr(strategy_module, ...)` 继续生效。
- 通知项目名由策略侧以 `self.strategy_name` 注入，模块不硬编码任何策略身份。

**两策略保留的委托包装**：`btc_eth` 与 `btc_eth_aggressive` 各自仅保留每个入口 **≤4 行**的极薄委托（形如 `async def _xxx(self, ...): return await protection_retry.yyy(...)`）；策略差异化逻辑（如激进版震荡入场三机制开关、策略 id / 名称）留在策略内。

> ⚠️ 影响面修正：因 `shared/protection_retry.py` 属 `shared/*` 变更，**R05 触发全量重建**（不再是「仅 btc-eth + aggr 两容器」）。这与 §12.2 部署二（R02–R07 合并为一次全量重建）一致；§13.1 R05 行已同步修正。

---

## 8. R06 — 入场超时撤单忽略部分成交和撤单竞态

### 8.1 改动文件清单

| 文件 | 改动 |
|------|------|
| `shared/order_fill_waiter.py`（**新增**） | `OrderFillResult` + 统一等待/终态读取助手 |
| `shared/api_retry_config.yaml` | 新增 `order_fill` 段（R06 助手默认） |
| `strategies/btc_eth/strategy.py` | `_wait_for_order_fill`（:5326-5406）改为调用助手；调用方（:2851-2863）据结果建仓/清零 |
| `strategies/btc_eth_aggressive/strategy.py` | 同构（:2860、:5335） |
| `strategies/new_coin/executor.py` | 入场等待（:307-334）改调用助手 |
| 各策略 `config.yaml` | 可选覆盖 `order_fill_check_interval_seconds` 等 |

### 8.2 关键接口签名

```python
# shared/order_fill_waiter.py
@dataclass
class OrderFillResult:
    status: str; executed_qty: Decimal; orig_qty: Decimal; remaining_qty: Decimal
    avg_price: Decimal; order_id: Optional[int]; client_order_id: Optional[str]
    raw: Dict
    @property
    def is_filled(self) -> bool: ...     # 完全成交
    @property
    def has_fill(self) -> bool: ...      # executed_qty > 0（含部分成交）

async def wait_order_final_state(
    client, symbol: str, *,
    order_id: Optional[int] = None,
    client_order_id: Optional[str] = None,
    timeout_seconds: float,
    check_interval: float,
    visibility_delay: float,             # PM 首查可见延迟（配置化，默认 0.5，替代硬编码 min(interval,0.5)）
    final_read_retries: int,             # 撤单后读终态重试次数
) -> OrderFillResult:
    """等待订单至终态并返回结构化结果；含 -2013 可见延迟重试、部分成交识别、
    超时撤单后读最终量、撤单 -2011/-2013 竞态查单。"""

async def read_order_final_state(client, symbol, order_id, *, read_retries, retry_interval) -> OrderFillResult:
    """仅读一次终态（不等待），用于撤单后/竞态后确认最终 executedQty（R06-F3/F4）。"""
```

### 8.3 状态流转（伪代码）

```
await asyncio.sleep(visibility_delay)                 # R06-F6 首循环可见延迟（配置）
deadline = now + timeout_seconds
while now < deadline:
    try: order = await client.get_order(symbol, order_id)
    except BinanceAPIError as e:
        if e.code == -2013: await sleep(check_interval); continue
        raise
    status = order["status"]; q = Decimal(order["executedQty"])
    if status == "FILLED":
        return OrderFillResult(status="FILLED", executed_qty=q, is_filled=True, ...)
    if status in ("CANCELED","EXPIRED","REJECTED"):
        if q > 0: return OrderFillResult(status="PARTIAL", executed_qty=q, has_fill=True, ...)  # R06-F2
        return OrderFillResult(status=f"{status}_UNFILLED", executed_qty=0, ...)
    await sleep(check_interval)

# 超时：撤单 + 读最终量（R06-F3）
try: await client.cancel_order(symbol, order_id)
except BinanceAPIError as e:
    if e.code in (-2011, -2013): pass      # 竞态：已成交/不存在 → 下面读终态（R06-F4）
    else: raise
return await read_order_final_state(client, symbol, order_id, read_retries, retry_interval)
```

### 8.4 配置项落地

`shared/api_retry_config.yaml` 下新增：

```yaml
order_fill:
  enabled: true                          # D4 开关（默认 true）
  check_interval_seconds: 2              # 轮询间隔
  pm_order_visibility_delay_seconds: 0.5 # PM 首查可见延迟（替代硬编码）
  final_state_read_retries: 2            # 撤单后读终态重试
```

各策略可用 `entry_order_timeout_seconds`（btc_eth 300 / new_coin 60）覆盖等待超时（沿用现有键）。

### 8.5 与既有机制衔接

- 调用方（btc_eth `_place_entry_order` :2851、aggr :2860、new_coin :310）：`has_fill` → 按实际成交量建仓+挂保护（R05/R06-F5）；`!has_fill` → 放弃（原行为）。
- **D3 微仓清零**：`has_fill` 但 `remaining_qty` 或实际量经精度截断后 `< 最小下单量` 或 `=0` → 调用 `shared/reduce_only_close.py` 减仓清零；清零失败 → 告警（不静默丢弃）。
- `-2013` 在助手内消化重试，不落外层直接判「未成交」。

### 8.6 验收标准对应

| 验收 | 对应设计 |
|------|---------|
| R06-AC1 | CANCELED + `executedQty=0.4` → 返回 PARTIAL(0.4)，入场方法建仓 0.4+挂保护 |
| R06-AC2 | 超时撤单后读终态 0.4 → 部分成交处理 |
| R06-AC3 | 读终态 0 → 未成交（不回归） |
| R06-AC4 | 撤单抛 `-2011` 且已成交 → `read_order_final_state` 确认量 |
| R06-AC5 | FILLED 路径行为不变 |
| R06-AC6 | 三策略共用助手（同构测试）

### 8.7 重复代码抽取

三处入场等待（btc_eth / aggr / new_coin）统一调用 `wait_order_final_state`，删除各自 `_wait_for_order_fill` 的内联实现（保留薄封装以兼容既有调用点）。

---

## 9. R07 — 归属 advisory lock 未覆盖开仓预占

### 9.1 改动文件清单

| 文件 | 改动 |
|------|------|
| `database/postgres/init-scripts/08-position-claims.sql`（**新增**） | 占用表 DDL |
| `shared/database.py` | 新增 `claim_position_atomic`（锁内原子查+写，:282 扩展） |
| `shared/position_ownership.py` | 新增 `try_claim_symbol` / `release_claim` / `cleanup_expired_claims`；`is_symbol_owned_by_other` 接入占用表判定 |
| `strategies/btc_eth/strategy.py` | `_open_new_position`（:2627-2646）改「判定+占位」原子；开仓成功/失败/平仓归零 释放 |
| `strategies/btc_eth_aggressive/strategy.py`、`strategies/new_coin/executor.py` | 同构接入 |
| 各策略 `config.yaml` | `ownership` 段新增占用配置 |

### 9.2 关键接口签名

```python
# shared/database.py
async def claim_position_atomic(
    self, lock_key: int, symbol: str, strategy: str, intent_id: str, ttl_minutes: int,
    *, competing_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """在 pg_advisory_xact_lock 事务内原子完成：查有效占用/归属 → 无冲突则 INSERT 占用。
    返回 {'claimed': bool, 'owner': Optional[str], 'claim_id': Optional[int]}。
    唯一部分索引冲突 → claimed=False（另一策略已占）。"""

# shared/position_ownership.py
async def try_claim_symbol(db_manager, symbol, my_record_name, *,
                           competing_record_names=None, ttl_minutes=30,
                           intent_id=None, enabled=True) -> Dict[str, Any]
async def release_claim(db_manager, symbol, my_record_name) -> bool
async def cleanup_expired_claims(db_manager) -> int
async def is_symbol_owned_by_other(db_manager, symbol, my_record_name,
                                   competing_record_names=None) -> bool
    # 语义保持不变；内部增强为「占用表(预占) OR trade_records(权威)」双重判定
```

### 9.3 状态流转（伪代码）

```
# 开仓前（锁内原子，R07-F1/F2/F3）
claim = await try_claim_symbol(db, symbol, my_record_name,
                               competing_record_names=self._competing_record_names,
                               ttl_minutes=cfg.claim_ttl_minutes,
                               intent_id=trade_intent_id, enabled=cfg.enabled)
if not claim["claimed"]:
    notify("已被持有，跳过开仓"); return False            # R07-AC1/AC2/AC6
# 释放锁后做外部请求（R07-F3/AC4）
set_leverage(); 仓位检查(); entry = place_order()
if entry 成交: 写 trade_records（归属权威）
elif entry 失败/放弃: await release_claim(db, symbol, my_record_name)   # R07-F4
# 平仓归零 → release_claim                                         # R07-F4/AC2
# 崩溃 → expires_at 过期 → cleanup_expired_claims 回收               # R07-F4/AC3
```

### 9.4 配置项落地（各策略 `ownership` 段，扩展既有段）

```yaml
ownership:
  enabled: true                      # 新增，D4 开关（默认 true，可回退到既有行为）
  claim_ttl_minutes: 30              # 新增
  claim_cleanup_interval_minutes: 10 # 新增
  lock_timeout_seconds: 5            # 新增
  record_name: "MTPCS策略"            # 沿用
  competing_record_names: ["MTPCS激进策略"]  # 沿用
```

### 9.5 与既有机制衔接

- `resolve_position_owner` / `filter_owned_positions` / `is_symbol_owned_by_other` 既有语义**不变**（归属权威仍是 `trade_records` 未平开仓单）；占用表只是「开仓前预占」。
- **衔接口径**：占用记录（`PENDING`/`ACTIVE`）在开仓窗口期互斥；开仓后 `trade_records` 写入即转为权威归属；平仓归零→释放占用。二者以 `symbol` 关联，**不替代**。
- DB 不可用 / advisory lock 不可用 → 降级为既有 `fetch_one_advisory_lock`（或普通查询）+ 保守策略（查询异常→不双开，R07-F6）。
- 归属为本策略（加仓场景）→ 放行，不被自身占用互斥（边界）。
- R05 交互：占位在开仓时建立，**不因保护失败被误释放**。

### 9.6 验收标准对应

| 验收 | 对应设计 |
|------|---------|
| R07-AC1 | 锁内原子占位 + 部分唯一索引 → 至多一个 `claimed=True` |
| R07-AC2 | 占位成功阻塞对家；释放后可再占 |
| R07-AC3 | `expires_at` 过期 → 清理后可再占 |
| R07-AC4 | 下单在锁外（断言锁持有期无下单调用） |
| R07-AC5 | `position_ownership` 既有测试不回归 |
| R07-AC6 | 冲突 → 「已被持有，跳过开仓」+ 告警，不抛异常中断 |

### 9.7 重复代码抽取

「查占位/写占位」集中在 `claim_position_atomic` 单点；各策略仅调用 `try_claim_symbol`/`release_claim`，不各自拼 SQL。

---

## 10. R08 — 孤儿清理在缺少策略状态时误撤真实持仓止损

### 10.1 改动文件清单

| 文件 | 改动 |
|------|------|
| `ai_tuner/cleanup/orphan_cleanup.py` | `execute`（:410-483）无状态分支加交易所持仓前置检查；复用 `_strategy_has_position`；构造函数 `position_lookback_days` 改从配置读 |
| `ai_tuner/config.yaml` | `orphan_cleanup`（:633-636）新增键 |

### 10.2 关键接口签名

```python
# ai_tuner/cleanup/orphan_cleanup.py
def _is_confirmed_no_position(self, symbol: str, exchange_positions) -> bool:
    """R08-F1/F2：仅当「交易所明确返回且不含该 symbol」才算确认无仓。
    exchange_positions is None（查询失败）→ 返回 False（保守，保留保护单）。"""

# execute() 无状态分支改造
if not state:
    if not self._is_confirmed_no_position(symbol, exchange_positions):
        skipped.append(f"{sn} | {symbol} | 无状态但保留(交易所持仓/无法确认)");  # R08-AC1/AC3
        if cfg.alert_on_missing_state: notify(...)
        continue
    if await self._strategy_has_position(sn, symbol):        # R08-F3 二次确认
        skipped.append(...); continue
    stale_orders.append(order)                               # 确认无仓+无活交易 → 才清理
```

### 10.3 状态流转（伪代码）

```
for order in open_orders:
    state = strategy_states.get(order.strategy_name)
    if not state:
        if exchange_positions is None or order.symbol in exchange_positions:
            跳过并告警（保保守）                                    # R08-F2/F4
        elif await self._strategy_has_position(sn, symbol):
            跳过                                                    # R08-F3
        else:
            stale_orders.append(order)                             # R08-AC2 才清理
        continue
    # 超时分支（:428-435）既有保护不变                              # R08-AC4
    # 场景B（:437-441/:458-483）既有逻辑不变                       # R08-AC5
```

### 10.4 配置项落地（`ai_tuner/config.yaml`）

```yaml
orphan_cleanup:
  enabled: true                       # 沿用
  interval_minutes: 30                # 沿用
  stale_hours_threshold: 2            # 沿用
  require_exchange_confirmation: true # 新增
  alert_on_missing_state: true        # 新增
  position_lookback_days: 7.0         # 新增（替代构造函数硬编码默认）
```

### 10.5 与既有机制衔接

- `exchange_positions is None`（API 失败）与空集合（确实无持仓）**严格区分**（既有代码已区分，保持）。
- `_strategy_has_position` 查询异常 → 已保守返回 `True`（:190-196），保持。
- 多策略同 symbol：逐单按所属策略判定，不误伤对家。

### 10.6 验收标准对应

| 验收 | 对应设计 |
|------|---------|
| R08-AC1 | 有持仓 → 不 `_cancel_order`，记 skipped/告警 |
| R08-AC2 | 无持仓且 `_strategy_has_position=False` → 才取消 |
| R08-AC3 | `exchange_positions is None` + 缺状态 → 跳过告警 |
| R08-AC4/AC5 | 超时分支、场景B 行为不变 |

### 10.7 重复代码抽取

所有进入取消的分支统一经由 `_is_confirmed_no_position` 前置判定，消除「无状态分支」与「超时分支」不一致的保护实现。

---

## 11. 数据库迁移脚本设计（R07 占用表）

### 11.1 放置路径

`database/postgres/init-scripts/08-position-claims.sql`（与既有 `03-create-trade-records.sql` 同风格，幂等 `IF NOT EXISTS`；容器首启即执行）。

### 11.2 SQL DDL

```sql
-- ============================================
-- 币种开仓占用表（R07：跨策略开仓预占互斥）
-- 归属权威仍是 trading.trade_records（未平开仓单）；本表仅为「开仓窗口期预占」
-- ============================================
CREATE SCHEMA IF NOT EXISTS trading;
GRANT ALL PRIVILEGES ON SCHEMA trading TO trading_user;

CREATE TABLE IF NOT EXISTS trading.position_claims (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    strategy        VARCHAR(50) NOT NULL,
    trade_intent_id VARCHAR(64) NOT NULL,          -- 一次交易决策的稳定标识
    claim_state     VARCHAR(16) NOT NULL DEFAULT 'PENDING',  -- PENDING/ACTIVE/RELEASED
    created_at      TIMESTAMP   NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMP   NOT NULL,          -- TTL：超时后可由清理任务回收
    released_at     TIMESTAMP,
    reason          VARCHAR(64)
);

-- 部分唯一索引：同一 symbol 至多一条「有效占用」（PENDING/ACTIVE）→ 并发互斥
CREATE UNIQUE INDEX IF NOT EXISTS uq_position_claims_active_symbol
    ON trading.position_claims(symbol)
    WHERE claim_state IN ('PENDING','ACTIVE');

-- 过期清理扫描索引
CREATE INDEX IF NOT EXISTS idx_position_claims_expires
    ON trading.position_claims(expires_at)
    WHERE claim_state IN ('PENDING','ACTIVE');

-- 策略维度查询索引
CREATE INDEX IF NOT EXISTS idx_position_claims_strategy
    ON trading.position_claims(strategy, symbol);
```

### 11.3 TTL 清理策略

- 写入时 `expires_at = NOW() + claim_ttl_minutes`。
- 定时任务（各策略主循环按 `claim_cleanup_interval_minutes`）调用 `cleanup_expired_claims`：将 `expires_at < NOW()` 且状态有效的占用置 `RELEASED`（**保留行不删**，便于审计与 §12.2 回滚）。
- 平仓归零/开仓失败/放弃 → 立即 `release_claim`（置 `RELEASED` + `released_at`）。

### 11.4 与 `trade_records` 的归属衔接

| 阶段 | 权威来源 | 说明 |
|------|---------|------|
| 判定→下单窗口 | `position_claims`（PENDING） | 防止两策略并发读到「无归属」同时下单 |
| 开仓成交后 | `trading.trade_records`（未平开仓单，`realized_pnl IS NULL`） | 既有归属权威；`is_symbol_owned_by_other`/`resolve_position_owner` 语义不变 |
| 平仓归零后 | 无 | 释放占用，归属判定自然失效 |

衔接原则：**占用表是「开仓前预占」，`trade_records` 是「开仓后权威」；两者以 `symbol` 关联，不互相替代**（R07-F5）。

---

## 12. 实施顺序与分批

### 12.1 关键依赖分析

| 依赖 | 说明 |
|------|------|
| R05 → R06 | R05 的「成交即登记」需 R06 的结构化成交结果（含部分成交量）。二者同改 `btc_eth/strategy.py`，必须同批或先后衔接 |
| R03/R04 → R06 抽象 | 若复用 §5.7 的 `shared/reduce_only_close.py`，则 R03/R04 亦触发全量重建 |
| R02/R06/R07 → shared | 三者均改动/新增 `shared/*`，**必然触发全量重建** |
| R01/R08 → 独立容器 | 分别只重建 kline-service / ai-tuner，零 shared 依赖 |

### 12.2 推荐批次（2 次部署，最少停机）

| 部署 | 内容 | 触发重建范围 | 说明 |
|------|------|-------------|------|
| **部署一** | **R01 + R08** | kline-service、ai-tuner | 完全独立、零 shared 依赖；先上线验证，风险最低 |
| **部署二** | **R02 + R03 + R04 + R05 + R06 + R07** | **全量**（所有策略 + ai-tuner + data-backend + dashboard-api） | 全部改动/依赖 shared；合并为**一次**全量重建 = 一次停机 |

> **与需求文档 §2.1 初版建议的偏差说明**：初版把 R03/R04/R05 归入「非全量」批次。但依据「禁止重复代码」硬约束，R03/R04 的平仓重算与等待、R05 的成交登记都应与 R06 共用 shared 助手；且 R05 显式依赖 R06 的成交结果。因此**三者必然并入全量批次**。若审查坚持 R03/R04 走非全量，则须在 new_coin/hrs 内**重复实现**相同逻辑（不推荐，违反规范）——见 §14 风险 #1。

**备选（若追求最大隔离）**：部署一 = R01（kline-service）；部署二 = R08（ai-tuner）；部署三 = R02/R03/R04/R05/R06/R07（全量）。代价：3 次部署。

### 12.3 提交粒度建议

- 部署一：1 个 commit（R01）或 2 个 commit（R01、R08），同一次 push。
- 部署二：按「shared 抽象 → 各策略接入」拆 2–3 个 commit，但**同一次 push**（一次 Actions Run），避免阶段性不一致。

---

## 13. 风险与回滚矩阵

### 13.1 逐条风险

| 编号 | 风险点 | 开关/回退 | revert 影响面 |
|------|--------|----------|--------------|
| R01 | 白名单过严拒绝线上合法但未登记的 symbol；interval 正则漏 `1w/1M` | 无开关；可用 `FIXED_SYMBOLS`/`TABLE_NAME_PATTERN` 环境变量临时放宽 | 仅 kline-service 重建 |
| R02 | `newClientOrderId` 意图边界误判 → 同一次开仓生成两个 ID | `api_retry.enabled=false` 回退到既有重试语义 | 全量重建 |
| R02 | 写请求不重试后，瞬时抖动直接失败 | 保留「核对未受理后重发」路径（`retry_after_verify`） | 同 |
| R03 | `reduceOnly` 与 PM 冲突被拒 | 策略内配置 `sync_before_reduce_only`（对账/撤同向单） | 仅 new-coin 重建 |
| R04 | 对账延迟误判「未平」 | 对账次数/间隔配置化；误判仅延迟清理，无资金风险 | 仅 hrs 重建 |
| R05 | 补挂耗尽后 `reduce` 误平真仓 | `on_exhausted` 默认 `alert`（D2），`reduce` 需人工开启 | 全量重建（新增 `shared/protection_retry.py`，见 §7.7） |
| R06 | 部分成交建仓后保护单量不匹配 → 超量 | 保护单量按实际成交量；`order_fill.enabled` 开关 | 全量重建 |
| R07 | 死锁/残留占用永久阻塞某币种 | `ownership.enabled=false` 回退；TTL + 定时清理 + 失败即释放 | 全量重建 + 回滚迁移（**保留表不删**） |
| R08 | 过保守导致真孤儿条件单残留（占配额） | `require_exchange_confirmation=false` 回退 | 仅 ai-tuner 重建 |

### 13.2 全局回退总闸

| 开关 | 默认 | 作用 |
|------|------|------|
| `api_retry.enabled` | `true` | R02 幂等/分级重试总闸 |
| `order_fill.enabled` | `true` | R06 结构化等待总闸 |
| `ownership.enabled` | `true` | R07 占用互斥总闸 |

三者置 `false` 均可**不改代码**快速回到既有行为（D4）。

---

## 14. 最不确定 / 风险最高的 3 个点（供审查重点盯）

1. **R03/R04/R05 的批次归属与「禁止重复代码」的冲突**：若复用 shared 助手 → 三者并入全量批次（偏离需求初版建议，但 2 次部署仍最少）；若坚持非全量 → 必须接受策略内重复实现（违规）。**需审查确认走哪条**（影响 §12 批次与停机次数）。
2. **R07 占用表与既有 `trade_records` 归属的双权威边界**：`claim`（预占）与 `trade_records`（权威）的切换时机、加仓场景「归属为本策略不被自身占用拦截」、`protection_pending` 与占用的交互，任一处理不当会造成「该开的开不了 / 该拦的没拦住」。
3. **R02 的 `newClientOrderId` 生命周期与 R06/R05 的成交结果口径**：Q7 已定「一次交易决策内复用」，但「一次决策」在加仓/补单/重试各路径的边界需精确落地（同一真实开仓若被判为两个意图 → 去重失效；反之复用过度 → 交易所把正常的二次下单当重复拒绝）。

---

## 15. 待办清单（Task List，供 python-engineer 原子执行）

> 执行顺序按 ID；标注依赖、涉及文件、对应验收标准（ACL）。

### 部署一（非全量）

| ID | 任务 | 涉及文件 | 依赖 | 对应 ACL |
|----|------|---------|------|---------|
| T01 | 新增 `build_kline_table_name` + `TableNameValidationError`；config 加 `TABLE_NAME_PATTERN`/`FIXED_SYMBOLS` | `services/kline_service/core/table_name_guard.py`(新)、`shared/core/config.py` | — | R01-F1/F2、R01-AC6 |
| T02 | `/klines/latest`、`/indicators` 改走 guard；建表失败即终止 | `services/kline_service/api/routes.py` | T01 | R01-F3/F4、AC1-AC5 |
| T03 | registry 入口改走 guard；补注入拒绝单测 | `registry_routes.py`、`tests/` | T01 | R01-F4、AC5 |
| T04 | 无状态分支加交易所持仓前置检查；复用 `_strategy_has_position`；`position_lookback_days` 入配置 | `ai_tuner/cleanup/orphan_cleanup.py`、`ai_tuner/config.yaml` | — | R08-F1..F5、AC1-AC5 |

### 部署二（全量，改动 shared）

| ID | 任务 | 涉及文件 | 依赖 | 对应 ACL |
|----|------|---------|------|---------|
| T05 | 新增 `shared/api_retry_config.yaml`；`_request` 拆分读/写策略；`place_order` 加 `client_order_id`；新增 `get_order_by_client_id` | `shared/binance_api.py`、`shared/utils.py`、`shared/api_retry_config.yaml`(新) | — | R02-F1..F5、AC1-AC5 |
| T06 | 新增 `OrderFillResult` + `wait_order_final_state` + `read_order_final_state` | `shared/order_fill_waiter.py`(新)、`api_retry_config.yaml` | — | R06-F1..F6、AC1-AC6 |
| T07 | 新增 `close_remaining`（剩余量重算+reduceOnly+`-2022`+撤单后读量+对账防反向） | `shared/reduce_only_close.py`(新) | T06 | R03-F1..F5、R04-F3、AC |
| T08 | 新增 `08-position-claims.sql`（表+部分唯一索引+TTL 索引） | `database/postgres/init-scripts/08-position-claims.sql`(新) | — | R07-F2、AC1 |
| T09 | `claim_position_atomic`（锁内原子查+写） | `shared/database.py` | T08 | R07-F1/F6、AC4 |
| T10 | `try_claim_symbol`/`release_claim`/`cleanup_expired_claims`；`is_symbol_owned_by_other` 接入占用判定（`enabled` 开关） | `shared/position_ownership.py` | T09 | R07-F1..F7、AC1-AC6 |
| T11 | btc_eth：`_open_new_position` 成交即登记 + `protection_pending`；保护单失败不丢态；`_try_replenish_protection`（on_exhausted 默认 alert） | `strategies/btc_eth/strategy.py`、`config.yaml` | T06 | R05-F1..F6、AC1-AC4 |
| T12 | btc_eth_aggressive：同构修复（键名一致） | `strategies/btc_eth_aggressive/strategy.py`、`config.yaml` | T11 | R05-F5、AC5 |
| T13 | btc_eth/aggr/new_coin：`_wait_for_order_fill` 改调 `wait_order_final_state`；调用方按 `has_fill` 建仓/微仓清零（D3） | 三策略 strategy/executor | T06、T07 | R06-F5、AC1-AC6 |
| T14 | new_coin：`_close_position` 用 `close_remaining` 重算剩余量 + reduceOnly；config 新增 close_position 键 | `strategies/new_coin/executor.py`、`config.yaml` | T07 | R03-F1..F6、AC1-AC6 |
| T15 | hrs：`close_position` 返回 `OrderFillResult`；时间止损/移动止盈分支加 `_finalize_close_if_filled` 守卫；config 新增 close_order 段 | `strategies/hrs/executor.py`、`strategy.py`、`config.yaml` | T07 | R04-F1..F5、AC1-AC5 |
| T16 | 各策略接入 `try_claim_symbol`/`release_claim`（锁定前占位、开仓失败释放、平仓归零释放、定时清理）；config `ownership` 段新增 | btc_eth/aggr/new_coin 开仓调用方 + config | T10 | R07-F3/F4、AC2/AC3/AC6 |
| T17 | 全量回归测试（Python 3.11）、幻觉测试 10 项、覆盖率验证 | `tests/` | T01-T16 | §13 全条 |

> T05–T16 需**同一次 push**（一次全量重建）；T01–T04 为部署一。

---

## 16. 配置项汇总（对照需求 §12，含新增开关）

| 编号 | 配置项 | 默认值 | 文件 |
|------|--------|--------|------|
| R01 | `TABLE_NAME_PATTERN` | `^kline_[a-z0-9]{3,20}_([0-9]+[mhdw]\|1M)$` | `services/kline_service/shared/core/config.py` |
| R01 | `FIXED_SYMBOLS` | 6 个币种 | 同上 |
| R01 | `KLINE_API_AUTH_ENABLED`/`KLINE_API_TOKEN` | `false`/`""`（预留不启用，Q1） | 同上 |
| R02 | `api_retry.enabled` | `true`（D4） | `shared/api_retry_config.yaml`(新) |
| R02 | `api_retry.read/write/verify/前缀` | 见 §4.4 | 同上 |
| R03 | `trading.close_position.reduce_only` 等 | 见 §5.4 | `strategies/new_coin/config.yaml` |
| R04 | `trading.close_order.*` | 见 §6.4 | `strategies/hrs/config.yaml` |
| R05 | `risk.protection_retry.*`（`on_exhausted=alert`，D2） | 见 §7.4 | btc_eth + aggr `config.yaml` |
| R06 | `order_fill.enabled`（D4）/`check_interval_seconds`/`pm_order_visibility_delay_seconds`/`final_state_read_retries` | 见 §8.4 | `shared/api_retry_config.yaml` |
| R07 | `ownership.enabled`（D4）/`claim_ttl_minutes`/`claim_cleanup_interval_minutes`/`lock_timeout_seconds` | 见 §9.4 | 各策略 `config.yaml` `ownership` 段 |
| R08 | `orphan_cleanup.require_exchange_confirmation`/`alert_on_missing_state`/`position_lookback_days` | 见 §10.4 | `ai_tuner/config.yaml` |

---

（文档结束）