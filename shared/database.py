"""
数据库管理
PostgreSQL连接池管理
"""
from typing import Optional, List, Dict, Any
import asyncpg
import re
import uuid
import structlog


logger = structlog.get_logger()

# 占用表相关 SQL 常量（占用判定/写入单点，禁止在业务层重复拼接）
#   见 docs/plans/fix-2026-09-29-p0-r01-r08-architecture.md §11
_CLAIM_SELECT_ACTIVE_SQL = (
    "SELECT strategy, id FROM trading.position_claims "
    "WHERE symbol = $1 AND claim_state IN ('PENDING','ACTIVE') "
    "ORDER BY id DESC LIMIT 1"
)
_CLAIM_RELEASE_EXPIRED_SQL = (
    "UPDATE trading.position_claims SET claim_state = 'RELEASED', released_at = NOW(), "
    "reason = 'expired' WHERE symbol = $1 AND claim_state IN ('PENDING','ACTIVE') "
    "AND expires_at <= NOW()"
)
_CLAIM_INSERT_SQL = (
    "INSERT INTO trading.position_claims "
    "(symbol, strategy, trade_intent_id, claim_state, expires_at) "
    "VALUES ($1, $2, $3, 'PENDING', NOW() + make_interval(mins => $4::int)) "
    "ON CONFLICT DO NOTHING RETURNING id"
)


class DatabaseError(Exception):
    """数据库异常"""
    pass


class SQLInjectionError(DatabaseError):
    """SQL注入异常"""
    pass


class DatabaseManager:
    """数据库管理器"""

    # 能力标记：仅真实 DatabaseManager 支持占用表原子占位；mock/旧实现据此自动降级
    supports_position_claims = True
    
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

    async def fetch_one_advisory_lock(
        self,
        lock_key: int,
        query: str,
        *args,
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """
        在 advisory lock 事务保护下执行单条查询

        用于需要"查+判"串行化的场景（如持仓归属互斥判定）：
        先取该业务 key 的 PostgreSQL advisory lock（pg_advisory_xact_lock），
        再在同一事务内执行查询，保证同 lock_key 的并发读-判被锁串行化，
        避免竞态窗口。

        Args:
            lock_key: advisory lock 键（int），调用方以 zlib.crc32 对业务 key 取 32 位
            query: SELECT 查询
            *args: 查询参数

        Returns:
            查询结果字典；无记录返回 None
        """
        if not self.pool:
            await self.connect()

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)
                row = await conn.fetchrow(query, *args, **kwargs)

        return dict(row) if row else None

    async def claim_position_atomic(
        self,
        lock_key: int,
        symbol: str,
        strategy: str,
        intent_id: Optional[str],
        ttl_minutes: int,
        *,
        competing_names: Optional[List[str]] = None,
        lock_timeout_seconds: float = 5.0,
    ) -> Dict[str, Any]:
        """
        在 advisory lock 事务内原子完成「查有效占用 + 写入本策略占用」（R07-F1/F2）

        流程（全程同一事务，锁内只做轻量「查+写」，外部请求必须在锁外）：
          1. 设置事务级 lock_timeout；获取 pg_advisory_xact_lock(lock_key)
          2. 先释放该 symbol 已过期的有效占用（部分唯一索引不区分 expires_at）
          3. 查询是否已有有效占用：
             - 占用者为本策略 => 幂等复用（claimed=True，返回既有 claim_id）
             - 占用者为对家（competing_names 为空或命中）=> claimed=False
             - 占用者非对家 => 不视为冲突，继续尝试插入
          4. INSERT（ON CONFLICT DO NOTHING）：成功则 claimed=True；
             被部分唯一索引拒绝则回查冲突方，claimed=False

        Args:
            lock_key: advisory lock 键（调用方以 zlib.crc32 对 symbol 取 32 位）
            symbol: 交易对
            strategy: 本策略归属名
            intent_id: 交易决策稳定标识；缺省时内部生成，保证 NOT NULL
            ttl_minutes: 占用有效期（分钟），写入 expires_at
            competing_names: 对家策略名列表；为空表示与任意其他策略互斥
            lock_timeout_seconds: advisory lock 获取/事务超时保护（秒）

        Returns:
            {'claimed': bool, 'owner': Optional[str], 'claim_id': Optional[int]}
            （部分唯一索引冲突返回 claimed=False，不抛异常）
        """
        if not self.pool:
            await self.connect()

        intent = intent_id or uuid.uuid4().hex
        timeout_ms = f"{int(lock_timeout_seconds * 1000)}ms"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # 事务级超时保护：锁等待超限抛错，避免长时间阻塞主循环
                await conn.execute("SELECT set_config('lock_timeout', $1, true)", timeout_ms)
                await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)
                await conn.execute(_CLAIM_RELEASE_EXPIRED_SQL, symbol)
                row = await conn.fetchrow(_CLAIM_SELECT_ACTIVE_SQL, symbol)
                if row:
                    existing_strategy = row["strategy"]
                    existing_id = row["id"]
                    if existing_strategy == strategy:
                        # 同策略重复占位幂等复用，不冲突
                        return {"claimed": True, "owner": strategy, "claim_id": existing_id}
                    if not competing_names or existing_strategy in competing_names:
                        return {"claimed": False, "owner": existing_strategy, "claim_id": existing_id}
                new_id = await conn.fetchval(
                    _CLAIM_INSERT_SQL, symbol, strategy, intent, int(ttl_minutes)
                )
                if new_id is not None:
                    return {"claimed": True, "owner": strategy, "claim_id": new_id}
                # 插入被部分唯一索引拒绝（并发/非对家已持有）：回查冲突方，不抛异常
                conflict = await conn.fetchrow(_CLAIM_SELECT_ACTIVE_SQL, symbol)
                owner = conflict["strategy"] if conflict else None
                claim_id = conflict["id"] if conflict else None
                return {"claimed": False, "owner": owner, "claim_id": claim_id}

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
