# 项目代码审查 — 2026-09-29

基线：`e4912619e513`，包含当前未提交文件。使用 `open-code-review-delegate`，OCR 仅用于确定文件范围和解析规则；结论由宿主代理结合源码、测试和本地模拟形成。

发现 **12 项高优先级问题、6 项中优先级问题**。主要风险集中于订单状态与真实仓位脱节、非幂等重试、跨策略互斥、AI 配置执行链和服务资源管理。建议先修复交易安全项与 SQL 注入，再验证调参与部署链路。

本次未修改业务代码、未提交或部署，未向交易所发送真实订单。动态复现使用假的 HTTP/数据库/交易所对象，不证明线上已经发生这些故障。

## 范围与覆盖

- 建立全仓清单 389 个文件：318 个 Python、20 个 YAML、9 个 shell、4 个自有 JavaScript、38 个其他配置/SQL/HTML/CSS/构建文件。
- OCR 全仓快照选中 301 个：`total_files=301`，`reviewed_files=291`，`skipped_files=10`，`coverage_rate=96.68%`。
- 其中 64 个文件做了关键调用链定向深审；其余主要为规则级静态/结构筛查。**覆盖率不表示逐行人工深审，更不表示测试覆盖率或代码无缺陷。**
- 跳过项为 docs 下生成的架构图/历史 HTML 设计稿，逐项原因列于 coverage.json。OCR 默认排除的 88 项另列，测试代码已纳入补充静态检查和分组执行。
- 初始工作区预览没有业务改动可审；为匹配全项目请求，在临时目录创建源码快照与空基线，仅操作临时 Git 元数据，原项目 Git 历史和索引未改变。
- 第三方 ECharts 压缩包、数据文件、日志、秘密配置和历史 Markdown 文档不是本轮源码审查目标。未访问线上数据库或复核线上防火墙、实际挂载、运行镜像。

## 高优先级与中优先级发现

### R01 [P1] K 线查询允许外部输入改变 SQL 结构

位置：[services/kline_service/api/routes.py:88](/Users/yl/vscode/Binance_quantitative_trading/services/kline_service/api/routes.py:88)。类别：`security`；级别：`high`。

公开查询参数 symbol、interval 未按交易对和周期白名单验证，就被拼入 FROM 表名。_table_exists 返回 false 后，自动建表即使失败也继续执行查询；因此不存在表检查不能阻断注入。同类逻辑也出现在 indicators 查询。Compose 将 8765 映射到所有接口，应用没有认证；实际公网可达性仍取决于服务器防火墙。

证据：本地提取真实路由函数，使用假的数据库连接；传入带 CROSS JOIN 的 interval 后，恶意 SQL 片段原样到达 fetch_all。未访问生产数据库，未执行注入 SQL。

建议：在路由和注册入口统一验证 symbol 与 interval，严格白名单化 SQL 标识符；自动建表失败后停止查询；限制接口访问。

### R02 [P1] 非幂等下单被通用重试器重复提交

位置：[shared/binance_api.py:195](/Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py:195)。类别：`bug`；级别：`high`。

_request 对所有方法统一重试，包括开仓 POST。若交易所已受理而响应超时，下一次请求会创建新的订单；place_order 没有生成稳定的客户订单标识，也没有先查询前一请求结果。默认 max_retries=3 表示最多四次提交。

证据：模拟第一次请求已受理但响应 TimeoutError，第二次成功：交易所侧接受订单 [1,2]，调用方只获得订单 2。

建议：将读请求与非幂等写请求的重试策略分开；为每次交易意图生成稳定标识，遇到未知执行结果先核对订单/成交/持仓，再决定是否重发。

关联：[shared/utils.py:63](/Users/yl/vscode/Binance_quantitative_trading/shared/utils.py:63)；[shared/binance_api.py:528](/Users/yl/vscode/Binance_quantitative_trading/shared/binance_api.py:528)。

### R03 [P1] 新币平仓重试原数量，部分成交后可能反向开多

位置：[strategies/new_coin/executor.py:2582](/Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py:2582)。类别：`bug`；级别：`high`。

close_quantity 只在重试循环外按原持仓计算。首笔 BUY 部分成交并撤单后，下一笔仍买入原数量，且未设置 reduceOnly；市价兜底也沿用同一数量。止盈单同时成交时也有相同过量平仓风险。

证据：真实 _close_position 方法配合本地模拟：初始 position=-1，第一笔 BUY 1 成交 0.4 后撤单，第二笔继续 BUY 1，最终 position=+0.4，函数返回 True。

建议：每次撤单后确认最终成交量、重新读取剩余持仓，所有平仓请求设置减仓约束，兜底也只能提交剩余量。

### R04 [P1] HRS 平仓失败或未成交仍撤保护单并删除持仓

位置：[strategies/hrs/strategy.py:1938](/Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py:1938)。类别：`bug`；级别：`high`。

时间止损和移动止盈分支没有验证 close_position 的结果，就回写盈亏、撤销保护单并 remove_position。执行器会在异常时返回 None；正常限价单仅收到 NEW 回执也被当成成功返回。真实仓位可能仍在，但本地管理和交易所止损一起被清除。

证据：模拟 close_position 返回 None：_monitor_positions 仍调用 cancel_all_orders 一次、remove_position 一次。已核对真实 _writeback_pnl 会容错返回，不会阻止后续清理。

建议：把平仓受理与完全成交分开处理；只有成交/持仓对账确认归零后才删除状态，失败或部分成交时保留/重建剩余仓位保护。

关联：[strategies/hrs/executor.py:913](/Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/executor.py:913)；[strategies/hrs/strategy.py:1983](/Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/strategy.py:1983)。

### R05 [P1] MTPCS 保护单失败后丢失已成交开仓的管理状态

位置：[strategies/btc_eth/strategy.py:2656](/Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py:2656)。类别：`bug`；级别：`high`。

入场已确认成交后，只要 STOP、TP1、TP2 任一创建失败，就直接返回 False；self.positions 直到全部成功才写入。STOP 失败会留下无止损仓位，TP 失败也会留下不受本地完整管理的仓位。激进版存在相同流程。

证据：模拟入场 FILLED、保护单失败：方法返回 False，positions 仍为空。后续补挂依赖持仓归属和数据库订单状态，不能替代开仓当下的状态与保护。

建议：成交后先登记真实持仓，把保护完整性作为独立状态；保护失败进入可重试补挂或减仓兜底流程，不能将已成交交易视为未发生。

关联：[strategies/btc_eth_aggressive/strategy.py:2665](/Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth_aggressive/strategy.py:2665)。

### R06 [P1] 入场超时撤单忽略部分成交和撤单竞态

位置：[strategies/btc_eth/strategy.py:2854](/Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py:2854)。类别：`bug`；级别：`high`。

_wait_for_order_fill 只把 FILLED 视为成交，对 CANCELED/EXPIRED 即使 executedQty>0 也返回 None；超时路径撤单后不读取最终成交量就退出。新币和 MTPCS 激进版存在同类处理，已成交的部分仓位不会进入正常保护流程。

证据：模拟订单 CANCELED、executedQty=0.4、origQty=1：真实入场方法返回 None，撤单回执中的成交量也未处理。

建议：撤单后核对订单最终状态与累计成交量；部分成交必须建仓保护或明确减仓清零，并处理查询与撤单之间完成成交的竞态。

关联：[strategies/btc_eth/strategy.py:5384](/Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py:5384)；[strategies/new_coin/executor.py:309](/Users/yl/vscode/Binance_quantitative_trading/strategies/new_coin/executor.py:309)；[strategies/btc_eth_aggressive/strategy.py:2863](/Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth_aggressive/strategy.py:2863)。

### R07 [P1] 归属 advisory lock 没有覆盖开仓预占，无法互斥

位置：[shared/database.py:308](/Users/yl/vscode/Binance_quantitative_trading/shared/database.py:308)。类别：`bug`；级别：`high`。

fetch_one_advisory_lock 在读取归属后就结束事务释放锁。调用方之后才设置杠杆、校验、下单和写交易记录；两个策略可以依次读到无归属并同时开同币种仓位。共享 PM 净仓位会合并或抵消，后续单一归属判断不能撤销这个结果。

证据：使用真实 DatabaseManager 方法和模拟事务锁并发执行两个策略：A、B 的 blocked 均为 false，均获准进入后续开仓。

建议：在锁内原子创建持久化的交易对占用/交易意图记录，再释放锁进行外部请求；以唯一约束与超时恢复维护占用状态。

关联：[shared/position_ownership.py:109](/Users/yl/vscode/Binance_quantitative_trading/shared/position_ownership.py:109)；[strategies/btc_eth/strategy.py:2628](/Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/strategy.py:2628)。

### R08 [P1] 孤儿清理在缺少策略状态时误撤真实持仓的止损

位置：[ai_tuner/cleanup/orphan_cleanup.py:416](/Users/yl/vscode/Binance_quantitative_trading/ai_tuner/cleanup/orphan_cleanup.py:416)。类别：`bug`；级别：`high`。

当 strategy_states 没有对应记录时，无条件加入 stale_orders 并 continue，绕过了交易所是否还有持仓的保护判断。首次持久化失败、状态尚未写入或状态丢失时，即使本轮已确认交易所持仓存在，也会撤掉该仓位的保护单。

证据：模拟交易所明确持有 BTCUSDT、策略状态为空、数据库有 STOP_LOSS：execute 仍调用 _cancel_order 一次。

建议：所有清理分支先检查交易所实时持仓；缺状态或无法确认无仓时保留保护单并告警，不能仅据状态缺失判定孤儿。

### R09 [P1] AI 自动应用绕过规范化与边界截断，写入错误配置类型

位置：[ai_tuner/scheduler/weekly_job.py:366](/Users/yl/vscode/Binance_quantitative_trading/ai_tuner/scheduler/weekly_job.py:366)。类别：`bug`；级别：`high`。

validate_params 返回的 validated 是已提取 to 并截断范围的扁平数值，但这里只在 errors 非空时使用它。正常建议和仅有 warnings 的越界建议继续使用原始 {from,to}；自动应用将其写成字典覆盖标量，人工审批提取 to 后又可能应用未截断的原值。

证据：真实校验器把 scoring.min_score=999 校正为 95，errors=[]，周任务分支却仍选择原始 {from:70,to:999}。实际写覆盖层后，load_strategy_config 读取到的 min_score 是 dict。现有自动应用测试用标量 mock，未覆盖真实 parser 格式。

建议：始终使用 validated 作为可执行配置，原始建议只保留供审计；保存、审批、自动应用共享同一份已校验值，增加 parser→validator→writer 的贯通测试。

关联：[ai_tuner/adapters/base_adapter.py:319](/Users/yl/vscode/Binance_quantitative_trading/ai_tuner/adapters/base_adapter.py:319)；[ai_tuner/scheduler/weekly_job.py:427](/Users/yl/vscode/Binance_quantitative_trading/ai_tuner/scheduler/weekly_job.py:427)；[ai_tuner/main.py:779](/Users/yl/vscode/Binance_quantitative_trading/ai_tuner/main.py:779)。

### R10 [P1] 调参写入位置与运行策略配置脱节

位置：[docker-compose.yml:236](/Users/yl/vscode/Binance_quantitative_trading/docker-compose.yml:236)。类别：`bug`；级别：`high`。

ai-tuner 将覆盖层写入宿主机 ./strategies；btc_eth、激进版、new_coin、grid 的策略目录来自镜像，没有挂载该目录。HRS 仅挂载单个 config.yaml，也看不到 tuning_overrides。各 main.py 只在启动时调用 load_strategy_config，没有应用后重载/重启流程。因此文件写入成功与 mark_applied 不表示运行策略已经采用新参数，普通重启多数容器也无法读取宿主覆盖层。

证据：交叉核对 Compose volumes、各 Dockerfile、所有 load_strategy_config 调用点及审批/自动应用路径；未检查线上容器是否有仓库之外的额外挂载。

建议：让调参与策略共享受控的版本化配置目录，并实现重载或受控重启及版本确认；只有运行进程确认版本后才标记 applied。

关联：[strategies/btc_eth/main.py:55](/Users/yl/vscode/Binance_quantitative_trading/strategies/btc_eth/main.py:55)；[strategies/hrs/main.py:34](/Users/yl/vscode/Binance_quantitative_trading/strategies/hrs/main.py:34)；[ai_tuner/main.py:791](/Users/yl/vscode/Binance_quantitative_trading/ai_tuner/main.py:791)。

### R11 [P1] Dashboard 每次请求创建连接池且不关闭

位置：[dashboard/backend/api/routes_docker.py:49](/Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/api/routes_docker.py:49)。类别：`performance`；级别：`high`。

Depends(get_data_service) 为每个请求构造 DataService；缓存未命中时 _ensure_initialized 新建 asyncpg 池和 BinanceClient，但依赖无 yield/finally，生产 lifespan 也不持有并关闭这些实例。周期刷新、多个端点和多个用户持续创建连接池，可能耗尽共享 PostgreSQL 连接，连带影响策略。

证据：核对依赖工厂、DataService._ensure_initialized 和 main_docker.lifespan；每个实例独立 _initialized/_db_manager，无关闭路径。未进行生产压测。

建议：在 lifespan 建立并关闭应用级 DataService，依赖返回共享实例；同时关闭 asyncpg pool 和 HTTP session。

关联：[dashboard/backend/services/data_service_docker.py:122](/Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/services/data_service_docker.py:122)；[dashboard/backend/main_docker.py:166](/Users/yl/vscode/Binance_quantitative_trading/dashboard/backend/main_docker.py:166)。

### R12 [P1] CI 增量构建遗漏跨目录依赖，修复不会进入对应容器

位置：[.github/workflows/deploy.yml:148](/Users/yl/vscode/Binance_quantitative_trading/.github/workflows/deploy.yml:148)。类别：`bug`；级别：`high`。

仅按 matrix.service.dir 及 shared/、VERSION、compose 判断重建，未覆盖 Dockerfile 的实际 COPY 输入。data-backend COPY dashboard/backend/services、core、config，修改这些文件只会重建 dashboard-api，实际运行快照/熔断任务的 data-backend 被跳过；单改根 requirements.txt 也会跳过全部服务。

证据：对照 matrix 的 data-backend dir=services/data_backend 与其 Dockerfile 的 COPY 清单，路径判断可以确定进入 should_build=false 分支。

建议：按每个镜像的实际构建依赖建立路径集合，根依赖文件/构建忽略规则触发所有受影响镜像；增加构建决策用例。

关联：[services/data_backend/Dockerfile:22](/Users/yl/vscode/Binance_quantitative_trading/services/data_backend/Dockerfile:22)；[.github/workflows/deploy.yml:133](/Users/yl/vscode/Binance_quantitative_trading/.github/workflows/deploy.yml:133)。

### R13 [P2] 回滚只恢复基础 YAML，不会撤销生效覆盖层

位置：[ai_tuner/main.py:928](/Users/yl/vscode/Binance_quantitative_trading/ai_tuner/main.py:928)。类别：`bug`；级别：`medium`。

审批和自动应用修改 tuning_overrides/.active，而回滚接口仍只调用旧版 RollbackManager.rollback 恢复 config.yaml。当前覆盖层指针保持原值，load_strategy_config 仍将有问题的参数覆盖回来；没有历史基础备份时还会直接返回无备份。

证据：本地基础值 70、覆盖值 80，rollback 返回 True 后，重新 load_strategy_config 的有效值仍是 80。

建议：回滚应切换到已知有效的覆盖层版本（或明确禁用当前覆盖层），确认运行版本并同步记忆状态。

### R14 [P2] 新覆盖层只包含本次差量，静默撤销之前调参

位置：[ai_tuner/deploy/config_operator.py:205](/Users/yl/vscode/Binance_quantitative_trading/ai_tuner/deploy/config_operator.py:205)。类别：`bug`；级别：`medium`。

每次 apply_overrides 从空字典生成新版本，随后 .active 只指向这个版本；loader 只合并基础配置和当前单个版本，不会叠加历史版本。AI 只调整一个参数时，之前已调整但本轮未提到的参数会回到基础值。

证据：基础 scoring=70；第一次覆盖 scoring=80；第二次只修改 risk.max_loss 后，读取到 scoring 又变回 70。

建议：以当前有效覆盖层合并本次已校验差量，形成完整版本快照；显式区分恢复默认与未调整。

关联：[shared/config_loader.py:95](/Users/yl/vscode/Binance_quantitative_trading/shared/config_loader.py:95)。

### R15 [P2] 熔断指数缺失结果被缓存整个整点，补写后仍失效

位置：[shared/circuit_breaker.py:196](/Users/yl/vscode/Binance_quantitative_trading/shared/circuit_breaker.py:196)。类别：`bug`；级别：`medium`。

未找到指数时也把 None 放入 _index_cache；同一 index_hour 再次读取立即返回缓存。指数任务延迟或数据库记录晚到时，即使稍后补写了触发熔断的指数，该策略在这一整点内仍按缺失放行。这里的问题是无法恢复，而不是既定的短时 fail-open 策略本身。

证据：模拟数据库第一次无记录、第二次本可返回 5%：两次 load_index 都返回 None，数据库仅被查询一次。

建议：对缺失值不缓存或使用短 TTL 重试；成功值缓存应有有界清理，补写后能重新评估熔断。

### R16 [P2] 网格部分卖出未减少成本，后续平均价与盈亏失真

位置：[strategies/grid/position_manager.py:114](/Users/yl/vscode/Binance_quantitative_trading/strategies/grid/position_manager.py:114)。类别：`bug`；级别：`medium`。

部分 SELL 减少 quantity 却保留原 total_cost，下一次 BUY 用过大的 total_cost 重算 avg_price。影响启用自动网格持仓管理的场景；当前半自动信号模式未必触发该分支。

证据：BUY 2@100 → SELL 1@110 → BUY 1@100，正确剩余成本均价应为 100，实际算出 150。

建议：按被平数量减少原持仓成本，保持 total_cost=quantity×avg_price，并明确处理越过零仓位的合约方向。

### R17 [P2] K 线测试全局替换 shared.utils，污染其他测试

位置：[tests/test_kline_service/conftest.py:36](/Users/yl/vscode/Binance_quantitative_trading/tests/test_kline_service/conftest.py:36)。类别：`test`；级别：`medium`。

conftest 在导入期改写 sys.modules["shared.utils"] 为 MagicMock，未按 fixture 恢复。之后收集的模块会导入假的重试器和时间转换函数，真实错误被掩盖或产生大量与执行顺序相关的失败。

证据：test_utils.py 单独运行 17 passed；与 test_kline_service/test_binance_client.py 合并运行变成 13 failed、25 passed，其中重试与参数校验测试使用了 MagicMock。

建议：为服务共享模块使用独立包名，或将补丁限定于 fixture 生命周期并恢复模块状态；保留合并运行验证。

### R18 [P2] 新币回测图表引用不可见的 mdates，生成过程失败

位置：[backtest/new_coin/report_generator.py:389](/Users/yl/vscode/Binance_quantitative_trading/backtest/new_coin/report_generator.py:389)。类别：`bug`；级别：`medium`。

matplotlib.dates 只在 generate_charts 方法的局部作用域导入，另两个 _plot_* 方法直接使用 mdates，运行到日期格式化必然 NameError。外层捕获后停止后续图表生成，报告可缺失资金与回撤图。

证据：Pyflakes 在 389、424 行报告 undefined name；核对 import 位于另一个方法的局部作用域。

建议：在模块或实际使用的方法导入 matplotlib.dates，或显式传入依赖，并用非空曲线验证图表输出。

关联：[backtest/new_coin/report_generator.py:339](/Users/yl/vscode/Binance_quantitative_trading/backtest/new_coin/report_generator.py:339)；[backtest/new_coin/report_generator.py:424](/Users/yl/vscode/Binance_quantitative_trading/backtest/new_coin/report_generator.py:424)。

## 测试与验证

本机执行环境为 Python 3.9.6 / pytest 8.4.2，生产 Dockerfile 为 Python 3.11，依赖版本也未完全锁齐。以下为各次命令真实结果，部分集合有重叠，不能累加为独立测试总数。没有把环境错误直接判定为生产业务缺陷。

| 范围 | 结果 |
|---|---|
| tests（排除 integration 与 kline 目录） | 703 passed / 22 failed / 175 errors / 1 skipped / 1 xfailed |
| ai_tuner/tests + ai_tuner/allocation/tests | 258 passed |
| tests/test_kline_service | 91 passed / 4 failed |
| strategies/btc_eth/tests | 185 passed |
| strategies/hrs/tests | 281 passed / 11 failed / 15 errors |
| 激进版与新币 tests/test_stop_loss_mark.py | 10 passed / 6 failed |
| test_utils.py 单独执行 | 17 passed |
| test_utils.py 与 K 线 API 客户端测试合并 | 25 passed / 13 failed |
| 多策略 tests 使用 importlib 收集尝试 | 6 个收集错误（_SixMetaPathImporter 与本机环境不兼容） |

- 318 个 Python 文件 AST 解析完成；Pyflakes 输出 447 条，绝大部分为未使用导入、无插值 f-string 等，未当作业务发现灌水。已追查有实际影响的未定义引用，类型注解中的 GridParams 未列为运行错误。
- 20 个 YAML 解析、9 个 shell 的 bash -n、4 个自有 JavaScript 的 node --check 均未报告语法错误。
- 两个本地复现脚本共记录 14 组结果，覆盖重复提交、并发互斥、开/平仓异常、SQL 输入传播、AI 校验/覆盖层/回滚及网格成本。脚本中的某些依赖使用 mock，适用于验证控制流；不替代真实 PostgreSQL/Binance 集成验证。
- 测试未全绿。主要环境/测试问题包括 Python 3.9 的 asyncio.Lock 生命周期、旧 fixture/方法参数不匹配、全局模块 mock 污染、既有断言与当前逻辑不一致。详细 traceback 保存在相邻日志中。
- 未运行 tests/integration 中访问真实 K 线服务的集成测试、历史回测批处理和运维下单脚本；这些文件只进行静态检查，避免把审查变成生产操作。

## 复现入口与附件

在项目根目录执行（仅本地 mock 与临时文件）：

```bash
python3 docs/reviews/2026-09-29/binance_review_repro.py
python3 docs/reviews/2026-09-29/binance_review_extra.py
```

- `findings.json`：满足 delegate skill 输出字段要求的结构化发现，含相对路径、起止行、级别、分类、证据和建议。
- `coverage.json`：以 `(path,status)` 为标识的 OCR 全部条目、审查层级与跳过原因，以及补充检查清单。
- `preview.json` / `rules.json`：OCR 原始文件选择和解析规则；`static-scan.json` / `all-file-checks.json` / `pyflakes.log`：全仓静态证据。
- `binance-review-*.log`：测试和复现原始输出。

建议修复顺序：R01–R08 先处理输入边界及真实仓位安全；R09–R14 修复调参与部署/资源链路；再处理 R15–R18。每次修复需补相应异常/并发测试，并在与生产一致的 Python 3.11 环境复跑。
