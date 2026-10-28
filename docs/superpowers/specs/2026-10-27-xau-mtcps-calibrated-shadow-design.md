# XAUUSDT MTCPS 黄金校准版影子验证设计

**日期**：2026-10-27

**状态**：实施物料与最终部署边界已完成，待独立 push / 部署授权

**目标品种**：`XAUUSDT`

**策略基线**：MTCPS 原版

**运行性质**：独立影子仿真，禁止真实下单

**虚拟初始权益**：300 USDT

## 1. 背景与问题定义

XAUUSDT 的 MTCPS 原版和激进版影子实例已于 2026-10-20 开始前向观察。行情采集、决策调度和研究数据落库均正常，但两个 profile 长期没有产生候选信号。审查发现，主要原因不是近期黄金没有波动，而是 ATR 过滤阈值沿用了 ETH 的绝对量纲：

| profile | 当前 `atr_abs_min` | 历史 1h ATR% 达标比例 |
|---|---:|---:|
| `xau_original` | 0.70% | 约 0.15% |
| `xau_aggressive` | 0.60% | 约 1.76% |

截至设计时点，XAUUSDT 近 1993 个 1h 样本的 ATR% 分布约为：P25 0.153%、P50 0.317%、P75 0.387%、P90 0.452%。近期 1h ATR14 约 0.277%，略低于 XAU 自身中位数，但并非异常低波动；日线 ATR14 约 1.30%。

因此，当前样本更准确的结论是“ETH 量纲的 ATR 门槛不适用于 XAU”，而不是“MTCPS 不适用于 XAU”。需要新增一个只校准 ATR 量纲的独立 profile，与原版、激进版并行前向验证。

## 2. 目标与非目标

### 2.1 目标

1. 在不改变现有原版和激进版的前提下，启动独立的“MTPCS 黄金校准版”。
2. 只校准 ATR 量纲及其历史初始化方式，保持 MTCPS 原版的其他信号、评分、入场、退出和风控规则不变。
3. 使 ATR 门槛基于 XAU 自身的闭合 1h K 线分布，并且在容器重启后可确定性重建。
4. 使用独立 instance、profile、run、300 USDT 虚拟权益和八周观察期，避免与现有实验混算。
5. 保留完整审计证据：校准数据截止时间、样本筛选规则、分位数、快照哈希、配置哈希和代码版本。

### 2.2 非目标

- 不修改 `xau_original` 或 `xau_aggressive` 的参数与历史数据。
- 不把校准版加入生产交易对池，不发送真实订单。
- 不依据当前或未来盈亏反向优化参数。
- 不调整 ADX、成交量、评分、止损、止盈、移动止损、时间止损或仓位规则。
- 不在本轮顺带修复所有既有动态 ATR 配置兼容问题。
- 不将校准版样本并入 2026-10-20 开始的原观察期。

## 3. 方案选择

### 3.1 候选方案

| 方案 | 说明 | 优点 | 主要问题 |
|---|---|---|---|
| A. 独立校准容器 | 新增独立 instance 与 profile，复用同一镜像和原版策略类 | 隔离完整、归因清晰、可独立停止 | 需增加少量配置与启动参数支持 |
| B. 在现有影子容器增加第三个 profile | 三个 profile 共用进程和 run | 改动看似较少 | 配置变更会造成现有 run 边界变化，启动故障也会连带影响对照组 |
| C. 仅离线回放校准参数 | 不新增在线 profile | 生产风险最低 | 无法取得独立前向决策样本，不满足立即验证目标 |

### 3.2 决策

采用方案 A：独立校准容器。

```yaml
service: mtcps-xau-calibrated-shadow
instance_id: mtcps_xau_calibrated_shadow
profile_id: xau_calibrated
run_id_prefix: xau-calibrated-shadow
symbol: XAUUSDT
execution_mode: shadow_batch
real_ordering_enabled: false
initial_virtual_equity_usdt: 300
```

校准容器复用 `ghcr.io/bearflower/trading-btc-eth:latest`，但使用独立环境变量、日志目录、数据目录和调度。不得复制第三套策略源码。

## 4. 策略基线与唯一变量

`xau_calibrated` 必须实例化 MTCPS 原版策略类。除以下 ATR 相关项外，其余配置逐项继承并冻结为 `xau_original` 启动校准版时的值：

- XAU 专属 `atr_abs_min`
- XAU 专属 ATR fallback 基准
- 动态 ATR 的正确最小历史样本键
- 从闭合 1h K 线确定性初始化 ATR 历史的开关

严禁同时调整：

- `adx_min`
- 成交量门槛
- 信号评分和等级门槛
- 入场价格与挂单有效期
- 止损、分批止盈和移动止损规则
- 杠杆、虚拟保证金和并发持仓限制
- 手续费、资金费与滑点口径

这样可以将候选信号数量和结果差异主要归因于 ATR 量纲适配，而不是多变量调参。

## 5. 校准数据与参数生成

### 5.1 数据截止与不可变快照

校准使用启动前已经闭合的 XAUUSDT 1h K 线。截止时间固定为实际部署前最近一个完整小时，不得包含部署后的 K 线。

快照至少记录：

- `symbol`
- 周期与起止时间
- 数据源
- 原始行数与有效行数
- 筛选规则版本
- ATR 算法版本
- 文件 SHA-256
- 生成时间
- Git commit

参数一旦生成即冻结到本次校准 run。后续市场数据不得回写或重算本 run 的启动参数。

### 5.2 有效样本规则

仅使用满足以下条件的闭合 1h K 线：

1. `open_time` 和 `close_time` 完整、合法且严格递增；相邻时间缺口单独记入 manifest，不因 TradFi 休市缺口直接淘汰缺口后的有效 K 线；
2. OHLC 为正数，并满足 `high >= max(open, close)`、`low <= min(open, close)`；
3. `volume > 0` 且 `trade_count > 0`；
4. `high > low`；
5. 不含重复时间戳；
6. ATR14 的 14 根预热窗口完整。

被排除的样本数量和原因必须进入 manifest，不能静默丢弃。

### 5.3 ATR 算法

必须复用生产决策路径的 ATR 定义：

```text
TR = max(
  high - low,
  abs(high - previous_close),
  abs(low - previous_close)
)

ATR14 = rolling_mean(TR, 14)
ATR_percent = ATR14 / close * 100
```

测试必须证明校准工具与生产指标实现对同一组 K 线输出一致，避免出现“校准算法”和“策略算法”两套口径。

### 5.4 参数生成规则

使用启动快照中所有有效 ATR% 样本计算：

- `atr_abs_min = P25 / 100`，写入配置时使用小数比例；
- `fallback_atr_percent = P50`，沿用动态过滤器的百分数单位；
- `atr_percentile = 50`；
- `atr_factor_strong` 继续沿用原版值；
- `min_history_count = 30`。

设计时点的预估结果为：

- `atr_abs_min` 约 0.0015，即 0.15%；
- `fallback_atr_percent` 约 0.32%；
- 常见动态有效门槛约 0.28%～0.32%。

这些只是设计期估算值。上线配置必须引用部署前快照实际计算结果及其哈希，不得直接抄写估算值。

## 6. 动态 ATR 的确定性初始化

现有动态 ATR 过滤器的历史主要存在进程内存中，容器重启后会冷启动；同时部分配置使用 `min_samples`，而实现读取 `min_history_count`。本轮采用局部、显式方案，避免改变其他生产策略：

1. `xau_calibrated` 只使用实现实际读取的 `min_history_count`；
2. 每次分析前，从该次决策可见的已闭合 1h K 线计算 ATR% 历史；
3. 用这组历史初始化校准 profile 的动态 ATR 过滤器；
4. 任何使用中的 K 线都不得晚于该决策的 `scheduled_at`；
5. 同一 K 线输入在首次运行、重试和容器重启后必须产生相同阈值。

初始化逻辑只对显式开启该能力的 `xau_calibrated` 生效。原版、激进版及生产 BTC/ETH 等策略的动态 ATR 行为不得改变。

本轮不全局解释或兼容 `min_samples`，以免无意改变现有生产阈值。该配置命名问题另行登记技术债。

## 7. 运行隔离与安全边界

### 7.1 数据隔离

- 使用独立 `instance_id`、`profile_id` 和 `run_id_prefix`。
- 使用独立的 300 USDT 虚拟初始权益。
- 决策事件仍只写入 `research` schema。
- 不写 `trading.*`，不进入生产看板、资金分配或熔断池。
- 报告不得将校准版事件与原版/激进版按同一 run 合并。

### 7.2 交易安全

校准容器必须继续满足现有影子安全边界：

- `execution_mode=shadow_batch`
- `real_ordering_enabled=false`
- 使用 `ReadOnlyBinanceClient`
- 禁用状态上报、资金分配和生产熔断接入
- 所有订单、撤单、杠杆、保证金和持仓修改接口 fail closed

新增容器的静态配置测试和运行时探针必须验证以上开关，不能只依赖人工查看 compose 文件。

### 7.3 调度隔离

校准版采用与现有影子实例错开的分钟调度，例如每小时第 20 分钟执行，降低同时请求 K 线和写库的瞬时压力。决策的 `scheduled_at` 仍归一到对应闭合小时，不能使用实际启动分钟改变信号时点。

## 8. Run 生命周期与观察周期

1. 校准版从部署后首个数据完整、无降级、无分析错误的标准调度事件开始计时。
2. 独立观察八周，不继承 2026-10-20 的原版/激进版起点。
3. 预计先在 2026-12-15 对三个 profile 做共同窗口的中期比较。
4. 校准版满八周后再进行正式准入评估。
5. 若中途修改校准参数、策略逻辑或数据筛选规则，必须关闭旧 run 并创建新 run，不得拼接样本。
6. 纯部署重启若代码、配置和快照哈希均未变化，应续用同一逻辑 run；若当前实现仍创建物理新 run，报告层必须按等价配置链聚合并保留边界，不得漏数或重复计数。

为了公平比较，报告至少同时输出：

- 校准版完整八周窗口；
- 从校准版起点开始的三 profile 同窗口结果；
- 原版/激进版自 2026-10-20 起的原始完整窗口。

## 9. 评估口径

校准版的候选信号增加不构成准入。正式评估继续采用现有 XAU 影子回放规范，包括：

- 净收益和收益因子；
- 最大回撤；
- 连续亏损；
- 候选数、成交数、未成交数与截止挂单数；
- 手续费、资金费和滑点后的净盈亏；
- 每边 1/2/4 bps 滑点压力；
- 活跃与非活跃时段分组；
- 数据质量和分析成功率；
- 15%/25%/30% 风险触停标记。

校准版仍需满足既有最小观察天数与有效虚拟交易样本要求。若八周后信号仍不足，只能判定“样本不足”，不得继续按盈亏结果放宽门槛。

## 10. 部署策略

### 10.1 保护现有对照组

上线优先采用定向部署新服务：构建包含新 profile 和必要代码的 btc-eth 镜像，仅创建 `mtcps-xau-calibrated-shadow`，不主动 recreate `mtcps-xau-shadow`。

若现有 CI 无法做到定向启动，实施计划必须先补齐该能力或明确说明会造成的 run 边界变化，经再次确认后才能执行全量 compose recreate。

现有原版和激进版在部署前后必须满足：

- 配置文件哈希不变；
- profile 列表不变；
- 观察期起点不变；
- 已有事件行数不减少；
- 不出现额外的同调度时点重复事件；
- 数据质量继续为正常。

### 10.2 回退

回退只停止校准容器：

```text
docker compose stop mtcps-xau-calibrated-shadow
```

停止校准版不得停止或重启 `mtcps-xau-shadow`、生产 `btc-eth-strategy` 或其他策略容器。研究数据保留用于审计，不做破坏性删除。

## 11. 测试与验收

### 11.1 单元与集成测试

至少覆盖：

1. 有效 K 线筛选与每类剔除原因；
2. ATR14 与生产指标实现一致；
3. P25/P50 单位换算正确；
4. 快照及 manifest 哈希篡改时 fail closed；
5. 校准 profile 映射到原版策略类；
6. `min_history_count` 被真实代码路径读取；
7. 首次运行、重试和重启后的 ATR 阈值一致；
8. 未来 K 线不会进入当前决策；
9. 原版和激进版行为不变；
10. compose 中校准服务具备全部只读安全开关；
11. 校准 run 与原 run 不会混写或混算；
12. 300 USDT 虚拟权益相互独立。

关键路径需做变异验证：故意破坏百分数换算、引入未来 K 线或移除历史初始化时，测试必须失败。

### 11.2 上线五层验证

按项目部署规范完成：

1. 容器状态与健康检查；
2. 镜像 ID；
3. VERSION/DEPLOY_ID；
4. 关键配置与代码文件 MD5；
5. 日志无错误。

此外必须验证：

- 校准 profile 与快照哈希正确加载；
- 首个标准调度事件成功落库；
- `data_quality_reason IS NULL`；
- ATR 阈值与本地同输入计算一致；
- 没有调用交易写接口；
- 原版和激进版仍正常产生决策事件；
- 19 个既有健康容器不因新增服务而缺失，新增后总数按预期增加。

## 12. 实施边界与工作区保护

当前主工作区存在其他未提交改动。实施阶段必须使用隔离 worktree 和 `codex/` 前缀分支，避免覆盖、提交或格式化用户现有改动。

设计文档提交后，下一步先编写逐文件实施计划，再进入测试驱动开发。未经实施计划复核，不直接修改生产代码或部署。

## 13. 完成定义

只有同时满足以下条件，才可宣布“黄金校准版已启动”：

1. 校准快照、manifest 和参数已冻结且可验证；
2. 新容器使用独立 instance/profile/run 并保持只读；
3. 新 profile 只改变本设计允许的 ATR 变量；
4. 首个无降级调度事件已成功落库；
5. ATR 阈值可由快照和公式复算；
6. 原版、激进版的配置、数据和运行状态未受影响；
7. 本地测试、变异测试、代码审查和上线五层验证全部通过；
8. 已记录校准版八周观察起止时间和回退命令。

## 14. 最终部署边界审查回标（2026-10-28）

校准服务与 `btc-eth-strategy`、`mtcps-xau-shadow` 复用
`trading-btc-eth:latest`。因此首次上线不得使用裸
`docker compose pull` / `docker compose up -d --remove-orphans`，否则镜像摘要变化会
recreate 既有两个消费者并切断其 active run。

首次创建使用本次 push 的 changed-files 一次性信号
`.github/deploy-modes/xau-calibrated-shadow-first-create` 选择
`calibrated-first-create` 模式。远端只允许：

```text
docker compose pull mtcps-xau-calibrated-shadow
docker compose up -d --no-deps mtcps-xau-calibrated-shadow
```

目标化分支只等待新容器通过真实运行态就绪探针，然后提前退出；不得运行全量
pull/up、`--remove-orphans`、数据库迁移或其他容器 restart。模式选择不依赖远端容器
是否存在，因此日后容器被删除不会被误判为“首次上线”；未命中一次性 changed-file
信号的普通部署继续走原全量路径。

新容器健康检查读取容器内 `/tmp` 原子 JSON 标记，并验证主 PID 存活、instance、
profile、run 前缀、cron、`shadow_batch` 及两个安全开关。标记仅在 profile 加载、DB
连接、run 准备、collector 装配和 scheduler 启动完成后写入；启动与退出均清理旧
标记。探针不访问或写入交易状态。

就绪超时自动回滚、人工回滚均只执行：

```text
docker compose stop mtcps-xau-calibrated-shadow
```

本回标只修订部署物料和健康判定，不代表已 push、已部署或八周观察期已开始。
