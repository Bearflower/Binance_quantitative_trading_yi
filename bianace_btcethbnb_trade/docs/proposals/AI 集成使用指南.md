# AI 第二意见集成使用指南

## 概述

DeepSeek AI 第二意见模块提供智能交易建议，作为系统决策的参考。系统会自动对比 AI 建议和系统信号，提供最终执行建议。

## 配置

### 1. 环境变量配置

在 `.env` 文件中添加：

```env
# DeepSeek API 配置
DEEPSEEK_API_KEY=your_api_key_here
ENABLE_DEEPSEEK=true
AI_CONFIDENCE_THRESHOLD=0.7
```

### 2. 策略参数配置

在 `config/strategy_params.json` 中配置：

```json
{
  "ai": {
    "enable_deepseek": true,
    "deepseek_api_key": "your_api_key_here",
    "confidence_threshold": "0.7",
    "agreement_required": "WEAK",  // STRONG/WEAK/DISAGREE
    "max_ai_calls_per_hour": 10
  }
}
```

## 使用方式

### 方式 1: 在调度器中自动使用

调度器已集成 AI 第二意见，会在每次分析时自动调用（如果启用）：

```bash
# 启动调度器（AI 会自动运行）
python scheduler_new.py --auto-trade
```

调度器会：
1. 生成系统信号
2. 调用 AI 获取第二意见
3. 对比两者决策
4. 根据一致性决定是否执行

### 方式 2: 手动调用 AI 顾问

```python
from ai_advisory import (
    get_deepseek_advisor,
    get_second_opinion,
    compare_decisions
)
from decimal import Decimal

# 1. 初始化顾问
advisor = get_deepseek_advisor(api_key='your_api_key')

# 2. 准备数据
signal = {
    '币种': 'BTCUSDT',
    '开仓方向': '多',
    '信号等级': 'A',
    '开仓价': Decimal('95000'),
    '止损价': Decimal('93000'),
    '开仓推荐度': 85
}

market_data = {
    'BTCUSDT': {
        'lastPrice': '95000',
        'priceChangePercent': '2.5',
        'volume': '12345'
    }
}

account_status = {
    'total_capital': '500',
    'available_balance': '450',
    'positions_count': 0
}

# 3. 获取 AI 第二意见
ai_opinion = get_second_opinion(
    signal=signal,
    market_data=market_data,
    account_status=account_status
)

print(f"AI 建议：{ai_opinion['ai_analysis']['direction']}")
print(f"AI 置信度：{ai_opinion['ai_analysis']['confidence']:.0%}")
print(f"AI 理由：{ai_opinion['ai_analysis']['reasoning']}")

# 4. 对比决策
comparison = compare_decisions(
    system_signal=signal,
    ai_analysis=ai_opinion['ai_analysis']
)

print(f"一致性：{comparison['agreement']}")
print(f"建议操作：{comparison['action']}")

# 5. 获取最终建议
final_recommendation = ai_opinion['recommendation']
print(f"最终建议：{final_recommendation}")
```

### 方式 3: 生成 AI 分析报告

```python
from ai_advisory import get_deepseek_advisor

# 初始化顾问
advisor = get_deepseek_advisor()

# 准备多个信号
signals = [
    {
        '币种': 'BTCUSDT',
        '开仓方向': '多',
        '信号等级': 'A',
        '开仓价': Decimal('95000'),
        '止损价': Decimal('93000'),
        '开仓推荐度': 85
    },
    {
        '币种': 'ETHUSDT',
        '开仓方向': '空',
        '信号等级': 'B',
        '开仓价': Decimal('2200'),
        '止损价': Decimal('2250'),
        '开仓推荐度': 70
    }
]

# 生成 AI 分析报告
report = advisor.generate_ai_report(
    signals=signals,
    market_data=market_data,
    account_status=account_status
)

print(f"总信号数：{report['total_signals']}")
print(f"强一致性：{report['statistics']['strong_agreement']}")
print(f"分歧数：{report['statistics']['disagreement']}")
print(f"一致率：{report['statistics']['agreement_rate']:.0%}")
print(f"总体建议：{report['overall_recommendation']}")

# 查看每个信号的 AI 意见
for opinion in report['ai_opinions']:
    print(f"\n{opinion['signal']['币种']}:")
    print(f"  AI 建议：{opinion['ai_analysis']['direction']}")
    print(f"  置信度：{opinion['ai_analysis']['confidence']:.0%}")
    print(f"  一致性：{opinion['comparison']['agreement']}")
```

## 决策逻辑

### 一致性判断

| 系统方向 | AI 方向 | AI 置信度 | 一致性 | 建议操作 |
|---------|--------|----------|--------|---------|
| 多 | BUY | ≥ 70% | STRONG | EXECUTE (执行) |
| 多 | BUY | < 70% | WEAK | CONSIDER (考虑) |
| 多 | HOLD/SELL | 任意 | DISAGREE | REVIEW (重新审查) |
| 空 | SELL | ≥ 70% | STRONG | EXECUTE (执行) |
| 空 | SELL | < 70% | WEAK | CONSIDER (考虑) |
| 空 | HOLD/BUY | 任意 | DISAGREE | REVIEW (重新审查) |

### 执行策略

```python
# 根据 AI 一致性决定是否执行
if comparison['agreement'] == 'STRONG':
    # 强一致性，直接执行
    execute_trade(signal)
    
elif comparison['agreement'] == 'WEAK':
    # 弱一致性，谨慎执行（可降低仓位）
    if signal['开仓推荐度'] >= 80:
        execute_trade(signal, reduced_margin=True)
    else:
        skip_trade(signal)
        
else:  # DISAGREE
    # 分歧，跳过或人工审查
    if signal['信号等级'] == 'S':
        # S 级信号，发送人工审查通知
        send_manual_review_alert(signal)
    else:
        skip_trade(signal)
```

## 实际集成示例

### 集成到调度器

调度器已经集成了 AI 第二意见，你只需要：

1. 配置 API 密钥
2. 启用 AI 功能
3. 启动调度器

```python
# scheduler_new.py 中已集成
class RuleEngineScheduler:
    def __init__(self, enable_auto_trade: bool = False, enable_ai: bool = False):
        # ... 其他初始化 ...
        
        # 初始化 AI 顾问
        if enable_ai:
            from ai_advisory import get_deepseek_advisor
            self.ai_advisor = get_deepseek_advisor()
        else:
            self.ai_advisor = None
    
    def run_analysis(self):
        # ... 信号检测 ...
        
        # 获取 AI 第二意见
        if self.ai_advisor and signals:
            for signal in signals:
                ai_opinion = self.ai_advisor.get_second_opinion(
                    signal=signal,
                    market_data=market_data,
                    account_status=account_status
                )
                
                # 根据 AI 意见调整执行策略
                if ai_opinion['recommendation'] == 'SKIP':
                    logger.info(f"跳过 {signal['币种']} (AI 建议)")
                    continue
                
                # 执行交易
                self.execute_trade(signal)
```

## 自定义提示词

你可以自定义 AI 分析的提示词模板：

```python
from ai_advisory import get_deepseek_advisor

advisor = get_deepseek_advisor()

# 自定义提示词模板
advisor.prompt_templates['second_opinion'] = """
你是专业的加密货币交易分析师，拥有 10 年量化交易经验。

【系统信号】
币种：{symbol}
方向：{direction}
等级：{grade}
开仓价：{entry_price}
止损价：{stop_loss}
推荐度：{recommendation_score}/100

【市场环境】
{market_context}

【账户状态】
{account_status}

请从以下角度分析：
1. 技术面分析（趋势、支撑阻力、指标）
2. 风险评估（回撤、波动率、相关性）
3. 仓位建议（杠杆、保证金分配）
4. 替代方案（更好的入场点或策略）

输出格式：
{{
    "recommendation": "BUY/SELL/HOLD",
    "confidence": 0.0-1.0,
    "reasoning": "详细分析",
    "technical_analysis": "技术面观点",
    "risk_assessment": "风险评估",
    "position_suggestion": "仓位建议",
    "alternative": "替代方案"
}}
"""
```

## 监控 AI 表现

```python
# 记录 AI 建议与实际结果
ai_performance_log = []

def log_ai_prediction(signal, ai_opinion, actual_result):
    """记录 AI 预测与实际结果"""
    log_entry = {
        'timestamp': datetime.now().isoformat(),
        'symbol': signal['币种'],
        'ai_prediction': ai_opinion['ai_analysis']['direction'],
        'ai_confidence': ai_opinion['ai_analysis']['confidence'],
        'actual_outcome': actual_result['outcome'],
        'pnl': actual_result['pnl'],
        'correct': (
            (ai_opinion['ai_analysis']['direction'] == 'BUY' and actual_result['pnl'] > 0) or
            (ai_opinion['ai_analysis']['direction'] == 'SELL' and actual_result['pnl'] < 0)
        )
    }
    ai_performance_log.append(log_entry)

# 统计 AI 准确率
def calculate_ai_accuracy():
    if not ai_performance_log:
        return 0
    
    correct = sum(1 for log in ai_performance_log if log['correct'])
    return correct / len(ai_performance_log)

print(f"AI 准确率：{calculate_ai_accuracy():.1%}")
```

## 注意事项

1. **API 成本**: AI 调用会产生费用，建议设置调用频率限制
2. **响应时间**: AI 分析需要几秒时间，可能影响执行速度
3. **置信度阈值**: 根据实际情况调整阈值（默认 0.7）
4. **人工审查**: 重大决策建议人工审查，不要完全依赖 AI
5. **性能监控**: 定期评估 AI 准确率，调整策略

## 故障排除

### 问题 1: API 调用失败
```python
# 检查 API 密钥
import os
api_key = os.getenv('DEEPSEEK_API_KEY')
if not api_key:
    print("错误：未配置 API 密钥")
```

### 问题 2: 响应解析失败
```python
# 检查响应格式
try:
    ai_response = advisor._call_deepseek_api(prompt)
    analysis = advisor._parse_ai_response(ai_response)
except json.JSONDecodeError as e:
    print(f"JSON 解析失败：{e}")
    print(f"原始响应：{ai_response}")
```

### 问题 3: 置信度过低
```python
# 调整阈值
advisor.confidence_threshold = Decimal('0.5')  # 降低到 50%
```

## 最佳实践

1. ✅ **渐进式集成**: 先用 AI 作为参考，逐步增加依赖
2. ✅ **设置限额**: 限制每小时 AI 调用次数
3. ✅ **监控准确率**: 定期评估 AI 表现
4. ✅ **人工审查**: 重要决策保留人工审查
5. ✅ **日志记录**: 记录所有 AI 建议和结果
6. ✅ **A/B 测试**: 对比使用 AI 和不使用 AI 的表现
