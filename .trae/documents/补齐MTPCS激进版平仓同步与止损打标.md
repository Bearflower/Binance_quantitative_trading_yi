# 补齐 MTPCS 激进版平仓同步与止损打标逻辑

## Context（背景）

用户要求 MTPCS（btc_eth）与 MTPCS 激进版（btc_eth_aggressive）都参考 HRS 移动止损方法完成「仓位分批止损 + 移动止损」。

经代码对比确认：

- **两版的分批止盈 / 移动止损算法本体完全一致**（`_check_partial_take_profit`、`_check_dynamic_trailing`、`_calculate_dynamic_trailing_stop`、`_sync_trailing_stop_order`，从 `_check_partial_take_profit` 起字节级相同），**无需改动**。
- 用户明确选择：不切换到 HRS 的简单 `best_price - ATR×倍数` 算法，保留 MTPCS 的提前启动移动止损。
- **激进版缺失两处原版已有的平仓鲁棒性逻辑**（∎114 行差异的全部来源）：
  1. `_sync_position_with_exchange()` —— 平仓前持仓同步。当 TP1/TP2/移动止损/止损条件单在**周期外**成交时，本地 `position.current_quantity` 会失真，导致后续分批止损/移动止损记账错误。这是「补齐分批止损 / 移动止损」正确性的核心。
  2. `_mark_stop_loss_if_needed()` —— 止损打标（供风控看板「最近止损次数」统计）。
  3. 模块常量 `_TAKE_PROFIT_CLOSE_REASONS = {"TP1", "TP2", "TRAILING_STOP"}`。

目标：仅向激进版移植这三处缺失逻辑，两策略风险行为对齐。

## 待修改文件

- `strategies/btc_eth_aggressive/strategy.py`
- 移植来源：`strategies/btc_eth/strategy.py`

> 激进版 `_close_position` 已有的内联 -2022 ReduceOnly 处理（L3219-3257）正常工作，**保留不重构**（最小改动原则），前置同步是增量补充。

## 实现步骤

### 1. 模块常量
在 `btc_eth_aggressive/strategy.py` 导入区之后的模块级位置，添加：

```python
_TAKE_PROFIT_CLOSE_REASONS = {"TP1", "TP2", "TRAILING_STOP"}
```

（与 `btc_eth/strategy.py` L37 完全一致）

### 2. 新增 `_sync_position_with_exchange`
从 `btc_eth/strategy.py` L3002-3076 **原样提取**，插入到激进版 `_close_position`（L3008）之前。

方法签名与逻辑（严格保留原版注释与返回契约 `{'closed','partially_closed','actual_quantity'}`）：
- 调 `self.binance.get_position(symbol)`
- 交易所返回空列表 / `posAmt≈0` → `position.current_quantity=0; direction='FLAT'; closed=True`
- 实际持仓 < 本地记录 → `partially_closed=True`，同步 `current_quantity`
- 其余 → 正常

### 3. 新增 `_mark_stop_loss_if_needed`
从 `btc_eth/strategy.py` L3469-3495 **原样提取**，插入到激进版 `_check_partial_take_profit`（L3383）之前。

逻辑：`close_reason` 属于 `_TAKE_PROFIT_CLOSE_REASONS` 则跳过；否则经 `getattr(self.binance,'trade_logger',None)` 探测，存在 `log_stop_loss` 才调用，异常静默跳过不影响平仓主流程。

### 4. `_close_position` 前置同步
在激进版 `_close_position` 中 `close_side = ...`（L3031）之后、`actual_close_quantity = min(...)`（L3034）之前插入（照搬原版 L3102-3112）：

```python
            # 入口持仓同步：先查交易所实际持仓量（条件单可能已部分成交）
            sync_result = await self._sync_position_with_exchange(symbol, position, close_reason)
            if sync_result['closed']:
                # 交易所已无持仓，说明已被条件单平仓，视为平仓成功
                logger.info(
                    f"{symbol} 平仓前同步发现交易所已无持仓，跳过下单",
                    close_reason=close_reason
                )
                # 止损打标：本次平仓若为止损（非止盈）且由条件单促成，记录止损供看板统计
                await self._mark_stop_loss_if_needed(symbol, position, close_reason)
                return True
```

### 5. 平仓成交后止损打标
在激进版 `_close_position` 回写 `update_realized_pnl` 成功后（约 L3310 之后），照搬原版 L3396-3397：

```python
                        # 止损打标：平仓成交后，若本次为止损（非止盈）则记录止损供看板统计
                        await self._mark_stop_loss_if_needed(symbol, position, close_reason, pnl)
```

## 验证

1. **幻觉测试**：核对所有引用真实存在——
   - `self.binance.get_position`（shared/binance_api.py 有定义）
   - `trade_logger.log_stop_loss` 与 `CLOSE_REASON_STOP_LOSS`（shared/trade_logger.py L441、L69 有定义）
   - 导入无新增依赖（两方法只用现有成员）
2. **单元测试**：新建 `strategies/btc_eth_aggressive/tests/test_stop_loss_mark.py`，镜像原版 `strategies/btc_eth/tests/test_stop_loss_mark.py`，覆盖：
   - 止盈不打标（TP1/TP2/TRAILING_STOP）
   - 止损类必打标（STOP_LOSS/TIME_STOP 等）
   - 平仓方向映射（LONG→SELL，SHORT→BUY）
   - realized_pnl 透传
   - trade_logger 缺失 / 缺 log_stop_loss 时静默跳过
   - `_sync_position_with_exchange` 的 closed / partially_closed / 正常 / 异常 四种分支（用 mock 的 get_position）
3. **回归测试**：运行 `strategies/btc_eth_aggressive` 与 `strategies/btc_eth` 现有测试，确保无回归。
4. **语法/导入确认**：`python -c "from strategies.btc_eth_aggressive.strategy import BTCEthStrategy"` 通过。

## 部署

仅改动激进版 `strategies/btc_eth_aggressive/`。部署时按 `.trae/rules/deployment.md` 变更范围分析确定激进版对应容器，仅按需重建受影响容器并执行五层验证。