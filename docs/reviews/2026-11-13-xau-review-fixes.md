# XAU 审查修复与验证

修复分支 fix/xau-review-20261113，基于 origin/main 4370b4aa。工作区 /tmp/xau-fix。带入原工作区既有 XAU 合约字段兼容、成交率和拒绝记录修复，其他策略修改保留在原工作区。

## 已实现

- 回放事件按 max(scheduled_at, evaluated_at) 推进及排序，截止后才可用的信号不入场。仅有 scheduled_at 的历史事件继续兼容，无法证明其实际可用时间。旧导出需确认是否包含 evaluated_at。
- 快照校验和 CLI 都从当前快照目录解析 manifest 文件名，支持迁移副本，校验实际消费的文件。重新核算分钟间隔、数量、边界和时间对齐，manifest 声明的缺口也保守保留。跳过校验时不授予数据完整状态。
- MIN_NOTIONAL 同时兼容 notional/minNotional。候选风控拒绝独立计数，从订单成交率分母排除。
- 引擎版本提升为 replay-v1.1.0，避免旧结果与新结果共用回放标识。
- repository 按 PostgreSQL INSERT 0 1 判断实际新增，冲突跳过返回 false。
- 巡检查询全部 run，检查过去24小时数据质量和 run 切换。校准观察期统计终点截断为结束前一小时。
- ATR 要求正有限阈值与正历史数量，行情同时输出最新开盘时间，超过四天未更新报警。四天是容纳周末休市的粗粒度阈值，精确交易日历检查仍需交易时段数据。
- 部署守护测试适配当前模式输出、矩阵和 compose profile，继续运行构建判断及危险命令变异反例。
- 新增宿主机只读证据采集脚本 scripts/capture_xau_runtime.py。ai-tuner 读取证据，检查 running/healthy、RestartCount、StartedAt、镜像和容器 ID，比较上次身份。证据缺失或超过25小时明确报警。

## 发布时的接入步骤

发布 ai-tuner 与影子采集器变更后，在 Docker 宿主机每日北京时间07:40运行以下命令，使07:45巡检能读取证据。输出路径使用现有 ai-tuner 日志 bind mount，部署前核对实际宿主路径。

    python3 scripts/capture_xau_runtime.py --output /root/trading_system/logs/ai_tuner/xau_runtime.json

默认容器读取 /app/logs/xau_runtime.json，可用 XAU_RUNTIME_EVIDENCE_FILE 覆盖。巡检在同目录写入上次成功读取的身份基线。这部分仅修改本地证据文件。用户已授权部署，受控发布脚本会安装 xau-runtime-evidence.timer，每日北京时间07:40生成证据，并在部署前后立即采集。发布路径限定更新三个目标容器，使用不可变镜像摘要，核对有效配置只改变镜像，保存原镜像用于失败回退，并检查其他容器身份。

## 验证

完整相关集包含回放器、三版影子策略、校准配置、部署守护及 ai-tuner XAU 巡检。最终结果为335 passed，git diff --check通过。新增失败复现已确认信号提前使用、截止后入场、复制快照误验、隐藏分钟缺口以及重复写入计数缺陷。

## 正式验收仍需的证据

校准版历史资金费结算日程尚不可独立核验，因此保留 insufficient_data。当前已有 prospective funding_schedule 采集模块，但它不能证明采集开始之前的历史日程。不能通过删除保护实现正式准入。观察期内既有事件缺口也需按原标准处理。

生产数据和既有盈亏报告尚未重新导出、回放。下一次评估应使用冻结的相同事件和分钟快照重跑三版，确认成交笔数、成交率、净盈亏和回撤差异。

## 上线核验（2026-11-14）

- 主程序镜像提交 093053afbf1fb9c9d34be76c92f1db27959c528d，发布号0000BD01，Actions运行43406178633成功。首次运行43405694660在容器激活之前被旧ledger打包条件阻断，修正条件并增加回归测试后重试成功。
- ai-tuner及两个影子容器均running/healthy、RestartCount=0。部署脚本核对其他运行容器ID，均保持一致。
- 2026-11-14北京时间07:40宿主机证据采集成功，07:45 ai-tuner巡检执行成功，sent=True。报告中的过去24小时run切换对应本次授权部署。
- 2026-11-14 08:40 UTC只读核验，三版最新决策为08:00 UTC，data_quality_reason为空；1h/4h/1d均100根。校准ATR history_count=86，final_threshold=0.31791056697999387。生产交易四表计数均0。
- active run=2、closed run=32，校准跨run重复小时6、缺口4，均为原有基线。行情注册到期2026-12-30 16:00 UTC。
- 宿主机脚本增加从容器/app/VERSION读取完整GIT_SHA，兼容镜像无版本标签。容器已退出导致版本读取失败时仍记录异常状态。该脚本独立更新到宿主机，不需重建已验证的应用镜像。
- 收尾测试338 passed，git diff --check通过。历史资金费日程证据和冻结数据重新回放仍按上节说明处理。
