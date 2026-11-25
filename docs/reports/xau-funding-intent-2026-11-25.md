# XAU 资金费边界与入场分类验证（2026-11-25）

状态：已合并并完成受限部署。PR #56，发布 commit `03774607ef47cb5e25f9afd5b4b49f2970bd8ee3`，DEPLOY_ID `00000101`。北京时间 2026-11-25 17:25:58 验证完成；冻结参数与生产交易业务表未改动。

## 交付

1. 资金费采集对接收时已过期的 XAU 预告做有界公开 GET 刷新，含 source < target、source == target、source > target 三种边界。正确保留请求/接收时间、哈希和常规币种成功批次。失败时不保存无效的可选 XAU。
2. 原版、激进版、校准版候选事件新增实际入场意图；所有订单/仓位/退出链路保留标签。历史不明确的标签为 unknown。
3. JSON 与 Markdown 报告按版本及趋势/价值回归/未知分组，输出信号、拒绝、订单、成交、截止挂单、成交率、净盈亏和费用。原资金、风控、匹配、准入逻辑不变，回放版本升级为 replay-v1.2.0。

## 验证

- Python 3.11 相关组合：417 passed，1 skipped（未配置专用可销毁本地 PostgreSQL 的 AI_TUNER_FUNDING_TEST_DSN / AI_TUNER_FUNDING_TEST_DB_ACK）。新增三个测试文件独立复核：88 passed。
- TDD 已确认失败后修复；覆盖实际策略分支、完整 engine.run、限价待成交/成交/拒绝、分批/最终平仓、坏标签、输入不变和指标守恒。
- 归档真实资金费核对：11/18 首笔匹配成功，但仍为 20/21 有效槽，1 个无效/缺失槽，verified=false。未放松验收或改写旧证据。
- 本地完整历史回放使用 2026-10-28 09:00 至 2026-11-18 01:00 UTC 的冻结输入（29760 根 1m）。11/18 原快照缺 exchangeInfo，从 11/10 快照找到 SHA256 与 manifest 完全相同的文件 `1383221d02ddb80970a0c6585e5250ad1060b23d4cc79430e95e4a10f84d908b`，仅加入独立验证副本；原归档未改写。
- 三版本全部既有经济指标/准入检查与旧报告相等；决策统计仅增加分类计数。原版 0 信号/0 已结/0 盈亏；激进版 1 信号/0 已结/0 盈亏；校准版 17 信号/4 已结/-0.8864995105691 USDT。旧事件均 unknown。此为旧窗口兼容验证，非截至 11/25 的最新业绩。
- 两名独立审查员分别检查规格与代码质量，最终无阻断项。修正双版本 Markdown 分组标题归属问题。
- 代码同步脚本默认硬编码到有未提交删除的主工作区，首次报缺失；仅在内存中将 PROJECT_ROOT 替换为本 worktree 后重跑：0 错误，1 个历史版本号警告（v6162 对 v2.2.0）。未改动原工作区或脚本。

本地证据：本分支 worktree 下 `data/research/xau-validation-20261125/`（不入 Git），含 snapshot、dual/calibrated 报告、comparison.json。

## 上线边界与后续验收

已通过新的专用 workflow 定向构建并发布资金费采集器及两个 XAU 影子容器；没有调用会额外更新 ai-tuner/定时器的旧 xau-review 发布模式。现役镜像、环境及冻结配置已固定，启动首轮采集延后至正常 cron，避免同小时跨 run 冲突。

上线验收必须再读取真实后续采集/决策证据，确认新字段、健康、版本与生产表零写入。新产生的有效资金费窗口才能证明线上修复；本地测试不等于上线通过。

原版/激进版 12/15 08:00 UTC、校准版 12/23 09:00 UTC 仍为评估节点；校准版历史四小时决策缺口及缺少事前资金费证据的窗口不因此消失。300 USDT pilot 仍需准入合格和用户明确确认。

## 追加：归因持久化与受限发布验收

- 相关组合最新 453 passed、2 skipped（两个依赖独立 PostgreSQL 的测试已在临时本地实例另行通过：2 passed）。真实 PostgreSQL 16 完成 12 条、三 profile、四类标签的写入/跨进程查询/数据库停启/导出验证。
- 新增不可变逐笔回放账本，16 项新测试；完整回放测试 228 passed。真实冻结窗口重新回放后，所有既有报告字段仅 replay_run_id 改变；原经济指标与准入结论逐字段一致。新 ID 将实际输入绑定进哈希。
- 原版/激进版账本 `0ad6fea5d07e271a`，1 条；校准版 `6f34d6fd66b545fe`，17 条（含拒绝/未成交及退出腿，不等于已结仓位数）。
- 发布前发现服务器权限清单缺少已部署 `trading.account_mode_equity` 登记。使用独立账户迁移分支已提交的 `89160896bf9cef194adefe74ed2383a47bc77aa6` 权威清单，在统一部署锁内先审计临时副本，再替换服务器 canonical 文件并再次审计：1343 对象、0 issues。新 SHA-256 `e9066387dc6080a667f80b01176e665e33a46abe650c9e1f01a170b6550d0809`。未执行 apply、GRANT 或数据库写入，旧清单保留在服务器 `.deploy_tmp/permissions-before-xau-20261125.json`。
- 新专用 workflow 只构建两种基于现役 digest 的薄镜像，激活资金费采集与两个 XAU shadow。合并提交使用 `[skip ci]` 防止普通全量部署；随后手动触发专用 workflow。部署前再次权限审计，校验所有文件、镜像、版本、环境/挂载/资源以及无关容器不变；失败回退三个目标，定时器保持不变。

- 服务器持久归档：`/root/trading_system/data/xau_replay_archives/xau-validation-20261125`，25 个证据文件均重新计算 SHA-256 验证并设只读。回放只在本地执行，服务器仅接收结果及冻结输入。
- 现役 Compose 与根环境文件因账户迁移出现环境变量漂移：发布冻结各目标实际 Env 的完整映射（移除根文件后来新增的键），保留原账户连接/配置；其余运行参数严格比对。镜像 VERSION 同时写 GIT_COMMIT/GIT_SHA，避免新代码误归入旧 run；切换默认等待 cron，既有数据不删改。

- 17:18 CST 上线前重新查询发现：校准版历史小时缺口为 6（原 11/02 04–07 UTC 四小时，加 11/23 11:00、15:00 UTC 两小时），跨 run 重复 6 小时；原版/激进版各缺口 0、历史重复 12 小时。此为部署前事实，旧窗口至 11/18 的“四小时”报告仍正确，但不能再当作当前全窗口基线。新发现缺口不回填、不放松准入，仍为 insufficient_data。四张生产交易表均 0，各周期行情 100/100，注册有效至 12/30 16:00 UTC。

## 正式发布结果

- PR：https://github.com/Bearflower/Binance_quantitative_trading_yi/pull/56 。Actions：https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/45584791299 ，结论 success，CI 发布回归 118 passed。
- 三容器 running/healthy、RestartCount=0，VERSION 的 GIT_COMMIT/GIT_SHA 均为 `03774607ef47cb5e25f9afd5b4b49f2970bd8ee3`。两 shadow 镜像 digest `ff80ae9d956017c52ebcf04199bdaef288e99c835c80e4963e6c35aae6362ead`；funding digest `8d315174782e0de9390153ab25240353064b842dbff71f1d799185aed9696aa9`。
- 激活记录位于服务器 `.releases/xau-attribution/03774607ef47cb5e25f9afd5b4b49f2970bd8ee3`：result.json=verified，audit 1343/0 issues，文件哈希/运行配置/启动日志全部验证。77 个非目标容器的身份、镜像、运行/健康/重启状态保持一致。
- 新 active run：原版/激进版 `xau-shadow-20261125092503-0be2071a10cedc86`；校准版 `xau-calibrated-shadow-20261125092524-b98c115f3a43de86`。三个 config_hash 与发布前完全相等，旧 run 已关闭，观察期不重置。
- 17:26 CST 核验：最新已到决策为 09:00 UTC，三版数据质量均正常；校准 ATR history_count=86，final_threshold=0.3912430811527793。四张 trading 生产表仍 0，各周期100根，注册覆盖12/30。历史重复/缺口未增加。
- 当前已完成部署与运行态验收；新 shadow run 的首次正常周期为 18:15/18:20 CST。发布时尚未到期，不将旧 run 的09:00事件冒称为新代码写入成功。趋势/价值回归实际候选标签须在新候选出现后按数据库证据核对；本轮持久化路径已通过真实 PostgreSQL 集成与停启测试。结算边界修复仍需后续真实边界证据，不能用普通槽位或单元测试替代。
