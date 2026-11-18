# HRS 优化发布与验收记录（2026-11-18）

本轮得到用户授权实施、离线验证和通过后的生产部署。生产交易 API 的人工验收只使用只读查询，不为凑样本开仓、平仓或改保护单。

## 首次发布

- 候选源码 `9204d908bce3d2325c31edf184757af14ae9422a`，独立 PostgreSQL 16 与候选 Python 3.11 镜像云端定向验证 **1133 passed**。运行记录：https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/44223966232 。
- 2026-11-18 02:51 UTC 初始化验收失败后，受控脚本恢复原 HRS 和市值镜像，独立报告容器移除，无关容器身份核验未变。加法数据库迁移保留。
- 运行态投影报 `InsufficientPrivilegeError`。只读排查发现实际非管理员 `trading_user` 无 `trading.hrs_equity_segment` 行锁所需 UPDATE 权限；现有 SELECT/INSERT 不足以执行 SELECT FOR UPDATE。不是资金费来源表或 sequence 权限不足。
- 修复验证中：仅授予一个列的 UPDATE 权限以满足行锁，同时用不可变触发器拒绝 UPDATE/DELETE/TRUNCATE；新增使用真实非管理员角色的完整刷新和重启验证。失败诊断只保存固定代码和白名单状态，不保存环境、原始错误文本或凭据。
- 回滚后只读仓位与保护复核：当前 WLDUSDT 数量 28，两套独立库存一致，STOP_MARKET closePosition 存在。该仓位是自然实盘事件，不能沿用之前 DOGE 快照当作当前状态。

## 发布后续修复

- `8061f173`：持久化每次初始化健康观测和回滚前失败代码；固定白名单脱敏，22 项测试通过。
- `2eb9ff78`：显式撤销主交易角色对原始决策证据的 INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER，只保留 SELECT；独立 writer 可追加，不能改旧记录，27 项采集测试通过，含真实默认 ACL 继承的复现与角色验证。
- `2193272a`：HRS 只减仓退出不再被回放误报为低于最小名义额；入场与数量步长约束保留，374 项回放/实验定向测试通过。依据 [Binance PM 错误码 -4164](https://developers.binance.com/en/docs/products/derivatives-trading-portfolio-margin/error-code)，reduce-only 有该最小名义额豁免。
- `8dd76700` / `56111762`：后台权益刷新和候选定时器的输入不再形成假扫描；真实缺起点/终点/上下文失败、篡改哈希和非因果时间仍阻止验收通过。报告原集 47 项通过，另外两项完整性用例通过；该修复已独立复审。

## 经济与数据验收边界

- 1% 单笔风险预算按最大亏损计算；本轮不改变 SL/TP 的经济参数。已实现冻结每仓退出计划、共用实盘/离线计算和预登记实验 CLI，缺合格窗口时不能选新参数。
- 8–9 月原始决策、OI/市值时点、实际资金流等快照缺失已完成审计封存，参见 `hrs-aug-sep-final-input-audit-2026-11-18.md`。历史旧亏损统计不是新规则的反事实结果。
- 日亏 3%、精确滚动 30×24h 回撤 6% 必须依赖真实原点与持久权益证据。盘中首次启用可能因 `daily_origin_missing` 拒新风险；不能伪造当天午夜或把初始段称为完整 30 天验收。
- 24 个连续完整小时与前 5 个自然闭合仓位是未来生产效果验收。报告容器运行健康不等于技术覆盖、交易绩效或 SL/TP 已验收通过。

## 当前结论

首次候选未发布成功；原版本运行。后续权限修复、再次候选验证与实际发布结果将追加在本记录。
