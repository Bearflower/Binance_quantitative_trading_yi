---
id: Binance_quantitative_trading-20260921-pm-positionrisk-no-isolation
类型: issue
项目: Binance_quantitative_trading
标题: PM 账户 positionRisk 无策略隔离导致占用虚增与越权管理他策略仓位
日期: 2026-09-21
标签: [bug, 资金限额, PM账户]
任务标识: capital_limit_enforcement_2026-09-21.md
状态: 已解决
审查: 已审查
---

# PM 账户 positionRisk 无策略隔离导致占用虚增与越权管理他策略仓位

## 现象
ai-tuner 月度资金分配把 new_coin 限额定为 156.33 USDT，但该策略实际占用保证金已达 249.30 USDT，超限 59%。部署完成后实测，`_rebuild_position_baseline` 重建出 **8 个币种、total_margin=348.8515**，而其中仅 `APLDUSDT / AMCUSDT / PATHUSDT` 属 new_coin：
```
{"position_count": 8, "total_margin": 348.8515,
 "symbols": ["SAMSUNGUSDT","BNBUSDT","AMCUSDT","XAUUSDT","MARSCOINUSDT","PATHUSDT","APLDUSDT","XRPUSDT"]}
```

## 根因
PM 投资组合保证金账户 `positionRisk` 返回**账户内全部空头**（含其他策略持仓），接口无策略隔离能力。由此产生双重危害：
1. `calc_current_occupied_margin()` 默认取 `position_tracking.keys()` → 占用被虚增为 348.85 > 限额 156.33 → 策略**永久停止开仓**；
2. `_sync_baseline_to_tracking` 为 5 个外来币种创建 tracking 条目 → `check_position_management` 的守卫 `if symbol not in self.position_tracking: return` 失效 → **越权管理他策略仓位**，可能调用 `cancel_all_algo_orders` 取消他策略的条件单。

## 解决
| 文件 | 改动 |
|---|---|
| `strategies/new_coin/executor.py` | 新增 `get_open_short_symbols()`：以 DB `new_coin.short_positions`（`status='open'`）作为「本策略自有币种」权威来源；异常返回 `None` 供降级 |
| `strategies/new_coin/strategy.py` | `_rebuild_position_baseline` 中插入 `_filter_own_positions()`，过滤后再 `_merge_rebuilt_positions`，`total_margin` 按过滤后求和；DB 不可用时降级为本地记录，本地亦空则跳过过滤并告警 |
| `tests/test_strategies/test_capital_allocation_enforcement.py` | 新增 `TestOwnPositionFilter` 4 用例（外来币种剔除 / DB 不可用降级本地 / 本地亦空跳过 / DB 空集合清空） |

止血动作：10:52 CST `docker stop trading_system-new_coin` 阻断 03:00 UTC 周期；核验旧容器 02:42 启动、02:52 停止，日志停在「等待 1071 秒到 03:00」，**该周期未执行，未发生越权行为**。

## 验证
- `pytest tests/test_strategies/` = **551 passed, 1 xfailed**（基线 547 → 本轮 +4）
- 部署 DEPLOY_ID=**59C00584**，五层验证全通过（L1 healthy / L2 container==latest==sha256:ddc5e03d...940fd6 / L3 容器内 DEPLOY_ID 一致 / L4 5 文件 MD5 全对 / L5 错误 0）
- 修复生效实测：`{"position_count":3,"total_margin":154.217,"symbols":["APLDUSDT","AMCUSDT","PATHUSDT"]}` → 占用 **154.217 ≤ 156.33** ✅，`position_tracking` 仅 3 个自有币种
- DB 权威来源核验：`new_coin.short_positions` open 记录恰为 APLDUSDT / AMCUSDT / PATHUSDT 三行

## 适用范围与修订
`strategies/new_coin/{executor.py,strategy.py}`；commit `9a3036c` 已上线（已 push origin/main）。共性风险面：**所有**读取 PM 账户 `positionRisk` 的策略（btc_eth / btc_eth_aggressive / hrs）均可能命中同型缺陷。无替代条目。

已知未修（用户拍板「本次不动，后续再修」）：btc_eth 与 btc_eth_aggressive 共用同一份月度额 208.44，但各自只统计自身占用 → 两版合计实际可达 2×208.44 超限。

## 教训
「接口返回全量 ≠ 本策略数据」——共享账户下，任何按账户维度查询的接口（positionRisk / 账户余额 / 订单列表）都必须显式做**归属过滤**，且过滤依据要取自本策略的权威来源（本例为 DB 自有记录），不能依赖内存态（重启即丢）。归属守卫要 fail-closed + 降级告警，不能默认全收。（AI 归纳，仅供参考）

## 关联
- 本地补充来源: [handoff](../../handoffs/capital_limit_enforcement_2026-09-21.md)（不随 Git 同步）
- 持久证据: 需求文档 `docs/requirements/capital_allocation/策略资金限额强制执行需求文档.md`（R1~R7 + AC-1~AC-8）；commit 9a3036c
- 必要证据摘要: 越限 59%（249.30 / 156.33）→ 修复后占用 154.217；551 passed, 1 xfailed（基线 547）；DEPLOY_ID 59C00584 五层验证通过
- 相关: [Binance_quantitative_trading-20260922-shared-account-position-ownership.md](./Binance_quantitative_trading-20260922-shared-account-position-ownership.md)（同属共享 PM 账户持仓归属隔离类缺陷）
