# 激进策略平仓预留自动释放验收

2026-11-16，SOLUSDT 入场订单 258858985691 已成交且交易所无持仓、普通挂单或条件挂单，但入场预留仍为 submitting。既有 reconcile 方法可在两路仓位与挂单证据齐全时释放预留。该方法主要在新入场准备触发，周期保护管理未接线，导致平仓后等待新信号。

修复提交 7f81958e 在 recover_reserved_protection 完成保护检查后调用原有 reconcile。周期持仓管理在遍历本地持仓之前调用该方法，空本地持仓也会执行。保留既有数据库事务、策略锁、终态订单、多路仓位和挂单校验。对账失败沿用异常隔离。

新增测试先复现两个失败用例，再验证无入场信号触发、异常隔离、活动仓位保留和已平仓释放。本地相关测试 63 passed、54 skipped。生产候选 Python 3.11 镜像内对账及风险分支测试 31 passed、37 skipped。跳过项目需要数据库测试条件；风险分支套件存在既有异步 setup 警告。跨 HRS 全套在激进镜像中缺少 ai_tuner 包，本地相关测试已覆盖该部分。

发布使用当前生产激进镜像为基底，仅覆盖 shared/entry_risk_policy.py。持有 compose_deploy.lock，比较 Compose 服务配置仅 image 变化，再仅更新 btc-eth-aggressive-strategy。部署前后 pre_activation 通过，持仓、保护单、普通挂单及分配哈希一致，其他运行容器身份一致。新容器 healthy。镜像为 local/aggr-periodic-reconcile:7f81958e，镜像 ID sha256:1b1119793595e0ee1ea55c7d397b7576a7d2cb45d74a6e59103e6f7c5547cc7d。生产发布覆盖配置指向新镜像。

服务器归档 /root/trading_system/.releases/aggr-reconcile-20261116 保存原容器配置、原发布指针、补丁、候选 Dockerfile、测试、部署脚本与前后门禁证据。已平仓 SOL 预留 released，BNB/ETH 活动预留保留。
