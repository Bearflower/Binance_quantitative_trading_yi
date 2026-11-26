# 全仓回归与生产发布验收（2026-11-26）

状态：最终交叉复审与生产发布准备中，尚未宣称完成部署。

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

重新发现仓库中 `test_*.py` 与 `*_test.py`，共 **537 个测试文件**。合并独立文件矩阵后，**9165 项通过，0 失败、0 错误、0 跳过**；没有遗漏或额外文件。每条采用结果均已与当前测试文件 SHA256 核对，日志和 JUnit 计数也逐项一致。

`tests/test_scripts/test_grid_release_guard.py` 在网格与发布组重复执行，统计只采用最新发布组结果。构建 artifact 断言更新后，已通过同一 runner、`final-env.json` 和本机网络隔离重新单独运行 `test_full_regression_build.py`，15 项通过。

仓库内已保留 [测试清单](all-tests.json)、[逐文件结果与 SHA256](per-file-results.json) 和 [汇总结果](validation-results.json)；完整逐文件哈希、日志和 JUnit 路径见 `/tmp/binance-production-readiness/final-consolidated.json`。这些结果证明当前回归覆盖；**尚未部署，也未据此宣称生产可用**。真实 CI 镜像构建、只读账户联调和部署后验收另行留证。
