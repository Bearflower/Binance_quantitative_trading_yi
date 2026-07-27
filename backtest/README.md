# 回测模块

本目录存储统一交易系统中各策略的回测相关内容。

## 📁 目录结构

```
backtest/
├── btc_eth/          # BTC/ETH策略回测
│   ├── reports/      # 回测报告
│   ├── scripts/      # 回测脚本
│   └── data/         # 回测数据
├── grid/             # 网格策略回测（预留）
└── new_coin/         # 新币策略回测（预留）
```

## 📖 使用说明

### 运行回测

```bash
cd backtest/btc_eth/scripts
python backtest_btc_eth_local.py
```

### 查看报告

回测报告保存在 `backtest/btc_eth/reports/` 目录下，按日期命名。

## 🔗 相关文档

- [业务需求文档](../docs/requirements/)
- [策略实现代码](../strategies/)
