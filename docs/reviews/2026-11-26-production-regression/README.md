# 全仓回归与生产发布验收（2026-11-26）

状态：首次候选已通过回归与 CI；真实 stage 发现 Docker 规范化差异，发布适配专组已复测通过，等待新候选提交、CI 和生产验收。

本轮在独立工作区 `codex/full-regression-production-20261126` 整合主分支、经典账户已发布版本及完整审查修复。原工作区未解决的文档冲突和无关修改保持原状。网格以已发布的 `8dd906c914c5c07e4ba63bd81b778ff1e213ed25` 为最新基线。

## 可复现的回归入口

Python 3.11；按每个测试文件运行独立 pytest 进程，覆盖项目根 tests、各策略、AI 调优与服务内部的测试。隔离用于避免不同部署包使用同名顶层模块或同名测试文件造成导入污染，不删除或跳过测试。

```bash
python docs/reviews/2026-11-26-production-regression/run_matrix.py \
  --root "$PWD" \
  --manifest docs/reviews/2026-11-26-production-regression/all-tests.json \
  --out /tmp/full-regression --jobs 4 --timeout 900
```

将 PostgreSQL 测试变量指向本机独立的临时库；有明确库名/一次性 ACK 要求的测试必须遵守各自断言。数据库组顺序执行，避免共享测试 schema 的并发干扰。真实 K 线只读联调通过 SSH 本地端口转发，设置 `KLINE_SERVICE_URL` 和 `KLINE_INTEGRATION_URL` 为转发后的 `/api/v1` 地址。

附带 `localguard/sitecustomize.py` 会阻断外部 socket 连接，只允许本机 TCP 和临时 Unix socket。测试不接入真实交易账户、不发送测试订单。真实交易所联调由单独的只读发布探针完成。

## 生产验收标准

权限契约审计通过；11 个完整镜像绑定同一提交和构建 Run；23 个目标容器逐个校验运行配置、健康、镜像 digest、VERSION 和实际代码 SHA256。两个独立研究观察器及无关服务保持原状。保留真实经典账户、冻结的风险政策、持久账本和已有保护。网格维护须在旧 writer 停止后持独占锁处理并核对已有订单，证据不足时禁止启动新 writer，禁止为通过验收自动平仓或解锁。

## 最终回归记录

此前候选 `9243f52cbc665a33144fbaa53c3a9ab85a4677b6` 的完整回归为 **537 个测试文件、9165 项通过，0 失败、0 错误、0 跳过**；CI Run `45843458488` 的 11 个镜像构建和离线导入也全部通过。

真实 stage 随后发现 Docker 29.6 / API 1.44 将 `HostConfig.OomKillDisable` 的显式 `null` 规范化为 `false`。发布适配只规范化创建请求中的这一显式空值，严格运行配置校验保持不变，并新增 7 项回归测试。修复冻结后，使用相同 runner、`final-env.json` 和本机网络隔离重新运行全部 **4 个发布测试文件，113 项通过，0 失败、0 错误、0 跳过**。

合并未改动业务测试的既有结果及最新发布专组结果，当前证据覆盖 **537 个测试文件、9172 项通过，0 失败、0 错误、0 跳过**。没有遗漏或额外文件；每条采用结果均与当前测试文件 SHA256 匹配，日志和 JUnit 计数逐项一致。网格与发布组重复运行的 `test_grid_release_guard.py` 只统计最新发布组结果。

仓库内已保留 [测试清单](all-tests.json)、[逐文件结果与 SHA256](per-file-results.json) 和 [汇总结果](validation-results.json)；完整日志和 JUnit 路径见 `/tmp/binance-production-readiness/final-consolidated.json`。本次发布适配必须绑定新的候选提交和重新构建的镜像；此前 SHA 的成功 CI 不作为适配后的镜像证明。**尚未完成生产部署，也未据此宣称生产可用**；真实联调和部署后验收另行留证。
