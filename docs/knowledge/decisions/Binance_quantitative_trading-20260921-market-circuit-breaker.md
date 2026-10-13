---
id: Binance_quantitative_trading-20260921-market-circuit-breaker
类型: decision
项目: Binance_quantitative_trading
标题: 组合级 1h 等权双向熔断（二期增 12h 累计维度，定位尾部保护非收益优化）
日期: 2026-09-21
标签: [风控, 熔断, 组合级]
任务标识: market_circuit_breaker_2026-09-21.md
状态: 已采纳
审查: 已审查
---

# 组合级 1h 等权双向熔断（二期增 12h 累计维度，定位尾部保护非收益优化）

## 背景
用户观察到「主流币全涨但 MTPCS 原版/激进版/HRS 全在做空，很危险」，逐步收敛出「组合级单边行情空头熔断」方案（用户 5 点决策后要求写需求文档）。一期定 1h 瞬时双向熔断；二期（2026-09-22）在同表新增 12h 累计双向维度（慢牛拦空 + 阴跌拦多）。

## 备选方案
原始 handoff 未记录备选方案对比（仅记最终决策），故此处不补造。已确认的取舍细节如下：
- **加权方式**：市值加权 vs 平权 → 回测结论「市值加权更钝」，**选平权**；分池平权（MTPCS 池 5 币 / HRS 池 62 币各自独立指数）；
- **指数计算器宿主**：**data-backend 容器**（作为第 5 个 job 注册），而非新建独立服务；
- **配置位置**：独立共享配置文件 `shared/circuit_breaker_config.yaml`（阈值/cron/fixed_pool 共用），而非三处各自维护；
- **共享存储**：项目级 PGSQL（项目无独立 Redis）；
- **表设计**：表 `public.market_circuit_breaker_index` **不落布尔熔断状态**，由策略实时判定；
- **12h 维度**：与 1h **同表同整点、不同列**（`equal_weight_12h`），不新建表；`include_12h: false` 时回退一期纯 1h 行为。

## 决策
一期：为系统增加**组合级、1h 尺度、等权涨跌幅驱动的单边暴动熔断（双向对称）**——
- **空头熔断**：持仓池等权 1h 涨幅 > 2% → 暂停 MTPCS 原版/激进版/HRS 新增空单；
- **多头熔断**：等权 1h 跌幅 > 2% → 暂停同策略新增多单；
- 均为**否决性拦截、不砍已有持仓**；指数计算器每 h 03 分算好**分池双指数**（mtpcs/hrs，写 PGSQL），三策略开仓前按方向读取并拦截；叠加单币级双轨检查（自身涨/跌 3%，冷却 4h）。
二期：新增 **12h 累计双向维度**（12h/+2% 拦空「慢牛防护」+ 12h/-2% 拦多「阴跌防护」），与 1h 判定并存。

## 理由
目标即理由：主流币单边暴动时同向空头组合存在系统性风险；回测核心结论明确熔断是**尾部保护闸门、而非收益优化器**（历史拦截量 0-2 笔、盈亏≈0）。（AI 归纳，仅供参考）

## 后果
- 眼下收益：为 MTPCS 原版/激进版/HRS 增加组合级单边熔断尾部保护，双向成本对称（指数带符号、cap 双边）；
- 技术债 / 待偿：
  - 多单方向阈值（2%/1%/3%）**继承空单回测结论、未独立回测**（列为风险 6，上线后观测）；
  - 看板是否展示 12h 指数/熔断状态**未决策**（可能本期不做，仅落库）；
  - 二期 handoff 记录 **git 提交推送未执行**（部署走 `one_click_deploy.sh` 手动路径，非 Actions push 路径）。

## 适用范围与修订
一期：`shared/circuit_breaker.py`、`shared/circuit_breaker_config.yaml`、`dashboard/backend/services/market_circuit_breaker_job.py`、`database/postgres/init-scripts/07-market-circuit-breaker.sql`、`strategies/{btc_eth,btc_eth_aggressive,hrs}/strategy.py`。
二期：同表新增 12h 三列（`equal_weight_12h/symbol_count_12h/symbols_12h`）、`compute_cumulative_return(klines_1h, hours=12)`、config 增 `include_12h/trigger_short_12h/trigger_long_12h`。
**部署状态**：一期编码于 2026-09-22 闭环（一期 handoff 记录为「仅剩部署」，未记录一期独立 commit SHA）；一期与二期（及方案 C 持仓归属互斥）以同一 commit `500b4a5` **一并部署上线**（`DEPLOY_ID=E7456CCE`，2026-09-22 16:5x，五层验证通过、7 容器 healthy，prod 已补 12h 三列）。无替代条目。

## 关联与证据
- 本地补充来源: [一期 handoff](../../handoffs/market_circuit_breaker_2026-09-21.md)、[二期 handoff](../../handoffs/market_circuit_breaker_2026-09-22.md)（不随 Git 同步）
- 持久证据: 需求文档 `docs/requirements/market_circuit_breaker/单边行情空头熔断需求文档.md`（v1.2：4.6 真值表 / 4.7 12h 维度 / 6.2 表结构 / 11.1 回测结论）；建表脚本 `database/postgres/init-scripts/07-market-circuit-breaker.sql`；commit 500b4a5
- 必要证据摘要: 回测历史拦截量 0-2 笔（盈亏≈0）、平权优于市值加权；二期 `tests/test_shared/test_circuit_breaker.py` 54 passed；部署 `DEPLOY_ID=E7456CCE` 五层验证通过、7 容器 healthy、关键文件 MD5（localhost=容器）一致（`shared/circuit_breaker.py=cd26a649` 等）；`shared/circuit_breaker_config.yaml` 为唯一事实源
- 相关: 无
