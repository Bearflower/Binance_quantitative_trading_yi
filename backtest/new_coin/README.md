# 新币做空策略回测框架

## 概述

本回测框架用于验证新币做空策略V4.1（信号质量优化版）的效果，支持向量化回测和逐笔回测两种模式。

## 功能特性

- ✅ 完整的回测流程（数据加载 → 信号检测 → 订单执行 → 统计分析 → 报告生成）
- ✅ 复用现有策略代码（评分引擎、形态识别）
- ✅ 真实的交易模拟（手续费、滑点、杠杆）
- ✅ 完善的风控机制（止损止盈、连续亏损暂停、最大回撤熔断）
- ✅ 详细的统计指标（胜率、盈亏比、最大回撤、夏普比率）
- ✅ 可视化报告（资金曲线、回撤曲线、盈亏分布）

## 目录结构

```
backtest/new_coin/
├── data/                      # 数据目录
│   ├── klines/               # K线数据（CSV格式）
│   │   ├── BTCUSDT_1h.csv
│   │   ├── ETHUSDT_1h.csv
│   │   └── ...
│   └── coin_list.json        # 交易对列表
├── scripts/                   # 脚本目录
│   ├── backtest.py           # 回测主程序
│   └── download_klines.py    # K线数据下载脚本
├── reports/                   # 报告目录
│   ├── backtest_report.md    # 回测报告
│   ├── trades.csv            # 交易记录
│   └── charts/               # 图表目录
│       ├── equity_curve.png
│       ├── drawdown_curve.png
│       └── pnl_distribution.png
├── backtest_engine.py        # 回测引擎核心类
├── data_loader.py            # 数据加载器
├── order_executor.py         # 订单执行器
├── position_manager.py       # 仓位管理器
├── statistics_analyzer.py    # 统计分析器
├── report_generator.py       # 报告生成器
├── test_limit_order.py       # 限价单改造测试脚本（V4.1，31个用例）
└── README.md                 # 说明文档
```

## 使用方法

### 1. 准备数据

#### 1.1 准备K线数据

将K线数据保存为CSV格式，放置在 `data/klines/` 目录下。

CSV文件格式（币安API导出，无列名行，按固定字段顺序解析）：
```csv
1609459200000,28923.63,28935.42,28900.00,28915.00,12.345,1609459199999,356789.12,1234,5.678,164012.34
...
```

字段顺序：`open_time, open, high, low, close, volume, close_time, quote_asset_volume, trade_count, taker_buy_volume, taker_buy_quote_volume`

#### 1.2 准备交易对列表

编辑 `data/coin_list.json` 文件（支持 `listing_time` 或 `onboardDateStr` 字段，自动处理 UTC 后缀）：

```json
[
  {
    "symbol": "BTCUSDT",
    "listing_time": "2025-01-01T00:00:00 UTC",
    "base_asset": "BTC",
    "quote_asset": "USDT"
  },
  {
    "symbol": "ETHUSDT",
    "onboardDateStr": "2025-01-02T00:00:00 UTC",
    "base_asset": "ETH",
    "quote_asset": "USDT"
  }
]
```

### 2. 运行回测

```bash
# 使用默认参数
python backtest/new_coin/backtest.py

# 指定参数
python backtest/new_coin/backtest.py \
    --initial_balance 500 \
    --start_date "2025-01-01" \
    --end_date "2025-12-31"
```

### 3. 查看报告

回测完成后，报告将保存在 `reports/` 目录下：

- `backtest_report.md` - Markdown格式的回测报告
- `trades.csv` - 详细的交易记录
- `charts/` - 可视化图表

## 回测参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| initial_balance | 500 | 初始资金（USDT） |
| commission_rate | 0.0004 | 手续费率（0.04%） |
| slippage_rate | 0.0001 | 滑点率（0.01%） |
| leverage | 2 | 杠杆倍数 |
| start_date | 2025-01-01 | 回测开始日期（自动补全为 00:00:00） |
| end_date | 2025-12-31 | 回测结束日期（自动补全为 23:59:59，包含整天） |

## 统计指标

| 指标 | 说明 |
|------|------|
| 总交易次数 | 回测期间的总交易次数 |
| 胜率 | 盈利交易次数 / 总交易次数 |
| 总盈亏 | 所有交易的总盈亏金额 |
| 总收益率 | 总盈亏 / 初始资金 |
| 平均盈亏 | 所有交易的平均盈亏金额 |
| 盈亏比 | 平均盈利 / 平均亏损 |
| 盈亏因子 | 总盈利 / 总亏损 |
| 最大回撤 | 从峰值到谷值的最大跌幅 |
| 夏普比率 | (年化收益率 - 无风险利率) / 年化波动率 |
| 平均持仓时间 | 所有交易的平均持仓时长 |

## 注意事项

### 1. 数据质量

- 确保K线数据完整、准确
- 检查交易对列表的有效性
- 验证配置参数的合理性

### 2. 回测限制

- 无法完全模拟真实市场环境
- 忽略了市场深度和流动性影响
- 无法模拟极端行情（闪崩、暴涨）

### 3. 参数优化

- 避免过度拟合
- 使用样本外数据验证
- 考虑参数稳定性

## 开发计划

### V1.1（计划中）

- [ ] 支持多策略对比
- [ ] 添加参数优化功能
- [ ] 支持实时回测

### V1.2（计划中）

- [ ] 实现向量化回测
- [ ] 使用多进程并行处理
- [ ] 优化数据加载速度

### V1.3（计划中）

- [ ] 添加交互式图表
- [ ] 支持自定义报告模板
- [ ] 添加Web界面

## 技术支持

如有问题，请查看：

- 策略文档：`docs/requirements/new_coin/新币做空策略 V4.0 完整版.md`（内容已更新至 V4.1，含限价单改造）
- 架构设计：`docs/architecture/新币做空策略回测框架设计.md`
- 策略代码：`strategies/new_coin/`
- 限价单改造测试：`backtest/new_coin/test_limit_order.py`（31 个用例，覆盖配置读取、限价计算、订单类型、容错机制、边界条件）

### 限价单改造测试说明（V4.1）

生产环境 `strategies/new_coin/executor.py` 已将所有市价单改为限价单（开仓 LIMIT、止损 STOP、止盈 TAKE_PROFIT、平仓 LIMIT 带市价回退容错）。测试脚本 `test_limit_order.py` 用于验证限价单改造的正确性：

```bash
# 运行限价单改造测试
python backtest/new_coin/test_limit_order.py
```

测试覆盖范围：
- 配置读取（`limit_order_slippage` 默认值与自定义值）
- 限价计算（开仓、止损、止盈、平仓的限价方向与偏移）
- 订单类型（LIMIT/STOP/TAKE_PROFIT 替代 MARKET/STOP_MARKET/TAKE_PROFIT_MARKET）
- 容错机制（平仓限价失败回退市价单）
- 边界条件（价格为0、滑点为0等）

---

**版本**: V1.2
**更新时间**: 2026-06-25
**作者**: 后端架构师
**变更记录**:
- 2026-06-25（V1.2）：新增限价单改造测试说明，目录结构补充 `test_limit_order.py`
- 2026-06-25（V1.1）：内容更新至 V4.1 信号质量优化版
