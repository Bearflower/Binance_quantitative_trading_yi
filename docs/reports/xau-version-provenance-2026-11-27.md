# XAU 版本追踪修复与复查（2026-11-27）

状态：PR #60 版本修复与 PR #61 精确 ID 执行器已合并；第二次 Actions 46014565497 已成功部署，五层与数据库 run 验证通过。三个 profile 新版本自然周期事件尚待北京时间 11/27 23:15、23:20 验收，当前不宣称全部验收闭合。风险 R2：只改影子事件版本身份和 run 分段，不改信号、参数或交易行为。沿用用户已授权的连续修复、测试后部署范围。

## 生产事实与根因

北京时间 21:36 只读检查：三个 profile 自 11/26 02:00 UTC 起各有 36 条事件，全部 no_signal/no_candidate，质量正常。资金费 11/26 01:45 UTC 至 11/27 13:30 UTC 共 430 个应到槽位无缺失、无失败；最新 13:35 UTC 槽成功。四张 production 表中本实例/profile 仍为零。

后续全量发布的 `c4a0bea5b9304779e4d64a526865541afce764a6` 镜像 VERSION 只有 GIT_SHA，没有 GIT_COMMIT。11/27 03:00 UTC 起每个 profile 的 11 条事件（共 33 条）git_commit 与 producer_git_commit 为空。active run 仍标记 `acee4c82dfefda216ee35b365533284d8ada44ee`，与现役镜像不符。

真实代码路径 `_read_git_info → run_forever → _prepare_shadow_run / ShadowCollector` 中，读取器没有 SHA 回退，run 复用只比较 commit 与配置；两个空 commit 导致版本变化未分段。归因状态本身持续落在 `research.shadow_decision_events.feature_snapshot`，并未丢失。

## 修复与验收契约

- 只有 GIT_SHA 时用其补齐 git_commit；保留原有显式 commit 和环境优先语义。
- 只有已知版本、commit、SHA、配置哈希都匹配，才复用 active run；缺版本或版本变化关闭旧 run、建立新 run。历史事件不更新、不删除，也不推断缺失版本。
- 现役底图固定为 `ghcr.io/bearflower/trading-btc-eth@sha256:80a5d2fc78f768f659f9ba5855013704ea3628f691d2afe9c48ed6550488962c`；仅叠加 collector.py、main.py、VERSION。只更新两个影子容器，资金费和其他容器纳入排除保护。
- 后续全量发布已去掉 calibrated 的 main.py 绑定。重试执行器按现役完整 inspect 克隆 Config/HostConfig/挂载/网络，逐项比对，按精确容器 ID 切换；不再使用旧 Compose 声明解析或 service 标签枚举。
- 权限清单、审计工具、规则、说明从已提交生产源 c4a0bea5 原样移入当前 main 基线（本地原工作区未提交文件不变）。CI 随本次 SHA 打包并记录两个审计文件哈希；激活前验证并执行包内 audit，不执行 apply、不覆盖服务器根目录契约。此轮无 DDL/GRANT。

## 验证证据

- 新增版本用例先在旧实现得到 5 failed / 4 passed，修复后全绿；最终 13 项覆盖 SHA-only env/file、SHA 独立变化、未知身份、配置独立变化，以及三个 profile 的真实入口→collector→普通/候选事件两条路径。
- 发布范围与旧挂载守护先失败再修复；审计包校验另有失败复现及篡改拒绝用例。
- 完整相关组合：544 passed、1 skipped；其中真实 Unix socket PostgreSQL 权限契约测试 18 项通过。跳过的资金费 PostgreSQL 测试与 SHA-only 归因持久化测试另行运行：2 passed。归因测试写入 15 条三 profile 候选/无信号事件，写入进程退出后独立读库并导出，事件、producer 与 run 的 SHA 一致。
- 初次移入来源测试文件包含 5 项依赖尚未并入 main 的 classic 账户业务集成，因缺对应迁移/业务版本失败。本次仅移入该文件中 18 项权限工具/契约测试；不修改或降低 classic 业务测试，来源分支仍保留。没有把这 5 项称为通过。
- 生产候选契约只读预审：1,355 对象、0 issues。两目标现役镜像、Env、挂载与资源约束预检通过。语法编译和 git diff --check 通过。
- 独立上下文审查已关闭核心逻辑与测试盲点；发布审查提出“审计资源须绑定本次发布”的问题已修复并复审放行，复核者独立运行版本/发布/真实PG权限 49 项通过。当前平台不具备项目流程指定的第三方模型，不宣称跨模型独立复核。

## 上线验收与不变边界

发布后须确认实际 digest / VERSION / 文件哈希、运行 healthy、新 active run 的 commit/SHA、自然 cron 新事件的 producer_git_commit，并核对无关容器、冻结配置、生产表零写入和小时缺口。没有真实候选则只能证明无信号状态归因正常，不能宣称已有新开仓归因样本。

原版/激进版历史缺口 0、重复小时各 12；校准版历史缺口 6、重复小时 6，不回填也不重置观察期。注册覆盖至 2026-12-30 16:00 UTC。窗口仍为 12/15 与 12/23；试跑准入仍按既定门槛，历史缺口仍影响充分性判断。

知识复用：采用 `container-json-logs-lost-on-recreate`（持久证据不能仅靠日志）、`shadow-compose-bind-mount-masks-image-config`（核对实际挂载）、`xau-replay-report-caliber-position-aggregation`（不改变历史评估口径）。本次只做局部元数据修复，不需要重做策略设计或服务器回测。

## 首次发布失败与执行器修复

Actions `46010982575` 在第一目标运行配置校验失败，回退旧镜像后 healthy；校准版未切换。Compose 按 service 标签处理了同标签的两个已停止备份，将其删除：`mtcps-xau-shadow-before-full-acee4c82`（旧 ID `07c852d34159276d6c24932076eb23af6ee4e9c94f450894c96cadf8df04af37`）和 `...-c4a0bea5`（旧 ID `c2e8e03c6ba906029c46ab04bc44823a5dc8442a663da2e336a2f3ee5dad97b1`）。不能称首次发布未影响非目标容器。

依据原 full-regression 发布目录的完整 inspect 和仍在本地的镜像，恢复了同名、未启动、restart=no 的配置替代容器；新 ID 分别为 `36c870f0be3f1a5bb983756e81b22d7e367c5ee881caaea4df91b5c2e6988829`、`3f49d0236b1c3455aee66bf8239fed29ecd4298ced21af15e3bd1d1bd7d4a85f`。运行配置与挂载逐项通过。原容器 ID、可写层和容器日志不能恢复；这只是配置恢复，不是原容器完整恢复。交易服务和资金费容器未变化，持久数据库未被本次发布改写。

执行器现以完整 ID 操作，旧容器停止且禁自动重启后保留，候选运行失败时恢复同一旧 ID。每步变更前原子落盘并 fsync，提供只接受本次发布目录的 `--recover`；不使用 Compose 管理生命周期。两目标均在停机前创建未启动候选并验证完整配置，再运行权限 audit。已在服务器对两现役目标分别完成未启动克隆探针，配置完全一致、清理后其他容器快照不变。

独立复核追加发现“第一目标在第二目标切换期间退出未复检”“回退旧配置漂移在启动后才发现”“清理后的恢复阶段不幂等”三项，均先加失败用例，再修复。重试前仍须回归、复审及新的不可变提交发布。实际发布成功和自然周期业务证据另行追加。

复审另复现成功结果已原子替换后 fsync/SSH 输出失败仍触发回退的矛盾状态。新增两项失败复现后，将结果提交置于操作回退区间之外；提交阶段异常保留已验证新容器和停机原容器，不能因输出异常自动回退。结果文件存在与实际版本需分别核对，CI 失败不等于切换失败。

重试执行器最终相关回归：549 passed、1 skipped（需显式本地 PG 配置的资金费集成，本轮前置已单独通过），其中发布守护 39 项、版本真实路径 13 项。git diff --check 通过。

C 最终复审放行：独立上下文复测 52 passed、diff 检查通过，确认前述四类执行器缺陷关闭，可仅部署两影子容器；尚需上线与自然周期验收。

## 第二次部署与已完成验收

- PR #61 合并 SHA `d402ad0c06a5c501277d1f03d6c8b172768ac2b3`；[Actions 46014565497](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/46014565497) success，DEPLOY_ID `00000401`。
- 两目标镜像 ID `sha256:be6d38aebe8b6008742bb5c559b9dd777fc7af9ade79daeedfe9bacb36820237`；原版/激进版容器 `300a004d72db` 于 14:19:42 UTC 启动，校准版 `3e8ba0e2b118` 于 14:20:49 UTC 启动。均 running/healthy、RestartCount=0，VERSION 两个 SHA 字段和三个发布文件哈希均匹配；启动后 ERROR/CRITICAL/FATAL/Traceback 为 0。
- 两目标完整运行配置均匹配克隆前状态（仅审定镜像/版本标签变化），执行模式 shadow_batch，跳过首次立即采集。原容器 b0947495c862/d96a6453836d 按原 ID 停止保留、restart=no。本次重试的 158 个排除容器全部未变化，包含资金费/实盘服务及前述两配置替代备份；此结论仅针对第二次发布。
- 权限包 audit：1,355 对象，0 issues；无 apply、DDL 或 GRANT。
- 数据库已持久化两个新 active run：`xau-shadow-20261127141943-b38cdde224914128`、`xau-calibrated-shadow-20261127142051-e9663e8424bec4da`，其 git_commit/git_sha 均为 d402ad0c。三个冻结 config_hashes 与发布前完全相同。
- 截至 14:21 UTC，最新自然事件仍为切换前 14:00 UTC 周期，三 profile 均 no_signal/no_candidate、质量为空。四张 production 表为 0；1h/4h/1d 各 100 根；原版/激进版缺口 0、重复小时各 12，校准版缺口 6、重复小时 6，未新增。资金费 439 个成熟应到槽位无缺失、无失败，注册到期日未变。
- 03:00–14:00 UTC 的空版本事件累计为三 profile 各 12 条，共 36 条，全部保留。第一次短暂新 run 的 0 事件分段也如实保留；不删除“难看”的分段或补写历史。

### 仍待完成的业务周期验收

新 run 当前各 0 事件。原版/激进版下次自然采集为 2026-11-27 15:15 UTC（北京时间23:15），校准版15:20 UTC（23:20），对应 scheduled_at=15:00 UTC。届时须确认三条事件的 git_commit 和 entry_attribution.producer_git_commit=d402ad0c，归因状态与信号一致、质量为空，并复核无新增缺口/跨 run 重复。不得人工触发采集制造证据。本轮真实 PostgreSQL 测试已覆盖趋势/价值回归候选持久化及进程退出后读取，但尚无新版本生产候选，不能把测试样本称为真实开仓样本。

本地只读证据位于 ignored 的 `data/research/xau-version-provenance-20261127/`；服务器私有发布状态在 `.releases/xau-attribution/d402ad0c06a5c501277d1f03d6c8b172768ac2b3/`，包含完整 inspect，不应公开 Env。

证据文件 SHA256：
- `release-acceptance.json`：`ff4acf1e5c29c06799ed524148f2c291dc0f3676009e7fa27e477d9e592ed25e`
- `runtime-after.json`：`6070bbbee7f97f7164caf6238ce4c0dffbeae5f753f06139f05510265b7c25f8`
- `post-release-safety.json`：`c718b4319f10af35c3ea00d7fa1d8ee6ade7c176f0b4b20f02cc2e647367d023`
- `backup-recovery.json`：`df9149b53644203bf990f64f7b73b3c0bdfd33c9a2ce8fc3669dc7e18248fc43`

补充复核：生产表查询移除 symbol 限制后，同一实例/profile 在全部交易对范围仍为四表 0；完整后置快照 `runtime-after-final.json` SHA256 `a0c011439717c34f48551c79e6ad10b051103fc2fded3bccc63c803bb582b982`。
