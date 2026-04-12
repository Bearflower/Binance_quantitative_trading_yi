# Stock-Backtest 技能使用示例

## 🎯 示例 1: 检测单只股票

### 场景
你想检测 603529 爱玛科技是否满足形态条件。

### 步骤

**1. 导出数据**
```bash
cd /Users/yl/vscode/stockfilter
python3 export_from_server_base64.py
```

**预期输出**:
```
从服务器导出 603529 的 300 天 K 线数据...
✅ 成功导出 219 条数据，保存到：data/backtest/603529_server_data.csv
```

**2. 形态检测**
```bash
python3 check_603529_from_exported.py
```

**预期输出**:
```
加载 219 条数据
检测日期：2025-09-30
使用数据：105 条 K 线
形态检测结果：False
...
检测结果汇总
❌ 在检测的日期中未发现满足形态条件的股票
```

**3. 查看报告**
```bash
cat 603529_真实数据检测报告.md
```

---

## 🎯 示例 2: 检测贵州茅台

### 场景
你想检测 600519 贵州茅台的形态。

### 步骤

**1. 修改导出脚本**

编辑 `export_from_server_base64.py`:
```python
code = '600519'  # 改为贵州茅台
days = 300
```

**2. 导出数据**
```bash
python3 export_from_server_base64.py
```

**3. 修改检测脚本**

编辑 `check_603529_from_exported.py`:
```python
# 修改加载数据部分
data_file = 'data/backtest/600519_server_data.csv'
```

**4. 运行检测**
```bash
python3 check_603529_from_exported.py
```

---

## 🎯 示例 3: 批量检测多只股票

### 场景
你想批量检测 10 只股票，找出满足形态条件的。

### 创建批量检测脚本

创建 `batch_check.py`:

```python
#!/usr/bin/env python3
"""
批量检测多只股票
"""

import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from export_from_server_base64 import export_via_base64
from strategy.pattern_detector import PatternDetector
from strategy.scoring import PatternScorer
from strategy.params import ConfigLoader


def check_single_stock(code):
    """检测单只股票"""
    print(f"\n{'='*60}")
    print(f"检测 {code}")
    print(f"{'='*60}")
    
    # 1. 导出数据
    df = export_via_base64(code=code, days=300)
    if df is None or len(df) < 200:
        print(f"✗ {code} 数据不足，跳过")
        return None
    
    # 2. 形态检测
    config = ConfigLoader('config.yaml')
    pattern_params = config.get_pattern_params()
    scoring_weights = config.get_scoring_weights()
    
    detector = PatternDetector(pattern_params)
    scorer = PatternScorer(scoring_weights)
    
    # 检测最近一个交易日
    df_check = df.tail(120)
    is_match, detail = detector.check_pattern(df_check)
    
    if is_match:
        score = scorer.score(detail, pattern_params)
        print(f"✅ {code} 满足形态条件！")
        print(f"   评分：{score:.2f}")
        print(f"   价格：{detail.get('current_close', 0):.2f}")
        return {
            'code': code,
            'score': score,
            'price': detail.get('current_close', 0),
            'detail': detail
        }
    else:
        print(f"❌ {code} 不满足形态条件")
        return None


def main():
    # 股票列表
    stocks = [
        '603529',  # 爱玛科技
        '600519',  # 贵州茅台
        '000858',  # 五粮液
        '002415',  # 海康威视
        '601318',  # 中国平安
        '600030',  # 中信证券
        '601166',  # 兴业银行
        '600276',  # 恒瑞医药
        '000333',  # 美的集团
        '600900'   # 长江电力
    ]
    
    results = []
    
    for code in stocks:
        result = check_single_stock(code)
        if result:
            results.append(result)
    
    # 汇总
    print(f"\n{'='*60}")
    print("批量检测汇总")
    print(f"{'='*60}")
    
    if results:
        print(f"\n发现 {len(results)} 只股票满足形态条件:")
        for r in sorted(results, key=lambda x: x['score'], reverse=True):
            print(f"  {r['code']}: {r['price']:.2f} (评分：{r['score']:.2f})")
    else:
        print("\n未发现满足形态条件的股票")


if __name__ == '__main__':
    main()
```

**运行批量检测**:
```bash
python3 batch_check.py
```

---

## 🎯 示例 4: 参数优化

### 场景
你想找到最优的形态检测参数。

### 创建参数优化脚本

创建 `optimize_params.py`:

```python
#!/usr/bin/env python3
"""
参数优化示例
"""

import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from backtest.optimizer import ParameterOptimizer
from backtest.data_manager import BacktestDataManager
from strategy.params import ConfigLoader


def main():
    # 加载数据
    data_manager = BacktestDataManager()
    data_file = 'data/backtest/603529_server_data.csv'
    df = data_manager.load_from_parquet(data_file.replace('.csv', '.parquet'))
    
    if df is None:
        # 从 CSV 加载
        import pandas as pd
        df = pd.read_csv(data_file)
        df['date'] = pd.to_datetime(df['date'])
    
    data = {'603529.SH': df}
    
    # 配置参数范围
    base_params = {
        'drop_period': 20,
        'support_lookback': 5,
    }
    
    param_ranges = {
        'drop_threshold': [0.10, 0.15, 0.20],
        'volume_shrink_ratio': [0.5, 0.6, 0.7],
        'surge_volume_ratio': [1.3, 1.5, 1.8],
        'retrace_ratio': [0.3, 0.5, 0.6]
    }
    
    # 运行优化
    optimizer = ParameterOptimizer(
        base_params=base_params,
        param_ranges=param_ranges,
        optimization_metric='sharpe_ratio'
    )
    
    print("开始参数优化...")
    results = optimizer.grid_search(
        data=data,
        start_date=datetime(2025, 8, 25),
        end_date=datetime(2026, 3, 20),
        max_workers=1
    )
    
    # 输出结果
    print(f"\n最优参数：{results['best_params']}")
    print(f"最优夏普比率：{results['best_score']:.4f}")
    
    # 保存结果
    optimizer.export_results('output/param_optimization.json')


if __name__ == '__main__':
    main()
```

**运行参数优化**:
```bash
python3 optimize_params.py
```

---

## 🎯 示例 5: 生成可视化报告

### 场景
你想生成可视化的回测报告。

### 创建可视化脚本

创建 `visualize_backtest.py`:

```python
#!/usr/bin/env python3
"""
可视化回测结果
"""

import sys
from pathlib import Path
import pandas as pd
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from backtest.engine import BacktestEngine
from backtest.visualizer import BacktestVisualizer
from strategy.params import ConfigLoader


def main():
    # 加载数据
    data_file = 'data/backtest/603529_server_data.csv'
    df = pd.read_csv(data_file)
    df['date'] = pd.to_datetime(df['date'])
    
    data = {'603529.SH': df}
    
    # 配置回测引擎
    config = ConfigLoader('config.yaml')
    pattern_params = config.get_pattern_params()
    
    engine = BacktestEngine(
        initial_cash=1000000,
        max_positions=3,
        min_score=70.0
    )
    
    engine.setup_strategy(pattern_params)
    
    # 运行回测
    report = engine.run(
        data=data,
        start_date=datetime(2025, 8, 25),
        end_date=datetime(2026, 3, 20)
    )
    
    # 可视化
    visualizer = BacktestVisualizer(report)
    visualizer.plot_all()
    
    print("✅ 可视化报告已生成")
    print("查看：output/visualization/backtest_report.html")


if __name__ == '__main__':
    main()
```

**运行可视化**:
```bash
python3 visualize_backtest.py
```

**查看报告**:
```bash
open output/visualization/backtest_report.html
```

---

## 🎯 示例 6: 定时自动检测

### 场景
你想每天收盘后自动检测所有股票。

### 创建定时任务脚本

创建 `daily_scan.py`:

```python
#!/usr/bin/env python3
"""
每日自动扫描
"""

import sys
from pathlib import Path
from datetime import datetime
import json

sys.path.insert(0, str(Path(__file__).parent))

from export_from_server_base64 import export_via_base64
from strategy.pattern_detector import PatternDetector
from strategy.scoring import PatternScorer
from strategy.params import ConfigLoader
from output.feishu import send_feishu_message  # 假设有飞书推送模块


def get_stock_list():
    """获取股票列表（从数据库或文件）"""
    # 这里简化处理，实际应该从数据库获取
    return [
        '603529', '600519', '000858', '002415',
        '601318', '600030', '601166', '600276'
    ]


def daily_scan():
    """执行每日扫描"""
    print(f"\n{'='*60}")
    print(f"每日扫描 - {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*60}")
    
    stocks = get_stock_list()
    config = ConfigLoader('config.yaml')
    pattern_params = config.get_pattern_params()
    scoring_weights = config.get_scoring_weights()
    
    detector = PatternDetector(pattern_params)
    scorer = PatternScorer(scoring_weights)
    
    matches = []
    
    for code in stocks:
        print(f"\n检测 {code}...")
        
        # 导出数据
        df = export_via_base64(code=code, days=300)
        if df is None or len(df) < 200:
            continue
        
        # 形态检测
        df_check = df.tail(120)
        is_match, detail = detector.check_pattern(df_check)
        
        if is_match:
            score = scorer.score(detail, pattern_params)
            matches.append({
                'code': code,
                'score': score,
                'price': detail.get('current_close', 0),
                'date': datetime.now().strftime('%Y-%m-%d')
            })
            
            print(f"✅ {code} 满足条件！评分：{score:.2f}")
    
    # 推送结果
    if matches:
        message = f"📈 今日发现 {len(matches)} 只股票满足形态条件:\n\n"
        for match in sorted(matches, key=lambda x: x['score'], reverse=True):
            message += f"{match['code']}: {match['price']:.2f} (评分：{match['score']:.2f})\n"
        
        send_feishu_message(message)
        print(f"\n已推送 {len(matches)} 只股票到飞书")
    
    # 保存结果
    output_file = f"output/daily_scan_{datetime.now().strftime('%Y%m%d')}.json"
    with open(output_file, 'w') as f:
        json.dump(matches, f, indent=2)
    
    print(f"\n结果已保存到：{output_file}")
    return matches


if __name__ == '__main__':
    daily_scan()
```

### 设置定时任务

```bash
# 编辑 crontab
crontab -e

# 添加每日 15:30 执行（交易日）
30 15 * * 1-5 cd /Users/yl/vscode/stockfilter && python3 daily_scan.py >> logs/daily_scan.log 2>&1
```

---

## 📝 总结

这些示例展示了 stock-backtest 技能的各种使用场景：

1. ✅ **单只股票检测** - 基础用法
2. ✅ **自定义股票** - 灵活配置
3. ✅ **批量检测** - 提高效率
4. ✅ **参数优化** - 策略调优
5. ✅ **可视化报告** - 直观展示
6. ✅ **定时任务** - 自动化

根据你的需求选择合适的示例，快速上手使用这个技能！
