"""
创建已注册标的配置表
"""

async def migrate_up(conn):
    """创建表"""
    
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS registered_symbols (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL UNIQUE,
            intervals TEXT[] NOT NULL,
            registered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            duration_days INTEGER NOT NULL DEFAULT 10,
            priority VARCHAR(20) NOT NULL DEFAULT 'normal',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            created_by VARCHAR(50) NOT NULL DEFAULT 'system',
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (duration_days >= 1 AND duration_days <= 30),
            CHECK (priority IN ('high', 'normal', 'low')),
            CHECK (status IN ('active', 'expired', 'cancelled'))
        )
    """)
    
    # 创建索引
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_registered_symbols_status 
        ON registered_symbols (status)
    """)
    
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_registered_symbols_expires_at 
        ON registered_symbols (expires_at)
    """)
    
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_registered_symbols_symbol 
        ON registered_symbols (symbol)
    """)
    
    print("✅ 已创建 registered_symbols 表")


async def migrate_down(conn):
    """删除表"""
    
    await conn.execute("DROP TABLE IF EXISTS registered_symbols CASCADE")
    print("✅ 已删除 registered_symbols 表")
