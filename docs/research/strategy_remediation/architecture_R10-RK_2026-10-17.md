# 架构/设计定稿：R10 / R12 + RK1 / R11 / RK2（2026-10-17）

> 状态：**已落地（2026-10-17 收口）**。本文原为「设计定稿 + 实现交接清单 + 先红后绿回归用例 + 能力限制披露」阶段产物；收口补记只如实记录代码实际实现与本稿的偏差（§1.5 / §7.1 / §8），**不宣称部署完成、不连生产库**。逐条闭环状态见 [requirements_R10-RK §7](requirements_R10-RK_2026-10-17.md)。
> 权威输入：
> - 需求与验收：[requirements_R10-RK_2026-10-17.md](requirements_R10-RK_2026-10-17.md)
> - 第三轮复审报告：[strategy_reaudit_2026-10-17/README.md](../../reviews/strategy_reaudit_2026-10-17/README.md)（只读，禁改）
> - 业务决策：[decisions.md](decisions.md)
> - 上轮架构（R7-R9）：[architecture_R7-R9_2026-10-16.md](architecture_R7-R9_2026-10-16.md)

---

## 0. 结论概览（一句话）

| 编号 | 分组 | 动作定性 | 涉及核心文件 |
|---|---|---|---|
| R10 | execution_facts（P1） | 修代码：`_resolve_page_outcome` 收尾改为「原始满页」判定 + 主循环去时间墙 | `scripts/export_execution_facts.py` |
| R12 | income（P2） | 修代码：income 无 ID 游标，满页到右边界标 `incomplete`（复用 R10 收尾）+ 游标不越界 | 同上 |
| RK1 | HRS（P2） | 修代码：三处失败路径升级为可识别告警（复用 `shared/notification.py`），DDL 迁移路径确认 | `strategies/hrs/strategy.py` |
| R11 | HRS 评分（P2） | **改测试+文档，不改生产代码**（已批准行为变化 V2.10） | `strategies/hrs/tests/test_scoring_engine.py` + 相关文档注释 |
| RK2 | HRS 统计（P2） | 零读取方确认 + 口径定义文档化 + 固定样本离线验证用例 | 新增测试文件 + 文档 |

**执行顺序**（承需求 §3）：R10 → R12 → R11 → RK1 → RK2；R8/R9 既有回归保持绿，任一红即阻断该组先行。

---

## 1. R10 分页契约定稿（闭区间 + 三情形收尾）

### 1.1 契约声明（全文唯一口径，闭区间）

- **请求窗口**：`[window_start_ms, window_end_ms]`（币安 PM UM `userTrades.endTime` 为**包含边界**）。
- **客户端过滤**：`time_ms <= window_end_ms`（保留等号）。
- **游标收尾**：完成 = 「**原始页**返回条数 `< page_limit`」，与 `time_ms` 是否触到右边界**无关**。
- **满页**（原始页 `>= page_limit`）时，**无论** `max_time` 是否 `>= window_end_ms`，都**尚未完成**，必须续传（fills 走 `fromId`）直到某页 `< page_limit`。

> 核心结论：`time_ms >= window_end_ms` **不再是**「完成」的充分条件。它只意味着「到达右边界」，不意味着「右边界内的记录已全部取回」。

### 1.2 缺陷主体与重写目标

现缺陷（[export_execution_facts.py#L908-L912](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L908-L912)）：

```python
ok_or_window_end = (
    page_complete                      # 过滤后条数 < limit
    or new_advance.time_ms >= window_end_ms   # ← 缺陷：满页触界即误判完成
    or new_advance.time_ms <= 0
)
```

关键问题与「过滤后 < limit」有关：`_one_page_iteration` 现把 `_filter_rows_within_window` **过滤后**的 `rows` 交给 `_resolve_page_outcome`，而续传页（fills `fromId`）不带 `endTime` 会夹带越界记录；若用「过滤后条数」判满页，会因越界记录被过滤而**误判排空**。因此判定必须基于**原始打印**条数，并单独保留「是否遇到越界记录」。

### 1.3 三情形收尾（重写 `_resolve_page_outcome`）

新增入参（`_one_page_iteration` 计算后透传）：

| 入参 | 类型 | 含义 |
|---|---|---|
| `raw_page_full` | `bool` | `len(raw_rows) >= page_limit`（**过滤前**原始页是否满） |
| `saw_out_of_window` | `bool` | 本页是否出现 `time_ms > window_end_ms` 被过滤的越界记录 |
| `supports_id_cursor` | `bool` | fills=`True`（有 `fromId`），income=`False`（无 ID 游标） |

判定（≤ 50 行，抽公共逻辑）：

```python
def _resolve_page_outcome(self, *, rows, raw_page_full, saw_out_of_window,
                          supports_id_cursor, source, ..., window_end_ms, ...):
    new_items, new_advance = self._consume_page(rows, ...)
    collected.extend(new_items)

    # 1）不可推进检测（fills=ID卡住；income=满页到右边界或同毫秒卡住）
    if self._detect_unstoppable(
        raw_page_full, new_advance, loop_advance, supports_id_cursor, window_end_ms,
    ):
        return self._build_unstoppable_result(..., saw_out_of_window=saw_out_of_window)

    # 2）三情形收尾
    if not raw_page_full:
        # (a) 原始页未满 → 记录已排空（闭区间内同毫秒零遗漏）
        return self._build_terminal_or_partial_result(
            ..., page_complete=True, saw_out_of_window=saw_out_of_window)

    # (b) 满页 → 未完成，继续续传（fills fromId / income 时间推进）
    return self._build_terminal_or_partial_result(
        ..., page_complete=False, saw_out_of_window=saw_out_of_window)
```

`_detect_unstoppable` 重写（替换现 L750-L760 三连检测）：

```python
@staticmethod
def _detect_unstoppable(raw_page_full, new_advance, loop_advance,
                        supports_id_cursor, window_end_ms):
    if not raw_page_full:
        return False
    if supports_id_cursor:
        # fills：有 fromId，只有「ID 卡住」才不可续传（时间卡住不阻塞）
        return new_advance.event_id == loop_advance.event_id
    # income：靠时间推进；到达右边界或同毫秒时间卡住 → 无法再细分
    return (new_advance.time_ms >= window_end_ms
            or new_advance.time_ms == loop_advance.time_ms)
```

### 1.4 积分点：主循环去「时间墙」

现主循环（[export_execution_facts.py#L558](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L558)）：

```python
while loop_advance.time_ms < window_end_ms and page_count < self.max_pages:
```

问题：fills 满页触界（`max_time == window_end_ms`）后，`loop_advance.time_ms < window_end_ms` 为假，循环提前退出，**无法再 `fromId` 续取同毫秒剩余记录**。必须去掉时间墙，仅保留页数守卫，由 `_resolve_page_outcome` 的 `final_cursor` 触发 `break`：

```python
while page_count < self.max_pages:
    page_count += 1
    done = await self._one_page_iteration(...)
    loop_advance = done.next_advance
    cursor, should_break = self._resolve_iteration_dispatch(done, errors)
    if should_break:
        break
else:
    # max_pages 强制退出（守卫不变）
    ...
```

安全性说明：
- 无限循环由 `max_pages` + `_detect_unstoppable`（fills ID 卡住 / income 到界）双重兜底。
- fills 续传页返回空 `[]` → `raw_page_full=False` → 完成 → `break`，不会空转死循环。
- fills 边界后仍有海量越界记录时会一路续传到 `id` 耗尽或 max_pages，这正是「确认排空」的正确代价，且窗口右边界通常贴近「现在」，越界量有限。

### 1.5 返回值结构（审计信息）

> **落地偏差（2026-10-17 收口）**：原设计拟在 `CursorRecord` **新增 `saw_out_of_window` 字段**（入库供审计）。**代码实际未采用**——为避免引入**死字段**（违反「禁止幽灵参数/死字段」规约），`saw_out_of_window` **收敛为 `_one_page_iteration` 内的局部变量**（[export_execution_facts.py L789](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L789)），`_resolve_page_outcome` 在单次迭代内即消费该布尔（判定「原始页满且过滤后为空」情形），**无需入库**。`CursorRecord` 字段表（[L113-132](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L113-L132)）**不含**该字段；`_make_cursor` 亦无对应参数。

原设计意图（保留审计信息）改由「局部变量 + 迭代内判定」满足：完成 / partial / incomplete 三态的收尾判定均正确使用 `saw_out_of_window`，只是不再落库。

---

## 2. 统一时间边界口径（五处改动点）

| # | 位置 | 现状 | 改动 |
|---|---|---|---|
| 1 | 请求参数（fills `api_call_factory`，[L447-L448](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L447-L448)） | `end_time=window_end_ms if fid is None else None` | **不变**（已是闭区间 + 续传无时间参数，与币安契约一致） |
| 2 | 请求参数（income `api_call_factory`，[L489-L492](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L489-L492)） | `start_time=cs, end_time=window_end_ms` | **不变**（income 无 ID 游标，时间推进模型保留；缺口转收尾判定处理） |
| 3 | 客户端过滤（`_filter_rows_within_window`，[L926-L943](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L926-L943)） | `t <= window_end_ms` | **不变**（已是闭区间） |
| 4 | 游标收尾（`_resolve_page_outcome` + `_detect_unstoppable`） | `max_time >= window_end` 当完成 | **按 §1.3 重写**（完成只看「原始页 < limit」；income 满页到界标 incomplete） |
| 5 | 测试 Fake 时间过滤 | 用 `< end_time` | **收紧为 `<= end_time`**，与客户端/币安闭区间一致 |

> 判定门禁（评审用）：「请求 / 过滤 / 游标 / Fake」四处同一闭区间判据；混用判不达标。填完第 4、5 处后，第 1-3 处无需改动，但需用一条一致性回归锁住。

---

## 3. R12 income 缺口标记设计

### 3.1 接口能力结论（事实依据，不凭猜测）

依据第一轮核查事实与需求 §2 R12 已确证结论：`um/income` **无 `fromId`/`tranId` 类事件游标**；官方文档虽列 `page`，但**语义未定义、不可依赖**。因此同毫秒超过 `limit` 的收支流水**无法用事件 ID 续取**。→ 选择**持续标记缺口**方案（保守路径），不实现续取。

### 3.2 「无法续采」的显式标记（复用 R10 收尾结果）

R10 修复后，`_resolve_page_outcome` 对 income（`supports_id_cursor=False`）自动获得正确行为：

- **未满页**（`raw_page_full=False`）→ `page_complete=True`、`request_status='ok'`（正常收尾）。
- **满页 + `max_time < window_end`** → `page_complete=False`、`request_status='partial'`，时间推进翻页（`tran_id` 去重保证不重复入账）。
- **满页 + `max_time >= window_end`**（或同毫秒时间卡住）→ `_detect_unstoppable` 命中 → `_build_unstoppable_result` 产出 `page_complete=False`、`request_status='incomplete'`，`error_detail` 携带缺口毫秒与原因。

`_build_unstoppable_result` 的缺口原因（区分来源，不硬编码）：
- income 到右边界：`"income 同毫秒记录可能超单页且接口不支持事件ID续传，无法确认右边界排空"`
- income 同毫秒卡住：`"income 同毫秒记录超过单页上限且接口不支持事件ID续传"`
- fills ID 卡住（防御）：`"接口返回未推进（事件ID卡住），无法继续翻页"`

### 3.3 可恢复游标语义（不越界、不 +1ms）

- `covered_until_ms` 停在**未排空的边界毫秒**（`new_advance.time_ms`），**不得** `+1ms`、不得越过 `window_end_ms`。
- 完成路径把 `covered_until_ms` 固定为 `window_end_ms`（而非 `new_advance.time_ms`，避免 fills 完成时带回越界记录使 `max_time` 溢出窗口）。当前 `_build_terminal_or_partial_result` 里 `covered = new_advance.time_ms or window_end_ms` 需改为完成时 `covered = window_end_ms`。
- 重跑时 `_load_cursor` 读到 `covered_until_ms` 后：incomplete 窗口 `cur_start` 停在缺毫秒，续采从该毫秒重来，`tran_id` 去重防重入账。

### 3.4 下游拒绝不完整结论

- 新增/明示下游消费点规则：任何消费 `page_complete=False`（尤其 `request_status='incomplete'`）的 income 结果，**不得产出净收益对账 / alpha 结论**。本轮不实现下游消费端（净收益对账仍排除），只以「**规则 + 代码审查项**」锁定（**消费端断言尚未实现，不得表述为『自动拒绝已验证』**）：不存在「用 `window_end_ms + 1` 作续传起点/过滤上界」的实现路径。

---

## 4. RK1 写入失败可见设计

### 4.1 告警通道选型（复用，不新造）

复用 `shared/notification.py` 的 `NotificationClient.send_alert`（异步，`project="alert"`，走独立 alert webhook）。`strategies/hrs/strategy.py` 已持有 `self.notification_client` 与 `self._should_notify(event)`（[L4483-L4497](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L4483-L4497)），复用即可。

新增一个私有辅助（收敛三处，禁重复）：

```python
async def _alert_signal_db_failure(self, kind: str, *, symbol=None, direction=None,
                                   signal_id=None, resolution=None, error=None) -> None:
    """信号落库/更新失败的可识别告警（kind 区分三类来源）。"""
    if not self._should_notify("signal_db_failure"):
        return
    parts = [f"kind={kind}"]
    if symbol: parts.append(f"symbol={symbol}")
    if direction: parts.append(f"direction={direction}")
    if signal_id is not None: parts.append(f"signal_id={signal_id}")
    if resolution: parts.append(f"resolution={resolution}")
    if error is not None: parts.append(f"error={error}")
    try:
        await self.notification_client.send_alert(
            title="HRS信号落库失败",
            message="; ".join(parts),
            level="warning",
        )
    except Exception as e:
        logger.warning("发送信号落库告警失败", error=str(e))
```

### 4.2 三处失败路径改造（替换纯 `logger.warning`）

| 位置 | 现状 | 改动（告警后仍 `logger.warning` 保留日志，不能吞主流程） |
|---|---|---|
| [_log_signal_to_db L3797-L3799](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3797-L3799) | `logger.warning("信号日志持久化失败", symbol, error)` | 追加 `await self._alert_signal_db_failure("insert_failed", symbol=symbol, direction=direction, error=str(e))`，仍返回 `None` |
| [_update_signal_resolution L3819-L3822](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3819-L3822) | `logger.warning("信号缺少 signal_id…", resolution)` | 追加 `await self._alert_signal_db_failure("signal_id_missing", resolution=resolution)`，仍 `return` |
| [_update_signal_resolution L3833-L3834](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3833-L3834) | `logger.warning("信号结果更新失败", signal_id, error)` | 追加 `await self._alert_signal_db_failure("update_failed", signal_id=signal_id, resolution=resolution, error=str(e))` |

> 可定位性取舍：`_update_signal_resolution` 现有签名仅 `(signal_id, resolution)`，无 `symbol`。为避免大面积改十数个调用点、且保持「signal_id 精准定位」，`signal_id=None` / `update_failed` 两类不强行注入 `symbol`（前者天然无 ID，后者可用 signal_id 定位）；`insert_failed` 已含 `symbol`。满足「含 symbol/signal_id/error 等可定位信息」的「等」字语义。

### 4.3 配置项清单（禁硬编码）

| 参数名 | 配置文件 | 默认值 | 说明 |
|---|---|---|---|
| `notification.events.signal_db_failure` | [`strategies/hrs/config.yaml`](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/config.yaml) 的 `notification.events` | `true` | 关告警不影响主流程（`_should_notify` 兜底：未配置时默认放行） |
| alert webhook 目标 | 环境变量 `FEISHU_WEBHOOK_ALERT`（或复用 btc_eth/default） | — | 由 `shared/notification.py` 维护，**不硬编码到 strategy** |

### 4.4 `_ensure_db_schema` 幂等迁移路径确认

已存在（[L3036-L3037](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3036-L3037) 建表含两列；[L3061-L3066](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3061-L3066) 幂等 `ADD COLUMN IF NOT EXISTS`）：
- **新库**：`CREATE TABLE IF NOT EXISTS` 直接带 `entry_mode` / `signal_origin` 两列。
- **旧库**：两条 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 幂等补齐。
- **结论**：迁移路径**代码正确、无需改**；仅「库表核对」标注**上线时执行**（部署后 `docker exec` 核验 `hrs.hrs_signals` 两列存在且可写非空值）。本轮不 apply、不连库。

---

## 5. R11 测试更新设计（已批准行为变化，不改生产代码）

### 5.1 根因定论（证据）

失败用例 [test_scoring_engine.py#L373-L389](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/tests/test_scoring_engine.py#L373-L389) `test_技术总分不足不应入场`：
- 输入 `direction="short"`，技术总分 `technical_score=2.0`，断言 `technical_score < 4.0` 时应拒。
- 但 [config.yaml#L38/L41](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/config.yaml#L38-L41) V2.10 已多空分离：做多技术门槛 `min_total_score=4.0`，**做空技术门槛 `short_min_total_score=2.0`**。
- [scoring_engine.py#L1556-L1561](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/scoring_engine.py#L1556-L1561) 映射 `short_min_technical_score = short_min_total_score = 2.0`。
- 因此 short 信号 `technical_score=2.0 >= 2.0` **通过**技术门槛，`should_entry=True`（总分 `6.4 >= short_entry_threshold=5.5`，基础形态亦通过）。HEAD 亦红。

→ 属**情形 b：已批准的行为变化**（V2.10 多空分离，提交 `3b4052d` 引入 config 未同步测试）。**动作 = 更新测试与文档，不改生产代码**。

### 5.2 用例改动清单

| 用例 | 改动 |
|---|---|
| `test_技术总分不足不应入场`（short，technical=2.0） | **改断言为 `engine.should_entry(result) is True`**（2.0 满足做空技术门槛），同步注释说明 V2.10 |
| 新增 `test_做空技术分不足应拒入场`（short，technical<2.0，如 1.5） | 断言 `should_entry is False`（真正技术不足，total 需保持 `>= short_entry_threshold` 使失败点落在技术门槛） |
| 新增 `test_standard_and_shadow_independent` | 断言修 standard 拦截不改影子链输出形态（承 R1/D2/D8） |

> 禁止：绕开断言、mock、或把 `<` 改 `<=`/`>` 之类阵痛式改期望。批准来源记入 `decisions.md`（编号/日期 `3b4052d` + 2026-10-17 本轮收口）。

### 5.3 需同步文档

- [混合反转策略（HRS）V2.5.1](file:///Users/yl/vscode/Binance_quantitative_trading/docs/requirements/HRS/混合反转策略（HRS）V2.5.1（融合修复版）完整文档.md) 的评分门槛段落：注明「V2.10 做空技术门槛为 `short_min_total_score=2.0`（做多 `min_total_score=4.0`）」。

---

## 6. RK2 口径落地设计

### 6.1 读取方排查结论（事实）

全代码树（排除 `docs/`）**无任何 `SELECT hrs.hrs_signals`**。`hrs_signals` 仅被 `strategies/hrs/strategy.py` INSERT/UPDATE 写入；dashboard 与 ai-tuner/研究脚本均读 `trading.trade_records`（真实成交），**不读** `hrs.hrs_signals`。结论：**当前零读取方**，但口径必须在数据可用前锁定。

### 6.2 口径定义表（权威，文档化 + 进入 `decisions.md` 附录）

| 口径 | SQL 语义定义 |
|---|---|
| 触发数 | `status IN ('triggered','resolved')` 的行数 |
| 超时数 | `resolution = 'timeout'` 的行数（R9 后开始落库） |
| 影子数 | `resolution = 'shadow'` 或 `signal_origin IN ('shadow_scoring','empty_pool')`（后者覆盖 `entry_mode`/`signal_origin` 历史兼容） |
| 真实执行数 | `resolution = 'executed'`（代码保证仅非 shadow 信号落 executed；**不自动等于真实成交**） |
| 执行率 | `executed / (executed + conflict + timeout)`（分母显式声明） |
| 未决行 | `status = 'triggered' AND resolution IS NULL` |
| 胜率分子 | `trading.trade_records` 中 `strategy='hrs'` 且 `realized_pnl > 0` 的已平仓单数 |
| 胜率分母 | `trading.trade_records` 中 `strategy='hrs'` 且 `realized_pnl IS NOT NULL` 的已平仓单数 |

> 关键红线：**胜率必须取 `trading.trade_records.realized_pnl`（真实成交）**，严禁用 `hrs_signals.resolution='executed'` 的行数或 `resolved` 计数当胜率分子。

**正交声明（关键，2026-10-17 收口补充）**：`conflict` 属**结果轴**（`resolution`），`shadow` 属**来源轴**（`signal_origin`），二者**非互斥、互相正交**。一条 `signal_origin='empty_pool'`（来源=影子）的 LV-RM 信号其 `resolution` 可为 `'conflict'`，因而**同时**计入「影子数」（来源轴）与「冲突数」（结果轴）。固定样本中该行为 1 行（ARBUSDT），故**影子数 = 4**（3 行 `resolution='shadow'` + 1 行 `signal_origin='empty_pool'`）、**冲突数 = 3**（2 行 `standard_scoring` origin + 1 行 `empty_pool` origin）。

**执行率分母规则与 3/9 vs 3/8 差异来源（2026-10-17 收口补充）**：
- **权威分母** = `executed + conflict + timeout`，**不按来源剔除**。固定样本 executed=3、conflict=3、timeout=3 → 分母 = **9**，执行率 = **3/9 ≈ 0.333**（`test_execution_rate` 断言）。
- **3/8 的来历**：若**错误地**把 conflict 与 shadow 当互斥、把「来源为影子的 conflict 行」从冲突分母删去，则 conflict=2 → 分母 = 8 → 得 **3/8**。此为**误算**：`shadow`（结果轴）本就不在分母，无需另删来源为影子的 conflict 行；结果轴与来源轴不可混算。
- 零分母保护：分母为 0 返回 `None`（`test_execution_rate_zero_denominator`）。

> 上述两处（正交声明 + 分母规则）为**收口新增**，是 RK2 口径可判定的核心；对应断言落于 `strategies/hrs/tests/test_rk2_signal_stats.py`。

### 6.3 失真风险披露（不修，仅记录）

- 跨周期重复行：同信号每周期 INSERT 一行，超时仅标当前行。
- `'abandoned'` 死值：`_update_signal_resolution` docstring 声明但代码从不写入（[L3812-L3813](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3812-L3813)）。
- `_send_cycle_summary` prod+shadow 混计（[strategy.py#L2654-L2690](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L2654-L2690)）：**可选、既有问题，本轮不改**，仅披露。

### 6.4 固定样本验证（离线、合成数据）

- **不新增生产统计函数**（避免零读取方下的死代码/幽灵函数）。固定样本验证在新增测试 `strategies/hrs/tests/test_rk2_signal_stats.py` 内以**测试本地 helper**（实现 §6.2 口径的分类计数）完成，作为口径回归锚点。
- 合成样本含：生产 `executed`、影子 `shadow`、超时 `timeout`、冲突 `conflict` 各若干 → 断言：
  1. `超时数` 不进入「真实执行数」与「胜率分子」。
  2. 影子数不进入「真实执行数」。
  3. 口径计数对 `resolution='timeout'` 行恒为 0 或显式排除。
- 未来若确有读取方接入，按 §6.2 口径抽公共函数，避免在同构 SQL 间复制。

---

## 7. 实现交接清单（逐文件逐函数，供 python-engineer 落地）

### 7.1 `scripts/export_execution_facts.py`（R10 + R12 共用，唯一实现）

| 函数/位置 | 改动点 |
|---|---|
| ~~`CursorRecord`（L113 附近）~~ | ~~新增字段 `saw_out_of_window`~~ → **未采用（死字段）**，见 §1.5；`saw_out_of_window` 为 `_one_page_iteration` 局部变量 [L789](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L789) |
| ~~`_make_cursor`（L153-L184）~~ | ~~新增 `saw_out_of_window` 参数~~ → **未采用**（无对应字段） |
| `_run_paging_until_done`（[L556-](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L556)） | 去掉 `loop_advance.time_ms < window_end_ms` 条件（去时间墙），仅保留 `page_count < self.max_pages`；`else` 分支（max_pages guard）不动 |
| `_one_page_iteration`（[L757-](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L757)） | 保 `raw_rows = await api_call`；`rows = _filter_rows_within_window(raw_rows, ...)`；计算 `raw_page_full = len(raw_rows) >= self.page_limit`、`saw_out_of_window = len(rows) < len(raw_rows)`；透传给 `_resolve_page_outcome` |
| `_resolve_page_outcome`（[L799-838](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L799-L838)） | 签名加 `raw_page_full`、`saw_out_of_window`、`supports_id_cursor`；按 §1.3 三情形重写；`ok_or_window_end` 删除 |
| `_detect_unstoppable`（[L732-755](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L732-L755)） | 重写为 §1.3 的 `raw_page_full`/`supports_id_cursor`/`window_end_ms` 版本，删除 `page_limit`/`rows` 入参 |
| `_build_unstoppable_result` | `error_detail` 按 §3.2 区分三来源 |
| `_build_terminal_or_partial_result` | 完成路径 `covered` 固定 `window_end_ms` |
| `export_fills`（L444-L449 工厂） | 传 `supports_id_cursor=True` |
| `export_income`（L488-L502 工厂） | 传 `supports_id_cursor=False` |
| **新增** `_PageCtx`（[L142-158](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L142-L158)） | 翻页上下文冻结数据类：身份 + 窗口 + 回调 + `supports_id_cursor`，构造一次向下传递，消除同构 kwargs |
| **新增** `_prepare_cursor_and_fetch`（[L657-661](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L657-L661)） | 前置守卫（Round 4 修正）：`cur_start >= ctx.window_end_ms` **且** `loaded_cursor is not None` **且** `loaded_cursor.page_complete` 三条件同时成立才幂等 OK 短路；无游标（首次查询）即便 `cur_start == window_end_ms`（单毫秒闭区间）也不得短路，必须进入翻页循环查一次（partial/incomplete 游标亦不短路）。**Round 4 说明**：round-3 曾写为「无游标也短路」，导致单毫秒点窗 `[T,T]` 首次查询被静默跳过、返回 0 条且误标 complete，本轮已改为「仅已有且已完成游标才短路」 |

> `supports_id_cursor` 传递方式：在 `_PageCtx` / `_prepare_cursor_and_fetch` / `_run_paging_until_done` / `_one_page_iteration` 链路上透传（fills/income 入口各给一次），不在 `api_call_factory` 塞额外逻辑。

> **落地实况（2026-10-17 收口）**：为满足「单函数 ≤ 50 行」，原 `_resolve_page_outcome` 一族进一步拆为 `_prepare_cursor_and_fetch`（守卫 + 载游标 + 分派）、`_run_paging_until_done`（主循环）、`_one_page_iteration`（请求→过滤→委托收尾）、`_resolve_iteration_dispatch`（消费 + 断点分派）等 ≤50 行小函数（[export_execution_facts.py L556-L838 区间](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L556-L838)）。`CursorRecord` **未新增字段**（见 §1.5）。

### 7.2 测试 Fake 收紧

| 文件 | 改动点 |
|---|---|
| 测试 Fake（fills/income 时间查询） | `< end_time` → `<= end_time`；定位现有 Fake 时间过滤谓词 |

### 7.3 `strategies/hrs/strategy.py`（RK1）

| 函数/位置 | 改动点 |
|---|---|
| 新增 `_alert_signal_db_failure` | 按 §4.1 实现（`_should_notify("signal_db_failure")` → `send_alert`） |
| `_log_signal_to_db` except（L3797-L3799） | 追加 `_alert_signal_db_failure("insert_failed", symbol, direction, error)` |
| `_update_signal_resolution` signal_id=None（L3819-L3822） | 追加 `_alert_signal_db_failure("signal_id_missing", resolution)` |
| `_update_signal_resolution` UPDATE except（L3833-L3834） | 追加 `_alert_signal_db_failure("update_failed", signal_id, resolution, error)` |
| `_ensure_db_schema`（L3061-L3066） | **不改**；仅确认幂等 ALTER 已达标，上线核对列存在 |

### 7.4 `strategies/hrs/config.yaml`（RK1 配置）

| 位置 | 改动点 |
|---|---|
| `notification.events` | 新增 `signal_db_failure: true`（若段落缺，补默认；`_should_notify` 未配置时已默认放行） |

### 7.5 R11 / RK2（测试 + 文档，不动生产代码）

| 文件 | 改动点 |
|---|---|
| `strategies/hrs/tests/test_scoring_engine.py` | 改 `test_技术总分不足不应入场` 断言为 True；新增 `test_做空技术分不足应拒入场`、`test_standard_and_shadow_independent` |
| `docs/requirements/HRS/混合反转策略（HRS）V2.5.1…md` | 评分门槛段落补做空技术门槛说明 |
| `strategies/hrs/tests/test_rk2_signal_stats.py`（新） | 固定样本分类统计 + 超时/影子不计入真实执行/胜率分子断言 |
| `docs/research/strategy_remediation/decisions.md` | 追加 R11 批准来源、RK2 口径定义表 |

---

## 8. 先红后绿回归用例清单（与需求 §3 对齐，补断言要点）

| 需求 | 文件（**实际落盘**，勘误见 requirements §3） | 用例 | 红（修复前） | 绿（修复后） |
|---|---|---|---|---|
| R10 | `tests/test_shared/test_execution_facts_regression.py` | `test_same_ms_exactly_at_limit` | 满页触界误报 complete 漏后续 | 取满、`page_complete=True`、不空转一页 |
| R10 | 同上 | `test_same_ms_limit_plus_one` | 只取回 limit 笔 | 取回 `limit+1`、`page_complete=True` |
| R10 | 同上 | `test_same_ms_four_trades_limit_two` | 4 笔同毫秒 limit=2 只取 2 | 取 4 笔、`page_complete=True` |
| R10 | `tests/test_accounting/test_fact_ingestion_gaps.py` | `test_full_page_but_time_reached_window_end_finishes_ok` | 越界记录致误判 | 越界被过滤、不进 collected、完成只看窗口内排空、原始页满须续传 |
| R10 | 同上 | `test_mid_window_failure_never_complete` | 中途失败仍 complete | `page_complete=False`、errors 非空、游标不推进、恢复续取 |
| R10 | 同上（Fake 时间谓词收紧 `<` → `<=`） | `test_time_boundary_consistency_fake_closed_interval` | Fake `< end` | 请求/过滤/游标/Fake 统一闭区间 |
| R12 | `tests/test_shared/test_execution_facts_regression.py` | `test_income_no_from_id_falls_back_to_incomplete` | 满页误报 complete | `page_complete=False` + `request_status='incomplete'` + 缺口原因 |
| R12 | `tests/test_accounting/test_fact_ingestion_gaps.py` | `test_income_resume_via_sink_cursor_broken_plus_resume_covers_full` | 满页触界误报 complete 漏收入 | 续取成功取全 / 或显式 `page_complete=False`+`incomplete`+缺口原因 |
| R12 | 约束（规则 + 代码审查项） | `test_income_never_uses_plus_one_ms_workaround`（位于 `tests/test_shared/test_execution_facts_regression.py` L771） | 用 +1ms 绕过 | 无 +1ms 跳过路径由该单测锁定；「不完整窗口不得进入净收益结论」的验证方式为**规则 + 代码审查项**（消费端门禁实现待办）。 |
| RK1 | `strategies/hrs/tests/test_rk1_signal_db_alert.py` | `test_insert_failed_kind_sends_alert` 等（11 用例） | 仅 logger.warning | 落库失败触发 `send_alert`（含定位信息） |
| RK1 | 同上 | `test_signal_id_missing_kind_sends_alert`、`test_update_failed_kind_sends_alert` | 仅 logger.warning | signal_id None / UPDATE 异常触发告警 |
| RK1 | 同上 | `test_insert_writes_entry_mode_and_signal_origin`（复用 R9 基线） | 两列可空/未写 | 生产/影子 INSERT 非空归属字段 |
| RK1 | 上线核对 | 库表核对两列存在 | 冷隔离、两列不存在 | 部署后 `ADD COLUMN IF NOT EXISTS` 生效（**上线时执行**） |
| R11 | `strategies/hrs/tests/test_scoring_engine.py` | `test_技术总分不足不应入场`（改） | 返回 True（红） | 按新口径（short 技术 2.0 通过）断言 True |
| R11 | 同上 | `test_做空技术分不足应拒入场`、`test_standard_and_shadow_independent` | 三轨混写互相影响 | short 技术分 < 2.0 拒入场；standard 拦截不改影子链输出 |
| RK2 | `strategies/hrs/tests/test_rk2_signal_stats.py` | `test_timeout_not_counted_as_execution`、`test_win_rate_must_come_from_trade_records` | 超时按行计胜率分子 | 超时/影子不计真实执行/胜率分子 |
| RK2 | 同上 | `TestFixedSampleStatistics`（13 行固定样本） | 口径未定义、随样本漂移 | 固定样本下各口径与预期一致（含执行率 3/9） |

---

## 9. 约束守约自检

- **不重复代码**：fills/income 收尾、游标、不可推进判定全部收敛到 `_resolve_page_outcome` / `_detect_unstoppable` / `_build_*` 单一实现，用 `supports_id_cursor` 区分而非复制同构分支。
- **不硬编码**：告警事件开关走 `notification.events.signal_db_failure`；缺口原因文案为常量字符串（非业务阈值）；阈值/限额沿用 `page_limit` / `max_pages` 既有配置。
- **不幽灵参数/死字段（收口修正）**：原拟新增的 `CursorRecord.saw_out_of_window` **字段未采用**（会被判为死字段），改为 `_one_page_iteration` 局部变量，由 `_resolve_page_outcome` 在迭代内直接消费；`supports_id_cursor` / `raw_page_full` 在判定内真实使用；告警辅助参数均按需传入、可省略。
- **单函数 ≤ 50 行、单行 ≤ 120 字符**：`_resolve_page_outcome` 三情形收敛即满足；`_alert_signal_db_failure` 拆分短小。
- **回测/生产分离**：本轮全部改动在 `scripts/`（离线采集）与 `strategies/hrs/`（生产 HRS）内，不触碰 `backtest/`；不涉及「生产代码 ↔ 回测代码」同步问题。

---

## 10. 能力限制披露（本轮绝不得宣称完成）— 收口状态

1. **不部署、不 push、不 apply 迁移、不连生产库、不调真实交易所**（收口仍成立）。RK1 库表核对属**上线时**验证，本轮只做离线断言（mock 注入断言告警被调用）；**生产库迁移生效 + 归属列实际写入**须上线后在服务器核验。
2. **R12 不据此产出净收益对账 / alpha 结论**：income 缺口的「拒绝下游结论」以规则 + 测试锁定，净收益对账本身仍在排除范围。income 同毫秒超一页**无法完整采集**，对应窗口净收益结论保持**不可验收**并须显式报告缺失窗口。
3. **R12 接口能力结论**：`um/income` 无事件游标、`page` 语义未定义（已确证）；持续标记缺口，**禁止**用 `window_end_ms + 1` 绕过。
4. **R11 不改生产代码**（收口确认成立）：根因已定位为「V2.10 多空分离为已批准行为变化」（提交 `3b4052d`），测试已按新口径更新，**未修生产代码**。
5. **RK2 零读取方**为当前代码树排查结论；若编码组在范围外发现读取方，须纳入口径改造清单再排期。
6. **`_send_cycle_summary` prod/shadow 分离**为既有问题、本轮不改；仅披露，避免范围蔓延。
7. **级联告警（新增披露）**：`insert_failed` → `signal_id_missing` 同根因双告警，判定**低危、本轮不修**，记入待办（见 requirements §7.5）。

---

**最后更新：** 2026-10-17（第三轮复审后架构**收口**：状态改为「已落地」；§1.5 记录 `saw_out_of_window` 字段未采用偏差；§6.2 增正交声明与执行率分母 3/9 vs 3/8 说明；§7.1/§8/§9 修正函数拆分与用例落盘名；§10 增级联告警披露；只声明实现落地，不宣称部署完成）