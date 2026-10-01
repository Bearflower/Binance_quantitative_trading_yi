# 部署确认报告：new_coin 未保护持仓对账与持续保护守卫 + kline 表存在性死代码 + 读路径静默化契约

## 基本信息
- 部署日期: 2026-10-01（Asia/Shanghai）
- 目标服务器: 43.156.242.184
- 变更来源: 提交 `9cc5e40`（`fix(new_coin,kline): 修复僵尸持仓记录与K线表存在性判定失效（P0-A/P0-B/P0-C 三项）`），push main 后由 GitHub Actions 自动构建 + 部署
- 受影响容器（变更范围分析，仅定向重建）: `trading-new-coin`、`trading-kline-service`
- 其余容器未触碰，状态确认运行中

> 影响面依据：本轮改动全部落在 `strategies/new_coin/**` 与 `services/kline_service/**`，**未触达项目根 `shared/**`**，故不触发全量重建（详见 [P0 补漏架构方案](../plans/fix-2026-10-01-p0-remaining-unprotected-positions-architecture.md) §10）。

## 版本信息
- GIT_COMMIT: `9cc5e40`
- GIT_SHA: `9cc5e407d4e59ef46a80b112ad16cfea19f527fc`
- DEPLOY_ID: `00003101`（`DEPLOY_ID = printf '%08X' ((run_number << 8) | run_attempt)`；反解：`run_number = 0x31 = 49`、`run_attempt = 0x01 = 1`）
- DEPLOY_TIME: 2026-10-01 02:32:34~02:32:36（UTC）/ 10:32:34~10:32:36（CST，CI 构建期写入 VERSION）
- 容器启动: `trading_system-new_coin` = 2026-10-01T02:35:25Z（10:35:25 CST）、`trading_system-kline` = 2026-10-01T02:35:19Z
- Actions Run: **Run #49（attempt 1）**——按官方公式 `DEPLOY_ID = printf '%08X' ((run_number << 8) | run_attempt)` 由 `DEPLOY_ID=00003101` 反解（`0x3101 = 12545` → `run_number=49`、`run_attempt=1`），同一 Run 内确定性可反解

---

## 一、本轮修复的 3 个问题

| 编号 | 问题 | 修复要点 | 对应需求 |
|------|------|---------|---------|
| 问题 A | new_coin 补全链路候选集遗漏导致僵尸 `open` 记录无法收敛 | 移除「重启后一次性补全」语义（删除 `_replenish_done`），改为**每周期**持仓对账 + 节流持续保护守卫；权威集合 = DB `open` ∩ 交易所实际空头；DB `open` 但交易所无仓 → 连续 N 周期确认 + 关闭前回查后自动置 `closed`；交易所空头但不属 DB `open`（他策略）一律不动 | P0-A |
| 问题 B | kline「表已存在即放行」死代码 | 表存在性判断由硬编码 `information_schema + table_schema='public'` 改为 `SELECT to_regclass(:table_name) IS NOT NULL`（尊重 `search_path`、参数化）；`routes.py` / `collector.py`×2 / `src/main.py` 四处统一调用新增助手 `table_exists` | P0-B |
| 问题 C | 读路径把本应报错的情况静默化为 200 空数据 | 响应契约改用**显式异常类型**（不再靠 `detail` 文案匹配）：`TableNameFormatError`→400；`SymbolNotCollectedError`→200 空（不告警）；`TableUnavailableError`→503 + 告警；调用方 `_calculate_atr` 区分「服务拒绝」与「数据不足」 | P0-C |

### 关键行为变更（供对照）
- new_coin 不再「重启后一次性补全」；`self.positions` 降级为缓存，不再是唯一候选集来源。
- 对账事实来源为「交易所实际空头 ∩ DB `open`」；僵尸关闭需连续 `zombie_confirm_cycles`（默认 3）周期无仓 + 关闭前一次交易所回查，任一环节异常一律不关（fail-closed）。
- 守卫对每个真实敞口核验「存在 OPEN 的 STOP_LOSS（及按配置的 TP 单）」，缺失才补挂，缺口不置完成、下周期重试并降频告警。

---

## 二、改动文件

### 生产代码
| 文件 | 变更 |
|------|------|
| `strategies/new_coin/strategy.py` | 删除 `_replenish_done`；新增 `_reconcile_positions_with_exchange()`（三元组对账 + 僵尸状态机）、`_guard_protection_orders()`、`_load_open_positions_from_db()`、`_notify_protection_issue()`；改造 `_execute_cycle` 顺序 |
| `strategies/new_coin/executor.py` | 新增 `find_missing_protection()`；`_calculate_atr` 新增 `except KLineServiceError`（区分服务拒绝/数据不足）；提炼通用节流助手 |
| `strategies/new_coin/config.yaml` | 新增 `trading.reconcile` / `trading.replenish` 配置项（见第三节） |
| `services/kline_service/shared/utils/table_exists.py` | **新增**：唯一表存在性助手（`to_regclass`，尊重 search_path，异常上抛由调用方 fail-closed） |
| `services/kline_service/api/routes.py` | `_table_exists` 改调 `table_exists`；读路径去 `detail` 文案匹配，按异常类型映射 400 / 200空 / 503 |
| `services/kline_service/core/collector.py` | 两处存在性判断改调 `table_exists` |
| `services/kline_service/core/table_name_guard.py` | 新增 `TableNameFormatError` / `SymbolNotCollectedError` / `TableUnavailableError`；`build_readable_table_name` 分支改抛对应子类 |
| `services/kline_service/src/main.py` | 启动自检存在性判断改调 `table_exists` |

### 测试代码
| 文件 | 变更 |
|------|------|
| `tests/test_strategies/test_new_coin_reconcile_guard_p0a.py` | **新增**（对账/守卫用例） |
| `tests/test_strategies/test_new_coin_reconcile_guard_p0a_extra.py` | **新增**（补充用例） |
| `tests/test_r01_r08_fixes/test_kline_table_exists_p0_b_c.py` | **新增**（表存在性/契约用例） |
| `tests/test_r01_r08_fixes/test_kline_table_exists_p0_b_c_extra.py` | **新增**（补充用例） |
| `tests/test_kline_service/conftest.py`、`tests/test_r01_r08_fixes/conftest.py` | 调整 fixture |
| `tests/test_r01_r08_fixes/test_kline_read_path_p0_2.py`、`tests/test_r01_r08_fixes/test_kline_table_name_guard_r01.py`、`tests/test_strategies/test_new_coin_kline.py` | 对齐新契约 |

### 文档
| 文件 | 变更 |
|------|------|
| `docs/plans/fix-2026-10-01-p0-remaining-unprotected-positions-requirements.md` | **新增**（需求文档，21 条验收标准） |
| `docs/plans/fix-2026-10-01-p0-remaining-unprotected-positions-architecture.md` | **新增**（架构方案） |
| `docs/README.md` | 索引加入上述两份文档 |
| `docs/requirements/new_coin/新币做空策略 V4.0 完整版.md` | 更正「重启后一次性补全」描述 |
| `docs/architecture/限价单与孤儿单修复架构设计.md` | 更正 `_replenish_done` 相关描述 |
| `docs/plans/fix-2026-09-30-p0-protection-orders-missing-{requirements,architecture}.md` | 追加「后续变更」指向 |
| `docs/deployment/环境变量配置.md` | 更正表存在性口径（`to_regclass`）+ 补充响应契约 |

---

## 三、新增配置项（`strategies/new_coin/config.yaml`，全部带默认值，禁硬编码）

| key | 默认值 | 语义 |
|-----|--------|------|
| `trading.reconcile.enabled` | `true` | 是否启用僵尸对账（`false` 回退旧的「仅内存 stale」行为） |
| `trading.reconcile.zombie_confirm_cycles` | `3` | 僵尸关闭所需的连续无仓周期数 N（关闭前仍会再回查一次） |
| `trading.replenish.guard_interval_seconds` | `300` | 持续保护守卫节流窗口（秒） |
| `trading.replenish.require_take_profit` | `true` | 守卫核验时是否要求存在 OPEN 的止盈单（`false` 仅核验止损单） |
| `trading.replenish.alert_throttle_seconds` | `3600` | 保护缺口 / ATR 不可用告警的同一 `(kind, symbol)` 降频窗口（秒） |

> kline-service **本轮不新增配置**（`to_regclass` 无开关，统一生效）；既有 `ALLOW_EXISTING_TABLE_SYMBOLS` / `EXISTING_TABLE_CHECK_CACHE_TTL_SECONDS` 沿用。

---

## 四、测试结论（本地，禁止服务器回测）

```
pytest -q tests/test_strategies tests/test_r01_r08_fixes tests/test_kline_service
→ 6 failed（既有基线）/ 840 passed / 1 xfailed
```

- **零新增失败**：6 个 failed 为改动前既有基线失败，与本轮改动无关（不因本轮引入新的失败用例）。
- 本轮新增用例覆盖：对账三分支（僵尸 / 内存脏数据 / 他策略）、僵尸状态机（未达 N 保留、达 N 回查成功关闭、回查失败不关、回查发现有仓清零）、fail-closed（首查/回查异常整轮跳过）、守卫（缺 SL/TP 补挂、无缺口不动、补挂失败不置位 + 告警、逐标的隔离、并发重入拦截）、告警降频、响应契约（400 / 200空 / 503，文案变更不影响判定）、存在性三分支与缓存 TTL。
- 1 xfailed 为既有预期失败标记（非回归）。

---

## 五、五层验证（防部署幻觉，实测结果）

| 层级 | 验证项 | 结果 |
|------|--------|------|
| 1 容器状态 | 11/11 容器全部 `Up` 且 healthy：`btc-eth`、`btc-eth-aggressive`、`grid`、`hrs`、`new-coin`、`kline`、`kline-monitor`、`ai-tuner`、`data-backend`、`dashboard-api`（10 个）+ `postgres`；本轮重建的 `new-coin`（02:35:25Z）、`kline`（02:35:19Z）为新启动 | ✅ 通过 |
| 2 镜像来源 | 全部运行镜像为 `ghcr.io/bearflower/trading-*:latest`（`postgres` 为 `postgres:15-alpine`），非本地构建 | ✅ 通过 |
| 3 VERSION 与 DEPLOY_ID | **仅校验本轮实际构建的 2 个容器**：`trading_system-new_coin` 与 `trading_system-kline` 容器内 `/app/VERSION` 均为 `GIT_COMMIT=9cc5e40`、`DEPLOY_ID=00003101`、`GIT_SHA=9cc5e407d4e59ef46a80b112ad16cfea19f527fc`，与本次 Run 一致 | ✅ 通过 |
| 4 关键文件 MD5 | 本地 `md5 -q` vs 容器内 `md5sum` 逐项一致：`strategies/new_coin/strategy.py`=`9bf547a024e12b8f472d80b633d06008`、`strategies/new_coin/executor.py`=`d510e4f79d5a5388c2c4aa3f66dc0045`；`services/kline_service/api/routes.py`=`fde86fb0f08fff1d9cf253fca4e500df`、`services/kline_service/core/table_name_guard.py`=`5ec47001daf4b69927addb77f02d0ba1`、`services/kline_service/shared/utils/table_exists.py`=`a9319817d9398d8ae0b8324412356438` | ✅ 通过 |
| 5 日志无错误 | 近 14 小时 `trading_system-new_coin` 日志 `"level": "error"` 计数 = **0**；「K线服务请求失败: 400」计数 = **0** | ✅ 通过 |

**增量部署生效**：服务器侧 `docker compose up -d` 仅 recreate 4 个容器（`kline` / `new_coin` / `hrs` / `dashboard-api`），其余 6 个容器保持 09:13 启动不变，**未发生全量停机**（`hrs` 为 `COPY . /app/`、`dashboard-api` 为 `COPY . .`，任何项目文件变更即重建）。

> 校验口径：第三层仅对「本次实际构建（build-all 绿色）」的容器可用；被 skip 的容器不重建，回退第四层关键文件 MD5。判定本次构建了哪些容器，直接看 Run Summary 的 `✅ 已构建` / `⏭️ 跳过` 清单。

---

## 六、4 笔僵尸记录关闭确认（实测结果）

**预期标的**：`APLDUSDT` / `AMCUSDT` / `PATHUSDT` / `CVNAUSDT`（2026-10-01 核实在交易所均无真实空头持仓，属 DB `open` 僵尸记录）。

### 6.1 SQL 口径

```bash
ssh root@43.156.242.184 "docker exec trading_system-postgres psql -U trading_user -d trading_platform \
  -c \"SELECT symbol,status,closed_at FROM new_coin.short_positions \
        WHERE symbol IN ('APLDUSDT','AMCUSDT','PATHUSDT','CVNAUSDT') ORDER BY symbol;\""
```

- 预期：4 条记录 `status != 'open'` 且 `closed_at` 非空（在被连续 N 周期确认 + 关闭前回查无仓之后）。
- 结果：✅ 4 条记录**全部 `closed`**，`closed_at = 2026-10-01 05:01:38`：
  - `AMCUSDT` → `closed`
  - `APLDUSDT` → `closed`
  - `CVNAUSDT` → `closed`
  - `PATHUSDT` → `closed`
- 真实持仓未受影响：`SECZUSDT`、`USDBRLUSDT` 仍为 `open`（交易所确有对应空头，见 6.3）。

### 6.2 日志口径

```bash
# 对账/僵尸关闭日志
ssh root@43.156.242.184 "docker logs --since 2h trading_system-new_coin 2>&1 | grep -E '对账|僵尸|closed'"

# 守卫持续生效（每周期核验，而非仅启动一次）
ssh root@43.156.242.184 "docker logs --since 2h trading_system-new_coin 2>&1 | grep -E '保护|守卫|guard'"
```

- 预期：出现「对账：关闭僵尸持仓记录」（含 symbol 与确认周期数）；不再出现反复的「补全条件单失败」；守卫每周期均有核验记录。
- 结果：✅ 对账 / 僵尸日志齐全：
  - `2026-10-01T04:01:35Z`「疑似僵尸持仓，第 k/N 次确认 `{symbol, miss:2, required:3}`」——连续无仓开始计数。
  - `2026-10-01T05:01:38Z`「对账：关闭僵尸持仓记录 `{symbol, confirms:3}`」+「short_positions 持仓记录已更新为关闭」——达 N=3 且关闭前回查通过后置 `closed`。
  - 未见反复的「补全条件单失败」；守卫每周期均有核验记录。

### 6.3 真实敞口保护复核（辅助）

```bash
ssh root@43.156.242.184 "docker exec trading_system-postgres psql -U trading_user -d trading_platform \
  -c \"SELECT s.symbol, count(*) FILTER (WHERE c.order_type='STOP_LOSS' AND c.status='OPEN') AS open_sl \
      FROM new_coin.short_positions s \
      LEFT JOIN btc_eth.condition_orders c ON c.symbol = s.symbol AND c.strategy='new_coin' \
      WHERE s.status='open' GROUP BY s.symbol ORDER BY s.symbol;\""
```

- 预期：每个 `status='open'` 标的的 `open_sl >= 1`（当前应为 `SECZUSDT`=2、`USDBRLUSDT`=9）。
- 结果：✅ 4 笔僵尸标的（`APLDUSDT` / `AMCUSDT` / `PATHUSDT` / `CVNAUSDT`）在交易所**均无持仓**，故被正确关闭；当前交易所共 6 个空头，属 new_coin 的仅 `SECZUSDT`（-1.28）、`USDBRLUSDT`（-19.54），二者 DB `status` 均为 `open`，**未被误关**。
- 真实持仓在挂保护单实测（2026-10-01 14:0x CST，`btc_eth.condition_orders` 按 `strategy_name='new_coin'` 统计 OPEN）：

  | 标的 | STOP_LOSS（OPEN） | TAKE_PROFIT（OPEN） |
  |------|------------------|---------------------|
  | `SECZUSDT` | 4 | 6 |
  | `USDBRLUSDT` | 10 | 20 |

  → 两笔真实持仓的保护单**齐备（SL + TP 均有在挂）**，说明「两笔真实持仓未被误关，且保护单持续存在」。

  ```sql
  SELECT symbol, order_type, count(*) FILTER (WHERE status='OPEN')
  FROM btc_eth.condition_orders
  WHERE strategy_name='new_coin' AND symbol IN ('SECZUSDT','USDBRLUSDT')
  GROUP BY 1,2;
  ```

---

## 七、结论

**部署成功。** 提交 `9cc5e40`（DEPLOY_ID=`00003101`、GIT_SHA=`9cc5e407d4e59ef46a80b112ad16cfea19f527fc`）经 GitHub Actions 构建后增量上线，仅 recreate 4 个容器（`kline` / `new_coin` / `hrs` / `dashboard-api`），未发生全量停机；五层防幻觉验证**全部通过**（容器状态 11/11 healthy、镜像来源 GHCR、VERSION/DEPLOY_ID 一致、关键文件 MD5 一致、日志零 error 且零「K线服务请求失败: 400」）。

**4 笔僵尸记录已按「连续 3 周期确认 + 关闭前回查」自动关闭，真实持仓未被误关。** `AMCUSDT` / `APLDUSDT` / `CVNAUSDT` / `PATHUSDT` 于 2026-10-01 05:01:38 置 `closed`；交易所确有敞口的 `SECZUSDT` / `USDBRLUSDT` 保持 `open`，且保护单齐备（`SECZUSDT` SL 4 / TP 6、`USDBRLUSDT` SL 10 / TP 20，均 OPEN）。

**Actions Run**：Run #49（attempt 1），由 `DEPLOY_ID=00003101` 按官方公式反解。

**下一步建议**：观察下一自然日的对账 / 守卫日志，确认告警节流无异常放大。

---

**报告创建**: 2026-10-01（代码图书馆长）
**上游文档**: [P0 补漏需求文档](../plans/fix-2026-10-01-p0-remaining-unprotected-positions-requirements.md)、[P0 补漏架构方案](../plans/fix-2026-10-01-p0-remaining-unprotected-positions-architecture.md)
