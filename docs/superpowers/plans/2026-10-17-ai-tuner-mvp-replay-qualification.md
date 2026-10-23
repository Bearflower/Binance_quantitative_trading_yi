# AI Tuner 阶段二：双策略确定性回放与资格考试 实施计划

> **状态**：修订版（2026-10-22）。取代原「只实现选中的单一策略」口径。
> **执行方式**：按 Task 0–10 顺序推进，每个 Task 完成即提交（本地 commit，**不 push、不部署**）。

**目标**：建设一套共享的确定性回放与资格考试底座，同时支持 `btc_eth_aggressive` 与 `hrs`；在最大回撤硬约束内，评估扣除手续费、滑点、资金费率后的风险调整收益。

**架构**：冻结数据快照 → 无未来数据 DataPortal → 确定性事件时钟 → 策略适配器（MTPCS 激进版 / HRS）→ 统一虚拟 Broker（费用/滑点/资金费/精度/保证金）→ 成绩卡与硬门禁 → `PASS` / `FAIL` / `BLOCKED_DATA`。

**技术栈**：Python（运行期与容器均为 3.9/3.10，禁用 3.10+ 专属语法）、`dataclasses`、`decimal.Decimal`、pandas/numpy（项目已有）、pytest。

---

## 零、决策记录（2026-10-22 用户裁定）

| 编号 | 决策项 | 结论 |
|---|---|---|
| D1 | 运行位置 | **本地离线优先**。框架代码随仓库与 ai-tuner 镜像打包，但回放/对账/考试**只在开发机执行**，不在容器内触发；符合 `coding-standards.md` 禁止服务器回测。 |
| D2 | 快照来源 | **本地冻结文件 + manifest（SHA-256）**。Postgres 仅通过只读导出脚本（SELECT / COPY TO STDOUT）产出快照到 git 忽略目录；回放只读本地文件，不连库。 |
| D3 | 实盘代码提取 | **授权**对 `strategies/btc_eth_aggressive/strategy.py` 做等价纯函数提取，范围仅限：评分、方向判断、入场过滤、止损/止盈/退出价格计算，及与上述计算直接相关的无副作用辅助函数。 |
| D4 | 样本不足时的口径 | 合成夹具只验证引擎确定性/撮合/成本/无未来数据/fail-closed，**不得记为「实盘金样对账通过」**；激进版约 10 条真实信号只能产出「部分对账报告」，最终状态 `BLOCKED_DATA`；HRS 三表无有效历史时只验证空数据接线与 `BLOCKED_DATA`；**样本不足不得写成策略 `FAIL`**；达到真实样本门禁后必须重跑正式 parity 与 qualify。 |

### D3 执行要求（逐条强制执行，不得放宽）

1. 先建立 characterization / golden 测试；
2. 冻结当前输入与逐字段输出；
3. 再移动代码；
4. 实盘与回放共同调用新纯函数；
5. 提取前后输出逐字段一致；
6. 不改变公式、默认值、异常语义与调用顺序；
7. **任何不一致都必须停止**，不得通过放宽测试解决。

---

## 一、阶段二边界（禁止事项）

阶段二只负责回放、对账、资格考试与生成不可变报告，**不得**：

- 修改生产策略参数；
- 写入或切换 `.active` 覆盖层；
- 调低 readiness 门禁；
- 触发生产自动生效；
- 让 LLM 修改代码或策略逻辑；
- 在容器内或服务器上执行回放。

输出只有 `PASS` / `FAIL` / `BLOCKED_DATA`，**不得**在阶段二输出「自动生效」。

---

## 二、Task 0：现有回放能力复用审计（已完成）

审计对象：`backtest/btc_eth/replay/`（本地 XAU 影子回放器，3623 行）。

| 能力 | 直接复用 | 抽象后复用 | 新建 | 依据 |
|---|---:|---:|---:|---|
| 快照哈希 / manifest 校验 | ✓ | | | `snapshot.py:104-129`（`_sha256_file` / `_sha256_text` / `_compute_manifest_sha256`）、`verify_snapshot:213-253`（顶层哈希防「嵌套哈希同步篡改」） |
| Decimal 撮合 / 精度舍入 | ✓ | | | `matching.py:13-93`（`q_round` / `round_price` / `round_quantity` / `clamp_notional` / `market_fill_price`）、`parse_symbol_filters:97` |
| 手续费 / 滑点 / 资金费 / PnL | ✓ | | | `fees.py:17-100`；PnL 已委托 `shared.trade_logger.TradeLogger.calculate_pnl`，无重复实现 |
| 不变量校验 | ✓ | | | `invariants.py:14-88` |
| 成绩卡指标 | | ✓ | | `report.py:130` `compute_metrics`、`:363` 最大回撤；但 `decide_admission:492` 是「xau_original vs xau_aggressive 选冠军」口径，**与本次「两策略不混选」冲突，禁止复用** |
| 硬门禁 | | ✓ | | `report.py:406` `_admission_checks` 阈值全走配置，但仅 7 项，缺样本量/盲测/相邻扰动/集中度/版本兼容 |
| XAU profile 事件引擎 | | ✓ | | `engine.py:126-135` 强制校验 `xau_original`/`xau_aggressive` config hash；`:361` 硬编码 `PER_PROFILES`；`state.py:17-56` 默认值即单品种 pilot（1 仓 / 30 USDT / 72h 冷却） |
| 无未来数据门户 | | | ✓ | 现有引擎假定事件已冻结到 cutoff（`__init__.py:8-9` 明确「不重算 score」），无逐查询 `as_of` 守卫，也无 universe / 外部快照读取面 |
| 同根 K 线止盈止损保守顺序 | | ✓ | | `engine.py:833` `_check_and_close_on_bar` 为单持仓 XAU 口径，通用化需重写判定骨架 |
| MTPCS 激进版适配器 | | | ✓ | 实盘 6382 行，纯计算散落（`_calculate_score:1374`、`_detect_breakout_pullback:1619`、`_determine_grade:2225`、`_determine_direction:2267`、`_calculate_tp_price:2597`、`_calculate_stop_price:3271`），同类方法大量耦合 DB / 下单 |
| HRS 适配器 | | | ✓ | `hrs/strategy.py` 4783 行 + `scoring_engine.py` 1619 行；三张 point-in-time 表行数均为 0 |
| 资格考试门禁 | | | ✓ | 全新 |

### 审计结论

1. **`backtest/` 不能作为运行期依赖**：`ai_tuner/Dockerfile` 仅 `COPY ai_tuner/ shared/ scripts/ai_tuner_mvp.py` 与 `strategies/*/config.yaml`，不含 `backtest/` 与策略代码。所谓「复用」= 把纯函数搬迁到双端都能导入的位置，**不是** `import backtest.*`。
2. **`backtest/btc_eth/replay/` 保持不动**：它绑定 XAU 双 profile 与冻结决策事件，服务影子仿真；本阶段不改造它，避免两个引擎互相污染。
3. **纯函数落点**：`strategies/btc_eth_aggressive/core/`（该目录已在 `strategies/btc_eth_aggressive/Dockerfile:20` 整体 COPY，实盘镜像自动打包）；实盘 `strategy.py` 与回放适配器共同 import。HRS 未获提取授权，适配器只调用现有模块，缺数据即 `BLOCKED_DATA`。

---

## 三、文件结构

```text
ai_tuner/replay/
  __init__.py                     # ENGINE_VERSION / COST_MODEL_VERSION
  models.py                       # Decimal 冻结模型
  clock.py                        # 确定性事件排序
  data_portal.py                  # as_of 强制 + FutureDataAccess + 覆盖度检查
  broker.py                       # 统一虚拟 Broker
  engine.py                       # 共享事件循环 + audit trail
  artifacts.py                    # 六个不可变文件 canonical 序列化
  adapters/base.py                # ReplayAdapter 协议
  adapters/mtpcs_aggressive.py    # 复用提取后的纯函数
  adapters/hrs.py                 # 以三表为契约，缺数据 BLOCKED_DATA
ai_tuner/regime/{__init__,features,classifier}.py
ai_tuner/qualification/{__init__,metrics,gates,runner}.py
ai_tuner/parity/{__init__,golden}.py
strategies/btc_eth_aggressive/core/{__init__,scoring,direction,filters,exits}.py
scripts/ai_tuner_mvp.py           # 扩展 replay / parity / qualify
scripts/export_replay_snapshot.py # 只读导出（SELECT / COPY TO STDOUT）
tests/test_ai_tuner_mvp/{replay,regime,qualification,parity}/
tests/test_strategies/test_btc_eth_aggressive_core_equivalence.py
.gitignore                        # 新增 var/replay/
```

---

## 四、Task 1：冻结输入快照

**实现**
- `scripts/export_replay_snapshot.py`：只读导出到 `var/replay/snapshots/<strategy>/<as_of>/`，仅 SELECT / COPY TO STDOUT，不写入任何生产表。
- 快照 manifest 至少记录 10 项：`strategy_id`、`as_of`、时间窗口、数据文件 SHA-256、基础配置哈希、参数 manifest 哈希、覆盖层哈希、代码提交 SHA、回放引擎版本、费用模型版本、数据缺口列表。
- 复用 `_sha256_file` / `_compute_manifest_sha256` 口径（搬迁实现，不 import `backtest`）。

**验收**：缺任一必需数据 → `BLOCKED_DATA`（不自动忽略）；同一目录重复校验哈希一致。

---

## 五、Task 2：无未来数据访问

**实现**
- 所有查询显式携带 `as_of`：K 线 `close_time <= as_of`；universe `observed_at / created_at <= as_of`；外部快照 `source_observed_at / collected_at <= as_of`；信号、资金费率、版本记录同规则。
- 越界 → `FutureDataAccess`，**整个资格考试失败**并写 `audit_events.jsonl` 证据。
- 覆盖度不足 → 结构化 coverage failure（`BLOCKED_DATA`），**禁止**用当前 API 值填补。

**验收**：未来数据访问用例全部失败退出；无 fallback 路径。

---

## 六、Task 3：统一虚拟 Broker

**实现**：市价单、限价单、止损、止盈、reduce-only、部分平仓、手续费、基准滑点、1.5×/2× 成本档、资金费率、价格与数量精度、最小名义价值、杠杆与保证金约束；同根 K 线同时触发止盈止损时采用**保守、确定性**顺序。

**验收**：成本单调性不变量 —— 提高手续费或滑点后，最终权益不得上升。

---

## 七、Task 4：MTPCS 激进版适配器

**实现**
- 按 D3 流程：characterization/golden 测试 → 冻结逐字段输出 → 提取到 `strategies/btc_eth_aggressive/core/` → 实盘与回放共用 → 逐字段比对。
- 覆盖：指标计算、趋势强度、breakout/pullback、评分、入场过滤、止盈止损、移动止损、时间退出。
- 禁止在回放目录手抄策略逻辑。

**验收**：提取前后逐字段一致；原全量策略测试与新增对账测试同时通过。

---

## 八、Task 5：HRS 适配器

**实现**：按历史时点恢复 `ai_tuner_universe_scan_runs` / `ai_tuner_universe_snapshots` / `ai_tuner_market_snapshots` —— 当时有效 universe、long/short membership、当时外部快照、当时评分阈值、黑名单与候选池状态、入场/退出/持仓管理。缺失即 `BLOCKED_DATA`，禁止实时补值。

**当前事实**：三表行数为 0 → 本轮只交付空数据接线与 `BLOCKED_DATA` 证据，不做正式考试。

---

## 九、Task 6：金样对账

**实现**
- 两个策略分别建立金样；选真实运行过的连续区间，冻结实盘信号/入场/退出/费用/盈亏。
- 使用完全相同的当前参数回放，逐项对比信号方向、时间、价格、退出原因、费用、盈亏、权益曲线。
- 容差**由真实数据校准并写入配置**，不得在代码里放宽。
- 术语（D4）：合成夹具 → 仅验证确定性；真实数据不足 → `partial_parity` + `BLOCKED_DATA`；不得把样本不足写成 `FAIL`。

**验收**：金样未通过（非样本不足）前不得运行新候选资格考试。

---

## 十、Task 7：市场状态分类

**实现**：6 类（涨/跌/震荡 × 正常/高波动）+ `STRESS`，阈值全部来自配置；只用当时已收盘数据；仅用于分组与历史版本匹配，**不得**变成新的交易条件。

---

## 十一、Task 8：成绩卡与硬门禁

**成绩卡**：扣费后净收益、最大回撤、Sortino、Calmar、单笔期望、胜率、盈亏比、有效信号数、已平仓交易数、收益集中度、分币种表现、分市场状态表现、1.5×/2× 成本表现、相邻参数扰动表现、全部淘汰原因。

**硬门禁（10 项，任一失败即不得参与综合评分）**：数据完整度、无未来数据、金样对账、最大回撤、最低样本量、最近两周盲测、参数相邻值稳定性、1.5× 成本压力、收益集中度、版本兼容性。

---

## 十二、Task 9：同场资格考试

**实现**：每个策略内部比较「当前覆盖层 + 1 个 LLM 新候选 + ≤3 个兼容历史覆盖层」，同数据、同窗口、同初始资金、同成本假设。窗口：前 8 周诊断区、最近 2 周盲测区、更早数据用于市场状态与稳定性验证。**盲测结果不得提供给 LLM**。输出仅 `PASS` / `FAIL` / `BLOCKED_DATA`。

**禁止**：跨策略比较、选「冠军」。

---

## 十三、Task 10：CLI 与不可变报告

```bash
python3 scripts/ai_tuner_mvp.py replay   --strategy btc_eth_aggressive --as-of 2026-10-22T00:00:00Z
python3 scripts/ai_tuner_mvp.py parity   --strategy btc_eth_aggressive --as-of 2026-10-22T00:00:00Z
python3 scripts/ai_tuner_mvp.py qualify  --strategy btc_eth_aggressive --candidate candidate.json --as-of 2026-10-22T00:00:00Z
```

`--strategy hrs` 使用相同命令。每次输出六个文件：`run_manifest.json`、`data_quality.json`、`parity_report.json`、`scorecard.json`、`qualification_decision.json`、`audit_events.jsonl`。

**序列化口径**：canonical JSON（`sort_keys=True`、`separators=(",", ":")`、Decimal→字符串、UTF-8、末尾单个换行）；`audit_events.jsonl` 行序按 `(timestamp, seq)` 固定。相同输入重复执行，文件**逐字节一致**。

**退出码**：`0` = PASS，`2` = FAIL，`3` = BLOCKED_DATA，`1` = 运行时错误。

---

## 十四、开发顺序

1. Task 0 审计（完成）与共享底座；
2. MTPCS 激进版适配器（等价提取）；
3. MTPCS 激进版金样对账（样本不足则 `partial_parity` + `BLOCKED_DATA`）；
4. 成绩卡与资格门禁；
5. HRS 适配器（接线 + `BLOCKED_DATA`）；
6. HRS 金样对账（待数据）；
7. 双策略 CLI 与不可变报告；
8. 提交级审查；
9. 只部署 shadow / 只读能力（本轮**不部署**，等复审）；
10. 阶段三自动生效另议。

---

## 十五、验收标准

- 相同输入重复回放，结果逐字节一致；
- 任意未来数据访问都会失败；
- 当前参数回放能够通过金样对账（样本不足时为 `BLOCKED_DATA`，不记 `FAIL`）；
- 当前版本与「完全相同的候选」结果一致；
- 更高成本不会产生更高净收益；
- 缺 K 线、universe 或外部快照时 fail-closed；
- 两个策略的数据、状态与参数互不串用；
- 两个策略均能单独执行 `replay` / `parity` / `qualify`；
- 资格考试不写生产覆盖层、不改 `auto_apply`、不降 readiness 阈值、不触发实盘参数变更；
- MTPCS 提取前后逐字段输出一致，全量策略测试通过。

---

## 十六、提交边界

- 每个 Task 独立本地 commit；**不 push、不部署**，等待复审。
- 真实交易数据与快照**不提交**（`var/replay/` 进 `.gitignore`）。
- 回放引擎版本、费用模型版本写入报告，便于复审追溯。
