# K 线数据获取监控指南

**创建时间：** 2026-04-03  
**任务：** 获取全市场股票 K 线数据（2025-08-01 至今）

---

## 📊 当前状态

### ✅ 进行中

- **后台脚本状态：** 运行中
- **当前进度：** 5 只股票已完成
- **数据总量：** 1,107 条 K 线记录
- **预计完成时间：** 1-2 小时

### 📈 已完成的股票

| 代码 | 数据条数 |
|------|---------|
| 000056 | 222 |
| 000060 | 222 |
| 000058 | 222 |
| 000059 | 222 |
| 603529 | 219 |

---

## 🔍 查看进度

### 方法 1：运行监控脚本（推荐）

```bash
cd /Users/yl/vscode/stockfilter
./monitor_fetch_progress.sh
```

### 方法 2：SSH 登录服务器查看

```bash
# 查看有 K 线数据的股票数量
ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem root@43.156.242.184 "docker exec -i stockfilter-app python3 -c \"from data.database import DatabaseManager; db = DatabaseManager(); import pandas as pd; df = pd.read_sql('SELECT COUNT(DISTINCT code) as cnt FROM klines', db.conn); print(f'当前有 K 线数据的股票数量：{df.iloc[0,0]}'); db.close()\""
```

### 方法 3：查看后台进程

```bash
# 查看脚本是否还在运行
ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem root@43.156.242.184 "ps aux | grep python | grep -v grep"
```

---

## ⏱️ 预计时间

- **总股票数：** 约 4,000 只（沪市 + 深市主板）
- **当前进度：** 5 只（0.125%）
- **预计完成时间：** 1-2 小时
- **速度：** 约 30-50 只股票/分钟

---

## 📋 后续步骤

### 步骤 1：等待完成

让后台脚本继续运行，你可以：
- 定期运行 `./monitor_fetch_progress.sh` 查看进度
- 或者等待脚本完成后通知你

### 步骤 2：导出数据到本地

数据获取完成后，运行：

```bash
cd /Users/yl/vscode/stockfilter
python3 export_all_sh_stocks.py
```

**输出：**
- 数据目录：`data/backtest/sh_stocks/`
- 文件格式：CSV
- 每只股票一个文件：`{code}_data.csv`

### 步骤 3：批量回测

```bash
python3 batch_backtest.py
```

**输出：**
- 回测报告：`backtest_report.md`
- JSON 结果：`backtest_results.json`

---

## 🛠️ 故障处理

### 如果脚本停止运行

**检查方法：**
```bash
./monitor_fetch_progress.sh
```

如果显示"⚠️ 未检测到后台脚本"，说明脚本已停止。

**重启脚本：**
```bash
ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem root@43.156.242.184 "docker exec -i stockfilter-app python3 << 'EOF' &
from data.database import DatabaseManager
from data.fetcher import get_stock_daily_kline
import time

db = DatabaseManager()
stocks_df = db.get_stock_list()
stocks_df = stocks_df[stocks_df['code'].str.startswith(('60', '00'))]
print(f'开始获取 {len(stocks_df)} 只股票的 K 线数据')

success = 0
error = 0
skip = 0

for idx, row in stocks_df.iterrows():
    code = row['code']
    existing = db.get_kline_history(code, days=300)
    if existing is not None and len(existing) > 200:
        skip += 1
        continue
    try:
        df = get_stock_daily_kline(row['symbol'], days=300)
        if df is not None and len(df) > 0:
            db.save_kline_history(code, df)
            success += 1
        else:
            error += 1
    except:
        error += 1
    
    if (idx+1) % 100 == 0:
        print(f'{idx+1}/{len(stocks_df)} | 成功:{success} 失败:{error} 跳过:{skip}')

print(f'完成！成功:{success} 失败:{error} 跳过:{skip}')
db.close()
EOF
"
```

### 如果数据不完整

**检查数据库中有哪些股票：**
```bash
ssh -i /Users/yl/vscode/inspection_automation/docs/only.pem root@43.156.242.184 "docker exec -i stockfilter-app python3 -c \"from data.database import DatabaseManager; db = DatabaseManager(); import pandas as pd; df = pd.read_sql('SELECT code, COUNT(*) as cnt FROM klines GROUP BY code ORDER BY cnt DESC', db.conn); print(df.to_string(index=False)); db.close()\""
```

---

## 📞 需要帮助？

如果你需要：
- 提前停止脚本
- 只获取特定股票
- 调整数据范围
- 查看详细信息

请告诉我！

---

**文档结束**
