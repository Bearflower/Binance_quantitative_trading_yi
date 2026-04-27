# 币安新币精准做空系统 v3.1

**版本**: v3.1  
**发布日期**: 2026-04-21  
**核心特性**: 基于 1 小时 K 线的技术面分析 + 三次冲顶形态识别

---

## 🎯 v3.1 版本概述

### 核心改进

1. **调整评分触发条件**
   - 取消每 30 分钟评分
   - 改为**每小时第 1 分钟**评分（10:01, 11:01, 12:01...）
   - 评分仅基于**刚刚收盘的那根 1 小时 K 线**

2. **三次冲顶判断逻辑优化**
   - 跨 K 线判断：记录最近 3-5 根已收盘的 1 小时 K 线的最高点
   - 支持两种形态：同一水平受阻、高点逐次降低
   - 不需要等待当前 K 线收盘

3. **成交量比较基准调整**
   - 用**前 5 根已收盘的 1 小时 K 线**的平均成交量作为基准
   - 当前 K 线成交量 > 1.5 倍基准 → 放量
   - 价格未创新高 → 滞涨
   - 放量 + 滞涨 → 放量滞涨信号

4. **信号冷却机制**
   - 如果某个币种在连续 2 个整点评分中都满足条件，只开仓一次
   - 开仓后立即标记为"已交易"
   - **2 小时冷却期**内不再评分

5. **时间窗口限制**
   - 新币上线**48 小时内**才参与评分
   - 超过 48 小时自动移除

### 技术面评分 v3.1

**评分公式**:
```
技术面评分 = 趋势评分 (4 分) + RSI 评分 (3 分) + 波动率评分 (3 分) 
           + 形态加分 (0-1 分) + 量价加分 (0-1 分)

满分：10 分
```

**开仓阈值**: ≥ 6.0 分

---

## 📁 项目结构

```
short_selling_system/
├── core/                           # 核心模块
│   ├── technical_analyzer_v31.py   # 技术分析器 v3.1 ⭐
│   ├── signal_manager.py           # 信号管理器（带冷却机制）
│   ├── scheduler.py                # 调度器（每小时第 1 分钟）
│   ├── scoring_engine.py           # 评分引擎
│   ├── binance_client.py           # 币安客户端
│   └── notifier.py                 # 通知服务
├── backtesting/                    # 回测模块
│   ├── backtest_v31_simple.py      # v3.1 回测脚本
│   └── analyze_pnl_v31.py          # v3.1 盈亏分析
├── scripts/                        # 脚本模块
│   ├── fetch_new_coins_klines_v2.py # 数据获取脚本
│   └── main_v31.py                 # 主程序 v3.1
├── data/                           # 数据文件
│   ├── new_coins_backtest.json     # 回测数据
│   └── signals.json                # 信号状态
├── results/                        # 回测结果
│   ├── backtest_v31_newcoins.json  # v3.1 回测结果
│   └── backtest_v31_pnl_report.json # v3.1 盈亏报告
├── logs/                           # 日志文件
├── main.py                         # 主程序（通用版）
├── config/                         # 配置文件
│   └── settings.py                 # 系统配置
└── requirements.txt                # Python 依赖
```

---

## 🚀 快速开始

### 本地开发环境

**1. 安装依赖**
```bash
pip install -r requirements.txt
```

**2. 配置环境变量**
```bash
cp .env.example .env
# 编辑 .env 文件，配置 API Key 等
```

**3. 启动监控服务**
```bash
python main_v31.py start
```

**4. 查看信号**
```bash
python main_v31.py signals
```

---

## 📊 回测验证

### 数据获取

```bash
# 从服务器获取 100 个新币的 K 线数据
python scripts/fetch_new_coins_klines_v2.py \
    --count 100 \
    --limit 500 \
    --output data/new_coins_backtest.json
```

**预期输出**:
- 币种数量：100 个
- K 线总数：约 50,000 根
- 文件大小：20-50MB
- 获取时间：5-10 分钟

### 运行回测

```bash
# 运行 v3.1 回测
python backtesting/backtest_v31_simple.py \
    --data data/new_coins_backtest.json \
    --days 90 \
    --output results/backtest_v31_newcoins.json
```

### 盈亏分析

```bash
# 生成盈亏报告
python backtesting/analyze_pnl_v31.py \
    --input results/backtest_v31_newcoins.json \
    --output results/backtest_v31_pnl_report.json
```

### 回测结果

**基础统计**:
- 总交易数：768 笔
- 涉及币种：36 个
- 平均评分：8.65 分
- 平均上线时间：24.7 小时

**形态识别**:
- 三次冲顶：745 次 (97.0%)
- 量价背离：71 次 (9.2%)

**盈利能力**:
- 胜率：72.3%
- 盈亏比：1.44
- 总收益率：1184.1%
- 平均盈利：2.91%
- 平均亏损：2.03%

---

## ⏰ 开仓时间窗口

### 最快开仓时间

**第 11 小时**（上线后 10 小时 +1 分钟）

```
00:00 新币上线
...
10:00 K 线 10 收盘（10 根 K 线完成）
11:01 第一次评分 ← 使用 K 线 1-10
     如果评分 ≥ 6.0 → 开仓 ✓
```

### 最晚开仓时间

**第 48 小时**（上线后 47 小时 +1 分钟）

```
47:00-48:00 K 线 48 形成
48:01       最后一次评分
            如果评分 ≥ 6.0 → 开仓 ✓
48:01 之后   超出 48 小时窗口，不再评分 ❌
```

### 完整时间窗口

| 时间段 | 评分状态 | 说明 |
|--------|----------|------|
| **第 1-10 小时** | 评分但无效 | K 线不足，返回 5.0 分 |
| **第 11 小时** | **首次有效评分** | 10 根 K 线，可能开仓 ✓ |
| **第 12-47 小时** | 正常评分 | 每小时评分一次 |
| **第 48 小时** | **最后一次评分** | 48 小时后移出监控 |

**可开仓窗口**: 38 小时（第 11-48 小时）  
**评分次数**: 最多 38 次（有效评分）

---

## 🔧 配置参数

### 核心配置

```python
# config/settings.py

# 信号生成
SIGNAL_THRESHOLD = 6.0        # 开仓阈值
LISTING_HOURS_MAX = 48        # 新币上线 48 小时内
COOLDOWN_HOURS = 2            # 冷却时间 2 小时
SCORING_MINUTE = 1            # 每小时第 1 分钟评分

# 技术面分析
MIN_KLINES = 10               # 最少 10 根 K 线
VOLUME_MA_PERIOD = 5          # 前 5 根 K 线
VOLUME_MULTIPLIER = 1.5       # 1.5 倍
TOP_PRICE_TOLERANCE = 0.02    # 价格容忍度 2%

# 通知服务
FEISHU_WEBHOOK = "https://open.feishu.cn/..."
```

---

## 📈 形态识别

### 三次冲顶形态

**判断规则**:
```python
# 1. 同一水平受阻（容忍 2% 误差）
if 3 个高点在同一价格水平（±2%）:
    形成三次冲顶

# 2. 高点逐次降低（每次至少降低 0.5%）
if high1 > high2 > high3:
    if (high1 - high2) / high1 > 0.5% and (high2 - high3) / high2 > 0.5%:
        形成三次冲顶
```

**回测识别率**: 97.0% (745/768)

### 量价背离

**判断规则**:
```python
avg_volume = (vol[-6] + vol[-5] + vol[-4] + vol[-3] + vol[-2]) / 5
current_volume = vol[-1]

if current_volume >= 1.5 * avg_volume:
    放量
    
if current_high <= max(high[-6:-1]):
    价格未创新高

if 放量 and 价格未创新高:
    放量滞涨 ✓
```

**回测识别率**: 9.2% (71/768)

---

## 🎯 实盘部署

### 服务器部署

**1. 准备部署文件**
```bash
# 创建部署配置
cat > .deploy_config << 'EOF'
SERVER_IP="43.156.242.184"
SERVER_USER="root"
SERVER_PROJECT_PATH="/root/short_selling_system"
DOCKER_CONTAINER_NAME="short_selling-app"
DOCKER_IMAGE_NAME="short_selling:latest"
PROJECT_NAME="short_selling_system"
DEPLOY_PACKAGE_NAME="deployment_package.tar.gz"
EOF
```

**2. 一键部署**
```bash
chmod +x one_click_deploy.sh
./one_click_deploy.sh
```

**3. 验证部署**
```bash
# 查看容器状态
ssh root@43.156.242.184 "docker ps -f name=short_selling-app"

# 查看日志
ssh root@43.156.242.184 "docker logs -f short_selling-app"
```

### 日常维护

**查看状态**:
```bash
ssh root@43.156.242.184 "docker ps -f name=short_selling-app"
```

**重启服务**:
```bash
ssh root@43.156.242.184 "docker restart short_selling-app"
```

**更新代码**:
```bash
./one_click_deploy.sh
```

---

## 📝 版本历史

### v3.1 (2026-04-21)

**核心改进**:
- ✅ 每小时第 1 分钟评分
- ✅ 支持 5-10 根 K 线就开始形态判断
- ✅ 三次冲顶跨 K 线判断
- ✅ 成交量比较基准优化
- ✅ 信号冷却机制
- ✅ 48 小时时间窗口

**回测表现**:
- 胜率 72.3%
- 盈亏比 1.44
- 总收益 1184%

### v3.0 (2026-04-01)

- 初始版本
- 每 30 分钟评分
- 使用 1 小时 K 线

---

## 📚 相关文档

- [v3.1 版本说明](../docs/plans/v31_版本说明.md)
- [v3.1 回测报告](../docs/plans/v31_回测报告.md)
- [v3.1 盈亏分析报告](../docs/plans/v31_盈亏分析报告.md)
- [服务器部署指南](../docs/plans/v31_服务器数据获取与回测指南.md)

---

## ⚠️ 风险提示

1. **回测不代表未来收益**
   - 过往表现不代表未来
   - 实际交易可能存在滑点、延迟

2. **做好风险控制**
   - 设置止损
   - 控制仓位
   - 分散投资

3. **小资金测试**
   - 实盘前用小资金测试（100-500U）
   - 监控实盘与回测差异

---

**项目版本**: v3.1  
**最后更新**: 2026-04-21  
**维护者**: AI Assistant  
**GitHub**: https://github.com/Bearflower/bianace_newtrade_trade.git
