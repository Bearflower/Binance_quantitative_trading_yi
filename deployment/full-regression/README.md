# 全量回归候选构建

`full-regression-release.yml` 仅在 `codex/full-regression-production-20261126` 分支构建镜像。它不连接生产服务器，不更新 `latest`，不启动生产服务。

`build-plan.json` 将 23 个生产服务容器映射到 11 个完整源码镜像。两个独立研究容器的入口不在此候选中，明确列入 `preserve_existing`；不得用候选镜像替换它们。数据库及其他项目不在构建/替换范围内。

每个镜像先推送包含完整提交 SHA、运行 ID 和重试次数的独立标签，再按 digest 在禁止网络、只读根文件系统、无运行时挂载的容器内验证模块导入。只有所有镜像都通过，才生成 `full-regression-release-<SHA>-<attempt>` 制品：

- `image-digests.json`：校验完整性后的镜像 digest、源码 SHA、运行 ID、容器映射及保留项。部署必须使用其中的 `image` digest 引用。
- `source-files.json`：构建工作区源码哈希，用于后续容器文件核对；它本身不证明生产容器已更新。
- `VERSION` 与 `build-plan.json`：本次版本身份及审核范围。

重跑时须重跑整个构建矩阵。同一制品拒绝缺失、重复、不同提交或不同重试次数的回执。

部署由新的受控运行器另行审核执行。不得绕过历史镜像锁或改写历史签名发布清单。运行器应保存并核对当前容器的 Env、Cmd、Entrypoint、Mounts、网络、健康检查与资源约束，保留 classic 账户、切点、冻结参数及风险锁。任何迁移之后、任何业务激活之前都必须使用本次权限清单审计；不得自动解锁交易或在启动交易 writer 后自动回滚。

AI Tuner 原运行时存在 `/app/strategies` 与 `/app/strategies/hrs` 的整目录代码挂载，会遮蔽新镜像代码。移除这两个代码挂载须作为显式部署变更审核，同时保留各策略精确的 `config.yaml`、`tuning_overrides` 和回执挂载。候选 AI Tuner 镜像已经包含 HRS 与 MTPCS 激进版实际导入依赖，构建探针不依赖宿主代码补齐。

部署成功仍须验证容器状态、实际镜像 digest、VERSION、未被配置挂载覆盖的关键源码哈希、启动后新增错误，以及真实只读接口/数据库联调。构建成功不等于完成这些生产验证。
