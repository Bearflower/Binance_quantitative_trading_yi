# 部署文档索引

本文档汇总 stockfilter 项目的部署指南、操作手册和运维文档。

## 📁 目录结构

```
deployment/
├── README.md                        # 本文档
├── 服务器部署指南.md                # Docker 部署
├── 飞书推送使用手册.md              # 飞书配置
└── 监控快速指南.md                  # 监控服务
```

## 📋 文档列表

| 文档名称 | 说明 | 版本 |
|---------|------|------|
| [服务器部署指南.md](服务器部署指南.md) | Docker 部署完整指南 | V1.0 |
| [飞书推送使用手册.md](飞书推送使用手册.md) | 飞书机器人配置与使用 | V1.0 |
| [监控快速指南.md](监控快速指南.md) | 数据同步监控方案 | V1.0 |

## 🚀 快速部署

### Docker 部署（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/your-repo/stockfilter.git
cd stockfilter

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入以下配置：
# - DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
# - FEISHU_WEBHOOK

# 3. 启动容器
docker-compose up -d

# 4. 查看日志
docker-compose logs -f

# 5. 验证服务
docker-compose ps
```

### 传统部署

```bash
# 1. 安装依赖
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. 初始化数据库
python init_db.py

# 3. 配置环境变量
export DB_HOST=localhost
export DB_NAME=stockfilter
export DB_USER=stockfilter_user
export DB_PASSWORD=your_password
export FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx

# 4. 运行扫描
python daily_scan.py

# 5. 配置定时任务
crontab -e
# 添加以下任务：
# 形态扫描：每个交易日 15:30
30 15 * * 1-5 cd /path/to/stockfilter && python daily_scan.py >> logs/daily_scan.log 2>&1
# 飞书推送：每个交易日 08:00
0 8 * * 1-5 cd /path/to/stockfilter && python feishu_push.py >> logs/feishu_push.log 2>&1
```

## ⚙️ 环境配置

### 数据库配置

```bash
# PostgreSQL 安装（Ubuntu）
sudo apt update
sudo apt install postgresql postgresql-contrib

# 创建数据库和用户
sudo -u postgres psql
CREATE DATABASE stockfilter;
CREATE USER stockfilter_user WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE stockfilter TO stockfilter_user;

# 创建 schema
psql -U stockfilter_user -d stockfilter
CREATE SCHEMA schema_stockfilter;
```

### 环境变量

```bash
# .env 文件配置
DB_HOST=localhost
DB_PORT=5432
DB_NAME=stockfilter
DB_USER=stockfilter_user
DB_PASSWORD=your_secure_password
DB_SCHEMA=schema_stockfilter

FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/your_webhook_url

LOG_LEVEL=INFO
LOG_DIR=logs
```

## 📊 服务监控

### 健康检查

```bash
# 检查数据库连接
psql -U stockfilter_user -d stockfilter -c "SELECT count(*) FROM stocks;"

# 检查数据同步进度
python check_sync_status.py

# 检查今日信号
python check_today.py
```

### 日志查看

```bash
# 实时查看扫描日志
tail -f logs/daily_scan.log

# 查看飞书推送日志
tail -f logs/feishu_push.log

# 查看错误日志
grep "ERROR" logs/*.log
```

### 性能监控

```bash
# 查看 CPU 使用率
top -p $(pgrep -f daily_scan)

# 查看内存使用率
ps aux | grep python | awk '{print $2, $4}'

# 查看磁盘使用率
df -h
```

## 🔧 常见问题

### 问题 1: 数据库连接失败

```bash
# 检查 PostgreSQL 服务
systemctl status postgresql

# 检查网络连接
telnet localhost 5432

# 查看数据库日志
tail -f /var/log/postgresql/postgresql-*.log
```

### 问题 2: AKShare 获取失败

```python
# 切换到备用数据源
from data.data_source import DataSourceManager
data_source = DataSourceManager(primary_source="adata")
stock_list = data_source.get_stock_list()
```

### 问题 3: 飞书推送失败

```bash
# 检查 Webhook URL 是否正确
echo $FEISHU_WEBHOOK

# 测试 Webhook
curl -X POST -H "Content-Type: application/json" \
  -d '{"msg_type":"text","content":{"text":"测试"}}' \
  $FEISHU_WEBHOOK
```

### 问题 4: 定时任务未执行

```bash
# 检查 crontab 配置
crontab -l

# 检查 cron 服务
systemctl status cron

# 查看 cron 日志
grep CRON /var/log/syslog
```

## 📝 运维手册

### 每日运维

```bash
# 1. 检查数据同步状态
python check_sync_status.py

# 2. 查看昨日信号
cat signals/signals_*.json | python -m json.tool

# 3. 检查日志是否有错误
grep "ERROR" logs/*.log

# 4. 清理旧日志（保留 30 天）
find logs/ -name "*.log" -mtime +30 -delete
```

### 每周运维

```bash
# 1. 数据库备份
pg_dump -U stockfilter_user stockfilter > backups/stockfilter_$(date +%Y%m%d).sql

# 2. 检查磁盘空间
df -h

# 3. 更新股票列表
python create_main_board_list.py

# 4. 查看周统计
python analyze_v22.py --week
```

### 每月运维

```bash
# 1. 数据库性能分析
psql -U stockfilter_user -d stockfilter -c "ANALYZE;"

# 2. 清理过期备份
find backups/ -name "*.sql" -mtime +90 -delete

# 3. 回测验证
python backtest_v21_final.py

# 4. 生成月度报告
python generate_backtest_report.py --month
```

## 🔐 安全建议

### 数据库安全

```bash
# 1. 限制数据库访问
sudo ufw allow from 127.0.0.1 to any port 5432

# 2. 使用强密码
# 至少 16 位，包含大小写字母、数字、特殊字符

# 3. 定期备份
# 每日自动备份，保留 30 天
```

### API 安全

```bash
# 1. 环境变量存储敏感信息
# 不要将 .env 提交到 Git

# 2. 限制 API 调用频率
# AKShare 设置超时和重试

# 3. 使用 HTTPS
# 飞书 Webhook 使用 HTTPS
```

### 服务器安全

```bash
# 1. 防火墙配置
sudo ufw enable
sudo ufw allow ssh
sudo ufw allow 5432

# 2. SSH 密钥登录
# 禁用密码登录

# 3. 定期更新系统
sudo apt update && sudo apt upgrade -y
```

## 📊 性能优化

### 数据库优化

```sql
-- 添加索引
CREATE INDEX idx_klines_code ON klines(code);
CREATE INDEX idx_klines_date ON klines(date);
CREATE INDEX idx_klines_code_date ON klines(code, date);

-- 定期分析表
ANALYZE stocks;
ANALYZE klines;

-- 清理过期数据
-- DELETE FROM klines WHERE date < '2020-01-01';
```

### 扫描优化

```python
# 使用并行处理
from concurrent.futures import ProcessPoolExecutor

def scan_parallel(stock_list):
    with ProcessPoolExecutor(max_workers=4) as executor:
        results = executor.map(check_single_stock, stock_list)
    return results
```

### 缓存优化

```python
# 缓存股票列表
@lru_cache(maxsize=1)
def get_stock_list():
    return db.get_stock_list()

# 缓存 K 线数据
@lru_cache(maxsize=100)
def get_kline_data(code, days):
    return db.get_kline_history(code, days)
```

## 🔗 相关资源

- [技术架构](../designs/技术架构文档_V2.1.md) - 系统技术架构设计
- [需求文档](../requirements/项目需求与迭代.md) - 完整业务需求
- [配置方案](../schemes/V2.1%20最终配置方案.md) - V2.1 最终配置方案
- [Vibe Coding 文档架构技能](../Vibe_Coding 文档架构技能.md) - 文档架构规范

---

**最后更新**: 2026-04-12  
**版本**: V1.0  
**维护者**: StockFilter Team
