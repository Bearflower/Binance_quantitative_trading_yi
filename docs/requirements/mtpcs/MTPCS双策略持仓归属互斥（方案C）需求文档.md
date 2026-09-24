# MTPCS 双策略持仓归属互斥（方案C）需求文档

**文档版本**: v1.1
**最后更新**: 2026-09-23
**维护者**: 项目团队
**涉及策略**: MTPCS 原版（btc_eth）、MTPCS 激进版（btc_eth_aggressive）
**部署状态**: 已上线灰度（btc_eth + 激进版）

---

## 1. 背景与问题

### 1.1 问题现象

MTPCS 原版（btc_eth）与激进版（btc_eth_aggressive）**共享同一个币安 PM 账户**。此前补保护单 / 止盈止损 / 对账逻辑**按"币种"而非"持仓归属"**工作，导致系统性互挂隐患：

线上案例：激进版开空某币后，原版运行时将该币误认为自己的持仓，凭空造态并**叠挂保护单**。原版对 SOL 叠挂 3 条 BUY 保护单，而原版并无 SOL 持仓。

这种"两策略在同一币上互相挂保护单、互相误平"的问题，是共享 PM 账户下多策略运行的核心风险。

### 1.2 根因

1. `get_position` 返回的是**共享账户的全部持仓**，无策略维度区分。
2. 原有归属判定仅靠**白名单（waitlist）+ 数量过滤**，无法判断该持仓到底是哪个策略开的。
3. 缺少"持仓归属"的权威来源，导致各策略无法确权、只能凭假设补保护单。

---

## 2. 目标与非目标

### 2.1 目标

- 建立**开仓订单级归属**判定：明确"这个持仓属于哪个策略"。
- 持仓归属互斥：**开仓持仓期**杜绝两策略对同一币并存开仓。
- 补保护单前做归属校验：只对"属于自己"的持仓补单造态。
- 根治共享 PM 账户下互挂保护单、互相误平的隐患。

### 2.2 非目标

- **不合并两策略持仓**：原版与激进版仍是独立策略、独立 schema、独立资金额度。
- **不改变单币级自身风控逻辑**（止损/止盈/移动保护），仅修正"归属判定"这一层。

---

## 3. 方案（边界语义 A + B）

### 3.1 核心原则：归属挂在开仓订单上，不是"币种 → 策略"

识别开仓单的判据（单点 SQL `_open_entry_sql`）：

```
trading.trade_records 中
  order_type IN ('LIMIT','MARKET')   -- 开仓单（非平仓/条件单）
  AND realized_pnl IS NULL            -- 未平开的开仓单
其 strategy 字段即该持仓的归属策略
```

### 3.2 边界A（归属兜底）

多笔未平开仓单并存时，以 `executed_at` **最新者**为归属。老仓的保护单在净仓被覆盖时**让渡给新家**，避免"两个策略同时认为自己持有"。

### 3.3 边界B（持仓期互斥）

任何策略开仓前，若发现**他策略**已经持有该币的未平开仓单（`is_symbol_owned_by_other`），则**跳过开仓**——从源头杜绝"双开单并存"；本策略自身加仓放行。

---

## 4. 实现

### 4.1 新模块 shared/position_ownership.py

| 函数 | 职责 |
|------|------|
| `load_ownership_config(config)` | 读取归属配置（`my_record_name` / `competing_record_names`） |
| `resolve_position_owner` | 边界A：返回该币未平开仓单中最新一条的归属策略 |
| `is_symbol_owned_by_other` | 边界B：判断该币是否被他策略持有开仓单 |
| `filter_owned_positions` | **上报归属过滤（2026-09-23新增）**：按归属过滤 margin_dict/qty_dict，剔除归属非本策略的币种，避免共享账户下把对家策略持仓误报成自己（见 4.4 与 5） |
| `_open_entry_sql` | 开仓单查询的单点 SQL（归属判定的唯一依据） |

### 4.2 并发防护（advisory lock）

归属判定经 `shared/database.py::fetch_one_advisory_lock`（PostgreSQL advisory lock）**串行化**，防两策略并发对同一币同时判定"归自己"而双开。

### 4.3 配置

两个 `config.yaml` 新增：
- `strategy.record_name`：如 `"MTPCS策略"` / `"MTPCS激进策略"`（本策略身份标识）
- 顶层 `ownership.competing_record_names`：被视作"他策略"的落库名单

TradeLogger 改读 `load_ownership_config(config)['my_record_name']`，去硬编码。

### 4.4 上报归属过滤（2026-09-23补全）

> **补全说明**：本节"保护单归属守卫、开仓互斥、孤儿守卫"三处接入点（第 5 节）在方案C落地时已有声明，但两个 `strategy.py` 的接入**实际缺位**。本次对两个 `strategy.py` 补全三处归属守卫，并新增**看板上报归属过滤**能力。

`shared/position_ownership.py::filter_owned_positions(db_manager, my_record_name, margin_dict, qty_dict)`：对 `margin_dict`/`qty_dict` 逐 symbol 做归属校验（`resolve_position_owner`），剔除"最新未平开仓单归属非本策略"的币种，避免共享 PM 账户下把对家策略开的仓误报成自己（触发场景：看板曾把激进版开的 SOL 空单显示成原版"一单一策略"）。

容错策略：
- `db_manager` / `my_record_name` 缺失 → 原样返回（保守，不误伤自家已成交持仓）；
- 单 symbol 归属查询异常 → 保留该 symbol（宁多报不误删自家仓）；
- 归属为 `None`（无未平开仓单）→ 视为通过，保留。

上报过滤路径用 `status_filter=False` 判定（含 NEW 挂单），否则归属查询恒为 None 导致过滤失效。

---

## 5. 接入点（两策略双改）

> **补全状态（2026-09-23）**：下表三处归属守卫 + `main.py` 上报过滤已全部补齐落地，与 `strategies/btc_eth` / `strategies/btc_eth_aggressive` 实际代码一致。

| 接入方法 | 改动 |
|----------|------|
| 构造函数 | 注入归属配置（`load_ownership_config`） |
| `_ensure_symbol_protection` | 补保护单前归属校验：归属=对方/未知 → **不补单、不造态 + 告警** |
| `_open_new_position` | 加持仓期互斥（边界B），他策略已持有 → 跳过开仓（拦截+告警） |
| `_do_startup_orphan_cleanup` | 孤儿清理加归属守卫（不清理归属其他策略的仓） |
| `main.py::run_strategy` | **两处上报点（开仓后立即上报、周期末上报）调用 `filter_owned_positions` 按归属过滤** |

原版与激进版 `strategy.py` 为**近乎一致的副本**，需双改保持逻辑同步。

---

## 6. 与激进版"同小时跳过原版"的关系

激进版"同小时跳过原版已开仓币"（`_get_original_entry_symbols_since_hour`）保留，作为**分析层优化**；**边界B（持仓期互斥）为最终闸门**，任何策略开仓前都会校验并跳过。

---

## 7. 测试

- 新增 `tests/test_position_ownership.py`（36 用例），覆盖边界A/B、归属解析、互斥判定。
- 连同 `tests/test_shared/test_circuit_breaker.py`，总计 **90 passed**。

---

## 8. 部署状态

- 方案C（边界A/B 归属判定与互斥）已上线灰度（btc_eth + 激进版），部署日期 2026-09-22。
- **2026-09-23 补全**：三处归属守卫接入（保护单前校验 / 开仓互斥 / 孤儿守卫）与 `main.py` 上报归属过滤（`filter_owned_positions`）补齐落地，回归测试保持通过。

---

## 9. 代码位置索引

| 项 | 位置 |
|----|------|
| 归属判定模块（新增） | [shared/position_ownership.py](../../../shared/position_ownership.py) |
| 归属配置 | [strategies/btc_eth/config.yaml](../../../strategies/btc_eth/config.yaml)、[strategies/btc_eth_aggressive/config.yaml](../../../strategies/btc_eth_aggressive/config.yaml) |
| 原版接入 | [strategies/btc_eth/strategy.py](../../../strategies/btc_eth/strategy.py) |
| 激进版接入 | [strategies/btc_eth_aggressive/strategy.py](../../../strategies/btc_eth_aggressive/strategy.py) |
| 测试 | [tests/test_position_ownership.py](../../../tests/test_position_ownership.py) |
| 迭代记录 | [项目需求迭代文档 第12节](../../plans/项目需求迭代文档.md) |

---

**文档结束**