# K 线数据同步常态化监控方案总结

## 📋 方案概述

针对 K 线数据同步任务容易失败的问题，建立**三层监控体系**：

```
应用层 → 数据层 → 系统层
  ↓        ↓        ↓
日志分析  SQL 查询  容器检查
```

---

## 🎯 核心问题

### 当前主要失败原因

1. **bigint out of range**（52 只股票，占 6.5%）
   - 成交量/成交额超出数据库字段范围
   - 需要修改数据库表结构

2. **网络连接问题**
   - AKShare 连接被断开
   - 已解决：改用 Baostock 作为主数据源

3. **任务意外停止**
   - 进程崩溃
   - 容器重启
   - 需要自动检测和恢复

---

## 📊 监控指标体系

### 1. 进度指标

| 指标 | 当前值 | 目标值 | 状态 |
|------|--------|--------|------|
| 总股票数 | 5,324 | 5,324 | ✅ |
| 需同步股票 | 4,351 | 0 | 🔄 进行中 |
| 已完成 | 989 | 4,351 | 22.7% |
| 成功率 | 93.3% | >95% | ⚠️ |

### 2. 质量指标

| 指标 | 正常范围 | 当前值 | 状态 |
|------|---------|--------|------|
| 数据完整率 | >80% | 75.1% | ⚠️ |
| 失败率 | <5% | 6.5% | ❌ |
| 进度更新 | <5 分钟 | 正常 | ✅ |

### 3. 系统指标

| 指标 | 状态 | 说明 |
|------|------|------|
| 容器状态 | ✅ Up (healthy) | 运行正常 |
| 数据库连接 | ✅ 正常 | PostgreSQL |
| 数据源 | ✅ Baostock | 稳定可靠 |

---

## 🛠️ 监控工具集

### 工具 1：sync_monitor.py

**用途：** Python 实时监控脚本

**功能：**
- ✅ 数据库统计查询
- ✅ 数据完整性分析
- ✅ 进度跟踪
- ✅ 异常检测

**使用：**
```bash
python3 sync_monitor.py
```

### 工具 2：monitor_sync.sh

**用途：** Shell 定时监控脚本

**功能：**
- ✅ 每 10 分钟自动检查
- ✅ 日志分析（成功/失败率）
- ✅ 容器状态检查
- ✅ 告警通知（可扩展飞书）

**使用：**
```bash
./monitor_sync.sh &
tail -f logs/sync_monitor.log
```

### 工具 3：Systemd 定时器

**用途：** 系统级定时任务

**配置：**
- `sync-monitor.service` - 服务定义
- `sync-monitor.timer` - 定时器

**安装：**
```bash
sudo cp docs/monitoring/sync-monitor.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable sync-monitor.timer
sudo systemctl start sync-monitor.timer
```

---

## 🚨 告警策略

### 告警级别

| 级别 | 条件 | 响应 | 通知 |
|------|------|------|------|
| 🔴 严重 | 容器停止 | 立即 | 飞书 + 短信 |
| 🟠 警告 | 失败率>10% | 30 分钟 | 飞书 |
| 🟡 提醒 | 5 分钟无进展 | 1 小时 | 飞书 |
| 🟢 信息 | 完成 50% | - | 飞书 |

### 告警配置

**飞书 Webhook：**
```bash
# 在 monitor_sync.sh 的 send_alert 函数中添加
FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_TOKEN"
curl -X POST "$FEISHU_WEBHOOK" \
  -H "Content-Type: application/json" \
  -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"$message\"}}"
```

---

## 🔧 常见问题处理

### 问题 1：bigint out of range

**影响：** 52 只股票无法同步

**解决方案：**
```sql
-- 修改数据库字段类型
ALTER TABLE klines 
  ALTER COLUMN volume TYPE BIGINT,
  ALTER COLUMN amount TYPE NUMERIC(20,2);
```

**执行：**
```bash
ssh root@43.156.242.184 "docker exec stockfilter-app psql -U stockfilter_user -d stockfilter -c 'ALTER TABLE klines ALTER COLUMN volume TYPE BIGINT;'"
```

### 问题 2：任务停止

**自动恢复脚本：**
```bash
#!/bin/bash
# check_and_restart.sh

CONTAINER="stockfilter-app"
TASK="sync_kline_history.py"

# 检查进程
if ! docker exec $CONTAINER ps aux | grep -q $TASK; then
    echo "任务已停止，正在重启..."
    docker exec -d $CONTAINER python3 /app/$TASK
    echo "任务已重启"
fi
```

### 问题 3：数据不一致

**检查脚本：**
```bash
docker exec stockfilter-app python3 -c "
from data.database import DatabaseManager
import pandas as pd
db = DatabaseManager()

# 查询每只股票的数据量
df = pd.read_sql('''
    SELECT code, COUNT(*) as cnt, MIN(date) as start, MAX(date) as end
    FROM klines
    GROUP BY code
    ORDER BY cnt DESC
''', db.conn)

print(df.to_string())
db.close()
"
```

---

## 📈 监控看板

### 实时看板命令

```bash
# 1. 实时进度（每 5 秒刷新）
watch -n 5 'ssh root@43.156.242.184 "docker exec stockfilter-app python3 -c \"from data.database import DatabaseManager; db=DatabaseManager(); import pandas as pd; df=pd.read_sql(\"SELECT COUNT(DISTINCT code) FROM klines\", db.conn); print(df.iloc[0,0]); db.close()\""'

# 2. 实时日志
ssh root@43.156.242.184 "docker logs stockfilter-app -f 2>&1" | grep -E "同步成功 | 同步失败 | 处理批次"

# 3. 容器状态（每 10 秒刷新）
watch -n 10 'ssh root@43.156.242.184 "docker ps -f name=stockfilter-app --format \"{{.Status}}\""'

# 4. 失败率统计
ssh root@43.156.242.184 "docker logs stockfilter-app --since 30m 2>&1" | grep -E "同步成功 | 同步失败" | sort | uniq -c
```

### 监控仪表盘（建议）

可以使用以下工具构建可视化看板：
- Grafana + Prometheus
- 飞书多维表格
- 自建 Web 界面

---

## 📋 运维手册

### 每日检查清单

- [ ] 查看监控日志，确认无异常告警
- [ ] 检查失败率（应<5%）
- [ ] 验证最新数据日期
- [ ] 查看容器健康状态
- [ ] 记录当日进度

### 每周检查清单

- [ ] 统计数据完整性分布
- [ ] 分析失败股票列表
- [ ] 清理异常数据
- [ ] 备份数据库
- [ ] 优化数据库性能

### 每月检查清单

- [ ] 系统性能评估
- [ ] 监控策略优化
- [ ] 告警阈值调整
- [ ] 文档更新

---

## 📞 支持与文档

### 文档位置

- **总体方案：** `docs/monitoring/同步监控方案.md`
- **快速指南：** `docs/monitoring/监控快速指南.md`
- **配置文件：** `docs/monitoring/sync-monitor.*`

### 脚本位置

- **Python 监控：** `sync_monitor.py`
- **Shell 监控：** `monitor_sync.sh`
- **日志文件：** `logs/sync_monitor.log`

### 联系方式

- **监控负责人：** [待填写]
- **技术支持：** [待填写]
- **升级流程：** 见 `docs/monitoring/同步监控方案.md`

---

## 🎯 下一步行动

### 立即执行

1. ✅ 创建监控脚本（已完成）
2. ✅ 编写监控文档（已完成）
3. ⏳ 配置飞书告警
4. ⏳ 修复 bigint 问题
5. ⏳ 部署 systemd 服务

### 短期目标（1 周）

- [ ] 完成所有股票数据同步
- [ ] 建立完整的监控体系
- [ ] 培训运维人员
- [ ] 建立值班制度

### 长期目标（1 月）

- [ ] 实现零失败率
- [ ] 自动化故障恢复
- [ ] 性能优化
- [ ] 数据可视化看板

---

**创建时间：** 2026-04-09  
**版本：** v1.1  
**状态：** 已部署  
**最后更新：** 2026-04-12 - 修复日志检测逻辑

---

## 🔧 更新日志

### v1.1 (2026-04-12) - 修复日志检测逻辑

**问题：** 监控脚本只检查"同步成功"关键词，但同步任务输出的是批次级别的日志

**修复内容：**
1. ✅ 更新 `monitor_sync.sh` - 使用正则表达式检查多种日志模式
2. ✅ 更新 `monitor_standalone.sh` - 支持检查"批次完成"
3. ✅ 更新 `monitor_hourly.sh` - 支持检查"批次完成"
4. ✅ 更新 `monitor_hourly_simple.sh` - 支持检查"批次完成"
5. ✅ 更新 `monitor_simple.sh` - 支持检查"处理批次"
6. ✅ 优化 `sync_kline_history.py` - 批次日志包含批次号信息

**修改详情：**
```bash
# 之前：只检查"同步成功"
grep -q "同步成功"

# 现在：检查多种进度日志
grep -qE "同步成功 | 批次完成 | 同步完成 | 处理批次"
```

**影响：** 解决了误告警问题，监控脚本现在能正确识别同步任务的运行状态

---
