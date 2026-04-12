#!/usr/bin/env python3
"""查看 trade_records 表结构"""
import sys
sys.path.append('/app')
from models.database import get_db_connection

with get_db_connection() as conn:
    with conn.cursor() as cursor:
        cursor.execute("""
            SELECT column_name, data_type 
            FROM information_schema.columns 
            WHERE table_name = 'trade_records'
            ORDER BY ordinal_position
        """)
        results = cursor.fetchall()
        print("\n=== trade_records 表结构 ===")
        for row in results:
            print(f"{row['column_name']}: {row['data_type']}")
