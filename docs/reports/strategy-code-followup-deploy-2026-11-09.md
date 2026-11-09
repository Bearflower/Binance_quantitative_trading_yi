# 七项策略代码后继发布验收（2026-11-09）

## 范围和结果

用户授权继续部署。受控发布固定生产前驱 pin `8991d03d3c4625612b4d42d2014e829aa6750f4f`，只替换 `btc-eth-aggressive-strategy` 与 `grid-strategy` 两张镜像。激进版修复退出 PnL 重复计数，加入 PM UM 止损成交只读证据校验；网格半自动模式拒绝直接实盘下单。风险政策、资本分配、127 笔未归属事件均未修改。

首个提交 `a79a3d78e9de10bf1c181a056c82852d687d19d7` 的 CI 构建包已到服务器，但一次性发布在预检前被前驱证明门禁阻断：该前驱的 `state.json` 只有完成状态，交易所通过证明单独存于 `exchange-after.json`。没有创建发布状态目录、没有拉起新镜像、没有改动 pin。修正读取位置后，以新标记、独立发布 ID 和新提交 `6279ded829c8013062d24b3841f912d1b340fe9c` 重试。

第二次 CI/CD 重试完成。生产 pin 为 `6279ded829c8013062d24b3841f912d1b340fe9c`，`status=active`；`state.json` 为 `status=complete, step=success`，`changed` 精确等于激进版和网格。激进版运行镜像为 `ghcr.io/bearflower/trading-btc-eth-aggr@sha256:6ee1ed7945b94ed15a4e5bf200dab38b66f2a8eb55b22a54097914be4c3cd460`，网格为 `ghcr.io/bearflower/trading-grid@sha256:148deaca0d6242c9507f3648ebb5ef6ea8411b3482eba2a09b55bf37cf7ab6c5`，两容器均 running/healthy。发布脚本校验了镜像 VERSION、源码 SHA256、compose 配置仅镜像变化、无关容器身份不变以及启动日志。

## 发布前置对账

2026-11-09 09:23–09:25 北京时间，预激活门禁先因两条旧 `submitting` 风险预留拒绝。签名查询证实 SOLUSDT 和 XRPUSDT 两笔开仓订单均 FILLED，`origQty=executedQty` 分别为 0.27 和 21.1，当前账户无这两币种持仓、普通挂单或条件挂单。调用既有 `RiskPolicyStore.reconcile`，在账户锁下再次核验订单、UM 账户持仓、positionRisk 和挂单后，两条预留与旧 claim 已释放；未撤单、未下单、未平仓。随后预激活只读门禁 `ok=true`，账户无持仓和挂单。

## 发布后交易所与政策验收

- 发布 `exchange-before.json` 与 `exchange-after.json` 均为 `ok=true`、后者 `read_only=true`；`position_exposure=[]`，普通挂单与条件挂单哈希前后相同。
- 资金分配哈希前后均为 `d7398bee4c57fc6c55e75023b244bc394c0c5de9deefd9eb0a2eb62bc33ea057`；发布脚本另比较风险政策行及活动指针快照，要求完全相同才可完成。
- 2026-11-09 发布后 `strategy_gate` 返回 `EntryDenied: missing or expired risk policy`。这是已有政策到期的拒新仓状态；本次没有续期，不代表策略可重新开仓。
- 本地发布与策略定向测试：137 passed。首次发布前的七项综合测试为 173 passed、2 skipped、1 deselected；单独排除的旧网格用例存在既有 mock 价格类型错误。

## 尚未由此次部署完成的内容

本次上线的是确定性代码修复和只读证据工具，不包含激进版 lot 级 STOP 退出驱动冷却、原版 8 月历史归属、网格盈利结论、自动分配解冻、现金流调整收益率或两个未来月度留出窗口。具体证据与验收门槛见 `seven-strategy-gaps-2026-11-07.md`。这些事项不能以本次容器健康或代码上线替代验收。
