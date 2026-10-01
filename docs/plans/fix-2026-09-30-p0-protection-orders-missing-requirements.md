# P0 修复需求文档：持仓保护单丢失（补全条件单失败 + 重复裸奔）

## 1. 文档信息

| 项 | 内容 |
|----|------|
| 文档名称 | P0 修复需求文档：持仓保护单丢失（补全条件单失败 + 重复裸奔） |
| 版本 | v1.0（初稿，待评审） |
| 作者 | requirements-document-expert |
| 创建日期 | 2026-09-30 |
| 触发事件 | 2026-09-30 部署（commit `cfe4ebe`，Actions Run #44）上线 R01–R08 后暴露 |
| 需求来源 | 生产事件（现象 + 根因链已取到生产证据，见 §2、§3） |
| 适用范围 | 本轮只修本文件 §4「修复范围」的三项（P0-1 / P0-2 / P0-3）；其余 R 系列问题不在本轮 |
| 下游环节 | backend-architect（架构设计）→ python-engineer（编码实现） |
| 验收口径 | 每项以本文件「验收标准」小节为准，编号 `P0-x-ACn`，可直接转为测试用例 |

> 说明：本文档只描述「应该是什么行为」，不写代码实现。文中「伪代码级状态流转」仅用于消除歧义，实现细节由架构/编码环节决定。
>
> ⚠️ 重要：本文档在核实阶段发现需求方对「根因环 2」的初始判断与实际代码不符，已在 §3.2 以生产证据更正。请以更正后的根因链为准（需求方三项修复方向仍然成立，但 P0-1 的落点需要重新聚焦，见 §4、§7）。

---

## 2. 事件背景（已核实事实）

### 2.1 现象（生产日志原文，容器 `trading_system-new_coin`，每周期重复）

```
检测到 2 个持仓需要补全条件单   symbols: ["USDBRLUSDT", "ACNUSDT"]
开始补全条件单: USDBRLUSDT   entry_price=5.1158
无本地存储的条件单 algoId，尝试从数据库回退查询
从数据库回退查询到条件单   count=24
开始取消孤儿条件单   total=24  source=position_tracking
条件单已不存在（可能已触发）  algo_id=2000001458540679  role=db_0   ...（24 条，全部 "已不存在"）
孤儿条件单清理完成   total=24  cancelled=24  failed=0
计算ATR失败: USDBRLUSDT, 错误: K线服务请求失败: 400        level=error
ATR计算失败，跳过补全条件单: USDBRLUSDT                     level=warning
补全条件单失败，下周期将重试: USDBRLUSDT                    level=warning
（ACNUSDT 同样流程，entry_price=177.7，DB 回退 3 个条件单）
条件单补全部分失败，下周期将重试  all_success=false sync_failed=false
```

发生周期：2026-09-30 的 04:00 / 05:00 / 06:00 / 07:00 UTC（12:00/13:00/14:00/15:00 CST），全部重复。

### 2.2 影响

- **直接（P0 资金风险）**：new_coin 的 `USDBRLUSDT`、`ACNUSDT` 两笔**做空持仓当前无止损/止盈保护单**，处于裸奔状态。
- **持续性**：补全逻辑每个周期重复「撤旧单 → 算 ATR 失败 → 不挂新单」，裸奔状态**不会自愈**，直到人工干预。
- **潜在**：只要某策略对「未处于 kline 注册表 active 状态」的标的取 K 线，就会命中同一个 400（见 §11 影响面）。

---

## 3. 根因链（逐环，含生产证据）

### 3.1 根因链总览

| 环 | 结论 | 证据 |
|----|------|------|
| R-A（**真实触发原因**） | new_coin **在"入场成功"后立即把刚建仓的标的从 kline 服务注销**，注册状态被置为 `cancelled` | `strategies/new_coin/strategy.py:776-784`；生产日志 05:02:00「入场成功，已停止监控: ACNUSDT」+「已取消标的注册：ACNUSDT」 |
| R-B（白名单口径） | kline 白名单只认"注册表 active"，不认"数据表已存在"，使"有数据但已注销"的标的被挡成 400 | `services/kline_service/core/table_name_guard.py:76-88`、`:122-141`；`api/routes.py:105-106`、`:191-192` |
| R-C（数据其实存在） | `kline_usdbrlusdt_1h`、`kline_acnusdt_1h` 两表在库真实存在，400 属"把有数据的标的挡在门外" | 服务器 `pg_tables` 实测（见 §3.3） |
| R-D（顺序缺陷放大后果） | 补全流程**先撤旧条件单、后算 ATR**，ATR 失败即 `return False`，导致旧保护单被撤、新单未挂 | `strategies/new_coin/executor.py:3253`（先撤）、`:3276`（后算）、`:3277-3281`（失败即 return） |
| R-E（重复不停） | 返回 `False` 使 `_replenish_done` 一直不置位，每周期重复撤单 + 重复失败 | `strategies/new_coin/strategy.py:102-103`、`:492-570` |

### 3.2 ⚠️ 更正：需求方原「环 2」判断有误（注册写入路径**并无**该缺陷）

需求方原描述为：「ACNUSDT 重新注册成功但 `status` 仍为 `cancelled`……疑似注册写入路径有缺陷（疑似 INSERT ... ON CONFLICT 只更新了有效期，没有把 status 复位为 active）」。

**该判断不成立，证据如下：**

1. **注册写入路径本身是正确的**（`services/kline_service/core/registry.py`）：
   - `register()`（`:76-115`）对已存在行分两条支路：`existing.status=='active'` → `_update_registration()`；否则 → `_reactivate_registration()`。
   - `_reactivate_registration()`（`:145-162`）显式 `config.status = 'active'` 后落库。
   - `_save_to_database()`（`:286-340`）的 UPDATE 语句**包含 `status = :status`**（`:303`），并非"只更新有效期/区间"。
   - 该文件自初始提交 `fd1d92c` 后**未被 `cfe4ebe` 修改**（`git log -- services/kline_service/core/registry.py` 仅 fd1d92c）。部署镜像内文件与仓库 HEAD 一致（已 `grep` 部署容器 `/app/core/registry.py` 确认 `status = :status` 存在、`_reactivate_registration` 存在）。
2. **生产日志显示 04:01:39 走的是"更新"而非"重新激活"，更新后仍是 active**：
   ```
   2026-09-30 04:01:39 | WARNING | core.registry | 标的 ACNUSDT 已注册，将更新配置
   2026-09-30 04:01:39 | INFO    | core.registry | ✅ 已更新标的注册：ACNUSDT，新过期时间：2026-10-10 04:01:39.764883
   ```
   即当时 ACNUSDT 在**内存缓存中即为 active**，走 `_update_registration`，`status` 保持 active。
3. **`status` 变成 `cancelled` 是一次显式注销**，发生在**持仓开仓成功之后**：
   ```
   2026-09-30 05:01:58  new_coin 开空仓 ACNUSDT，entry=177.7，随后挂 SL/TP1/TP2（algo 2000001476792189/191/192）
   2026-09-30 05:02:00  new_coin 日志「入场成功，已停止监控: ACNUSDT」
   2026-09-30 05:02:00  kline 日志「✅ 已取消标的注册：ACNUSDT」/「API: 取消注册 ACNUSDT」
   ```
   对应代码：`strategies/new_coin/strategy.py:776-784`——
   ```python
   if entry_success:
       # 入场成功后立即停止对该币种的监控
       self.listing_detector.known_symbols.add(symbol)
       await self.listing_detector._save_known_symbols()
       await self.kline_service.unregister_symbol(symbol)   # ← 把刚建仓的标的注销掉
       logger.info(f"入场成功，已停止监控: {symbol}")
   ```
4. **数据自洽**：`registered_at` 保持 09-29（`_save_to_database` UPDATE「不含 registered_at，该字段在创建后不再修改」，注释见 `registry.py:298`），`expires_at` 被 04:01:39 的更新顺延到 10-10，`status` 被 05:02:00 的注销改为 cancelled——三字段完全吻合"先更新、后注销"的时序。

**结论**：这不是"注册写入缺陷"，而是 **new_coin 主动注销了仍有持仓的标的**（R-A）。因此需求方三项修复方向中的「修掉"重新注册成功但 status 仍为 cancelled"」不应作为**主攻方向**（当前代码无此缺陷），应改为**防御性加固 + 聚焦"持仓标的不得被注销 / 用前必须 active"**（见 §7）。

### 3.3 数据表存在性证据（服务器只读实测）

```
# pg_tables（DB=trading_platform, user=trading_user）
 kline_acnusdt_1h
 kline_usdbrlusdt_1h

# registered_symbols
 ACNUSDT    | cancelled | registered_at=2026-09-29 06:01:47 | expires_at=2026-10-10 04:01:39 | duration_days=10 | created_by=api
 USDBRLUSDT | cancelled | registered_at=2026-09-18 15:01:58 | expires_at=2026-10-03 09:01:33 | duration_days=10 | created_by=api
 SECZUSDT   | active    | registered_at=2026-09-29 06:01:48 | expires_at=2026-10-10 04:01:40 | duration_days=10 | created_by=api
```

`ACNUSDT` 与 `SECZUSDT` 在同一次 04:01:39 被更新（`expires_at` 均为 10-10 04:01:3x），但 `SECZUSDT` 仍 active、`ACNUSDT` 变为 cancelled，唯一差异就是 ACNUSDT 随后被 new_coin 注销——进一步印证 R-A。

---

## 4. 修复范围与非目标

### 4.1 修复范围（三项，全部要做）

| 编号 | 类别 | 一句话目标 | 落点 |
|------|------|-----------|------|
| **P0-1** | 策略侧 | 持仓标的必须保持 K 线可用（不得注销有持仓的标的；用前保证 active） | `strategies/new_coin/strategy.py`；`services/kline_service/{api/registry_routes.py,core/registry.py}`（防御性） |
| **P0-2** | 服务侧 | kline 白名单放行"格式合法且数据表已存在"的标的，同时保持 fail-closed | `services/kline_service/core/table_name_guard.py`、`api/routes.py` |
| **P0-3** | 策略侧 | 补全保护单改为"先算 ATR 再撤旧单"，任何失败路径都不裸奔 | `strategies/new_coin/executor.py`（`replenish_conditional_orders`） |

### 4.2 非目标（本轮明确不做）

1. **不修** R01–R08 之外的其它问题（如 R09–R18）；不在本轮重构整个执行器。
2. **不改变** kline 白名单的注入防护强度（表名严格正则、参数化查询必须保留）。
3. **不改变** PM 账户下单语义（条件单参数、reduceOnly、精度处理）。
4. **不引入**服务器回测；所有验证本地 mock/单测 + 服务器只读核对。
5. **不把** kline 白名单整体放开为"任意 symbol 只要表存在就允许注册"——放行仅限「格式合法 + 表已存在」的**查询/读取**路径，注册与建表路径的白名单语义保持不变。
6. **不移除** new_coin「入场后停止对该币"新币检测"监控」的业务意图——要保留"停止检测/停止新建采集任务"的收益，同时保证"已有持仓所需的数据可用"（两目标需解耦，见 §7.2 与开放问题 Q1）。

---

## 5. 全局约束（三项必须遵守）

### 5.1 交易与安全硬约束

1. **裸奔兜底优先**：任何失败路径都不得在"没有能力挂回新保护单"时撤掉已有的保护单。
2. **fail-closed**：白名单/表名校验任一层失败即拒绝；禁止为放行而放宽表名正则或改参数化查询为字符串拼接（**不得引入 SQL 注入面**）。
3. **禁止服务器回测**：验证一律本地 mock/单测，服务器侧只做只读核对。
4. **数据库只存状态/交易数据**：阈值、重试次数、超时、间隔、白名单、放行开关等一律走配置或算法推导。

### 5.2 代码质量硬约束（编码环节强制）

- **禁止硬编码**：本轮不得写死具体币种名/数值；已有硬编码须顺带提取（见 §9.3、§12）。
- **禁止重复代码**：相同逻辑（尤其"补全保护单"这类 ≥3 处同构）必须抽公共模块。项目已有 `shared/protection_retry.py` 作为跨策略保护单逻辑单点（btc_eth / 激进版共用），new_coin 走自己的 `executor.py` 路径；是否抽取见 §12 与开放问题 Q2。
- **禁止幽灵参数**：未使用参数用 `_` 前缀或删除。
- 单函数 ≤ 50 行；单行 ≤ 120 字符；注释、日志一律中文。

### 5.3 术语表

| 术语 | 定义 |
|------|------|
| 注册态 | `registered_symbols.status` 的值（`active` / `expired` / `cancelled`） |
| 白名单 | `build_kline_table_name` 允许的 (symbol, interval) 集合 = `FIXED_SYMBOLS ∪ settings.SYMBOLS ∪ 注册表 active` |
| 保护单 | 硬止损 SL、第一目标止盈 TP1、第二目标止盈 TP2 三类条件单 |
| 补全 | 策略重启/发现持仓后，为持仓重新挂齐缺失的保护单（`replenish_conditional_orders`） |
| 裸奔 | 真实持仓存在但无任何止损/止盈保护单 |
| 数据表 | `kline_{symbol}_{interval}` 形式的 K 线物理表 |
| 未自愈 | 失败状态在后续周期被重复执行且持续失败，不会自动恢复 |

---

## 6. 修复总表

| 编号 | 严重度 | 类别 | 基线位置 | 影响容器 | 触发全量重建 |
|------|--------|------|----------|----------|--------------|
| P0-1 | P0 | 资金风险 | `strategies/new_coin/strategy.py:776-784`；`services/kline_service/api/registry_routes.py:183-196`、`core/registry.py:76-162` | new-coin（+ kline-service 若改注册接口） | 否（new-coin，或加 kline-service） |
| P0-2 | P0 | 安全/可用性 | `services/kline_service/core/table_name_guard.py:76-88`、`:122-141`；`api/routes.py:52-62`、`:105-106`、`:191-192` | kline-service | 否（仅 kline-service） |
| P0-3 | P0 | 资金风险 | `strategies/new_coin/executor.py:3215-3530`（重点 `:3251-3281`） | new-coin | 否（仅 new-coin） |

> 建议实施顺序：P0-3（止血，最小改动，先让持仓恢复保护）→ P0-1（消除复发根因）→ P0-2（服务侧兜底，覆盖"历史已注销持仓"及未来同类标的）。
>
> 三项若合并为**一次** new-coin + kline-service 重建，可减少停机次数；是否抽 shared 见 §12。

---

## 7. P0-1（策略侧）：持仓标的必须保持 K 线可用

### 7.1 问题现状

`strategies/new_coin/strategy.py:776-784`：入场成功后无条件 `unregister_symbol(symbol)`，把刚建仓的标的注册态置为 `cancelled`；随后该标的的任何 K 线读取（含补全保护单时的 ATR）被白名单拒绝（400）。

### 7.2 修复目标

- **持仓标的的 K 线读取必须长期可用**：只要存在未平仓持仓，就不能因"停止新币监控"而失去该标的的 K 线服务能力。
- **注册态一致性**：策略使用某标的 K 线前，该标的应处于注册表 `active` 态；注册接口对"重新注册已存在行"必须幂等地把 `status` 复位为 `active` 并返回真实状态供调用方校验。
- **保留原业务收益**：仍要停止"对该币的新币检测/新采集任务创建"，避免资源浪费（两目标解耦）。

### 7.3 功能需求

- **P0-1-F1 有持仓不得注销**：`unregister_symbol` 的调用点必须前置「该 symbol 无本策略未平仓持仓」判定；有持仓时不注销（保留注册态与采集任务）。
- **P0-1-F2 用前 ensure-active（兜底）**：策略在取某标的 K 线前（尤其补全/维护既有持仓路径），若该标的非 active 注册态，应先"确保注册 active"（重新注册并**校验返回 status 确为 active**）；确保失败则不得继续走到 ATR 计算。
- **P0-1-F3 注册接口幂等复位 status**：`registry.register()` 对已存在行（无论 `active`/`expired`/`cancelled`）必须把 `status` 复位为 `active` 并续期；接口返回体必须回传**落库后的真实 status**（不得只回传内存对象）。这是防御性加固，禁止依赖"当前代码看起来正确"。
- **P0-1-F4 注销接口返回可校验**：策略侧 `unregister_symbol` 成功后应能确认注册态确实变更；策略侧 `_registered_symbols` 缓存必须与真实注册态保持同步（注销失败时不得误判为已注销）。
- **P0-1-F5 停止监控与保留数据解耦**：把"停止对新币的检测/新采集任务创建"（检测层）与"注销 K 线注册"（数据层）分开：
  - 未持仓且已"过龄"的标的：可按原逻辑注销；
  - 有持仓的标的：即便停止新币检测，也必须保留 K 线注册（或改由"持仓驱动"的注册保活机制）。
- **P0-1-F6 伪代码级状态流转**：
  ```
  # 入场成功后
  known_symbols.add(symbol); save_known_symbols()      # 停止新币检测（保留）
  if not has_open_position(symbol):                    # 仅无持仓才注销
      unregister_symbol(symbol)

  # 取 K 线前（补全/指标路径）
  if not is_registered_active(symbol):
      ok, real_status = register_symbol(symbol, intervals=[interval])
      if ok and real_status == 'active':
          mark_registered(symbol)
      else:
          log + 告警; return FAIL   # 不进入 ATR 计算
  ```

### 7.4 验收标准

- **P0-1-AC1（复现场景）**：对某标的存在未平仓持仓时，入场成功后**不再**调用 `unregister_symbol`；该标的注册态保持/恢复 `active`；kline 查询返回 200。
- **P0-1-AC2**：标的注册态为 `cancelled` 且本策略有持仓 → 取 K 线前触发 ensure-active，注册成功后注册态变 `active`，随后 ATR 计算成功。
- **P0-1-AC3**：对已存在且 `status='cancelled'` 的行重新注册 → 落库 `status='active'`，接口返回体 `data.status == 'active'`（对 `expired` 同理）。
- **P0-1-AC4**：无持仓且过龄的标的，仍按原逻辑注销（不回归）。
- **P0-1-AC5**：ensure-active 失败（注册接口报错/返回非 active）时，代码不进入 ATR 计算，不撤旧保护单（与 P0-3 协同），并告警。
- **P0-1-AC6**：`_registered_symbols` 缓存与真实注册态一致：注销成功后移除、注销失败时保留（不误判）。

### 7.5 边界与异常场景

- 进程重启后 `_registered_symbols` 为空但 DB 中仍有 active 行：以注册表真实状态为准，不因缓存空而重复注册或误注销。
- 注册表缓存（`SymbolRegistry._cache` 单例，仅启动加载一次 `status='active'` 行）与 DB 可能不一致：修复须以 DB 真实行为准，不得仅信任内存缓存（评估是否需要失效/重载机制，见开放问题 Q3）。
- 同一 symbol 同时被其它策略注册：注销/注销判定只针对"本策略持仓"，不得误伤他策略（沿用跨策略现状，勿扩大范围）。
- 注册/注销接口超时：注册（幂等，可重试）与注销（幂等，对 `-2011` 类可忽略）按各自语义处理；策略侧不得因接口抖动而误判状态。

### 7.6 配置化要求

| 配置项 | 建议默认值 | 位置 | 说明 |
|--------|-----------|------|------|
| `kline.keep_registration_when_position_open`（**新增**） | `true` | `strategies/new_coin/config.yaml`（`kline` 段） | 有持仓时是否保留 K 线注册 |
| `kline.ensure_active_before_use`（**新增**） | `true` | 同上 | 取 K 线前是否 ensure-active |
| `kline.ensure_active_retries`（**新增**） | `2` | 同上 | ensure-active 重试次数 |
| `kline.ensure_active_retry_interval`（**新增**） | `2`（秒） | 同上 | 重试间隔 |
| `kline.interval` | 现有 `'1h'` | 同上 | 沿用 |

> 禁止在代码中写死上述数值；`mctps_symbols`（`executor.py:3236`）等硬编码集合须一并评估提取（见 §12）。

---

## 8. P0-2（服务侧）：白名单放行"数据表已存在且格式合法"的标的

### 8.1 问题现状

`table_name_guard.build_kline_table_name()`（`:122-141`）三层校验：格式层 → 白名单层 → 表名层。白名单层（`_collect_whitelist`，`:76-88`）仅含 `FIXED_SYMBOLS ∪ settings.SYMBOLS ∪ 注册表 active`。因此"数据表已存在但注册态非 active"的标的（如已注销的 ACNUSDT/USDBRLUSDT）在**有数据**的情况下仍被 400 拒绝。

### 8.2 修复目标

在**不放宽注入防护**的前提下，让"格式合法且 `kline_{symbol}_{interval}` 数据表真实存在"的标的可以通过校验；白名单与"表存在"是**或**关系。

### 8.3 功能需求

- **P0-2-F1 放行条件**：允许读取的条件 = `symbol/interval 格式合法` **且**（`命中现有白名单` **或** `数据表 kline_{symbol}_{interval} 真实存在`）。
- **P0-2-F2 保持 fail-closed**：
  - 表名仍必须整体匹配配置的 `TABLE_NAME_PATTERN`；不匹配一律拒绝。
  - `symbol` 仍必须匹配 `SYMBOL_FORMAT_PATTERN`（`^[A-Z0-9]{3,20}$`）。
  - **存在性查询必须参数化**（如 `information_schema.tables WHERE table_name = :table_name`），**禁止**把 symbol/interval 拼进 SQL。
  - 校验层任何异常（DB 不可用等）→ 保守拒绝，不得放行任意输入。
- **P0-2-F3 interval 处理**：当走"表存在"放行路径时，`interval` 由"表名整体匹配严格正则"隐含约束（`^kline_[a-z0-9]{3,20}_([0-9]+[mhdw]|1M)$`）；不得因放行而跳过 interval 的合法性。
- **P0-2-F4 单一入口**：`build_kline_table_name` 仍是"以 symbol/interval 构造表名"的唯一入口（routes / registry_routes 共用）。"表存在"这一信息需由调用方（具备 DB 连接的路由层）**注入**给校验函数，或由校验函数接受一个"存在性判定回调"；禁止在路由层另写一套校验。
- **P0-2-F5 放行范围**：仅作用于**读取/查询**路径（`/klines/latest`、`/indicators`）。注册/建表/采集路径的白名单语义保持不变（新标的仍须先注册）。
- **P0-2-F6 可观测性**：走"表存在放行"时记 INFO（含 symbol/interval/来源），便于审计"为何某未注册标的被放行"。

### 8.4 验收标准

- **P0-2-AC1（复现场景）**：`kline_usdbrlusdt_1h` 存在且 USDBRLUSDT 注册态为 cancelled → 请求 `/api/v1/klines/latest?symbol=USDBRLUSDT&interval=1h` 返回 **200 且带数据**（修复前为 400）。
- **P0-2-AC2**：`symbol` 格式非法（含分号/引号/空格/CROSS JOIN）→ 4xx，**不执行任何 SQL**（断言 `fetch_all`/存在性查询未被调用）。
- **P0-2-AC3**：`symbol` 格式合法但**白名单未命中且数据表不存在** → 仍 4xx（不放行），且不执行该表 SELECT。
- **P0-2-AC4**：`BTCUSDT`/`1h` 等合法请求行为与修复前完全一致（不回归）。
- **P0-2-AC5**：DB 不可用/存在性查询异常 → fail-closed 拒绝（4xx/500），不放行。
- **P0-2-AC6**：表名正则单测：`kline_btcusdt_1h`、`kline_ethusdt_15m`、`kline_solusdt_1d` 通过；注入样例（含空格/分号/引号/CROSS JOIN/大写可疑片段）全部拒绝。
- **P0-2-AC7**：`/indicators` 的放行/拒绝行为与 `/klines/latest` 一致。

### 8.5 边界与异常场景

- 大小写：`symbol` 统一大写比对，表名小写生成；`interval` 严格精确匹配。
- 并发：存在性校验不得引入跨请求共享可变状态竞态；如对"表存在"结果做缓存，须有明确的失效口径（**见开放问题 Q4**）。
- `information_schema` 权限：运行账号需能读取 `information_schema.tables`（现 `_table_exists` 已在用该查询，权限沿用）。
- 与 P0-1 的协同：P0-1 负责"不该注销"，P0-2 负责"已注销也能读"，两者共同覆盖"历史已裸奔持仓"。

### 8.6 配置化要求

| 配置项 | 建议默认值 | 位置 | 说明 |
|--------|-----------|------|------|
| `TABLE_NAME_PATTERN` | 现有 `^kline_[a-z0-9]{3,20}_([0-9]+[mhdw]|1M)$` | `services/kline_service/shared/core/config.py`（已有） | 沿用，不得写死到路由 |
| `SYMBOL_FORMAT_PATTERN` | 现有 `^[A-Z0-9]{3,20}$`（已有） | 同上 | 沿用 |
| `ALLOW_EXISTING_TABLE_SYMBOLS`（**新增**） | `true` | 同上 | 是否启用"表存在即放行" |
| `EXISTING_TABLE_CHECK_CACHE_TTL_SECONDS`（**新增**，可选） | `0` | 同上 | 存在性结果缓存 TTL（若做缓存）；`0` = 实时查询不缓存 |

> 路由/校验层不得出现任何字面量正则或币种白名单；一律从配置读取。
>
> 注：`EXISTING_TABLE_CHECK_CACHE_TTL_SECONDS` 本表按「实时查询不缓存」定为 `0`，以架构文档 §4.3 与实现（`services/kline_service/shared/core/config.py`）为准。

---

## 9. P0-3（策略侧）：补全流程改为"先算 ATR 再撤旧单"

### 9.1 问题现状

`strategies/new_coin/executor.py::replenish_conditional_orders()`：
- `:3253` 先 `await self.cancel_all_algo_orders(symbol)`（撤掉该标的旧条件单）；
- `:3276` 才 `atr = await self._calculate_atr(symbol)`；
- `:3277-3281` `if atr <= 0: logger.warning("ATR计算失败，跳过补全条件单"); return False` —— **直接返回，不挂新单**。

即"旧保护单被撤 → ATR 失败 → 新保护单没挂上 → 持仓裸奔"，且 `return False` 使 `_replenish_done` 永不置位（`strategy.py:102-103`、`:492-570`），每周期重复。

### 9.2 修复目标

补全流程的任何失败路径，都不得产生"旧保护单已撤、新保护单未挂"的中间态。**只在确认新保护单所需的一切前提就绪后，才允许撤旧单。**

### 9.3 功能需求

- **P0-3-F1 只读前置先做**：撤旧单之前，先完成所有只读准备：取真实持仓量、计算 ATR、取精度（tick/step）、取当前价、计算 SL/TP1/TP2 价格与数量。
- **P0-3-F2 前提不足即中止，不撤单**：任一前置不满足（无持仓视为"无需补全"另论；ATR≤0、精度获取失败、价格无效等）→ **不撤旧单**，直接返回失败（保留现有保护单），并告警、下周期重试。
- **P0-3-F3 撤单失败不叠单**：撤旧单失败时，不得继续挂新单（避免新旧叠加或数量错配），保守返回失败并告警。
- **P0-3-F4 撤单后失败需可恢复**：撤旧单成功、但后续挂新单部分失败 → 必须记录缺口并进入下周期补全（沿用 `_replenished_symbols` 不置位机制），且告警可见。
- **P0-3-F5 幂等与重复可控**：连续周期调用不得产生重复超额条件单；`_replenished_symbols` 与 `_replenish_done` 语义保持（成功才置位）。
- **P0-3-F6 伪代码级状态流转**：
  ```
  # 1) 只读准备
  position = read_exchange_position(symbol)      # 无空头持仓 → return 无需补全
  atr = calc_atr(symbol); if atr <= 0: return FAIL     # 不撤单
  tick, step = get_precision(symbol)
  price = get_current_price(symbol)
  plans = build_sl_tp_plans(position, atr, tick, step, price)   # 全部算好

  # 2) 确认可挂后再撤旧单
  if any(plan invalid): return FAIL              # 不撤单
  canc = cancel_all_algo_orders(symbol); if canc.failed: return FAIL   # 撤失败不继续挂

  # 3) 挂新单（失败记录缺口，下周期补全）
  results = place(sl, tp1, tp2)
  return all(results)
  ```

### 9.4 验收标准

- **P0-3-AC1（复现场景）**：模拟 `_calculate_atr` 返回 0（如 kline 400）→ `cancel_all_algo_orders` **不被调用**（旧保护单保留）；函数返回 `False`；发送告警。
- **P0-3-AC2**：ATR 正常 → 先算完所有价格/数量 → 才调用 `cancel_all_algo_orders` → 挂新单成功 → 返回 `True`（不回归）。
- **P0-3-AC3**：撤旧单失败（返回 `failed>0` 或抛异常）→ 不挂新单，返回失败，告警。
- **P0-3-AC4**：撤旧单成功但 SL 成功、TP1 失败 → 返回 `False`，`_replenished_symbols` 不置位，下周期重试（缺口可见）。
- **P0-3-AC5**：无空头持仓 → 不撤单、不挂单，返回"无需补全"（不得因无持仓误撤他人保护单）。
- **P0-3-AC6**：连续两个周期调用，第二个周期若已全部成功则不再撤/挂（幂等）。

### 9.5 边界与异常场景

- `_calculate_atr` 内部依赖 `kline_service.get_klines`（无 Binance 兜底）——ATR 失败即中止，不撤单（与 P0-3-F2 一致）。是否给 ATR 加"交易所 K 线兜底"属可选增强（**见开放问题 Q5**）。
- 撤旧单与挂新单之间进程崩溃：重启后由补全流程 + `_replenish_done` 收敛；评估现有启动恢复路径是否覆盖（沿用现有机制）。
- PM 条件单 `-2011`/`-2013`/`-4164` 等幂等错误码：`replenish` 现有 `ignore_error_codes` 语义保持，但须提取为配置（见 §12）。
- 数据库不可用不阻塞补全（补全以交易所为准）。

### 9.6 配置化要求

| 配置项 | 建议默认值 | 位置 | 说明 |
|--------|-----------|------|------|
| `trading.replenish.cancel_after_ready`（**新增**） | `true` | `strategies/new_coin/config.yaml` | 是否"先算 ATR 再撤单"（总开关，便于回退） |
| `trading.replenish.ignore_error_codes`（**新增**） | `['-4164','-2011','-2021','-4136','-4507']` | 同上 | 幂等忽略错误码（替换现有硬编码集合） |
| `trading.min_notional`（**新增**） | `5` | 同上 | 替换 `executor.py:72` 的 `_MIN_NOTIONAL = Decimal('5')` |
| `trading.protected_symbols`（**新增/提取**） | `['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','TRXUSDT']` | 同上 | 替换 `executor.py:3236` 的 `mctps_symbols` 硬编码 |

> 现有 `kline.atr_period`、`stop_loss_percent`、`emergency_stop`、`atr_stop_multiplier` 等沿用，不得新增硬编码。

---

## 10. 验收命令清单（可执行）

### 10.1 本地（mock/单测，禁止服务器回测）

```bash
# P0-3：先算 ATR 再撤单——ATR 失败不得撤单
pytest -q tests/ strategies/new_coin/tests/ -k "replenish or P0_3 or atr"

# P0-1：有持仓不注销 / ensure-active
pytest -q strategies/new_coin/tests/ -k "registration or ensure_active or P0_1"

# P0-2：白名单 + 表存在放行 + 注入拒绝（假连接断言未执行 SQL）
pytest -q services/kline_service/tests/ -k "table_name or whitelist or P0_2"

# 全量回归
pytest -q
```

### 10.2 服务器只读核对（修复部署后）

```bash
# 1) 两个目标标的的 K 线查询应从 400 变为 200 且有数据（P0-2）
ssh root@43.156.242.184 "docker exec trading_system-kline sh -c \"curl -s -o /dev/null -w '%{http_code}\\n' 'http://127.0.0.1:8000/api/v1/klines/latest?symbol=USDBRLUSDT&interval=1h&limit=18'\""
ssh root@43.156.242.184 "docker exec trading_system-kline sh -c \"curl -s -o /dev/null -w '%{http_code}\\n' 'http://127.0.0.1:8000/api/v1/klines/latest?symbol=ACNUSDT&interval=1h&limit=18'\""
# 期望：两条均 200

# 2) 注入用例必须被拒（P0-2-F2）
ssh root@43.156.242.184 "docker exec trading_system-kline sh -c \"curl -s -o /dev/null -w '%{http_code}\\n' \\\"http://127.0.0.1:8000/api/v1/klines/latest?symbol=BTCUSDT';DROP%20TABLE%20x;--&interval=1h\\\"\""
# 期望：4xx

# 3) 注册表状态核对（P0-1）
ssh root@43.156.242.184 "docker exec trading_system-postgres psql -U trading_user -d trading_platform -c \"SELECT symbol,status,registered_at,expires_at FROM registered_symbols WHERE symbol IN ('ACNUSDT','USDBRLUSDT');\""
# 期望：有持仓的标的 status=active（或经 ensure-active 后为 active）

# 4) new_coin 日志：不再出现"K线服务请求失败: 400"与"ATR计算失败，跳过补全条件单"（P0-3/P0-1）
ssh root@43.156.242.184 "docker logs --since 30m trading_system-new_coin 2>&1 | grep -E 'K线服务请求失败: 400|ATR计算失败，跳过补全|补全条件单' | tail -20"
# 期望：无 400/ATR 失败；出现"条件单全部补全完成"

# 5) 交易所侧确认两笔持仓已挂齐保护单（P0 资金风险解除）
ssh root@43.156.242.184 "docker logs --since 30m trading_system-new_coin 2>&1 | grep -E '补全止损条件单成功|补全 TP1|补全 TP2|条件单全部补全完成' | tail -20"
# 期望：USDBRLUSDT、ACNUSDT 均出现 SL/TP 补全成功
```

### 10.3 部署（自动化）

- push main → GitHub Actions 自动构建 + GHCR + SSH 部署（见 `.trae/rules/deployment.md`）。
- 变更面：`services/kline_service/*` → 重建 kline-service；`strategies/new_coin/*` → 重建 new-coin；若抽 `shared/*` → 全量重建。
- 部署后按 §10.2 核对；核对不过视为部署失败。

---

## 11. 影响面评估

### 11.1 白名单 400 的波及范围

| 策略 | 是否可能命中同一 400 | 依据 |
|------|--------------------|------|
| new_coin | **是（已发生）** | 持有已注销标的，ATR 无兜底（`executor.py:1628`） |
| btc_eth | 否 | 标的全在 `FIXED_SYMBOLS ∪ settings.SYMBOLS`（BTC/ETH/BNB/SOL/XRP/TRX），恒白名单 |
| btc_eth_aggressive | 否 | 同上 |
| grid | 否 | 主用 ETHUSDT（`settings.SYMBOLS`）；其余取 K 线亦为 BTC/ETH，恒白名单 |
| hrs | **需评估** | hrs 会动态注册候选币；但其注销条件为「不在候选池 + **无持仓** + 不在黑名单」（`hrs/strategy.py:1327-1343`），**且** `market_data.get_klines` 失败会**回退币安 API**（`hrs/market_data.py:400-415`），故风险显著低于 new_coin；仍建议按 `_should_unregister` 逐条复核"持仓中注销"是否被完全排除 |

> 关键差异：new_coin 与 hrs 的对比说明——**new_coin 缺两样东西**：(a) HRS 那样的"有持仓不注销"保护；(b) HRS 那样的"kline 失败回退币安 API"兜底。这也是 P0-1（补 a）与可选增强（补 b，见 Q5）的由来。

### 11.2 历史数据面

- `registered_symbols` 中已有大量 `cancelled`/`expired` 历史行；new_coin 的持仓标的（如 AMCUSDT、APLDUSDT、CVNAUSDT、PATHUSDT 等）需逐一核对注册态，凡"有持仓且非 active"者均属本次 P0 受影响对象。
- P0-2 上线后，这类"历史已注销但仍有持仓"的标的可立即恢复 K 线读取（前提：数据表存在），为 P0-1 修复前的存量持仓兜底。

### 11.3 部署影响

- P0-2 仅重建 kline-service；P0-1、P0-3 仅重建 new-coin。向后兼容：合法请求行为不变；非法请求仍拒绝。
- **注意**：kline-service 重建会短暂中断 K 线服务，所有策略的 K 线读取在重建窗口内可能失败——建议与 new-coin 保持一致的 P0-3 止血先行，再单独滚动 kline-service。

---

## 12. 约束落地：重复代码与硬编码

### 12.1 是否需要抽 shared（评估）

- 现状：保护单补全逻辑存在三处形态——`shared/protection_retry.py`（btc_eth / 激进版共用）、`strategies/new_coin/executor.py::replenish_conditional_orders`、`strategies/hrs/executor.py::replenish_position_orders`。
- 判断：若本轮**只修 new_coin 一处**（最小止血），可不抽；但"先算后撤"的顺序约束在 hrs 等补全路径同样适用。**若确认 ≥3 处同构需一并修正，则必须抽公共模块**（项目已有 `shared/protection_retry.py` 作为单点基础，建议把"补全前置校验 + 先算后撤"下沉到该模块），代价是触发**全量重建**。
- 该取舍见开放问题 Q2，由架构环节拍板。

### 12.2 本轮顺带修正的硬编码（均在 new_coin）

| 位置 | 硬编码 | 建议 |
|------|--------|------|
| `strategies/new_coin/executor.py:72` | `_MIN_NOTIONAL = Decimal('5')` | 提取为 `trading.min_notional` |
| `strategies/new_coin/executor.py:66` | `_TP2_IGNORE_ERROR_CODES = ['-4164','-2011','-2021']` | 提取为配置 |
| `strategies/new_coin/executor.py:3236` | `mctps_symbols = {...}` 写死 6 币种 | 提取为 `trading.protected_symbols` |
| `strategies/new_coin/executor.py:3307` | 局部 `ignore_error_codes` 字面量 | 与上述统一走配置 |

---

## 13. 风险与回滚

| 编号 | 风险 | 缓解 | 回滚 |
|------|------|------|------|
| P0-2 | "表存在即放行"扩大读取面，可能被探测 | 仅放行"格式合法 + 表真实存在"；表名严格正则 + 参数化查询不变；仅读路径生效；记 INFO 审计 | revert commit（仅 kline-service 重建） |
| P0-2 | 存在性查询增加 DB 负载 | 可选 TTL 缓存（配置化）；`information_schema` 查询轻量 | 关闭 `ALLOW_EXISTING_TABLE_SYMBOLS` |
| P0-1 | 有持仓不注销导致采集任务/资源长期保留 | 仅在"有未平仓持仓"期间保活；平仓后按原逻辑回收 | 关闭 `kline.keep_registration_when_position_open` |
| P0-1 | ensure-active 抖动导致误判 | 重试次数/间隔配置化；失败即不计算 ATR（保守），不撤旧单 | 关闭 `kline.ensure_active_before_use` |
| P0-3 | "先算后撤"后，若撤单与新单之间失败 → 仍可能短暂无保护 | 撤单成功即连续挂新单；挂单失败记录缺口下周期收敛；告警 | 关闭 `trading.replenish.cancel_after_ready`（回到旧顺序，不推荐） |

**紧急止血（P0-3 上线前可用，手工、只读优先）**：对 USDBRLUSDT、ACNUSDT 两笔持仓，可在不重启容器的前提下，由 P0-2 修复后让补齐流程自然成功；或人工核对交易所侧并补挂保护单（属运维，需用户明确授权后执行）。

---

## 14. 开放问题（需人工拍板）

**Q1（P0-1）"停止新币监控"与"保留 K 线注册"如何解耦**：是"有持仓即不注销"，还是"注销后由持仓保活任务重新注册"？前者改动小、语义清晰；后者更彻底但引入新任务。需架构/负责人拍板。

**Q2（全局）是否抽 `shared/` 公共补全模块**：只修 new_coin（不触发全量重建）还是抽 shared 一次到位（触发全量重建、停机面大）？需架构 + 负责人拍板。

**Q3（P0-1）注册表缓存一致性**：`SymbolRegistry._cache` 单例仅启动加载一次 active 行，长期运行会与 DB 漂移（`unregister`/`cleanup` 会改内存）。是否本轮顺带引入缓存失效/按需重载？需架构拍板。

**Q4（P0-2）"表存在"结果是否缓存**：每次请求实时查 `information_schema` vs TTL 缓存。实时查询最准确、开销可控；缓存需定失效口径。需架构拍板。

**Q5（P0-3）ATR 是否加交易所 K 线兜底**：new_coin 的 `_calculate_atr` 仅走 kline 服务、无兜底；hrs 有币安兜底。是否给 new_coin 也加兜底（彻底消除"kline 不可用即无法补全"）？需交易负责人拍板。

**Q6（验收）存量持仓处置**：USDBRLUSDT、ACNUSDT 两笔当前裸奔持仓，是"等修复部署后自动补齐"还是"立即人工补挂"？需用户拍板（涉及真实资金安全，建议优先人工确认）。

---

## 15. 核实记录：实际读过的文件与关键行号

| 文件 | 关键行号 | 用途 |
|------|---------|------|
| `strategies/new_coin/strategy.py` | `:96-103`（`_registered_symbols` / `_replenish_done`）、`:492-576`（补全触发循环）、**`:776-784`（入场后注销——根因 R-A）** | 触发与根因定位 |
| `strategies/new_coin/executor.py` | `:1601-1664`（`_calculate_atr`，仅走 kline 无兜底）、`:3215-3530`（`replenish_conditional_orders`）、`:3251-3255`（先撤单）、`:3275-3281`（后算 ATR，失败即 return）、`:3307`/`:66`/`:72`/`:3236`（硬编码） | 顺序缺陷与硬编码 |
| `services/kline_service/core/table_name_guard.py` | `:76-88`（`_collect_whitelist`）、`:122-141`（`build_kline_table_name` 三层校验） | 白名单口径 |
| `services/kline_service/api/routes.py` | `:41-62`（`_table_exists`/`_validated_table_name`）、`:105-118`、`:191-204`（校验入口） | 唯一校验入口 |
| `services/kline_service/api/registry_routes.py` | `:21`（复用 `_validated_table_name`）、`:160-248`（注册路由）、`:183-196`（新增/更新判定）、`:251-306`（注销路由） | 注册/注销入口，确认无第二白名单 |
| `services/kline_service/core/registry.py` | `:76-115`（`register`）、`:117-143`（`_update_registration`，不改 status）、`:145-162`（`_reactivate_registration`，置 active）、`:286-340`（`_save_to_database`，UPDATE 含 status） | 证明"注册写入无缺陷" |
| `services/kline_service/db/migrations/create_registered_symbols_table.py` | `:9-23` | 表结构（`status` 默认 active，无 `is_active` 列） |
| `services/kline_service/shared/core/config.py` | `:38-57`（SYMBOLS/COLLECT_INTERVALS/TABLE_NAME_PATTERN/SYMBOL_FORMAT_PATTERN/FIXED_SYMBOLS） | 白名单来源与配置 |
| `strategies/new_coin/config.yaml` | `:279-295`（detector/kline 段） | 配置落点 |
| `shared/kline_service.py` | `:63-164`（`get_klines`，端点 `{service_url}/klines/latest`）、`:166-214`（`register_symbol`） | 客户端行为 |
| `shared/protection_retry.py` | `:46-95`、`:317-433` | 既有保护单/补挂单点实现 |
| `strategies/hrs/strategy.py` | `:1327-1359`（注销条件"无持仓"）、`:2694-2710`（重新注册） | 对照：hrs 的保护 |
| `strategies/hrs/market_data.py` | `:388-418`（kline 失败回退币安 API） | 对照：hrs 的兜底 |

### 15.1 额外发现（需求方描述之外）

1. **★根因修正**：`status=cancelled` 不是"注册写入缺陷"，而是 new_coin **入场成功后主动注销持仓标的**（`strategy.py:776-784`）——见 §3.2。这是本事件最关键的更正。
2. **无第二白名单入口**：白名单仅 `table_name_guard` 一处实现；`registry_routes.py:21` 复用 `routes._validated_table_name`，未另起一套（验证了"唯一入口"）。
3. **new_coin 的 ATR 无兜底**：`_calculate_atr`（`executor.py:1601-1664`）在 kline 失败时直接 `return Decimal('0')`，而 hrs 有币安兜底——这是 new_coin 比 hrs 更脆弱的直接原因。
4. **注册表缓存漂移**：`SymbolRegistry._cache` 仅启动加载一次；`_update_registration` 不改 status、`_save_to_database` 不改 `registered_at`，导致"内存状态/DB 状态/本地字段"三者可能不一致（见 Q3）。
5. **补全流程的其它硬编码**：`_MIN_NOTIONAL=5`、`mctps_symbols`、`ignore_error_codes`（§12.2），属既有硬编码违规，本轮应顺带提取。
6. **`_replenished_symbols` 与 `_replenish_done` 双重标志**：`executor` 有 `_replenished_symbols`（`:209`），`strategy` 有 `_replenish_done`（`:103`）；失败时两者都不置位，形成"永久重试"，这也是"每周期重复"的结构原因；修复 P0-3 后仍需确认收敛路径不会造成无限重试。

---

（文档结束）
