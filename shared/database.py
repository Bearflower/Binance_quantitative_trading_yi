"""
数据库管理
PostgreSQL连接池管理
"""
from typing import Optional, List, Dict, Any
import asyncpg
import re
import structlog


logger = structlog.get_logger()


class DatabaseError(Exception):
    """数据库异常"""
    pass


class SQLInjectionError(DatabaseError):
    """SQL注入异常"""
    pass


class DatabaseManager:
    """数据库管理器"""
    
    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        min_pool_size: int = 5,
        max_pool_size: int = 20
    ):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        # 使用私有属性存储密码
        self._password = password
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size
        
        self.pool: Optional[asyncpg.Pool] = None
        
        logger.info(
            "数据库管理器初始化",
            host=host,
            port=port,
            database=database,
            password=self.password  # 使用脱敏后的属性
        )
    
    @property
    def password(self) -> str:
        """
        获取脱敏后的数据库密码
        
        Returns:
            脱敏后的密码（显示前2位和后2位，中间用*代替）
        """
        if len(self._password) <= 4:
            return '*' * len(self._password)
        # 显示前2位和后2位，中间用*代替
        masked_length = len(self._password) - 4
        return f"{self._password[:2]}{'*' * masked_length}{self._password[-2:]}"
    
    def _validate_sql(self, query: str) -> None:
        """
        验证SQL语句安全性
        
        Args:
            query: SQL语句
        
        Raises:
            SQLInjectionError: 如果检测到危险的SQL语句
        """
        # 转换为大写进行检测
        query_upper = query.upper().strip()
        
        # 禁止多语句执行（检测中间的分号）
        # 移除末尾的分号后再检测
        query_stripped = query.strip().rstrip(';')
        if ';' in query_stripped:
            raise SQLInjectionError("禁止执行多条SQL语句")
        
        # 禁止危险操作
        dangerous_keywords = [
            r'\bDROP\b',
            r'\bTRUNCATE\b',
            r'\bALTER\b',
            r'\bCREATE\b',
            r'\bGRANT\b',
            r'\bREVOKE\b',
            r'\bEXEC\b',
            r'\bEXECUTE\b',
            r'\bXP_\w+',
            r'\bSP_\w+'
        ]
        
        for pattern in dangerous_keywords:
            if re.search(pattern, query_upper):
                raise SQLInjectionError(f"检测到危险的SQL操作: {pattern}")
        
        # 检测注释注入
        if '--' in query or '/*' in query or '*/' in query:
            raise SQLInjectionError("检测到SQL注释注入风险")
        
        # 检测UNION注入
        if re.search(r'\bUNION\b.*\bSELECT\b', query_upper):
            raise SQLInjectionError("检测到UNION注入风险")
    
    async def connect(self):
        """建立数据库连接池"""
        if self.pool is None:
            self.pool = await asyncpg.create_pool(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self._password,  # 使用私有属性
                min_size=self.min_pool_size,
                max_size=self.max_pool_size
            )
            
            logger.info(
                "数据库连接池已建立",
                min_size=self.min_pool_size,
                max_size=self.max_pool_size
            )
    
    async def disconnect(self):
        """关闭数据库连接池"""
        if self.pool:
            await self.pool.close()
            self.pool = None
            
            logger.info("数据库连接池已关闭")
    
    async def execute(
        self,
        query: str,
        *args,
        **kwargs
    ) -> str:
        """
        执行SQL语句（INSERT, UPDATE, DELETE）
        
        Args:
            query: SQL语句
            *args: 参数
        
        Returns:
            执行结果
        
        Raises:
            SQLInjectionError: 如果检测到危险的SQL语句
        """
        # SQL安全检查
        self._validate_sql(query)
        
        if not self.pool:
            await self.connect()
        
        async with self.pool.acquire() as conn:
            result = await conn.execute(query, *args, **kwargs)
            
            logger.debug(
                "SQL执行成功",
                query=query[:100],
                result=result
            )
            
            return result
    
    async def fetch_one(
        self,
        query: str,
        *args,
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """
        查询单条记录
        
        Args:
            query: SQL语句
            *args: 参数
        
        Returns:
            查询结果（字典）
        
        Raises:
            SQLInjectionError: 如果检测到危险的SQL语句
        """
        # SQL安全检查
        self._validate_sql(query)
        
        if not self.pool:
            await self.connect()
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *args, **kwargs)
            
            if row:
                return dict(row)
            
            return None
    
    async def fetch_all(
        self,
        query: str,
        *args,
        **kwargs
    ) -> List[Dict[str, Any]]:
        """
        查询多条记录
        
        Args:
            query: SQL语句
            *args: 参数
        
        Returns:
            查询结果列表
        
        Raises:
            SQLInjectionError: 如果检测到危险的SQL语句
        """
        # SQL安全检查
        self._validate_sql(query)
        
        if not self.pool:
            await self.connect()
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args, **kwargs)
            
            return [dict(row) for row in rows]
    
    async def execute_ddl(
        self,
        query: str,
        *args,
        **kwargs
    ) -> str:
        """
        执行DDL语句（CREATE TABLE、CREATE INDEX 等）

        绕过 _validate_sql 安全校验，仅用于系统初始化、自动建表等可信场景。
        业务代码不应调用此方法执行 DML 操作。

        Args:
            query: DDL语句
            *args: 参数

        Returns:
            执行结果

        Raises:
            ValueError: 如果检测到多条SQL语句（分号分隔）
        """
        # DDL 仅做基础安全检查：禁止多语句执行
        query_stripped = query.strip().rstrip(';')
        if ';' in query_stripped:
            raise SQLInjectionError("禁止执行多条DDL语句")

        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            result = await conn.execute(query, *args, **kwargs)

            logger.debug(
                "DDL执行成功",
                query=query[:100],
                result=result
            )

            return result

    async def execute_transaction(
        self,
        queries: List[tuple]
    ) -> bool:
        """
        执行事务

        Args:
            queries: 查询列表 [(query, args), ...]

        Returns:
            是否成功

        Raises:
            ValueError: 如果查询列表为空
            SQLInjectionError: 如果检测到危险的SQL语句
        """
        # 参数验证
        if not queries:
            raise ValueError("查询列表不能为空")

        # SQL安全检查
        for query, _ in queries:
            self._validate_sql(query)

        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for query, args in queries:
                    await conn.execute(query, *args)

        logger.info(
            "事务执行成功",
            query_count=len(queries)
        )

        return True
