# 任务列表

## 进行中的任务

（暂无）

## 已完成的任务

### [最后更新:2026-10-10 09:27] HRS 撤单后条件单 DB 状态同步修复（已上线，两轮观察验证闭环）
- **状态**: 已完成（R3，0→8 全流程；GA / GC 通过；上线验证闭环）
- **创建时间**: 2026-10-09
- **完成时间**: 2026-10-09 22:31
- **涉及容器**: 全部 10 个镜像重建（`shared/condition_orders.py` 变更触发全量；`git diff` 全量重建逻辑）
- **问题**: 飞书「孤儿条件单自动清理完成」数量异常（LITEUSDT 5 / HBARUSDT 49，「跳过（正常持仓）」376~420）——DB 中 OPEN 假孤儿线性累积（约 `140h × 3 = 420`）
- **根因**: HRS 撤单（补单清场 / 平仓清场 / 批量撤单）成功后只清本地内存 `algo_ids`，不回写 `condition_orders` → 交易所已无单而 DB 永久残留 OPEN，被孤儿清理反复「取消」（Binance `-2011` 被当作成功）并产生误导告警
- **实现**: 3 个代码文件——`shared/condition_orders.py`（新增 `mark_open_orders_canceled` 整币种置 CANCELED）、`strategies/hrs/position_manager.py`（`cancel_all_orders` / `_cancel_individual_orders` 成功后调 `_sync_db_canceled`，`failed>0` 保守保留）、`tests/test_position_manager_cancel.py`（新增 5 条 AC 用例）
- **测试/审查**: 新增+相关 16 passed；GA（R3 独立设计复核）通过 → GC（C 段独立审查，含 4 处撤单调用点核对）通过，无阻塞项
- **部署**: commit `e9466537`，Actions Run #54（`DEPLOY_ID=00003601`），五层防幻觉验证全绿（容器 / 镜像 / VERSION 一致 / 关键文件 MD5 / 部署日志）
- **上线验证（两轮监控）**: 21:21 HRS 补单周期 AEROUSDT `UPDATE 3` 撤旧建新 + COINUSDT/DRAMUSDT 整币种同步清沉积；21:56 清理首轮 `canceled=64`（牛来USDT 清空）→ 总量 **274→210**；22:26 清理次轮**无取消无失败、告警停止** → **210→209**，**只降不增**。飞书「跳过」由 376~420 降至 255
- **遗留（范围外、不阻塞）**: ① ai-tuner 对 `-2011` 口径仍无法区分「本就已撤销」与「真取消」；② `shared/reduce_only_close.py:275` 的 `-2022` 路径未同步 DB（无 DB 句柄且为共享模块），薄概率残留会在下次成功撤单或孤儿清理收敛
- **观察项**: 剩余 ~209 条为有持仓 symbol 历史沉积，随持仓平掉后自然收敛；未产生新告警
- **文档**: `docs/plans/fix-2026-10-09-hrs-condition-order-db-sync.md`（§7.2 时间序列）；交接快照 `docs/handoffs/hrs_condition_order_db_sync_2026-10-09.md`
- **文档对照**: `code-document-curator` 技能未安装，已人工核对——无文档与本变更相悖（v6.23 孤儿条件单需求 R2.4「取消成功后更新 CANCELED」/ 限价单与孤儿单修复方案 L964 均如此要求，本修复使 strategy 侧撤单路径满足之）

### [最后更新:2026-10-09 14:55] 月度资金分配月份口径修复 + 零开仓垫底规则（已上线，2026-10 已补生成）
- **状态**: 已完成（R3，A→A-review→B→C→D 全流程，GD 通过）
- **完成时间**: 2026-10-09 14:55
- **涉及容器**: 实际重建 **ai-tuner + hrs + dashboard-api** 3 个镜像（deploy.yml §6.5：hrs/dashboard-api 全项目 COPY，`ai_tuner/` 变更必然连带；handoff 原预测「仅 ai-tuner」已在发布前修正并经用户授权），其余 7 服务容器未重启
- **问题**: 看板【月度资金分配】停留 2026-09；9/30 月度任务未生成 2026-10 记录 → 10 月四策略 DB 限额全月落空
- **根因**: `monthly_job.run_monthly_allocation()` 用「刚结束当月」同时作幂等键与写入 month，而消费方（capital_manager/daily_refresher/dashboard）按当前生效月查询 → 幂等命中跳过，次月永久查不到
- **实现**: 双月份口径 `--pnl-month/--effective-month`（月末自动推导、跨年）、幂等键/写库/通知/配置五处统一生效月、半参拒绝；零开仓按 `order_type NOT IN ('PNL_SUMMARY','CONDITIONAL_ORDER')` 行数判定（不依赖 status——限价单成交恒为 NEW，A-review F1 阻塞项闭环），排序键 `(has_trades, -return_rate, strategy_id)`，rank_ratios 总额 85% 不变；查询异常双层 fail-open；95 用例、变更三模块行/分支 100%
- **审查**: A-review（GLM-5.3）F1 阻塞闭环后 GA 放行；C（GLM-5.3 独立新对话）**GC 通过**无阻塞（N1 参数核对/N2/N3 知悉）；报告 `docs/reviews/2026-10-09_capital_allocation_month_fix_zerotrade_c_review.md`
- **部署**: commit `48b8bf2`，Actions Run #53（`DEPLOY_ID=00003501`），部署日志 `DEPLOY_SUCCESS`；五层防幻觉全绿（三容器 healthy/镜像为本轮新建/VERSION 一致/3 模块 MD5 逐一一致/零 error）
- **补生成**: 容器 `/tmp/manual_allocation_trigger.py --pnl-month 2026-09 --effective-month 2026-10`（MD5 校验后执行）→ `2026-10` active 4 策略：aggr 170.44(0.3,+30.76%) > btc_eth 142.03(0.25) > new_coin 113.63(0.2) > hrs 56.81(0.1,-40.76%)，总额 568.13/可分 482.91(85%)；9 月四策略均有成交（38/137/154/313 行），垫底规则本月无触发对象；飞书卡片发送成功
- **上线验证**: ① DB 记录正确；② 容器内真实 `CapitalManager.resolve_monthly_limit()` 四策略均 source=db 命中 2026-10 额度、`_current_month()=2026-10`、5 策略容器零限额 DB 失败日志；③ dashboard-api `/api/ai-monitor` 返回 2026-10 active
- **观察项（不阻塞）**: daily_refresher 今日按 2026-10 刷新日志；各策略下次开仓按新限额判定；hrs 实时占用 64.94/56.81≈114%（既有持仓，新限额约束新开仓）；new_coin config 缺 `position_sizing` 段的 error 为既有现象（capital_limits 写入正常）；10/31 月末自动任务应生成 2026-11（验证月份口径的下一个自然观察点）
- **回退**: append-only 删 2026-10 行 + 回滚镜像即可，2026-08/09 不受影响（未使用）
- **交接快照**: `docs/handoffs/capital_allocation_month_fix_zerotrade_2026-10-09.md`

### [最后更新:2026-10-09 12:00] 看板最大回撤口径统一 + HRS 重复记账修复（已上线）
- **状态**: 已完成
- **创建时间**: 2026-10-09
- **完成时间**: 2026-10-09 11:52
- **涉及容器**: 全部 10 个策略/服务镜像重建（`shared/trade_logger.py` 变更）；迁移容器 `trading_system-postgres`
- **问题**: ① 月度 tab HRS 最大回撤 97.8% 离谱；② 月/周/日 tab 下 MTCPS 激进版与新币做空最大回撤完全不变；③ HRS 重复记账
- **根因**: ① 回撤用快照复利链推导导致失真；② HRS `_writeback_pnl_for_full_close` 先 `insert_pnl_summary` 再 `mark_stop_loss`，而 `_mark_existing_close_record` 匹配窗口仅 ±10 分钟 → 匹配失败降级再写一条（确定性双写）；③ 现象② 系新口径下最深回撤都落在最近 30 天内（各窗口均包含）→ 同值，非 bug
- **实现**: 净值改 `equity = base_capital + cum_pnl`、`net_value = equity/base_capital`（起点 1.0、不复利、负值钳 0 → 回撤上限 100%）；`base_capital` 取 `allocated_amount` 四级退化；三序列同步截取窗口、历史月份不取未来快照；新增 `*_perf_window_*` 样本窗口字段 + 前端展示；HRS 改单条写入 + `since` 幂等去重
- **审查**: C 段 GLM-5.3 独立审查（未参与实现）R1 GC 有条件通过 → R2 复审 R-B1 闭环 → **GA 放行**；报告 `docs/reviews/2026-10-09_C段实现审查报告_看板口径与HRS修复.md`
- **部署**: Actions Run #52（commit `b43a8d9`，`DEPLOY_ID=00003401`），五层防幻觉验证全绿（容器/镜像/VERSION 10/10/关键文件 MD5/部署日志）
- **数据迁移**: `database/postgres/migrations/2026-10-09-hrs-pnl-dedup.sql` 已执行——删 48 条汇总重复 / 误删真实成交 **0** / 止损标记迁移 18 / SL 19→19 / HRS 总盈亏 -197.2060→**-33.4482**；备份表 `trading.trade_records_hrs_dedup_backup_20261009`（48 行）可回滚
- **上线验证**: 月度口径 MTPCS 70.00% / 激进 9.86% / 新币 6.70% / 账户级 5.69%；HRS 100%（用户已拍板接受的分母过渡语义）；`/api/risk?days=7` 止损次数 2
- **遗留/观察**: HRS 日/周回撤分母自愈（日 ≈2026-10-10 17:00、周 ≈2026-12-09）；面板「打不开」经查为**客户端侧**（服务端 nginx/dashboard-api 全 200、用户 IP 11:54 已成功加载），非本次回归
- **文档更新**: `docs/design/dashboard_architecture.md` §4.2.3 追加 v3 口径说明；`docs/design/dashboard_ui_design.md` 追加样本窗口展示说明
- **交接快照**: `docs/handoffs/max_drawdown_caliber_and_hrs_dup_2026-10-09.md`
- **备注**: `/finish` 流程要求的 `code-document-curator` 技能当前未安装，已按 `development-workflow` 规则改用现有工具人工完成文档对照检查

### [最后更新:2026-09-21 11:45] 数据后台独立容器（调度宿主解耦）
- **状态**: 已完成
- **创建时间**: 2026-09-21
- **完成时间**: 2026-09-21 11:42
- **涉及容器**: 新建 data-backend、重建 dashboard-api
- **问题**: 4 个数据维护定时任务（净资产快照/指标预计算/持仓对账/佣金回填）的 APScheduler 全挂在 dashboard，dashboard 被撤改则数据连续性受损
- **实现**: 新建 shared/scheduler 工具箱 + services/data_backend 容器（单 DataService + 单 AsyncIOScheduler + 4 job + 预热 + SIGTERM 优雅关闭）；dashboard 删除全部调度器（-132 行）退化为纯 API
- **部署**: 先 data_backend 后 dashboard（关闭双写窗口）；两容器五层验证全通过
- **解耦证据**: dashboard 近 5 分钟任务日志 = 0；data-backend 每分钟 `03:3x:16 指标预计算完成`，与 DB `metric_snapshot.updated_at=03:39:16` 完全对齐
- **遗留**: 首轮任务双跑（幂等无害）、data_backend/deploy.sh MD5 标签错误、equity_snapshot 今日行需 23:30 后复核
- **过程记录**: `.trae/memories/2026-09/21/1145-data-backend-decoupling.md`
- **交接快照**: `docs/handoffs/commissions_backfill_2026-09-21.md`

### [最后更新:2026-09-21 11:45] new_coin 持仓基线归属过滤（P0 热修）
- **状态**: 已完成
- **创建时间**: 2026-09-21
- **完成时间**: 2026-09-21 11:10
- **涉及容器**: trading_system-new_coin（按需重建）
- **问题**: 基线重建出 8 个币种 total_margin=348.85，仅 3 个属 new_coin → ①占用虚增致永久停止开仓 ②为他策略币种建 tracking 条目致越权管理他策略仓位
- **根因**: PM 账户 positionRisk 返回账户内全部空头，无策略隔离
- **修复**: executor 新增 get_open_short_symbols（DB short_positions 为权威来源）；strategy 新增 _filter_own_positions 过滤后再合并；新增 4 单测
- **止血**: 10:52 停 new_coin 容器阻断 03:00 UTC 周期（周期未执行，无越权）；PATHUSDT 减仓 21.99 → 7.33 张
- **部署**: 五层验证全通过；生效证据 position_count=3 / total_margin=154.217 ≤ 156.33
- **过程记录**: `.trae/memories/2026-09/21/1145-new-coin-baseline-ownership-filter.md`
- **交接快照**: `docs/handoffs/capital_limit_enforcement_2026-09-21.md`

### [最后更新:2026-09-21 10:35] 佣金回填功能实现 + 部署
- **状态**: 已完成
- **创建时间**: 2026-09-21
- **完成时间**: 2026-09-21 10:35
- **涉及容器**: dashboard-api（仅重建）、策略容器（不重启）
- **问题**: dashboard 看板总佣金恒为 0。共因：币安下单返回不含 commission 字段（佣金只在 userTrades 成交明细返回）
- **方案**: 事后回填。公共逻辑放 shared/trade_logger.py，调度宿主复用 dashboard APScheduler
- **实现**: reconcile_commissions + get_user_trades + commission_reconcile_job + main_docker 调度器；配置走 env（周期3600s/窗口24h）
- **佣金符号修复**: userTrades commission 为无符号正数，需按项目"佣金=负值支出"口径取负落库
- **部署**: 仅重建 dashboard-api，五层验证通过，落库佣金均为负值（HRS -0.0277 / MTPCS激进 -0.1005 / 新币 -0.1196）
- **遗留**: git 未提交、调度宿主耦合 dashboard 隐患、条件单平仓佣金无法归集、单测未补跑
- **过程记录**: `.trae/memories/2026-09/21/1035-commission-backfill.md`
- **交接快照**: `docs/handoffs/commissions_backfill_2026-09-21.md`

### [最后更新:2026-09-11 10:38] HRS 调优失败修复（JSON 解析 + 配置缺失）
- **状态**: 已完成
- **创建时间**: 2026-09-11
- **完成时间**: 2026-09-11 10:38
- **涉及容器**: ai-tuner、trading_system-hrs、trading_system-new_coin
- **问题**: HRS LLM 调优 JSON 解析失败（`Extra data: line 1 column 51`）+ 配置缺 `trading.max_positions`
- **修复**: response_parser 三层加固（字符串感知提取 + raw_decode 兜底）、HRS/new_coin config 补 `trading.max_positions: 3`
- **测试**: 修正断言 + 新增 6 个测试用例
- **部署**: 按需重建 ai-tuner/new_coin，重启 hrs，MD5 验证一致
- **过程记录**: `.trae/memories/2026-09/11/1038-ea7c2f1a.md`

### [最后更新:2026-07-16 10:30] 限价单与孤儿单修复 + 部署
- **状态**: 已完成
- **创建时间**: 2026-07-16
- **完成时间**: 2026-07-16 10:30
- **涉及容器**: trading_system-new_coin、ai-tuner
- **修复内容**:
  - ✅ **限价单改造（C方案）**: new_coin 策略 8 处市价单改为限价单（开仓 LIMIT、止损 STOP、止盈 TAKE_PROFIT、平仓 LIMIT 带超时重试→市价回退）
  - ✅ **交易层拦截（B方案）**: shared/binance_api.py 增加市价单检测警告，通过 webhook 通知
  - ✅ **本地 algoId 管理（B方案）**: new_coin 创建条件单时保存 algoId，平仓时用本地记录直接取消，不再依赖已废弃的 get_open_algo_orders() API
  - ✅ **ai-tuner 兜底清理（A方案）**: 新增 orphan_cleanup 模块，每 30 分钟检测 strategy_states 异常并通知
- **部署验证**:
  - ✅ trading_system-new_coin: Up 3 hours (healthy)
  - ✅ ai-tuner: Up (healthy)，孤儿单清理任务已注册到调度器
  - ✅ 限价单类型已生效（LIMIT/STOP/TAKE_PROFIT，无 MARKET 条件单）
  - ✅ 调度器任务：周度AI调优、月度资金分配、利润提取提醒、孤儿条件单清理检查
- **文档更新**:
  - 需求文档: 限价单与孤儿单修复方案.md
  - 架构设计: 限价单与孤儿单修复架构设计.md
  - 完全平仓后取消孤儿条件单.md (v1.0→v2.0)
  - 新币做空策略 V4.0 完整版.md (补充说明)
  - 需求索引 README.md (补充遗漏文档)

### [最后更新:2026-05-12 23:03] v6.16.8版本实现 - 动态ATR + 动态成交量 + 币种差异化
- **状态**: 已完成
- **创建时间**: 2026-05-12
- **完成时间**: 2026-05-12 23:03
- **进展**:
  - ✅ 已读取完整方案文档
  - ✅ 已分析现有代码结构
  - ✅ 已更新config.yaml添加币种差异化配置
  - ✅ 已更新dynamic_atr_filter.py支持币种差异化参数
  - ✅ 已创建backtest_v6168.py回测脚本
  - ✅ 已运行回测并生成对比报告
- **核心改进**:
  - 动态ATR过滤器：基于历史35%分位数 + ADX调节 + 币种绝对下限
  - 动态成交量过滤器：基于过去20小时均量 + ADX调节 + 币种差异化倍数
  - 币种差异化参数配置：为BTC、ETH、BNB、SOL、XRP、TRX分别设置独立阈值
- **回测结果对比**:
  | 指标 | v6.16.7 | v6.16.8 | 变化 |
  |------|---------|---------|------|
  | 总收益率 | 6.63% | 5.70% | -0.93% ↓ |
  | 总交易次数 | 99 | 146 | +47 ↑ |
  | 胜率 | 67.68% | 66.44% | -1.24% ↓ |
  | 最大回撤 | 3.29% | 10.28% | +6.99% ↑ |
  | 夏普比率 | 1.55 | 0.60 | -0.95 ↓ |
- **关键发现**:
  - ❌ v6.16.8表现不如v6.16.7
  - ❌ 动态ATR过滤器过于宽松，导致更多低质量信号
  - ❌ 动态成交量过滤器效果不佳，阈值设置过低
  - ❌ S级信号门槛降低，信号质量下降
  - ❌ A级信号表现恶化，成为主要亏损源
- **优化建议**:
  - 收紧动态ATR过滤器（SOLUSDT绝对下限0.6%→0.7%）
  - 提高动态成交量阈值（SOLUSDT S级1.8→2.0，A级1.5→1.8）
  - 恢复S级额外验证（4小时ADX>30，收盘价与EMA21距离<1.5×ATR）
- **报告位置**:
  - 对比报告: /backtest/btc_eth/reports/v6167_vs_v6168_comparison.md
  - v6.16.8回测脚本: /backtest/btc_eth/scripts/backtest_v6168.py

### [最后更新:2026-05-09 13:05] ETHUSDT网格交易策略优化回测
- **状态**: 已完成
- **创建时间**: 2026-05-09
- **完成时间**: 2026-05-09 13:05
- **进展**:
  - ✅ 已更新优化参数配置
  - ✅ 已修复回测引擎除零错误
  - ✅ 已执行优化后的回测
  - ✅ 已生成对比分析报告
- **优化参数**:
  - 网格数量: 12（优化前20）
  - 网格范围: 10-15（优化前8-30）
  - 最大回撤: 15%（优化前10%）
  - 硬止损: -15%（优化前-8%）
  - 日亏损限制: 3%（优化前5%）
  - 仓位上限: 80%（优化前30%）
- **优化效果**:
  - 总收益率: -21.95%（优化前-99.95%，改善78%）
  - 最大回撤: 164.81%（优化前100.34%，恶化64.47%）
  - 夏普比率: -0.20（优化前1.58，恶化1.78）
  - 总交易次数: 99（优化前2176，减少95.45%）
- **关键发现**:
  - ✅ 收益率大幅改善，保留了78%的初始资金
  - ✅ 交易频率显著降低，减少手续费侵蚀
  - ❌ 最大回撤异常（超过100%），计算逻辑有误
  - ❌ 出现空头持仓，持仓管理存在问题
  - ❌ 网格重置过于频繁（15%阈值过小）
- **下一步优化**:
  - 实现ATR动态网格间距
  - 提高网格重置阈值到25%
  - 添加市场状态识别（ADX）
  - 修复持仓管理逻辑
  - 降低仓位比例到50%
- **报告位置**:
  - 优化后报告: /backtest/grid/reports/backtest_report_20260509_130515.md
  - 对比分析: /backtest/grid/reports/ETHUSDT_optimization_comparison_report.md
