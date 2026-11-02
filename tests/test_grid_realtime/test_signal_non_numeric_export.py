"""无数值边界提醒的出口确认：只改变会话可用性，不伪造参考。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from strategies.grid.realtime.reference_store import (
    ExportStore, ReferenceReader, SessionState, connect, ensure_schema)
from strategies.grid.realtime.rules import parse_profile
from strategies.grid.signal_bot import GridSignalBot


@pytest.mark.asyncio
async def test_successful_non_numeric_send_marks_no_active_reference(
        tmp_path, profile_raw):
    conn = connect(str(tmp_path / "grid.sqlite3"), 1000)
    ensure_schema(conn)
    store = ExportStore(conn)
    sid = store.begin_session(100)
    bot = GridSignalBot.__new__(GridSignalBot)
    bot.export_store = store
    bot._state = SessionState(sid, 0, None, None)
    bot._send_notification = AsyncMock(return_value=True)
    signal = SimpleNamespace(grid_params=None)

    assert await bot._send_with_export(signal)
    bot._send_notification.assert_awaited_once_with(signal)
    assert ReferenceReader(conn, parse_profile(profile_raw)).current_snapshot(101)[1] \
        == "NO_ACTIVE_REFERENCE"


@pytest.mark.asyncio
async def test_failed_non_numeric_send_does_not_confirm_reference_state(
        tmp_path, profile_raw):
    conn = connect(str(tmp_path / "grid.sqlite3"), 1000)
    ensure_schema(conn)
    store = ExportStore(conn)
    sid = store.begin_session(100)
    bot = GridSignalBot.__new__(GridSignalBot)
    bot.export_store = store
    bot._state = SessionState(sid, 0, None, None)
    bot._send_notification = AsyncMock(return_value=False)

    assert not await bot._send_with_export(SimpleNamespace(grid_params=None))
    assert ReferenceReader(conn, parse_profile(profile_raw)).current_snapshot(101)[1] \
        == "MISSING"
