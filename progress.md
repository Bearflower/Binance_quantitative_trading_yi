## 2026-11-17 HRS 持续实施补充

- 实际生产已出现 DOGE 标准多仓：两独立库存一致，STOP_MARKET closePosition 和全量 TP1 均存在；不主动交易或平仓凑验收。新发布保留旧仓已有保护和原退出参数。
- 真实进程 `/app/logs/tuning-loaded.json` 配置哈希为 `39e90a4d1b1cc526f42d14fb3248a9b292c0aa500a36e4df76159efda25fbb5e`，与封存 effective_config 一致；V20260819.yaml 仅注释，因此实际 fallback 并非取错挂载。
- 完成最终 HTTP 风险复核、ContextVar 任务/生命周期隔离、所有原始市值 provider ID 留证、标准/LV 各级早退原因、真实执行结果、开仓时冻结退出计划；复审发现 JSON null/浮点溢出后已复现修复。
- 无 HTTP 尝试的门禁否决预留现在可提交释放；先前发送结果不明则不释放。实际 PostgreSQL 检查已证明异常抛出后释放事务仍提交，避免误回滚。
- 回放不借用成交前分钟高点，不使用未公布结算标记价；延迟资金费保持经济发生时刻/信息获得时刻分离，未知时停新风险并继续退出。
- 957 项完整定向测试通过；现存持仓账本启动证明与持续验收报告正在完成独立复审。未进行本轮生产发布。

# 进展

2026-11-18 12:00 CST：6bde184c实际发布成功，1150候选测试通过。配置hash/源采集/独立writer新鲜、三个pin active、WLD28两独立库存及原硬止损未变、无关容器未替换。非admin权限与回滚后重试SHA覆盖缺陷已修复。新仓仅daily_origin_missing拒绝；独立只读报告数据coherent、source_error空、财务reasons空，仍0完整小时/0闭合样本/PENDING。离线窗口精确午夜对齐的可用性新缺口正在单独修复，不改实盘经济参数。

2026-11-18 最新状态：真实生产数据账本预检的来源/逐笔归属/资金费证明通过，剩余真实午夜原点缺失。候选 9204d908 云端 1133 测试通过；生产首次初始化因 trading_user 对权益段 SELECT FOR UPDATE 缺最小权限而失败并已回滚。正在用真实非管理员角色补全链路测试，并修复发布失败的脱敏诊断。完整 Aug/Sep 反事实绩效仍因历史输入不足受阻，SL/TP 不选新参数。

2026-11-17：用户批准完整落地，已建立隔离工作树，读取设计和分任务开发技能，开始生产基线核对。


- 生产 HRS Python+相关共享模块共20文件已导出 `/tmp/hrs-opt-production`，哈希清单 `docs/reports/hrs-production-source-baseline-2026-11-17.json`；与分支逐字节一致。基线464测试通过。
- 市值任务：19f8aeaf补6个CG唯一映射（CMC全部仍禁用），交易所元数据筛除TradFi；两轮审查修复失效统计和坏元数据缓存，最终6b3a55e9。原始缺市值35为6crypto/29TradFi，非LV52完整清单。
- 风险核心：fff0c4a2提供risk_ledger/loss_gate/repository及SQL，45测试含8真实PG；根代理按审查修正精确滚动30*24h窗口到6b3a55e9，联合55测试通过。边界仍要求真实午夜与滚动端点，不能造历史。
- 采集：a7eac1b1新增decision_capture/append-only表+独立snapshotwriter+strategy装饰器。7测试含真实PG通过；spec审查发现nestedID、failure跨scan清零、采集失败阻碍维护、LV/执行否决缺覆盖、伪complete等5项。根代理已修主要项未提交，目前9capture+HRS共441测试通过；需复审并继续补完整上下文/导出器。Sticky failed不得begin清除，public采集失败返回已取得公开价格，靠entryguard拒新仓。
- 回放由agent /root/replay_design实现中，自己拥有core_decision/exit_geometry/scoring sync接口和replay子系统/测试，根代理不得覆盖其文件；待其提交后spec/code审查。已有273回归通过、1m LIMIT15m超时模型和资金费证据，但未最终提交。
- 活跃PG unix-only：`postgresql://yl@/postgres?host=/tmp/hrs-opt-pg&port=55441`，data=/tmp/hrs-opt-pg/data。Python=/tmp/hrs-test-venv/bin/python(3.12)。
- 未做任何本轮生产部署。当前prod仍认证市值386e4330 / HRS2cf4f020。下一步根代理：完成capture复审、权益采集/新门禁串行接入、live共享core/exits和每仓退出参数、SLTP实验CLI、历史窗口审计、云端构建+部署+持续验收。


---
## 先前七项整改记录（保留，不属于本次 HRS 状态）

# 七项整改进度

## 2026-11-07

- 阅读既有整改方案、七项清单及相关技能；确认主工作区有大量未提交改动。
- 在 `/private/tmp/strategy-seven-gaps` 建立独立工作树 `codex/seven-strategy-gaps`，基于 `origin/main` 8991d03d。
- 只读检查生产活动 pin 为 8991d03d，相关七个容器均健康。
- 下一步：运行针对性基线测试，追溯 STOP 成交链和七项具体代码边界。
- 基线定向测试：146 passed、2 skipped。旧网格集成套件 14 passed、1 failed，失败是既有 mock 价格类型错误。
- 网格 direct-order 与 disabled-bot 两项用例先失败；已加半自动模式双层拒单，定向 4 passed。尚未发布。
- 生产只读 `strategy_gate` 通过；当前 SOLUSDT 多仓 0.27，持仓保护门禁通过。不得沿用前日空仓判断。
- 激进版退出计数错误先以失败测试复现，修复后频控/网格定向测试 36 passed。
- 新增 PM UM 按 algoId 只读查询，要求响应身份匹配；测试先因方法缺失失败，修复后 38 passed。
- 新增 STOP order/fills 纯校验器；测试先红后绿，区分触发、实际普通订单、reduceOnly、重复/缺失成交。它只给订单级事实，尚不授权 lot 归属或启动冷却。
- 新增订单级只读查询编排：algoId → actualOrderId → 普通订单 → userTrades；1000 条满页时拒绝宣称完整。测试先因函数缺失失败，再通过。
- 七项现状、整改边界和验收门槛写入 `docs/reports/seven-strategy-gaps-2026-11-07.md`。
- 综合定向验证：173 passed、2 skipped、1 deselected；deselected 是已确认的旧网格 mock 价格类型故障。`git diff --check` 通过。

## 2026-11-09

- 七项持续跟踪清单已提交并推送主线；活动生产 pin 为 `6279ded8`，风险政策版本 2 到 11 月 12 日 10:54 北京时间。
- 激进版新增稳定 `episode_entry_order_id`，加仓保留首单，元数据持久化和恢复保留该字段；旧快照缺字段时不倒推。
- 新增 `trading.aggressive_stop_bindings` 候选归属表和写入函数，重复写入仅在身份完全一致时接受；所有新 STOP 的通用下单路径与极端行情直写路径尝试绑定。绑定失败只影响归属证据，不阻断已挂交易所保护单。
- 定向测试 18 passed。旧大套件组合 130 passed、18 failed，失败集中于旧开仓 mock/频控构造，需要基线核对；不能宣称全回归通过。
- 下一步：退出事件幂等事务、逐笔成交唯一性与数量守恒、冷却门禁、数据库集成和受控发布。当前代码未部署。
- 同日继续：新增只读闭合批次证明（首入场 → 所有中间成交 → STOP 完整成交，没有中间归零/重开）及不可变退出事件/逐笔 ID 表。定向 60 passed；扫描器、冷却门禁、真实 PG 事务和受控发布仍待完成。
- 同日继续：接入待核 STOP 扫描和新开仓门禁，定向 66 passed。旧大套件 130 passed、18 failed，失败数与接线前相同。识别跨重启绑定落库失败的持久恢复缺口，故不发布、不标完成。
- 增加绑定失败的进程内与已保存持仓快照拦截；完全数据库断连又重启的窗口仍需外部持久 outbox/只读重建。定向 67 passed，Python 编译检查通过。
- 修复持仓恢复时保留未绑定 STOP 标记，防止下一次元数据持久化误清该标记；定向 68 passed。
- 继续：核实 Binance PM UM algo 支持 `clientAlgoId` 下单与查询；新增 `/app/data` fsync 意图日志，在交易所 POST 前写入，DB 绑定后解决，重启可按 ID 查回。旧仓批次未知保持待复核。极端行情 STOP 收紧先写日志再撤旧保护单。
- 本机一次性 PostgreSQL 55432 `stop_tests` 应用新脚本成功，真实外键、幂等、跨交易 tradeId 冲突回滚测试 1 passed；测试实例已停止。
- 新代码仍仅在隔离分支；生产只读预检、受控发布与真实 STOP 样本验收待完成。
- 新增本轮定向 88 passed，真实 PG 1 passed；发现并修复“交易所已接受 STOP 后日志解决标记异常导致返回 None、可能重挂”的竞态。
- 生产只读门禁版本 2 通过、账户空仓无挂单；持久目录存在。四条旧持仓快照均无首单身份，其中 SOL 更新于 11 月 8 日晚；不得自动回填归属。
- 同日 15:49–15:57 北京时间：独立复审三轮反馈已修复。STOP 意图日志 v2 记录预写/准备 POST，明确拒单关闭，未知响应保留；极端行情撤单不确定时以旧 algo 状态恢复；硬止损与移动止损分别回填；人工复核事件可继续重扫。四条旧快照缺批次身份时新开仓拒绝。最新 53 个 STOP 定向测试通过，语法和 diff 检查通过；提交 `54fa0195`，未发布。
- 生产只读 `strategy_gate --revision 2 --require-reduced` 再次通过，权益约 518.2783 USDT、空仓无普通/条件挂单、分配哈希不变。旧 BNB/ETH STOP 撤销无实际订单，SOL/XRP STOP 有实际成交，但均缺首单身份，不推定批次归属。
- 发布路径调查：CI 仅 main 推送自动部署，普通模式会受活动 scoped pin 阻断；既有 `strategy-risk-followup` manifest 绑定旧基线 `8991d03d` 和旧发布范围，不能复用。需要为当前 pin `6279ded8` 新建限定发布模式和 manifest，不能直接推本隔离分支或走全量部署。
