# -*- coding: utf-8 -*-
"""
DB schema / table / column 探测与时间列类型绑定（fail-closed 不变量的统一入口）

auditor.py 与 kline_coverage.py 都需要探测列 data_type、
按列类型把 datetime 正确绑定到 asyncpg（naive 去 tzinfo、aware 补 UTC）。
这些逻辑原本在两处各自实现（复制粘贴），现统一到本模块，
消除重复代码，确保探测 SQL、bind 行为、异常兜底口径完全一致。

本模块同时为 ai-tuner 镜像内的运行时提供"不依赖 services/ 包"的能力
（ai_tuner/Dockerfile 不 COPY services/，且 services.kline_service.core
依赖 shared.utils.logger 单文件模块不是包，运行时 ImportError）。
"""

from datetime import datetime, timezone
from typing import Optional

import structlog

logger = structlog.get_logger()


# information_schema.data_type aware 字面量
# 与 PostgreSQL information_schema.columns.data_type 口径一致
TS_AWARE = "timestamp with time zone"


async def fetch_column_data_type(db, schema: str, table: str, column: str
                                 ) -> Optional[str]:
    """通过 information_schema.columns 探测目标列的 data_type

    Args:
        db:     异步 DB 接口（fetch_one）
        schema: schema 名
        table:  表名
        column: 列名

    Returns:
        "timestamp with time zone" / "timestamp without time zone" /
        其他 data_type 字符串；探测异常 → None（fail-closed，不炸）。
    """
    sql = """
    SELECT data_type
    FROM information_schema.columns
    WHERE table_schema = $1 AND table_name = $2 AND column_name = $3
    """
    try:
        row = await db.fetch_one(sql, schema, table, column)
        return row["data_type"] if row else None
    except Exception as exc:
        logger.warning("列类型探测失败", schema=schema, table=table,
                       column=column, error=str(exc))
        return None


def _coerce_for_dtype(dtype: Optional[str], dt: datetime) -> datetime:
    """按已探测到的列 data_type 规整 dt（aware → UTC、naive → 去 tzinfo）"""
    if dtype == TS_AWARE:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    # 显式 naive 或探测失败 → 去 tzinfo
    return dt.replace(tzinfo=None)


async def bind_datetime(db, schema: str, table: str, column: str,
                        dt: datetime) -> datetime:
    """按目标时间列类型把 dt 正确绑定到 asyncpg

    消除 asyncpg naive/aware 混用炸掉连接的问题。

    行为：
      - aware 列 → dt.astimezone(timezone.utc)（dt 无 tzinfo 则补 UTC）
      - naive 列 → dt.replace(tzinfo=None)
      - 探测失败（None）→ 降级 naive 去 tzinfo（保证不炸）

    Args:
        db:     异步 DB 接口
        schema: schema 名（用于探测 data_type）
        table:  表名
        column: 时间列名
        dt:     要绑定的 datetime（tz-aware 或 naive 均可）

    Returns:
        正确适配该列类型的 datetime
    """
    dtype = await fetch_column_data_type(db, schema, table, column)
    return _coerce_for_dtype(dtype, dt)


async def bind_bounds(db, schema: str, table: str, column: str,
                      start: datetime, end: datetime
                      ) -> tuple[datetime, datetime]:
    """一次探测同时绑定窗口起止，消除调用方逐行重复的两次 bind_datetime

    与两次调用 bind_datetime 的返回值完全一致；仅把 information_schema
    探测从 2 次降为 1 次（同一列无需重复探测）。
    """
    dtype = await fetch_column_data_type(db, schema, table, column)
    return _coerce_for_dtype(dtype, start), _coerce_for_dtype(dtype, end)


async def table_exists(db, schema: str, table: str) -> Optional[bool]:
    """information_schema.tables 查表是否存在

    Args:
        db:     异步 DB 接口
        schema: schema 名
        table:  表名（含前缀）

    Returns:
        True 表示存在，False 表示查询成功且确定不存在；探测异常 → None。
        三态返回让调用方能够 fail-closed，同时避免把数据库故障伪装成缺表。
    """
    sql = """
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = $1 AND table_name = $2
    """
    try:
        row = await db.fetch_one(sql, schema, table)
        return row is not None
    except Exception as exc:
        logger.warning("表存在性探测失败", schema=schema, table=table, error=str(exc))
        return None
