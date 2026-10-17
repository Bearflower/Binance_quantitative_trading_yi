# 架构设计定稿：R1–R6 策略修复收口（2026-10-16）

> 状态：**设计定稿 + 已落地（2026-10-16 收口）**。本文定义架构与契约；实现落地状态与偏差修正见 §7，不含实现代码，不修改任何源文件、配置、数据库。
> 权威输入（与本设计逐条对齐）：
> - 需求与验收：`requirements_R1-R6_2026-10-16.md`
> - 复审报告：`../../reviews/strategy_reaudit_2026-10-16/README.md`
> - 复现脚本：`../../reviews/strategy_reaudit_2026-10-16/reproduce_findings.py`
> - 业务决策：`decisions.md`（D2 / D4 / D5 / D8）
>
> 本文所有行号为**当前工作区实际行号**（含未提交改动，HEAD≈bc1df60），编码前须用 `grep` 复核再落刀。

---

## 1. 总体设计原则与分层

### 1.1 四条贯穿性原则

1. **生产路径与 shadow 路径彻底分离（R1）**
   `score()` 的主结果恒为「旧版 standard 决策链」的结果（完全不接收 `price_change_24h` 影响）；EMM / 半EMM / 空池 LV-RM 全部作为**并行的 shadow 结果**产出，可独立读取、可同源对照，但**不参与生产冲突消解、不真实下单、绝不落 `executed`**。判据用「同一输入启用 shadow 前后，旧版真实订单意图完全一致」作为可测断言。

2. **事务边界：事实先可靠落库并确认成功，才推进游标（R2/R3/R6）**
   游标覆盖位置**不得领先于**已持久化事实。事实（含 raw 原始响应）写入成功并确认后，才 `save_cursor`；任一失败必须返回 `page_complete=False` 且 `errors` 非空，游标不推进、保持可重试范围。游标身份必须完整到「同一账户 + 完整查询过滤条件」，避免不同 income_type / 账户互相覆盖。

3. **新鲜度链路：保留源行情时间或请求起止，覆盖到真正下单前（R5）**
   取价即记录「源行情时间（可得时）＋请求起止时间」，新鲜度按真实延迟判定；报价及其时间/固定参照**一路传到 executor 真正下单前**，准备超时后重取并重判，未知源时间绝不静默放行。

4. **分页推进：按各接口可用能力用「事件 ID / 页码」续传，不可推进则显式不完整（R6）**
   fills 用 `id`（`fromId`）续传；income 若接口不支持 `tranId` 续传，则在同毫秒满页无法推进时保持 `page_complete=False` 并显式报告。**禁止 `+1ms` 跳过、禁止无限重取同页。**

### 1.2 分组与文件交叉（零交叉，可并行）

| 分组 | 成员 | 唯一涉及文件 | 优先级 |
|---|---|---|---|
| C | R2 + R3 + R6 | `scripts/export_execution_facts.py`、`shared/execution_facts_sink.py`、`database/postgres/init-scripts/11-execution-facts.sql` | P1，先做 |
| A | R1 + R4 | `strategies/hrs/strategy.py`、`strategies/hrs/scoring_engine.py`、`strategies/hrs/config.yaml`、`strategies/hrs/candidate_pool.py` | P1 |
| B | R5 | `strategies/new_coin/strategy.py`、`strategies/new_coin/executor.py` | P2 |

三组文件零交叉，可并行编码、独立红绿。**顺序建议：先 C（账本完整性，游标键规则定义贯穿后续）→ A（HRS 行为口径）→ B（新币新鲜度）**；每组内部回归用例先红后绿再交接。

---

## 2. 逐项设计

### 2.1 R1 [P1] HRS 影子逻辑不得替代原生产信号

#### 2.1.1 现状（根因）

- `ScoringEngine.score()`（`scoring_engine.py:1126-1329`）单返回值：EMM 触发时 `if is_emm`（L1169）直接 `entry_mode="emm"` 返回，standard 分支（L1192-1234）**不执行**，standard 不再独立计算。
- standard 分支内 `price_change_decimal` 传入 `calculate_technical_score`（L1196），触发 `extreme_bonus`（`scoring_engine.py:313-330`）——即 price_change_24h 也污染 standard 技术分。
- standard 分支内 `semi_emm` 判定（L1204-1234）改变 `entry_mode` 与地板值。
- `_is_shadow_entry_mode(entry_mode)`（`strategy.py:505-521`）只看 `entry_mode`，`lv_rm` 一律按 `config.lv_rm.execution_mode=shadow` 判定，非空池 LV-RM 也被影子化。
- `_check_lv_rm_entries`（`strategy.py:2559-2703`）产出 `entry_mode="lv_rm"` 信号，**无 `signal_origin`**；两个入口（`_run_lv_rm_only_cycle` L2525 空池 / `_finalize_and_execute_signals` L1313 非空池）无法区分来源。

#### 2.1.2 目标契约

1. `score()` 返回「production 主结果（恒 `standard`）＋ shadow 结果列表」。
   - **主结果**等价于 `price_change_24h=None` 的旧版 standard：`entry_mode="standard"`、不传 `price_change`、无 `extreme_bonus`、**不触发 semi_emm**。
   - **shadow 结果**：EMM（若触发，`entry_mode="emm"`）与半EMM（若触发，`entry_mode="semi_emm"`）各一条独立 `ScoringResult`，挂到主结果上可单独读取。
2. `signal_origin ∈ {"empty_pool","nonempty_pool"}` 仅对 `lv_rm` 信号有意义。
3. `_is_shadow_entry_mode(entry_mode, signal_origin=None)`：
   - `emm`/`semi_emm`：按 `config.{mode}.execution_mode == "shadow"`（D2 已拍板）。
   - `lv_rm`：仅 `signal_origin=="empty_pool"` 才按 `config.lv_rm.execution_mode=="shadow"`；`nonempty_pool` 返回 `False`（保持原政策）。
   - `signal_origin` 缺失（防御）：fail-closed 返回 `True`（影子）并 `logger.warning`，避免误扩大实盘暴露。

#### 2.1.3 具体改造点（文件/函数级）

**A. `ScoringResult` 扩展 shadow 承载**（`scoring_engine.py:17-62`）
- 新增字段：`shadows: List["ScoringResult"] = field(default_factory=list)`。
- `to_dict()`（L39-62）追加 `"shadows": [s.to_dict() for s in self.shadows]`。
- shadow 结果是叶子（其 `shadows` 恒为空）。

**B. `score()` 拆为编排 + 三条链**（`scoring_engine.py:1126-1329`）
- 提取公共子评分（去重，禁止重复代码）：
  - `contract_score`（`calculate_contract_score`，L1157 已算一次，永不重复）。
  - `sentiment_score`（`calculate_sentiment_score`，L1237，不依赖 entry_mode）。
- 新增私有 `_score_standard(symbol, direction, oi_market_cap_ratio, patterns, funding_rate, has_market_cap, klines_4h)`：复刻 L1192-1234 中「`price_change_decimal=None`、去掉 semi_emm 分支」后的 standard 链，`entry_mode="standard"`，`extreme_bonus=0`。返回 primary `ScoringResult`。
- 新增私有 `_score_emm(...)`、`_score_semi_emm(...)`：产出 `entry_mode="emm"/"semi_emm"` 的 shadow `ScoringResult`（保留 price_change_24h、floor 地板、extreme_bonus 等新增逻辑；extreme_bonus 依赖 price_change_24h，**从 standard 主链整体移入 shadow 计算**，因为它是 B1 新增输入，不属旧版 standard 公式）。
- `score()` 编排：先 `_score_standard` 得 primary；再判 `check_emm_conditions`/`check_semi_emm_conditions`，把命中的 shadow 结果 `append` 到 `primary.shadows`；返回 primary。

**C. `analyze()` 分离生产与 shadow 信号**（`strategy.py:606-661`）
- `short_score = score(...)` 后：
  - `should_entry(short_score)` 仅判定 primary（standard）→ 生产信号。
  - 遍历 `short_score.shadows`：对 `entry_mode in ("emm","semi_emm")` 且 `should_entry(s)` 的，产出 shadow 信号。
- `result["short"]` / `result["long"]` 追加 `"shadows": [...]`（落库证据）。

**D. `signal_origin` 注入与传递**（`strategy.py`）
- `_check_lv_rm_entries(..., signal_origin: str)` 签名新增必填参数。
- 构造 signal dict（L2662-2696）写入 `"signal_origin": signal_origin`。
- 两入口分别传：`_run_lv_rm_only_cycle`（L2546）→ `"empty_pool"`；`_finalize_and_execute_signals`（L1313）→ `"nonempty_pool"`。
- EMM/半EMM 的 shadow 信号不设 `signal_origin`（`_is_shadow_entry_mode` 对其不看该字段）。

**E. `_is_shadow_entry_mode` 签名扩展**（`strategy.py:505-521`）
- 改 `_is_shadow_entry_mode(self, entry_mode, signal_origin=None)`，逻辑见 2.1.2 目标契约 3。

**F. `execute_signal` 影子闸门**（`strategy.py:852-870`）
- 改为 `if self._is_shadow_entry_mode(entry_mode, signal.get("signal_origin")):`。影子信号仍跑完风控/仓位/资金校验，最后 `shadow_blocked=True; return True`，不真实下单（该段逻辑已存在，仅补 `signal_origin` 入参）。

**G. `_resolve_conflicts` 冲突隔离**（`strategy.py:1739-1809` + `_finalize_and_execute_signals` L1345-1346）
- `_finalize_and_execute_signals` 将信号**分桶**：`production_signals` 与 `shadow_signals`（判断 `_is_shadow_entry_mode(entry_mode, signal_origin)`）。
- `production_signals` 走 `_resolve_conflicts`（现有冲突选择逻辑，只影响 production）。
- `shadow_signals` 走**新增 `_execute_shadow_signals(shadow_signals)`**：逐条 `execute_signal`（内部必然 `shadow_blocked`），成功后统一 `_update_signal_resolution(..., "shadow")`，**不参与冲突消解、不抢占 production 信号 slot**。

#### 2.1.4 数据结构 / 字段

| 字段 | 类型 | 取值 | 位置 |
|---|---|---|---|
| `ScoringResult.shadows` | `List[ScoringResult]` | EMM/半EMM shadow 结果 | `scoring_engine.py` dataclass |
| `signal["signal_origin"]` | `str` | `empty_pool`/`nonempty_pool` | 仅 lv_rm 信号 dict |
| `signal["shadow_blocked"]` | `bool` | 影子闸门置位 | `execute_signal` |
| `result["short"]["shadows"]` | `list[dict]` | shadow 落库证据 | `analyze` |

#### 2.1.5 边界与异常路径

- `price_change_24h=None` → `check_emm_conditions`/`check_semi_emm_conditions` 返回 `False` → `shadows` 为空，主结果照常。与修复前 `price_change_24h=None` 行为一致。
- `has_market_cap=False`（与 R4 联动）→ `score()` 前置 veto（见 R4），主结果为 veto，不产生任何信号。
- shadow 信号若因风控被 `execute_signal` 提前 `return False`（未到影子闸门）→ 不落库、不计 shadow；这是既有行为，不改变。

#### 2.1.6 可测断言（转回归用例）

- 固定技术/情绪分（`calculate_technical_score→(10,...)`、`calculate_sentiment_score→(10,...)`）：`score("TESTUSDT","short",.3,{},.01,price_change_24h=None)` 与 `price_change_24h=30` 两调，其 **primary** 的 `entry_mode` 与 `should_entry` 完全一致（复现脚本 `shadow_replaces_standard` 反转为 `before_entry == after_entry`）。
- 触发 EMM 时 `primary.shadows` 非空且 `entry_mode=="emm"`，可单独读取。
- `_is_shadow_entry_mode("lv_rm","nonempty_pool") is False`；`_is_shadow_entry_mode("lv_rm","empty_pool") is True`（复现脚本 `lv_rm_existing_also_shadow` 反转）。

---

### 2.2 R4 [P1] 缺真实市值的新入场必须显式拒绝

#### 2.2.1 现状

- `_fetch_market_cap_ratio`（`strategy.py:309-348`）开仓侧 `require_true_for_entry=True`，代理/缺失返回 `has_market_cap=False`（L343）。
- `calculate_contract_score`（`scoring_engine.py:216-268`）L239-241：`has_market_cap=False` → `fallback`（`short=0`/`long=5`）。
- `analyze()`（`strategy.py:574-589`）：不拒绝，仍调 `score` 用 fallback 分。
- 动态阈值样本门槛：`thresholds.sample_count = len(funding_rates)`（`candidate_pool.py:491`），以费率样本数冒充 OI/市值样本数。
- 候选池 OR-2 条件3（OI/市值比）**已正确**排除代理/缺失（`candidate_pool.py:757-777`、`851-866`），不做额外收紧（见下）。

#### 2.2.2 目标契约（D4/D5 已拍板）

1. 开仓侧 `has_market_cap=False`（`oi_proxy`/`missing`/过期）→ 显式拒绝依赖该因子的新入场，`should_entry=False`，真实 `execute_short/long` 不发生。
2. 存量退出（`require_true_for_entry=False`）不受阻，仅记录「输入不可用」（D5 已实现，本轮不动）。
3. 动态阈值逐截面按**该截面自身有效样本数**过大样本门槛，禁止以费率样本数冒充。

#### 2.2.3 具体改造点

**A. 开仓侧显式拒绝（veto）**（`scoring_engine.py` + `strategy.py`）
- 在 `score()` 与 `score_lv_rm()` **入口前新增统一闸门**：`if not has_market_cap:` 直接返回 `ScoringResult(veto=True, veto_reason="缺少真实未过期市值，拒绝新入场", entry_mode="standard"/"lv_rm", ...)`，不进入 contract fallback、不进入技术/情绪凑分链。
- 落点：
  - `score()`：`scoring_engine.py` L1156（`calculate_contract_score` 调用前）。
  - `score_lv_rm()`：`scoring_engine.py` L631 前（LV-RM 同为开仓侧，需一致拒绝）。
- `should_entry()` 已在 L1348-1350 处理 `veto → False`，无需改。
- `calculate_contract_score` 的 fallback 分支：**保留**（退出侧/其他非开仓调用仍可能用），但开仓链不再触达它。避免改其语义影响其他调用方。
- 退出路径（`_monitor_positions`/保护/减仓）不经过 `score()`/`score_lv_rm()` 开仓闸门，天然不受影响；D5 已由 `_fetch_market_cap_ratio(require_true_for_entry=False)` 侧保证。

**B. 动态阈值逐截面样本校验**（`candidate_pool.py:410-530` + `DynamicThresholds`）
- `DynamicThresholds` 增加字段：`funding_sample_count`、`oi_market_cap_sample_count`、`ema_sample_count`（`oi_market_cap_sample_count == true_market_cap_samples`）。
- `_compute_dynamic_thresholds`：用 `len(oi_market_caps)` (= `true_market_cap_samples`) 赋 `oi_market_cap_sample_count`，用 `len(funding_rates)` 赋 `funding_sample_count`，用 `len(ema_deviations)` 赋 `ema_sample_count`；删除 `thresholds.sample_count = len(funding_rates)`（L491）。
- `is_valid()` 改为**多截面整体门槛**：`funding_sample_count >= min_sample_size AND oi_market_cap_sample_count >= min_sample_size`（至少 OI/市值截面达标），不足则返回 `False` 并在日志标注缺样本的截面名 → 调用方整体回退固定阈值。
- 配置：复用 `candidate_pool.dynamic_thresholds.min_sample_size`（`config.yaml:207`，默认 10），不新增参数；如未来需按截面差异化，才在 `dynamic_thresholds` 下新增 `min_true_market_cap_samples`（本轮无需）。

**C. OR-2 入池规则：维持现状，不做额外收紧**
- 结论：候选池 OR-2「条件3 已排除代理/缺失」为正确行为（广筛层不真实下单）；需要收紧的是**评分链显式拒绝（A 点）**，而非入池。这样「缺市值但借涨跌幅/资金费率入池的币」在 `should_entry` 阶段被拦住，真实下单不发生，同时保留候选池观测完整性（D4「先影子比较被排除币与机会变化」）。
- 落地：不改 `_count_short_conditions`/`_count_long_conditions`，在文档「风险与不做清单」中明确此边界，避免下游误解为「缺市值不能入池」。

#### 2.2.4 数据结构 / 字段

| 字段 | 类型 | 取值 | 位置 |
|---|---|---|---|
| `DynamicThresholds.oi_market_cap_sample_count` | `int` | 真实市值样本数 | `candidate_pool.py` |
| `ScoringResult.veto_reason` | `str` | `缺少真实未过期市值，拒绝新入场` | 闸门返回 |

#### 2.2.5 边界与异常路径

- `has_market_cap=False` 且方向 `long`：不再吃到 `fallback long=5` 凑分，直接 veto；复现脚本 `missing_cap_entry` 反转为 `should_entry == False`。
- 退出侧 `has_market_cap` 由 `_fetch_market_cap_ratio(require_true_for_entry=False)` 决定，与开仓闸门解耦，不阻断保护/减仓。
- 动态阈值 `oi_market_cap_sample_count` 不足但 `funding_sample_count` 足 → 整体 `is_valid()=False`，回退固定阈值（满足验收断言 2）。

#### 2.2.6 可测断言（转回归用例）

- `score("TESTUSDT","long",0,{},.01,has_market_cap=False)` → `should_entry is False`，真实 `execute_*` 调用次数 == 0。
- 构造「fee 样本多、真实市值样本少」截面 → 动态阈值 `is_valid() is False`，回退固定阈值（`missing_cap_entry` 反转 + 样本门槛断言）。

---

### 2.3 R5 [P2] 新币价格新鲜度覆盖真实请求延迟与下单前重校

#### 2.3.1 现状

- `_fetch_fresh_ticker`（`strategy.py:442-479`）：`await get_ticker_price` 返回后才 `observed_at = int(time.time())`，再 `age = int(time.time()) - observed_at` 恒 0；无源时间戳、无请求起止。
- `_fetch_fresh_price_and_check_rebound`（`strategy.py:481-540`）：拿到 `(price, observed_at)` 但 `observed_at` 未继续传递。
- `execute_signal`（`strategy.py:554-666`）：`current_price = fresh_price` 后直接 `execute_short(symbol, score_result, current_price)`（L634-639），新鲜度与固定参照未传到 executor。
- `executor.execute_short`（`executor.py:390+`）：余额→基线→杠杆→占位→额度→精度→设杠杆（多步耗时）后才 `_place_short_order(..., current_price)`，期间**无重取价/新鲜度/反弹重判**。

#### 2.3.2 目标契约

1. 保留「源行情时间（可得时）＋请求起止时间」，新鲜度按真实延迟（或源时间）判定；源时间不可得时至少限制请求延迟并标注代理口径。
2. 慢返回（请求延迟 > `max_age_seconds`）判超龄拒绝，不得因「返回瞬间时间差 0」放行。
3. 报价及其时间/固定参照传到真正下单前；执行准备超时后重取并重判反弹/新鲜度；重取后突破固定参照按 D3 口径（非影子拒绝 / 影子放行留痕）。
4. 未知源时间不静默放行。

#### 2.3.3 具体改造点

**A. 新增报价快照结构 `FreshQuote`**（`strategies/new_coin/strategy.py` 顶部，模块级 dataclass）
- 字段：
  - `price: Decimal`
  - `source_time_ms: Optional[int]` —— 权威源时间戳（ticker 的 `closeTime`/`transTime`/`serverTime`，可得才非 None）
  - `request_started_ms: int`、`request_finished_ms: int`
  - `source_time_kind: str` —— `"server_time"` / `"request_span"`（代理）
- 新鲜度统一计算函数 `_quote_age_ms(quote, now_ms) -> int`：源时间可得用 `now - source_time_ms`，否则用 `request_finished_ms - request_started_ms`（代理口径）。（禁止在 `_fetch_fresh_ticker` 与 executor 各写一份，唯一实现。）

**B. `_fetch_fresh_ticker` 改造**（`strategy.py:442-479`）
- 返回 `(Optional[FreshQuote], Optional[str])`（价 + 拒绝原因）。改造后：
  - `request_started_ms = int(time.time()*1000)` 在 `await` 之前。
  - `request_finished_ms` 在 `await` 之后。
  - 源时间：若 ticker 返回带权威时间戳字段则取之，否则 `source_time_ms=None`（代理口径）。
  - 判定：`request_span_ms = request_finished_ms - request_started_ms`；`age_ms = _quote_age_ms(...)`；`if age_ms > max_age_seconds*1000 or request_span_ms > max_age_seconds*1000` → 返回 `(None, "实时价超龄，拒绝新开仓")`。
- 关键：**请求延迟独立判超龄**，即使 `observed_at` 恰为当前时间也用 `request_span` 卡住（满足验收断言 1：60s 请求拒绝）。

**C. 调用链传递 `FreshQuote`**（`strategy.py`）
- `_fetch_fresh_price_and_check_rebound`（L481-540）改为返回 `(Optional[FreshQuote], Optional[str] reject_reason, Optional[str] shadow_reason)`，不再返回裸 float；`ref_high` 与 `FreshQuote` 一并向上返回（固定参照不重算，D3）。
- `execute_signal`（L611-639）改取 `FreshQuote` 并透传到 `execute_short`。

**D. `executor.execute_short` 下单前重校**（`strategies/new_coin/executor.py:390+`，`_place_short_order` L475 前）
- 签名扩展：新增 `quote: FreshQuote` 与可选 `entry_guard: Optional[Callable[[], Awaitable[Tuple[Optional[FreshQuote], Optional[str]]]]]`。
  - `entry_guard` 由 `strategy.execute_signal` 注入（封装「重取价 + 新鲜度判定 + 反弹重判」的唯一实现，禁止 executor 复制该逻辑）。
- 在 `_place_short_order` 之前（额度/精度/杠杆等耗时步骤后）：
  - `now_ms = int(time.time()*1000)`；`age = _quote_age_ms(quote, now_ms)`。
  - 若 `age > max_age_seconds` → `await entry_guard()` 重新取价并重判；
    - 重取超龄/价无效/反弹突破（非影子）→ 释放占位 `_release_claim_quietly(symbol,"open_failed")`，`return (None, reason)`。
    - 重取成功 → 用新 `FreshQuote.price` 作为最终开仓价；反弹突破且影子开启 → 留痕放行（同 `_record_shadow_rebound` 语义）。
- 参数兜底：`max_age_seconds` 与反弹开关从 executor 既有 `config` 读（`scoring.rebound_check.max_price_age_seconds` / `shadow.enabled`），复用 strategy 与 executor 共享的同一配置段，禁止硬编码。

#### 2.3.4 数据结构 / 字段

| 字段 | 类型 | 取值 | 位置 |
|---|---|---|---|
| `FreshQuote.price` | `Decimal` | 实时价 | new strategy/exe |
| `FreshQuote.source_time_ms` | `Optional[int]` | 权威源时间 | new strategy |
| `FreshQuote.request_started_ms/finished_ms` | `int` | 请求起止 | new strategy |
| `FreshQuote.source_time_kind` | `str` | `server_time`/`request_span` | new strategy |
| `execute_short(quote, entry_guard)` | 参数 | 下单前重校入参 | executor |

#### 2.3.5 边界与异常路径

- 源时间未知 → `source_time_kind="request_span"`，日志明确「源时间不可得，降级为请求延迟代理」；请求延迟超限 → 拒绝（满足验收断言 4）。
- 反弹突破参照：重取后仍按 D3「固定高点、不抬高」口径；非影子拒绝、影子留痕放行。
- legacy signal（无 `breakout_reference`）→ 保持旧路径，不强制新鲜度（兼容未升级信号）。

#### 2.3.6 可测断言（转回归用例）

- mock `get_ticker_price` 内推进 clock 60s、`max_age_seconds=5` → 判定超龄拒绝（复试脚本 `slow_ticker_accepted` 反转）。
- 取价到下单前耗时 > `max_age_seconds` → 断言调用 `entry_guard` 重取并重判，最终拒绝或重取成功，而非沿用陈旧价。
- 无权威源时间戳 → 断言不静默放行（`source_time_kind="request_span"` + 拒绝或明确记录）。

---

### 2.4 R2 + R3 [P1] 事实与游标一致性 + 游标身份完整

> R2 与 R3 根因同源（游标生命周期/身份模型设计缺陷），**一并设计，不打两个补丁**。

#### 2.4.1 现状

- `_run_export_loop`（`scripts/export_execution_facts.py:557-592`）：L590 `_save_cursor(cursor)` 先于 L591 `_maybe_sink_upsert(...)`。
- `_maybe_sink_upsert`（L679-695）吞异常；sink `_bulk_upsert_with_raw`（`execution_facts_sink.py:178-204`）吞异常；sink `save_cursor`（L318-329）吞异常。
- 游标键：`(source, symbol, window_start_ms)`；income 用 `symbol=None`，缺 `account_id`、缺 `income_type`。`load_cursor`/`save_cursor` key 在脚本 L703、L722。

#### 2.4.2 目标契约（选型：先事实后游标 + 失败上抛）

**选型说明**：采用「**先可靠写事实并确认成功，再推进游标**」而非「事实与游标同事务」，理由：
- 改动面小、复用现有 `upsert_fills`/`upsert_income`（已同事务写 fact+raw）。
- 事实为幂等 upsert，重复写无害；游标写失败仅导致重试重采该段并幂等回补，不丢数据。
- R2 的「不引入全新中间件、优先复用现有 sink 事务能力」约束成立。

1. 事实写入成功确认后才 `save_cursor`；`upsert` 或 `save_cursor` 任一失败 → `page_complete=False` + `errors` 非空，游标不推进。
2. sink 的 `upsert_fills`/`upsert_income`/`save_cursor` 失败**必须上抛**（不再全吞），由导出循环捕获转换为 `errors`。
3. 游标身份 = `source + account_id + income_type + symbol + window_start_ms`；`account_id` 统一纳入；income 以 `income_type` 参与键；fills 以 `symbol` 参与键、`income_type=None`。
4. 原始响应随事实同事务进入 `execution_fact_raw`（已满足 R2「可恢复持久化载体」，不改）。

#### 2.4.3 具体改造点

**A. sink 失败可见**（`shared/execution_facts_sink.py`）
- `_bulk_upsert_with_raw`（L178-204）：删除「全吞 + warning」，改为 `raise`（异常向上传递；日志仍打中文 error）。
- `upsert_fills`/`upsert_income`（L208-238）：透传上抛。
- `save_cursor`（L318-329）：失败改为 `raise`（不再吞）。
- `load_cursor`（L293-316）：读失败仍 `return None`（降级窗口起点，可接受），但查询键含新身份列（未命中即 None，不会误中旧游标）。

**B. 游标身份模型**（`scripts/export_execution_facts.py` + `execution_facts_sink.py` + `11-execution-facts.sql`）
- `CursorRecord` 增加字段：`account_id: str`、`income_type: Optional[str]`（fills 为 None）。
- 脚本侧 key 元组（`_cursor_store` L332、`_load_cursor` L703、`_save_cursor` L722）扩展为 `(source, account_id, income_type, symbol, window_start_ms)`。
- sink `_SQL_CURSOR_LOAD`（L89-95）、`_SQL_CURSOR_UPSERT`（L75-87）增加 `account_id`、`income_type` 两列；`_cursor_to_args`（L125-140）/`_cursor_from_row`（L143-162）同步。
- 表 `execution_fact_cursor` 唯一键：`UNIQUE NULLS NOT DISTINCT (source, account_id, income_type, symbol, window_start_ms)`（`11-execution-facts.sql:84`）。
- 调用点：`export_fills` 传 `income_type=None` + `symbol=币种`；`export_income` 传 `income_type=income_type_arg` + `symbol=None`；两处均传 `account_id`。

**C. 导出循环顺序与失败反馈**（`scripts/export_execution_facts.py:557-592`）
- `_run_export_loop` 收尾改为：
  1. `await self._sink_upsert_checked(sink_upsert, collected, source)`（可靠写事实；失败 raise 或返回 error）。
  2. 事实成功后才 `await self._save_cursor_checked(cursor)`。
  3. 两步任一失败 → 构造/改写 `cursor` 为 `page_complete=False, request_status="error", error_detail="<中文原因>"`，errors 追加该原因，返回。
- `_maybe_sink_upsert` → 改造为 `_sink_upsert_checked`（成功返回、失败上抛）；`_save_cursor` → `_save_cursor_checked`（sink 失败上抛、内存 store 仍写但**不得**据此视为已推进）。

#### 2.4.4 旧游标迁移口径（本轮不 apply，仅出规则）

- 旧行键 `(source, symbol, window_start_ms)`（缺 `account_id`/`income_type`）：**不删除、不复用其 `page_complete` 完成标记**。
- 新代码以新键查询（带 `account_id`/`income_type`），旧行因键不匹配**天然不被命中** → 自动从窗口起点重采，幂等 upsert 回补，属「冷隔离迁移」。
- 规则备案：若未来需清理/重绑定，可在 init-scripts 新增一条「补 `account_id`/`income_type` 默认值」的幂等迁移；**本轮不创建、不执行**。

#### 2.4.5 可测断言（转回归用例，`cursor_before_persist` 反转）

- 事实写失败注入（`upsert_fills` 抛）→ `page_complete=False` 且 `errors` 非空；恢复后重试 `persisted trade_id 集合 == {1,2}`。
- 游标写失败注入（`save_cursor` 抛）→ 判定未推进；重试从原窗口起点续采，最终集合完整。
- 中途终止注入（sink 不落盘）→ 重试回补缺失段，集合完整。
- income_type 隔离（`income_filter_cursor_collision` 反转）：同窗依次采 REALIZED_PNL(1100)/COMMISSION(500) 各完整，`COMMISSION` 不被 PNL 游标跳过；不同 `account_id` 互不影响；游标读取/保存键含 `source+account_id+income_type(+symbol+window_start_ms)`。

---

### 2.5 R6 [P2] 同毫秒跨页推进

#### 2.5.1 现状

- `_consume_page`（`scripts/export_execution_facts.py:338-376`）只求本页 `max_time`；`_one_page_iteration`（L594+）用 `loop_cur_start`（仅 max time）作下一请求 `start_time`，`last_event_id` 虽记录但**未用于续传**。
- 同毫秒 4 笔、`page_limit=2` → 请求起点 `[1, 1000, 1000]`，只取 2 笔后 `max_pages` 退出（复试脚本 `same_ms_cross_page`）。

#### 2.5.2 目标契约

1. fills（`userTrades`）用 `id`（`fromId`）续传，同毫秒跨页完整取回。
2. income（`um/income`）若接口支持 `tranId` 续传则用之；**若不支持**，同毫秒满页无法用 time 推进时 → `page_complete=False` + 显式 error，不无限重取、不 `+1ms`。
3. 覆盖边界：同毫秒 = limit、= limit+1、多页、最后时间等于窗口右边界。

#### 2.5.3 具体改造点

**A. 引入「推进点」`PageAdvance`**（`scripts/export_execution_facts.py`）
- 定义 `PageAdvance(time_ms: int, event_id: Optional[int])` 替代裸 `loop_cur_start: int`。
- `_consume_page` 返回 `(new_items, PageAdvance)`：`time_ms = 本页 max time`、`event_id = 本页 max id/tranId`。
- `api_call_factory` 签名扩展为 `Callable[[int, Optional[int]], Any]`（`start_time, from_id`）。

**B. fills 续传**（`export_fills` L405-409 + `_one_page_iteration`）
- `api_call_factory = lambda cs, fid: api_client.get_user_trades(symbol, start_time=cs, end_time=window_end_ms, limit=page_limit, fromId=fid)`。
- 满页推进：下一请求 `start_time = 本页最大 time`、`from_id = 本页最大 trade_id`；Binance `userTrades` 支持 `startTime+fromId` 组合，同毫秒剩余靠 `fromId` 越过。
- `page_complete` 判定保持「返回条数 < limit」。

**C. income 续传（能力分支）**（`export_income` L443-448）
- 若注入的 `api_client.get_income_history` 支持 `tranId` 续传参数 → 用 `tranId` 作为 `event_id` 续传（与 fills 同构）。
- 若不支持 → 当「本页满 且 本页全部 `time == 本页起始 max time`」（同毫秒溢出、time 已无法推进）时，**不推进**，返回 `page_complete=False` + `error_detail="同毫秒记录超过单页且接口不支持事件ID续传，无法继续"`，不得无限重取、不得 `+1ms`。

**D. 推进失败即不完整**（`_run_paging_until_done` L528-555）
- 循环内检测「本页消费后 `PageAdvance.event_id is None 且 time_ms == loop_cur_start.time_ms 且 页满`」→ 视为不可推进，走 C 的显式不完整路径，`final_cursor`/`partial_cursor` 标记 `page_complete=False`，错误入 errors。

#### 2.5.4 数据结构 / 字段

| 字段 | 类型 | 取值 | 位置 |
|---|---|---|---|
| `PageAdvance.time_ms` | `int` | 本页最大时间 | script |
| `PageAdvance.event_id` | `Optional[int]` | 本页最大 id/tranId | script |
| `CursorRecord.last_event_id` | `Optional[int]` | 已存在，用于续传推进 | script |

#### 2.5.5 可测断言（转回归用例，`same_ms_cross_page` 反转）

- 同毫秒 = limit：完整取回、`page_complete=True`、不触发 `max_pages`。
- 同毫秒 = limit+1：跨页单条，取回全部 limit+1。
- 同毫秒 4 笔、limit=2：取回全部 4，`page_complete=True`。
- 最后时间 == 窗口右边界：正确收尾、无越界请求。

---

## 3. 接口 / 契约变更清单

| 变更点 | 谁改 | 改什么 | 调用方需同步 | 兼容层 |
|---|---|---|---|---|
| `ScoringResult.shadows` | `scoring_engine.py` | dataclass 加字段 + `to_dict` | `analyze`、测试 | 默认空列表，旧构造不传即可 |
| `score()` 返回语义 | `scoring_engine.py` | 主结果恒 standard，shadow 挂在 `.shadows` | `analyze`、复试脚本断言 | 主结果仍 `ScoringResult`，旧 `entry_mode` 读取在 production 路径仍 `standard` |
| `_is_shadow_entry_mode(entry_mode, signal_origin=None)` | `strategy.py` | 加第二参 | `execute_signal` | 默认 `None` → fail-closed |
| `_check_lv_rm_entries(..., signal_origin)` | `strategy.py` | 加必填参 | 两入口 | 无（本仓内闭包） |
| `DynamicThresholds` 样本字段 | `candidate_pool.py` | 加 3 字段 + 改 `is_valid` | 动态阈值消费处 | 保留 `sample_count` 可继续供非市值截面判断 |
| `FreshQuote` + `_fetch_fresh_ticker` 返回 | `new_coin/strategy.py` | 新结构 + 返回改 `(FreshQuote, reason)` | `_fetch_fresh_price_and_check_rebound` | 内部重构，无外部调用方 |
| `execute_short(quote, entry_guard)` | `new_coin/executor.py` | 加下单前重校入参 | `strategy.execute_signal` | `entry_guard` 可选，缺省时跳过重校（兼容旧测试） |
| `CursorRecord` 加 `account_id/income_type` | `scripts/export_execution_facts.py` | dataclass + 游标键 | sink load/save | 旧键数据不命中，走冷隔离迁移 |
| sink `upsert_*`/`save_cursor` 上抛 | `shared/execution_facts_sink.py` | 删全吞、改 raise | `_run_export_loop` 捕获 | 脚本侧新增 `_*_checked` 包装 |
| `_SQL_CURSOR_LOAD/UPSERT`、表唯一键 | sink sql + `11-execution-facts.sql` | 加 `account_id/income_type` 列与唯一键 | 无 | 冷隔离（不 apply） |
| `api_call_factory(start_time, from_id)` | script | 签名扩展 | 两个 export 入口 | 旧 lambda 需改，闭包内 |

**配置项清单（禁硬编码）**

| 参数 | 配置文件位置 | 默认值 | 说明 |
|---|---|---|---|
| `scoring.rebound_check.max_price_age_seconds` | `strategies/new_coin/config.yaml` | 30 | 新币报价新鲜度上限（复用现有） |
| `scoring.rebound_check.shadow.enabled` | `strategies/new_coin/config.yaml` | 现有 | 反弹影子开关 |
| `candidate_pool.dynamic_thresholds.min_sample_size` | `strategies/hrs/config.yaml:207` | 10 | 动态阈值逐截面最小样本 |
| `candidate_pool.dynamic_thresholds.enabled` | `strategies/hrs/config.yaml` | 现有 | 动态阈值开关 |
| `emm/semi_emm/lv_rm.execution_mode` | `strategies/hrs/config.yaml:142/173/542` | `shadow` | 影子/执行开关（复用） |
| `scoring.contract.fallback` | `strategies/hrs/config.yaml:78-80` | 保留 | 仅退出/非开仓路径用 |

---

## 4. 设计与验收标准对应关系

| 需求 | 验收断言 | 本设计如何满足 | 回归用例来源 |
|---|---|---|---|
| R1 | 旧订单意图不变 / 新增模式出证据 / 非空池 LV-RM 不影子 / 影子不抢占 | 2.1：`score()` 主结果恒 standard + `.shadows`；`signal_origin` 驱动 lv_rm 影子判定；`_execute_shadow_signals` 分桶隔离冲突 | `shadow_replaces_standard`、`lv_rm_existing_also_shadow` 反转 |
| R2 | 事实写失败/游标写失败/中途终止三注入，重试后集合 == 完整采集 | 2.4：先事实后游标 + sink 上抛 + errors 可见 | `cursor_before_persist` 反转 |
| R3 | income_type 互不窜跳 / 账户互不覆盖 / 游标键含完整身份 | 2.4：游标键扩 `account_id+income_type`，冷隔离迁移 | `income_filter_cursor_collision` 反转 |
| R4 | 缺失/代理/过期不 execute / 样本门槛不冒充 / 存量退出不受阻 | 2.2：`score()/score_lv_rm()` 开仓 veto + 逐截面 `is_valid` | `missing_cap_entry` 反转 |
| R5 | 慢返回拒绝 / 执行前重校 / 重取突破参照 / 未知源时间 | 2.3：`FreshQuote` 请求起止 + executor 下单前 `entry_guard` | `slow_ticker_accepted` 反转 |
| R6 | 同毫秒 = limit / limit+1 / 多页 / 窗口右边界 | 2.5：fills `fromId` 续传 + income 能力分支（不可推进显式不完整） | `same_ms_cross_page` 反转 |

> 所有回归用例与 `reproduce_findings.py` 一脉相承：只注入 mock/合成行情，不启动策略、不连 DB、不连交易所。

---

## 5. 分组与实施顺序 + 交叉检查

- 组间文件零交叉（见 1.2），可并行。
- 顺序：`C → A → B`。每组内部：先写红测试（复现 review 失败）→ 实现 → 绿。
- 交叉注意：A 组（R1 主结果恒 standard + `.shadows`）与 A 组（R4 开仓 veto）都在 `scoring_engine.py::score()` 入口，**合并在同一函数的前置与拆分，不拆成两个补丁**；C 组内 R2/R3/R6 都动 `_run_export_loop`/`_one_page_iteration`/游标键，**合并在同一轮游标生命周期重构**。

---

## 6. 风险与不做清单

### 6.1 不做（本轮明确排除）

- 不 push main、不部署、不 apply 任何 DB 迁移、不连生产库、不调真实交易所。
- 不放行 EMM / 空池 LV-RM 真实执行（仍需用户单独拍板）；不改变 standard 评分公式/阈值/权重本身。
- 不把「非空池 LV-RM 原本应被停止」当修复口径；若确需停止全部 LV-RM 属显式策略变更，另立记录，不混入 R1。
- 不额外收紧候选池 OR-2 入池（条件3 已正确排除代理/缺失），收紧点落在评分链显式拒绝。
- 不改变 fills/income 幂等主键 `(account_id,symbol,trade_id)` / `(account_id,tran_id)`。
- 不引入跨进程分布式锁或全新存储中间件。
- 不执行旧游标 SQL 迁移（仅出冷隔离规则）。
- 不在同一变更集改新币总分/技术门槛（保持 9/7 组合行为）。

### 6.2 关键风险

1. **R1 主结果与 shadow 拆分若遗漏 `extreme_bonus` 迁移**，会导致 standard 技术分仍被 price_change_24h 污染 → 验收断言 1 失败。已明确：extreme_bonus 整体移入 shadow 链。
2. **R2 顺序反转不彻底**：任何「仍先 `save_cursor`」的残留路径都会复现漏采。已明确：`_run_export_loop` 唯一出口统一先事实后游标。
3. **income 续传能力未知**：若封装层 `get_income_history` 实际不支持 `tranId`，必须走「显式不完整」分支，禁止擅自 `+1ms`。编码前需读 `shared/binance_api.py` 确认该接口签名。
4. **游标冷隔离依赖键不匹配**：若 sink 查询未加 `account_id/income_type` 条件，仍会误中旧游标。已明确 sQL 与新键一并改造。
5. **executor 引入 `entry_guard` 需防幽灵参数**：`entry_guard`/`quote` 必须真实使用（下单前重校），不得定义后不用。
6. **禁止重复代码**：新鲜度计算（`_quote_age_ms`）、游标构造/推进、shadow 分桶逻辑须唯一实现，禁止各组各写一份同构代码。

---

**最后更新：** 2026-10-16（架构设计定稿，供 `python-engineer-local` 分组并行编码）

---

## 7. 落地状态（2026-10-16 收口）

> R1–R6 三组（C/A/B）均已按本文契约落地，回归**先红后绿**通过，**本轮新增失败 0**。以下登记「与设计定稿的实现偏差」与落地事实，**不覆盖上文设计原文**。测试证据见 [复审报告 §7](../../reviews/strategy_reaudit_2026-10-16/README.md) 与 [需求 §6](requirements_R1-R6_2026-10-16.md)。

### 7.1 各组落地

| 组 | 状态 | 关键实现落点 |
|---|---|---|
| C（R2+R3+R6） | ✅ 落地 | `scripts/export_execution_facts.py` 事务边界改为「先 sink_upsert 事实（含 raw）成功 → 再 `save_cursor`」；sink upsert/save_cursor 由吞异常改为 raise；游标键扩为 `(source, account_id, income_type, symbol, window_start_ms)`；fills 用 `fromId` 续传；income 同毫秒满页无 ID 续传 → 显式不完整 |
| A（R1+R4） | ✅ 落地 | `scoring_engine.py` `score()` 拆「编排 + `_score_standard`/`_score_emm`/`_score_semi_emm`」，主结果恒 standard、shadow 挂 `.shadows`；`strategy.py` `_is_shadow_entry_mode(entry_mode, signal_origin)`、`signal_origin ∈ {empty_pool, nonempty_pool}`；开仓侧 `has_market_cap=False` 入口 veto；`DynamicThresholds` 逐截面样本数 + 多截面 `is_valid()` |
| B（R5） | ✅ 落地 | `new_coin/strategy.py` 新增 `FreshQuote`（请求起止 + 源时间/代理）；`_fetch_fresh_ticker` 请求延迟独立判超龄；`executor.execute_short(quote, entry_guard)` 下单前重校并重判反弹 |

### 7.2 实现偏差（修正设计表述）

- **income 续传实际分支（修正 §2.5.3-C 假设）**：`shared/binance_api.get_income_history` **未暴露 `tranId` 续传参数**，因此实际走「不支持 → 同毫秒满页无法用 time 推进时保持 `page_complete=False`、`request_status='incomplete'`」分支（`_make_incomplete_cursor` 三连检测：本页满 + 时间卡住 + ID 卡住）。**未实现 `tranId` 续传**，也未 `+1ms`。fills 侧 `shared/binance_api.get_user_trades` 已增可选 `from_id`（映射币安 `fromId`），fills 走 ID 续传。
- **新鲜度 `age_ms` 代理口径（修正 §2.3.3）**：`shared/binance_api` 的 ticker **不返回源时间戳（`closeTime`/`transTime`/`serverTime`）**，实际 `FreshQuote.source_time_ms` 恒为 `None`、`source_time_kind="request_span"`；`age_ms = now_ms − request_started_ms`（**含请求耗时 + 执行准备耗时**，非「源时间差」）。设计 §2.3.3-B 所述「有源时间则用之」在当前封装下不可得，实务以请求延迟代理兜底（满足验收断言「未知源时间不静默放行」）。
- **shadow 语义（修正 §2.1.2 / 补语义）**：EMM / 半EMM 的 shadow 信号经 `signal_origin="shadow_scoring"` 标识（非 `emm`/`semi_emm` 字面值）；`lv_rm` 用 `empty_pool`/`nonempty_pool`。shadow **是否影子化仍取决于 config** `{mode}.execution_mode == "shadow"`——若改为 `execute`，该链信号将转正式下单（属显式放行，本轮未放行）。
- **函数长度偏差（§1.3 硬约束）**：`scripts/export_execution_facts.py` 有 3 个函数**仅因 `async def` 参数声明行**计数超过 50 行（实际函数体 ≤51 行），记为可接受偏差；进一步拆分会引入语义风险，本轮不拆。
- **游标迁移（§2.4.4 落地）**：`11-execution-facts.sql` 新唯一键**仅对全新安装生效**；**本轮未 apply 任何迁移**，旧游标靠「新键不命中 → 窗口起点重采 + 幂等 upsert 回补」冷隔离。

### 7.3 triage：已知既有失败（非本轮引入）

test_fix_verification 3F、test_market_data/test_position_claims/test_position_ownership（本机 Python 3.9 无 current event loop）、test_pattern 4F、test_stop_loss_mark 4F、`test_scoring_engine::test_技术总分不足不应入场` 1F、`test_binance_api::production_url` 1F 均为既有失败（旧用例相对当前 config / 环境过时，HEAD 亦红），**未在本轮修复**，不冒充本轮回归。