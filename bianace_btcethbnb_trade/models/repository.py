#!/usr/bin/env python3
"""
数据仓库基类

提供统一的数据访问层，封装数据库操作，提供更高级的抽象。

设计模式：Repository Pattern（数据仓库模式）
优点：
1. 分离业务逻辑和数据访问逻辑
2. 提供统一的数据访问接口
3. 便于单元测试和Mock
4. 支持查询优化和缓存

版本: v1.0.0
创建时间: 2026-04-27
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, TypeVar, Generic
from datetime import datetime
from decimal import Decimal

from models.database import get_db_connection

logger = logging.getLogger(__name__)

# 泛型类型变量
T = TypeVar('T')


class BaseRepository(ABC, Generic[T]):
    """
    数据仓库基类

    提供通用的数据库操作方法，所有具体的数据仓库类都继承自此类。

    功能：
    1. 通用的CRUD操作
    2. 统一的错误处理
    3. 统一的日志记录
    4. 查询优化支持

    使用示例：
        class TradeRepository(BaseRepository[Dict]):
            def get_by_order_id(self, order_id: int) -> Optional[Dict]:
                return self.find_one("SELECT * FROM trades WHERE order_id = %s", (order_id,))
    """

    def __init__(self, table_name: str, primary_key: str = 'id'):
        """
        初始化数据仓库

        Args:
            table_name: 表名
            primary_key: 主键字段名（默认'id'）
        """
        self.table_name = table_name
        self.primary_key = primary_key
        self.logger = logging.getLogger(f"{self.__class__.__name__}")

    # ==================== 基础查询方法 ====================

    def find_one(self, query: str, params: tuple = None) -> Optional[Dict[str, Any]]:
        """
        查询单条记录

        Args:
            query: SQL查询语句
            params: 查询参数

        Returns:
            查询结果字典，如果不存在则返回None
        """
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, params or ())
                    result = cursor.fetchone()
                    return dict(result) if result else None
        except Exception as e:
            self.logger.error(f"查询单条记录失败：{str(e)}", exc_info=True)
            raise

    def find_many(
        self,
        query: str,
        params: tuple = None,
        limit: int = None
    ) -> List[Dict[str, Any]]:
        """
        查询多条记录

        Args:
            query: SQL查询语句
            params: 查询参数
            limit: 返回记录数量限制

        Returns:
            查询结果列表
        """
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    if limit:
                        query += f" LIMIT {limit}"
                    cursor.execute(query, params or ())
                    results = cursor.fetchall()
                    return [dict(row) for row in results]
        except Exception as e:
            self.logger.error(f"查询多条记录失败：{str(e)}", exc_info=True)
            raise

    def find_by_id(self, id_value: Any) -> Optional[Dict[str, Any]]:
        """
        根据ID查询单条记录

        Args:
            id_value: 主键值

        Returns:
            查询结果字典，如果不存在则返回None
        """
        query = f"SELECT * FROM {self.table_name} WHERE {self.primary_key} = %s"
        return self.find_one(query, (id_value,))

    def find_all(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        查询所有记录

        Args:
            limit: 返回记录数量限制

        Returns:
            查询结果列表
        """
        query = f"SELECT * FROM {self.table_name} ORDER BY {self.primary_key} DESC"
        return self.find_many(query, limit=limit)

    # ==================== 插入和更新方法 ====================

    def insert(self, data: Dict[str, Any]) -> int:
        """
        插入记录

        Args:
            data: 数据字典

        Returns:
            影响的行数
        """
        try:
            columns = ', '.join(data.keys())
            placeholders = ', '.join(['%s'] * len(data))
            values = self._prepare_values(data)

            query = f"""
                INSERT INTO {self.table_name} ({columns})
                VALUES ({placeholders})
            """

            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, values)
                    conn.commit()
                    return cursor.rowcount
        except Exception as e:
            self.logger.error(f"插入记录失败：{str(e)}", exc_info=True)
            raise

    def insert_and_return_id(self, data: Dict[str, Any]) -> Any:
        """
        插入记录并返回ID

        Args:
            data: 数据字典

        Returns:
            插入记录的主键ID
        """
        try:
            columns = ', '.join(data.keys())
            placeholders = ', '.join(['%s'] * len(data))
            values = self._prepare_values(data)

            query = f"""
                INSERT INTO {self.table_name} ({columns})
                VALUES ({placeholders})
                RETURNING {self.primary_key}
            """

            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, values)
                    result = cursor.fetchone()
                    conn.commit()
                    return result[self.primary_key] if result else None
        except Exception as e:
            self.logger.error(f"插入记录失败：{str(e)}", exc_info=True)
            raise

    def update(self, id_value: Any, data: Dict[str, Any]) -> int:
        """
        更新记录

        Args:
            id_value: 主键值
            data: 更新数据字典

        Returns:
            影响的行数
        """
        try:
            set_clause = ', '.join([f"{k} = %s" for k in data.keys()])
            values = self._prepare_values(data)
            values = values + (id_value,)

            query = f"""
                UPDATE {self.table_name}
                SET {set_clause}, updated_at = CURRENT_TIMESTAMP
                WHERE {self.primary_key} = %s
            """

            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, values)
                    conn.commit()
                    return cursor.rowcount
        except Exception as e:
            self.logger.error(f"更新记录失败：{str(e)}", exc_info=True)
            raise

    def upsert(
        self,
        data: Dict[str, Any],
        conflict_columns: List[str],
        update_columns: List[str] = None
    ) -> int:
        """
        插入或更新记录（UPSERT）

        Args:
            data: 数据字典
            conflict_columns: 冲突检测列
            update_columns: 冲突时更新的列（默认更新所有列）

        Returns:
            影响的行数
        """
        try:
            columns = ', '.join(data.keys())
            placeholders = ', '.join(['%s'] * len(data))
            values = self._prepare_values(data)

            # 冲突检测
            conflict_clause = ', '.join(conflict_columns)

            # 冲突时更新的列
            if update_columns is None:
                update_columns = [k for k in data.keys() if k not in conflict_columns]

            update_clause = ', '.join([f"{k} = EXCLUDED.{k}" for k in update_columns])

            query = f"""
                INSERT INTO {self.table_name} ({columns})
                VALUES ({placeholders})
                ON CONFLICT ({conflict_clause})
                DO UPDATE SET {update_clause}, updated_at = CURRENT_TIMESTAMP
            """

            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, values)
                    conn.commit()
                    return cursor.rowcount
        except Exception as e:
            self.logger.error(f"UPSERT操作失败：{str(e)}", exc_info=True)
            raise

    def delete(self, id_value: Any) -> int:
        """
        删除记录

        Args:
            id_value: 主键值

        Returns:
            影响的行数
        """
        try:
            query = f"DELETE FROM {self.table_name} WHERE {self.primary_key} = %s"

            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, (id_value,))
                    conn.commit()
                    return cursor.rowcount
        except Exception as e:
            self.logger.error(f"删除记录失败：{str(e)}", exc_info=True)
            raise

    # ==================== 批量操作方法 ====================

    def batch_insert(self, data_list: List[Dict[str, Any]]) -> int:
        """
        批量插入记录

        Args:
            data_list: 数据字典列表

        Returns:
            影响的行数
        """
        if not data_list:
            return 0

        try:
            # 使用第一条记录的列作为模板
            columns = list(data_list[0].keys())
            columns_str = ', '.join(columns)
            placeholders = ', '.join(['%s'] * len(columns))

            query = f"""
                INSERT INTO {self.table_name} ({columns_str})
                VALUES ({placeholders})
            """

            # 准备所有值
            values_list = [self._prepare_values(data, columns) for data in data_list]

            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.executemany(query, values_list)
                    conn.commit()
                    return cursor.rowcount
        except Exception as e:
            self.logger.error(f"批量插入失败：{str(e)}", exc_info=True)
            raise

    # ==================== 统计和查询方法 ====================

    def count(self, where_clause: str = None, params: tuple = None) -> int:
        """
        统计记录数量

        Args:
            where_clause: WHERE子句（不含WHERE关键字）
            params: 查询参数

        Returns:
            记录数量
        """
        try:
            query = f"SELECT COUNT(*) as count FROM {self.table_name}"
            if where_clause:
                query += f" WHERE {where_clause}"

            result = self.find_one(query, params)
            return result['count'] if result else 0
        except Exception as e:
            self.logger.error(f"统计记录失败：{str(e)}", exc_info=True)
            raise

    def exists(self, where_clause: str, params: tuple) -> bool:
        """
        检查记录是否存在

        Args:
            where_clause: WHERE子句（不含WHERE关键字）
            params: 查询参数

        Returns:
            是否存在
        """
        return self.count(where_clause, params) > 0

    def sum(
        self,
        column: str,
        where_clause: str = None,
        params: tuple = None
    ) -> Decimal:
        """
        求和

        Args:
            column: 求和列名
            where_clause: WHERE子句
            params: 查询参数

        Returns:
            求和结果
        """
        try:
            query = f"SELECT COALESCE(SUM({column}), 0) as total FROM {self.table_name}"
            if where_clause:
                query += f" WHERE {where_clause}"

            result = self.find_one(query, params)
            return Decimal(str(result['total'])) if result else Decimal('0')
        except Exception as e:
            self.logger.error(f"求和失败：{str(e)}", exc_info=True)
            raise

    # ==================== 辅助方法 ====================

    def _prepare_values(self, data: Dict[str, Any], columns: List[str] = None) -> tuple:
        """
        准备参数值

        Args:
            data: 数据字典
            columns: 列名列表（如果指定，按此顺序取值）

        Returns:
            参数元组
        """
        if columns:
            return tuple(data.get(col) for col in columns)
        return tuple(data.values())

    def execute_query(self, query: str, params: tuple = None) -> int:
        """
        执行查询（INSERT/UPDATE/DELETE）

        Args:
            query: SQL语句
            params: 参数

        Returns:
            影响的行数
        """
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(query, params or ())
                    conn.commit()
                    return cursor.rowcount
        except Exception as e:
            self.logger.error(f"执行查询失败：{str(e)}", exc_info=True)
            raise

    # ==================== 抽象方法 ====================

    @abstractmethod
    def get_entity_name(self) -> str:
        """
        获取实体名称

        Returns:
            实体名称
        """
        pass
