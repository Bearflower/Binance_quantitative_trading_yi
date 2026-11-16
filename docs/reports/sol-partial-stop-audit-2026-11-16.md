# SOL 部分 STOP 后续平仓审计验收

2026-11-16 SOLUSDT 入场订单 258858985691 成交 0.34。08:15:38 北京时间 STOP 订单 258908510718 成交 0.10（两笔 userTrades），09:10:03 reduceOnly LIMIT 258920400783 成交剩余 0.24。原审核窗口仅取至 STOP 最后成交，净仓位仍有 0.24，无法生成 closed_stop_episode 证明，导致新入场报 STOP evidence pending review。

提交 b03c7487 在直接止损整仓证明失败时，先验证两路仓位均平且无普通或条件挂单，再读取完整窗口。校验完整成交链从原始入场至最终归零，拒绝中间归零重开、止损后加仓、缺失成交与 STOP 身份不符。止损订单盈亏仅归属其自身成交；止损冷却时间仍取真实 STOP 最后成交时间。

本地测试 25 passed、1 skipped（PostgreSQL 集成环境）；生产候选镜像内相关测试与预留对账回归 36 passed。候选源于此前生产激进镜像，仅覆盖 stop_episode_evidence.py。持 compose_deploy.lock 发布，前后门禁通过，交易所持仓、挂单、分配与其他容器身份核对一致，容器 healthy。镜像 local/aggr-partial-stop:b03c7487，ID sha256:3848c31ea742868d8a7c9c1a723b43a3655915b25750482e24fde9934f34f735。

发布后正常自动审计将 algo 2000001699647175 转 verified，旧取消 STOP 2000001698385204 转 no_fill。STOP 账本为 quantity=0.10、net_pnl=0.084388500000、last_fill_time_ms=1794788138191。SOL 入场预留已 released。后续新入场继续执行既有冷却规则。

服务器证据与源码 bundle 归档 /root/trading_system/.releases/aggr-reconcile-20261116。源码已推送 codex/aggr-flat-reconcile-20261116 分支，远端合并待完成。
