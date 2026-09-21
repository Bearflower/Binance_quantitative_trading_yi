# 看板 4 项问题修复方案

## Context（背景）

用户在看板（http://43.156.242.184）发现 4 个问题：

1. **AI 调优执行**：卡片本应只展示最近一批（0913）调优结果，但页面仍混入了 0913 之外的历史批次（0911、0906）执行记录。
2. **调优执行 + 月度资金分配两块卡片**：目前都是全宽独占一行，希望缩窄后放在同一行（左右并排）。
3. **最近止损次数为 0**：风控看板显示止损次数恒为 0，但近 7 天实际存在 26 条带负盈亏的平仓记录，属数据缺失。
4. **策略详情页数据口径不一致**：汇总卡（order_count/closed_count）与底部币种明细表的数值互相矛盾（如 ETHUSDT order_count=0 却有盈亏、汇总 XRP=2 而明细 XRP=3）。

目标：修复前端展示 + 根治止损打标链路 + 统一详情页数据口径。

---

## 一、需求 1·调优执行只显示最新批次

**根因**：
- 前端 `renderTuningRuns`（main.js:341）只取 `runs[0]`，但后端 `_get_ai_tuning_runs`（data_service_docker.py:1832）返回所有批次 + 按 executed_at 排序的单条记录，多个 run_key 混在数组中。
- 需求为"在下一次 AI 调优前只显示 0913 那一批"。正确做法应以 `run_key` 为批，取最新一个 `run_key` 的所有策略结果。

**修复方案**：
- 后端 `_get_ai_tuning_runs`：按 `run_key` 分组，仅返回**最新一个 run_key** 批次的结果（该批内各策略按 executed_at 排序）。
- 前端 `renderTuningRuns`：改为渲染整个 `tuning_runs` 数组（该数组已是单批），每行一个策略。
- 文件：
  - `/Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py`（`_get_ai_tuning_runs`）
  - `/Users/yl/vscode/Binance_quantitative_trading/dashboard/frontend/js/main.js`（`renderTuningRuns`）

---

## 二、需求 2·两卡片缩窄同排

**根因**：两块卡均套 `ai-card ai-card-wide`（全宽独占一行）。

**修复方案**：
- 将 `AI 调优执行` 与 `月度资金分配` 两块卡片从宽卡片改为**半宽并排**（使用包裹容器 flex 布局，或改为 `ai-card` 非 wide 并置于同行 grid）。
- 文件：
  - `/Users/yl/vscode/Binance_quantitative_trading/dashboard/frontend/index.html`（两块卡片外层结构）
  - `/Users/yl/vscode/Binance_quantitative_trading/dashboard/frontend/css/style.css`（并排布局样式）

---

## 三、问题 3·止损次数为 0（核心逻辑修复）

**根因（已确认）**：
- 看板统计依赖 `trade_records.close_reason='STOP_LOSS'`（data_service_docker.py:2319）。
- 但生产库 2467 条 `close_reason` 全为 NULL，说明打标逻辑**从未成功落地**。
- 打标逻辑在各策略已存在（new_coin/strategy.py:1131、btc_eth/strategy.py:3470、shared/base_strategy.py:215），但存在多个未被真实止损路径触发的缺口：
  - **new_coin**（strategy.py:1038）：持仓由条件单自动平仓、`pnl = _get_position_pnl()` 返回 `None` 时，走 `del self.positions[symbol]` 直接删除持仓**并 `continue`，跳过 1112 的 `pnl<0` 打标**。这是主缺口。
  - **btc_eth**：打标只在 `_close_position` 被以非止盈 reason 调用时触发（3479），依赖主循环主动调用平仓；条件单自动成交后若未走该路径则不打标。
  - 生产近 30h 日志无任何"止损打标"记录，印证打标点全部未触发。

**修复方案（仅修打标链路，历史不回补）**：
1. **new_coin**：在"持仓已平仓 + pnl=None"分支（strategy.py:1038-1047），补充判空后的止损打标——当查不到具体 pnl 但确为平仓时，也从 `trade_records` 读取该笔平仓的 `realized_pnl` 或按 `side='BUY'` 定位平仓单，若 pnl<0 或平仓单可判定为止损则调用 `mark_stop_loss`；至少保证亏损平仓必然打标。
2. **btc_eth**：审计 `_close_position` 打标触发是否覆盖"条件单自动成交→持仓消失"路径（3103-3112 已有，但需确认主循环确实会以非止盈 reason 调用 `_close_position`）。若缺失，在持仓消失清理处补 `_mark_stop_loss_if_needed`。
3. **hrs**：确认其止损平仓调用 `mark_stop_loss` 的条件是否覆盖条件单自动平仓路径。
4. 统一保证：**任何策略 / 币种 / 平仓路径下，亏损平仓必打 `STOP_LOSS` 标记**；止盈（TP1/TP2/TRAILING_STOP）不打。
5. 新增/更新单元测试（测试止损平仓确实落 `close_reason='STOP_LOSS'`）。
6. 修复后仅 re-deploy 受影响策略容器（new_coin、btc_eth、hrs、btc_eth_aggressive）及 dashboard-api。

**主要文件**：
- `strategies/new_coin/strategy.py`
- `strategies/btc_eth/strategy.py`
- `strategies/hrs/strategy.py`
- `strategies/btc_eth/tests/test_stop_loss_mark.py`、`strategies/new_coin/tests/test_stop_loss_mark.py`（补测试）
- `shared/trade_logger.py`（如需增强 `_get_position_pnl` 判定的公共辅助可复用）

---

## 四、问题 4·详情页数据口径不一致

**根因（已确认）**：
- 详情页汇总卡数据来自 `get_strategies`（data_service_docker.py:606-644）→ `order_count` 来自 DB `trade_records` 按 `_STRATEGY_KEY_MAP` 映射。
- 币种明细来自 `get_strategy_symbols`（data_service_docker.py:695-808）→ `order_count` 来自 DB 按 `_db_strategy_names`（含多个落库名）匹配，`closed_count/fill_count` 来自 Binance income API。
- 问题：**汇总与明细的策略名映射/归属逻辑不一致**，导致：
  - 明细 ETHUSDT 在 `_db_strategy_names` 匹配到记录或有 income 归属，但汇总 `_STRATEGY_KEY_MAP` 里 ETH 对应策略落库数含义不同 → order_count 对不上。
  - `daily_counts[sym] = sym["order_count"]` 沿用了明细口径，与汇总卡也不同。

**修复方案**：统一汇总卡与币种明细的**口径**：
- 汇总卡的 `order_count/closed_count/win_count/loss_count` 改为与明细一致的来源：
  - `order_count`：汇总该策略所有 symbol 的 DB `trade_records` 计数（按 `_db_strategy_names` 匹配，与明细一致）。
  - `closed_count/win_count/loss_count`：按 symbol income REALIZED_PNL 聚合后上卷求和（与明细 wins/losses 求和一致）。
  - 修正 `daily_counts` 用同一 DB 口径生成，保证图表与卡一致。
- 若涉及不同策略（btc_eth vs btc_eth_aggressive 对比），确保策略名映射（`_STRATEGY_KEY_MAP` / `_db_strategy_names`）在汇总与明细完全一致。
- 文件：
  - `/Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py`（`get_strategies` 与 `get_strategy_detail`/`get_strategy_symbols` 口径统一）

---

## 五、部署与验证

1. **前端变更**（需求1、2）：仅重打包 dashboard 前端 + dashboard-api，deploy.sh。
2. **后端变更**（问题3、4）：
   - 问题4 → dashboard-api。
   - 问题3 → new_coin、btc_eth、hrs、btc_eth_aggressive 策略容器（按变更范围分析按需重建）。
3. **验证清单**：
   - 需求1：调优执行卡只显示 0913 一批所有策略。
   - 需求2：调优执行与月度资金分配两卡同排。
   - 问题3：触发一笔止损平仓后，看板"最近止损次数" > 0；DB `close_reason='STOP_LOSS'` 出现。单元测试通过。
   - 问题4：详情页汇总卡 order_count/closed_count 与币种明细表求和完全一致；切日/周/月一致。
   - 五层部署验证（VERSION/MD5）。

---

## 关键文件总览

| 文件 | 变更 |
|------|------|
| dashboard/backend/services/data_service_docker.py | 需求1（调优批次）、问题4（口径统一） |
| dashboard/frontend/js/main.js | 需求1（渲染）、需求2（布局）可能涉及 |
| dashboard/frontend/index.html | 需求2（卡片并排） |
| dashboard/frontend/css/style.css | 需求2（样式） |
| strategies/new_coin/strategy.py | 问题3（pnl=None 补打标） |
| strategies/btc_eth/strategy.py | 问题3（打标触发覆盖条件单路径） |
| strategies/hrs/strategy.py | 问题3（确认打标覆盖） |
| strategies/{new_coin,btc_eth}/tests/test_stop_loss_mark.py | 问题3（补测试） |