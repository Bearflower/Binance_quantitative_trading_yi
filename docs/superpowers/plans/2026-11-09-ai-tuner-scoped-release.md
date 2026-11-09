# AI Tuner 新信封热修的受控单服务发布方案

日期：2026-11-09。状态：**待设计与发布审查，未部署**。

## 现状与目标

`main` 已包含新信封提示词与结构化诊断兼容修复（`dc8c911e`、`6bfc5df4`）。本地测试 `ai_tuner/tests` 为 237 passed，`tests/test_ai_tuner_mvp` 为 757 passed / 5 skipped，独立提交级审查未发现剩余阻断项。

[Actions Run 42558894513](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/42558894513) 构建成功，但部署 job 在 `Block generic uploads when active release pins exist` 步骤退出；服务器活动 pin 为 `6279ded829c8013062d24b3841f912d1b340fe9c`，`status=active`。只读核对的生产 `ai-tuner` 仍 `running/healthy`，`VERSION.GIT_SHA=4ca5230a591f52742d737ac82f12947db4fd809c`，`purpose_driven_mvp.mode=audit`、`selected_strategy=null`。这次热修**尚未进入生产**。

目标是只发布 `ai-tuner` 镜像并保持现有策略镜像、compose 生效配置、风控政策、资金分配及交易所状态。发布后继续处于 `audit`，仅生成建议，不能使覆盖层或资格考试结果自动生效。

## 实现范围

1. 在 `select_deploy_mode.py` 增加专门的 `ai-tuner-successor` 模式。只接受本轮新建的一次性 marker；基线必须是审查时固定的远端 `main` SHA。严格校验推送范围：仅放行受控发布脚本、清单、工作流接线、测试、文档及 marker，不允许策略代码、共享模块、配置、数据库或风控规则夹带。若其他人先推进 `main`，重新冻结基线与清单后再提交。
2. 为该模式构建**仅 `ai-tuner`** 的镜像，使用本次提交唯一 tag 和不可变 digest；不更新其他镜像的 `latest`。CI 将 digest 和与提交 SHA 绑定的清单打包上传。通用上传 compose、同步 init-scripts、资金费凭据轮换和通用 `up` 均不执行。
3. 远端脚本必须在现有 `compose_deploy.lock` 内执行。预检先校验活动 pin、前驱 `state.json` 完成状态、前驱 overlay 哈希、目标 `ai-tuner` 当前镜像、所有被 pin 管辖的容器身份/健康、compose 栈列表及有效配置。校验交易所只读证明、活动风险政策与资金分配摘要，并留存前置快照；任一证据不完整则在拉镜像或重建前失败。
4. 只创建新的 release 目录和一份继承前驱 overlay 的候选 overlay；唯一允许改变的字段是 `services.ai-tuner.image`。对每个服务比较 compose `config --format json`，目标服务仅允许 image 引用变化，其余服务必须逐字段相同。用 `docker pull <digest>` 与无网络容器核对 `/app/VERSION`、`common_rules.txt`、`weekly_job.py` 的 SHA-256、`config.yaml` 的 `audit/null` 状态。
5. 仅对 `ai-tuner` 执行 `docker compose ... up -d --no-deps ai-tuner`。等待 `running/healthy`，核对运行镜像 ID、`VERSION.GIT_SHA`、两份源码 SHA、配置模式和启动日志。再次读取所有非目标容器身份、交易所/政策/资金分配证据；必须与前置快照一致。资金费采集器不停止、不重建，观察部署后连续槽位，不回填历史缺槽。
6. 后验全部通过后，原子更新活动 pin 指向新 overlay 与完整状态证据，写一条唯一的 `DEPLOY_SUCCESS`。若目标容器已重建但后验失败，保留失败状态并告警；不自动回滚到未经验证的旧镜像，不修改策略参数/风险额度/覆盖层。重试使用新 marker、新 release ID 和重新冻结的证据，不能重跑同一个发布包。

## 测试与门禁

- 纯逻辑测试：marker 必须恰好新增一次；清单哈希与允许路径、基线 SHA、镜像 digest 不匹配均 fail closed；CI 仅选 `ai-tuner`，不触发 `hrs`/`dashboard-api` 构建。
- 隔离运行时测试：pin 缺失或漂移、overlay 被篡改、容器不健康、镜像 VERSION/源码哈希错误、compose 非 image 字段变化、其他容器身份变化、政策/资金分配/交易所证据变化，均阻断。验证失败路径不会改 pin、不会把部分部署写成成功。
- 发布前重新运行本次两个测试套件和新发布测试，审查 `origin/main..release` 全部文件及是否有并行推送。先做只读生产预检；失败则不启动发布。
- 发布后五层验收：Actions 与远端唯一成功记录、目标容器健康/镜像 ID、`VERSION`、关键文件 SHA、日志与候选周度运行结果。不能把构建成功或 `main` 含代码当成生产生效。

## 不属于本次发布的事项

此发布不切换 `audit` 到 `live`，不选择 `selected_strategy`，不调整信号/K 线/外部快照门槛，不进行服务器回测，不修改资金分配或风险政策。真实金样对账与完整资格考试仍应保持 `BLOCKED_DATA`，直到各自证据门禁通过。
