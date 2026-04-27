#!/usr/bin/env python3
"""
检查 603529 爱玛科技在 V2.4 版本下是否满足形态
时间范围：2025-08-25 到 2026-03-30
使用服务器数据库查询
"""

import pandas as pd
import sys
from pathlib import Path
from datetime import datetime, timedelta

# 添加项目根目录到路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from backtester_v24 import BacktesterV24

def fetch_data_from_server():
    """从服务器获取 603529 的数据"""
    import subprocess
    
    # SSH 命令
    ssh_cmd = [
        'ssh', '-o', 'StrictHostKeyChecking=no', '-i', '/Users/yl/vscode/inspection_automation/docs/only.pem',
        'root@43.156.242.184',
        '''
        cd /root/stockfilter && python3 << 'PYEOF'
import pandas as pd
import psycopg2
from psycopg2 import sql

# 连接数据库
conn = psycopg2.connect(
    host="localhost",
    database="stockfilter",
    user="postgres",
    password="postgres123"
)

# 查询 603529 的数据
query = """
SELECT date, open, high, low, close, volume, amount
FROM klines
WHERE stock_code = '603529'
  AND date >= '2025-08-25'
  AND date <= '2026-03-30'
ORDER BY date
"""

df = pd.read_sql_query(query, conn)
conn.close()

# 输出为 CSV 格式
print(df.to_csv(index=False))
PYEOF
        '''
    ]
    
    try:
        result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            csv_data = result.stdout
            # 解析 CSV
            from io import StringIO
            df = pd.read_csv(StringIO(csv_data))
            return df
        else:
            print(f"❌ SSH 执行失败：{result.stderr}")
            return None
    except Exception as e:
        print(f"❌ 获取数据失败：{e}")
        return None

def check_603529_v24():
    print(f"📊 603529 爱玛科技 V2.4 形态检测")
    print(f"时间范围：2025-08-25 到 2026-03-30")
    print("=" * 80)
    
    # 尝试从服务器获取数据
    print("正在从服务器数据库获取数据...")
    df = fetch_data_from_server()
    
    if df is None or len(df) == 0:
        print("❌ 无法从服务器获取数据")
        print("\n尝试使用 Baostock 实时获取数据...")
        
        # 使用 Baostock 获取数据
        try:
            import baostock as bs
            
            # 登录
            lg = bs.login()
            
            # 获取日线数据（后复权）
            rs = bs.query_history_k_data_plus(
                "sh.603529",
                "date,open,high,low,close,volume,amount",
                start_date="2025-08-01",
                end_date="2026-03-30",
                frequency="d",
                adjustflag="2"  # 后复权
            )
            
            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())
            
            df = pd.DataFrame(data_list, columns=rs.fields)
            
            # 登出
            bs.logout()
            
            if len(df) == 0:
                print("❌ Baostock 也未获取到数据")
                return
            
            print(f"✅ 从 Baostock 获取到 {len(df)} 条数据")
            
        except Exception as e:
            print(f"❌ Baostock 获取失败：{e}")
            return
    
    # 数据预处理
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date')
    
    # 转换数值列
    numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount']
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    
    print(f"\n数据概览：")
    print(f"  数据条数：{len(df)}")
    print(f"  日期范围：{df['date'].min().date()} 到 {df['date'].max().date()}")
    print(f"  最新收盘价：{df['close'].iloc[-1]:.2f}")
    print("=" * 80)
    
    # 创建 V2.4 回测器
    backtester = BacktesterV24()
    
    # 检测形态
    patterns = backtester.check_all_patterns(
        df=df,
        code="603529",
        period_start="2025-08-25",
        period_end="2026-03-30"
    )
    
    if patterns:
        print(f"\n✅ 检测到 {len(patterns)} 个满足 V2.4 形态的信号！\n")
        
        for i, pattern in enumerate(patterns, 1):
            print(f"{'='*80}")
            print(f"信号 {i}:")
            for key, value in pattern.items():
                if isinstance(value, float):
                    print(f"  {key}: {value:.4f}" if value < 1 else f"  {key}: {value:.2f}")
                else:
                    print(f"  {key}: {value}")
            print(f"{'='*80}\n")
    else:
        print(f"\n❌ 在 2025-08-25 到 2026-03-30 期间未检测到满足 V2.4 形态的信号\n")
        
        # 分析原因
        print("=" * 80)
        print("可能的原因分析：")
        print("=" * 80)
        
        # 检查跌幅
        if len(df) > 30:
            df_calc = df.copy()
            df_calc['max_close_30'] = df_calc['close'].rolling(window=30, min_periods=1).max()
            df_calc['drop_from_max'] = (df_calc['max_close_30'] - df_calc['close']) / df_calc['max_close_30']
            
            max_drop = df_calc['drop_from_max'].max()
            max_drop_date = df_calc.loc[df_calc['drop_from_max'].idxmax(), 'date']
            print(f"1. 期间最大跌幅：{max_drop:.2%} (V2.4 要求：≥8%)")
            print(f"   日期：{max_drop_date}")
            
            if max_drop < 0.08:
                print(f"   ❌ 跌幅不足，这是主要原因\n")
            else:
                print(f"   ✅ 跌幅满足要求\n")
        
        # 检查成交量
        if len(df) > 20:
            df_calc = df.copy()
            df_calc['avg_volume_20'] = df_calc['volume'].rolling(window=20, min_periods=1).mean()
            df_calc['volume_ratio'] = df_calc['volume'] / df_calc['avg_volume_20']
            
            min_volume_ratio = df_calc['volume_ratio'].min()
            max_volume_ratio = df_calc['volume_ratio'].max()
            print(f"2. 量比范围：{min_volume_ratio:.2f} - {max_volume_ratio:.2f} (V2.4 要求：1.2-15)")
            
            if max_volume_ratio < 1.2:
                print(f"   ❌ 成交量未明显放大\n")
            else:
                print(f"   ✅ 成交量有放大\n")
        
        # 检查放量上涨
        if len(df) > 20:
            df_calc = df.copy()
            df_calc['avg_volume_20'] = df_calc['volume'].rolling(window=20, min_periods=1).mean()
            df_calc['volume_ratio'] = df_calc['volume'] / df_calc['avg_volume_20']
            df_calc['daily_return'] = df_calc['close'].pct_change()
            
            surge_days = df_calc[(df_calc['daily_return'] >= 0.03) & (df_calc['volume_ratio'] >= 1.2)]
            print(f"3. 放量上涨天数（涨幅≥3% 且量比≥1.2）：{len(surge_days)}")
            
            if len(surge_days) == 0:
                print(f"   ❌ 没有明显的放量上涨\n")
            else:
                print(f"   ✅ 有放量上涨\n")
                print(f"   最近一次：{surge_days['date'].iloc[-1]}，涨幅：{surge_days['daily_return'].iloc[-1]:.2%}")
        
        print("=" * 80)

if __name__ == "__main__":
    check_603529_v24()
