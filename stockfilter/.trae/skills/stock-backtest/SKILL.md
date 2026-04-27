---
name: "stock-backtest"
description: "Exports stock K-line data from PostgreSQL server to local CSV/JSON and runs pattern backtesting. Invoke when user wants to backtest stocks using server database data or run local backtests with real historical data."
---

# 股票本地回测技能

## 🎯 技能说明

本技能提供了一套**完整的股票数据导出和本地回测流程**，基于 PostgreSQL 数据库和自研的形态检测策略。适用于需要从服务器数据库导出真实 K 线数据到本地进行回测的场景。

**核心功能**：
- ✅ **SSH 远程数据导出** - 通过 SSH 连接服务器，从 Docker 容器内的 PostgreSQL 数据库导出数据
- ✅ **Base64 编码传输** - 避免 SSH 命令转义问题，确保数据传输完整
- ✅ **CSV/JSON 本地存储** - 自动保存到本地 data/backtest/ 目录
- ✅ **形态检测回测** - 基于"大跌→缩量→放量→回踩"四步检测策略
- ✅ **详细分析报告** - 生成走势分析、形态检测、投资建议等完整报告
- ✅ **参数优化支持** - 支持网格搜索和随机搜索参数优化

**适用场景**：
- 从服务器 PostgreSQL 数据库导出股票 K 线数据
- 在本地使用真实历史数据进行回测
- 验证股票形态筛选策略的有效性
- 参数优化和策略迭代
- 批量回测多只股票

---

## 📋 标准部署流程

### 第一步：环境准备

**1. 确认项目结构**

确保项目包含以下目录结构：

```
stockfilter/
├── backtest/                   # 回测模块
│   ├── data_manager.py        # 数据管理
│   ├── engine.py              # 回测引擎
│   ├── optimizer.py           # 参数优化
│   └── visualizer.py          # 可视化
├── strategy/                   # 策略模块
│   ├── pattern_detector.py    # 形态检测
│   ├── scoring.py             # 形态评分
│   └── params.py              # 参数管理
├── export_from_server_base64.py  # 数据导出脚本
├── check_603529_from_exported.py # 形态检测脚本
├── config.yaml                 # 配置文件
└── data/backtest/              # 数据目录
```

**2. 安装依赖**

```bash
pip install pandas numpy akshare psycopg2-binary pyyaml
```

**3. 配置服务器连接**

编辑 `export_from_server_base64.py`，配置服务器信息：

```python
server_ip = "43.156.242.184"      # 服务器 IP
server_user = "root"              # SSH 用户
ssh_key = "/Users/yl/vscode/inspection_automation/docs/only.pem" # SSH 密钥路径

# 数据库配置（在 Docker 容器内）
os.environ['DB_HOST'] = '10.3.0.12'
os.environ['DB_PORT'] = '5432'
os.environ['DB_NAME'] = 'stockfilter'
os.environ['DB_USER'] = 'stockfilter_user'
os.environ['DB_PASSWORD'] = 'Stock@2024'
```

---

### 第二步：从服务器导出数据

**1. 创建导出脚本**

在 `export_from_server_base64.py` 创建：

```python
#!/usr/bin/env python3
"""
通过 SSH 从服务器导出股票 K 线数据（使用 base64 编码避免转义问题）
"""

import subprocess
import base64
import pandas as pd
from pathlib import Path


def export_via_base64(code='603529', days=300):
    """
    将 Python 脚本 base64 编码后在服务器执行
    
    Args:
        code: 股票代码
        days: 获取天数
    """
    server_ip = "43.156.242.184"
    server_user = "root"
    ssh_key = "/Users/yl/vscode/inspection_automation/docs/only.pem"
    
    print(f"从服务器导出 {code} 的 {days} 天 K 线数据...")
    
    # Python 脚本
    python_script = f"""
import os
import psycopg2
import sys

os.environ['DB_HOST'] = '10.3.0.12'
os.environ['DB_PORT'] = '5432'
os.environ['DB_NAME'] = 'stockfilter'
os.environ['DB_USER'] = 'stockfilter_user'
os.environ['DB_PASSWORD'] = 'Stock@2024'
os.environ['DB_SCHEMA'] = 'schema_stockfilter'

try:
    conn = psycopg2.connect(
        host=os.environ['DB_HOST'],
        port=os.environ['DB_PORT'],
        database=os.environ['DB_NAME'],
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        options=f'-c search_path={{os.environ["DB_SCHEMA"]}}'
    )
    
    cursor = conn.cursor()
    cursor.execute('''
        SELECT date, open, high, low, close, volume, amount
        FROM klines
        WHERE code = %s
        ORDER BY date DESC
        LIMIT %s
    ''', ('{code}', {days}))
    
    for row in cursor.fetchall():
        print(f'{{row[0]}},{{row[1]}},{{row[2]}},{{row[3]}},{{row[4]}},{{row[5]}},{{row[6]}}')
    
    cursor.close()
    conn.close()
except Exception as e:
    print(f"ERROR: {{e}}", file=sys.stderr)
    sys.exit(1)
""".strip()
    
    # Base64 编码
    encoded_script = base64.b64encode(python_script.encode()).decode()
    
    # Docker 命令
    docker_cmd = f"echo {encoded_script} | base64 -d | docker exec -i stockfilter-app python3 -"
    
    # SSH 命令
    ssh_command = f"ssh -i {ssh_key} {server_user}@{server_ip} \"{docker_cmd}\""
    
    result = subprocess.run(ssh_command, shell=True, capture_output=True, text=True, timeout=60)
    
    if result.returncode != 0 or not result.stdout:
        print(f"导出失败：{result.stderr}")
        return None
    
    # 解析 CSV 数据
    lines = result.stdout.strip().split('\n')
    data = []
    for line in lines:
        if line.strip() and ',' in line:
            parts = line.split(',')
            if len(parts) >= 7:
                data.append({
                    'date': parts[0],
                    'open': float(parts[1]),
                    'high': float(parts[2]),
                    'low': float(parts[3]),
                    'close': float(parts[4]),
                    'volume': int(float(parts[5])),
                    'amount': float(parts[6])
                })
    
    if not data:
        return None
    
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['date'])
    df.sort_values('date', inplace=True)
    
    # 保存到本地
    output_file = Path('data/backtest') / f"{code}_server_data.csv"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_file, index=False)
    
    print(f"✅ 成功导出 {len(df)} 条数据，保存到：{output_file}")
    return df
```

**2. 执行数据导出**

```bash
# 导出 603529 的 300 天 K 线数据
python3 export_from_server_base64.py
```

**预期输出**：
```
从服务器导出 603529 的 300 天 K 线数据...
✅ 成功导出 219 条数据，保存到：data/backtest/603529_server_data.csv
```

---

### 第三步：运行形态检测回测

**1. 创建检测脚本**

在 `check_stock_from_exported.py` 创建：

```python
#!/usr/bin/env python3
"""
对从服务器导出的股票数据进行形态检测
"""

import pandas as pd
from pathlib import Path
from strategy.pattern_detector import PatternDetector
from strategy.scoring import PatternScorer
from strategy.params import ConfigLoader


def check_pattern(code='603529', check_dates=None):
    """
    形态检测
    
    Args:
        code: 股票代码
        check_dates: 检测日期列表
    """
    if check_dates is None:
        check_dates = [
            '2025-09-30', '2025-10-31', '2025-11-30',
            '2025-12-31', '2026-01-31', '2026-02-28', '2026-03-20'
        ]
    
    # 加载数据
    data_file = f'data/backtest/{code}_server_data.csv'
    if not Path(data_file).exists():
        print(f"数据文件不存在：{data_file}")
        return
    
    df = pd.read_csv(data_file)
    df['date'] = pd.to_datetime(df['date'])
    df.sort_values('date', inplace=True)
    
    print(f"加载 {len(df)} 条数据")
    
    # 加载配置
    config = ConfigLoader('config.yaml')
    pattern_params = config.get_pattern_params()
    scoring_weights = config.get_scoring_weights()
    
    detector = PatternDetector(pattern_params)
    scorer = PatternScorer(scoring_weights)
    
    match_dates = []
    
    # 逐日检测
    for check_date in check_dates:
        df_check = df[df['date'] <= pd.to_datetime(check_date)].tail(120)
        
        if len(df_check) < 60:
            continue
        
        is_match, detail = detector.check_pattern(df_check)
        
        if is_match:
            score = scorer.score(detail, pattern_params)
            match_dates.append({
                'date': check_date,
                'score': score,
                'price': detail.get('current_close', 0),
                'detail': detail
            })
            
            print(f"\n✅ {check_date} 满足形态条件！")
            print(f"   评分：{score:.2f}")
            print(f"   价格：{detail.get('current_close', 0):.2f}")
    
    # 汇总
    print(f"\n{'='*60}")
    if match_dates:
        print(f"发现 {len(match_dates)} 个日期满足形态条件:")
        for match in match_dates:
            print(f"  - {match['date']}: {match['price']:.2f} (评分：{match['score']:.2f})")
    else:
        print("未发现满足形态条件的信号")
    
    return match_dates
```

**2. 执行形态检测**

```bash
python3 check_stock_from_exported.py
```

---

### 第四步：生成检测报告

**1. 创建报告生成脚本**

```python
#!/usr/bin/env python3
"""
生成形态检测报告
"""

import pandas as pd
from pathlib import Path
from datetime import datetime


def generate_report(code='603529', match_results=None):
    """
    生成 Markdown 格式检测报告
    """
    data_file = f'data/backtest/{code}_server_data.csv'
    df = pd.read_csv(data_file)
    df['date'] = pd.to_datetime(df['date'])
    
    # 走势分析
    report = f"""# {code} 形态检测报告

## 📋 检测概要

- **股票代码**: {code}
- **检测时间**: {df['date'].min().strftime('%Y-%m-%d')} ~ {df['date'].max().strftime('%Y-%m-%d')}
- **数据来源**: 服务器 PostgreSQL 数据库
- **数据量**: {len(df)} 条 K 线

## 📊 走势分析

| 指标 | 数值 |
|------|------|
| 交易日数量 | {len(df)} |
| 起始价 | {df.iloc[0]['close']:.2f} |
| 收盘价 | {df.iloc[-1]['close']:.2f} |
| 最高价 | {df['high'].max():.2f} |
| 最低价 | {df['low'].min():.2f} |
| 涨跌幅 | {(df.iloc[-1]['close'] - df.iloc[0]['close'])/df.iloc[0]['close']*100:.2f}% |

## 🔍 形态检测结果

{'✅ 发现信号' if match_results else '❌ 未发现信号'}

"""
    
    if match_results:
        report += "| 检测日期 | 评分 | 价格 |\n|------|------|------|\n"
        for match in match_results:
            report += f"| {match['date']} | {match['score']:.2f} | {match['price']:.2f} |\n"
    
    # 保存报告
    output_file = Path(f"{code}_检测报告.md")
    output_file.write_text(report, encoding='utf-8')
    
    print(f"✅ 报告已保存到：{output_file}")
```

---

## 🚀 快速使用

### 检测单只股票

```bash
# 1. 导出数据
python3 export_from_server_base64.py

# 2. 形态检测
python3 check_stock_from_exported.py

# 3. 生成报告
python3 generate_report.py
```

### 批量检测多只股票

```python
from export_from_server_base64 import export_via_base64
from check_stock_from_exported import check_pattern

stocks = ['603529', '600519', '000858']

for code in stocks:
    print(f"\n检测 {code}...")
    
    # 导出
    df = export_via_base64(code=code, days=300)
    if df is None:
        continue
    
    # 检测
    results = check_pattern(code=code)
    
    # 生成报告
    generate_report(code=code, match_results=results)
```

---

## 📊 输出示例

### 数据文件

```csv
date,open,high,low,close,volume,amount
2025-05-06,85.50,86.65,84.20,85.80,125000,10750000
2025-05-07,85.80,86.20,84.50,85.30,98000,8370000
...
```

### 检测报告

```markdown
# 603529 形态检测报告

## 📋 检测概要
- 股票代码：603529
- 数据量：219 条 K 线

## 📊 走势分析
- 起始价：84.34
- 收盘价：66.82
- 涨跌幅：-20.77%

## 🔍 形态检测结果
❌ 未发现信号
```

---

## 🔧 参数配置

### 形态检测参数

编辑 `config.yaml`：

```yaml
pattern:
  drop_threshold: 0.15        # 跌幅阈值 15%
  volume_shrink_ratio: 0.6    # 缩量阈值 60%
  surge_volume_ratio: 1.5     # 放量倍数 1.5
  surge_price_ratio: 0.05     # 放量涨幅 5%
  retrace_ratio: 0.5          # 回踩幅度 50%
```

### 服务器配置

编辑 `export_from_server_base64.py`：

```python
server_ip = "43.156.242.184"      # 服务器 IP
server_user = "root"              # SSH 用户
ssh_key = "/Users/yl/vscode/inspection_automation/docs/only.pem" # SSH 密钥
```

---

## 📝 最佳实践

### 1. 数据准备

- ✅ 至少获取 200 天数据（确保形态检测有足够窗口）
- ✅ 使用 SSH 密钥认证（避免密码泄露）
- ✅ 数据保存到本地（避免重复请求）
- ✅ 定期检查数据完整性

### 2. 回测验证

- ✅ 使用真实历史数据
- ✅ 多只股票批量测试
- ✅ 考虑交易成本
- ✅ 关注最大回撤和夏普比率

### 3. 参数优化

- ✅ 不要过度拟合
- ✅ 参数应有经济意义
- ✅ 样本外验证
- ✅ 保留安全边际

---

## ⚠️ 注意事项

1. **SSH 连接**: 确保 SSH 密钥权限正确（chmod 600）
2. **Docker 容器**: 确保服务器上的 stockfilter-app 容器正常运行
3. **数据库权限**: 确保数据库用户有查询权限
4. **数据质量**: 检查数据完整性（无缺失、无异常值）
5. **投资风险**: 回测结果仅供参考，不构成投资建议

---

## 🎯 技能复用

### 在其他项目中使用

**1. 复制核心文件**

```bash
cp export_from_server_base64.py your-project/
cp -r backtest/ your-project/
cp -r strategy/ your-project/
```

**2. 配置数据库连接**

修改数据库配置为你的环境：

```python
os.environ['DB_HOST'] = 'your-db-host'
os.environ['DB_PORT'] = '5432'
os.environ['DB_NAME'] = 'your-db'
os.environ['DB_USER'] = 'your-user'
os.environ['DB_PASSWORD'] = 'your-password'
```

**3. 运行导出和回测**

```bash
python3 export_from_server_base64.py
python3 check_pattern.py
```

---

## 📚 相关文档

- [回测系统使用指南](backtest/README.md)
- [股票形态筛选系统](股票形态筛选系统.md)
- [603529 真实数据检测报告](603529_真实数据检测报告.md)

---

**技能版本**: v1.0  
**最后更新**: 2026-04-02  
**适用范围**: A 股股票形态回测  
**核心优势**: SSH 远程导出 + 真实数据回测 + 完善分析
