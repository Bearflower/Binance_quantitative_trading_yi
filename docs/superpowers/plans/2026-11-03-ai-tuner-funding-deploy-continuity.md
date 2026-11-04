# 资金费日程采集器部署连续性实施计划

依据同日已批准的设计文档实施，范围仅限部署路径、测试和运行说明。历史缺槽不补写，资格考试与自动调优门禁不变。

## 任务 1：变更范围分类

在 `tests/test_scripts/test_select_funding_deploy.py` 先写 push range 的正反用例：纯文档/普通策略变更为 `keep`，采集器、共享依赖、Compose、DDL、部署脚本等变更为 `rotate`；未知事件、空范围、无法解析 diff、重命名任一端命中敏感路径均为 `rotate`。确认红灯后在 `scripts/select_funding_deploy.py` 实现；确认绿灯并检查输出只含受控枚举。

## 任务 2：CI 传递与模板渲染

先扩充 `tests/test_scripts/test_render_remote_deploy.py`，要求渲染后包含唯一 `FUNDING_DEPLOY_ACTION`，拒绝非法动作并保持原输出不变。随后修改 `scripts/render_remote_deploy.py`、`.github/workflows/deploy.yml`。特殊一次性部署模式原样运行；普通 full 部署把分类结果传至远端。渲染脚本执行 `bash -n`。

## 任务 3：远端 keep/rotate 路径

先更新 `tests/test_ai_tuner_mvp/test_funding_schedule_cli.py` 及部署静态/模拟测试，使 keep 路径必须在部署前后检查同一容器 ID、启动时间、Compose 标签及健康状态；不得 stop、不得 `--remove-orphans`、不得再次对采集器 `up`。rotate 保留先停后迁移/ACL/启动。权限门禁失败须停止精确采集器并退出。随后修改 `scripts/remote_deploy_runtime.sh`，运行 shell 语法检查与两路径测试。

## 任务 4：集成审查与上线

运行目标测试、仓库相关回归、`git diff --check`；独立代码审查后修复发现。提交隔离分支，核对远端 main、仓库部署规则和生产预检。仅在部署路径审查通过时快进推送 main；查 Actions、容器 ID/健康与采集日志，并观察至少三个连续闭合五分钟槽位。若新缺槽或身份意外变化，停止进一步部署，保留证据并告警。不得用回填掩盖历史缺槽。
