---
id: Binance_quantitative_trading-20260922-shared-account-position-ownership
类型: issue
项目: Binance_quantitative_trading
标题: 共享 PM 账户下补单按币种不按归属导致双策略互挂保护单
日期: 2026-09-22
标签: [bug, 持仓归属, PM账户]
任务标识: mtpcs_double-strategy-position-ownership_2026-09-22.md
状态: 待复查
审查: 已审查
---

# 共享 PM 账户下补单按币种不按归属导致双策略互挂保护单

## 现象
原版 btc_eth 于 2026-09-22 09:01 对 SOL 下 3 条 BUY 条件单（保护单），但原版并无 SOL 持仓。该 SOL 空仓由激进版 btc_eth_aggressive 于同日 08:10 开立（来源记 qty=2.35）。来源记载的交易所当前持仓：SOLUSDT 空 1.2 @118.57，归属激进版（2026-09-22 08:10 LIMIT SELL）。即原版为「别人家的仓位」凭空补挂了保护单。

## 根因
`strategies/btc_eth/strategy.py` 的 `_ensure_position_protection()`（L5447）/ `_ensure_symbol_protection()`（L5502）补单时，数据源 `binance.get_position()`（`shared/binance_api.py` L830，`/papi/v1/um/positionRisk`）返回**共享账户全部持仓**（含其他策略仓位），而过滤仅按 `symbol not in managed_symbols`（自家白名单）+ 数量 ≥ `min_position_amt`，**不区分持仓归属**。于是对白名单内任意币种，`_get_or_create_position_state()`（L5543）会凭空造出持仓态并补挂 STOP_LOSS / TAKE_PROFIT。当原版与激进版持有同一币种时（重叠币 BTC/ETH/BNB/SOL/XRP），一方即会为另一方的仓位补单。
（注：`btc_eth.condition_orders` 为真实条件单表；查询 OPEN 单用 `get_open_orders(db, strategy_name)` 已按 strategy_name 隔离，**非本次缺陷点**。）

## 解决
**根治（方案C）尚未完成**，仅完成临时止血。

止血（2026-09-22）：撤销原版名义的 3 条 SOL 保护单，DB 置 `CANCELED`——algo 2000001454562391（STOP_LOSS）、2000001454562403（TAKE_PROFIT）、2000001454562410（TAKE_PROFIT），均 `cancel_algo_order`（DELETE `/papi/v1/um/algo/order`）返回 `complete=True`。撤后 SOL 的 OPEN 保护单仅剩激进版 3 条（STOP_LOSS 2000001454458419、TAKE_PROFIT 2000001454458421 / 2000001454458426）。

根治设计（用户已拍板语义，09-22 来源记录为「待开发」）：
1. 归属判定挂在**开仓订单**上（而非「币种→策略」）——识别开仓单：`trade_records` 中 `order_type IN ('LIMIT','MARKET')` 且 `realized_pnl IS NULL`，策略名取该开仓单的 strategy；
2. 边界语义 = A + B：A 按最新开仓单判定归属（老仓保护单在净仓被覆盖时让渡给新家）；B 持仓期互斥，任何策略发现同名币已有他人未平开仓单即跳过开仓；
3. 修改范围：原版 + 激进版 `btc_eth/strategy.py` 的 `_ensure_position_protection` / `_ensure_symbol_protection` 加归属前置校验，开仓逻辑加持仓期互斥；再评估 HRS / new_coin；
4. 部署：先灰度 btc_eth + 激进版（重叠币 BTC/ETH/BNB/SOL/XRP），回归要求现有单测 179 passed 不得回归。

进展补充（本地后续 handoff 记录）：09-23 的补全 handoff 记「三处归属守卫（`_ensure_symbol_protection` / `_open_new_position` / `_do_startup_orphan_cleanup`）与 main.py 两处上报过滤已代码落地、单测回归通过，但**尚未部署**（涉及 `shared/`，待重建 btc-eth + btc-eth-aggressive 两容器并按五层验证）」。

## 验证
- 止血验证（已实测）：3 条撤单 `cancel_algo_order` 全部返回 `complete=True`；DB 对应记录置 `CANCELED`；核验 SOL 现有 OPEN 保护单仅激进版 3 条，其他重叠币 BTC/ETH/BNB/XRP 当前无持仓、无叠加单。
- **根治无验证证据**：09-22 来源记方案C尚未开始编码；09-23 后续 handoff 记代码已落地且单测通过，但**未部署上线**，无部署 / 运行实测证据。故本条目状态为 `待复查`。

## 适用范围与修订
`strategies/btc_eth/strategy.py`（原版）与 `strategies/btc_eth_aggressive/strategy.py`（激进版），涉及共享层 `shared/binance_api.py`（get_position L830）、`shared/position_ownership.py`，以及表 `btc_eth.condition_orders` / `trading.trade_records`。当前修复未上线，**复发风险存在**：原版容器下次重启时 `_ensure_position_protection` 会再对 SOL 补挂（激进版仓仍在、归属缺陷未除）。无替代条目。

## 教训
「接口返回全量 ≠ 本策略数据」——共享账户下，任何按账户维度查询的接口（positionRisk / 账户余额 / 订单列表）都必须显式做**归属过滤**，且过滤依据要取自本策略的权威来源（本例为开仓订单归属），不能依赖内存态（重启即丢）。归属守卫要 fail-closed + 降级告警，不能默认全收。此外止血只是临时手段，只要根治未上线，任何容器重启都可能让旧缺陷复发。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/mtpcs_double-strategy-position-ownership_2026-09-22.md)（不随 Git 同步）；后续进展另见 [handoff](../../handoffs/mtpcs_position_ownership_complete_2026-09-23.md)（不随 Git 同步）
- 持久证据: 需求文档 `docs/requirements/mtpcs/MTPCS双策略持仓归属互斥（方案C）需求文档.md`（#4.4）、`docs/plans/项目需求迭代文档.md`（#12.6）
- 必要证据摘要: 止血撤单 3 条均返回 complete=True；SOL 现存 OPEN 保护单仅激进版 3 条；根治未上线，无对应 commit
- 相关: [Binance_quantitative_trading-20260921-pm-positionrisk-no-isolation.md](./Binance_quantitative_trading-20260921-pm-positionrisk-no-isolation.md)（同属共享 PM 账户持仓归属隔离类缺陷）
