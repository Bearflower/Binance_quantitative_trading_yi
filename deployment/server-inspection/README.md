# 2026-11-28 巡检修复受限发布

本发布仅允许替换 `trading_system-hrs-acceptance` 和 `ai-tuner`。基线 ID、镜像 digest、各自唯一允许覆盖的 Python 文件及唯一环境差异在 `plan.json` 固定。不能套用到其他实例或随意更新基线。

CI 只在 `codex/server-inspection-fix-20261128` 分支推送或手动触发，构建两个 immutable-base overlay，验证 `/app/VERSION`、源码 SHA-256 和离线导入，输出带两镜像 digest 的发布包。它不连接生产、不推 `latest`、不自动激活。提交和推送仍遵守项目授权约束。

发布包包含本版统一权限审计工具/清单、生命周期登记工具及哈希。仅运行 `audit`，不执行迁移、apply 或补权。生产凭据只继承现有容器环境，完整配置快照仅写入 0700 发布目录的 0600 文件；命令失败不回显可能带凭据的输出。

## 生产步骤

将 CI 的发布包放在独立目录。以下命令中的 SHA 必须是实际 CI 源码 SHA：

```bash
python3 /path/to/bundle/scripts/server_inspection_release.py stage --bundle /path/to/bundle --source SHA
python3 /path/to/bundle/scripts/server_inspection_release.py activate --bundle /path/to/bundle --source SHA
```

`stage` 在共享 compose 发布锁下确认两个旧 ID/image 均未变化，记录所有其他运行容器的 ID 和 StartedAt，执行权限 audit，核验镜像 digest、基础层、源码、VERSION 和离线导入。克隆保持原 Config/HostConfig、挂载、网络与资源限制，仅暂时关闭候选自动重启，更新版本标签，并给 reporter 增加 8 MiB 来源上限配置。

Reporter 候选先启动独立只读探针：原 256 MiB 容器限制、无生产文件挂载、临时 `/tmp` 和 `/app/logs`、仅现有 DB 网络；完整读取报告后检查 coherent=true、无 source_error，峰值 RSS 不超过内存限额的 80%。允许合法 `PENDING`，不要求历史完整或收益通过。探针最长 300 秒并在结束时按精确 ID 删除。该探针不运行策略、交易写接口或发送通知。

`activate` 再做权限 audit，核验 staging 后的旧/新实例指纹，然后依次停旧、改名、启动新实例。每一步按精确 ID 保存证据，并核验健康、源码、环境、挂载和其他容器不变。Reporter 必须写出启动后的新报告，最长额外等待 300 秒，防止旧报告文件使 Docker 提前 healthy。全部切换成功后重新核验两个实例。

运行切换失败时按已记录 ID 倒序回退：先关闭失败新实例的自动重启并停止，再恢复旧实例名称及原 restart policy。回退失败写入 `activation.json.rollback_failures`，不得宣称成功。旧容器与镜像保留，不自动清理。

只有全部运行验证成功后才原子追加两个旧实例的退役登记；使用 `/root/trading_system/.ops/container_retirements.lock`，保留既有条目，通过完整 ID/镜像/命令/退出状态/健康接替者的 `partition` 核验。运行切换失败不发布登记。若仅最终登记写入失败，状态保留 `runtime_verified`，不重复切换已验证运行实例；修复登记写入环境后可重试：

```bash
python3 /path/to/bundle/scripts/server_inspection_release.py finalize --bundle /path/to/bundle --source SHA
```

`finalize` 会重新验证两实例身份、健康、源码、配置以及所有受保护容器，然后幂等追加相同登记。已有不同登记会阻断，绝不覆盖。发布旧 pin 和历史签名清单不修改，本次凭据独立保存于 `.releases/server-inspection/SHA/`。

## 验收

- 权限 audit 为本发布清单且 issues 为空。
- 两目标的新 image digest、VERSION、源码哈希和运行配置与发布包一致。
- Reporter 新报告读取完整、无来源预算错误；自然样本不足仍明确 PENDING。
- AI 主服务健康；当前经典账户对账应按自然周期核验，不伪造 PM 历史归属。
- 其他运行容器 ID 和启动时间与 stage 前完全相同。
- 两条新退役登记通过核验，旧容器和回滚镜像仍可用。

该脚本不管理全局磁盘清理、旧巡检归档或历史业务批准。
