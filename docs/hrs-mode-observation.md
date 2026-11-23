# HRS 四种入场模式核算与 LV-RM 回调观察

## 四组实盘账

按开仓方式分别核算 standard、emm、semi_emm、lv_rm。signal 的发生时间与完整持仓的平仓时间各自按半开窗口统计。区分生产路径、影子路径、归属不清与交易所实成。获取报告时使用 HRS 实例的账户身份，以只读可重复读事务读取。部分止盈和多次成交合并为一个持仓周期。核对开仓信号、订单、成交、实际佣金和资金费后才统计净胜率和每笔净期望。初始止损风险有确证时另算净 R 倍数。旧数据按证据覆盖报告，样本为空时显示 null。一次拒单可以属于生产路径，实盘胜率分母只含可核实的闭合交易。

生产服务器上的限定发布目录含以下入口。报告默认写到标准输出。单次报表进程使用现有实例的数据库身份，只读事务结束后销毁。常驻盘口观察器单独使用 hrs_acceptance_reader。

```bash
python3 release/hrs-observer/activate.py "$release_dir" "$release_sha" \
  --report-start 2026-11-18T00:00:00+08:00 --report-end 2026-11-23T00:00:00+08:00
```

## 影子研究的冻结条件

研究对象是原始评分通过的每个 LV-RM 方向，包括后续被持仓、风控或名额拦截的机会。只读决策记录保留当时的指标与行情。研究使用信号 K 线以前连续 48 根已收盘小时线，以影线极值、收盘极值各画一套锚。做多要求低点先于高点，做空要求高点先于低点。平坦、波段相反、锚点顺序歧义或行情不足的候选保留在总数里。观察 38.2%/61.8% 和普通 30%/70% 的相同 ±3 个百分点区间，并与全体候选比较。按方向、RSI、布林带偏离、入场侧深度和点差比较有共同样本的组。

模拟每个信号单独交易 100 USDT，使用公开币安永续合约 20 档可见盘口，按入场和六小时后反向退出的相同币数量逐档估算。两次报价的可用窗口分别为对应时点起 60 秒。默认每边模型手续费 4 基点，另以每边 5 基点额外滑点做敏感性复算。模拟收益为资金费前的固定期限价格研究，独立于实际持仓止损、止盈、仓位和资金分配。盘口可能撤单，模拟成交不能证明同价实盘成交。缺报价、迟到、盘口无效、深度不足和未到退出时刻均不填零收益。

首轮发现期截至 **2026 年 12 月 1 日 00:00 北京时间**。六小时观察可能跨过该边界的发现期信号单列。首次独立评估使用 12 月整月信号，最后一笔的退出窗口结束后再导出。若样本太少则继续按自然月累计，同时保留逐月结果。调整参数需注明新版本并另起未来留出窗。

## 抽取与复算

确认常驻容器名为 trading_system-hrs-research-observer。两个导出命令用同一组带时区参数。生产端仅导出；在本地运行研究。示例在 2027 年 1 月 1 日北京时间 06:01 之后覆盖完整 12 月；提前导出时将窗口改为已过去的时间。传输过程应检查命令退出码，在两个文件都完整后再复算。

```bash
window=(--start 2026-12-01T00:00:00+08:00 --end 2027-01-01T00:00:00+08:00 --as-of 2027-01-01T06:01:00+08:00)
docker exec trading_system-hrs-research-observer python -m strategies.hrs.research_export analysis "${window[@]}" > analysis.jsonl
docker exec trading_system-hrs-research-observer python -m strategies.hrs.research_export quotes --sqlite /observations/receipts.sqlite "${window[@]}" > quotes.jsonl
python -m backtest.hrs.lv_rm_retracement --analysis analysis.jsonl --quotes quotes.jsonl \
  --holdout-start 2026-12-01T00:00:00+08:00 --output observation.json
python -m backtest.hrs.lv_rm_retracement --analysis analysis.jsonl --quotes quotes.jsonl \
  --holdout-start 2026-12-01T00:00:00+08:00 --slippage-bps 5 --output observation-stress.json
```

## 验收标准

| 项目 | 核对方式与通过条件 |
|---|---|
| 实盘核算 | 四模式和 unknown 齐全，影子信号不能增加实盘交易数。部分止盈、重复成交、超过五个周期、模式冲突、缺手续费或资金费、未知初始止损风险均有回归覆盖。 |
| 采集隔离与恢复 | 常驻进程只有只读数据库身份和公开 GET 接口。验证读库心跳、镜像和四个模块哈希。相同信号与采样阶段只有一条终态记录；重启后能继续等待退出，超时保留缺失原因。 |
| 估算与防止前视 | 对照多空测试的手算盘口结果。每组列出候选、可配对、排除和覆盖率。检查过期盘口、乱序或交叉盘口、未来 K 线、历史迟写和跨留出期样本隔离。 |
| 研究是否有增益 | 在独立期核对两套锚点的 fib 相对普通回调、全体候选和可比条件分层的扣费差值；检查换锚导致的命中翻转、滑点压力结果，以及剔除单一币种或单个重叠行情后的表现。只有双锚和成本压力下优势仍存在、且来源跨币种和行情时才值得进一步独立复验。缺可比样本、缺盘口、优势仅靠一个币种或一次插针时均记为待验证。 |

发布只创建独立观察容器。SQLite 收据保存在 /root/trading_system/research/hrs-mode-observer/observations。暂停采集用 docker stop trading_system-hrs-research-observer。保留数据目录可以续采；停机时缺失的历史盘口会保留为缺失。
