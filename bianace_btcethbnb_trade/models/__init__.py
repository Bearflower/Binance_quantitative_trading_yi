# Models package
"""
数据模型包

提供数据库访问和数据仓库功能。

模块：
- database: 数据库连接和管理
- repository: 数据仓库基类
- entities: 具体的数据仓库实现
"""

from models.database import (
    DatabaseManager,
    get_db_manager,
    get_db_connection,
    get_connection_pool
)

from models.repository import BaseRepository

from models.entities import (
    TradeRepository,
    FrequencyRepository,
    PerformanceRepository
)

__all__ = [
    # 数据库管理
    'DatabaseManager',
    'get_db_manager',
    'get_db_connection',
    'get_connection_pool',

    # 数据仓库基类
    'BaseRepository',

    # 具体数据仓库
    'TradeRepository',
    'FrequencyRepository',
    'PerformanceRepository',
]
