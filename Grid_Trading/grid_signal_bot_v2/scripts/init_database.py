"""
数据库初始化脚本
创建网格信号灯系统所需的数据库表
"""

import os
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 数据库连接配置
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://binance:Bianace%402024@43.156.242.184:5432/binance_data"
)

# SQL 脚本
CREATE_TABLES_SQL = """
-- 创建 Schema
CREATE SCHEMA IF NOT EXISTS grid_signal;

-- 设置默认搜索路径
SET search_path TO grid_signal, public;

-- 1. 信号推送历史表
CREATE TABLE IF NOT EXISTS grid_signals (
    id SERIAL PRIMARY KEY,
    signal_time TIMESTAMP NOT NULL,
    market_state VARCHAR(20) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    grid_params JSONB NOT NULL,
    is_pushed BOOLEAN DEFAULT FALSE,
    pushed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. 市场状态历史表
CREATE TABLE IF NOT EXISTS market_states (
    id SERIAL PRIMARY KEY,
    check_time TIMESTAMP NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    state VARCHAR(20) NOT NULL,
    adx DECIMAL(5, 2),
    adx_4h DECIMAL(5, 2),
    ema_fast DECIMAL(12, 2),
    ema_slow DECIMAL(12, 2),
    trend_strength DECIMAL(5, 4),
    confidence DECIMAL(5, 4),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. 网格参数历史表
CREATE TABLE IF NOT EXISTS grid_parameters (
    id SERIAL PRIMARY KEY,
    create_time TIMESTAMP NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    market_state VARCHAR(20) NOT NULL,
    upper_price DECIMAL(12, 2) NOT NULL,
    lower_price DECIMAL(12, 2) NOT NULL,
    grid_count INTEGER NOT NULL,
    grid_type VARCHAR(20) NOT NULL,
    grid_direction VARCHAR(20) NOT NULL,
    leverage INTEGER NOT NULL,
    total_investment DECIMAL(12, 2) NOT NULL,
    stop_upper_price DECIMAL(12, 2),
    stop_lower_price DECIMAL(12, 2),
    terminate_upper_price DECIMAL(12, 2) NOT NULL,
    terminate_lower_price DECIMAL(12, 2) NOT NULL,
    atr_value DECIMAL(12, 2) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. 触发事件记录表
CREATE TABLE IF NOT EXISTS trigger_events (
    id SERIAL PRIMARY KEY,
    trigger_time TIMESTAMP NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    trigger_type VARCHAR(50) NOT NULL,
    description TEXT NOT NULL,
    severity DECIMAL(3, 2),
    details JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 5. 推送记录表
CREATE TABLE IF NOT EXISTS notification_logs (
    id SERIAL PRIMARY KEY,
    push_time TIMESTAMP NOT NULL,
    signal_id INTEGER REFERENCES grid_signals(id),
    notification_type VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def init_database():
    """初始化数据库"""
    print("=" * 60)
    print("🗄️  数据库初始化开始")
    print("=" * 60)
    
    try:
        # 连接数据库
        print(f"\n📡 连接数据库...")
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        cursor = conn.cursor()
        
        print("✅ 数据库连接成功")
        
        # 执行 SQL 脚本
        print("\n📝 创建数据表...")
        cursor.execute(CREATE_TABLES_SQL)
        
        print("✅ 数据表创建成功")
        
        # 验证表是否创建成功
        print("\n🔍 验证表结构...")
        cursor.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'grid_signal'
            ORDER BY table_name;
        """)
        
        tables = cursor.fetchall()
        print(f"\n已创建的表：")
        for table in tables:
            print(f"  - {table[0]}")
        
        # 关闭连接
        cursor.close()
        conn.close()
        
        print("\n" + "=" * 60)
        print("✅ 数据库初始化完成！")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ 数据库初始化失败：{e}")
        sys.exit(1)


if __name__ == "__main__":
    init_database()
