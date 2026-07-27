"""
共享数据库连接池模块

提供统一的 PostgreSQL 数据库连接管理
"""

from databases import Database
from contextlib import asynccontextmanager
from typing import Optional, AsyncGenerator
import os
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from shared.utils.logger import get_logger

logger = get_logger(__name__)


class DatabaseManager:
    """数据库管理器"""
    
    _instance: Optional["DatabaseManager"] = None
    _database: Optional[Database] = None
    
    def __new__(cls) -> "DatabaseManager":
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """初始化数据库管理器"""
        if self._database is None:
            database_url = os.getenv(
                "DATABASE_URL",
                "postgresql://binance:secure_password_here@localhost:5432/binance_data"
            )
            self._database = Database(
                database_url,
                min_size=5,
                max_size=20
            )
            logger.info("数据库管理器初始化完成")
    
    async def connect(self) -> None:
        """连接数据库"""
        if self._database and not self._database.is_connected:
            try:
                await self._database.connect()
                logger.info("数据库连接成功")
            except Exception as e:
                logger.error(f"数据库连接失败：{e}")
                raise
    
    async def disconnect(self) -> None:
        """断开数据库连接"""
        if self._database and self._database.is_connected:
            await self._database.disconnect()
            logger.info("数据库连接已关闭")
    
    @property
    def database(self) -> Optional[Database]:
        """获取数据库实例"""
        return self._database
    
    @asynccontextmanager
    async def get_connection(self) -> AsyncGenerator[Database, None]:
        """获取数据库连接（上下文管理器）"""
        if not self._database or not self._database.is_connected:
            await self.connect()
        
        try:
            yield self._database
        finally:
            pass
    
    async def execute(self, query: str, values: dict = None) -> any:
        """执行 SQL 语句"""
        async with self.get_connection() as db:
            return await db.execute(query, values)
    
    async def fetch_one(self, query: str, values: dict = None) -> any:
        """查询单条记录"""
        async with self.get_connection() as db:
            return await db.fetch_one(query, values)
    
    async def fetch_all(self, query: str, values: dict = None) -> list:
        """查询多条记录"""
        async with self.get_connection() as db:
            return await db.fetch_all(query, values)
    
    async def fetch_val(self, query: str, values: dict = None, column: int = 0) -> any:
        """查询单个值"""
        async with self.get_connection() as db:
            return await db.fetch_val(query, values, column=column)
    
    async def is_connected(self) -> bool:
        """检查是否已连接"""
        return self._database.is_connected if self._database else False


# 全局数据库管理器实例
db_manager = DatabaseManager()


@asynccontextmanager
async def get_db() -> AsyncGenerator[Database, None]:
    """获取数据库连接（依赖注入用）"""
    async with db_manager.get_connection() as db:
        yield db
