import os
from decimal import Decimal
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# DeepSeek API Configuration
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', 'your_api_key_here')
DEEPSEEK_MODEL = os.getenv('DEEPSEEK_MODEL', 'deepseek-chat')
DEEPSEEK_API_BASE = os.getenv('DEEPSEEK_API_BASE', 'https://api.deepseek.com/v1')

# Binance Configuration - Default URLs
BINANCE_CONTRACT_URLS = {
    'BTCUSDT': 'https://www.binance.com/en/futures/BTCUSDT',
    'ETHUSDT': 'https://www.binance.com/en/futures/ETHUSDT',
    'BNBUSDT': 'https://www.binance.com/en/futures/BNBUSDT'
}

# Scheduling Configuration
SCHEDULE_TIME = os.getenv('SCHEDULE_TIME', '08:30')
TIMEZONE = os.getenv('TIMEZONE', 'Asia/Shanghai')

# Analysis Prompt Template
ANALYSIS_PROMPT_TEMPLATE = os.getenv('ANALYSIS_PROMPT_TEMPLATE', """【核心指令】你必须严格按照以下要求执行分析并输出报告：

## 一、分析任务
基于提供的 Binance 实时数据和 traderule.txt 交易规则，对 BTCUSDT、ETHUSDT、BNBUSDT 三个交易对进行全面分析，并**严格按照指定的五章结构输出报告**。

## 二、账户信息（500U 阶段一·全仓版）
- 总资金：500U
- 单笔风险金额：固定 10U（总资金的 2%）
- 最大总保证金占用：150U（30%）
- 允许同时持仓：≤ 2 个品种
- 允许交易信号等级：S 级 + A 级（禁止 B 级）
- 初始杠杆：≤ 5 倍，浮盈后可至 8 倍

## 三、报告格式要求（⚠️ 必须严格遵守，不得更改）

⚠️ **重要提示**：你必须**严格按照以下五章结构**输出分析报告，**不得更改章节标题和顺序，不得省略任何章节**。如果违反格式要求，报告将被视为无效。

### 第一章：市场概览与分析背景
- 分析时间（格式：YYYY-MM-DD HH:MM:SS）
- 分析币种（列出所有分析的币种）
- 当前市场环境概述（整体趋势判断）

### 第二章：各币种详细技术分析
#### 2.1 BTCUSDT 分析
- K 线形态识别
- 关键支撑位与阻力位
- 技术指标分析（EMA21、ATR14、RSI 等）
- 市场结构判断
- 资金费率分析
- 交易深度评估

#### 2.2 ETHUSDT 分析
- K 线形态识别
- 关键支撑位与阻力位
- 技术指标分析（EMA21、ATR14、RSI 等）
- 市场结构判断
- 资金费率分析
- 交易深度评估

#### 2.3 BNBUSDT 分析
- K 线形态识别
- 关键支撑位与阻力位
- 技术指标分析（EMA21、ATR14、RSI 等）
- 市场结构判断
- 资金费率分析
- 交易深度评估

### 第三章：最终开仓建议与风险管理
#### 3.1 综合开仓建议
- 开仓方向（多/空/观望）
- 开仓推荐度（0-100 分）
- 推荐开仓币种及优先级
- 信号等级评估

#### 3.2 具体交易参数
- 开仓价（限价）
- 止损价
- 分批止盈价及对应仓位比例
- 开仓数量（或名义价值）
- 杠杆倍数
- 保证金占用
- 强平风险评估

#### 3.3 风险评估
- 主要风险点
- 风险应对措施
- 最大可接受亏损
- 双重止损设置

#### 3.4 JSON 格式交易建议（⚠️ 必须包含，用于自动交易执行）
⚠️ **重要**：在第三章末尾，你必须使用 ```json 代码块输出交易建议列表，格式如下：

```json
[
    {
        "币种": "BTCUSDT",
        "开仓方向": "多",
        "开仓推荐度": 85,
        "信号等级": "A",
        "开仓价": 95000,
        "强平价": 85000,
        "止损价": 93000,
        "止盈设置": {
            "TP1": {"价格": 97000, "仓位比例": "50%"},
            "TP2": {"价格": 99000, "仓位比例": "30%"},
            "TP3": {"价格": 100000, "仓位比例": "20%"}
        },
        "保证金": 30,
        "实际杠杆": 5,
        "风险占比": "2%",
        "通过检查清单": true,
        "备注": "符合 500U 阶段一交易规则"
    }
]
```

**字段说明**：
- 如果不建议开仓某个币种，开仓方向填写"观望"，信号等级填写"无"
- 止盈设置必须包含 TP1、TP2、TP3 三个层级
- 所有价格必须是具体数值，不能使用"N/A"或空值
- 通过检查清单：true/false，表示是否符合所有交易规则

### 第四章：交易操作总结（固定篇章，必须包含）
#### 4.1 核心观点提炼
用 1-2 句话概括本次分析的核心结论

#### 4.2 操作建议摘要
- **开仓方向**：[多/空/观望]
- **推荐度**：[0-100 分]
- **建议开仓币种**：[具体币种]
- **杠杆倍数**：[具体倍数]
- **保证金占用**：[具体金额]

#### 4.3 关键价位速查
- **入场价**：[具体价格]
- **止损价**：[具体价格]
- **止盈价**：[TP1/TP2/TP3 具体价格]
- **幽灵止损**：[具体价格]

#### 4.4 执行要点
- 最佳入场时机
- 需要重点监控的信号
- 突发情况应对策略
- 自动化执行流程

#### 4.5 风险警示
- 主要风险因素
- 风险等级评估（高/中/低）
- 建议风控措施
- 保证金率预警

#### 4.6 后续跟踪计划
- 下次分析时间建议
- 需要重点关注的市场变化
- 可能的策略调整方向
- 利润提取计划

## 四、分析依据
请严格基于提供的 traderule.txt（500U 阶段一·全仓版专用规则）进行系统化分析，所有建议必须符合 500U 阶段一的全仓特规要求。

## 五、重要提示
1. ⚠️ **必须按照上述五章结构输出，不得省略任何章节**
2. ⚠️ **第四章"交易操作总结"为固定篇章，必须包含所有 6 个子项（4.1-4.6）**
3. ⚠️ **第三章必须包含 3.4 节的 JSON 代码块，这是自动交易执行的必要条件**
4. 语言简洁明了，数据准确无误
5. 如果某个币种不适合开仓，明确标注"观望"并说明理由
6. 请基于提供的 traderule.txt 规则进行系统化分析
7. 所有参数计算必须符合全仓模式的仓位计算公式
8. 严格遵守禁止交易情形的要求
9. ⚠️ **再次强调：报告格式必须严格遵循上述五章结构，否则视为无效输出**
10. ⚠️ **JSON 代码块中的交易建议将直接用于自动下单，请确保数据准确完整**
""")

# File Paths
TRADE_RULE_DOCX_PATH = os.getenv('TRADE_RULE_DOCX_PATH', './trade_rule.docx')
TRADE_RULE_500U_PATH = os.getenv('TRADE_RULE_500U_PATH', './500Utrade_rule_v3.0.md')
SCREENSHOT_OUTPUT_DIR = os.getenv('SCREENSHOT_OUTPUT_DIR', './data/screenshots')
REPORT_OUTPUT_DIR = os.getenv('REPORT_OUTPUT_DIR', './reports')
LOG_DIR = os.getenv('LOG_DIR', './logs')

# Lark Notification
LARK_WEBHOOK_URL = os.getenv('LARK_WEBHOOK_URL', '')

# Supported currencies
SUPPORTED_CURRENCIES = os.getenv('SUPPORTED_CURRENCIES', 'BTCUSDT,ETHUSDT,BNBUSDT').split(',')

# 500U 阶段一账户配置
TOTAL_CAPITAL = 500  # 当前总资金（阶段一）
SINGLE_POSITION_MARGIN = 30  # 单仓保证金（U），固定为 30U
MAX_POSITIONS = 2  # 同时最大持仓数
RESERVE_CAPITAL = 400  # 预留备用金（U）
SCHEDULE_INTERVAL_HOURS = 4  # 运行周期间隔（小时）

# 自动化交易配置
ENVIRONMENT = os.getenv('ENVIRONMENT', 'production')  # development/production
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
BINANCE_SECRET_KEY = os.getenv('BINANCE_SECRET_KEY', '')
BINANCE_API_BASE_URL = os.getenv('BINANCE_API_BASE_URL', 'https://papi.binance.com')
BINANCE_TESTNET = os.getenv('BINANCE_TESTNET', 'false').lower() == 'true'

# 交易参数配置
LEVERAGE = int(os.getenv('LEVERAGE', '20'))  # 默认杠杆倍数
MAX_SINGLE_TRADE_AMOUNT = Decimal(os.getenv('MAX_SINGLE_TRADE_AMOUNT', '100'))  # 单笔最大交易金额
MAX_DAILY_TRADES = int(os.getenv('MAX_DAILY_TRADES', '10'))  # 每日最大交易次数

# 监控配置
MONITORING_INTERVAL_MINUTES = int(os.getenv('MONITORING_INTERVAL_MINUTES', '15'))  # 监控间隔 (分钟)
PROFIT_TAKE_THRESHOLD = Decimal(os.getenv('PROFIT_TAKE_THRESHOLD', '0.02'))  # 止盈阈值 (2%)
STOP_LOSS_THRESHOLD = Decimal(os.getenv('STOP_LOSS_THRESHOLD', '0.01'))  # 止损阈值 (1%)
LIQUIDATION_WARNING_LEVEL = Decimal(os.getenv('LIQUIDATION_WARNING_LEVEL', '0.1'))  # 强平警告线 (10%)

# 数据库配置
DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://bianace_user:Bianace@2024@postgres:5432/trading_platform?schema=schema_bianace')
