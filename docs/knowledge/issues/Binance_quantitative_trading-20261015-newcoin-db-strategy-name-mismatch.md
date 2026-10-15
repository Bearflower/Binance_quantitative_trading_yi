---
id: Binance_quantitative_trading-20261015-newcoin-db-strategy-name-mismatch
类型: issue
项目: Binance_quantitative_trading
标题: new_coin 平仓单据查询的 strategy 过滤值恒为「新币做空策略」（配置缺失 db_strategy_name），与 trade_records 实际写库名 new_coin 不符致盈亏回写/补正止损打标/周报复盘全部为空转
日期: 2026-10-15
标签: [bug, new_coin, trade_records, 策略名不一致, 配置缺失, 静默空转, 高优先级]
任务标识: newcoin_db_strategy_name_mismatch_2026-10-15
状态: 待复查
审查: 待审查
---

# new_coin 平仓单据查询 strategy 过滤值不匹配致盈亏回写空转

## 现象
（既有缺陷，**B 段已落地（未提交、未 push，待上线观察）**；2026-10-15 R2 时区修复上线后的业务验收复核中实证发现）

new_coin 在「平仓 → 回写盈亏」「条件单平仓 → 补正止损打标」「周一复盘统计」三条链路上，
**查询恒返回 0 行**，表现为 `realized_pnl` 永远写不进平仓单、亏损平仓的单不能补正止损打标、
周报恒输出「本周无交易记录，跳过复盘」。

- 症状是**静默**的：查询本身不报错，只留下 `warning`「未找到平仓订单记录，无法回写盈亏」。
- 本次时区修复（R2 / commit `94ca3ed`）只消除了同一查询上的 `DataError`，
  **不改变**本缺陷：查询能执行了，但依然匹配不到任何行。

## 根因
- 4 处查询原本把「本策略在 `trade_records.strategy` 列中的取值」写作
  `self.config.get('strategy', {}).get('db_strategy_name', '新币做空策略')`：
  [strategy.py](../../../strategies/new_coin/strategy.py) 的 `_weekly_review` 周报、
  `_backfill_close_pnl`、`_monitor_positions` 的「补正止损打标」分支与「平仓回写」分支。
  **行号口径（2026-10-15 B 段落地后更新）**：
  容器基线 `94ca3ed`（错误写法所在行）= **L925 / L1343 / L1456 / L1505**；
  B 段落地前的本地旧快照（含在途未提交改动，md5 `431649bb…` / 2793 行）= L1121 / L1539 / L1652 / L1701；
  记录期内该本地行号曾在 L1546/L1659/L1708 与 L1539/L1652/L1701 间漂移，HEAD 亦已由 `94ca3ed` 前进到 `8071f06`。
  本次 B 段（方案 1）落地时，对方 WIP 已消失，`strategy.py` 回落至 HEAD 口径，本地行号 = **L925 / L1343 / L1456 / L1505**（与容器基线一致）；
  4 处替换为 `self.my_record_name` 后**实测运行行号** = **L929 / L1347 / L1460 / L1509**（因新增 1 行 `import`、`__init__` 缓存 2 行、`_weekly_review` 内 1 行注释而下移 4）。
  **一律按函数名定位**，勿按固定行号。
- 配置里**从未定义** `strategy.db_strategy_name`（`strategies/new_coin/config.yaml` 无此键；
  容器内实测 `load_strategy_config('/app/strategies/new_coin')` → `strategy.db_strategy_name = MISSING`）
  → 4 处查询的 `$1` 恒为中文默认值 `'新币做空策略'`。
- 但本策略在 `trading.trade_records` 中的行**全部**记在 `strategy = 'new_coin'`
  （写入方 `strategies/new_coin/main.py` 原为 L107 `TradeLogger(db_manager, "new_coin")` 硬编码；
  **B 段已统一为同一来源**：现 L108-109 `my_record_name = load_ownership_config(config)['my_record_name']` → `TradeLogger(db_manager, my_record_name)`；
  `config.yaml` L8 明确 `record_name: "new_coin"` 即「与 trade_records 写库名一致」）。
  全表 `strategy` 取值仅 `hrs / btc_eth_aggressive / new_coin / btc_eth`，
  **不存在**任何 `'新币做空策略'` 行。
- 后果链（`close_order` / `close_rec` 恒为 None）：
  1. `elif not close_order:` → 只打 warning，不调 `update_realized_pnl`
     → **`_backfill_close_pnl` 这条回写路径永不执行**（new_coin 生产代码仅两处调用 `update_realized_pnl`，
     均被该查询门控）。**⚠️ 措辞修正（2026-10-15 复核）**：`realized_pnl` 列本身**另有兜底**——
     ai_tuner 的 `IncomeReconciler`（通用 PnL 校准器，[income_reconciler.py L263](../../../ai_tuner/reconciler/income_reconciler.py#L263)）
     以 `realized_pnl IS NULL` 为条件匹配 Binance income 后 `UPDATE ... SET realized_pnl=$1 WHERE id=$2`（**按 id、无 strategy 过滤**），
     实测 `new_coin` 有 372 行 pnl 非空即来自该路径。故「pnl 列会缺值」**不能**作为本条的后果断言。
  2. `if close_rec and close_rec.get('realized_pnl') is not None:` 恒不成立
     → 条件单自动平仓的亏损**无法补正止损打标**、`consecutive_losses` 也不据此累加（风控统计口径缺失）。
     **独立佐证**：实测 `new_coin` 全表 869 行 `close_reason` **全部为 NULL**（`IncomeReconciler` 只写 `realized_pnl`，不写 `close_reason`），
     说明止损打标确实从未落地。
  3. `if not records:` → 周一复盘恒输出「本周无交易记录，跳过复盘」（同一 SQL 形态，`$1` 不同已实测 0 行 vs ≥1 行）。
- 该写法在 `db_strategy_name` 未配置时**默认值本身就是错的**（中文旧显示名），属「错误默认值 + 配置缺失」
  双重叠加；且只有 new_coin 读这个键（全仓 `grep db_strategy_name` 仅命中 new_coin 与其测试），
  其他策略配置里只有 `record_name`（英文写库名）→ **不是全策略系统性缺陷，是 new_coin 的遗留写法**。

## 解决
**B 段已落地（代码已改，尚未提交 / 未 push，待上线观察）**；方案见 `docs/plans/fix-2026-10-15-new-coin-db-strategy-name-mismatch-requirements.md`。

按**方案 1** 实施：统一改为从既有权威来源取策略名 —— 模块级复用
`shared/position_ownership.load_ownership_config(config)['my_record_name']`
（其实现即 `config['strategy']['record_name']`，缺失时回退 `strategy.name` 并记 warning），
在 `NewCoinStrategy.__init__` 缓存为 `self.my_record_name`，4 处查询统一使用它；
`main.py` 的 `TradeLogger(db_manager, "new_coin")` 亦已改为同一来源，实现「写库名」与「查库名」单一来源。
备选方案 2（改动最小）：仅在 `config.yaml` 补 `strategy.db_strategy_name: new_coin`——
但会把错误默认值 `'新币做空策略'` 留在代码里，**已否决**。

## 验证
- **生产实证（2026-10-15，只读查询）**：
  - 运行时配置：容器内 `load_strategy_config('/app/strategies/new_coin')` → `strategy.db_strategy_name = '<MISSING>'`。
  - 同窗口对照（MSTUUSDT BUY、`executed_at` 落在 `2026-10-13 14:00` ~ `2026-10-14 14:00`）：
    `strategy='new_coin'` → **3 行**；`strategy='新币做空策略'` → **0 行**。
  - 全表 `group by strategy`：无 `'新币做空策略'` 取值。
  - 调用点静态证据：`grep update_realized_pnl\(` 在 new_coin 生产代码仅命中 `strategy.py` 两处调用（记录时为 `:1361 / 1524`；B 段落地后实测为 **`:1358 / 1521`**，按函数名定位、勿按固定行号），二者均被上述查询门控。
- **B 段落地验证证据（2026-10-15，本地工作区；代码已改、未提交）**：
  - 静态：AST 解析通过；`strategy.py` 中 `db_strategy_name` 全文件计数 = **0**，`self.my_record_name` = **5** 次（`__init__` 1 + 4 处查询各 1）；`'新币做空策略'` 字面量已从查询取值处消失（仅余日志文案等非业务字符串）。
  - 运行时取值：`load_ownership_config(cfg)['my_record_name'] == 'new_coin'`（config 来源 `load_strategy_config('strategies/new_coin')`，`has_overrides=True`、`active_version=V20260830`）。
  - 新增 AC 用例 `strategies/new_coin/tests/test_db_strategy_name_unified.py` → **7 passed**，覆盖 AC1（`record_name` 有值分支）/ AC5（缺失回退 `strategy.name` + warning 恰 1 条）/ AC4（周报 `$1=='new_coin'` 且不早退）/ AC3（补正止损打标与平仓回写两分支 `$1` + `LIMIT 1`）/ `_backfill_close_pnl` 附加 / 哨兵恒等（证明 `$1` 取自 `self.my_record_name` 属性而非碰巧等值）；4 处替换点全部经运行期验证。
  - 零回归对照：以 `git archive HEAD` 归档快照为基线 vs 工作区，对 `strategies/new_coin/` + `tests/test_strategies/test_new_coin_kline.py` 夹逼比对，两侧均 **`25 failed, 204 passed`**，**失败用例 ID 集合逐条一致**（唯一差异为断言内动态时间戳）；25 项属已知既有测试债（见下方关联 issue）。
  - 代码规范检测通过（无违规）；幻觉测试 10 项通过；1 条 fixture 忠实性建议（`object.__new__` 绕过 `__init__` 须补派生属性）已采纳修复。
- **待验证项（修复后必须补证）**：
  1. ~~4 处查询 `$1` 等于写库名的断言（单测，不依赖 mock DB 的宽松匹配）~~ → **已补**（`test_db_strategy_name_unified.py` 直断言 driver 收到的第一个位置参数，非宽松匹配）；
  2. 端到端：构造/复演一次真实平仓，确认 `realized_pnl` **落到预期的那一行**（注意
     `update_realized_pnl` 降级匹配模式受 `order_type NOT IN ('LIMIT','CONDITIONAL_ORDER')`、
     `realized_pnl IS NULL`、`±time_window`（上限 1800s）三重约束，需实测而非推断）→ **仍待上线后实测**；
  3. ~~周报：周一触发时不再输出「本周无交易记录，跳过复盘」~~ → **已补**（AC4 用例）。
- **测试盲区提示（B 段已修正）**：相关用例 fixture 原直接把 `db_strategy_name` 写成 `'新币做空策略'`
  （[test_realized_pnl_fix.py L68/L741](../../../strategies/new_coin/test_realized_pnl_fix.py#L68)、
  [tests/test_stop_loss_mark.py L23](../../../strategies/new_coin/tests/test_stop_loss_mark.py#L23)）
  → 测试**固化了错误契约**；B 段已把 fixture 口径修正为 `record_name: 'new_coin'`
  （`test_stop_loss_mark.py` 的 `object.__new__` fixture 另补派生属性 `strategy.my_record_name = 'new_coin'`），
  并新增 `tests/test_db_strategy_name_unified.py` 直接断言 `$1`；`tests/test_strategies/test_new_coin_kline.py` fixture 策略名键亦同步为 `'name': 'new_coin'` + `'record_name': 'new_coin'`。

## 适用范围与修订
- 适用于 `strategies/new_coin/strategy.py` 的 4 处查询（容器基线 L925 / L1343 / L1456 / L1505；
  对应函数名 `_weekly_review` / `_backfill_close_pnl` / `_monitor_positions` 的「补正止损打标」与「平仓回写」两分支）。
- 与 R2「executed_at 时区修复」（commit `94ca3ed`）**同源但独立**：R2 修的是「参数时区绑定」，
  本条是「过滤值口径」；R2 有意不改本处以维持其「失败集合不扩大」证据链。
- 历史影响需评估：`new_coin` 平仓单缺 `realized_pnl` 的存量数据（何时开始失效无法精确归因，
  至少 2026-10-13 之后无新回写；全表仅 869 行的写库名口径与查询口径自始不一致）→
  是否一次性补写历史盈亏，见方案 §6（需单独授权，不在代码修复范围内）。
- 无替代条目。
- **B 段落点摘要（2026-10-15）**：`strategy.py`（+1 导入、+1 `__init__` 缓存行、4 处替换）、`main.py`（去硬编码、统一来源）、3 处 fixture 口径修正、新增 `strategies/new_coin/tests/test_db_strategy_name_unified.py`（7 用例全绿）；**代码未提交、未 push**，待提交后走上线观察（见方案 §7）。

## 教训
1. **「查询关键字」必须是唯一来源，不能靠代码里的默认值兜底**：默认值是中文显示名、写库名是英文
   record_name，二者不一致时配置缺一项就静默空转——**空转比报错更难发现**（只有一条 warning）。
   凡涉及「按策略名检索自己写入的数据」，应统一使用同一常量/配置源（本项目为 `strategy.record_name`）。
2. **配置缺失 + 错误默认值 = 定时炸弹**：`config.get(k, <错值>)` 的写法让「忘记配置」等价于「功能关闭」，
   且不会在启动期暴露。关键标识类配置应在启动时校验并**显式报错或强告警**，而不是取一个可能过期的旧名兜底。
3. **验收不能只看错误计数**：R2 上线后 `invalid input for query argument` 计数为 0，看似「恢复正常」，
   但业务实质（盈亏落库）从未发生——**「没有报错」不等于「功能生效」**，
   必须独立核对「写入是否真的发生」（本轮靠 DB 侧逐窗口对照才发现）。
4. **测试 fixture 写成什么口径，就会保护什么错误**：fixture 把错误名写死，使得缺陷在测试里完全隐形。
（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [new_coin_executed_at_timezone 交接](../../handoffs/new_coin_executed_at_timezone_2026-10-15.md)（不随 Git 同步）
- 修复方案: `docs/plans/fix-2026-10-15-new-coin-db-strategy-name-mismatch-requirements.md`
- 持久证据: [strategies/new_coin/strategy.py](../../../strategies/new_coin/strategy.py) 4 处查询
  （容器基线 L925 / L1343 / L1456 / L1505；「空匹配后只 warning」「补正止损打标门控」为其紧邻分支，按函数名定位）；[strategies/new_coin/config.yaml](../../../strategies/new_coin/config.yaml) L8（`record_name:"new_coin"`，无 `db_strategy_name`，容器与本地一致，容器内 `grep -c db_strategy_name` = 0）；
  [strategies/new_coin/main.py](../../../strategies/new_coin/main.py) L108-109（原 L107 为 `TradeLogger(db_manager, "new_coin")` 硬编码，**B 段已统一**为 `my_record_name = load_ownership_config(config)['my_record_name']` → `TradeLogger(db_manager, my_record_name)`）；
  [shared/position_ownership.py](../../../shared/position_ownership.py) L175（`load_ownership_config` 定义，返回 dict 的 `my_record_name` 键在 L208）——
  该文件虽被 `8071f06` 修改，**此权威取名入口仍有效**，方案1 依赖不变
- 必要证据摘要: `config.strategy.db_strategy_name` 未配置（容器内实测 MISSING）→ 4 处查询 `$1='新币做空策略'`；
  同窗口对照组 `new_coin` 3 行 / `新币做空策略` 0 行；new_coin 生产代码仅 2 处 `update_realized_pnl` 调用且均被该查询门控
  → 盈亏回写、补正止损打标、周报复盘三链路静默空转；既有缺陷、与 R2 时区修复无关。待验证项见「验证」。
- 相关: [Binance_quantitative_trading-20261015-newcoin-stoploss-entry-time-type](./Binance_quantitative_trading-20261015-newcoin-stoploss-entry-time-type.md)（同链路的止损打标问题）、
  [Binance_quantitative_trading-20261015-newcoin-legacy-test-debt](./Binance_quantitative_trading-20261015-newcoin-legacy-test-debt.md)（相关 fixture 陈旧）、
  [Binance_quantitative_trading-20261015-naive-timestamp-beijing-convention](../decisions/Binance_quantitative_trading-20261015-naive-timestamp-beijing-convention.md)（R2 同源修复）
