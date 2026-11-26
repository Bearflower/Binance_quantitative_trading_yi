# ETH 跨账户持仓归属误判修复

default 账户激进策略 ETHUSDT 实际入场订单 8389766318133453495，交易所数量 0.016。归属查询仅按 symbol 查最新 trade_records，误命中 grid_pm_ethusdt_01 的网格订单，导致月度额度检查报 tracked position owner mismatch。

提交 b600eb3c 为 _open_entry_sql 增加当前 TRADE_ACCOUNT_ID 过滤，历史 NULL 按 default 解释。保留现有 symbol 参数契约、状态条件和归属调用方。环境账户值作为 SQL 字面值时转义引号。

新测试修复前 4 failed、2 passed；修复后账户隔离及占用回归 19 passed。完整归属套件 64 passed、2 failed；修改前复跑同样两项失败（激进策略保护 fixture 陈旧），未引入新失败。候选生产镜像 Python 3.11 中新增 6 项测试通过。真实数据库只读 A/B 查询 owner_before=grid、owner_after=btc_eth_aggressive。

发布前因 AI Tuner 更新发布指针而停止一次。重新核验当前指针 05a9ed17、激进镜像与源文件未变，基于新配置仅替换激进服务镜像。持 compose_deploy.lock，发布前后门禁通过，交易所持仓/保护单/分配、其他运行容器身份一致，新容器 healthy。生产 _owns_exchange_position 实际方法通过只读数据库验证 ETH=True。PENDING claim 与 submitting 预留按现有在仓占用规则继续保留。

镜像 local/aggr-account-owner:b600eb3c，ID sha256:a432f964360e98bb9796c264cfe80f9ccb051d0da865ac611127a8d341335ef5。服务器归档 /root/trading_system/.releases/aggr-reconcile-20261116，保存 images-owner.json、state-owner.json 和前后门禁证据。
