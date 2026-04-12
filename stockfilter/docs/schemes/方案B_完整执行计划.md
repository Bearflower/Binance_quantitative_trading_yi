# 方案 B 完整执行计划

**更新时间：** 2026-04-03 12:15  
**目标：** 本地获取历史数据 → 上传服务器 → 日常增量更新

---

## 📋 执行步骤

### 阶段 1：本地批量获取历史数据 ⏳ 进行中

**脚本：** `fetch_all_local.py`

**状态：**
- ⚠️ AKShare 网络连接不稳定
- ⚠️ 大量"Remote end closed connection"错误
- 📊 当前获取速度：很慢（约 1-2 只/分钟）
- 📁 已获取数据：很少（目录几乎为空）

**问题：**
- AKShare 接口限流或网络问题
- 备用数据源（adata/baostock）可能在工作，但也可能失败

**解决方案：**
1. 让当前脚本继续运行（等待完成）
2. 或者优化获取脚本（添加重试、降低频率）
3. 或者分批获取（先获取指定的 3 只股票）

---

### 阶段 2：上传到服务器 📤 准备就绪

**脚本：** `upload_to_server.sh`

**功能：**
1. 检查本地 CSV 文件
2. 上传到服务器 `/tmp/kline_csv/`
3. 导入 PostgreSQL 数据库
4. 清理临时文件

**使用方法：**
```bash
cd /Users/yl/vscode/stockfilter
./upload_to_server.sh
```

**预计时间：**
- 上传：5-10 分钟（取决于数据量）
- 导入：5-10 分钟

---

### 阶段 3：服务器日常增量更新 🔄 需要实现

**功能：** 每天早上获取当日 K 线数据，增量更新

**实现方案：**

#### 方案 A：修改现有脚本

在服务器上创建定时任务脚本：

```bash
# /root/stockfilter/daily_update.sh
#!/bin/bash
docker exec -i stockfilter-app python main.py --update
```

#### 方案 B：使用现有调度器

服务器上已有 `scheduler_new.py`，配置为每天早上 07:30 运行

**需要确认：**
- ✅ 调度器是否已配置
- ✅ 是否能正常获取当日数据
- ✅ 是否有增量更新机制

---

## ⚠️ 当前问题

### 问题 1：AKShare 网络不稳定

**现象：**
```
AKShare 获取 002094.SZ 异常：('Connection aborted.', 
RemoteDisconnected('Remote end closed connection without response'))
```

**影响：**
- 获取速度极慢
- 大量股票获取失败

**可能的解决方案：**

1. **添加重试机制**
   ```python
   for attempt in range(3):
       try:
           df = get_stock_daily_kline(symbol, days=300)
           if df is not None:
               break
       except:
           time.sleep(2)
   ```

2. **降低请求频率**
   ```python
   # 每获取 10 只暂停 5 秒（而不是 100 只暂停 1 秒）
   if (idx + 1) % 10 == 0:
       time.sleep(5)
   ```

3. **使用备用数据源**
   - adata
   - baostock
   - 多个数据源轮询

---

## 🎯 建议的下一步

### 选项 A：优化获取脚本（推荐）

创建一个新的、更稳健的获取脚本：

```bash
python3 fetch_all_local_robust.py
```

**改进：**
- ✅ 添加重试机制（每只股票最多重试 3 次）
- ✅ 降低请求频率（每 10 只暂停 5 秒）
- ✅ 更好的错误处理
- ✅ 详细日志输出

### 选项 B：先获取指定的 3 只股票

快速获取你最初关心的 3 只股票，测试回测：

```bash
python3 batch_backtest_auto.py
```

这个脚本会自动获取：
- 603529 爱玛科技
- 002665 共达电声
- 000062 深圳华强

### 选项 C：等待当前脚本完成

让 `fetch_all_local.py` 继续运行，可能需要较长时间。

---

## 📊 完整时间估算

### 乐观估计（网络正常）

| 阶段 | 时间 |
|------|------|
| 本地获取（3000 只） | 20-30 分钟 |
| 上传到服务器 | 5-10 分钟 |
| 导入数据库 | 5-10 分钟 |
| **总计** | **30-50 分钟** |

### 悲观估计（当前网络状况）

| 阶段 | 时间 |
|------|------|
| 本地获取（3000 只） | 2-4 小时 |
| 上传到服务器 | 5-10 分钟 |
| 导入数据库 | 5-10 分钟 |
| **总计** | **2-4 小时** |

---

## 📁 文件清单

### 已创建

- ✅ `fetch_all_local.py` - 本地批量获取脚本
- ✅ `upload_to_server.sh` - 上传到服务器脚本
- ✅ `batch_backtest_auto.py` - 一键回测（自动获取数据）
- ✅ `batch_backtest.py` - 批量回测（读取本地 CSV）

### 需要创建

- ⏳ `fetch_all_local_robust.py` - 稳健版获取脚本（可选）
- ⏳ `daily_update.sh` - 服务器日常增量更新脚本

---

## 🔔 当前建议

**立即执行：**

1. **停止当前的获取脚本**（AKShare 错误太多）
2. **先测试 3 只股票**
   ```bash
   python3 batch_backtest_auto.py
   ```
3. **验证回测流程正常**

**然后：**

4. 创建稳健版获取脚本
5. 批量获取全市场数据
6. 上传到服务器
7. 配置日常增量更新

---

**你想：**
1. 先测试 3 只股票的回测？
2. 还是继续等待当前获取完成？
3. 或者创建更稳健的获取脚本？
