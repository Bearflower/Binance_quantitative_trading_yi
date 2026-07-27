# 网格策略回测模块

本目录包含ETHUSDT网格交易策略的回测引擎和相关工具。

## 📁 目录结构

```
backtest/grid/
├── scripts/              # 回测脚本
│   ├── backtest_grid.py           # 回测引擎主程序
│   └── backtest_visualization.py  # 可视化模块
├── reports/              # 回测报告
│   ├── ETHUSDT_detailed_report.md # 详细分析报告
│   ├── ETHUSDT_equity_curve.png   # 权益曲线图
│   └── ETHUSDT_trade_analysis.png # 交易分析图
└── README.md             # 本文档
```

## 🚀 快速开始

### 运行回测

```bash
cd /Users/yl/vscode/Binance_quantitative_trading/backtest/grid/scripts
python3 backtest_grid.py
```

### 查看报告

回测完成后，报告会自动生成在 `reports/` 目录下：

- **基础报告**: `ETHUSDT_backtest_report_YYYYMMDD_HHMMSS.md`
- **详细报告**: `ETHUSDT_detailed_report.md`
- **可视化图表**: `ETHUSDT_equity_curve.png`, `ETHUSDT_trade_analysis.png`

## 📊 回测结果摘要

### 最近回测结果 (2026-05-09)

| 指标 | 数值 |
|------|------|
| 总收益率 | -99.95% |
| 最大回撤 | 100.34% |
| 夏普比率 | 1.58 |
| 胜率 | 71.46% |
| 盈亏比 | 0.01 |
| 总交易次数 | 2176 |

### 关键发现

#### 优势
- 胜率较高（71.46%）
- 在震荡市场中表现稳定
- 最大连续盈利次数达1059次

#### 问题
- 极端亏损（-99.95%）
- 盈亏比严重失衡（0.01）
- 风险控制缺失
- 参数设置不合理

## ⚙️ 配置说明

回测引擎使用的配置文件位于：
```
/strategies/grid/config.yaml
```

主要配置项：
- **网格参数**: 网格类型、数量、间距等
- **市场状态检测**: ADX阈值、EMA周期等
- **风险控制**: 最大回撤、止损比例等
- **交易参数**: 杠杆、保证金、手续费等

## 📈 回测引擎功能

### 核心功能
1. **数据加载**: 支持多时间框架K线数据（15m, 1h, 4h）
2. **指标计算**: ADX, EMA, ATR等技术指标
3. **市场状态检测**: 震荡/上升/下降趋势识别
4. **网格交易模拟**: 完整的网格交易逻辑
5. **风险控制**: 回撤监控、止损机制
6. **报告生成**: Markdown报告 + 可视化图表

### 技术特点
- 使用Decimal进行精确计算
- 支持动态网格参数调整
- 考虑手续费和滑点
- 完整的交易记录和权益曲线

## 🔧 自定义回测

### 修改参数

编辑 `/strategies/grid/config.yaml` 文件：

```yaml
# 网格配置示例
grid:
  type: dynamic
  count: 20
  spacing: 100
  min_grid_count: 8
  max_grid_count: 30

# 风险控制示例
risk:
  max_drawdown: 0.1
  stop_loss_percent: 0.1
```

### 修改回测逻辑

编辑 `backtest_grid.py` 文件中的相关方法：

- `initialize_grid()`: 网格初始化逻辑
- `check_grid_orders()`: 订单检查逻辑
- `check_risk()`: 风险控制逻辑
- `_calculate_metrics()`: 指标计算逻辑

## 📋 优化建议

基于回测结果，建议进行以下优化：

### 1. 参数优化
- 增加网格间距至ATR的2-3倍
- 减少网格数量至10-15个
- 确保每格利润率>1%

### 2. 风险控制
- 设置总资金止损线（-20%）
- 限制单次交易仓位（<5%）
- 添加每日最大亏损限制

### 3. 市场适应性
- 在强趋势市场（ADX>40）暂停网格
- 优化网格重置条件
- 实现动态参数调整

## 📚 相关文档

- [策略配置文档](../../strategies/grid/config.yaml)
- [策略实现代码](../../strategies/grid/strategy.py)
- [网格计算器](../../strategies/grid/grid_calculator.py)
- [市场状态检测](../../strategies/grid/market_state.py)

## ⚠️ 重要提示

1. **当前策略参数存在严重问题**，不建议直接用于实盘交易
2. 请根据优化建议调整参数后，在模拟环境中充分测试
3. 回测结果仅供参考，实盘交易需谨慎评估风险

## 📞 联系方式

如有问题或建议，请联系项目维护人员。

---
**最后更新**: 2026-05-09
**维护者**: 资深Python工程师
