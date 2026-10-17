---
id: Binance_quantitative_trading-20261015-newcoin-stoploss-entry-time-type
类型: issue
项目: Binance_quantitative_trading
标题: new_coin 止损打标 entry_time 类型不符（str 传入 datetime 形参）致 AttributeError 被吞、持仓残留 + consecutive_losses 虚增
日期: 2026-10-15
标签: [bug, new_coin, 止损打标, 类型不符, 静默吞异常, 高优先级]
任务标识: newcoin_stoploss_entry_time_type_2026-10-15
状态: 待复查
审查: 待审查
---

# new_coin 止损打标 entry_time 类型不符（str 传入 datetime 形参）致 AttributeError 被吞

## 现象
（既有缺陷，**尚未修复**；GLM-5.3 在 R2 发布前独立复审中实证复现，较首轮估计更严重）

new_coin 策略在「亏损平仓 → 加入止损监控列表（黑名单检测）→ 止损打标」链路上抛
`AttributeError: 'str' object has no attribute 'isoformat'`，但该异常被上游 `except Exception`
**静默吞掉**，表现为止损打标未落地、持仓记录未清理，且据此产生连锁副作用。

## 根因
- 调用点 [strategy.py L1564](../../../strategies/new_coin/strategy.py#L1564-L1568)：
  `await self._add_to_stop_loss_monitor(symbol=symbol, entry_price=entry_price, entry_time=entry_time)`。
- 此处 `entry_time` 实为**字符串**（来源见 L438 / L1183 / L2183，均以 `isoformat()` 字符串形式存入 `self.positions`）。
- 但 `_add_to_stop_loss_monitor` 的形参签名声明为 `entry_time: datetime`，其函数体
  [L2520](../../../strategies/new_coin/strategy.py#L2517-L2522) 直接调用 `entry_time.isoformat()`
  → `AttributeError: 'str' object has no attribute 'isoformat'`。
- 该异常沿调用栈上抛，被 `_monitor_positions` 的逐币兜底
  [L1592 `except Exception`](../../../strategies/new_coin/strategy.py#L1592-L1596) 吞掉（仅记 logger.error）。
- 后果链：止损打标 `log_stop_loss` 未执行 → 紧随其后的
  [L1586 `del self.positions[symbol]`](../../../strategies/new_coin/strategy.py#L1585-L1587) 也**不会执行**
  → 持仓记录残留 → **每轮重复判定「已平仓」**，`consecutive_losses` 每轮 +1
  → 可**误触发整策略暂停**（`_check_consecutive_loss_pause`）。
- 历史包袱：该缺陷在发布基线 `fce42bf`（及更早 HEAD L1550 / L2506）中已存在，**非 R2 引入、不在 R2 diff 行内**，
  故不阻塞 R2 发布，但资金/风控语义受损，须独立修复。

## 解决
**未解决（另开工单，高优先级）**。修复方向：调用处改为传**解析后的 datetime**
（可复用 R2 新增的 `_entry_time_to_naive_beijing` 把字符串解析为 datetime 后再传入），
使 `_add_to_stop_loss_monitor` 内部 `.isoformat()` 成立；同时评估是否把该函数形参类型标注与调用契约对齐。

> **注意测试盲区**：整个测试套件对 `_add_to_stop_loss_monitor` 做了 mock，
> 现有测试**不会**暴露此 AttributeError → 修复须补一条不 mock 该方法的直调用例。

## 验证
- **复现证据（GLM-5.3 独立复审，2026-10-15）**：按上述调用链构造 `entry_time` 为字符串入参，
  `_add_to_stop_loss_monitor` 内 `.isoformat()` 抛 `AttributeError`；证据链见发布前 C 段复审记录。
- 静态证据：`grep` 可确认调用点 L1564 的实参来源为字符串（L438/L1183/L2183），
  且形参标注为 `datetime`（L2504-L2507 docstring `entry_time: 入场时间`）。
- **待验证项**：修复后须新增（不 mock 该方法的）回归用例，断言止损打标落地、
  `del positions` 执行、`consecutive_losses` 不再每轮 +1；并确认不再误触发策略暂停。

## 适用范围与修订
- 适用于 `strategies/new_coin/strategy.py` 止损/黑名单监控链路；基线 `fce42bf`（2026-10-15 复核）。
- 与 R2「executed_at 时区修复」（commit `94ca3ed`）**同源但独立**：R2 修的是查询参数时区，
  本缺陷是**类型不符**（str vs datetime）；R2 有意不改本处以避免破坏其「失败集合不扩大」证据链。
- 修复可复用 [决策：naive 北京时间归一约定](../decisions/Binance_quantitative_trading-20261015-naive-timestamp-beijing-convention.md) 中的 `_entry_time_to_naive_beijing`。
- 无替代条目。

## 教训
1. **「静默吞异常」会放大次生灾害**：本例真正的 bug 是一个 `AttributeError`，但被逐币 `try/except Exception`
   伪装成「止损打标没做」的表象，还顺带让 `del positions` 与 `consecutive_losses` 语义全乱——
   排查时若只看表象（止损统计缺失）会漏掉「持仓残留 + 计数虚增 + 误暂停」的严重链条。
   对「清理/打标」这类**必须成功**的副作用，不应与主流程共用同一个宽容 except。
2. **形参类型标注必须与调用契约一致，且要有防呆**：`entry_time: datetime` 却稳定收到 `str`，
   在动态语言里不会在定义处报错，只在 `.isoformat()` 处爆——应在函数入口做
   `isinstance` 归一或显式解析（如统一走 `_entry_time_to_naive_beijing`），把类型错误前移到可观测处。
3. **mock 越全，覆盖越瞎**：整条链路被 mock 后，真实的类型/序列化错误永远测不出来；
   对「曾经用 mock 保护」的关键函数，应至少保留一条**不 mock 的直调用例**。
（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [new_coin_executed_at_timezone 交接](../../handoffs/new_coin_executed_at_timezone_2026-10-15.md)（不随 Git 同步）
- 持久证据: [strategies/new_coin/strategy.py](../../../strategies/new_coin/strategy.py) L1564（调用点）、L2504-L2522（`_add_to_stop_loss_monitor` 形参标注与 `.isoformat()`）、L1592（兜底 except）、L438/L1183/L2183（`entry_time` 字符串来源）
- 必要证据摘要: 调用点 L1564 以字符串 `entry_time` 传入签名 `entry_time: datetime` 的函数，其内 L2520 `.isoformat()` 抛 `AttributeError`，被 L1592 吞掉 → 止损打标未执行、`del positions` 未执行、持仓残留致每轮重复判定、`consecutive_losses` 每轮 +1、可误触发策略暂停。基线 `fce42bf` 已存在，非 R2 引入。待验证项：修复后新增非 mock 用例并复跑。
- 相关: [Binance_quantitative_trading-20261015-aware-naive-timezone-defect-family](./Binance_quantitative_trading-20261015-aware-naive-timezone-defect-family.md)（同在 new_coin `strategy.py` 的时区/类型族缺陷）、[Binance_quantitative_trading-20261015-newcoin-legacy-test-debt](./Binance_quantitative_trading-20261015-newcoin-legacy-test-debt.md)（`_monitor_positions` 逐币 try/except 吞异常亦为该测试债的伪装来源）
