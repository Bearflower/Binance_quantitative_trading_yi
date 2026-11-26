# XAU 资金费恢复与归因状态修复（2026-11-26）

状态：实现、本地验证和独立规格/质量复核通过，准备受限发布。工作分支 `codex/xau-runtime-repair-20261126`。

## 事实与根因

- 三个目标运行的仍是已发布 `03774607`，归因代码没有遗漏上线。新代码已连续写入三个 profile 各16个小时的 no_signal 事件，数据质量正常，四张 production trading 表零写入。没有新候选，因此旧设计不写 entry_intent，空值不能解释为归因未部署。
- 11/25 13:00 UTC 有失败批次：INVALID_NEXT_FUNDING_TIME，接收时间13:00:01.178359；整批0条观测。旧代码只为XAU自身过期刷新，必采币种过期立即终止整批。历史失败载荷未保存触发币种，不能断言具体币种；本轮补充symbol证据。
- 13:25 UTC 没有批次。Docker journal记录 funding 容器21:23:34 CST停止，21:24:09等待35秒未退出被强制终止；同一容器ID于21:26:44恢复。两个shadow也在21:26:55恢复，代码/镜像未改变，RestartCount仍0。这是停启而非新镜像部署；不得仅凭RestartCount=0推断从未停机。原daemon恢复时直接等下一槽，错过仍在120秒采集窗口内的当前槽。
- 原版/激进版小时缺口0，历史重复各12小时；校准版历史缺口6、重复6，均未新增。注册仍覆盖12/30。

## 改动边界

1. 必采资金费批次遇过期预告，统一使用最多3次、每次最多10秒的完整公开GET刷新。必采币种必须全部验证，保持当前槽120秒上限；不猜未来结算时间、不放宽准入、不回填历史。失败记录保留初始时序并加入symbol。
2. daemon启动时检查当前槽是否已经有持久化批次；没有且仍在120秒内才用当下真实请求采集一次。已有批次/过期跳过，数据库失败则关闭启动，常规五分钟调度保持。
3. 每条新影子事件的feature_snapshot新增entry_attribution，schema_version=1，含producer_git_commit和明确status：recorded/unknown/no_candidate/data_unavailable/analysis_error。只有真实candidate可有趋势或价值回归意图；无信号不伪造订单，历史事件不修改。
4. 专用发布只更新两个XAU影子容器和funding采集器，基于现役digest构建薄层；CLI新增文件哈希校验。环境、挂载、冻结参数、无关容器与审计/回退门禁保持。

## 验证

- 归因18项新增场景先失败后修复；shadow/replay相关338passed。
- 真实本地PostgreSQL：三profile共15条candidate/no_signal事件，写入进程退出后独立读取与导出，归因状态、数值和来源保持；没有连接生产数据库进行测试写入。
- 两个资金费改动均经失败复现后修复，startup真实daemon→loop→collector验证+104秒启动，当前窗口耗尽、已存在槽与数据库错误均有覆盖。

- 完整相关组合517 passed、1 skipped；跳过的funding PostgreSQL集成另行1 passed，归因PostgreSQL集成1 passed。两名独立复核者分别运行161项针对性测试，无阻断项。
- 发布前权限审计1347对象、0 issues；canonical SHA256 `9cd14a74769c508295d5568fb0228a57d5de7198be46bcbf10b46a654b194ae4`。仅audit，无权限或数据库结构变更。

## 上线结果

待记录。历史两个资金费缺口与校准版六小时缺口不会由本轮修复消失，准入继续按真实证据判断。
