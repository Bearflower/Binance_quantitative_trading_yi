# 需求定义与验收标准：第三轮复审后修复项 R10/R11/R12 + RK1/RK2（2026-10-17）

> 状态：**已实现 + 已通过回归（收口，2026-10-17）**。逐条闭环状态与证据见 **§7 收口**。
> 本文原为「需求 + 验收标准 + 先红后绿回归场景」阶段产物；收口补记只如实记录落地现状与能力限制，**不顺带宣称净收益对账 / 部署验收等排除项完成**。
> 权威输入：
> - 第三轮复审报告：[strategy_reaudit_2026-10-17/README.md](../../reviews/strategy_reaudit_2026-10-17/README.md)（只读，禁改）
> - 上轮需求与验收：[requirements_R7-R9_2026-10-16.md](requirements_R7-R9_2026-10-16.md)（R7/R8/R9 口径与 RK 披露项承袭自此处）
> - 上轮架构设计：[architecture_R7-R9_2026-10-16.md](architecture_R7-R9_2026-10-16.md)
> - 业务决策记录：[decisions.md](decisions.md)

---

## 1. 背景与范围

### 1.1 本轮目标

第三轮复审判定：上轮 R7/R8/R9 的**直接根因均已处理**，但发现 **1 个 P1 代码缺陷 + 4 项待闭环事项**。顾问已确认修复方向。本轮**只修这 5 项**，逐条满足「问题定义 → 根因 → 验收标准 → 先红后绿回归场景」的可判定闭环，并把对应场景固化为回归用例。

**关键约束：不重新设计已通过的 R8（新币重算数量）与 R9（唯一 signal_id 更新），仅保留其既有回归测试作为防回归基线。**

本轮 5 项清单：

| 编号 | 对应任务号 | 优先级 | 分组 | 问题一句话 |
|---|---|---|---|---|
| R10 | 1 | P1 | execution_facts | 成交分页右边界静默漏数：满页到达窗口结束毫秒即宣布采集完整 |
| RK1 | 2 | P2（上线必验） | HRS | `entry_mode`/`signal_origin` 两列未确认落地，写入失败无可识别告警 |
| R11 | 3 | P2（策略验收前） | HRS 评分引擎 | `test_技术总分不足不应入场` 真实断言失败（三轨拆分未提交） |
| RK2 | 4 | P2（统计使用前） | HRS 统计 | 超时信号开始落库，下游口径可能变化，需定义并锁定统计口径 |
| R12 | 5 | P2（收益研究前） | income | income 同毫秒超过一页仍无法完整采集，需设计补采方案或持续标记缺口 |

### 1.2 范围边界（明确排除项，本轮不得宣称完成）

| 排除项 | 说明 |
|---|---|
| 净收益对账 / 权益守恒 / 全策略归属账本 | 属后续研究 + 验收，仍排除（R12 只处理「补采/缺口标记」，**不**据此得出净收益结论） |
| MTPCS 趋势 / 回归分支定位、分支独立净收益 | 研究/验收工作，仍排除 |
| R8（新币重算数量）、R9（唯一 ID 更新）的重新设计 | 已通过，**不重设计**，仅保留回归测试防倒退 |
| push main / 部署 / GHCR / 服务器 | 本轮不触发部署（RK1 的「库表核对」标注**上线时**执行，本轮不执行） |
| apply 迁移 / 连接生产库 | 不执行任何 DB 迁移，不连生产库 |
| 调用真实交易所 | 所有测试走 mock / Fake API / 合成行情 / 内存 DB |

### 1.3 硬约束（全程遵守）

- 不 push main、不部署、不 apply 迁移、不连生产库、不调真实交易所。
- 工作区存在大量其他未提交改动，**只允许触碰本轮相关文件**，不得顺带 reformat / 重构无关文件。
- 代码注释与日志一律中文。
- 禁止硬编码业务参数（阈值、时长、限额、分页数一律读配置或由算法推导）。
- 禁止重复代码：连续 5 行以上同构、或逻辑相同仅变量名不同，必须抽公共函数（尤其 R10 与 R12 共用的收尾/游标逻辑必须收敛为唯一实现）。
- 禁止幽灵参数 / 死字段：函数形参与结构体字段必须被真实使用。
- 单函数 ≤ 50 行；单行 ≤ 120 字符。

---

## 2. 逐条需求

### R10 [P1] 成交分页右边界不得静默漏数（属 execution_facts 组，核心业务逻辑）

**问题描述**
`_resolve_page_outcome`（[export_execution_facts.py L908-L912](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L908-L912)）把 `new_advance.time_ms >= window_end_ms` 当作**独立完成条件**（`ok_or_window_end`），即使本页已满（`page_complete = len(rows) < self.page_limit` 为 False）。当窗口最后一毫秒还有多笔记录时，「最大时间已到边界」并不能证明「该毫秒已全部取完」。

**独立复现**：窗口 `[1, 1000]`，5 笔成交全部 `time=1000`，`page_limit=2`。第一页返回 `id=1,2`；结果只请求一次、采得 2 笔、`page_complete=True`、`errors=[]`，其余 3 笔静默丢失。

**位置**
- [export_execution_facts.py#L908-L912](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L908-L912)（`ok_or_window_end` 完成条件，缺陷主体）
- [export_execution_facts.py#L926-L943](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L926-L943)（`_filter_rows_within_window` 客户端右边界过滤，`t <= window_end_ms`，闭区间口径）
- [export_execution_facts.py#L447-L448](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L447-L448)（fills `api_call_factory`：`end_time=window_end_ms if fid is None else None`）

**根因**
- 时间边界口径在「请求 / 过滤 / 游标收尾 / 测试」四处不一致：客户端过滤与币安接口用**闭区间**（`time <= window_end_ms`，币安 PM UM `userTrades` 的 `endTime` 是**包含边界**），但收尾判定却把 `time_ms >= window_end_ms` 单独当作「已排空」信号，把「到达右边界」误当成「右边界内的记录已全部取回」。
- 现有测试未覆盖「结束毫秒超过一页」：`test_last_time_equals_window_boundary` 的数据实为 500/800/999（无记录恰落右边界）；客户端边界测试仅有 2 笔落结束毫秒（恰好一页）。Fake 时间查询用 `< end_time`，与客户端 `<= end_time` 口径也不一致。

**修复方向（仅供编写验收，不写实现）**
沿用当前**闭区间**口径（与币安 `endTime` 包含边界、客户端 `time <= window_end_ms` 一致）。满页且到达窗口结束毫秒时**仍需续传**（用 `fromId` 继续，fills 已有 ID 续传路径），直到某页返回**少于 `limit`** 才确认该毫秒已排空。同时**保留「原始页是否满页、是否实际遇到越界记录」信息**，供收尾判定与下游审计，不得把「遇到过越界记录」误当作「已排空」。

**期望行为（修复口径）**
1. 结束毫秒有 `limit` 笔时：恰好取满、正确收尾 `page_complete=True`，且**不发起多余请求**。
2. 结束毫秒有 `limit+1` 笔或跨多页时：**全部取回**，直到某页返回 `< limit` 才 `page_complete=True`（闭区间内同毫秒记录零遗漏）。
3. 结束毫秒后还有越界记录（`time_ms > window_end_ms`）时：越界记录被客户端过滤，不污染游标与去重，收尾判定**只依据窗口内记录是否排空**，不因越界记录存在而误判完成。
4. 采到边界**中途失败后恢复**：保存游标后恢复，能继续取回剩余同毫秒记录，不重不漏。
5. **缺数据时不得返回 `page_complete=True`**：任一页请求失败、或游标/sink 写入失败时，`page_complete` 必须为 False 且 `errors` 非空，游标不推进。
6. **时间边界口径统一**：请求、客户端过滤、游标收尾、测试 Fake 四处统一为闭区间（或本项目明确选定并一致地改为半开区间，二选一），不得混用。本文默认选闭区间。

**验收标准（可判定）**
- 构造合成窗口 `[1,1000]`：
  - 结束毫秒恰好 `limit` 笔 → 取回全部、`page_complete=True`、请求次数符合预期（不空转一页）。
  - 结束毫秒 `limit+1` 笔 → 取回 `limit+1` 笔、`page_complete=True`。
  - 结束毫秒跨多页（如 `limit=2`，共 5 笔 `time=1000`）→ 取回 5 笔、`page_complete=True`。
- 构造窗口边界后有越界记录 → 采集结果每笔 `time_ms <= window_end_ms`，越界记录不进 `collected`，`page_complete` 只由窗口内记录排空与否决定。
- 构造「取到边界中途失败」→ 断言 `page_complete=False`、`errors` 非空、游标未越过失败页；恢复后能续取剩余。
- 时间口径一致性断言：所有断言与 Fake 的过滤判据统一用闭区间，Fake 不得再用 `< end_time` 冒充 `<= end_time`。
- 复用性：fills 与 income 共用收尾/游标逻辑唯一实现，不复制同构分支。

---

### RK1 [P2，上线必验] HRS 归属列未确认落地 + 写入失败无可识别告警（属 HRS 组）

**问题描述**
R9 在 `_ensure_db_schema` 新增了 `entry_mode` / `signal_origin` 两列（建表声明 + 幂等 `ADD COLUMN IF NOT EXISTS`），但**当前仅代码声明、未部署、未 apply**，线上 HRS 仍跑旧代码，两列尚不存在。同时 `_log_signal_to_db` 与 `_update_signal_resolution` 的失败路径**只有 `logger.warning`**，用户明确要求「不能只有不易发现的日志」。

**位置**
- [strategy.py#L3036-L3037](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3036-L3037)（建表两列声明）
- [strategy.py#L3062](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3062)、[strategy.py#L3065](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3065)（幂等 `ADD COLUMN IF NOT EXISTS`）
- [strategy.py#L3768-L3786](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3768-L3786)（`_log_signal_to_db` INSERT 写入两列）
- [strategy.py#L3797-L3799](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3797-L3799)、[strategy.py#L3819-L3822](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3819-L3822)、[strategy.py#L3833-L3834](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3833-L3834)（三处失败路径仅 `logger.warning`）

**根因**
- 归属列落地受「上线部署」这一关口卡住：幂等 DDL 只在代码里，尚未生效；本轮不部署、不连生产库，属「冷隔离」状态，因此**库表核对必须标注上线时执行**。
- 失败路径无「可识别」的告警/监控：`logger.warning` 会混入大量普通日志，用户无法可靠发现「信号落库失败 / 归属字段缺失 / 结果更新失败」，可能造成归属证据静默丢失。

**期望行为（修复口径）**
1. 两列 `entry_mode` / `signal_origin` 在**部署后**实际存在（`ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 幂等生效，hrs 容器启动日志出现「数据库 Schema 初始化完成」且两条 ALTER 未报错）。
2. 生产信号与影子信号均能在 INSERT 时**写入对应字段**（`entry_mode` 写入场模式，`signal_origin` 写信号来源），结果按**唯一 `signal_id`**更新（R9 已通过口径，不重设计）。
3. **写入失败有可识别的告警或监控**：将「信号落库失败」「信号缺少 signal_id 跳过更新」「信号结果更新失败」由纯 `logger.warning` 升级为**可识别的告警渠道**（复用项目既有飞书通知 `shared/notification.py` 或等价告警，不新造轮子、不硬编码目标），失败需含 `symbol`/`signal_id`/`error` 等可定位信息，且可用于监控指标（如失败计数器）而非仅一条易淹没的日志。

**验收标准（可判定）**
- 上线时库表核对（**本轮不执行，标注为上线步骤**）：`hrs.hrs_signals` 存在 `entry_mode`、`signal_origin` 两列，`hrs_signals` 表中该两列可写入非空值。
- 离线断言（本轮可做）：`_log_signal_to_db` 失败、`_update_signal_resolution` 的 `signal_id is None` 与 UPDATE 异常，三条路径均触发**可识别的告警调用**（在 mock 注入下断言持告警通道被调用、告警内容含定位信息），而非仅 `logger.warning`。
- 离线断言：生产与影子信号 INSERT 均写入非空 `entry_mode` / `signal_origin`；结果更新仍按 `WHERE id=$1`（引用 R9 既有回归，勿重写）。
- 离线断言：告警通道的开关/目标不硬编码（读配置或复用既有通知封装），关闭告警时不影响主流程执行。

---

### R11 [P2，策略验收前] 评分引擎真实断言失败须定位根因并纠正（属 HRS 评分引擎组，核心业务逻辑）

**问题描述**
失败用例 [test_scoring_engine.py#L373-L389](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/tests/test_scoring_engine.py#L373-L389) `test_技术总分不足不应入场`：期望 `technical_score < 4.0` 时 `should_entry` 为 False，**当前返回 True**。复审指出 `scoring_engine.py` 存在**整轮未提交的三轨（standard / emm / lv_rm）拆分重构**，重构后的行为与已提交测试及策略定义不一致。

**位置**
- [scoring_engine.py#L1490-L1620](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/scoring_engine.py#L1490-L1620)（`should_entry`；技术门槛判定在 L1562-L1570，`lv_rm` 直接 `return True` 在 L1586-L1594）
- [scoring_engine.py#L1392-L1462](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/scoring_engine.py#L1392-L1462)（`score()` 主入口，三轨拆分与 shadow 链）
- [test_scoring_engine.py#L373-L389](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/tests/test_scoring_engine.py#L373-L389)（失败用例）

**根因（待编码组在「策略定义 vs 当前行为」之间定位，两点其一）**
- 情形 a（可能）：三轨拆分重构改变了 standard 链的 `technical_score` 计算或 `entry_mode` 判定，使「技术分不足」的信号未被拦截 → 行为**偏离策略定义**，须修代码。
- 情形 b（可能）：三轨拆分系**已批准的行为变化**（如技术门槛下放至各轨、standard 链门槛调整），则须更新测试与文档反映新口径。
- **禁止**：仅为使测试通过而改写期望值；必须先定位根因，再决定「修代码」或「改测试+文档」。

**期望行为（修复口径）**
1. 失败根因明确（落到具体函数/分支/字段，不泛泛而谈）。
2. 若行为偏离策略定义（HRS 标准模式技术总分 < 门槛即不入场，见策略文档 [混合反转策略（HRS）V2.5.1](file:///Users/yl/vscode/Binance_quantitative_trading/docs/requirements/HRS/混合反转策略（HRS）V2.5.1（融合修复版）完整文档.md)）→ 修代码使 `should_entry` 恢复正确拦截。
3. 若属已批准行为变化 → 更新测试与文档，并**记录该口径变化的批准来源（决策编号/日期）**，不匿名改期望。
4. **标准评分与影子评分互不影响**：standard 主结果与 EMM/半EMM/LV-RM 影子链各自独立判定，修 standard 拦截不得连带改变影子链的输出形态（承 R1/D2/D8 口径）。

**验收标准（可判定）**
- 提供根因定位结论 + 对应证据（失败输入下 `entry_mode`、`technical_score`、`total_score`、`should_entry` 的实际取值与走到的分支）。
- 分支结论一（修代码）：`test_技术总分不足不应入场` 由红转绿，其余 `test_scoring_engine` 既有用例不回归（`test_基础形态评分不足不应入场`、`test_一票否决不应入场` 等仍绿）。
- 分支结论二（改测试+文档）：给出批准来源；测试与文档同改；并新增一条「标准与影子互不影响」的断言。
- 无论哪一分支：不得出现「绕开断言、mocking，或把 `<` 改成 `<=`/`>` 之类的阵痛式改期望」。

---

### RK2 [P2，统计使用前] 超时信号落库后的下游统计口径必须定义并锁定（属 HRS 统计组）

**问题描述**
R9 把「落库 / 超时过滤」顺序对调后，**超时信号现在也会被落库**到 `hrs.hrs_signals`（`status='triggered'` → 按 `signal_id` 精准标 `timeout`）。下游凡按 `hrs.hrs_signals` 统计（信号总数、超时占比、归属、胜率）的口径**都可能变化**。当前**无人排查过读取方**，也未定义「触发数 / 超时数 / 影子数 / 真实执行数 / 执行率」的权威口径。

**位置**
- [strategy.py#L3768-L3786](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3768-L3786)（信号落库，超时信号现已被落库）
- 读取方待排查（本轮排查范围含 display/dashboard、研究脚本、任何含 `hrs_signals` 的 SELECT/统计 SQL——当前代码树内 `hrs_signals` 仅见 strategy.py 写入与测试、文档，**未见 dashboard/研究脚本直接 SELECT**，但仍需以排查结果确认，见 §6）

**根因**
- 落库顺序对调是 R9 修复「超时信号需按唯一 ID 标记」的必要代价，但未同步排查下游统计方，导致「信号行数」与「真实交易结果」两个概念可能被混用。

**期望行为（修复口径，本轮只做「必要性维护」不新造统计系统）**
1. **排查所有读取 `hrs.hrs_signals` 的查询、看板、研究脚本**，列出清单与读取字段。
2. **分别定义并文档化**以下口径，使「信号行数」与「真实成交」严格区分：
   - 触发数（triggered 信号行数）
   - 超时数（`resolution='timeout'` 行数，R9 后开始入表）
   - 影子数（`resolution='shadow'` 或 `signal_origin='shadow_scoring'` / 空池 LV-RM 影子）
   - 真实执行数（`resolution='executed'` 且对应**真实成交/持仓**）
   - 执行率（分子只用「真实执行数」，分母口径显式声明）
3. **交易胜率必须依据真实交易结果**，不能直接依据信号行数或 `resolution='executed'` 的行数——`executed` 只表示「信号被标记执行」，须与成交事实/持仓结果挂钩后才可作为胜率分子（承 R9 澄清：`executed` 不等于伪造成交，但也不自动等于真实盈亏）。
4. 用**含生产 / 影子 / 超时 / 冲突信号的固定样本**验证统计口径：新增超时记录**不得被算作成交**，影子记录**不得被算作真实执行或成交**。
5. 新旧数据比较需**明确口径差异**：切时序前后的「信号总数 / 超时占比」不可直接同比，需标注「超时信号自何部署起才落库」。

**验收标准（可判定）**
- 产出读取方排查清单（文件/表/查询用途），零读取方也须写明「已排查、无读取方」的结论与依据。
- 输出一份口径定义（可并入 `decisions.md` 或本文附录），上述 5 个口径一一明确，胜率分子分母显式。
- 固定样本验证（离线、合成数据）：样本含生产 executed、影子 shadow、超时 timeout、冲突 conflict 各若干 → 断言「超时数」不进入「真实执行数」与「胜率分子」；影子数不进入「真实执行数」。
- 新增超时记录不得被任何「成交 / 胜率」统计计入（运行时断言：统计函数对 `resolution='timeout'` 行计数为 0 或显式排除）。

---

### R12 [P2，收益研究前] income 同毫秒超过一页仍无法完整采集，需补采方案或持续标记缺口（属 income 组，核心业务逻辑）

**问题描述**
`export_income` 走 `um/income`，其 `api_call_factory` **忽略续传游标 `_fid`**，每页都用 `start_time=cs, end_time=window_end_ms` 重新按时间定位（[export_execution_facts.py#L489-L492](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L489-L492)），`um/income` 无 `fromId` 类事件游标，同毫秒多笔超过 `limit` 时**无法翻页续取**，也无法用 `tranId` 续传。

**位置**
- [export_execution_facts.py#L489-L492](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L489-L492)（income `api_call_factory`，忽略 `_fid`，仅时间定位）
- [export_execution_facts.py#L558](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L558)（主循环 `while loop_advance.time_ms < window_end_ms`，到达右边界即停，同毫秒无法继续翻页）
- [export_execution_facts.py#L908-L912](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L908-L912)（与 R10 共用的 `_resolve_page_outcome`，右边界判定同样影响 income）

**根因**
- `um/income` 无 `fromId` 类游标，且当前实现无基于 `tranId` 或更细时间片的翻页能力；主循环 `time_ms < window_end_ms` 的退出条件也使「到达右边界」后无法在同毫秒内续取。
- 现有 `page_complete` 判定在「满页 + 到达右边界」时会误报完成（与 R10 同源），income 因而**尚未暴露不完整**，存在静默漏收入的更大风险。

**期望行为（修复口径）**
1. **核实 `um/income` 接口可用的分页 / 历史导出能力**（是否有事件 ID 游标、`startTime/endTime` 粒度、返回量上限），作为补采方案的事实依据；不凭猜测设计。
2. 设计**补采方案**并把结论落为文档/决策（`decisions.md` 或本文附录）：若接口支持 `tranId` / 事件 ID 续传 → 实现同毫秒续取；若确认不支持 → 转入「持续标记缺口」。
3. **未解决前持续标记缺口**：凡「同毫秒可能超过一页」或「无法确认完整」的窗口，`page_complete=False`、`request_status='incomplete'`，并在结果中携带「缺口原因」。
4. **保留可恢复进度**：即使不完整，也写入已采得的记录与游标（`covered_until_ms` 不得越过未排空的边界毫秒），使重跑可续、不重复入账。
5. **阻止不完整窗口进入正式净收益结论**：下游凡消费 `page_complete=False` 的 income 结果，不得据此产出净收益对账/alpha 结论。（本条为**规则声明**；**下游消费端门禁的实现与断言在本轮未完成、亦未验证**——本仓库未发现净收益消费端的完整性拒绝实现。）
6. **不能靠时间戳 `+1ms` 跳过**：禁止用「把窗口右边界 +1ms」的方式绕过同毫秒翻页问题——那只是把漏数藏进另一个窗口，仍会漏或重。

**验收标准（可判定）**
- 接口能力核实结论有据（文档或官方引用），并依此给出方案选择。
- 同毫秒 `limit` 笔 → `page_complete=True`（正常收尾）。
- 同毫秒 `limit+1` 笔：若方案已实现续取 → 取回全部、`page_complete=True`；若标记缺口 → `page_complete=False`、`request_status='incomplete'`、携带缺口原因，**且绝不得 `page_complete=True`**。
- 不完整窗口：游标 `covered_until_ms` 停在仍可能有余量的边界毫秒（不推进越过），重跑可续取、去重不重入账。
- 「阻止进入净收益结论」：本轮**已规定**下游不得据 `page_complete=False` 的 income 结果产出净收益结论（**规则 + 代码审查项锁定**）。**当前仓库未实现该下游消费点门禁、无对应断言，不得表述为『自动拒绝已验证』。**
- 「禁止 +1ms」：断言不存在用 `window_end_ms + 1` 作为续传起点/过滤上界的实现路径（代码审查项 + 测试项）。

---

## 3. 「先红后绿」新增回归用例清单（汇总）

> 每条回归先写「修复前红」断言，再在修复后转绿。R8/R9 的既有回归（`test_reprice_budget_r8.py`、`test_r9_signal_attribution.py`）作为**防回归基线**保留，不改。

| 需求 | 新增回归文件/组（实际落盘） | 用例 | 红（修复前） | 绿（修复后） |
|---|---|---|---|---|
| R10 | `tests/test_shared/test_execution_facts_regression.py` | `test_same_ms_exactly_at_limit` | 满页到达结束毫秒误报 `page_complete=True` 且漏后续 | 结束毫秒恰好 limit 笔，取全、`page_complete=True`、不空转一页 |
| R10 | 同上 | `test_same_ms_limit_plus_one` | 结束毫秒 limit+1 笔只取回 limit 笔 | 取回 limit+1 笔、`page_complete=True` |
| R10 | 同上 | `test_same_ms_four_trades_limit_two` | 4 笔同毫秒 limit=2，只取 2 笔 | 取回 4 笔、`page_complete=True` |
| R10 | 同上 + `tests/test_accounting/test_fact_ingestion_gaps.py::test_full_page_but_time_reached_window_end_finishes_ok` | `test_out_of_window_rows_filtered_not_misjudged` | 越界记录导致误判 | 越界记录被过滤、不进 collected、`page_complete` 只由窗口内排空决定 |
| R10 | `tests/test_accounting/test_fact_ingestion_gaps.py` | `test_mid_window_failure_never_complete` | 中途失败仍报 complete | `page_complete=False`、errors 非空、游标不推进、恢复后续取 |
| R10 | 同上（Fake 时间谓词收紧） | `test_time_boundary_consistency_fake_closed_interval` | Fake 用 `< end`、与客户端 `<= end` 不一致 | 请求/过滤/游标/Fake 统一闭区间 |
| RK1 | `strategies/hrs/tests/test_rk1_signal_db_alert.py` | `test_insert_failed_kind_sends_alert` | 仅 logger.warning | 落库失败触发可识别告警（含定位信息） |
| RK1 | 同上 | `test_signal_id_missing_kind_sends_alert`、`test_update_failed_kind_sends_alert` | 仅 logger.warning | signal_id None / UPDATE 异常触发告警 |
| RK1 | 同上 | `test_insert_writes_entry_mode_and_signal_origin` | 两列可空/未写 | 生产与影子 INSERT 均写非空归属字段（复用 R9 基线） |
| RK1 | 上线核对（非单测） | 库表核对两列存在 | 冷隔离、两列不存在 | 部署后 `ADD COLUMN IF NOT EXISTS` 生效，两列存在（**上线时执行**） |
| R11 | `strategies/hrs/tests/test_scoring_engine.py`（修复既有用例 + 新增 2 条） | `test_技术总分不足不应入场` | 返回 True（红） | 改断言为 `is True`（V2.10 做空门槛 2.0，行为已批准） |
| R11 | 同上（新增） | `test_做空技术分不足应拒入场`、`test_standard_and_shadow_independent` | 三轨混写致互相影响 | short 技术分 < 2.0 拒入场；standard 拦截不改变影子链输出形态 |
| RK2 | `strategies/hrs/tests/test_rk2_signal_stats.py` | `test_timeout_not_counted_as_execution`、`test_win_rate_must_come_from_trade_records` | 超时信号按行数计入胜率分子 | 超时/影子不计入真实执行数与胜率分子 |
| RK2 | 同上 | `test_fixed_sample_row_count` 等（13 行固定样本，`TestFixedSampleStatistics`） | 口径未定义、结果随样本漂移 | 固定样本（生产 executed 3 / 影子 shadow 4 / 超时 timeout 3 / 冲突 conflict 3 / 未决 1）下各项口径与预期一致 |
| R12 | `tests/test_shared/test_execution_facts_regression.py` | `test_income_no_from_id_falls_back_to_incomplete` | 满页误报 complete | `page_complete=False` + `request_status='incomplete'` + 缺口原因 |
| R12 | `tests/test_accounting/test_fact_ingestion_gaps.py` | `test_income_resume_via_sink_cursor_broken_plus_resume_covers_full` | 满页到达边界误报 complete 漏收入 | 续取成功取全 / 或显式 `page_complete=False`+`incomplete`+缺口原因 |
| R12 | 约束（规则 + 代码审查项） | `test_income_never_uses_plus_one_ms_workaround`（位于 `tests/test_shared/test_execution_facts_regression.py` L771） | 用 +1ms 绕过 | 无 +1ms 跳过路径由该单测锁定；「不完整窗口不得进入净收益结论」的验证方式为**规则 + 代码审查项**（无单测；消费端门禁实现待办）。（Round 4 勘误：原列出的 `test_income_incomplete_never_enters_pnl_conclusion` 无实际定义，已更正为规则+审查项。） |

**执行顺序建议**：R10（P1，账本采集契约，且 R12 共用收尾）→ R12（income 补采/缺口）→ R11（评分引擎）→ RK1（告警通道）→ RK2（统计口径）。R8/R9 既有回归保持绿，任一回归红即阻断该组先行。

**勘误（2026-10-17 收口补记）**：上表初稿所列「建议文件名」与**实际落盘名**不一致，已按实际落盘修正：
- R10 的 `tests/test_shared/test_execution_facts_right_boundary.py` → 实际为 `tests/test_shared/test_execution_facts_regression.py`（R10 边界用例位于该类文件 `TestR6SameMsCrossPage`）；另在 `tests/test_accounting/test_fact_ingestion_gaps.py::test_full_page_but_time_reached_window_end_finishes_ok` 覆盖「原始页满 + 触界须续传」。
- RK1 的 `strategies/hrs/tests/test_rk1_schema_and_alert.py` → 实际为 `strategies/hrs/tests/test_rk1_signal_db_alert.py`（11 用例：三类 kind 直调 / 开关关闭 / 三处调用点 / 成功不告警 / 告警发送异常吞掉）。
- R12 的 `tests/test_shared/test_income_pagination.py` **未单独建文件**，income 缺口用例落在 `tests/test_shared/test_execution_facts_regression.py::test_income_no_from_id_falls_back_to_incomplete` 与 `tests/test_accounting/test_fact_ingestion_gaps.py::test_income_resume_via_sink_cursor_broken_plus_resume_covers_full`。
- RK2 `test_rk2_signal_stats.py` 与 R11 `test_scoring_engine.py` 落盘名与初稿一致。

---

## 4. 时间边界口径统一要求（R10 与 R12 共同前提）

- 本项目**默认选闭区间**：请求窗口为 `[start, end]`，客户端过滤 `time_ms <= window_end_ms`，游标收尾「窗口内记录是否排空」按闭区间判定，测试 Fake 按 `start <= time <= end` 过滤。
- 若编码组有充分理由改半开区间（`[start, end)`），必须**四处同步改**：请求 `endTime`、客户端过滤 `< window_end_ms`、游标收尾、测试 Fake，并新增「相邻窗口无缺口」的衔接用例。
- 二者**仅取其一并全文一致**，评审时用「请求/过滤/游标/Fake 四处同一判据」作为门禁项，混用判不达标。

---

## 5. 本轮明确不宣称完成的项（范围红线）

- **净收益对账、权益守恒、全策略归属账本**：仍排除。R12 只解决「补采/缺口标记」，不据此产出任何净收益/alpha 结论。
- **MTPCS 趋势 / 回归分支定位、分支独立净收益**：仍排除。
- **R8（新币重算数量）、R9（唯一 signal_id 更新）**：已通过，不重设计，仅保留回归基线。
- **部署 / 上线 / apply 迁移 / 连生产库 / 调真实交易所**：全程不做；RK1 库表核对标注「上线时」由运维/部署环节执行，本轮只写离线断言。
- 本轮任何结论**不得宣称「已部署 / 已上线」**。

---

## 6. 风险与待确认（转编码/调度者）— 收口状态

1. **R11 根因归属**：已定位为**情形 b——已批准的行为变化（V2.10 多空分离）**，批准来源提交 `3b4052d`；动作 = 改测试 + 同步文档，不改生产代码。见 §7 收口。
2. **RK2 读取方排查结论**：已排查，**当前全仓零读取方**（`hrs.hrs_signals` 仅被 `strategy.py` 写入）；口径已定义并锁定，见 §7 与 architecture §6.2。
3. **R12 接口能力核实**：已确证 `um/income` **无事件 ID / tranId 类游标**，同毫秒超单页**无法完整续采** → 到界即返回 `request_status='incomplete'` / `page_complete=False`，**禁止**用时间戳 `+1ms` 跳过。见 §7 能力限制。
4. **告警通道选型**：已复用 `shared/notification.py::send_alert`，开关走 `strategies/hrs/config.yaml::notification.events.signal_db_failure`（默认 `true`），目标不硬编码。见 §7。
5. **资金/交易逻辑风险标注**：R10、R11、R12 涉及真实交易/收益判定，已执行强制测试；离线合成数据不等同生产库/交易所集成验证，见 §7 能力限制。

---

## 7. 收口：逐条闭环状态与本轮实际改动（2026-10-17 补记）

> 本节为**收口补记**：只如实记录本轮实际落地现状、证据与能力限制，**不顺带宣称**净收益对账 / 部署验收等 §5 排除项完成，也不主张「已部署 / 已上线」。

### 7.1 逐条闭环状态表

| 编号 | 闭环结论 | 关键证据（文件 · 行号） |
|---|---|---|
| **R10** | ✅ **已修复**（P1 右边界静默漏数闭环） | 守卫为 [export_execution_facts.py `_prepare_cursor_and_fetch` L657-661](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L657-L661)：`cur_start >= ctx.window_end_ms` **且** `loaded_cursor is not None` **且** `loaded_cursor.page_complete` 三条件同时成立才幂等短路；无游标（首次查询）即便 `cur_start == window_end_ms`（单毫秒闭区间）也必须进入翻页循环查一次；原始页满判定 [_one_page_iteration L788-790](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L788-L790)（`raw_page_full = len(raw_rows) >= page_limit`，`saw_out_of_window = len(rows) < len(raw_rows)`）；三情形收尾 [_resolve_page_outcome L799-838](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L799-L838)。回归 [test_execution_facts_regression.py::TestR6SameMsCrossPage](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_shared/test_execution_facts_regression.py#L334-L436) 与 [test_fact_ingestion_gaps.py L341-362](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_accounting/test_fact_ingestion_gaps.py#L341-L362)。Round 4 修正：round-3 曾写为『无游标也短路』，导致单毫秒点窗 `[T,T]` 首次查询被静默跳过、返回 0 条且误标 complete，本轮已改为『仅已有且已完成游标才短路』 |
| **RK1** | ✅ **已修复**（离线闭环）／ ⏳ **运行时落地待上线后验证** | 新增 [strategy.py `_alert_signal_db_failure` L4493-4541](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L4493-L4541)（三 kind：`insert_failed`/`signal_id_missing`/`update_failed`）；三处失败路径调用 [L3800-3802](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3800-L3802)（insert_failed）、[L3828-3830](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3828-L3830)（signal_id_missing）、[L3844-3846](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3844-L3846)（update_failed）；开关 [config.yaml L524](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/config.yaml#L524)。回归 [test_rk1_signal_db_alert.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/tests/test_rk1_signal_db_alert.py)（11 用例）。建表两列声明 [strategy.py L3021-3041](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3021-L3041)、幂等迁移 [L3060-3066](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py#L3060-L3066) |
| **R11** | ✅ **已闭环**（已批准行为变化 V2.10，只改测试不改生产代码） | [test_scoring_engine.py L373-455](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/tests/test_scoring_engine.py#L373-L455)：改 `test_技术总分不足不应入场` 断言 + 新增 `test_做空技术分不足应拒入场`、`test_standard_and_shadow_independent`。批准来源：提交 `3b4052d`（V2.10 多空分离，做多 `min_total_score=4.0` / 做空 `short_min_total_score=2.0`） |
| **RK2** | ⚠️ **口径需声明 → 已定义并锁定**（零读取方，潜伏风险） | 排查结论：全代码树零 `SELECT hrs.hrs_signals`；口径定义落 [architecture §6.2](file:///Users/yl/vscode/Binance_quantitative_trading/docs/research/strategy_remediation/architecture_R10-RK_2026-10-17.md)。回归 [test_rk2_signal_stats.py](file:///Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/tests/test_rk2_signal_stats.py)（13 行固定样本 + 红线断言） |
| **R12** | ⚠️ **能力受限（能力限制已闭环，采集完整性未闭环）** | income 无事件游标：到界即 `page_complete=False` / `request_status='incomplete'`；`covered_until_ms` 停在未排空边界毫秒（不越界、不 `+1ms`）。回归 [test_income_no_from_id_falls_back_to_incomplete](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_shared/test_execution_facts_regression.py#L403-L421)、[test_income_resume_via_sink_cursor_broken_plus_resume_covers_full](file:///Users/yl/vscode/Binance_quantitative_trading/tests/test_accounting/test_fact_ingestion_gaps.py#L563-L600) |

### 7.2 R10 与本轮架构设计的偏差（代码实际 vs 架构定稿）

architecture_R10-RK §1.5 曾设计「`CursorRecord` 新增 `saw_out_of_window` 字段」。**代码实际未采用**：为避免引入**死字段**（违反「禁止幽灵参数/死字段」），`saw_out_of_window` **仅是 `_one_page_iteration` 内的局部变量**（[L789](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L789)），`CursorRecord` 字段表（[L113-132](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L113-L132)）**不含**该字段。判定所需信息由 `_resolve_page_outcome` 在单次迭代内消费即可，无需入库。该偏差已同步至 architecture §1.5 / §7.1。

此外，`_resolve_page_outcome` 及相关收尾逻辑按「单函数 ≤ 50 行」进一步拆分：抽 `_PageCtx`（[L142-158](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L142-L158)），并将原方法拆为 [`_prepare_cursor_and_fetch` L631-672](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L631-L672)、[`_run_paging_until_done` L556-](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L556)、[`_one_page_iteration` L757-](file:///Users/yl/vscode/Binance_quantitative_trading/scripts/export_execution_facts.py#L757) 等（均 ≤50 行）。

### 7.3 统计口径定义（RK2 关键，权威定义见 architecture §6.2）

| 口径 | 语义定义 |
|---|---|
| 触发数 | `status IN ('triggered','resolved')` 的行数 |
| 超时数 | `resolution = 'timeout'` 的行数（R9 后开始落库） |
| 影子数 | `resolution = 'shadow'` **或** `signal_origin IN ('shadow_scoring','empty_pool')` |
| 真实执行数 | `resolution = 'executed'`（**不自动等于真实成交**） |
| 执行率 | `executed / (executed + conflict + timeout)`（分母显式声明，shadow 不计入） |
| 未决行 | `status = 'triggered' AND resolution IS NULL` |
| 胜率分子/分母 | `trading.trade_records` 中 `strategy='hrs'` 的已平仓单（`realized_pnl > 0` / `IS NOT NULL`） |

**正交声明（关键）**：`conflict`（结果轴，`resolution`）与 `shadow`（来源轴，`signal_origin`）**非互斥、互相正交**——一条 `signal_origin='empty_pool'`（来源=影子）的 LV-RM 信号其 `resolution` 可为 `'conflict'`，故**同时**被「影子数」与「冲突数」计入。固定样本中该行为 1 行（ARBUSDT），使**影子数 = 4**（3 shadow + 1 empty_pool）而**冲突数 = 3**（2 standard + 1 empty_pool）。

**执行率分母差异来源（3/9 vs 3/8）**：固定样本 executed=3、conflict=3、timeout=3 → 分母 = 3+3+3 = **9**，执行率 = **3/9 ≈ 0.333**（`test_execution_rate` 断言）。若**错误地**把 conflict 与 shadow 当作互斥、将来源为影子的 conflict 行从冲突分母剔除，则 conflict=2 → 分母 = 8 → 得 **3/8**。**权威口径为 3/9**：执行率分母**不按来源剔除**，凡 `resolution ∈ {executed, conflict, timeout}` 均计入分母；`shadow`（结果轴）本就不在分母，无需另删来源为影子的 conflict 行。

**红线（强制）**：交易**胜率必须取 `trading.trade_records.realized_pnl`**（真实成交），**严禁**用 `hrs_signals.resolution='executed'` 的行数或信号行数推导胜率（回归 `test_win_rate_must_come_from_trade_records` 构造「信号 executed 数 ≠ 胜率分母」反例锁定）。

### 7.4 能力限制披露（本轮绝不得宣称完成）

1. **RK1 运行时落地待上线后验证**：本轮离线**仅确认「幂等迁移路径存在」**（`ADD COLUMN IF NOT EXISTS` 代码正确）。**生产库迁移生效 + 归属列（`entry_mode`/`signal_origin`）实际写入** 须**上线后**在服务器 `docker exec` 核验（`hrs.hrs_signals` 两列存在且可写非空值、hrs 容器启动日志「数据库 Schema 初始化完成」无报错）。**本轮不 apply、不连生产库、不部署**。
2. **income 同毫秒超一页无法完整采集**：`um/income` 无事件 ID 游标，同毫秒 `> page_limit` 的记录**无法完整续采**；未解决前**对应窗口的净收益结论保持「不可验收」**，采集结果须显式标记 `page_complete=False` / `request_status='incomplete'` 并**报告缺失窗口**；下游**禁止**据 `page_complete=False` 的 income 结果产出净收益对账 / alpha 结论。（本轮为规则声明；**消费端门禁实现与验证仍为待办**，见 §7.5。）
3. **级联告警（已知行为，低危，本轮不修）**：`_log_signal_to_db` 的 INSERT 失败（`insert_failed`）后，`signal_id=None` 会**级联触发** `signal_id_missing` 告警——**同根因两条告警**。判定为**低危**（仅告警重复，不影响主流程、不误伤数据），本轮**不修**，**记入待办**后续评估是否合并去重。register: 见 §7.5 待办。
4. **审计材料适配留痕（见 §7.6）**。

### 7.5 待办（本轮不修，登记备查）

- 级联告警去重（`insert_failed` → `signal_id_missing` 同根因双告警，见 §7.4 第 3 点）。
- RK2 若未来接入读取方（动态拼 SQL / 未出现在本次 grep 的脚本），须按 §7.3 / architecture §6.2 口径抽公共函数并纳入改造清单。
- income 完整采集能力（若官方提供 `tranId` 等续传能力再升级为「续取方案」）。

### 7.6 审计材料适配说明（可追溯性）

本轮第三方复审材料 `docs/reviews/strategy_reaudit_2026-10-17/verify_offline.py` 的 `boundary` 断言**已随 R10 修复适配**：原「缺陷可复现（`boundary['actual'] == 2`，只请求一次、采得 2 笔）」改为「**修复后 `boundary['actual'] == 5 and boundary['complete']`，且 `len(boundary['calls']) >= 2`**（第一次满页、续传确认排空）」，脚本内已含适配注释，`results.json` 落盘为 `boundary.actual=5 / complete=true / calls=5`（时间戳 2026-10-17 08:15）。复审 README 的**原始问题描述（缺陷可复现）作为历史记录保留不改**；「修复已使断言由红转绿」的事实记录于本节，保持可追溯。

---

## 8. 第四轮复审收口补记（2026-10-17）

> 本节如实记录第四轮复审（材料 `docs/reviews/strategy_reaudit_2026-10-17_round4/`，冻结不改）提出的问题与本轮处置，**不宣称五项全闭环**。各项须分别表述为：本地实现通过 / 上线待验 / 口径文档完成 / 采集能力未补齐。

### 8.1 [P2] 单毫秒闭区间点窗被守卫跳过（已修复）
round-3 的守卫过纠正使**首次查询**的闭区间单毫秒点窗 `[1000,1000]` 被短路：不调用 API、返回 0 条、却误标 `page_complete=True`。已改为「仅已有且已完成游标才短路」（见 §7.1 R10 行）。新增回归 `tests/test_shared/test_execution_facts_regression.py::TestPointWindowGuards`（单毫秒一条/空/超一页、income 单毫秒超页 incomplete）、`TestIdempotentShortCircuitPreserved`（已完成游标短路 / 未完成游标不短路）、`TestReverseWindowGuardsNaturalPaging`（反向窗口自然进翻页返回 ok 空），并同步 `tests/test_accounting/test_fact_ingestion_gaps.py::test_true_empty_window_returns_ok_noop` 的调用次数断言。验收覆盖：单毫秒零条/一条/超过一页 + 已完成/未完成游标重跑。

### 8.2 各项状态收正（不得统称全闭环）
- **原「结束毫秒超过一页」漏数**：**通过**（独立案例 5 笔全部取回）。
- **RK1**：本地实现通过；**生产库迁移生效与归属列实际写入仍待上线验收**（本轮无该证据）。
- **R11 评分**：行为断言已修复（short 门槛 2.0）；另有 `TestPerformance::test_评分引擎吞吐量` 失败（阈值 5000，实测约 3045–3453）。归因结论为 **(A) 预先存在 + 环境相关**：阈值硬编码在 `test_scoring_engine.py` L523 于 HEAD 即存在；`ScoringEngine.score()` 为同步纯计算，与本轮 HRS 异步 DB/告警改动**完全解耦**；`TestPerformance` 类零改动；本机（M 系列 macOS）复跑 24019/24160/24175 稳定通过。**不降阈值**，建议补基准环境说明。**故不得报告「评分测试全绿」。**
- **RK2**：统计口径已定义并锁定；**当前仓库未发现生产统计读取方**；新增测试为本地 helper，非生产统计实现。
- **R12 income**：显式缺口保护通过（重跑仍正确保持 incomplete）；**完整补采能力未完成，消费端门禁未验证**。

### 8.3 反向窗口口径
反向窗口（`start > end`）本轮**不显式拒绝**，为守卫修正后的自然行为：无游标且初始到界 → 进入翻页循环查一次空窗口 → `page_complete=True` / `request_status='ok'` / `records=[]`（由 `TestReverseWindowGuardsNaturalPaging` 锁定）。已在回归中单独定义该行为。

---

**最后更新：** 2026-10-17（第三轮复审后需求**收口**：状态改为「已实现 + 已通过回归」；新增 §7 逐条闭环状态表 / R10 架构偏差 / 统计口径定义 / 能力限制披露 / 待办 / 审计材料适配；修正 §3 回归用例文件名勘误；只声明 R10/RK1/R11/RK2/R12 五项状态，不顺带宣称 §5 排除项与部署完成）