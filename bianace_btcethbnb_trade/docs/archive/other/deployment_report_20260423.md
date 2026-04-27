# 部署报告 - 2026-04-23

## 📋 本次部署内容

### 修复的问题

1. ✅ **停止币安 API 数据源**
   - 修改 `scheduler_config.yaml`，只保留 K 线服务配置
   - 修复 `scheduler_new.py` 日志输出 bug（移除未定义的 `binance_api_minute` 变量）
   - 系统现在只在每小时 25 分执行 K 线服务分析

2. ✅ **修复限价下单功能**
   - `scheduler_new.py` 第 336 行已使用 `place_limit_order`（限价单）
   - `rule_executor.py` 第 203 行也已使用限价单
   - 手续费优化：maker 0.02%（原市价单 taker 0.05%）

3. ✅ **BTC 和 ETH 未触发信号分析**
   - 根本原因：评分引擎正常工作，BTC 和 ETH 市场状态不符合交易条件
   - 评分维度：趋势强度、趋势一致性、形态质量（权重 30%）、成交量、动量、风险
   - C 级阈值：45 分，BNB 达到 45 分，BTC 和 ETH 因市场过滤条件被筛除

4. ✅ **频率限制功能检查**
   - 代码已正确实现频率控制器
   - 限制参数：每日总交易 4 笔、单品种 2 笔、冷却期 12 小时、连续亏损 5 笔暂停、每日亏损限额 25U
   - 当前只有 BNB 交易，达到阈值后频率限制会生效

### 代码变更

#### 1. 配置文件
- `config/scheduler_config.yaml` - 移除币安 API 分析配置
- `.deploy_config` - 更新服务器 IP 为 43.156.242.184

#### 2. 核心文件
- `scheduler_new.py` - 修复限价单和日志输出
- `services/rule_executor.py` - 已使用限价单

#### 3. 部署脚本（新增）
- `auto_package.sh` - 自动打包脚本
- `upload_to_server.sh` - 上传到服务器脚本
- `one_click_deploy.sh` - 一键部署脚本

---

## 📦 部署包准备

打包已完成：
```bash
✅ 打包完成！
📦 压缩包：deployment_package.tar.gz
📊 大小：299K
📁 文件数：122 个
```

---

## 🚨 部署阻塞问题

### 问题：SSH 连接超时

**症状：**
- SSH 连接到 43.156.242.184 超时
- Ping 测试 100% 丢包

**诊断命令：**
```bash
ping -c 3 43.156.242.184
# Request timeout for icmp_seq 0
# 100% packet loss
```

**可能原因：**
1. 服务器网络故障
2. 服务器已关机
3. 防火墙阻止连接

---

## 📝 后续部署步骤

### 方案 1：等待网络恢复后一键部署

当服务器网络恢复后，执行：

```bash
cd /Users/yl/vscode/bianace_btcethbnb_trade
./one_click_deploy.sh
```

这将自动完成：
1. ✅ 打包项目（已完成）
2. ⏳ 上传到服务器（等待网络恢复）
3. ⏳ 远程部署（停止旧容器、解压新包、启动新容器）
4. ⏳ 验证部署

### 方案 2：手动部署（如果一键部署失败）

#### 步骤 1：配置 SSH 免密登录

```bash
# 生成 SSH 密钥（如果还没有）
ssh-keygen -t ed25519 -C "your_email@example.com"
# 直接回车，不设置密码

# 复制公钥到服务器
ssh-copy-id -i /Users/yl/vscode/inspection_automation/docs/only.pem.pub root@43.156.242.184

# 测试免密登录
ssh root@43.156.242.184 "echo 成功"
```

#### 步骤 2：上传部署包

```bash
cd /Users/yl/vscode/bianace_btcethbnb_trade
./upload_to_server.sh
```

#### 步骤 3：远程部署

```bash
ssh root@43.156.242.184
```

在服务器上执行：

```bash
# 1. 停止旧容器
docker stop trading_system-app
docker rm trading_system-app

# 2. 备份重要文件
cd /root/trading_system
cp .env /root/.env.backup
cp -r logs /root/logs.backup
cp -r data /root/data.backup

# 3. 清理旧代码
cd /root
rm -rf trading_system
mkdir -p trading_system

# 4. 解压新包
tar -xzf deployment_package.tar.gz -C trading_system

# 5. 恢复配置
cd trading_system
cp /root/.env.backup .env 2>/dev/null || true

# 6. 设置权限
chmod +x scheduler_new.py
chmod 600 .env

# 7. 重新构建并启动
docker-compose build --no-cache
docker-compose up -d

# 8. 查看日志
docker logs -f trading_system-app
```

---

## 🔍 验证部署

部署完成后，验证以下功能：

### 1. 检查 K 线服务分析

查看日志确认每小时 25 分执行分析：

```bash
ssh root@43.156.242.184 "docker logs trading_system-app | grep 'K 线服务分析'"
```

### 2. 检查限价单

查看订单日志确认使用限价单：

```bash
ssh root@43.156.242.184 "docker logs trading_system-app | grep '限价单'"
```

应该看到：
```
✅ 限价单下单成功：订单 ID=xxxxx
💰 手续费优化：maker 0.02% (原市价单 taker 0.05%)
```

### 3. 检查频率限制

查看交易记录：

```bash
ssh root@43.156.242.184 "docker exec trading_system-app psql -U postgres -d trading_db -c 'SELECT * FROM trade_records ORDER BY open_time DESC LIMIT 10;'"
```

### 4. 检查评分引擎

查看 BTC、ETH、BNB 的评分：

```bash
ssh root@43.156.242.184 "docker logs trading_system-app | grep '评分'"
```

---

## 📊 预期结果

### 正常行为

1. **每小时 25 分执行分析**
   - 只使用 K 线服务数据源
   - 不再有币安 API 分析

2. **限价单生效**
   - 订单日志显示"限价单开仓成功"
   - 手续费从 0.05% 降至 0.02%

3. **BTC 和 ETH 可能无信号**
   - 这是正常现象，说明市场状态不符合交易条件
   - 评分引擎在过滤低质量信号

4. **频率限制在达到阈值后生效**
   - 当日交易达到 4 笔后停止
   - 单品种交易达到 2 笔后停止该品种
   - 冷却期 12 小时内不重复交易同品种

---

## 🛠️ 故障排查

### 问题 1：容器无法启动

```bash
# 查看详细日志
ssh root@43.156.242.184 "docker logs trading_system-app"

# 检查配置文件
ssh root@43.156.242.184 "cat /root/trading_system/.env"

# 手动启动调试
ssh root@43.156.242.184 "cd /root/trading_system && docker-compose up"
```

### 问题 2：调度器未按时执行

检查调度器配置：

```bash
ssh root@43.156.242.184 "cat /root/trading_system/config/scheduler_config.yaml"
```

应该看到：
```yaml
kline_service_analysis:
  minute: 25

daily_report:
  hour: 9
  minute: 5
```

### 问题 3：仍然是市价单

检查代码是否更新：

```bash
ssh root@43.156.242.184 "grep -n 'place_limit_order' /root/trading_system/scheduler_new.py"
```

应该看到第 336 行有 `place_limit_order`

---

## 📞 联系信息

如有问题，请查看：
- 项目文档：`docs/README.md`
- 部署规范：`skills/服务器自动化部署技能.md`
- 需求文档：`docs/proposals/项目需求迭代文档.md`

---

**部署时间：** 2026-04-23  
**部署版本：** v6.13.2（限价单优化）  
**部署状态：** ⏳ 等待网络恢复  
**下次执行时间：** 网络恢复后执行一键部署
