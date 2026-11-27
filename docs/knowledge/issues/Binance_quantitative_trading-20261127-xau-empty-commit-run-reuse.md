---
id: Binance_quantitative_trading-20261127-xau-empty-commit-run-reuse
类型: issue
项目: Binance_quantitative_trading
标题: SHA-only 构建导致 XAU 事件版本为空并跨版本复用 run
日期: 2026-11-27
标签: [bug, XAU, 归因, 版本证据, 受限发布]
任务标识: xau-version-provenance-20261127
状态: 待复查
审查: 已审查
---

# SHA-only 构建导致 XAU 事件版本为空并跨版本复用 run

## 现象与根因

11/27 21:36 北京时间，三个 XAU profile 已持续写入归因状态，但当日 03:00 UTC 起各 11 条事件的 git_commit、producer_git_commit 为空。现役镜像 SHA 为 c4a0bea5，active run 仍记录 acee4c82。

完整构建只写 GIT_SHA；读取器不向 git_commit 回退。run 复用只比较 commit 与配置，因两个空 commit 相等而误判同一代码版本。

## 解决

`86f888ee3648c01e3caf47c97b9acabfae8dd7d6`：`strategies/btc_eth/shadow/collector.py::_read_git_info` 在缺 commit 时使用 SHA；`shadow/main.py::_prepare_shadow_run` 要求已知 commit、SHA、配置哈希全匹配。事件和 producer 继续持久化到 research JSONB；历史事件不回填。

发布仅更新两个影子容器，继承现役不可变底图，仅覆盖 collector/main/VERSION。资金费与交易服务保持不变。随本次 SHA 固化权限审计资源并校验哈希后只 audit。

## 验证

13 项版本测试、18 项发布测试；完整相关组合 544 passed、1 skipped，独立 PostgreSQL 两项通过（SHA-only 真实写库→进程退出→独立读取/导出，以及资金费持久化）。独立上下文复核另运行 49 项全绿。

PR #60 已合并，受限 Actions `46010982575` 发布失败；第一目标回到旧镜像，校准版未切换。Compose 误清理两个同标签停机备份，已按旧 inspect 恢复未启动配置替代容器，原可写层/日志无法恢复。执行器改为精确 ID 切换与同 ID 回退，待新一轮发布和自然 cron 验收。详见报告事故记录，不能把测试或配置替代称为上线成功或完整恢复。

## 适用范围与修订

只涉及 XAU 影子代码身份与分段；不改变趋势/价值回归判定、虚拟撮合或资金计算。修复前 33 条空版本事件保留为历史证据限制。无替代条目。

## 教训

AI 归纳，仅供参考：运行版本必须按完整身份判断；空字符串相等不能证明运行代码相同。健康状态和 RestartCount 不能替代镜像、run 与事件的版本一致性核对。

## 关联

- [核验报告](../../reports/xau-version-provenance-2026-11-27.md)
- [归因持久化测试](../../../tests/test_strategies/test_xau_attribution_postgres.py)
- [容器重建日志丢失](Binance_quantitative_trading-20261028-container-json-logs-lost-on-recreate.md)
- [挂载遮蔽镜像配置](Binance_quantitative_trading-20261020-shadow-compose-bind-mount-masks-image-config.md)

## 受限重试结果（22:21 北京时间）

PR #61 / d402ad0c，Actions46014565497成功。两个影子healthy、0重启、版本/文件/运行配置核对通过；两个新run的commit/SHA已落库一致，冻结参数未变化。158个排除容器在重试中不变，四production表0。原ID停机备份保留；初次误删的两个旧备份仍仅为配置替代，不称完整恢复。最终相关549passed1skipped，独立52passed。新版本三profile自然事件待23:15/23:20，不提前标已解决。旧空版本累计36条不回填。
