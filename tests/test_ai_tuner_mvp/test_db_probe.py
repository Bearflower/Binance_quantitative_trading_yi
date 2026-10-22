# -*- coding: utf-8 -*-
"""
ai_tuner.readiness.db_probe 单元测试

覆盖：fetch_column_data_type / bind_datetime / bind_bounds / table_exists
全部分支；AsyncMock / MagicMock，不连真库。
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# fetch_column_data_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_column_data_type_hit_returns_data_type():
    """information_schema 命中 → 返回 data_type 字符串"""
    from ai_tuner.readiness import db_probe

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(return_value={"data_type": "timestamp without time zone"})

    result = await db_probe.fetch_column_data_type(fake_db, "s", "t", "c")
    assert result == "timestamp without time zone"


@pytest.mark.asyncio
async def test_fetch_column_data_type_missing_returns_none():
    """information_schema 未命中（fetch_one → None）→ 返回 None"""
    from ai_tuner.readiness import db_probe

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(return_value=None)

    result = await db_probe.fetch_column_data_type(fake_db, "s", "t", "c")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_column_data_type_exception_returns_none():
    """查询异常 → 返回 None 且不抛"""
    from ai_tuner.readiness import db_probe

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(side_effect=RuntimeError("boom"))

    result = await db_probe.fetch_column_data_type(fake_db, "s", "t", "c")
    assert result is None


# ---------------------------------------------------------------------------
# bind_datetime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_datetime_aware_column_aware_dt_converts_to_utc():
    """aware 列 + aware dt（非 UTC）→ 转 UTC aware"""
    from ai_tuner.readiness import db_probe

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(return_value={"data_type": db_probe.TS_AWARE})

    # 显式 +08:00 时区的 aware dt，用于验证转 UTC 的真实偏移
    tz8 = timezone(timedelta(hours=8))
    dt_plus8 = datetime(2026, 10, 17, 18, 0, tzinfo=tz8)

    result = await db_probe.bind_datetime(fake_db, "s", "t", "c", dt_plus8)
    assert result.tzinfo == timezone.utc
    assert result.hour == 10  # 18:00+08 → 10:00 UTC


@pytest.mark.asyncio
async def test_bind_datetime_aware_column_naive_dt_adds_utc():
    """aware 列 + naive dt → 补 UTC tzinfo"""
    from ai_tuner.readiness import db_probe

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(return_value={"data_type": db_probe.TS_AWARE})

    naive = datetime(2026, 10, 17, 10, 0)
    result = await db_probe.bind_datetime(fake_db, "s", "t", "c", naive)
    assert result.tzinfo == timezone.utc
    assert result.hour == 10


@pytest.mark.asyncio
async def test_bind_datetime_naive_column_strips_aware_tzinfo():
    """naive 列 + aware dt → 去 tzinfo"""
    from ai_tuner.readiness import db_probe

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(return_value={"data_type": "timestamp without time zone"})

    aware = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
    result = await db_probe.bind_datetime(fake_db, "s", "t", "c", aware)
    assert result.tzinfo is None
    assert result.hour == 10


@pytest.mark.asyncio
async def test_bind_datetime_probe_failure_strips_tzinfo():
    """探测失败（None）→ 去 tzinfo（降级 naive 行为）"""
    from ai_tuner.readiness import db_probe

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(return_value=None)

    aware = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
    result = await db_probe.bind_datetime(fake_db, "s", "t", "c", aware)
    assert result.tzinfo is None


# ---------------------------------------------------------------------------
# bind_bounds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "column_dtype",
    [
        "timestamp with time zone",
        "timestamp without time zone",
        None,  # 探测失败
    ],
)
async def test_bind_bounds_matches_two_bind_datetime_calls(column_dtype):
    """bind_bounds 结果逐项等于两次 bind_datetime；探测只发生 1 次"""
    from ai_tuner.readiness import db_probe

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(return_value=(
        {"data_type": column_dtype} if column_dtype is not None else None
    ))

    tz8 = timezone(timedelta(hours=8))
    # start: +08:00 aware dt；end: UTC aware dt（同时覆盖两种 aware 输入）
    start = datetime(2026, 10, 17, 18, 0, tzinfo=tz8)
    end = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)

    bound_start, bound_end = await db_probe.bind_bounds(
        fake_db, "s", "t", "c", start, end,
    )

    # 断言：information_schema.columns 探测只发生 1 次
    assert fake_db.fetch_one.await_count == 1, (
        f"bind_bounds 应只探测一次列类型，实际 fetch_one await_count="
        f"{fake_db.fetch_one.await_count}"
    )

    # 语义等价：分别调用两次 bind_datetime（各自探测一次）后逐项比对
    expected_start = await db_probe.bind_datetime(fake_db, "s", "t", "c", start)
    expected_end = await db_probe.bind_datetime(fake_db, "s", "t", "c", end)
    assert bound_start == expected_start
    assert bound_end == expected_end


# ---------------------------------------------------------------------------
# table_exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_exists_hit_returns_true():
    """information_schema.tables 查到行 → True"""
    from ai_tuner.readiness import db_probe

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(return_value={"?column?": 1})

    result = await db_probe.table_exists(fake_db, "s", "t")
    assert result is True


@pytest.mark.asyncio
async def test_table_exists_missing_returns_false():
    """information_schema.tables 返回 None → False"""
    from ai_tuner.readiness import db_probe

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(return_value=None)

    result = await db_probe.table_exists(fake_db, "s", "t")
    assert result is False


@pytest.mark.asyncio
async def test_table_exists_exception_returns_none():
    """查询异常 → None（三态返回：故障 ≠ 缺表）"""
    from ai_tuner.readiness import db_probe

    fake_db = MagicMock()
    fake_db.fetch_one = AsyncMock(side_effect=RuntimeError("boom"))

    result = await db_probe.table_exists(fake_db, "s", "t")
    assert result is None
