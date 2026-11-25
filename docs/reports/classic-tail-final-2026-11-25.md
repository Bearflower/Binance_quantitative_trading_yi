# 经典账户切换尾项验收（2026-11-25）

最终验收时间：**2026-11-25 21:38:28，Asia/Shanghai**。

数据库自动维护根因及配置一致性问题已修复上线。25 个维护范围内服务全部运行且通过各自健康检查（无健康探针的观察器验证进程运行），当前容器重启计数均为 0。PM 历史权益仍缺外部凭据，不在本次完成项中。

## 数据库根因与修复

PostgreSQL 15.19 的 bootstrap OID 10 原来是已降权的应用账号 `trading_user`，原生自动维护无法处理迁移管理员持有的受保护表。补偿定时任务只能清积压，不能改变这一身份问题。

本次通过受控 SQL 分离身份，没有直接修改系统目录、提升应用账号或转移受保护表给应用：

| 身份 | OID | 运行属性 |
|---|---:|---|
| `trading_bootstrap_admin` | 10 | SUPERUSER、NOLOGIN、无密码、无角色成员关系 |
| `trading_user` | 382863 | LOGIN、NOSUPERUSER、NOCREATEDB、NOCREATEROLE、NOREPLICATION |

应用凭据与设置在服务端复制并验证；原有 BYPASSRLS 兼容例外保持不变。1,306 个已审核的存量 owner 对象转到新的同名应用角色，六组默认 ACL 按契约迁移；受保护 owner 和运行权限上限不变。比早先演练多出的四项只是 BIGTIME/OPG 的两张受管 K 线表及序列。

先关闭应用登录、排空会话，再等待完整的 60 秒认证超时窗口。角色变更与权限修复、验收在同一事务中执行。真实应用凭据通过生产网络上的 SCRAM 新连接验证。最终旧 OID 应用会话为 0，新 OID 应用会话为 29。

生产原生维护证据：管理员持有的临时合成表在 **21:31:17** 自动完成 VACUUM 和 ANALYZE，各计数 1，死元组 0；未用手工 VACUUM 冒充原生验证。验证表随后通过身份及成功计数守卫删除。

补偿 `trading-postgres-maintenance.timer` 已停用且禁用。唯一权限契约的 `allow_legacy_demoted` 已改为 **false**，普通审计不再接受旧故障布局。服务器标准入口 `scripts/database_permissions.py` 与 `database/postgres/permissions.json` 已同步，旧文件私有归档。最终审计 **1,347 对象、0 issues**，严格契约 SHA256：

`9cd14a74769c508295d5568fb0228a57d5de7198be46bcbf10b46a654b194ae4`

实现及演练：`6ca76c20`；严格契约回归：`a1bf9f86`。完整数据库/脚本测试 **97 passed**。详见 [维护与回退手册](../deployment/postgres-bootstrap-identity.md)。已生成的回退 SQL 保留当时审核过的过渡契约；没有重写或伪造历史执行结果。

## 恢复期间发现并补发的 UTC 校验

恢复原容器时，激进版与网格旧镜像仍含 `timezone('Asia/Shanghai', ...)` 的切点检查。`position_claims.created_at` 实际存无时区 UTC，旧代码将合法经典认领 193/194/195 误判为 PM 遗留。仓库已有 `153809d1` 的 UTC 修正，但此前漏发到这两个镜像。当前源码及 UTC SQL的只读复证均无旧 PM 预留或认领触发。

没有删除三个现仓认领、重置账户权益或绕过启动校验。补发只在各自现网镜像之上覆盖 `shared/binance_account_mode.py` 和 `VERSION`，保留原交易改动、环境变量与挂载；其余实际调用该检查的原版、HRS、新币镜像也逐一核实为 UTC。

[构建 45624528310](https://github.com/Bearflower/Binance_quantitative_trading_yi/actions/runs/45624528310)：源码 `ca19a098e267192aa955400c3fd78f80dc286325`，工作流 `37dda7d1`，DEPLOY_ID `00000E01`。

| 服务 | 已验收镜像 |
|---|---|
| MTPCS 激进版 | `sha256:b3860372c73fd699a3d322928155680af7e1f2e059fa244fb4cbb560a1b75872` |
| 网格信号服务 | `sha256:33402d8098d1d847e66c1e2ac4f71f8cd4bb14768f9349495afe8c244a40d403` |

实际模块 SHA256 均为 `1140031b10a24a4328fd0ef225aa90a39f88bdbac96ac0e2a5865821204d7255`。11 项相关回归、独立脚本复审及两个使用真实凭据的签名只读完整预检通过。新容器环境和挂载与旧容器逐项相同，最终均 healthy、零重启。

维护时间比原预计 90 秒更长：服务分批退出耗时增加，且两个旧镜像的遗漏需补构建。21:23:30 开始窗口，21:26:33 身份事务提交，21:26:38 新应用认证通过；普通服务陆续恢复，激进版于 21:35:29、网格于 21:36:06 使用修正镜像启动。原维护脚本因两个旧镜像未通过健康验收而报错，该失败记录原样保留；后续修复和最终验收另存证据，没有把第一次恢复标成成功。

## 账户、数据与配置验收

紧邻维护前与最终恢复后的签名 GET 快照对比：

- 主账户 ETH +0.015、XRP +26.1、BNB +0.04，数量、方向、均价一致。
- 普通挂单为 0；8 张交易所条件单的身份、方向、数量、触发价及 reduce-only/close-position 状态一致；各持仓均有覆盖全仓的止损。
- 三个认领和订单创建回执均属于 `btc_eth_aggressive`，认领未修改。HRS 本地持仓和待处理超时记录均为空。
- 网格子账户仍为空仓、无普通及条件挂单。

截至 21:09 的独立成交复核，经典段三笔成交和三条手续费均唯一归属激进版；佣金合计 **−0.02136825 USDT**，已实现盈亏与资金费均为 0，未归属、冲突、成交缺收入、覆盖缺口均为 0。详见 [资金证据与对账尾项](classic-ledger-remainder-2026-11-25.md)。此结论不代替尚缺的 PM 期末权益凭据。

数据后台已实际加载随镜像打包的配置，端口类型保持整数。HRS 新加载器与配置身份检查均已上线，自然生成回执为 V20260819，参数哈希仍为 `39e90a4d1b1cc526f42d14fb3248a9b292c0aa500a36e4df76159efda25fbb5e`。详见 [配置和恢复验证报告](classic-tail-configuration-2026-11-25.md)。

## 备份与私有证据

全量物理备份已在同架构 PostgreSQL 15.19 隔离容器实际恢复，`pg_verifybackup`、WAL 重放及业务表核对通过。临时容器和解包副本已清理。初次离机传输不完整，补传曾报告散列重试；最终独立 `shasum -a 256 -c SHA256SUMS` 对 base、WAL 和 manifest 三个文件全部通过，源归档 SHA256 也再次确认未变化。

服务器原始备份保留于 `/root/trading_system/database/postgres/backups/codex-bootstrap-20261125-1804`；离机备份与验证记录保留于 `/Users/yl/.codex/pm-cutover/20261125-89160896/db-root-repair/physical/`，未把凭据或备份提交进仓库。

运行证据目录：`/root/trading_system/.releases/pm-window-89160896/db-root-repair/`。关键文件：

- `production-window/final-acceptance.json`、`native-proof.json`、`canonical-audit.json`。
- `account-immediate-before-window.json`、`account-after-window.json`。
- `claim-guard-release/` 中两个镜像、只读预检及激活记录。
- `candidate/artifacts/` 中当时生成的正向、回退、登录关闭/恢复 SQL；原文件继续保存。

最终服务器根盘 **76%**，约 **15 GiB** 可用；本次没有删除无关镜像、数据卷或回滚容器。

## 尚不能闭合的唯一业务尾项

仍缺主账户平仓后、切换前 PM 期末权益及对应资金明细。现有成交、手续费、转账和余额证据已归档，但不能据此虚构 PM 权益锚点或资产折算值。相关 HRS 权益校验及 AI 自动分配限制继续保留，等真实明细补齐后按既有对账工具接续历史。
