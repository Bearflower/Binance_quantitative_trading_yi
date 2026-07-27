#!/usr/bin/env python3
"""
执行数据库迁移
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from shared.core.database import db_manager
from db.migrations import create_registered_symbols_table


async def migrate_up():
    """执行向上迁移"""
    print("开始执行数据库迁移...")
    
    try:
        # 连接数据库
        await db_manager.connect()
        print("✅ 数据库连接成功")
        
        async with db_manager.get_connection() as conn:
            # 执行迁移
            await create_registered_symbols_table.migrate_up(conn)
            
        print("✅ 数据库迁移完成")
        
    except Exception as e:
        print(f"❌ 迁移失败：{e}")
        raise
    finally:
        await db_manager.disconnect()


async def migrate_down():
    """执行向下迁移"""
    print("开始回滚数据库迁移...")
    
    try:
        # 连接数据库
        await db_manager.connect()
        print("✅ 数据库连接成功")
        
        async with db_manager.get_connection() as conn:
            # 执行回滚
            await create_registered_symbols_table.migrate_down(conn)
            
        print("✅ 数据库回滚完成")
        
    except Exception as e:
        print(f"❌ 回滚失败：{e}")
        raise
    finally:
        await db_manager.disconnect()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "down":
        asyncio.run(migrate_down())
    else:
        asyncio.run(migrate_up())
