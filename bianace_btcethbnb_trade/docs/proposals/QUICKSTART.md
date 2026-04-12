# 快速启动指南

## 系统部署与使用

### 1. 环境准备

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填入你的 API 密钥
```

### 2. 配置文件

编辑 `config/strategy_params.json` 调整策略参数：

```json
{
  "account": {
    "total_capital": "500",
    "single_position_margin": "30",
    "max_positions": "2"
  },
  "risk_management": {
    "take_profit_levels": {
      "tp1_multiplier": "1.5",
      "tp2_multiplier": "2.5",
      "tp1_ratio": "0.3",
      "tp2_ratio": "0.3",
      "tp3_ratio": "0.4"
    }
  }
}
```

### 3. 启动调度器

```bash
# 方式 1: 启动每小时调度器（推荐）
python scheduler_new.py

# 方式 2: 立即执行一次（带自动交易）
python scheduler_new.py --auto-trade

# 方式 3: 立即执行一次（不带自动交易，仅测试）
python scheduler_new.py --dry-run
```

### 4. 运行测试

```bash
# 运行单元测试
python -m pytest tests/test_core_modules.py -v

# 或直接运行
python tests/test_core_modules.py
```

### 5. 使用回测工具

```python
from backtesting import run_backtest
from datetime import datetime
from decimal import Decimal

# 准备历史数据
historical_data = {
    'BTCUSDT': [...],  # K 线数据
}

# 运行回测
report = run_backtest(
    historical_data=historical_data,
    start_date=datetime(2025, 1, 1),
    end_date=datetime(2025, 12, 31),
    initial_capital=Decimal('500')
)

# 查看结果
print(f"总收益率：{report['summary']['total_return']:.1%}")
```

### 6. 生成报告

```python
from reporting import generate_weekly_report

# 生成周报
trades = [...]  # 交易记录列表
report = generate_weekly_report(trades)

# 查看摘要
print(report['summary'])

# 格式化为飞书消息
from reporting import get_performance_reporter
reporter = get_performance_reporter()
message = reporter.format_lark_message(report)
```

## 常见问题

### Q1: 如何调整单笔风险金额？
编辑 `config/strategy_params.json`:
```json
{
  "account": {
    "risk_amount_per_trade": "10"  // 调整为 10U
  }
}
```

### Q2: 如何修改信号等级判定标准？
编辑 `config/strategy_params.json`:
```json
{
  "signal_detection": {
    "grade_thresholds": {
      "S": {"min_score": 90, "max_leverage": 5},
      "A": {"min_score": 80, "max_leverage": 4},
      "B": {"min_score": 70, "max_leverage": 3}
    }
  }
}
```

### Q3: 如何启用 AI 第二意见？
编辑 `config/strategy_params.json`:
```json
{
  "ai": {
    "enable_deepseek": true,
    "deepseek_api_key": "your_api_key",
    "confidence_threshold": "0.7"
  }
}
```

### Q4: 如何查看系统日志？
```bash
# 实时查看日志
tail -f logs/scheduler_new.log

# 查看错误日志
tail -f logs/error.log
```

### Q5: 如何停止调度器？
```bash
# 按 Ctrl+C 停止
```

## 监控与告警

### 飞书通知
系统会自动在以下情况发送飞书通知：
- 每小时分析完成
- 检测到交易信号
- 交易执行成功/失败
- 应急事件触发
- 周报/月报生成

### 日志文件
- `logs/scheduler_new.log` - 主日志
- `logs/error.log` - 错误日志
- `logs/trades.log` - 交易记录

## 下一步

1. **小资金测试**: 建议先用小资金（100-200U）测试 1-2 周
2. **监控表现**: 关注胜率、盈亏比、最大回撤
3. **参数优化**: 根据实际表现微调参数
4. **逐步加仓**: 表现稳定后逐步增加到目标资金

## 技术支持

遇到问题请查看：
- 完整文档：`.trae/documents/重构完成报告_20260401.md`
- 模块接口定义：`.trae/documents/模块接口定义.md`
- 依赖分析：`.trae/documents/现有系统依赖分析.md`
