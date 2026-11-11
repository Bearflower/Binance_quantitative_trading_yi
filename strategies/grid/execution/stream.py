"""PM user data stream with explicit reconnect and REST reconciliation hooks."""

import asyncio
import json
import time
from typing import Awaitable, Callable, Optional

import aiohttp


def user_stream_url(mode: str, listen_key: str,
                    testnet_base: Optional[str] = None) -> str:
    if not isinstance(listen_key, str) or not listen_key.isalnum():
        raise ValueError("invalid user stream listen key")
    if mode == "live":
        base = "wss://fstream.binance.com/pm"
    elif mode == "testnet":
        if not testnet_base or not testnet_base.startswith("wss://"):
            raise ValueError("testnet WebSocket endpoint must be verified and explicit")
        base = testnet_base.rstrip("/")
    else:
        raise ValueError("user stream requires testnet or live mode")
    return base + "/ws/" + listen_key


class UserStream:
    def __init__(self, exchange, mode: str,
                 on_event: Callable[[dict], Awaitable[None]],
                 on_reconnect: Callable[[], Awaitable[None]],
                 on_disconnect: Callable[[], Awaitable[None]],
                 on_idle: Callable[[], Awaitable[None]],
                 *, testnet_base: Optional[str] = None):
        self.exchange = exchange
        self.mode = mode
        self.on_event = on_event
        self.on_reconnect = on_reconnect
        self.on_disconnect = on_disconnect
        self.on_idle = on_idle
        self.testnet_base = testnet_base
        self.ready = False

    async def run_forever(self) -> None:
        while True:
            try:
                key = await self.exchange.start_user_stream()
                url = user_stream_url(self.mode, key, self.testnet_base)
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, heartbeat=45) as socket:
                        await self.on_reconnect()
                        self.ready = True
                        connected_at = time.monotonic()
                        keepalive_at = connected_at + 30 * 60
                        while time.monotonic() - connected_at < 23 * 60 * 60:
                            if time.monotonic() >= keepalive_at:
                                renewed = await self.exchange.keepalive_user_stream()
                                if renewed != key:
                                    raise RuntimeError("listen key changed")
                                keepalive_at = time.monotonic() + 30 * 60
                            try:
                                message = await socket.receive(timeout=60)
                            except asyncio.TimeoutError:
                                # Account streams can legitimately be idle. A
                                # REST reconciliation establishes health.
                                await self.on_idle()
                                continue
                            if message.type == aiohttp.WSMsgType.TEXT:
                                event = json.loads(message.data)
                                if not isinstance(event, dict):
                                    raise ValueError("malformed user stream event")
                                if event.get("e") == "listenKeyExpired":
                                    raise RuntimeError("listen key expired")
                                await self.on_event(event)
                            elif message.type in (aiohttp.WSMsgType.CLOSED,
                                                  aiohttp.WSMsgType.CLOSE,
                                                  aiohttp.WSMsgType.ERROR):
                                raise RuntimeError("user stream disconnected")
            except asyncio.CancelledError:
                self.ready = False
                await self.on_disconnect()
                raise
            except Exception:
                self.ready = False
                await self.on_disconnect()
                await asyncio.sleep(5)
            else:
                self.ready = False
                await self.on_disconnect()
