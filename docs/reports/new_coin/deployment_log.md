# 部署确认报告

---
## 2026-09-27 追加部署（修复重启恢复未回填 ATR 导致误平仓 + CI 构建期写入 VERSION）

### 变更内容
- **修复「重启恢复路径未回填 ATR → `atr=0` → ATR 相关止盈/止损阈值退化为 0 → 重启后立即误平仓」（资金风险）**
  - `strategies/new_coin/executor.py`：`_check_trailing_stop`（移动止盈，阈值 `atr×倍数=0` 后 `price_bounce>=0` 恒真 → 重启后首次检查即误平）与 `_check_dynamic_trailing`（动态利润保护，硬止损价退化为 `entry_price` → 价格一回入场价即误平）各加 `atr<=0` 守卫，跳过本轮比较；两处同构守卫提取为 `_warn_invalid_atr(symbol, context, atr) -> bool`
  - `strategies/new_coin/strategy.py`：`_rebuild_position_baseline` 在同步基线前用 `asyncio.gather` 批量算 ATR（新增 `_calc_rebuilt_atr_map`）；`_sync_baseline_to_tracking` 增加 `atr_map` 入参 + 新增静态方法 `_backfill_tracking_atr`（仅「新值 > 0 且条目 `atr <= 0`」时回填，**绝不覆盖**条件单恢复路径已写入的有效 atr；仍无有效 ATR 时汇总一次 WARN；不写数据库）
- **部署防幻觉增强：第三层 VERSION 校验由「失效」转为「可用」**
  - `.github/workflows/deploy.yml`：build 侧新增「📊 记录构建决策到 Summary」「🏷️ 生成 VERSION」两步——构建前把 `DEPLOY_TIME`(UTC)/`GIT_COMMIT`/`GIT_SHA`/`DEPLOY_ID` 写入仓库根 `VERSION`（进入 `context: .` 的 build context）；deploy 侧用同一公式注入 `__DEPLOY_ID__`，部署日志行追加 `deploy_id=...`
  - `DEPLOY_ID = printf '%08X' ((run_number << 8) | run_attempt)`，确定性且可反解（`run_number = id >> 8`、`run_attempt = id & 0xFF`）
  - `services/kline_service/Dockerfile`、`services/kline_monitor/Dockerfile`：各加 `COPY VERSION /app/VERSION`（此前无 VERSION 导致第三层失效）；本次后 **10 个镜像全部内置 `/app/VERSION`**
  - 口径补充：第三层**只校验本次实际构建的容器**；被 skip 的容器不重建（VERSION 仍为上次构建值/老镜像可能无此文件），回退第四层关键文件 MD5
- **测试**：`tests/test_strategies/test_new_coin_tracking_entry.py`（新增 ATR 守卫回归测试 + 工厂一致性测试）、`tests/test_strategies/test_capital_allocation_enforcement.py`（mock 补绑）

### 部署事实
- 提交：`ba0bdf2`（代码修复）+ `ef4e2de`（CI + Dockerfile），一起 push → **Actions Run #25 全绿**
- 本次 Run 期望值：`run_number=25`、`run_attempt=1` → **`DEPLOY_ID = 00001901`**、`GIT_SHA = ef4e2de9693f2abd42bdaf4d5ac6ccd422dd4558`
- 实际构建：仅 `new-coin` / `kline-service` / `kline-monitor`（另 7 个 job 的「生成 VERSION」步骤为 skipped，判定正确）

### 验证结果（五层验证）
| 层级 | 内容 | 结果 |
|------|------|------|
| 1 容器状态 | 11 个 trading 容器全部 `Up (healthy)`（含本次未重建的 7 个） | ✅ |
| 2 镜像 | 全部为 `ghcr.io/bearflower/trading-*:latest` | ✅ |
| 3 VERSION | 本次构建的 3 个容器内 `/app/VERSION` 均为 `DEPLOY_ID=00001901` / `GIT_SHA=ef4e2de...`，与本次 Run 一致；skip 的 7 个容器按新口径不校验（回退第四层） | ✅ |
| 4 文件MD5 | `new-coin` 容器内 `executor.py` / `strategy.py` 与本地逐字节一致 | ✅ |
| 5 日志错误 | `new-coin` 重启后 `error` 计数 = 0；启动流程正常、对齐整点周期 | ✅ |

### 功能验证（本次修复的关键运行证据）
重启后 `new-coin` 日志依次出现：

```
info     基线重建完成            {'position_count': 4, 'symbols': ['XRPUSDT','牛来USDT','SOLUSDT','USDBRLUSDT']}
warning  基线重建：跳过非本策略持仓  {'own_symbols': ['AMCUSDT','APLDUSDT','PATHUSDT','USDBRLUSDT']}
info     持仓基线已同步到 position_tracking  {'symbols': ['USDBRLUSDT']}
info     持仓基线重建完成          {'position_count': 1, 'total_margin': 50.6613}
```

- **未出现**「重启基线重建：部分币种 ATR 缺失」告警 → 说明恢复条目的 `atr` 回填成功且 > 0（该告警的触发条件正是「回填后 `atr <= 0`」）
- **未出现**「移动止盈缺少有效 ATR，跳过本轮检查」/「触发移动止盈」/「触发动态利润保护止损」→ 修复前 `atr=0` 会在此刻立即误平尾仓，本次未发生
- 部署日志：`[2026-09-27 21:33:20] DEPLOY_SUCCESS commit=ef4e2de9693f2... deploy_id=00001901`，与容器内 VERSION 一一对应 ✅

### MD5（本地 = 容器内，`new-coin`）
| 文件 | MD5 |
|------|------|
| strategies/new_coin/executor.py | 71ab506293a9d1b2cb2e1ee8ff39386a |
| strategies/new_coin/strategy.py | a6b43acde5e31be6befcdba7f8ee6fc2 |

> 回滚比对基线（上一次部署）：`executor.py=d04d173311f443565eaa3965e0e0ad11`、`strategy.py=eedd5b02e977e881618a82d2f2446e5d`。

### 测试
- ✅ 本地 `tests/test_strategies`：**572 passed / 1 xfailed / 0 failed**
- ✅ 新增 ATR 守卫回归用例 3 项（`atr=0` 不平仓且仍维护最低价 / `atr=0` 创新低只更新最低价 / `atr` 键缺失视为 0 跳过），并含「无防护时阈值 0 会误判触发」的证据断言
- ✅ 3 个既有读侧用例（剩余量缺失回退 / 全部缺失 / 显式 0）改为显式给出有效 `atr`，**原断言保留**，未丢失「不漏平」覆盖
- ✅ 幻觉测试 10 项逐项实读源文件核实（`asyncio`/`Decimal`/`Optional`/`List` 导入；`_calculate_atr` 为 async 且全部失败路径返回 `Decimal('0')`；`ensure_tracking_entry` 关键字参数；`_sync_baseline_to_tracking` 唯一生产调用点已传 `atr_map`；`_build_tracking_entry` 含 `atr` 键；新增行最长 103 字符）
- ✅ 规范检测（code-specification-inspector）首轮发现 2 处已整改：两处同构 ATR 守卫提取为 `_warn_invalid_atr`；`deploy.yml` 部署日志行 133 字符拆为 `LOG_FILE`/`LOG_MSG`（现最长新增行 103 字符）
- ✅ `deploy.yml` YAML 解析通过；「生成 VERSION」步骤与部署远程脚本**经 YAML 解析后的 shell** `bash -n` rc=0
- ✅ 部署方式确认：仅重建 3 个镜像，其余 7 个正确 skip（`.github/` 变更非独占未触发全量，`VERSION` 未随提交变更）

### 本次暴露的既有问题（已于后续提交修复）✅
- **问题（本次部署时暴露）**：`deploy.yml` 部署脚本第 2 步对 compose 全部 `container_name` 做**无条件 `docker rm -f`**（原意是清理「独立 docker run 残留容器」以规避命名冲突），导致**每次部署都会强制移除并重建全部容器**（本次 7 个未变更镜像的容器也在 21:32:53 被一并重启）。这使「只 recreate 镜像变了的容器」的增量设计失效，且每次部署都给所有策略带来短时停机。
- **修复（代码已就绪，随本次推送生效）**：第 2 步改为按「残留容器判定口径」清理——先取本项目管辖容器 ID 集合 `PROJECT_IDS=$(docker compose ps -a -q)`，再对候选名取 `docker inspect -f '{{.Id}}' <name>`：
  - **命中本项目 → 保留**，交给后续 `docker compose up -d` 按镜像变化**增量 recreate**；
  - **未命中 → 判定为独立 `docker run` 残留 → 才 `docker rm -f`**；
  - 新增空值守卫：`PROJECT_IDS` 为空（compose 异常）时**不删任何容器**，只打警告，宁可由 `compose up` 自行报命名冲突。
- **验证结论（本地/干跑）**：YAML 解析通过、`bash -n` rc=0；生产服务器干跑判定 **11/11 全部 KEEP（零误删）**；受控 stub 三场景功能测试通过——A 正常部署 11 保留/0 移除、B 仅移除真正的独立残留、C 守卫分支 0 删除。（本次代码尚未 push，实际部署结果以上线后 Actions Run 为准。）

---
## 2026-09-27 追加部署（修复重启后 position_tracking 条目残缺：remaining_quantity KeyError / 静默漏平）

### 变更内容
- **修复 `{"error": "'remaining_quantity'", "event": "市价平仓 TP1 部分失败: APLDUSDT"}`（假失败日志）**：重启恢复路径只创建残缺条目 `{'algo_ids': {}}`，读侧四处受影响
  - 根因（仅 new_coin，其他策略无 `_sync_baseline_to_tracking` 恢复模式）：`strategy.py` 条件单恢复只建 `{'algo_ids': {}}`，`_sync_baseline_to_tracking` 只补 `entry_price/entry_quantity/entry_time`，最终缺 `remaining_quantity`、`target1_reached`、`target2_reached`、`atr`、`lowest_price`、`highest_price`、`direction`、`trailing_activated`、`trailing_stop_price`、`pending_profit_pct`、`current_tier_index`
  - TP1/TP2 市价平仓回写 `remaining_quantity` 抛 KeyError（**单其实已成交**，仅日志假失败且 `target1_reached` 未置位）
  - `update_target_status` 的 `remaining_quantity *=` 抛 KeyError（原两处字面量重复 16 行）
  - `_check_trailing_stop` 读 `.get('remaining_quantity', 0)` == 0 → **静默不平尾仓**，随后 `clear_position_tracking` 清跟踪
  - `_sync_trailing_stop_order` / 补单路径同样受影响
- **写入侧**：新增唯一创建入口 `executor.py::_build_tracking_entry()`（三处重复字面量收敛为一处）与幂等 `ensure_tracking_entry()`；正常开仓、补全条件单、重启恢复（条件单恢复 + 基线同步）三条路径统一走工厂，字段集完全一致；`_sync_baseline_to_tracking` 显式写 `remaining_quantity = pos['quantity']`（重启初值 = 交易所当前在仓数量）；`entry_time` 仍经 `to_aware_utc` 规范化
- **读侧**：新增 `_mark_partial_close()`（回写不再抛 KeyError）与 `_should_close_remaining()`（剩余量缺失时退化到最近跟踪数量，避免静默漏平）；`update_target_status` 改为先取值再赋值
- **测试**：新增 `tests/test_strategies/test_new_coin_tracking_entry.py`（11 项，含复现 L3080 写回路径）；`tests/test_strategies/test_capital_allocation_enforcement.py` 的 `_build_baseline_executor` mock 补绑真实 `_build_tracking_entry` / `ensure_tracking_entry`（生产代码现经该方法写条目）

### 验证结果（五层验证）
| 层级 | 内容 | 结果 |
|------|------|------|
| 1 容器状态 | trading_system-new_coin `Up (healthy)` | ✅ |
| 2 镜像 | `ghcr.io/bearflower/trading-new-coin:latest` | ✅ |
| 3 VERSION | ⚠️ 仓库内 VERSION 为静态文件（`DEPLOY_ID=2C5243DD` / `GIT_SHA=9aa9b62`，2026-09-23），未随本次 CI 更新；容器内与本地完全一致，故以文件 MD5 为准 | ⚠️ |
| 4 文件MD5 | 容器内 executor.py / strategy.py 与本地逐字节一致 | ✅ |
| 5 日志错误 | 重启后 `error`/`traceback` 计数 = 0；无 `持仓跟踪缺少剩余数量` 告警 | ✅ |

### MD5（本地=容器内）
| 文件 | MD5 |
|------|-----|
| strategies/new_coin/executor.py | d04d173311f443565eaa3965e0e0ad11 |
| strategies/new_coin/strategy.py | eedd5b02e977e881618a82d2f2446e5d |

### 测试
- ✅ 本地 `tests/test_strategies` 565 passed / 1 xfailed / 0 failed；新增测试 11 passed（修复前 10 failed / 1 passed 复现回归）
- ✅ 幻觉测试 10 项逐项实读源文件核实（import / 属性 / 配置键 `target1_close_percent` `target2_close_percent` / `rebuilt` 键 `quantity` `entry_price` `entry_time` / 同步异步上下文 / 新增行 ≤120 字符）
- ✅ 规范检测（code-specification-inspector）：无硬编码、无重复代码、无幽灵参数、无新增超长行/超长函数 —— 通过
- ✅ 部署路径：仅 `strategies/new_coin/` 变更 → 仅 `new-coin` 构建，其余 9 个矩阵 job skip（GitHub Actions Run #23 全绿）
- ✅ 启动路径验证：容器重启后日志「持仓基线已同步到 position_tracking symbols=['USDBRLUSDT']」，`position_count=1`（非本策略持仓已过滤），无 `remaining_quantity` 告警

---
## 2026-09-22 追加部署（修复 entry_time 类型不一致导致止损失效 + 重复代码重构）

### 变更内容
- **修复 `unsupported operand type(s) for -: 'datetime.datetime' and 'str'`（75 个错误）**：APLDUSDT 的紧急止损/时间止损自 9-21 起每小时报错跳过，**资金保护实际未生效**
  - 根因：`entry_time` 类型约定不一致。`self.positions[symbol]['entry_time']` 是 ISO 字符串（持久化格式），而 `executor.position_tracking` 约定为 aware datetime
  - 链路：`strategy.py::rebuild_from_exchange` → `_merge_rebuilt_positions` → `self.positions = rebuilt` → `_sync_baseline_to_tracking`（原样写入字符串）→ executor 两个止损方法做减法时 TypeError。**该链路仅在重启后执行**，9-20 部署重启后触发
  - `shared/utils.py`：新增 `to_aware_utc(value, default=None)`，作为时间规范化的唯一实现（ISO 字符串 / naive datetime / aware datetime → aware UTC）
  - `strategies/new_coin/strategy.py`：`_sync_baseline_to_tracking` 写入 `position_tracking` 前转换类型（失败时以 `datetime.now(timezone.utc)` 兜底）
  - `strategies/new_coin/executor.py`：新增 `_normalize_entry_time(symbol, entry_time, check_name)` 公共方法，两个止损检查入口统一规范化；无法解析时告警并跳过检查，避免静默失效
- **规范重构**：初版在两处各写了约 10 行相同的"规范化→判空→告警→return"守卫块，违反项目"禁止重复代码"规则（连续 5 行以上相同逻辑即违规），已提取为公共方法 `_normalize_entry_time`

### 验证结果（五层验证）
| 层级 | 内容 | 结果 |
|------|------|------|
| 1 容器状态 | 6 个受影响容器 Up (healthy)，kline-monitor 亦正常 | ✅ |
| 2 镜像ID | 均以 `--no-cache` 重建并 Recreate | ✅ |
| 3 上传完整性 | 本地 = 服务器宿主机 MD5 一致 | ✅ |
| 4 文件MD5 | 6 个容器内 `shared/utils.py` 全部一致；new_coin/hrs/ai-tuner 三文件全部一致 | ✅ |
| 5 日志错误 | 部署后各容器 `error=0`、`unsupported_operand=0` | ✅ |

### MD5（本地=服务器=容器内）
| 文件 | MD5 |
|------|-----|
| shared/utils.py | ab9b079637df8392ddb21dfa2598eea2 |
| strategies/new_coin/strategy.py | e964fe2109f0af8d7507ed54fa6927bc |
| strategies/new_coin/executor.py | ce72e54adde01b1d767f6a034b32c3d3 |

### 测试
- ✅ 本地 22 项独立验证全部 PASS（含 AST 抽取真实函数体执行）
- ✅ 容器内运行时验证：直接执行容器内 `to_aware_utc` 与方法体 —— ISO 字符串/naive/aware 均返回 aware UTC 且相减成功；None/非法字符串返回 None 并告警
- ✅ 容器内端到端验证：以字符串 `entry_time` 调用真实的 `_check_emergency_stop`、`_check_time_stop`，`logger.error` 调用数 = 0（修复前会在此抛 TypeError）
- ✅ 启动路径验证：容器重启后 `_sync_baseline_to_tracking` 正常执行（日志"持仓基线已同步到 position_tracking"），无错误
- ⏳ 待确认：下一整点周期（02:02 UTC）APLDUSDT 不再出现 `unsupported operand`（修复前每小时 2 次）
- ✅ 部署方式：`shared/` 变更 → 重建 btc-eth-strategy、btc-eth-aggressive-strategy、grid-strategy、new-coin-strategy、hrs-strategy、ai-tuner

### 部署过程异常记录（重要）
- 部署中途发现 **new_coin 容器被外部进程 SIGTERM→SIGKILL 终止且未重建**（`Exited (137)`；docker events 显示 `container kill signal=15` 后 `signal=9`）。根因是此前用 `StopCommand` 中断本地 SSH 命令后，服务器端 `docker-compose` 进程成为孤儿并继续执行到"停止旧容器"阶段后被终止，留下「容器已停但未重建」的中间状态
- 处置：执行 `docker-compose up -d`（不带服务名）恢复全部服务，随后复验容器内 MD5 与运行时验证均通过
- 教训：**中断 SSH 部署命令后，必须检查服务器端是否残留 compose 进程**（`pgrep -fa docker-compose`），并逐项确认所有容器处于 Up 状态（对应 deployment.md 问题 5/6/7）

---
## 2026-09-20 追加部署（修复开仓失败原因被吞掉：真实原因透传）

### 变更内容
- **修复通知误报「入场失败: 做空限价单未成交或失败」**：HUTUSDT 总分 7.45 实际是被「总持仓保证金超限(199.27/150)」风控拦截（已持 3 仓 = `trading.max_positions` 上限），**未下任何单**，但通知却显示与实际不符的「限价单未成交」（7.45 分按规则应走市价单，文案自相矛盾）
- 根因：`executor.py` 的 `execute_short()` 有 6 条业务失败路径均返回裸 `None`，唯一调用方 `strategy.py` 无论哪条路径都硬编码上报同一文案，真实原因被吞掉
- 修复：`execute_short()` 返回契约改为 `Tuple[Optional[Dict[str, Any]], str]`，透传真实失败原因（账户余额不足 / 仓位大小计算失败 / 总仓位超限 / 总持仓保证金超限 / 开空仓下单失败 / 市价单未成交 / 限价单超时未成交 / 执行异常）；未成交分支按 `use_market_order` 区分「市价单 / 限价单」文案
- 风控判定条件与阈值来源**未改动**（阈值仍来自 `capital_mgr.get_total_margin_limit()` 读配置，无硬编码）
- 改动文件：`strategies/new_coin/executor.py`、`strategies/new_coin/strategy.py`（+ 测试 `tests/test_strategies/test_executor_trailing.py`）

### 验证结果（五层验证）
| 层级 | 内容 | 结果 |
|------|------|------|
| 1 容器状态 | trading_system-new_coin Up (healthy) | ✅ |
| 2 镜像ID | 容器镜像 == 最新构建 `sha256:4b193e12e665957fe3ab295fef82a7e9108d091f2718b273a5ac6e44ca53c2be` | ✅ |
| 3 VERSION 文件 | N/A（该 Dockerfile 未 COPY VERSION），以第四层关键文件 MD5 为主证 | ⚠️ N/A |
| 4 文件MD5 | 容器内 executor.py / strategy.py 与本地完全一致 | ✅ |
| 5 日志错误 | 部署后 200 行内错误数 0，启动初始化正常并对齐整点周期 | ✅ |

### MD5（本地 = 服务器 = 容器内）
| 文件 | MD5 |
|------|-----|
| strategies/new_coin/executor.py | ee775c65be059ad8c72682b26bca1fe5 |
| strategies/new_coin/strategy.py | 41e0815894169d0b5faf11bc6913dd3a |

### 测试
- ✅ `tests/test_strategies/` **303 passed / 0 failed**；新增 9 用例覆盖 6 条业务失败路径 + 异常路径 + 透传 + 空原因兜底
- ✅ 覆盖率核对：`execute_short` 改动的 8 处 return 行（205/212/238/256/273/302/371/379）全部命中
- ✅ 部署方式：按需重建，**仅 `new-coin-strategy`**（未触碰其他容器）；服务器旧文件已备份至 `executor.py.bak_20260920_165611`、`strategy.py.bak_20260920_165611`
- 📌 已知局限：该容器 Dockerfile 未 `COPY VERSION`，第三层校验不可用；建议后续补充 `COPY VERSION /app/VERSION` 以恢复该层校验

---

## 2026-09-20 追加部署（修复 -2013 重试噪音 + short_positions 时区写入失败）

### 变更内容
- **修复 `-2013 Order does not exist` 重试噪音**：`-2013` 表示订单已成交/已撤销（正常竞态），重试无意义
  - `shared/binance_api.py`：`-2013` 加入 `_NON_RETRYABLE_ERROR_CODES`
  - `shared/utils.py`：`-2013` 归入 debug 降级列表（原为 warning）
  - `strategies/new_coin/executor.py`：限价单超时取消时 `-2011/-2013` 视为"取消目标已达成"，降级为 info
- **修复 `short_positions 持仓记录写入失败`（时区错误）**：`_insert_short_position`/`_update_short_position_closed` 传入 `datetime.now(timezone.utc)`（带时区），而 `opened_at`/`closed_at` 列为 `TIMESTAMP`（无时区），asyncpg 拒绝绑定。改用 `datetime.now()`（naive UTC，与项目其他落库代码一致）
- **数据回填**：`new_coin.short_positions` 表此前因该 bug 从未写入成功（0 行），回填当前两个在持持仓
  - AMCUSDT：37.09 @ 2.69816554，opened_at 2026-09-19 19:03:42
  - APLDUSDT：3.57 @ 27.86000000，opened_at 2026-09-19 17:03:32
  - 回填后恢复两处依赖该表的能力：HRS 候选池排除同币种开仓（candidate_pool.py）、new_coin 重启兜底恢复（strategy.py）

### 验证结果（五层验证）
| 层级 | 内容 | 结果 |
|------|------|------|
| 1 容器状态 | 6 个受影响容器全部 Up (healthy) | ✅ |
| 2 镜像ID | 均以 `--no-cache` 重建并 Recreate | ✅ |
| 3 上传完整性 | 3 个文件本地/服务器 MD5 一致 | ✅ |
| 4 文件MD5 | 6 个容器内 shared 文件全部一致 | ✅ |
| 5 日志错误 | 部署后 5 分钟各容器错误数均为 0 | ✅ |

### MD5（本地=服务器=容器内）
| 文件 | MD5 |
|------|-----|
| shared/binance_api.py | 589639bf3069d96ac260c55eb8475f05 |
| shared/utils.py | c37b44536015d3f7cc442e1efc7e06d1 |
| strategies/new_coin/executor.py | 7b6c4aed1a77e9ba73ecc9e68c01c1f3 |

### 测试
- ✅ 单元测试：`-2013` 不重试（调用 1 次即抛）；可重试码 `-1003` 正常重试 3 次
- ✅ 运行时复现：容器内旧写法（aware）精确复现 `invalid input for query argument $5 ... can't subtract offset-naive and offset-aware datetimes`；新写法（naive）写入成功；测试数据已回滚
- ✅ 部署方式：`shared/` 变更 → 按需重建 btc-eth-strategy、btc-eth-aggressive-strategy、grid-strategy、new-coin-strategy、hrs-strategy、ai-tuner（未触碰 postgres/kline/kline-monitor）

---

## 2026-09-15 追加部署（激活移动止损：止盈成交检测）

### 变更内容
- **修复移动止损从未生效的 bug**：原代码中移动止损已存在（MTPCS/HRS 同源）但 `target2_reached` 从未被标记，导致移动止损永不激活，实际退化为纯固定止盈
- 新增「止盈成交检测」机制：通过对比交易所实际持仓数量与上次跟踪数量，检测 TP1/TP2 条件单成交（100%→70% 标记 target1_reached；70%→30% 标记 target2_reached 并激活移动止损；→0 全部平仓）
- 激活后启用已有双机制移动止损：回撤阶梯动态止损（shared/dynamic_trailing.py，MTPCS 同源）+ 最低价反弹 1.5×ATR（HRS 风格），锁住更多利润
- 新增 `trading.position_detection` 配置块（enabled / qty_tolerance_ratio / qty_tolerance_absolute / zero_qty_threshold）
- 改动文件：`strategies/new_coin/executor.py`（新增 `_get_exchange_position_qty`/`detect_take_profit_fills`/`clear_position_tracking`）、`strategies/new_coin/strategy.py`、`strategies/new_coin/config.yaml`

### 验证结果（五层验证）
| 层级 | 内容 | 结果 |
|------|------|------|
| 1 容器状态 | Up (healthy) | ✅ |
| 2 镜像ID | 容器 fc6ea8 == 本次构建 `trading_system-new-coin-strategy:latest` | ✅ |
| 4 文件MD5 | 三方一致（本地=服务器=容器内） | ✅ |
| 5 日志错误 | 启动后 200 行无 error/exception | ✅ |

### MD5（本地=服务器=容器内）
| 文件 | MD5 |
|------|-----|
| executor.py | d7b4527f099fd909ca79cc6221183d25 |
| strategy.py | 8a629c71fe160f4c7d11a573fd3bfd91 |
| config.yaml | e1bc92708defc0359e9f7f023460dd04 |

### 测试
- ✅ 新增 `tests/test_strategies/test_take_profit_fill_detect.py`（14 用例）
- ✅ 全量回归 272 passed
- ✅ 部署方式：按需重建（仅 new-coin-strategy，未触碰其他容器），`--no-cache` 防部署幻觉
- 部署 ID: `9D2DB354`

---

## 基本信息
- 部署时间: 2026-09-03 12:53 (Asia/Shanghai)
- 目标服务器: 43.156.242.184
- 项目名称: trading_system
- 容器名称: trading_system-new_coin
- 部署范围: 仅 new_coin 容器（新增总持仓保证金上限机制）

## 版本信息
- 部署方式: 定向部署（仅 new_coin 服务，未重建其他容器）
- 变更: total_position_margin_limit 配置 + executor 5.5.1 检查 + capital_manager.get_total_margin_limit

## 变更内容
- strategies/new_coin/config.yaml: trading 段新增 `total_position_margin_limit: 150`（总持仓保证金上限）
- strategies/new_coin/executor.py: 开仓前新增 5.5.1 总持仓保证金检查（保证金=仓位价值/杠杆，超限跳过开仓），与 capital_limits 并存取更严格
- shared/capital_manager.py: 新增 `get_total_margin_limit()`，每次调用动态读取配置文件 trading.total_position_margin_limit，禁止硬编码

## 验证结果

### 第一层：容器运行状态
- ✅ Up (healthy)

### 第二层：镜像与容器
- ✅ new_coin 镜像以 --no-cache 重建，容器已 Recreate 并启动

### 第三层/第四层：容器内代码 MD5 对比
| 文件 | 本地 MD5 | 容器内 MD5 | 结果 |
|------|----------|-----------|------|
| strategies/new_coin/config.yaml | 58536e18df85a3beea296a04da847dd6 | 58536e18df85a3beea296a04da847dd6 | ✅ |
| strategies/new_coin/executor.py | da6ec439a7a8b38d6ba71faf36d19685 | da6ec439a7a8b38d6ba71faf36d19685 | ✅ |
| shared/capital_manager.py | 2df681ad776b951d549cff312bad7930 | 2df681ad776b951d549cff312bad7930 | ✅ |

### 第五层：功能验证
- ✅ 容器内配置确认：`total_position_margin_limit: 150` 存在
- ✅ 策略初始化无 error/exception/traceback，交易执行器初始化正常（leverage=2, max_positions=3, single_position_margin=50）

## 最终结论
✅ **部署成功！new_coin 容器已运行本次新代码，代码级验证通过。**

---
## 2026-09-07 追加部署（时间止损前置复核）

### 变更内容
- 新增 `trading.time_stop_review` 多因素复核机制：持仓满72h未达第一目标时，先综合评分（趋势/反转形态/量能/情绪）判断空头逻辑是否仍成立，成立则继续持有，否则止损100%
- 增加豁免规则：距第一目标跌幅≥70% 豁免时间止损，继续持有到目标
- 数据异常时按 `bias_hold`（默认偏向继续持有）决策
- 改动文件：`strategies/new_coin/executor.py`、`strategies/new_coin/config.yaml`

### 验证结果
- ✅ 本地/服务器 MD5 一致（executor `dc52a62b…`、config `eed98441…`）
- ✅ 容器内 `_time_stop_review`/`_compute_review_score` 存在，`time_stop_review.enabled=true`
- ✅ 容器 Up (healthy)，启动无 error/exception
- ✅ 75 项测试通过（含 12 项新增复核专项测试）

---
## 2026-09-04 追加部署（移除持仓数量上限）

### 变更内容
- 移除 new_coin「持仓数量上限」机制（原 `trading.max_positions: 3`），改由**总持仓保证金上限（月度分配 monthly_limit 动态约束）**控制持仓规模
- 改动文件：`strategies/new_coin/executor.py`、`strategies/new_coin/strategy.py`、`strategies/new_coin/config.yaml`

### 验证结果
- ✅ 本地/服务器 MD5 一致（executor `7008423f…`、strategy `378068ac…`、config `b1f802aa…`）
- ✅ 容器内实际代码无 max_positions 残留（仅历史 .bak 备份含旧内容）
- ✅ 容器 Up (healthy)，启动无 error/exception
- ✅ 26 项测试通过

---
## 2026-09-03 追加部署（动态月度来源）

### 变更内容
- shared/capital_manager.py: `get_total_margin_limit()` 改为**优先读取 `capital_limits.monthly_limit`（月度资金分配金额，每月由 AI 动态更新），未配置时回退 `trading.total_position_margin_limit`（150）**

### 验证结果
- ✅ 容器内 MD5 `91fdac2b24fa705226eedb9690d2d27e` 与本地一致
- ✅ 功能验证：未配置 monthly 时返回 150 回退值
- ✅ 容器 Up (healthy)，启动无 error/exception