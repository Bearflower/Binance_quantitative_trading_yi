# P0 修复需求文档：new_coin 补全链路候选集遗漏 + kline「表已存在即放行」死代码 + 读路径静默化可观测性

## 1. 文档信息

| 项 | 内容 |
|----|------|
| 文档名称 | P0 修复需求文档：new_coin 补全链路候选集遗漏 + kline「表已存在即放行」死代码 + 读路径静默化可观测性 |
| 版本 | v1.0（初稿，待评审） |
| 作者 | requirements-document-expert |
| 创建日期 | 2026-10-01 |
| 触发事件 | 上一轮 `c494571`（P0 保护单修复）与其补丁 `c6082a5`（读路径不再 400）已于 2026-10-01 08:55–09:03 CST 部署完成，本轮为其补漏 |
| 需求来源 | 生产实测（现象 + 根因链均已取到生产/源码证据，见 §2、§3） |
| 适用范围 | 本轮只修本文件 §4「修复范围」的三项（问题 A / 问题 B / 问题 C）；其余问题不在本轮（见 §8） |
| 下游环节 | backend-architect（架构设计）→ python-engineer（编码实现） |
| 验收口径 | 每项以 §6「验收标准」为准，编号 `P0-A-ACn` / `P0-B-ACn` / `P0-C-ACn`，可直接转为测试用例 |

> 说明：本文档只描述「应该是什么行为」，不写代码实现。文中「伪代码级状态流转」仅用于消除歧义，实现细节由架构/编码环节决定。
>
> ⚠️ 重要更正（已核实，见 §3.5）：需求方给出的「4 笔未平持仓完全没有保护单」这一前提，经交易所实际持仓核对后**不成立**——这 4 个标的当前在交易所**没有任何真实空头持仓**，它们只是 `new_coin.short_positions` 里 4 条**僵尸 `open` 记录**。本轮对问题 A 的修复方向（候选集来源 + 对账 + 连续守卫）仍然成立，但「恢复机制」的正确动作是**自动对账关闭僵尸记录**，而**不是**盲目补挂保护单（详见 §5）。请以更正后的根因链为准。

---

## 2. 事件背景（已核实事实）

### 2.1 本轮部署背景（已核实）

- `c494571`（修复 new_coin 持仓保护单丢失：P0-1 策略侧 ensure-active 注册、P0-2 kline 侧「表已存在即放行」、P0-3 补全顺序改为「先算 ATR 再撤旧单」）与 `c6082a5`（扩大 `FIXED_SYMBOLS` + 读路径白名单不命中时返回 200 空数据）已于 **2026-10-01 08:55–09:03 CST（= 00:55–01:03 UTC）** 部署完成。
- 容器重启时间：`trading_system-kline` 于 **09:02:35 CST（01:02:35 UTC）** 重启；`trading_system-new_coin` 于 **00:58:23 UTC** 重启。
- 本文档引用的 `new_coin` 日志取自该容器 **00:58:23 UTC 之后**的运行。

### 2.2 需求方提供的生产实测数据（已复核，结论见 §3.5）

**数据来源命令**（生产服务器执行，只读）：

```bash
ssh root@43.156.242.184 "docker exec trading_system-postgres \
  psql -U trading_user -d trading_platform \
  -c \"SELECT symbol,status,entry_price,opened_at FROM new_coin.short_positions WHERE status='open' ORDER BY opened_at;\""
```

**`new_coin.short_positions` 中 `status='open'` 共 6 笔（已核实）**：

| symbol | entry_price | opened_at |
|--------|-------------|-----------|
| APLDUSDT | 27.86 | 2026-09-19 17:03:32 |
| AMCUSDT | 2.69816554 | 2026-09-19 19:03:42 |
| PATHUSDT | 13.63 | 2026-09-20 06:03:53 |
| USDBRLUSDT | 5.1161 | 2026-09-23 10:01:38 |
| CVNAUSDT | 60.71 | 2026-09-29 11:01:39 |
| SECZUSDT | 15.59 | 2026-09-30 14:05:14 |

**条件单表实为 `btc_eth.condition_orders`**（new_coin 经 `search_path` 落该 schema；`public.condition_orders` 无记录）。按 symbol × order_type × status 统计（已核实）：

| symbol | STOP_LOSS OPEN | STOP_LOSS CANCELED | TAKE_PROFIT OPEN | TAKE_PROFIT CANCELED |
|--------|----------------|--------------------|------------------|----------------------|
| APLDUSDT | 0 | 10 | 0 | 19 |
| AMCUSDT | 0 | 4 | 0 | 8 |
| PATHUSDT | 0 | 3 | 0 | 5 |
| CVNAUSDT | 0 | 1 | 0 | 2 |
| SECZUSDT | 2 | 0 | 4 | 0 |
| USDBRLUSDT | 9 | 0 | 18 | 0 |
| ACNUSDT（已非持仓） | 2 | 0 | 4 | 0 |

即：**APLDUSDT / AMCUSDT / PATHUSDT / CVNAUSDT 的止损与止盈条件单全部为 CANCELED，OPEN 数为 0**；SECZUSDT / USDBRLUSDT 有保护；ACNUSDT 已不是持仓但残留 OPEN 条件单。

### 2.3 本轮新增核实：交易所实际持仓（决定性证据）

在 `trading_system-new_coin` 容器内只读调用 `/papi/v1/um/positionRisk`（与策略同端点、同账户口径）：

```bash
ssh root@43.156.242.184 "docker exec trading_system-new_coin python -c \"
import asyncio, os
from shared.binance_api import BinanceClient
async def m():
    c = BinanceClient(api_key=os.getenv('BINANCE_API_KEY'), api_secret=os.getenv('BINANCE_API_SECRET'), testnet=False)
    async with c:
        pos = await c.get_position()
    for p in pos:
        if float(p.get('positionAmt',0) or 0) != 0:
            print(p.get('symbol'), p.get('positionAmt'))
asyncio.run(m())
\""
```

**实测结果（2026-10-01 01:13 UTC，已核实）——非零持仓仅 9 个**：

```
ETHUSDT -0.019   SECZUSDT -1.28   SEIUSDT -727.0   USDBRLUSDT -19.54
HBARUSDT -351.0  XRPUSDT +19.6    BZUSDT +0.54     SOLUSDT +0.47
牛来USDT -109.0
```

**关键结论：APLDUSDT / AMCUSDT / PATHUSDT / CVNAUSDT 不在交易所持仓列表中**（真实空头为 `SECZUSDT / USDBRLUSDT` 两个；`ETHUSDT / SEIUSDT / HBARUSDT / 牛来USDT` 是其他策略的持仓；`XRPUSDT / BZUSDT / SOLUSDT` 为多头）。

**`new_coin` 启动日志（00:58:24 UTC，与源码一致，已核实）**：

```
基线重建完成                           symbols: [HBARUSDT, ACNUSDT, SECZUSDT, USDBRLUSDT, ETHUSDT, 牛来USDT, SEIUSDT]
基线重建：跳过非本策略持仓（PM 账户 positionRisk 含全账户空头）
                                       symbols: [HBARUSDT, ETHUSDT, 牛来USDT, SEIUSDT]
                                       own_symbols: [ACNUSDT, AMCUSDT, APLDUSDT, CVNAUSDT, PATHUSDT, SECZUSDT, USDBRLUSDT]
持仓基线已同步到 position_tracking      symbols: [ACNUSDT, SECZUSDT, USDBRLUSDT]
持仓基线重建完成                        symbols: [ACNUSDT, SECZUSDT, USDBRLUSDT]
...
检测到 3 个持仓需要补全条件单           symbols: [ACNUSDT, SECZUSDT, USDBRLUSDT]
```

即：交易所空头（7 个）∩ 本策略自有币种（DB `open` 7 个）= **{ACNUSDT, SECZUSDT, USDBRLUSDT} 3 个**，补全循环只处理了这 3 个。APLDUSDT / AMCUSDT / PATHUSDT / CVNAUSDT 确实**从未进入补全循环**（与需求方观察一致）。

### 2.4 kline 表 schema 核实（问题 B 证据）

```bash
ssh root@43.156.242.184 "docker exec trading_system-postgres \
  psql -U trading_user -d trading_platform \
  -c \"SELECT table_schema, count(*) FROM information_schema.tables WHERE table_name LIKE 'kline_%' GROUP BY table_schema;\" \
  -c \"SHOW search_path;\" -c \"SELECT current_schema();\""
```

**实测结果（已核实）**：

- 全部 **527 张** `kline_%` 表都在 **`btc_eth`** schema；`public` 下**没有** kline 表副本。
- `search_path = btc_eth, btc_eth_aggressive, new_coin, grid, trading, public`
- `current_schema() = btc_eth`

---

## 3. 根因链（逐环，含源码路径 + 行号 + 生产证据）

### 3.1 根因链总览

| 编号 | 问题 | 结论 | 关键证据 |
|------|------|------|---------|
| R-A1 | 补全候选集来源 | 候选集**内存优先**：`self.positions` 非空即用内存，只有内存为空才回退查 DB `open` | `strategies/new_coin/strategy.py:551`、`:554-565` |
| R-A2 | `self.positions` 的口径 | `self.positions` 是「**交易所空头 ∩ 本策略自有币种(DB open)**」的交集；DB `open` 但交易所无仓的标的会被**静默剔除** | `strategy.py:1756-1798`、`:1826-1862`、`:1787`；日志「持仓基线重建完成 symbols: 3 个」 |
| R-A3 | 数据库对账缺口（**真凶**） | `_sync_positions_from_exchange` **只遍历 `self.positions`**，从不遍历 DB；因此「DB `open` 但不在 `self.positions`」的记录**永远无法被关闭** | `strategy.py:996-1032`（尤其 `:1013`）、`:1021` → `_handle_position_closed` → `executor.py:1370-1406` |
| R-A4 | 补全只做一次 | `_replenish_done` 一次性标志：首次成功后永不复核，持仓后续失去保护单不会自愈 | `strategy.py:102-103`、`:547`、`:618`、`:629` |
| R-B1 | kline 读路径存在性判断 | `_table_exists` **硬编码 `table_schema = 'public'`**，而表全在 `btc_eth` → 恒返回 False | `services/kline_service/api/routes.py:48-56` |
| R-B2 | P0-2 分支变死代码 | 「表已存在即放行」分支仅当存在性判断为真才走，而 R-B1 使其**永不为真** | `core/table_name_guard.py:214-225`；`api/routes.py:94-103`、`:124-138` |
| R-B3 | 同类硬编码扩散 | 同一硬编码 `table_schema='public'` 还出现在采集写路径与启动自检 → 噪音日志/误判 | `core/collector.py:219-224`、`:291-297`；`src/main.py:98-103` |
| R-C1 | 读路径静默化 | 白名单不命中返回 200 空数据，调用方无法区分「服务拒绝」与「真无数据」 | `api/routes.py:202-204`、`:250-258`、`:292-298`、`:344-350`；`shared/kline_service.py:139-165` |
| R-C2 | 空数据下游退化 | 调用方拿到空 list 时 `_calculate_atr` 返回 0，可能使 ATR 阈值退化为 0 且**无告警** | `strategies/new_coin/executor.py:1634-1669`；日志「计算ATR失败…400」被 `c6082a5` 改成 200 空后不再报错 |

### 3.2 问题 A 根因（逐环展开）

**R-A1｜候选集内存优先（`strategy.py:546-565`）**

```python
# strategy.py:546-554
if not self._replenish_done:
    # 优先从数据库状态获取持仓，如为空则从 new_coin.short_positions 表恢复
    positions_to_replenish = dict(self.positions) if self.positions else None
    ...
    if not positions_to_replenish:
        # 从 new_coin.short_positions 表恢复自己的持仓（B1 修复：精确过滤）
        db_positions = await self.db.fetch_all(
            "SELECT symbol, entry_price, opened_at FROM new_coin.short_positions "
            "WHERE status = 'open' ORDER BY opened_at ASC")
```

只要 `self.positions` 非空，DB `open` 集合（本轮为 7 个）**根本不会被读取**。本轮 `self.positions` = 3 个（见 R-A2），故候选集只有 3 个。

**R-A2｜`self.positions` = 交易所 ∩ DB open（`strategy.py:1756-1798`）**

`_restore_state()`（`strategy.py:1619-1754`）先从 `strategy_states` 恢复 `self.positions`（`:1640`），随后调用 `_rebuild_position_baseline()`（`:1754`）。后者：

1. `rebuild_from_exchange`（`shared/position_baseline.py:296-337`）取交易所空头（本轮返回 7 个）；
2. `_filter_own_positions`（`strategy.py:1826-1862`）按 `get_open_short_symbols()`（`executor.py:939-959`，即 DB `open` 的 7 个）过滤，**剔除他策略持仓**；
3. `self.positions = rebuilt`（`strategy.py:1787`）= 交易所 ∩ DB open = **{ACN, SECZ, USDBRL} 3 个**。

`strategy_states.main.positions` 实际只保存了 `{ACNUSDT, SECZUSDT, USDBRLUSDT}`（本次已核实），与上述交集一致。因此 `self.positions` 从来不包含那 4 个标的 → R-A1 把 DB 回退整段跳过 → 它们**不进补全循环**。

**R-A3｜数据库对账缺口（真凶，`strategy.py:996-1032`）**

```python
# strategy.py:1012-1022
# 清理 self.positions 中已不存在的持仓（条件单平仓后交易所已无仓位）
stale_symbols = [s for s in self.positions if s not in actual_short_symbols]   # ← 只遍历 self.positions
if stale_symbols:
    for s in stale_symbols:
        await self._handle_position_closed(s, self.positions.get(s, {}))
        del self.positions[s]
```

对账**只遍历 `self.positions`**，从不遍历 `new_coin.short_positions`。而 `_update_short_position_closed`（`executor.py:1370-1406`，唯一把 DB 置为 closed 的地方）仅由 `_handle_position_closed`（`strategy.py:1034`，`:1048`）与主动/条件单平仓路径调用——**全部针对 `self.positions` 内的标的**。

因此形成死循环：某标的在交易所已无仓 → 基线交集把它剔除出 `self.positions` → 对账永远扫不到它 → DB `status` 永远停在 `open` → 表现为「有 4 笔未平持仓没有保护单」的假象。

**R-A4｜补全只做一次（`strategy.py:102-103 / 547 / 618 / 629`）**

`_replenish_done` 首次成功后置位且**不再复位**（仅进程重启才归零）。即便某标的此后失去条件单，也不会被再次补全。

> 补充：即使这 4 个标的被强行塞进补全循环，`replenish_conditional_orders` → `_prepare_replenish_plans`（`executor.py:3384-3405`）第一步 `_resolve_short_quantity`（`executor.py:3321-3327`，读 `/papi/v1/um/positionRisk`）会得到 0 → 返回 `_REPLENISH_NO_POSITION`（`:3395-3398`）→ 视为成功、**不挂任何单**。也就是说「补全」对僵尸记录天然无效。

### 3.3 问题 B 根因（逐环展开）

**R-B1｜`_table_exists` 硬编码 public（`services/kline_service/api/routes.py:48-56`）**

```python
async def _table_exists(conn, table_name: str) -> bool:
    query = """
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_name = :table_name AND table_schema = 'public'
        )
    """
    return await conn.fetch_val(query, {"table_name": table_name})
```

`btc_eth` schema 下有 527 张 `kline_%` 表，`public` 下一张都没有 → 该查询**恒返回 False**。

**R-B2｜P0-2 分支成为死代码（`core/table_name_guard.py:188-226`）**

`build_readable_table_name` 的顺序为：格式/表名层 → 白名单命中即放行 → **未命中且开关开启且注入了 `table_exists` → 表存在则放行**（`:214-225`）。存在性回调由 `routes.py:_make_table_exists_checker`（`:106-112`）注入，最终落到 R-B1 的 `_table_exists`。因 R-B1 恒 False，`:221` 的 `if not exists` 永远成立 → 抛 `TableNameValidationError` → **「表已存在即放行」分支永远走不到**。上一轮 P0-2 修复因此等于没生效。

**R-B3｜同类硬编码扩散**

同一模式还出现在：
- `services/kline_service/core/collector.py:219-224`（`_create_table_if_not_exists` 的存在性判断）
- `services/kline_service/core/collector.py:291-297`（`ensure_table` 的存在性判断）
- `services/kline_service/src/main.py:98-103`（启动自检 expected_tables）

后果：对已存在表仍反复走「创建」分支（`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` 幂等，功能上不出错，但产生误导性日志）；启动自检会把实际存在的核心表误报为「缺失」。

### 3.4 问题 C 根因（逐环展开）

**R-C1｜读路径把「本该报错」的情况静默化为 200 空数据**

`c6082a5` 后，`/klines/latest`（`routes.py:250-258`）与 `/indicators`（`:344-350`）在 `HTTPException(400)` 且 `detail` **文本**含「不在白名单」或「表不存在」时，返回 `{"code":0,"message":"无数据（不在采集范围）","data":[]}`。这带来两个问题：
1. 区分依据是 **detail 文案字符串匹配**（`"不在白名单" in detail`），脆弱、易随文案改动失效；
2. 「白名单未命中」与「命中白名单但表缺失/建表失败/DB 异常」被**混为一谈**——后者本应报错/告警，却也被降级为静默空数据（`_resolve_ready_table` 在 `_ensure_table_ready` 失败时统一返回 `""`，见 `:161-172`）。

**R-C2｜调用方拿到空数据后的退化（`executor.py:1634-1669`）**

```python
klines = await self.kline_service.get_klines(symbol=symbol, interval=interval, limit=limit)
if not klines or len(klines) < period + 1:
    logger.warning(f"K线数据不足，无法计算ATR: {symbol}")
    return Decimal('0')
```

`get_klines`（`shared/kline_service.py:139-165`）对 200+空 list **不抛异常**，直接返回 `[]`。于是「服务拒绝」与「数据不足」在下游不可区分，ATR 返回 0。

已核实的对照证据：重启日志中 ACN/SECZ/USDBRL 在 00:58 时 ATR 计算失败是 **`K线服务请求失败: 400`（error 级）**；`c6082a5` 上线后同类情况会变成 **200 空 list（无 error 级信号）**。对持仓保护而言，ATR=0 会让 ATR 相关阈值退化（`executor.py:1904-1911` 只汇总告警一次），**且不一定产生任何告警**——正是需求方担心的「持仓失去保护后无人知晓」。

### 3.5 ⚠️ 关键更正（需求方初始前提部分不成立）

需求方判断：「仍有 4 笔未平持仓完全没有保护单」——**其中「完全没有保护单」成立，但「未平持仓」不成立**。

- 已核实：APLDUSDT / AMCUSDT / PATHUSDT / CVNAUSDT 在交易所**没有真实空头持仓**（§2.3）。
- 它们只是 `new_coin.short_positions` 中 **4 条僵尸 `open` 记录**（§2.2），且条件单已全部 CANCELED。
- 因此问题 A 的**正确修复落点是「对账 + 连续守卫 + 告警」**，而不是「给这 4 笔补挂保护单」。若误按「补挂」处理，`_resolve_short_quantity` 会返回 0（无仓），补全直接返回「无持仓」成功，僵尸记录仍不会被关闭，死循环继续（见 §5）。

> 需求方三项修复方向**依然成立**（候选集来源确有缺陷、kline 存在性判断确为死代码、读路径静默化确有风险），只是 P0-A 的落点需从「补挂保护单」重新聚焦到「候选集/对账/守卫/告警」。

---

## 4. 修复范围

### 4.1 问题 A（P0）：new_coin 持仓-条件单一致性

| 编号 | 修复点 | 说明（不写实现细节） |
|------|--------|----------------------|
| A-1 | **候选集改为权威来源** | 补全/守卫的标的集合**不得以内存优先**。应以「本策略自有未平仓币种（DB `open`）」与「交易所实际空头」的**交集**为准（真实敞口）；`self.positions` 仅作缓存，不作为唯一来源。 |
| A-2 | **DB ↔ 交易所双向对账** | 每周期对「DB `open` 集合」与「交易所实际空头」做对账：<br>· DB `open` 但交易所无仓 → 判为**僵尸记录**，自动置 `closed`（写入 `closed_at`）并记日志/计数；<br>· 交易所有仓且 ∈ DB `open` → 真实敞口，纳入保护守卫；<br>· 交易所有仓但 ∉ DB `open` → 判为**他策略持仓**，**一律不动**（PM 账户归属口径）。 |
| A-3 | **连续保护守卫（替代一次性补全）** | 把 `_replenish_done` 的「只补一次」改为**按配置周期的持续守卫**：对每个真实敞口核验「存在且仅存在 1 条 OPEN 的 STOP_LOSS + 按配置的 TP 单」，缺失即补挂。 |
| A-4 | **失败可重试且可告警** | 补全/守卫任一标的存在缺口（含 ATR 不可用、挂单失败、撤单失败）时：不置「已完成」、下周期重试，并发出告警（飞书，按配置降频）。 |
| A-5 | **逐标的隔离与幂等** | 单标的失败不得阻断其余标的；重复运行不得重复挂单；同一 symbol 不得并发补全。 |

### 4.2 问题 B（P0）：kline 表存在性判断尊重 search_path

| 编号 | 修复点 | 说明 |
|------|--------|------|
| B-1 | **统一存在性判断** | 把 `routes.py:48-56` 的 `_table_exists` 改为**尊重 search_path** 的判断（如 `SELECT to_regclass(:table_name) IS NOT NULL`），保持**参数化**、**fail-closed**（查询异常 → 视为不存在/拒绝，不静默放行）。 |
| B-2 | **消除同类硬编码** | `collector.py:219-224`、`collector.py:291-297`、`src/main.py:98-103` 三处同步替换；全仓不得再出现硬编码 `table_schema = 'public'` 的 kline 存在性判断（R01「单词点」原则）。 |
| B-3 | **缓存语义正确** | 存在性缓存（TTL）不得把「已存在」误判为「不存在」；缓存口径需与新的判断方式一致。 |
| B-4 | **分支真实性** | 修复后，「表已存在且格式合法」的标的（即使不在白名单）读路径必须真正放行（记 `source=existing_table`）。 |

### 4.3 问题 C（P0）：读路径静默化的可观测性与契约

| 编号 | 修复点 | 说明 |
|------|--------|------|
| C-1 | **明确三类响应契约** | ① 参数非法（注入风险）→ **400**，必须保持；② 白名单未命中且表不存在（尚未采集）→ **200 空数据**，不告警；③ **命中白名单/应采集但表缺失、建表失败、DB 异常** → **不得 200 空**（应 5xx 或显式错误码）+ 告警。 |
| C-2 | **去除文案匹配** | 400 与「无数据 200」的区分不得依赖 `detail` 文案字符串（现 `routes.py:256`、`:348`），改用显式错误类型/错误码。 |
| C-3 | **调用方不得静默** | `get_klines` 拿到空数据时，调用方必须显式区分「服务拒绝」与「数据不足」，对「服务拒绝」记 ERROR 级日志并可告警。 |
| C-4 | **保护缺失必告警** | 任一真实未平持仓在守卫核验时发现缺少 OPEN 的 STOP_LOSS / TP 单，必须飞书告警（含 symbol、缺哪类单、重试次数）。 |
| C-5 | **ATR=0 不得退化执行** | 监控路径中 ATR 不可用（=0）时，不得按 0 阈值触发 ATR 相关平仓动作，须跳过并告警。 |
| C-6 | **告警降频** | 同一 symbol 同一原因在配置窗口内只告警一次，避免刷屏。 |

---

## 5. 这 4 笔「持仓」的保护单如何恢复（明确回答）

### 5.1 结论：**自动对账关闭僵尸记录**，而不是「自动补全」或「一次性人工修复」

基于 §2.3 的核实，正确机制如下（三选一，按优先级）：

1. **首选——自动对账（问题 A 的 A-2）**：修复后的对账逻辑每周期发现「DB `open` 但交易所无仓」，自动把记录置为 `closed`。僵尸记录消失后，就不会再出现「看起来有 4 笔无保护持仓」的假象。**这是本轮要交付的机制**，无需人工介入。
2. **兜底——一次性人工修复（仅在自动对账未上线前的临时手段）**：按 §5.3 的 SQL 手动将这 4 条 `open` 记录置为 `closed`。**不是本轮正式交付**，仅用于确需立即止血时。
3. **不采用——自动补全保护单**：因为 `_resolve_short_quantity` 对无仓标的返回 0，补全会直接返回「无持仓」成功，既挂不上单也关不掉僵尸记录，**只会让死循环继续**。因此「给这 4 笔补挂保护单」是错误动作。

> 若生产复核发现这 4 个标的**确实在交易所有真实空头**（即 §2.3 结果被推翻），则正确动作是 A-1/A-3：由权威候选集识别出真实敞口并**自动补全**（`replenish_conditional_orders`），再按 §6 的 P0-A 验收。请架构环节把「先用 §5.3 命令确认是否存在真实敞口」写入实现前置步骤。

### 5.2 恢复后的可核验验证口径

**（1）僵尸记录是否已关闭（对账生效）**：

```bash
ssh root@43.156.242.184 "docker exec trading_system-postgres psql -U trading_user -d trading_platform \
  -c \"SELECT symbol,status,closed_at FROM new_coin.short_positions \
        WHERE symbol IN ('APLDUSDT','AMCUSDT','PATHUSDT','CVNAUSDT') ORDER BY symbol;\""
```

预期：4 条记录 `status != 'open'` 且 `closed_at` 非空。

**（2）真实敞口是否都有保护单**：

```bash
# 真实空头（交易所）∩ DB open，每一个都必须有 OPEN 的 STOP_LOSS
ssh root@43.156.242.184 "docker exec trading_system-postgres psql -U trading_user -d trading_platform \
  -c \"SELECT s.symbol, s.status AS pos_status, \
        count(*) FILTER (WHERE c.order_type='STOP_LOSS' AND c.status='OPEN') AS open_sl \
      FROM new_coin.short_positions s \
      LEFT JOIN btc_eth.condition_orders c ON c.symbol = s.symbol AND c.strategy='new_coin' \
      WHERE s.status='open' GROUP BY s.symbol, s.status ORDER BY s.symbol;\""
```

预期：每一行的 `open_sl >= 1`（当前应为 SECZUSDT=2、USDBRLUSDT=9）。

**（3）日志口径**：

```bash
ssh root@43.156.242.184 "docker logs --since 2h trading_system-new_coin 2>&1 | grep -E '对账|僵尸|closed|保护'"
```

预期出现「对账：关闭僵尸持仓记录」类日志与计数，且不再出现反复的「补全条件单失败」。

**（4）守卫是否持续生效**：

```bash
ssh root@43.156.242.184 "docker logs --since 2h trading_system-new_coin 2>&1 | grep -iE 'control|guard'"
```

预期：每个守卫周期都有核验记录（而非仅启动一次）。

### 5.3 一次性人工修复 SQL（仅临时止血，非本轮交付）

> ⚠️ 执行前先用 §2.3 命令确认这些标的确无交易所持仓；若有真实敞口，**禁止**关闭其记录。

```sql
-- 仅用于把「交易所无仓的僵尸 open 记录」置为 closed；closed_at 必须为 naive timestamp
UPDATE new_coin.short_positions
SET status = 'closed', closed_at = now() AT TIME ZONE 'UTC'
WHERE symbol IN ('APLDUSDT','AMCUSDT','PATHUSDT','CVNAUSDT') AND status = 'open';
```

执行后按 §5.2 的（1）核验。

---

## 6. 验收标准

> 编号规则：`P0-A-ACn`（问题 A）、`P0-B-ACn`（问题 B）、`P0-C-ACn`（问题 C）。每条均可直接转为测试用例。

### 6.1 问题 A（P0-A）

| 编号 | 验收标准 | 验证方式 |
|------|---------|---------|
| P0-A-AC1 | 补全/守卫的标的集合来自**权威集合**（DB `open` ∩ 交易所实际空头），不再「内存非空即用内存」；日志须打印候选集来源与成员 | 构造「内存有 A、DB open 有 A+B、交易所仅有 B」场景，断言候选集含 B；日志含来源字段 |
| P0-A-AC2 | DB `open` 但交易所无仓的标的，每个周期被自动置为 `closed` 且写入 `closed_at`，并记录计数日志 | 造 3 条僵尸记录，跑一轮守卫，断言 3 条变 `closed`、`closed_at` 非空 |
| P0-A-AC3 | 每个真实敞口（DB `open` ∩ 交易所空头）都有且仅有 1 条 OPEN 的 STOP_LOSS（及按配置的 TP 单）；缺失即补挂 | 删除某真实敞口的 SL，跑守卫，断言补挂 1 条 OPEN SL |
| P0-A-AC4 | `_replenish_done` 一次性语义被移除，改为按配置周期（配置项）持续核验；不得只在启动补一次 | 断言守卫周期 > 1 次可复核；配置项存在且生效 |
| P0-A-AC5 | 守卫存在缺口（ATR 不可用 / 挂单失败 / 撤单失败）时不置「已完成」，下周期重试，且发出告警（降频） | mock 挂单失败，断言不置位、下轮重试、触发告警 |
| P0-A-AC6 | 交易所空头 ∩ DB `open` 之外的交易所持仓**一律不动**（不补单、不平仓、不告警为他策略） | 造他策略持仓，断言无任何操作 |
| P0-A-AC7 | 幂等：重复运行不重复挂单；同一 symbol 不并发补全 | 并发调用两次守卫，断言 SL 数不增 |
| P0-A-AC8 | 逐标的隔离：单标的失败不阻断其余标的处理 | 让 A 失败，断言 B/C 仍被处理 |
| P0-A-AC9 | 僵尸记录被关闭后，不再反复出现在任何「补全/告警」日志中 | 跑两轮守卫，断言第二轮无该 symbol 相关日志 |

### 6.2 问题 B（P0-B）

| 编号 | 验收标准 | 验证方式 |
|------|---------|---------|
| P0-B-AC1 | `routes.py` 的存在性判断**尊重 search_path**（`to_regclass(:table_name)` 或等价），参数化，无硬编码 schema | 代码审查 + 单测：mock 连接返回 `True/False/异常` 三分支 |
| P0-B-AC2 | 全仓不再存在 kline 存在性判断的硬编码 `table_schema = 'public'`（`routes.py` / `collector.py`×2 / `src/main.py`） | `grep` 断言 0 命中 |
| P0-B-AC3 | 「表已存在即放行」在真实 schema（`btc_eth`）下可命中：构造「表 `btc_eth.kline_xxx_1h` 存在但不在白名单」的 symbol，读路径放行（200+数据）并记 `source=existing_table` | 集成测试（真实/等价 schema 环境） |
| P0-B-AC4 | fail-closed：存在性查询异常时返回拒绝（400）而非静默放行 | 单测：存在性回调抛异常 → 断言拒绝 |
| P0-B-AC5 | 存在性缓存（TTL）不把「已存在」误判为「不存在」 | 单测：先查得存在，TTL 内命中应为存在 |
| P0-B-AC6 | 启动自检（`src/main.py`）不再把实际存在的核心表误报为缺失 | 单测/日志断言 |

### 6.3 问题 C（P0-C）

| 编号 | 验收标准 | 验证方式 |
|------|---------|---------|
| P0-C-AC1 | 三类响应契约明确：参数非法→400；白名单未命中且表不存在→200 空；**应采集但表缺失/建表失败/DB 异常→非 200 且告警** | 单测覆盖三类输入，断言状态码与告警 |
| P0-C-AC2 | 400 与「无数据 200」的区分不再依赖 `detail` 文案字符串匹配 | 代码审查 + 单测：改文案不影响判定 |
| P0-C-AC3 | 调用方对「服务拒绝」记 ERROR 并可告警；对「数据不足」保留 warning；两者可区分 | 单测：mock 返回空 list 且错误码=应采集 → 断言 ERROR/告警 |
| P0-C-AC4 | 真实未平持仓缺少 OPEN STOP_LOSS/TP 单时，必发告警（含 symbol、缺哪类、重试次数） | mock 缺单场景，断言告警被调用 |
| P0-C-AC5 | ATR=0（数据不可用）时监控不按 0 阈值平仓，跳过并告警 | 单测：ATR=0 → 断言无 ATR 相关平仓调用 |
| P0-C-AC6 | 同一 symbol 同一原因在配置窗口内仅告警一次 | 连续触发，断言告警次数 = 1 |

**合计：问题 A 9 条 + 问题 B 6 条 + 问题 C 6 条 = 21 条验收标准。**

---

## 7. 影响面

| 变更点 | 影响范围 | 风险 |
|--------|---------|------|
| A-1/A-2/A-3/A-4/A-5 | `strategies/new_coin/strategy.py`、`strategies/new_coin/executor.py`、`strategies/new_coin/config.yaml` | 触及交易主流程与资金安全，必须走强制测试；改动涉及 `new_coin` 容器，需重建 `trading-new-coin` |
| A-2 对账 | `new_coin.short_positions` 数据（会新增 `closed` 记录） | 若归属判定错误，可能误关他策略或漏关；需 A-AC6 保护 |
| B-1/B-2 | `services/kline_service/api/routes.py`、`core/collector.py`、`src/main.py`（+ 可能新增公共判断函数） | 读路径放行口径变化，需保证仍 fail-closed，不得放宽 SQL 注入防护 |
| B-3 | 存在性缓存 | 缓存口径变化，需防「误判不存在」 |
| C-1/C-2 | `services/kline_service/api/routes.py` | 响应契约变化，须同步核对所有调用方（`shared/kline_service.py`、各策略） |
| C-3/C-4/C-5/C-6 | `strategies/new_coin/*`（调用方与监控） | 新增告警，需确认飞书通道与降频，避免刷屏 |
| 部署 | `services/kline_service`、`strategies/new_coin` 两个镜像 | push main 后 Actions 增量重建这两类容器；`shared/` 若改动会触发全部策略容器重建 |

> 说明：本次不涉及数据库表结构变更（`short_positions` 已有 `status/closed_at` 列），无迁移脚本需求。

---

## 8. 不在本轮范围

1. 其他策略（`btc_eth` / `grid` / `hrs` / `btc_eth_aggressive`）的补全/对账逻辑——仅当 `shared/` 有共用改动时评估影响，但不在本轮主动修改。
2. `new_coin` 入场时的 `short_positions` 写入失败告警（属于「交易所敞口但 DB 无记录」的另一类问题，本轮不覆盖）。
3. `pm`/账户层面的保证金、杠杆、资金费率等计算逻辑。
4. 条件单表（`btc_eth.condition_orders`）的历史数据清洗（ACNUSDT 残留 OPEN 条件单等）。
5. 前端/看板展示变更。
6. 上一轮 `c494571` / `c6082a5` 已验收的 P0-1/P0-3 逻辑（本轮不重做），仅在必要时对齐。

---

## 9. 约束与规范（写进实现）

- **禁硬编码**：阈值/周期/误差码/告警窗口等必须来自配置（如 `replenish` / `kline` 段），严禁写死。
- **禁重复代码**：连续 5 行以上同构逻辑视为违规（问题 B 的四处存在性判断应提炼为单一实现）。
- **禁幽灵参数**：定义即使用。
- **单函数 ≤ 50 行、单行 ≤ 120 字符**，同一文件风格一致。
- **注释与日志一律中文**（含新增告警文案）。
- **回测只能在本地跑**，严禁在服务器执行回测（本次不涉及回测，仅提醒）。
- **fail-closed**：任何校验/查询异常一律拒绝，不静默放行。
- **不写代码**：本文档只定义行为，实现由架构/编码环节完成。

---

## 10. 需要架构环节重点决策的点

1. **候选集与对账的单一事实来源**：如何在同一处同时表达「本策略归属（DB open）」「真实敞口（交易所空头）」「僵尸（DB open 但交易所无）」三种状态，并保证与既有 `_filter_own_positions` / `get_open_short_symbols` 不重复、不冲突（避免出现第二套归属判定）。
2. **守卫周期与幂等/并发**：连续守卫的周期配置、与 `_monitor_positions` 的关系、同一 symbol 的并发互斥、如何避免每周期重复撤挂造成条件单抖动。
3. **僵尸关闭的触发条件与安全边界**：确需「交易所连续 N 次无仓」才关，还是单次即可；如何防止交易所接口抖动导致误关真实敞口。
4. **响应契约的错误码设计**：问题 C 的「非 200 且可告警」用 5xx 还是自定义 `code`；如何在不破坏既有调用方（`shared/kline_service.py` 及各策略）的前提下落地。
5. **告警通道与降频**：复用现有飞书通道的哪一级别、窗口配置放哪个配置段、与既有降频机制的复用。
6. **`shared/` 改动面**：若对账/守卫逻辑放入 `shared/`，将触发全部策略容器重建；需评估是否应放在 `strategies/new_coin/` 内以收敛部署影响。
