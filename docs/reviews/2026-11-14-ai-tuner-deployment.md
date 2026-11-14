# AI Tuner 全项目修复部署验收

2026-11-14 北京时间约 19:13 完成定向发布。修复来自 c0ad0c99520c3746b1324362b473ccf75f87bb33。通用 Actions run 43576980014 在生产版本锁检查处停止，随后使用生产实际镜像叠加经三方合并的修复进行部署，保留生产独立发布的网格、HRS 与其他既有修复。

## 运行证据

- ai-tuner、trading_system-btc_eth、trading_system-btc_eth_aggressive、trading_system-hrs、trading_system-new_coin、trading_system-grid 全部 healthy。
- AI Tuner 39 个文件和每个消费者 4 个文件，合计 59 个文件哈希与部署包一致。
- 生产源代码与审查导出时的哈希在构建前逐文件核对。叠加后的本地回归 1,244 passed、9 skipped。跳过项为需独立数据库的测试，本次部署没有将跳过项宣称为通过。
- 五个消费者已生成 tuning-loaded.json，配置哈希与部署前一致。覆盖层共享目录使用各消费者实际读取的配置快照。原服务器 strategies 目录中部分旧活动版本与消费者不同，未用这些旧建议替换当前消费者配置。
- 已执行成交补录，inserted=0、unresolved=0。随后执行 PnL 校准，最近七天 income_count=16，updated=0，成功完成。前一轮已补齐 ONDOUSDT 订单 12546107884 的交易所确认成交归属。
- 启动日志确认校准器按 30 分钟周期注册。检查新 AI Tuner 容器启动日志未发现 error 或 traceback。

## 发布与恢复位置

服务器发布目录为 /root/trading_system/.releases/ai-tuner-full-review/c0ad0c99-retry3。before.json、pin-before.json、images.json、state.json 保存发布证据。before.json 含容器运行配置，仅限服务器本地受限权限访问。部署工具和叠加源在 /root/trading_system/.deploy_tmp/tuner-review-layer。

原容器以原名加 -pre-tuner-review 保留并停止。新容器镜像使用 local/ai-tuner-review-前容器名:c0ad0c99。生产 ledger-risk-active.json 已指向新的发布记录与镜像覆盖文件。镜像保留生产原 VERSION，修复身份由容器标签 ai-tuner.review.commit、部署文件哈希及 state.json 共同证明。

本次生成了写接口令牌并仅保存于受限运行配置。后续自动化客户端需要从受限配置获取令牌。

## 仍需跟踪

HRS 和激进版已有的活动指针引用缺失覆盖文件，实际使用基础配置，加载回执 version=null。两者配置哈希与部署前相同。此处保留真实状态，不标记调优已生效。

正式资格凭据、运行中自动重新加载以及市场状态覆盖门禁的既有待办仍适用。此次部署完成修复代码与启动加载回执接线，尚未完成正式资格考试。
