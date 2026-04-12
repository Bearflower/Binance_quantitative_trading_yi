# 自适应趋势网格策略系统

基于趋势识别的自适应网格交易策略，用于币安永续合约市场。

## 核心特性

- 🎯 **市场状态识别**: 自动识别震荡、上升趋势、下降趋势
- 📊 **自适应网格**: 根据市场状态和波动率动态调整网格参数
- 🛡️ **多层次风控**: 硬止损、移动止盈、紧急暂停、动态仓位
- ⚡ **异步架构**: 基于 asyncio + aiohttp 的高性能异步事件驱动
- 📈 **实时监控**: 飞书/钉钉/Telegram 报警，详细的日志和性能指标
- 🔧 **参数自动调整**: 6 个可调整参数，无需终止重建网格
- 🚀 **自动化部署**: 一键部署到远程服务器，Docker 容器化管理

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                      自适应趋势网格策略系统                   │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐       │
│  │  数据层  │  │  策略层  │  │  执行层  │  │  监控层  │       │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘       │
└─────────────────────────────────────────────────────────────┘
```

### 组件说明

- **数据层**: 币安 API 客户端、K 线数据管理、技术指标计算
- **策略层**: 市场状态识别、网格参数计算、风险管理
- **执行层**: 网格管理、订单执行、任务调度
- **监控层**: 日志管理、报警通知、性能监控

## 快速开始

### 1. 环境要求

- Python 3.8+
- Linux/MacOS
- 稳定的网络连接
- 币安 API 密钥

### 2. 安装

```bash
# 进入项目目录
cd adaptive_grid_trading

# 创建虚拟环境
python -m venv venv

# 激活虚拟环境
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt
```

### 3. 配置

```bash
# 复制配置模板
cp config/config.yaml.template config/config.yaml
cp config/.env.template config/.env

# 编辑配置文件，填入币安 API 密钥
vim config/.env
```

### 4. 运行

```bash
# 开发模式
python src/main.py

# 后台运行
nohup python src/main.py > logs/app.log 2>&1 &

# 或使用启动脚本
./scripts/start.sh
```

## 项目结构

```
adaptive_grid_trading/
├── README.md                 # 项目说明
├── requirements.txt          # Python 依赖
├── config/                   # 配置文件目录
│   ├── config.yaml.template  # 配置模板
│   └── .env.template         # 环境变量模板
├── src/                      # 源代码目录
│   ├── main.py               # 程序入口
│   ├── data/                 # 数据层
│   ├── strategy/             # 策略层
│   ├── execution/            # 执行层
│   ├── monitoring/           # 监控层
│   └── utils/                # 工具函数
├── tests/                    # 测试目录
├── scripts/                  # 脚本目录
├── logs/                     # 日志目录
├── data/                     # 数据目录
│   └── database.db           # SQLite 数据库
└── docs/                     # 文档目录
```

## 核心功能

### 1. 市场状态识别

通过 ADX、EMA 等指标自动识别市场状态：

- **震荡**: ADX < 20
- **上升趋势**: ADX ≥ 25 且 EMA 快线 > EMA 慢线
- **下降趋势**: ADX ≥ 25 且 EMA 快线 < EMA 慢线
- **多周期确认**: 1H+4H 趋势一致才行动

### 2. 自适应网格参数

根据市场状态和波动率动态调整：

- **网格边界**: 基于 ATR 的趋势偏移
- **网格数量**: 根据波动率自适应（20-50 个）
- **网格方向**: 震荡双向，趋势单向

### 3. 参数自动调整

**6 个可调整参数**（通过终止重建方式）：

1. 网格价格范围（上边界/下边界）
2. 网格数量
3. 停止上移价格
4. 停止下移价格
5. 网格终止最低价格（硬止损线）
6. 网格终止最高价格（硬止盈线）

**保守策略**（2026-03-20 优化）：

为避免频繁操作和减少手续费，系统采用保守的调整策略：

- **ATR 变化阈值**: 20% → 35%（大幅提高）
- **边界接近阈值**: 0.5×ATR → 1.5×ATR（3 倍）
- **价格偏离检查**: 新增 > 10% 才调整
- **终止价格偏离**: 新增 > 15% 才调整
- **市场状态确认**: 需要连续 3 次确认
- **触发严重性**: 必须 > 0.7（忽略轻微触发）
- **极端情况保护**: 突破 10% 或 ATR±50% 立即调整

**效果对比**：

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| 调整频率 | 每日 3-6 次 | 每周 1-3 次 |
| 手续费 | 100% | 20-30%（节省 70-80%） |
| 监控频率 | 每日 3-6 次 | 每日 3-6 次（保持不变） |

**触发条件**（优化后）：

- ✅ ATR 变化 > 35%（原 20%）
- ✅ 市场状态连续变化 3 次（原单次）
- ✅ 价格偏离网格中心 > 10%（新增）
- ✅ 终止价格偏离 > 15%（新增）
- ✅ 触发严重性 > 0.7（新增）
- ⚡ 极端情况（突破 10%、ATR±50%）立即调整

### 4. 风险管理

- **硬止损**: 总亏损 ≥ 8%
- **移动止盈**: 盈利≥15% 启动，回撤 50% 触发
- **紧急暂停**: 5 分钟内突破 3 层网格
- **动态仓位**: 基于波动率调整
- **滑点保护**: 限价单→最优价→市价单

## 配置说明

### 策略参数配置

```yaml
strategy:
  indicators:
    adx_period: 14
    adx_trend_threshold: 25
    atr_period: 14
    atr_smoothing: 14
  
  grid:
    base_grid_count: 30
    min_grid_count: 20
    max_grid_count: 50
  
  risk:
    hard_stop_loss: -0.08
    trailing_profit_start: 0.15
    trailing_profit_retrace: 0.5
```

### 参数调整配置

**保守模式配置**（推荐）：

```yaml
execution:
  parameter_adjustment:
    enabled: true
    conservative_mode: true  # 启用保守模式
    min_interval: 14400  # 4 小时
    max_adjustments_per_day: 6
    
    # 保守模式阈值
    atr_change_threshold: 0.35  # ATR 变化 > 35%
    price_deviation_threshold: 0.10  # 价格偏离 > 10%
    terminate_deviation_threshold: 0.15  # 终止价格偏离 > 15%
    state_confirm_count: 3  # 状态连续确认 3 次
    edge_approach_threshold: 1.5  # 边界接近 < 1.5×ATR
    min_trigger_severity: 0.7  # 最小触发严重性
```

**关闭保守模式**（如需恢复敏感模式）：

```yaml
execution:
  parameter_adjustment:
    conservative_mode: false  # 关闭保守模式
```

## 监控报警

支持多种报警渠道：

- **飞书机器人**（推荐）
- **钉钉机器人**
- **Telegram Bot**

配置方式：编辑 `config/.env` 文件，填入对应的 webhook 地址。

## 测试

```bash
# 运行单元测试
pytest tests/ -v

# 运行测试并生成覆盖率报告
pytest tests/ --cov=src --cov-report=html

# 查看覆盖率报告
open htmlcov/index.html
```

## 开发

### 代码规范

```bash
# 格式化代码
black src/ tests/

# 检查代码规范
flake8 src/ tests/

# 类型检查
mypy src/
```

### 运行测试

```bash
# 运行所有测试
pytest

# 运行特定测试
pytest tests/test_market_state.py -v
```

## 部署

### 本地部署

```bash
# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp config/.env.template config/.env
vim config/.env

# 启动服务
python src/main.py
```

### Docker 部署（推荐）

```bash
# 构建镜像
docker-compose build

# 启动容器
docker-compose up -d

# 查看日志
docker-compose logs -f

# 停止容器
docker-compose down
```

### 服务器自动化部署

系统支持一键自动化部署到远程服务器：

```bash
# 1. 配置服务器信息
vim .deploy_config

# 2. 执行一键部署（打包 + 上传 + 部署）
./one_click_deploy.sh
```

**部署流程**：
1. 自动打包项目文件（排除不必要的文件）
2. 通过 SSH 上传到远程服务器
3. 远程构建 Docker 镜像
4. 启动容器并验证健康状态

**管理命令**：

```bash
# 查看系统状态
./scripts/check_status.sh

# 查看容器状态
ssh root@<server_ip> "docker ps -f name=grid-trading"

# 查看实时日志
ssh root@<server_ip> "docker logs -f grid-trading"

# 重启容器
ssh root@<server_ip> "docker restart grid-trading"

# 查看资源使用
ssh root@<server_ip> "docker stats grid-trading"
```

详见 [docs/DEPLOYMENT_REPORT.md](docs/DEPLOYMENT_REPORT.md)

## 文档

### 项目文档
- [产品需求文档](../memory-bank/product-requirements.md)
- [架构设计文档](../memory-bank/architecture.md)
- [实施计划](../memory-bank/implementation-plan.md)
- [进度跟踪](../memory-bank/progress.md)
- [技术选型](../memory-bank/tech-stack.md)

### 部署文档
- [部署报告](docs/DEPLOYMENT_REPORT.md) - 线上部署详细报告和运行状态
- [服务器部署指南](docs/server_deployment.md)
- [生产环境部署](docs/production_deployment.md)
- [保守策略部署报告](docs/CONSERVATIVE_STRATEGY_DEPLOYED.md) - 保守策略优化部署详情

### 策略优化文档
- [调整策略优化方案](docs/ADJUSTMENT_STRATEGY_OPTIMIZATION.md) - 详细的优化设计方案
- [策略优化完成报告](docs/STRATEGY_OPTIMIZATION_COMPLETE.md) - 优化实施总结
- [网格操作指南](docs/GRID_OPERATIONS_GUIDE.md) - 网格操作和管理手册

## 常见问题

### Q: 如何申请币安 API 密钥？

A: 访问币安官网 → 账户管理 → API 管理 → 创建 API，勾选"允许合约交易"权限。

### Q: 测试网和主网如何切换？

A: 修改 `config/config.yaml` 中的 `exchange.testnet` 字段：
- `testnet: true` - 测试网
- `testnet: false` - 主网

### Q: 如何查看运行日志？

A: 日志文件位于 `logs/adaptive_grid.log`，可使用以下命令查看：
```bash
tail -f logs/adaptive_grid.log
```

### Q: 参数调整会影响现有网格吗？

A: 币安不支持直接修改网格参数的 API。系统采用 **switch_grid** 方式（终止旧网格 + 创建新网格）实现参数调整。虽然会产生交易费用，但通过保守策略已大幅减少调整频率（节省 70-80% 手续费）。

### Q: 什么是保守策略？

A: 保守策略是 2026-03-20 优化的参数调整策略，通过提高触发阈值来减少频繁操作：

- **调整频率**: 从每日 3-6 次降低到每周 1-3 次
- **手续费节省**: 约 70-80%
- **监控频率**: 保持每日 3-6 次（不变）
- **极端情况**: 保留快速调整机制（突破 10%、ATR±50% 立即调整）

### Q: 如何调整保守策略的阈值？

A: 修改 `config/config.yaml` 中的 `execution.parameter_adjustment` 配置：

```yaml
execution:
  parameter_adjustment:
    price_deviation_threshold: 0.10  # 调整价格偏离阈值
    atr_change_threshold: 0.35       # 调整 ATR 变化阈值
    state_confirm_count: 3           # 调整状态确认次数
```

建议先观察 1-2 周，根据实际调整频率微调阈值。

## 风险提示

⚠️ **交易有风险，投资需谨慎**

- 本系统仅供学习研究使用
- 请勿用于真实交易，除非您完全理解相关风险
- 过往表现不代表未来收益
- 使用本系统进行交易的一切风险由用户自行承担

## 许可证

MIT License

## 联系方式

如有问题，请通过以下方式联系：

- GitHub Issues
- 项目讨论区

---

**最后更新**: 2026-03-20
