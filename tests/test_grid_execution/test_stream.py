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
