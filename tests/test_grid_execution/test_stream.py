import pytest

from strategies.grid.execution.stream import user_stream_url


def test_live_stream_url_uses_pm_listen_key_path():
    assert user_stream_url("live", "abc123") == "wss://fstream.binance.com/pm/ws/abc123"


def test_testnet_requires_explicit_verified_endpoint():
    with pytest.raises(ValueError, match="testnet WebSocket"):
        user_stream_url("testnet", "abc123")
    assert user_stream_url("testnet", "abc123",
        testnet_base="wss://stream.binancefuture.com") == (
        "wss://stream.binancefuture.com/ws/abc123")


def test_rejects_malformed_listen_key():
    with pytest.raises(ValueError):
        user_stream_url("live", "bad/key")


@pytest.mark.asyncio
async def test_idle_control_frames_do_not_starve_listen_key_renewal(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from strategies.grid.execution import stream

    socket = MagicMock()
    socket.receive = AsyncMock()
    session = MagicMock()
    session.ws_connect.return_value.__aenter__.return_value = socket
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    monkeypatch.setattr(stream.aiohttp, 'ClientSession', factory)
    # Simulate a receive that never returns an account message because the
    # library keeps consuming heartbeats. Advance only the stream clock.
    now = [0]
    from types import SimpleNamespace
    monkeypatch.setattr(stream, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    async def idle_frame_wait(awaitable, timeout):
        awaitable.close()
        now[0] += timeout
        raise asyncio.TimeoutError
    monkeypatch.setattr(stream.asyncio, 'wait_for', idle_frame_wait)
    exchange = MagicMock()
    exchange.start_user_stream = AsyncMock(return_value='abc123')
    exchange.keepalive_user_stream = AsyncMock(side_effect=asyncio.CancelledError)
    idle = AsyncMock()
    client = stream.UserStream(exchange, 'live', AsyncMock(), AsyncMock(), AsyncMock(), idle)
    with pytest.raises(asyncio.CancelledError):
        await client.run_forever()
    assert idle.await_count == 30
    exchange.keepalive_user_stream.assert_awaited_once()


@pytest.mark.asyncio
async def test_websocket_close_logs_stage_and_code_without_private_text(monkeypatch, caplog):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from strategies.grid.execution import stream

    socket = MagicMock(close_code=1006)
    socket.receive = AsyncMock(return_value=stream.aiohttp.WSMessage(
        stream.aiohttp.WSMsgType.ERROR, RuntimeError("private-test-listen-key"), None))
    session = MagicMock()
    session.ws_connect.return_value.__aenter__.return_value = socket
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    monkeypatch.setattr(stream.aiohttp, "ClientSession", factory)
    exchange = MagicMock()
    exchange.start_user_stream = AsyncMock(return_value="PrivateTestListenKey")
    disconnect = AsyncMock(side_effect=asyncio.CancelledError)
    client = stream.UserStream(exchange, "live", AsyncMock(), AsyncMock(), disconnect, AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await client.run_forever()
    assert client.disconnect_stage == "websocket_error"
    assert "stage=websocket_error" in caplog.text
    assert "ws_close_code=1006" in caplog.text
    assert "private-test-listen-key" not in caplog.text
    assert "PrivateTestListenKey" not in caplog.text
