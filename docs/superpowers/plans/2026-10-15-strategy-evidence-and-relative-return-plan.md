# 策略行为修复、数据补证与相对收益研究执行方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> 本方案交由用户执行。本次只编写方案；不启动代码修改、数据迁移、部署或交易。若交给其他任务执行，按工作包交接，不要求一次执行全部。

**Goal:** 将五类策略从“文档上描述的策略”落实为可复现的运行行为、可对账的净收益与风险暴露，再验证组合执行，最后研究相对收益增量。

**Architecture:** 保留现有单策略执行结构，先做局部行为修复与旁路证据采集。成交事实账本与研究数据独立保存，验证后再切换绩效消费者；组合执行作为后续独立模块，不直接拼接两个策略的下单函数。

**Tech Stack:** 现有 Python、pytest/异步 mock、PostgreSQL、Docker；本地离线回放与研究。所有回测仅在本地运行。

---

## 一、如何使用这份方案

依据：[代码审计报告](../../research/code_audit_2026-10-15/README.md)、[源码哈希与离线证据](../../research/code_audit_2026-10-15/verification.json)。审计基线为 2026-10-15 本地工作区，不等于线上镜像，且已有其他任务修改新币代码。实施前必须重新确认差异，避免重复修复。

本文所有代码路径相对仓库根目录 `/Users/yl/vscode/Binance_quantitative_trading`。标注“新增”的文件是建议交付位置，当前不代表已存在。SQL 表名与字段需先核对实际 schema，不能依照文档猜列后直接更新生产。

**实施原则：**

- 一个独立行为变更一个变更集；不要把 EMM 接线、阈值调整、仓位扩大和收益修复混在一起。
- 修复会改变机会集的分支，先影子计算，对比旧版决策；测试通过不等于允许扩大实盘暴露。
- 已有资金比例先保持；先统一观察口径，再讨论统一仓位算法。三种仓位公式不同不自动等于程序错误。
- 历史策略结果按旧版本保留。新逻辑重跑的结果另标“反事实回测”，不能冒充原实盘。
- 复现审计问题的旧探针在修复后可能故意失败；应保留审计快照，新增目标行为测试，不要改旧证据让它重新变绿。
- 生产部署按项目部署规范独立执行；共享模块变更按规范重建所有受影响消费者。版本回滚不得破坏新格式持仓恢复和已成交仓位保护。

## 二、总顺序、依赖与交付

| 工作包 | 内容 | 前置条件 | 独立交付 | 初步工作量* |
|---|---|---|---|---|
| A | 固定基线、业务决策、复现案例 | 无 | baseline + decision register | 0.5–1 人日 |
| B | HRS/新币行为与持仓语义修复 | A | 6 个可独立评审的修复任务 | 3–6 人日 |
| C | 决策时点数据与来源证据 | A；接入随 B | 可回放的数据样本与质量报告 | 3–5 人日 |
| D | 逐笔净收益与归属账本 | A，C 的时间/标识契约 | 对账报告、并行绩效输出 | 5–10 人日 |
| E | 统一风险观察、已有组合分析 | C+D 通过 | 暴露、重叠、共同尾损报告 | 2–4 人日 |
| F | 多腿执行验证、最小截面实验 | 执行原型可在 C 后开始；结论依赖 D+E | 故障回放与样本外报告 | 5–10 人日 |

*这是拆分工作的预算估计，不是承诺；历史成交能否补全、交易所导出范围、旧仓迁移会显著影响 D。市场观察和积累样本另计，不能靠固定几天替代样本充分性。

依赖：`A → B/C → D → E → F研究结论`。B 与 C/D 的只读采集可并行；共享 schema、持仓格式及 shared API 变更必须先定契约再合并。F 的模拟执行原型不必等待策略盈利证明，但不得提前接入实盘。

## 三、A：先固定基线与业务规则

### A1. 建立可重现基线

**读取：** `shared/config_loader.py`；五个 `strategies/*/main.py`、`config.yaml`、`tuning_overrides/.active`；`shared/capital_manager.py`；实际部署 compose 与镜像信息。

**新增交付：** `docs/research/strategy_remediation/baseline.md`、`baseline_manifest.json`（目录为新增建议）。

- [ ] 保存 Git SHA、未提交改动清单、关键源码 SHA256；标出其他任务负责的文件，先对齐修改边界。
- [ ] 导出线上每个容器的镜像 digest、启动时间、关键源码 hash；只读，不用镜像标签代替版本。
- [ ] 记录请求覆盖版本、实际应用版本、脱敏配置 hash；记录月度额度值、来源、生效月和读取时间。
- [ ] 对比审计报告，给每个问题标记：仍存在／已修复未部署／线上不同版本／无法确认。
- [ ] 每个仍存在的问题建立最小输入与实际输出案例；只用本地 fixture/mock。

本地基线检查命令（旧问题未修复时使用）：

```bash
python3 docs/research/code_audit_2026-10-15/verify_readonly.py
```

预期：在原审计源码上 9 项检查通过；当前代码已修复时，先解释差异，不强行恢复旧行为。

**验收：**每个策略能唯一关联代码、信号配置和运行额度；差异未知项单列，不能统一写“当前最新版本”。

### A2. 把业务选择写成决策记录

**新增交付：** `docs/research/strategy_remediation/decisions.md`。下面是建议口径，需实施者明确采纳或替换并记录理由；本方案没有批准上线启用。

| 决策 | 建议口径 | 为什么需要独立决定 |
|---|---|---|
| HRS EMM 的24h涨跌幅 | 推荐使用已关闭1h K线的 close(t)/close(t−24h)−1，需要25个连续收盘点，再乘100得到百分比数值；缺数据不触发该模式，记录原因 | 交易所滚动ticker与整点K线窗口不是同一个因子；当前最低24根分析要求不自动满足此输入 |
| EMM/半EMM启用 | 接通代码后先影子；常规与新增模式按 entry_mode 分开统计 | 修复会新增以前不存在的交易 |
| 新币反弹判断 | 推荐比较“下单前新鲜实时价”与“信号生成时固定的最近N根已关闭K线最高价” | 也可以定义为最后收盘突破此前N根高点；两种规则不同，必须选定 |
| 市值缺失 | 新入口要求真实、未过期的市值才使用该比率；缺失币单列/跳过这一入场路径 | 不给缺失伪造0.05，不自动重分权重；会改变候选池，先影子比较 |
| 缺市值的已有仓位 | 不阻断保护／减仓，持仓复核明确记录“输入不可用” | 开仓数据要求不能变成无法退出 |
| 资金口径 | 先统一报告单位，不立即统一三种下单公式 | 避免借重构改变风险承受规模 |
| 相对收益目标 | 主目标：降低市场方向暴露的截面多空；另报相对 BTC/市场基准表现 | 超额收益不等于市场中性，等金额也不等于Beta中性 |

**验收：**每项都有选定口径、负责人、日期、版本、预期改变哪些交易；没有确定的业务选择只做观测，不部署改变信号的代码。

## 四、B：行为与持仓语义修复

> **排期约束（A-review 补充，2026-10-15）：** B1/B2/B4/B5/B6a 全部改动 HRS `strategy.py`，**必须串行**，不得并行编辑同一文件。B 段开工日若 HEAD 前进，须先重核 baseline（[docs/research/strategy_remediation/baseline.md](../../research/strategy_remediation/baseline.md)）。

### B1. 接通 HRS EMM/半EMM 的输入与模式传递

**修改：** `strategies/hrs/strategy.py`、`strategies/hrs/scoring_engine.py`（只在必要时）、`strategies/hrs/config.yaml`（显式影子/执行策略）。
**测试：** 扩展 `strategies/hrs/tests/test_scoring_engine.py`；新增 `strategies/hrs/tests/test_emm_input_contract.py`。

- [ ] 写失败测试：常规 analyze 的多/空评分均收到同一时点的 price_change_24h；明确单位是百分比数值，20表示20%，不是0.20。
- [ ] 根据 A2 从截至决策时点的完整窗口计算输入；不足24h、零基准价、缺口、未来K线分别返回不可用与原因。
- [ ] 将值传给 score；模式、原因、输入窗口记录到决策证据。
- [ ] 把 score_result.entry_mode 传入生成的执行 signal。仅补 score 参数仍不完整：后续 execute_signal 默认 standard，会丢失模式语义。
- [ ] 验证 `entry_mode` **信号→下单→execute_signal 读取**一致（见 [strategy.py:433](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L433) 的读取点）。**本变更集只负责「signal 携带 + execute_signal 读取」；状态持久化与重启恢复归 B5**（`position_manager.add_position` 当前不存 entry_mode），不得在本任务越界改 position_manager。
- [ ] 本地 mock 执行并比较旧版与修复版信号差集，新增交易按模式单列。

**验收样例：**缺失涨跌幅→EMM不触发且有原因；只满足2项→按半EMM规则；3项满足→EMM；关闭模块→不触发；两方向均覆盖；未来数据不得改变本次决策。

```bash
python3 -m pytest strategies/hrs/tests/test_scoring_engine.py strategies/hrs/tests/test_emm_input_contract.py -q
```

预期：新增用例在旧逻辑暴露缺口，修复后通过，且不调用真实行情或下单接口。

### B2. 接通空候选池 LV-RM，并统一信号收尾

**修改：** `strategies/hrs/strategy.py`。
**新增测试：** `strategies/hrs/tests/test_lv_rm_empty_pool_execution.py`。

- [ ] 写失败测试：标准候选为空，LV-RM生成1个合格信号，必须进入统一信号处理一次。
- [ ] 用具名 short_signals/long_signals 保存结果，不再传入随后丢弃的匿名空列表。
- [ ] 将非空主周期的信号收尾提为一个内部方法，空池和非空池共用：去重→超时处理→记录→组合预过滤→冲突消解→execute_signal→结果记录。
- [ ] 明确 LV-RM 缺失单币涨幅的处理，禁止因换入口绕过其他风险闸门。
- [ ] 让持仓监控在无信号、候选扫描失败、暂停开仓、结算窗口等路径仍有明确调度；可用外层 finally，但一次周期仅执行一次，监控异常不能覆盖原始错误。
- [ ] **影子对比（D8，A-review 补充）：** 先影子计算，仅记录「旧版丢弃 vs 修复版进入统一收尾」的新增信号（按 `entry_mode=lv_rm` 单列），**影子期间不得真实调用execute_signal下单**；由用户放行后才执行。

建议验收测试结构（fixture 需提供无网络的 hrs_strategy）：

```python
@pytest.mark.asyncio
async def test_empty_pool_lv_signal_reaches_execution_once(hrs_strategy):
    hrs_strategy.candidate_pool.get_lv_rm_scan_range = AsyncMock(return_value=['TESTUSDT'])
    async def produce(symbols, shorts, longs):
        shorts.append({'symbol': 'TESTUSDT', 'direction': 'short',
                       'score': 8.0, 'current_price': 100.0, 'entry_mode': 'lv_rm'})
    hrs_strategy._check_lv_rm_entries = AsyncMock(side_effect=produce)
    hrs_strategy.execute_signal = AsyncMock(return_value=True)
    await hrs_strategy._run_lv_rm_only_cycle()
    hrs_strategy.execute_signal.assert_awaited_once()
```

该测试还须补配本地DB/风险门 mock，使信号正常通过，不能通过替换整个收尾函数掩盖链路错误。

**验收：**空池链路单测可见信号到达统一收尾、非空池不重复；冲突／暂停／超时照常拒绝；失败路径仍监控存量仓位；日志能连接 signal 到最终拒绝或成交。**运行态启用按 D8 先影子**（影子阶段用显式开关禁止真实下单），用户放行后才执行；「测试通过 ≠ 可实盘启用」。

```bash
python3 -m pytest strategies/hrs/tests/test_lv_rm_empty_pool_execution.py -q
```

### B3. 修复新币反弹判断及降级标志传递

**修改：** `strategies/new_coin/strategy.py`、必要时 `strategies/new_coin/executor.py`、`scoring_engine.py`。
**新增测试：** `strategies/new_coin/tests/test_rebound_entry_contract.py`。

- [ ] 按 A2 固定 breakout_reference_high、reference_window_end、signal_time，不能在下单前随价格重算并抬高参照线。
- [ ] 在尽量接近下单的入口获取实时价格，记录 observed_at；超龄、无效或获取失败时拒绝该次新开仓并给原因。
- [ ] 用实时价比较固定高点，再做占位、额度、精度与下单；执行延迟超出新鲜度要求时重查，不能保证网络期间价格完全不变。
- [ ] 将 sentiment_degraded 从分析结果显式传给 should_entry；记录实际 OI 回看时长。
- [ ] 不在同一变更集中修改总分／技术门槛。
- [ ] **影子对比（A-review 补充）：** 该分支修复后由「恒不触发」变为可达的拒绝闸门，会减少部分新开仓；先影子对比旧版与新版的入场决策差集（按 symbol 单列被拒绝的开仓），再决定放行。

**验收输入：**固定高点100，实时价99.9→通过反弹门；100→按“超过”规则通过；100.1→拒绝；ticker过期→拒绝；降级模式技术门槛高于普通门槛的fixture应被拒绝。当前9/7门槛组合行为不应意外改变。

```bash
python3 -m pytest strategies/new_coin/tests/test_rebound_entry_contract.py -q
```

### B4. 给市值真实来源与缺失状态建模

**修改：** `shared/market_cap.py`、`strategies/hrs/market_data.py`、`candidate_pool.py`、`strategy.py`。
**新增测试：** `tests/test_shared/test_market_cap_provenance.py`。

- [ ] 列出 get_market_cap_with_fallback 全部消费者，保留旧接口兼容或同步迁移；避免改返回类型导致其他策略故障。
- [ ] 新接口明确返回 value、source、as_of、available_at、is_proxy、missing_reason；不能只靠正数判断有真实市值。
- [ ] HRS 候选分位数只用符合A2质量要求的真实样本；记录有效样本数、覆盖率，样本不足时明确停止该次依赖此阈值的新入场。
- [ ] 如保留OI×20用于展示，source=oi_proxy且不混入真实市值因子；禁止用0填充代替缺失。
- [ ] 缺失值不会阻止存量仓位保护和退出；影子报告展示被排除币与机会变化。

**验收：**真实、缺失、代理、过期分别测试；三个不同OI的代理值不得在“真实OI/市值”截面中形成0.05样本；有效样本数量可核对。

```bash
python3 -m pytest tests/test_shared/test_market_cap_provenance.py -q
```

### B5. HRS 开仓时间、复核时间与入场模式分离

**修改：** `strategies/hrs/position_manager.py`、`strategy.py`、`executor.py`。
**新增测试：** `strategies/hrs/tests/test_position_lifecycle_metadata.py`。

- [ ] 定义不可变 position_id/opened_at、可变 next_review_at，以及 entry_mode、entry_config_hash；使用带时区UTC在内部计算。**（position_id/opened_at/entry_mode 三字段格式须与 C1 证据契约先行一次定形，避免状态文件二次迁移——A-review 补充。）**
- [ ] 时间止损复核只移动 next_review_at，不改 opened_at；收入匹配与去重使用原始持仓身份。
- [ ] 保存 entry_mode，并贯穿首次保护、补单、时间退出和重启恢复；LV-RM48h与standard72h按选定规则区分。
- [ ] 旧状态迁移：优先用可靠入场订单时间恢复 opened_at；只能找到已被重置的时间时标记 opening_time_uncertain，不编造原始时刻。**（D9）entry_mode 未知的存量仓：按当前全局 72h 时限继续复核，标记 `entry_mode_unknown`，不编造模式、不中途改按 48h。**
- [ ] 定义兼容读和新格式写，先在fixture中演练旧仓恢复；回滚版本必须能读取已有新格式，或另给兼容回滚包。

**验收：**入场t0，t0+72h延长复核，opened_at仍为t0；期间费用/部分退出没有被截掉；重启后模式、时钟与保护参数一致；旧仓未知项可见（`entry_mode_unknown` 按 72h）。

```bash
python3 -m pytest strategies/hrs/tests/test_position_lifecycle_metadata.py tests/test_strategies/test_time_stop_review.py -q
```

### B6. 分策略统一成交事实使用，修正风控计数语义

本任务分 B6a/HRS、B6b/新币、B6c/MTPCS加仓三个独立变更集，不一次修改所有执行器。

**定位：** `strategies/hrs/strategy.py:execute_signal/_monitor_positions`、`executor.py:OpenResult`；`strategies/new_coin/executor.py:execute_short`；`shared/protection_retry.py:merge_position`。
**新增测试：** `tests/test_strategies/test_fill_fact_consistency.py`。

- [ ] 分清 planned_qty、filled_qty、avg_fill_price；持仓初值及保护数量依据已确认成交，未知均价不能悄悄称为真实均价。
- [ ] HRS/新币接收实际成交信息并按真实数量落仓；MTPCS加仓按实际增量成交均价更新，不用信号价替代。
- [ ] 逐个检查“全平就record_profit”“时间止损就record_loss”等调用；按同一持仓净结果及确定状态更新计数，未确定收益不得预先判胜负。
- [ ] 验证部分成交、撤单竞态、均价晚到、零成交、重复回调、保护失败重试；不得因先写失败就丢失已成交敞口。
- [ ] **（A-review 补充）** 胜负计数语义修正会改变连续亏损暂停的触发，须补连续亏损暂停闸门的回归用例（B6a）。

**验收：**计划10、实成4时，仓位/保护均围绕4；旧仓2@100加仓1@110，均价为103.333…，不因信号价105变成101.666…；相同成交事件回放两次数量不翻倍。

```bash
python3 -m pytest tests/test_strategies/test_fill_fact_consistency.py tests/test_shared/test_order_fill_waiter.py -q
```

### 检查点 G1：行为可以解释

- [ ] B1–B6各有目标行为测试、旧新版信号/订单差异说明；新增模式仍可独立关闭新入场。
- [ ] 所有拒绝保留原因；关闭新入场不关闭已有仓位监控和保护。
- [ ] 代码、需求文档、配置注释、版本号对齐；未以“开启开关”替代调用链测试。
- [ ] 上线观察按模式和配置版本统计新增信号、拒绝、成交和错误；不以短期盈利作为修复验收条件。

## 五、C：建立“当时知道什么”的数据证据

### C1. 固定决策与特征契约

**读取：** `shared/kline_service.py`、`services/kline_service/api/routes.py`、`core/collector.py`、HRS候选/行情模块、新币检测器。
**建议新增：** `shared/decision_evidence.py`（纯模型/序列化）；`database/migrations/20261015_decision_evidence.sql`（先核对迁移目录规范）；`tests/test_shared/test_decision_evidence.py`。

> **C1 交付状态（2026-10-15）**：已交付 `shared/decision_evidence.py`、`database/postgres/migrations/2026-10-15-decision-evidence.sql`、`tests/test_shared/test_decision_evidence.py`（19 用例，`pytest` 全绿）。
> - **迁移目录已按规范核对**：实际路径为 `database/postgres/migrations/`（手动迁移目录，**非** `init-scripts/`）；该 SQL **仅落仓库、未 apply**，服务器部署**不会自动执行**（CI 只执行 `init-scripts/`）。属「孤儿 schema 对象」风险，须待消费方迁入 `init-scripts/` 才随部署生效。参见 [deploy-yml-init-scripts-unsynced](../../knowledge/issues/Binance_quantitative_trading-20261001-deploy-yml-init-scripts-unsynced.md)。
> - **表名修正**：架构稿原写的 `decision_evidence_tasks` 是无意义后缀笔误，SQL 已改为 `trading.decision_evidence`；幂等键为 `decision_id`（决策级）+ `UNIQUE NULLS NOT DISTINCT (decision_id, name, source)`（特征级）。
> - **稳定标识口径（确定性、无时间依赖）**：`cycle_id = sha256("strategy_id|symbol|direction|entry_mode|cycle_anchor")[:16]`，其中 `cycle_anchor` 是周期固定锚点（如该周期 K 线窗口起始毫秒），**不是 `now`**；`decision_id = sha256("account_id|strategy_id|symbol|direction|entry_mode|cycle_id")[:32]`，**只含意图键**，不含会漂移的 score/thresholds。
> - **本轮定位：旁路只读、先影子、零行为变更**——纯模型/序列化，不 `import asyncpg`、不连库、无 IO 副作用，**未接入任何策略生产者/消费方**；`available_at` 语义用 `check_available_at_constraint` 显式校验（`available_at` 为 None 或 `availability=unknown` 时不适用；晚于 `decision_time_utc` 则计为失败并给中文原因）。

每条决策的最低字段：

```text
account_id, strategy_id, decision_id, cycle_id, decision_time_utc,
code_hash, config_hash, entry_mode, universe_snapshot_id,
symbol, direction, score_components, thresholds,
planned_qty, planned_notional, margin_limit, margin_limit_source,
accepted, rejection_reason
```

每个特征：`name,value,unit,event_time,available_at,fetched_at,source,quality_flag,fallback_reason,lookback_actual`。

- [x] 为决策生成稳定标识，重试关联同一意图，不凭当前时间每次生成新意图。（`generate_cycle_id` / `generate_decision_id`，纯函数、无时间依赖）
- [x] 在实际消费特征的时点保存证据；历史回填缺少 available_at 时保留unknown，不能以event_time冒充。（`FeatureEvidence.availability ∈ {live,lagging,unknown}`；`check_available_at_constraint` 显式校验，不适用时放行）
- [ ] 候选全集、排除原因与有效阈值一起保存，包括被其他策略占用/额度拦截的有效信号。**（部分：契约已具备 `universe_snapshot_id` / `accepted` / `rejection_reason` / `thresholds` 字段，候选全集的实际采集待接线）**
- [ ] 分批接入一个策略并验证，再接其他策略；序列化失败应有明确处理规则，不能静默丢证据。**（2026-10-15 R2 已接入首个策略 btc_eth（`decision_evidence.enabled` 默认关闭、全吞异常旁路、不改 result、不抛主循环）；序列化失败规则已实现——`serialize_decision` 永不抛异常，`EvidenceSerializeResult(ok, payload, failed_records, errors)` 累计失败并保留成功特征，脱敏不计入 `failed_records`；其它策略未接，待验证通过后推广）**

**验收（本轮范围内已达成）：**稳定标识可重建同一意图、不暴露密钥（`account_id` 白名单 + 疑似密钥哈希化、递归剔除 `*key/secret/token/signature*` 字段）、单特征序列化失败不中断整体；未接入消费者 → 「同一 decision_id 可重建执行结果」待接线后补验。测试命令：`python3 -m pytest tests/test_shared/test_decision_evidence.py -q`（19 用例全绿）。

### C1 运维接线（R2/R3，2026-10-15 需求分析拍板）

**R2｜C1 接首个策略生产者（只接 btc_eth）**
- 接线点：`strategies/btc_eth/strategy.py` 的 `analyze()`（[代码定位](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py#L712-L1086)）。返回结构已确认：拒绝分支返回 `{symbol,score,grade,reason}`（约 8 处 `analysis_result` early-return）；接受分支在第 1046 行构造含 `direction` 的 signal dict、第 1077 行 `return signal`。
- 产出：每次决策生成一个 `DecisionEvidence` + 若干 `FeatureEvidence`（score/grade/direction/market_state/thresholds 等），写入 `trading.decision_evidence` / `trading.decision_feature_evidence`。
- 硬约束：**开关控制、默认关闭**；**单点旁路**（接线点由架构师选位，但必须是单一收口，不侵入每个 early-return）；**旁路安全**——证据写入任何失败都不得改变开仓/拒绝决策、不得抛出打断主循环，只打 warning 日志。
- 序列化复用已交付 `serialize_decision`（永不抛异常）；`decision_id/cycle_id` 用 `generate_*` 稳定标识，`cycle_anchor` 用周期固定锚点（非 `now`）。
- 只接 btc_eth；new_coin/grid/hrs/aggressive 暂不接（验证通过后再推广）。

> **落地状态（2026-10-15，R2/R3 已落地）**：上述设计已实现——新增 [shared/decision_evidence_sink.py](../../../shared/decision_evidence_sink.py)（`PostgresDecisionEvidenceSink.upsert_evidence`，单事务写 `trading.decision_evidence` + `decision_feature_evidence`）、[strategies/btc_eth/evidence_recorder.py](../../../strategies/btc_eth/evidence_recorder.py)（`DecisionEvidenceRecorder`，全吞异常旁路），并在 [strategies/btc_eth/main.py](../../../strategies/btc_eth/main.py) `initialize()`（enabled 真值时构造、异常降级关闭）与 `run_strategy()`（`analyze()` 后单点 `await _rec.record(result, symbol)`）完成接线；`strategies/btc_eth/config.yaml` 末尾新增 `decision_evidence:` 段。**开关 `decision_evidence.enabled: false` 默认关闭 → 生产行为零变化。** 详见 [spec](../../specs/2026-10-15-r1-r2-r3-evidence-execution-facts/spec.md)。

**R3｜decision-evidence DDL 迁 init-scripts**
- 源 `database/postgres/migrations/2026-10-15-decision-evidence.sql` 迁入 `database/postgres/init-scripts/10-decision-evidence.sql`。**已落地**（[10-decision-evidence.sql](../../../database/postgres/init-scripts/10-decision-evidence.sql)）。
- 按 init-scripts 现有约定改写：去掉 `BEGIN/COMMIT`，保留 `CREATE TABLE IF NOT EXISTS` + 索引；**补充表级 `GRANT SELECT,INSERT,UPDATE,DELETE ... TO trading_user` 与 `GRANT USAGE,SELECT ON SEQUENCE trading.decision_feature_evidence_id_seq TO trading_user`**（源 DDL 缺授权）。结尾补 `DO $$ RAISE NOTICE $$`。
- 保持幂等，部署按编号顺序 `psql --set ON_ERROR_STOP=on -f` 执行。源 `migrations/` 文件保留不删、不 apply，仅在文件头追加「已迁入 init-scripts 为权威执行路径」。

### C2. 数据质量、上市池与时间完整性

**建议新增：** `scripts/audit_strategy_input_quality.py`、`tests/test_shared/test_strategy_input_quality.py`、`docs/research/strategy_remediation/input_quality_report.md`。

- [ ] 检查K线根数、关闭时间、缺口、乱序和重复；不要把请求200根当实际拿到200根（共享客户端当前最多100）。
- [ ] HRS合成4h只允许完整且对齐的4根1h；缺一根要明确拒绝或降级，不允许伪装完整。
- [ ] 为OI保存真实回看窗口；费率区分已结算、预测、结算周期及现金流，不能统一固定年化后丢失原始值。
- [ ] 保存历史可交易池、上线/退市、symbol变更及过滤原因；补采表明可追溯范围，缺历史不能用今天名单替代。
- [ ] 为每种输入配置新鲜度/最小覆盖要求；具体数值按策略周期确定并在A2记录，缺数据时退出保护继续可用。

**验收：**报告按策略/日期/字段显示缺失率、代理率、时延分布和实际根数；有效样本有 `available_at <= decision_time` 证据；未来或不完整数据被测试识别。

### 检查点 G2：研究样本可用

- [ ] 不把缺失填零；真实/代理/理论三类可独立筛选。
- [ ] 随机抽查及所有异常案例都能回到原始数据。
- [ ] 时间范围与可用覆盖明确；无法复现的阶段不进入主样本绩效，只作为探索资料。

## 六、D：先重建净收益事实，再切换绩效与分配

### D1. 只读采集与不可变原始事实

**读取：** `shared/binance_api.py`、`shared/trade_logger.py`、`shared/database.py`、实际订单/成交/income表。
**建议新增：** `scripts/export_execution_facts.py`、`database/migrations/20261015_execution_facts.sql`、`tests/test_accounting/test_fact_ingestion.py`。

> **D1 交付状态（2026-10-15）**：已交付 `scripts/export_execution_facts.py`、`database/postgres/migrations/2026-10-15-execution-facts.sql`、`tests/test_accounting/{test_fact_ingestion.py,test_fact_ingestion_gaps.py}`（合计 65 用例，`pytest` 全绿；连同 C1 的 19 用例共 84 passed）。
> - **四表**：`trading.execution_fact_fill`（主键 `account_id,symbol,trade_id`）、`execution_fact_income`（主键 `account_id,tran_id`）、`execution_fact_cursor`（`UNIQUE (source,symbol,window_start_ms)` 断点恢复）、`execution_fact_raw`（`source,entity_key,entity_id` 原始留痕 + `payload_sha256`）。
> - **幂等键口径**：fills 用币安 `userTrades` 响应的 **`id`**（自增整数，同毫秒多笔仍可区分），**不是**响应里的 `tradeId` 字段；income 用 `um/income` 的 **`tranId`**；**禁止**用「时间窗+币种」去重。
> - **枚举/结构选型**：`page_complete` 仅当返回条数 `< limit` 才为 TRUE（满页必须继续翻）；`max_pages` 守卫防无限翻页。
> - **迁移目录同上**：SQL 在 `migrations/`，**仅落仓库、未 apply**，不随部署生效（孤儿 schema 风险）。**（2026-10-15 R3 更新：已迁入 `init-scripts/11-execution-facts.sql` 随 CI 部署执行，源 `migrations/` 文件保留不删不 apply——见下方「D1 运维接线」落地状态。）**
> - **CLI 定位（本轮）：旁路纯采集 + 注入式 sink**——CLI **不组装真实数据库 sink**，采集结果以 JSONL 输出到 stdout；**真库写入由调用方显式注入 sink 负责**（避免 CLI 未经审批直接污染生产库）。**（2026-10-15 R1 更新：CLI 已可组装真实 PG sink，由环境变量 `EXECUTION_FACTS_DB_WRITE` 开关控制、缺省关闭——见下方「D1 运维接线」落地状态。）**
> - **×1 已知风险（须记录）**：`BinanceClient.get_income_history` 在 `use_unified_account=False` 时**直接返回 `[]` 并只打 warning**，脚本无从区分「真的无收入」与「非统一账户被静默拦截」；脚本的 while 循环会把空数组判为 `page_complete=TRUE` 正常收尾、不抛异常。已在 `ExecutionFactExporter` docstring 告知调用方须传入统一账户客户端（`use_unified_account=True`）。数据完整性取决于调用方。
> - **×1 分页契约约束（2026-10-16 R7 补记）**：币安 PM UM `userTrades` 的 `fromId` 与 `startTime`/`endTime` **互斥**，二者不可同现于同一请求。`export_fills` 续传页现只发 `fromId`（时间定位与 ID 续传分离），窗口右边界改由客户端过滤；`get_user_trades` 对非法组合加互斥守卫（`raise ValueError`）。约束说明与修复收口见 [R7–R9 收口架构 §2.1 / §8](../research/strategy_remediation/architecture_R7-R9_2026-10-16.md)。

- [ ] 先导出最小闭环样本：一次完整开平、部分止盈、加仓、跨日资金费、重启后平仓、HRS旧时间复核、网格人工交易。**（仍未交付：2026-10-15 R1/R3 后 CLI 已可注入真实 sink、DDL 已迁入 `init-scripts/`，但开关默认关闭且本轮未 push/未部署，故**尚未对真实库执行采集**；待开启 `EXECUTION_FACTS_DB_WRITE` + 部署后补）**
- [x] 分页拉取订单、fills、income和权益/资金划转；账户+symbol+tradeId去重fills，income按接口事件标识定义复合键；不能只靠时间窗+币种去重。（`export_fills` / `export_income` + Set 去重；`get_user_trades` 新增 `end_time`/`limit` 分页参数，向后兼容）
- [x] 记录分页游标/窗口边界、覆盖期、请求失败；重叠时间窗可重复采集且幂等，limit=1000不代表数据完整。（`CursorRecord` 含 `covered_until_ms`/`request_status`/`error_detail`/`retry_count`；`export_execution_facts.py` docstring 明确「limit=1000 不代表数据完整」）
- [x] 保留原始响应的可追溯副本、采集时间、schema版本；敏感字段脱敏。（`sanitize_payload` 递归剔除密钥/签名，`compute_sha256` 打 payload 指纹；表含 `fetched_at`/`schema_version`）

**验收fixture（已满足）：**1501条记录全部取得；重复页不重复计入；边界同毫秒事件不丢；断点恢复结果与一次完整采集一致；不同币相同tradeId不冲突。（`python3 -m pytest tests/test_accounting/ -q`：65 用例全绿）

### D1 运维接线（R1/R3，2026-10-15 需求分析拍板）

**R1｜D1 注入真实 PG sink**
- 新建基于 asyncpg 的 sink（建议 `shared/execution_facts_sink.py`），实现 `upsert_fills / upsert_income / save_cursor / load_cursor`，把 FillFact / IncomeFact / CursorRecord / raw 四类数据幂等写对应 4 表。
- ON CONFLICT 覆盖各表主键/唯一键：fill `(account_id,symbol,trade_id)`、income `(account_id,tran_id)`、cursor `(source,symbol,window_start_ms)`（含 NULLS NOT DISTINCT）、raw `(source,entity_key,entity_id)`。
- raw 落库前必须脱敏 + `payload_sha256`：FillFact/IncomeFact 已带脱敏后 `raw_payload` 与 `payload_sha256`，sink 在 `upsert_fills/upsert_income` 内一并写对应 raw 表（`entity_key`=fill/income，`entity_id`=对应主键 id）。
- `scripts/export_execution_facts.py` 的 `_run_cli`（[定位](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L836-L861)）改为可组装该真实 sink，**开关控制、默认关闭**（默认仍旁路 JSONL 输出）。**已落地**：新增环境变量 `EXECUTION_FACTS_DB_WRITE`（truthy=1/true/yes/on，缺省关闭）与 `_maybe_open_sink`/`_maybe_close_sink`（connect 失败降级为 (None,None)，`finally` 释放 pool）。

> **落地状态（2026-10-15，R1/R3 已落地）**：上述设计已实现——新增 [shared/execution_facts_sink.py](../../../shared/execution_facts_sink.py)（`PostgresExecutionFactsSink`，写 `execution_fact_fill/income/raw/cursor` 四表），[scripts/export_execution_facts.py](../../../scripts/export_execution_facts.py) 增加上述开关与 sink 装配。**开关默认关闭 → CLI 仍为纯 JSONL 旁路、不触碰生产库。** 详见 [spec](../../specs/2026-10-15-r1-r2-r3-evidence-execution-facts/spec.md)。

**R3｜execution-facts DDL 迁 init-scripts**
- 源 `database/postgres/migrations/2026-10-15-execution-facts.sql` 迁入 `database/postgres/init-scripts/11-execution-facts.sql`。**已落地**（[11-execution-facts.sql](../../../database/postgres/init-scripts/11-execution-facts.sql)）。
- 同上：去掉 `BEGIN/COMMIT`，补表级 `GRANT SELECT,INSERT,UPDATE,DELETE ... TO trading_user` 与序列授权（`execution_fact_cursor_id_seq`），结尾补 `DO $$ RAISE NOTICE $$`。源 `migrations/` 文件保留不删、不 apply。

### D2. 订单→持仓周期→策略归属

**建议新增：** `shared/execution_attribution.py`、`tests/test_accounting/test_execution_attribution.py`。
**关联现有：** `shared/position_ownership.py`、策略持仓状态、clientOrderId、decision_id、entry/close记录。

- [ ] 定义 position_id 为可解释的持仓生命周期；同币平仓后再开必须新周期。
- [ ] 优先按明确订单/成交关系匹配策略；其次依据可证明的持仓归属区间，记录匹配规则和置信分类。
- [ ] 无法归属时记 unmatched，绝不按最近一笔或当前所有者强行归属旧成交。
- [ ] 资金费率按结算时点的实际持仓归属；多策略共享净仓无法唯一分摊时标记 allocated，并保存分摊方法。
- [ ] 明确手工交易、净持仓模式、对冲持仓模式以及策略同币交接的边界。

**验收：**跨策略同币、同一币多轮开平、手工减仓、对冲双向持仓不会误归属；unmatched金额和比例可见，人工修订有版本与原因。

### D3. 净收益与权益守恒

**建议新增：** `shared/net_pnl_ledger.py`、`scripts/reconcile_net_pnl.py`、`tests/test_accounting/test_net_pnl_reconciliation.py`。

统一正负号：交易盈亏和funding为有符号金额；commission为正的成本；external_cashflow净流入为正。

```text
net_realized = gross_realized + funding - commission + other_income
Δequity - external_cashflow
    = gross_realized + funding - commission + other_income + Δunrealized
```

- [ ] 原始逐笔与PNL_SUMMARY只能择一计入同一经济事件；汇总记录作为兼容输出，不再叠加到事实账本。
- [ ] 非USDT费用保留原币种/原金额及估值时点，记录换算来源，不能直接按数值相加。
- [ ] 先校验账户总账，再看策略归属；策略合计加unmatched应等于账户可分配部分。
- [ ] 建立日级、持仓周期级差异表：漏费、重复、未实现变化、划转、未知归属、理论估算分别显示。
- [ ] 容忍值按币种精度、换算精度预先确定并版本化；不能看完差额再放宽。

**数值测试：**gross=10、funding=-1、commission=0.2时net=8.8；若Δunrealized=-3、external_cashflow=20，Δequity应25.8。重复导入不改变结果。

**验收：**验收样本内数量、费用、权益差异全部可解释；大范围历史缺失明确披露，禁止以“匹配率达到某百分比”直接替代金额核验。

### D4. 并行输出后再切换看板、回撤与月度分配

**现有消费者：** `dashboard/backend/services/metric_precompute_job.py`、`performance_metric_job.py`、`shared/performance_metrics.py`、`ai_tuner/allocation/pnl_collector.py`、`allocation_calculator.py`。
**新增测试：** `tests/test_accounting/test_metric_consumer_consistency.py`。

- [ ] 新旧账本并行输出，解释每个历史差异；原始账本只追加/更正事件，不破坏性覆盖旧事实。
- [ ] 固定绩效收益率的资本分母与外部资金流处理；配置分配额变化不能被当成投资盈利。
- [ ] 已实现收益/资金基数的月度排名与含未实现净值的总回报分别命名，避免混用。
- [ ] 数据质量不足时，月度分配输出“不可评价”；执行政策建议沿用上次已批准分配或人工基准，不能用0收益替代缺失自动排名。
- [ ] 看板、风控、调优、分配逐一核对口径并切换，提供版本选择和可回退读取路径；已持仓保护不依赖统计切换。

测试入口（本包新增测试全部落地后）：

```bash
python3 -m pytest tests/test_accounting/ -q
python3 -m pytest ai_tuner/allocation/tests/ tests/test_strategies/test_capital_allocation_enforcement.py -q
```

### 检查点 G3：收益可用于研究

- [ ] 账本同时给出exact/allocated/theoretical/unmatched构成，主结论有明确使用范围。
- [ ] 重复采集、部分退出和重启不重复记账；每个绩效数字可追溯到事实记录。
- [ ] 历史不可恢复期不用于主样本训练和策略排名结论；没有因为“修复了代码”就宣布历史PnL正确。

## 七、E：统一风险观察，再判断五类策略是否互补

### E1. 风险快照，不改变原仓位算法

**现有来源：** 五策略持仓状态、`shared/position_baseline.py`、`shared/capital_manager.py`。
**建议新增：** `shared/risk_snapshot.py`、`tests/test_accounting/test_risk_snapshot.py`。

每个同一时刻快照至少包含：actual_qty、mark_price、signed_notional、gross_notional、estimated_margin、exchange_margin（可得时）、stop_distance、planned_stop_loss、protection_status、equity、capital_allocation、source_time。

```text
signed_notional = 有符号数量 × mark_price × contract_size
portfolio_gross = Σabs(signed_notional)
portfolio_net   = Σsigned_notional
planned_stop_loss = abs(qty) × abs(entry_price - stop_price) × contract_size
```

- [ ] 区分新仓预计止损、现有仓位从当前价到保护价的损失，以及含手续费/滑点的压力损失。
- [ ] 没有有效保护单时 planned_stop_loss 不能写0，须unknown/unprotected；止损价也不是最大损失保证。
- [ ] 保证金估算与交易所实际值分列，不能把名义额/配置杠杆当强平风险的全部解释。

**验收：**同一额度的三种策略差异能被量化，毛敞口不被多空净额抵消掩盖；网格人工仓若未归属则保留unknown。

### E2. 既有组合的真实重叠与边际贡献

**建议新增：** `backtest/portfolio_evidence/analyze_existing.py`、`docs/research/strategy_remediation/portfolio_baseline.md`。

- [ ] 按策略、趋势/震荡、entry_mode、多空、币种、上市年龄、市场状态拆分收益和风险。
- [ ] 在共同有数据的日历上计算净收益相关、共同回撤、最差时段损失、同向持仓时长、市场Beta和换手；未运行/无数据不能当零收益。
- [ ] 原版/激进版同时信号与互斥拒绝分别统计；独立回测相加只是理想对照，实际组合须按共享额度和占位回放。
- [ ] 先比较不调整权重的真实组合，再做风险归一化的研究对照；不得把归一化结果称为原实盘。
- [ ] 用分块重采样/不同市场区间做不确定性评估；短样本低相关不等于稳定互补。

**验收：**回答哪些分支共同赚钱、共同亏损、占用相同资本；能解释费用与市场方向贡献；无法回答的部分明确标为样本不足。

## 八、F：组合执行与截面实验分开立项

### F1. 先固定组合执行契约，只接模拟交易适配器

**建议新增：** `shared/portfolio_execution.py`、`tests/test_portfolio_execution/test_state_machine.py`。接口合同先限定：portfolio_decision_id、leg_id、target_qty、filled_qty、avg_fill_price、client_order_id、deadline、risk_reservation_id、status。

状态建议：

```text
PLANNED → RESERVED → SUBMITTING → PARTIAL / BALANCED
PARTIAL → HEDGING / UNWINDING → BALANCED / CLOSED
任意无法确认的状态 → RECONCILING → 经交易所事实确认的状态
```

- [ ] 成组预留总额度，单币互斥只是其中一项；预留检查与写入必须同一事务/锁规则，防不同币并发超额。
- [ ] 下单意图与client_order_id持久化后提交；超时先查询/对账，不生成新ID盲目重下。
- [ ] 残余净敞口/允许偏离/最长未平衡时间作为显式参数；取值由实施者按成交能力和预算记录，不由示例猜测。
- [ ] 规定一腿失败后补对冲或减已成交腿的顺序、限额、超时；“没有双腿原子成交”是状态机必须处理的常态。
- [ ] 重启从持久化意图、真实订单和实际仓位恢复，而非相信最后内存状态。

### F2. 必须通过的故障矩阵

| 注入场景 | 必须观察到的结果 |
|---|---|
| 两腿均正常成交 | 数量与净敞口在事先限制内，正确释放未用预留 |
| 一腿拒绝，另一腿成交 | 明确对冲/减仓动作，不能将组合标成已平衡 |
| 部分成交后撤单 | 按实际剩余敞口处理，未成交计划量不当成仓位 |
| 请求超时但订单已成交 | 按原client_order_id查回，禁止重复开仓 |
| 撤单后迟到成交 | 更新事实账本并再次处置残余暴露 |
| 下单后进程崩溃 | 重启无重复单，恢复保护和预留 |
| 两个组合抢同币/总额度 | 最多一个合法占用，账户预算不穿透 |
| 风控暂停/行情过期 | 停新单，但仍可对账、撤单、减仓及维护保护 |
| 交易所状态不可查询 | 标unknown/reconciling，不能标closed并释放全部风险预留 |

```bash
python3 -m pytest tests/test_portfolio_execution/test_state_machine.py -q
```

**G4验收：**全部故障测试可重复；每个终态能由真实模拟成交与剩余仓位证明；这些通过仅表示模拟执行契约成立，不等于实盘可用。

### F3. 一个最小截面实验，避免同时搜索多个策略

**建议新增：** `backtest/cross_sectional_relative/{dataset.py,experiment.py,metrics.py}`、`docs/research/strategy_remediation/relative_return_experiment.md`。

- [ ] 先选定一个可被C数据完整支持的横向特征，记录经济假设、持有期、方向；例如拥挤度反转仅是候选，必须满足真实市值/费率数据要求才可选。
- [ ] 在看测试结果前固定币池、上市年龄、流动性约束、调仓时间、分组比例、权重上限、退出和费用模型；保存实验版本和全部尝试。
- [ ] 分开报告等金额多空与训练窗口估计Beta约束；Beta估计不得使用未来收益，样本不足要有明确回退/不交易规则。
- [ ] 含退市与不可交易时段；资金费率按实际结算计，滑点、手续费、成交容量、换手都进入净收益。
- [ ] 时间顺序划分训练/验证/最终测试；持仓标签跨界时做间隔隔离，最终测试不用来调参。
- [ ] 比较：现有组合、候选单独、现有组合+候选；同时给出理想成交与F1/F2约束下可成交结果。
- [ ] 成功标准在运行前写定：样本外扣费效果、市场暴露、压力损失、容量和对现有组合的边际改善；没有足够证据则结论为不成立/待更多样本，不强行选赢家。

**交付：**完整实验配置、数据hash、全部尝试列表、净收益与暴露曲线、成本敏感性、失败区间、可复现命令。盈利是研究结果，不是必须完成的开发验收条件。

## 九、每个变更包的交接模板

```text
任务编号与目标：
基线代码/配置/数据版本：
选定业务规则与影响分支：
修改文件与持久化格式变化：
失败复现输入与修复前结果：
修复后结果与实际执行的测试命令：
信号/仓位/收益口径变化：
旧状态兼容与回滚方法：
部署消费者与线上核验记录：
仍未解决的证据缺口：
```

每个包单独评审与验证；是否提交、合并和部署由执行者按项目授权处理。不要为减少批次而合并多个独立风险变化。

## 十、建议你现在启动的第一批

1. A1/A2：先对齐正在进行的其他修复与线上版本，落实三个业务决策：EMM窗口、新币反弹参考、市值缺失政策。
2. B1/B2：处理HRS信号可达性；与B5一起检查模式与时间字段，但保持独立变更集，先影子观察。
3. B3/B4：分别修复新币过滤与市值来源，配置阈值不顺手一起调。
4. 同时启动C1和D1只读采集，避免边修边丢失后续需要的事实。

第一批结束交付“行为可解释、证据开始持续保存”。不要此时就重分资金、优化组合权重或根据修复后几笔盈利决定上线新的相对收益策略。

---

## 十一、架构设计：R1/R2/R3 落地细节（backend-architect，2026-10-15）

> 本节承接「C1 运维接线（R2/R3）」与「D1 运维接线（R1/R3）」，把需求分析拍板落成可直接交 python-engineer 的实现方案。只出设计（模块/类/方法签名/完整 SQL/配置项/测试清单），不写实现代码。所有代码路径相对仓库根。

### 11.0 关键事实（已复核，实现时按此为准）

- D1 脚本 [export_execution_facts.py](../../../scripts/export_execution_facts.py)：`ExecutionFactExporter(account_id, api_client, sink=None, page_limit, max_pages)`；sink 为鸭子类型 + `getattr` 守卫，脚本调用 `await sink.upsert_fills` / `upsert_income` / `load_cursor(source,symbol,window_start_ms)` / `save_cursor(cursor)`，任一失败只 warning 不中断采集。`_run_cli`（L836）当前不组装 sink；`_build_client_from_env`（L771）只读 `BINANCE_API_KEY/BINANCE_API_SECRET`。
- `shared/database.py::DatabaseManager` 有 `execute/fetch_one/fetch_all/execute_ddl/execute_transaction`；`_validate_sql` 禁止 SQL 串含 `--`、`/*`、`*/`、`;` 及 `CREATE/GRANT/ALTER/DROP/TRUNCATE/...` 等词。**因此 sink 落库的 DML 串必须：无注释、无分号、无危险词。** 这与 `init-scripts/*.sql`（由 psql 直执行、允许 `--` 注释）是两条不同执行路径，勿混淆。
- `shared/decision_evidence.py`：`DecisionEvidence` / `FeatureEvidence` / `EvidenceSerializeResult`；`generate_cycle_id(strategy_id,symbol,direction,entry_mode,cycle_anchor)->[:16]`、`generate_decision_id(account_id,strategy_id,symbol,direction,entry_mode,cycle_id)->[:32]`（纯函数，无 now）；`serialize_decision(decision)->EvidenceSerializeResult(ok,payload,failed_records,errors)`（永不抛异常）。`payload["features"]` 是特征 list，每项键：`name/value/unit/event_time/available_at/availability/fetched_at/source/quality_flag/fallback_reason/lookback_actual`（时间为 ISO str 或 None）。
- btc_eth 决策点 [strategy.py::analyze()](../../../strategies/btc_eth/strategy.py#L712-L1086)：拒绝早退分支（约 10 处 `return analysis_result`）仅含 `{symbol,score,grade,reason}`，无 `direction`；接受分支 L1046 构造含 `direction`（`'LONG'/'SHORT'`）与 `market_state`（`'STRONG_TREND'/'RANGING'`）、`entry_price/initial_stop_loss/atr/leverage/position_size_usdt/quantity/position_ratio/timestamp/price_change_1h/tp1_price/tp2_price` 的 signal dict。**analyze() 唯一调用处在 [main.py::run_strategy()](../../../strategies/btc_eth/main.py#L168)（`result = await strategy.analyze(symbol)`，随后 `if 'direction' in result`）**。
- `main.py::initialize()` 从 `DATABASE_HOST/PORT/NAME/USER/PASSWORD` 构造并 connect DatabaseManager（[main.py:79-86](../../../strategies/btc_eth/main.py#L79-L86)）；策略实例持有 `self.db_manager`。
- K 线数据：`shared/kline_service.py::get_multi_timeframe_data` 每根 K 线 dict 含 `open_time`（毫秒 int）/`close`/`close_time`（[kline_service.py:146-156](../../../shared/kline_service.py#L146-L156)）。`open_time` 是"周期固定锚点"最可靠来源，但 analyze 内部变量，主循环拿不到。

---

### 11.1 R1 sink 设计（`shared/execution_facts_sink.py` 新增）

**模块/类**:`shared/execution_facts_sink.py`，类 `PostgresExecutionFactsSink`。

**构造函数（注入连接，便于离线单测注入 Fake）**：

```text
class PostgresExecutionFactsSink:
    def __init__(self, db_manager):
        # db_manager 为 DatabaseManager（或满足 execute/fetch_one/pool 的鸭子类型）
        self.db = db_manager
```

**四个方法签名（与脚本调用点严格对齐）**：

```text
async def upsert_fills(self, facts: List[FillFact]) -> None
async def upsert_income(self, facts: List[IncomeFact]) -> None
async def load_cursor(self, source: str, symbol: Optional[str], window_start_ms: int) -> Optional[CursorRecord]
async def save_cursor(self, cursor: CursorRecord) -> None
```

**raw 写入归属**：由 `upsert_fills` / `upsert_income` **顺带写**（不设独立对外方法）。fill 的 `raw_payload`/`payload_sha256` 在 `FillFact` 内已脱敏+指纹完成，sink 只负责落库，不重复脱敏。raw 复合键折叠约定：因 `execution_fact_raw` 主键是 `(source,entity_key,entity_id)` 且 `entity_id` 为 TEXT，需把业务复合键编码进 `entity_id`：

```text
fill   → source="userTrades", entity_key="fill",   entity_id=f"{account_id}:{symbol}:{trade_id}"
income → source="um/income",  entity_key="income", entity_id=f"{account_id}:{tran_id}"
```

**内部 SQL 常量（无注释、无分号、无危险词；参数走 `$n`）**：

fill upsert（冲突目标 = PK `(account_id,symbol,trade_id)`）：

```sql
INSERT INTO trading.execution_fact_fill
 (account_id, symbol, trade_id, order_id, side, qty, price, quote_qty,
  commission, commission_asset, realized_pnl, trade_time_utc)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
ON CONFLICT (account_id, symbol, trade_id) DO UPDATE SET
 order_id = EXCLUDED.order_id, side = EXCLUDED.side,
 qty = EXCLUDED.qty, price = EXCLUDED.price, quote_qty = EXCLUDED.quote_qty,
 commission = EXCLUDED.commission, commission_asset = EXCLUDED.commission_asset,
 realized_pnl = EXCLUDED.realized_pnl, trade_time_utc = EXCLUDED.trade_time_utc,
 fetched_at = now()
```

income upsert（冲突目标 = PK `(account_id,tran_id)`）：

```sql
INSERT INTO trading.execution_fact_income
 (account_id, tran_id, symbol, income_type, income, asset, info, trade_id, event_time_utc)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
ON CONFLICT (account_id, tran_id) DO UPDATE SET
 income = EXCLUDED.income, asset = EXCLUDED.asset, info = EXCLUDED.info,
 trade_id = EXCLUDED.trade_id, event_time_utc = EXCLUDED.event_time_utc,
 fetched_at = now()
```

raw upsert（冲突目标 = PK `(source,entity_key,entity_id)`）：

```sql
INSERT INTO trading.execution_fact_raw
 (source, entity_key, entity_id, raw_payload, payload_sha256)
VALUES ($1,$2,$3,$4,$5)
ON CONFLICT (source, entity_key, entity_id) DO UPDATE SET
 raw_payload = EXCLUDED.raw_payload, payload_sha256 = EXCLUDED.payload_sha256,
 fetched_at = now()
```

cursor upsert（冲突目标 = `UNIQUE NULLS NOT DISTINCT (source,symbol,window_start_ms)`）：

```sql
INSERT INTO trading.execution_fact_cursor
 (source, symbol, window_start_ms, window_end_ms, last_event_id,
  covered_until_ms, returned_count, page_complete, request_status,
  error_detail, retry_count, last_fetched_at)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11, now())
ON CONFLICT (source, symbol, window_start_ms) DO UPDATE SET
 window_end_ms = EXCLUDED.window_end_ms, last_event_id = EXCLUDED.last_event_id,
 covered_until_ms = EXCLUDED.covered_until_ms, returned_count = EXCLUDED.returned_count,
 page_complete = EXCLUDED.page_complete, request_status = EXCLUDED.request_status,
 error_detail = EXCLUDED.error_detail, retry_count = EXCLUDED.retry_count,
 last_fetched_at = now()
```

`load_cursor` SELECT（用 `IS NOT DISTINCT FROM` 匹配 `symbol=NULL`，否则 NULL 永不命中；不落库注释故无 `--`）：

```sql
SELECT source, symbol, window_start_ms, window_end_ms, last_event_id,
       covered_until_ms, returned_count, page_complete, request_status,
       error_detail, retry_count
FROM trading.execution_fact_cursor
WHERE source = $1 AND symbol IS NOT DISTINCT FROM $2 AND window_start_ms = $3
```

**`load_cursor/save_cursor` 与 `CursorRecord` 字段映射**（直映，无歧义）：

| CursorRecord 字段 | SQL 列 |
|---|---|
| `source` | `source` |
| `symbol` | `symbol`（可 NULL） |
| `window_start_ms` | `window_start_ms` |
| `window_end_ms` | `window_end_ms` |
| `last_event_id` | `last_event_id` |
| `covered_until_ms` | `covered_until_ms` |
| `returned_count` | `returned_count`（int；None→0） |
| `page_complete` | `page_complete`（bool） |
| `request_status` | `request_status` |
| `error_detail` | `error_detail` |
| `retry_count` | `retry_count`（int；None→0） |

**事务/批量策略**：`upsert_fills` 内——先对 fill 行批量 upsert，再在同一事务内写 raw（每 fill 一条 raw），最后提交；`upsert_income` 同理。v1 用 `self.db.execute_transaction([(sql, args), ...])` 逐行展开（复用现有安全封装、天然满足 `_validate_sql`、单事务无额外往返，Fake 只需实现 `execute_transaction`）；若实测单次采集行数达万级出现明显慢，再升级为 `conn.executemany`（属实现期优化，不改接口）。`save_cursor`/`load_cursor` 单行，用 `self.db.execute` / `self.db.fetch_one`。

**类型转换注意点**：
- NUMERIC 列（qty/price/quote_qty/commission/realized_pnl/income）：`str → Decimal`，`None`/空串 → `None`；`Decimal(str)` 异常时置 `None` + warning（脏值不阻塞）。
- 毫秒 → TIMESTAMPTZ（trade_time_utc/event_time_utc）：`datetime.fromtimestamp(ms/1000, tz=timezone.utc)`；`None` → `None`。
- JSONB（raw_payload）：`json.dumps(payload, sort_keys=True, ensure_ascii=False)` 得到 str 传参，与 `compute_sha256` 的 canonical 口径一致。

**错误处理**：四个方法内部各自 `try/except` 全吞，`structlog.warning`（中文），**不向上抛**。与脚本侧 `getattr`+try/except 形成双保险——sink 自身永不让采集/调用方崩溃。

---

### 11.2 R1 CLI 接线（`scripts/export_execution_facts.py` 修改）

**开关载体与默认值**：环境变量 `EXECUTION_FACTS_DB_WRITE`，truthy（`1/true/yes/on`，大小写不敏感）才启用；`false`/未设置 → 停用。**默认关闭**，保持现状 JSONL 行为不变。

**改造后的 `_run_cli` 结构草图（伪代码级，不写实现）**：

```text
async def _run_cli(args):
    start_ms, now_ms, symbols = _resolve_window(args)
    client, code = _build_client_from_env()
    if client is None: return code

    db_write = os.environ.get("EXECUTION_FACTS_DB_WRITE", "false").lower() in {"1","true","yes","on"}
    db_manager = None
    exporter = None
    try:
        sink = None
        if db_write:
            db_manager = DatabaseManager(host=..., port=..., database=..., user=..., password=...)  # 读 DATABASE_* 环境变量
            await db_manager.connect()
            sink = PostgresExecutionFactsSink(db_manager)  # import shared.execution_facts_sink
        exporter = ExecutionFactExporter(account_id=args.account_id, api_client=client,
                                          sink=sink, page_limit=args.page_limit)
        exit_code = 0
        if args.mode in ("fills","both") and symbols:
            exit_code |= await _run_cli_fills(exporter, symbols, start_ms, now_ms)
        if args.mode in ("income","both"):
            exit_code |= await _run_cli_income(exporter, start_ms, now_ms, args.income_type)
        return exit_code
    finally:
        if db_manager is not None:
            await db_manager.disconnect()   # 无论成败都释放连接池
```

要点：开关关闭时 `exporter` 不传 `sink`（沿用 JSONL）；开启时创建 pool + sink 并注入；`finally` 释放 pool；`DatabaseManager` 构造参数从 `DATABASE_HOST/PORT/NAME/USER/PASSWORD` 环境变量读取，与 `btc_eth/main.py` 口径一致（禁硬编码密码，实际值仍走 env）。

---

### 11.3 R2 btc_eth 接线（新增 2 模块 + `main.py` 单点改动）

**单一插入点（唯一收口，不侵入 analyze 内部任何 early-return）**：[main.py::run_strategy()](../../../strategies/btc_eth/main.py#L168) 中 `result = await strategy.analyze(symbol)` 之后、`analysis_results.append(result)` / `if 'direction' in result` 之前，调用一次旁路记录函数：

```text
result = await strategy.analyze(symbol)
await record_decision_evidence(result=result, symbol=symbol, strategy=strategy)  # 全吞异常，不影响后续
analysis_results.append(result)
```

**新模块与职责**：
- `shared/decision_evidence_sink.py`：`PostgresDecisionEvidenceSink(db_manager)`，`upsert_evidence(payload: dict)`（payload 来自 `serialize_decision(...).payload`），负责决策级 + 特征级两条 DML（SQL 见下）。
- `strategies/btc_eth/evidence_recorder.py`：`DecisionEvidenceRecorder(strategy, sink, config)`，方法 `async record(result, symbol)`，负责从 analyze 返回值组装 `DecisionEvidence` + 特征、算稳定 id、调 `serialize_decision`、再调 sink 写库。

**accepted/rejected 判定与字段填充**：
- `accepted = ('direction' in result)`；`direction = result.get('direction', 'FLAT')`（无 direction → 决策方向 `'FLAT'`，符合 DDL CHECK）。
- `rejection_reason = result.get('reason')`（rejected 时）；accepted 时 `rejection_reason = None`。
- `entry_mode`：accepted 时由 `result['market_state']` 映射 `'RANGING'→'ranging'`、`'STRONG_TREND'→'trend'`；rejected 时无 market_state → `'unknown'`（需固定字符串以稳定 id）。
- `strategy_id` 固定 `"btc_eth"`；`account_id` 从配置 `decision_evidence.account_id` 读，缺省 fallback 到 env `TRADE_ACCOUNT_ID`，再 fallback `"default"`（在白名单内，不触发脱敏）。

**cycle_anchor 口径**：用"向下取整到最近 1h 的起始毫秒"作为周期固定锚点（确定性、非 now；同一周期内重试得到相同 id）——新增纯函数 `_hourly_anchor_ms(ts) = int(ts * 1000) // 3600000 * 3600000`，从决策时刻计算。主循环拿不到 analyze 内部 `klines['1h'][-1]['open_time']`，故 v1 用取整锚点；后续若需精确锚到已关闭 K 线 open_time，可在 analyze signal 里追加一个只读字段 `kline_open_time_1h`（不改决策逻辑），口径变更时需记录 decision_id 变化。

**config 开关位置**（`strategies/btc_eth/config.yaml` 新增顶层段落，默认关闭）：

```yaml
decision_evidence:
  enabled: false                 # 旁路只读证据采集开关，默认关闭
  account_id: ""                 # 空=回退 env TRADE_ACCOUNT_ID，再回退 default
  db_timeout_seconds: 3.0        # 写库超时（旁路不得拖慢主循环）
```

读取方式：`main.py::initialize()` 里 `_dev = config.get('decision_evidence', {})`，`enabled` 为 True 时用现存 `db_manager` 构造 `PostgresDecisionEvidenceSink`、再构造 recorder 挂到 strategy（或主循环内使用）。**enabled False 时不构造 sink，record 直接返回**。

**失败旁路保证**：`record_decision_evidence` 整段 `try/except` 全吞，只 `logger.warning`；内部可包 `asyncio.wait_for(..., timeout=config['decision_evidence']['db_timeout_seconds'])`；**绝不改变 `result` 内容、绝不参与 `if 'direction' in result` 判定、绝不抛给主循环**。

**decision_evidence 两条 DML（无注释、无分号；`updated_at` 显式写 now()）**：

```sql
INSERT INTO trading.decision_evidence
 (account_id, strategy_id, decision_id, cycle_id, decision_time_utc, code_hash,
  config_hash, entry_mode, universe_snapshot_id, symbol, direction, score_components,
  thresholds, planned_qty, planned_notional, margin_limit, margin_limit_source,
  accepted, rejection_reason, updated_at)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19, now())
ON CONFLICT (decision_id) DO UPDATE SET
 account_id = EXCLUDED.account_id, strategy_id = EXCLUDED.strategy_id,
 cycle_id = EXCLUDED.cycle_id, decision_time_utc = EXCLUDED.decision_time_utc,
 code_hash = EXCLUDED.code_hash, config_hash = EXCLUDED.config_hash,
 entry_mode = EXCLUDED.entry_mode, universe_snapshot_id = EXCLUDED.universe_snapshot_id,
 symbol = EXCLUDED.symbol, direction = EXCLUDED.direction,
 score_components = EXCLUDED.score_components, thresholds = EXCLUDED.thresholds,
 planned_qty = EXCLUDED.planned_qty, planned_notional = EXCLUDED.planned_notional,
 margin_limit = EXCLUDED.margin_limit, margin_limit_source = EXCLUDED.margin_limit_source,
 accepted = EXCLUDED.accepted, rejection_reason = EXCLUDED.rejection_reason,
 updated_at = now()
```

```sql
INSERT INTO trading.decision_feature_evidence
 (decision_id, name, value, unit, event_time, available_at, availability,
  fetched_at, source, quality_flag, fallback_reason, lookback_actual)
VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
ON CONFLICT (decision_id, name, source) DO UPDATE SET
 value = EXCLUDED.value, unit = EXCLUDED.unit,
 event_time = EXCLUDED.event_time, available_at = EXCLUDED.available_at,
 availability = EXCLUDED.availability, fetched_at = EXCLUDED.fetched_at,
 quality_flag = EXCLUDED.quality_flag, fallback_reason = EXCLUDED.fallback_reason,
 lookback_actual = EXCLUDED.lookback_actual
```

注意：特征 `value` 列是 TEXT，`payload["features"][i]["value"]` 可能为 dict/list，落库前 `_to_text(v) = v if isinstance(v,str) else json.dumps(v, ensure_ascii=False, default=str)`。`event_time/available_at/fetched_at` 为 ISO str 或 None，asyncpg 对 timestamptz 接受 ISO str（或由 recorder 直接传 datetime）。

**组装顺序（recorder.record 内，伪代码级）**：

```text
try:
    anchor = _hourly_anchor_ms(decision_time)
    direction = result['direction'] if 'direction' in result else 'FLAT'
    entry_mode = 由 result.get('market_state') 映射；rejected 用 'unknown'
    cycle_id = generate_cycle_id("btc_eth", symbol, direction, entry_mode, anchor)
    decision_id = generate_decision_id(account_id, "btc_eth", symbol, direction, entry_mode, cycle_id)
    decision = DecisionEvidence(account_id=..., strategy_id="btc_eth", decision_id=..., cycle_id=...,
        decision_time_utc=<带tz datetime>, entry_mode=..., symbol=symbol, direction=...,
        score_components={...}, thresholds={...}, accepted=..., rejection_reason=...,
        planned_qty=float(result.get('quantity')) when float-able else None,
        planned_notional=float(result.get('position_size_usdt')) when float-able else None)
    decision.features = [FeatureEvidence(name='score', value=result.get('score', 0), ...),
                         FeatureEvidence(name='grade', value=result.get('grade'), ...), ...]  # 逐键 get 守卫
    serialized = serialize_decision(decision)        # 永不抛异常
    if serialized.payload is not None:
        await asyncio.wait_for(sink.upsert_evidence(serialized.payload), timeout=db_timeout_seconds)
except Exception as exc:
    logger.warning("决策证据记录失败，不影响主循环", symbol=symbol, error=str(exc)[:200])
```

特征清单（最小可落地，均由 result 键存在时 `get` 守卫，缺失跳过）：`score`、`grade`、`market_state`（accepted 时）、`entry_price`、`initial_stop_loss`、`atr`、`leverage`、`position_size_usdt`、`quantity`、`position_ratio`、`price_change_1h`、`tp1_price`、`tp2_price`。`code_hash`/`config_hash` v1 置 None（后续再引入 config 内容 hash，避免本轮引入文件 hash 复杂度）。

---

### 11.4 R3 DDL 迁移（init-scripts）

**新文件**：
- `database/postgres/init-scripts/10-decision-evidence.sql`
- `database/postgres/init-scripts/11-execution-facts.sql`

**统一改造口径**（对齐 [08-position-claims.sql](../../../database/postgres/init-scripts/08-position-claims.sql) 风格）：
1. 顶部：`CREATE SCHEMA IF NOT EXISTS trading;` + `GRANT ALL PRIVILEGES ON SCHEMA trading TO trading_user;`（自包含，幂等）。
2. 表用 `CREATE TABLE IF NOT EXISTS`；索引用 `CREATE INDEX IF NOT EXISTS`。
3. **去掉 `BEGIN;` / `COMMIT;`**（init-scripts 约定不包裹事务）。
4. 每表补 `GRANT SELECT, INSERT, UPDATE, DELETE ON <table> TO trading_user;`。
5. 序列授权（仅自增列存在）：
   - `10-...`: `GRANT USAGE, SELECT ON SEQUENCE trading.decision_feature_evidence_id_seq TO trading_user;`
   - `11-...`: `GRANT USAGE, SELECT ON SEQUENCE trading.execution_fact_cursor_id_seq TO trading_user;`
   （`decision_evidence`/`execution_fact_fill`/`execution_fact_income`/`execution_fact_raw` 主键均为业务键、无 BIGSERIAL 序列，不需序列授权。）
6. 结尾 `DO $$ BEGIN RAISE NOTICE '...表创建完成: trading.xxx'; END $$;`。
7. 保持幂等；部署按编号顺序 `psql --set ON_ERROR_STOP=on -f` 执行，靠 `IF NOT EXISTS` 幂等。

**源 migrations 文件处理**：`migrations/2026-10-15-*-evidence.sql` 保留作历史（**不删、不 apply**），在其文件头注释追加一行"已迁入 init-scripts/10-*/11-*，init-scripts 成为权威执行路径"。避免与仓库中其他未提交改动交叉。

---

### 11.5 测试方案（全部离线，不连真实 PG、不调交易所）

| 编号 | 用例目标 | 方式 |
|---|---|---|
| T1 | sink 幂等 SQL 断言：确认 `upsert_fills/upsert_income/save_cursor` 生成的 SQL 串**不含 `--`、`/*`、`;`、`CREATE/GRANT/ALTER`**，冲突目标/更新列正确、参数映射正确、`upsert_fills` 顺带写 `execution_fact_raw` | Fake db 记录 `(query, args)`，断言字符串与参数 |
| T2 | 脱敏：sink 只负责 `json.dumps`（sort_keys）落 raw，不引入敏感字段、不重复脱敏 | 构造含 `raw_payload` 的 `FillFact`，验证 raw 参数里 payload 已为无密钥 dict |
| T3 | cursor 往返：`save_cursor`→`load_cursor` 返回等价 `CursorRecord`（含 `symbol=None` 分支，验证 `IS NOT DISTINCT FROM` 语义） | Fake 内存 store + 断言 SQL 含 `IS NOT DISTINCT FROM` |
| T4 | CLI 开关行为：`EXECUTION_FACTS_DB_WRITE` 关闭→exporter 不注入 sink（仅 JSONL）；开启→注入 Fake sink 并调用 `upsert_fills/upsert_income/save_cursor`；finally 释放 pool | pytest + monkeypatch env + fake client |
| T5 | btc_eth 旁路开关：`config['decision_evidence']['enabled']=False` 时 record 不调用 sink；accepted 与 rejected 两种 result 分别得到 `accepted=True/False`、正确 `direction/entry_mode/rejection_reason` | Fake sink，断言调用次数与参数 |
| T6 | 失败不影响决策：Fake sink.`upsert_evidence` 抛异常 → `record` 吞掉、`result` 内容不变、主循环继续 | 断言无异常抛出 + result 引用不变 |
| T7 | 已有回归：`tests/test_shared/test_decision_evidence.py`（19 用例）、`tests/test_accounting/`（65 用例）继续全绿 | `python3 -m pytest` |

**R3 本地校验方式（无本地 PG）**：
1. 静态检查：人工/脚本核对两个 init-scripts 文件——无 `BEGIN/COMMIT`、`CREATE SCHEMA IF NOT EXISTS`+`GRANT`、`CREATE TABLE/INDEX IF NOT EXISTS`、表级 GRANT 完整、序列名精确、结尾 `DO $$` RAISE NOTICE、编号 10/11 不撞现有 01-09、`psql` 语法无非法 token。
2. 幂等性人工核对：两条 GRANT/CREATE 反复执行无副作用（IF NOT EXISTS / 重复 GRANT 幂等）。
3. 若本地有 Docker，可 `docker run -e POSTGRES_PASSWORD=... postgres:15-alpine` + `psql -f` 双跑验证幂等（可选，不作为强制验收）。

**不引入新依赖**：Fake db/sink 均用 `AsyncMock`/简单内存对象实现，沿用项目现有 pytest 组织方式。

---

### 11.6 风险与坑（全量清单）

1. **income 非统一账户静默空返**：`get_income_history` 在 `use_unified_account=False` 时返回 `[]` 且只 warning，sink 无从区分"真空"与"被拦"，while 循环把空数组判 `page_complete=TRUE` 正常收尾。调用方必须用统一账户客户端（既有已知风险，见 D1）。
2. **jsonb/numeric 类型**：NUMERIC 由 `str→Decimal`，脏数值需降级 None 不阻塞；JSONB 需 `json.dumps(sort_keys=True)`；特征 `value` 列 TEXT，dict/list 需 `_to_text` 化。
3. **`_validate_sql` 对注释/分号/危险词限制**：sink DML 串必须无 `--`、无 `/*`、无 `;`、无 `CREATE/GRANT/ALTER`；DDL 文件（psql 直执行）仍可用 `--` 注释——两条执行路径勿混。
4. **cursor `NULLS NOT DISTINCT`**：`load_cursor` 必须 `IS NOT DISTINCT FROM $2` 匹配 `symbol=NULL`；`ON CONFLICT (source,symbol,window_start_ms)` 依赖 PostgreSQL 15+。
5. **`updated_at` 无触发器**：`decision_evidence` 每次 UPDATE 必须显式 `updated_at = now()`，否则停留在建行默认值。
6. **并发写**：`decision_feature_evidence` 的 `UNIQUE NULLS NOT DISTINCT (decision_id,name,source)` 在多进程/重试并发下靠 `ON CONFLICT DO UPDATE` 覆盖，需确认 PG 版本支持。
7. **`execution_fact_raw.entity_id` 复合键折叠**：`fill → account_id:symbol:trade_id`、`income → account_id:tran_id`，分隔符/顺序必须与 sink 内唯一对应，避免跨 account/symbol 冲突或反解歧义（账号/symbol/trade_id 均不含 `:`，安全）。
8. **cycle_anchor 取整口径**：v1 用"最近 1h 向下取整毫秒"，同周期重试得同 id；若后续改为精确 `kline_open_time_1h`，decision_id 会变，需记录口径变更。
9. **account_id 来源未统一**：btc_eth 主循环无 account_id，需从 `decision_evidence.account_id` → env `TRADE_ACCOUNT_ID` → `"default"` 兜底，避免空值进入 `generate_decision_id`。
10. **旁路不得影响决策/采集**：sink 四方法 + recorder.record 双层全吞异常，数据库慢时用 `db_timeout_seconds` 兜底，禁止 join 到 `if 'direction'` 判定。
11. **性能**：v1 逐行 `execute_transaction`；万级行时升级 `executemany`（不改变接口/魔术签名）。
12. **迁移编号**：10/11 与现有 01-09 连续；两个文件都 `CREATE SCHEMA IF NOT EXISTS trading` 自包含，避免依赖 01 的执行顺序。
