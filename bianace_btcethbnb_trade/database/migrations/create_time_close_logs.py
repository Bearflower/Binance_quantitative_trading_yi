#!/usr/bin/env python3
"""
数据库迁移脚本 - v6.13.3

新增 time_close_logs 表用于记录时间平仓日志
"""

import psycopg2
import os
from dotenv import load_dotenv

load_dotenv()

# 获取数据库连接配置
DATABASE_URL = os.getenv('DATABASE_URL')

def create_time_close_logs_table():
    """创建时间平仓日志表"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        # 创建 time_close_logs 表
        create_table_sql = """
        CREATE TABLE IF NOT EXISTS time_close_logs (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            position_side VARCHAR(10) NOT NULL,
            reason TEXT NOT NULL,
            order_id BIGINT NOT NULL,
            close_time TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            
            -- 索引
            INDEX idx_symbol (symbol),
            INDEX idx_close_time (close_time),
            INDEX idx_order_id (order_id)
        );
        """
        
        cur.execute(create_table_sql)
        conn.commit()
        
        print("✅ time_close_logs 表创建成功")
        
        cur.close()
        conn.close()
        
    except Exception as e:
        print(f"❌ 创建表失败：{str(e)}")
        raise

if __name__ == '__main__':
    print("=" * 60)
    print("数据库迁移 - v6.13.3")
    print("=" * 60)
    print("创建 time_close_logs 表...")
    create_time_close_logs_table()
    print("=" * 60)
    print("迁移完成！")
    print("=" * 60)
