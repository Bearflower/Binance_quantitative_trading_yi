#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清空 K 线数据表"""

from data.database import DatabaseManager

db = DatabaseManager()
try:
    db.conn.execute('TRUNCATE TABLE klines RESTART IDENTITY')
    db.conn.commit()
    print("✅ K 线表已清空")
except Exception as e:
    print(f"❌ 清空失败：{e}")
finally:
    db.close()
