# 任务列表

## 进行中的任务

（暂无）

## 已完成的任务

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
