# 网格交易信号灯系统 V2.0

基于趋势识别的自适应网格交易信号灯系统（半自动模式）

## 🎯 项目简介

网格交易信号灯系统 V2.0 是一个**半自动信号灯系统**，用于币安永续合约市场。系统自动分析市场状态，计算最优网格参数，并通过飞书推送可执行的操作指令，用户根据指令手动在币安网页端创建或修改网格。

### 核心特性

- 🎯 **市场状态识别**: 自动识别震荡、上升趋势、下降趋势、强趋势暂停
- 📊 **自适应网格**: 根据市场状态和波动率动态计算网格参数
- 💰 **仓位验证**: 检查每格最小合约张数，给出资金建议
- 📤 **信号推送**: 通过飞书推送完整的操作指令
- 🗄️ **数据持久化**: PostgreSQL 存储历史信号和市场状态

## 🚀 快速开始

### 1. 环境要求

- Python 3.8+
- PostgreSQL 12+
- Docker（可选）

### 2. 安装

```bash
# 克隆项目
cd grid_signal_bot_v2

# 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt
```

### 3. 配置

```bash
# 复制配置模板
cp config/.env.template config/.env

# 编辑配置文件
vim config/.env
```

配置以下关键参数：

```ini
# 数据库连接
DATABASE_URL=postgresql://grid_user:password@localhost:5432/grid_trading

# K 线服务地址
KLINE_SERVICE_URL=http://localhost:8000

# 推送服务地址
NOTIFICATION_SERVICE_URL=http://localhost:8766

# 定时运行配置
RUN_MINUTE=35                    # 每小时的指定分钟运行（0-59）
REST_HOURS=0,1,2,3,4,5          # 休息时间段（凌晨00:00-06:00休息）
```

### 4. 数据库初始化

```bash
# 执行数据库迁移
psql -U grid_user -d grid_trading -f migrations/001_create_initial_tables.sql
```

### 5. 运行

```bash
# 单次运行（测试）
python src/main.py --once

# 循环运行（生产）
python src/main.py
```

## 📖 使用指南

### 工作流程

1. **定时巡检**: 每小时的35分自动巡检（凌晨00:00-06:00休息）
2. **市场分析**: 分析 1H 和 4H K 线，判断市场状态
3. **参数计算**: 根据市场状态计算最优网格参数
4. **仓位验证**: 检查资金可行性
5. **信号推送**: 通过飞书推送操作指令
6. **手动执行**: 用户在币安网页端手动创建网格

### 定时运行说明

系统采用定时运行模式，具有以下特点：

- **运行时间**: 每小时的35分运行（可配置）
- **休息时间段**: 凌晨00:00-06:00不运行（可配置）
- **智能跳过**: 如果运行时间在休息时间段内，自动跳到下一个非休息时间

**运行时间示例**：

| 时间 | 状态 |
|------|------|
| 00:35 | 😴 休息 |
| 01:35 | 😴 休息 |
| 02:35 | 😴 休息 |
| 03:35 | 😴 休息 |
| 04:35 | 😴 休息 |
| 05:35 | 😴 休息 |
| 06:35 | ✅ 运行 |
| 07:35 | ✅ 运行 |
| ... | 每小时35分 |
| 23:35 | ✅ 运行 |

**配置说明**：

```bash
# 修改运行时间（例如：每小时的20分运行）
RUN_MINUTE=20

# 修改休息时间段（例如：凌晨00:00-07:00休息）
REST_HOURS=0,1,2,3,4,5,6

# 取消休息时间段（全天运行）
REST_HOURS=
```

### 推送内容

推送消息包含以下信息：

- 📊 当前市场数据（价格、ATR、ADX）
- 📐 建议网格参数（价格区间、网格数量、网格模式）
- 🎯 止盈止损价格
- 📈 上移/下移功能（如适用）
- 💰 资金可行性提醒
- 💡 详细操作指令

## 🏗️ 项目结构

```
grid_signal_bot_v2/
├── src/
│   ├── main.py                    # 主程序入口
│   ├── core/                      # 核心模块
│   │   ├── market_analyzer.py     # 市场状态分析器
│   │   ├── grid_calculator.py     # 网格参数计算器
│   │   ├── position_validator.py  # 仓位验证器
│   │   └── parameter_comparator.py # 参数对比器
│   ├── data/                      # 数据模块
│   │   ├── database.py            # 数据库管理
│   │   └── kline_client.py        # K 线服务客户端
│   ├── notification/              # 通知模块
│   │   └── notification_client.py # 推送服务客户端
│   └── utils/                     # 工具模块
│       └── config.py              # 配置管理
├── config/                        # 配置文件
│   ├── config.yaml                # 主配置文件
│   └── .env.template              # 环境变量模板
├── migrations/                    # 数据库迁移
│   └── 001_create_initial_tables.sql
├── tests/                         # 测试文件
├── requirements.txt               # Python 依赖
└── README.md                      # 项目文档
```

## ⚙️ 配置说明

### 策略参数

```yaml
strategy:
  grid:
    base_grid_count: 30      # 基准网格数量
    min_grid_count: 5        # 最小网格数量
    max_grid_count: 50       # 最大网格数量
    min_profit_rate: 0.01    # 每格最小利润率 1%
  
  indicators:
    adx_period: 14           # ADX 周期
    adx_weak_threshold: 20   # ADX 弱趋势阈值
    adx_trend_threshold: 25  # ADX 趋势确认阈值
    adx_strong_threshold: 40 # ADX 强趋势暂停阈值
```

### 触发条件

```yaml
triggers:
  grid_width_change: 0.05      # 网格宽度变化 > 5%
  grid_count_change: 0.10      # 网格数量变化 > 10%
  atr_change: 0.20             # ATR 变化 > 20%
  profit_rate_warning: 0.012   # 每格利润率 < 1.2%
```

## 🧪 测试

```bash
# 运行单元测试
pytest tests/ -v

# 运行测试并生成覆盖率报告
pytest tests/ --cov=src --cov-report=html
```

## 🐳 Docker 部署

```bash
# 构建镜像
docker build -t grid-signal-bot:v2.0 .

# 运行容器
docker run -d \
  --name grid-signal-bot \
  --env-file config/.env \
  grid-signal-bot:v2.0
```

## 📊 数据库表结构

### grid_signals - 信号推送历史

| 字段 | 类型 | 说明 |
|------|------|------|
| id | SERIAL | 主键 |
| signal_time | TIMESTAMP | 信号时间 |
| market_state | VARCHAR(20) | 市场状态 |
| symbol | VARCHAR(20) | 交易对 |
| grid_params | JSONB | 网格参数 |
| is_pushed | BOOLEAN | 是否已推送 |

### market_states - 市场状态历史

| 字段 | 类型 | 说明 |
|------|------|------|
| id | SERIAL | 主键 |
| check_time | TIMESTAMP | 检查时间 |
| symbol | VARCHAR(20) | 交易对 |
| state | VARCHAR(20) | 市场状态 |
| adx | DECIMAL(5,2) | ADX 值 |
| trend_strength | DECIMAL(5,4) | 趋势强度系数 |

## ⚠️ 风险提示

- 本系统仅供学习研究使用
- 网格交易非保本，单边行情可能导致亏损
- 杠杆风险：高杠杆会放大亏损
- 手动操作延迟：价格可能在推送后快速变化
- 请勿用于真实交易，除非您完全理解相关风险

## 📝 更新日志

### V2.0 (2026-04-24)

- ✨ 全新半自动信号灯模式
- ✨ 强趋势暂停机制（ADX≥40）
- ✨ 仓位验证和资金建议
- ✨ 多时间框架确认
- ✨ 移动止盈机制
- ✨ 定时运行模式（每小时的指定分钟运行）
- ✨ 休息时间段配置（凌晨00:00-06:00休息）
- ✨ 本地技术指标计算（ADX、EMA、ATR）
- 🗑️ 移除全自动交易功能

## 📄 许可证

MIT License

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

---

**最后更新**: 2026-04-24
