#!/usr/bin/env python3
"""统计过去7天各币种的交易情况"""

import asyncio
import asyncpg
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv

load_dotenv()

async def main():
    db_url = os.getenv('DATABASE_URL', 'postgresql://binance:test_password_123456@47.99.141.133:5432/trading')
    
    try:
        conn = await asyncpg.connect(db_url)
        
        # 监控的币种列表
        symbols = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'TRXUSDT']
        
        # 查询过去7天的交易记录
        query = """
        SELECT symbol, COUNT(*) as trade_count, MAX(created_at) as last_trade_time
        FROM trade_records
        WHERE created_at >= NOW() - INTERVAL '7 days'
        GROUP BY symbol
        ORDER BY trade_count DESC
        """
        
        rows = await conn.fetch(query)
        
        print("\n" + "="*60)
        print("过去7天交易统计")
        print("="*60)
        
        traded_symbols = {}
        for row in rows:
            traded_symbols[row['symbol']] = {
                'count': row['trade_count'],
                'last_time': row['last_trade_time']
            }
            print(f"{row['symbol']:12} | 交易次数: {row['trade_count']:3} | 最后交易时间: {row['last_trade_time']}")
        
        print("\n" + "="*60)
        print("过去7天未交易的币种")
        print("="*60)
        
        no_trade_symbols = []
        for symbol in symbols:
            if symbol not in traded_symbols:
                no_trade_symbols.append(symbol)
                print(f"❌ {symbol}")
        
        if not no_trade_symbols:
            print("✅ 所有币种都有交易记录")
        
        print("\n" + "="*60)
        
        await conn.close()
        
    except Exception as e:
        print(f"错误: {e}")

if __name__ == "__main__":
    asyncio.run(main())
